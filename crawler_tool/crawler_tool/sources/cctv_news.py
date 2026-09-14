from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from crawler_tool.domain import ContentSearchRequest, SourceErrorCode, SourceStatus
from crawler_tool.infrastructure.detail_content import parse_detail_content
from crawler_tool.infrastructure.detail_time import parse_detail_time
from crawler_tool.sources.base import AdapterResult, RawItem, SourceAdapter

BASE_URL = "https://news.cctv.com"
DATA_FILE_TEMPLATE = BASE_URL + "/2019/07/gaiban/cmsdatainterface/page/{column}_{page}.jsonp"
# focus_date 是北京时间；统一转换为 UTC ISO 再入 content.v1。
CST = timezone(timedelta(hours=8))


class CctvNewsAdapter(SourceAdapter):
    """Anonymous first-screen reader of CCTV news column list data files.

    Column lists live at static JSONP files (``cmsdatainterface/page/
    {column}_{page}.jsonp``): no auth, no signature, no challenge observed,
    and robots declares no restrictions. Scope stays first-screen: pagination
    files (``_2``+) are not exposed as cursors. Records carry second-level
    publish times, briefs, keywords and stable ARTI ids — better field
    quality than most HTML list pages.
    """

    platform = "cctv_news"
    adapter_version = "0.2.0-anonymous"
    allowed_columns = ("china", "world", "society", "economy", "sports", "military", "tech")

    def __init__(self, http_get: Any | None = None, *, column: str = "china", clock: datetime | None = None) -> None:
        self.column = column if column in self.allowed_columns else "china"
        self.http_get = http_get
        self.clock = clock or datetime.now(timezone.utc)

    def health(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": "unknown",
            "adapter_version": self.adapter_version,
            "capabilities": ["search"],
            "pagination": "disabled",
            "authMode": "anonymous",
            "column": self.column,
            "notes": "official_media_list_first_screen_discovery",
        }

    def fetch_detail(self, url: str) -> AdapterResult:
        """有边界详情页补抓：单次 GET、同时提取发布时间与正文、失败不重试。

        文章页为静态 HTML，正文以 HTML 片段存于内嵌脚本变量 ``contentdate``、
        发布时间存于 ``publishDate``（YYYYMMDDHHMMSS），由 detail_content /
        detail_time 纯函数解析。由 TimeEnrichment 显式启用，默认搜索路径不调用；
        验证码/频控即停，任一命中即 SUCCESS，metadata 只带命中的字段。
        """
        if self.http_get is None:
            return AdapterResult(
                status=SourceStatus.SOURCE_UNAVAILABLE,
                error_code=SourceErrorCode.SOURCE_UNAVAILABLE,
                message="CCTV news HTTP transport is not configured",
                retryable=False,
            )
        try:
            response = self.http_get(url, timeout=10)
            status_code = getattr(response, "status_code", None)
            if status_code is not None:
                if status_code == 429:
                    return AdapterResult(status=SourceStatus.RATE_LIMITED, error_code=SourceErrorCode.RATE_LIMITED, message="CCTV news detail request was rate limited", retryable=False)
                if status_code in {401, 403}:
                    return AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_REQUIRED, message="CCTV news rejected the detail request", retryable=False)
                if status_code >= 400:
                    return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"CCTV news detail returned HTTP {status_code}", retryable=False)
            content = getattr(response, "content", None)
            page = content.decode("utf-8", errors="replace") if isinstance(content, (bytes, bytearray)) else str(getattr(response, "text", content))
        except TimeoutError:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.NETWORK_TIMEOUT, message="CCTV news detail request timed out", retryable=False)
        except Exception as exc:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"CCTV news detail request failed: {type(exc).__name__}", retryable=False)
        detail_time = parse_detail_time(page, clock=self.clock)
        detail_content = parse_detail_content(page)
        if detail_time is None and detail_content is None:
            return AdapterResult(status=SourceStatus.EMPTY, message="detail page exposes no publish time or content signal")
        metadata: dict[str, Any] = {}
        if detail_time is not None:
            metadata.update({
                "publishedAt": detail_time.iso,
                "publishedAtConfidence": detail_time.confidence,
                "detectedBy": detail_time.detected_by,
            })
        if detail_content is not None:
            metadata.update({"content": detail_content.text, "contentDetectedBy": detail_content.detected_by})
        return AdapterResult(status=SourceStatus.SUCCESS, response_metadata=metadata)

    def search(self, request: ContentSearchRequest) -> AdapterResult:
        if self.http_get is None:
            return AdapterResult(
                status=SourceStatus.SOURCE_UNAVAILABLE,
                error_code=SourceErrorCode.SOURCE_UNAVAILABLE,
                message="CCTV news HTTP transport is not configured",
                retryable=True,
            )
        try:
            response = self.http_get(DATA_FILE_TEMPLATE.format(column=self.column, page=1), timeout=10)
            status_code = getattr(response, "status_code", None)
            if status_code == 429:
                return AdapterResult(status=SourceStatus.RATE_LIMITED, error_code=SourceErrorCode.RATE_LIMITED, message="CCTV news list was rate limited", retryable=True)
            if status_code in {401, 403}:
                return AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_REQUIRED, message="CCTV rejected the anonymous list request", retryable=False)
            if status_code is not None and status_code >= 500:
                return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"CCTV news list returned HTTP {status_code}", retryable=True)
            if status_code is not None and status_code >= 400:
                return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"CCTV news list returned HTTP {status_code}", retryable=False)
            payload = self.parse_jsonp(response.text)
            if payload is None:
                return AdapterResult(
                    status=SourceStatus.PARSER_CHANGED,
                    error_code=SourceErrorCode.PARSER_CHANGED,
                    message="CCTV response is not a JSONP envelope",
                    retryable=False,
                    response_metadata={"contentTypeClass": "non_jsonp"},
                )
            return self._filter_by_query(self.parse_data(payload), request.query)
        except TimeoutError:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.NETWORK_TIMEOUT, message="CCTV request timed out", retryable=True)
        except Exception as exc:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"CCTV request failed: {type(exc).__name__}", retryable=True)

    def _filter_by_query(self, result: AdapterResult, query: str) -> AdapterResult:
        """栏目列表不认识关键词：本地按标题/摘要/关键词过滤，让 search 语义成立。

        匹配不到时如实报 empty（附原因），绝不把无关的最新列表伪装成搜索结果。
        """
        if result.status is not SourceStatus.SUCCESS or not result.items:
            return result
        tokens = [token for token in str(query or "").split() if token] or [str(query or "")]
        matched = [
            item for item in result.items
            if _matches(item.payload, tokens)
        ]
        metadata = {**(result.response_metadata or {}), "keywordFiltered": {"queryTokens": tokens, "matched": len(matched), "candidates": len(result.items)}}
        if not matched:
            return AdapterResult(
                status=SourceStatus.EMPTY,
                response_metadata=metadata,
                message=f"最新栏目列表（{len(result.items)} 条）中没有匹配关键词的条目",
            )
        result.items = matched
        result.response_metadata = metadata
        return result

    def parse_jsonp_and_map(self, text: str) -> AdapterResult:
        """Offline parse of a saved JSONP list file (tests and future capture reuse)."""
        payload = self.parse_jsonp(text)
        if payload is None:
            return AdapterResult(
                status=SourceStatus.PARSER_CHANGED,
                error_code=SourceErrorCode.PARSER_CHANGED,
                message="CCTV response is not a JSONP envelope",
                retryable=False,
                response_metadata={"contentTypeClass": "non_jsonp"},
            )
        return self.parse_data(payload)

    @staticmethod
    def parse_jsonp(text: str) -> dict[str, Any] | None:
        """Strip the ``callback({...})`` wrapper and decode the JSON body."""
        start = text.find("(")
        end = text.rfind(")")
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start + 1 : end])
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def parse_data(self, payload: dict[str, Any]) -> AdapterResult:
        data = payload.get("data")
        records = data.get("list") if isinstance(data, dict) else None
        if not isinstance(records, list):
            return AdapterResult(
                status=SourceStatus.PARSER_CHANGED,
                error_code=SourceErrorCode.PARSER_CHANGED,
                message="CCTV list envelope is missing data.list",
                retryable=False,
                response_metadata=self._metadata(records=None),
            )
        if not records:
            return AdapterResult(status=SourceStatus.EMPTY, response_metadata=self._metadata(records=0))
        items: list[RawItem] = []
        for position, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            title = " ".join(str(record.get("title") or "").split()) or None
            url = record.get("url")
            source_id = record.get("id")
            if not title or not url or not source_id:
                continue
            published_at = _to_utc(record.get("focus_date"))
            items.append(RawItem(
                platform=self.platform,
                raw_data_ref=f"raw/cctv_news/{source_id}.jsonp",
                payload={
                    "sourceItemId": str(source_id),
                    "sourceType": "news_media",
                    "contentType": "article",
                    "title": title,
                    "summary": _clean(record.get("brief")),
                    "content": None,
                    "url": url,
                    "publishedAt": published_at,
                    "media": _media(record),
                    "method": "http",
                    "adapterVersion": self.adapter_version,
                    "hasFullContent": False,
                    "publishedAtConfidence": 1.0 if published_at else 0.0,
                    "warnings": ["search_result_summary_only"],
                    "ext": {
                        "keywords": _clean(record.get("keywords")),
                        "listPosition": position,
                        "cctvColumn": self.column,
                        "focusDateRaw": record.get("focus_date"),
                    },
                },
            ))
        return AdapterResult(
            items=items,
            status=SourceStatus.SUCCESS if items else SourceStatus.EMPTY,
            response_metadata=self._metadata(records=len(records)),
        )

    def _metadata(self, *, records: int | None) -> dict[str, Any]:
        values = {"column": self.column, "recordCount": records}
        signature = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return {**values, "structureSignature": "sha256:" + hashlib.sha256(signature).hexdigest()[:12]}


def _to_utc(value: Any) -> str | None:
    if not value:
        return None
    try:
        moment = datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
    except ValueError:
        return None
    return moment.astimezone(timezone.utc).isoformat()


def _clean(value: Any) -> str | None:
    if not value:
        return None
    text = " ".join(str(value).split())
    return text or None


def _media(record: dict[str, Any]) -> list[dict[str, str]]:
    images = []
    for key in ("image", "image2", "image3"):
        url = _clean(record.get(key))
        if url:
            images.append({"type": "image", "url": url})
    return images


def _matches(payload: dict[str, Any], tokens: list[str]) -> bool:
    haystack = " ".join(filter(None, [
        payload.get("title"),
        payload.get("summary"),
        (payload.get("ext") or {}).get("keywords"),
    ])).lower()
    return any(token.lower() in haystack for token in tokens if token)
