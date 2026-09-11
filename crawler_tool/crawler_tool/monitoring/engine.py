"""监测引擎：一次 tick = 一批条目 → 漏斗 → 分级告警 + 状态回写。

流程（MONITORING_DESIGN.md）：
  预处理(归一化/子句/新鲜度门槛)
    → 字面主体通道（动作词快车道 + 宾语窗口 + 族级排除词）
    → 慢车道（主体命中无动作词 → LLM 分诊，可降级）
    → 行业入围通道（标题含行业词但无主体命中 → LLM 批判，可降级）
    → 高危旁路（未识别主体 + 负面/重大动作词 → 日报头条）
    → 事件组合并（近似标题 → 跨来源共振）
    → 压制门槛（dedupKey 冷却 / 事件组硬窗口 / 进展更新）
    → 打分分级（灰度协议可强制全黄）
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from crawler_tool.monitoring.matching import (
    ActionHit,
    SubjectHit,
    contains_any,
    find_action_hit,
    match_subjects,
)
from crawler_tool.monitoring.models import (
    EVENT_TYPES,
    Alert,
    MonitorConfig,
    MonitoredItem,
    RuleFamily,
    parse_utc,
)
from crawler_tool.monitoring.normalize import (
    normalize_text,
    split_clauses,
    title_fingerprint,
    titles_similar,
)
from crawler_tool.monitoring.store import MonitorStore
from crawler_tool.monitoring.triage import (
    DisabledTriage,
    TriageClient,
    TriageVerdict,
    build_triage_prompt,
    parse_triage_response,
)

EVENT_TYPE_TO_FAMILY = {
    "PRODUCT_LAUNCH": "competitor_launch",
    "PRODUCT_CHANGE": "product_change",
    "FINANCING": "financing",
    "POLICY": "policy",
    "PUBLIC_OPINION": "public_opinion",
    "RECRUITMENT": "recruitment",
}
DEFAULT_EVENT_WEIGHTS = {
    "PRODUCT_LAUNCH": 2.0,
    "PRODUCT_CHANGE": 2.0,
    "FINANCING": 1.8,
    "POLICY": 1.5,
    "PUBLIC_OPINION": 1.5,
    "RECRUITMENT": 1.2,
}


@dataclass
class _Entry:
    """单条目的一次评估现场。"""
    item: MonitoredItem
    norm_title: str
    norm_text: str
    clauses: list[str]
    title_hash: str
    first_seen: datetime
    effective_time: datetime
    decay: float
    time_fallback: bool
    subject_hits: list[SubjectHit] = field(default_factory=list)
    negative_word: str | None = None
    industry_word: str | None = None
    fast_hits: list[ActionHit] = field(default_factory=list)
    slow_eligible: bool = False  # 主体命中但无确认动作词
    route: str = "dropped"  # fast | slow | industry | high_risk | dropped
    drop_reason: str | None = None
    time_inherited: bool = False  # 发布时间继承自同事件组报道
    published_at_override: datetime | None = None


@dataclass
class _Candidate:
    entry: _Entry
    family: RuleFamily | None
    subject: SubjectHit | None
    action: ActionHit | None
    via: str  # fast_lane | triage | high_risk
    base_score: float
    confidence_factor: float = 1.0
    why: str = ""
    flags: dict[str, bool] = field(default_factory=dict)


class MonitorEngine:
    def __init__(
        self,
        config: MonitorConfig,
        store: MonitorStore,
        triage_client: TriageClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.triage_client = triage_client if triage_client is not None else DisabledTriage()
        self.triage_enabled = triage_client is not None and getattr(triage_client, "available", True)

    # ---- 对外入口 ----

    def run_tick(self, raw_items: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        state = self.store.load_state()
        entries = self._evaluate_items(raw_items, state, now)
        candidates = self._collect_candidates(entries, now)
        alerts, suppressed, observation = self._finalize(entries, candidates, state, now)
        # 观察箱必须真正落盘——只计数不写文件曾让 --report 永远看到空箱。
        self.store.append_observation(observation)
        self._update_state(entries, alerts, state, now)

        summary = {
            "generatedAt": now.isoformat(),
            "processed": len(entries),
            "routes": self._count_routes(entries),
            "suppressed": suppressed,
            "alerts": [alert.to_payload() for alert in alerts],
            "observationAdded": len(observation),
            "degradedTriage": not self.triage_enabled,
            "tickCount": int(state.get("tickCount", 0)),
        }
        return summary

    # ---- 第 0 步：预处理 + 字面通道 ----

    def _evaluate_items(self, raw_items: list[dict[str, Any]], state: dict, now: datetime) -> list[_Entry]:
        settings = self.config.settings
        entries: list[_Entry] = []
        for raw in raw_items:
            try:
                item = raw if isinstance(raw, MonitoredItem) else MonitoredItem(**raw)
            except Exception:
                continue
            norm_title = normalize_text(item.title)
            norm_text = normalize_text(" ".join(part for part in (item.title, item.summary or "") if part))
            clauses = split_clauses(norm_text)

            previous = (state.get("items") or {}).get(item.dedup_key) or {}
            first_seen = (
                parse_utc(item.first_seen_at)
                or parse_utc(previous.get("firstSeenAt"))
                or now
            )

            # 新鲜度门槛：publishedAt 可靠则用之，否则 firstSeen 兜底（不拒之门外）。
            published = parse_utc(item.published_at)
            confidence = item.published_at_confidence
            time_fallback = False
            effective = published if (published and (confidence is None or confidence >= 0.5)) else None
            if effective is None:
                effective = first_seen
                time_fallback = True
            delta_hours = max(0.0, (now - effective).total_seconds() / 3600.0)
            if delta_hours > settings.lookback_hours:
                entries.append(self._dropped_entry(item, norm_title, norm_text, clauses, first_seen, now, "stale"))
                continue
            decay = math.exp(-delta_hours / max(settings.tau_hours, 0.1))

            entry = _Entry(
                item=item,
                norm_title=norm_title,
                norm_text=norm_text,
                clauses=clauses,
                title_hash=title_fingerprint(norm_title),
                first_seen=first_seen,
                effective_time=effective,
                decay=decay,
                time_fallback=time_fallback,
            )
            entry.negative_word = contains_any(norm_text, self.config.negative_words)
            entry.subject_hits = match_subjects(clauses, self.config.entities)
            self._route_literal(entry, now)
            entries.append(entry)
        return entries

    def _dropped_entry(self, item: MonitoredItem, norm_title: str, norm_text: str, clauses: list[str],
                       first_seen: datetime, now: datetime, reason: str) -> _Entry:
        entry = _Entry(
            item=item,
            norm_title=norm_title,
            norm_text=norm_text,
            clauses=clauses,
            title_hash=title_fingerprint(norm_title),
            first_seen=first_seen,
            effective_time=now,
            decay=0.0,
            time_fallback=False,
        )
        entry.route = "dropped"
        entry.drop_reason = reason
        return entry

    def _route_literal(self, entry: _Entry, now: datetime) -> None:
        settings = self.config.settings
        if entry.subject_hits:
            confirmed: list[ActionHit] = []
            for hit in entry.subject_hits:
                for family in self.config.families:
                    if hit.entity.type not in family.applies_to:
                        continue
                    if contains_any(entry.norm_text, family.exclude_words):
                        continue  # 族级排除词（"招聘"对 launch 族等）
                    action_hit = find_action_hit(entry.clauses, hit, family, settings.object_window_chars)
                    if action_hit is not None:
                        confirmed.append(action_hit)
            if confirmed:
                entry.fast_hits = confirmed
                entry.route = "fast"
                return
            entry.slow_eligible = True
            entry.route = "slow"
            return
        entry.industry_word = contains_any(entry.norm_title, self.config.industry_words)
        if entry.industry_word:
            entry.route = "industry"
            return
        # 高危旁路：未识别主体 + 标题含负面/重大动作词 → 人眼兜底，绝不静默。
        if contains_any(entry.norm_title, self.config.major_action_words) or (
            entry.negative_word and entry.negative_word in normalize_text(entry.item.title)
        ):
            entry.route = "high_risk"
            return
        entry.route = "dropped"
        entry.drop_reason = "noMatch"

    # ---- 候选生成（含 LLM 分诊） ----

    def _collect_candidates(self, entries: list[_Entry], now: datetime) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for entry in entries:
            if entry.route != "fast":
                continue
            for action_hit in entry.fast_hits:
                subject = self._subject_for_family(entry, action_hit.family)
                candidates.append(self._fast_candidate(entry, action_hit, subject))

        slow_entries = [e for e in entries if e.route == "slow"]
        industry_entries = [e for e in entries if e.route == "industry"]
        queue = self._topk(slow_entries, settings_key="slow_lane_per_tick") + \
            self._topk(industry_entries, settings_key="industry_channel_per_tick")
        verdicts = self._run_triage(queue)

        for entry, verdict in zip(queue, verdicts):
            candidate = self._candidate_from_verdict(entry, verdict)
            if candidate is not None:
                candidates.append(candidate)

        for entry in entries:
            if entry.route == "high_risk":
                candidates.append(self._high_risk_candidate(entry))
        return candidates

    def _resolve_group_key(self, entry: _Entry, group_samples: dict[str, str]) -> str:
        """精确指纹命中直接归组；否则按近似标题归并到已知事件组。"""
        settings = self.config.settings
        if entry.title_hash in group_samples:
            return entry.title_hash
        for ghash, sample in group_samples.items():
            if titles_similar(
                entry.norm_title,
                sample,
                min_overlap=settings.cluster_overlap_threshold,
                min_shared=settings.cluster_min_shared_bigrams,
            ):
                return ghash
        group_samples[entry.title_hash] = entry.norm_title
        return entry.title_hash

    def _subject_for_family(self, entry: _Entry, family: RuleFamily) -> SubjectHit | None:
        for hit in entry.subject_hits:
            if hit.entity.type in family.applies_to:
                return hit
        return entry.subject_hits[0] if entry.subject_hits else None

    def _fast_candidate(self, entry: _Entry, action_hit: ActionHit, subject: SubjectHit | None) -> _Candidate:
        weight = action_hit.family.weight_for(subject.entity.type) if subject else action_hit.family.weight
        strength = subject.strength if subject else 1.0
        sentiment = self.config.settings.negative_sentiment_factor if entry.negative_word else 1.0
        credibility = self.config.credibility_for(entry.item.platform)
        base = weight * strength * sentiment * entry.decay * credibility
        parts = [f"规则[{action_hit.family.name}]", f"主体“{subject.surface}”" if subject else None,
                 f"动作“{action_hit.action}”"]
        if action_hit.object_word:
            parts.append(f"宾语“{action_hit.object_word}”")
        why = "，".join(part for part in parts if part)
        return _Candidate(
            entry=entry,
            family=action_hit.family,
            subject=subject,
            action=action_hit,
            via="fast_lane",
            base_score=base,
            why=why,
            flags={"objectUnverified": action_hit.object_unverified},
        )

    def _candidate_from_verdict(self, entry: _Entry, verdict: TriageVerdict | None) -> _Candidate | None:
        if verdict is None or not verdict.is_event or not verdict.event_types:
            return None
        event_type = verdict.event_types[0]
        family = self.config.family_by_id(EVENT_TYPE_TO_FAMILY.get(event_type, ""))
        if family is not None and entry.subject_hits:
            subject = self._subject_for_family(entry, family)
        else:
            subject = entry.subject_hits[0] if entry.subject_hits else None
        if family is not None and subject is not None and subject.entity.type not in family.applies_to:
            weight = family.weight_for(subject.entity.type)
        elif family is not None:
            weight = family.weight
        else:
            weight = DEFAULT_EVENT_WEIGHTS.get(event_type, 1.5)
        strength = subject.strength if subject else 0.8
        sentiment = self.config.settings.negative_sentiment_factor if entry.negative_word else 1.0
        credibility = self.config.credibility_for(entry.item.platform)
        confidence_factor = 0.5 + 0.5 * verdict.confidence
        base = weight * strength * sentiment * entry.decay * credibility * confidence_factor
        why = f"分诊判定[{event_type} 置信{verdict.confidence:.2f}]：{verdict.why}"
        return _Candidate(
            entry=entry,
            family=family,
            subject=subject,
            action=None,
            via="triage",
            base_score=base,
            confidence_factor=confidence_factor,
            why=why,
            flags={},
        )

    def _high_risk_candidate(self, entry: _Entry) -> _Candidate:
        word = entry.negative_word or contains_any(entry.norm_title, self.config.major_action_words) or ""
        return _Candidate(
            entry=entry,
            family=None,
            subject=None,
            action=None,
            via="high_risk",
            base_score=0.0,
            why=f"未识别主体命中敏感词“{word}”，待人工判读",
            flags={"unidentifiedSubject": True},
        )

    def _run_triage(self, queue: list[_Entry]) -> list[TriageVerdict | None]:
        if not queue:
            return []
        if not self.triage_enabled:
            return [None] * len(queue)
        verdicts: list[TriageVerdict | None] = []
        batch_size = max(1, self.config.settings.triage_batch_size)
        for start in range(0, len(queue), batch_size):
            batch = queue[start: start + batch_size]
            entries_payload = [
                {
                    "index": position,
                    "title": entry.item.title,
                    "summary": entry.item.summary or "",
                    "subjectHint": entry.industry_word or "、".join(hit.surface for hit in entry.subject_hits),
                }
                for position, entry in enumerate(batch)
            ]
            try:
                raw = self.triage_client.complete(build_triage_prompt(entries_payload))
            except Exception:
                verdicts.extend([None] * len(batch))
                continue
            verdicts.extend(parse_triage_response(raw, len(batch)))
        return verdicts

    def _topk(self, entries: list[_Entry], settings_key: str) -> list[_Entry]:
        limit = getattr(self.config.settings, settings_key)
        def rank(entry: _Entry) -> tuple[float, float]:
            strength = max((hit.strength for hit in entry.subject_hits), default=0.5)
            return (strength, entry.decay)
        return sorted(entries, key=rank, reverse=True)[:limit]

    # ---- 合并、压制、打分分级 ----

    def _finalize(
        self,
        entries: list[_Entry],
        candidates: list[_Candidate],
        state: dict,
        now: datetime,
    ) -> tuple[list[Alert], dict[str, int], list[dict[str, Any]]]:
        settings = self.config.settings
        suppressed = {"cooldown": 0, "groupWindow": 0, "duplicateTitle": 0}
        observation: list[dict[str, Any]] = []
        alerts: list[Alert] = []
        seq = 0

        # 高危旁路独立成告警（不参与组合并打分，永远 yellow 进日报）。
        for candidate in [c for c in candidates if c.via == "high_risk"]:
            seq += 1
            alerts.append(self._high_risk_alert(candidate, now, seq))
            observation.append(self._observation_entry(candidate.entry, reason="high_risk_bypass"))

        # —— 事件组解析：精确指纹优先，近似标题归并（同故事不同措辞并入同组）——
        group_samples: dict[str, str] = {
            ghash: str(gstate.get("sampleTitle"))
            for ghash, gstate in (state.get("groups") or {}).items()
            if gstate.get("sampleTitle")
        }
        groups: dict[str, list[_Candidate]] = {}
        for candidate in candidates:
            if candidate.via == "high_risk":
                continue
            key = self._resolve_group_key(candidate.entry, group_samples)
            groups.setdefault(key, []).append(candidate)

        items_state = state.setdefault("items", {})
        groups_state = state.setdefault("groups", {})

        for title_hash, members in groups.items():
            members.sort(key=lambda c: c.base_score, reverse=True)
            anchor = members[0]
            evidence_items = self._unique_items(members)
            platforms = sorted({member.entry.item.platform for member in members})

            # 组样本/最早发布时间先落状态——被压制的组也要留样本，供未来归并与继承。
            group_state = groups_state.setdefault(title_hash, {})
            if not group_state.get("sampleTitle"):
                group_state["sampleTitle"] = anchor.entry.norm_title
            real_times = [t for t in (parse_utc(member.entry.item.published_at) for member in members) if t]
            historical_time = parse_utc(group_state.get("earliestPublishedAt"))
            earliest_published = min([t for t in (historical_time, *real_times) if t], default=None)
            if earliest_published is not None:
                group_state["earliestPublishedAt"] = earliest_published.isoformat()

            # 时间继承：anchor 无可信发布时间时向组内/历史借，衰减随之重算。
            if anchor.entry.time_fallback and earliest_published is not None:
                delta_hours = max(0.0, (now - earliest_published).total_seconds() / 3600.0)
                inherited_decay = math.exp(-delta_hours / max(settings.tau_hours, 0.1))
                if anchor.entry.decay > 0:
                    anchor.base_score *= inherited_decay / anchor.entry.decay
                anchor.entry.decay = inherited_decay
                anchor.entry.effective_time = earliest_published
                anchor.entry.time_inherited = True
                anchor.entry.published_at_override = earliest_published

            # 压制一：dedupKey 冷却。
            cooldown_hit = False
            for member in members:
                entry_state = items_state.get(member.entry.item.dedup_key) or {}
                alerted_at = parse_utc(entry_state.get("alertedAt"))
                if alerted_at and (now - alerted_at).total_seconds() < settings.cooldown_hours * 3600:
                    cooldown_hit = True
                    break
            if cooldown_hit:
                suppressed["cooldown"] += 1
                continue

            # 压制二：事件组硬窗口 + 显著更新再报。
            group_state = groups_state.get(title_hash) or {}
            last_alert = parse_utc(group_state.get("lastAlertAt"))
            progress_update = False
            if last_alert:
                within_window = (now - last_alert).total_seconds() < settings.event_group_hard_window_hours * 3600
                last_heat = float(group_state.get("lastHeat") or 0.0)
                current_heat = max((member.entry.item.heat_value for member in members), default=0.0)
                grew = current_heat >= last_heat * (1 + settings.velocity_growth_ratio) and \
                    (current_heat - last_heat) >= settings.velocity_growth_absolute
                if within_window and not grew:
                    suppressed["groupWindow"] += 1
                    continue
                if grew:
                    progress_update = True

            score, resonance_bonus, heat_bonus, velocity_bonus = self._score(anchor, members, state, platforms, now)
            if score >= settings.red_threshold:
                priority = "red"
            elif score >= settings.orange_threshold:
                priority = "orange"
            else:
                priority = "yellow"
            if settings.gray_mode:
                priority = "yellow"
            if progress_update:
                priority = "orange" if not settings.gray_mode else "yellow"

            seq += 1
            alert = self._build_alert(
                anchor=anchor,
                members=members,
                title_hash=title_hash,
                platforms=platforms,
                score=round(score, 3),
                resonance_bonus=resonance_bonus,
                priority=priority,
                now=now,
                seq=seq,
                progress_update=progress_update,
            )
            alerts.append(alert)
            groups_state[title_hash] = {
                "lastAlertAt": now.isoformat(),
                "lastScore": score,
                "lastHeat": max(item.heat_value for item in evidence_items),
                "platforms": platforms,
                "lastSeenAt": now.isoformat(),
            }
            for member in members:
                entry_state = items_state.setdefault(member.entry.item.dedup_key, {})
                entry_state["alertedAt"] = now.isoformat()

        self._observe_unresolved(entries, candidates, observation)
        return alerts, suppressed, observation

    def _score(
        self,
        anchor: _Candidate,
        members: list[_Candidate],
        state: dict,
        platforms: list[str],
        now: datetime,
    ) -> tuple[float, float, float, float]:
        settings = self.config.settings
        resonance_bonus = 0.0
        if len(platforms) > 1:
            resonance_bonus = min(
                settings.resonance_bonus_cap,
                (len(platforms) - 1) * settings.resonance_bonus_per_source,
            )
        heat_bonus, velocity_bonus = self._heat_and_velocity(anchor, state, now)
        score = anchor.base_score * (1 + resonance_bonus + heat_bonus + velocity_bonus)
        return score, resonance_bonus, heat_bonus, velocity_bonus

    def _heat_and_velocity(self, anchor: _Candidate, state: dict, now: datetime) -> tuple[float, float]:
        settings = self.config.settings
        item = anchor.entry.item
        heat_bonus = 0.0
        velocity_bonus = 0.0
        history = (state.get("heat") or {}).get(item.platform) or []
        if item.heat_value > 0 and len(history) >= settings.heat_history_min:
            below = sum(1 for value in history if value < item.heat_value)
            percentile = below / len(history)
            if percentile >= 0.9:
                heat_bonus = 0.5
            elif percentile >= 0.6:
                heat_bonus = 0.25
        previous = (state.get("items") or {}).get(item.dedup_key) or {}
        last_heat = float(previous.get("lastHeat") or 0.0)
        if item.heat_value > 0 and last_heat > 0:
            growth = item.heat_value - last_heat
            if growth >= settings.velocity_growth_absolute and growth / last_heat >= settings.velocity_growth_ratio:
                velocity_bonus = settings.velocity_bonus
        return heat_bonus, velocity_bonus

    def _build_alert(
        self,
        anchor: _Candidate,
        members: list[_Candidate],
        title_hash: str,
        platforms: list[str],
        score: float,
        resonance_bonus: float,
        priority: str,
        now: datetime,
        seq: int,
        progress_update: bool,
    ) -> Alert:
        entry = anchor.entry
        item = entry.item
        subject = anchor.subject
        evidence = []
        for member in sorted(members, key=lambda c: c.base_score, reverse=True):
            evidence.append(self._evidence_of(member.entry.item))
        event_types: list[str] = []
        if anchor.family is not None and anchor.family.event_types:
            event_types = list(anchor.family.event_types)
        flags = dict(anchor.flags)
        flags["timeFallback"] = entry.time_fallback
        flags["timeInherited"] = entry.time_inherited
        if progress_update:
            flags["progressUpdate"] = True
        if anchor.via == "triage":
            flags["llmTriage"] = True
        matched_spans: dict[str, Any] = {"titleHash": title_hash}
        if anchor.action is not None:
            matched_spans["action"] = anchor.action.action
            if anchor.action.object_word:
                matched_spans["object"] = anchor.action.object_word
        if subject is not None:
            matched_spans["subject"] = subject.surface
        return Alert(
            alert_id=f"alr_{now.strftime('%Y%m%d%H%M%S')}_{seq:03d}",
            generated_at=now.isoformat(),
            matched_rule=anchor.family.id if anchor.family else "unknown",
            priority=priority,  # type: ignore[arg-type]
            score=score,
            event_types=event_types,
            subject_entity_id=subject.entity.entity_id if subject else None,
            subject_name=subject.entity.canonical_name if subject else None,
            matched_spans=matched_spans,
            first_seen_at=entry.first_seen.isoformat(),
            published_at=(entry.published_at_override or item.published_at).isoformat() if entry.published_at_override else item.published_at,
            resonance={"sourceCount": len(platforms), "platforms": platforms, "resonanceBonus": resonance_bonus},
            evidence=evidence,
            flags=flags,
            why=anchor.why,
        )

    def _high_risk_alert(self, candidate: _Candidate, now: datetime, seq: int) -> Alert:
        item = candidate.entry.item
        return Alert(
            alert_id=f"alr_{now.strftime('%Y%m%d%H%M%S')}_{seq:03d}",
            generated_at=now.isoformat(),
            matched_rule="high_risk_bypass",
            priority="yellow",
            score=0.0,
            event_types=[],
            subject_entity_id=None,
            subject_name=None,
            matched_spans={"title": item.title},
            first_seen_at=candidate.entry.first_seen.isoformat(),
            published_at=item.published_at,
            resonance={"sourceCount": 1, "platforms": [item.platform]},
            evidence=[self._evidence_of(item)],
            flags={"unidentifiedSubject": True, "timeFallback": candidate.entry.time_fallback},
            why=candidate.why,
        )

    @staticmethod
    def _evidence_of(item: MonitoredItem) -> dict[str, Any]:
        return {
            "contentId": item.content_id,
            "title": item.title,
            "platform": item.platform,
            "url": item.url,
            "publishedAt": item.published_at,
            "dedupKey": item.dedup_key,
        }

    @staticmethod
    def _unique_items(members: list[_Candidate]) -> list[MonitoredItem]:
        seen: set[str] = set()
        items: list[MonitoredItem] = []
        for member in members:
            if member.entry.item.dedup_key in seen:
                continue
            seen.add(member.entry.item.dedup_key)
            items.append(member.entry.item)
        return items

    def _observe_unresolved(
        self,
        entries: list[_Entry],
        candidates: list[_Candidate],
        observation: list[dict[str, Any]],
    ) -> None:
        candidate_items = {c.entry.item.dedup_key for c in candidates}
        # 跨轮次去重：同一条目同原因只入箱一次（历史箱体可能很长，只回看尾部）。
        history = self.store.load_observation(limit=300)
        seen_pairs = {(e.get("dedupKey"), e.get("reason")) for e in history}
        for entry in entries:
            if entry.item.dedup_key in candidate_items:
                continue
            if entry.route == "slow" and entry.slow_eligible:
                reason = "triage_unavailable_or_non_event"
            elif entry.route == "industry":
                reason = "industry_channel_unresolved"
            else:
                continue
            if (entry.item.dedup_key, reason) in seen_pairs:
                continue
            seen_pairs.add((entry.item.dedup_key, reason))
            observation.append(self._observation_entry(entry, reason=reason))

    @staticmethod
    def _observation_entry(entry: _Entry, reason: str) -> dict[str, Any]:
        return {
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "platform": entry.item.platform,
            "title": entry.item.title,
            "dedupKey": entry.item.dedup_key,
            "subjectHint": entry.industry_word or "、".join(hit.surface for hit in entry.subject_hits) or None,
        }

    # ---- 状态回写 ----

    def _update_state(self, entries: list[_Entry], alerts: list[Alert], state: dict, now: datetime) -> None:
        settings = self.config.settings
        items_state = state.setdefault("items", {})
        alerted_keys = {
            dedup_key
            for alert in alerts
            for evidence in alert.evidence
            if (dedup_key := evidence.get("dedupKey"))
        }
        for entry in entries:
            if entry.route == "dropped" and entry.drop_reason == "stale":
                continue
            entry_state = items_state.setdefault(entry.item.dedup_key, {})
            entry_state.setdefault("firstSeenAt", entry.first_seen.isoformat())
            entry_state["lastSeenAt"] = now.isoformat()
            entry_state["lastHeat"] = entry.item.heat_value
            if entry.item.dedup_key in alerted_keys:
                entry_state["alertedAt"] = now.isoformat()
        heat_state = state.setdefault("heat", {})
        for entry in entries:
            if entry.item.heat_value > 0:
                history = heat_state.setdefault(entry.item.platform, [])
                history.append(entry.item.heat_value)
                if len(history) > settings.heat_history_cap:
                    del history[: len(history) - settings.heat_history_cap]
        state["tickCount"] = int(state.get("tickCount", 0)) + 1
        self.store.save_state(self.store.prune_state(state, settings.state_retention_days, now))

    @staticmethod
    def _count_routes(entries: list[_Entry]) -> dict[str, int]:
        routes: dict[str, int] = {}
        for entry in entries:
            key = entry.route if entry.route != "dropped" else f"dropped_{entry.drop_reason or 'unknown'}"
            routes[key] = routes.get(key, 0) + 1
        return routes
