from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Callable


class PaginationError(ValueError):
    code = "INVALID_CURSOR"


class CursorExpired(PaginationError):
    code = "CURSOR_EXPIRED"


class CursorPlatformMismatch(PaginationError):
    code = "CURSOR_PLATFORM_MISMATCH"


class CursorRequiresSinglePlatform(PaginationError):
    code = "CURSOR_REQUIRES_SINGLE_PLATFORM"


@dataclass(frozen=True)
class PageRequest:
    continuation: str | None = None


def request_fingerprint(request: Any) -> str:
    payload = {
        "query": request.query,
        "published_after": request.published_after.isoformat() if request.published_after else None,
        "published_before": request.published_before.isoformat() if request.published_before else None,
        "include_content": request.include_content,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class CursorCodec:
    def __init__(self, secret: str | bytes, *, ttl_seconds: int = 900, clock: Callable[[], float] = time.time):
        if not secret:
            raise ValueError("cursor secret is required")
        self.secret = secret.encode() if isinstance(secret, str) else secret
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    def encode(self, *, platform: str, fingerprint: str, continuation: str, adapter_version: str) -> str:
        now = int(self.clock())
        payload = {"v": 1, "platform": platform, "fingerprint": fingerprint, "continuation": continuation, "adapter_version": adapter_version, "iat": now, "exp": now + self.ttl_seconds}
        body = self._encode_part(payload)
        signature = hmac.new(self.secret, body.encode(), hashlib.sha256).digest()
        return f"{body}.{self._b64(signature)}"

    def decode(self, token: str, *, platform: str, fingerprint: str, adapter_version: str) -> PageRequest:
        try:
            body, supplied = token.split(".", 1)
            expected = hmac.new(self.secret, body.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(self._unb64(supplied), expected):
                raise PaginationError("invalid cursor signature")
            payload = json.loads(self._unb64(body))
            if payload.get("v") != 1 or payload.get("platform") != platform:
                raise CursorPlatformMismatch("cursor platform does not match request")
            if payload.get("fingerprint") != fingerprint or payload.get("adapter_version") != adapter_version:
                raise PaginationError("cursor does not match request")
            if not payload.get("continuation"):
                raise PaginationError("cursor continuation is missing")
            if int(self.clock()) > int(payload.get("exp", 0)):
                raise CursorExpired("cursor has expired")
            return PageRequest(str(payload["continuation"]))
        except PaginationError:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError) as exc:
            raise PaginationError("malformed cursor") from exc

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    @classmethod
    def _encode_part(cls, value: Any) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return cls._b64(raw)

    @staticmethod
    def _unb64(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
