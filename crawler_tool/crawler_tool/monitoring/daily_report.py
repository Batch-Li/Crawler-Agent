"""每日监测日报：把当天的告警、观察箱、运行统计生成为一份 markdown 文件。

由 scheduled_tick 每轮自动刷新（幂等：同一天重写同一份文件），
也可用 run_tick --daily-report 手动生成。人只负责看和标注。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
import re

from crawler_tool.monitoring.store import MonitorStore

_STATUS = {"pending": "待处理", "dispatched": "已启动分析", "confirmed": "已确认", "dismissed": "已排除"}
_PRIORITY = {"yellow": "黄色线索", "orange": "橙色线索", "red": "红色线索"}
_SOURCE = {"toutiao": "今日头条搜索", "south_weekend": "南方周末", "cctv_news": "央视新闻"}
_REASON = {"triage_unavailable_or_non_event": "未启用研判或未识别为事件",
           "industry_watch": "行业观察", "below_threshold": "未达告警阈值"}


def _text(value: Any) -> str:
    return re.sub(r'[\x00-\x1f\u200b-\u200f\u202a-\u202e\ufeff]', ' ', str(value or '')).replace('<', '&lt;').replace('>', '&gt;')


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
        f"- 累计监测轮次：{state.get('tickCount', 0)}",
        f"- 告警扩展触发：{(state.get('agentRuns') or {}).get(prefix, 0)} 次（不含主题统一分析）",
        "",
        "## 告警",
        "",
    ]
    if not alerts:
        lines.append("（今日无告警）")
    for alert in sorted(alerts, key=lambda a: float(a.get("score") or 0), reverse=True):
        evidence = (alert.get("evidence") or [{}])[0]
        lines.append(
            f"- **{_STATUS.get(alert.get('status'), '已记录')} · {_PRIORITY.get(alert.get('priority'), '观察线索')}"
            f" · 规则分 {alert.get('score')}** 主体：{_text(alert.get('subjectName') or '-')}"
            f"；来源：{'、'.join(_SOURCE.get(p, p) for p in (alert.get('resonance') or {}).get('platforms', []))}"
        )
        lines.append(f"  - {_text(evidence.get('title', ''))}")
        lines.append(f"  - 理由：{alert.get('why', '')}")
        if alert.get("statusNote"):
            lines.append(f"  - 标注：{alert['statusNote']}")
    lines += ["", "## 观察箱新增", ""]
    if not observation:
        lines.append("（今日无新增）")
    for entry in observation[-30:]:
        lines.append(f"- {_text(entry.get('title', ''))}（{_SOURCE.get(entry.get('platform'), entry.get('platform'))}；{_REASON.get(entry.get('reason'), '留作观察')}）")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    report_path = out_path / f"daily_report_{prefix}.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
