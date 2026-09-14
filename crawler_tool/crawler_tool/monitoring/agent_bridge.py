"""Agent 适配桥：监测层与 DISCOVERY_WORKFLOW 的全部接缝。

三个组件：
- item_to_evidence / alert_to_seed_event：ContentItem/告警 → 工作流契约（Evidence/seedEvent）；
- SearchContentCollectionTool：工作流 CollectionTool 协议的 crawler_tool 实现（补上"尚未接入"项）；
- MonitoringTool：给 agent 的稳定门面——ingest / pending_alerts / 状态回写 / 观察箱。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx

from crawler_tool.monitoring.config_loader import load_monitor_config
from crawler_tool.monitoring.engine import MonitorEngine
from crawler_tool.monitoring.models import MonitoredItem
from crawler_tool.monitoring.store import MonitorStore
from crawler_tool.monitoring.triage import TriageClient

# ---- 工作流契约转换 ----


def item_to_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """ContentItem 风格字典 → DISCOVERY_WORKFLOW 的 Evidence。"""
    dedup = item.get("dedup") if isinstance(item.get("dedup"), dict) else {}
    quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
    ext = item.get("ext") if isinstance(item.get("ext"), dict) else {}
    return {
        "evidenceId": str(item.get("contentId") or item.get("dedupKey") or dedup.get("dedupKey") or "").strip(),
        "title": str(item.get("title") or ""),
        "source": str(item.get("platform") or ""),
        "url": item.get("url"),
        "publishedAt": item.get("publishedAt"),
        "publishedAtConfidence": quality.get("publishedAtConfidence"),
        "publishedAtRaw": ext.get("publishedAtRaw"),
        "snippet": item.get("summary") or (item.get("content") if quality.get("hasFullContent") is False else None),
        "content": None if quality.get("hasFullContent") is False else item.get("content"),
        "dedupKey": item.get("dedupKey") or dedup.get("dedupKey"),
        "hasFullContent": quality.get("hasFullContent"),
        "sourceName": ext.get("sourceName"),
        "qualityWarnings": quality.get("warnings") or [],
    }


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


# ---- CollectionTool 协议实现 ----


class SearchRequest(dict):
    """兼容工作流 §5.1 的请求结构（query/industry/roundIndex/limit）。"""


class SearchContentCollectionTool:
    """把 crawler_tool 的统一搜索包装成工作流的 CollectionTool。

    - 默认走本机 HTTP API（--serve 常驻）；
    - 测试/进程内可注入 search_fn(query, limit) -> {"items": [...]}。
    """

    name = "crawler_tool_search_content"

    def __init__(
        self,
        search_fn: Callable[[str, int], dict[str, Any]] | None = None,
        base_url: str = "http://127.0.0.1:8301",
        platforms: list[str] | None = None,
        timeout: float = 30.0,
        enrich_time: bool = False,
        enrich_content: bool = False,
    ) -> None:
        self._search_fn = search_fn
        self._base_url = base_url.rstrip("/")
        self._platforms = platforms
        self._timeout = timeout
        # 有边界详情页补全：随每次 search 请求显式声明，由采集服务侧限预算。
        self._enrich_time = enrich_time
        self._enrich_content = enrich_content

    def search(self, request: Any) -> list[dict[str, Any]]:
        return self.search_with_reports(request)["evidence"]

    def search_with_reports(self, request: Any) -> dict[str, Any]:
        """保留来源状态供 Agent 审计；search() 继续兼容旧的列表接口。"""
        query = str(request.get("query") if isinstance(request, dict) else getattr(request, "query", ""))
        limit = int((request.get("limit") if isinstance(request, dict) else getattr(request, "limit", 20)) or 20)
        payload = self._invoke(query, limit)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError("search-content 响应必须包含 items 列表")
        items = payload["items"]
        # 缺少可追溯标识的条目不进 Evidence（证据必须可回溯）。
        evidence = [
            item_to_evidence(entry)
            for entry in items
            if isinstance(entry, dict) and item_to_evidence(entry)["evidenceId"].strip()
        ]
        return {
            "evidence": evidence,
            "status": payload.get("status", "success"),
            "partial": payload.get("partial", False),
            "sourceReports": payload.get("sourceReports", []),
            "droppedCount": len(items) - len(evidence),
        }

    def _invoke(self, query: str, limit: int) -> dict[str, Any]:
        if self._search_fn is not None:
            return self._search_fn(query, limit)
        body: dict[str, Any] = {"query": query, "limit": limit, "freshness": "prefer_fresh"}
        if self._platforms:
            body["platforms"] = self._platforms
        if self._enrich_time:
            body["enrichTime"] = True
        if self._enrich_content:
            body["enrichContent"] = True
        response = httpx.post(
            f"{self._base_url}/api/v1/tool/search-content",
            json=body,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()


# ---- Agent 门面 ----


class MonitoringTool:
    """给 agent 的进程内监测门面：喂条目 → 拿告警 → 起发现循环。

    用法（agent 侧）：
        tool = MonitoringTool(config_dir="config/monitoring", triage_client=my_llm)
        summary = tool.ingest(crawl_items)          # 每轮爬取后调用
        for alert in tool.pending_alerts():          # 取 pending 告警
            seed = alert_to_seed_event(alert)        # → EVENT_EXPANSION
        tool.update_alert_status(alert["alertId"], "processing")
    """

    def __init__(
        self,
        config_dir: str | Path | None = None,
        data_dir: str | Path = "data/monitoring",
        triage_client: TriageClient | None = None,
    ) -> None:
        self.config = load_monitor_config(config_dir)
        self.store = MonitorStore(data_dir)
        self.engine = MonitorEngine(self.config, self.store, triage_client=triage_client)

    @staticmethod
    def _flatten_content_item(entry: dict[str, Any]) -> dict[str, Any]:
        """真实 ContentItem 的 dedupKey 在嵌套 dedup 对象里——展平成 MonitoredItem 形态。"""
        flat = dict(entry)
        if not flat.get("dedupKey"):
            dedup = flat.get("dedup") if isinstance(flat.get("dedup"), dict) else {}
            if dedup.get("dedupKey"):
                flat["dedupKey"] = dedup["dedupKey"]
        return flat

    def ingest(self, items: list[dict[str, Any]], now: Any = None) -> dict[str, Any]:
        """喂入一批爬取条目（ContentItem 风格字典），返回 tick 摘要并落盘告警。"""
        normalized: list[dict[str, Any]] = []
        skipped = 0
        for entry in items:
            if isinstance(entry, MonitoredItem):
                normalized.append(entry.model_dump(by_alias=True))
                continue
            if not isinstance(entry, dict):
                skipped += 1
                continue
            candidate = self._flatten_content_item(entry)
            try:
                MonitoredItem(**candidate)  # 提前校验，坏条目不进引擎
            except Exception:
                skipped += 1
                continue
            normalized.append(candidate)
        summary = self.engine.run_tick(normalized, now=now)
        alerts = summary.pop("alerts")
        self.store.append_alerts(alerts)
        summary["alertsWritten"] = len(alerts)
        # 静默吞条目曾让"爬到 19 条、处理 0 条"藏了一整轮——丢弃必须留痕。
        summary["skippedInvalid"] = skipped
        return summary

    def pending_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.pending_alerts(limit)

    def update_alert_status(self, alert_id: str, status: str, note: str = "") -> bool:
        """status: pending | processing | accepted(tp) | rejected(fp) | archived。"""
        return self.store.update_alert_status(alert_id, status, note)

    def observation_report(self, limit: int = 200) -> list[dict[str, Any]]:
        """观察箱：未识别主体提名、行业通道未决、分诊不可用留档。"""
        return self.store.load_observation(limit)

    def alerts_file(self) -> Path:
        return self.store.alerts_path
