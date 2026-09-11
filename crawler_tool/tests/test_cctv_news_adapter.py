import pathlib

import pytest

from crawler_tool.domain import ContentSearchRequest
from crawler_tool.interfaces.app import make_service
from crawler_tool.sources import SourceRegistry
from crawler_tool.sources.cctv_news import CctvNewsAdapter


ROOT = pathlib.Path(__file__).parent / "fixtures"


def _fixture_text() -> str:
    return (ROOT / "cctv_news_china_page1.jsonp").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": "application/javascript"}


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _search_request(query="人工智能"):
    return ContentSearchRequest(query=query, platforms=["cctv_news"], limit=10)


# --- fixture parser ---

def test_fixture_parses_eighty_records_with_full_fields():
    result = CctvNewsAdapter().parse_jsonp_and_map(_fixture_text())

    assert result.status.value == "success"
    assert len(result.items) == 80
    first = result.items[0].payload
    assert first["sourceItemId"].startswith("ARTI")
    assert first["title"]
    assert first["summary"]
    assert first["url"].startswith("https://news.cctv.com/2026/09/03/")
    # focus_date 19:59:34 北京时间 → UTC ISO
    assert first["publishedAt"] == "2026-09-03T11:59:34+00:00"
    assert first["publishedAtConfidence"] == 1.0
    assert first["sourceType"] == "news_media"
    assert first["contentType"] == "article"
    assert first["method"] == "http"
    assert first["media"] and first["media"][0]["type"] == "image"
    assert first["ext"]["keywords"]
    assert first["ext"]["cctvColumn"] == "china"
    assert "search_result_summary_only" in first["warnings"]


def test_source_ids_are_stable_across_parses():
    first = CctvNewsAdapter().parse_jsonp_and_map(_fixture_text())
    second = CctvNewsAdapter().parse_jsonp_and_map(_fixture_text())

    ids_first = [item.payload["sourceItemId"] for item in first.items]
    ids_second = [item.payload["sourceItemId"] for item in second.items]
    assert ids_first == ids_second
    assert len(set(ids_first)) == len(ids_first)


def test_jsonp_callback_name_does_not_matter():
    payload = 'anything_cb({"data": {"total": 1, "list": [{"id": "ARTIx1", "title": "标题", "url": "https://news.cctv.com/2026/09/03/ARTIx1.shtml", "focus_date": "2026-09-03 08:00:00", "brief": "摘要"}]}})'

    result = CctvNewsAdapter().parse_jsonp_and_map(payload)

    assert result.status.value == "success"
    assert result.items[0].payload["publishedAt"] == "2026-09-03T00:00:00+00:00"


def test_empty_list_maps_to_empty_not_error():
    payload = 'china({"data": {"total": 0, "list": []}})'

    result = CctvNewsAdapter().parse_jsonp_and_map(payload)

    assert result.status.value == "empty"
    assert result.items == []


def test_missing_list_is_parser_changed():
    payload = 'china({"data": {"rows": []}})'

    result = CctvNewsAdapter().parse_jsonp_and_map(payload)

    assert result.status.value == "parser_changed"
    assert result.error_code.value == "PARSER_CHANGED"


def test_records_without_title_or_url_are_skipped():
    payload = 'china({"data": {"total": 2, "list": [{"id": "ARTIa"}, {"id": "ARTIb", "title": "有标题", "url": "https://news.cctv.com/2026/09/03/ARTIb.shtml", "focus_date": "2026-09-03 08:00:00"}]}})'

    result = CctvNewsAdapter().parse_jsonp_and_map(payload)

    assert result.status.value == "success"
    assert len(result.items) == 1
    assert result.items[0].payload["sourceItemId"] == "ARTIb"


# --- fake transport ---

def test_search_success_with_fake_transport_filters_by_keyword():
    transport = FakeTransport(FakeResponse(_fixture_text()))
    adapter = CctvNewsAdapter(http_get=transport)

    # "上海" 在 fixture 的 80 条里经标题/摘要/关键词命中 5 条。
    result = adapter.search(_search_request("上海"))

    assert result.status.value == "success"
    assert len(result.items) == 5
    assert result.response_metadata["keywordFiltered"]["candidates"] == 80
    assert result.response_metadata["keywordFiltered"]["matched"] == 5
    url, _ = transport.calls[0]
    assert url.endswith("/2019/07/gaiban/cmsdatainterface/page/china_1.jsonp")


def test_search_without_keyword_match_is_empty_with_reason_not_fake_results():
    transport = FakeTransport(FakeResponse(_fixture_text()))
    adapter = CctvNewsAdapter(http_get=transport)

    # fixture 最新列表里没有"人工智能"相关条目：必须如实 empty，不能把无关列表当结果。
    result = adapter.search(_search_request("人工智能"))

    assert result.status.value == "empty"
    assert result.items == []
    assert "没有匹配关键词" in (result.message or "")
    assert result.response_metadata["keywordFiltered"]["matched"] == 0


def test_search_multi_token_query_matches_any_token():
    transport = FakeTransport(FakeResponse(_fixture_text()))
    adapter = CctvNewsAdapter(http_get=transport)

    # "青岛" 2 条、"洪水" 2 条（标题口径），摘要/关键词口径合计 4 条，任一命中即保留。
    result = adapter.search(_search_request("青岛 洪水"))

    assert result.status.value == "success"
    assert 0 < len(result.items) < 80


def test_unknown_column_falls_back_to_china():
    transport = FakeTransport(FakeResponse(_fixture_text()))
    adapter = CctvNewsAdapter(http_get=transport, column="not-a-column")

    adapter.search(_search_request())

    assert transport.calls[0][0].endswith("/page/china_1.jsonp")


def test_search_http_429_is_rate_limited_single_call():
    transport = FakeTransport(FakeResponse(status_code=429))
    adapter = CctvNewsAdapter(http_get=transport)

    result = adapter.search(_search_request())

    assert result.status.value == "rate_limited"
    assert len(transport.calls) == 1


def test_search_http_500_is_network_error():
    transport = FakeTransport(FakeResponse(status_code=500))
    adapter = CctvNewsAdapter(http_get=transport)

    result = adapter.search(_search_request())

    assert result.status.value == "network_error"
    assert result.retryable is True


def test_non_jsonp_body_is_parser_changed():
    transport = FakeTransport(FakeResponse("<html>error page</html>"))
    adapter = CctvNewsAdapter(http_get=transport)

    result = adapter.search(_search_request())

    assert result.status.value == "parser_changed"
    assert result.response_metadata["contentTypeClass"] == "non_jsonp"


def test_unconfigured_transport_is_clean_unavailable():
    adapter = CctvNewsAdapter(http_get=None)

    result = adapter.search(_search_request())

    assert result.status.value == "source_unavailable"


# --- 注册与装配 ---

def test_registry_defaults_exclude_cctv_and_health_reports_column():
    registry = SourceRegistry(
        {
            "south_weekend": lambda: CctvNewsAdapter(column="china"),
            "toutiao": lambda: CctvNewsAdapter(column="china"),
            "cctv_news": lambda: CctvNewsAdapter(),
        },
        default_platforms=("south_weekend", "toutiao"),
    )

    assert "cctv_news" not in registry.defaults()
    assert registry.defaults() == ["south_weekend", "toutiao"]
    adapter = registry.create("cctv_news")
    try:
        health = adapter.health()
    finally:
        adapter.close()
    assert health["authMode"] == "anonymous"
    assert health["column"] == "china"


def test_make_service_wires_cctv_as_explicit_only(monkeypatch):
    monkeypatch.delenv("SOGOU_WECHAT_MODE", raising=False)
    service = make_service()

    assert "cctv_news" not in service.registry.defaults()
    assert "south_weekend" in service.registry.defaults()
    reports = {report["platform"]: report for report in service.registry.health()}
    assert "cctv_news" in reports
    assert reports["cctv_news"]["adapter_version"] == CctvNewsAdapter.adapter_version


@pytest.fixture(autouse=True)
def _stable_env(monkeypatch):
    monkeypatch.setenv("SOGOU_WECHAT_MODE", "disabled")
    yield
