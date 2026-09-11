from fastapi.testclient import TestClient

from crawler_tool.interfaces import create_app
from crawler_tool.interfaces.app import make_service


def _set_env(monkeypatch, *, mode=None, cookie=None):
    if mode is None:
        monkeypatch.delenv("XHS_MODE", raising=False)
    else:
        monkeypatch.setenv("XHS_MODE", mode)
    if cookie is None:
        monkeypatch.delenv("XHS_SESSION_COOKIE", raising=False)
    else:
        monkeypatch.setenv("XHS_SESSION_COOKIE", cookie)


def _health_by_platform(service):
    return {entry["platform"]: entry for entry in service.registry.health()}


def test_xiaohongshu_is_registered_but_never_a_default_platform(monkeypatch):
    _set_env(monkeypatch)
    service = make_service()

    assert "xiaohongshu" in service.registry.factories
    assert service.registry.defaults() == ["south_weekend", "toutiao"]


def test_xiaohongshu_health_follows_mode_configuration(monkeypatch):
    _set_env(monkeypatch)
    entry = _health_by_platform(make_service())["xiaohongshu"]
    assert entry["authMode"] == "anonymous"
    assert entry["credentialStatus"] == "not_required"
    assert entry["capabilities"] == []

    _set_env(monkeypatch, mode="authorized_first_page")
    entry = _health_by_platform(make_service())["xiaohongshu"]
    assert entry["authMode"] == "authorized_first_page"
    assert entry["credentialStatus"] == "unavailable"

    # 只断言配置状态；携带真实会话能力时不触发任何网络请求
    _set_env(monkeypatch, mode="authorized_first_page", cookie="web_session=fake-secret")
    entry = _health_by_platform(make_service())["xiaohongshu"]
    assert entry["credentialStatus"] == "configured"
    assert entry["capabilities"] == ["search"]


def test_api_explicit_xhs_query_declines_cleanly_when_disabled(monkeypatch):
    _set_env(monkeypatch)
    app = create_app(lambda: make_service())
    response = TestClient(app).post("/api/v1/tool/search-content", json={
        "query": "人工智能",
        "platforms": ["xiaohongshu"],
        "limit": 5,
        "freshness": "prefer_fresh",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["items"] == []
    assert len(body["sourceReports"]) == 1
    report = body["sourceReports"][0]
    assert report["platform"] == "xiaohongshu"
    assert report["status"] == "source_unavailable"
    assert report["error_code"] == "SOURCE_UNAVAILABLE"
    assert report["retryable"] is False


def test_api_default_query_keeps_two_source_contract(monkeypatch):
    _set_env(monkeypatch)
    app = create_app(lambda: make_service())
    response = TestClient(app).post("/api/v1/tool/search-content", json={
        "query": "人工智能", "freshness": "cache_only",
    })

    assert response.status_code == 200
    body = response.json()
    assert [report["platform"] for report in body["sourceReports"]] == ["south_weekend", "toutiao"]


def test_composition_shares_one_session_view_across_paths(monkeypatch):
    _set_env(monkeypatch)
    service = make_service()

    assert service.capture_ingest_service.store is service.recent_store
    assert service.wechat_search_service.store is service.recent_store
