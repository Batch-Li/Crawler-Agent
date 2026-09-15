from __future__ import annotations

import time as time_module
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from crawler_tool.pagination import (
    CursorCodec,
    CursorRequiresSinglePlatform,
    PageRequest,
    PaginationError,
    request_fingerprint,
)
from crawler_tool.application.recent_store import RecentItemsStore
from crawler_tool.application.time_enrichment import TimeEnrichmentService
from crawler_tool.domain import ContentSearchRequest, ContentSearchResponse, SourceReport, SourceStatus
from crawler_tool.normalization.content_normalizer import normalize_raw_item
from crawler_tool.sources.base import SourceAdapter
from crawler_tool.sources.registry import SourceRegistry


def _balanced_truncate(items: list, limit: int) -> list:
    """多来源均衡截断：超过限额时按平台轮转选取，而不是顺序截断。

    顺序截断会让先处理的平台占满全部限额、把后续来源（如手动捕获平台）
    完全挤出具外——统一搜索的语义是"各来源都要有 representation"。
    每个平台内部保持原有顺序，平台间按首次出现顺序轮转。
    """
    if len(items) <= limit:
        return items
    by_platform: dict[str, deque] = {}
    for item in items:
        by_platform.setdefault(item.platform, deque()).append(item)
    balanced: list = []
    while len(balanced) < limit and any(by_platform.values()):
        for queue in by_platform.values():
            if queue:
                balanced.append(queue.popleft())
                if len(balanced) >= limit:
                    break
    return balanced


@dataclass
class CachedPage:
    items: list[Any]
    next_continuation: str | None = None
    status: SourceStatus = SourceStatus.SUCCESS
    error_code: str | None = None
    message: str | None = None
    retryable: bool | None = None
    diagnostics: dict[str, Any] | None = None
    cached_at: float | None = field(default=None)


# Platforms whose production queries are answered from the shared session view
# instead of the network: manual-capture sources plus wechat's dedicated
# (rate-limited) endpoint, whose realtime path stays separate on purpose.
_LOCAL_HISTORY_PLATFORMS = {"xiaohongshu", "douyin", "wechat"}
_HISTORY_HINT_MESSAGES = {
    "xiaohongshu": "{platform} 网络路径禁用且本会话无该关键词的捕获记录；请在浏览器捕获后重试",
    "douyin": "{platform} 网络路径禁用且本会话无该关键词的捕获记录；请在浏览器捕获后重试",
    "wechat": "{platform} 本会话无该关键词的历史结果；实时关键词检索请使用专用端点 /api/v1/tool/search-wechat-articles",
}


class InMemoryContentCache:
    def __init__(self) -> None:
        self._items: dict[str, CachedPage | list[Any]] = {}

    def get(self, key: str) -> CachedPage | list[Any] | None:
        return self._items.get(key)

    def set(self, key: str, value: CachedPage | list[Any]) -> None:
        self._items[key] = value


class CrawlService:
    """Shared application service for scheduled and Agent-triggered crawling."""

    def __init__(
        self,
        registry: SourceRegistry,
        cache: InMemoryContentCache | None = None,
        cursor_codec: CursorCodec | None = None,
        *,
        cache_ttl_seconds: float | None = 600.0,
        clock: Any = None,
        history: RecentItemsStore | None = None,
        time_enrichment: TimeEnrichmentService | None = None,
    ):
        self.registry = registry
        self.cache = cache or InMemoryContentCache()
        self.cursor_codec = cursor_codec
        # Transient failures (rate limit, auth, parser changes) are never cached;
        # successful pages expire so that `prefer_cached` stays honest.
        self.cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock if clock is not None else time_module.time
        # Shared session view: every successfully normalized item lands here.
        self.recent_store = history if history is not None else RecentItemsStore()
        # 详情页时间补全仅在请求显式 enrichTime 时发请求；默认跳过手动捕获来源。
        self.time_enrichment = time_enrichment or TimeEnrichmentService(
            registry, skip_platforms=set(_LOCAL_HISTORY_PLATFORMS)
        )

    def _cache_entry_fresh(self, entry: CachedPage) -> bool:
        if self.cache_ttl_seconds is None or entry.cached_at is None:
            return True
        return (self._clock() - entry.cached_at) <= self.cache_ttl_seconds

    def _cache_key(self, request: ContentSearchRequest, platform: str, continuation: str | None = None) -> str:
        return ":".join([
            "content.v1", platform, request_fingerprint(request), continuation or "first-page",
        ])

    def _page_request(self, request: ContentSearchRequest, platforms: list[str]) -> PageRequest | None:
        if not request.cursor:
            return None
        if request.platforms is None or len(request.platforms) != 1:
            raise CursorRequiresSinglePlatform("cursor requires exactly one explicit platform")
        if self.cursor_codec is None:
            raise PaginationError("cursor pagination is not configured")
        platform = platforms[0]
        adapter = self.registry.create(platform)
        try:
            return self.cursor_codec.decode(
                request.cursor,
                platform=platform,
                fingerprint=request_fingerprint(request),
                adapter_version=adapter.adapter_version,
            )
        finally:
            adapter.close()

    def _next_cursor(self, *, platform: str, adapter_version: str, request: ContentSearchRequest, continuation: str | None) -> str | None:
        if not continuation or self.cursor_codec is None:
            return None
        return self.cursor_codec.encode(
            platform=platform,
            fingerprint=request_fingerprint(request),
            continuation=continuation,
            adapter_version=adapter_version,
        )

    def _upgrade_from_store(self, item):
        """同一 content_id 的库存条目若带全文而实时结果只有摘要，用库存版本替换。"""
        stored = self.recent_store.get(item.content_id)
        if stored is None or stored.content_id != item.content_id:
            return item
        stored_full = bool(getattr(getattr(stored, "quality", None), "has_full_content", False))
        live_full = bool(getattr(getattr(item, "quality", None), "has_full_content", False))
        if stored_full and not live_full:
            return stored
        return item

    def search(self, request: ContentSearchRequest) -> ContentSearchResponse:
        request_id = f"req_{id(request):x}"
        platforms = list(request.platforms) if request.platforms is not None else self.registry.defaults()
        page_request = self._page_request(request, platforms)
        items = []
        reports = []
        seen_keys: set[str] = set()
        next_cursor: str | None = None
        for platform in platforms:
            adapter: SourceAdapter | None = None
            try:
                if platform in _LOCAL_HISTORY_PLATFORMS:
                    # Capture-only platforms (network paths disabled): explicit
                    # queries are answered from the shared manual-capture view.
                    if page_request is not None:
                        reports.append(SourceReport(
                            platform=platform,
                            status="source_unavailable",
                            error_code="SOURCE_UNAVAILABLE",
                            retryable=False,
                            message=f"{platform} 为手动捕获来源，不支持分页查询",
                        ))
                        continue
                    matches = []
                    if request.query:
                        matches = self.recent_store.query(
                            platform=platform, keyword=request.query, limit=max(request.limit, 10)
                        )
                    if matches:
                        count = 0
                        for item in matches:
                            if item.dedup.dedup_key not in seen_keys:
                                seen_keys.add(item.dedup.dedup_key)
                                items.append(item)
                                count += 1
                        reports.append(SourceReport(
                            platform=platform,
                            status=SourceStatus.SUCCESS.value,
                            count=count,
                            cached=True,
                            message="served from local manual-capture history; network access stays disabled",
                        ))
                    else:
                        reports.append(SourceReport(
                            platform=platform,
                            status="source_unavailable",
                            error_code="SOURCE_UNAVAILABLE",
                            retryable=False,
                            message=_HISTORY_HINT_MESSAGES[platform].format(platform=platform),
                        ))
                    continue
                cache_key = self._cache_key(request, platform, page_request.continuation if page_request else None)
                cached = self.cache.get(cache_key) if request.freshness != "prefer_fresh" else None
                if isinstance(cached, CachedPage) and not self._cache_entry_fresh(cached):
                    cached = None
                if cached is not None:
                    entry = cached if isinstance(cached, CachedPage) else CachedPage(items=cached)
                    normalized = entry.items
                    result_status = entry.status if normalized else SourceStatus.EMPTY
                    result_cached = True
                    result_error, result_message, result_retryable = entry.error_code, entry.message, entry.retryable
                    result_diagnostics = entry.diagnostics
                    next_continuation = entry.next_continuation
                    adapter_version = None
                elif request.freshness == "cache_only":
                    normalized = []
                    result_status = SourceStatus.EMPTY
                    result_cached = False
                    result_error, result_message, result_retryable = "CACHE_MISS", "no cached content", False
                    result_diagnostics = None
                    next_continuation = None
                    adapter_version = None
                else:
                    adapter = self.registry.create(platform)
                    result = adapter.search_page(request, page_request) if page_request else adapter.search(request)
                    normalized = []
                    normalization_errors = 0
                    for raw_item in result.items:
                        try:
                            normalized.append(normalize_raw_item(raw_item, query=request.query, trace_id=request_id))
                        except Exception:
                            normalization_errors += 1
                    result_status = result.status
                    if result_status == SourceStatus.SUCCESS and not normalized:
                        result_status = SourceStatus.EMPTY
                    if normalization_errors and not normalized and result_status == SourceStatus.SUCCESS:
                        result_status = SourceStatus.FAILED
                    result_cached = result.cached
                    result_error, result_message, result_retryable = result.error_code, result.message, result.retryable
                    result_diagnostics = result.response_metadata
                    next_continuation = result.next_cursor
                    adapter_version = adapter.adapter_version
                    # Transient failures must not poison the cache: only stable
                    # outcomes (success/empty) are stored, with a timestamp.
                    if result_status in (SourceStatus.SUCCESS, SourceStatus.EMPTY):
                        self.cache.set(cache_key, CachedPage(
                            items=normalized,
                            next_continuation=next_continuation,
                            status=result_status,
                            error_code=result_error,
                            message=result_message,
                            retryable=result_retryable,
                            diagnostics=result_diagnostics,
                            cached_at=self._clock(),
                        ))
                if len(platforms) == 1 and next_continuation:
                    version = adapter_version
                    if version is None:
                        cached_adapter = self.registry.create(platform)
                        try:
                            version = cached_adapter.adapter_version
                        finally:
                            cached_adapter.close()
                    next_cursor = self._next_cursor(
                        platform=platform,
                        adapter_version=version,
                        request=request,
                        continuation=next_continuation,
                    )
                if normalized:
                    self.recent_store.add(normalized)
                for item in normalized:
                    if item.dedup.dedup_key not in seen_keys:
                        seen_keys.add(item.dedup.dedup_key)
                        items.append(item)
                reports.append(SourceReport(
                    platform=platform,
                    status=result_status.value,
                    count=len(normalized),
                    cached=result_cached,
                    error_code=result_error,
                    retryable=result_retryable,
                    message=result_message,
                    diagnostics=result_diagnostics,
                ))
            except PaginationError:
                raise
            except Exception as exc:
                reports.append(SourceReport(
                    platform=platform,
                    status=SourceStatus.FAILED.value,
                    error_code="SOURCE_ERROR",
                    retryable=False,
                    message=str(exc),
                ))
            finally:
                if adapter is not None:
                    adapter.close()
        # 会话库升级：同一 content_id 的库存条目若已有全文（如浏览器自动化回填），
        # 用它替换实时摘要条目，让补抓内容进入证据流；无库存副本时原样返回。
        items = [self._upgrade_from_store(item) for item in items]
        # 详情页补全（时间/正文）：只作用于本次响应（截断后）的条目，失败不进 SourceReport。
        final_items = _balanced_truncate(items, request.limit)
        if (request.enrich_time or request.enrich_content) and final_items:
            final_items, _ = self.time_enrichment.enrich(final_items, include_content=request.enrich_content)
            self.recent_store.add(final_items)
        failed = any(report.status not in {SourceStatus.SUCCESS.value, SourceStatus.EMPTY.value} for report in reports)
        return ContentSearchResponse(
            request_id=request_id,
            status="partial" if failed and items else ("failed" if failed and not items else "success"),
            partial=failed,
            items=final_items,
            source_reports=reports,
            next_cursor=next_cursor,
        )
