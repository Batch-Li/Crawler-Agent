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


def test_south_weekend_detail_is_explicitly_unsupported_for_now():
    result = SouthWeekendAdapter().fetch_detail("https://www.infzm.com/contents/317022")
    assert result.status is SourceStatus.SOURCE_UNAVAILABLE
