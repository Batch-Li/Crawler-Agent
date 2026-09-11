from __future__ import annotations

import re
import hashlib
import json
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urlparse

from crawler_tool.domain import ContentSearchRequest, SourceErrorCode, SourceStatus
from crawler_tool.sources.base import AdapterResult, RawItem, SourceAdapter
from crawler_tool.sources.weibo_authorized import parse_cookie_header


class WechatAuthorizedGet:
    allowed_hosts = {"mp.weixin.qq.com"}

    def __init__(self, http_get: Any, cookie_header: str) -> None:
        self.http_get = http_get
        self.cookies = parse_cookie_header(cookie_header)

    def __call__(self, url: str, **kwargs: Any) -> Any:
        if (urlparse(url).hostname or "").lower() not in self.allowed_hosts:
            raise ValueError("Wechat request target is not allowed")
        kwargs["cookies"] = dict(self.cookies)
        kwargs["follow_redirects"] = False
        return self.http_get(url, **kwargs)


class WechatAuthorizedListAdapter(SourceAdapter):
    """Default-disabled WeChat authorized first-page list adapter."""

    platform = "wechat"
    adapter_version = "0.1.0-authorized-list"
    base_url = "https://mp.weixin.qq.com"

    def __init__(self, http_get: Any | None = None, *, cookie_header: str = "", max_items: int = 5) -> None:
        self.credential_configured = bool(parse_cookie_header(cookie_header))
        self.http_get = WechatAuthorizedGet(http_get, cookie_header) if http_get and self.credential_configured else None
        self.max_items = max(1, min(max_items, 10))

    def health(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": "unknown",
            "adapter_version": self.adapter_version,
            "capabilities": ["search"],
            "authMode": "authorized_first_page",
            "credentialStatus": "configured" if self.credential_configured else "unavailable",
            "pagination": "disabled",
        }

    def get_token(self) -> tuple[str | None, AdapterResult | None]:
        if self.http_get is None:
            return None, AdapterResult(status=SourceStatus.SOURCE_UNAVAILABLE, error_code=SourceErrorCode.SOURCE_UNAVAILABLE, message="Wechat authorized session is not configured", retryable=False)
        try:
            response = self.http_get(f"{self.base_url}/", timeout=10)
            if _is_auth_response(response):
                return None, AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_EXPIRED, message="Wechat authorized session has expired", retryable=False)
            token = _token(response.text)
            return (token, None) if token else (None, AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Wechat session token is missing", retryable=False))
        except Exception as exc:
            return None, AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"Wechat token request failed: {type(exc).__name__}", retryable=True)

    def search_account(self, account_name: str, token: str) -> tuple[dict[str, Any] | None, AdapterResult | None]:
        try:
            response = self.http_get(f"{self.base_url}/cgi-bin/searchbiz", params={"action": "search_biz", "token": token, "lang": "zh_CN", "f": "json", "ajax": "1", "query": account_name, "begin": "0", "count": "1"}, timeout=10)
            if _is_auth_response(response):
                return None, AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_EXPIRED, message="Wechat authorized session has expired", retryable=False)
            account_data = response.json()
            business_error = _base_resp_result(response, account_data, stage="account_search")
            if business_error:
                return None, business_error
            accounts = account_data.get("list")
            if accounts is None:
                return None, _error_result(SourceStatus.PARSER_CHANGED, SourceErrorCode.PARSER_CHANGED, "Wechat account search response changed", diagnostics=_safe_metadata(response, stage="account_search", payload=account_data))
            if not accounts:
                return None, AdapterResult(status=SourceStatus.EMPTY)
            return accounts[0], None
        except Exception as exc:
            return None, AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"Wechat account search failed: {type(exc).__name__}", retryable=True)

    def list_articles(self, fakeid: str, token: str, limit: int) -> tuple[list[dict[str, Any]] | None, AdapterResult | None]:
        try:
            response = self.http_get(f"{self.base_url}/cgi-bin/appmsgpublish", params={"sub": "list", "sub_action": "list_ex", "begin": "0", "count": str(min(limit, self.max_items)), "fakeid": fakeid, "type": "101_1_102_103", "show_type": "", "free_publish_type": "1_102_103", "search_card": "0", "query": "", "token": token, "lang": "zh_CN", "f": "json", "ajax": "1"}, timeout=10)
            if _is_auth_response(response):
                return None, AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_EXPIRED, message="Wechat authorized session has expired", retryable=False)
            payload = response.json()
            business_error = _base_resp_result(response, payload, stage="article_list")
            if business_error:
                return None, business_error
            records = payload.get("app_msg_list")
            if records is None:
                return None, _error_result(SourceStatus.PARSER_CHANGED, SourceErrorCode.PARSER_CHANGED, "Wechat article list response changed", diagnostics=_safe_metadata(response, stage="article_list", payload=payload))
            return records, None
        except Exception as exc:
            return None, AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"Wechat article list failed: {type(exc).__name__}", retryable=True)

    def search(self, request: ContentSearchRequest) -> AdapterResult:
        token, error = self.get_token()
        if error:
            return error
        account, error = self.search_account(request.query, token)
        if error:
            return error
        records, error = self.list_articles(str(account["fakeid"]), token, request.limit)
        if error:
            return error
        return self.parse_listing({"app_msg_list": records}, account_name=account.get("nickname") or request.query)

    def parse_payload(self, capture: dict[str, Any]) -> AdapterResult:
        """Offline parse of a manually captured MP-backend list envelope.

        Accepts the documented ``app_msg_list`` family (appmsgpublish
        ``sub=list`` and appmsg ``action=list_ex`` responses a human opened
        in a browser). Business errors keep the existing taxonomy; unknown
        shapes are parser changes, never empty results.
        """
        records = capture.get("app_msg_list") if isinstance(capture, dict) else None
        if not isinstance(records, list):
            return _error_result(
                SourceStatus.PARSER_CHANGED,
                SourceErrorCode.PARSER_CHANGED,
                "Wechat capture does not match the documented app_msg_list envelope",
                retryable=False,
                diagnostics=_safe_metadata(None, stage="capture", payload=capture),
            )
        business_error = _base_resp_result(None, capture, stage="capture")
        if business_error:
            return business_error
        result = self.parse_listing({"app_msg_list": records}, account_name=None)
        for item in result.items:
            item.payload["method"] = "browser"
        return result

    def parse_listing(self, payload: dict[str, Any], *, account_name: str | None) -> AdapterResult:
        records = payload.get("app_msg_list")
        if records is None:
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Wechat article list response changed", retryable=False)
        items: list[RawItem] = []
        for position, record in enumerate(records):
            url = record.get("link")
            title = _clean(record.get("title"))
            if not url or not title:
                continue
            source_id = str(record.get("aid") or record.get("appmsgid") or _article_id(url) or "") or None
            published_at = _timestamp(record.get("create_time"))
            items.append(RawItem(
                platform=self.platform,
                raw_data_ref=None,
                payload={
                    "sourceItemId": source_id,
                    "sourceType": "news_media",
                    "contentType": "article",
                    "title": title,
                    "summary": _clean(record.get("digest") or record.get("summary")),
                    "content": None,
                    "url": url,
                    "author": {"name": _clean(record.get("author")) or account_name},
                    "publishedAt": published_at,
                    "method": "authorized_session",
                    "adapterVersion": self.adapter_version,
                    "hasFullContent": False,
                    "publishedAtConfidence": 1.0 if published_at else 0.0,
                    "warnings": ["search_result_summary_only"],
                    "ext": {"accountName": account_name, "listPosition": position},
                },
            ))
        return AdapterResult(items=items, status=SourceStatus.SUCCESS if items else SourceStatus.EMPTY)


def _base_resp_result(response: Any, payload: Any, *, stage: str) -> AdapterResult | None:
    if not isinstance(payload, dict):
        return _error_result(SourceStatus.PARSER_CHANGED, SourceErrorCode.PARSER_CHANGED, "Wechat response is not an object", diagnostics=_safe_metadata(response, stage=stage, payload=payload))
    base = payload.get("base_resp")
    if not isinstance(base, dict):
        return None
    code = base.get("ret", base.get("errcode"))
    if code in (None, 0, "0"):
        return None
    if isinstance(code, bool) or not isinstance(code, (int, str)) or (isinstance(code, str) and not code.isdigit()):
        safe_code = None
    else:
        safe_code = int(code)
    category = "rate_limited" if safe_code in {200013, 45009} else "auth_or_permission" if safe_code in {200003, 200008, 200011, 40001, 40014} else "invalid_request" if safe_code in {40002, 40003} else "unknown_error"
    status = SourceStatus.RATE_LIMITED if category == "rate_limited" else SourceStatus.AUTHENTICATION_REQUIRED if category == "auth_or_permission" else SourceStatus.NETWORK_ERROR
    error_code = SourceErrorCode.RATE_LIMITED if category == "rate_limited" else SourceErrorCode.AUTH_REQUIRED if category == "auth_or_permission" else SourceErrorCode.INVALID_RESPONSE
    metadata = _safe_metadata(response, stage=stage, payload=payload)
    metadata.update({"baseRespCode": safe_code, "baseRespCategory": category})
    return _error_result(status, error_code, "Wechat upstream request returned a business error", retryable=False, diagnostics=metadata)

def _safe_metadata(response: Any, *, stage: str, payload: Any = None) -> dict[str, Any]:
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type", "")).lower()
    kind = "json" if "json" in content_type else "html" if "html" in content_type else "unknown"
    if isinstance(payload, dict):
        keys = sorted(str(key) for key in payload.keys())[:20]
        has_list = "app_msg_list" in payload
        has_base = "base_resp" in payload
        record_count = len(payload.get("app_msg_list") or []) if isinstance(payload.get("app_msg_list"), list) else 0
    else:
        keys, has_list, has_base, record_count = [], False, False, 0
    shape = {"stage": stage, "kind": kind, "keys": keys, "hasList": has_list, "hasBaseResp": has_base, "recordCount": record_count}
    return {**shape, "structureSignature": "sha256:" + hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:12]}


def _error_result(status: SourceStatus, code: str, message: str, *, retryable: bool = False, diagnostics: dict[str, Any] | None = None) -> AdapterResult:
    return AdapterResult(status=status, error_code=code, message=message, retryable=retryable, response_metadata=diagnostics)

def _token(page: str) -> str | None:
    match = re.search(r"(?:[?&]|\b)token=(\d+)", page)
    return match.group(1) if match else None


def _is_auth_response(response: Any) -> bool:
    if getattr(response, "status_code", None) in {401, 403}:
        return True
    return any(marker in getattr(response, "text", "") for marker in ("请登录", "安全验证", "验证码"))


def _clean(value: Any) -> str | None:
    if not value:
        return None
    text = re.sub(r"</?em>", "", str(value))
    text = unescape(" ".join(text.split()))
    return text or None


def _timestamp(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat() if value not in (None, "") else None
    except (TypeError, ValueError, OSError):
        return None


def _article_id(url: str) -> str | None:
    match = re.search(r"(?:mid|idx|sn)=([^&#]+)", url)
    return match.group(1) if match else None
