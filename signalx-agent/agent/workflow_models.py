"""事件发现循环使用的状态、请求和结果模型。"""

from __future__ import annotations

from enum import Enum
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .models import Confidence, Event, Evidence, EvidenceAnchor, PartialDate, SchemaModel


class WorkflowMode(str, Enum):
    """会议确定的两种事件发现入口。"""

    BULK_DISCOVERY = "BULK_DISCOVERY"
    EVENT_EXPANSION = "EVENT_EXPANSION"


class StopReason(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    MAX_ROUNDS = "MAX_ROUNDS"
    TOOL_BUDGET_EXHAUSTED = "TOOL_BUDGET_EXHAUSTED"
    NO_NEW_QUERIES = "NO_NEW_QUERIES"
    NO_PROGRESS = "NO_PROGRESS"
    NO_EVIDENCE = "NO_EVIDENCE"
    FAILED = "FAILED"
    MODEL_BUDGET_EXHAUSTED = "MODEL_BUDGET_EXHAUSTED"


class WorkflowRequest(SchemaModel):
    """一次事件发现工作流的启动参数。"""

    mode: WorkflowMode
    industry: str = Field(min_length=1)
    preset_keywords: list[str] = Field(default_factory=list)
    seed_event: Event | None = None
    max_rounds: int = Field(default=3, ge=1, le=10)
    max_tool_calls: int = Field(default=6, ge=1, le=50)
    max_llm_calls: int = Field(default=24, ge=0, le=500)
    per_round_query_limit: int = Field(default=5, ge=1, le=50)
    per_query_limit: int = Field(default=20, ge=1, le=100)
    max_no_progress_rounds: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def validate_mode_input(self) -> "WorkflowRequest":
        self.preset_keywords = _unique_non_empty(self.preset_keywords)
        if self.mode == WorkflowMode.BULK_DISCOVERY and not self.preset_keywords:
            raise ValueError("BULK_DISCOVERY 必须提供 presetKeywords")
        if self.mode == WorkflowMode.EVENT_EXPANSION and self.seed_event is None:
            raise ValueError("EVENT_EXPANSION 必须提供 seedEvent")
        return self


class SearchRequest(SchemaModel):
    """Agent 调用外部采集 Tool 时使用的稳定参数。"""

    query: str = Field(min_length=1)
    industry: str = Field(min_length=1)
    round_index: int = Field(ge=1)
    limit: int = Field(ge=1, le=100)


EventCategory = Literal[
    "PRODUCT_LAUNCH",
    "PRODUCT_UPDATE",
    "PRICE_CHANGE",
    "MARKET_ENTRY",
    "PARTNERSHIP",
    "FINANCING",
    "MERGER_ACQUISITION",
    "PERSONNEL_CHANGE",
    "POLICY_REGULATION",
    "FINANCIAL_PERFORMANCE",
    "RISK_INCIDENT",
    "MARKETING_CAMPAIGN",
    "TECHNOLOGY_RELEASE",
    "OTHER",
]


class EventClassification(SchemaModel):
    event_id: str = Field(min_length=1)
    category: EventCategory
    labels: list[str] = Field(default_factory=list)
    confidence: Confidence

    @model_validator(mode="after")
    def normalize_labels(self) -> "EventClassification":
        self.labels = _unique_non_empty(self.labels)
        return self


class EventRelation(SchemaModel):
    from_event_id: str = Field(min_length=1)
    to_event_id: str = Field(min_length=1)
    relation_type: Literal["FOLLOW_UP", "RESPONSE", "CAUSES", "CONTRADICTS", "RELATED"]
    reason: str = Field(min_length=1)
    evidence: list[EvidenceAnchor] = Field(min_length=1)
    confidence: Confidence
    status: Literal["supported", "needs_review"] = "supported"


class TimelineEntry(SchemaModel):
    event_id: str
    title: str
    event_time: datetime | None = None
    event_date: PartialDate | None = None
    time_status: Literal["known", "partial", "unknown"]


class EventChain(SchemaModel):
    """模型判断的故事线；历史无关系字段的分组结果仍可读取。"""

    chain_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str | None = None
    event_ids: list[str] = Field(min_length=1)
    relations: list[EventRelation] = Field(default_factory=list)
    confidence: Confidence | None = None
    unresolved_questions: list[str] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)


class EventProcessingResult(SchemaModel):
    """字段提取、事件分类和事件链处理后的统一快照。"""

    events: list[Event] = Field(default_factory=list)
    classifications: list[EventClassification] = Field(default_factory=list)
    chains: list[EventChain] = Field(default_factory=list)
    unlinked_event_ids: list[str] = Field(default_factory=list)
    chain_unresolved_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_event_references(self) -> "EventProcessingResult":
        for name, ids in (
            ("event", [item.event_id for item in self.events]),
            ("classification", [item.event_id for item in self.classifications]),
            ("chain", [item.chain_id for item in self.chains]),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"{name} ID 不能重复")
        event_ids = {event.event_id for event in self.events}
        aliases = [key for event in self.events for key in event.merged_event_ids]
        if len(aliases) != len(set(aliases)) or event_ids.intersection(aliases):
            raise ValueError("合并来源不能重复归属，也不能仍作为独立事件")
        classification_ids = {
            classification.event_id for classification in self.classifications
        }
        chain_event_ids = {
            event_id for chain in self.chains for event_id in chain.event_ids
        }
        if not classification_ids.issubset(event_ids):
            raise ValueError("classification 引用了不存在的 eventId")
        if not chain_event_ids.issubset(event_ids):
            raise ValueError("eventChain 引用了不存在的 eventId")
        if (len(self.unlinked_event_ids) != len(set(self.unlinked_event_ids))
                or not set(self.unlinked_event_ids).issubset(event_ids - chain_event_ids)):
            raise ValueError("unlinkedEventIds 必须为未进入事件链的唯一事件 ID")
        return self


class QueryPlan(SchemaModel):
    """查询规划器对本轮是否继续采集的决定。"""

    should_continue: bool
    queries: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    unresolved_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_queries(self) -> "QueryPlan":
        self.unresolved_questions = _unique_non_empty(self.unresolved_questions)
        self.queries = _unique_non_empty(self.queries)
        if self.should_continue and not self.queries:
            raise ValueError("继续采集时必须至少提供一个 query")
        if not self.should_continue:
            self.queries = []
        return self


class CollectionSourceReport(SchemaModel):
    """来源诊断只保存结构字段，不接收上游原始错误正文。"""

    platform: str = Field(min_length=1)
    status: Literal[
        "success", "empty", "authentication_required", "rate_limited",
        "parser_changed", "network_error", "source_unavailable", "failed",
    ]
    count: int = Field(default=0, ge=0)
    retryable: bool | None = None


class CollectionResult(SchemaModel):
    evidence: list[Evidence] = Field(default_factory=list)
    status: Literal["success", "partial", "failed"] = "success"
    source_reports: list[CollectionSourceReport] = Field(default_factory=list)
    dropped_count: int = Field(default=0, ge=0)


class ToolCallRecord(SchemaModel):
    query: str
    success: bool
    result_count: int = Field(default=0, ge=0)
    error: str | None = None
    status: Literal["success", "partial", "failed"] = "success"
    source_reports: list[CollectionSourceReport] = Field(default_factory=list)
    dropped_count: int = Field(default=0, ge=0)


class EvidenceReview(SchemaModel):
    evidence_id: str = Field(min_length=1)
    decision: Literal["relevant", "irrelevant", "uncertain"]
    reason: str = Field(min_length=1)


class FilteredEvidence(SchemaModel):
    evidence: Evidence
    decision: Literal["irrelevant", "uncertain"]
    reason: str = Field(min_length=1)


class WorkflowError(SchemaModel):
    stage: Literal["planning", "filtering", "processing"]
    error_type: str
    message: str


class WorkflowRound(SchemaModel):
    round_index: int = Field(ge=1)
    plan_reason: str
    tool_calls: list[ToolCallRecord]
    new_evidence_count: int = Field(ge=0)
    total_evidence_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    chain_count: int = Field(ge=0)
    updated_evidence_count: int = Field(default=0, ge=0)
    evidence_updates: list[Evidence] = Field(default_factory=list)
    processing_status: Literal["completed", "skipped", "failed"] = "skipped"
    llm_calls: int = Field(default=0, ge=0)
    snapshot: EventProcessingResult | None = None
    errors: list[WorkflowError] = Field(default_factory=list)
    collected_evidence: list[Evidence] = Field(default_factory=list)
    evidence_reviews: list[EvidenceReview] = Field(default_factory=list)


class WorkflowResult(SchemaModel):
    mode: WorkflowMode
    industry: str
    evidence: list[Evidence]
    events: list[Event]
    classifications: list[EventClassification]
    chains: list[EventChain]
    rounds: list[WorkflowRound]
    stop_reason: StopReason
    llm_calls: int = Field(default=0, ge=0)
    collection_calls: int = Field(default=0, ge=0)
    pending_queries: list[str] = Field(default_factory=list)
    processed_evidence_ids: list[str] = Field(default_factory=list)
    unprocessed_evidence_ids: list[str] = Field(default_factory=list)
    errors: list[WorkflowError] = Field(default_factory=list)
    filtered_evidence: list[FilteredEvidence] = Field(default_factory=list)
    unreviewed_evidence: list[Evidence] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    stop_message: str = ""
    unlinked_event_ids: list[str] = Field(default_factory=list)
    chain_unresolved_questions: list[str] = Field(default_factory=list)


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
