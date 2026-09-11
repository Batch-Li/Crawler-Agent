from __future__ import annotations

import unittest
from typing import Any

from agent.models import Event, Evidence
from agent.query_planner import LLMQueryPlanner
from agent.workflow_models import WorkflowMode, WorkflowRequest


class FakeLLM:
    model_name = "fake-model"

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls = 0

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        self.calls += 1
        return self.responses.pop(0)


class QueryPlannerTests(unittest.TestCase):
    def test_bulk_mode_uses_presets_without_llm(self) -> None:
        llm = FakeLLM([])
        request = WorkflowRequest(
            mode=WorkflowMode.BULK_DISCOVERY,
            industry="智能客服",
            preset_keywords=["AI 客服", "客服机器人"],
        )

        plan = LLMQueryPlanner(llm).initial_plan(request)

        self.assertEqual(plan.queries, ["AI 客服", "客服机器人"])
        self.assertEqual(llm.calls, 0)

    def test_event_expansion_uses_llm_to_generate_initial_queries(self) -> None:
        llm = FakeLLM(
            [
                {
                    "shouldContinue": True,
                    "queries": ["A 公司 AI 客服 新品", "A 公司 AI 客服 价格"],
                    "reason": "补充产品与价格信息",
                }
            ]
        )
        request = WorkflowRequest(
            mode=WorkflowMode.EVENT_EXPANSION,
            industry="智能客服",
            seed_event=Event(
                event_id="evt_seed",
                title="A 公司进入智能客服市场",
                entities=["A 公司"],
                evidence=[
                    Evidence(
                        evidence_id="evd_seed",
                        title="A 公司发布新品",
                        source="测试来源",
                    )
                ],
            ),
        )

        plan = LLMQueryPlanner(llm).initial_plan(request)

        self.assertTrue(plan.should_continue)
        self.assertEqual(len(plan.queries), 2)
        self.assertEqual(llm.calls, 1)


if __name__ == "__main__":
    unittest.main()
