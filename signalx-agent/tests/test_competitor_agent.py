from __future__ import annotations

import unittest
from typing import Any

from pydantic import ValidationError

from agent.competitor import CompetitorAgent, CompetitorAgentError
from agent.models import Event


VALID_DRAFT = {
    "subjectCompany": "云智科技",
    "actionType": "PRODUCT_LAUNCH",
    "product": {
        "name": "云智 AI 客服",
        "category": "智能客服",
        "features": ["多轮对话", "企业知识库问答"],
        "pricing": None,
        "targetCustomers": ["中小企业"],
        "positioning": None,
    },
    "keyFindings": [
        {
            "finding": "云智科技推出面向中小企业的 AI 客服产品",
            "evidenceIds": ["evd_001"],
            "confidence": 0.92,
        }
    ],
    "summary": "云智科技进入中小企业智能客服市场。",
    "uncertainties": ["证据未提供产品价格"],
    "confidence": 0.86,
}


def build_event() -> Event:
    return Event.model_validate(
        {
            "eventId": "evt_001",
            "title": "云智科技发布 AI 客服产品",
            "eventTypes": ["PRODUCT_LAUNCH"],
            "entities": ["云智科技"],
            "evidence": [
                {
                    "evidenceId": "evd_001",
                    "title": "云智科技发布 AI 客服产品",
                    "content": "产品面向中小企业，支持多轮对话和知识库问答。",
                    "source": "云智科技官网",
                }
            ],
        }
    )


class FakeLLM:
    model_name = "fake-model"

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        self.calls.append(user_prompt)
        return self.responses.pop(0)


class CompetitorAgentTests(unittest.TestCase):
    def test_generates_valid_analysis_and_fixed_metadata(self) -> None:
        llm = FakeLLM([VALID_DRAFT.copy()])
        result = CompetitorAgent(llm).analyze(build_event())

        self.assertEqual(result.event_id, "evt_001")
        self.assertEqual(result.agent_type, "COMPETITOR")
        self.assertEqual(result.model_info.model, "fake-model")
        self.assertEqual(result.model_info.prompt_version, "competitor.v1")
        self.assertEqual(result.key_findings[0].evidence_ids, ["evd_001"])

    def test_retries_when_model_references_unknown_evidence(self) -> None:
        invalid = {
            **VALID_DRAFT,
            "keyFindings": [
                {
                    "finding": "没有证据支持的结论",
                    "evidenceIds": ["evd_not_exists"],
                    "confidence": 0.99,
                }
            ],
        }
        llm = FakeLLM([invalid, VALID_DRAFT.copy()])
        result = CompetitorAgent(llm, max_attempts=2).analyze(build_event())

        self.assertEqual(len(llm.calls), 2)
        self.assertIn("不存在的 evidenceId", llm.calls[1])
        self.assertEqual(result.key_findings[0].evidence_ids, ["evd_001"])

    def test_raises_after_all_attempts_fail(self) -> None:
        invalid = {**VALID_DRAFT, "confidence": 2.0}
        llm = FakeLLM([invalid.copy(), invalid.copy()])

        with self.assertRaises(CompetitorAgentError):
            CompetitorAgent(llm, max_attempts=2).analyze(build_event())

    def test_event_rejects_duplicate_evidence_ids(self) -> None:
        payload = build_event().model_dump(by_alias=True)
        payload["evidence"].append(payload["evidence"][0].copy())

        with self.assertRaises(ValidationError):
            Event.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
