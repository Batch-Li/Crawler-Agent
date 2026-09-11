from __future__ import annotations

import re
from typing import Any

from crawler_tool.application.recent_store import RecentItemsStore
from crawler_tool.domain import ContentSearchResponse, SourceReport, SourceStatus, WechatKeywordSearchRequest
from crawler_tool.normalization.content_normalizer import normalize_raw_item
from crawler_tool.sources.base import RawItem
from crawler_tool.sources.wechat_authorized import WechatAuthorizedListAdapter, _clean


class WechatKeywordSearchService:
    def __init__(
        self,
        adapter_factory,
        *,
        enabled: bool = False,
        store: RecentItemsStore | None = None,
        credential_configured: bool = False,
    ) -> None:
        self.adapter_factory = adapter_factory
        self.enabled = enabled
        self.store = store if store is not None else RecentItemsStore()
        self.credential_configured = credential_configured

    def health(self) -> dict[str, Any]:
        # credentialStatus reflects configuration only, never cookie validity.
        if not self.enabled:
            credential = "disabled"
        elif self.credential_configured:
            credential = "configured"
        else:
            credential = "unavailable"
        return {
            "platform": "wechat",
            "status": "unknown",
            "adapter_version": "0.1.0-authorized",
            "capabilities": ["search"] if self.enabled and self.credential_configured else [],
            "authMode": "authorized_first_page" if self.enabled else "disabled",
            "credentialStatus": credential,
            "pagination": "disabled",
            "detail": "disabled",
        }

    def search(self, request: WechatKeywordSearchRequest) -> ContentSearchResponse:
        request_id = f"req_{id(request):x}"
        accounts = request.normalized_accounts()
        if not self.enabled:
            return self._response(request_id, [], [SourceReport(platform="wechat", status="source_unavailable", error_code="SOURCE_UNAVAILABLE", retryable=False, message="Wechat keyword search is disabled")], True)
        adapter: WechatAuthorizedListAdapter = self.adapter_factory()
        try:
            token, error = adapter.get_token()
            if error:
                return self._response(request_id, [], [self._report("wechat", error)], True)
            items = []
            reports = []
            for account_name in accounts:
                account, error = adapter.search_account(account_name, token)
                if error:
                    reports.append(self._report("wechat", error))
                    continue
                fakeid = account.get("fakeid") if account else None
                if not fakeid:
                    reports.append(SourceReport(platform="wechat", status="parser_changed", error_code="PARSER_CHANGED", retryable=False, message="Wechat account identifier is missing"))
                    continue
                records, error = adapter.list_articles(str(fakeid), token, request.limit)
                if error:
                    reports.append(self._report("wechat", error))
                    continue
                matched = self._matched_raw_items(records or [], account.get("nickname") or account_name, request.keyword, adapter)
                normalized = []
                for raw in matched:
                    try:
                        normalized.append(normalize_raw_item(raw, query=request.keyword, trace_id=request_id))
                    except Exception:
                        continue
                items.extend(normalized)
                reports.append(SourceReport(platform="wechat", status="success" if normalized else "empty", count=len(normalized)))
            deduped = []
            seen = set()
            for item in items:
                if item.dedup.dedup_key not in seen:
                    seen.add(item.dedup.dedup_key)
                    deduped.append(item)
            if items:
                self.store.add(items)
            failed = any(report.status not in {"success", "empty"} for report in reports)
            return self._response(request_id, deduped[:request.limit], reports, failed)
        finally:
            adapter.close()

    def _matched_raw_items(self, records: list[dict[str, Any]], account_name: str, keyword: str, adapter: WechatAuthorizedListAdapter) -> list[RawItem]:
        needle = _match_text(keyword)
        result = adapter.parse_listing({"app_msg_list": records}, account_name=account_name)
        matched = []
        for raw, record in zip(result.items, records):
            fields = [name for name in ("title", "digest", "summary") if needle in _match_text(record.get(name) or "")]
            if fields:
                raw.payload["ext"]["keywordMatch"] = {"fields": fields}
                matched.append(raw)
        return matched

    @staticmethod
    def _report(platform: str, result) -> SourceReport:
        return SourceReport(platform=platform, status=result.status.value, count=0, error_code=result.error_code, retryable=result.retryable, message=result.message, diagnostics=result.response_metadata)

    @staticmethod
    def _response(request_id: str, items: list[Any], reports: list[SourceReport], failed: bool) -> ContentSearchResponse:
        return ContentSearchResponse(request_id=request_id, status="partial" if failed and items else ("failed" if failed else "success"), partial=failed, items=items, source_reports=reports)


def _match_text(value: Any) -> str:
    return re.sub(r"\s+", "", _clean(value) or "").casefold()
