from fastapi.testclient import TestClient

from crawler_tool.application import CrawlService
from crawler_tool.interfaces import create_app
from crawler_tool.pagination import CursorCodec
from crawler_tool.sources import SourceRegistry, ToutiaoAdapter


def test_health_and_search_http_contract():
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    client = TestClient(app)

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    response = client.post("/api/v1/tool/search-content", json={"query": "AI客服"})
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["items"] == []




def test_toutiao_pagination_http_round_trip_and_cursor_errors():
    pages = {
        0: open("tests/fixtures/toutiao_search_page_0.html", encoding="utf-8").read(),
        1: open("tests/fixtures/toutiao_search_page_1.html", encoding="utf-8").read(),
    }
    calls = []

    class FixtureResponse:
        status_code = 200
        headers = {"content-type": "text/html"}

        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    def http_get(*args, **kwargs):
        page_number = kwargs["params"]["page_num"]
        calls.append(page_number)
        return FixtureResponse(pages[page_number])

    app = create_app(lambda: CrawlService(
        SourceRegistry({"toutiao": lambda: ToutiaoAdapter(http_get=http_get)}),
        cursor_codec=CursorCodec("test-secret", clock=lambda: 100),
    ))
    client = TestClient(app)
    first = client.post("/api/v1/tool/search-content", json={
        "query": "AI客服", "platforms": ["toutiao"], "freshness": "prefer_fresh",
    })
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["nextCursor"]
    assert first_body["items"][0]["sourceItemId"] == "123456"

    second = client.post("/api/v1/tool/search-content", json={
        "query": "AI客服", "platforms": ["toutiao"], "cursor": first_body["nextCursor"], "freshness": "prefer_fresh",
    })
    assert second.status_code == 200
    assert second.json().get("nextCursor") is None
    assert second.json()["items"][0]["sourceItemId"] == "654321"
    assert calls == [0, 1]

    invalid = client.post("/api/v1/tool/search-content", json={
        "query": "AI客服", "platforms": ["toutiao"], "cursor": first_body["nextCursor"][:-1] + "A",
    })
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "INVALID_CURSOR"




def test_wechat_keyword_endpoint_is_disabled_by_default():
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    response = TestClient(app).post("/api/v1/tool/search-wechat-articles", json={
        "keyword": "AI", "accountNames": ["Demo Account"],
    })

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["sourceReports"][0]["platform"] == "wechat"
    assert body["sourceReports"][0]["status"] == "source_unavailable"


def test_wechat_keyword_endpoint_validates_account_names():
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    response = TestClient(app).post("/api/v1/tool/search-wechat-articles", json={
        "keyword": "AI", "accountNames": [],
    })

    assert response.status_code == 422
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    response = TestClient(app).post("/api/v1/tool/search-content", json={"limit": 1})
    assert response.status_code == 422


def test_ingest_xhs_capture_endpoint_parses_saved_response():
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    capture = {
        "code": 0,
        "data": {"items": [{
            "model_type": "note",
            "id": "ingest-001",
            "url": "https://www.xiaohongshu.com/explore/ingest-001",
            "note_card": {"display_title": "手工投喂示例"},
        }]},
    }
    response = TestClient(app).post("/api/v1/tool/ingest-xhs-capture", json=capture)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert len(body["items"]) == 1
    assert body["items"][0]["sourceItemId"] == "ingest-001"
    assert body["sourceReports"][0]["platform"] == "xiaohongshu"
    assert body["sourceReports"][0]["status"] == "success"


def test_ingest_capture_endpoint_routes_douyin_and_validates_platform():
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    capture = {
        "aweme_list": [{
            "aweme_id": "ingest-video-9",
            "desc": "多平台投喂示例",
            "create_time": 1724068800,
            "author": {"nickname": "作者"},
        }],
    }
    response = TestClient(app).post("/api/v1/tool/ingest-capture", json={
        "platform": "douyin", "keyword": "示例", "capture": capture,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert len(body["items"]) == 1
    assert body["items"][0]["platform"] == "douyin"
    assert body["items"][0]["contentType"] == "video"
    assert body["sourceReports"][0]["platform"] == "douyin"

    bad_platform = TestClient(app).post("/api/v1/tool/ingest-capture", json={
        "platform": "weibo", "capture": capture,
    })
    assert bad_platform.status_code == 422


def test_ingest_capture_endpoint_auto_detects_wechat_list_envelope():
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    capture = {
        "base_resp": {"ret": 0, "errmsg": "ok"},
        "app_msg_list": [{
            "aid": "ingest-wx-1",
            "title": "端点投喂示例文章",
            "link": "https://mp.weixin.qq.com/s?__biz=MzA0demo&mid=1&idx=1&sn=ingestwx",
            "author": "示例作者",
            "create_time": 1724068800,
        }],
    }
    response = TestClient(app).post("/api/v1/tool/ingest-capture", json={
        "keyword": "人工智能", "capture": capture,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert len(body["items"]) == 1
    assert body["items"][0]["platform"] == "wechat"
    assert body["sourceReports"][0]["platform"] == "wechat"


def test_ingest_xhs_capture_endpoint_rejects_invalid_capture():
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    response = TestClient(app).post("/api/v1/tool/ingest-xhs-capture", json={"capture": "not-an-object"})

    assert response.status_code == 422


def test_ingest_capture_cors_is_scoped_to_capture_sites_on_loopback():
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    client = TestClient(app)
    capture = {"aweme_list": []}

    preflight = client.options("/api/v1/tool/ingest-capture", headers={
        "Origin": "https://www.douyin.com",
        "Access-Control-Request-Method": "POST",
        "Host": "127.0.0.1:8301",
    })
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "https://www.douyin.com"

    posted = client.post("/api/v1/tool/ingest-capture", json={"platform": "douyin", "capture": capture}, headers={
        "Origin": "https://www.douyin.com",
        "Host": "127.0.0.1:8301",
    })
    assert posted.status_code == 200
    assert posted.headers["access-control-allow-origin"] == "https://www.douyin.com"

    # 其他来源与回环以外的主机一概不给跨域放行。
    foreign = client.options("/api/v1/tool/ingest-capture", headers={
        "Origin": "https://evil.example",
        "Host": "127.0.0.1:8301",
    })
    assert "access-control-allow-origin" not in foreign.headers

    rebound = client.post("/api/v1/tool/search-content", json={"query": "AI"}, headers={
        "Origin": "https://www.douyin.com",
        "Host": "attacker.example",
    })
    assert rebound.status_code == 200
    assert "access-control-allow-origin" not in rebound.headers


def test_captured_items_view_lists_recent_ingest_newest_first():
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    client = TestClient(app)
    douyin_capture = {"aweme_list": [{"aweme_id": "v1", "desc": "视频甲", "create_time": 1724068800}]}
    xhs_capture = {
        "code": 0,
        "data": {"items": [{
            "model_type": "note",
            "id": "n1",
            "url": "https://www.xiaohongshu.com/explore/n1",
            "note_card": {"display_title": "笔记乙"},
        }]},
    }
    assert client.post("/api/v1/tool/ingest-capture", json={"platform": "douyin", "keyword": "关键词", "capture": douyin_capture}).status_code == 200
    assert client.post("/api/v1/tool/ingest-capture", json=xhs_capture).status_code == 200

    view = client.get("/api/v1/tool/captured-items")
    assert view.status_code == 200
    body = view.json()
    assert [item["sourceItemId"] for item in body["items"]] == ["n1", "v1"]

    douyin_only = client.get("/api/v1/tool/captured-items", params={"platform": "douyin"}).json()
    assert [item["sourceItemId"] for item in douyin_only["items"]] == ["v1"]

    keyword_view = client.get("/api/v1/tool/captured-items", params={"keyword": "关键词"}).json()
    assert len(keyword_view["items"]) == 1
    assert keyword_view["items"][0]["platform"] == "douyin"

    capped = client.get("/api/v1/tool/captured-items", params={"limit": 1}).json()
    assert len(capped["items"]) == 1

    assert client.get("/api/v1/tool/captured-items", params={"limit": 500}).status_code == 422


def test_captured_items_view_page_and_exports():
    app = create_app(lambda: CrawlService(SourceRegistry({})))
    client = TestClient(app)
    assert client.post("/api/v1/tool/ingest-capture", json={
        "code": 0,
        "data": {"items": [{
            "model_type": "note",
            "id": "n9",
            "url": "https://www.xiaohongshu.com/explore/n9",
            "note_card": {"display_title": "导出标题样例"},
        }]},
    }).status_code == 200

    page = client.get("/view")
    assert page.status_code == 200
    assert "/api/v1/tool/captured-items" in page.text
    # 平台选择器覆盖全部已注册来源。
    for platform_option in ("sogou_wechat", "cctv_news"):
        assert f'value="{platform_option}"' in page.text
    # 新平台的过滤参数在 API 侧同样被接受（不再 422）。
    assert client.get("/api/v1/tool/captured-items", params={"platform": "cctv_news"}).status_code == 200
    assert client.get("/api/v1/tool/captured-items", params={"platform": "sogou_wechat"}).status_code == 200
    assert client.get("/api/v1/tool/captured-items", params={"platform": "not_a_platform"}).status_code == 422

    exported = client.get("/api/v1/tool/captured-items/export", params={"format": "csv"})
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert exported.text.startswith("﻿")
    first_data_row = exported.text.splitlines()[1]
    assert "导出标题样例" in first_data_row
    assert first_data_row.startswith("cnt_")

    as_json = client.get("/api/v1/tool/captured-items/export", params={"format": "json"}).json()
    assert as_json["count"] == 1
    assert as_json["items"][0]["sourceItemId"] == "n9"

    bad_format = client.get("/api/v1/tool/captured-items/export", params={"format": "xlsx"})
    assert bad_format.status_code == 422


def test_session_view_collects_network_search_results():
    from crawler_tool.sources import SouthWeekendAdapter

    class GoodAdapter(SouthWeekendAdapter):
        def search(self, request):
            return self.parse_payload({"data": {"list": [{
                "id": "s1", "subject": "南周样例", "introtext": "正文",
                "publish_time": "2026-08-23T10:20:00+08:00", "url": "1",
            }]}})

    service = CrawlService(SourceRegistry({"south_weekend": GoodAdapter}))
    client = TestClient(create_app(lambda: service))
    response = client.post("/api/v1/tool/search-content", json={
        "query": "AI客服", "platforms": ["south_weekend"], "freshness": "prefer_fresh",
    })
    assert response.status_code == 200

    view = client.get("/api/v1/tool/captured-items", params={"platform": "south_weekend"}).json()
    assert [item["sourceItemId"] for item in view["items"]] == ["s1"]
    assert view["items"][0]["platform"] == "south_weekend"

    page = client.get("/view")
    assert 'value="wechat"' in page.text
    assert 'value="douyin"' in page.text


def test_capture_queue_page_and_freshness_status():
    from datetime import datetime, timedelta, timezone

    from crawler_tool.application.recent_store import RecentItemsStore
    from crawler_tool.normalization.content_normalizer import normalize_raw_item
    from crawler_tool.sources import RawItem

    now = datetime.now(timezone.utc)
    store = RecentItemsStore()

    def seed(platform, item_id, keyword, age_days):
        store.add([normalize_raw_item(RawItem(platform=platform, payload={
            "sourceItemId": item_id,
            "sourceType": "social_media",
            "contentType": "post" if platform == "xiaohongshu" else "video",
            "title": f"{keyword}-{item_id}",
            "url": f"https://example.com/{item_id}",
        }), query=keyword, collected_at=now - timedelta(days=age_days))])

    seed("xiaohongshu", "old-1", "人工智能", age_days=10)
    seed("douyin", "new-1", "大模型备案", age_days=0)

    service = CrawlService(SourceRegistry({}), history=store)
    client = TestClient(create_app(lambda: service))

    page = client.get("/capture")
    assert page.status_code == 200
    assert "/api/v1/tool/capture-status" in page.text

    status = client.get("/api/v1/tool/capture-status", params={
        "keywords": "人工智能,大模型备案,全新词", "max_age_days": 3,
    }).json()

    by_word = {row["keyword"]: row for row in status["keywords"]}
    xhs = by_word["人工智能"]["platforms"]["xiaohongshu"]
    assert xhs["count"] == 1
    assert xhs["fresh"] is False  # 10 天前 → 过期
    assert by_word["人工智能"]["platforms"]["douyin"]["count"] == 0

    dy = by_word["大模型备案"]["platforms"]["douyin"]
    assert dy["count"] == 1
    assert dy["fresh"] is True

    unknown = by_word["全新词"]["platforms"]
    assert unknown["xiaohongshu"]["count"] == 0
    assert unknown["xiaohongshu"]["fresh"] is False

    assert client.get("/api/v1/tool/capture-status").status_code == 422


def test_source_health_includes_wechat_service_state():
    from crawler_tool.application.wechat_search_service import WechatKeywordSearchService

    service = CrawlService(SourceRegistry({}))
    service.wechat_search_service = WechatKeywordSearchService(
        lambda: None, enabled=True, credential_configured=True
    )
    body = TestClient(create_app(lambda: service)).get("/api/v1/tool/source-health").json()

    wechat = [entry for entry in body["sources"] if entry["platform"] == "wechat"]
    assert len(wechat) == 1
    assert wechat[0]["credentialStatus"] == "configured"
    assert wechat[0]["capabilities"] == ["search"]
