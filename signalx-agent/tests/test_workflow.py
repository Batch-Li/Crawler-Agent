from __future__ import annotations

import unittest

from agent.models import Event, Evidence
from agent.workflow import EventDiscoveryWorkflow
from agent.workflow_models import (
    EventChain,
    EventClassification,
    EventProcessingResult,
    QueryPlan,
    SearchRequest,
    StopReason,
    WorkflowMode,
    WorkflowRequest,
)


def evidence(evidence_id: str, title: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        title=title,
        source="测试来源",
    )


class FakeCollector:
    name = "fake-collector"

    def __init__(self, results: dict[str, list[Evidence]]) -> None:
        self.results = results
        self.calls: list[SearchRequest] = []

    def search(self, request: SearchRequest) -> list[Evidence]:
        self.calls.append(request)
        return self.results.get(request.query, [])


class FakeProcessor:
    def __init__(self) -> None:
        self.received: list[list[str]] = []

    def process(self, items: list[Evidence], *, previous=None, industry=None, time_fallback=False) -> EventProcessingResult:
        self.received.append([item.evidence_id for item in items])
        previous = previous or EventProcessingResult()
        old_by_evidence = {
            item.evidence_id: event.event_id
            for event in previous.events for item in event.evidence
        }
        event_map = {event.event_id: event for event in previous.events}
        updates = [
            Event(
                event_id=old_by_evidence.get(item.evidence_id, f"evt_{item.evidence_id}"),
                title=item.title or item.evidence_id,
                event_types=["PRODUCT_LAUNCH"],
                entities=["测试公司"],
                evidence=[item],
            )
            for item in items
        ]
        event_map.update({event.event_id: event for event in updates})
        events = list(event_map.values())
        classifications = [
            EventClassification(
                event_id=event.event_id,
                category="PRODUCT_LAUNCH",
                confidence=0.9,
            )
            for event in events
        ]
        chains = []
        if events:
            chains.append(
                EventChain(
                    chain_id="chain_product_launch",
                    category="PRODUCT_LAUNCH",
                    title="产品发布事件链",
                    event_ids=[event.event_id for event in events],
                )
            )
        return EventProcessingResult(
            events=events,
            classifications=classifications,
            chains=chains,
        )


class FakePlanner:
    def __init__(self, initial: QueryPlan, next_plans: list[QueryPlan]) -> None:
        self.initial = initial
        self.next_plans = next_plans
        self.next_calls = 0

    def initial_plan(self, request: WorkflowRequest) -> QueryPlan:
        return self.initial

    def next_plan(
        self,
        request: WorkflowRequest,
        snapshot: EventProcessingResult,
        seen_queries: set[str],
        round_index: int,
    ) -> QueryPlan:
        self.next_calls += 1
        return self.next_plans.pop(0)


class EventDiscoveryWorkflowTests(unittest.TestCase):
    def test_bulk_discovery_runs_multiple_rounds_and_deduplicates_evidence(self) -> None:
        collector = FakeCollector(
            {
                "AI 客服": [evidence("evd_1", "新品发布")],
                "AI 客服 价格": [
                    evidence("evd_1", "新品发布"),
                    evidence("evd_2", "产品价格"),
                ],
            }
        )
        processor = FakeProcessor()
        planner = FakePlanner(
            QueryPlan(
                should_continue=True,
                queries=["AI 客服"],
                reason="行业预设关键词",
            ),
            [
                QueryPlan(
                    should_continue=True,
                    queries=["AI 客服 价格"],
                    reason="补充价格证据",
                ),
                QueryPlan(
                    should_continue=False,
                    reason="证据已经充分",
                ),
            ],
        )
        request = WorkflowRequest(
            mode=WorkflowMode.BULK_DISCOVERY,
            industry="企业服务",
            preset_keywords=["AI 客服"],
            max_rounds=3,
            max_tool_calls=4,
        )

        result = EventDiscoveryWorkflow(collector, processor, planner).run(request)

        self.assertEqual([call.query for call in collector.calls], ["AI 客服", "AI 客服 价格"])
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(len(result.rounds), 2)
        self.assertEqual(result.rounds[1].new_evidence_count, 1)
        self.assertEqual(result.stop_reason, StopReason.COMPLETED)

    def test_event_expansion_preserves_seed_event_and_evidence(self) -> None:
        seed = Event(
            event_id="evt_seed",
            title="A 公司发布产品",
            evidence=[evidence("evd_seed", "初始事件")],
        )
        collector = FakeCollector(
            {"A 公司 产品功能": [evidence("evd_2", "功能介绍")]}
        )
        processor = FakeProcessor()
        planner = FakePlanner(
            QueryPlan(
                should_continue=True,
                queries=["A 公司 产品功能"],
                reason="围绕种子事件扩展",
            ),
            [QueryPlan(should_continue=False, reason="扩展完成")],
        )
        request = WorkflowRequest(
            mode=WorkflowMode.EVENT_EXPANSION,
            industry="企业服务",
            seed_event=seed,
        )

        result = EventDiscoveryWorkflow(collector, processor, planner).run(request)

        self.assertEqual({item.evidence_id for item in result.evidence}, {"evd_seed", "evd_2"})
        self.assertIn("evt_seed", {event.event_id for event in result.events})
        self.assertEqual(processor.received[0], ["evd_seed", "evd_2"])

    def test_tool_call_budget_stops_extra_queries(self) -> None:
        collector = FakeCollector(
            {
                "查询一": [evidence("evd_1", "内容一")],
                "查询二": [evidence("evd_2", "内容二")],
            }
        )
        planner = FakePlanner(
            QueryPlan(
                should_continue=True,
                queries=["查询一", "查询二"],
                reason="批量查询",
            ),
            [],
        )
        request = WorkflowRequest(
            mode=WorkflowMode.BULK_DISCOVERY,
            industry="测试行业",
            preset_keywords=["查询一"],
            max_tool_calls=1,
        )

        result = EventDiscoveryWorkflow(
            collector,
            FakeProcessor(),
            planner,
        ).run(request)

        self.assertEqual([call.query for call in collector.calls], ["查询一"])
        self.assertEqual(result.stop_reason, StopReason.TOOL_BUDGET_EXHAUSTED)

    def test_repeated_query_stops_without_calling_tool_again(self) -> None:
        collector = FakeCollector({"重复查询": [evidence("evd_1", "内容一")]})
        planner = FakePlanner(
            QueryPlan(
                should_continue=True,
                queries=["重复查询"],
                reason="第一轮",
            ),
            [
                QueryPlan(
                    should_continue=True,
                    queries=[" 重复查询 "],
                    reason="错误地重复查询",
                )
            ],
        )
        request = WorkflowRequest(
            mode=WorkflowMode.BULK_DISCOVERY,
            industry="测试行业",
            preset_keywords=["重复查询"],
        )

        result = EventDiscoveryWorkflow(
            collector,
            FakeProcessor(),
            planner,
        ).run(request)

        self.assertEqual(len(collector.calls), 1)
        self.assertEqual(result.stop_reason, StopReason.NO_NEW_QUERIES)


if __name__ == "__main__":
    unittest.main()
