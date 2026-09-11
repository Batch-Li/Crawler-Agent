from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from crawler_tool.domain import SourceErrorCode, SourceStatus
from crawler_tool.sources.base import AdapterResult, RawItem, SourceAdapter


class DouyinAdapter(SourceAdapter):
    """Parser-only Douyin adapter; data enters via manual capture ingestion.

    No network methods are implemented: the Douyin web surface requires
    dynamic signatures for automated access. This parser maps manually
    captured public responses onto ContentItem payloads.
    """

    platform = "douyin"
    adapter_version = "0.1.0-parser"

    def health(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": "unknown",
            "adapter_version": self.adapter_version,
            "capabilities": [],
            "authMode": "manual_capture",
            "credentialStatus": "not_required",
            "pagination": "disabled",
            "detail": "disabled",
        }

    def search(self, request: Any) -> AdapterResult:
        # Douyin web/API access requires dynamic signatures; production
        # network search stays disabled. Data enters via manual capture only.
        return AdapterResult(
            status=SourceStatus.SOURCE_UNAVAILABLE,
            error_code=SourceErrorCode.SOURCE_UNAVAILABLE,
            message="Douyin network search is disabled; use manual capture ingestion",
            retryable=False,
        )

    def parse_payload(self, payload: Any) -> AdapterResult:
        if not isinstance(payload, dict):
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Douyin response is not an object", retryable=False)
        records = _extract_records(payload)
        if records is None:
            return AdapterResult(status=SourceStatus.PARSER_CHANGED, error_code=SourceErrorCode.PARSER_CHANGED, message="Douyin capture does not match a known envelope", retryable=False)
        items: list[RawItem] = []
        for record in records:
            item = _parse_aweme(record)
            if item is not None:
                items.append(RawItem(platform=self.platform, payload=item))
        return AdapterResult(items=items, status=SourceStatus.SUCCESS if items else SourceStatus.EMPTY)


def _extract_records(payload: dict[str, Any]) -> list[Any] | None:
    """Recognize the documented response envelopes without guessing."""
    if isinstance(payload.get("aweme_list"), list):
        return payload["aweme_list"]
    data = payload.get("data")
    if isinstance(data, list) and any(isinstance(entry, dict) and ("aweme_info" in entry or "aweme_id" in entry) for entry in data):
        flattened = []
        for entry in data:
            if isinstance(entry, dict):
                inner = entry.get("aweme_info")
                if isinstance(inner, dict):
                    flattened.append(inner)
                elif "aweme_id" in entry:
                    flattened.append(entry)
        return flattened
    if isinstance(data, dict) and isinstance(data.get("aweme_details"), list):
        return [detail for detail in data["aweme_details"] if isinstance(detail, dict)]
    return None


def _parse_aweme(record: Any) -> dict[str, Any] | None:
    """Map one aweme record; returns None for non-video or malformed entries."""
    if not isinstance(record, dict):
        return None
    aweme_id = str(record.get("aweme_id") or "").strip()
    title = _clean_text(record.get("desc"))
    url = f"https://www.douyin.com/video/{aweme_id}" if aweme_id else None
    if not aweme_id or not title or not url:
        return None
    author = record.get("author") if isinstance(record.get("author"), dict) else {}
    statistics = record.get("statistics") if isinstance(record.get("statistics"), dict) else {}
    published_raw = record.get("create_time")
    video = record.get("video") if isinstance(record.get("video"), dict) else {}
    play_url = video.get("play_addr_lowbr") if isinstance(video.get("play_addr_lowbr"), dict) else video.get("play_addr")
    media_url = play_url.get("url_list", [None])[0] if isinstance(play_url, dict) and play_url.get("url_list") else None
    item = {
        "sourceItemId": aweme_id,
        "sourceType": "social_media",
        "contentType": "video",
        "title": title,
        "summary": title,
        # Deliberately canonical: share URLs carry volatile tracking params.
        "url": url,
        "author": {"name": _clean_text(author.get("nickname")), "id": str(author.get("sec_uid")) if author.get("sec_uid") else None},
        "publishedAt": _timestamp_seconds(published_raw),
        "metrics": {
            "likeCount": _integer(statistics.get("digg_count")),
            "commentCount": _integer(statistics.get("comment_count")),
            "shareCount": _integer(statistics.get("share_count")),
            "collectCount": _integer(statistics.get("collect_count")),
            "viewCount": _integer(statistics.get("play_count")),
        },
        "method": "http",
        "adapterVersion": DouyinAdapter.adapter_version,
        "hasFullContent": False,
        "publishedAtConfidence": 1.0 if _timestamp_seconds(published_raw) else 0.0,
        "warnings": ["search_result_summary_only", "content_missing"],
        "ext": {"publishedAtRaw": published_raw},
    }
    if media_url and re.match(r"^https://", str(media_url)):
        item["media"] = [{"type": "video", "url": media_url}]
    return item


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _timestamp_seconds(value: Any) -> str | None:
    try:
        number = int(value)
        return datetime.fromtimestamp(number, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
