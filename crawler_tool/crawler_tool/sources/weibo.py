from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse
import hashlib
import json

from bs4 import BeautifulSoup

from crawler_tool.domain import ContentSearchRequest, SourceErrorCode, SourceStatus
from crawler_tool.sources.base import AdapterResult, RawItem, SourceAdapter


@dataclass(frozen=True)
class WeiboPageDiagnosis:
    classification: str
    has_search_container: bool
    card_count: int
    post_card_count: int
    post_link_count: int
    text_node_count: int
    error_box_count: int
    login_signal: bool
    challenge_signal: bool
    rate_limit_signal: bool
    empty_result_signal: bool

    def report(self) -> dict[str, Any]:
        values = {
            "classification": self.classification,
            "hasSearchContainer": self.has_search_container,
            "cardCount": self.card_count,
            "postCardCount": self.post_card_count,
            "postLinkCount": self.post_link_count,
            "textNodeCount": self.text_node_count,
            "errorBoxCount": self.error_box_count,
            "loginSignal": self.login_signal,
            "challengeSignal": self.challenge_signal,
            "rateLimitSignal": self.rate_limit_signal,
            "emptyResultSignal": self.empty_result_signal,
        }
        signature = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return {**values, "structureSignature": "sha256:" + hashlib.sha256(signature).hexdigest()[:12]}


class WeiboAdapter(SourceAdapter):
    """Parser-first Weibo adapter. Authentication stays outside the adapter."""

    platform = "weibo"
    adapter_version = "0.2.0-anonymous"

    def __init__(self, http_get: Any | None = None) -> None:
        self.http_get = http_get

    def health(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": "unknown",
            "adapter_version": self.adapter_version,
            "capabilities": ["search"],
            "pagination": "disabled",
            "authMode": "anonymous_best_effort",
        }

    def search(self, request: ContentSearchRequest) -> AdapterResult:
        if self.http_get is None:
            return AdapterResult(status=SourceStatus.SOURCE_UNAVAILABLE, error_code=SourceErrorCode.SOURCE_UNAVAILABLE, message="Weibo HTTP transport is not configured", retryable=True)
        try:
            response = self.http_get("https://s.weibo.com/weibo", params={"q": request.query, "typeall": 1, "suball": 1, "page": 1}, timeout=10)
            status_code = getattr(response, "status_code", None)
            if status_code in {301, 302, 303, 307, 308}:
                # httpx 0.28 的 raise_for_status 对 3xx 也会抛 HTTPStatusError，
                # 会在兜底分支被误报成 network_error。微博在会话失效/登录墙时
                # 返回重定向，必须显式归类为认证问题才有可行动的诊断信号。
                location = str(getattr(response, "headers", {}).get("location", ""))
                redirect_host = (urlparse(location).hostname or "") or None
                metadata = {"redirectStatus": status_code, "redirectHost": redirect_host}
                return AdapterResult(
                    status=SourceStatus.AUTHENTICATION_REQUIRED,
                    error_code=SourceErrorCode.AUTH_REQUIRED,
                    message=f"Weibo redirected the request (HTTP {status_code}" + (f" to {redirect_host}" if redirect_host else "") + "); session or access was not accepted",
                    retryable=False,
                    response_metadata=metadata,
                )
            if status_code == 429:
                return AdapterResult(status=SourceStatus.RATE_LIMITED, error_code=SourceErrorCode.RATE_LIMITED, message="Weibo search was rate limited", retryable=True)
            if status_code in {401, 403}:
                return AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_REQUIRED, message="Weibo rejected the anonymous search request", retryable=False)
            if status_code is not None and status_code >= 500:
                return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"Weibo search returned HTTP {status_code}", retryable=True)
            if status_code is not None and status_code >= 400:
                return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"Weibo search returned HTTP {status_code}", retryable=False)
            response.raise_for_status()
            content_type = getattr(response, "headers", {}).get("content-type", "").lower()
            if content_type and "html" not in content_type and "xhtml" not in content_type:
                return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message="Weibo returned a non-HTML response", retryable=False, response_metadata={"contentTypeClass": "non_html"})
            return self.parse_html(response.text)
        except TimeoutError:
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.NETWORK_TIMEOUT, message="Weibo request timed out", retryable=True)
        except Exception as exc:
            # 保留状态码细节（不含 Cookie/完整 URL），否则诊断无从下手。
            status_part = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f" (HTTP {status_part})" if status_part else ""
            return AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code=SourceErrorCode.INVALID_RESPONSE, message=f"Weibo request failed: {type(exc).__name__}{detail}", retryable=True)

    def diagnose_html(self, page: str) -> WeiboPageDiagnosis:
        soup = BeautifulSoup(page, "html.parser")
        cards = soup.select("div.card")
        text_nodes = soup.select("p.txt")
        post_links = [
            link for link in soup.select("a[href]")
            if re.search(r"weibo\.com/\d+/[\w-]+", link.get("href", ""))
        ]
        post_card_count = sum(
            1 for card in cards
            if card.select_one("p.txt") is not None
            and any(re.search(r"weibo\.com/\d+/[\w-]+", link.get("href", "")) for link in card.select("a[href]"))
        )
        rate_limit = any(marker in page for marker in ("访问频次过高", "请求过于频繁"))
        challenge = any(marker in page for marker in ("请完成安全验证", "请输入验证码", "人机验证"))
        login = any(marker in page for marker in ("请登录后", "登录后查看", "登录后继续"))
        has_container = "card-wrap" in page or bool(cards)
        empty_signal = "暂无相关微博" in page or "没有找到相关微博" in page
        if rate_limit:
            classification = SourceStatus.RATE_LIMITED.value
        elif login or challenge:
            classification = SourceStatus.AUTHENTICATION_REQUIRED.value
        elif post_card_count:
            classification = SourceStatus.SUCCESS.value
        elif empty_signal:
            classification = SourceStatus.EMPTY.value
        else:
            classification = SourceStatus.PARSER_CHANGED.value
        return WeiboPageDiagnosis(
            classification=classification,
            has_search_container=has_container,
            card_count=len(cards),
            post_card_count=post_card_count,
            post_link_count=len(post_links),
            text_node_count=len(text_nodes),
            error_box_count=len(soup.select(".m-error-box")),
            login_signal=login,
            challenge_signal=challenge,
            rate_limit_signal=rate_limit,
            empty_result_signal=empty_signal,
        )

    def parse_html(self, page: str) -> AdapterResult:
        diagnosis = self.diagnose_html(page)
        metadata = {"pageDiagnosis": diagnosis.report()}
        if diagnosis.classification == SourceStatus.RATE_LIMITED.value:
            return AdapterResult(status=SourceStatus.RATE_LIMITED, error_code=SourceErrorCode.RATE_LIMITED, message="Weibo returned a rate limit page", retryable=True, response_metadata=metadata)
        if diagnosis.classification == SourceStatus.AUTHENTICATION_REQUIRED.value:
            return AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code=SourceErrorCode.AUTH_REQUIRED, message="Weibo requires login or returned a challenge page", retryable=False, response_metadata=metadata)
        if diagnosis.classification == SourceStatus.EMPTY.value:
            return AdapterResult(status=SourceStatus.EMPTY, response_metadata=metadata)
        if diagnosis.classification == SourceStatus.PARSER_CHANGED.value:
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Weibo search card structure is missing or incomplete", retryable=False, response_metadata=metadata)
        soup = BeautifulSoup(page, "html.parser")
        cards = soup.select("div.card")
        items = []
        for card in cards:
            text_node = card.select_one("p.txt")
            time_link = next((link for link in card.select("a[href]") if re.search(r"weibo\.com/\d+/\w+", link.get("href", ""))), None)
            if text_node is None or time_link is None:
                continue
            url = urljoin("https://weibo.com", time_link.get("href"))
            source_id = _post_id(url)
            content = text_node.get_text(" ", strip=True)
            author_node = card.select_one("a.name")
            title = content[:80] if content else None
            action_text = " ".join(node.get_text(" ", strip=True) for node in card.select("a, span"))
            published_raw = time_link.get_text(" ", strip=True)
            items.append(RawItem(
                platform=self.platform,
                raw_data_ref=f"raw/weibo/{source_id or 'unknown'}.html",
                payload={
                    "sourceItemId": source_id,
                    "sourceType": "social_media",
                    "contentType": "post",
                    "title": title,
                    "content": content,
                    "url": url,
                    "author": {"name": author_node.get_text(" ", strip=True) if author_node else None},
                    "metrics": {
                        "likeCount": _metric(action_text, "赞"),
                        "commentCount": _metric(action_text, "评论"),
                        "shareCount": _metric(action_text, "转发"),
                    },
                    "method": "http",
                    "adapterVersion": self.adapter_version,
                    "hasFullContent": False,
                    "publishedAtConfidence": 0.25 if published_raw else 0.0,
                    "warnings": ["search_result_content_may_be_truncated", "published_at_relative_or_unparsed"],
                    "ext": {"publishedAtRaw": published_raw},
                },
            ))
        return AdapterResult(items=items, status=SourceStatus.SUCCESS if items else SourceStatus.EMPTY, response_metadata=metadata)


def _post_id(url: str) -> str | None:
    match = re.search(r"weibo\.com/\d+/([\w-]+)", url)
    return match.group(1) if match else None


def _metric(text: str, label: str) -> int | None:
    match = re.search(rf"{label}\s*([\d.]+)\s*(万|亿)?", text)
    if not match:
        return None
    value = float(match.group(1))
    multiplier = {"万": 10_000, "亿": 100_000_000}.get(match.group(2), 1)
    return int(value * multiplier)
