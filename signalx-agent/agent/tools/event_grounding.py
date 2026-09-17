"""逐事件复核证据立场与时间角色；日期由原文计算。"""
import json

from pydantic import ValidationError

from ..event_time import ground_event_time
from ..evidence_validation import resolve_anchor
from ..llm import generate_json, validation_feedback
from ..prompts import EVENT_GROUNDING_SYSTEM_PROMPT, EVENT_GROUNDING_AUDIT_SYSTEM_PROMPT
from ..tool_models import EventGroundingBatch, EventGroundingAuditBatch, EventMergeBatch
from .event_merge import EventMergeTool


class EventGroundingToolError(RuntimeError):
    pass


class EventGroundingTool:
    name = 'event_grounding'
    description = '逐条复核事件证据立场和发生、目标、报道时间。'

    def __init__(self, llm, max_attempts=2):
        if max_attempts < 1:
            raise ValueError('max_attempts 必须大于等于 1')
        self.llm, self.max_attempts = llm, max_attempts

    def invoke(self, events):
        result, chunk, size = [], [], 0
        for event in events:
            if event.time_reviewed and {a.evidence_id for a in event.evidence_assessments} == {e.evidence_id for e in event.evidence}:
                result.append(event)
                continue
            if chunk and (len(chunk) >= 6 or size + len(event.evidence) > 20):
                result.extend(self._review(chunk))
                chunk, size = [], 0
            chunk.append(event)
            size += len(event.evidence)
        if chunk:
            result.extend(self._review(chunk))
        by_id = {e.event_id: e for e in result}
        return self.audit([by_id[e.event_id] for e in events])

    def audit(self, events):
        if not events:
            return events
        result, chunk, size = [], [], 0
        for event in events:
            if len(event.evidence) > 20:
                if chunk:
                    result.extend(self._audit_once(chunk))
                    chunk, size = [], 0
                result.append(self._audit_large_event(event))
                continue
            if chunk and (len(chunk) >= 12 or size + len(event.evidence) > 20):
                result.extend(self._audit_once(chunk))
                chunk, size = [], 0
            chunk.append(event)
            size += len(event.evidence)
        if chunk:
            result.extend(self._audit_once(chunk))
        return result

    def _audit_large_event(self, original):
        event = original.model_copy(deep=True)
        for start in range(0, len(original.evidence), 15):
            evidence = original.evidence[start:start + 15]
            ids = {e.evidence_id for e in evidence}
            part = event.model_copy(update={'evidence': evidence,
                'evidence_assessments': [a for a in event.evidence_assessments if a.evidence_id in ids],
                'time_candidates': [c for c in event.time_candidates if c.anchor.evidence_id in ids]})
            reviewed = self._audit_once([part], allow_revisions=False)[0]
            assessments = {a.evidence_id: a for a in event.evidence_assessments}
            assessments.update({a.evidence_id: a for a in reviewed.evidence_assessments})
            event.evidence_assessments = list(assessments.values())
            event.time_candidates = [c for c in event.time_candidates if c.anchor.evidence_id not in ids] + reviewed.time_candidates
        return ground_event_time(event).model_copy(update={'grounding_audited': True})

    def _audit_once(self, events, allow_revisions=True):
        sources = {e.evidence_id: e.model_dump(mode='json', by_alias=True) for event in events for e in event.evidence}
        context = {'events': [dict(eventId=e.event_id, title=e.title, summary=e.summary,
                    actionKind=e.action_kind, claimType=e.claim_type,
                    evidenceIds=[x.evidence_id for x in e.evidence],
                    evidenceAssessments=[a.model_dump(by_alias=True) for a in e.evidence_assessments],
                    timeCandidates=[c.model_dump(by_alias=True) for c in e.time_candidates]) for e in events],
                   'sources': sources}
        prompt = ('输出 JSON Schema：\n' + json.dumps(EventGroundingAuditBatch.model_json_schema(by_alias=True), ensure_ascii=False)
                  + '\n输入：\n' + json.dumps(context, ensure_ascii=False))
        if not allow_revisions:
            prompt += '\n本批仅含热门节点的一部分来源，只修正这些来源的立场和时间。不得根据局部来源改写整个节点，revisions 必须为 []。节点拆分由全量事件归并阶段处理。'
        feedback = ''
        for _ in range(self.max_attempts):
            try:
                audit = EventGroundingAuditBatch.model_validate(generate_json(
                    self.llm, EVENT_GROUNDING_AUDIT_SYSTEM_PROMPT, prompt + feedback))
                if not allow_revisions and audit.revisions:
                    raise ValueError('局部来源复查只能输出 corrections，revisions 必须为空')
                corrected = self.apply_audit(events, audit)
                break
            except (ValidationError, ValueError, TypeError) as exc:
                feedback = '\n修正上次输出：\n' + validation_feedback(exc)
        else:
            raise EventGroundingToolError('事件依据反例审查未通过校验')
        # 拆分产生的新节点重新逐条归属，不递归发起审查。
        fresh = [e for e in corrected if not e.time_reviewed]
        replacements = {e.event_id: e for e in self._review(fresh)} if fresh else {}
        return [replacements.get(e.event_id, e).model_copy(update={'grounding_audited': True}) for e in corrected]

    @classmethod
    def apply_audit(cls, events, audit):
        by_id, changed = {e.event_id: e for e in events}, {}
        revised = {r.target_event_id for r in audit.revisions}
        corrections = {}
        for correction in audit.corrections:
            key = correction.event_id
            if key in corrections:
                previous = corrections[key]
                old_ids = {a.evidence_id for a in previous.evidence_assessments}
                if old_ids.intersection(a.evidence_id for a in correction.evidence_assessments):
                    raise ValueError('同一证据立场不能重复修正')
                correction = correction.model_copy(update={
                    'evidence_assessments': previous.evidence_assessments + correction.evidence_assessments,
                    'discard_time_candidate_indices': previous.discard_time_candidate_indices + correction.discard_time_candidate_indices,
                    'additional_time_candidates': previous.additional_time_candidates + correction.additional_time_candidates})
            corrections[key] = correction
        for correction in corrections.values():
            key = correction.event_id
            if key not in by_id or key in changed or key in revised:
                raise ValueError('修正目标必须唯一、已知且不与拆分重叠')
            event = by_id[key]
            indices = correction.discard_time_candidate_indices
            if len(indices) != len(set(indices)) or any(i < 0 or i >= len(event.time_candidates) for i in indices):
                raise ValueError('要删除的时间候选下标必须有效且唯一')
            assessments = {a.evidence_id: a for a in event.evidence_assessments}
            patch_ids = [a.evidence_id for a in correction.evidence_assessments]
            if len(patch_ids) != len(set(patch_ids)) or not set(patch_ids).issubset(assessments):
                raise ValueError('仅可更正当前事件已有的唯一证据立场')
            assessments.update({a.evidence_id: a for a in correction.evidence_assessments})
            review = dict(eventId=key, evidenceAssessments=list(assessments.values()),
                timeCandidates=[c for i, c in enumerate(event.time_candidates) if i not in indices]
                               + correction.additional_time_candidates)
            changed[key] = cls.materialize([event], EventGroundingBatch(reviews=[review]))[0]
        corrected = [changed.get(e.event_id, e) for e in events]
        return EventMergeTool.materialize(corrected, EventMergeBatch(groups=[], revisions=audit.revisions))

    def _review(self, events):
        # 热门节点可能汇集数十或数百篇报道，不能让单节点绕过输出预算。
        if any(len(e.evidence) > 15 for e in events):
            reviewed = []
            for original in events:
                parts = [self._review_chunk([original.model_copy(update={'evidence': original.evidence[start:start + 15]})])[0]
                         for start in range(0, len(original.evidence), 15)]
                candidates = {c.model_dump_json(by_alias=True): c for p in parts for c in p.time_candidates}
                event = original.model_copy(deep=True)
                event.evidence_assessments = [a for p in parts for a in p.evidence_assessments]
                event.time_candidates = list(candidates.values())
                event.time_reviewed = True
                reviewed.append(ground_event_time(event))
            return reviewed
        return self._review_chunk(events)

    def _review_chunk(self, events):
        context = [dict(eventId=e.event_id, title=e.title, summary=e.summary,
                        claimType=e.claim_type, actionKind=e.action_kind,
                        evidence=[x.model_dump(mode='json', by_alias=True) for x in e.evidence]) for e in events]
        prompt = ('输出 JSON Schema：\n' + json.dumps(EventGroundingBatch.model_json_schema(by_alias=True), ensure_ascii=False)
                  + '\n输入：\n' + json.dumps(context, ensure_ascii=False))
        feedback = ''
        for _ in range(self.max_attempts):
            try:
                batch = EventGroundingBatch.model_validate(generate_json(
                    self.llm, EVENT_GROUNDING_SYSTEM_PROMPT, prompt + feedback))
                return self.materialize(events, batch)
            except (ValidationError, ValueError, TypeError) as exc:
                feedback = '\n修正上次输出：\n' + validation_feedback(exc)
        raise EventGroundingToolError('证据立场与时间复核未通过校验')

    @staticmethod
    def materialize(events, batch):
        reviews = {r.event_id: r for r in batch.reviews}
        if len(reviews) != len(batch.reviews) or set(reviews) != {e.event_id for e in events}:
            raise ValueError('必须对每个已知 eventId 恰好复核一次')
        result = []
        for original in events:
            review = reviews[original.event_id]
            ids = [a.evidence_id for a in review.evidence_assessments]
            if len(ids) != len(set(ids)) or set(ids) != {e.evidence_id for e in original.evidence}:
                raise ValueError('必须评估事件的每条证据，不能遗漏或重复')
            event = original.model_copy(deep=True)
            event.evidence_assessments = []
            for assessment in review.evidence_assessments:
                anchor = resolve_anchor(assessment, event.evidence)
                event.evidence_assessments.append(assessment.model_copy(update={'quote': anchor.quote}))
            event.time_candidates = []
            for candidate in review.time_candidates:
                candidate = candidate.model_copy(deep=True)
                candidate.anchor = resolve_anchor(candidate.anchor, event.evidence)
                if candidate.year_context:
                    if candidate.year_context.evidence_id != candidate.anchor.evidence_id:
                        raise ValueError('年份上下文必须来自同一证据')
                    candidate.year_context = resolve_anchor(candidate.year_context, event.evidence)
                event.time_candidates.append(candidate)
            event.time_reviewed = True
            result.append(ground_event_time(event))
        return result
