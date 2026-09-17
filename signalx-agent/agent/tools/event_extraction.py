"""从新证据抽取事件，并与已确认事件匹配；身份不依赖生成标题。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from ..llm import StructuredLLM, generate_json, validation_feedback
from ..models import Event, Evidence
from ..evidence_validation import normalized_quote, resolve_anchor
from ..event_time import ground_event_time, refresh_event_evidence, _combine_dates
from ..prompts import EVENT_EXTRACTION_SYSTEM_PROMPT
from ..tool_models import EventDraft, EventDraftBatch, EventExtractionRequest, EventExtractionResult


class EventExtractionToolError(RuntimeError):
    """事件抽取在限定重试次数内未能得到合法结果。"""


class EventExtractionTool:
    name = "event_extraction"
    description = "抽取新证据中的事件，匹配已有事件并保留其 ID。"
    input_model = EventExtractionRequest
    output_model = EventExtractionResult

    def __init__(self, llm: StructuredLLM, max_attempts: int = 2,
                 max_evidence_per_call: int = 20) -> None:
        if max_attempts < 1 or max_evidence_per_call < 1:
            raise ValueError("max_attempts 和 max_evidence_per_call 必须大于等于 1")
        self.llm = llm
        self.max_attempts = max_attempts
        self.max_evidence_per_call = max_evidence_per_call

    def invoke(self, request: EventExtractionRequest | dict[str, Any]) -> EventExtractionResult:
        normalized = EventExtractionRequest.model_validate(request)
        events = {event.event_id: event for event in normalized.existing_events}
        for start in range(0, len(normalized.evidence), self.max_evidence_per_call):
            chunk = normalized.evidence[start:start + self.max_evidence_per_call]
            updates = self._extract_bounded(chunk, normalized.industry, list(events.values()))
            events.update({event.event_id: event for event in updates})
        return EventExtractionResult(events=list(events.values()))

    def _extract_bounded(self, evidence: list[Evidence], industry: str | None,
                         existing: list[Event]) -> list[Event]:
        try:
            return self._extract_chunk(evidence, industry, existing)
        except EventExtractionToolError:
            # 大批次结构／引文失败时缩小批次重试，不放松校验；全部调用仍受总预算约束。
            if len(evidence) <= 5:
                raise
            middle = len(evidence) // 2
            first = self._extract_bounded(evidence[:middle], industry, existing)
            accumulated = {e.event_id: e for e in [*existing, *first]}
            second = self._extract_bounded(evidence[middle:], industry, list(accumulated.values()))
            return list({e.event_id: e for e in [*first, *second]}.values())

    def _extract_chunk(self, evidence: list[Evidence], industry: str | None,
                       existing: list[Event]) -> list[Event]:
        context = {
            "industry": industry,
            "evidence": [item.model_dump(by_alias=True, mode="json") for item in evidence],
            "existingEvents": [
                {
                    "eventId": event.event_id,
                    "title": event.title,
                    "summary": event.summary,
                    "actionKind": event.action_kind,
                    "claimType": event.claim_type,
                    "entities": event.entities,
                    "eventTime": event.event_time.isoformat() if event.event_time else None,
                    "eventDate": event.event_date.model_dump() if event.event_date else None,
                    "evidenceIds": [item.evidence_id for item in event.evidence],
                    "anchor": event.anchor.model_dump(by_alias=True) if event.anchor else None,
                    "mergedEventIds": event.merged_event_ids,
                }
                for event in existing
            ],
        }
        schema = EventDraftBatch.model_json_schema(by_alias=True)
        feedback = None
        last_error = None
        evidence_by_id = {item.evidence_id: item for item in evidence}
        existing_ids = {key for event in existing for key in [event.event_id, *event.merged_event_ids]}
        for _ in range(self.max_attempts):
            parts = [
                "请从新 Evidence 抽取事件，并与 existingEvents 匹配。",
                "输出 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False),
                "输入：\n" + json.dumps(context, ensure_ascii=False),
            ]
            if feedback:
                parts.append("上一次输出校验失败，请修正：\n" + feedback)
            try:
                batch = EventDraftBatch.model_validate(generate_json(
                    self.llm, EVENT_EXTRACTION_SYSTEM_PROMPT, "\n\n".join(parts),
                ))
                for draft in batch.events:
                    unknown = set(draft.evidence_ids) - evidence_by_id.keys()
                    if unknown:
                        raise ValueError("事件引用了不存在的 evidenceId: " + ", ".join(sorted(unknown)))
                    if draft.matched_event_id and draft.matched_event_id not in existing_ids:
                        raise ValueError("matchedEventId 必须来自 existingEvents")
                    draft.anchor = resolve_anchor(draft.anchor, list(evidence_by_id.values()))
                    if draft.time_anchor:
                        draft.time_anchor = resolve_anchor(draft.time_anchor, list(evidence_by_id.values()))
                # Event 构建也在重试范围内，不让草稿到正式对象的校验绕过重试。
                return self._materialize_events(batch.events, evidence_by_id, existing)
            except (ValidationError, ValueError, TypeError) as exc:
                last_error = exc
                feedback = validation_feedback(exc)
        raise EventExtractionToolError(
            f"事件抽取在 {self.max_attempts} 次尝试后仍未通过校验"
        ) from last_error

    @classmethod
    def _materialize_events(cls, drafts: list[EventDraft],
                            evidence_by_id: dict[str, Evidence],
                            existing: list[Event]) -> list[Event]:
        events = {event.event_id: event for event in existing}
        aliases = {key: event.event_id for event in existing for key in event.merged_event_ids}
        changed: dict[str, Event] = {}
        for draft in drafts:
            event_id = draft.matched_event_id or cls._event_id(draft)
            event_id = aliases.get(event_id, event_id)
            if event_id not in events and not draft.matched_event_id:
                # 已有 anchor 可反查旧 ID；无 anchor 的旧数据才使用保守匹配。
                candidates = [
                    event for event in events.values()
                    if (event.anchor and event.anchor.evidence_id == draft.anchor.evidence_id
                        and _normalize_text(event.anchor.quote) == _normalize_text(draft.anchor.quote)
                        and event.action_kind == draft.action_kind and event.claim_type == draft.claim_type)
                    or (event.anchor is None
                        and _normalize_text(event.title) == _normalize_text(draft.title)
                        and set(draft.evidence_ids).intersection(item.evidence_id for item in event.evidence)
                        and (not event.entities or not draft.entities or event.entities[0] == draft.entities[0])
                        and (event.event_time is None or draft.event_time is None or event.event_time == draft.event_time))
                ]
                if len(candidates) == 1:
                    event_id = candidates[0].event_id
            old = events.get(event_id)
            if old:
                if old.action_kind != 'other' and draft.action_kind != 'other' and old.action_kind != draft.action_kind:
                    raise ValueError('不同动作不能匹配同一事件；发布、预购、发售分别抽取')
                if old.claim_type != draft.claim_type:
                    raise ValueError('计划、预测、传闻、评价与已报道发生的动作应分别保留，不可互相覆盖')
                old = refresh_event_evidence(old, evidence_by_id)
            linked = {item.evidence_id: item for item in old.evidence} if old else {}
            linked.update({key: evidence_by_id[key] for key in draft.evidence_ids})
            entities = list(dict.fromkeys([*(old.entities if old else []), *draft.entities]))
            title = draft.title
            summary = draft.summary if draft.summary is not None else (old.summary if old else None)
            if linked and all(item.has_full_content is False for item in linked.values()):
                if not title.startswith(('报道称', '媒体称', '据报道', '传闻', '爆料')):
                    title = '报道称：' + title
            event = Event(
                event_id=event_id,
                title=title,
                summary=summary,
                action_kind=draft.action_kind if draft.action_kind != 'other' or not old else old.action_kind,
                claim_type=draft.claim_type,
                event_time=draft.event_time or (old.event_time if old else None),
                event_date=old.event_date if old else None,
                time_anchor=draft.time_anchor or (old.time_anchor if old else None),
                time_warnings=old.time_warnings if old else [],
                merged_event_ids=old.merged_event_ids if old else [],
                entities=entities,
                evidence=list(linked.values()),
                anchor=old.anchor if old and old.anchor else draft.anchor,
                event_types=old.event_types if old else [],
                alert_meta=old.alert_meta if old else None,
            )
            event = ground_event_time(event)
            if old and (old.event_time or (old.event_date and old.event_date.precision != 'month_day')) \
                    and not event.event_time and (not event.event_date or event.event_date.precision == 'month_day'):
                candidates = [e.event_date for e in (old, event) if e.event_date]
                _, conflict = _combine_dates(candidates)
                if not conflict:
                    event.event_time, event.event_date, event.time_anchor = old.event_time, old.event_date, old.time_anchor
                else:
                    event.time_warnings.append('新增来源与旧日期不一致，需逐条时间复核。')
            events[event_id] = changed[event_id] = event
        return list(changed.values())

    @staticmethod
    def _event_id(draft: EventDraft) -> str:
        # 同一证据片段换标题/补时间不会换 ID；跨来源匹配使用 matchedEventId。
        parts = [draft.anchor.evidence_id, _normalize_text(draft.anchor.quote)]
        if draft.action_kind != 'other' or draft.claim_type != 'reported':
            parts += [draft.action_kind, draft.claim_type]
        signature = json.dumps(parts, ensure_ascii=False)
        return "evt_" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]


def _normalize_text(value: str) -> str:
    return normalized_quote(value)
