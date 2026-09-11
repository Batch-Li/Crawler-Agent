"""按配置做精确主体别名归一化，不把产品名或相似名称当作公司别名。"""
import unicodedata

from .models import Event


class EntityResolver:
    def __init__(self, aliases: dict[str, str] | None = None):
        self.aliases: dict[str, str] = {}
        for alias, canonical in (aliases or {}).items():
            canonical = canonical.strip()
            for name in (alias, canonical):
                key = self.key(name)
                if not key or not canonical:
                    raise ValueError("主体名称不能为空")
                if key in self.aliases and self.aliases[key] != canonical:
                    raise ValueError("主体别名配置有冲突")
                self.aliases[key] = canonical

    @staticmethod
    def key(name: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()

    def canonical_name(self, name: str) -> str:
        return self.aliases.get(self.key(name), name)

    def normalize(self, event: Event) -> Event:
        names = list(dict.fromkeys(self.canonical_name(name) for name in event.entities))
        return event.model_copy(update={"entities": names})
