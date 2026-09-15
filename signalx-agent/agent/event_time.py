"""时间必须落到证据；日期精度独立于绝对时刻。"""
import calendar
import re
from datetime import datetime, timedelta, timezone

from .evidence_validation import resolve_anchor
from .models import Event, Evidence, EvidenceAnchor, PartialDate

_DATE = re.compile(r'(?<!\d)([12]\d{3})(?:\s*年\s*|-)(\d{1,2})(?:(?:\s*月\s*|-)(\d{1,2})(?:\s*[日号])?|\s*月|(?![\d-]))(?!\d)')
_ISO = re.compile(r'(?<!\d)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})(?!\d)')
_CST = timezone(timedelta(hours=8))
# 无年份中文月日（"5月6日"）：本身不足以定日期，仅当能与高置信发布时间的月日
# （北京时间）对应时才借年份；lookbehind 避开 "2024年1月2日" 与 "24周年庆"。
_MONTH_DAY = re.compile(r'(?<![\d年])(\d{1,2})月(\d{1,2})[日号](?!\d)')
_BORROW_CONFIDENCE = 0.85


def _dates(text, excluded_spans=()):
    result = []
    occupied = list(excluded_spans)
    for match in _DATE.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in excluded_spans):
            continue
        occupied.append(match.span())
        year, month, day = match.groups()
        value = f'{int(year):04d}-{int(month):02d}' + (f'-{int(day):02d}' if day else '')
        try:
            result.append((PartialDate(value=value, precision='day' if day else 'month'), match.group()))
        except ValueError:
            continue
    for match in re.finditer(r'(?<!\d)([12]\d{3})年', text):
        if not any(match.start() < end and match.end() > start for start, end in occupied):
            result.append((PartialDate(value=match.group(1), precision='year'), match.group()))
    return result


def _month_days(text: str, excluded_spans=()) -> list[tuple[tuple[int, int], str]]:
    """无年份中文月日候选 ((月, 日), 原文片段)；只做形态匹配，不推断年份。"""
    results: list[tuple[tuple[int, int], str]] = []
    for match in _MONTH_DAY.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in excluded_spans):
            continue
        month, day = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            results.append(((month, day), match.group()))
    return results


def _borrow_year(event: Event, month: int, day: int) -> str | None:
    """月日与高置信（≥0.85）发布时间的月日（北京时间）一致时借年份；唯一才借。

    多个不同年份（跨年报道）或全部不一致都视为不可借，返回 None。
    """
    years: set[int] = set()
    for item in event.evidence:
        published = item.published_at
        if published is None or (item.published_at_confidence or 0) < _BORROW_CONFIDENCE:
            continue
        local = published.astimezone(_CST)
        if local.month == month and local.day == day:
            years.add(local.year)
    if len(years) == 1:
        return f'{next(iter(years)):04d}-{month:02d}-{day:02d}'
    return None


def refresh_event_evidence(event: Event, updates: dict[str, Evidence]) -> Event:
    """替换来源时复核继承的时间引用；旧快照及新模型引用不受影响。"""
    refreshed = event.model_copy(deep=True)
    refreshed.evidence = [updates.get(item.evidence_id, item) for item in refreshed.evidence]
    anchor = event.time_anchor
    if anchor and any(item.evidence_id == anchor.evidence_id and updates.get(item.evidence_id, item) != item
                      for item in event.evidence):
        try:
            refreshed.time_anchor = resolve_anchor(anchor, refreshed.evidence)
        except ValueError:
            refreshed.event_time = refreshed.event_date = refreshed.time_anchor = None
            refreshed.time_warnings = list(dict.fromkeys([
                *refreshed.time_warnings, '证据内容已更新，原时间引用失效，已清除旧时间。',
            ]))
    return refreshed


def ground_event_time(event: Event, *, allow_fallback: bool = False) -> Event:
    """显式 timeAnchor 优先；旧输出只保守恢复同年月的唯一日期。

    allow_fallback（默认关）：模型没给时间锚点、或锚点引用解析不出任何
    日期/时刻时，按证据强度降序保守兜底；锚点只含无年份月日时尝试借年份。
    落地日期一律带「待人工复核」警告。兜底必须在本函数内实现——timeline
    渲染会重跑本函数并先清空时间，函数外写入的结果会被渲染抹掉。
    """
    event = event.model_copy(deep=True)
    attempted = event.event_time
    anchor = event.time_anchor
    if anchor:
        anchor = resolve_anchor(anchor, event.evidence)
    elif attempted:
        # 旧协议没有时间锚点；不从当前时间、采集时间或 publishedAt 猜测。
        candidates = {}
        for evidence in event.evidence:
            for text in (evidence.title, evidence.content, evidence.snippet):
                for partial, quote in _dates(text or ''):
                    if partial.value.startswith(attempted.strftime('%Y-%m')):
                        candidates[partial.value] = EvidenceAnchor(evidence_id=evidence.evidence_id, quote=quote)
        if len(candidates) == 1:
            anchor = next(iter(candidates.values()))
    event.event_time = None
    event.event_date = None
    event.time_anchor = anchor
    if anchor:
        instant_matches = list(_ISO.finditer(anchor.quote))
        instants = {m.group() for m in instant_matches}
        excluded = [m.span() for m in instant_matches]
        dates = {d.value: d for d, _ in _dates(anchor.quote, excluded)}
        excluded += [m.span() for m in _DATE.finditer(anchor.quote)]
        month_days = _month_days(anchor.quote, excluded)
        if len(instants) == 1 and not dates:
            event.event_time = datetime.fromisoformat(next(iter(instants)).replace('Z', '+00:00')).astimezone(timezone.utc)
        elif len(dates) == 1 and not instants:
            event.event_date = next(iter(dates.values()))
        elif len(instants) + len(dates) > 1:
            event.time_warnings = list(dict.fromkeys([
                *event.time_warnings, '时间引用包含多个日期或时刻，事件时间待核实。',
            ]))
        elif allow_fallback and len(month_days) == 1:
            # 锚点只有无年份月日：与高置信发布时间月日（北京时间）一致才借年份。
            borrowed = _borrow_year(event, *month_days[0][0])
            if borrowed is not None:
                event.event_date = PartialDate(value=borrowed, precision='day')
                event.time_warnings = list(dict.fromkeys([
                    *event.time_warnings,
                    '月日来自时间锚点，年份借自高置信发布时间（北京时间比对一致），待人工复核。',
                ]))
            else:
                event.time_anchor = None
                event = _fallback_ground_time(event)
        elif allow_fallback:
            # 锚点解析不出任何日期/时刻：不静默放弃，转入保守兜底。
            event.time_anchor = None
            event = _fallback_ground_time(event)
    elif allow_fallback:
        event = _fallback_ground_time(event)
    if attempted is not None and event.event_time is None:
        warning = ('仅有日期证据，未保留模型补出的日或时刻。' if event.event_date
                   else '事件时刻缺少明确原文依据，已置空。')
        event.time_warnings = list(dict.fromkeys([*event.time_warnings, warning]))
    return event


def _corroborated_candidates(candidates: dict, event: Event) -> list[str]:
    """与高置信（≥0.85）发布时间日历日（北京时间）一致的候选值列表。"""
    stamps: set[str] = set()
    for evidence in event.evidence:
        published = evidence.published_at
        if published is None or (evidence.published_at_confidence or 0) < _BORROW_CONFIDENCE:
            continue
        local = published.astimezone(_CST)
        stamps.add(f"{local.year:04d}-{local.month:02d}-{local.day:02d}")
        stamps.add(f"{local.year:04d}-{local.month:02d}")  # 月精度候选亦可对照
    return [value for value in candidates if value in stamps]


def _fallback_ground_time(event: Event) -> Event:
    """无模型时间锚点时的保守兜底；任何结果都只是待复核的线索。

    阶段 1（文本锚定）：全部证据的标题/正文/摘要里恰好有一个不同日期值，
    以命中原文为锚点落日精度日期；多个候选只警告不落地。
    阶段 1.5（月日借年份）：全文只有无年份"X月X日"且恰有一个不同值时，
    与高置信（≥0.85）发布时间的月日（北京时间）一致才借年份落日精度；
    借不到（跨年歧义或全不匹配）继续阶段 2。
    阶段 2（发布时间戳，仅在阶段 1 无任何候选时）：全部高置信（≥0.85）
    发布时间的日历日（北京时间）一致才落日精度日期；不设 eventTime、不伪造正文锚点。
    """
    candidates: dict[str, tuple[PartialDate, str, str]] = {}
    for evidence in event.evidence:
        for text in (evidence.title, evidence.content, evidence.snippet):
            for partial, quote in _dates(text or ''):
                candidates.setdefault(partial.value, (partial, evidence.evidence_id, quote))
    if len(candidates) == 1:
        partial, evidence_id, quote = next(iter(candidates.values()))
        event.time_anchor = EvidenceAnchor(evidence_id=evidence_id, quote=quote)
        event.event_date = partial
        event.time_warnings = list(dict.fromkeys([
            *event.time_warnings, '事件日期来自证据原文显式日期（无模型时间锚点），待人工复核。',
        ]))
        return event
    if candidates:
        # 交叉验证：多候选中恰有一个与高置信发布时间（北京时间）一致 → 文本×时间戳
        # 双源互证落地该候选；0 个或多个一致仍维持不落地。
        corroborated = _corroborated_candidates(candidates, event)
        if len(corroborated) == 1:
            partial, evidence_id, quote = candidates[corroborated[0]]
            event.time_anchor = EvidenceAnchor(evidence_id=evidence_id, quote=quote)
            event.event_date = partial
            event.time_warnings = list(dict.fromkeys([
                *event.time_warnings,
                '多个候选日期中经高置信发布时间交叉验证（北京时间一致）选中，待人工复核。',
            ]))
            return event
        detail = (f'（其中 {len(corroborated)} 个与高置信发布时间一致）' if corroborated else '')
        event.time_warnings = list(dict.fromkeys([
            *event.time_warnings, f'时间线索存在 {len(candidates)} 个候选日期{detail}，未自动落时间。',
        ]))
        return event
    # 阶段 1.5：全文只有无年份月日、且能借到唯一年份时落地（借不到继续阶段 2）。
    month_day_candidates: dict[tuple[int, int], tuple[str, str]] = {}
    for evidence in event.evidence:
        for text in (evidence.title, evidence.content, evidence.snippet):
            body = text or ''
            excluded = [m.span() for m in _DATE.finditer(body)] + [m.span() for m in _ISO.finditer(body)]
            for month_day, quote in _month_days(body, excluded):
                month_day_candidates.setdefault(month_day, (evidence.evidence_id, quote))
    if len(month_day_candidates) == 1:
        (month, day), (evidence_id, quote) = next(iter(month_day_candidates.items()))
        borrowed = _borrow_year(event, month, day)
        if borrowed is not None:
            event.time_anchor = EvidenceAnchor(evidence_id=evidence_id, quote=quote)
            event.event_date = PartialDate(value=borrowed, precision='day')
            event.time_warnings = list(dict.fromkeys([
                *event.time_warnings,
                '事件日期来自证据原文月日与高置信发布时间借年份（无完整日期），待人工复核。',
            ]))
            return event
    stamped = [item for item in event.evidence
               if item.published_at is not None and (item.published_at_confidence or 0) >= 0.85]
    # 中文新闻的语义日期按北京时间取日历日：避免北京时间凌晨 0~8 点发布的
    # 文章被 UTC 日期错标成前一天。
    days = {item.published_at.astimezone(_CST).date() for item in stamped}
    if len(days) == 1:
        event.event_date = PartialDate(value=days.pop().isoformat(), precision='day')
        event.time_warnings = list(dict.fromkeys([
            *event.time_warnings, '事件日期来自证据发布时间戳（非正文锚点），待人工复核。',
        ]))
    elif stamped:
        event.time_warnings = list(dict.fromkeys([
            *event.time_warnings, '高置信发布时间互相矛盾，未自动落时间。',
        ]))
    return event


def time_bounds(event: Event):
    if event.event_time is not None:
        return event.event_time, event.event_time
    if event.event_date is None:
        return None
    parts = [int(part) for part in event.event_date.value.split('-')]
    year, month, day = parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1
    end_month = month if len(parts) > 1 else 12
    end_day = day if len(parts) > 2 else calendar.monthrange(year, end_month)[1]
    # 无时区日历日期使用保守范围，仅用于排序及确定性时间冲突检查，不写回 eventTime。
    return (datetime(year, month, day, tzinfo=timezone.utc) - timedelta(hours=14),
            datetime(year, end_month, end_day, tzinfo=timezone.utc) + timedelta(days=1, hours=12))


def event_sort_key(event: Event):
    bounds = time_bounds(event)
    return (bounds is None, bounds[0] if bounds else datetime.max.replace(tzinfo=timezone.utc), event.event_id)
