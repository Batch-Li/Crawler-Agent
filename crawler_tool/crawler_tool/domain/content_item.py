from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


Platform = Literal[
    "toutiao", "weibo", "wechat", "xiaohongshu", "douyin",
    "south_weekend", "sogou_wechat", "cctv_news", "official_website", "other"
]
SourceType = Literal[
    "news_media", "social_media", "official", "ecommerce", "government", "other"
]
ContentType = Literal["article", "post", "video", "image", "notice", "product", "other"]
QualityStatus = Literal["success", "partial", "failed"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Author(StrictModel):
    id: str | None = None
    name: str | None = None
    url: HttpUrl | None = None
    verified: bool | None = None


class Metrics(StrictModel):
    like_count: int | None = Field(default=None, alias="likeCount")
    comment_count: int | None = Field(default=None, alias="commentCount")
    share_count: int | None = Field(default=None, alias="shareCount")
    view_count: int | None = Field(default=None, alias="viewCount")
    collect_count: int | None = Field(default=None, alias="collectCount")


class Media(StrictModel):
    type: str
    url: HttpUrl


class CollectionContext(StrictModel):
    task_id: int | None = Field(default=None, alias="taskId")
    query: str | None = None
    method: Literal["api", "http", "browser", "mobile", "authorized_session", "other"]
    adapter: str
    adapter_version: str = Field(alias="adapterVersion")
    trace_id: str = Field(alias="traceId")


class DedupInfo(StrictModel):
    dedup_key: str = Field(alias="dedupKey")
    content_hash: str = Field(alias="contentHash")


class Quality(StrictModel):
    status: QualityStatus
    has_full_content: bool = Field(alias="hasFullContent")
    published_at_confidence: float = Field(ge=0, le=1, alias="publishedAtConfidence")
    warnings: list[str] = Field(default_factory=list)


class ContentItem(StrictModel):
    schema_version: Literal["content.v1"] = Field(default="content.v1", alias="schemaVersion")
    content_id: str = Field(alias="contentId")
    platform: Platform
    source_type: SourceType = Field(alias="sourceType")
    source_item_id: str | None = Field(default=None, alias="sourceItemId")
    content_type: ContentType = Field(alias="contentType")
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    language: str = "zh-CN"
    url: HttpUrl
    canonical_url: HttpUrl | None = Field(default=None, alias="canonicalUrl")
    author: Author = Field(default_factory=Author)
    published_at: datetime | None = Field(default=None, alias="publishedAt")
    collected_at: datetime = Field(alias="collectedAt")
    metrics: Metrics = Field(default_factory=Metrics)
    media: list[Media] = Field(default_factory=list)
    collection: CollectionContext
    dedup: DedupInfo
    quality: Quality
    raw_data_ref: str | None = Field(default=None, alias="rawDataRef")
    ext: dict[str, Any] = Field(default_factory=dict)
