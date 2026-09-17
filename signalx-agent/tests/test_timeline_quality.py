"""时间链质量回归：原子节点、年月精度、语义删边与可读输出。"""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from agent.event_time import ground_event_time, time_bounds
from agent.discovery_demo import refine_result
from agent.llm import ModelBudgetExceeded, ResponsePrefixLLM, model_call_budget, request_digest
from agent.models import Event, Evidence, EvidenceAnchor, PartialDate
from agent.timeline import render_timeline
from agent.tool_models import EventMergeBatch, EventGroundingBatch, EventGroundingAuditBatch
from agent.tools.event_grounding import EventGroundingTool
from agent.tools.event_chain import EventChainTool, EventChainToolError
from agent.tools.event_extraction import EventExtractionTool, EventExtractionToolError
from agent.tools.event_merge import EventMergeTool
from agent.workflow_models import EventChain, EventProcessingResult, WorkflowResult, StopReason
from tests.test_event_tools import chain_response, QueueLLM
from tests.test_live_regressions import ReviewLLM


def event(key, text, **kwargs):
    return ground_event_time(Event(event_id=key, title=text,
        evidence=[Evidence(evidence_id=key, title=text, source='测试来源')],
        anchor=EvidenceAnchor(evidence_id=key, quote=text),
        time_anchor=EvidenceAnchor(evidence_id=key, quote=text), **kwargs))


def request(events):
    return {'events': events, 'classifications': [
        {'eventId': e.event_id, 'category': 'OTHER', 'confidence': .9} for e in events]}


class TimelineQualityTests(unittest.TestCase):
    def test_goal_year_after_relative_milestone_is_not_occurrence_year(self):
        text = '新品发布后24小时内，将2027年供货目标提高至1500万'
        result = self.grounded([text], [self.candidate(0, text)])
        self.assertIsNone(result.event_date)
        self.assertEqual(result.target_date.value, '2027')
        self.assertEqual(result.time_candidates[0].role, 'target')

    def test_low_confidence_review_keep_does_not_promote_edge(self):
        events = [event('a', '消息一'), event('b', '消息二')]
        payload = chain_response(events, 'FOLLOW_UP')
        payload['chains'][0]['relations'][0]['confidence'] = .75
        output = EventChainTool(QueueLLM([payload])).invoke(request(events))
        self.assertEqual(output.chains[0].relations[0].status, 'needs_review')
        self.assertNotIn('→', render_timeline(EventProcessingResult(events=events, chains=output.chains)))

    def test_processor_preserves_new_nodes_added_by_grounding_audit(self):
        from agent.event_processor import SignalXEventProcessor
        from tests.test_event_tools import ProcessorLLM
        first, new = event('first', '现有功能'), event('new', '未来功能计划', claim_type='forecast')
        class Grounder:
            def invoke(self, events):
                return [events[0], new]
        processor = SignalXEventProcessor.from_llm(ProcessorLLM())
        processor.grounder = Grounder()
        from tests.test_regressions import LocalLLM
        from agent.tools.event_classification import EventClassificationTool
        processor.classifier = EventClassificationTool(LocalLLM())
        processor.chain_builder = EventChainTool(LocalLLM())
        output = processor.process([], previous=EventProcessingResult(events=[first]))
        self.assertEqual({e.event_id for e in output.events}, {'first', 'new'})
        self.assertEqual({c.event_id for c in output.classifications}, {'first', 'new'})

    def test_audit_removes_borrowed_release_date_without_losing_material(self):
        text = '9月10日发布新品，黄牛加价数千元'
        original = self.grounded([text], [self.candidate(0, '9月10日发布新品')])
        before = original.model_dump_json()
        audit = EventGroundingAuditBatch(corrections=[{'eventId': 'e', 'discardTimeCandidateIndices': [0]}])
        result = EventGroundingTool.apply_audit([original], audit)[0]
        self.assertIsNone(result.event_date)
        self.assertIsNone(result.time_anchor)
        self.assertEqual(result.evidence, original.evidence)
        self.assertEqual(original.model_dump_json(), before)
        audit.corrections[0].discard_time_candidate_indices = [3]
        with self.assertRaises(ValueError):
            EventGroundingTool.apply_audit([original], audit)

    def test_audit_cannot_invent_or_duplicate_stance_evidence(self):
        original = self.grounded(['支持不同应用同屏'], [])
        patch = {'eventId': 'e', 'evidenceAssessments': [{'evidenceId': 'unknown',
            'quote': '支持不同应用同屏', 'stance': 'context', 'reason': '不同命题'}]}
        with self.assertRaises(ValueError):
            EventGroundingTool.apply_audit([original], EventGroundingAuditBatch(corrections=[patch]))
        patch['evidenceAssessments'][0]['evidenceId'] = '0'
        result = EventGroundingTool.apply_audit([original], EventGroundingAuditBatch(corrections=[patch]))[0]
        self.assertEqual(result.evidence_assessments[0].stance, 'context')

    def test_separate_time_and_stance_patches_for_same_event_are_combined(self):
        original = self.grounded(['9月10日发布新品，黄牛加价'], [self.candidate(0, '9月10日发布新品')])
        patches = [{'eventId': 'e', 'discardTimeCandidateIndices': [0]},
                   {'eventId': 'e', 'evidenceAssessments': [{'evidenceId': '0', 'quote': '黄牛加价',
                     'stance': 'context', 'reason': '另一个动作'}]}]
        result = EventGroundingTool.apply_audit([original], EventGroundingAuditBatch(corrections=patches))[0]
        self.assertIsNone(result.event_date)
        self.assertEqual(result.evidence_assessments[0].stance, 'context')
        with self.assertRaises(ValueError):
            EventGroundingTool.apply_audit([original], EventGroundingAuditBatch(corrections=[patches[1], patches[1]]))

    def test_invalid_day_is_not_downgraded_to_valid_month(self):
        for text in ('2月30日发布', '2026年2月30日发布'):
            self.assertIsNone(self.grounded([text], [self.candidate(0, text)]).event_date)

    def test_resume_replays_then_calls_live_and_rejects_changed_requests(self):
        class Delegate:
            model_name = 'test'
            response_log = None
            calls = 0
            def generate_json(self, system, user):
                self.calls += 1
                return {'live': True}
        record = {'requestSha256': request_digest('system', 'user'), 'content': '{"saved":true}', 'finishReason': 'stop'}
        delegate = Delegate()
        model = ResponsePrefixLLM(delegate, [record], 'hash')
        self.assertEqual(model.generate_json('system', 'user'), {'saved': True})
        self.assertEqual(delegate.calls, 0)
        self.assertEqual(model.generate_json('next', 'request'), {'live': True})
        self.assertEqual((model.replayed_calls, delegate.calls), (1, 1))
        with self.assertRaises(RuntimeError):
            ResponsePrefixLLM(delegate, [record], 'hash').generate_json('changed', 'user')

    def grounded(self, texts, candidates, **kwargs):
        publication = kwargs.pop('publication', {})
        evidence = [Evidence(evidence_id=str(i), source='test', content=text, **publication)
                    for i, text in enumerate(texts)]
        original = Event(event_id='e', title='事件', evidence=evidence, **kwargs)
        review = {'eventId': 'e', 'evidenceAssessments': [
            {'evidenceId': e.evidence_id, 'quote': e.content, 'stance': 'support', 'reason': '固定标注'} for e in evidence],
            'timeCandidates': candidates}
        return EventGroundingTool.materialize([original], EventGroundingBatch(reviews=[review]))[0]

    def candidate(self, index, quote, role='occurrence', **extra):
        return {'anchor': {'evidenceId': str(index), 'quote': quote}, 'role': role, **extra}

    def test_conflicting_release_dates_keep_common_month_and_all_quotes(self):
        texts = ['苹果在2026年9月10日才进场', '2026-09-09 iPhone Duo 发布;开发者中心上线']
        output = self.grounded(texts, [self.candidate(i, t) for i, t in enumerate(texts)])
        self.assertEqual(output.event_date.value, '2026-09')
        self.assertEqual(output.date_status, 'conflicting')
        self.assertEqual(len(output.time_candidates), 2)
        self.assertEqual(ground_event_time(output), output)

    def test_consistent_multi_source_dates_recover_missing_year(self):
        texts = ['2026年9月10日发布新品', '9月10日发布新品']
        result = self.grounded(texts, [self.candidate(i, t) for i, t in enumerate(texts)])
        self.assertEqual(result.event_date.value, '2026-09-10')
        self.assertEqual(result.date_status, 'resolved')

    def test_report_and_forecast_target_do_not_become_occurrence_date(self):
        text = '9月16日消息，分析师预测2027年供应增长'
        result = self.grounded([text], [self.candidate(0, '9月16日消息', 'report'),
            self.candidate(0, '分析师预测2027年供应增长', 'target')], claim_type='forecast')
        self.assertIsNone(result.event_date)
        self.assertEqual(result.report_date.value, '--09-16')
        self.assertEqual(result.target_date.value, '2027')

    def test_relative_days_require_reliable_publication_and_keep_local_calendar(self):
        text = '昨天发布新品'
        result = self.grounded([text], [self.candidate(0, text)],
            publication={'published_at': '2026-01-01T00:30:00+08:00'})
        self.assertEqual(result.event_date.value, '2025-12-31')
        self.assertEqual(result.date_basis, 'relative')
        result = self.grounded([text], [self.candidate(0, text)], publication={
            'published_at': '2026-09-17T01:00:00Z', 'quality_warnings': ['published_at_relative']})
        self.assertIsNone(result.event_date)

    def test_year_context_is_explicit_and_local_to_source(self):
        text = '2026 秋季发布会结束后，9月10日上线配件'
        output = self.grounded([text], [self.candidate(0, '9月10日上线配件',
            yearContext={'evidenceId': '0', 'quote': '2026 秋季发布会'})])
        self.assertEqual(output.event_date.value, '2026-09-10')
        self.assertEqual(output.date_basis, 'context')
        output = self.grounded([text], [self.candidate(0, '9月10日上线配件')],
            publication={'published_at': '2026-09-17T00:00:00Z'})
        self.assertEqual(output.event_date.value, '--09-10')
        with self.assertRaises(ValueError):
            self.grounded([text, '2026年其他事件'], [self.candidate(0, '9月10日上线配件',
                yearContext={'evidenceId': '1', 'quote': '2026年其他事件'})])

    def test_month_only_and_plan_dates_retain_precision(self):
        text = '10月份的新系统出来以后，应用可以适配'
        output = self.grounded([text], [self.candidate(0, '10月份的新系统', 'target')], claim_type='planned')
        self.assertEqual(output.event_date.value, '--10')
        self.assertEqual(output.event_date.precision, 'month_only')
        self.assertIsNone(time_bounds(output))
        self.assertIn('10月', render_timeline(EventProcessingResult(events=[output])))

    def test_grounding_rejects_omitted_assessment_and_invented_quote(self):
        one = event('a', '9月10日新品发布')
        with self.assertRaises(ValueError):
            EventGroundingTool.materialize([one], EventGroundingBatch(reviews=[
                {'eventId': 'a', 'evidenceAssessments': [], 'timeCandidates': []}]))
        with self.assertRaises(ValueError):
            self.grounded(['9月10日新品发布'], [self.candidate(0, '9月11日发布')])

    def test_counterevidence_and_context_are_not_counted_as_support(self):
        sources = [Evidence(evidence_id=str(i), title=t, source='test', url=f'https://example.com/{i}') for i, t in enumerate([
            '有望支持微信双开', '不等同于传统意义的应用双开', '能同时开两个App'])]
        item = Event(event_id='rumor', title='微信双开传闻', claim_type='rumor', evidence=sources)
        review = {'eventId': 'rumor', 'timeCandidates': [], 'evidenceAssessments': [
            {'evidenceId': e.evidence_id, 'quote': e.title, 'stance': stance, 'reason': '固定语义标注'}
            for e, stance in zip(sources, ['support', 'contradict', 'context'])]}
        output = EventGroundingTool.materialize([item], EventGroundingBatch(reviews=[review]))[0]
        text = render_timeline(EventProcessingResult(events=[output]))
        self.assertIn('https://example.com/0', text)
        self.assertNotIn('https://example.com/1', text)
        self.assertNotIn('https://example.com/2', text)
        self.assertEqual(len(output.evidence), 3)

    def test_real_case_refutation_is_ordered_after_rumor_without_fabricated_dates(self):
        rumor = event('z-rumor', '有消息说手机能原生双开微信', claim_type='rumor')
        reply = event('a-refutation', '针对双开微信的消息辟谣：实际只是多窗口', claim_type='refutation')
        payload = chain_response([rumor, reply], 'RESPONSE')
        payload['chains'][0]['relations'][0]['status'] = 'needs_review'
        llm = ReviewLLM([payload, {'reviews': [{'relationId': '0:0', 'decision': 'keep',
            'reason': '辟谣明确回应此前的双开传闻。'}]}])
        output = EventChainTool(llm).invoke(request([reply, rumor]))
        relation = output.chains[0].relations[0]
        self.assertEqual((relation.status, relation.temporal_basis), ('supported', 'text'))
        self.assertEqual(output.chains[0].event_ids, ['z-rumor', 'a-refutation'])
        text = render_timeline(EventProcessingResult(events=[reply, rumor], chains=output.chains))
        self.assertLess(text.index(rumor.title), text.index(reply.title))
        self.assertIn('E1 → E2', text)
        self.assertNotIn('2026', text)

    def test_story_view_leaves_unlinked_news_in_report(self):
        events = [event('a', '9月10日发布'), event('b', '10月23日发售计划', claim_type='planned'),
                  event('c', '另一条独立新闻')]
        relation = chain_response(events[:2], 'FOLLOW_UP')['chains'][0]['relations'][0]
        chain = EventChain(chain_id='story', title='发布至发售安排', category='STORYLINE',
                           event_ids=['a', 'b'], relations=[relation])
        text = render_timeline(EventProcessingResult(events=events, chains=[chain]))
        self.assertIn('## 发布至发售安排', text)
        self.assertIn('计划 10月23日', text)
        self.assertNotIn('另一条独立新闻', text)
        self.assertIn('其余 1 个节点', text)
        self.assertIn('[完整报告](report.md)', text)

    def test_missing_fields_and_uncertain_relations_only_have_short_footer(self):
        known = event('known', '新品发布', claim_type='reported')
        known.evidence[0].title = '9月10日新品发布'
        known.time_anchor = EvidenceAnchor(evidence_id='known', quote='9月10日新品发布')
        unknown = event('unknown', '新品适配', claim_type='reported')
        unknown.evidence[0].published_at = None
        forecast = event('forecast', '分析师预测供货', claim_type='forecast')
        relation = chain_response([known, unknown], 'FOLLOW_UP')['chains'][0]['relations'][0]
        relation.update(status='needs_review', reason='缺乏先后依据，不应进入正文')
        result = EventProcessingResult(events=[known, unknown, forecast], chains=[EventChain(
            chain_id='c', title='新品进展', category='STORYLINE', event_ids=['known', 'unknown'], relations=[relation],
            unresolved_questions=['具体时间待核实'])])
        before = result.model_dump_json()
        text = render_timeline(result)
        body, footer = text.split('## 备注', 1)
        for phrase in ('未披露', '年份未明', '来源未提供', '日期缺失', '待核实', '尚未确定', '缺乏先后依据'):
            self.assertNotIn(phrase, body)
        self.assertNotIn('<details>', text)
        self.assertNotIn('→', body)
        self.assertIn('9月10日', body)
        self.assertIn('新品适配', body)
        self.assertIn('预测', body)
        self.assertNotIn('2026-09-10', body)
        self.assertIn('[完整结果](result.json)', footer)
        self.assertLess(len(footer), 180)
        self.assertEqual(result.model_dump_json(), before)

    def test_publication_dates_stay_separate_and_empty_date_column_is_omitted(self):
        item = event('one', '新品改版')
        item.evidence[0].published_at = Evidence(evidence_id='x', title='x', source='test',
                                                published_at='2026-09-17T00:00:00Z').published_at
        guess = event('guess', '分析师预测需求', claim_type='forecast')
        text = render_timeline(EventProcessingResult(events=[item, guess]))
        news = text.split('## 相关报道', 1)[1].split('## 预测、传闻与观点', 1)[0]
        self.assertIn('报道日期', news)
        self.assertIn('2026-09-17', news)
        self.assertNotIn('事件日期', news)
        forecasts = text.split('## 预测、传闻与观点', 1)[1].split('## 备注', 1)[0]
        self.assertNotIn('日期', forecasts)

    def test_model_date_hint_cannot_override_source_date(self):
        source = Evidence(evidence_id='s', title='9月10日发布', source='test')
        draft = {'title': source.title, 'evidenceIds': ['s'],
                 'anchor': {'evidenceId': 's', 'quote': source.title},
                 'timeAnchor': {'evidenceId': 's', 'quote': source.title},
                 'eventDate': {'value': '2099-01-01', 'precision': 'day'}}
        result = EventExtractionTool(ReviewLLM([{'events': [draft]}])).invoke({'evidence': [source]})
        self.assertEqual(result.events[0].event_date.value, '--09-10')
        draft['timeAnchor'] = None
        result = EventExtractionTool(ReviewLLM([{'events': [draft]}])).invoke({'evidence': [source]})
        self.assertIsNone(result.events[0].event_date)

    def test_refinement_preserves_input_and_marks_budget_failure(self):
        one = event('one', '9月10日新品发布')
        source = WorkflowResult(mode='BULK_DISCOVERY', industry='测试', evidence=one.evidence,
            events=[one], classifications=[], chains=[], rounds=[], stop_reason=StopReason.COMPLETED)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            path.write_text(source.model_dump_json(by_alias=True), encoding='utf-8')
            original = path.read_bytes()
            llm = QueueLLM([{'classifications': [{'eventId': 'one', 'category': 'PRODUCT_LAUNCH', 'confidence': .9}]}])
            result = refine_result(path, llm, 3)
            self.assertEqual(result.processing_mode, 'event_refinement')
            self.assertEqual(result.input_result_sha256, hashlib.sha256(original).hexdigest())
            self.assertEqual(result.stop_reason, StopReason.COMPLETED)
            self.assertEqual(result.collection_calls, 0)
            self.assertIn('未重新采集或抽取', render_timeline(result))
            failed = refine_result(path, ReviewLLM([]), 0)
            self.assertEqual(failed.stop_reason, StopReason.MODEL_BUDGET_EXHAUSTED)
            self.assertEqual(failed.events, source.events)
            self.assertEqual(path.read_bytes(), original)

    def test_global_review_splits_rumor_and_refutation_with_all_evidence(self):
        yes = Evidence(evidence_id='yes', title='爆料称支持双开', source='test')
        no = Evidence(evidence_id='no', title='澄清不支持多账号双开', source='test')
        old = Event(event_id='mixed', title='支持双开', evidence=[yes, no])
        drafts = [{'title': ev.title, 'claimType': kind, 'evidenceIds': [ev.evidence_id],
                   'anchor': {'evidenceId': ev.evidence_id, 'quote': ev.title}}
                  for ev, kind in ((yes, 'rumor'), (no, 'refutation'))]
        payload = {'groups': [], 'revisions': [{'targetEventId': 'mixed', 'events': drafts}]}
        batch = EventMergeBatch.model_validate(payload)
        before = old.model_dump_json()
        result = EventMergeTool.materialize([old], batch)
        self.assertEqual(result[0].event_id, 'mixed')
        self.assertEqual([e.claim_type for e in result], ['rumor', 'refutation'])
        self.assertEqual({e.evidence_id for x in result for e in x.evidence}, {'yes', 'no'})
        self.assertIn('不支持', result[1].title)
        self.assertEqual(old.model_dump_json(), before)
        batch.revisions[0].events.pop()
        with self.assertRaises(ValueError):
            EventMergeTool.materialize([old], batch)

    def test_revision_cannot_invent_quotes_or_read_other_events(self):
        a, b = event('a', '传闻支持'), event('b', '澄清不支持')
        for key, quote in [('b', '澄清不支持'), ('a', '编造引文')]:
            batch = EventMergeBatch.model_validate({'groups': [], 'revisions': [{'targetEventId': 'a', 'events': [
                {'title': quote, 'claimType': 'refutation', 'evidenceIds': [key],
                 'anchor': {'evidenceId': key, 'quote': quote}}]}]})
            with self.assertRaises(ValueError):
                EventMergeTool.materialize([a, b], batch)

    def test_yearless_dates_are_validated_and_never_become_absolute(self):
        a = event('a', '9月16日发布')
        self.assertEqual(a.event_date, PartialDate(value='--09-16', precision='month_day'))
        self.assertIsNone(time_bounds(a))
        self.assertIsNone(a.event_time)
        self.assertIsNone(event('invalid', '13月40日发布').event_date)
        self.assertEqual(event('leap', '2月29日发布').event_date.value, '--02-29')

    def test_mixed_dates_do_not_become_one_milestone(self):
        self.assertIsNone(event('a', '9月16日预购，10月23日发售').event_date)
        self.assertTrue(event('a', '9月16日预购，10月23日发售').time_warnings)

    def test_three_actions_from_one_article_stay_separate(self):
        source = Evidence(evidence_id='s', source='test', title='新品时间表',
                          content='9月10日发布。10月16日开启预购。10月23日正式发售。')
        drafts = [{'title': title, 'actionKind': action, 'claimType': claim,
                   'evidenceIds': ['s'], 'anchor': {'evidenceId': 's', 'quote': quote},
                   'timeAnchor': {'evidenceId': 's', 'quote': quote}}
                  for title, action, claim, quote in (
                      ('新品发布', 'release', 'reported', '9月10日发布'),
                      ('新品计划预购', 'preorder', 'planned', '10月16日开启预购'),
                      ('新品计划发售', 'sale', 'planned', '10月23日正式发售'))]
        output = EventExtractionTool(ReviewLLM([{'events': drafts}])).invoke({'evidence': [source]})
        self.assertEqual(len(output.events), 3)
        self.assertEqual([e.event_date.value for e in output.events], ['--09-10', '--10-16', '--10-23'])
        self.assertEqual([e.claim_type for e in output.events], ['reported', 'planned', 'planned'])
        groups = {'groups': [{'eventIds': [e.event_id for e in output.events], 'reason': '同一个产品',
                              'evidence': [{'evidenceId': 's', 'quote': source.content}], 'confidence': .99}]}
        with self.assertRaises(ValueError):
            EventMergeTool.materialize(output.events, EventMergeBatch.model_validate(groups))

    def test_matched_event_cannot_overwrite_another_action(self):
        old = event('old', '9月10日发布', action_kind='release')
        source = Evidence(evidence_id='s', title='10月16日预购', source='test')
        draft = {'title': '新品预购', 'actionKind': 'preorder', 'matchedEventId': 'old',
                 'evidenceIds': ['s'], 'anchor': {'evidenceId': 's', 'quote': source.title}}
        with self.assertRaises(EventExtractionToolError):
            EventExtractionTool(ReviewLLM([{'events': [draft]}]), max_attempts=1).invoke(
                {'evidence': [source], 'existingEvents': [old]})

    def test_short_new_quote_keeps_previous_full_date(self):
        old = event('old', '2026年9月10日发布', action_kind='release')
        source = Evidence(evidence_id='s', title='9月10日发布', source='test')
        anchor = {'evidenceId': 's', 'quote': source.title}
        draft = {'title': '新品发布', 'actionKind': 'release', 'matchedEventId': 'old',
                 'evidenceIds': ['s'], 'anchor': anchor, 'timeAnchor': anchor}
        result = EventExtractionTool(ReviewLLM([{'events': [draft]}])).invoke(
            {'evidence': [source], 'existingEvents': [old]})
        self.assertEqual(result.events[0].event_date.value, '2026-09-10')

    def test_semantic_review_removes_false_contradiction_without_losing_events(self):
        events = [event('multi', '手机支持不同应用分屏运行'), event('clone', '手机不支持微信多账号双开')]
        payload = chain_response(events, 'CONTRADICTS')
        original = json.dumps(payload)
        llm = ReviewLLM([payload, {'reviews': [{'relationId': '0:0', 'decision': 'reject',
                                               'reason': '不同应用多任务与同一应用多账号是不同功能。'}]}])
        result = EventChainTool(llm).invoke(request(events))
        self.assertEqual(result.chains, [])
        self.assertEqual(result.unlinked_event_ids, ['multi', 'clone'])
        self.assertEqual(len(result.rejected_relations), 1)
        self.assertEqual(json.dumps(payload), original)

    def test_rejected_bridge_splits_chain_and_keeps_valid_branch(self):
        events = [event(str(i), f'事件{i}') for i in range(3)]
        reviews = {'reviews': [{'relationId': '0:0', 'decision': 'keep', 'reason': '有具体动作联系'},
                               {'relationId': '0:1', 'decision': 'reject', 'reason': '仅主题相似'}]}
        output = EventChainTool(ReviewLLM([chain_response(events), reviews])).invoke(request(events))
        self.assertEqual(output.chains[0].event_ids, ['0', '1'])
        self.assertEqual(output.unlinked_event_ids, ['2'])
        self.assertEqual(len(output.chains[0].timeline), 2)

    def test_semantic_review_is_complete_and_within_budget(self):
        events = [event('a', '事件一'), event('b', '事件二')]
        for reviews in ({'reviews': []}, {'reviews': [{'relationId': 'made-up', 'decision': 'keep', 'reason': '错误引用'}]}):
            with self.assertRaises(EventChainToolError):
                EventChainTool(ReviewLLM([chain_response(events), reviews]), max_attempts=1).invoke(request(events))
        with model_call_budget(1), self.assertRaises(ModelBudgetExceeded):
            EventChainTool(ReviewLLM([chain_response(events)])).invoke(request(events))

    def test_related_and_uncertain_edges_never_get_direction_arrows(self):
        events = [event('a', '2026年9月1日发布'), event('b', '2026年9月5日更新')]
        relation = chain_response(events)['chains'][0]['relations'][0]
        chain = EventChain(chain_id='c', title='关联', category='STORYLINE', event_ids=['a', 'b'], relations=[relation])
        result = EventProcessingResult(events=events, chains=[chain])
        before = result.model_dump_json()
        self.assertNotIn('→', render_timeline(result))
        self.assertEqual(result.model_dump_json(), before)
        chain.relations[0].relation_type = 'FOLLOW_UP'
        chain.relations[0].status = 'needs_review'
        self.assertNotIn('→', render_timeline(result))
        chain.relations[0].status = 'supported'
        self.assertIn('E1 → E2', render_timeline(result))

    def test_each_node_shown_once_and_plans_separate_from_occurrences(self):
        events = [event('late', '9月16日供应商回应'), event('early', '9月10日新品发布'),
                  event('plan', '10月23日计划发售', claim_type='planned', action_kind='sale'),
                  event('guess', '分析师预计供货不足', claim_type='forecast')]
        result = EventProcessingResult(events=events, chains=[EventChain(chain_id='x', title='组',
            category='STORYLINE', event_ids=['late', 'early'])])
        text = render_timeline(result)
        self.assertLess(text.index('9月10日新品发布'), text.index('9月16日供应商回应'))
        self.assertEqual(text.count('9月10日新品发布'), 1)
        self.assertIn('已宣布的计划', text)
        self.assertIn('预测、传闻与观点', text)
        self.assertNotIn('2026-09', text)


if __name__ == '__main__':
    unittest.main()
