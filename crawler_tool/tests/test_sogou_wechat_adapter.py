import pathlib

import pytest

from crawler_tool.application import CrawlService
from crawler_tool.domain import ContentSearchRequest
from crawler_tool.interfaces.app import make_service
from crawler_tool.sources import SourceRegistry
from crawler_tool.sources.sogou_wechat import BASE_URL, SogouWechatAdapter


ROOT = pathlib.Path(__file__).parent / "fixtures"


def _fixture_html() -> str:
    return (ROOT / "sogou_wechat_search_success.html").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text="", status_code=200, content_type="text/html; charset=utf-8"):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _search_request(query="人工智能"):
    return ContentSearchRequest(query=query, platforms=["sogou_wechat"], limit=10)


# --- fixture parser ---

def test_fixture_parses_ten_discovery_results():
    result = SogouWechatAdapter(mode="anonymous_best_effort").parse_html(_fixture_html())

    assert result.status.value == "success"
    assert len(result.items) == 10
    first = result.items[0].payload
    assert first["sourceType"] == "news_media"
    assert first["contentType"] == "article"
    assert first["title"]
    assert first["url"].startswith("https://weixin.sogou.com/link?url=")
    assert first["publishedAt"].startswith("2026-")
    assert first["publishedAtConfidence"] == 1.0
    assert first["method"] == "http"
    assert first["author"]["name"]
    assert "article_url_is_sogou_wrapped" in first["warnings"]
    assert "synthetic_source_id" in first["warnings"]
    assert "search_result_summary_only" in first["warnings"]
    assert first["sourceItemId"].startswith("sg_")


def test_source_ids_are_stable_across_parses():
    adapter = SogouWechatAdapter(mode="anonymous_best_effort")
    first = adapter.parse_html(_fixture_html())
    second = adapter.parse_html(_fixture_html())

    ids_first = [item.payload["sourceItemId"] for item in first.items]
    ids_second = [item.payload["sourceItemId"] for item in second.items]
    assert ids_first == ids_second
    assert len(set(ids_first)) == len(ids_first)


def test_parse_real_page_reports_diagnosis_metadata():
    result = SogouWechatAdapter(mode="anonymous_best_effort").parse_html(_fixture_html())

    diagnosis = result.response_metadata["pageDiagnosis"]
    assert diagnosis["classification"] == "success"
    assert diagnosis["resultBlockCount"] == 10
    assert diagnosis["titleAnchorCount"] == 10
    assert diagnosis["timeScriptCount"] == 10
    assert diagnosis["rateLimitSignal"] is False


# --- 失败分类：验证码即停、空结果、结构变化 ---

def test_challenge_page_is_rate_limited_without_retry():
    page = "<html><body><div id='wrapper'>请输入验证码 antispider</div></body></html>"

    result = SogouWechatAdapter(mode="anonymous_best_effort").parse_html(page)

    assert result.status.value == "rate_limited"
    assert result.error_code.value == "RATE_LIMITED"
    assert result.retryable is False
    assert result.response_metadata["pageDiagnosis"]["rateLimitSignal"] is True


def test_empty_result_page_is_empty_not_error():
    page = '<html><body><div id="wrapper"><p>抱歉，没有找到相关微信文章</p></div></body></html>'

    result = SogouWechatAdapter(mode="anonymous_best_effort").parse_html(page)

    assert result.status.value == "empty"
    assert result.items == []


def test_unrecognized_page_is_parser_changed_not_empty():
    result = SogouWechatAdapter(mode="anonymous_best_effort").parse_html("<html><body>hello</body></html>")

    assert result.status.value == "parser_changed"
    assert result.error_code.value == "PARSER_CHANGED"


# --- fake transport ---

def test_disabled_mode_makes_zero_network_requests():
    transport = FakeTransport(FakeResponse("<html></html>"))
    adapter = SogouWechatAdapter(http_get=transport, mode="disabled")

    result = adapter.search(_search_request())

    assert result.status.value == "source_unavailable"
    assert transport.calls == []


def test_search_success_with_fake_transport():
    transport = FakeTransport(FakeResponse(_fixture_html()))
    adapter = SogouWechatAdapter(http_get=transport, mode="anonymous_best_effort")

    result = adapter.search(_search_request())

    assert result.status.value == "success"
    assert len(result.items) == 10
    url, kwargs = transport.calls[0]
    assert url == f"{BASE_URL}/weixin"
    assert kwargs["params"] == {"type": 2, "query": "人工智能"}


def test_search_http_429_is_rate_limited_single_call():
    transport = FakeTransport(FakeResponse(status_code=429))
    adapter = SogouWechatAdapter(http_get=transport, mode="anonymous_best_effort")

    result = adapter.search(_search_request())

    assert result.status.value == "rate_limited"
    assert result.retryable is False
    assert len(transport.calls) == 1


def test_search_non_html_response_is_rejected():
    transport = FakeTransport(FakeResponse("{}", content_type="application/json"))
    adapter = SogouWechatAdapter(http_get=transport, mode="anonymous_best_effort")

    result = adapter.search(_search_request())

    assert result.status.value == "network_error"
    assert result.response_metadata["contentTypeClass"] == "non_html"


# --- 注册与装配 ---

def test_registry_defaults_exclude_sogou_and_health_reports_mode():
    registry = SourceRegistry(
        {"sogou_wechat": lambda: SogouWechatAdapter(mode="disabled")},
        default_platforms=("south_weekend", "toutiao"),
    )

    assert registry.defaults() == []
    adapter = registry.create("sogou_wechat")
    try:
        health = adapter.health()
    finally:
        adapter.close()
    assert health["mode"] == "disabled"
    assert health["authMode"] == "disabled"


def test_service_explicit_query_on_disabled_sogou_is_clean_unavailable():
    service = CrawlService(SourceRegistry({"sogou_wechat": lambda: SogouWechatAdapter(mode="disabled")}))

    response = service.search(_search_request())

    assert response.status == "failed"
    report = response.source_reports[0]
    assert report.platform == "sogou_wechat"
    assert report.status == "source_unavailable"
    assert "SOGOU_WECHAT_MODE" in report.message


def test_make_service_wires_sogou_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SOGOU_WECHAT_MODE", raising=False)
    service = make_service()

    assert "sogou_wechat" not in service.registry.defaults()
    reports = {report["platform"]: report for report in service.registry.health()}
    assert reports["sogou_wechat"]["mode"] == "disabled"


def test_make_service_wires_sogou_best_effort_mode(monkeypatch):
    monkeypatch.setenv("SOGOU_WECHAT_MODE", "anonymous_best_effort")
    service = make_service()

    assert "sogou_wechat" not in service.registry.defaults()
    adapter = service.registry.create("sogou_wechat")
    try:
        assert adapter.health()["mode"] == "anonymous_best_effort"
    finally:
        adapter.close()


def test_make_service_rejects_unknown_sogou_mode(monkeypatch):
    monkeypatch.setenv("SOGOU_WECHAT_MODE", "turbo")

    service = make_service()

    adapter = service.registry.create("sogou_wechat")
    try:
        assert adapter.health()["mode"] == "disabled"
    finally:
        adapter.close()


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch):
    # make_service 在构造时读取环境变量；固定默认值保证测试互不影响。
    monkeypatch.setenv("SOGOU_WECHAT_MODE", "disabled")
    yield
