from __future__ import annotations

import argparse
import json
import os
from typing import Any

from crawler_tool.application import CrawlService
from crawler_tool.application.capture_ingest_service import CaptureIngestService
from crawler_tool.application.time_enrichment import TimeEnrichmentService
from crawler_tool.application.wechat_search_service import WechatKeywordSearchService
from crawler_tool.pagination import CursorCodec
from crawler_tool.domain import ContentSearchRequest, WechatKeywordSearchRequest
from crawler_tool.infrastructure import bind_http_get
from crawler_tool.infrastructure.http_client import HttpClient
from crawler_tool.sources import SourceRegistry
from crawler_tool.sources.south_weekend import SouthWeekendAdapter
from crawler_tool.sources.cctv_news import CctvNewsAdapter
from crawler_tool.sources.sogou_wechat import SogouWechatAdapter
from crawler_tool.sources.toutiao import ToutiaoAdapter
from crawler_tool.sources.weibo import WeiboAdapter
from crawler_tool.sources.weibo_authorized import WeiboAuthorizedAdapter
from crawler_tool.sources.xiaohongshu import XiaohongshuAdapter
from crawler_tool.sources.xiaohongshu_authorized import XiaohongshuAuthorizedAdapter
from crawler_tool.sources.wechat_authorized import WechatAuthorizedListAdapter
from crawler_tool.validation import (
    LiveValidationError,
    ValidationConfig,
    run_south_weekend_pagination_evidence,
    run_toutiao_two_page_smoke,
    run_weibo_anonymous_smoke,
    run_xiaohongshu_first_page_smoke,
)


def make_service(*, wayback_fallback: bool = False) -> CrawlService:
    """Build the focused Tool MVP with real public search sources enabled."""
    secret = os.environ.get("CRAWLER_CURSOR_SECRET")
    weibo_mode = os.environ.get("WEIBO_MODE", "anonymous")
    if weibo_mode == "legacy_authorized":
        cookie_header = os.environ.get("WEIBO_SESSION_COOKIE", "")
        def factory() -> WeiboAuthorizedAdapter:
            client = HttpClient(follow_redirects=False)
            adapter = WeiboAuthorizedAdapter(http_get=client.get, cookie_header=cookie_header)
            original_close = adapter.close
            def close() -> None:
                try:
                    original_close()
                finally:
                    client.close()
            adapter.close = close
            return adapter
        weibo_factory = factory
    else:
        weibo_factory = bind_http_get(WeiboAdapter)
    xhs_mode = os.environ.get("XHS_MODE", "disabled")
    if xhs_mode == "authorized_first_page":
        xhs_cookie_header = os.environ.get("XHS_SESSION_COOKIE", "")
        def xhs_factory() -> XiaohongshuAuthorizedAdapter:
            client = HttpClient(follow_redirects=False)
            adapter = XiaohongshuAuthorizedAdapter(http_post=client.post, cookie_header=xhs_cookie_header)
            original_close = adapter.close
            def close() -> None:
                try:
                    original_close()
                finally:
                    client.close()
            adapter.close = close
            return adapter
    else:
        def xhs_factory() -> XiaohongshuAdapter:
            return XiaohongshuAdapter(mode=xhs_mode if xhs_mode in ("disabled", "anonymous") else "disabled")
    # 搜狗微信公开索引：默认启用（2026-09 实测稳定；SOGOU_WECHAT_MODE=disabled 可显式关闭）。
    # 即便启用，仍须在请求里指名 sogou_wechat，绝不进入默认联搜。包装链接不解析，验证码即停不重试。
    sogou_mode = os.environ.get("SOGOU_WECHAT_MODE", "anonymous_best_effort")
    if sogou_mode == "anonymous_best_effort":
        def sogou_factory() -> SogouWechatAdapter:
            client = HttpClient(follow_redirects=False)
            adapter = SogouWechatAdapter(http_get=client.get, mode="anonymous_best_effort")
            original_close = adapter.close
            def close() -> None:
                try:
                    original_close()
                finally:
                    client.close()
            adapter.close = close
            return adapter
    else:
        def sogou_factory() -> SogouWechatAdapter:
            return SogouWechatAdapter(mode="disabled")
    registry = SourceRegistry({
        "south_weekend": bind_http_get(SouthWeekendAdapter),
        "toutiao": bind_http_get(ToutiaoAdapter),
        "weibo": weibo_factory,
        "xiaohongshu": xhs_factory,
        "sogou_wechat": sogou_factory,
        # 央视网官方媒体列表：无凭据无风控；灰度期仅显式指定可用，暂不入默认联搜。
        "cctv_news": bind_http_get(CctvNewsAdapter),
    }, default_platforms=("south_weekend", "toutiao"))
    crawl_service = CrawlService(
        registry,
        cursor_codec=CursorCodec(secret) if secret else None,
        # Wayback 第二级兜底默认关闭；--wayback-fallback 才注入传输。
        time_enrichment=TimeEnrichmentService(
            registry, wayback_http_get=HttpClient().get,
        ) if wayback_fallback else None,
    )
    shared_history = crawl_service.recent_store
    wechat_enabled = os.environ.get("WECHAT_MODE") == "authorized_first_page"
    wechat_cookie = os.environ.get("WECHAT_SESSION_COOKIE", "")
    wechat_service = WechatKeywordSearchService(
        lambda: WechatAuthorizedListAdapter(
            http_get=HttpClient(follow_redirects=False).get,
            cookie_header=wechat_cookie,
        ),
        enabled=wechat_enabled,
        store=shared_history,
        credential_configured=bool(wechat_cookie.strip()),
    )
    crawl_service.wechat_search_service = wechat_service
    # Manual capture ingestion never touches the network; it feeds the same session view.
    crawl_service.capture_ingest_service = CaptureIngestService(store=shared_history)
    return crawl_service


def run_smoke(query: str, limit: int) -> dict[str, Any]:
    response = make_service().search(ContentSearchRequest(
        query=query,
        platforms=["south_weekend"],
        limit=min(limit, 10),
        freshness="prefer_fresh",
    ))
    return {
        "status": response.status,
        "partial": response.partial,
        "items": [
            {
                "contentId": item.content_id,
                "title": item.title,
                "url": str(item.canonical_url or item.url),
                "publishedAt": item.published_at.isoformat() if item.published_at else None,
                "hasFullContent": item.quality.has_full_content,
                "warnings": item.quality.warnings,
            }
            for item in response.items
        ],
        "sourceReports": [report.model_dump(by_alias=True) for report in response.source_reports],
    }


app = None


def main() -> None:
    parser = argparse.ArgumentParser(description="SignalX crawler Tool MVP")
    parser.add_argument("--smoke", metavar="QUERY", help="Run one South Weekend search and print public output")
    parser.add_argument("--live-smoke-toutiao", metavar="QUERY", help="Run a gated real two-page Toutiao validation")
    parser.add_argument("--live-smoke-weibo", metavar="QUERY", help="Run a gated real anonymous Weibo first-page validation")
    parser.add_argument("--live-smoke-xhs", metavar="QUERY", help="Run a gated real Xiaohongshu first-page validation")
    parser.add_argument("--verify-south-weekend-pagination", metavar="QUERY", help="Run a gated South Weekend pagination evidence check")
    parser.add_argument("--online", action="store_true", help="Allow a real validation network request")
    parser.add_argument("--confirm-real", action="store_true", help="Confirm the limited real validation request")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--wayback-fallback", action="store_true",
                        help="启用 Wayback CDX 首次收录时间兜底（默认关闭，详情页无果后逐条 1 次请求）")
    parser.add_argument("--serve", action="store_true", help="Run local HTTP Tool API on 127.0.0.1:8301")
    args = parser.parse_args()

    if args.live_smoke_toutiao or args.verify_south_weekend_pagination or args.live_smoke_weibo or args.live_smoke_xhs:
        query = args.live_smoke_toutiao or args.verify_south_weekend_pagination or args.live_smoke_weibo or args.live_smoke_xhs
        config = ValidationConfig(query=query, allow_network=args.online, confirm_real=args.confirm_real)
        try:
            if args.live_smoke_toutiao:
                report, code = run_toutiao_two_page_smoke(config)
            elif args.live_smoke_weibo:
                report, code = run_weibo_anonymous_smoke(config)
            elif args.live_smoke_xhs:
                report, code = run_xiaohongshu_first_page_smoke(config)
            else:
                report, code = run_south_weekend_pagination_evidence(config)
        except LiveValidationError:
            print(json.dumps({"verification": "real_source_validation", "outcome": "blocked"}, ensure_ascii=False))
            raise SystemExit(2)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(code)
    if args.smoke:
        print(json.dumps(run_smoke(args.smoke, args.limit), ensure_ascii=False, indent=2))
        return
    if args.serve:
        import uvicorn
        from functools import partial

        from crawler_tool.interfaces import create_app

        uvicorn.run(create_app(partial(make_service, wayback_fallback=args.wayback_fallback)),
                    host="127.0.0.1", port=8301)
        return
    parser.print_help()


if __name__ == "__main__":
    main()
