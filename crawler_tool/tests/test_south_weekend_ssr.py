from datetime import datetime, timezone
from pathlib import Path

from crawler_tool.domain import SourceStatus
from crawler_tool.sources import SouthWeekendAdapter


FIXTURES = Path("tests/fixtures")


def test_south_weekend_ssr_extracts_items_and_next_cursor():
    page = (FIXTURES / "south_weekend_search_success.html").read_text(encoding="utf-8")
    result = SouthWeekendAdapter(clock=datetime(2026, 8, 24, tzinfo=timezone.utc)).parse_html(page)
    assert result.status is SourceStatus.SUCCESS
    assert result.next_cursor is None
    assert len(result.items) == 3
    first = result.items[0].payload
    assert first["sourceItemId"] == "317022"
    assert first["url"] == "https://www.infzm.com/contents/317022"
    assert first["ext"]["section"] == "新金融"
    assert first["metrics"]["commentCount"] == 2
    assert "published_at_year_inferred" in first["warnings"]


def test_south_weekend_ssr_empty_is_empty_not_parser_change():
    page = (FIXTURES / "south_weekend_search_empty.html").read_text(encoding="utf-8")
    result = SouthWeekendAdapter().parse_html(page)
    assert result.status is SourceStatus.EMPTY
    assert result.next_cursor is None


def test_south_weekend_ssr_empty_without_result_panel_is_empty():
    page = (FIXTURES / "south_weekend_search_empty_real.html").read_text(encoding="utf-8")
    result = SouthWeekendAdapter().parse_html(page)
    assert result.status is SourceStatus.EMPTY
    assert result.items == []


def test_south_weekend_ssr_missing_panel_is_parser_changed():
    result = SouthWeekendAdapter().parse_html("<html><body>unexpected</body></html>")
    assert result.status is SourceStatus.PARSER_CHANGED


class _DetailResponse:
    def __init__(self, body: bytes, *, status_code: int = 200):
        self.content = body
        self.status_code = status_code


def test_south_weekend_detail_extracts_time_by_priority():
    """JSON-LD > meta > 可见文本；fixture 三处日期互不相同，验证优先级。"""
    page = (FIXTURES / "infzm_contents_detail.html").read_bytes()
    result = SouthWeekendAdapter(http_get=lambda url, timeout: _DetailResponse(page)).fetch_detail(
        "https://www.infzm.com/contents/317022"
    )
    assert result.status is SourceStatus.SUCCESS
    assert result.response_metadata == {
        "publishedAt": "2026-08-20T01:15:00+00:00",
        "publishedAtConfidence": 1.0,
        "detectedBy": "json_ld",
    }


def test_south_weekend_detail_decodes_gb18030_bytes():
    page = (FIXTURES / "infzm_contents_detail.html").read_text(encoding="utf-8").encode("gb18030")
    result = SouthWeekendAdapter(http_get=lambda url, timeout: _DetailResponse(page)).fetch_detail(
        "https://www.infzm.com/contents/317022"
    )
    assert result.status is SourceStatus.SUCCESS
    assert result.response_metadata["publishedAt"] == "2026-08-20T01:15:00+00:00"


def test_south_weekend_detail_challenge_stops_without_retry():
    # 真实站点正文为 GB18030；fixture 同编码模拟，避免 gb18030-first 解码乱码掩盖挑战标记
    page = (FIXTURES / "infzm_contents_detail_challenge.html").read_text(encoding="utf-8").encode("gb18030")
    calls = []

    def http_get(url, timeout):
        calls.append(url)
        return _DetailResponse(page)

    result = SouthWeekendAdapter(http_get=http_get).fetch_detail("https://www.infzm.com/contents/317022")
    assert result.status is SourceStatus.AUTHENTICATION_REQUIRED
    assert result.retryable is False
    assert calls == ["https://www.infzm.com/contents/317022"]


def test_south_weekend_detail_rate_limited_is_not_retried():
    result = SouthWeekendAdapter(http_get=lambda url, timeout: _DetailResponse(b"", status_code=429)).fetch_detail(
        "https://www.infzm.com/contents/317022"
    )
    assert result.status is SourceStatus.RATE_LIMITED
    assert result.retryable is False


def test_south_weekend_detail_without_transport_is_unavailable():
    result = SouthWeekendAdapter().fetch_detail("https://www.infzm.com/contents/317022")
    assert result.status is SourceStatus.SOURCE_UNAVAILABLE
    assert result.retryable is False


def test_south_weekend_payload_full_iso_keeps_full_confidence():
    """JSON 路径完整 ISO：置信 1.0，原始串入 ext，无时间警告。"""
    result = SouthWeekendAdapter().parse_payload({
        "data": {"list": [{
            "id": "317022",
            "url": "317022",
            "subject": "标题",
            "publish_time": "2026-08-23T10:20:00+08:00",
        }]},
    })
    payload = result.items[0].payload
    assert payload["publishedAt"] == "2026-08-23T10:20:00+08:00"
    assert payload["publishedAtConfidence"] == 1.0
    assert not [w for w in payload["warnings"] if w.startswith("published_at")]
    assert payload["ext"]["publishedAtRaw"] == "2026-08-23T10:20:00+08:00"


def test_south_weekend_payload_date_only_is_not_overestimated():
    """JSON 路径日期-only：按北京时间零点落地，置信 0.85，不再被默认 1.0 高估。"""
    result = SouthWeekendAdapter().parse_payload({
        "data": {"list": [{
            "id": "317022",
            "url": "317022",
            "subject": "标题",
            "publish_time": "2026-08-23",
        }]},
    })
    payload = result.items[0].payload
    assert payload["publishedAt"] == "2026-08-23T00:00:00+08:00"
    assert payload["publishedAtConfidence"] == 0.85
    assert "published_at_time_missing" in payload["warnings"]


def test_south_weekend_payload_unparseable_time_stays_empty_with_raw():
    """JSON 路径解析失败：publishedAt 置空、置信 0、unparsed 警告、原始串保留。"""
    result = SouthWeekendAdapter().parse_payload({
        "data": {"list": [{
            "id": "317022",
            "url": "317022",
            "subject": "标题",
            "publish_time": "_not-a-time_",
        }]},
    })
    payload = result.items[0].payload
    assert payload["publishedAt"] is None
    assert payload["publishedAtConfidence"] == 0.0
    assert "published_at_unparsed" in payload["warnings"]
    assert payload["ext"]["publishedAtRaw"] == "_not-a-time_"


def test_south_weekend_detail_includes_content_when_qualified():
    """正文合格（≥40 字符）时随同一次 GET 进入 metadata。"""
    page = (FIXTURES / "infzm_contents_detail.html").read_text(encoding="utf-8").replace(
        "据报道，该产品于发布会上正式亮相。",
        "据报道，该产品于发布会上正式亮相，现场演示了多轮对话与工单自动流转能力，" * 3,
    )
    result = SouthWeekendAdapter(http_get=lambda url, timeout: _DetailResponse(page.encode("utf-8"))).fetch_detail(
        "https://www.infzm.com/contents/317022"
    )
    assert result.status is SourceStatus.SUCCESS
    assert result.response_metadata["publishedAt"] == "2026-08-20T01:15:00+00:00"
    assert "正式亮相" in result.response_metadata["content"]
    assert result.response_metadata["contentDetectedBy"] == "visible_article"


def test_south_weekend_detail_success_with_content_only():
    """时间无信号但正文合格：仍 SUCCESS，metadata 只带正文字段。"""
    page = "<html><body><div class='nfzm-content'><p>" + "正文内容足够长，包含充分的事实细节与背景。" * 5 + "</p></div></body></html>"
    result = SouthWeekendAdapter(http_get=lambda url, timeout: _DetailResponse(page.encode("utf-8"))).fetch_detail(
        "https://www.infzm.com/contents/317022"
    )
    assert result.status is SourceStatus.SUCCESS
    assert "publishedAt" not in result.response_metadata
    assert "content" in result.response_metadata
