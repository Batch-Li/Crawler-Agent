from __future__ import annotations

from typing import Callable

from crawler_tool.infrastructure.http_client import HttpClient
from crawler_tool.sources.base import SourceAdapter


def http_transport_factory(*, timeout: float = 10.0, verify: bool | str = True) -> Callable[[], HttpClient]:
    """Create per-adapter HTTP clients with safe TLS defaults."""
    return lambda: HttpClient(timeout=timeout, verify=verify)


def bind_http_get(adapter_factory: Callable[..., SourceAdapter], *, timeout: float = 10.0, verify: bool | str = True) -> Callable[[], SourceAdapter]:
    """Bind an adapter factory to an injected HttpClient without platform secrets."""
    def factory() -> SourceAdapter:
        client = HttpClient(timeout=timeout, verify=verify)
        adapter = adapter_factory(http_get=client.get)
        original_close = adapter.close

        def close() -> None:
            try:
                original_close()
            finally:
                client.close()

        adapter.close = close  # type: ignore[method-assign]
        return adapter

    return factory
