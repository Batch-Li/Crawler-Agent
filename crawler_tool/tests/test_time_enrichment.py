"""时间补全编排：预算上限、即停、失败隔离、来源跳过、CrawlService 挂钩。"""

from __future__ import annotations

from crawler_tool.application import CrawlService
from crawler_tool.application.time_enrichment import TimeEnrichmentService
from crawler_tool.domain import ContentSearchRequest, SourceStatus
from crawler_tool.normalization import normalize_raw_item
from crawler_tool.sources import RawItem, SourceRegistry
from crawler_tool.sources.base import AdapterResult, SourceAdapter


def _item(url: str, published: str | None = None, confidence: float | None = None):
    payload = {
        "sourceItemId": url.rsplit("/", 1)[-1],
        "sourceType": "news_media",
        "contentType": "article",
        "title": f"标题 {url}",
        "summary": "摘要",
        "url": url,
        "method": "http",
        "warnings": ["search_result_summary_only"],
    }
    if published:
        payload["publishedAt"] = published
    if confidence is not None:
        payload["publishedAtConfidence"] = confidence
    return normalize_raw_item(RawItem(platform="south_weekend", payload=payload), query="测试")


class FakeDetailAdapter(SourceAdapter):
    platform = "south_weekend"
    adapter_version = "0.0.0-fake"

    def __init__(self, outcomes: list[AdapterResult]) -> None:
        self.outcomes = list(outcomes)
        self.detail_urls: list[str] = []

    def search(self, request: ContentSearchRequest) -> AdapterResult:
        return AdapterResult(status=SourceStatus.EMPTY)

    def fetch_detail(self, url: str) -> AdapterResult:
        self.detail_urls.append(str(url))
        if self.outcomes:
            return self.outcomes.pop(0)
        return AdapterResult(status=SourceStatus.EMPTY)


def detail_success(iso: str) -> AdapterResult:
    return AdapterResult(status=SourceStatus.SUCCESS, response_metadata={
        "publishedAt": iso, "publishedAtConfidence": 1.0, "detectedBy": "json_ld",
    })


def _service(adapter: FakeDetailAdapter, **kwargs) -> tuple[CrawlService, FakeDetailAdapter]:
    service = CrawlService(SourceRegistry({"south_weekend": lambda: adapter}), **kwargs)
    return service, adapter


def test_enrich_applies_time_and_marks_warning():
    adapter = FakeDetailAdapter([detail_success("2026-08-20T01:15:00+00:00")])
    items = [_item("https://www.infzm.com/contents/1"), _item("https://www.infzm.com/contents/2", "2026-08-23T10:20:00+08:00", 1.0)]
    service, adapter = _service(adapter)
    result, fetches = service.time_enrichment.enrich(items)
    assert fetches == 1
    assert result[0].published_at is not None
    assert result[0].quality.published_at_confidence == 1.0
    assert "published_at_from_detail" in result[0].quality.warnings
    assert result[0].ext["detailTimeFetched"] is True
    # 已有高置信时间的条目不是候选
    assert adapter.detail_urls == ["https://www.infzm.com/contents/1"]
    # 原 items 中的对象不被就地修改
    assert items[0].published_at is None


def test_enrich_budget_caps_requests():
    adapter = FakeDetailAdapter([])
    items = [_item(f"https://www.infzm.com/contents/{i}") for i in range(5)]
    service, _ = _service(adapter)
    _, fetches = service.time_enrichment.enrich(items)
    assert fetches == 5  # 默认上限
    service_small, _ = _service(FakeDetailAdapter([]))
    service_small.time_enrichment.max_fetches = 2
    _, fetches = service_small.time_enrichment.enrich(items)
    assert fetches == 2


def test_enrich_stops_platform_on_challenge_and_marks_failure():
    adapter = FakeDetailAdapter([
        AdapterResult(status=SourceStatus.AUTHENTICATION_REQUIRED, error_code="AUTH_REQUIRED", retryable=False),
    ])
    items = [_item(f"https://www.infzm.com/contents/{i}") for i in range(3)]
    service, adapter = _service(adapter)
    result, fetches = service.time_enrichment.enrich(items)
    assert fetches == 1  # 挑战页即停，不消耗剩余预算
    assert adapter.detail_urls == ["https://www.infzm.com/contents/0"]
    assert "detail_fetch_failed" in result[0].quality.warnings
    assert result[1].published_at is None
    assert "detail_fetch_failed" not in result[1].quality.warnings


def test_enrich_failure_does_not_discard_item():
    adapter = FakeDetailAdapter([AdapterResult(status=SourceStatus.NETWORK_ERROR, error_code="INVALID_RESPONSE", retryable=False)])
    items = [_item("https://www.infzm.com/contents/1")]
    service, _ = _service(adapter)
    result, fetches = service.time_enrichment.enrich(items)
    assert fetches == 1
    assert result[0].title == items[0].title
    assert result[0].published_at is None
    assert "detail_fetch_failed" in result[0].quality.warnings


def test_enrich_skips_manual_capture_platforms():
    class CaptureAdapter(FakeDetailAdapter):
        platform = "xiaohongshu"

    adapter = CaptureAdapter([])
    items = [normalize_raw_item(RawItem(platform="xiaohongshu", payload={
        "sourceItemId": "1", "title": "捕获", "url": "https://www.xiaohongshu.com/x/1", "method": "other",
    }), query="测试")]
    service = CrawlService(SourceRegistry({"xiaohongshu": lambda: adapter}))
    _, fetches = service.time_enrichment.enrich(items)
    assert fetches == 0
    assert adapter.detail_urls == []


def test_crawl_service_enrich_time_flag_controls_requests():
    page = "<html><body>空搜索</body></html>"

    class StubResponse:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = page
        content = page.encode("utf-8")

        def raise_for_status(self):
            return None

    class SearchAdapter(FakeDetailAdapter):
        def search(self, request: ContentSearchRequest) -> AdapterResult:
            return AdapterResult(items=[RawItem(platform=self.platform, payload={
                "sourceItemId": "9", "title": "缺时间标题", "summary": "摘要",
                "url": "https://www.infzm.com/contents/9", "method": "http",
            })], status=SourceStatus.SUCCESS)

    adapter = SearchAdapter([])
    service, adapter = _service(adapter)

    off = service.search(ContentSearchRequest(query="测试", platforms=["south_weekend"], limit=5))
    assert adapter.detail_urls == []
    assert off.status == "success"

    on = service.search(ContentSearchRequest(
        query="测试", platforms=["south_weekend"], limit=5,
        enrich_time=True,
    ))
    assert adapter.detail_urls == ["https://www.infzm.com/contents/9"]
    assert on.status == "success"  # 详情失败不得升级为 partial
    assert on.source_reports[0].status == "success"
    assert on.items[0].published_at is None  # outcome=EMPTY 无时间可填
    assert "detail_fetch_failed" in on.items[0].quality.warnings
    # 会话视图同步为最新版本
    history = service.recent_store.query(platform="south_weekend", keyword="测试", limit=10)
    refreshed = [entry for entry in history if entry.source_item_id == "9"]
    assert refreshed and "detail_fetch_failed" in refreshed[0].quality.warnings


def test_balanced_truncate_interleaves_platforms():
    """多来源超过限额时轮转截断：先处理的平台不再挤掉后续来源。"""
    from collections import deque

    from crawler_tool.application.crawl_service import _balanced_truncate

    items = (
        [_item(f"https://www.infzm.com/contents/sw{i}") for i in range(10)]
        + [normalize_raw_item(RawItem(platform="toutiao", payload={
            "sourceItemId": f"tt{i}", "title": f"头条{i}", "url": f"https://example.com/tt{i}", "method": "http",
        }), query="测试") for i in range(18)]
        + [normalize_raw_item(RawItem(platform="xiaohongshu", payload={
            "sourceItemId": f"xhs{i}", "title": f"小红书{i}", "url": f"https://www.xiaohongshu.com/x/{i}", "method": "other",
        }), query="测试") for i in range(18)]
    )
    balanced = _balanced_truncate(items, 6)
    platforms = [item.platform for item in balanced]
    # 轮转：南周/头条/小红书 各 2 条，平台内保持原顺序
    assert platforms == ["south_weekend", "toutiao", "xiaohongshu"] * 2
    assert [item.source_item_id for item in balanced if item.platform == "south_weekend"] == ["sw0", "sw1"]
    # 限额内不截断
    assert _balanced_truncate(items[:3], 6) == items[:3]
    assert deque is not None


# --- 正文补抓（enrichContent）---


def _content_success(text: str, detected_by: str = "embedded_variable") -> AdapterResult:
    return AdapterResult(status=SourceStatus.SUCCESS, response_metadata={
        "content": text, "contentDetectedBy": detected_by,
    })


def test_enrich_content_applies_full_content():
    adapter = FakeDetailAdapter([_content_success("全文正文，事实细节充分。" * 10)])
    items = [_item("https://www.infzm.com/contents/1")]
    service, _ = _service(adapter)
    result, fetches = service.time_enrichment.enrich(items, include_content=True)
    assert fetches == 1
    assert result[0].content is not None and "全文正文" in result[0].content
    assert result[0].quality.has_full_content is True
    assert "content_from_detail" in result[0].quality.warnings
    assert result[0].ext["detailContentFetched"] is True
    assert result[0].ext["detailContentDetectedBy"] == "embedded_variable"
    # metadata 无时间字段时不造时间
    assert result[0].published_at is None


def test_enrich_without_content_flag_leaves_content_alone():
    adapter = FakeDetailAdapter([AdapterResult(status=SourceStatus.SUCCESS, response_metadata={
        "publishedAt": "2026-08-20T01:15:00+00:00", "publishedAtConfidence": 1.0,
        "detectedBy": "json_ld", "content": "这段全文不应被应用。"})])
    items = [_item("https://www.infzm.com/contents/1")]
    service, _ = _service(adapter)
    result, _ = service.time_enrichment.enrich(items)
    assert result[0].published_at is not None
    assert result[0].content is None
    assert "content_from_detail" not in result[0].quality.warnings


def test_enrich_applies_time_and_content_from_same_fetch():
    adapter = FakeDetailAdapter([AdapterResult(status=SourceStatus.SUCCESS, response_metadata={
        "publishedAt": "2026-08-20T01:15:00+00:00", "publishedAtConfidence": 1.0,
        "detectedBy": "json_ld", "content": "一次请求同时命中的全文。",
        "contentDetectedBy": "visible_article"})])
    items = [_item("https://www.infzm.com/contents/1")]
    service, _ = _service(adapter)
    result, fetches = service.time_enrichment.enrich(items, include_content=True)
    assert fetches == 1
    assert result[0].published_at is not None and result[0].content is not None
    assert result[0].ext["detailTimeFetched"] is True
    assert result[0].ext["detailContentFetched"] is True


def test_content_and_time_share_budget():
    adapter = FakeDetailAdapter([])
    items = [_item(f"https://www.infzm.com/contents/{i}") for i in range(3)]
    service, adapter = _service(adapter)
    service.time_enrichment.max_fetches = 1
    _, fetches = service.time_enrichment.enrich(items, include_content=True)
    assert fetches == 1
    assert len(adapter.detail_urls) == 1


def test_content_missing_marks_warning_on_failure():
    adapter = FakeDetailAdapter([AdapterResult(status=SourceStatus.EMPTY)])
    items = [_item("https://www.infzm.com/contents/1")]
    service, _ = _service(adapter)
    result, _ = service.time_enrichment.enrich(items, include_content=True)
    assert "detail_fetch_failed" in result[0].quality.warnings
    assert "content_missing_from_detail" in result[0].quality.warnings
