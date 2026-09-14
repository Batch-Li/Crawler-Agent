"""时间兜底：两阶段语义、默认关闭、timeline 重渲染幂等、工作流透传。"""

import unittest
from datetime import datetime, timezone

from agent.event_time import ground_event_time
from agent.models import Event, Evidence, PartialDate
from agent.timeline import render_timeline


def evidence(eid, *, title="证据", snippet=None, published=None, confidence=None, source="south_weekend"):
    payload = {"evidenceId": eid, "title": title, "source": source}
    if snippet:
        payload["snippet"] = snippet
    if published:
        payload["publishedAt"] = published
    if confidence is not None:
        payload["publishedAtConfidence"] = confidence
    return Evidence.model_validate(payload)


def event(*evidences, **extra):
    return Event.model_validate({
        "eventId": "evt_1", "title": "某公司发布新品",
        "evidence": list(evidences), **extra,
    })


class Stage1TextAnchorTests(unittest.TestCase):
    def test_unique_text_date_grounds_day_precision_with_anchor(self):
        result = ground_event_time(event(
            evidence("e1", snippet="该产品于 2025年3月29日 正式发布"),
            evidence("e2", title="另一样报道"),
        ), allow_fallback=True)
        self.assertEqual(result.event_date, PartialDate(value="2025-03-29", precision="day"))
        self.assertEqual(result.time_anchor.evidence_id, "e1")
        self.assertIn("待人工复核", result.time_warnings[-1])
        self.assertIsNone(result.event_time)

    def test_multiple_candidate_dates_only_warns(self):
        result = ground_event_time(event(
            evidence("e1", snippet="2025年3月29日 发布"),
            evidence("e2", snippet="回顾 2023-02-21 的旧款"),
        ), allow_fallback=True)
        self.assertIsNone(result.event_date)
        self.assertIsNone(result.time_anchor)
        self.assertTrue(any("2 个候选日期" in warning for warning in result.time_warnings))

    def test_no_candidates_falls_to_stage2(self):
        result = ground_event_time(event(
            evidence("e1", title="没有日期的报道", published="2026-08-23T02:20:00+00:00", confidence=0.85),
        ), allow_fallback=True)
        self.assertEqual(result.event_date, PartialDate(value="2026-08-23", precision="day"))
        self.assertIsNone(result.time_anchor)
        self.assertTrue(any("发布时间戳" in warning for warning in result.time_warnings))


class Stage2PublishedAtTests(unittest.TestCase):
    def test_consistent_high_confidence_days_ground_date(self):
        result = ground_event_time(event(
            evidence("e1", published="2026-08-23T02:20:00+00:00", confidence=0.85),
            evidence("e2", published="2026-08-23T10:00:00+00:00", confidence=1.0),
        ), allow_fallback=True)
        self.assertEqual(result.event_date, PartialDate(value="2026-08-23", precision="day"))
        self.assertIsNone(result.time_anchor)

    def test_conflicting_days_only_warns(self):
        result = ground_event_time(event(
            evidence("e1", published="2026-08-23T02:20:00+00:00", confidence=0.85),
            evidence("e2", published="2026-08-24T10:00:00+00:00", confidence=0.85),
        ), allow_fallback=True)
        self.assertIsNone(result.event_date)
        self.assertTrue(any("互相矛盾" in warning for warning in result.time_warnings))

    def test_low_confidence_stamps_are_ignored(self):
        result = ground_event_time(event(
            evidence("e1", published="2026-08-23T02:20:00+00:00", confidence=0.65),
        ), allow_fallback=True)
        self.assertIsNone(result.event_date)
        self.assertFalse(any("发布时间戳" in warning for warning in result.time_warnings))


class DefaultOffAndIdempotencyTests(unittest.TestCase):
    def test_default_off_leaves_time_empty(self):
        result = ground_event_time(event(
            evidence("e1", snippet="2025年3月29日 发布", published="2026-08-23T02:20:00+00:00", confidence=1.0),
        ))
        self.assertIsNone(result.event_date)
        self.assertIsNone(result.time_anchor)
        self.assertEqual(result.time_warnings, [])

    def test_model_anchor_takes_priority_over_fallback(self):
        anchored = event(
            evidence("e1", snippet="该产品于 2025年3月29日 正式发布"),
            time_anchor={"evidenceId": "e1", "quote": "2025年3月29日 正式发布"},
        )
        result = ground_event_time(anchored, allow_fallback=True)
        self.assertEqual(result.event_date, PartialDate(value="2025-03-29", precision="day"))
        self.assertFalse(any("兜底" in warning or "复核" in warning for warning in result.time_warnings))

    def test_timeline_rerender_is_idempotent_with_fallback(self):
        from agent.workflow_models import WorkflowResult

        def result_with(events):
            return WorkflowResult.model_validate({
                "mode": "BULK_DISCOVERY", "industry": "测试", "evidence": [],
                "events": events, "classifications": [], "chains": [], "rounds": [],
                "stopReason": "COMPLETED", "timeFallback": True,
            })

        grounded = ground_event_time(event(evidence("e1", snippet="该产品于 2025年3月29日 正式发布")),
                                     allow_fallback=True)
        first = render_timeline(result_with([grounded]))
        # 模拟从 JSON 重新加载后再渲染（render_timeline 会重跑 ground_event_time）
        second = render_timeline(result_with([ground_event_time(grounded, allow_fallback=True)]))
        self.assertEqual(first, second)
        self.assertIn("2025-03-29（仅知日）", first)


class WorkflowIntegrationTests(unittest.TestCase):
    def test_request_flag_reaches_processor_and_result(self):
        from agent.workflow import EventDiscoveryWorkflow
        from agent.workflow_models import EventProcessingResult, WorkflowRequest

        seen = {}

        class StubCollector:
            name = "stub"

            def __init__(self, items):
                self.items = items

            def search(self, request):
                return self.items

        class SpyProcessor:
            def process(self, evidence, *, previous=None, industry=None, time_fallback=False):
                seen["time_fallback"] = time_fallback
                return EventProcessingResult()

        class NoopPlanner:
            def initial_plan(self, request):
                from agent.workflow_models import QueryPlan
                return {"shouldContinue": False, "queries": [], "reason": "测试"}

            def next_plan(self, *args, **kwargs):
                raise AssertionError("不应进入下一轮规划")

        item = evidence("e1", snippet="2025年3月29日 发布")
        workflow = EventDiscoveryWorkflow(
            collector=StubCollector([item]), processor=SpyProcessor(), planner=NoopPlanner(),
        )
        result = workflow.run(WorkflowRequest.model_validate({
            "mode": "BULK_DISCOVERY", "industry": "测试", "presetKeywords": ["词"],
            "timeFallback": True,
        }))
        self.assertTrue(seen["time_fallback"])
        self.assertTrue(result.time_fallback)

    def test_result_defaults_to_false_for_old_payloads(self):
        from agent.workflow_models import WorkflowResult

        result = WorkflowResult.model_validate({
            "mode": "BULK_DISCOVERY", "industry": "测试", "evidence": [], "events": [],
            "classifications": [], "chains": [], "rounds": [], "stopReason": "COMPLETED",
        })
        self.assertFalse(result.time_fallback)


if __name__ == "__main__":
    unittest.main()
