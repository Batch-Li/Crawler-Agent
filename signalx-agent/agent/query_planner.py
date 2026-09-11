"""行业关键词入口与事件扩展查询规划器。"""

from __future__ import annotations

import json

from pydantic import ValidationError

from .llm import StructuredLLM, generate_json, validation_feedback
from .prompts import QUERY_PLANNER_SYSTEM_PROMPT
from .workflow_models import (
    EventProcessingResult,
    QueryPlan,
    WorkflowMode,
    WorkflowRequest,
)


class QueryPlannerError(RuntimeError):
    """查询规划器无法生成合法计划。"""


class LLMQueryPlanner:
    """预设关键词确定性启动，后续由 LLM 决定是否扩展。"""

    def __init__(
        self,
        llm: StructuredLLM,
        max_queries_per_round: int = 5,
        max_attempts: int = 2,
    ) -> None:
        if max_queries_per_round < 1:
            raise ValueError("max_queries_per_round 必须大于等于 1")
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于等于 1")
        self.llm = llm
        self.max_queries_per_round = max_queries_per_round
        self.max_attempts = max_attempts

    def initial_plan(self, request: WorkflowRequest) -> QueryPlan:
        if request.mode == WorkflowMode.BULK_DISCOVERY:
            return QueryPlan(
                should_continue=True,
                queries=request.preset_keywords[: self.max_queries_per_round],
                reason="使用行业预设关键词启动批量事件发现",
            )

        seed = request.seed_event
        assert seed is not None
        context = {
            "phase": "initial_event_expansion",
            "industry": request.industry,
            "seedEvent": {
                "eventId": seed.event_id,
                "title": seed.title,
                "summary": seed.summary,
                "eventTypes": seed.event_types,
                "entities": seed.entities,
                "evidenceTitles": [item.title for item in seed.evidence if item.title],
            },
            "seenQueries": [],
            "maxQueries": self.max_queries_per_round,
        }
        return self._generate_plan(context)

    def next_plan(
        self,
        request: WorkflowRequest,
        snapshot: EventProcessingResult,
        seen_queries: set[str],
        round_index: int,
    ) -> QueryPlan:
        seen_keys = {" ".join(query.split()).casefold() for query in seen_queries}
        pending = [
            query for query in request.preset_keywords
            if " ".join(query.split()).casefold() not in seen_keys
        ] if request.mode == WorkflowMode.BULK_DISCOVERY else []
        if pending:
            return QueryPlan(
                should_continue=True,
                queries=pending[:self.max_queries_per_round],
                reason="继续执行尚未搜索的行业预设关键词",
            )
        context = {
            "phase": "next_round",
            "industry": request.industry,
            "mode": request.mode.value,
            "completedRound": round_index,
            "seenQueries": sorted(seen_queries),
            "maxQueries": self.max_queries_per_round,
            "events": [
                {
                    "eventId": event.event_id,
                    "title": event.title,
                    "summary": event.summary,
                    "eventTypes": event.event_types,
                    "entities": event.entities,
                    "evidenceCount": len(event.evidence),
                }
                for event in snapshot.events[:50]
            ],
            "classifications": [
                item.model_dump(by_alias=True)
                for item in snapshot.classifications[:50]
            ],
            "chains": [
                item.model_dump(by_alias=True) for item in snapshot.chains[:30]
            ],
        }
        return self._generate_plan(context)

    def _generate_plan(self, context: dict) -> QueryPlan:
        schema = QueryPlan.model_json_schema(by_alias=True)
        feedback: str | None = None
        last_error: Exception | None = None

        for _ in range(self.max_attempts):
            parts = [
                "请根据上下文决定是否需要继续采集，并生成下一轮查询。",
                "输出 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False),
                "上下文：\n" + json.dumps(context, ensure_ascii=False),
            ]
            if feedback:
                parts.append("上一次输出校验失败，请修正：\n" + feedback)
            try:
                payload = generate_json(
                    self.llm,
                    QUERY_PLANNER_SYSTEM_PROMPT,
                    "\n\n".join(parts),
                )
                plan = QueryPlan.model_validate(payload)
                plan.queries = plan.queries[: self.max_queries_per_round]
                return plan
            except (ValidationError, ValueError, TypeError) as exc:
                last_error = exc
                feedback = validation_feedback(exc)

        raise QueryPlannerError(
            f"查询规划在 {self.max_attempts} 次尝试后仍未通过校验: {last_error}"
        ) from last_error
