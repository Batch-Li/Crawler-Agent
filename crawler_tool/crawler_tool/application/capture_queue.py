"""捕获关键词队列：服务端持久化，agent 推送与 /capture 页共享。

- 条目形态 ``{"keyword", "addedAt", "source"}``；keyword 以 casefold 判重；
- tmp + ``os.replace`` 原子写（参照 MonitorStore 的 state.json 模式）；
- 读改写加进程内锁；单机单人使用语义，不承诺多进程并发。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


class CaptureQueueStore:
    def __init__(self, path: str | Path = "data/capture_queue.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def list_keywords(self) -> list[dict[str, str]]:
        with self._lock:
            return self._load()

    def add(self, keywords: list[str], *, source: str = "manual") -> dict[str, int]:
        """逐条判重追加；返回 {"added", "count"}。空串与重复词静默跳过。"""
        with self._lock:
            entries = self._load()
            existing = {str(entry.get("keyword", "")).casefold() for entry in entries}
            added = 0
            for keyword in keywords:
                cleaned = str(keyword).strip()
                if not cleaned or cleaned.casefold() in existing:
                    continue
                entries.append({
                    "keyword": cleaned,
                    "addedAt": datetime.now(timezone.utc).isoformat(),
                    "source": source,
                })
                existing.add(cleaned.casefold())
                added += 1
            if added:
                self._save(entries)
            return {"added": added, "count": len(entries)}

    def remove(self, keyword: str) -> bool:
        with self._lock:
            entries = self._load()
            kept = [
                entry for entry in entries
                if str(entry.get("keyword", "")).casefold() != keyword.strip().casefold()
            ]
            if len(kept) == len(entries):
                return False
            self._save(kept)
            return True

    def _load(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []  # 半行损坏不拖垮队列视图
        return payload if isinstance(payload, list) else []

    def _save(self, entries: list[dict[str, str]]) -> None:
        temp_path = self.path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(entries, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.path)
