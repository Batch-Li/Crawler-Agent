"""Agent 自动触发器测试：选件、幂等、每日上限、无执行器降级。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from crawler_tool.monitoring.agent_bridge import MonitoringTool
from crawler_tool.monitoring.agent_trigger import AgentTrigger

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def make_tool(tmp_path: Path) -> MonitoringTool:
    return MonitoringTool(data_dir=tmp_path / "mon")


def seed_alerts(tool: MonitoringTool) -> None:
    """三条告警：2.4 / 2.16 达标，1.2 不达标。"""
    tool.ingest([
        {"contentId": "c1", "dedupKey": "k1", "title": "智言科技发布新一代折叠屏手机",
         "platform": "cctv_news", "publishedAt": None, "metrics": {}},
        {"contentId": "c2", "dedupKey": "k2", "title": "快答云宣布完成B轮融资",
         "platform": "cctv_news", "publishedAt": None, "metrics": {}},
        {"contentId": "c3", "dedupKey": "k3", "title": "智言科技组建芯片研发团队",
         "platform": "toutiao", "publishedAt": None, "metrics": {}},
    ], now=NOW)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, seed_event: dict, budget: dict) -> dict:
        self.calls.append({"seed": seed_event, "budget": budget})
        return {"events": [{"eventId": seed_event["eventId"]}], "classifications": [], "chains": []}


def setup_trigger(tmp_path: Path, runner=None, **kwargs):
    tool = make_tool(tmp_path)
    seed_alerts(tool)
    trigger = AgentTrigger(tool, runner=runner, **kwargs)
    return tool, trigger


def test_high_score_alerts_trigger_discovery_and_persist(tmp_path: Path):
    tool, trigger = setup_trigger(tmp_path, runner=RecordingRunner())
    report = trigger.run_due_triggers(now=date(2026, 9, 7))
    assert report.due == 2
    assert report.dispatched == 2

    tool2 = MonitoringTool(data_dir=tmp_path / "mon")
    # 两条达标的告警都已回写状态、结果落盘
    alerts = {a["alertId"]: a for a in tool2.store.load_alerts()}
    dispatched = [a for a in alerts.values() if a["status"] == "dispatched"]
    assert len(dispatched) == 2
    assert all("发现循环" in a["statusNote"] for a in dispatched)
    files = list((tmp_path / "mon" / "discoveries").glob("*.json"))
    assert len(files) == 2
    payload = json_load(files[0])
    assert payload["seedEvent"]["eventTypes"] == ["PRODUCT_LAUNCH"]
    assert payload["budget"]["maxToolCalls"] == 6


def test_idempotent_second_run_skips_dispatched(tmp_path: Path):
    runner = RecordingRunner()
    tool, trigger = setup_trigger(tmp_path, runner=runner)
    trigger.run_due_triggers(now=date(2026, 9, 7))
    second = trigger.run_due_triggers(now=date(2026, 9, 7))
    assert second.due == 0 and second.dispatched == 0
    assert len(runner.calls) == 2  # 没有重复触发


def test_daily_cap_blocks_overflow(tmp_path: Path):
    runner = RecordingRunner()
    tool, trigger = setup_trigger(tmp_path, runner=runner, max_runs_per_day=1)
    report = trigger.run_due_triggers(now=date(2026, 9, 7))
    assert report.dispatched == 1
    assert report.skipped_reason == "daily_cap_reached"
    assert len(runner.calls) == 1


def test_without_runner_status_untouched_and_due_reported(tmp_path: Path):
    tool, trigger = setup_trigger(tmp_path, runner=None)
    report = trigger.run_due_triggers(now=date(2026, 9, 7))
    assert report.due == 2
    assert report.dispatched == 0
    assert report.skipped_reason == "no_runner_configured"
    # 人工判读流程不受影响：状态仍是 pending
    assert all(a["status"] == "pending" for a in tool.store.load_alerts())


def test_daily_cap_allows_exact_remaining_slots(tmp_path: Path):
    runner = RecordingRunner()
    tool, trigger = setup_trigger(tmp_path, runner=runner, max_runs_per_day=2)
    report = trigger.run_due_triggers(now=date(2026, 9, 7))
    assert report.dispatched == 2
    assert report.skipped_reason is None
    assert tool.store.load_state()["agentRuns"]["2026-09-07"] == 2


def test_failed_runner_is_saved_counted_and_does_not_abort_next_alert(tmp_path: Path):
    class FlakyRunner(RecordingRunner):
        def run(self, seed_event, budget):
            result = super().run(seed_event, budget)
            if len(self.calls) == 1:
                raise RuntimeError("private-exception-detail")
            return result

    tool, trigger = setup_trigger(tmp_path, runner=FlakyRunner(), max_runs_per_day=2)
    report = trigger.run_due_triggers(now=date(2026, 9, 7))
    assert report.dispatched == 2 and report.failed == 1
    assert tool.store.load_state()["agentRuns"]["2026-09-07"] == 2
    files = list(trigger.discoveries_dir.glob("*.json"))
    assert len(files) == 2
    payloads = [json_load(path) for path in files]
    assert {p["executionStatus"] for p in payloads} == {"completed", "failed"}
    assert "private-exception-detail" not in str(payloads)
    assert trigger.run_due_triggers(now=date(2026, 9, 7)).dispatched == 0


def test_partial_workflow_result_is_not_reported_as_completed(tmp_path: Path):
    class PartialRunner:
        def run(self, seed_event, budget):
            return {"stopReason": "MODEL_BUDGET_EXHAUSTED", "events": []}

    tool, trigger = setup_trigger(tmp_path, runner=PartialRunner())
    report = trigger.run_due_triggers(now=date(2026, 9, 7))
    assert report.partial == 2 and report.failed == 0
    assert all(json_load(p)["executionStatus"] == "partial" for p in trigger.discoveries_dir.glob("*.json"))


def json_load(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
