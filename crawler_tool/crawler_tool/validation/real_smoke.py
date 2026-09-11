from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from crawler_tool.domain import ContentSearchRequest, SourceStatus
from crawler_tool.pagination import PageRequest
from crawler_tool.sources.south_weekend import SouthWeekendAdapter
from crawler_tool.sources.toutiao import ToutiaoAdapter


class LiveValidationError(ValueError):
    pass


class RequestBudgetExceeded(LiveValidationError):
    pass


@dataclass(frozen=True)
class ValidationConfig:
    query: str
    allow_network: bool = False
    confirm_real: bool = False
    timeout_seconds: float = 10.0


class GuardedGet:
    def __init__(self, client: Any, *, maximum_requests: int = 2) -> None:
        self.client = client
        self.maximum_requests = maximum_requests
        self.count = 0
        self.pages: list[int | None] = []
        self.responses: list[Any] = []

    def __call__(self, url: str, **kwargs: Any) -> Any:
        if self.count >= self.maximum_requests:
            raise RequestBudgetExceeded("request budget exceeded")
        if kwargs.get("headers", {}).get("Cookie") or kwargs.get("cookies"):
            raise LiveValidationError("credentials are not allowed")
        self.count += 1
        params = kwargs.get("params", {})
        self.pages.append(params["page_num"] if "page_num" in params else params.get("page"))
        response = self.client.get(url, **kwargs)
        self.responses.append(response)
        return response


class GuardedPost:
    def __init__(self, client: Any, *, maximum_requests: int = 1) -> None:
        self.client = client
        self.maximum_requests = maximum_requests
        self.count = 0
        self.responses: list[Any] = []

    def __call__(self, url: str, **kwargs: Any) -> Any:
        if self.count >= self.maximum_requests:
            raise RequestBudgetExceeded("request budget exceeded")
        if "Cookie" in kwargs.get("headers", {}) or kwargs.get("cookies"):
            raise LiveValidationError("credentials are not allowed")
        self.count += 1
        response = self.client.post(url, **kwargs)
        self.responses.append(response)
        return response


def require_live_validation(config: ValidationConfig) -> None:
    if not config.allow_network or not config.confirm_real:
        raise LiveValidationError("real validation requires explicit double confirmation")


def run_toutiao_two_page_smoke(config: ValidationConfig, *, client_factory: Callable[..., Any] = httpx.Client) -> tuple[dict[str, Any], int]:
    require_live_validation(config)
    with client_factory(timeout=config.timeout_seconds, verify=True, follow_redirects=False, trust_env=False) as client:
        guarded = GuardedGet(client)
        adapter = ToutiaoAdapter(http_get=guarded)
        request = ContentSearchRequest(query=config.query, platforms=["toutiao"], limit=10, freshness="prefer_fresh")
        first = adapter.search(request)
        if first.status is not SourceStatus.SUCCESS or not first.items or first.next_cursor != "1":
            return _report("toutiao_two_page_smoke", "failed", guarded, [first]), 4
        second = adapter.search_page(request, PageRequest("1"))
        if second.status is not SourceStatus.SUCCESS or not second.items:
            return _report("toutiao_two_page_smoke", "failed", guarded, [first, second]), 3
        passed = guarded.count == 2 and guarded.pages == [0, 1] and _item_ids(first.items) != _item_ids(second.items)
        return _report("toutiao_two_page_smoke", "passed" if passed else "failed", guarded, [first, second]), 0 if passed else 4


def run_south_weekend_pagination_evidence(config: ValidationConfig, *, client_factory: Callable[..., Any] = httpx.Client) -> tuple[dict[str, Any], int]:
    require_live_validation(config)
    with client_factory(timeout=config.timeout_seconds, verify=True, follow_redirects=False, trust_env=False) as client:
        guarded = GuardedGet(client)
        adapter = SouthWeekendAdapter(http_get=guarded)
        request = ContentSearchRequest(query=config.query, platforms=["south_weekend"], limit=10, freshness="prefer_fresh")
        first = adapter.search(request)
        if first.status is not SourceStatus.SUCCESS or not first.items:
            return _report("south_weekend_pagination_evidence", "indeterminate", guarded, [first]), 3
        second = adapter.search_page(request, PageRequest("2"))
        if second.status is not SourceStatus.SUCCESS or not second.items:
            return _report("south_weekend_pagination_evidence", "evidence_inconclusive", guarded, [first, second]), 4
        passed = guarded.count == 2 and guarded.pages == [None, 2] and _item_ids(first.items) != _item_ids(second.items)
        return _report("south_weekend_pagination_evidence", "evidence_confirmed" if passed else "evidence_inconclusive", guarded, [first, second]), 0 if passed else 4


def run_weibo_anonymous_smoke(config: ValidationConfig, *, client_factory: Callable[..., Any] = httpx.Client) -> tuple[dict[str, Any], int]:
    require_live_validation(config)
    from crawler_tool.sources.weibo import WeiboAdapter
    with client_factory(timeout=config.timeout_seconds, verify=True, follow_redirects=False, trust_env=False) as client:
        guarded = GuardedGet(client, maximum_requests=1)
        adapter = WeiboAdapter(http_get=guarded)
        result = adapter.search(ContentSearchRequest(query=config.query, platforms=["weibo"], limit=10, freshness="prefer_fresh"))
        diagnosis = adapter.diagnose_html(guarded.responses[0].text).report() if guarded.responses else {}
        return {"verification": "weibo_anonymous_first_page", "outcome": "observed", "requestCount": guarded.count, "status": result.status.value, "itemCount": len(result.items), "itemIdHashes": sorted(_hash(v) for v in _item_ids(result.items)), "diagnostics": diagnosis}, 0


def run_xiaohongshu_first_page_smoke(config: ValidationConfig, *, client_factory: Callable[..., Any] = httpx.Client) -> tuple[dict[str, Any], int]:
    require_live_validation(config)
    from crawler_tool.sources.xiaohongshu import XiaohongshuAdapter
    with client_factory(timeout=config.timeout_seconds, verify=True, follow_redirects=False, trust_env=False) as client:
        guarded = GuardedPost(client)
        result = XiaohongshuAdapter(http_post=guarded, mode="anonymous").probe_first_page(ContentSearchRequest(query=config.query, platforms=["xiaohongshu"], limit=10, freshness="prefer_fresh"))
        outcome = "observed" if result.status in {SourceStatus.SUCCESS, SourceStatus.EMPTY} else "authentication_required" if result.status is SourceStatus.AUTHENTICATION_REQUIRED else "rate_limited" if result.status is SourceStatus.RATE_LIMITED else "parser_changed" if result.status is SourceStatus.PARSER_CHANGED else "failed"
        code = 0 if result.status in {SourceStatus.SUCCESS, SourceStatus.EMPTY} else 3
        return {"verification": "xiaohongshu_anonymous_first_page", "outcome": outcome, "requestCount": guarded.count, "status": result.status.value, "itemCount": len(result.items), "itemIdHashes": sorted(_hash(v) for v in _item_ids(result.items)), "responseType": "json"}, code


def _report(name: str, outcome: str, guarded: GuardedGet, results: list[Any]) -> dict[str, Any]:
    return {"verification": name, "outcome": outcome, "requestCount": guarded.count, "pages": [{"page": guarded.pages[i] if i < len(guarded.pages) else None, "status": r.status.value, "httpStatus": getattr(guarded.responses[i], "status_code", None) if i < len(guarded.responses) else None, "itemCount": len(r.items), "itemIdHashes": sorted(_hash(v) for v in _item_ids(r.items)), "nextPageObserved": bool(r.next_cursor)} for i, r in enumerate(results)]}


def _item_ids(items: list[Any]) -> set[str]:
    return {str(item.payload.get("sourceItemId") or item.payload.get("url")) for item in items if item.payload.get("sourceItemId") or item.payload.get("url")}


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]
