"""会话库升级：同 content_id 的库存全文条目替换实时摘要条目，进入证据流。"""

from crawler_tool.application import CrawlService
from crawler_tool.domain import ContentSearchRequest, SourceStatus
from crawler_tool.normalization import normalize_raw_item
from crawler_tool.sources import RawItem, SourceRegistry
from crawler_tool.sources.base import AdapterResult, SourceAdapter

FULL_BODY = "浪潮信息副总裁刘军接受中新网记者采访，介绍了智能化工大模型的架构与落地进展。" * 4
SNIPPET = "全球最大规模AI巨量模型在京发布，未来进行开源共享"


def _raw(item_id: str, *, full: bool) -> RawItem:
    content = FULL_BODY if full else SNIPPET
    payload = {
        "sourceItemId": item_id,
        "sourceType": "news_media",
        "contentType": "article",
        "title": "全球最大规模AI巨量模型在京发布",
        "summary": content[:40],
        "content": content,
        "url": f"https://www.toutiao.com/a{item_id}/?channel=",
        "method": "http",
        "hasFullContent": full,
    }
    if not full:
        payload["warnings"] = ["search_result_summary_only"]
    return RawItem(platform="toutiao", payload=payload)


class _StubAdapter(SourceAdapter):
    platform = "toutiao"
    adapter_version = "0.0.0-fake"

    def __init__(self, items: list[RawItem]) -> None:
        self.items = items

    def search(self, request: ContentSearchRequest) -> AdapterResult:
        return AdapterResult(items=self.items, status=SourceStatus.SUCCESS)


def _service(items: list[RawItem]) -> CrawlService:
    return CrawlService(SourceRegistry({"toutiao": lambda: _StubAdapter(items)}))


def _request() -> ContentSearchRequest:
    return ContentSearchRequest.model_validate({"query": "AI 大模型", "platforms": ["toutiao"], "limit": 10})


def test_store_full_content_upgrades_live_snippet():
    live_raw = _raw("7012981260557484574", full=False)
    full = normalize_raw_item(_raw("7012981260557484574", full=True), query="AI 大模型")
    live = normalize_raw_item(live_raw, query="AI 大模型")
    assert live.content_id == full.content_id  # _source_id 修复后两侧同 ID
    service = _service([live_raw])
    service.recent_store.add([full])

    response = service.search(_request())

    assert response.status == "success"
    assert len(response.items) == 1
    assert response.items[0].content_id == full.content_id
    assert response.items[0].content == FULL_BODY
    assert response.items[0].quality.has_full_content is True


def test_without_store_copy_snippet_passes_through():
    live_raw = _raw("7012981260557484574", full=False)
    service = _service([live_raw])
    response = service.search(_request())
    assert response.items[0].content == SNIPPET
    assert response.items[0].quality.has_full_content is False

def test_store_add_does_not_downgrade_full_content():
    """库层不降级：同 ID 摘要捕获不得覆盖已回填的全文条目。"""
    from crawler_tool.application.recent_store import RecentItemsStore

    store = RecentItemsStore()
    full = normalize_raw_item(_raw("7012981260557484574", full=True), query="q")
    snippet = normalize_raw_item(_raw("7012981260557484574", full=False), query="q")
    store.add([full])
    store.add([snippet])
    stored = store.get(full.content_id)
    assert stored.quality.has_full_content is True
    assert stored.content == FULL_BODY
