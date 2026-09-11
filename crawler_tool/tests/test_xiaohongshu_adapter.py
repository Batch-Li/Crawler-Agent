import json
import pathlib

from crawler_tool.domain import SourceStatus
from crawler_tool.sources.xiaohongshu import XiaohongshuAdapter


ROOT = pathlib.Path(__file__).parent / "fixtures"


def test_xiaohongshu_parser_maps_note_and_skips_rec_query():
    payload = json.loads((ROOT / "xiaohongshu_search_success.json").read_text(encoding="utf-8"))
    result = XiaohongshuAdapter().parse_payload(payload)

    assert result.status is SourceStatus.SUCCESS
    assert len(result.items) == 1
    item = result.items[0].payload
    assert item["sourceItemId"] == "note-001"
    assert item["url"] == "https://www.xiaohongshu.com/explore/note-001"
    assert item["content"] is None
    assert item["hasFullContent"] is False
    assert item["metrics"]["likeCount"] == 12


def test_xiaohongshu_disabled_mode_makes_no_network_request():
    calls = []
    adapter = XiaohongshuAdapter(http_post=lambda *args, **kwargs: calls.append(1), mode="disabled")
    result = adapter.search(type("Request", (), {"query": "AI"})())

    assert result.status is SourceStatus.SOURCE_UNAVAILABLE
    assert calls == []


def test_xiaohongshu_schema_change_is_not_empty():
    result = XiaohongshuAdapter().parse_payload({"code": 0, "data": {}})

    assert result.status is SourceStatus.PARSER_CHANGED
