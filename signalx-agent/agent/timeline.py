"""将已有结果导出为可直接阅读的 Markdown；不调用模型。"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from .event_time import event_sort_key, ground_event_time
from .models import Event
from .workflow_models import EventProcessingResult, WorkflowResult

_RELATIONS = {'FOLLOW_UP': '后续进展', 'RESPONSE': '回应', 'CAUSES': '因果', 'CONTRADICTS': '矛盾', 'RELATED': '相关'}


def _cell(value):
    return str(value or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '&#124;').replace('\n', ' ').replace('\r', ' ').replace('[', '\\[').replace(']', '\\]')


def date_label(event: Event):
    if event.event_date:
        value = event.event_date.value
        return value + {'year': '（仅知年）', 'month': '（仅知月）', 'day': '（仅知日）'}[event.event_date.precision]
    return event.event_time.isoformat() if event.event_time else '时间待核实'


def render_timeline(result: WorkflowResult | EventProcessingResult, *, note='') -> str:
    by_id = {e.event_id: ground_event_time(e) for e in result.events}
    lines = ['# 事件时间链', '']
    if note:
        lines += [_cell(note), '']
    if isinstance(result, WorkflowResult):
        lines += [f'运行状态：{result.stop_reason.value}；事件 {len(result.events)} 个，候选链 {len(result.chains)} 条。', '']
        if result.unprocessed_evidence_ids:
            lines += [f'尚有 {len(result.unprocessed_evidence_ids)} 条新增／更新证据未完成处理。', '']
    lines += ['关系来自模型判断；日期按证据精度显示，未知日期单列，箭头不等于因果。', '']
    groups = [(c.title, [by_id[key] for key in c.event_ids if key in by_id], c) for c in result.chains]
    linked = {e.event_id for _, events, _ in groups for e in events}
    unlinked = [e for e in by_id.values() if e.event_id not in linked]
    if unlinked:
        groups.append(('未串联事件', unlinked, None))
    if not groups:
        lines += ['暂无事件或可展示的时间链。', '']
    for title, events, chain in groups:
        lines += ['## ' + _cell(title), '']
        events = sorted(events, key=event_sort_key)
        labels = {e.event_id: f'E{index}' for index, e in enumerate(events, 1)}
        for dated in (True, False):
            rows = [e for e in events if bool(e.event_date or e.event_time) == dated]
            if not rows:
                continue
            lines += [('### 有日期依据' if dated else '### 时间未确定（不代表排在后面发生）'), '',
                      '| 节点 | 材料时间 | 事件 | 证据 |', '|---|---|---|---|']
            for event in rows:
                citations = []
                priority = [a.evidence_id for a in (event.time_anchor, event.anchor) if a]
                sources = sorted(event.evidence, key=lambda e: priority.index(e.evidence_id) if e.evidence_id in priority else len(priority))
                for evidence in sources[:3]:
                    url = evidence.url or ''
                    source = _cell(evidence.source_name or urlparse(url).hostname or evidence.source)
                    if urlparse(url).scheme in ('http', 'https'):
                        safe_url = url.replace(' ', '%20').replace('(', '%28').replace(')', '%29').replace('<', '%3C').replace('>', '%3E')
                        citations.append(f'[{source}]({safe_url})')
                    else:
                        citations.append(_cell(evidence.evidence_id))
                lines.append(f'| {labels[event.event_id]} | {date_label(event)} | {_cell(event.title)} | {len(event.evidence)} 条；{"、".join(dict.fromkeys(citations))} |')
            lines.append('')
        if chain:
            lines += ['### 关联依据', '']
            for relation in chain.relations:
                if relation.from_event_id not in labels or relation.to_event_id not in labels:
                    continue
                status = '待核实' if relation.status == 'needs_review' else '模型判定有依据'
                lines += [f'- {labels[relation.from_event_id]} → {labels[relation.to_event_id]}：{_RELATIONS[relation.relation_type]}；{status}；置信度 {relation.confidence:g}。{_cell(relation.reason)}']
            lines.append('')
        questions = []
        for question in [*(chain.unresolved_questions if chain else []), *(w for e in events for w in e.time_warnings)]:
            for event in events:
                for key in [event.event_id, *event.merged_event_ids]:
                    question = question.replace(key, labels[event.event_id])
            if question not in questions:
                questions.append(question)
        if questions:
            lines += ['待核实：', '', *['- ' + _cell(q) for q in questions], '']
        merged = sum(len(e.merged_event_ids) for e in events)
        if merged:
            lines += [f'已合并 {merged} 个重复事件 ID；完整引用、原文和 ID 沿革见同目录结果 JSON。', '']
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
