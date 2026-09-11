from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from crawler_tool.domain import ContentItem
from crawler_tool.sources.base import RawItem


_KNOWN_PAYLOAD_FIELDS = {
    "contentId", "sourceItemId", "source_item_id", "sourceType", "contentType",
    "title", "content", "summary", "language", "url", "canonicalUrl", "author",
    "publishedAt", "metrics", "media", "taskId", "method", "adapterVersion",
    "qualityStatus", "hasFullContent", "publishedAtConfidence", "warnings", "ext",
}
_TRACKING_KEYS = {"spm", "from", "source", "refer", "ref", "share_token"}


def _hash(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def _stable_id(value: str) -> str:
    return "cnt_" + sha256(value.encode("utf-8")).hexdigest()[:24]


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, urlencode(query), ""))


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def normalize_raw_item(
    raw: RawItem,
    *,
    query: str | None = None,
    trace_id: str | None = None,
    collected_at: datetime | None = None,
) -> ContentItem:
    """Normalize platform data into the strict content.v1 contract.

    Adapters map source fields to the documented payload keys. Any remaining
    platform-specific fields are retained in ``ext`` rather than silently lost.
    """
    payload: dict[str, Any] = raw.payload
    source_item_id = payload.get("sourceItemId") or payload.get("source_item_id")
    url = payload.get("url")
    if not url:
        raise ValueError("raw item is missing url")
    canonical_url = canonicalize_url(payload.get("canonicalUrl") or url)
    dedup_key = f"{raw.platform}:{source_item_id}" if source_item_id else _hash(f"{raw.platform}:{canonical_url}")
    title = _clean_text(payload.get("title"))
    content = _clean_text(payload.get("content"))
    warnings = list(payload.get("warnings") or [])
    if not content:
        warnings.append("content_missing")
    if not payload.get("publishedAt"):
        warnings.append("published_at_missing")
    content_hash = _hash(f"{title or ''}\n{content or ''}\n{canonical_url}")
    now = collected_at or datetime.now(timezone.utc)
    unknown_fields = {key: value for key, value in payload.items() if key not in _KNOWN_PAYLOAD_FIELDS}
    ext = {**unknown_fields, **(payload.get("ext") or {})}
    return ContentItem.model_validate(
        {
            "schemaVersion": "content.v1",
            "contentId": payload.get("contentId") or _stable_id(dedup_key),
            "platform": raw.platform,
            "sourceType": payload.get("sourceType", "other"),
            "sourceItemId": source_item_id,
            "contentType": payload.get("contentType", "other"),
            "title": title,
            "content": content,
            "summary": _clean_text(payload.get("summary")),
            "language": payload.get("language", "zh-CN"),
            "url": url,
            "canonicalUrl": canonical_url,
            "author": payload.get("author") or {},
            "publishedAt": payload.get("publishedAt"),
            "collectedAt": now,
            "metrics": payload.get("metrics") or {},
            "media": payload.get("media") or [],
            "collection": {
                "taskId": payload.get("taskId"),
                "query": query,
                "method": payload.get("method", "other"),
                "adapter": raw.platform,
                "adapterVersion": payload.get("adapterVersion", "0.1.0"),
                "traceId": trace_id or _stable_id(f"trace:{now.isoformat()}:{dedup_key}"),
            },
            "dedup": {"dedupKey": dedup_key, "contentHash": content_hash},
            "quality": {
                "status": payload.get("qualityStatus", "success"),
                "hasFullContent": bool(payload.get("hasFullContent", content is not None)),
                "publishedAtConfidence": float(payload.get("publishedAtConfidence", 1.0 if payload.get("publishedAt") else 0.0)),
                "warnings": warnings,
            },
            "rawDataRef": raw.raw_data_ref,
            "ext": ext,
        }
    )
