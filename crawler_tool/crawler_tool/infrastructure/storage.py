from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class CacheBackend(Protocol):
    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...


class RawStorage(Protocol):
    def save(self, platform: str, trace_id: str, payload: Any) -> str: ...
    def load(self, reference: str) -> Any | None: ...


class InMemoryCache:
    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def get(self, key: str) -> Any | None:
        return self._values.get(key)

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        self._values[key] = value


class InMemoryRawStorage:
    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def save(self, platform: str, trace_id: str, payload: Any) -> str:
        reference = f"raw/{platform}/{trace_id}"
        self._values[reference] = payload
        return reference

    def load(self, reference: str) -> Any | None:
        return self._values.get(reference)
