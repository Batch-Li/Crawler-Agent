from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .xiaohongshu import XiaohongshuAdapter


def parse_cookie_header(value: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in value.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if separator and name and cookie_value:
            cookies[name] = cookie_value
    return cookies


class XhsCredentialError(ValueError):
    pass


class XhsAuthorizedPost:
    """Small authorized-session transport; the session never enters URLs or payloads."""

    allowed_hosts = {"edith.xiaohongshu.com"}

    def __init__(self, http_post: Any, cookie_header: str):
        if not cookie_header.strip():
            raise XhsCredentialError("Xiaohongshu authorized session is not configured")
        self.http_post = http_post
        self.cookies = parse_cookie_header(cookie_header)
        if not self.cookies:
            raise XhsCredentialError("Xiaohongshu authorized session is invalid")

    def __call__(self, url: str, **kwargs: Any) -> Any:
        host = (urlparse(url).hostname or "").lower()
        if host not in self.allowed_hosts:
            raise XhsCredentialError("Xiaohongshu authorized request target is not allowed")
        kwargs["follow_redirects"] = False
        kwargs["cookies"] = dict(self.cookies)
        return self.http_post(url, **kwargs)


class XiaohongshuAuthorizedAdapter(XiaohongshuAdapter):
    """Temporary authorized first-page mode sharing the fixture-tested parser."""

    adapter_version = "0.1.0-authorized"

    def __init__(self, http_post: Any | None = None, *, cookie_header: str = "") -> None:
        self.credential_configured = bool(parse_cookie_header(cookie_header))
        super().__init__(
            http_post=XhsAuthorizedPost(http_post, cookie_header)
            if http_post is not None and self.credential_configured
            else None,
            mode="authorized_first_page",
        )

    def search(self, request):
        if not self.credential_configured or self.http_post is None:
            from crawler_tool.domain import SourceErrorCode, SourceStatus
            from crawler_tool.sources.base import AdapterResult
            return AdapterResult(
                status=SourceStatus.SOURCE_UNAVAILABLE,
                error_code=SourceErrorCode.SOURCE_UNAVAILABLE,
                message="Xiaohongshu authorized session is not configured",
                retryable=False,
            )
        return self.probe_first_page(request)

    def health(self) -> dict[str, Any]:
        report = super().health()
        report.update({
            "authMode": "authorized_first_page",
            "capabilities": ["search"] if self.credential_configured else [],
            "credentialStatus": "configured" if self.credential_configured else "unavailable",
        })
        return report
