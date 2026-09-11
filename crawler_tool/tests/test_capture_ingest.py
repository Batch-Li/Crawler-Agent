import copy
import json
import pathlib

from crawler_tool.application.capture_ingest_service import (
    CaptureIngestService,
    detect_capture_platform,
)
from crawler_tool.domain import CaptureIngestRequest
from crawler_tool.sources.douyin import DouyinAdapter


ROOT = pathlib.Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def _ingest(payload, keyword=None, platform=None):
    return CaptureIngestService().ingest(CaptureIngestRequest(keyword=keyword, platform=platform, capture=payload))


# --- request model ---

def test_bare_capture_body_is_wrapped_by_request_model():
    payload = _load("xiaohongshu_search_success.json")
    request = CaptureIngestRequest.model_validate(payload)

    assert request.capture == payload
    assert request.platform is None

    wrapped = CaptureIngestRequest.model_validate({"keyword": "人工智能", "capture": payload})
    assert wrapped.keyword == "人工智能"
    assert wrapped.capture == payload


def test_platform_envelope_detection():
    assert detect_capture_platform(_load("xiaohongshu_search_success.json")) == "xiaohongshu"
    assert detect_capture_platform(_load("douyin_search_success.json")) == "douyin"
    assert detect_capture_platform({"aweme_list": []}) == "douyin"
    assert detect_capture_platform({"data": {"aweme_details": []}}) == "douyin"
    assert detect_capture_platform({"hello": "world"}) is None


# --- xiaohongshu path ---

def test_ingest_maps_xiaohongshu_fixture_to_content_items():
    response = _ingest(_load("xiaohongshu_search_success.json"), keyword="人工智能")

    assert response.status == "success"
    assert response.partial is False
    assert len(response.items) == 1
    item = response.items[0]
    assert item.platform == "xiaohongshu"
    assert str(item.url).endswith("/explore/note-001")
    assert item.collection.method == "http"
    assert item.quality.has_full_content is False
    assert item.ext["captureIngest"] == {"origin": "manual_capture", "platform": "xiaohongshu", "keyword": "人工智能"}
    report = response.source_reports[0]
    assert report.platform == "xiaohongshu"
    assert report.status == "success"
    assert report.count == 1


def test_ingest_dedups_repeated_captures_of_the_same_note():
    payload = _load("xiaohongshu_search_success.json")
    payload["data"]["items"].append(copy.deepcopy(payload["data"]["items"][0]))

    response = _ingest(payload)

    assert response.status == "success"
    assert len(response.items) == 1
    assert response.source_reports[0].count == 1


def test_ingest_records_only_query_warmup_cards_as_empty():
    payload = _load("xiaohongshu_search_success.json")
    payload["data"]["items"] = [
        {"model_type": "rec_query", "id": "warm-up", "note_card": {"display_title": "人工智能"}},
    ]

    response = _ingest(payload)

    assert response.status == "success"
    assert response.items == []
    assert response.source_reports[0].status == "empty"


def test_ingest_flags_xiaohongshu_schema_change_as_parser_changed():
    response = _ingest({"code": 0, "data": {}})

    assert response.status == "failed"
    assert response.source_reports[0].platform == "xiaohongshu"
    assert response.source_reports[0].status == "parser_changed"


def test_ingest_preserves_xiaohongshu_business_error_classification():
    response = _ingest({"code": 1001, "success": False})

    assert response.status == "failed"
    assert response.source_reports[0].status == "authentication_required"
    assert response.source_reports[0].error_code == "AUTH_REQUIRED"


# --- douyin path ---

def test_douyin_adapter_maps_fixture_records():
    result = DouyinAdapter().parse_payload(_load("douyin_search_success.json"))

    assert result.status.value == "success"
    assert len(result.items) == 1
    payload = result.items[0].payload
    assert payload["sourceItemId"] == "7300000000000000001"
    assert payload["url"] == "https://www.douyin.com/video/7300000000000000001"
    assert payload["contentType"] == "video"
    assert payload["author"]["name"] == "示例创作者"
    assert payload["metrics"]["viewCount"] == 9001
    assert payload["metrics"]["likeCount"] == 123
    assert payload["publishedAt"].startswith("2024-08-19T12:00:00")
    assert payload["media"][0]["type"] == "video"


def test_douyin_adapter_accepts_aweme_list_envelope():
    result = DouyinAdapter().parse_payload({
        "aweme_list": [{
            "aweme_id": "42",
            "desc": "列表信封样例",
            "create_time": 1724068800,
            "author": {"nickname": "作者"},
        }],
    })

    assert result.status.value == "success"
    assert len(result.items) == 1


def test_douyin_adapter_rejects_unknown_envelope_without_fake_empty():
    result = DouyinAdapter().parse_payload({"filters": {}, "cursor": 0})

    assert result.status.value == "parser_changed"


def test_ingest_maps_douyin_fixture_with_auto_detection():
    response = _ingest(_load("douyin_search_success.json"), keyword="示例")

    assert response.status == "success"
    assert len(response.items) == 1
    item = response.items[0]
    assert item.platform == "douyin"
    assert item.ext["captureIngest"]["platform"] == "douyin"
    assert item.collection.method == "http"
    assert response.source_reports[0].platform == "douyin"


# --- wechat path ---

def test_platform_envelope_detection_includes_wechat():
    assert detect_capture_platform(_load("wechat_capture_success.json")) == "wechat"
    assert detect_capture_platform({"app_msg_list": []}) == "wechat"
    # 账号搜索信封不是文章列表，不能按 wechat 列表解析。
    assert detect_capture_platform({"base_resp": {"ret": 0}, "list": [{}]}) is None


def test_wechat_adapter_maps_captured_list_records():
    from crawler_tool.sources.wechat_authorized import WechatAuthorizedListAdapter

    result = WechatAuthorizedListAdapter().parse_payload(_load("wechat_capture_success.json"))

    assert result.status.value == "success"
    assert len(result.items) == 2
    first = result.items[0].payload
    assert first["sourceItemId"] == "2652200000_1"
    assert first["title"] == "示例公众号文章：人工智能动态"
    assert first["summary"] == "一篇用于解析器测试的示例摘要"
    assert first["author"] == {"name": "示例作者"}
    assert first["method"] == "browser"
    assert first["publishedAt"].startswith("2024-08-19T12:00:00")
    assert first["url"].startswith("https://mp.weixin.qq.com/s?")
    second = result.items[1].payload
    assert second["author"] == {"name": None}
    assert second["summary"] is None


def test_wechat_adapter_skips_records_without_title_or_link():
    from crawler_tool.sources.wechat_authorized import WechatAuthorizedListAdapter

    payload = _load("wechat_capture_success.json")
    payload["app_msg_list"].append({"aid": "2652200000_3", "title": "", "link": ""})

    result = WechatAuthorizedListAdapter().parse_payload(payload)

    assert result.status.value == "success"
    assert len(result.items) == 2


def test_wechat_adapter_rejects_unknown_capture_shape_without_fake_empty():
    from crawler_tool.sources.wechat_authorized import WechatAuthorizedListAdapter

    result = WechatAuthorizedListAdapter().parse_payload({"base_resp": {"ret": 0}, "msg_list": []})

    assert result.status.value == "parser_changed"
    assert result.error_code.value == "PARSER_CHANGED"


def test_wechat_adapter_maps_empty_list_to_empty_not_error():
    from crawler_tool.sources.wechat_authorized import WechatAuthorizedListAdapter

    result = WechatAuthorizedListAdapter().parse_payload({"base_resp": {"ret": 0}, "app_msg_list": []})

    assert result.status.value == "empty"
    assert result.items == []


def test_wechat_adapter_preserves_base_resp_error_classification():
    from crawler_tool.sources.wechat_authorized import WechatAuthorizedListAdapter

    result = WechatAuthorizedListAdapter().parse_payload({
        "base_resp": {"ret": 200013, "errmsg": "freq control"},
        "app_msg_list": [],
    })

    assert result.status.value == "rate_limited"
    assert result.error_code.value == "RATE_LIMITED"
    assert result.retryable is False
    assert result.response_metadata["baseRespCode"] == 200013
    assert result.response_metadata["baseRespCategory"] == "rate_limited"


def test_ingest_maps_wechat_fixture_with_auto_detection():
    response = _ingest(_load("wechat_capture_success.json"), keyword="人工智能")

    assert response.status == "success"
    assert response.partial is False
    assert len(response.items) == 2
    item = response.items[0]
    assert item.platform == "wechat"
    assert item.collection.method == "browser"
    assert item.ext["captureIngest"] == {"origin": "manual_capture", "platform": "wechat", "keyword": "人工智能"}
    report = response.source_reports[0]
    assert report.platform == "wechat"
    assert report.status == "success"
    assert report.count == 2


def test_ingested_wechat_capture_is_served_by_unified_query():
    from crawler_tool.application import CrawlService
    from crawler_tool.application.recent_store import RecentItemsStore
    from crawler_tool.domain import ContentSearchRequest
    from crawler_tool.sources import SourceRegistry

    store = RecentItemsStore()
    ingested = CaptureIngestService(store=store).ingest(
        CaptureIngestRequest(keyword="人工智能", platform="wechat", capture=_load("wechat_capture_success.json"))
    )
    assert ingested.status == "success"

    service = CrawlService(SourceRegistry({}), history=store)
    hit = service.search(ContentSearchRequest(
        query="人工智能", platforms=["wechat"], freshness="prefer_fresh",
    ))

    assert hit.source_reports[0].platform == "wechat"
    assert hit.source_reports[0].status == "success"
    assert hit.source_reports[0].cached is True
    assert len(hit.items) == 2
    assert "示例公众号文章：人工智能动态" in {item.title for item in hit.items}


def test_ingest_unknown_envelope_is_a_clean_parser_error_not_empty():
    response = _ingest({"whatever": [1, 2, 3]})

    assert response.status == "failed"
    report = response.source_reports[0]
    assert report.platform == "other"
    assert report.status == "parser_changed"
    assert report.error_code == "PARSER_CHANGED"


def test_ingest_explicit_platform_mismatch_surfaces_parser_changed():
    # 明确指定平台但投喂了另一个平台的信封：不猜测，按解析变化处理。
    response = _ingest(_load("xiaohongshu_search_success.json"), platform="douyin")

    assert response.status == "failed"
    assert response.source_reports[0].platform == "douyin"
    assert response.source_reports[0].status == "parser_changed"


# --- 统一入口：显式查询捕获型平台时读本地会话历史 ---

def test_explicit_query_serves_local_history_for_capture_platforms():
    from crawler_tool.application import CrawlService
    from crawler_tool.application.recent_store import RecentItemsStore
    from crawler_tool.domain import ContentSearchRequest
    from crawler_tool.sources import SourceRegistry

    store = RecentItemsStore()
    ingested = CaptureIngestService(store=store).ingest(
        CaptureIngestRequest(keyword="人工智能", platform="douyin", capture=_load("douyin_search_success.json"))
    )
    assert ingested.status == "success"

    service = CrawlService(SourceRegistry({}), history=store)
    response = service.search(ContentSearchRequest(
        query="人工智能", platforms=["douyin"], limit=5, freshness="prefer_fresh",
    ))

    report = response.source_reports[0]
    assert report.platform == "douyin"
    assert report.status == "success"
    assert report.cached is True
    assert len(response.items) == 1
    assert response.items[0].platform == "douyin"


def test_local_history_miss_keeps_clean_unavailable_for_capture_platforms():
    from crawler_tool.application import CrawlService
    from crawler_tool.application.recent_store import RecentItemsStore
    from crawler_tool.domain import ContentSearchRequest
    from crawler_tool.sources import SourceRegistry

    service = CrawlService(SourceRegistry({}), history=RecentItemsStore())
    response = service.search(ContentSearchRequest(
        query="从未捕获的词", platforms=["xiaohongshu"], freshness="prefer_fresh",
    ))

    report = response.source_reports[0]
    assert report.platform == "xiaohongshu"
    assert report.status == "source_unavailable"
    assert report.error_code == "SOURCE_UNAVAILABLE"
    assert response.items == []


def test_wechat_unified_query_serves_session_history_and_points_to_dedicated_endpoint():
    from crawler_tool.application import CrawlService
    from crawler_tool.application.recent_store import RecentItemsStore
    from crawler_tool.domain import ContentSearchRequest
    from crawler_tool.normalization.content_normalizer import normalize_raw_item
    from crawler_tool.sources import RawItem, SourceRegistry

    store = RecentItemsStore()
    store.add([normalize_raw_item(RawItem(platform="wechat", payload={
        "sourceItemId": "wx-001",
        "sourceType": "social_media",
        "contentType": "post",
        "title": "人工智能政策解读",
        "url": "https://mp.weixin.qq.com/s/wx001",
    }), query="人工智能", collected_at=None)])

    service = CrawlService(SourceRegistry({}), history=store)

    hit = service.search(ContentSearchRequest(
        query="人工智能", platforms=["wechat"], freshness="prefer_fresh",
    ))
    assert hit.source_reports[0].platform == "wechat"
    assert hit.source_reports[0].status == "success"
    assert hit.source_reports[0].cached is True
    assert len(hit.items) == 1
    assert hit.items[0].title == "人工智能政策解读"

    miss = service.search(ContentSearchRequest(
        query="别的词", platforms=["wechat"], freshness="prefer_fresh",
    ))
    report = miss.source_reports[0]
    assert report.status == "source_unavailable"
    assert "search-wechat-articles" in (report.message or "")
