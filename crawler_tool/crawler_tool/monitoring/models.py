"""监测层数据模型：主体档案、规则族、阈值、条目与告警契约。

约定：
- 输入条目接受 ContentItem 风格的 camelCase 字典（宽松解析，extra ignore）；
- 告警输出统一 by_alias=True 导出，供 agent 消费与 JSONL 存档；
- 所有阈值集中在 WatchSettings，运行行为不得硬编码绕过。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GRADE = Literal["red", "orange", "yellow"]
SURFACE_KIND = Literal["canonical", "alias", "product"]

# DISCOVERY_WORKFLOW 的事件类型枚举（告警/种子事件共用）。
EVENT_TYPES = (
    "PRODUCT_LAUNCH",
    "PRODUCT_CHANGE",
    "FINANCING",
    "POLICY",
    "PUBLIC_OPINION",
    "RECRUITMENT",
)


class ProductAlias(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str
    aliases: list[str] = Field(default_factory=list)


class EntityProfile(BaseModel):
    """主体档案：公司名/产品名/别名都映射到同一 entityId（主体单点改善之一）。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    entity_id: str = Field(alias="entityId")
    canonical_name: str = Field(alias="canonicalName")
    type: Literal["self", "competitor", "regulator", "industry_actor"] = "competitor"
    aliases: list[str] = Field(default_factory=list)
    products: list[ProductAlias] = Field(default_factory=list)
    # 易混淆实体（字面包含主体词的非目标对象），命中即否决该处匹配。
    confusable_blocklist: list[str] = Field(default_factory=list, alias="confusableBlocklist")
    enabled: bool = True

    def surfaces(self) -> list[tuple[str, SURFACE_KIND]]:
        """全部可匹配字面：(文本, 类型)。类型决定主体强度。"""
        result: list[tuple[str, SURFACE_KIND]] = [(self.canonical_name, "canonical")]
        result.extend((alias, "alias") for alias in self.aliases)
        for product in self.products:
            result.append((product.name, "product"))
            result.extend((alias, "product") for alias in product.aliases)
        # 长词优先匹配，避免"智言"抢在"智言科技"前面。
        return sorted(result, key=lambda pair: len(pair[0]), reverse=True)


class RuleFamily(BaseModel):
    """规则族：主体集合 × 动作词 × 宾语窗口 × 族级排除词。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    name: str
    applies_to: list[str] = Field(alias="appliesTo")
    actions: list[str]
    # 宾语窗口内出现才确认命中（"发布…产品"）；window 里先出现 notice 词则降级慢车道（"发布…公告"）。
    object_words: list[str] = Field(default_factory=list, alias="objectWords")
    notice_words: list[str] = Field(default_factory=list, alias="noticeWords")
    # 族级排除词（非全局）：如"招聘"对 launch 族排除，对 recruitment 族是主体动作。
    exclude_words: list[str] = Field(default_factory=list, alias="excludeWords")
    weight: float
    # 按主体类型覆盖权重：我方负面(3.0) > 竞品发布(2.0) > 政策(1.5)。
    weights: dict[str, float] = Field(default_factory=dict)
    event_types: list[str] = Field(default_factory=list, alias="eventTypes")

    def weight_for(self, entity_type: str) -> float:
        return self.weights.get(entity_type, self.weight)


class WatchSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    lookback_hours: float = Field(default=6, alias="lookbackHours")
    tau_hours: float = Field(default=6, alias="tauHours")
    cooldown_hours: float = Field(default=24, alias="cooldownHours")
    event_group_hard_window_hours: float = Field(default=2, alias="eventGroupHardWindowHours")
    resonance_window_hours: float = Field(default=12, alias="resonanceWindowHours")
    resonance_bonus_per_source: float = Field(default=0.3, alias="resonanceBonusPerSource")
    resonance_bonus_cap: float = Field(default=0.9, alias="resonanceBonusCap")
    red_threshold: float = Field(default=3.0, alias="redThreshold")
    orange_threshold: float = Field(default=2.0, alias="orangeThreshold")
    # 灰度协议：上线初期一切告警降为 yellow 进日报，攒分布后再关。
    gray_mode: bool = Field(default=True, alias="grayMode")
    negative_sentiment_factor: float = Field(default=1.5, alias="negativeSentimentFactor")
    slow_lane_per_tick: int = Field(default=40, alias="slowLanePerTick")
    industry_channel_per_tick: int = Field(default=30, alias="industryChannelPerTick")
    triage_batch_size: int = Field(default=8, alias="triageBatchSize")
    heat_history_min: int = Field(default=30, alias="heatHistoryMin")
    heat_history_cap: int = Field(default=200, alias="heatHistoryCap")
    velocity_growth_ratio: float = Field(default=0.2, alias="velocityGrowthRatio")
    velocity_growth_absolute: float = Field(default=10, alias="velocityGrowthAbsolute")
    velocity_bonus: float = Field(default=0.25, alias="velocityBonus")
    object_window_chars: int = Field(default=14, alias="objectWindowChars")
    state_retention_days: float = Field(default=14, alias="stateRetentionDays")
    # 事件组近似归并：重叠系数 + 最少共享二元组（真实同故事对校准值）。
    cluster_overlap_threshold: float = Field(default=0.35, alias="clusterOverlapThreshold")
    cluster_min_shared_bigrams: int = Field(default=8, alias="clusterMinSharedBigrams")
    # Agent 自动触发（业务决策 2026-09-07：自动化优先）：score 达标即触发发现循环，
    # 不受灰度显示模式影响；每日上限防误报风暴烧穿预算。
    agent_auto_trigger: bool = Field(default=True, alias="agentAutoTrigger")
    agent_trigger_score: float = Field(default=2.0, alias="agentTriggerScore")
    agent_max_runs_per_day: int = Field(default=10, alias="agentMaxRunsPerDay")
    agent_max_rounds: int = Field(default=3, alias="agentMaxRounds")
    agent_max_tool_calls: int = Field(default=6, alias="agentMaxToolCalls")


class MonitorConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    settings: WatchSettings = Field(default_factory=WatchSettings)
    families: list[RuleFamily] = Field(default_factory=list)
    entities: list[EntityProfile] = Field(default_factory=list)
    negative_words: list[str] = Field(default_factory=list, alias="negativeWords")
    industry_words: list[str] = Field(default_factory=list, alias="industryWords")
    # 高危旁路用：未识别主体 + 这些词（标题内）→ 直接进日报头条。
    major_action_words: list[str] = Field(default_factory=list, alias="majorActionWords")
    source_credibility: dict[str, float] = Field(default_factory=dict, alias="sourceCredibility")

    def family_by_id(self, family_id: str) -> RuleFamily | None:
        return next((family for family in self.families if family.id == family_id), None)

    def credibility_for(self, platform: str) -> float:
        return self.source_credibility.get(platform, 1.0)


class MonitoredItem(BaseModel):
    """监测输入条目（ContentItem 宽松视图）。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    content_id: str = Field(alias="contentId")
    dedup_key: str = Field(alias="dedupKey")
    title: str
    summary: str | None = None
    url: str | None = None
    platform: str
    published_at: str | None = Field(default=None, alias="publishedAt")
    published_at_confidence: float | None = Field(default=None, alias="publishedAtConfidence")
    # 真实 ContentItem 的 metrics 里可能混有 null，这里宽松收下，取热度时再过滤。
    metrics: dict[str, Any] = Field(default_factory=dict)
    first_seen_at: str | None = Field(default=None, alias="firstSeenAt")
    query: str | None = None

    @property
    def heat_value(self) -> float:
        return float(sum(
            value for value in self.metrics.values()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ))


class Alert(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    alert_id: str = Field(alias="alertId")
    generated_at: str = Field(alias="generatedAt")
    matched_rule: str = Field(alias="matchedRule")
    priority: GRADE
    score: float
    event_types: list[str] = Field(default_factory=list, alias="eventTypes")
    subject_entity_id: str | None = Field(default=None, alias="subjectEntityId")
    subject_name: str | None = Field(default=None, alias="subjectName")
    matched_spans: dict[str, Any] = Field(default_factory=dict, alias="matchedSpans")
    first_seen_at: str = Field(alias="firstSeenAt")
    published_at: str | None = Field(default=None, alias="publishedAt")
    resonance: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    flags: dict[str, bool] = Field(default_factory=dict)
    why: str = ""
    status: str = "pending"
    status_note: str = Field(default="", alias="statusNote")

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


def parse_utc(value: str | None) -> datetime | None:
    """宽松 ISO 解析；无时区按 UTC（与 content.v1 的 UTC ISO 约定一致）。"""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
