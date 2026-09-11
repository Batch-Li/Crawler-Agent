from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .base import SourceAdapter
from crawler_tool.domain.content_item import Platform


@dataclass
class SourceRegistry:
    factories: dict[Platform, Callable[[], SourceAdapter]]
    default_platforms: tuple[Platform, ...] | None = None

    def defaults(self) -> list[Platform]:
        if self.default_platforms is None:
            return list(self.factories)
        return [platform for platform in self.default_platforms if platform in self.factories]

    def create(self, platform: Platform) -> SourceAdapter:
        try:
            return self.factories[platform]()
        except KeyError as exc:
            raise ValueError(f"unsupported platform: {platform}") from exc

    def health(self) -> list[dict[str, object]]:
        reports = []
        for platform in self.factories:
            adapter = self.create(platform)
            try:
                reports.append(adapter.health())
            finally:
                adapter.close()
        return reports
