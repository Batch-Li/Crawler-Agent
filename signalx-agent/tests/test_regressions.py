"""审阅中复现问题的回归测试；使用实际 Tool/处理器，无网络请求。"""

from __future__ import annotations

import json
import unittest

from agent import (
    Event, EventDiscoveryWorkflow, EventExtractionTool, Evidence,
    LLMQueryPlanner, SignalXEventProcessor,
)
from agent.tools import EventChainTool
from agent.workflow_models import (
    EventClassification, EventProcessingResult, QueryPlan, StopReason, WorkflowRequest,
)
from tests.test_workflow import FakeCollector, FakePlanner, FakeProcessor
from tests.test_event_tools import chain_response


def evidence(key="evd_1", title="甲公司发布产品 A", **kwargs):
    return Evidence(evidence_id=key, title=title, source="本地测试", **kwargs)


def draft(item, **kwargs):
    return {
        "title": item.title, "entities": ["甲公司"],
        "evidenceIds": [item.evidence_id],
        "anchor": {"evidenceId": item.evidence_id, "quote": item.title},
        **kwargs,
    }


def request(**kwargs):
    return WorkflowRequest.model_validate({
        "mode": "BULK_DISCOVERY", "industry": "企业服务",
        "presetKeywords": ["查询一"], **kwargs,
    })


def planner(*queries):
    plans = [QueryPlan(should_continue=True, queries=[query], reason="测试查询") for query in queries]
    return FakePlanner(plans[0], [*plans[1:], QueryPlan(should_continue=False, reason="完成")])


class LocalLLM:
    model_name = "local-regression"

    def __init__(self, extraction_responses=None, plan_responses=None):
        self.extraction_responses = list(extraction_responses or [])
        self.plan_responses = list(plan_responses or [])
        self.calls = []
        self.category = "PRODUCT_LAUNCH"

    def generate_json(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        if "查询规划器" in system_prompt:
            return self.plan_responses.pop(0)
        context = json.JSONDecoder().raw_decode(user_prompt.split("输入：\n", 1)[1])[0]
        if "同一事件复核工具" in system_prompt:
            return {"groups": []}
        if "事件抽取工具" in system_prompt:
            if self.extraction_responses:
                result = self.extraction_responses.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
            return {"events": [draft(Evidence.model_validate(item)) for item in context["evidence"]]}
        if "事件关联工具" in system_prompt:
            return chain_response([Event.model_validate(e) for e in context["events"]])
        return {"classifications": [
            {"eventId": item["eventId"], "category": self.category, "confidence": .9}
            for item in context["events"]
        ]}


class RegressionTests(unittest.TestCase):
    def test_invalid_query_plan_retries_with_serializable_feedback(self):
        llm = LocalLLM(plan_responses=[
            {"shouldContinue": True, "queries": [], "reason": "缺信息"},
            {"shouldContinue": False, "reason": "结束"},
        ])
        result = LLMQueryPlanner(llm).next_plan(request(), EventProcessingResult(), {"查询一"}, 1)
        self.assertFalse(result.should_continue)
        self.assertEqual(len(llm.calls), 2)

    def test_blank_evidence_reference_and_fabricated_quote_retry(self):
        item = evidence()
        for invalid in (
            draft(item, evidenceIds=["   "]),
            draft(item, anchor={"evidenceId": item.evidence_id, "quote": "不存在的原文"}),
            draft(item, matchedEventId="不存在的历史事件"),
        ):
            with self.subTest(invalid=invalid):
                llm = LocalLLM(extraction_responses=[{"events": [invalid]}, {"events": [draft(item)]}])
                result = EventExtractionTool(llm).invoke({"evidence": [item]})
                self.assertEqual(len(result.events), 1)
                self.assertEqual(len(llm.calls), 2)

    def test_evidence_ids_are_case_sensitive(self):
        items = [evidence("EVD_A"), evidence("evd_a")]
        llm = LocalLLM(extraction_responses=[{"events": [draft(items[0], evidenceIds=["EVD_A", "evd_a"])]}])
        result = EventExtractionTool(llm).invoke({"evidence": items})
        self.assertEqual({item.evidence_id for item in result.events[0].evidence}, {"EVD_A", "evd_a"})

    def test_title_rewording_does_not_change_identity_or_duplicate_event(self):
        item = evidence()
        variants = [draft(item), draft(item, title="产品 A 由甲公司正式推出", eventTime="2026-09-01T09:00:00+08:00")]
        llm = LocalLLM(extraction_responses=[{"events": [variants[0]]}, {"events": variants}])
        tool = EventExtractionTool(llm)
        first = tool.invoke({"evidence": [item]})
        second = tool.invoke({"evidence": [item]})
        self.assertEqual(len(second.events), 1)
        self.assertEqual(first.events[0].event_id, second.events[0].event_id)

    def test_two_actions_in_one_article_remain_distinct(self):
        item = evidence(content="甲公司发布产品 A；甲公司召回产品 B。")
        variants = [
            draft(item),
            draft(item, title="甲公司召回产品 B", anchor={"evidenceId": item.evidence_id, "quote": "甲公司召回产品 B"}),
        ]
        result = EventExtractionTool(LocalLLM(extraction_responses=[{"events": variants}])).invoke({"evidence": [item]})
        self.assertEqual(len(result.events), 2)
        self.assertEqual(len({event.event_id for event in result.events}), 2)

    def test_new_source_matches_seed_with_one_classification_and_original_id(self):
        old, new = evidence(), evidence("evd_2", "产品 A 已由甲公司发布")
        seed = Event(event_id="evt_seed", title=old.title, entities=["甲公司"], evidence=[old])
        llm = LocalLLM(extraction_responses=[{"events": [draft(new, matchedEventId=seed.event_id)]}])
        result = EventDiscoveryWorkflow(
            FakeCollector({"查询一": [new]}), SignalXEventProcessor.from_llm(llm), planner("查询一"),
        ).run(request(mode="EVENT_EXPANSION", seedEvent=seed))
        self.assertEqual([event.event_id for event in result.events], [seed.event_id])
        self.assertEqual([item.event_id for item in result.classifications], [seed.event_id])
        self.assertEqual({item.evidence_id for item in result.events[0].evidence}, {"evd_1", "evd_2"})
        self.assertEqual(len(seed.evidence), 1)

    def test_seed_is_classified_even_when_planner_stops_immediately(self):
        item = evidence()
        seed = Event(event_id="evt_seed", title=item.title, evidence=[item])
        result = EventDiscoveryWorkflow(
            FakeCollector({}), SignalXEventProcessor.from_llm(LocalLLM()),
            FakePlanner(QueryPlan(should_continue=False, reason="不需要补充"), []),
        ).run(request(mode="EVENT_EXPANSION", seedEvent=seed))
        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(result.collection_calls, 0)
        self.assertEqual([c.event_id for c in result.classifications], [seed.event_id])

    def test_seed_anchor_lookup_preserves_legacy_id_on_later_rewording(self):
        item = evidence()
        seed = Event(event_id="legacy_id", title=item.title, evidence=[item])
        llm = LocalLLM(extraction_responses=[
            {"events": [draft(item, matchedEventId=seed.event_id)]},
            {"events": [draft(item, title="产品 A 已由甲公司推出")]},
        ])
        tool = EventExtractionTool(llm)
        first = tool.invoke({"evidence": [item], "existingEvents": [seed]})
        second = tool.invoke({"evidence": [item], "existingEvents": first.events})
        self.assertEqual([event.event_id for event in second.events], [seed.event_id])

    def test_model_budget_failure_preserves_prior_snapshot_and_pending_evidence(self):
        llm = LocalLLM()
        result = EventDiscoveryWorkflow(
            FakeCollector({"查询一": [evidence()], "查询二": [evidence("evd_2", "甲公司发布产品 B")]}),
            SignalXEventProcessor.from_llm(llm), planner("查询一", "查询二"),
        ).run(request(maxLlmCalls=3))
        self.assertEqual(result.stop_reason, StopReason.MODEL_BUDGET_EXHAUSTED)
        self.assertEqual(result.llm_calls, 3)
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.unprocessed_evidence_ids, ["evd_2"])
        self.assertEqual(result.rounds[-1].processing_status, "failed")
        self.assertEqual(result.rounds[0].snapshot, result.rounds[1].snapshot)

    def test_timeout_preserves_successful_round_and_sanitizes_error(self):
        llm = LocalLLM(extraction_responses=[{"events": [draft(evidence())]}, TimeoutError("SECRET_TEST_VALUE")])
        result = EventDiscoveryWorkflow(
            FakeCollector({"查询一": [evidence()], "查询二": [evidence("evd_2")]}),
            SignalXEventProcessor.from_llm(llm), planner("查询一", "查询二"),
        ).run(request())
        self.assertEqual(result.stop_reason, StopReason.FAILED)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.errors[0].error_type, "TimeoutError")
        self.assertNotIn("SECRET_TEST_VALUE", result.model_dump_json())

    def test_planning_failure_returns_successful_processing(self):
        class BrokenPlanner(FakePlanner):
            def next_plan(self, *args):
                raise TimeoutError("模拟规划超时")
        result = EventDiscoveryWorkflow(
            FakeCollector({"查询一": [evidence()]}), SignalXEventProcessor.from_llm(LocalLLM()),
            BrokenPlanner(QueryPlan(should_continue=True, queries=["查询一"], reason="启动"), []),
        ).run(request())
        self.assertEqual(result.stop_reason, StopReason.FAILED)
        self.assertEqual(result.errors[0].stage, "planning")
        self.assertEqual(len(result.events), 1)
        self.assertFalse(result.unprocessed_evidence_ids)

    def test_unchanged_evidence_is_skipped_and_updates_are_processed(self):
        item = evidence()
        updated = item.model_copy(update={"content": "补充产品定价"})
        processor = FakeProcessor()
        result = EventDiscoveryWorkflow(
            FakeCollector({"查询一": [item], "查询二": [item], "查询三": [updated]}),
            processor, planner("查询一", "查询二", "查询三"),
        ).run(request(maxRounds=4, maxNoProgressRounds=2))
        self.assertEqual([r.processing_status for r in result.rounds], ["completed", "skipped", "completed"])
        self.assertEqual(len(processor.received), 2)
        self.assertEqual(result.rounds[2].updated_evidence_count, 1)
        self.assertFalse(result.unprocessed_evidence_ids)

    def test_only_changed_events_are_reclassified(self):
        llm = LocalLLM()
        processor = SignalXEventProcessor.from_llm(llm)
        first = processor.process([evidence()])
        second = processor.process([evidence("evd_2", "甲公司发布产品 B")], previous=first)
        classification_inputs = [
            json.JSONDecoder().raw_decode(user.split("输入：\n", 1)[1])[0]
            for system, user in llm.calls if "事件分类工具" in system
        ]
        self.assertEqual([len(item["events"]) for item in classification_inputs], [1, 1])
        self.assertEqual(len(second.events), 2)
        self.assertEqual(len(second.chains), 1)

    def test_reclassification_replaces_old_routing_category(self):
        llm = LocalLLM()
        processor = SignalXEventProcessor.from_llm(llm)
        item = evidence()
        first = processor.process([item])
        llm.category = "PRICE_CHANGE"
        second = processor.process([item.model_copy(update={"content": "更新价格说明"})], previous=first)
        self.assertEqual(second.events[0].event_id, first.events[0].event_id)
        self.assertEqual(second.events[0].event_types, ["PRICE_CHANGE"])
        self.assertEqual(first.events[0].event_types, ["PRODUCT_LAUNCH"])

    def test_all_presets_are_queued_including_after_empty_first_round(self):
        keywords = [f"关键词{i}" for i in range(6)]
        llm = LocalLLM(plan_responses=[{"shouldContinue": False, "reason": "完成"}])
        collector = FakeCollector({keywords[-1]: [evidence()]})
        result = EventDiscoveryWorkflow(collector, FakeProcessor(), LLMQueryPlanner(llm)).run(
            request(presetKeywords=keywords, maxToolCalls=10),
        )
        self.assertEqual([call.query for call in collector.calls], keywords)
        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual([len(r.tool_calls) for r in result.rounds], [5, 1])
        self.assertFalse(result.pending_queries)

    def test_budget_reports_unexecuted_keywords(self):
        keywords = [f"关键词{i}" for i in range(6)]
        result = EventDiscoveryWorkflow(
            FakeCollector({keywords[0]: [evidence()]}), FakeProcessor(), LLMQueryPlanner(LocalLLM()),
        ).run(request(presetKeywords=keywords, maxToolCalls=2))
        self.assertEqual(result.stop_reason, StopReason.TOOL_BUDGET_EXHAUSTED)
        self.assertEqual(result.pending_queries, keywords[2:])

    def test_model_budget_is_reset_per_run(self):
        llm = LocalLLM()
        workflow = EventDiscoveryWorkflow(
            FakeCollector({"查询一": [evidence()]}), SignalXEventProcessor.from_llm(llm), LLMQueryPlanner(llm),
        )
        for _ in range(2):
            result = workflow.run(request(maxLlmCalls=1, maxToolCalls=1))
            self.assertEqual(result.llm_calls, 1)
            self.assertEqual(result.stop_reason, StopReason.MODEL_BUDGET_EXHAUSTED)
        self.assertEqual(len(llm.calls), 2)

    def test_chains_compare_actual_times_and_treat_naive_time_as_unknown(self):
        events = [Event(
            event_id=key, title=key, event_time=time, entities=["甲公司"], evidence=[evidence()],
        ) for key, time in (
            ("earlier", "2026-09-01T09:00:00+08:00"),
            ("later", "2026-09-01T02:00:00+00:00"),
            ("unknown", "2020-01-01T00:00:00"),
        )]
        result = EventChainTool(LocalLLM()).invoke({
            "events": events,
            "classifications": [EventClassification(
                event_id=event.event_id, category="PRODUCT_LAUNCH", confidence=.9,
            ) for event in events],
        })
        self.assertEqual(result.chains[0].event_ids, ["earlier", "later", "unknown"])
        self.assertIsNone(events[-1].event_time)


if __name__ == "__main__":
    unittest.main()
