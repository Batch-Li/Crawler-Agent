"""主题覆盖、分页边界与自动报告；测试不调用真实网络或模型。"""
import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from agent.models import Evidence, Event, EvidenceAnchor
from agent.report import render_report, write_report
from agent.timeline import render_timeline
from agent.tools.evidence_relevance import EvidenceRelevanceTool
from agent.tools.event_extraction import EventExtractionTool, EventExtractionToolError
from agent.tools.event_chain import EventChainTool, EventChainToolError
from agent.llm import ModelBudgetExceeded, model_call_budget
from agent.workflow import EventDiscoveryWorkflow
from agent.workflow_models import WorkflowRequest, WorkflowResult, StopReason
from tests.test_workflow import FakeCollector, FakeProcessor
from agent.query_planner import LLMQueryPlanner
from tests.test_live_regressions import ReviewLLM
from tests.test_crawler_integration import item, FixtureLLM


class TopicTests(unittest.TestCase):
    def test_singleton_chain_becomes_unlinked_but_unknown_id_is_rejected(self):
        events = [Event(event_id=str(i), title='报道线索', evidence=[Evidence(
            evidence_id=str(i), title='报道线索', source='test')]) for i in range(2)]
        payload = {'chains': [{'title': '独立线索', 'eventIds': ['0'], 'relations': [],
                               'unresolvedQuestions': ['缺少关联依据']}]}
        request = {'events': events, 'classifications': [
            {'eventId': e.event_id, 'category': 'OTHER', 'confidence': 0.5} for e in events]}
        original = json.dumps(payload)
        result = EventChainTool(ReviewLLM([payload]), max_attempts=1).invoke(request)
        self.assertEqual(result.chains, [])
        self.assertEqual(result.unlinked_event_ids, ['0', '1'])
        self.assertIn('缺少关联依据', result.unresolved_questions)
        self.assertEqual(json.dumps(payload), original)
        payload['chains'][0]['eventIds'] = ['unknown']
        with self.assertRaises(EventChainToolError):
            EventChainTool(ReviewLLM([payload]), max_attempts=1).invoke(request)
    def test_invalid_large_batch_splits_without_relaxing_anchors_or_budget(self):
        rows = [Evidence(evidence_id=str(i), title=f'新闻{i}', source='test') for i in range(6)]
        good = lambda group: {'events': [{'title': x.title, 'evidenceIds': [x.evidence_id],
                                         'anchor': {'evidenceId': x.evidence_id, 'quote': x.title}} for x in group]}
        replies = [{'events': [{'title': '错误'}]}, good(rows[:3]), good(rows[3:])]
        with model_call_budget(3):
            result = EventExtractionTool(ReviewLLM(replies), max_attempts=1).invoke({'evidence': rows})
        self.assertEqual(len(result.events), 6)
        with model_call_budget(1), self.assertRaises(ModelBudgetExceeded):
            EventExtractionTool(ReviewLLM(replies), max_attempts=1).invoke({'evidence': rows})
        with model_call_budget(1), self.assertRaises(EventExtractionToolError):
            EventExtractionTool(ReviewLLM([{'events': [{'title': '错误'}]}]), max_attempts=1).invoke({'evidence': rows[:1]})
    def test_topic_monitor_cli_exports_raw_report_and_preserves_previous_run(self):
        from agent.crawler_cli import main
        def post(url, *, json, timeout):
            return httpx.Response(200, json={'items': [item()],
                'sourceReports': [{'platform': 'cctv_news', 'status': 'success', 'count': 1}]},
                request=httpx.Request('POST', url))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config'
            config.mkdir()
            (config / 'queries.json').write_text(json.dumps({'queries': ['智言科技'], 'platforms': ['cctv_news']}))
            args = ['monitor', '--live', '--industry', '手机', '--topic', '智言科技',
                    '--config-dir', str(config), '--data-dir', str(root / 'monitoring'),
                    '--crawl-dir', str(root / 'crawl'), '--target-evidence', '1']
            with patch('agent.crawler_cli.DeepSeekLLM', return_value=FixtureLLM()), \
                 patch('crawler_tool.monitoring.agent_bridge.httpx.post', side_effect=post), \
                 contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(args), 0)
                first = Path(json.loads(stdout.getvalue())['outputDirectory'])
                original = {p.name: p.read_bytes() for p in first.iterdir()}
                self.assertEqual(set(original), {'result.json', 'timeline.md', 'report.md', 'search_responses.jsonl'})
                self.assertEqual(main(args), 0)
                self.assertEqual(len(list((root / 'monitoring/topics').iterdir())), 2)
                self.assertEqual(original, {p.name: p.read_bytes() for p in first.iterdir()})
                self.assertIn('智言科技', (first / 'report.md').read_text())

    def test_topic_keeps_different_actions_without_model_or_single_seed(self):
        titles = ['iPhone Duo 应用双开', 'iPhone Duo Max / Mini版曝光', '国内应用适配 iPhone Duo',
                  'iPhone-Duo 供货预测']
        rows = [Evidence(evidence_id=str(i), title=t, source='toutiao') for i, t in enumerate(titles)]
        req = WorkflowRequest(mode='BULK_DISCOVERY', industry='手机', topic='iPhone Duo', preset_keywords=['iPhone Duo'])
        llm = ReviewLLM([])
        self.assertTrue(all(r.decision == 'relevant' for r in EvidenceRelevanceTool(llm).review(req, rows)))
        self.assertEqual(llm.calls, 0)

    def test_bulk_topic_filter_rejects_noise_and_keeps_all_preset_queries(self):
        relevant = Evidence(evidence_id='topic', title='iPhone Duo 应用适配', source='toutiao')
        noise = Evidence(evidence_id='noise', title='Apple Watch Dynamic duo', source='toutiao')
        llm = ReviewLLM([{'reviews': [{'evidenceId': 'noise', 'decision': 'irrelevant', 'reason': '手表宣传语'}]},
                        {'shouldContinue': False, 'reason': '预设查询处理完成'}])
        flow = EventDiscoveryWorkflow(FakeCollector({'手机': [noise], '适配': [relevant]}), FakeProcessor(),
                                     LLMQueryPlanner(llm), EvidenceRelevanceTool(llm))
        result = flow.run(WorkflowRequest(mode='BULK_DISCOVERY', industry='手机', topic='iPhone Duo',
                                         preset_keywords=['手机', '适配'], per_round_query_limit=1))
        self.assertEqual(result.collection_calls, 2)
        self.assertEqual([e.evidence_id for e in result.evidence], ['topic'])
        self.assertEqual(len(result.filtered_evidence), 1)

    def test_summary_only_title_remains_attributed(self):
        ev = Evidence(evidence_id='one', title='iPhone Duo 发布', source='toutiao', has_full_content=False)
        llm = ReviewLLM([{'events': [{'title': ev.title, 'evidenceIds': ['one'],
                                    'anchor': {'evidenceId': 'one', 'quote': ev.title}}]}])
        output = EventExtractionTool(llm).invoke({'evidence': [ev]})
        self.assertEqual(output.events[0].title, '报道称：iPhone Duo 发布')
        self.assertEqual(ev.title, 'iPhone Duo 发布')

    def test_report_uses_publication_chronology_without_guessing_event_year(self):
        ev = Evidence(evidence_id='one', title='iPhone Duo 价格\u200b ￥??.??', source='toutiao',
                      url='https://example.com/news', snippet='9月10日发布', has_full_content=False,
                      published_at='2026-09-17T00:00:00Z', quality_warnings=['published_at_relative'])
        event = Event(event_id='evt_internal', title='报道称：新品发布', evidence=[ev],
                      time_anchor=EvidenceAnchor(evidence_id='one', quote='9月10日发布'))
        result = WorkflowResult(mode='BULK_DISCOVERY', industry='手机', topic='iPhone Duo',
                                target_evidence_count=30, evidence=[ev], events=[event], classifications=[],
                                chains=[], rounds=[], stop_reason=StopReason.TOOL_BUDGET_EXHAUSTED)
        original = result.model_dump_json()
        report = render_report(result)
        self.assertIn('约 2026-09-17', report)
        self.assertIn('9月10日（年份未明）', report)
        self.assertIn('未达到', report)
        self.assertNotIn('2026-09-10', report)
        self.assertNotIn('TOOL_BUDGET', report)
        self.assertNotIn('\u200b', report)
        self.assertNotIn('??', report)
        self.assertNotIn('evt_internal', report)
        self.assertNotIn('TOOL_BUDGET', render_timeline(result))
        self.assertEqual(result.model_dump_json(), original)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.md'
            write_report(result, path)
            with self.assertRaises(FileExistsError):
                write_report(result, path)


class PaginationTests(unittest.TestCase):
    def test_single_source_cursor_dedup_and_raw_archive(self):
        from crawler_tool.monitoring.agent_bridge import SearchContentCollectionTool
        calls = []
        def post(url, *, json, timeout):
            calls.append(json)
            page = json.get('cursor')
            rows = [item('a')] if page is None else [item('a'), item('b')]
            payload = {'items': rows, 'sourceReports': [{'platform': json['platforms'][0], 'status': 'success'}],
                       'nextCursor': 'next' if page is None else None}
            return httpx.Response(200, json=payload, request=httpx.Request('POST', url))
        with tempfile.TemporaryDirectory() as tmp, patch('crawler_tool.monitoring.agent_bridge.httpx.post', side_effect=post):
            archive = Path(tmp) / 'raw.jsonl'
            tool = SearchContentCollectionTool(platforms=['toutiao'], max_pages=3, archive_path=archive)
            result = tool.search_with_reports({'query': 'iPhone Duo', 'limit': 20})
            self.assertEqual(len(result['evidence']), 2)
            self.assertEqual(result['requestCount'], 2)
            self.assertEqual(calls[1]['cursor'], 'next')
            self.assertEqual(calls[0]['platforms'], ['toutiao'])
            raw = [json.loads(line) for line in archive.read_text().splitlines()]
            self.assertEqual(len(raw[1]['response']['items']), 2)

    def test_request_budget_and_failed_page_preserve_prior_results(self):
        from crawler_tool.monitoring.agent_bridge import SearchContentCollectionTool
        calls = []
        def post(url, *, json, timeout):
            calls.append(json)
            if len(calls) == 2:
                raise httpx.ReadTimeout('PRIVATE_TEST_VALUE')
            return httpx.Response(200, json={'items': [item('a')], 'nextCursor': 'next',
                'sourceReports': [{'platform': 'toutiao', 'status': 'success'}]}, request=httpx.Request('POST', url))
        with patch('crawler_tool.monitoring.agent_bridge.httpx.post', side_effect=post):
            tool = SearchContentCollectionTool(platforms=['toutiao'], max_pages=5, max_requests=2)
            result = tool.search_with_reports({'query': 'iPhone Duo', 'limit': 20})
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(len(result['evidence']), 1)
            tool.search_with_reports({'query': '价格', 'limit': 20})
            self.assertEqual(len(calls), 2)
            self.assertNotIn('PRIVATE_TEST_VALUE', str(result))

    def test_repeated_cursor_stops_without_duplicate_items(self):
        from crawler_tool.monitoring.agent_bridge import SearchContentCollectionTool
        with patch('crawler_tool.monitoring.agent_bridge.httpx.post') as post:
            post.return_value = httpx.Response(200, json={'items': [item('a')], 'nextCursor': 'same',
                'sourceReports': [{'platform': 'toutiao', 'status': 'success'}]}, request=httpx.Request('POST', 'http://localhost'))
            result = SearchContentCollectionTool(platforms=['toutiao'], max_pages=5).search_with_reports({'query': 'x', 'limit': 20})
            self.assertEqual(post.call_count, 2)
            self.assertEqual(len(result['evidence']), 1)


class BulkCollectionTests(unittest.TestCase):
    def test_story_transport_failures_stop_after_two_without_hiding_budget(self):
        class APITimeoutError(RuntimeError):
            pass
        class Model:
            calls = 0
            def generate_json(self, *args):
                self.calls += 1
                if self.calls == 1:
                    return {'groups': [[0, 1], [2, 3], [4, 5]]}
                raise APITimeoutError('simulated')
        events = [Event(event_id=f'e{i}', title='线索', evidence=[Evidence(evidence_id=f's{i}', title='线索', source='test')]) for i in range(31)]
        request = {'events': events, 'classifications': [
            {'eventId': e.event_id, 'category': 'OTHER', 'confidence': .9} for e in events]}
        model = Model()
        result = EventChainTool(model).invoke(request)
        self.assertEqual(model.calls, 3)
        self.assertTrue(result.review_incomplete)
        self.assertEqual(len(result.unlinked_event_ids), 31)
        with model_call_budget(1), self.assertRaises(ModelBudgetExceeded):
            EventChainTool(Model()).invoke(request)

    def test_oversized_story_candidate_is_split_with_shared_boundary(self):
        from tests.test_event_tools import QueueLLM
        events = [Event(event_id=f'e{i}', title='线索', evidence=[Evidence(evidence_id=f's{i}', title='线索', source='test')]) for i in range(31)]
        model = QueueLLM([{'groups': [list(range(13))]}, {'chains': []}, {'chains': []}])
        result = EventChainTool(model).invoke({'events': events, 'classifications': [
            {'eventId': e.event_id, 'category': 'OTHER', 'confidence': .9} for e in events]})
        self.assertEqual(model.calls, 3)
        self.assertEqual(len(result.unlinked_event_ids), 31)
        self.assertFalse(result.review_incomplete)

    def test_invalid_merge_candidate_preserves_nodes_and_flags_incomplete_review(self):
        from agent.tools.event_merge import EventMergeTool
        events = [Event(event_id=f'e{i}', title='线索', evidence=[Evidence(evidence_id=f's{i}', title='线索', source='test')]) for i in range(41)]
        llm = ReviewLLM([{'candidates': [[0, 1]]}, {'groups': [{'eventIds': ['unknown']}]}])
        result = EventMergeTool(llm, max_attempts=1).invoke(events)
        self.assertEqual(len(result), 41)
        self.assertTrue(all(e.merge_review_pending for e in result[:2]))
        self.assertFalse(any(e.merge_review_pending for e in events))

    def test_invalid_story_candidate_keeps_independent_nodes_without_false_relations(self):
        events = [Event(event_id=f'e{i}', title='线索', evidence=[Evidence(evidence_id=f's{i}', title='线索', source='test')]) for i in range(31)]
        model = ReviewLLM([{'groups': [[0, 1]]}, {'chains': [{'title': '缺少引用'}]}])
        result = EventChainTool(model, max_attempts=1).invoke({'events': events, 'classifications': [
            {'eventId': e.event_id, 'category': 'OTHER', 'confidence': .9} for e in events]})
        self.assertTrue(result.review_incomplete)
        self.assertEqual(len(result.unlinked_event_ids), 31)
        self.assertEqual(result.chains, [])

    def test_large_story_candidates_are_verified_with_original_evidence(self):
        from tests.test_event_tools import QueueLLM, chain_response
        events = [Event(event_id=f'e{i}', title=f'动作{i}', evidence=[Evidence(evidence_id=f's{i}', title=f'动作{i}', source='test')]) for i in range(31)]
        model = QueueLLM([{'groups': [[0, 1]]}, chain_response(events[:2], 'FOLLOW_UP')])
        result = EventChainTool(model).invoke({'events': events, 'classifications': [
            {'eventId': e.event_id, 'category': 'OTHER', 'confidence': .9} for e in events]})
        self.assertEqual(len(result.chains), 1)
        self.assertEqual(len(result.unlinked_event_ids), 29)
        self.assertEqual(model.calls, 3)
        self.assertEqual(result.chains[0].relations[0].status, 'supported')

    def test_popular_event_audit_preserves_all_sources_and_corrections(self):
        from agent.tools.event_grounding import EventGroundingTool
        from agent.models import EvidenceAssessment
        sizes = []
        class LLM:
            def generate_json(self, system, prompt):
                context = json.JSONDecoder().raw_decode(prompt.split('输入：\n')[1])[0]
                sizes.append(len(context['sources']))
                event = context['events'][0]
                return {'corrections': [{'eventId': event['eventId'], 'evidenceAssessments': [
                    {'evidenceId': key, 'quote': '发布', 'stance': 'context', 'reason': '背景'}
                    for key in event['evidenceIds']]}], 'revisions': []}
        event = Event(event_id='popular', title='发布', time_reviewed=True,
            evidence=[Evidence(evidence_id=str(i), title='发布', source='test') for i in range(46)],
            evidence_assessments=[EvidenceAssessment(evidence_id=str(i), quote='发布', stance='support', reason='初审') for i in range(46)])
        result = EventGroundingTool(LLM()).audit([event])[0]
        self.assertEqual(sizes, [15, 15, 15, 1])
        self.assertEqual(len(result.evidence), 46)
        self.assertEqual(len(result.evidence_assessments), 46)
        self.assertTrue(all(a.stance == 'context' for a in result.evidence_assessments))
        self.assertTrue(all(a.stance == 'support' for a in event.evidence_assessments))

    def test_large_merge_candidates_require_full_evidence_verification(self):
        from agent.tools.event_merge import EventMergeTool
        events = [Event(event_id=f'e{i}', title='发布', evidence=[Evidence(evidence_id=f's{i}', title='发布', source='test')]) for i in range(41)]
        llm = ReviewLLM([{'candidates': [[0, 1]]}, {'groups': [{
            'eventIds': ['e0', 'e1'], 'reason': '同一发布', 'confidence': .95,
            'evidence': [{'evidenceId': 's0', 'quote': '发布'}, {'evidenceId': 's1', 'quote': '发布'}]}]}])
        result = EventMergeTool(llm).invoke(events)
        self.assertEqual(len(result), 40)
        self.assertEqual(len(result[0].evidence), 2)
        self.assertEqual(result[0].merged_event_ids, ['e1'])
        self.assertEqual(llm.calls, 2)

    def test_overlapping_merge_candidates_do_not_merge_without_verification(self):
        from agent.tools.event_merge import EventMergeTool
        events = [Event(event_id=f'e{i}', title='线索', evidence=[Evidence(evidence_id=f's{i}', title='线索', source='test')]) for i in range(41)]
        llm = ReviewLLM([{'candidates': [[0, 1], [1, 2]]}, {'groups': []}])
        self.assertEqual(EventMergeTool(llm).invoke(events), events)
        self.assertEqual(llm.calls, 2)

    def test_source_challenge_stops_later_queries_and_preserves_other_source(self):
        from crawler_tool.monitoring.agent_bridge import SearchContentCollectionTool
        def post(url, *, json, timeout):
            p = json['platforms'][0]
            return httpx.Response(200, json={'items': [item()] if p == 'cctv_news' else [],
                'status': 'success' if p == 'cctv_news' else 'failed',
                'sourceReports': [{'platform': p, 'status': 'success' if p == 'cctv_news' else 'authentication_required'}]},
                request=httpx.Request('POST', url))
        with patch('crawler_tool.monitoring.agent_bridge.httpx.post', side_effect=post) as mock:
            tool = SearchContentCollectionTool(platforms=['toutiao', 'cctv_news'], max_pages=3)
            tool.search_with_reports({'query': 'first', 'limit': 100})
            result = tool.search_with_reports({'query': 'second', 'limit': 100})
            self.assertEqual(mock.call_count, 3)
            self.assertEqual(result['requestCount'], 1)
            self.assertEqual(len(result['evidence']), 1)
            self.assertEqual(tool.blocked_sources, {'toutiao': 'authentication_required'})

    def test_collect_all_queries_and_load_interrupted_raw_records(self):
        from agent.bulk_collection import collect, load_crawl_evidence
        from agent.crawler_integration import CrawlerCollectionTool
        from crawler_tool.monitoring.agent_bridge import SearchContentCollectionTool
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp) / 'run'
            tool = SearchContentCollectionTool(search_fn=lambda q, limit: {'items': [item(q)]})
            result = collect(CrawlerCollectionTool(tool), queries=['a', 'b', 'a'], industry='手机', output_dir=root, delay=0)
            self.assertEqual(result['completedQueries'], 2)
            self.assertEqual(len(json.loads((root / 'evidence.json').read_text())), 2)
            with self.assertRaises(FileExistsError):
                collect(CrawlerCollectionTool(tool), queries=['c'], industry='手机', output_dir=root, delay=0)
            a = item('one', title='iPhone Duo 发布', ext={'accountName': '作者'}, publishedAt='2026-09-10T01:00:00Z')
            b = {**a, 'contentId': 'two', 'title': 'iPhone  Duo 发布', 'dedup': {'dedupKey': 'changed-url'}}
            (root / 'search_responses.jsonl').write_text(json.dumps({'response': {'items': [a, b]}}))
            evidence = load_crawl_evidence(root)
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0].source_name, '作者')
            self.assertEqual(evidence[0].evidence_id, 'one')

    def test_large_relevance_review_is_complete_and_bounded(self):
        sizes = []
        class LLM:
            def generate_json(self, system, prompt):
                data = json.JSONDecoder().raw_decode(prompt.split('输入：\n')[1])[0]
                sizes.append(len(data['evidence']))
                return {'reviews': [{'evidenceId': e['evidenceId'], 'decision': 'irrelevant', 'reason': '其他主题'} for e in data['evidence']]}
        rows = [Evidence(evidence_id=str(i), title='其他新闻', source='test') for i in range(71)]
        request = WorkflowRequest(mode='BULK_DISCOVERY', industry='手机', topic='目标', preset_keywords=['目标'])
        self.assertEqual(len(EvidenceRelevanceTool(LLM()).review(request, rows)), 71)
        self.assertEqual(sizes, [30, 30, 11])

    def test_popular_event_reviews_all_sources_in_small_batches(self):
        from agent.tools.event_grounding import EventGroundingTool
        from tests.test_event_tools import grounding_response
        sizes = []
        class LLM:
            def generate_json(self, system, prompt):
                sizes.append(sum(len(e['evidence']) for e in json.JSONDecoder().raw_decode(prompt.split('输入：\n')[1])[0]))
                return grounding_response(prompt)
        event = Event(event_id='popular', title='发布', evidence=[Evidence(evidence_id=str(i), title='发布', source='test') for i in range(46)])
        result = EventGroundingTool(LLM())._review([event])[0]
        self.assertEqual(sizes, [15, 15, 15, 1])
        self.assertEqual(len(result.evidence_assessments), 46)
        self.assertEqual(len(event.evidence_assessments), 0)


if __name__ == '__main__':
    unittest.main()
