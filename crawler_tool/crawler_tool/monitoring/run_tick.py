"""监测 tick CLI：跑监测 / 看日报 / 标注告警状态，三个动作一个入口。

用法：

  跑一次监测（30 分钟调度的第二步，紧跟爬取脚本之后）：
    python -m crawler_tool.monitoring.run_tick ^
        --items data/crawl/crawl_20260906.jsonl ^
        --config-dir config/monitoring --data-dir data/monitoring

  接 LLM 慢车道（键从环境变量 MONITOR_LLM_API_KEY 读取，绝不写进命令行）：
    set MONITOR_LLM_API_KEY=sk-...   （或 PowerShell: $env:MONITOR_LLM_API_KEY="sk-..."）
    ... run_tick --items ... --triage deepseek ...

  每日日报（看告警摘要，不跑 tick）：
    ... run_tick --report --data-dir data/monitoring

  标注判读结果（灰度期每日动作）：
    ... run_tick --set-status alr_20260906_120000_001 accepted --note "确认为竞品发布"
    ... run_tick --set-status alr_20260906_120002_003 rejected --note "误报：公告非产品"

设计为单次、无状态进程：状态与告警都在 --data-dir 落盘，重启不丢。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

VALID_STATUSES = ("pending", "processing", "accepted", "rejected", "archived")


def _load_items(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                # 支持每行一个条目，或每行一个 {"items": [...]} 的整轮响应。
                if "items" in payload and isinstance(payload["items"], list):
                    items.extend(entry for entry in payload["items"] if isinstance(entry, dict))
                else:
                    items.append(payload)
    return items


def _build_triage_client(name: str):
    if name == "off":
        return None
    if name == "deepseek":
        from crawler_tool.monitoring.llm_clients import DeepSeekTriage

        return DeepSeekTriage()
    raise ValueError(f"unknown triage client: {name}")


def _print_report(tool) -> None:
    alerts = tool.store.load_alerts()
    if not alerts:
        print("（暂无告警）")
        alerts = []
    for alert in reversed(alerts):  # 最新在前
        platforms = "、".join((alert.get("resonance") or {}).get("platforms", []))
        flags = ",".join(key for key, value in (alert.get("flags") or {}).items() if value)
        status = alert.get("status") or "pending"
        print(
            f"[{status}|{alert.get('priority')}] {alert.get('alertId')} "
            f"{alert.get('matchedRule')} score={alert.get('score')} "
            f"主体={alert.get('subjectName') or '-'} 平台={platforms or '-'}"
            + (f" 标记={flags}" if flags else "")
        )
        why = alert.get("why") or ""
        if why:
            print(f"    理由: {why}")
        for note_flag in ("statusNote",):
            if alert.get(note_flag):
                print(f"    标注: {alert[note_flag]}")
    observation = tool.store.load_observation(limit=50)
    print(f"\n观察箱最近 {min(50, len(observation))} 条（共累计文件内全部）：")
    for entry in observation[-10:]:
        print(f"  [{entry.get('reason')}] {entry.get('title')}（{entry.get('platform')}）")


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitoring tick / daily report / alert labeling")
    parser.add_argument("--items", default=None, help="爬取产出 JSONL（每行一条 ContentItem 或一轮响应）")
    parser.add_argument("--config-dir", default=None, help="覆盖配置目录（base_rules.json / entities.json）")
    parser.add_argument("--data-dir", default="data/monitoring", help="告警/观察箱/状态落盘目录")
    parser.add_argument("--triage", choices=["off", "deepseek"], default="off",
                        help="慢车道 LLM 分诊；键读环境变量 MONITOR_LLM_API_KEY")
    parser.add_argument("--report", action="store_true", help="打印告警日报与观察箱摘要（不跑 tick）")
    parser.add_argument("--set-status", nargs=2, metavar=("ALERT_ID", "STATUS"),
                        help=f"更新告警状态，STATUS 取值：{'/'.join(VALID_STATUSES)}")
    parser.add_argument("--note", default="", help="随 --set-status 写入的判读备注")
    args = parser.parse_args()

    from crawler_tool.monitoring.agent_bridge import MonitoringTool
    from crawler_tool.monitoring.config_loader import entities_source

    tool = MonitoringTool(config_dir=args.config_dir, data_dir=args.data_dir,
                          triage_client=_build_triage_client(args.triage))

    if args.set_status:
        alert_id, status = args.set_status
        if status not in VALID_STATUSES:
            raise SystemExit(f"invalid status: {status}（可选：{'/'.join(VALID_STATUSES)}）")
        changed = tool.update_alert_status(alert_id, status, args.note)
        print(json.dumps({"updated": changed, "alertId": alert_id, "status": status},
                         ensure_ascii=False))
        return

    if args.report:
        _print_report(tool)
        return

    if not args.items:
        parser.print_help()
        return

    source = entities_source(args.config_dir)
    if source == "packaged_example":
        print("⚠ 当前主体档案是包内虚构示例（未找到 entities.json）——上线前必须替换！", flush=True)
    items = _load_items(Path(args.items))
    summary = tool.ingest(items)
    summary["entitiesSource"] = source
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
