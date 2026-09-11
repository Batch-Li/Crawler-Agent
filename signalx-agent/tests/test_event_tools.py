from __future__ import annotations

import re
import unittest
from datetime import datetime
from typing import Any

from agent.event_processor import SignalXEventProcessor
from agent.models import Event, Evidence
from agent.tool_models import (
    EventChainRequest,
    EventClassificationRequest,
    EventExtractionRequest,
)
from agent.tools import (
    EventChainTool,
    EventClassificationTool,
    EventExtractionTool,
)
from agent.workflow_models import EventClassification


def evidence(evidence_id: str, title: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        title=title,
        source="测试来源",
        published_at="2026-09-01T10:00:00+08:00",
    )


class QueueLLM:
    model_name = "fake-model"

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls = 0

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        self.calls += 1
        return self.responses.pop(0)


class ProcessorLLM:
    """根据 Prompt 类型生成合法响应，用于验证完整组合链路。"""

    model_name = "processor-fake-model"

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if "事件抽取工具" in system_prompt:
            return {
                "events": [
                    {
                        "title": "甲公司发布产品 A",
                        "summary": "甲公司发布产品 A。",
                        "eventTime": "2026-08-01T09:00:00+08:00",
                        "entities": ["甲公司", "产品 A"],
                        "evidenceIds": ["evd_1"],
                        "anchor": {"evidenceId": "evd_1", "quote": "甲公司发布产品 A"},
                    },
                    {
                        "title": "甲公司发布产品 B",
                        "summary": "甲公司发布产品 B。",
                        "eventTime": "2026-09-01T09:00:00+08:00",
                        "entities": ["甲公司", "产品 B"],
                        "evidenceIds": ["evd_2"],
                        "anchor": {"evidenceId": "evd_2", "quote": "甲公司发布产品 B"},
                    },
                ]
            }
        event_ids = list(dict.fromkeys(re.findall(r"evt_[0-9a-f]{20}", user_prompt)))
        return {
            "classifications": [
                {
                    "eventId": event_id,
                    "category": "PRODUCT_LAUNCH",
                    "labels": ["智能产品"],
                    "confidence": 0.92,
                }
                for event_id in event_ids
            ]
        }


class EventToolTests(unittest.TestCase):
    def test_extraction_materializes_evidence_and_generates_id(self) -> None:
        llm = QueueLLM(
            [
                {
                    "events": [
                        {
                            "title": "甲公司发布智能客服产品",
                            "summary": "甲公司发布一款智能客服产品。",
                            "eventTime": "2026-09-01T10:00:00+08:00",
                            "entities": ["甲公司", "智能客服产品"],
                            "evidenceIds": ["evd_1", "evd_2"],
                            "anchor": {"evidenceId": "evd_1", "quote": "报道一"},
                        }
                    ]
                }
            ]
        )
        tool = EventExtractionTool(llm)
        request = EventExtractionRequest(
            evidence=[evidence("evd_1", "报道一"), evidence("evd_2", "报道二")],
            industry="企业服务",
        )

        result = tool.invoke(request)

        self.assertEqual(len(result.events), 1)
        self.assertRegex(result.events[0].event_id, r"^evt_[0-9a-f]{20}$")
        self.assertEqual(
            [item.evidence_id for item in result.events[0].evidence],
            ["evd_1", "evd_2"],
        )
        self.assertEqual(result.events[0].event_types, [])

    def test_extraction_retries_unknown_evidence_reference(self) -> None:
        valid_event = {
            "title": "甲公司发布产品",
            "entities": ["甲公司"],
            "evidenceIds": ["evd_1"],
            "anchor": {"evidenceId": "evd_1", "quote": "甲公司发布产品"},
        }
        llm = QueueLLM(
            [
                {
                    "events": [
                        {
                            **valid_event,
                            "evidenceIds": ["evd_unknown"],
                            "anchor": {"evidenceId": "evd_unknown", "quote": "甲公司发布产品"},
                        }
                    ]
                },
                {"events": [valid_event]},
            ]
        )

        result = EventExtractionTool(llm).invoke(
            {"evidence": [evidence("evd_1", "甲公司发布产品")]}
        )

        self.assertEqual(llm.calls, 2)
        self.assertEqual(len(result.events), 1)

    def test_classification_requires_exact_event_coverage(self) -> None:
        events = [
            Event(event_id="evt_1", title="事件一", evidence=[evidence("evd_1", "一")]),
            Event(event_id="evt_2", title="事件二", evidence=[evidence("evd_2", "二")]),
        ]
        llm = QueueLLM(
            [
                {
                    "classifications": [
                        {
                            "eventId": "evt_1",
                            "category": "PRODUCT_LAUNCH",
                            "confidence": 0.8,
                        }
                    ]
                },
                {
                    "classifications": [
                        {
                            "eventId": "evt_1",
                            "category": "PRODUCT_LAUNCH",
                            "confidence": 0.8,
                        },
                        {
                            "eventId": "evt_2",
                            "category": "PRICE_CHANGE",
                            "confidence": 0.7,
                        },
                    ]
                },
            ]
        )

        result = EventClassificationTool(llm).invoke(
            EventClassificationRequest(events=events)
        )

        self.assertEqual(llm.calls, 2)
        self.assertEqual(
            [item.event_id for item in result.classifications],
            ["evt_1", "evt_2"],
        )

    def test_chain_groups_same_category_and_primary_entity_in_time_order(self) -> None:
        events = [
            Event(
                event_id="evt_late",
                title="甲公司发布产品 B",
                event_time=datetime.fromisoformat("2026-09-01T10:00:00+08:00"),
                entities=["甲公司", "产品 B"],
                evidence=[evidence("evd_2", "二")],
            ),
            Event(
                event_id="evt_early",
                title="甲公司发布产品 A",
                event_time=datetime.fromisoformat("2026-08-01T10:00:00+08:00"),
                entities=["甲公司", "产品 A"],
                evidence=[evidence("evd_1", "一")],
            ),
            Event(
                event_id="evt_other",
                title="乙公司发布产品",
                entities=["乙公司"],
                evidence=[evidence("evd_3", "三")],
            ),
        ]
        classifications = [
            EventClassification(
                event_id=event.event_id,
                category="PRODUCT_LAUNCH",
                confidence=0.9,
            )
            for event in events
        ]

        result = EventChainTool().invoke(
            EventChainRequest(events=events, classifications=classifications)
        )

        self.assertEqual(len(result.chains), 1)
        self.assertEqual(
            result.chains[0].event_ids,
            ["evt_early", "evt_late"],
        )
        self.assertIn("甲公司", result.chains[0].title)

    def test_processor_runs_tools_and_attaches_primary_category(self) -> None:
        processor = SignalXEventProcessor.from_llm(
            ProcessorLLM(),
            industry="企业服务",
        )

        result = processor.process(
            [
                evidence("evd_1", "甲公司发布产品 A"),
                evidence("evd_2", "甲公司发布产品 B"),
            ]
        )

        self.assertEqual(len(result.events), 2)
        self.assertTrue(
            all(event.event_types == ["PRODUCT_LAUNCH"] for event in result.events)
        )
        self.assertEqual(len(result.classifications), 2)
        self.assertEqual(len(result.chains), 1)


if __name__ == "__main__":
    unittest.main()
