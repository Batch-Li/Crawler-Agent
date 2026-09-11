from fastapi.testclient import TestClient

from crawler_tool.application import CrawlService
from crawler_tool.interfaces import create_app
from crawler_tool.sources import SourceRegistry, SouthWeekendAdapter, ToutiaoAdapter, WeiboAdapter


def test_source_health_route():
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    response = TestClient(app).get("/api/v1/tool/source-health")
    assert response.status_code == 200
    assert response.json() == {"sources": []}


def test_source_health_reports_registered_real_sources():
    app = create_app(lambda: CrawlService(SourceRegistry({
        "south_weekend": SouthWeekendAdapter,
        "toutiao": ToutiaoAdapter,
        "weibo": WeiboAdapter,
    })))
    response = TestClient(app).get("/api/v1/tool/source-health")

    assert response.status_code == 200
    assert response.json() == {"sources": [
        {"platform": "south_weekend", "status": "unknown", "adapter_version": "0.2.0-ssr", "capabilities": ["search"], "pagination": "unverified"},
        {"platform": "toutiao", "status": "unknown", "adapter_version": "0.2.0", "capabilities": ["search", "pagination"], "pagination": "fixture_verified"},
        {"platform": "weibo", "status": "unknown", "adapter_version": "0.2.0-anonymous", "capabilities": ["search"], "pagination": "disabled", "authMode": "anonymous_best_effort"},
    ]}
