"""Agent 自动触发器：score 达标的告警 → 种子事件 → 发现循环执行点。

设计（业务决策 2026-09-07：自动化优先，触发线 2.0）：
- 触发用 score 而非显示级别——灰度模式只影响展示，不阻断自动化；
- 幂等：同一 alertId 只触发一次（结果文件 + 状态回写双保险）；
- 每日上限：防止误报风暴烧穿爬取/LLM 预算；
- 执行点DiscoveryRunner 是标准接缝：agent 组的 discovery_workflow 到位后
  以适配类注入；未注入时只报告待触发数量，不改动告警状态（人工流程不受影响）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from ._core.bridge import MonitoringTool, alert_to_seed_event
from ._core.models import parse_utc


class DiscoveryRunner(Protocol):
    """发现循环执行点协议。实现方：agent 组的 discovery_workflow 适配器。

    seed_event 为 alert_to_seed_event 的输出（EVENT_EXPANSION 种子）；
    budget 为 {"maxRounds": int, "maxToolCalls": int, "maxLlmCalls": int}；
    返回发现循环的结果字典（events/classifications/chains/analysis...）。
    """

    def run(self, seed_event: dict[str, Any], budget: dict[str, int]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TriggerReport:
    due: int
    dispatched: int
    skipped_reason: str | None = None
    failed: int = 0
    partial: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {"due": self.due, "dispatched": self.dispatched, "skippedReason": self.skipped_reason,
                "failed": self.failed, "partial": self.partial}


class AgentTrigger:
    def __init__(
        self,
        tool: MonitoringTool,
        runner: DiscoveryRunner | None = None,
        *,
        trigger_score: float = 2.0,
        max_runs_per_day: int = 10,
        max_rounds: int = 3,
        max_tool_calls: int = 6,
        max_llm_calls: int = 32,
    ) -> None:
        self.tool = tool
        self.runner = runner
        self.trigger_score = trigger_score
        self.max_runs_per_day = max_runs_per_day
        self.max_rounds = max_rounds
        self.max_tool_calls = max_tool_calls
        self.max_llm_calls = max_llm_calls
        self.discoveries_dir = tool.store.data_dir / "discoveries"
        self.discoveries_dir.mkdir(parents=True, exist_ok=True)

    # ---- 主入口 ----

    def run_due_triggers(self, now: date | None = None) -> TriggerReport:
        today = (now or date.today()).isoformat()
        due = self.due_alerts()
        if not due:
            return TriggerReport(due=0, dispatched=0)
        if self.runner is None:
            # 无执行器：只报告，不改告警状态（人工判读流程不受影响）。
            return TriggerReport(due=len(due), dispatched=0, skipped_reason="no_runner_configured")
        dispatched = 0
        failed = partial = 0
        for alert in due:
            if self._runs_today(today) >= self.max_runs_per_day:
                return TriggerReport(due=len(due), dispatched=dispatched, skipped_reason="daily_cap_reached",
                                     failed=failed, partial=partial)
            payload = self._dispatch(alert, today)
            failed += payload["executionStatus"] == "failed"
            partial += payload["executionStatus"] == "partial"
            dispatched += 1
        return TriggerReport(due=len(due), dispatched=dispatched, failed=failed, partial=partial)

    # ---- 选件 ----

    def due_alerts(self) -> list[dict[str, Any]]:
        """score 达标 + 待判读 + 未触发过，按分数降序。"""
        due = [
            alert for alert in self.tool.store.load_alerts()
            if alert.get("status") == "pending"
            and float(alert.get("score") or 0) >= self.trigger_score
            and not self._already_dispatched(alert)
        ]
        due.sort(key=lambda a: float(a.get("score") or 0), reverse=True)
        return due

    def _already_dispatched(self, alert: dict[str, Any]) -> bool:
        return (self.discoveries_dir / f"{alert.get('alertId')}.json").exists()

    def _runs_today(self, today: str) -> int:
        state = self.tool.store.load_state()
        return int((state.get("agentRuns") or {}).get(today, 0))

    # ---- 执行 ----

    def _dispatch(self, alert: dict[str, Any], today: str) -> dict[str, Any]:
        seed = alert_to_seed_event(alert)
        budget = {"maxRounds": self.max_rounds, "maxToolCalls": self.max_tool_calls,
                  "maxLlmCalls": self.max_llm_calls}
        try:
            result = self.runner.run(seed_event=seed, budget=budget)  # type: ignore[union-attr]
            # 在写入前验证协议，日期等必须由 runner 转成 JSON 兼容字段。
            if not isinstance(result, dict):
                raise TypeError("DiscoveryRunner 必须返回字典")
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            # 失败也落盘并占用次数，避免下一轮无界重试或中断后续告警。
            result = {"stopReason": "FAILED", "errors": [{
                "stage": "runner", "errorType": type(exc).__name__,
                "message": "发现循环执行失败；未保存原始异常内容",
            }]}
        stop_reason = result.get("stopReason", "PARTIAL")
        execution_status = ("completed" if stop_reason == "COMPLETED" else
                            "failed" if stop_reason == "FAILED" else "partial")
        payload = {
            "alertId": alert.get("alertId"),
            "seedEvent": seed,
            "budget": budget,
            "result": result,
            "executionStatus": execution_status,
            "dispatchedAt": today,
        }
        (self.discoveries_dir / f"{alert.get('alertId')}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self.tool.update_alert_status(str(alert.get("alertId")), "dispatched", "已自动触发发现循环")
        state = self.tool.store.load_state()
        runs = state.setdefault("agentRuns", {})
        runs[today] = int(runs.get(today, 0)) + 1
        self.tool.store.save_state(state)
        return payload

class NewsSignalTrigger(AgentTrigger):
    """触发器入口：待处理信号 → 资格／额度／去重检查 → 外部 runner。

    只负责派发和状态；实际调查、模型调用及预算执行由 Agent 组完成。
    """

    def __init__(self, tool: MonitoringTool, runner=None):
        settings = tool.config.settings
        super().__init__(tool, runner, trigger_score=settings.agent_trigger_score,
                         max_runs_per_day=settings.agent_max_runs_per_day,
                         max_rounds=settings.agent_max_rounds, max_tool_calls=settings.agent_max_tool_calls,
                         max_llm_calls=getattr(tool, 'max_llm_calls', 32))
        self.current_time = datetime.now(timezone.utc)

    def due_alerts(self) -> list[dict]:
        due = []
        for alert in super().due_alerts():
            if alert.get("signalSchema") != "news_signal.v1" or not alert.get("triggerEligible"):
                continue
            # 延后的 pending 信号也检查有效期，避免次日额度恢复后调查过期材料。
            stamp = (parse_utc(alert.get("publishedAt")) if not alert.get("flags", {}).get("timeFallback")
                     else parse_utc(alert.get("firstSeenAt")))
            if stamp is None or not -timedelta(minutes=5) <= self.current_time - stamp <= timedelta(hours=self.tool.config.settings.lookback_hours):
                continue
            due.append(alert)
        return due

    def run_due_triggers(self, now=None):
        if isinstance(now, datetime):
            self.current_time = now.astimezone(timezone.utc)
            return super().run_due_triggers(self.current_time.date())
        self.current_time = datetime.now(timezone.utc)
        return super().run_due_triggers(now)
