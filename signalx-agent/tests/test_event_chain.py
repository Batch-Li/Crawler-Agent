"""事件关联的协议、证据与工作流回归测试；全部使用模拟模型。"""
import json
import unittest
from copy import deepcopy

from agent import Event, EventChainTool, EventChainToolError, SignalXEventProcessor
from agent.llm import ModelBudgetExceeded, model_call_budget
from agent.query_planner import LLMQueryPlanner
from agent.timeline import render_timeline
from agent.workflow import EventDiscoveryWorkflow
from agent.workflow_models import EventChain, EventProcessingResult, StopReason, WorkflowResult
from tests.test_event_tools import QueueLLM, chain_response, evidence, catalog_events
from tests.test_regressions import LocalLLM, planner, request
from tests.test_workflow import FakeCollector


def chain_input():
    events = [Event(
        event_id=key, title=title, entities=entities, event_time=time,
        evidence=[evidence("source_" + key, title)],
    ) for key, title, entities, time in (
        ("launch", "甲公司发布产品 A", ["甲公司", "产品 A"], "2026-09-01T09:00:00+08:00"),
        ("price", "甲公司下调产品 A 月费", ["甲公司", "产品 A"], "2026-09-02T09:00:00+08:00"),
        ("response", "乙公司回应甲公司的产品 A 降价", ["乙公司", "产品 A"], "2026-09-03T09:00:00+08:00"),
    )]
    return {"events": events, "industry": "虚构测试", "classifications": [
        {"eventId": e.event_id, "category": category, "confidence": .9}
        for e, category in zip(events, ("PRODUCT_LAUNCH", "PRICE_CHANGE", "MARKETING_CAMPAIGN"))
    ]}


class ChainTests(unittest.TestCase):
    def test_cross_category_cross_entity_chain_has_evidence_and_timeline(self):
        data = chain_input()
        payload = chain_response(data["events"], "FOLLOW_UP")
        payload["chains"][0]["relations"][1]["relationType"] = "RESPONSE"
        data["events"].reverse()
        llm = QueueLLM([payload])
        with model_call_budget(2) as budget:
            result = EventChainTool(llm).invoke(data)
        chain = result.chains[0]
        self.assertEqual(budget.calls, 2)
        self.assertEqual(chain.category, "STORYLINE")
        self.assertEqual(chain.event_ids, ["launch", "price", "response"])
        self.assertEqual([e.event_id for e in chain.timeline], chain.event_ids)
        self.assertTrue(all(e.time_status == "known" for e in chain.timeline))
        self.assertEqual([r.relation_type for r in chain.relations], ["FOLLOW_UP", "RESPONSE"])
        self.assertEqual(len(chain.relations[0].evidence), 2)
        self.assertEqual(result.unlinked_event_ids, [])

    def test_empty_model_result_does_not_fall_back_to_company_grouping(self):
        data = chain_input()
        for event, category in zip(data["events"], data["classifications"]):
            event.entities = ["甲公司"]
            category["category"] = "PRODUCT_LAUNCH"
        llm = QueueLLM([{"chains": [], "unresolvedQuestions": ["缺少直接关联证据"]}])
        result = EventChainTool(llm).invoke(data)
        self.assertEqual(result.chains, [])
        self.assertEqual(result.unlinked_event_ids, ["launch", "price", "response"])
        self.assertEqual(result.unresolved_questions, ["缺少直接关联证据"])

    def test_zero_or_one_event_does_not_call_model(self):
        for count in (0, 1):
            data = chain_input()
            data["events"] = data["events"][:count]
            data["classifications"] = data["classifications"][:count]
            llm = QueueLLM([])
            with model_call_budget(0):
                result = EventChainTool(llm).invoke(data)
            self.assertEqual(result.chains, [])
            self.assertEqual(llm.calls, 0)
            self.assertEqual(result.unlinked_event_ids, [e.event_id for e in data["events"]])

    def test_invalid_graphs_and_references_retry_then_fail_without_fallback(self):
        data = chain_input()
        valid = chain_response(data["events"], "FOLLOW_UP")
        cases = {}
        for name in ("unknown_member", "duplicate_member", "unknown_endpoint", "self_edge",
                     "duplicate_edge", "one_sided", "fabricated_quote", "third_party_quote",
                     "disconnected", "time_reversed", "unknown_chain", "duplicate_chain",
                     "invented_timeline", "bad_confidence"):
            payload = deepcopy(valid)
            chain = payload["chains"][0]
            edge = chain["relations"][0]
            if name == "unknown_member": chain["eventIds"].append("invented")
            elif name == "duplicate_member": chain["eventIds"].append("launch")
            elif name == "unknown_endpoint": edge["toEventId"] = "invented"
            elif name == "self_edge": edge["toEventId"] = edge["fromEventId"]
            elif name == "duplicate_edge": chain["relations"].append(deepcopy(edge))
            elif name == "one_sided": edge["evidence"].pop()
            elif name == "fabricated_quote": edge["evidence"][0]["quote"] = "并未出现的因果结论"
            elif name == "third_party_quote": edge["evidence"].append({"evidenceId": "source_response", "quote": data["events"][2].title})
            elif name == "disconnected": chain["relations"].pop()
            elif name == "time_reversed": edge["fromEventId"], edge["toEventId"] = edge["toEventId"], edge["fromEventId"]
            elif name == "unknown_chain": chain["matchedChainId"] = "invented"
            elif name == "duplicate_chain": payload["chains"].append(deepcopy(chain))
            elif name == "invented_timeline": chain["timeline"] = []
            elif name == "bad_confidence": edge["confidence"] = 1.1
            cases[name] = payload
        cases["wrong_stage"] = {"classifications": []}
        for name, invalid in cases.items():
            with self.subTest(name=name):
                llm = QueueLLM([invalid, valid])
                self.assertEqual(len(EventChainTool(llm).invoke(data).chains), 1)
                self.assertEqual(llm.calls, 3)
                with self.assertRaises(EventChainToolError):
                    EventChainTool(QueueLLM([invalid]), max_attempts=1).invoke(data)

    def test_directed_cycle_is_rejected_even_when_all_dates_equal(self):
        data = chain_input()
        for e in data["events"]:
            e.event_time = data["events"][0].event_time
        payload = chain_response(data["events"], "FOLLOW_UP")
        payload["chains"][0]["relations"].append(
            chain_response([data["events"][2], data["events"][0]], "FOLLOW_UP")["chains"][0]["relations"][0])
        with self.assertRaises(EventChainToolError):
            EventChainTool(QueueLLM([payload]), max_attempts=1).invoke(data)

    def test_unknown_time_does_not_override_reviewed_relative_order(self):
        data = chain_input()
        data["events"][0].event_time = None
        payload = chain_response(data["events"], "FOLLOW_UP")
        before = deepcopy(payload)
        result = EventChainTool(QueueLLM([payload])).invoke(data)
        chain = result.chains[0]
        self.assertEqual(chain.event_ids, ["launch", "price", "response"])
        self.assertIsNone(chain.timeline[0].event_time)
        self.assertEqual(chain.timeline[0].time_status, "unknown")
        self.assertEqual(chain.relations[0].status, "supported")
        self.assertEqual(chain.relations[0].temporal_basis, 'text')
        self.assertEqual(payload, before)

    def test_chain_id_survives_rename_and_model_matched_extension(self):
        data = chain_input()
        small = {**data, "events": data["events"][:2], "classifications": data["classifications"][:2]}
        first = EventChainTool(QueueLLM([chain_response(small["events"])])).invoke(small)
        renamed = chain_response(small["events"])
        renamed["chains"][0]["title"] = "改写链标题"
        again = EventChainTool(QueueLLM([renamed])).invoke({**small, "previousChains": first.chains})
        self.assertEqual(first.chains[0].chain_id, again.chains[0].chain_id)
        extended = chain_response(data["events"])
        extended["chains"][0]["matchedChainId"] = first.chains[0].chain_id
        result = EventChainTool(QueueLLM([extended])).invoke({**data, "previousChains": first.chains})
        self.assertEqual(result.chains[0].chain_id, first.chains[0].chain_id)
        self.assertEqual(len(result.chains[0].event_ids), 3)

    def test_matched_chain_must_share_an_event(self):
        data = chain_input()
        previous = EventChain(chain_id="old", category="STORYLINE", title="其他链", event_ids=["other"])
        payload = chain_response(data["events"])
        payload["chains"][0]["matchedChainId"] = "old"
        with self.assertRaises(EventChainToolError):
            EventChainTool(QueueLLM([payload]), max_attempts=1).invoke({**data, "previousChains": [previous]})

    def test_chain_budget_exhaustion_keeps_prior_workflow_snapshot(self):
        llm = LocalLLM()
        result = EventDiscoveryWorkflow(
            FakeCollector({"查询一": [evidence("one", "发布产品")], "查询二": [evidence("two", "产品调价")]}),
            SignalXEventProcessor.from_llm(llm), planner("查询一", "查询二"),
        ).run(request(maxLlmCalls=9))
        self.assertEqual(result.stop_reason, StopReason.MODEL_BUDGET_EXHAUSTED)
        self.assertEqual(result.llm_calls, 9)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.chains, [])
        self.assertEqual(result.unprocessed_evidence_ids, ["two"])
        self.assertEqual(result.rounds[0].snapshot, result.rounds[1].snapshot)

    def test_retry_cannot_exceed_model_budget(self):
        llm = QueueLLM([{"chains": [{"title": "缺字段"}]}])
        with model_call_budget(1):
            with self.assertRaises(ModelBudgetExceeded):
                EventChainTool(llm).invoke(chain_input())
        self.assertEqual(llm.calls, 1)

    def test_pending_chain_questions_reach_planner_and_final_output(self):
        class Model(LocalLLM):
            def generate_json(self, system, user):
                if "事件关联工具" in system:
                    self.calls.append((system, user))
                    context = json.loads(user.split("输入：\n", 1)[1])
                    payload = chain_response(catalog_events(context), "FOLLOW_UP")
                    payload["unresolvedQuestions"] = ["需要独立来源"]
                    return payload
                return super().generate_json(system, user)

        llm = Model(plan_responses=[{"shouldContinue": False, "reason": "测试结束"}])
        result = EventDiscoveryWorkflow(
            FakeCollector({"查询一": [evidence("one", "发布产品"), evidence("two", "产品调价")]}),
            SignalXEventProcessor.from_llm(llm), LLMQueryPlanner(llm),
        ).run(request())
        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(result.llm_calls, 8)
        self.assertIn("需要独立来源", result.unresolved_questions)
        self.assertFalse(any("先后时间待核实" in q for q in result.unresolved_questions))
        prompt = next(user for system, user in llm.calls if "查询规划器" in system)
        self.assertIn('"chainUnresolvedQuestions": ["需要独立来源"]', prompt)
        self.assertIn('"timeStatus": "unknown"', prompt)

    def test_legacy_chain_json_stays_readable(self):
        old = EventChain.model_validate({"chainId": "old", "title": "历史分组",
                                         "category": "PRODUCT_LAUNCH", "eventIds": ["a", "b"]})
        self.assertEqual(old.relations, [])
        self.assertEqual(old.timeline, [])

    def test_timeline_moves_stop_to_footer_and_keeps_questions_in_json(self):
        result = WorkflowResult(
            mode='BULK_DISCOVERY', industry='测试', evidence=[], events=[],
            classifications=[], chains=[], rounds=[], stop_reason=StopReason.NO_EVIDENCE,
            stop_message='来源没有返回可用材料',
            unresolved_questions=['缺少独立来源', '缺少独立来源'],
            chain_unresolved_questions=['需要后续进展'],
        )
        text = render_timeline(result)
        body, footer = text.split('## 备注', 1)
        self.assertNotIn('停止说明', body)
        self.assertIn('停止说明：来源没有返回可用材料', footer)
        self.assertNotIn('缺少独立来源', text)
        self.assertNotIn('需要后续进展', text)
        self.assertIn('缺少独立来源', result.unresolved_questions)
        self.assertIn('需要后续进展', result.chain_unresolved_questions)

    def test_snapshot_timeline_keeps_unlinked_questions_out_of_body(self):
        event = Event(event_id='evt_current', title='产品发布',
                      evidence=[evidence('source', '产品发布')], merged_event_ids=['evt_old'])
        result = EventProcessingResult(events=[event], chain_unresolved_questions=[
            'evt_old 的具体日期待核实', 'evt_current 的具体日期待核实'])
        before = result.model_dump_json()
        text = render_timeline(result)
        self.assertIn('产品发布', text)
        self.assertNotIn('evt_old', text)
        self.assertNotIn('evt_current', text)
        self.assertNotIn('具体日期待核实', text)
        self.assertIn('缺失信息、口径分歧', text.split('## 备注', 1)[1])
        self.assertEqual(result.model_dump_json(), before)

    def test_timeline_does_not_list_questions_from_chains(self):
        events = chain_input()['events'][:2]
        chain = EventChain(chain_id='chain', title='产品进展', category='STORYLINE',
                           event_ids=[e.event_id for e in events],
                           unresolved_questions=['需核实关联方向'])
        result = EventProcessingResult(events=events, chains=[chain],
                                       chain_unresolved_questions=['需核实关联方向'])
        text = render_timeline(result)
        self.assertNotIn('需核实关联方向', text)
        self.assertEqual(text.count('## 备注'), 1)
        self.assertEqual(result.chain_unresolved_questions, ['需核实关联方向'])


if __name__ == "__main__":
    unittest.main()
