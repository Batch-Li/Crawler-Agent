"""详情页补全（时间/正文）：有边界的 best-effort 增强，仅由调用方显式启用。

两级 provider：
1. 详情页补抓（``fetch_detail``，时间与正文共用同一次 GET）；
2. Wayback CDX 首次收录时间（默认关闭，``wayback_http_get`` 注入才启用）。

边界（与项目红线一致）：
- 仅补「缺时间/置信度低」或「缺正文」的条目，按响应顺序最多 max_fetches 条；
- 每条最多 1 次 GET，不重试；时间与正文共享该预算；
- 命中验证码/频控页即停该来源，不消耗剩余预算；
- 失败只加条目级 warning，不写入 SourceReport——partial 判定
  （crawl_service.search）会把任何非 success/empty 来源报告升格为
  整次搜索的降级信号，best-effort 增强不应触发它。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from crawler_tool.domain import ContentItem, SourceStatus
from crawler_tool.infrastructure.wayback_time import (
    CIRCUIT_BREAKER_THRESHOLD,
    WaybackUnavailable,
    earliest_capture,
)
from crawler_tool.sources.base import SourceAdapter
from crawler_tool.sources.registry import SourceRegistry

_DETAIL_WARNING = "published_at_from_detail"
_FAILURE_WARNING = "detail_fetch_failed"
_WAYBACK_WARNING = "published_at_from_wayback"
_CONTENT_WARNING = "content_from_detail"
_CONTENT_MISSING_WARNING = "content_missing_from_detail"
# 命中即停的来源状态：对抗/频控信号不因换条目而消失。
_STOP_PLATFORM_STATUSES = {SourceStatus.AUTHENTICATION_REQUIRED, SourceStatus.RATE_LIMITED}
# 手动捕获来源（xiaohongshu/douyin/wechat）的条目来自会话历史，
# 不对其 URL 发详情/兜底请求；与 crawl_service._LOCAL_HISTORY_PLATFORMS 一致。
MANUAL_CAPTURE_PLATFORMS = {"xiaohongshu", "douyin", "wechat"}


class TimeEnrichmentService:
    def __init__(
        self,
        registry: SourceRegistry,
        *,
        max_fetches: int = 5,
        confidence_threshold: float = 0.7,
        skip_platforms: set[str] | None = None,
        wayback_http_get: Callable[..., Any] | None = None,
    ) -> None:
        self.registry = registry
        self.max_fetches = max_fetches
        self.confidence_threshold = confidence_threshold
        self.skip_platforms = MANUAL_CAPTURE_PLATFORMS if skip_platforms is None else skip_platforms
        # Wayback 第二级兜底：None = 关闭（默认）。
        self.wayback_http_get = wayback_http_get

    def enrich(self, items: list[ContentItem], *, include_content: bool = False) -> tuple[list[ContentItem], int]:
        """返回 (新条目列表, 实际发出的详情请求数)。无候选时原样返回、零请求。

        include_content：同时为缺正文条目补全文；时间与正文共享每条 1 次 GET
        与全局 max_fetches 预算。缺省 False 时行为与旧版逐字节一致。
        """
        candidates = [
            index for index, item in enumerate(items)
            if item.platform not in self.skip_platforms
            and (self._needs_time(item) or (include_content and self._needs_content(item)))
        ]
        if not candidates:
            return items, 0
        result = list(items)
        fetches = 0
        by_platform: dict[str, list[int]] = {}
        for index in candidates:
            by_platform.setdefault(items[index].platform, []).append(index)
        for platform, indexes in by_platform.items():
            adapter: SourceAdapter | None = None
            try:
                adapter = self.registry.create(platform)
                for index in indexes:
                    if fetches >= self.max_fetches:
                        break
                    item = items[index]
                    outcome = adapter.fetch_detail(item.url)
                    if outcome.error_code == "DETAIL_FETCH_UNSUPPORTED":
                        # 该来源不提供详情能力：静默跳过整个平台，不标失败、不耗预算。
                        break
                    fetches += 1
                    metadata = outcome.response_metadata if isinstance(outcome.response_metadata, dict) else {}
                    if outcome.status is SourceStatus.SUCCESS:
                        wants_time = self._needs_time(item)
                        wants_content = include_content and self._needs_content(item)
                        if (wants_time and metadata.get("publishedAt")) or (wants_content and metadata.get("content")):
                            result[index] = self._apply(item, metadata, wants_time, wants_content)
                            continue
                    result[index] = self._mark_failed(item, include_content)
                    if outcome.status in _STOP_PLATFORM_STATUSES:
                        break
            finally:
                if adapter is not None:
                    adapter.close()
        if self.wayback_http_get is not None:
            failures = 0
            for index in candidates:
                if failures >= CIRCUIT_BREAKER_THRESHOLD:
                    break
                item = result[index]
                if item.published_at is not None:
                    continue  # 详情阶段已补到时间
                try:
                    iso = earliest_capture(str(item.url), self.wayback_http_get)
                except WaybackUnavailable:
                    failures += 1
                    continue
                if iso:
                    result[index] = self._apply_wayback(item, iso)
        return result, fetches

    def _needs_time(self, item: ContentItem) -> bool:
        return item.published_at is None or item.quality.published_at_confidence < self.confidence_threshold

    def _needs_content(self, item: ContentItem) -> bool:
        return item.content is None or not item.quality.has_full_content

    def _apply(self, item: ContentItem, metadata: dict, wants_time: bool = True, wants_content: bool = False) -> ContentItem:
        quality_update: dict[str, Any] = {"warnings": list(item.quality.warnings)}
        ext = dict(item.ext)
        if wants_time and metadata.get("publishedAt"):
            published_at = datetime.fromisoformat(str(metadata["publishedAt"]))
            quality_update["published_at_confidence"] = float(metadata.get("publishedAtConfidence", 0.85))
            quality_update["warnings"] = list(dict.fromkeys([*quality_update["warnings"], _DETAIL_WARNING]))
            ext["detailTimeFetched"] = True
            ext["detailTimeDetectedBy"] = str(metadata.get("detectedBy", "detail"))
            item = item.model_copy(update={"published_at": published_at})
        if wants_content and metadata.get("content"):
            quality_update["has_full_content"] = True
            quality_update["warnings"] = list(dict.fromkeys([*quality_update["warnings"], _CONTENT_WARNING]))
            ext["detailContentFetched"] = True
            ext["detailContentDetectedBy"] = str(metadata.get("contentDetectedBy", "detail"))
            item = item.model_copy(update={"content": str(metadata["content"])})
        return item.model_copy(update={
            "quality": item.quality.model_copy(update=quality_update), "ext": ext,
        })

    def _mark_failed(self, item: ContentItem, include_content: bool = False) -> ContentItem:
        warnings = list(dict.fromkeys([*item.quality.warnings, _FAILURE_WARNING]))
        if include_content and self._needs_content(item):
            warnings = list(dict.fromkeys([*warnings, _CONTENT_MISSING_WARNING]))
        return item.model_copy(update={"quality": item.quality.model_copy(update={"warnings": warnings})})

    def _apply_wayback(self, item: ContentItem, iso: str) -> ContentItem:
        published_at = datetime.fromisoformat(iso)
        quality = item.quality.model_copy(update={
            "published_at_confidence": 0.6,
            "warnings": list(dict.fromkeys([*item.quality.warnings, _WAYBACK_WARNING])),
        })
        ext = {**item.ext, "waybackTimeFetched": True}
        return item.model_copy(update={"published_at": published_at, "quality": quality, "ext": ext})
