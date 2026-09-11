"""字面匹配层：主体档案匹配（含 blocklist 否决）与动作词/宾语窗口判定。

设计要点：
- 主体与动作词必须同子句（同句邻近），跨子句不算命中；
- 宾语窗口：动作词后 N 字内，notice 词先出现 → 降级慢车道（"发布澄清公告"）；
  object 词先出现 → 确认（"发布新一代AI客服"）；两者都无 → 确认但打 objectUnverified 标。
"""

from __future__ import annotations

from dataclasses import dataclass

from crawler_tool.monitoring.models import EntityProfile, RuleFamily
from crawler_tool.monitoring.normalize import normalize_text


@dataclass(frozen=True)
class SubjectHit:
    entity: EntityProfile
    surface: str
    surface_kind: str  # canonical | alias | product
    strength: float
    clause_index: int
    clause_text: str

    @staticmethod
    def strength_for(kind: str) -> float:
        return {"canonical": 1.0, "alias": 0.8, "product": 0.7}.get(kind, 0.5)


@dataclass(frozen=True)
class ActionHit:
    family: RuleFamily
    action: str
    clause_index: int
    object_word: str | None
    object_unverified: bool


def _vetoed_by_blocklist(clause: str, start: int, end: int, entity: EntityProfile) -> bool:
    """匹配片段落在任何 blocklist 词的覆盖范围内 → 否决（"智言教育"吃掉"智言"）。"""
    for blocked in entity.confusable_blocklist:
        blocked_norm = normalize_text(blocked)
        if not blocked_norm:
            continue
        search_from = 0
        while True:
            found = clause.find(blocked_norm, search_from)
            if found == -1:
                break
            if found <= start and found + len(blocked_norm) >= end:
                return True
            search_from = found + 1
    return False


def _best_surface_in_clauses(
    entity: EntityProfile,
    clauses: list[str],
) -> SubjectHit | None:
    best: SubjectHit | None = None
    # surfaces() 已按长度降序：长词（canonical/具体别名）优先于短词（"智言"）。
    for surface, kind in entity.surfaces():
        surface_norm = normalize_text(surface)
        if not surface_norm:
            continue
        for clause_index, clause in enumerate(clauses):
            found = clause.find(surface_norm)
            if found == -1:
                continue
            end = found + len(surface_norm)
            if _vetoed_by_blocklist(clause, found, end, entity):
                continue
            candidate = SubjectHit(
                entity=entity,
                surface=surface_norm,
                surface_kind=kind,
                strength=SubjectHit.strength_for(kind),
                clause_index=clause_index,
                clause_text=clause,
            )
            # surfaces 降序尝试，第一个可命中的 surface 即该实体最强匹配。
            if best is None or (candidate.strength, -candidate.clause_index) > (best.strength, -best.clause_index):
                best = candidate
    return best


def match_subjects(
    clauses: list[str],
    entities: list[EntityProfile],
) -> list[SubjectHit]:
    """返回每个实体最多一个最优命中（强度最高、子句最靠前），按强度降序。"""
    hits: list[SubjectHit] = []
    for entity in entities:
        if not entity.enabled:
            continue
        best = _best_surface_in_clauses(entity, clauses)
        if best is not None:
            hits.append(best)
    hits.sort(key=lambda hit: (-hit.strength, hit.clause_index))
    return hits


def _first_word_position(window: str, words: list[str]) -> tuple[int, str] | None:
    best: tuple[int, str] | None = None
    for word in words:
        word_norm = normalize_text(word)
        if not word_norm:
            continue
        position = window.find(word_norm)
        if position == -1:
            continue
        if best is None or position < best[0]:
            best = (position, word_norm)
    return best


def find_action_hit(
    clauses: list[str],
    subject: SubjectHit,
    family: RuleFamily,
    object_window_chars: int,
) -> ActionHit | None:
    """在主体所在子句内找动作词并做宾语窗口检查；无同句动作词返回 None。"""
    clause = clauses[subject.clause_index] if subject.clause_index < len(clauses) else ""
    for action in family.actions:
        action_norm = normalize_text(action)
        if not action_norm:
            continue
        start = 0
        while True:
            position = clause.find(action_norm, start)
            if position == -1:
                break
            start = position + len(action_norm)
            # 名词歧义守卫："发布会"里的"发布"是名词成分，不是动作（"发布会员"类
            # 罕见表述会被误跳过，由慢车道兜底）。
            if clause[start: start + 1] == "会":
                continue
            window_start = start
            window = clause[window_start: window_start + object_window_chars]
            object_found = _first_word_position(window, family.object_words)
            notice_found = _first_word_position(window, family.notice_words)
            if notice_found is not None and (object_found is None or notice_found[0] < object_found[0]):
                return None  # "发布…公告"：降级慢车道
            object_word = object_found[1] if object_found else None
            return ActionHit(
                family=family,
                action=action_norm,
                clause_index=subject.clause_index,
                object_word=object_word,
                object_unverified=object_found is None,
            )
    return None


def contains_any(text: str, words: list[str]) -> str | None:
    """返回第一个命中的词（文本应为已归一化），未命中返回 None。"""
    for word in words:
        word_norm = normalize_text(word)
        if word_norm and word_norm in text:
            return word_norm
    return None
