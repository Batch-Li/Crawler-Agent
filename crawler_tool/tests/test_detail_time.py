"""detail_time 纯函数：提取优先级、有效性边界、可见文本限幅。"""

from datetime import datetime, timezone

from crawler_tool.infrastructure.detail_time import parse_detail_time

CLOCK = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def test_json_ld_wins_over_meta_and_visible_text():
    page = (
        '<html><head><meta property="article:published_time" content="2026-08-21T08:00:00+08:00"></head>'
        "<body><script type=\"application/ld+json\">{\"@type\":\"NewsArticle\",\"datePublished\":\"2026-08-20T09:15:00+08:00\"}</script>"
        "<p>2026-08-22 11:30 报道</p></body></html>"
    )
    detail = parse_detail_time(page, clock=CLOCK)
    assert detail is not None
    assert detail.iso == "2026-08-20T01:15:00+00:00"
    assert detail.confidence == 1.0
    assert detail.detected_by == "json_ld"


def test_meta_wins_over_visible_text():
    page = (
        '<html><head><meta name="pubdate" content="2026-08-21T08:00:00+08:00"></head>'
        "<body><p>2026-08-22 11:30 报道</p></body></html>"
    )
    detail = parse_detail_time(page, clock=CLOCK)
    assert detail is not None
    assert detail.iso == "2026-08-21T00:00:00+00:00"
    assert detail.detected_by == "meta"


def test_visible_text_fallback_is_limited_and_downgraded():
    page = "<html><body><h1>标题</h1><p>2026年8月22日 11:30 报道</p></body></html>"
    detail = parse_detail_time(page, clock=CLOCK)
    assert detail is not None
    assert detail.iso == "2026-08-22T03:30:00+00:00"
    assert detail.confidence == 0.85
    assert detail.detected_by == "visible_text"


def test_naive_time_assumes_cst():
    page = '<html><head><meta property="article:published_time" content="2026-08-23 10:20:00"></head><body></body></html>'
    detail = parse_detail_time(page, clock=CLOCK)
    assert detail is not None
    assert detail.iso == "2026-08-23T02:20:00+00:00"


def test_rejects_future_beyond_tolerance():
    page = '<html><head><meta property="article:published_time" content="2026-09-20T00:00:00+08:00"></head><body></body></html>'
    assert parse_detail_time(page, clock=CLOCK) is None


def test_rejects_pre_2000_dates():
    page = '<html><head><meta property="article:published_time" content="1999-01-01T00:00:00+08:00"></head><body></body></html>'
    assert parse_detail_time(page, clock=CLOCK) is None


def test_rejects_side_bar_dates_beyond_head_region():
    """可见文本限幅：正文后部的推荐位日期不进入可见文本候选；
    脚本级兜底对多个不同日期宁缺毋滥，同样拒绝。"""
    head = "<p>正文没有日期。</p>" * 300
    page = (
        f"<html><body>{head}"
        "<p>2026-08-22 相关文章推荐</p><p>2026-08-25 更多阅读</p></body></html>"
    )
    assert parse_detail_time(page, clock=CLOCK) is None


def test_malformed_json_ld_is_ignored_gracefully():
    page = (
        "<html><head></head><body>"
        "<script type=\"application/ld+json\">{not json</script>"
        '<meta property="article:published_time" content="2026-08-21T08:00:00+08:00">'
        "</body></html>"
    )
    detail = parse_detail_time(page, clock=CLOCK)
    assert detail is not None
    assert detail.detected_by == "meta"


def test_embedded_script_unique_date_is_last_resort():
    """SPA 站点（南方周末实测）：无任何标记，发布时间只存在于状态脚本。"""
    page = (
        "<html><head><title>正文 JS 渲染的详情页</title></head><body>"
        "<div>可见文本没有任何日期。</div>"
        "<script>window.__STATE__={publishTime:'2026-08-23',articleId:329095}</script>"
        "</body></html>"
    )
    detail = parse_detail_time(page, clock=CLOCK)
    assert detail is not None
    assert detail.iso == "2026-08-22T16:00:00+00:00"
    assert detail.confidence == 0.85
    assert detail.detected_by == "embedded_script"


def test_embedded_script_multiple_dates_are_refused():
    """脚本里出现多个不同日期（相关推荐/侧栏状态）→ 宁缺毋滥。"""
    page = (
        "<html><body>"
        "<script>window.__STATE__={publishTime:'2026-08-23'}</script>"
        "<script>var related=[{time:'2026-08-20'},{time:'2026-08-21'}];</script>"
        "</body></html>"
    )
    assert parse_detail_time(page, clock=CLOCK) is None


def test_compact_publish_date_variable_cctv():
    """央视网 2026 版契约：var publishDate = "20260913112937 "（含尾随空格）。"""
    page = '<html><script>var publishDate = "20260913112937 ";</script></html>'
    detail = parse_detail_time(page, clock=CLOCK.replace(year=2027))
    assert detail is not None
    # 11:29:37 北京时间 → 03:29:37 UTC
    assert detail.iso == "2026-09-13T03:29:37+00:00"
    assert detail.confidence == 1.0
    assert detail.detected_by == "embedded_variable"


def test_compact_publish_date_invalid_month_falls_through():
    page = '<html><script>var publishDate = "20261313112937";</script></html>'
    assert parse_detail_time(page, clock=CLOCK.replace(year=2027)) is None
