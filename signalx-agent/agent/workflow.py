"""支持增量处理、双预算和失败保留快照的事件发现循环。"""

from __future__ import annotations

from typing import Protocol

from .llm import ModelBudgetExceeded, ModelCallBudget, model_call_budget
from .models import Evidence
from .workflow_models import (
    CollectionResult, EventProcessingResult, EvidenceReview, FilteredEvidence, QueryPlan, SearchRequest, StopReason, ToolCallRecord,
    WorkflowError, WorkflowMode, WorkflowRequest, WorkflowResult, WorkflowRound,
)


class CollectionTool(Protocol):
    """由采集组实现；本模块仅依赖标准 Evidence。"""

    name: str

    def search(self, request: SearchRequest) -> list[Evidence] | CollectionResult: ...


class EventProcessor(Protocol):
    def process(
        self, evidence: list[Evidence], *,
        previous: EventProcessingResult | None = None, industry: str | None = None,
        time_fallback: bool = False,
    ) -> EventProcessingResult:
        """输入新增/变更证据及上次快照，返回包含全部已有事件的完整快照。"""


class QueryPlanner(Protocol):
    def initial_plan(self, request: WorkflowRequest) -> QueryPlan: ...

    def next_plan(
        self, request: WorkflowRequest, snapshot: EventProcessingResult,
        seen_queries: set[str], round_index: int,
    ) -> QueryPlan: ...


class EvidenceFilter(Protocol):
    def review(self, request: WorkflowRequest, evidence: list[Evidence]) -> list[EvidenceReview]: ...


class EventDiscoveryWorkflow:
    def __init__(self, collector: CollectionTool, processor: EventProcessor,
                 planner: QueryPlanner, relevance_filter: EvidenceFilter | None = None) -> None:
        self.collector = collector
        self.processor = processor
        self.planner = planner
        self.relevance_filter = relevance_filter

    def run(self, request: WorkflowRequest | dict) -> WorkflowResult:
        request = WorkflowRequest.model_validate(request)
        with model_call_budget(request.max_llm_calls) as budget:
            return self._run(request, budget)

    def _run(self, request: WorkflowRequest, budget: ModelCallBudget) -> WorkflowResult:
        seed = request.seed_event
        evidence_by_id = {item.evidence_id: item for item in seed.evidence} if seed else {}
        processed_evidence: dict[str, Evidence] = {}
        snapshot = EventProcessingResult(events=[seed] if seed else [])
        rounds: list[WorkflowRound] = []
        errors: list[WorkflowError] = []
        seen: set[str] = set()
        pending: dict[str, str] = {}
        filtered: dict[str, FilteredEvidence] = {}
        unreviewed: dict[str, Evidence] = {}
        review_cache: dict[str, tuple[Evidence, EvidenceReview]] = {}
        unresolved_questions: list[str] = []
        no_progress_rounds = 0

        def enqueue(queries: list[str]) -> None:
            for query in queries:
                normalized = " ".join(query.split())
                key = self._query_key(normalized)
                if key and key not in seen:
                    pending.setdefault(key, normalized)

        def failure(stage: str, exc: Exception) -> StopReason:
            # 外部异常可能含请求头、正文等信息，持久化记录只保留类型与阶段。
            errors.append(WorkflowError(
                stage=stage, error_type=type(exc).__name__,
                message=f"{stage} 阶段失败，保留上次成功快照；错误类型：{type(exc).__name__}",
            ))
            return (StopReason.MODEL_BUDGET_EXHAUSTED
                    if isinstance(exc, ModelBudgetExceeded) else StopReason.FAILED)

        def finish(reason: StopReason) -> WorkflowResult:
            degraded = any(call.status != "success" for row in rounds for call in row.tool_calls)
            if degraded and reason in (StopReason.COMPLETED, StopReason.NO_EVIDENCE, StopReason.NO_NEW_QUERIES, StopReason.NO_PROGRESS):
                reason = StopReason.PARTIAL if snapshot.classifications else StopReason.FAILED
            return WorkflowResult(
                mode=request.mode, industry=request.industry,
                evidence=list(evidence_by_id.values()),
                events=snapshot.events, classifications=snapshot.classifications,
                chains=snapshot.chains, rounds=rounds, stop_reason=reason,
                collection_calls=len(seen), llm_calls=budget.calls,
                pending_queries=list(pending.values()),
                processed_evidence_ids=[
                    key for key, value in evidence_by_id.items()
                    if processed_evidence.get(key) == value
                ],
                unprocessed_evidence_ids=[
                    key for key, value in evidence_by_id.items()
                    if processed_evidence.get(key) != value
                ],
                errors=errors,
                filtered_evidence=list(filtered.values()), unreviewed_evidence=list(unreviewed.values()),
                unresolved_questions=list(dict.fromkeys([
                    *unresolved_questions, *snapshot.chain_unresolved_questions,
                    *(question for chain in snapshot.chains for question in chain.unresolved_questions),
                ])),
                unlinked_event_ids=snapshot.unlinked_event_ids,
                chain_unresolved_questions=snapshot.chain_unresolved_questions,
                time_fallback=request.time_fallback,
                stop_message={
                    StopReason.NO_PROGRESS: "连续查询没有新增或更新的有效证据，停止扩搜；待核实问题仍未解决。",
                    StopReason.COMPLETED: "已按当前查询计划处理完毕；事件链为空不影响正常结束。",
                    StopReason.MAX_ROUNDS: "达到轮数上限，保留结果和待执行查询。",
                    StopReason.TOOL_BUDGET_EXHAUSTED: "达到搜索调用上限，未发送额外请求。",
                    StopReason.MODEL_BUDGET_EXHAUSTED: "达到模型调用上限，未发送额外请求。",
                }.get(reason, reason.value),
            )

        if request.mode == WorkflowMode.BULK_DISCOVERY:
            enqueue(request.preset_keywords)
        try:
            plan = QueryPlan.model_validate(self.planner.initial_plan(request))
        except Exception as exc:
            return finish(failure("planning", exc))
        enqueue(plan.queries)
        unresolved_questions = plan.unresolved_questions

        while True:
            dirty = [
                item for key, item in evidence_by_id.items()
                if processed_evidence.get(key) != item
            ]
            if not pending and not plan.should_continue and not dirty:
                return finish(StopReason.COMPLETED)
            if len(rounds) >= request.max_rounds:
                return finish(StopReason.MAX_ROUNDS)
            if pending and len(seen) >= request.max_tool_calls:
                return finish(StopReason.TOOL_BUDGET_EXHAUSTED)
            # plan=false 仍会处理尚未分类的种子事件；重复查询则受 NO_NEW_QUERIES 控制。
            if plan.should_continue and not pending and not dirty:
                return finish(StopReason.NO_NEW_QUERIES)

            before = dict(evidence_by_id)
            before_llm_calls = budget.calls
            before_errors = len(errors)
            calls: list[ToolCallRecord] = []
            collected_items: list[Evidence] = []
            limit = min(request.per_round_query_limit, request.max_tool_calls - len(seen))
            for key, query in list(pending.items())[:limit]:
                del pending[key]
                seen.add(key)
                try:
                    raw = self.collector.search(SearchRequest(
                        query=query, industry=request.industry,
                        round_index=len(rounds) + 1, limit=request.per_query_limit,
                    ))
                    collected = (raw if isinstance(raw, CollectionResult)
                                 else CollectionResult(evidence=raw[:request.per_query_limit]))
                    items = collected.evidence[:request.per_query_limit]
                    collected_items.extend(items)
                    calls.append(ToolCallRecord(
                        query=query, success=collected.status != "failed", result_count=len(items),
                        status=collected.status, source_reports=collected.source_reports,
                        dropped_count=collected.dropped_count,
                    ))
                except Exception as exc:
                    calls.append(ToolCallRecord(
                        query=query, success=False,
                        status="failed",
                        error=f"采集失败：{type(exc).__name__}",
                    ))

            incoming = {item.evidence_id: item for item in collected_items}
            reviews: list[EvidenceReview] = []
            reason = None
            if self.relevance_filter is not None and request.mode == WorkflowMode.EVENT_EXPANSION:
                to_review = [item for key, item in incoming.items()
                             if evidence_by_id.get(key) != item
                             and (key not in review_cache or review_cache[key][0] != item)]
                try:
                    reviewed = self.relevance_filter.review(request, to_review) if to_review else []
                    expected = {item.evidence_id for item in to_review}
                    if len(reviewed) != len(expected) or {r.evidence_id for r in reviewed} != expected:
                        raise ValueError("相关性审查遗漏或重复引用证据")
                    for review in reviewed:
                        review_cache[review.evidence_id] = (incoming[review.evidence_id], review)
                    for key, item in incoming.items():
                        if evidence_by_id.get(key) == item:
                            continue
                        review = review_cache[key][1]
                        reviews.append(review)
                        if review.decision == "relevant":
                            evidence_by_id[key] = item
                            filtered.pop(key, None)
                        else:
                            filtered[key] = FilteredEvidence(evidence=item, decision=review.decision, reason=review.reason)
                        unreviewed.pop(key, None)
                except Exception as exc:
                    unreviewed.update({item.evidence_id: item for item in to_review})
                    reason = failure("filtering", exc)
            else:
                evidence_by_id.update(incoming)

            dirty = [
                item for key, item in evidence_by_id.items()
                if processed_evidence.get(key) != item
            ]
            status = "skipped"
            if dirty and reason is None:
                try:
                    candidate = EventProcessingResult.model_validate(self.processor.process(
                        dirty, previous=snapshot.model_copy(deep=True), industry=request.industry,
                        time_fallback=request.time_fallback,
                    ))
                    self._validate_snapshot(candidate, snapshot, evidence_by_id)
                    snapshot = candidate
                    processed_evidence = dict(evidence_by_id)
                    status = "completed"
                except Exception as exc:
                    status = "failed"
                    reason = failure("processing", exc)

            rounds.append(WorkflowRound(
                round_index=len(rounds) + 1, plan_reason=plan.reason, tool_calls=calls,
                new_evidence_count=len(evidence_by_id.keys() - before.keys()),
                updated_evidence_count=sum(
                    key in before and value != before[key] for key, value in evidence_by_id.items()
                ),
                total_evidence_count=len(evidence_by_id), event_count=len(snapshot.events),
                chain_count=len(snapshot.chains), evidence_updates=dirty,
                processing_status=status, llm_calls=budget.calls - before_llm_calls,
                snapshot=snapshot.model_copy(deep=True), errors=errors[before_errors:],
                collected_evidence=collected_items, evidence_reviews=reviews,
            ))
            if reason is not None:
                return finish(reason)
            progress = any(before.get(key) != item for key, item in evidence_by_id.items())
            no_progress_rounds = 0 if progress else no_progress_rounds + bool(calls)
            mandatory_pending = request.mode == WorkflowMode.BULK_DISCOVERY and any(
                self._query_key(query) not in seen for query in request.preset_keywords
            )
            if not evidence_by_id and not mandatory_pending and not pending:
                return finish(StopReason.NO_EVIDENCE)
            if (plan.should_continue and calls and not mandatory_pending
                    and no_progress_rounds >= request.max_no_progress_rounds):
                if not unresolved_questions:
                    unresolved_questions = ["当前事件尚缺少可直接验证或补充的证据。"]
                return finish(StopReason.NO_PROGRESS)
            if pending:
                plan = QueryPlan(
                    should_continue=True, queries=list(pending.values()),
                    reason="继续执行尚未完成的关键词或查询",
                )
                continue
            if not plan.should_continue:
                return finish(StopReason.COMPLETED)
            if not evidence_by_id:
                return finish(StopReason.NO_EVIDENCE)
            if len(seen) >= request.max_tool_calls:
                return finish(StopReason.TOOL_BUDGET_EXHAUSTED)
            if len(rounds) >= request.max_rounds:
                return finish(StopReason.MAX_ROUNDS)
            try:
                plan = QueryPlan.model_validate(self.planner.next_plan(
                    request, snapshot, set(seen), len(rounds),
                ))
            except Exception as exc:
                return finish(failure("planning", exc))
            enqueue(plan.queries)
            unresolved_questions = plan.unresolved_questions

    @staticmethod
    def _validate_snapshot(candidate: EventProcessingResult, previous: EventProcessingResult,
                           evidence: dict[str, Evidence]) -> None:
        event_ids = {item.event_id for item in candidate.events}
        merged_ids = {key for item in candidate.events for key in item.merged_event_ids}
        if not {item.event_id for item in previous.events}.issubset(event_ids | merged_ids):
            raise ValueError("处理器遗漏已有事件；需保留原 eventId")
        for old in previous.events:
            if old.event_id in event_ids:
                continue
            target = next(item for item in candidate.events if old.event_id in item.merged_event_ids)
            if not {e.evidence_id for e in old.evidence}.issubset(e.evidence_id for e in target.evidence):
                raise ValueError("合并已有事件必须保留其证据")
        if event_ids != {item.event_id for item in candidate.classifications}:
            raise ValueError("每个事件必须恰好有一个分类")
        for event in candidate.events:
            if any(item.evidence_id not in evidence for item in event.evidence):
                raise ValueError("处理器返回了未知证据")

    @staticmethod
    def _query_key(query: str) -> str:
        return " ".join(query.split()).casefold()
