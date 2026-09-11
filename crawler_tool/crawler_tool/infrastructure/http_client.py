from __future__ import annotations

from typing import Any

import httpx


class HttpClient:
    """Small injectable HTTP boundary with TLS verification and timeouts."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        verify: bool | str = True,
        follow_redirects: bool = True,
        client: httpx.Client | None = None,
    ):
        self._client = client or httpx.Client(timeout=timeout, verify=verify, follow_redirects=follow_redirects, trust_env=False)
        self._owned = client is None

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._client.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._client.post(url, **kwargs)

    def close(self) -> None:
        if self._owned:
            self._client.close()
