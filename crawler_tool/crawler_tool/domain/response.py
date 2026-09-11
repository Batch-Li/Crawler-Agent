from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from .content_item import ContentItem, Platform


class SourceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Platform
    status: Literal[
        "success", "empty", "authentication_required", "rate_limited",
        "parser_changed", "network_error", "source_unavailable", "failed"
    ]
    count: int = Field(default=0, ge=0)
    cached: bool = False
    error_code: str | None = None
    retryable: bool | None = None
    message: str | None = None
    diagnostics: dict[str, Any] | None = None


class ContentSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    request_id: str
    status: Literal["success", "partial", "failed"]
    partial: bool = False
    items: list[ContentItem] = Field(default_factory=list)
    source_reports: list[SourceReport] = Field(default_factory=list, alias="sourceReports")
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class ContentDetailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_id: str | None = Field(default=None, alias="contentId")
    url: str | None = None
    refresh_if_stale: bool = Field(default=False, alias="refreshIfStale")


class SourceHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[dict[str, object]]
