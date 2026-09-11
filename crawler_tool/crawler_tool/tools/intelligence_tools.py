from __future__ import annotations

from crawler_tool.application import CrawlService
from crawler_tool.domain import ContentSearchRequest, ContentSearchResponse, SourceHealthResponse, WechatKeywordSearchRequest


class IntelligenceTools:
    """Minimal, stable crawler Tool facade for the self-designed Agent."""

    def __init__(self, crawl_service: CrawlService, wechat_search_service=None):
        self.crawl_service = crawl_service
        self.wechat_search_service = wechat_search_service or getattr(crawl_service, "wechat_search_service", None)

    def search_wechat_articles(self, request: WechatKeywordSearchRequest) -> ContentSearchResponse:
        if self.wechat_search_service is None:
            from crawler_tool.application.wechat_search_service import WechatKeywordSearchService
            return WechatKeywordSearchService(lambda: None, enabled=False).search(request)
        return self.wechat_search_service.search(request)

    def search_content(self, request: ContentSearchRequest) -> ContentSearchResponse:
        return self.crawl_service.search(request)

    def ingest_capture(self, request):
        service = getattr(self.crawl_service, "capture_ingest_service", None)
        if service is None:
            from crawler_tool.application.capture_ingest_service import CaptureIngestService
            service = CaptureIngestService()
        return service.ingest(request)

    def get_source_health(self) -> SourceHealthResponse:
        sources = list(self.crawl_service.registry.health())
        wechat = getattr(self.crawl_service, "wechat_search_service", None)
        if wechat is not None:
            sources.append(wechat.health())
        return SourceHealthResponse(sources=sources)
