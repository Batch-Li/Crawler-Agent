from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from crawler_tool.domain import ContentSearchRequest, Platform, SourceStatus


@dataclass
class RawItem:
    platform: Platform
    payload: dict[str, Any]
    raw_data_ref: str | None = None


@dataclass
class AdapterResult:
    items: list[RawItem] = field(default_factory=list)
    status: SourceStatus = SourceStatus.SUCCESS
    error_code: str | None = None
    message: str | None = None
    retryable: bool | None = None
    cached: bool = False
    next_cursor: str | None = None
    response_metadata: dict[str, Any] | None = None


class SourceAdapter(ABC):
    platform: Platform
    adapter_version = "0.1.0"

    @abstractmethod
    def search(self, request: ContentSearchRequest) -> AdapterResult:
        """Search the source and return platform-shaped raw items."""

    def search_page(self, request: ContentSearchRequest, page: Any) -> AdapterResult:
        """Search an already verified continuation; adapters opt in when supported."""
        return self.search(request)

    def fetch_detail(self, url: str) -> AdapterResult:
        """Fetch a content detail page when a source supports it."""
        return AdapterResult(
            status=SourceStatus.SOURCE_UNAVAILABLE,
            error_code="DETAIL_FETCH_UNSUPPORTED",
            message=f"{self.platform} detail fetching is not configured",
            retryable=False,
        )

    def health(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": "unknown",
            "adapter_version": self.adapter_version,
            "capabilities": ["search"],
        }

    def close(self) -> None:
        return None
