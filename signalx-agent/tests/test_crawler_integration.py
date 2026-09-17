from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent.crawler_integration import CrawlerCollectionTool, WorkflowDiscoveryRunner, build_crawler_workflow
from agent.event_processor import SignalXEventProcessor
from agent.models import Evidence
from agent.prompts import EVENT_CLASSIFICATION_SYSTEM_PROMPT, EVENT_EXTRACTION_SYSTEM_PROMPT, QUERY_PLANNER_SYSTEM_PROMPT
from agent.query_planner import LLMQueryPlanner
from agent.workflow import EventDiscoveryWorkflow
from agent.workflow_models import SearchRequest, StopReason, WorkflowResult

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
TITLE = "智言科技发布新一代折叠屏手机"


def item(content_id="c1", **extra):
    return {"contentId": content_id, "title": TITLE, "platform": "cctv_news",
            "summary": "新品支持双屏协作", "content": None,
            "url": f"https://example.com/{content_id}", "publishedAt": None,
            "dedup": {"dedupKey": f"key-{content_id}"}, "metrics": {}, **extra}


class FixtureLLM:
    """确定性协议回放，验证接线；不评测模型质量。"""

    model_name = "integration-fixture"

    def __init__(self):
        self.calls = 0

    def generate_json(self, system_prompt, user_prompt):
        self.calls += 1
        if '事件依据的独立反例审查员' in system_prompt:
            return {'corrections': [], 'revisions': []}
        if '事件证据与时间复核工具' in system_prompt:
            from tests.test_event_tools import grounding_response
            return grounding_response(user_prompt)
        marker = "上下文：\n" if system_prompt == QUERY_PLANNER_SYSTEM_PROMPT else "输入：\n"
        data = json.JSONDecoder().raw_decode(user_prompt.split(marker, 1)[1])[0]
        if system_prompt == QUERY_PLANNER_SYSTEM_PROMPT:
            initial = data["phase"] == "initial_event_expansion"
            return {"shouldContinue": initial, "queries": ["智言科技 新品"] if initial else [],
                    "reason": "离线协议验证"}
        if system_prompt == EVENT_EXTRACTION_SYSTEM_PROMPT:
            first = data["evidence"][0]
            draft = {"title": TITLE, "entities": ["智言科技"],
                     "evidenceIds": [e["evidenceId"] for e in data["evidence"]],
                     "anchor": {"evidenceId": first["evidenceId"],
                                "quote": first.get("snippet") or first.get("title") or first["content"]}}
            if data["existingEvents"]:
                draft["matchedEventId"] = data["existingEvents"][0]["eventId"]
            return {"events": [draft]}
        if system_prompt == EVENT_CLASSIFICATION_SYSTEM_PROMPT:
            return {"classifications": [{"eventId": e["eventId"], "category": "PRODUCT_LAUNCH",
                                         "confidence": .9} for e in data["events"]]}
        raise AssertionError("unexpected model stage")


@unittest.skipUnless(importlib.util.find_spec("crawler_tool"), "需安装 ../crawler_tool[dev]")
class CrawlerIntegrationTests(unittest.TestCase):
    def collector(self, payload):
        from crawler_tool.monitoring import SearchContentCollectionTool
        return CrawlerCollectionTool(SearchContentCollectionTool(search_fn=lambda q, limit: payload))

    def workflow(self, payload, llm=None):
        llm = llm or FixtureLLM()
        return EventDiscoveryWorkflow(self.collector(payload), SignalXEventProcessor.from_llm(llm), LLMQueryPlanner(llm))

    def bulk(self, payload):
        return self.workflow(payload).run({
            "mode": "BULK_DISCOVERY", "industry": "智能手机", "presetKeywords": ["新品"],
        })

    def test_bridge_preserves_content_snippet_nested_id_and_null_time(self):
        from crawler_tool.monitoring import item_to_evidence
        row = item(contentId=None, content="完整正文")
        evidence = Evidence.model_validate(item_to_evidence(row))
        self.assertEqual(evidence.evidence_id, "key-c1")
        self.assertEqual(evidence.content, "完整正文")
        self.assertEqual(evidence.snippet, "新品支持双屏协作")
        self.assertIsNone(evidence.published_at)
        result = self.collector({"items": [row]}).search(SearchRequest(
            query="新品", industry="智能手机", round_index=1, limit=10,
        ))
        self.assertEqual(len(result.evidence), 1)

        flat = item(contentId=None, dedup={}, dedupKey="flat-key")
        self.assertEqual(Evidence.model_validate(item_to_evidence(flat)).evidence_id, "flat-key")

    def test_summary_only_evidence_can_anchor_an_event(self):
        result = self.bulk({"items": [item(title=None)]})
        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(result.events[0].anchor.quote, "新品支持双屏协作")
        self.assertIsNone(result.events[0].evidence[0].content)

    def test_partial_source_failure_keeps_evidence_and_sanitized_diagnostics(self):
        result = self.bulk({"items": [item()], "status": "partial", "sourceReports": [
            {"platform": "cctv_news", "status": "success", "count": 1},
            {"platform": "weibo", "status": "rate_limited", "retryable": False,
             "message": "private-upstream-detail", "diagnostics": {"cookie": "test-only"}},
        ]})
        self.assertEqual(result.stop_reason, StopReason.PARTIAL)
        call = result.rounds[0].tool_calls[0]
        self.assertTrue(call.success)
        self.assertEqual(call.source_reports[1].status, "rate_limited")
        self.assertNotIn("private-upstream-detail", result.model_dump_json())
        self.assertNotIn("test-only", result.model_dump_json())
        self.assertEqual(len(result.events), 1)

    def test_empty_success_is_distinct_from_failed_sources_and_malformed_payload(self):
        empty = self.bulk({"items": [], "sourceReports": [{"platform": "toutiao", "status": "empty"}]})
        self.assertEqual(empty.stop_reason, StopReason.NO_EVIDENCE)
        failed = self.bulk({"items": [], "sourceReports": [{"platform": "toutiao", "status": "network_error"}]})
        self.assertEqual(failed.stop_reason, StopReason.FAILED)
        self.assertFalse(failed.rounds[0].tool_calls[0].success)
        self.assertEqual(self.bulk({"unexpected": []}).stop_reason, StopReason.FAILED)

    def test_invalid_rows_are_counted_without_discarding_valid_evidence(self):
        result = self.bulk({"items": [item(), {"broken": True}, item("bad", platform="")]})
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.rounds[0].tool_calls[0].dropped_count, 2)
        self.assertEqual(result.stop_reason, StopReason.PARTIAL)

    def test_http_api_to_workflow_contract_without_public_network(self):
        from fastapi.testclient import TestClient
        from crawler_tool.application import CrawlService
        from crawler_tool.domain import SourceStatus
        from crawler_tool.interfaces import create_app
        from crawler_tool.sources import SourceRegistry
        from crawler_tool.sources.base import SourceAdapter, AdapterResult, RawItem

        class FixtureAdapter(SourceAdapter):
            platform = "cctv_news"

            def search(self, request):
                return AdapterResult(status=SourceStatus.SUCCESS, items=[RawItem(
                    platform=self.platform, payload={"title": TITLE, "summary": "新品支持双屏协作",
                    "sourceType": "news_media", "contentType": "article", "url": "https://example.com/1"},
                )])

        app = create_app(lambda: CrawlService(SourceRegistry({"cctv_news": FixtureAdapter})))
        with TestClient(app) as client:
            def post(url, *, json, timeout):
                self.assertEqual(url, "http://127.0.0.1:8301/api/v1/tool/search-content")
                self.assertEqual(json["platforms"], ["cctv_news"])
                self.assertEqual(json["freshness"], "prefer_fresh")
                return client.post("/api/v1/tool/search-content", json=json)

            with patch("crawler_tool.monitoring.agent_bridge.httpx.post", side_effect=post):
                result = build_crawler_workflow(FixtureLLM(), platforms=["cctv_news"]).run({
                    "mode": "BULK_DISCOVERY", "industry": "智能手机", "presetKeywords": ["新品"],
                })
        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertTrue(result.evidence[0].evidence_id.startswith("cnt_"))
        self.assertEqual(result.rounds[0].tool_calls[0].source_reports[0].status, "success")

    def test_runner_preserves_alert_identity_metadata_and_model_budget(self):
        from crawler_tool.monitoring import alert_to_seed_event
        seed = alert_to_seed_event({"alertId": "alert-1", "why": "竞品发布", "subjectName": "智言科技",
            "eventTypes": ["PRODUCT_CHANGE"], "priority": "yellow", "score": 2.4,
            "evidence": [item()], "resonance": {"platforms": ["cctv_news"]}})
        llm = FixtureLLM()
        runner = WorkflowDiscoveryRunner(self.workflow({"items": [item("c2")]}, llm), industry="智能手机")
        result = runner.run(seed, {"maxRounds": 3, "maxToolCalls": 6, "maxLlmCalls": 6})
        restored = WorkflowResult.model_validate(json.loads(json.dumps(result)))
        self.assertEqual(restored.stop_reason, StopReason.COMPLETED)
        self.assertEqual(restored.events[0].event_id, "alert-1")
        self.assertEqual(restored.events[0].alert_meta.score, 2.4)
        self.assertEqual(restored.events[0].event_types, ["PRODUCT_LAUNCH"])
        self.assertEqual({e.evidence_id for e in restored.events[0].evidence}, {"c1", "c2"})
        blocked = runner.run(seed, {"maxRounds": 3, "maxToolCalls": 6, "maxLlmCalls": 0})
        self.assertEqual(blocked["stopReason"], "MODEL_BUDGET_EXHAUSTED")
        self.assertEqual(llm.calls, 6)

    def test_scheduled_tick_injects_runner_persists_result_and_is_idempotent(self):
        from crawler_tool.monitoring.scheduled_tick import run_scheduled_tick
        llm = FixtureLLM()
        runner = WorkflowDiscoveryRunner(self.workflow({"items": [item("c2")]}, llm), industry="智能手机")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queries = root / "queries.json"
            queries.write_text(json.dumps({"queries": ["智言科技"], "queriesPerTick": 1,
                                            "platforms": ["cctv_news"]}), encoding="utf-8")
            options = dict(data_dir=root / "mon", crawl_dir=root / "crawl", queries_file=queries,
                           poster=lambda q, p, limit: {"items": [item()]}, agent_runner=runner, now=NOW)
            summary = run_scheduled_tick(**options)
            self.assertEqual(summary["agentTrigger"]["dispatched"], 1)
            self.assertEqual(summary["agentTrigger"]["failed"], 0)
            saved = list((root / "mon/discoveries").glob("*.json"))
            self.assertEqual(len(saved), 1)
            payload = json.loads(saved[0].read_text())
            self.assertEqual(payload["executionStatus"], "completed")
            self.assertEqual(payload["result"]["events"][0]["eventId"], payload["alertId"])
            self.assertEqual(run_scheduled_tick(**options)["agentTrigger"]["dispatched"], 0)
            self.assertEqual(llm.calls, 6)

    def test_monitor_cli_injects_configured_sources_and_reports_budget_stop(self):
        from agent.crawler_cli import main
        from crawler_tool.monitoring.scheduled_tick import run_scheduled_tick

        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as output:
            root = Path(tmp)
            config = root / "config"
            config.mkdir()
            (config / "queries.json").write_text(json.dumps({
                "queries": ["智言科技"], "platforms": ["cctv_news"],
            }), encoding="utf-8")
            def tick(**kwargs):
                return run_scheduled_tick(**kwargs, poster=lambda q, p, limit: {"items": [item()]}, now=NOW)

            with patch("agent.crawler_cli.DeepSeekLLM", return_value=FixtureLLM()), \
                 patch("agent.crawler_cli.build_crawler_workflow", return_value=self.workflow({"items": [item()]})) as build, \
                 patch("crawler_tool.monitoring.scheduled_tick.run_scheduled_tick", side_effect=tick):
                code = main(["monitor", "--live", "--industry", "智能手机", "--config-dir", str(config),
                             "--data-dir", str(root / "mon"), "--crawl-dir", str(root / "crawl"),
                             "--max-llm-calls", "0"])
            self.assertEqual(code, 2)
            self.assertEqual(build.call_args.kwargs["platforms"], ["cctv_news"])
            self.assertEqual(json.loads(output.getvalue())["agentTrigger"]["partial"], 1)

    def test_cli_requires_live_flag_and_refuses_to_overwrite_output(self):
        from agent.crawler_cli import main
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["discover", "--industry", "智能手机", "--keyword", "新品", "--output-dir", "unused"])
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            workflow = self.workflow({"items": [item()]})
            with patch("agent.crawler_cli.DeepSeekLLM", return_value=FixtureLLM()), \
                 patch("agent.crawler_cli.build_crawler_workflow", return_value=workflow):
                args = ["discover", "--live", "--industry", "智能手机", "--keyword", "新品",
                        "--output-dir", str(Path(tmp) / "run")]
                self.assertEqual(main(args), 0)
                output = Path(tmp) / "run/result.json"
                before = output.read_bytes()
                self.assertEqual(main(args), 2)
                self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
