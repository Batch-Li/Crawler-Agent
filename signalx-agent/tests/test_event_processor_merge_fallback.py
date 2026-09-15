"""事件合并失败的兜底：保留已抽取事件继续分类/串联，公告进 merge_notices。"""

import unittest
from types import SimpleNamespace

from agent.event_processor import SignalXEventProcessor
from agent.models import Event, Evidence
from agent.tools.event_merge import EventMergeToolError
from agent.tool_models import EventExtractionRequest
from agent.workflow_models import EventClassification


def evidence(evidence_id: str, title: str) -> Evidence:
    return Evidence(evidence_id=evidence_id, title=title, source="测试来源")


class StubExtractor:
    def invoke(self, request: EventExtractionRequest) -> SimpleNamespace:
        return SimpleNamespace(events=[
            Event(event_id="evt_a", title="甲公司发布产品 A", entities=["甲公司"],
                  evidence=[evidence("evd_1", "甲公司发布产品 A")]),
            Event(event_id="evt_b", title="甲公司发布产品 A2", entities=["甲公司"],
                  evidence=[evidence("evd_2", "甲公司发布产品 A2")]),
        ])


class StubClassifier:
    def invoke(self, request):
        return SimpleNamespace(classifications=[
            EventClassification(event_id=event.event_id, category="PRODUCT_LAUNCH", confidence=0.9)
            for event in request.events
        ])


class StubChain:
    def invoke(self, request):
        return SimpleNamespace(chains=[], unlinked_event_ids=[], unresolved_questions=[])


class FailingMerger:
    def invoke(self, events):
        raise EventMergeToolError("同一事件复核未通过校验（ValidationError: 模拟失败）")


class FailingChain:
    def invoke(self, request):
        from agent.tools.event_chain import EventChainToolError
        raise EventChainToolError("事件关联未通过引用、时间或结构校验（ValidationError: 模拟失败）")


class MergeFallbackTests(unittest.TestCase):
    def test_merge_failure_keeps_extracted_events(self):
        processor = SignalXEventProcessor(
            extractor=StubExtractor(), classifier=StubClassifier(),
            chain_builder=StubChain(), industry="测试", merger=FailingMerger(),
        )
        result = processor.process([evidence("evd_1", "甲公司发布产品 A"),
                                    evidence("evd_2", "甲公司发布产品 A2")])
        self.assertEqual(len(result.events), 2)  # 抽取结果不丢
        self.assertTrue(any("事件合并未完成" in notice for notice in result.merge_notices))
        self.assertEqual(len(result.classifications), 2)  # 分类照常完成
        self.assertEqual(len(result.chains), 0)  # 串联照常完成（无链可建）

    def test_successful_merge_has_no_notices(self):
        class IdentityMerger:
            def invoke(self, events):
                return events

        processor = SignalXEventProcessor(
            extractor=StubExtractor(), classifier=StubClassifier(),
            chain_builder=StubChain(), industry="测试", merger=IdentityMerger(),
        )
        result = processor.process([evidence("evd_1", "甲公司发布产品 A"),
                                    evidence("evd_2", "甲公司发布产品 A2")])
        self.assertEqual(len(result.events), 2)
        self.assertEqual(result.merge_notices, [])


if __name__ == "__main__":
    unittest.main()


class ChainFallbackTests(unittest.TestCase):
    def test_chain_failure_keeps_events_and_classifications(self):
        processor = SignalXEventProcessor(
            extractor=StubExtractor(), classifier=StubClassifier(),
            chain_builder=FailingChain(), industry="测试",
        )
        result = processor.process([evidence("evd_1", "甲公司发布产品 A"),
                                    evidence("evd_2", "甲公司发布产品 A2")])
        self.assertEqual(len(result.events), 2)
        self.assertEqual(len(result.classifications), 2)
        self.assertEqual(result.chains, [])
        self.assertTrue(any("事件链生成未完成" in notice for notice in result.merge_notices))
