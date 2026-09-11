from datetime import datetime, timezone

from crawler_tool.domain import ContentItem
from crawler_tool.normalization import normalize_raw_item
from crawler_tool.sources import RawItem


def test_normalize_content_v1():
    item = normalize_raw_item(
        RawItem(
            platform="weibo",
            raw_data_ref="raw/weibo/example.json",
            payload={
                "sourceItemId": "4987654321",
                "sourceType": "social_media",
                "contentType": "post",
                "title": "某公司发布新款 AI 客服产品",
                "content": "完整正文",
                "url": "https://weibo.com/xxx/4987654321",
                "publishedAt": "2026-08-23T10:20:00+08:00",
                "method": "api",
            },
        ),
        query="AI客服",
        trace_id="crawl_test_001",
        collected_at=datetime(2026, 8, 23, 2, 25, tzinfo=timezone.utc),
    )
    assert isinstance(item, ContentItem)
    assert item.schema_version == "content.v1"
    assert item.dedup.dedup_key == "weibo:4987654321"
    assert item.collection.trace_id == "crawl_test_001"


def test_content_item_preserves_unmapped_raw_fields_in_ext():
    raw = RawItem(
        platform="weibo",
        payload={
            "sourceType": "social_media",
            "contentType": "post",
            "title": "title",
            "url": "https://example.com/post/1",
            "unexpected": "must not be silently ignored",
        },
    )
    item = normalize_raw_item(raw)
    assert item.ext["unexpected"] == "must not be silently ignored"
