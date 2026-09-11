from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .content_item import Platform


CapturePlatform = Literal["xiaohongshu", "douyin", "wechat"]


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
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    platforms: list[Platform] | None = None
    published_after: datetime | None = None
    published_before: datetime | None = None
    limit: int = Field(default=20, ge=1, le=100)
    freshness: Literal["cache_only", "prefer_cached", "prefer_fresh"] = "prefer_cached"
    include_content: bool = False
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)


class FreshCollectionRequest(ContentSearchRequest):
    reason: str = Field(min_length=1, max_length=500)
