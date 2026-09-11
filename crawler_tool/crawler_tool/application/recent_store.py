from __future__ import annotations

import threading
from collections import deque


class RecentItemsStore:
    """Bounded in-memory session view of normalized ContentItems.

    All ingestion paths (network search adapters, authorized wechat flows,
    manual capture ingestion) share one instance so operators get a single
    "/view"-style listing regardless of how an item arrived. Entries are
    de-duplicated by contentId, keeping the most recent occurrence; the store
    is intentionally volatile (lost on restart).
    """

    def __init__(self, *, max_size: int = 500) -> None:
        self._lock = threading.Lock()
        self._items: deque = deque(maxlen=max_size)
        self._index: dict[str, int] = {}

    def add(self, items) -> None:
        with self._lock:
            for item in items:
                key = item.content_id
                existing = self._index.get(key)
                if existing is not None:
                    # Replace prior occurrence, refreshing recency.
                    del self._items[existing]
                    self._reindex()
                self._items.append(item)
                self._index[key] = len(self._items) - 1

    def query(self, *, platform: str | None = None, keyword: str | None = None, limit: int = 50) -> list:
        with self._lock:
            snapshot = list(self._items)
        selected = []
        needle = keyword.casefold() if keyword else None
        for item in reversed(snapshot):
            if platform is not None and item.platform != platform:
                continue
            if needle is not None and needle not in str(item.collection.query or "").casefold():
                continue
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def _reindex(self) -> None:
        self._index = {item.content_id: position for position, item in enumerate(self._items)}
