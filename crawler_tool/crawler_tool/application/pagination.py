"""Compatibility re-exports for the canonical pagination protocol."""

from crawler_tool.pagination import (
    CursorCodec,
    CursorExpired,
    CursorPlatformMismatch,
    CursorRequiresSinglePlatform,
    PageRequest,
    PaginationError,
    request_fingerprint,
)

__all__ = [
    "CursorCodec",
    "CursorExpired",
    "CursorPlatformMismatch",
    "CursorRequiresSinglePlatform",
    "PageRequest",
    "PaginationError",
    "request_fingerprint",
]
