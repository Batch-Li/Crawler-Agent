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
from .tools.event_merge import EventMergeTool
from .event_time import ground_event_time, refresh_event_evidence
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
        merger: EventMergeTool | None = None,
    ) -> None:
        self.extractor = extractor
        self.classifier = classifier
        self.chain_builder = chain_builder
        self.industry = industry
        self.entity_resolver = EntityResolver(entity_aliases)
        self.merger = merger

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
            chain_builder=EventChainTool(llm, max_attempts=max_attempts, entity_aliases=entity_aliases),
            industry=industry,
            entity_aliases=entity_aliases,
            merger=EventMergeTool(llm, max_attempts=max_attempts),
        )

    def process(
        self, evidence: list[Evidence], *,
        previous: EventProcessingResult | None = None,
        industry: str | None = None,
    ) -> EventProcessingResult:
        """只抽取增量证据；复核、分类及串联全部成功后提交完整快照。"""
        previous = previous or EventProcessingResult()
        industry = industry or self.industry
        updated_evidence = {item.evidence_id: item for item in evidence}
        events = [self.entity_resolver.normalize(refresh_event_evidence(event, updated_evidence))
                  for event in previous.events]
        if evidence:
            events = self.extractor.invoke(EventExtractionRequest(
                evidence=evidence, industry=industry, existing_events=events,
            )).events
        events = [ground_event_time(self.entity_resolver.normalize(event)) for event in events]
        if self.merger:
            events = self.merger.invoke(events)
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
                industry=industry,
                previous_chains=previous.chains,
            )
        )
        return EventProcessingResult(
            events=enriched_events,
            classifications=ordered_classifications,
            chains=chains.chains,
            unlinked_event_ids=chains.unlinked_event_ids,
            chain_unresolved_questions=chains.unresolved_questions,
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
