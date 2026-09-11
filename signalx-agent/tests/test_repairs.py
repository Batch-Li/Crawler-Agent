"""真实联调故障的离线回归；无联网、无真实模型调用。"""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from agent import Event, EventDiscoveryWorkflow, SignalXEventProcessor
from agent.evidence_validation import resolve_anchor
from agent.event_time import ground_event_time
from agent.models import Evidence, EvidenceAnchor, PartialDate
from agent.timeline import render_timeline, write_timeline
from agent.tool_models import EventMergeBatch
from agent.tools import EventChainTool, EventMergeTool
from agent.tools.event_extraction import EventExtractionTool
from agent.workflow_models import EventProcessingResult, StopReason
from crawler_tool.monitoring.agent_bridge import item_to_evidence
from tests.test_event_tools import QueueLLM, chain_response, evidence
from tests.test_regressions import LocalLLM, planner, request
from tests.test_workflow import FakeCollector


def timed(text, attempted='2024-09-01T00:00:00Z', anchor=True):
    return Event(event_id='event', title='产品发布', event_time=attempted,
                 evidence=[Evidence(evidence_id='source', title='产品发布', content=text, source='测试')],
                 time_anchor=EvidenceAnchor(evidence_id='source', quote=text) if anchor else None)


def group(events):
    return {'groups': [{'eventIds': [e.event_id for e in events], 'reason': '同款产品同一次发布的多份描述',
                        'confidence': .95, 'evidence': [{'evidenceId': e.evidence[0].evidence_id,
                        'quote': e.evidence[0].title} for e in events]}]}


class RepairTests(unittest.TestCase):
    def test_real_cjk_spacing_difference_rewrites_exact_current_quote(self):
        text = '两年后的答案,至少在华为这里已经越来越明确。9月7日下...'
        anchor = EvidenceAnchor(evidence_id='source', quote='两年后的答案,至少在 华为 这里已经越来越明确。')
        result = resolve_anchor(anchor, [Evidence(evidence_id='source', content=text, source='头条摘要')])
        self.assertEqual(result.quote, '两年后的答案,至少在华为这里已经越来越明确。')
        self.assertIn(result.quote, text)
        self.assertIn('在 华为', anchor.quote)

    def test_quote_matching_keeps_words_numbers_negation_and_punctuation(self):
        for source, quote in [('产品未发布', '产品已发布'), ('Mate XT 2', 'Mate XT'),
                              ('the rapist', 'therapist'), ('价格100元', '价格10元')]:
            # 子串仍可合法引用，唯独完整词语拆并及事实改写不放行。
            if quote == 'Mate XT':
                quote = 'MateXT 2'
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'evidenceId=source'):
                resolve_anchor(EvidenceAnchor(evidence_id='source', quote=quote), [evidence('source', source)])

    def test_precision_is_grounded_in_source_not_model_filled_day(self):
        event = ground_event_time(timed('该产品于2024年9月推出。', anchor=False))
        self.assertIsNone(event.event_time)
        self.assertEqual(event.event_date, PartialDate(value='2024-09', precision='month'))
        self.assertTrue(event.time_warnings)
        self.assertIsNone(ground_event_time(timed('该产品刚刚发布。', anchor=False)).event_date)

    def test_day_year_and_exact_iso_do_not_invent_precision(self):
        for text, value, precision in [('2026年9月7日发布', '2026-09-07', 'day'),
                                        ('2026年 9月 7日发布', '2026-09-07', 'day'),
                                        ('2024-09 推出', '2024-09', 'month'),
                                        ('于2024年推出', '2024', 'year')]:
            event = ground_event_time(timed(text, attempted=None))
            self.assertIsNone(event.event_time)
            self.assertEqual(event.event_date.value, value)
            self.assertEqual(event.event_date.precision, precision)
        event = ground_event_time(timed('发布时刻2026-09-01T09:30:00+08:00。'))
        self.assertEqual(event.event_time.isoformat(), '2026-09-01T01:30:00+00:00')
        self.assertIsNone(event.event_date)

    def test_ambiguous_relative_or_yearless_time_remains_unknown(self):
        for text in ('9月7日12:08预订', '今天发布', '2024年9月发布，2024年10月开售'):
            event = ground_event_time(timed(text, attempted=None))
            self.assertIsNone(event.event_time)
            self.assertIsNone(event.event_date)

    def test_extraction_persists_time_anchor_and_summary_quality(self):
        item = Evidence(evidence_id='source', title='产品发布', snippet='产品于2024年9月推出',
                        source='toutiao', source_name='原发布方', has_full_content=False)
        draft = {'title': '产品发布', 'evidenceIds': ['source'], 'anchor': {'evidenceId': 'source', 'quote': '产品发布'},
                 'eventTime': '2024-09-01T00:00:00Z', 'timeAnchor': {'evidenceId': 'source', 'quote': item.snippet}}
        event = EventExtractionTool(QueueLLM([{'events': [draft]}])).invoke({'evidence': [item]}).events[0]
        self.assertEqual(event.event_date.value, '2024-09')
        self.assertIsNone(event.event_time)
        self.assertFalse(event.evidence[0].has_full_content)

    def test_merge_keeps_identity_evidence_and_remaps_old_chain(self):
        events = [Event(event_id=key, title='同款产品发布', evidence=[evidence(key, '同款产品发布')]) for key in ('old', 'duplicate', 'next')]
        merged = EventMergeTool(QueueLLM([group(events[:2])])).invoke(events)
        self.assertEqual([e.event_id for e in merged], ['old', 'next'])
        self.assertEqual(merged[0].merged_event_ids, ['duplicate'])
        self.assertEqual(len(merged[0].evidence), 2)
        payload = chain_response(events)
        result = EventChainTool(QueueLLM([payload])).invoke({'events': merged, 'classifications': [
            {'eventId': e.event_id, 'category': 'PRODUCT_LAUNCH', 'confidence': .9} for e in merged]})
        self.assertEqual(len(result.chains[0].relations), 1)
        self.assertEqual(set(result.chains[0].event_ids), {'old', 'next'})
        self.assertEqual(len(events[0].evidence), 1)

    def test_merge_rejects_bad_ids_missing_evidence_low_confidence_and_dates(self):
        events = [Event(event_id=key, title='产品发布', evidence=[evidence(key, '产品发布')]) for key in ('one', 'two')]
        for kind in ('unknown', 'low', 'missing', 'overlap', 'conflict'):
            payload, source = group(events), deepcopy(events)
            if kind == 'unknown': payload['groups'][0]['eventIds'][1] = 'invented'
            elif kind == 'low': payload['groups'][0]['confidence'] = .5
            elif kind == 'missing': payload['groups'][0]['evidence'].pop()
            elif kind == 'overlap': payload['groups'].append(deepcopy(payload['groups'][0]))
            else:
                source[0].event_date = PartialDate(value='2024-09', precision='month')
                source[1].event_date = PartialDate(value='2026-09', precision='month')
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                EventMergeTool.materialize(source, EventMergeBatch.model_validate(payload))

    def test_workflow_accepts_audited_merge_of_prior_event(self):
        class Model(LocalLLM):
            def generate_json(self, system, user):
                if '同一事件复核工具' in system:
                    self.calls.append((system, user))
                    context = json.loads(user.split('输入：\n', 1)[1])
                    return group([Event.model_validate(e) for e in context['events']])
                return super().generate_json(system, user)
        llm = Model()
        workflow = EventDiscoveryWorkflow(FakeCollector({'查询一': [evidence('one', '产品发布')],
                                                         '查询二': [evidence('two', '同一产品推出')]}),
                                         SignalXEventProcessor.from_llm(llm), planner('查询一', '查询二'))
        result = workflow.run(request())
        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].event_id, result.rounds[0].snapshot.events[0].event_id)
        self.assertEqual(len(result.events[0].merged_event_ids), 1)
        self.assertEqual(result.llm_calls, 5)

    def test_bridge_preserves_provenance_without_labeling_summary_as_body(self):
        mapped = item_to_evidence({'contentId': 'id', 'title': '标题', 'platform': 'toutiao', 'content': '搜索摘要',
                                  'quality': {'hasFullContent': False, 'warnings': ['search_result_summary_only']},
                                  'ext': {'sourceName': '原始发布方'}})
        event = Evidence.model_validate(mapped)
        self.assertIsNone(event.content)
        self.assertEqual(event.snippet, '搜索摘要')
        self.assertFalse(event.has_full_content)
        self.assertEqual(event.source_name, '原始发布方')

    def test_merge_cannot_hide_discarded_historical_evidence(self):
        first, second = evidence('first', '原报道'), evidence('second', '新报道')
        previous = EventProcessingResult(events=[Event(event_id='old', title='事件', evidence=[first])])
        candidate = EventProcessingResult(events=[Event(event_id='new', title='事件', evidence=[second], merged_event_ids=['old'])])
        with self.assertRaisesRegex(ValueError, '保留其证据'):
            EventDiscoveryWorkflow._validate_snapshot(candidate, previous, {'first': first, 'second': second})

    def test_timeline_is_readable_partial_dates_and_refuses_overwrite(self):
        snapshot = EventProcessingResult(events=[ground_event_time(timed('该产品于2024年9月推出。', anchor=False))])
        text = render_timeline(snapshot, note='真实数据离线修复；非新增真实模型验收')
        self.assertIn('2024-09（仅知月）', text)
        self.assertNotIn('2024-09-01', text)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'timeline.md'
            write_timeline(snapshot, path)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                write_timeline(snapshot, path)
            self.assertEqual(path.read_bytes(), original)

    def test_previous_chain_prompt_excludes_stale_quotes(self):
        events = [Event(event_id=key, title='事件', evidence=[evidence(key, '事件')]) for key in ('one', 'two')]
        data = {'events': events, 'classifications': [{'eventId': e.event_id, 'category': 'OTHER', 'confidence': .9} for e in events]}
        first = EventChainTool(QueueLLM([chain_response(events)])).invoke(data)
        class Model:
            def generate_json(self, system, user):
                self.context = json.loads(user.split('输入：\n', 1)[1])
                return {'chains': []}
        model = Model()
        EventChainTool(model).invoke({**data, 'previousChains': first.chains})
        self.assertEqual(set(model.context['previousChains'][0]), {'chainId', 'title', 'eventIds'})


if __name__ == '__main__':
    unittest.main()
