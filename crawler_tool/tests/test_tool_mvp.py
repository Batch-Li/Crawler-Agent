from fastapi.testclient import TestClient

from crawler_tool.application import CrawlService
from crawler_tool.interfaces import create_app
from crawler_tool.sources import AdapterResult, SourceAdapter, SourceRegistry
from crawler_tool.sources.south_weekend import SouthWeekendAdapter
from crawler_tool.sources.toutiao import ToutiaoAdapter


class FixtureSouthWeekendAdapter(SouthWeekendAdapter):
    def search(self, request):
        page = open("tests/fixtures/south_weekend_search_success.html", encoding="utf-8").read()
        return self.parse_html(page)


class FixtureToutiaoAdapter(ToutiaoAdapter):
    def search(self, request):
        page = open("tests/fixtures/toutiao_search.html", encoding="utf-8").read()
        return self.parse_html(page)


class FixtureToutiaoFailingAdapter(ToutiaoAdapter):
    def search(self, request):
        from crawler_tool.sources import AdapterResult
        from crawler_tool.domain import SourceErrorCode, SourceStatus

        return AdapterResult(
            status=SourceStatus.RATE_LIMITED,
            error_code=SourceErrorCode.RATE_LIMITED,
            message="fixture rate limit",
            retryable=True,
        )


def test_search_http_returns_toutiao_content_items_and_source_report():
    app = create_app(lambda: CrawlService(SourceRegistry({"toutiao": FixtureToutiaoAdapter})))
    response = TestClient(app).post("/api/v1/tool/search-content", json={
        "query": "AI客服",
        "platforms": ["toutiao"],
        "freshness": "prefer_fresh",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["items"][0]["platform"] == "toutiao"
    assert body["items"][0]["canonicalUrl"] == "https://example.com/news/123456"
    assert body["items"][0]["ext"]["sourceName"] == "科技媒体"
    assert body["sourceReports"][0]["platform"] == "toutiao"
    assert body["sourceReports"][0]["status"] == "success"


def test_search_http_supports_south_weekend_and_toutiao_with_partial_report():
    app = create_app(lambda: CrawlService(SourceRegistry({
        "south_weekend": FixtureSouthWeekendAdapter,
        "toutiao": FixtureToutiaoFailingAdapter,
    })))
    response = TestClient(app).post("/api/v1/tool/search-content", json={
        "query": "AI客服",
        "platforms": ["south_weekend", "toutiao"],
        "limit": 5,
        "freshness": "prefer_fresh",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["partial"] is True
    assert len(body["items"]) == 3
    assert [report["platform"] for report in body["sourceReports"]] == ["south_weekend", "toutiao"]
    assert body["sourceReports"][0]["status"] == "success"
    assert body["sourceReports"][1]["status"] == "rate_limited"
    assert body["sourceReports"][1]["error_code"] == "RATE_LIMITED"
    assert body["sourceReports"][1]["retryable"] is True


def test_search_http_returns_content_items_and_source_report():
    app = create_app(lambda: CrawlService(SourceRegistry({"south_weekend": FixtureSouthWeekendAdapter})))
    response = TestClient(app).post("/api/v1/tool/search-content", json={
        "query": "AI客服",
        "platforms": ["south_weekend"],
        "limit": 2,
        "freshness": "prefer_fresh",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert len(body["items"]) == 2
    assert body["items"][0]["platform"] == "south_weekend"
    assert body["items"][0]["canonicalUrl"] == "https://www.infzm.com/contents/317022"
    assert body["sourceReports"] == [{
        "platform": "south_weekend",
        "status": "success",
        "count": 3,
        "cached": False,
        "error_code": None,
        "retryable": None,
        "message": None,
        "diagnostics": None,
    }]
