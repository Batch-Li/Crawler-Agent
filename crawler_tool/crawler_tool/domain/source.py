from __future__ import annotations

from enum import StrEnum


class SourceStatus(StrEnum):
    SUCCESS = "success"
    EMPTY = "empty"
    AUTHENTICATION_REQUIRED = "authentication_required"
    RATE_LIMITED = "rate_limited"
    PARSER_CHANGED = "parser_changed"
    NETWORK_ERROR = "network_error"
    SOURCE_UNAVAILABLE = "source_unavailable"
    FAILED = "failed"


class SourceErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    PARSER_CHANGED = "PARSER_CHANGED"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
