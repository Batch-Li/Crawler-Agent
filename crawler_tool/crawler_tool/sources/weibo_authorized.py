from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .weibo import WeiboAdapter


def parse_cookie_header(value: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in value.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if separator and name and cookie_value:
            cookies[name] = cookie_value
    return cookies


class WeiboCredentialError(ValueError):
    pass


class WeiboAuthorizedGet:
    """Small authorized-session transport; credentials never enter adapter payloads."""

    allowed_hosts = {"s.weibo.com", "weibo.com"}

    def __init__(self, http_get: Any, cookie_header: str):
        if not cookie_header.strip():
            raise WeiboCredentialError("Weibo authorized session is not configured")
        self.http_get = http_get
        self.cookies = parse_cookie_header(cookie_header)
        if not self.cookies:
            raise WeiboCredentialError("Weibo authorized session is invalid")

    def __call__(self, url: str, **kwargs: Any) -> Any:
        host = (urlparse(url).hostname or "").lower()
        if host not in self.allowed_hosts:
            raise WeiboCredentialError("Weibo authorized request target is not allowed")
        kwargs["follow_redirects"] = False
        kwargs["cookies"] = dict(self.cookies)
        return self.http_get(url, **kwargs)


class WeiboAuthorizedAdapter(WeiboAdapter):
    """Temporary authorized mode sharing the anonymous Weibo parser."""

    adapter_version = "0.1.0-authorized"

    def __init__(self, http_get: Any | None = None, *, cookie_header: str = "") -> None:
        self.credential_configured = bool(parse_cookie_header(cookie_header))
        super().__init__(
            http_get=WeiboAuthorizedGet(http_get, cookie_header)
            if http_get is not None and self.credential_configured
            else None
        )

    def search(self, request):
        if not self.credential_configured:
            from crawler_tool.domain import SourceErrorCode, SourceStatus
            from crawler_tool.sources.base import AdapterResult
            return AdapterResult(
                status=SourceStatus.SOURCE_UNAVAILABLE,
                error_code=SourceErrorCode.SOURCE_UNAVAILABLE,
                message="Weibo authorized session is not configured",
                retryable=False,
            )
        return super().search(request)

    def health(self) -> dict[str, Any]:
        report = super().health()
        report.update({
            "authMode": "legacy_authorized",
            "pagination": "disabled",
            "credentialStatus": "configured" if self.credential_configured else "unavailable",
        })
        return report
