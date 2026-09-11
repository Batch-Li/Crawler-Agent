from datetime import datetime, timezone
import json

from crawler_tool.application import CrawlService
from crawler_tool.pagination import CursorCodec, PageRequest, PaginationError
from crawler_tool.domain import ContentSearchRequest, SourceStatus
from crawler_tool.normalization import canonicalize_url, normalize_raw_item
from crawler_tool.sources import RawItem, SourceRegistry, SouthWeekendAdapter, ToutiaoAdapter


def test_canonicalize_url_removes_tracking_parameters():
    assert canonicalize_url("HTTPS://Example.COM/a?utm_source=x&id=1#frag") == "https://example.com/a?id=1"


def test_unknown_raw_fields_are_preserved_in_ext():
    item = normalize_raw_item(RawItem(
        platform="weibo",
        payload={
            "sourceType": "social_media",
            "contentType": "post",
            "url": "https://example.com/post/1?utm_source=x",
            "vendorSpecific": {"rank": 1},
        },
    ), collected_at=datetime.now(timezone.utc))
    assert item.ext["vendorSpecific"] == {"rank": 1}
    assert item.content_id.startswith("cnt_")


def test_south_weekend_fixture_parser():
    payload = json.loads(open("tests/fixtures/south_weekend_search.json", encoding="utf-8").read())
    result = SouthWeekendAdapter().parse_payload(payload)
    assert result.status is SourceStatus.SUCCESS
    assert result.items[0].payload["url"].endswith("/123456")
    assert result.items[0].payload["metrics"]["commentCount"] == 35




def test_cursor_codec_round_trip_and_tamper_rejection():
    codec = CursorCodec("test-secret", clock=lambda: 100)
    token = codec.encode(platform="toutiao", fingerprint="fp", continuation="1", adapter_version="0.1.0")
    assert codec.decode(token, platform="toutiao", fingerprint="fp", adapter_version="0.1.0") == PageRequest("1")
    try:
        codec.decode(token[:-1] + ("A" if token[-1] != "A" else "B"), platform="toutiao", fingerprint="fp", adapter_version="0.1.0")
    except PaginationError:
        pass
    else:
        raise AssertionError("tampered cursor must be rejected")



def test_toutiao_cursor_round_trip_requests_second_page():
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

    adapter_factory = lambda: ToutiaoAdapter(http_get=http_get)
    codec = CursorCodec("test-secret", clock=lambda: 100)
    service = CrawlService(SourceRegistry({"toutiao": adapter_factory}), cursor_codec=codec)
    first = service.search(ContentSearchRequest(query="AI客服", platforms=["toutiao"], freshness="prefer_fresh"))
    second = service.search(ContentSearchRequest(query="AI客服", platforms=["toutiao"], cursor=first.next_cursor, freshness="prefer_fresh"))

    assert calls == [0, 1]
    assert first.next_cursor
    assert second.next_cursor is None
    assert first.items[0].source_item_id != second.items[0].source_item_id

    class GoodAdapter(SouthWeekendAdapter):
        def search(self, request):
            result = self.parse_payload({"data": {"list": [{
                "id": "1", "subject": "same", "introtext": "text",
                "publish_time": "2026-08-23T10:20:00+08:00", "url": "1"
            }]}})
            return result

    class FailingAdapter(SouthWeekendAdapter):
        platform = "weibo"
        def search(self, request):
            from crawler_tool.sources.base import AdapterResult
            return AdapterResult(status=SourceStatus.RATE_LIMITED, error_code="RATE_LIMITED", retryable=True)

    service = CrawlService(SourceRegistry({"south_weekend": GoodAdapter, "weibo": FailingAdapter}))
    response = service.search(ContentSearchRequest(query="same", platforms=["south_weekend", "weibo"]))
    assert response.status == "partial"
    assert response.partial is True
    assert len(response.items) == 1
    assert response.source_reports[1].status == "rate_limited"




def test_three_source_search_keeps_success_when_weibo_auth_fails():
    from crawler_tool.sources.base import AdapterResult

    class GoodAdapter(SouthWeekendAdapter):
        def search(self, request):
            return self.parse_payload({"data": {"list": [{"id": "1", "subject": "same", "url": "1"}]}})

    class AuthFailAdapter(SouthWeekendAdapter):
        platform = "weibo"
        def search(self, request):
            return AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code="AUTH_REQUIRED", retryable=False)

    service = CrawlService(SourceRegistry({"south_weekend": GoodAdapter, "toutiao": GoodAdapter, "weibo": AuthFailAdapter}))
    response = service.search(ContentSearchRequest(query="same", platforms=["south_weekend", "toutiao", "weibo"], freshness="prefer_fresh"))

    assert response.status == "partial"
    assert response.partial is True
    assert len(response.items) == 1
    assert response.source_reports[-1].platform == "weibo"
    assert response.source_reports[-1].status == "authentication_required"

    called = []

    class SpyAdapter(SouthWeekendAdapter):
        def __init__(self, platform):
            self.platform = platform
            self.http_get = None
            self.clock = datetime.now(timezone.utc)

        def search(self, request):
            called.append(self.platform)
            from crawler_tool.sources.base import AdapterResult
            return AdapterResult(status=SourceStatus.EMPTY)

    registry = SourceRegistry({
        "south_weekend": lambda: SpyAdapter("south_weekend"),
        "toutiao": lambda: SpyAdapter("toutiao"),
        "weibo": lambda: SpyAdapter("weibo"),
    }, default_platforms=("south_weekend", "toutiao"))

    CrawlService(registry).search(ContentSearchRequest(query="AI客服"))

    assert called == ["south_weekend", "toutiao"]


def test_transient_failures_are_not_cached_and_success_expires():
    from crawler_tool.sources.base import AdapterResult

    class FlakyAdapter(SouthWeekendAdapter):
        def __init__(self, fail_times=0):
            self.calls = 0
            self.fail_times = fail_times

        def search(self, request):
            self.calls += 1
            if self.calls <= self.fail_times:
                return AdapterResult(status=SourceStatus.RATE_LIMITED, error_code="RATE_LIMITED", retryable=True)
            return self.parse_payload({"data": {"list": [{
                "id": "1", "subject": "缓存测试", "introtext": "正文",
                "publish_time": "2026-08-23T10:20:00+08:00", "url": "1",
            }]}})

    clock = {"now": 1000.0}
    adapter = FlakyAdapter(fail_times=1)
    service = CrawlService(
        SourceRegistry({"toutiao": lambda: adapter}),
        cache_ttl_seconds=600.0,
        clock=lambda: clock["now"],
    )
    request = ContentSearchRequest(query="AI客服", platforms=["toutiao"], freshness="prefer_cached")

    # 失败不入缓存：第二次仍会真实请求并拿到成功结果。
    first = service.search(request)
    assert first.source_reports[0].status == "rate_limited"
    second = service.search(request)
    assert second.status == "success"
    assert adapter.calls == 2

    # 成功结果进入缓存：第三次直接命中。
    third = service.search(request)
    assert third.source_reports[0].cached is True
    assert adapter.calls == 2

    # TTL 过期后重新请求。
    clock["now"] += 601.0
    fourth = service.search(request)
    assert fourth.source_reports[0].cached is False
    assert adapter.calls == 3

    # 成功结果进入会话视图，且重复搜索按 contentId 合并。
    store_adapter = FlakyAdapter(fail_times=0)
    store_service = CrawlService(SourceRegistry({"toutiao": lambda: store_adapter}))
    same_request = ContentSearchRequest(query="AI客服", platforms=["toutiao"], freshness="prefer_fresh")
    store_service.search(same_request)
    store_service.search(same_request)
    assert len(store_service.recent_store) == 1
    viewed = store_service.recent_store.query(platform="south_weekend")
    assert viewed[0].title == "缓存测试"


def test_failed_search_results_do_not_enter_session_view():
    class FailingOnly(SouthWeekendAdapter):
        platform = "weibo"

        def search(self, request):
            from crawler_tool.sources.base import AdapterResult
            return AdapterResult(status=SourceStatus.RATE_LIMITED, error_code="RATE_LIMITED", retryable=True)

    service = CrawlService(SourceRegistry({"weibo": FailingOnly}))
    response = service.search(ContentSearchRequest(query="AI客服", platforms=["weibo"], freshness="prefer_fresh"))

    assert response.status == "failed"
    assert len(service.recent_store) == 0
