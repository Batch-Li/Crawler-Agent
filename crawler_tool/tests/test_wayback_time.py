"""Wayback CDX 兜底：解析、空记录、失败区分、请求内熔断、默认关闭。"""

from __future__ import annotations

from crawler_tool.application.time_enrichment import TimeEnrichmentService
from crawler_tool.infrastructure.wayback_time import WaybackUnavailable, earliest_capture
from crawler_tool.normalization import normalize_raw_item
from crawler_tool.domain import SourceStatus
from crawler_tool.sources import RawItem, SourceRegistry
from crawler_tool.sources.base import AdapterResult, SourceAdapter


class _Response:
    def __init__(self, payload, *, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_earliest_capture_parses_cdx_rows():
    calls = []

    def http_get(url, params, timeout):
        calls.append((url, params, timeout))
        return _Response([["timestamp"], ["20200515032145"]])

    iso = earliest_capture("https://example.com/a", http_get)
    assert iso == "2020-05-15T03:21:45+00:00"
    url, params, _ = calls[0]
    assert url == "https://web.archive.org/cdx/search/cdx"
    assert params["limit"] == 1 and params["filter"] == "statuscode:200"


def test_earliest_capture_empty_archive_is_none_not_error():
    assert earliest_capture("https://example.com/a", lambda url, params, timeout: _Response([])) is None
    assert earliest_capture("https://example.com/a", lambda url, params, timeout: _Response([["timestamp"]])) is None


def test_earliest_capture_network_failure_raises_unavailable():
    def http_get(url, params, timeout):
        raise TimeoutError("timeout")

    try:
        earliest_capture("https://example.com/a", http_get)
        raise AssertionError("应当抛出 WaybackUnavailable")
    except WaybackUnavailable:
        pass


def test_earliest_capture_http_error_and_bad_json_raise():
    try:
        earliest_capture("https://example.com/a", lambda url, params, timeout: _Response([], status_code=503))
        raise AssertionError("503 应当抛出 WaybackUnavailable")
    except WaybackUnavailable:
        pass
    try:
        earliest_capture("https://example.com/a", lambda url, params, timeout: _Response(ValueError("bad json")))
        raise AssertionError("坏 JSON 应当抛出 WaybackUnavailable")
    except WaybackUnavailable:
        pass


def _item(url: str):
    return normalize_raw_item(RawItem(platform="south_weekend", payload={
        "sourceItemId": url.rsplit("/", 1)[-1], "title": "缺时间", "summary": "摘要",
        "url": url, "method": "http", "warnings": ["search_result_summary_only"],
    }), query="测试")


class _NoopAdapter(SourceAdapter):
    platform = "south_weekend"
    adapter_version = "0.0.0-fake"

    def search(self, request):
        return AdapterResult(status=SourceStatus.EMPTY)


def _service(wayback_http_get):
    registry = SourceRegistry({"south_weekend": lambda: _NoopAdapter()})
    return TimeEnrichmentService(registry, wayback_http_get=wayback_http_get)


def test_wayback_fills_missing_time_with_low_confidence():
    def http_get(url, params, timeout):
        return _Response([["timestamp"], ["20210501000000"]])

    result, fetches = _service(http_get).enrich([_item("https://www.infzm.com/contents/5")])
    assert fetches == 0  # 适配器不支持详情（零网络）→ 不计请求；Wayback 直接兜底
    assert result[0].published_at is not None
    assert result[0].quality.published_at_confidence == 0.6
    assert "published_at_from_wayback" in result[0].quality.warnings
    assert "detail_fetch_failed" not in result[0].quality.warnings
    assert result[0].ext["waybackTimeFetched"] is True


def test_wayback_circuit_breaks_after_consecutive_failures():
    calls = []

    def http_get(url, params, timeout):
        calls.append(url)
        raise TimeoutError("timeout")

    items = [_item(f"https://www.infzm.com/contents/{i}") for i in range(5)]
    result, _ = _service(http_get).enrich(items)
    assert len(calls) == 2  # 连续失败 2 次即熔断
    assert all(item.published_at is None for item in result)


def test_wayback_skipped_when_disabled():
    items = [_item("https://www.infzm.com/contents/1")]
    result, _ = _service(None).enrich(items)
    assert result[0].published_at is None
    assert "published_at_from_wayback" not in result[0].quality.warnings
