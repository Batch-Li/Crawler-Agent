"""DeepSeek 驱动的事件关联；程序校验引用、原文、时间和图结构。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from ..entity_resolution import EntityResolver
from ..llm import StructuredLLM, generate_json, validation_feedback
from ..models import Event
from ..evidence_validation import normalized_quote, resolve_anchor
from ..event_time import event_sort_key, time_bounds
from ..prompts import EVENT_CHAIN_SYSTEM_PROMPT
from ..tool_models import EventChainDraft, EventChainDraftBatch, EventChainRequest, EventChainResult
from ..workflow_models import EventChain, TimelineEntry

_DIRECTIONAL = {"FOLLOW_UP", "RESPONSE", "CAUSES"}


class EventChainToolError(RuntimeError):
    """模型关联结果在限定次数内未通过校验。"""


class EventChainTool:
    name = "event_chain"
    description = "由模型关联故事线，输出带证据、类型、置信度和时间节点的事件链。"
    input_model = EventChainRequest
    output_model = EventChainResult

    def __init__(self, llm: StructuredLLM, max_attempts: int = 2,
                 entity_aliases: dict[str, str] | None = None):
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于等于 1")
        self.llm = llm
        self.max_attempts = max_attempts
        self.entity_resolver = EntityResolver(entity_aliases)

    def invoke(self, request: EventChainRequest | dict[str, Any]) -> EventChainResult:
        request = EventChainRequest.model_validate(request)
        if len(request.events) < 2:
            return EventChainResult(unlinked_event_ids=[e.event_id for e in request.events])
        context = {
            "industry": request.industry,
            "events": [self.entity_resolver.normalize(e).model_dump(mode="json", by_alias=True)
                       for e in request.events],
            "classifications": [c.model_dump(by_alias=True) for c in request.classifications],
            # 旧引文可能来自不同版本的搜索摘要；只传链身份，引用必须重新从当前证据选择。
            "previousChains": [{"chainId": c.chain_id, "title": c.title, "eventIds": c.event_ids}
                               for c in request.previous_chains],
        }
        prompt = ("输出 JSON Schema：\n"
                  + json.dumps(EventChainDraftBatch.model_json_schema(by_alias=True), ensure_ascii=False)
                  + "\n输入：\n" + json.dumps(context, ensure_ascii=False))
        feedback = ""
        last_error: Exception | None = None
        for _ in range(self.max_attempts):
            try:
                batch = EventChainDraftBatch.model_validate(generate_json(
                    self.llm, EVENT_CHAIN_SYSTEM_PROMPT, prompt + feedback))
                return self._materialize(batch, request)
            except (ValidationError, ValueError, TypeError) as exc:
                feedback = "\n请修正上次输出：\n" + validation_feedback(exc)
                last_error = exc
        raise EventChainToolError(f"事件关联未通过引用、时间或结构校验（{type(last_error).__name__}: {last_error}）")

    @classmethod
    def _materialize(cls, batch: EventChainDraftBatch, request: EventChainRequest) -> EventChainResult:
        event_by_id = {e.event_id: e for e in request.events}
        aliases = {key: e.event_id for e in request.events for key in e.merged_event_ids}
        previous = {c.chain_id: c for c in request.previous_chains}
        chains = []
        seen_ids, seen_members = set(), set()
        for original in batch.chains:
            draft = original.model_copy(deep=True)
            if len(draft.event_ids) != len(set(draft.event_ids)):
                raise ValueError("事件链成员不能重复")
            original_members = set(draft.event_ids)
            if not original_members.issubset(event_by_id.keys() | aliases.keys()):
                raise ValueError("事件链成员必须来自当前或已合并的已知事件")
            original_pairs = set()
            for relation in draft.relations:
                pair = frozenset((relation.from_event_id, relation.to_event_id))
                if len(pair) != 2 or not pair.issubset(original_members) or pair in original_pairs:
                    raise ValueError("关系两端必须为链内不同事件，且不能重复")
                original_pairs.add(pair)
            draft.event_ids = list(dict.fromkeys(aliases.get(key, key) for key in draft.event_ids))
            relations = []
            for relation in draft.relations:
                start, end = relation.from_event_id, relation.to_event_id
                relation.from_event_id, relation.to_event_id = aliases.get(start, start), aliases.get(end, end)
                if start != end and relation.from_event_id == relation.to_event_id:
                    continue  # 同一事件已合并，不把重复报道伪装成关系。
                relations.append(relation)
            draft.relations = relations
            if len(draft.event_ids) < 2:
                continue
            cls._validate_draft(draft, event_by_id)
            members = frozenset(draft.event_ids)
            if members in seen_members:
                raise ValueError("同一事件集合不能重复生成事件链")
            seen_members.add(members)
            matched = draft.matched_chain_id
            if matched is not None:
                if matched not in previous or not members.intersection(aliases.get(key, key) for key in previous[matched].event_ids):
                    raise ValueError("matchedChainId 必须引用包含共同事件的已有链")
            else:
                exact = [c.chain_id for c in request.previous_chains if set(c.event_ids) == members]
                matched = exact[0] if len(exact) == 1 else None
            chain_id = matched or "chain_" + hashlib.sha256(
                json.dumps(sorted(members), ensure_ascii=False).encode()).hexdigest()[:20]
            if chain_id in seen_ids:
                raise ValueError("同一已有事件链不能被重复引用")
            seen_ids.add(chain_id)
            ordered = sorted((event_by_id[key] for key in draft.event_ids), key=_event_sort_key)
            for relation in draft.relations:
                first, second = (time_bounds(event_by_id[key]) for key in (relation.from_event_id, relation.to_event_id))
                if relation.relation_type in _DIRECTIONAL and (
                        first is None or second is None or first[1] >= second[0]):
                    relation.status = "needs_review"
                    draft.unresolved_questions.append(
                        f"{relation.from_event_id} → {relation.to_event_id} 的先后时间待核实。")
            chains.append(EventChain(
                chain_id=chain_id, category="STORYLINE", title=draft.title, summary=draft.summary,
                event_ids=[e.event_id for e in ordered], relations=draft.relations,
                confidence=draft.confidence,
                unresolved_questions=list(dict.fromkeys(draft.unresolved_questions)),
                timeline=[TimelineEntry(event_id=e.event_id, title=e.title, event_time=e.event_time,
                                        event_date=e.event_date,
                                        time_status="known" if e.event_time is not None else "partial" if e.event_date else "unknown")
                          for e in ordered],
            ))
        linked = {key for chain in chains for key in chain.event_ids}
        return EventChainResult(
            chains=chains, unlinked_event_ids=[e.event_id for e in request.events if e.event_id not in linked],
            unresolved_questions=list(dict.fromkeys(batch.unresolved_questions)),
        )

    @staticmethod
    def _validate_draft(draft: EventChainDraft, events: dict[str, Event]) -> None:
        members = set(draft.event_ids)
        if len(members) != len(draft.event_ids) or not members.issubset(events):
            raise ValueError("事件链成员必须为唯一的已知 eventId")
        adjacent = {key: set() for key in members}
        directed = {key: set() for key in members}
        seen_pairs = set()
        for relation in draft.relations:
            start, end = relation.from_event_id, relation.to_event_id
            if start == end or start not in members or end not in members:
                raise ValueError("关系两端必须是链内不同事件")
            pair = frozenset((start, end))
            if pair in seen_pairs:
                raise ValueError("同一对事件不能重复建立关系")
            seen_pairs.add(pair)
            first, second = events[start], events[end]
            first_ids = {e.evidence_id for e in first.evidence}
            second_ids = {e.evidence_id for e in second.evidence}
            cited = {anchor.evidence_id for anchor in relation.evidence}
            if not cited.issubset(first_ids | second_ids) or not (
                    cited.intersection(first_ids) and cited.intersection(second_ids)):
                raise ValueError("每条关系必须引用两端事件的证据，不能借用其他事件的证据")
            relation.evidence = [resolve_anchor(anchor, [*first.evidence, *second.evidence]) for anchor in relation.evidence]
            adjacent[start].add(end)
            adjacent[end].add(start)
            if relation.relation_type in _DIRECTIONAL:
                first_bounds, second_bounds = time_bounds(first), time_bounds(second)
                if first_bounds and second_bounds and first_bounds[0] > second_bounds[1]:
                    raise ValueError("有向关系与已知事件时间矛盾")
                directed[start].add(end)
        visited, pending = set(), [next(iter(members))]
        while pending:
            key = pending.pop()
            if key not in visited:
                visited.add(key)
                pending.extend(adjacent[key] - visited)
        if visited != members:
            raise ValueError("关系必须连接事件链的全部成员")
        # 先后／回应／因果关系必须无环；不把 RELATED 当作时间方向。
        indegrees = {key: 0 for key in members}
        for targets in directed.values():
            for target in targets:
                indegrees[target] += 1
        pending = [key for key, degree in indegrees.items() if degree == 0]
        processed = 0
        while pending:
            key = pending.pop()
            processed += 1
            for target in directed[key]:
                indegrees[target] -= 1
                if indegrees[target] == 0:
                    pending.append(target)
        if processed != len(members):
            raise ValueError("有向事件关系不能形成循环")


def _text(value: str) -> str:
    return normalized_quote(value)


def _event_sort_key(event: Event) -> tuple[bool, datetime, str]:
    return event_sort_key(event)
