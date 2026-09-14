"""详情页正文提取：三级优先级、噪声过滤、长度上限。"""

from pathlib import Path

from crawler_tool.infrastructure.detail_content import parse_detail_content

FIXTURES = Path(__file__).parent / "fixtures"


def test_cctv_embedded_variable_content():
    page = (FIXTURES / "cctv_contents_detail.html").read_text(encoding="utf-8")
    result = parse_detail_content(page)
    assert result is not None and result.detected_by == "embedded_variable"
    assert "文旅新动能" in result.text
    assert "奔县游" in result.text
    # 实体已解码、标签已剥离、段落按行分隔
    assert "&ldquo;" not in result.text and "<p>" not in result.text
    assert result.text.count("\n") >= 1
    # 相关阅读的干扰日期不进入正文
    assert "相关阅读" not in result.text


def test_json_ld_article_body_wins():
    page = (
        '<html><script type="application/ld+json">'
        '{"@type":"NewsArticle","articleBody":"这是一段足够长的正文内容，用来越过最小长度过滤阈值，'
        "包含充分的事实细节与背景介绍，确保超过四十字符的合格线。\"}"
        "</script><body><div class=\"content_area\"><p>容器文本远远长于阈值，但 JSON-LD 优先级更高不应被采用。</p></div></body></html>"
    )
    result = parse_detail_content(page)
    assert result is not None and result.detected_by == "json_ld"
    assert "JSON-LD" not in result.text


def test_visible_article_container_fallback():
    page = (FIXTURES / "infzm_contents_detail.html").read_text(encoding="utf-8")
    # fixture 的段落不足 _MIN_CHARS，扩写一份足够长的正文
    page = page.replace(
        "据报道，该产品于发布会上正式亮相。",
        "据报道，该产品于发布会上正式亮相，现场演示了多轮对话与工单自动流转能力，" * 3,
    )
    result = parse_detail_content(page)
    assert result is not None and result.detected_by == "visible_article"
    assert "正式亮相" in result.text


def test_spa_page_without_signals_returns_none():
    page = "<html><head><title>空页面</title></head><body><div id='app'></div><p>太短</p></body></html>"
    assert parse_detail_content(page) is None


def test_short_embedded_variable_rejected():
    page = "<html><script>var contentdate = '<p>太短的正文</p>';</script></html>"
    assert parse_detail_content(page) is None


def test_long_content_capped():
    paragraph = "<p>" + "正文内容非常长。" * 4000 + "</p>"
    result = parse_detail_content(f"<html><script>var contentdate = '{paragraph}';</script></html>")
    assert result is not None
    assert len(result.text) <= 20000
