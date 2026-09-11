from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from html import unescape
from typing import Any
from urllib.parse import urljoin

from lxml import html

from crawler_tool.domain import ContentSearchRequest, SourceErrorCode, SourceStatus
from crawler_tool.sources.base import AdapterResult, RawItem, SourceAdapter
from crawler_tool.pagination import PageRequest


class SouthWeekendAdapter(SourceAdapter):
    """South Weekend SSR search adapter with JSON compatibility parsing."""

    platform = "south_weekend"
    adapter_version = "0.2.0-ssr"
    base_url = "https://www.infzm.com"

    def __init__(self, http_get: Any | None = None, *, clock: datetime | None = None) -> None:
        self.http_get = http_get
        self.clock = clock or datetime.now(timezone.utc)

    def health(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": "unknown",
            "adapter_version": self.adapter_version,
            "capabilities": ["search"],
            "pagination": "unverified",
        }

    def search(self, request: ContentSearchRequest) -> AdapterResult:
        return self._search(request, page=1)

    def search_page(self, request: ContentSearchRequest, page: PageRequest) -> AdapterResult:
        try:
            page_number = int(page.continuation or "")
        except ValueError:
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="South Weekend pagination continuation is invalid", retryable=False)
        if page_number < 2:
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="South Weekend pagination continuation is invalid", retryable=False)
        return self._search(request, page=page_number)

    def _search(self, request: ContentSearchRequest, *, page: int) -> AdapterResult:
        if self.http_get is None:
            return AdapterResult(
                status=SourceStatus.SOURCE_UNAVAILABLE,
                error_code=SourceErrorCode.SOURCE_UNAVAILABLE,
                message="South Weekend HTTP transport is not configured",
                retryable=True,
            )
        try:
            params = {"k": request.query}
            if page > 1:
                params.update({"page": page, "format": "json"})
            response = self.http_get(
                f"{self.base_url}/search",
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            is_json = "json" in content_type or response.content.lstrip().startswith(b"{")
            if is_json:
                result = self.parse_payload(response.json(), page_number=page)
            else:
                result = self.parse_html(_decode_html(response.content), page_number=page)
            return result
        except TimeoutError:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.NETWORK_TIMEOUT, message="South Weekend request timed out", retryable=True)
        except Exception as exc:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"South Weekend request failed: {type(exc).__name__}", retryable=True)

    def parse_html(self, page: str, *, page_number: int = 1) -> AdapterResult:
        if _looks_like_challenge(page):
            return AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_REQUIRED, message="South Weekend returned a login or challenge page", retryable=False)
        tree = html.fromstring(page)
        panel = tree.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " nfzm-panel--list ")]')
        if not panel:
            status = _parse_status_script(page)
            if status.get("content_ids") == [] or "暂无搜索结果" in page:
                return AdapterResult(status=SourceStatus.EMPTY)
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="South Weekend SSR result panel is missing", retryable=False)
        rows = panel[0].xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " nfzm-list ") and contains(concat(" ", normalize-space(@class), " "), " ui-line ")]/li')
        status = _parse_status_script(page)
        items: list[RawItem] = []
        for row in rows:
            link_nodes = row.xpath('.//a[contains(@href, "/contents/")]')
            link = link_nodes[0] if link_nodes else None
            if link is None:
                continue
            href = link.get("href") or ""
            url, source_id = _content_url(href)
            if not url or not source_id:
                continue
            title_nodes = row.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " nfzm-content-item__title ")]//h5')
            title_node = title_nodes[0] if title_nodes else None
            description_nodes = row.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " nfzm-content-item__description ")]')
            description_node = description_nodes[0] if description_nodes else None
            meta_nodes = row.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " nfzm-content-item__meta ")]/span')
            meta = [" ".join(node.itertext()).strip() for node in meta_nodes]
            section = meta[0] if meta else None
            date_text = meta[1] if len(meta) > 1 else None
            comment_text = next((value for value in meta[2:] if "评论" in value), None)
            image_nodes = row.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " nfzm-content-item__cover ")]//img')
            image = image_nodes[0] if image_nodes else None
            published_at, date_warnings, confidence = _parse_display_date(date_text, self.clock)
            warnings = ["search_result_summary_only", *date_warnings]
            items.append(RawItem(
                platform=self.platform,
                raw_data_ref=f"raw/south_weekend/{source_id}.html",
                payload={
                    "sourceItemId": source_id,
                    "sourceType": "news_media",
                    "contentType": "article",
                    "title": _node_text(title_node),
                    "summary": _node_text(description_node),
                    "content": _node_text(description_node),
                    "url": url,
                    "author": {"name": None},
                    "publishedAt": published_at,
                    "metrics": {"commentCount": _safe_int_from_text(comment_text)},
                    "media": [{"type": "image", "url": urljoin(self.base_url, image.get("src"))}] if image is not None and image.get("src") else [],
                    "method": "http",
                    "adapterVersion": self.adapter_version,
                    "hasFullContent": False,
                    "publishedAtConfidence": confidence,
                    "warnings": warnings,
                    "ext": {"section": section, "publishedAtRaw": date_text},
                },
            ))
        if not items:
            return AdapterResult(status=SourceStatus.EMPTY)
        return AdapterResult(items=items, status=SourceStatus.SUCCESS)

    def parse_payload(self, payload: dict[str, Any], *, page_number: int = 1) -> AdapterResult:
        records = payload.get("data", {}).get("list")
        if records is None:
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="missing data.list in South Weekend response", retryable=False)
        items = []
        for record in records:
            source_id = str(record.get("id") or record.get("url") or "") or None
            raw_url = str(record.get("url") or "")
            if not raw_url:
                continue
            url = raw_url if raw_url.startswith("http") else f"{self.base_url}/contents/{raw_url.lstrip('/')}"
            items.append(RawItem(
                platform=self.platform,
                raw_data_ref=f"raw/south_weekend/{source_id or 'unknown'}.json",
                payload={
                    "sourceItemId": source_id,
                    "sourceType": "news_media",
                    "contentType": "article",
                    "title": record.get("short_subject") or record.get("subject"),
                    "summary": record.get("introtext"),
                    "content": record.get("introtext"),
                    "url": url,
                    "author": {"name": record.get("author") or None},
                    "publishedAt": record.get("publish_time"),
                    "metrics": {"commentCount": _safe_int(record.get("comment_count"))},
                    "method": "http",
                    "adapterVersion": self.adapter_version,
                    "hasFullContent": False,
                    "warnings": ["search_result_summary_only"],
                },
            ))
        return AdapterResult(
            items=items,
            status=SourceStatus.SUCCESS if items else SourceStatus.EMPTY,
            next_cursor=None,
        )


def _next_page_continuation(has_next: Any, page_number: int) -> str | None:
    return None
def _decode_html(content: bytes) -> str:
    """Decode South Weekend SSR bytes; server headers claim UTF-8 but GB18030 is used."""
    for encoding in ("gb18030", "utf-8"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _content_url(href: str) -> tuple[str | None, str | None]:
    match = re.search(r"/contents/(\d+)", href)
    if not match:
        return None, None
    source_id = match.group(1)
    return urljoin("https://www.infzm.com", f"/contents/{source_id}"), source_id


def _parse_status_script(page: str) -> dict[str, Any]:
    match = re.search(r"__STATUS__\s*=\s*(\{.*?\})\s*</script>", page, re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def _node_text(node: Any) -> str | None:
    if node is None:
        return None
    text = unescape(" ".join(node.itertext()).strip())
    return text or None


def _parse_display_date(value: str | None, clock: datetime) -> tuple[str | None, list[str], float]:
    if not value:
        return None, ["published_at_missing"], 0.0
    value = value.strip()
    tz = timezone(timedelta(hours=8))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=tz)
        return parsed.isoformat(), ["published_at_time_missing"], 0.85
    if re.fullmatch(r"\d{2}-\d{2}", value):
        month, day = map(int, value.split("-"))
        year = clock.astimezone(tz).year
        candidate = date(year, month, day)
        today = clock.astimezone(tz).date()
        if candidate > today + timedelta(days=2):
            candidate = date(year - 1, month, day)
        return datetime.combine(candidate, datetime.min.time(), tzinfo=tz).isoformat(), ["published_at_year_inferred", "published_at_time_missing"], 0.65
    try:
        return datetime.fromisoformat(value).astimezone(tz).isoformat(), [], 1.0
    except ValueError:
        return None, ["published_at_unparsed"], 0.0


def _safe_int_from_text(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _looks_like_challenge(page: str) -> bool:
    return any(marker in page for marker in ("验证码", "安全验证", "登录后继续"))
