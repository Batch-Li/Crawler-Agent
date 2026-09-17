"""时间必须落到证据；日期精度独立于绝对时刻。"""
import calendar
import re
from datetime import datetime, timedelta, timezone

from .evidence_validation import resolve_anchor
from .models import Event, Evidence, EvidenceAnchor, PartialDate

_DATE = re.compile(r'(?<!\d)([12]\d{3})(?:\s*年\s*|-)(\d{1,2})(?:(?:\s*月\s*|-)(\d{1,2})(?:\s*[日号])?|\s*月|(?![\d-]))(?!\d)')
_ISO = re.compile(r'(?<!\d)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})(?!\d)')


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
    for match in re.finditer(r'(?<!\d)([12]\d{3})\s*年', text):
        if not any(match.start() < end and match.end() > start for start, end in occupied):
            result.append((PartialDate(value=match.group(1), precision='year'), match.group()))
            occupied.append(match.span())
    for match in re.finditer(r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]', text):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        occupied.append(match.span())
        try:
            result.append((PartialDate(value=f'--{int(match[1]):02d}-{int(match[2]):02d}',
                                       precision='month_day'), match.group()))
        except ValueError:
            continue
    for match in re.finditer(r'(?<!\d)(\d{1,2})\s*月(?:份)?', text):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        try:
            result.append((PartialDate(value=f'--{int(match[1]):02d}', precision='month_only'), match.group()))
        except ValueError:
            continue
    return result


def _reliable_publication(source):
    return source.published_at is not None and not any(
        warning.startswith('published_at_') for warning in source.quality_warnings)


def _candidate_date(candidate, sources):
    """计算一个已审阅的时间片段；绝不采用模型生成的日期字符串。"""
    anchor = resolve_anchor(candidate.anchor, list(sources.values()))
    source = sources[anchor.evidence_id]
    matches = list(_ISO.finditer(anchor.quote))
    dates = {d.value: d for d, _ in _dates(anchor.quote, [m.span() for m in matches])}
    if len(matches) == 1 and not dates:
        return None, datetime.fromisoformat(matches[0].group().replace('Z', '+00:00')).astimezone(timezone.utc), 'explicit'
    if len(dates) > 1 or matches:
        return None, None, 'unknown'
    partial = next(iter(dates.values()), None)
    basis = 'explicit'
    relative_days = re.findall(r'今天|今日|昨天|昨日|前天', anchor.quote)
    if not partial and len(set(relative_days)) == 1 and _reliable_publication(source):
        offset = {'今天': 0, '今日': 0, '昨天': -1, '昨日': -1, '前天': -2}[relative_days[0]]
        value = (source.published_calendar_date or source.published_at.date()) + timedelta(days=offset)
        return PartialDate(value=value.isoformat(), precision='day'), None, 'relative'
    if partial and partial.precision in ('month_day', 'month_only'):
        year = None
        relative_years = set(re.findall(r'今年|去年|明年', anchor.quote))
        if len(relative_years) == 1 and _reliable_publication(source):
            year = (source.published_calendar_date or source.published_at.date()).year + {'今年': 0, '去年': -1, '明年': 1}[next(iter(relative_years))]
            basis = 'relative'
        elif candidate.year_context:
            context = resolve_anchor(candidate.year_context, list(sources.values()))
            if context.evidence_id != anchor.evidence_id:
                raise ValueError('年份上下文必须来自同一条证据')
            years = set(re.findall(r'(?<!\d)([12]\d{3})(?!\d)', context.quote))
            if len(years) == 1:
                year, basis = int(next(iter(years))), 'context'
        if year:
            try:
                partial = PartialDate(value=str(year) + partial.value[1:],
                                      precision='day' if partial.precision == 'month_day' else 'month')
            except ValueError:
                return None, None, 'unknown'
    return partial, None, basis


def _parts(value):
    if value.precision.startswith('month_'):
        return (None, *map(int, value.value[2:].split('-')))
    return tuple(map(int, value.value.split('-')))


def _combine_dates(values):
    """相容来源补全精度；冲突只保留共同精度，不投票选一个日期。"""
    if not values:
        return None, False
    components = [_parts(d) + (None,) * (3 - len(_parts(d))) for d in values]
    columns = [{row[i] for row in components if row[i] is not None} for i in range(3)]
    conflict = any(len(column) > 1 for column in columns)
    year, month, day = [next(iter(column)) if len(column) == 1 else None for column in columns]
    if len(columns[0]) > 1:
        return None, True
    if len(columns[1]) > 1:
        month = day = None
    if year:
        value = f'{year:04d}' + (f'-{month:02d}' if month else '') + (f'-{day:02d}' if month and day else '')
        precision = 'day' if month and day else 'month' if month else 'year'
    elif month:
        value, precision = f'--{month:02d}' + (f'-{day:02d}' if day else ''), 'month_day' if day else 'month_only'
    else:
        return None, conflict
    return PartialDate(value=value, precision=precision), conflict


def _ground_reviewed_time(event):
    sources = {e.evidence_id: e for e in event.evidence}
    roles = {'occurrence': [], 'target': [], 'report': []}
    for candidate in event.time_candidates:
        date, instant, basis = _candidate_date(candidate, sources)
        if candidate.role == 'occurrence' and date and date.precision == 'year' and re.search(
                re.escape(date.value) + r'\s*年[^，,。；;]{0,12}目标', candidate.anchor.quote):
            # “发布后24小时内，将2027年供货目标提高”里的2027修饰目标而非提高动作。
            candidate.role = 'target'
        if date or instant:
            roles[candidate.role].append((date, instant, basis, candidate.anchor))
    event.event_time = event.event_date = event.target_date = event.report_date = None
    event.time_anchor = None
    event.date_status, event.date_basis = 'unknown', 'unknown'
    event.time_warnings = [w for w in event.time_warnings if '原时间引用失效' in w]
    primary = roles['occurrence']
    # 计划节点按目标日展示；预测的目标年份不能冒充预测发表日期。
    if event.claim_type == 'planned':
        primary = roles['target'] or primary
    dates = [d if d else PartialDate(value=t.date().isoformat(), precision='day') for d, t, _, _ in primary]
    event.event_date, conflict = _combine_dates(dates)
    if primary:
        event.date_status = 'conflicting' if conflict else 'resolved'
        event.date_basis = 'relative' if any(x[2] == 'relative' for x in primary) else 'context' if any(x[2] == 'context' for x in primary) else 'explicit'
        event.time_anchor = primary[0][3]
        if all(t is not None for _, t, _, _ in primary) and len({t for _, t, _, _ in primary}) == 1:
            event.event_time, event.event_date = primary[0][1], None
    if conflict:
        event.time_warnings.append('来源日期存在分歧，显示共同精度；各口径见 timeCandidates。')
    for role, field in (('target', 'target_date'), ('report', 'report_date')):
        values = [d for d, _, _, _ in roles[role] if d]
        if role == 'report' and values:
            # 多篇报道可以在不同日期发表，不构成事件发生日期冲突。
            value = min(values, key=lambda d: (d.value.startswith('--'), d.value))
        else:
            value, _ = _combine_dates(values)
        setattr(event, field, value)
    return event


def refresh_event_evidence(event: Event, updates: dict[str, Evidence]) -> Event:
    """替换来源时复核继承的时间引用；旧快照及新模型引用不受影响。"""
    refreshed = event.model_copy(deep=True)
    refreshed.evidence = [updates.get(item.evidence_id, item) for item in refreshed.evidence]
    if refreshed.evidence != event.evidence:
        refreshed.time_reviewed = False
        refreshed.grounding_audited = False
        refreshed.time_candidates = []
        refreshed.evidence_assessments = []
        refreshed.target_date = refreshed.report_date = None
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


def ground_event_time(event: Event) -> Event:
    """显式 timeAnchor 优先；旧输出只保守恢复同年月的唯一日期。"""
    event = event.model_copy(deep=True)
    if event.time_reviewed:
        return _ground_reviewed_time(event)
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
        dates = {d.value: d for d, _ in _dates(anchor.quote, [m.span() for m in instant_matches])}
        if len(instants) == 1 and not dates:
            event.event_time = datetime.fromisoformat(next(iter(instants)).replace('Z', '+00:00')).astimezone(timezone.utc)
        elif len(dates) == 1 and not instants:
            event.event_date = next(iter(dates.values()))
        elif len(instants) + len(dates) > 1:
            event.time_warnings = list(dict.fromkeys([
                *event.time_warnings, '时间引用包含多个日期或时刻，事件时间待核实。',
            ]))
    if attempted is not None and event.event_time is None:
        warning = ('仅有日期证据，未保留模型补出的日或时刻。' if event.event_date
                   else '事件时刻缺少明确原文依据，已置空。')
        event.time_warnings = list(dict.fromkeys([*event.time_warnings, warning]))
    event.date_status = 'resolved' if event.event_date or event.event_time else 'unknown'
    event.date_basis = 'explicit' if event.date_status == 'resolved' else 'unknown'
    return event


def time_bounds(event: Event):
    if event.event_time is not None:
        return event.event_time, event.event_time
    if event.event_date is None or event.event_date.precision in ('month_day', 'month_only'):
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
    if bounds:
        return (0, bounds[0], event.event_id)
    if event.event_date and event.event_date.precision in ('month_day', 'month_only'):
        parts = list(map(int, event.event_date.value[2:].split('-')))
        month, day = parts[0], parts[1] if len(parts) > 1 else 1
        return (1, datetime(2000, month, day, tzinfo=timezone.utc), event.event_id)
    return (2, datetime.max.replace(tzinfo=timezone.utc), event.event_id)
