"""组合事件抽取、分类和事件链 Tool 的默认 EventProcessor。"""

from __future__ import annotations

from .llm import StructuredLLM
from .entity_resolution import EntityResolver
from .models import Event, Evidence
from .tool_models import (
    EventChainRequest,
    EventClassificationRequest,
    EventExtractionRequest,
)
from .tools import EventChainTool, EventClassificationTool, EventExtractionTool
from .workflow_models import EventClassification, EventProcessingResult


class SignalXEventProcessor:
    """现有发现循环可直接注入的非爬虫处理器。"""

    def __init__(
        self,
        extractor: EventExtractionTool,
        classifier: EventClassificationTool,
        chain_builder: EventChainTool,
        industry: str | None = None,
        entity_aliases: dict[str, str] | None = None,
    ) -> None:
        self.extractor = extractor
        self.classifier = classifier
        self.chain_builder = chain_builder
        self.industry = industry
        self.entity_resolver = EntityResolver(entity_aliases)

    @classmethod
    def from_llm(
        cls,
        llm: StructuredLLM,
        industry: str | None = None,
        max_attempts: int = 2,
        entity_aliases: dict[str, str] | None = None,
    ) -> "SignalXEventProcessor":
        """用同一个结构化 LLM 快速创建默认处理器。"""
        return cls(
            extractor=EventExtractionTool(llm, max_attempts=max_attempts),
            classifier=EventClassificationTool(llm, max_attempts=max_attempts),
            chain_builder=EventChainTool(entity_aliases=entity_aliases),
            industry=industry,
            entity_aliases=entity_aliases,
        )

    def process(
        self, evidence: list[Evidence], *,
        previous: EventProcessingResult | None = None,
        industry: str | None = None,
    ) -> EventProcessingResult:
        """只抽取新增/变更证据；完整快照在三个阶段均成功后一次性返回。"""
        previous = previous or EventProcessingResult()
        industry = industry or self.industry
        updated_evidence = {item.evidence_id: item for item in evidence}
        events = [self.entity_resolver.normalize(event.model_copy(update={
            "evidence": [updated_evidence.get(item.evidence_id, item) for item in event.evidence],
        })) for event in previous.events]
        if evidence:
            events = self.extractor.invoke(EventExtractionRequest(
                evidence=evidence, industry=industry, existing_events=events,
            )).events
        events = [self.entity_resolver.normalize(event) for event in events]
        previous_by_id = {item.event_id: item for item in previous.events}
        classifications = {item.event_id: item for item in previous.classifications}
        changed = [event for event in events if (
            event.event_id not in classifications
            or event != previous_by_id.get(event.event_id)
        )]
        classification = self.classifier.invoke(
            EventClassificationRequest(
                events=changed,
                industry=industry,
            )
        )
        classifications.update({item.event_id: item for item in classification.classifications})
        ordered_classifications = [classifications[event.event_id] for event in events]
        enriched_events = self._attach_event_types(
            events, ordered_classifications,
        )
        chains = self.chain_builder.invoke(
            EventChainRequest(
                events=enriched_events,
                classifications=ordered_classifications,
            )
        )
        return EventProcessingResult(
            events=enriched_events,
            classifications=ordered_classifications,
            chains=chains.chains,
        )

    @staticmethod
    def _attach_event_types(
        events: list[Event],
        classifications: list[EventClassification],
    ) -> list[Event]:
        category_by_event_id = {
            item.event_id: item.category for item in classifications
        }
        return [
            event.model_copy(
                update={
                    "event_types": [
                        category_by_event_id[event.event_id],
                    ]
                }
            )
            for event in events
        ]
