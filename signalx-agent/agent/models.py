"""竞品 Agent 的输入、草稿和最终输出模型。"""

from __future__ import annotations

from datetime import date, datetime, timezone
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def to_camel(value: str) -> str:
    """将 snake_case 字段名转换成 JSON 使用的 camelCase。"""
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class SchemaModel(BaseModel):
    """Agent 协议模型的公共配置。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


def utc_time(value: datetime | None) -> datetime | None:
    """无时区的旧数据没有可比较的绝对时间，保留为未知而非猜测时区。"""
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


class EvidenceAnchor(SchemaModel):
    """足以区分核心动作的证据原文片段，用于新事件身份。"""

    evidence_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class Evidence(SchemaModel):
    """Agent 可引用的一条外部证据。"""

    evidence_id: str = Field(min_length=1)
    title: str | None = None
    content: str | None = None
    snippet: str | None = None
    dedup_key: str | None = None
    source: str = Field(min_length=1)
    url: str | None = None
    published_at: datetime | None = None
    # 发布时间置信度与原始串：来自采集侧 ContentItem；旧 evidence 文件缺省即可，
    # 时间兜底（event_time.ground_event_time）只在高置信时使用。
    published_at_confidence: Confidence | None = None
    published_at_raw: str | None = None
    has_full_content: bool | None = None
    source_name: str | None = None
    quality_warnings: list[str] = Field(default_factory=list)

    _normalize_time = field_validator("published_at")(utc_time)

    @model_validator(mode="after")
    def require_readable_content(self) -> "Evidence":
        if not self.title and not self.content and not self.snippet:
            raise ValueError("evidence 必须至少包含 title、content 或 snippet")
        return self


class AlertMeta(SchemaModel):
    """保留监测来源信息；告警分数不是事件抽取置信度。"""

    priority: str | None = None
    score: float | None = Field(default=None, allow_inf_nan=False)
    matched_rule: str | None = None
    platforms: list[str] = Field(default_factory=list)


class PartialDate(SchemaModel):
    """日历日期，不假装拥有精确时刻或时区。"""

    value: str
    precision: Literal["year", "month", "day"]

    @model_validator(mode="after")
    def validate_date(self):
        patterns = {"year": r"\d{4}", "month": r"\d{4}-\d{2}", "day": r"\d{4}-\d{2}-\d{2}"}
        if not re.fullmatch(patterns[self.precision], self.value):
            raise ValueError("日期必须与精度一致")
        date.fromisoformat(self.value + {"year": "-01-01", "month": "-01", "day": ""}[self.precision])
        return self


class Event(SchemaModel):
    """竞品 Agent 的标准输入。"""

    event_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str | None = None
    event_time: datetime | None = None
    event_date: PartialDate | None = None
    time_anchor: EvidenceAnchor | None = None
    time_warnings: list[str] = Field(default_factory=list)
    merged_event_ids: list[str] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(min_length=1)
    anchor: EvidenceAnchor | None = None
    alert_meta: AlertMeta | None = None

    _normalize_time = field_validator("event_time")(utc_time)

    @model_validator(mode="after")
    def require_unique_evidence_ids(self) -> "Event":
        ids = [item.evidence_id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("同一个 Event 内 evidenceId 不能重复")
        if self.anchor and self.anchor.evidence_id not in ids:
            raise ValueError("Event.anchor 必须引用本事件关联的 Evidence")
        if self.time_anchor and self.time_anchor.evidence_id not in ids:
            raise ValueError("timeAnchor 必须引用本事件证据")
        if len(self.merged_event_ids) != len(set(self.merged_event_ids)) or self.event_id in self.merged_event_ids:
            raise ValueError("合并来源 ID 必须唯一，且不能包含自身")
        return self


CompetitorActionType = Literal[
    "PRODUCT_LAUNCH",
    "PRODUCT_UPDATE",
    "PRICE_CHANGE",
    "MARKET_ENTRY",
    "PARTNERSHIP",
    "OTHER",
]


class ProductAnalysis(SchemaModel):
    """事件中涉及的竞品信息。"""

    name: str | None = None
    category: str | None = None
    features: list[str] = Field(default_factory=list)
    pricing: str | None = None
    target_customers: list[str] = Field(default_factory=list)
    positioning: str | None = None


class Finding(SchemaModel):
    """一条必须由输入证据支撑的竞品发现。"""

    finding: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: Confidence


class CompetitorDraft(SchemaModel):
    """LLM 只能生成的业务字段，固定元信息由 Agent 主体补充。"""

    subject_company: str | None = None
    action_type: CompetitorActionType
    product: ProductAnalysis
    key_findings: list[Finding] = Field(min_length=1)
    summary: str = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list)
    confidence: Confidence


class ModelInfo(SchemaModel):
    model: str
    prompt_version: str


class CompetitorAnalysis(CompetitorDraft):
    """竞品 Agent 对外输出的最终结构。"""

    event_id: str
    agent_type: Literal["COMPETITOR"] = "COMPETITOR"
    model_info: ModelInfo
