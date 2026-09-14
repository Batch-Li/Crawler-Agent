"""详情页发布时间提取：纯函数、零网络、只取时间不取正文。

优先级遵循业界标准做法（Google 新闻发布商规范）：
JSON-LD ``datePublished`` > meta ``article:published_time`` > 其他发布时间 meta
> 可见文本正则（限定文档前部，防侧栏"相关文章"日期污染）。

有效性约束：年份限 2000-2099；时间不得晚于当前时钟 +2 天；无时区按北京时间处理。
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from lxml import html

_CST = timezone(timedelta(hours=8))
_MAX_AGE_TOLERANCE = timedelta(days=2)
_VISIBLE_TEXT_LIMIT = 2000

_SCRIPT_LD_JSON = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I
)
_DATE_WITH_TIME = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})[日号]?\s*(\d{1,2}):(\d{2})")
_DATE_ONLY = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})[日号]?")

_META_EXPRESSIONS = (
    '//meta[@property="article:published_time"]/@content',
    '//meta[@property="og:article:published_time"]/@content',
    '//meta[@name="pubdate"]/@content',
    '//meta[@name="publishdate"]/@content',
    '//meta[@name="publish-date"]/@content',
    '//meta[@name="publication_date"]/@content',
    '//meta[@itemprop="datePublished"]/@content',
)
# 央视网 2026 版详情页契约：var publishDate = "20260913112937 "（紧凑 YYYYMMDDHHMMSS，
# 引号内可带尾随空格）。平台原生结构化时间，置信等同 meta。
_COMPACT_PUBLISH_DATE = re.compile(r"publishDate\s*=\s*[\"']?(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")


@dataclass(frozen=True)
class DetailTime:
    iso: str
    confidence: float
    detected_by: str  # "json_ld" | "meta" | "visible_text"


def parse_detail_time(page: str, *, clock: datetime) -> DetailTime | None:
    """按优先级提取详情页发布时间；无任何有效信号返回 None。

    最后一级「内嵌脚本唯一日期」：SPA 站点（如南方周末）正文 JS 渲染、
    无任何结构化标记，发布时间只存在于页面状态脚本里。仅当全页脚本
    恰好含一个不同日期值时采纳（多个即拒，防相关推荐/侧栏日期污染）。
    """
    for value in _json_ld_dates(page):
        parsed = _to_iso(value, clock)
        if parsed:
            return DetailTime(parsed, 1.0, "json_ld")
    try:
        tree = html.fromstring(page)
    except ValueError:
        return None
    for expression in _META_EXPRESSIONS:
        for value in tree.xpath(expression):
            parsed = _to_iso(value, clock)
            if parsed:
                return DetailTime(parsed, 1.0, "meta")
    compact = _COMPACT_PUBLISH_DATE.search(page)
    if compact:
        parsed = _to_iso("-".join(compact.groups()[:3]) + " " + ":".join(compact.groups()[3:]), clock)
        if parsed:
            return DetailTime(parsed, 1.0, "embedded_variable")
    for value in _visible_dates(tree):
        parsed = _to_iso(value, clock)
        if parsed:
            return DetailTime(parsed, 0.85, "visible_text")
    script_values = set(_script_dates(page))
    if len(script_values) == 1:
        parsed = _to_iso(next(iter(script_values)), clock)
        if parsed:
            return DetailTime(parsed, 0.85, "embedded_script")
    return None


def _json_ld_dates(page: str) -> Iterator[str]:
    for block in _SCRIPT_LD_JSON.findall(page):
        try:
            payload = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        yield from _walk_date_published(payload)


def _walk_date_published(payload: Any) -> Iterator[str]:
    if isinstance(payload, dict):
        value = payload.get("datePublished")
        if isinstance(value, str) and value.strip():
            yield value.strip()
        for child in payload.values():
            yield from _walk_date_published(child)
    elif isinstance(payload, list):
        for child in payload:
            yield from _walk_date_published(child)


def _visible_dates(tree: Any) -> Iterator[str]:
    """仅扫描文档前部可见文本（标题邻近区域），侧栏/推荐位日期不进入候选。"""
    clone = deepcopy(tree)
    for element in clone.xpath("//script | //style"):
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    text = " ".join(" ".join(clone.itertext()).split())[:_VISIBLE_TEXT_LIMIT]
    for match in _DATE_WITH_TIME.finditer(text):
        year, month, day, hour, minute = match.groups()
        yield f"{year}-{month}-{day} {hour}:{minute}"
    for match in _DATE_ONLY.finditer(text):
        year, month, day = match.groups()
        yield f"{year}-{month}-{day}"


def _script_dates(page: str) -> Iterator[str]:
    """页面内嵌脚本中的日期候选（含时刻优先）；同一处日期只计一次。"""
    occupied: list[tuple[int, int]] = []
    for match in _DATE_WITH_TIME.finditer(page):
        occupied.append(match.span())
        year, month, day, hour, minute = match.groups()
        yield f"{year}-{month}-{day} {hour}:{minute}"
    for match in _DATE_ONLY.finditer(page):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        year, month, day = match.groups()
        yield f"{year}-{month}-{day}"


def _to_iso(value: Any, clock: datetime) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    moment: datetime | None = None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        match = _DATE_WITH_TIME.fullmatch(text) or _DATE_ONLY.fullmatch(text)
        if match:
            groups = [int(part) for part in match.groups()]
            try:
                if len(groups) >= 5:
                    moment = datetime(groups[0], groups[1], groups[2], groups[3], groups[4])
                else:
                    moment = datetime(groups[0], groups[1], groups[2])
            except ValueError:
                return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_CST)
    moment = moment.astimezone(timezone.utc)
    if moment.year < 2000 or moment > clock + _MAX_AGE_TOLERANCE:
        return None
    return moment.isoformat()
