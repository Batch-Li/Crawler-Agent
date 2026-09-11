"""真实联调暴露的相关性、主体别名及收敛问题。无联网请求。"""
import json
import tempfile
import unittest
from pathlib import Path

from agent.crawler_integration import build_crawler_workflow, load_entity_aliases
from agent.entity_resolution import EntityResolver
from agent.models import Event, Evidence
from agent.llm import model_call_budget
from agent.tools import EventChainTool
from agent.tools.evidence_relevance import EvidenceRelevanceTool, RELEVANCE_PROMPT
from agent.workflow import EventDiscoveryWorkflow
from agent.workflow_models import (
    CollectionResult, EventClassification, QueryPlan, StopReason, WorkflowRequest,
)
from tests.test_crawler_integration import FixtureLLM, item
from tests.test_workflow import FakeCollector, FakePlanner, FakeProcessor


def evidence(key, title):
    return Evidence(evidence_id=key, title=title, source="test")


def request(**extra):
    return WorkflowRequest(mode="EVENT_EXPANSION", industry="智能穿戴", seed_event=Event(
        event_id="seed", title="华为 WATCH GT7 发布", entities=["华为技术"],
        evidence=[evidence("seed-evidence", "华为 WATCH GT7 发布")],
    ), **extra)


class ReviewLLM:
    model_name = "offline-review"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate_json(self, system, user):
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class LiveRegressionTests(unittest.TestCase):
    def test_aliases_normalize_model_context_without_forcing_a_chain(self):
        events = [Event(event_id=str(i), title="产品发布", entities=[name],
                        evidence=[evidence(str(i), "新品发布")])
                  for i, name in enumerate(["华为技术", "Huawei", "华为云"])]
        types = [EventClassification(event_id=e.event_id, category="PRODUCT_LAUNCH", confidence=.9) for e in events]
        class Model:
            model_name = "alias-fixture"

            def generate_json(self, system, user):
                self.context = json.loads(user.split("输入：\n", 1)[1])
                return {"chains": []}

        model = Model()
        tool = EventChainTool(model, entity_aliases={"华为": "华为技术", "Huawei": "华为技术"})
        result = tool.invoke({"events": events, "classifications": types})
        self.assertEqual(result.chains, [])
        self.assertEqual(result.unlinked_event_ids, ["0", "1", "2"])
        self.assertEqual([e["entities"] for e in model.context["events"]],
                         [["华为技术"], ["华为技术"], ["华为云"]])
        self.assertEqual(events[1].entities, ["Huawei"])
        normalized = EntityResolver({"华为": "华为技术"}).normalize(request().seed_event)
        self.assertEqual(normalized.event_id, "seed")

    def test_alias_file_ignores_products_disabled_entities_and_detects_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entities.json"
            path.write_text(json.dumps({"entities": [
                {"entityId": "1", "canonicalName": "甲公司", "aliases": ["Alpha"],
                 "products": [{"name": "Phone7"}]},
                {"entityId": "2", "canonicalName": "乙公司", "aliases": ["Beta"], "enabled": False},
            ]}), encoding="utf-8")
            aliases = load_entity_aliases(path)
            self.assertEqual(aliases["Alpha"], "甲公司")
            self.assertNotIn("Phone7", aliases)
            self.assertNotIn("Beta", aliases)
        with self.assertRaisesRegex(ValueError, "冲突"):
            EntityResolver({"Alpha": "甲公司", "ALPHA": "乙公司"})

    def test_relevance_requires_exact_id_coverage_and_retries(self):
        rows = [evidence("old", "GT3 发布"), evidence("new", "GT7 发布")]
        model = ReviewLLM([
            {"reviews": [{"evidenceId": "invented", "decision": "relevant", "reason": "错误引用"}]},
            {"reviews": [
                {"evidenceId": "old", "decision": "irrelevant", "reason": "其他型号"},
                {"evidenceId": "new", "decision": "relevant", "reason": "同一型号的直接报道"},
            ]},
        ])
        with model_call_budget(2) as budget:
            result = EvidenceRelevanceTool(model).review(request(), rows)
        self.assertEqual(budget.calls, 2)
        self.assertEqual([r.decision for r in result], ["irrelevant", "relevant"])

    def test_filtered_evidence_keeps_raw_content_and_stops_without_expanding(self):
        old = evidence("old", "2021 年华为 WATCH GT3 发布")
        model = ReviewLLM([{"reviews": [{"evidenceId": "old", "decision": "irrelevant", "reason": "仅涉及旧型号"}]}])
        planner = FakePlanner(QueryPlan(should_continue=True, queries=["GT7 发布", "GT7 价格"],
                                       reason="核实", unresolved_questions=["GT7 是否发布？"]), [])
        processor = FakeProcessor()
        collector = FakeCollector({"GT7 发布": [old]})
        result = EventDiscoveryWorkflow(collector, processor, planner, EvidenceRelevanceTool(model)).run(
            request(per_round_query_limit=1))
        self.assertEqual(result.stop_reason, StopReason.NO_PROGRESS)
        self.assertEqual(result.pending_queries, ["GT7 价格"])
        self.assertEqual(result.unresolved_questions, ["GT7 是否发布？"])
        self.assertEqual(result.filtered_evidence[0].evidence, old)
        self.assertEqual(result.rounds[0].collected_evidence, [old])
        self.assertEqual(processor.received, [["seed-evidence"]])
        self.assertEqual(planner.next_calls, 0)
        self.assertEqual(len(result.events), 1)

    def test_rejected_identical_evidence_is_not_reviewed_again(self):
        old = evidence("old", "GT3 发布")
        model = ReviewLLM([{"reviews": [{"evidenceId": "old", "decision": "uncertain", "reason": "缺少目标型号"}]}])
        planner = FakePlanner(QueryPlan(should_continue=True, queries=["一", "二", "三"], reason="核实"), [])
        result = EventDiscoveryWorkflow(FakeCollector({"一": [old], "二": [old]}), FakeProcessor(),
            planner, EvidenceRelevanceTool(model)).run(request(per_round_query_limit=1, max_no_progress_rounds=2))
        self.assertEqual(result.stop_reason, StopReason.NO_PROGRESS)
        self.assertEqual(result.collection_calls, 2)
        self.assertEqual(model.calls, 1)
        self.assertEqual(result.filtered_evidence[0].decision, "uncertain")

    def test_filter_failure_and_exhausted_budget_preserve_unreviewed_evidence(self):
        new = evidence("new", "GT7 价格")
        for failure, cap in ((TimeoutError("DO_NOT_LOG_TEST_VALUE"), 3), (None, 0)):
            with self.subTest(cap=cap):
                model = ReviewLLM([failure])
                planner = FakePlanner(QueryPlan(should_continue=True, queries=["价格"], reason="核实"), [])
                result = EventDiscoveryWorkflow(FakeCollector({"价格": [new]}), FakeProcessor(),
                    planner, EvidenceRelevanceTool(model)).run(request(max_llm_calls=cap))
                self.assertEqual(result.unreviewed_evidence, [new])
                self.assertEqual(result.errors[0].stage, "filtering")
                self.assertNotIn("DO_NOT_LOG_TEST_VALUE", result.model_dump_json())
                self.assertEqual(result.events[0].event_id, "seed")
                self.assertEqual(result.stop_reason, StopReason.FAILED if cap else StopReason.MODEL_BUDGET_EXHAUSTED)

    def test_empty_round_does_not_skip_remaining_mandatory_keywords(self):
        planner = FakePlanner(QueryPlan(should_continue=True, queries=["空", "有效"], reason="预设"),
                              [QueryPlan(should_continue=False, reason="结束")])
        result = EventDiscoveryWorkflow(FakeCollector({"有效": [evidence("new", "新品发布")]}),
            FakeProcessor(), planner).run(WorkflowRequest(mode="BULK_DISCOVERY", industry="智能穿戴",
                preset_keywords=["空", "有效"], per_round_query_limit=1))
        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(result.collection_calls, 2)

    def test_failed_sources_are_not_reported_as_business_no_progress(self):
        class FailedCollector:
            def search(self, request):
                return CollectionResult(status="failed")
        result = EventDiscoveryWorkflow(FailedCollector(), FakeProcessor(), FakePlanner(
            QueryPlan(should_continue=True, queries=["查询"], reason="核实"), [])).run(request())
        self.assertEqual(result.stop_reason, StopReason.PARTIAL)
        self.assertEqual(result.rounds[0].tool_calls[0].status, "failed")

    def test_factory_wires_relevance_and_alias_resolution(self):
        from unittest.mock import patch
        from crawler_tool.monitoring import alert_to_seed_event

        class Model(FixtureLLM):
            def generate_json(self, system, user):
                if system == RELEVANCE_PROMPT:
                    self.calls += 1
                    context = json.JSONDecoder().raw_decode(user.split("输入：\n", 1)[1])[0]
                    return {"reviews": [{"evidenceId": e["evidenceId"], "decision": "relevant",
                                         "reason": "新品直接报道"} for e in context["evidence"]]}
                return super().generate_json(system, user)

        model = Model()
        seed = alert_to_seed_event({"alertId": "seed", "subjectName": "智言科技", "evidence": [item()]})
        with patch("crawler_tool.monitoring.agent_bridge.SearchContentCollectionTool._invoke",
                   return_value={"items": [item("new")]}):
            result = build_crawler_workflow(model).run({"mode": "EVENT_EXPANSION", "industry": "手机", "seedEvent": seed})
        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(result.llm_calls, 5)
        self.assertEqual(result.rounds[0].evidence_reviews[0].decision, "relevant")


if __name__ == "__main__":
    unittest.main()
