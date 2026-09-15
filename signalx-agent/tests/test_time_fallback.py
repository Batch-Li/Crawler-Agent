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


class AnchorFallthroughAndMonthDayTests(unittest.TestCase):
    """方案①锚点短路修复 + 方案②月日借年份（e2e-all-source-002 的实际场景）。"""

    def test_unparseable_anchor_falls_through_instead_of_silence(self):
        """方案①：锚点引用解析不出任何日期时不再静默放弃。"""
        anchored = event(
            evidence("e1", title="现场嘉宾共同见证了发布时刻", published="2026-08-23T02:20:00+00:00", confidence=0.85),
            time_anchor={"evidenceId": "e1", "quote": "现场嘉宾共同见证了发布时刻"},
        )
        result = ground_event_time(anchored, allow_fallback=True)
        self.assertEqual(result.event_date, PartialDate(value="2026-08-23", precision="day"))
        self.assertTrue(any("发布时间戳" in warning for warning in result.time_warnings))

    def test_anchor_month_day_borrows_year_from_published_at(self):
        """方案②：锚点只有"5月6日"，借高置信发布时间的年份（讯飞 e2e 场景）。"""
        anchored = event(
            evidence("e1", snippet="智东西5月6日报道,今天下午,讯飞星火认知大模型发布",
                     published="2023-05-06T13:54:02+00:00", confidence=1.0),
            time_anchor={"evidenceId": "e1", "quote": "智东西5月6日报道,今天下午,讯飞星火认知大模型发布"},
        )
        result = ground_event_time(anchored, allow_fallback=True)
        self.assertEqual(result.event_date, PartialDate(value="2023-05-06", precision="day"))
        self.assertIsNotNone(result.time_anchor)
        self.assertTrue(any("借" in warning for warning in result.time_warnings))

    def test_borrow_is_idempotent_on_rerender(self):
        anchored = event(
            evidence("e1", snippet="智东西5月6日报道,今天下午发布",
                     published="2023-05-06T13:54:02+00:00", confidence=1.0),
            time_anchor={"evidenceId": "e1", "quote": "智东西5月6日报道,今天下午发布"},
        )
        once = ground_event_time(anchored, allow_fallback=True)
        twice = ground_event_time(once, allow_fallback=True)
        self.assertEqual(twice.event_date, once.event_date)
        self.assertEqual(twice.time_anchor, once.time_anchor)
        self.assertEqual(twice.time_warnings, once.time_warnings)

    def test_fallback_month_day_borrows_year_without_anchor(self):
        result = ground_event_time(event(
            evidence("e1", snippet="新品于5月6日正式发布", published="2023-05-06T13:54:02+00:00", confidence=1.0),
        ), allow_fallback=True)
        self.assertEqual(result.event_date, PartialDate(value="2023-05-06", precision="day"))
        self.assertEqual(result.time_anchor.evidence_id, "e1")
        self.assertTrue(any("月日" in warning for warning in result.time_warnings))

    def test_month_day_mismatch_falls_to_stage2(self):
        result = ground_event_time(event(
            evidence("e1", snippet="报道称6月9日正式发布", published="2026-08-23T02:20:00+00:00", confidence=1.0),
        ), allow_fallback=True)
        # 月日与发布时间不一致 → 借不到年份 → 阶段 2 用发布时间戳落地
        self.assertEqual(result.event_date, PartialDate(value="2026-08-23", precision="day"))
        self.assertTrue(any("发布时间戳" in warning for warning in result.time_warnings))

    def test_cross_year_conflict_never_borrows(self):
        """同一月日出现在两个不同年份的发布时间里：不借，也不落时间。"""
        result = ground_event_time(event(
            evidence("e1", snippet="5月6日举办发布会", published="2023-05-06T10:00:00+00:00", confidence=0.9),
            evidence("e2", snippet="另一年同日举办", published="2024-05-06T10:00:00+00:00", confidence=0.9),
        ), allow_fallback=True)
        self.assertIsNone(result.event_date)
        self.assertTrue(any("互相矛盾" in warning for warning in result.time_warnings))

    def test_low_confidence_timestamp_never_lends_year(self):
        result = ground_event_time(event(
            evidence("e1", snippet="新品于5月6日正式发布", published="2023-05-06T13:54:02+00:00", confidence=0.65),
        ), allow_fallback=True)
        self.assertIsNone(result.event_date)

    def test_default_off_month_day_stays_empty(self):
        result = ground_event_time(event(
            evidence("e1", snippet="新品于5月6日正式发布", published="2023-05-06T13:54:02+00:00", confidence=1.0),
        ))
        self.assertIsNone(result.event_date)
        self.assertIsNone(result.time_anchor)
        self.assertEqual(result.time_warnings, [])


if __name__ == "__main__":
    unittest.main()


class CrossValidationTests(unittest.TestCase):
    """多候选日期与高置信发布时间交叉验证（正文越厚日期越多的场景）。"""

    def test_multi_candidate_with_unique_timestamp_match_lands(self):
        result = ground_event_time(event(
            evidence("e1", snippet="回顾2024年3月的旧品之后，2026年7月18日正式发布新品",
                     published="2026-07-18T12:18:00+00:00", confidence=0.9),
        ), allow_fallback=True)
        self.assertEqual(result.event_date, PartialDate(value="2026-07-18", precision="day"))
        self.assertTrue(any("交叉验证" in warning for warning in result.time_warnings))

    def test_multi_match_still_refuses(self):
        result = ground_event_time(event(
            evidence("e1", snippet="2023年5月6日举办首届，2024年5月6日举办第二届",
                     published="2024-05-06T10:00:00+00:00", confidence=0.9),
            evidence("e2", snippet="另一年同日举办", published="2023-05-06T10:00:00+00:00", confidence=0.9),
        ), allow_fallback=True)
        self.assertIsNone(result.event_date)
        self.assertTrue(any("2 个与高置信发布时间一致" in warning for warning in result.time_warnings))

    def test_no_timestamp_agreement_keeps_old_behavior(self):
        result = ground_event_time(event(
            evidence("e1", snippet="2025年3月29日 发布"),
            evidence("e2", snippet="回顾 2023-02-21 的旧款"),
        ), allow_fallback=True)
        self.assertIsNone(result.event_date)
        self.assertTrue(any("2 个候选日期" in warning for warning in result.time_warnings))


class BeijingCalendarDayTests(unittest.TestCase):
    """阶段②按北京时间取日历日：凌晨 0~8 点发布的文章不再错标前一天。"""

    def test_phase2_uses_beijing_calendar_day(self):
        # UTC 2026-08-31T16:30Z = 北京时间 2026-09-01 00:30
        result = ground_event_time(event(
            evidence("e1", title="凌晨发布的报道", published="2026-08-31T16:30:00+00:00", confidence=0.9),
        ), allow_fallback=True)
        self.assertEqual(result.event_date, PartialDate(value="2026-09-01", precision="day"))

    def test_phase2_conflict_check_also_in_beijing_days(self):
        # UTC 分别是 08-23/08-24 两天，北京时间都是 08-24 → 一致，落地
        result = ground_event_time(event(
            evidence("e1", published="2026-08-23T16:30:00+00:00", confidence=0.9),
            evidence("e2", published="2026-08-24T02:00:00+00:00", confidence=0.9),
        ), allow_fallback=True)
        self.assertEqual(result.event_date, PartialDate(value="2026-08-24", precision="day"))
