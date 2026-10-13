"""文本归一化：主体单点改善之二——匹配前先消灭字面变体。

覆盖：全角→半角（NFKC）、大小写折叠、空白折叠。
繁简转换预留钩子（当前不引入映射表，见 MONITORING_DESIGN.md 的扩展点）。
"""

from __future__ import annotations

import re
import unicodedata

# NFKC 会把全角标点（，；！？）转成 ASCII，再叠加 CJK 句读一起切分子句。
_CLAUSE_SPLIT = re.compile(r"[。！？!?；;，,\n\r\t]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return _WHITESPACE.sub(" ", text).strip()


def split_clauses(normalized_text: str) -> list[str]:
    """按句读切分子句；同句邻近 = 主体与动作词落在同一切片。"""
    return [clause for clause in _CLAUSE_SPLIT.split(normalized_text or "") if clause.strip()]


def title_fingerprint(normalized_title: str) -> str:
    """近似标题指纹：去全部空白与标点后取哈希，供事件组聚类与重复压制。"""
    import hashlib

    stripped = re.sub(r"[\W_]+", "", normalized_title, flags=re.UNICODE)
    return "grp_" + hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16]


def title_tokens(normalized_title: str) -> set[str]:
    """字符二元组集合：中文标题的轻量相似度基底（无分词依赖）。"""
    stripped = re.sub(r"[\W_]+", "", normalized_title, flags=re.UNICODE)
    return {stripped[i:i + 2] for i in range(len(stripped) - 1)}


def titles_similar(
    a: str,
    b: str,
    *,
    min_overlap: float = 0.35,
    min_shared: int = 8,
) -> bool:
    """同故事近似标题判定：重叠系数 = |交集| / 较短标题的二元组数。

    阈值用真实同故事对校准（同故事 0.484/共享15，不同故事 0.0）——
    见 MONITORING_DESIGN §3 事件组聚类。
    """
    tokens_a, tokens_b = title_tokens(a), title_tokens(b)
    if not tokens_a or not tokens_b:
        return False
    shared = len(tokens_a & tokens_b)
    return shared >= min_shared and shared / min(len(tokens_a), len(tokens_b)) >= min_overlap
