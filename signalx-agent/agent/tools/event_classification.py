"""为 Event 生成受控主分类和细分标签的结构化 Tool。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from ..llm import StructuredLLM, generate_json, validation_feedback
from ..models import Event
from ..prompts import EVENT_CLASSIFICATION_SYSTEM_PROMPT
from ..tool_models import (
    EventClassificationBatch,
    EventClassificationRequest,
    EventClassificationResult,
)
from ..workflow_models import EventClassification


class EventClassificationToolError(RuntimeError):
    """事件分类在限定重试次数内未能得到合法结果。"""


class EventClassificationTool:
    """使用受控枚举分类 Event，并强制校验 Event ID 完整覆盖。"""

    name = "event_classification"
    description = "为每个 Event 生成一个受控主分类、细分标签和置信度。"
    input_model = EventClassificationRequest
    output_model = EventClassificationResult

    def __init__(
        self,
        llm: StructuredLLM,
        max_attempts: int = 2,
        max_events_per_call: int = 30,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于等于 1")
        if max_events_per_call < 1:
            raise ValueError("max_events_per_call 必须大于等于 1")
        self.llm = llm
        self.max_attempts = max_attempts
        self.max_events_per_call = max_events_per_call

    def invoke(
        self,
        request: EventClassificationRequest | dict[str, Any],
    ) -> EventClassificationResult:
        normalized = (
            request
            if isinstance(request, EventClassificationRequest)
            else EventClassificationRequest.model_validate(request)
        )
        if not normalized.events:
            return EventClassificationResult()

        classifications: list[EventClassification] = []
        for chunk in _chunks(normalized.events, self.max_events_per_call):
            classifications.extend(
                self._classify_chunk(chunk, normalized.industry)
            )
        return EventClassificationResult(classifications=classifications)

    def _classify_chunk(
        self,
        events: list[Event],
        industry: str | None,
    ) -> list[EventClassification]:
        schema = EventClassificationBatch.model_json_schema(by_alias=True)
        context = {
            "industry": industry,
            "events": [
                {
                    "eventId": event.event_id,
                    "title": event.title,
                    "summary": event.summary,
                    "eventTime": (
                        event.event_time.isoformat() if event.event_time else None
                    ),
                    "entities": event.entities,
                    "evidence": [
                        {
                            "evidenceId": item.evidence_id,
                            "title": item.title,
                            "content": item.content,
                            "snippet": item.snippet,
                            "source": item.source,
                        }
                        for item in event.evidence
                    ],
                }
                for event in events
            ],
        }
        expected_ids = {event.event_id for event in events}
        feedback: str | None = None
        last_error: Exception | None = None

        for _ in range(self.max_attempts):
            parts = [
                "请为每个输入 Event 选择一个主分类。",
                "输出 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False),
                "输入：\n" + json.dumps(context, ensure_ascii=False),
            ]
            if feedback:
                parts.append("上一次输出校验失败，请修正：\n" + feedback)
            try:
                payload = generate_json(
                    self.llm,
                    EVENT_CLASSIFICATION_SYSTEM_PROMPT,
                    "\n\n".join(parts),
                )
                batch = EventClassificationBatch.model_validate(payload)
                self._validate_event_coverage(batch.classifications, expected_ids)
                return batch.classifications
            except (ValidationError, ValueError, TypeError) as exc:
                last_error = exc
                feedback = validation_feedback(exc)

        raise EventClassificationToolError(
            f"事件分类在 {self.max_attempts} 次尝试后仍未通过校验: {last_error}"
        ) from last_error

    @staticmethod
    def _validate_event_coverage(
        classifications: list[EventClassification],
        expected_ids: set[str],
    ) -> None:
        returned_ids = [item.event_id for item in classifications]
        duplicate_ids = sorted(
            item_id for item_id in set(returned_ids) if returned_ids.count(item_id) > 1
        )
        if duplicate_ids:
            raise ValueError(
                "分类结果重复引用 eventId: " + ", ".join(duplicate_ids)
            )

        returned_id_set = set(returned_ids)
        missing_ids = expected_ids - returned_id_set
        unknown_ids = returned_id_set - expected_ids
        problems: list[str] = []
        if missing_ids:
            problems.append("遗漏 eventId: " + ", ".join(sorted(missing_ids)))
        if unknown_ids:
            problems.append("未知 eventId: " + ", ".join(sorted(unknown_ids)))
        if problems:
            raise ValueError("；".join(problems))


def _chunks(values: list[Event], size: int) -> Iterable[list[Event]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
