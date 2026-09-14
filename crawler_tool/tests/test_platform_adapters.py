from __future__ import annotations

import pathlib

from crawler_tool.domain import ContentSearchRequest, SourceErrorCode, SourceStatus
from crawler_tool.pagination import PageRequest
from crawler_tool.sources import ToutiaoAdapter, WeiboAdapter


ROOT = pathlib.Path(__file__).parent / "fixtures"


class StubResponse:
    def __init__(self, text: str = "", *, status_code: int = 200, content_type: str = "text/html") -> None:
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_toutiao_fixture_parser_extracts_decoded_url_and_fields():
    result = ToutiaoAdapter().parse_html((ROOT / "toutiao_search.html").read_text(encoding="utf-8"))
    assert result.status is SourceStatus.SUCCESS
    item = result.items[0].payload
    assert item["url"] == "https://example.com/news/123456"
    assert item["sourceItemId"] == "123456"
    assert item["author"]["name"] is None
    assert item["ext"]["sourceName"] == "科技媒体"
    assert item["metrics"]["commentCount"] == 35


def _toutiao_card(source_name: str, time_text: str) -> str:
    return (
        '<div class="s-result-list"><div class="cs-card">'
        '<a class="text-underline-hover" href="https://example.com/news/123456">标题</a>'
        '<div class="cs-source-content">'
        f'<span class="text-ellipsis">{source_name}</span>'
        f'<span class="text-ellipsis">{time_text}</span>'
        "</div></div></div>"
    )


def test_toutiao_parses_date_only_publish_time():
    from datetime import datetime, timezone

    clock = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    result = ToutiaoAdapter(clock=clock).parse_html(_toutiao_card("科技媒体", "2026-08-23"))
    payload = result.items[0].payload
    # 纯日期按北京时间零点换算 UTC
    assert payload["publishedAt"] == "2026-08-22T16:00:00+00:00"
    assert payload["publishedAtConfidence"] == 0.85
    assert "published_at_time_missing" in payload["warnings"]
    assert payload["ext"]["publishedAtRaw"] == "2026-08-23"


def test_toutiao_parses_relative_publish_time():
    from datetime import datetime, timezone

    clock = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    result = ToutiaoAdapter(clock=clock).parse_html(_toutiao_card("科技媒体", "3小时前"))
    payload = result.items[0].payload
    assert payload["publishedAt"] == "2026-09-06T09:00:00+00:00"
    assert payload["publishedAtConfidence"] == 0.6
    assert "published_at_relative" in payload["warnings"]


def test_toutiao_extended_time_forms():
    """真实卡片新增形态：周/月/年前、中文与点分隔日期、MM-DD HH:MM。"""
    from datetime import datetime, timezone

    clock = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    result = ToutiaoAdapter(clock=clock).parse_html(
        (ROOT / "toutiao_time_forms.html").read_text(encoding="utf-8")
    )
    payload = {item.payload["sourceItemId"]: item.payload for item in result.items}

    # 3周前：clock(UTC 12:00 = CST 20:00) - 21 天
    assert payload["100001"]["publishedAtConfidence"] == 0.6
    assert "published_at_relative" in payload["100001"]["warnings"]
    assert payload["100001"]["publishedAt"].startswith("2026-08-16T12:00:00+00:00")
    # 8个月前：日历月回退到 2026-01-06，保留时刻
    assert payload["100002"]["publishedAt"].startswith("2026-01-06T12:00:00+00:00")
    assert payload["100002"]["publishedAtConfidence"] == 0.6
    # 2年前：2024-09-06，保留时刻
    assert payload["100003"]["publishedAt"].startswith("2024-09-06T12:00:00+00:00")
    # 中文日期：按北京时间零点换算 UTC，置信 0.85
    assert payload["100004"]["publishedAt"] == "2023-02-20T16:00:00+00:00"
    assert payload["100004"]["publishedAtConfidence"] == 0.85
    assert "published_at_time_missing" in payload["100004"]["warnings"]
    # 点分隔日期同纯日期
    assert payload["100005"]["publishedAt"] == "2023-02-20T16:00:00+00:00"
    assert payload["100005"]["publishedAtConfidence"] == 0.85
    # MM-DD HH:MM：年份推断（02-21 早于时钟，取当年），置信 0.65
    assert payload["100006"]["publishedAt"] == "2026-02-21T06:30:00+00:00"
    assert payload["100006"]["publishedAtConfidence"] == 0.65
    assert "published_at_year_inferred" in payload["100006"]["warnings"]


def test_toutiao_real_world_time_forms():
    """真实卡片出现的形态：缺年月日（1月14日）与相对词+时刻（昨天10:37）。"""
    from datetime import datetime, timezone

    clock = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    # 1月14日：已过当年 1 月且不在未来窗口 → 推断为 2026 年
    result = ToutiaoAdapter(clock=clock).parse_html(_toutiao_card("科技媒体", "1月14日"))
    payload = result.items[0].payload
    assert payload["publishedAt"] == "2026-01-13T16:00:00+00:00"
    assert payload["publishedAtConfidence"] == 0.65
    assert "published_at_year_inferred" in payload["warnings"]
    # 昨天10:37：北京时间昨天同时刻
    result = ToutiaoAdapter(clock=clock).parse_html(_toutiao_card("科技媒体", "昨天10:37"))
    payload = result.items[0].payload
    assert payload["publishedAt"] == "2026-09-05T02:37:00+00:00"
    assert payload["publishedAtConfidence"] == 0.6
    assert "published_at_relative" in payload["warnings"]
    # 今天08:05：北京时间今天同时刻
    result = ToutiaoAdapter(clock=clock).parse_html(_toutiao_card("科技媒体", "今天08:05"))
    assert result.items[0].payload["publishedAt"] == "2026-09-06T00:05:00+00:00"
    # 前天07:38：北京时间前天（09-04）07:38 = 09-03T23:38Z
    result = ToutiaoAdapter(clock=clock).parse_html(_toutiao_card("科技媒体", "前天07:38"))
    assert result.items[0].payload["publishedAt"] == "2026-09-03T23:38:00+00:00"
    assert result.items[0].payload["publishedAtConfidence"] == 0.6


def test_toutiao_fallback_scans_spans_when_primary_position_fails():
    """主位置（最末 span）不是时间时，在同容器其余 span 里兜底取最末可解析者。"""
    from datetime import datetime, timedelta, timezone

    clock = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    result = ToutiaoAdapter(clock=clock).parse_html(
        (ROOT / "toutiao_time_forms.html").read_text(encoding="utf-8")
    )
    payload = {item.payload["sourceItemId"]: item.payload for item in result.items}

    # 主位置是"置顶"（不可解析），"昨天" 在更早的 span 里
    assert payload["100007"]["publishedAt"] == (clock - timedelta(days=1)).isoformat()
    assert payload["100007"]["publishedAtConfidence"] == 0.6
    assert payload["100007"]["ext"]["publishedAtRaw"] == "昨天"
    # 无时间卡：全 span 均不可解析，保持置空 + unparsed
    assert payload["100008"]["publishedAt"] is None
    assert payload["100008"]["publishedAtConfidence"] == 0.0
    assert "published_at_unparsed" in payload["100008"]["warnings"]
    # 干扰负样本：标题里的日期、非时间 span 都不误判为发布时间
    assert payload["100009"]["publishedAt"] is None
    assert payload["100009"]["publishedAtConfidence"] == 0.0


def test_toutiao_unwraps_double_wrapped_jump_link():
    """回归：真实页面存在双层嵌套跳转链，必须循环剥皮到真实地址。"""
    href = (
        "https://sou.toutiao.com/search/jump?"
        "url=" + "https%3A%2F%2Fsou.toutiao.com%2Fsearch%2Fjump%3F"
        "url%3Dhttp%253A%252F%252Fwww.toutiao.com%252Fa7682297523326698013%252F"
        "%26channel%3D%26source%3Dnews&aid=4916&jtoken="
    )
    from crawler_tool.sources.toutiao import _extract_target_url

    target = _extract_target_url(href)
    assert target == "http://www.toutiao.com/a7682297523326698013/"




def test_toutiao_parser_detects_next_page_and_stops_at_last_page():
    first_page = ToutiaoAdapter().parse_html(
        (ROOT / "toutiao_search_page_0.html").read_text(encoding="utf-8"), page_number=0
    )
    second_page = ToutiaoAdapter().parse_html(
        (ROOT / "toutiao_search_page_1.html").read_text(encoding="utf-8"), page_number=1
    )

    assert first_page.next_cursor == "1"
    assert first_page.items[0].payload["sourceItemId"] == "123456"
    assert second_page.next_cursor is None
    assert second_page.items[0].payload["sourceItemId"] == "654321"


def test_toutiao_search_page_uses_continuation_parameter():
    calls = []
    pages = {
        0: (ROOT / "toutiao_search_page_0.html").read_text(encoding="utf-8"),
        1: (ROOT / "toutiao_search_page_1.html").read_text(encoding="utf-8"),
    }

    def http_get(*args, **kwargs):
        page_number = kwargs["params"]["page_num"]
        calls.append(page_number)
        return StubResponse(pages[page_number])

    adapter = ToutiaoAdapter(http_get=http_get)
    first_page = adapter.search(ContentSearchRequest(query="AI客服"))
    second_page = adapter.search_page(ContentSearchRequest(query="AI客服"), PageRequest("1"))

    assert calls == [0, 1]
    assert first_page.next_cursor == "1"
    assert second_page.next_cursor is None

    result = ToutiaoAdapter().parse_html("<html><body>unrelated page</body></html>")
    assert result.status is SourceStatus.PARSER_CHANGED


def test_toutiao_challenge_page_is_auth_error():
    result = ToutiaoAdapter().parse_html("<html><body>请完成安全验证</body></html>")
    assert result.status is SourceStatus.AUTHENTICATION_REQUIRED
    assert result.error_code is SourceErrorCode.AUTH_REQUIRED


def test_toutiao_search_uses_public_first_page_parameters():
    calls = []
    page = (ROOT / "toutiao_search.html").read_text(encoding="utf-8")

    def http_get(*args, **kwargs):
        calls.append((args, kwargs))
        return StubResponse(page)

    result = ToutiaoAdapter(http_get=http_get).search(ContentSearchRequest(query="AI客服"))

    assert result.status is SourceStatus.SUCCESS
    assert calls == [(
        ("https://so.toutiao.com/search",),
        {"params": {"dvpf": "pc", "source": "input", "keyword": "AI客服", "pd": "synthesis", "page_num": 0}, "timeout": 10},
    )]


def test_toutiao_search_maps_rate_limit_without_retrying():
    def http_get(*args, **kwargs):
        return StubResponse(status_code=429)

    result = ToutiaoAdapter(http_get=http_get).search(ContentSearchRequest(query="AI客服"))

    assert result.status is SourceStatus.RATE_LIMITED
    assert result.error_code is SourceErrorCode.RATE_LIMITED
    assert result.retryable is True


def test_toutiao_search_maps_auth_and_server_errors():
    for status_code, expected_status, retryable in [
        (403, SourceStatus.AUTHENTICATION_REQUIRED, False),
        (503, SourceStatus.NETWORK_ERROR, True),
        (404, SourceStatus.NETWORK_ERROR, False),
    ]:
        result = ToutiaoAdapter(http_get=lambda *args, **kwargs: StubResponse(status_code=status_code)).search(
            ContentSearchRequest(query="AI客服")
        )
        assert result.status is expected_status
        assert result.retryable is retryable


def test_toutiao_search_rejects_non_html_response():
    result = ToutiaoAdapter(http_get=lambda *args, **kwargs: StubResponse(
        "{}", content_type="application/json"
    )).search(ContentSearchRequest(query="AI客服"))

    assert result.status is SourceStatus.NETWORK_ERROR
    assert result.error_code is SourceErrorCode.INVALID_RESPONSE
    assert result.retryable is False


def test_weibo_fixture_parser_converts_chinese_metrics():
    result = WeiboAdapter().parse_html((ROOT / "weibo_search.html").read_text(encoding="utf-8"))
    assert result.status is SourceStatus.SUCCESS
    item = result.items[0].payload
    assert item["sourceItemId"] == "AbCdEf123"
    assert item["method"] == "http"
    assert item["metrics"]["shareCount"] == 12000
    assert item["metrics"]["likeCount"] == 25000




def test_weibo_search_uses_anonymous_first_page_parameters():
    calls = []
    page = (ROOT / "weibo_search.html").read_text(encoding="utf-8")

    def http_get(*args, **kwargs):
        calls.append((args, kwargs))
        return StubResponse(page)

    result = WeiboAdapter(http_get=http_get).search(ContentSearchRequest(query="AI客服"))

    assert result.status is SourceStatus.SUCCESS
    assert calls == [(
        ("https://s.weibo.com/weibo",),
        {"params": {"q": "AI客服", "typeall": 1, "suball": 1, "page": 1}, "timeout": 10},
    )]


def test_weibo_search_maps_http_failures_without_credentials():
    for status_code, expected_status, retryable in [
        (429, SourceStatus.RATE_LIMITED, True),
        (403, SourceStatus.AUTHENTICATION_REQUIRED, False),
        (503, SourceStatus.NETWORK_ERROR, True),
        (404, SourceStatus.NETWORK_ERROR, False),
    ]:
        result = WeiboAdapter(http_get=lambda *args, **kwargs: StubResponse(status_code=status_code)).search(
            ContentSearchRequest(query="AI客服")
        )
        assert result.status is expected_status
        assert result.retryable is retryable


def test_weibo_search_rejects_non_html_response():
    result = WeiboAdapter(http_get=lambda *args, **kwargs: StubResponse("{}", content_type="application/json")).search(
        ContentSearchRequest(query="AI客服")
    )

    assert result.status is SourceStatus.NETWORK_ERROR
    assert result.error_code is SourceErrorCode.INVALID_RESPONSE
    assert result.retryable is False


def test_weibo_login_page_is_auth_error():
    result = WeiboAdapter().parse_html("<html><body>请登录后继续访问</body></html>")
    assert result.status is SourceStatus.AUTHENTICATION_REQUIRED
