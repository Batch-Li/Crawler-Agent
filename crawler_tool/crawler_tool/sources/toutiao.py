from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from lxml import html

from crawler_tool.domain import ContentSearchRequest, SourceErrorCode, SourceStatus
from crawler_tool.sources.base import AdapterResult, RawItem, SourceAdapter
from crawler_tool.pagination import PageRequest

_CST = timezone(timedelta(hours=8))


class ToutiaoAdapter(SourceAdapter):
    """Parser-first Toutiao search adapter without embedded credentials."""

    platform = "toutiao"
    adapter_version = "0.2.0"

    def __init__(self, http_get: Any | None = None, *, clock: datetime | None = None) -> None:
        self.http_get = http_get
        self.clock = clock or datetime.now(timezone.utc)

    def health(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": "unknown",
            "adapter_version": self.adapter_version,
            "capabilities": ["search", "pagination"],
            "pagination": "fixture_verified",
        }

    def search(self, request: ContentSearchRequest) -> AdapterResult:
        return self._search(request, page_number=0)

    def search_page(self, request: ContentSearchRequest, page: PageRequest) -> AdapterResult:
        try:
            page_number = int(page.continuation or "")
        except ValueError:
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Toutiao pagination continuation is invalid", retryable=False)
        if page_number < 1:
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Toutiao pagination continuation is invalid", retryable=False)
        return self._search(request, page_number=page_number)

    def _search(self, request: ContentSearchRequest, *, page_number: int) -> AdapterResult:
        if self.http_get is None:
            return AdapterResult(
                status=SourceStatus.SOURCE_UNAVAILABLE,
                error_code=SourceErrorCode.SOURCE_UNAVAILABLE,
                message="Toutiao HTTP transport is not configured",
                retryable=True,
            )
        try:
            response = self.http_get(
                "https://so.toutiao.com/search",
                params={"dvpf": "pc", "source": "input", "keyword": request.query, "pd": "synthesis", "page_num": page_number},
                timeout=10,
            )
            status_code = getattr(response, "status_code", None)
            if status_code is not None:
                if status_code == 429:
                    return AdapterResult(
                        status=SourceStatus.RATE_LIMITED,
                        error_code=SourceErrorCode.RATE_LIMITED,
                        message="Toutiao search request was rate limited",
                        retryable=True,
                    )
                if status_code in {401, 403}:
                    return AdapterResult(
                        status=SourceStatus.AUTHENTICATION_REQUIRED,
                        error_code=SourceErrorCode.AUTH_REQUIRED,
                        message="Toutiao rejected the anonymous search request",
                        retryable=False,
                    )
                if status_code >= 500:
                    return AdapterResult(
                        status=SourceStatus.NETWORK_ERROR,
                        error_code=SourceErrorCode.INVALID_RESPONSE,
                        message=f"Toutiao search returned HTTP {status_code}",
                        retryable=True,
                    )
                if status_code >= 400:
                    return AdapterResult(
                        status=SourceStatus.NETWORK_ERROR,
                        error_code=SourceErrorCode.INVALID_RESPONSE,
                        message=f"Toutiao search returned HTTP {status_code}",
                        retryable=False,
                    )
            response.raise_for_status()
            content_type = getattr(response, "headers", {}).get("content-type", "").lower()
            if content_type and "html" not in content_type and "xhtml" not in content_type:
                return AdapterResult(
                    status=SourceStatus.NETWORK_ERROR,
                    error_code=SourceErrorCode.INVALID_RESPONSE,
                    message="Toutiao search returned a non-HTML response",
                    retryable=False,
                )
            return self.parse_html(response.text, page_number=page_number)
        except TimeoutError:
            return AdapterResult(SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.NETWORK_TIMEOUT, message="Toutiao request timed out", retryable=True)
        except Exception as exc:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"Toutiao request failed: {type(exc).__name__}", retryable=True)

    def parse_html(self, page: str, *, page_number: int = 0) -> AdapterResult:
        if _contains_auth_or_challenge(page):
            return AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_REQUIRED, message="Toutiao returned an authentication or verification page", retryable=False)
        tree = html.fromstring(page)
        cards = tree.xpath('//div[contains(@class, "s-result-list")]//div[contains(@class, "cs-card")]')
        if not cards:
            if "s-result-list" not in page:
                return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Toutiao search container is missing", retryable=False)
            return AdapterResult(status=SourceStatus.EMPTY)
        items = []
        for card in cards:
            title_nodes = card.xpath('.//a[contains(@class, "text-underline-hover")][1]')
            if not title_nodes:
                continue
            anchor = title_nodes[0]
            target_url = _extract_target_url(anchor.get("href") or "")
            if not target_url:
                continue
            title = _text(anchor)
            summary_nodes = card.xpath('.//span[contains(@class, "text-underline-hover")]')
            source_nodes = card.xpath('.//div[contains(@class, "cs-source-content")]//span[contains(@class, "text-ellipsis")]')
            source_values = [_text(node) for node in source_nodes if _text(node)]
            source_name = source_values[0] if source_values else None
            published = source_values[-1] if len(source_values) > 1 else None
            published_iso, published_confidence, time_warning = _parse_time_signal(published, self.clock)
            comment_text = " ".join(card.xpath('.//*[contains(text(), "评论")]/text()'))
            items.append(RawItem(
                platform=self.platform,
                raw_data_ref=f"raw/toutiao/{_source_id(target_url) or 'unknown'}.html",
                payload={
                    "sourceItemId": _source_id(target_url),
                    "sourceType": "news_media",
                    "contentType": "article",
                    "title": title,
                    "summary": _text(summary_nodes[0]) if summary_nodes else None,
                    "content": _text(summary_nodes[0]) if summary_nodes else None,
                    "url": target_url,
                    "author": {"name": None},
                    "publishedAt": published_iso,
                    "metrics": {"commentCount": _number(comment_text)},
                    "method": "http",
                    "adapterVersion": self.adapter_version,
                    "hasFullContent": False,
                    "publishedAtConfidence": published_confidence,
                    "warnings": ["search_result_summary_only"] + ([time_warning] if time_warning else []),
                    "ext": {"sourceName": source_name, "publishedAtRaw": published},
                },
            ))
        next_continuation = _next_page_continuation(tree, page_number)
        return AdapterResult(
            items=items,
            status=SourceStatus.SUCCESS if items else SourceStatus.EMPTY,
            next_cursor=next_continuation,
        )


def _next_page_continuation(tree: Any, page_number: int) -> str | None:
    for href in tree.xpath('//a[@href]/@href'):
        values = parse_qs(urlparse(href).query).get("page_num")
        if not values:
            continue
        try:
            candidate = int(values[0])
        except ValueError:
            continue
        if candidate == page_number + 1:
            return str(candidate)
    return None


def _contains_auth_or_challenge(page: str) -> bool:
    return any(marker in page for marker in ("验证码", "安全验证", "登录后", "登录以继续"))


def _parse_time_signal(raw: str | None, clock: datetime) -> tuple[str | None, float, str | None]:
    """头条时间信号解析：完整 ISO > 带时刻无时区 > 纯日期 > 相对表述 > MM-DD。

    真实搜索页的卡片段落常给"2026-08-23"或"3小时前"这类形态——旧版只认
    完整 ISO，导致真实数据 publishedAt 全部为空。返回 (UTC ISO, 置信度, 警告)。
    """
    if not raw:
        return None, 0.0, None
    value = str(raw).strip()
    # 纯日期必须先于 fromisoformat 判定（3.11 的 fromisoformat 也接受日期-only）。
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if m:
        moment = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=_CST)
        return moment.astimezone(timezone.utc).isoformat(), 0.85, "published_at_time_missing"
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_CST)
        return moment.astimezone(timezone.utc).isoformat(), 1.0, None
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", value)
    if m:
        today = clock.astimezone(_CST).date()
        candidate = date(today.year, int(m[1]), int(m[2]))
        if candidate > today + timedelta(days=2):
            candidate = date(today.year - 1, int(m[1]), int(m[2]))
        moment = datetime.combine(candidate, datetime.min.time(), tzinfo=_CST)
        return moment.astimezone(timezone.utc).isoformat(), 0.65, "published_at_year_inferred"
    m = re.fullmatch(r"(\d+)\s*分钟前", value)
    if m:
        return (clock - timedelta(minutes=int(m[1]))).isoformat(), 0.6, "published_at_relative"
    m = re.fullmatch(r"(\d+)\s*小时前", value)
    if m:
        return (clock - timedelta(hours=int(m[1]))).isoformat(), 0.6, "published_at_relative"
    m = re.fullmatch(r"(\d+)\s*天前", value)
    if m:
        return (clock - timedelta(days=int(m[1]))).isoformat(), 0.6, "published_at_relative"
    if value in ("昨天", "昨日"):
        return (clock - timedelta(days=1)).isoformat(), 0.6, "published_at_relative"
    if value in ("今天", "今日"):
        return clock.isoformat(), 0.6, "published_at_relative"
    return None, 0.0, "published_at_unparsed"


def _text(node: Any) -> str | None:
    value = " ".join(node.itertext()).strip()
    return unescape(value) or None


def _extract_target_url(href: str, *, max_hops: int = 3) -> str | None:
    """解包头条跳转链：真实页面存在双层嵌套（jump?url=<jump?url=<真实地址>>），
    循环剥皮直到没有 url 参数，最多 max_hops 层防止异常构造死循环。"""
    url = href
    for _ in range(max_hops):
        parsed = urlparse(url)
        values = parse_qs(parsed.query).get("url")
        if not values:
            break
        url = unquote(values[0])
    if url.startswith("http"):
        return url
    return None


def _source_id(url: str) -> str | None:
    match = re.search(r"/(\d{6,})(?:[/?#]|$)", url)
    return match.group(1) if match else None


def _number(value: str) -> int | None:
    digits = "".join(re.findall(r"\d+", value))
    return int(digits) if digits else None
