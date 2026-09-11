from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler_tool.domain import ContentSearchRequest, SourceErrorCode, SourceStatus
from crawler_tool.sources.base import AdapterResult, RawItem, SourceAdapter

BASE_URL = "https://weixin.sogou.com"


@dataclass(frozen=True)
class SogouPageDiagnosis:
    classification: str
    result_block_count: int
    title_anchor_count: int
    time_script_count: int
    rate_limit_signal: bool
    login_signal: bool
    empty_result_signal: bool
    has_wrapper: bool

    def report(self) -> dict[str, Any]:
        values = {
            "classification": self.classification,
            "resultBlockCount": self.result_block_count,
            "titleAnchorCount": self.title_anchor_count,
            "timeScriptCount": self.time_script_count,
            "rateLimitSignal": self.rate_limit_signal,
            "loginSignal": self.login_signal,
            "emptyResultSignal": self.empty_result_signal,
            "hasWrapper": self.has_wrapper,
        }
        signature = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return {**values, "structureSignature": "sha256:" + hashlib.sha256(signature).hexdigest()[:12]}


class SogouWechatAdapter(SourceAdapter):
    """Best-effort anonymous WeChat article discovery via the public Sogou index.

    Scope is discovery only: title, account name, publish time, summary and
    Sogou's wrapped article link. The wrapped link is never resolved here —
    each resolution costs one extra request and stays out of budget.
    Rate-limit/challenge pages are classified honestly and never retried:
    the caller stops instead of fighting the captcha.
    """

    platform = "sogou_wechat"
    adapter_version = "0.1.0-anonymous"

    def __init__(self, http_get: Any | None = None, *, mode: str = "disabled") -> None:
        self.mode = mode if mode in ("disabled", "anonymous_best_effort") else "disabled"
        self.http_get = http_get if self.mode == "anonymous_best_effort" else None

    def health(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": "unknown",
            "adapter_version": self.adapter_version,
            "capabilities": ["search"],
            "pagination": "disabled",
            "authMode": "anonymous_best_effort" if self.mode == "anonymous_best_effort" else "disabled",
            "mode": self.mode,
            "notes": "discovery_only_wrapped_links_no_retry_on_challenge",
        }

    def search(self, request: ContentSearchRequest) -> AdapterResult:
        if self.http_get is None:
            return AdapterResult(
                status=SourceStatus.SOURCE_UNAVAILABLE,
                error_code=SourceErrorCode.SOURCE_UNAVAILABLE,
                message="Sogou wechat discovery is disabled; set SOGOU_WECHAT_MODE=anonymous_best_effort to enable",
                retryable=False,
            )
        try:
            response = self.http_get(f"{BASE_URL}/weixin", params={"type": 2, "query": request.query}, timeout=10)
            status_code = getattr(response, "status_code", None)
            if status_code == 429:
                return AdapterResult(status=SourceStatus.RATE_LIMITED, error_code=SourceErrorCode.RATE_LIMITED, message="Sogou wechat search was rate limited", retryable=False)
            if status_code in {401, 403}:
                return AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_REQUIRED, message="Sogou rejected the anonymous search request", retryable=False)
            if status_code is not None and status_code >= 500:
                return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"Sogou wechat search returned HTTP {status_code}", retryable=True)
            if status_code is not None and status_code >= 400:
                return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"Sogou wechat search returned HTTP {status_code}", retryable=False)
            content_type = str(getattr(response, "headers", {}).get("content-type", "")).lower()
            if content_type and "html" not in content_type and "xhtml" not in content_type:
                return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message="Sogou returned a non-HTML response", retryable=False, response_metadata={"contentTypeClass": "non_html"})
            return self.parse_html(response.text)
        except TimeoutError:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.NETWORK_TIMEOUT, message="Sogou request timed out", retryable=True)
        except Exception as exc:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"Sogou request failed: {type(exc).__name__}", retryable=True)

    def diagnose_html(self, page: str) -> SogouPageDiagnosis:
        soup = BeautifulSoup(page, "html.parser")
        blocks = soup.select("div.txt-box")
        title_anchors = soup.select('div.txt-box h3 a[uigs^="article_title_"]')
        time_scripts = len(re.findall(r"timeConvert\('(\d+)'\)", page))
        rate_limit = any(marker in page for marker in ("antispider", "请输入验证码", "访问过于频繁", "您的访问出错了"))
        login = "请登录" in page
        empty_signal = "没有找到" in page
        has_wrapper = 'id="wrapper"' in page
        if rate_limit:
            classification = SourceStatus.RATE_LIMITED.value
        elif login:
            classification = SourceStatus.AUTHENTICATION_REQUIRED.value
        elif blocks and title_anchors:
            classification = SourceStatus.SUCCESS.value
        elif empty_signal and has_wrapper:
            classification = SourceStatus.EMPTY.value
        else:
            classification = SourceStatus.PARSER_CHANGED.value
        return SogouPageDiagnosis(
            classification=classification,
            result_block_count=len(blocks),
            title_anchor_count=len(title_anchors),
            time_script_count=time_scripts,
            rate_limit_signal=rate_limit,
            login_signal=login,
            empty_result_signal=empty_signal,
            has_wrapper=has_wrapper,
        )

    def parse_html(self, page: str) -> AdapterResult:
        diagnosis = self.diagnose_html(page)
        metadata = {"pageDiagnosis": diagnosis.report()}
        if diagnosis.classification == SourceStatus.RATE_LIMITED.value:
            return AdapterResult(status=SourceStatus.RATE_LIMITED, error_code=SourceErrorCode.RATE_LIMITED, message="Sogou returned a challenge page; stopping without retry", retryable=False, response_metadata=metadata)
        if diagnosis.classification == SourceStatus.AUTHENTICATION_REQUIRED.value:
            return AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_REQUIRED, message="Sogou requires login", retryable=False, response_metadata=metadata)
        if diagnosis.classification == SourceStatus.EMPTY.value:
            return AdapterResult(status=SourceStatus.EMPTY, response_metadata=metadata)
        if diagnosis.classification == SourceStatus.PARSER_CHANGED.value:
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Sogou wechat result structure is missing or changed", retryable=False, response_metadata=metadata)
        soup = BeautifulSoup(page, "html.parser")
        items: list[RawItem] = []
        for position, block in enumerate(soup.select("div.txt-box")):
            title_anchor = block.select_one('h3 a[uigs^="article_title_"]')
            if title_anchor is None:
                continue
            title = " ".join(title_anchor.get_text(" ", strip=True).split())
            href = title_anchor.get("href", "")
            if not title or not href:
                continue
            url = urljoin(f"{BASE_URL}/", href)
            time_match = re.search(r"timeConvert\('(\d+)'\)", str(block))
            published_at = _timestamp(time_match.group(1) if time_match else None)
            account_node = block.select_one("div.s-p span.all-time-y2")
            account_name = _account(account_node.get_text(" ", strip=True) if account_node else None)
            summary_node = block.select_one(".txt-info")
            summary = " ".join(summary_node.get_text(" ", strip=True).split()) if summary_node else None
            source_id = "sg_" + hashlib.sha256(f"{account_name or ''}|{title}|{published_at or ''}".encode("utf-8")).hexdigest()[:16]
            items.append(RawItem(
                platform=self.platform,
                raw_data_ref=f"raw/sogou_wechat/{source_id}.html",
                payload={
                    "sourceItemId": source_id,
                    "sourceType": "news_media",
                    "contentType": "article",
                    "title": title,
                    "summary": summary,
                    "content": None,
                    "url": url,
                    "author": {"name": account_name},
                    "publishedAt": published_at,
                    "method": "http",
                    "adapterVersion": self.adapter_version,
                    "hasFullContent": False,
                    "publishedAtConfidence": 1.0 if published_at else 0.0,
                    "warnings": [
                        "search_result_summary_only",
                        "article_url_is_sogou_wrapped",
                        "synthetic_source_id",
                    ],
                    "ext": {"accountName": account_name, "listPosition": position},
                },
            ))
        return AdapterResult(items=items, status=SourceStatus.SUCCESS if items else SourceStatus.EMPTY, response_metadata=metadata)


def _timestamp(value: str | None) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat() if value else None
    except (TypeError, ValueError, OSError):
        return None


def _account(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw.rstrip("：:").strip() or None
