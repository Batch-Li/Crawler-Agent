"""每日监测日报：把当天的告警、观察箱、运行统计生成为一份 markdown 文件。

由 scheduled_tick 每轮自动刷新（幂等：同一天重写同一份文件），
也可用 run_tick --daily-report 手动生成。人只负责看和标注。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from crawler_tool.monitoring.store import MonitorStore


def generate_daily_report(
    store: MonitorStore,
    out_dir: str | Path = "data/reports",
    day: date | None = None,
) -> Path:
    day = day or datetime.now(timezone.utc).date()
    prefix = day.isoformat()
    alerts = [json_alert for json_alert in store.load_alerts() if str(json_alert.get("generatedAt", "")).startswith(prefix)]
    observation = [e for e in store.load_observation(limit=500) if str(e.get("observedAt", "")).startswith(prefix)]
    state = store.load_state()

    pending = [a for a in alerts if a.get("status") == "pending"]
    lines: list[str] = [
        f"# 监测日报 {prefix}",
        "",
        f"- 告警：{len(alerts)} 条（待判读 {len(pending)}）",
        f"- 观察箱新增：{len(observation)} 条",
        f"- 累计 tick：{state.get('tickCount', 0)}",
        f"- agent 触发：{(state.get('agentRuns') or {}).get(prefix, 0)} 次",
        "",
        "## 告警",
        "",
    ]
    if not alerts:
        lines.append("（今日无告警）")
    for alert in sorted(alerts, key=lambda a: float(a.get("score") or 0), reverse=True):
        evidence = (alert.get("evidence") or [{}])[0]
        lines.append(
            f"- **[{alert.get('status')}|{alert.get('priority')}] {alert.get('matchedRule')}"
            f" score={alert.get('score')}** 主体={alert.get('subjectName') or '-'}"
            f" 平台={'+'.join((alert.get('resonance') or {}).get('platforms', []))}"
        )
        lines.append(f"  - {evidence.get('title', '')}")
        lines.append(f"  - 理由：{alert.get('why', '')}")
        if alert.get("statusNote"):
            lines.append(f"  - 标注：{alert['statusNote']}")
    lines += ["", "## 观察箱新增", ""]
    if not observation:
        lines.append("（今日无新增）")
    for entry in observation[-30:]:
        lines.append(f"- [{entry.get('reason', '')}] {entry.get('title', '')}（{entry.get('platform')}）")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    report_path = out_path / f"daily_report_{prefix}.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
