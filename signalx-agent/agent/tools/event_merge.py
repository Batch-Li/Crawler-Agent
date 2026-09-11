"""抽取后的同一事件复核：模型决定合并，程序保留证据及 ID 沿革。"""
import json

from pydantic import ValidationError

from ..evidence_validation import resolve_anchor
from ..event_time import time_bounds
from ..llm import generate_json, validation_feedback
from ..models import Event
from ..prompts import EVENT_MERGE_SYSTEM_PROMPT
from ..tool_models import EventMergeBatch


class EventMergeToolError(RuntimeError):
    pass


class EventMergeTool:
    name = 'event_merge'
    description = '模型复核重复事件，合并证据并保留 ID 沿革。'

    def __init__(self, llm, max_attempts=2):
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于等于 1")
        self.llm, self.max_attempts = llm, max_attempts

    def invoke(self, events: list[Event]) -> list[Event]:
        if len(events) < 2:
            return events
        prompt = ('输出 JSON Schema：\n' + json.dumps(EventMergeBatch.model_json_schema(by_alias=True), ensure_ascii=False)
                  + '\n输入：\n' + json.dumps({'events': [e.model_dump(mode='json', by_alias=True) for e in events]}, ensure_ascii=False))
        feedback = ''
        for _ in range(self.max_attempts):
            try:
                batch = EventMergeBatch.model_validate(generate_json(self.llm, EVENT_MERGE_SYSTEM_PROMPT, prompt + feedback))
                return self.materialize(events, batch)
            except (ValidationError, ValueError, TypeError) as exc:
                feedback = '\n修正上次输出：\n' + validation_feedback(exc)
        raise EventMergeToolError('同一事件复核未通过校验')

    @staticmethod
    def materialize(events: list[Event], batch: EventMergeBatch) -> list[Event]:
        by_id = {e.event_id: e for e in events}
        if len(by_id) != len(events):
            raise ValueError('输入事件 ID 不能重复')
        used, replacements, removed = set(), {}, set()
        for group in batch.groups:
            ids = set(group.event_ids)
            if len(ids) != len(group.event_ids) or not ids.issubset(by_id) or ids & used:
                raise ValueError('合并组必须包含唯一的已知 ID，各组不能重叠')
            if group.confidence < .85:
                raise ValueError('合并证据不足：低置信度候选应保留独立事件，不输出合并组')
            members = [e for e in events if e.event_id in ids]
            cited = {a.evidence_id for a in group.evidence}
            if any(not cited.intersection(e.evidence_id for e in member.evidence) for member in members):
                raise ValueError('合并说明必须引用每个成员的证据')
            sources = [e for member in members for e in member.evidence]
            for anchor in group.evidence:
                resolve_anchor(anchor, sources)
            bounds = [value for e in members if (value := time_bounds(e)) is not None]
            if bounds and max(b[0] for b in bounds) > min(b[1] for b in bounds):
                raise ValueError('发生时间互不相容的事件不能合并')
            canonical = members[0].model_copy(deep=True)
            canonical.evidence = list({e.evidence_id: e for member in members for e in member.evidence}.values())
            canonical.entities = list(dict.fromkeys(name for e in members for name in e.entities))
            canonical.merged_event_ids = list(dict.fromkeys(
                key for e in members for key in [e.event_id, *e.merged_event_ids] if key != canonical.event_id))
            timed = [e for e in members if time_bounds(e) is not None]
            if timed:
                # 同一事件已通过时间相容检查，保留精度最高且有证据的时间。
                best = min(timed, key=lambda e: time_bounds(e)[1] - time_bounds(e)[0])
                canonical.event_time, canonical.event_date, canonical.time_anchor = best.event_time, best.event_date, best.time_anchor
            canonical.time_warnings = list(dict.fromkeys(w for e in members for w in e.time_warnings))
            replacements[canonical.event_id] = canonical
            removed.update(ids - {canonical.event_id})
            used.update(ids)
        return [replacements.get(e.event_id, e) for e in events if e.event_id not in removed]
