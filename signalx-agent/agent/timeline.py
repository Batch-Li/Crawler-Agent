"""将已有结果导出为可直接阅读的 Markdown；不调用模型。"""
import argparse
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

from .event_time import event_sort_key, ground_event_time
from .models import Event
from .workflow_models import EventProcessingResult, WorkflowResult
from .tools.event_chain import order_story_events

_RELATIONS = {'FOLLOW_UP': '后续进展', 'RESPONSE': '回应', 'CAUSES': '因果', 'CONTRADICTS': '矛盾', 'RELATED': '相关'}
_CLAIMS = {'reported': '报道进展', 'planned': '已宣布计划', 'forecast': '预测', 'rumor': '传闻', 'opinion': '观点', 'refutation': '辟谣／更正'}
STOP_LABELS = {'COMPLETED': '本轮处理完成', 'PARTIAL': '部分完成', 'MAX_ROUNDS': '达到轮数上限',
               'TOOL_BUDGET_EXHAUSTED': '达到检索上限', 'NO_NEW_QUERIES': '没有新的有效查询',
               'NO_PROGRESS': '检索无新增', 'NO_EVIDENCE': '未获得材料', 'FAILED': '运行失败',
               'MODEL_BUDGET_EXHAUSTED': '达到模型调用上限'}


def _cell(value):
    text = ''.join(c for c in str(value or '') if unicodedata.category(c) not in ('Cf', 'Cc') or c in '\n\r\t')
    text = re.sub(r'[￥¥$]\?+[.\?]*', '价格未显示', text)
    text = ' '.join(text.split())
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '&#124;').replace('[', '\\[').replace(']', '\\]').replace('`', '\\`')


def date_label(event: Event, *, compact=False):
    if event.event_date:
        value = event.event_date.value
        if event.event_date.precision in ('month_day', 'month_only'):
            parts = list(map(int, value[2:].split('-')))
            return f'{parts[0]}月' + (f'{parts[1]}日' if len(parts) > 1 else '') + ('' if compact else '（年份未明）')
        return value + ('' if compact else {'year': '（仅知年）', 'month': '（仅知月）', 'day': ''}[event.event_date.precision])
    if event.event_time:
        return event.event_time.isoformat()
    if event.time_anchor:
        grounded = ground_event_time(event)
        if grounded.event_date or grounded.event_time:
            return date_label(grounded, compact=compact)
    return '' if compact else '未披露'


def _citations(event):
    priority = [a.evidence_id for a in (event.time_anchor, event.anchor) if a]
    supported = {a.evidence_id for a in event.evidence_assessments if a.stance == 'support'}
    sources = [e for e in event.evidence if not event.evidence_assessments or e.evidence_id in supported]
    sources = sorted(sources, key=lambda e: priority.index(e.evidence_id) if e.evidence_id in priority else len(priority))
    links = []
    for evidence in sources:
        url = evidence.url or ''
        source = _cell(evidence.source_name or urlparse(url).hostname or evidence.source)
        if urlparse(url).scheme in ('http', 'https'):
            safe_url = url.replace(' ', '%20').replace('(', '%28').replace(')', '%29').replace('<', '%3C').replace('>', '%3E')
            link = f'[{source}]({safe_url})'
        else:
            link = source
        if link not in links:
            links.append(link)
        if len(links) == 2:
            break
    return ('、'.join(links) + (f' 等 {len(sources)} 条' if len(sources) > len(links) else '')) or '[材料](result.json)'


def _publication(event):
    supported = {a.evidence_id for a in event.evidence_assessments if a.stance == 'support'}
    dated = [e for e in event.evidence if e.published_at and (not event.evidence_assessments or e.evidence_id in supported)]
    report = date_label(event.model_copy(update={'event_date': event.report_date}), compact=True) if event.report_date else ''
    if not dated:
        return report
    first = min(dated, key=lambda e: e.published_at)
    if event.report_date:
        value = event.report_date.value
        if not value.startswith('--') and value <= first.published_at.date().isoformat():
            return report
        if value.startswith('--') and value[1:] == first.published_at.strftime('-%m-%d'):
            return report
    approximate = any(x in first.quality_warnings for x in ('published_at_relative', 'published_at_year_inferred'))
    return ('约 ' if approximate else '') + first.published_at.date().isoformat()


def _story_date(event):
    date = date_label(event, compact=True)
    if date:
        return ('计划 ' if event.claim_type == 'planned' else '') + date
    publication = _publication(event)
    return '报道 ' + publication if publication else ''


def _render_stories(chains, by_id):
    lines, labels = [], {}
    for chain, relations in chains:
        ids = {key for r in relations for key in (r.from_event_id, r.to_event_id)}
        events = order_story_events([by_id[key] for key in chain.event_ids if key in ids], relations)
        lines += ['## ' + _cell(chain.title), '']
        new = [e for e in events if e.event_id not in labels]
        if new:
            show_date = any(_story_date(e) for e in new)
            lines += ['| 节点 | ' + ('时间 | ' if show_date else '') + '进展 | 类型 | 来源 |',
                      '|---|---|---|---|---|' if show_date else '|---|---|---|---|']
            for event in new:
                labels[event.event_id] = f'E{len(labels) + 1}'
                title = re.sub(r'^(?:报道称|媒体称|据报道)[：:]?\s*', '', event.title)
                lines.append(f'| {labels[event.event_id]} | ' + (_story_date(event) + ' | ' if show_date else '')
                             + f'{_cell(title)} | {_CLAIMS[event.claim_type]} | {_citations(event)} |')
            lines.append('')
        for relation in relations:
            a, b = relation.from_event_id, relation.to_event_id
            separator = ' → ' if relation.relation_type in ('FOLLOW_UP', 'RESPONSE', 'CAUSES') else ' / '
            lines.append(f'- {labels[a]}{separator}{labels[b]}（{_RELATIONS[relation.relation_type]}）：{_cell(relation.reason)}')
        lines.append('')
    remaining = len(by_id) - len(labels)
    if remaining:
        lines += [f'其余 {remaining} 个节点及全部新闻见[完整报告](report.md)。', '']
    return lines


def render_timeline(result: WorkflowResult | EventProcessingResult, *, note='') -> str:
    by_id = {e.event_id: ground_event_time(e) for e in result.events}
    events = sorted(by_id.values(), key=event_sort_key)
    lines = ['# 事件时间线', '']
    if isinstance(result, WorkflowResult):
        lines += [f'{STOP_LABELS[result.stop_reason.value]}；整理 {len(events)} 个节点。', '']
    reported = [e for e in events if e.claim_type in ('reported', 'refutation')]
    dated = [e for e in reported if e.event_time or (e.event_date and e.event_date.precision != 'month_day')]
    partial = [e for e in reported if e.event_date and e.event_date.precision == 'month_day']
    unknown = [e for e in reported if not e.event_time and not e.event_date]
    unknown.sort(key=lambda e: (not _publication(e), _publication(e).removeprefix('约 '), e.event_id))
    sections = [('事件进展', [*dated, *partial], '事件日期'),
                ('相关报道', [e for e in unknown if _publication(e)], '报道日期'),
                ('其他进展', [e for e in unknown if not _publication(e)], ''),
                ('已宣布的计划', [e for e in events if e.claim_type == 'planned'], '目标日期'),
                ('预测、传闻与观点', [e for e in events if e.claim_type in ('forecast', 'rumor', 'opinion')], '事件／目标日期')]
    supported_chains = [(chain, [r for r in chain.relations if r.status == 'supported'
                        and r.from_event_id in by_id and r.to_event_id in by_id]) for chain in result.chains]
    supported_chains = [(c, relations) for c, relations in supported_chains if relations]
    if supported_chains:
        lines += _render_stories(supported_chains, by_id)
        sections = []
    labels = {e.event_id: f'E{i}' for i, e in enumerate((e for _, rows, _ in sections for e in rows), 1)}
    for title, rows, date_heading in sections:
        if not rows:
            continue
        lines += ['## ' + title, '']
        publication = date_heading == '报道日期'
        show_date = bool(date_heading) and (publication or any(e.event_date or e.event_time for e in rows))
        lines += [f'| 节点 | {date_heading + " | " if show_date else ""}进展 | 类型 | 来源 |',
                  '|---|---|---|---|---|' if show_date else '|---|---|---|---|']
        for event in rows:
            title = re.sub(r'^(?:报道称|媒体称|据报道)[：:]?\s*', '', event.title)
            date = _publication(event) if publication else date_label(event, compact=True)
            lines.append(f'| {labels[event.event_id]} | {date + " | " if show_date else ""}{_cell(title)} | {_CLAIMS[event.claim_type]} | {_citations(event)} |')
        lines.append('')
    if not events:
        lines += ['暂无可展示的事件。', '']
    pending = sum(r.status == 'needs_review' for c in result.chains for r in c.relations)
    remarks = []
    if note:
        remarks.append(_cell(note))
    if isinstance(result, WorkflowResult):
        if result.processing_mode == 'event_refinement':
            remarks.append('沿用已存事件复核，未重新采集或抽取。')
        if result.stop_message and result.stop_reason.value != 'COMPLETED':
            remarks.append('停止说明：' + _cell(result.stop_message))
        if result.unprocessed_evidence_ids:
            remarks.append(f'{len(result.unprocessed_evidence_ids)} 条材料未完成处理。')
    if events:
        remarks.append('日期按来源精度展示；报道日期不作事件日期，箭头表示证据支持的动作顺序。')
    conflicts = sum(e.date_status == 'conflicting' for e in events)
    if conflicts:
        remarks.append(f'{conflicts} 个节点日期口径不同，已保留共同精度。')
    if any(e.merge_review_pending for e in events):
        remarks.append('部分重复候选未通过归并校验，暂保留独立节点。')
    if any(e.has_full_content is False for event in events for e in event.evidence):
        remarks.append('部分材料为搜索摘要。')
    gaps = (pending or unknown or any(e.time_warnings for e in events)
            or any(c.unresolved_questions for c in result.chains)
            or result.chain_unresolved_questions
            or (isinstance(result, WorkflowResult) and result.unresolved_questions))
    remarks.append(('缺失信息、口径分歧及待核验关联保留在' if gaps else '完整证据与分析见')
                   + '[完整结果](result.json)。')
    lines += ['## 备注', '', ' '.join(remarks), '']
    return '\n'.join(lines)


def write_timeline(result, path: Path, *, note=''):
    with path.open('x', encoding='utf-8') as stream:
        stream.write(render_timeline(result, note=note))


def main():
    parser = argparse.ArgumentParser(description='从结果 JSON 单独导出时间链，不访问网络')
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding='utf-8'))
    payload = payload.get('result', payload)
    cls = WorkflowResult if 'stopReason' in payload else EventProcessingResult
    write_timeline(cls.model_validate(payload), args.output)


if __name__ == '__main__':
    main()
