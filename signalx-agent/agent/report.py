"""由运行结果自动生成面向人的主题新闻报告；不改变任何原始材料。"""
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from .models import Evidence
from .timeline import STOP_LABELS, _cell, date_label
from .workflow_models import WorkflowResult

PLATFORMS = {'toutiao': '今日头条搜索', 'south_weekend': '南方周末', 'cctv_news': '央视新闻',
             'weibo': '微博', 'sogou_wechat': '搜狗微信'}
STATUSES = {'success': '正常', 'empty': '无结果', 'network_error': '网络失败',
            'authentication_required': '需要登录或验证', 'rate_limited': '限流',
            'parser_changed': '页面结构变化', 'source_unavailable': '不可用', 'failed': '失败'}


def source_link(item: Evidence, title: str | None = None) -> str:
    label = _cell(title or item.source_name or urlparse(item.url or '').hostname or PLATFORMS.get(item.source, item.source))
    if urlparse(item.url or '').scheme not in ('http', 'https'):
        return label
    url = item.url.replace(' ', '%20').replace('(', '%28').replace(')', '%29').replace('<', '%3C').replace('>', '%3E')
    return f'[{label}]({url})'


def publication_label(item: Evidence) -> str:
    if item.published_at is None:
        return '来源未提供'
    approximate = any(x in item.quality_warnings for x in ('published_at_relative', 'published_at_year_inferred'))
    return ('约 ' if approximate else '') + item.published_at.date().isoformat()


def render_report(result: WorkflowResult) -> str:
    collected = [e for row in result.rounds for e in row.collected_evidence]
    unique = {e.dedup_key or e.evidence_id for e in collected}
    lines = [f'# {_cell(result.topic or result.industry)} 监测报告', '',
             f'本轮：{STOP_LABELS[result.stop_reason.value]}。', '',
             f'- 检索返回 {len(collected)} 条，跨查询去重 {len(unique)} 条；保留相关材料 {len(result.evidence)} 条。',
             f'- 归并为 {len(result.events)} 个事件线索、{len(result.chains)} 条候选关联链。',
             f'- 执行 {result.collection_calls} 个查询、{sum(c.request_count for r in result.rounds for c in r.tool_calls)} 次采集接口请求、{result.llm_calls} 次模型调用。']
    if result.collection_mode == 'saved_evidence':
        lines[-1] = f'- 本次重处理已保存材料，未重新访问新闻网站；读取 {result.collection_calls} 个文件批次，调用模型 {result.llm_calls} 次。'
        lines.append('- 本次完成状态只反映材料重处理；原采集阶段的来源失败和覆盖范围仍以输入运行记录为准。')
    if result.processing_mode == 'event_refinement':
        lines[4] = f'- 沿用输入结果中的 {len(result.evidence)} 条材料，仅复核已有事件与关系；未重新采集或抽取。'
        lines[6] = f'- 本次调用模型 {result.llm_calls} 次；输入结果指纹见 inputResultSha256。'
    if result.replayed_llm_calls:
        lines[6] = f'- 重放 {result.replayed_llm_calls} 次已保存响应，新调用模型 {result.llm_calls - result.replayed_llm_calls} 次；来源指纹见 responseReplaySha256。'
    if result.unprocessed_evidence_ids:
        lines.append(f'- 尚有 {len(result.unprocessed_evidence_ids)} 条材料未完成事件处理；事件概览保留上一次成功快照。')
    if result.target_evidence_count:
        hit = len(result.evidence) >= result.target_evidence_count
        lines.append(f'- 相关材料目标 {result.target_evidence_count} 条：' + ('已达到。' if hit else '未达到，当前覆盖不足。'))
    if result.stop_message:
        lines.append('- ' + _cell(result.stop_message))
    summary_only = sum(e.has_full_content is False for e in result.evidence)
    undated = sum(e.published_at is None for e in result.evidence)
    lines += ['', '## 证据范围', '',
              f'相关材料中 {summary_only} 条为搜索摘要，{undated} 条缺少报道时间。下文整理媒体说法，不把多篇转载视为独立核实。',
              '新闻清单按报道日期排序（约表示相对时间换算）；事件日期单独展示，二者不互相替代。', '',
              '## 事件概览', '']
    for i, event in enumerate(result.events, 1):
        lines.append(f'{i}. **{_cell(event.title)}**（事件日期：{date_label(event)}；{len(event.evidence)} 条依据）')
        if event.summary:
            lines.append('   ' + _cell(event.summary))
    if not result.events:
        lines.append('本轮尚未形成事件；已采集材料见下表。')
    lines += ['', '## 新闻清单', '', '| 报道日期（UTC） | 标题／原文 | 发布方 | 材料类型 |', '|---|---|---|---|']
    for item in sorted(result.evidence, key=lambda e: (e.published_at is None, -(e.published_at.timestamp() if e.published_at else 0), e.evidence_id)):
        kind = '全文' if item.has_full_content is True else '搜索摘要' if item.has_full_content is False else '未标注'
        lines.append(f'| {publication_label(item)} | {source_link(item, item.title or "无标题")} | {_cell(item.source_name or PLATFORMS.get(item.source, item.source))} | {kind} |')
    lines += ['', '## 来源与采集边界', '']
    counts = Counter((s.platform, s.status) for row in result.rounds for call in row.tool_calls for s in call.source_reports)
    for platform in sorted({p for p, _ in counts}):
        status = '、'.join(f'{STATUSES.get(s, "异常")} {n} 次' for (p, s), n in counts.items() if p == platform)
        lines.append(f'- {PLATFORMS.get(platform, platform)}：{status}。')
    notes = list(dict.fromkeys(n for row in result.rounds for call in row.tool_calls for n in call.collection_notes))
    for note in notes:
        for key, value in PLATFORMS.items():
            note = note.replace(key, value)
        lines.append('- ' + _cell(note))
    if summary_only:
        lines.append('- 当前采集接口未提供这些材料的正文，需采集端补充正文与准确发布时间后进一步核验。')
    questions = list(dict.fromkeys(result.unresolved_questions))
    if questions:
        lines += ['', '## 尚缺的证据', '']
        for question in questions:
            for event in result.events:
                question = question.replace(event.event_id, event.title)
            lines.append('- ' + _cell(question))
    lines += ['', '## 原始文件', '', '- [事件时间链](timeline.md)', '- [完整结构化结果](result.json)']
    if result.collection_mode == 'live_search':
        lines.append('- [搜索接口原始响应](search_responses.jsonl)')
    lines.append('')
    return '\n'.join(lines)


def write_report(result: WorkflowResult, path: Path) -> None:
    with path.open('x', encoding='utf-8') as stream:
        stream.write(render_report(result))
