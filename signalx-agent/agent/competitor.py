"""固定工作流的竞品 Agent 第一版。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .llm import StructuredLLM, generate_json, validation_feedback
from .models import (
    CompetitorAnalysis,
    CompetitorDraft,
    Event,
    ModelInfo,
)
from .prompts import COMPETITOR_SYSTEM_PROMPT


class CompetitorAgentError(RuntimeError):
    """竞品 Agent 在限定重试次数内无法生成有效结果。"""


class CompetitorAgent:
    """将标准 Event 转换为带证据引用的竞品分析。"""

    agent_type = "COMPETITOR"

    def __init__(
        self,
        llm: StructuredLLM,
        prompt_version: str = "competitor.v1",
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于等于 1")
        self.llm = llm
        self.prompt_version = prompt_version
        self.max_attempts = max_attempts

    def analyze(self, event: Event | dict[str, Any]) -> CompetitorAnalysis:
        """执行竞品分析并校验所有证据引用。"""
        normalized_event = event if isinstance(event, Event) else Event.model_validate(event)
        last_error: Exception | None = None
        feedback: str | None = None

        for _ in range(self.max_attempts):
            user_prompt = self._build_user_prompt(normalized_event, feedback)
            try:
                payload = generate_json(
                    self.llm,
                    COMPETITOR_SYSTEM_PROMPT,
                    user_prompt,
                )
                draft = CompetitorDraft.model_validate(payload)
                self._validate_evidence_references(normalized_event, draft)
                return CompetitorAnalysis(
                    **draft.model_dump(),
                    event_id=normalized_event.event_id,
                    agent_type=self.agent_type,
                    model_info=ModelInfo(
                        model=self.llm.model_name,
                        prompt_version=self.prompt_version,
                    ),
                )
            except (ValidationError, ValueError, TypeError) as exc:
                last_error = exc
                feedback = self._format_validation_feedback(exc)

        raise CompetitorAgentError(
            f"竞品分析在 {self.max_attempts} 次尝试后仍未通过校验: {last_error}"
        ) from last_error

    @staticmethod
    def _validate_evidence_references(event: Event, draft: CompetitorDraft) -> None:
        available_ids = {item.evidence_id for item in event.evidence}
        referenced_ids = {
            evidence_id
            for finding in draft.key_findings
            for evidence_id in finding.evidence_ids
        }
        unknown_ids = referenced_ids - available_ids
        if unknown_ids:
            raise ValueError(
                "keyFindings 引用了不存在的 evidenceId: "
                + ", ".join(sorted(unknown_ids))
            )

    def _build_user_prompt(self, event: Event, feedback: str | None) -> str:
        schema = CompetitorDraft.model_json_schema(by_alias=True)
        parts = [
            "请分析下面的 Event。",
            "\n输出 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False),
            "\nEvent：\n" + event.model_dump_json(by_alias=True),
        ]
        if feedback:
            parts.append(
                "\n上一次输出没有通过校验，请修正以下问题后重新输出完整 JSON：\n"
                + feedback
            )
        return "\n".join(parts)

    @staticmethod
    def _format_validation_feedback(exc: Exception) -> str:
        return validation_feedback(exc)
