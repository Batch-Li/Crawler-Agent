from __future__ import annotations

import re
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from crawler_tool.domain import ContentSearchRequest, SourceErrorCode, SourceStatus
from crawler_tool.sources.base import AdapterResult, RawItem, SourceAdapter


class XiaohongshuAdapter(SourceAdapter):
    """Fixture-first Xiaohongshu search adapter; network is opt-in."""

    platform = "xiaohongshu"
    adapter_version = "0.1.0-parser"

    def __init__(self, http_post: Any | None = None, *, mode: str = "disabled") -> None:
        self.http_post = http_post
        self.mode = mode

    def health(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": "unknown",
            "adapter_version": self.adapter_version,
            "capabilities": ["search"] if self.mode != "disabled" else [],
            "authMode": "authorized_first_page" if self.mode == "authorized_first_page" else "anonymous",
            "credentialStatus": "configured" if self.mode == "authorized_first_page" else "not_required",
            "pagination": "disabled",
            "detail": "disabled",
        }

    def search(self, request: ContentSearchRequest) -> AdapterResult:
        if self.mode == "disabled" or self.http_post is None:
            return AdapterResult(status=SourceStatus.SOURCE_UNAVAILABLE, error_code=SourceErrorCode.SOURCE_UNAVAILABLE, message="Xiaohongshu search is disabled", retryable=False)
        return AdapterResult(status=SourceStatus.SOURCE_UNAVAILABLE, error_code=SourceErrorCode.SOURCE_UNAVAILABLE, message="Xiaohongshu network search requires an approved provider", retryable=False)

    def probe_first_page(self, request: ContentSearchRequest, *, cookie_header: str | None = None) -> AdapterResult:
        if self.http_post is None:
            return AdapterResult(status=SourceStatus.SOURCE_UNAVAILABLE, error_code=SourceErrorCode.SOURCE_UNAVAILABLE, message="Xiaohongshu HTTP transport is not configured", retryable=False)
        try:
            kwargs = {
                "json": {"ext_flags": [], "image_formats": ["jpg", "webp", "avif"], "keyword": request.query, "note_type": 0, "page": 1, "page_size": min(request.limit, 10), "sort": "time_descending"},
                "timeout": 10,
                "follow_redirects": False,
            }
            if cookie_header:
                kwargs["headers"] = {"Cookie": cookie_header}
            response = self.http_post("https://edith.xiaohongshu.com/api/sns/web/v1/search/notes", **kwargs)
            status_code = getattr(response, "status_code", None)
            if status_code == 429:
                return AdapterResult(status=SourceStatus.RATE_LIMITED, error_code=SourceErrorCode.RATE_LIMITED, message="Xiaohongshu search was rate limited", retryable=False, response_metadata={"httpStatus": 429, "responseType": "unknown", "businessCodeCategory": "rate"})
            if status_code in {401, 403}:
                return AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_REQUIRED, message="Xiaohongshu search requires authorization", retryable=False, response_metadata={"httpStatus": status_code, "responseType": "unknown", "businessCodeCategory": "auth"})
            if status_code is not None and status_code >= 400:
                return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message="Xiaohongshu search returned an HTTP error", retryable=False, response_metadata={"httpStatus": status_code, "responseType": "unknown", "businessCodeCategory": "unknown"})
            content_type = getattr(response, "headers", {}).get("content-type", "").lower()
            if content_type and "json" not in content_type:
                return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Xiaohongshu response is not JSON", retryable=False, response_metadata={"httpStatus": status_code, "responseType": "html", "businessCodeCategory": "parser"})
            payload = response.json()
            result = self.parse_payload(payload)
            result.response_metadata = _metadata(status_code, content_type, payload)
            return result
        except TimeoutError:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.NETWORK_TIMEOUT, message="Xiaohongshu request timed out", retryable=False)
        except Exception as exc:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"Xiaohongshu probe failed: {type(exc).__name__}", retryable=False)

    def parse_payload(self, payload: Any) -> AdapterResult:
        if not isinstance(payload, dict):
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Xiaohongshu response is not an object", retryable=False)
        if payload.get("code") not in (None, 0):
            category = _business_category(payload.get("code"))
            status = SourceStatus.AUTHENTICATION_REQUIRED if category == "auth" else SourceStatus.RATE_LIMITED if category == "rate" else SourceStatus.NETWORK_ERROR
            error_code = SourceErrorCode.AUTH_REQUIRED if category == "auth" else SourceErrorCode.RATE_LIMITED if category == "rate" else SourceErrorCode.INVALID_RESPONSE
            return AdapterResult(status=status, error_code=error_code, message="Xiaohongshu returned an access error", retryable=False)
        data = payload.get("data")
        records = data.get("items") if isinstance(data, dict) else None
        if records is None:
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Xiaohongshu result list is missing", retryable=False)
        if not isinstance(records, list):
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Xiaohongshu result list is invalid", retryable=False)
        items: list[RawItem] = []
        for record in records:
            if not isinstance(record, dict) or record.get("model_type") == "rec_query":
                continue
            if record.get("model_type") not in ("note", "normal"):
                continue
            card = record.get("note_card")
            if not isinstance(card, dict):
                continue
            title = _clean(card.get("display_title"))
            note_id = str(record.get("id") or "") or None
            url = _note_url(record, note_id)
            if not title or not note_id or not url:
                continue
            user = card.get("user") if isinstance(card.get("user"), dict) else {}
            interact = card.get("interact_info") if isinstance(card.get("interact_info"), dict) else {}
            published_raw = record.get("publish_time") or card.get("last_update_time")
            items.append(RawItem(
                platform=self.platform,
                payload={
                    "sourceItemId": note_id,
                    "sourceType": "social_media",
                    "contentType": "post",
                    "title": title,
                    "summary": title,
                    "content": None,
                    "url": url,
                    "author": {"name": _clean(user.get("nickname"))},
                    "publishedAt": _timestamp(published_raw),
                    "metrics": {
                        "likeCount": _integer(interact.get("liked_count")),
                        "commentCount": _integer(interact.get("comment_count")),
                        "shareCount": _integer(interact.get("shared_count")),
                        "collectCount": _integer(interact.get("collected_count")),
                    },
                    "method": "authorized_session" if self.mode == "authorized_first_page" else "http",
                    "adapterVersion": self.adapter_version,
                    "hasFullContent": False,
                    "publishedAtConfidence": 1.0 if _timestamp(published_raw) else 0.0,
                    "warnings": ["search_result_summary_only", "content_missing"],
                    "ext": {"publishedAtRaw": published_raw},
                },
            ))
        return AdapterResult(items=items, status=SourceStatus.SUCCESS if items else SourceStatus.EMPTY)


def _metadata(status_code: Any, content_type: str, payload: Any) -> dict[str, Any]:
    keys = sorted(str(key) for key in payload.keys())[:20] if isinstance(payload, dict) else []
    code = payload.get("code") if isinstance(payload, dict) and isinstance(payload.get("code"), int) and not isinstance(payload.get("code"), bool) else None
    category = _business_category(code) if code not in (None, 0) else "success"
    shape = {"httpStatus": status_code, "responseType": "json" if "json" in content_type else "unknown", "topLevelKeys": keys, "businessCode": code, "businessCodeCategory": category}
    return {**shape, "structureSignature": "sha256:" + hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:12]}


def _business_category(code: Any) -> str:
    if code in {1001, 300012, 300013}:
        return "auth"
    if code in {300011, 300014, 429}:
        return "rate"
    if code in {300001, 300002}:
        return "signature"
    return "unknown"

def _clean(value: Any) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _note_url(record: dict[str, Any], note_id: str | None) -> str | None:
    for value in (record.get("url"), record.get("note_url"), record.get("share_url")):
        if isinstance(value, str) and value.startswith("https://") and "xiaohongshu.com" in (urlparse(value).hostname or ""):
            return value
    return f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else None


def _timestamp(value: Any) -> str | None:
    try:
        number = int(value)
        if number > 10_000_000_000:
            number //= 1000
        return datetime.fromtimestamp(number, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
