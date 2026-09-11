"""非爬虫 Agent Tool 使用的稳定输入输出模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator

from .models import Confidence, Event, Evidence, EvidenceAnchor, SchemaModel, utc_time
from .workflow_models import EventChain, EventClassification, EventRelation


class EventDraft(SchemaModel):
    """LLM 提取的事件草稿；正式 ID 和证据对象由程序补充。"""

    title: str = Field(min_length=1)
    summary: str | None = None
    event_time: datetime | None = None
    entities: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    anchor: EvidenceAnchor
    matched_event_id: str | None = Field(default=None, min_length=1)
    time_anchor: EvidenceAnchor | None = None

    _normalize_time = field_validator("event_time")(utc_time)

    @model_validator(mode="after")
    def normalize_lists(self) -> "EventDraft":
        self.entities = _unique_non_empty(self.entities)
        # ID 区分大小写，不能使用实体名称的 casefold 去重规则。
        self.evidence_ids = list(dict.fromkeys(value.strip() for value in self.evidence_ids))
        if not self.evidence_ids or any(not value for value in self.evidence_ids):
            raise ValueError("evidenceIds 必须包含非空的证据 ID")
        if self.anchor.evidence_id not in self.evidence_ids:
            raise ValueError("anchor.evidenceId 必须包含在 evidenceIds 中")
        if self.time_anchor and self.time_anchor.evidence_id not in self.evidence_ids:
            raise ValueError("timeAnchor 必须引用本事件证据")
        return self


class EventDraftBatch(SchemaModel):
    events: list[EventDraft] = Field(default_factory=list)


class EventExtractionRequest(SchemaModel):
    evidence: list[Evidence] = Field(min_length=1)
    industry: str | None = None
    existing_events: list[Event] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_unique_evidence_ids(self) -> "EventExtractionRequest":
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("event extraction 输入的 evidenceId 不能重复")
        event_ids = [item.event_id for item in self.existing_events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("existingEvents 的 eventId 不能重复")
        return self


class EventExtractionResult(SchemaModel):
    events: list[Event] = Field(default_factory=list)


class EventMergeGroup(SchemaModel):
    event_ids: list[str] = Field(min_length=2)
    reason: str = Field(min_length=1)
    evidence: list[EvidenceAnchor] = Field(min_length=1)
    confidence: Confidence


class EventMergeBatch(SchemaModel):
    groups: list[EventMergeGroup]


class EventClassificationRequest(SchemaModel):
    events: list[Event] = Field(default_factory=list)
    industry: str | None = None

    @model_validator(mode="after")
    def require_unique_event_ids(self) -> "EventClassificationRequest":
        event_ids = [item.event_id for item in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("event classification 输入的 eventId 不能重复")
        return self


class EventClassificationResult(SchemaModel):
    classifications: list[EventClassification] = Field(default_factory=list)


class EventClassificationBatch(SchemaModel):
    """LLM 分类响应使用的包裹结构。"""

    classifications: list[EventClassification] = Field(default_factory=list)


class EventChainRequest(SchemaModel):
    events: list[Event] = Field(default_factory=list)
    classifications: list[EventClassification] = Field(default_factory=list)
    industry: str | None = None
    previous_chains: list[EventChain] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_complete_classifications(self) -> "EventChainRequest":
        event_ids = [item.event_id for item in self.events]
        classification_ids = [item.event_id for item in self.classifications]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("event chain 输入的 eventId 不能重复")
        if len(classification_ids) != len(set(classification_ids)):
            raise ValueError("event chain 输入的 classification eventId 不能重复")
        if set(event_ids) != set(classification_ids):
            raise ValueError("event chain 要求每个 Event 恰好有一个分类结果")
        chain_ids = [chain.chain_id for chain in self.previous_chains]
        if len(chain_ids) != len(set(chain_ids)):
            raise ValueError("previousChains 的 ID 不能重复")
        return self


class EventChainDraft(SchemaModel):
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    event_ids: list[str] = Field(min_length=2)
    relations: list[EventRelation] = Field(min_length=1)
    confidence: Confidence
    unresolved_questions: list[str] = Field(default_factory=list)
    matched_chain_id: str | None = Field(default=None, min_length=1)


class EventChainDraftBatch(SchemaModel):
    chains: list[EventChainDraft]
    unresolved_questions: list[str] = Field(default_factory=list)


class EventChainResult(SchemaModel):
    chains: list[EventChain] = Field(default_factory=list)
    unlinked_event_ids: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)


def _unique_non_empty(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            result.append(normalized)
            seen.add(key)
    return result
