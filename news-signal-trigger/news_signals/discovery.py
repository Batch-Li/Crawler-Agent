"""新闻信号发现；消费外部新闻数据，通过注入的触发器对接外部 Agent。"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4
from pydantic import Field, field_validator
from ._core.bridge import MonitoringTool, alert_to_seed_event, item_to_evidence
from ._core.config_loader import load_monitor_config
from ._core.engine import MonitorEngine
from ._core.matching import match_subjects, contains_any
from ._core.models import MonitorConfig, MonitoredItem, parse_utc
from ._core.normalize import normalize_text, split_clauses
from .schema import Event, Evidence, SchemaModel
from .trigger import NewsSignalTrigger

class NewsProfile(SchemaModel):
    enterprise_id: str = Field(min_length=1)
    enterprise_name: str = Field(min_length=1)
    industry: str = Field(min_length=1)
    business_keywords: list[str] = Field(min_length=1)
    exclude_words: list[str] = Field(default_factory=list)
    require_published_at: bool = False
    max_llm_calls: int = Field(default=24, ge=0, le=500)

    @field_validator("business_keywords", "exclude_words")
    @classmethod
    def clean_words(cls, values):
        words = list(dict.fromkeys(word.strip() for word in values if word.strip()))
        if values and not words:
            raise ValueError("关键词不能全部为空")
        return words

class NewsDecision(SchemaModel):
    input_index: int
    evidence_id: str | None = None
    decision: Literal["kept", "dropped", "observed", "duplicate"]
    reason: str
    business_terms: list[str] = Field(default_factory=list)
    subject_ids: list[str] = Field(default_factory=list)

class NewsSignal(SchemaModel):
    schema_version: Literal["news_signal.v1"] = "news_signal.v1"
    signal_id: str
    signal_type: Literal["COMPETITOR_EVENT", "SELF_EVENT", "PUBLIC_OPINION", "INDUSTRY_EVENT"]
    enterprise_id: str
    enterprise_name: str
    industry: str
    subject_entity_id: str | None = None
    subject_name: str | None = None
    title: str
    event_types: list[str]
    score: float = Field(ge=0, allow_inf_nan=False)
    priority: Literal["yellow", "orange", "red"]
    trigger_reason: str
    business_terms: list[str]
    generated_at: datetime
    published_at: str | None = None
    flags: dict[str, bool]
    evidence: list[Evidence] = Field(min_length=1)
    seed_event: Event
    trigger_eligible: bool
    investigation_status: Literal["observed", "pending", "completed", "partial", "failed"]
    investigation_directory: str | None = None

def _write_json(path: Path, payload) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")

def content_item(raw: dict) -> dict:
    """接受采集 ContentItem 或标准 Evidence；不修改传入对象。"""
    if "evidenceId" in raw and "source" in raw:
        evidence = Evidence.model_validate(raw)
        return {
            "contentId": evidence.evidence_id, "title": evidence.title or "",
            "platform": evidence.source, "summary": evidence.snippet, "content": evidence.content,
            "url": evidence.url, "publishedAt": raw.get("publishedAt"),
            "dedupKey": evidence.dedup_key or evidence.evidence_id,
            "quality": {"hasFullContent": evidence.has_full_content, "warnings": evidence.quality_warnings},
            "ext": {"sourceName": evidence.source_name,
                    **({'publication': evidence.publication.model_dump(mode='json', by_alias=True)} if evidence.publication else {})},
        }
    item = MonitoringTool._flatten_content_item(deepcopy(raw))
    if not item.get("dedupKey"):
        item["dedupKey"] = item.get("contentId")
    return item

class NewsMonitorEngine(MonitorEngine):
    """沿用监测评分；同主体、规则和动作才进行近似标题归组。"""

    def _route_literal(self, entry, now):
        super()._route_literal(entry, now)
        for index, hit in enumerate(entry.fast_hits):
            if not hit.object_unverified:
                continue
            clause = entry.clauses[hit.clause_index]
            position = clause.find(hit.action)
            before = clause[max(0, position - self.config.settings.object_window_chars):position]
            # 新闻常用“某系列手机开启预约”，宾语在动作之前；不跨句借对象。
            word = contains_any(before, hit.family.object_words)
            if word and not contains_any(before, hit.family.notice_words):
                entry.fast_hits[index] = replace(hit, object_word=word, object_unverified=False)

    def run_tick(self, raw_items, now=None):
        self._news_samples = {}
        summary = super().run_tick(raw_items, now=now)
        state = self.store.load_state()
        for key, sample in self._news_samples.items():
            state.setdefault("groups", {}).setdefault(key, {}).setdefault("sampleTitle", sample)
        self.store.save_state(state)
        return summary

    def _resolve_group_key(self, entry, group_samples):
        scope = [sorted(h.entity.entity_id for h in entry.subject_hits),
                 sorted((h.family.id, h.action) for h in entry.fast_hits)]
        prefix = "newsgrp_" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()[:12] + "_"
        samples = {key: value for key, value in group_samples.items() if key.startswith(prefix)}
        original = entry.title_hash
        entry.title_hash = prefix + original
        try:
            key = super()._resolve_group_key(entry, samples)
        finally:
            entry.title_hash = original
        group_samples.update(samples)
        self._news_samples[key] = samples[key]
        return key

class NewsSignalDiscovery:
    """信号发现入口：外部新闻 → 筛选／评分／归组 → news_signal.v1。

    采集端负责新闻和原文时间；此模块保存信号，runner 交给触发器调用。
    """
    def __init__(self, config_path: str | Path, data_dir: str | Path, runner=None, publication_reader=None):
        self.config_path = Path(config_path)
        if self.config_path.is_dir():
            self.config_path /= "config.json"
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.profile = NewsProfile.model_validate(payload["profile"])
        overrides = payload["monitor"]
        if "entities" not in overrides:
            raise ValueError("新闻监测需显式配置 monitor.entities")
        config = MonitorConfig.model_validate({**load_monitor_config().model_dump(by_alias=True), **overrides})
        if not any(entity.enabled for entity in config.entities):
            raise ValueError("至少配置一个启用的企业或竞品主体")
        settings = config.settings
        if (not all(math.isfinite(v) for v in (settings.agent_trigger_score, settings.lookback_hours, settings.cooldown_hours))
                or settings.agent_max_runs_per_day < 0 or settings.agent_trigger_score < 0
                or settings.lookback_hours <= 0 or settings.cooldown_hours <= 0):
            raise ValueError("新闻监测阈值、有效期或每日预算不合法")
        if not 1 <= settings.agent_max_rounds <= 50 or not 1 <= settings.agent_max_tool_calls <= 500:
            raise ValueError("触发预算不合法")
        self.tool = MonitoringTool(data_dir=data_dir)
        self.tool.max_llm_calls = self.profile.max_llm_calls
        self.tool.config = config
        self.tool.engine = NewsMonitorEngine(self.tool.config, self.tool.store)
        self.trigger = NewsSignalTrigger(self.tool, runner)
        self.data_dir = Path(data_dir)
        self.publication_reader = publication_reader

    @contextmanager
    def _lock(self):
        path = self.data_dir / "news_monitor.lock"
        try:
            handle = path.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise RuntimeError("新闻监测目录正在使用；异常中断后需确认进程退出再清理锁文件") from exc
        try:
            with handle:
                handle.write(datetime.now(timezone.utc).isoformat())
            yield
        finally:
            path.unlink()

    def screen(self, items: list, now: datetime, *, check_time=True) -> tuple[list[dict], list[NewsDecision]]:
        accepted, decisions, seen = [], [], set()
        for index, raw in enumerate(items):
            item = None
            try:
                item = content_item(raw)
                monitored = MonitoredItem.model_validate(item)
                parsed_evidence = Evidence.model_validate(item_to_evidence(item))
                if not str(item.get("title") or "").strip():
                    raise ValueError("新闻标题为空")
            except (ValueError, TypeError, AttributeError):
                decisions.append(NewsDecision(input_index=index, decision="dropped", reason="条目缺少有效标识、来源或标题"))
                continue
            decision = dict(input_index=index, evidence_id=item["contentId"])
            published = parsed_evidence.published_at
            calendar_date = (parsed_evidence.published_calendar_date if parsed_evidence.publication and
                             parsed_evidence.publication.status == 'verified' else None)
            confidence = monitored.published_at_confidence
            if check_time and self.profile.require_published_at and (parsed_evidence.publication is None or
                    parsed_evidence.publication.status != 'verified' or not calendar_date):
                decisions.append(NewsDecision(**decision, decision="observed", reason="缺少可靠发布时间，不自动调查"))
                continue
            if check_time and published and (confidence is None or confidence >= .5):
                age = (now - published).total_seconds() / 3600
                if age > self.tool.config.settings.lookback_hours or age < -5 / 60:
                    decisions.append(NewsDecision(**decision, decision="dropped", reason="报道超出监测时间窗或时间在未来"))
                    continue
            elif check_time and calendar_date:
                # 原文只给日期或未标时区时按日历日筛选，不伪造绝对时刻。
                age_days = (now.date() - calendar_date).days
                if age_days < 0 or age_days > self.tool.config.settings.lookback_hours / 24:
                    decisions.append(NewsDecision(**decision, decision="dropped", reason="报道超出监测日期窗口"))
                    continue
            if item["dedupKey"] in seen:
                decisions.append(NewsDecision(**decision, decision="duplicate", reason="本轮重复条目"))
                continue
            text = normalize_text(" ".join(str(item.get(k) or "") for k in ("title", "summary", "content")))
            excluded = next((w for w in self.profile.exclude_words if normalize_text(w) in text), None)
            if excluded:
                decisions.append(NewsDecision(**decision, decision="dropped", reason=f"命中排除词：{excluded}"))
                continue
            hits = match_subjects(split_clauses(text), self.tool.config.entities)
            terms = list(dict.fromkeys(word for word in self.profile.business_keywords
                                      if any(normalize_text(word) in hit.clause_text for hit in hits)))
            if not hits or not terms:
                observed = bool(hits or any(normalize_text(w) in text for w in self.profile.business_keywords))
                decisions.append(NewsDecision(**decision, decision="observed" if observed else "dropped",
                                              reason="主体与企业业务缺少同句关联" if observed else "无目标主体及业务关联"))
                continue
            seen.add(item["dedupKey"])
            accepted.append(item)
            decisions.append(NewsDecision(**decision, decision="kept", reason="配置主体与企业业务同句相关",
                                          business_terms=terms, subject_ids=[h.entity.entity_id for h in hits]))
        return accepted, decisions

    def run(self, items: list | None = None, *, now: datetime | None = None,
            fetcher: Callable | None = None, collection_mode: str = "saved_news") -> dict:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("监测时间必须带时区")
        now = now.astimezone(timezone.utc)
        with self._lock():
            state = self.tool.store.load_state()
            if state.get("newsEnterpriseId") not in (None, self.profile.enterprise_id):
                raise ValueError("更换目标企业请使用新的监测目录")
            output = self.data_dir / "ticks" / (now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8])
            output.mkdir(parents=True, exist_ok=False)
            collection = {}
            if fetcher is not None:
                items, collection = fetcher(output, int(state.get("tickCount", 0)))
            if not isinstance(items, list):
                raise ValueError("新闻输入必须是列表")
            _write_json(output / "input.json", items)
            screened_items = items
            if self.publication_reader is not None:
                candidates, _ = self.screen(items, now, check_time=False)
                # 新闻原文优先于检索包装链接和商品介绍，避免读后者耗尽预算。
                candidates.sort(key=lambda item: ('article_url_is_sogou_wrapped' in item.get('quality', {}).get('warnings', []),
                    item.get('platform') not in ('other', 'south_weekend'), item.get('publishedAt') is None))
                refreshed = self.publication_reader.read_items(candidates, output / 'article_responses.jsonl')
                by_id = {item['contentId']: item for item in refreshed}
                screened_items = [by_id.get(raw.get('contentId'), raw) if isinstance(raw, dict) else raw for raw in items]
                collection['publication'] = self.publication_reader.summary()
            accepted, decisions = self.screen(screened_items, now)
            _write_json(output / "screening.json", [d.model_dump(mode="json", by_alias=True) for d in decisions])
            # 引擎消费摘要与正文的合并视图；原始 ContentItem 单独保留，不用缩略告警代替证据。
            views = [{**item, "summary": " ".join(str(item.get(k) or "") for k in ("summary", "content")),
                      "publishedAt": (Evidence.model_validate(item_to_evidence(item)).published_at.isoformat()
                                      if Evidence.model_validate(item_to_evidence(item)).published_at else None)}
                     for item in accepted]
            monitor = self.tool.engine.run_tick(views, now=now)
            alerts = self._signals(monitor.pop("alerts"), accepted, decisions)
            self.tool.store.append_alerts(alerts)
            state = self.tool.store.load_state()
            state["newsEnterpriseId"] = self.profile.enterprise_id
            self.tool.store.save_state(state)
            settings = self.tool.config.settings
            self.trigger.current_time = now
            prior_dispatches = set(self.trigger.discoveries_dir.glob("*.json"))
            trigger = (self.trigger.run_due_triggers(now).to_payload() if settings.agent_auto_trigger
                       else {"due": len(self.trigger.due_alerts()), "dispatched": 0, "skippedReason": "auto_trigger_disabled", "failed": 0, "partial": 0})
            if self.publication_reader is not None:
                collection['publication'] = self.publication_reader.summary()
            signals = [self._signal_payload(alert) for alert in alerts]
            _write_json(output / "signals.json", signals)
            investigations = []
            for path in sorted(set(self.trigger.discoveries_dir.glob("*.json")) - prior_dispatches):
                dispatched = json.loads(path.read_text(encoding="utf-8"))
                investigations.append({"signalId": path.stem, "executionStatus": dispatched["executionStatus"],
                                       "dispatchResult": str(path.resolve()),
                                       "llmCalls": dispatched["result"].get("llmCalls", 0)})
            summary = {
                "schemaVersion": "news_monitor.v1", "generatedAt": now.isoformat(),
                "enterpriseId": self.profile.enterprise_id, "collectionMode": collection_mode,
                "inputItems": len(items), "keptItems": len(accepted),
                "screeningCounts": {kind: sum(d.decision == kind for d in decisions)
                                    for kind in ("kept", "dropped", "observed", "duplicate")},
                "signalsCreated": len(signals), "agentTrigger": trigger,
                "investigations": investigations,
                "monitor": monitor, "collection": collection,
                "pendingEligible": len(self.trigger.due_alerts()),
                "outputDirectory": str(output.resolve()), "dataDirectory": str(self.data_dir.resolve()),
            }
            _write_json(output / "summary.json", summary)
            return summary

    def _signals(self, alerts: list[dict], items: list[dict], decisions: list[NewsDecision]) -> list[dict]:
        originals = {item["contentId"]: item for item in items}
        terms = {d.evidence_id: d.business_terms for d in decisions if d.decision == "kept"}
        prior_ids = {alert.get("alertId") for alert in self.tool.store.load_alerts()}
        result = []
        for alert in alerts:
            evidence = {entry["contentId"]: originals[entry["contentId"]]
                        for entry in alert["evidence"] if entry.get("contentId") in originals}
            if not evidence:
                continue
            alert["evidence"] = list(evidence.values())
            business = sorted({term for key in evidence for term in terms.get(key, [])})
            content = sorted({normalize_text(" ".join(str(item.get(k) or "") for k in ("title", "summary", "content")))
                              for item in evidence.values()})
            identity = [self.profile.enterprise_id, alert.get("subjectEntityId"), alert["matchedRule"],
                        alert["matchedSpans"].get("titleHash"), content]
            alert["alertId"] = "news_" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
            if alert["alertId"] in prior_ids:
                continue
            prior_ids.add(alert["alertId"])
            alert["signalSchema"] = "news_signal.v1"
            alert["businessTerms"] = business
            alert["why"] += "；与企业业务相关：" + "、".join(business)
            alert["triggerEligible"] = bool(business and alert.get("subjectEntityId")
                                            and not alert.get("flags", {}).get("objectUnverified")
                                            and alert["score"] >= self.trigger.trigger_score)
            alert["status"] = "pending" if alert["triggerEligible"] else "observed"
            result.append(alert)
        return result

    def _signal_payload(self, alert: dict) -> dict:
        seed = Event.model_validate(alert_to_seed_event(alert))
        subject = next((e for e in self.tool.config.entities if e.entity_id == alert.get("subjectEntityId")), None)
        signal_type = ("PUBLIC_OPINION" if "PUBLIC_OPINION" in alert["eventTypes"] else
                       "SELF_EVENT" if subject and subject.type == "self" else
                       "COMPETITOR_EVENT" if subject and subject.type == "competitor" else "INDUSTRY_EVENT")
        path = self.trigger.discoveries_dir / (alert["alertId"] + ".json")
        status = "pending" if alert["triggerEligible"] else "observed"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            status = payload["executionStatus"]
        directory = self.data_dir / "investigations" / alert["alertId"]
        return NewsSignal(
            signal_id=alert["alertId"], signal_type=signal_type,
            enterprise_id=self.profile.enterprise_id, enterprise_name=self.profile.enterprise_name,
            industry=self.profile.industry, subject_entity_id=alert.get("subjectEntityId"),
            subject_name=alert.get("subjectName"), title=seed.title, event_types=alert["eventTypes"],
            score=alert["score"], priority=alert["priority"], trigger_reason=alert["why"],
            business_terms=alert["businessTerms"], generated_at=alert["generatedAt"], published_at=alert.get("publishedAt"),
            flags=alert["flags"], evidence=seed.evidence, seed_event=seed, trigger_eligible=alert["triggerEligible"],
            investigation_status=status, investigation_directory=str(directory.resolve()) if directory.exists() else None,
        ).model_dump(mode="json", by_alias=True)
