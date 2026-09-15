from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .content_item import Platform


CapturePlatform = Literal["xiaohongshu", "douyin", "wechat", "toutiao"]


class CaptureIngestRequest(BaseModel):
    """Manually captured public response fed into a capture parser.

    ``capture`` is required; otherwise the raw captured response may be sent
    as the whole body and is wrapped automatically. ``platform`` is optional:
    when omitted, the capture envelope is auto-detected.
    """

    model_config = ConfigDict(extra="forbid")
    keyword: str | None = Field(default=None, min_length=1, max_length=200)
    platform: CapturePlatform | None = None
    capture: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _wrap_bare_capture(cls, value: Any) -> Any:
        # The operator usually pastes the raw API response; accept it as-is.
        if isinstance(value, dict) and "capture" not in value:
            return {"capture": value}
        return value


class ContentSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    query: str = Field(min_length=1, max_length=500)
    platforms: list[Platform] | None = None
    published_after: datetime | None = None
    published_before: datetime | None = None
    limit: int = Field(default=20, ge=1, le=100)
    freshness: Literal["cache_only", "prefer_cached", "prefer_fresh"] = "prefer_cached"
    include_content: bool = False
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    # 有边界详情页补抓：显式启用；不参与 cursor 请求指纹（pagination.request_fingerprint），
    # 对既有分页令牌零影响。enrichContent 与 enrichTime 共享同一次详情 GET 与每轮预算。
    enrich_time: bool = Field(default=False, alias="enrichTime")
    enrich_content: bool = Field(default=False, alias="enrichContent")


class FreshCollectionRequest(ContentSearchRequest):
    reason: str = Field(min_length=1, max_length=500)


class CaptureQueuePushRequest(BaseModel):
    """agent 运行后把查询词推入捕获队列；source 区分 agent 推送与手工添加。"""

    model_config = ConfigDict(extra="forbid")
    keywords: list[str] = Field(min_length=1, max_length=50)
    source: Literal["agent", "manual"] = "manual"

    @model_validator(mode="after")
    def _clean_keywords(self) -> "CaptureQueuePushRequest":
        cleaned = []
        seen: set[str] = set()
        for keyword in self.keywords:
            word = keyword.strip()
            if word and word[:200].casefold() not in seen:
                seen.add(word[:200].casefold())
                cleaned.append(word[:200])
        if not cleaned:
            raise ValueError("keywords must contain at least one non-empty entry")
        self.keywords = cleaned
        return self
