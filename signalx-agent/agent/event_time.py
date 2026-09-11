"""时间必须落到证据；日期精度独立于绝对时刻。"""
import calendar
import re
from datetime import datetime, timedelta, timezone

from .evidence_validation import resolve_anchor
from .models import Event, EvidenceAnchor, PartialDate

_DATE = re.compile(r'(?<!\d)([12]\d{3})(?:\s*年\s*|-)(\d{1,2})(?:(?:\s*月\s*|-)(\d{1,2})(?:\s*[日号])?|\s*月|(?![\d-]))(?!\d)')
_ISO = re.compile(r'(?<!\d)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})(?!\d)')


def _dates(text):
    result = []
    for match in _DATE.finditer(text):
        year, month, day = match.groups()
        value = f'{int(year):04d}-{int(month):02d}' + (f'-{int(day):02d}' if day else '')
        try:
            result.append((PartialDate(value=value, precision='day' if day else 'month'), match.group()))
        except ValueError:
            continue
    if not result:
        result = [(PartialDate(value=m.group(1), precision='year'), m.group())
                  for m in re.finditer(r'(?<!\d)([12]\d{3})年', text)]
    return result


def ground_event_time(event: Event) -> Event:
    """显式 timeAnchor 优先；旧输出只保守恢复同年月的唯一日期。"""
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
        instants = {m.group() for m in _ISO.finditer(anchor.quote)}
        if len(instants) == 1:
            event.event_time = datetime.fromisoformat(next(iter(instants)).replace('Z', '+00:00')).astimezone(timezone.utc)
        else:
            dates = {d.value: d for d, _ in _dates(anchor.quote)}
            if len(dates) == 1:
                event.event_date = next(iter(dates.values()))
    if attempted is not None and event.event_time is None:
        warning = ('仅有日期证据，未保留模型补出的日或时刻。' if event.event_date
                   else '事件时刻缺少明确原文依据，已置空。')
        event.time_warnings = list(dict.fromkeys([*event.time_warnings, warning]))
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
