from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent.evaluate import ExpectedResult, evaluate
from agent.workflow_models import WorkflowResult


ROOT = Path(__file__).resolve().parents[1]


class DiscoveryDemoTests(unittest.TestCase):
    def test_resume_accepts_only_known_missing_transport_tail(self):
        from agent.discovery_demo import resume_llm, input_digest
        from agent.workflow_models import WorkflowError
        class Delegate:
            model_name = 'fixture'
            response_log = None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'input.json'
            source.write_text('[]')
            saved = WorkflowResult(mode='BULK_DISCOVERY', industry='测试', stop_reason='FAILED', llm_calls=2,
                evidence=[], events=[], classifications=[], chains=[], rounds=[],
                input_result_sha256=input_digest(source), errors=[WorkflowError(stage='processing', error_type='APITimeoutError', message='超时')])
            (root / 'result.json').write_text(saved.model_dump_json(by_alias=True))
            (root / 'model_responses.jsonl').write_text(json.dumps({'content': '{}', 'finishReason': 'stop'}) + '\n')
            self.assertEqual(len(resume_llm(Delegate(), root, source).records), 1)
            saved.errors[0].error_type = 'ValueError'
            (root / 'result.json').write_text(saved.model_dump_json(by_alias=True))
            with self.assertRaises(ValueError):
                resume_llm(Delegate(), root, source)

    def run_demo(self, target: Path, *extra: str):
        return subprocess.run([
            sys.executable, "-B", "-m", "agent.discovery_demo",
            "examples/discovery/evidence.json", "--replay", "examples/discovery/responses.json",
            "--output-dir", str(target), *extra,
        ], cwd=ROOT, capture_output=True, text=True, timeout=15)

    def test_offline_demo_saves_rounds_and_matches_expected_grouping(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            completed = self.run_demo(target)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = WorkflowResult.model_validate_json((target / "result.json").read_text())
            self.assertEqual(result.llm_calls, 11)
            self.assertEqual(len(result.events), 2)
            self.assertEqual(len(result.chains), 1)
            self.assertTrue((target / "timeline.md").exists())
            first = json.loads((target / "round-001.json").read_text())
            last = json.loads((target / "round-002.json").read_text())
            self.assertEqual(first["snapshot"]["events"][0]["eventId"], last["snapshot"]["events"][0]["eventId"])
            expected = ExpectedResult.model_validate_json((ROOT / "examples/discovery/expected.json").read_text())
            metrics = evaluate(result, expected)
            self.assertEqual(metrics["eventGroupingRecall"], 1.0)
            self.assertEqual(metrics["classificationAccuracyOnMatchedGroups"], 1.0)
            # 同一目录不可覆盖，旧结果不变。
            before = (target / "result.json").read_bytes()
            self.assertNotEqual(self.run_demo(target).returncode, 0)
            self.assertEqual((target / "result.json").read_bytes(), before)

    def test_budget_stop_still_saves_partial_result_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "partial"
            completed = self.run_demo(target, "--max-llm-calls", "4")
            self.assertEqual(completed.returncode, 2, completed.stderr)
            result = WorkflowResult.model_validate_json((target / "result.json").read_text())
            self.assertEqual(len(result.events), 1)
            self.assertEqual(result.llm_calls, 4)
            self.assertEqual(len(result.unprocessed_evidence_ids), 2)
            self.assertTrue((target / "round-002.json").exists())

    def test_evaluation_penalizes_duplicate_groups_and_wrong_labels(self):
        gold = ExpectedResult(annotation_source="regression", events=[
            {"evidenceIds": ["a"], "category": "PRODUCT_LAUNCH"},
        ])
        result = WorkflowResult.model_validate({
            "mode": "BULK_DISCOVERY", "industry": "测试", "evidence": [],
            "events": [
                {"eventId": key, "title": key, "evidence": [{"evidenceId": "a", "title": "测试", "source": "测试"}]}
                for key in ("event1", "event2")
            ],
            "classifications": [
                {"eventId": key, "category": "OTHER", "confidence": .5}
                for key in ("event1", "event2")
            ], "chains": [], "rounds": [], "stopReason": "COMPLETED",
        })
        metrics = evaluate(result, gold)
        self.assertEqual(metrics["eventGroupingPrecision"], .5)
        self.assertEqual(metrics["classificationAccuracyOnMatchedGroups"], 0)


if __name__ == "__main__":
    unittest.main()
