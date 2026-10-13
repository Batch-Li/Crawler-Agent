"""信号字段与种子事件适配；没有采集或调查实现。"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from .store import MonitorStore
from .config_loader import load_monitor_config

def item_to_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """ContentItem 风格字典 → DISCOVERY_WORKFLOW 的 Evidence。"""
    dedup = item.get("dedup") if isinstance(item.get("dedup"), dict) else {}
    quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
    ext = item.get("ext") if isinstance(item.get("ext"), dict) else {}
    evidence = {
        "evidenceId": str(item.get("contentId") or item.get("dedupKey") or dedup.get("dedupKey") or "").strip(),
        "title": str(item.get("title") or ""),
        "source": str(item.get("platform") or ""),
        "url": item.get("url"),
        "publishedAt": item.get("publishedAt"),
        "snippet": item.get("summary") or (item.get("content") if quality.get("hasFullContent") is False else None),
        "content": None if quality.get("hasFullContent") is False else item.get("content"),
        "dedupKey": item.get("dedupKey") or dedup.get("dedupKey"),
        "hasFullContent": quality.get("hasFullContent"),
        "sourceName": ext.get("sourceName") or ext.get("accountName"),
        "qualityWarnings": quality.get("warnings") or [],
    }
    if isinstance(ext.get("publication"), dict):
        evidence["publication"] = ext["publication"]
        value = ext["publication"].get("value")
        if value:
            evidence["publishedCalendarDate"] = value[:10]
    return evidence

def alert_to_seed_event(alert: dict[str, Any]) -> dict[str, Any]:
    """告警 → EVENT_EXPANSION 的 seedEvent（DISCOVERY_WORKFLOW §6.2 形态）。"""
    # 同一内容可能因多关键词命中而重复进入告警；种子只保留一个引用。
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for entry in alert.get("evidence", []):
        mapped = item_to_evidence(entry)
        previous = evidence_by_id.get(mapped["evidenceId"], {})
        evidence_by_id[mapped["evidenceId"]] = {
            **mapped, **previous,
            **{key: value for key, value in mapped.items() if value is not None and value != ""},
        }
    evidence = list(evidence_by_id.values())
    primary = alert.get("evidence", [{}])[0] if alert.get("evidence") else {}
    subject_name = alert.get("subjectName")
    return {
        "eventId": str(alert.get("alertId") or ""),
        "title": str(primary.get("title") or alert.get("why") or ""),
        "summary": str(alert.get("why") or primary.get("title") or ""),
        "eventTypes": list(alert.get("eventTypes") or []),
        "entities": [subject_name] if subject_name else [],
        "evidence": evidence,
        "alertMeta": {
            "priority": alert.get("priority"),
            "score": alert.get("score"),
            "matchedRule": alert.get("matchedRule"),
            "platforms": (alert.get("resonance") or {}).get("platforms", []),
        },
    }

class MonitoringTool:
    def __init__(self, data_dir):
        self.store = MonitorStore(data_dir)
        self.config = load_monitor_config()

    @staticmethod
    def _flatten_content_item(entry: dict[str, Any]) -> dict[str, Any]:
        """真实 ContentItem 的 dedupKey 在嵌套 dedup 对象里——展平成 MonitoredItem 形态。"""
        flat = dict(entry)
        if not flat.get("dedupKey"):
            dedup = flat.get("dedup") if isinstance(flat.get("dedup"), dict) else {}
            if dedup.get("dedupKey"):
                flat["dedupKey"] = dedup["dedupKey"]
        return flat

    def update_alert_status(self, alert_id, status, note=""):
        return self.store.update_alert_status(alert_id, status, note)
