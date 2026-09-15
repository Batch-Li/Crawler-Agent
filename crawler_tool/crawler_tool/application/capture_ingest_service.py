from __future__ import annotations

from typing import Any

from crawler_tool.application.recent_store import RecentItemsStore
from crawler_tool.domain import CaptureIngestRequest, ContentSearchResponse, SourceReport, SourceStatus
from crawler_tool.normalization.content_normalizer import normalize_raw_item
from crawler_tool.sources.douyin import DouyinAdapter
from crawler_tool.sources.toutiao import ToutiaoAdapter
from crawler_tool.sources.wechat_authorized import WechatAuthorizedListAdapter
from crawler_tool.sources.xiaohongshu import XiaohongshuAdapter


class CaptureIngestService:
    """Multi-platform manual-capture ingestion: parse saved public responses offline.

    This path never issues network requests. The operator captures data by
    hand in a browser and feeds the saved JSON (or forwards it with the
    capture helper script) to the Tool; platform parsers and the content.v1
    normalizer do the rest. Successful items flow into the shared recent-items
    session view; durable storage stays out of scope.
    """

    def __init__(
        self,
        parser_factories: dict[str, Any] | None = None,
        *,
        store: RecentItemsStore | None = None,
    ) -> None:
        self._parser_factories = parser_factories or {
            "xiaohongshu": XiaohongshuAdapter,
            "douyin": DouyinAdapter,
            "wechat": WechatAuthorizedListAdapter,
            "toutiao": ToutiaoAdapter,
        }
        self.store = store if store is not None else RecentItemsStore()

    def ingest(self, request: CaptureIngestRequest) -> ContentSearchResponse:
        request_id = f"req_{id(request):x}"
        platform = self._resolve_platform(request.platform, request.capture)
        if platform is None:
            report = SourceReport(
                platform="other",
                status="parser_changed",
                error_code="PARSER_CHANGED",
                retryable=False,
                message="capture does not match any supported platform envelope; set platform explicitly",
            )
            return self._response(request_id, [], [report], True)
        adapter = self._parser_factories[platform]()
        try:
            result = adapter.parse_payload(request.capture)
        finally:
            adapter.close()
        if result.error_code is not None:
            report = SourceReport(
                platform=platform,
                status=result.status.value,
                count=0,
                error_code=result.error_code.value if hasattr(result.error_code, "value") else result.error_code,
                retryable=result.retryable,
                message=result.message,
                diagnostics=result.response_metadata,
            )
            return self._response(request_id, [], [report], True)
        dropped = 0
        normalized = []
        seen: set[str] = set()
        for raw_item in result.items:
            marker = {"origin": "manual_capture", "platform": platform}
            if request.keyword:
                marker["keyword"] = request.keyword
            raw_item.payload.setdefault("ext", {})["captureIngest"] = marker
            try:
                item = normalize_raw_item(raw_item, query=request.keyword, trace_id=request_id)
            except Exception:
                dropped += 1
                continue
            if item.dedup.dedup_key in seen:
                continue
            seen.add(item.dedup.dedup_key)
            normalized.append(item)
        status = result.status
        if status == SourceStatus.SUCCESS and not normalized:
            status = SourceStatus.EMPTY
        if normalized:
            self.store.add(normalized)
        report = SourceReport(
            platform=platform,
            status=status.value,
            count=len(normalized),
            message=f"{dropped} captured record(s) failed normalization" if dropped else None,
        )
        failed = report.status not in {SourceStatus.SUCCESS.value, SourceStatus.EMPTY.value}
        return self._response(request_id, normalized, [report], failed)

    def _resolve_platform(self, explicit: str | None, capture: dict[str, Any]) -> str | None:
        if explicit is not None:
            return explicit if explicit in self._parser_factories else None
        return detect_capture_platform(capture)

    @staticmethod
    def _response(request_id: str, items: list, reports: list[SourceReport], failed: bool) -> ContentSearchResponse:
        return ContentSearchResponse(
            request_id=request_id,
            status="failed" if failed and not items else ("partial" if failed else "success"),
            partial=failed,
            items=items,
            source_reports=reports,
        )


def detect_capture_platform(capture: dict[str, Any]) -> str | None:
    """Identify a platform from documented envelope shapes only."""
    if not isinstance(capture, dict):
        return None
    if "aweme_list" in capture or _is_douyin_details(capture.get("data")):
        return "douyin"
    data = capture.get("data")
    if isinstance(data, list) and any(isinstance(entry, dict) and ("aweme_info" in entry or "aweme_id" in entry) for entry in data):
        return "douyin"
    # MP 后台发给登录者看的文章列表（appmsgpublish sub=list / appmsg
    # action=list_ex）共用 app_msg_list 信封。
    if "app_msg_list" in capture:
        return "wechat"
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return "xiaohongshu"
    # Xiaohongshu answers errors as a bare code-first object (e.g. business
    # failures) with no data section at all.
    code = capture.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        return "xiaohongshu"
    return None


def _is_douyin_details(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("aweme_details"), list)
