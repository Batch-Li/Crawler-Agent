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
ClaimType = Literal['reported', 'planned', 'forecast', 'rumor', 'opinion', 'refutation']
ActionKind = Literal['release', 'preorder', 'sale', 'price_change', 'update', 'partnership', 'response', 'other']


def utc_time(value: datetime | None) -> datetime | None:
    """无时区的旧数据没有可比较的绝对时间，保留为未知而非猜测时区。"""
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


class EvidenceAnchor(SchemaModel):
    """足以区分核心动作的证据原文片段，用于新事件身份。"""

    evidence_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class EvidenceAssessment(EvidenceAnchor):
    stance: Literal['support', 'contradict', 'context', 'mixed']
    reason: str = Field(min_length=1)


class TimeCandidate(SchemaModel):
    anchor: EvidenceAnchor
    role: Literal['occurrence', 'target', 'report']
    year_context: EvidenceAnchor | None = None


class Publication(SchemaModel):
    """原文页面的发布时间及可回溯依据；失败时不保留估算值。"""

    status: Literal['verified', 'missing', 'conflicting', 'unavailable', 'blocked', 'budget_exhausted', 'invalid_url']
    value: str | None = None
    raw: str | None = None
    precision: Literal['day', 'minute', 'second'] | None = None
    source_url: str | None = None
    locator: str | None = None
    response_ref: str | None = None
    response_sha256: str | None = None

    @model_validator(mode='after')
    def require_original_time(self):
        if self.status == 'verified':
            if not all((self.value, self.raw, self.precision, self.source_url, self.locator)):
                raise ValueError('原文发布时间必须包含时间、精度及原文定位')
            if self.precision == 'day':
                date.fromisoformat(self.value)
            else:
                datetime.fromisoformat(self.value)
        elif self.value is not None:
            raise ValueError('未取得原文发布时间时 value 必须为空')
        return self


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
    published_calendar_date: date | None = None
    publication: Publication | None = None
    has_full_content: bool | None = None
    source_name: str | None = None
    quality_warnings: list[str] = Field(default_factory=list)

    _normalize_time = field_validator("published_at")(utc_time)

    @model_validator(mode='before')
    @classmethod
    def preserve_publication_calendar_date(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        stamp = value.get('publishedAt', value.get('published_at'))
        if stamp and 'publishedCalendarDate' not in value and 'published_calendar_date' not in value:
            try:
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00')) if isinstance(stamp, str) else stamp
                if parsed.tzinfo and parsed.utcoffset() is not None:
                    value['published_calendar_date'] = parsed.date()
            except (ValueError, AttributeError):
                pass
        return value

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
    precision: Literal["year", "month", "day", "month_day", "month_only"]

    @model_validator(mode="after")
    def validate_date(self):
        patterns = {"year": r"\d{4}", "month": r"\d{4}-\d{2}", "day": r"\d{4}-\d{2}-\d{2}",
                    "month_day": r"--\d{2}-\d{2}", "month_only": r"--\d{2}"}
        if not re.fullmatch(patterns[self.precision], self.value):
            raise ValueError("日期必须与精度一致")
        if self.precision in ('month_day', 'month_only'):
            date.fromisoformat('2000' + self.value[1:] + ('-01' if self.precision == 'month_only' else ''))
        else:
            date.fromisoformat(self.value + {"year": "-01-01", "month": "-01", "day": ""}[self.precision])
        return self


class Event(SchemaModel):
    """竞品 Agent 的标准输入。"""

    event_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str | None = None
    action_kind: ActionKind = 'other'
    claim_type: ClaimType = 'reported'
    event_time: datetime | None = None
    event_date: PartialDate | None = None
    time_anchor: EvidenceAnchor | None = None
    time_warnings: list[str] = Field(default_factory=list)
    time_candidates: list[TimeCandidate] = Field(default_factory=list)
    time_reviewed: bool = False
    grounding_audited: bool = False
    merge_review_pending: bool = False
    date_status: Literal['unknown', 'resolved', 'conflicting'] = 'unknown'
    date_basis: Literal['explicit', 'relative', 'context', 'unknown'] = 'unknown'
    target_date: PartialDate | None = None
    report_date: PartialDate | None = None
    evidence_assessments: list[EvidenceAssessment] = Field(default_factory=list)
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
        assessed = [a.evidence_id for a in self.evidence_assessments]
        if len(assessed) != len(set(assessed)) or not set(assessed).issubset(ids):
            raise ValueError('证据归属必须引用本事件的唯一证据 ID')
        for candidate in self.time_candidates:
            if candidate.anchor.evidence_id not in ids:
                raise ValueError('时间候选必须引用本事件证据')
            if candidate.year_context and candidate.year_context.evidence_id != candidate.anchor.evidence_id:
                raise ValueError('年份上下文必须来自同一条证据')
        if len(self.merged_event_ids) != len(set(self.merged_event_ids)) or self.event_id in self.merged_event_ids:
            raise ValueError("合并来源 ID 必须唯一，且不能包含自身")
        return self
