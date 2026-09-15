"""真实采集发现 / 单次监测联动入口；不注册定时任务。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from .crawler_integration import WorkflowDiscoveryRunner, build_crawler_workflow
from .llm import DeepSeekLLM
from .models import Event
from .workflow_models import StopReason, WorkflowRequest
from .workflow_models import WorkflowResult
from .timeline import write_timeline


def _capture_queue_queries(result: WorkflowResult) -> list[str]:
    """已执行查询原文 + 未完成查询；推原文保证捕获回来后子串匹配能命中。"""
    queries = [call.query for record in result.rounds for call in record.tool_calls]
    queries.extend(result.pending_queries)
    return list(dict.fromkeys(queries))


def push_capture_queue(*, base_url: str, queries: list[str]) -> dict[str, object]:
    """推送失败只记录在返回值里，不影响运行退出码，也不重试。"""
    if not queries:
        return {}
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/api/v1/tool/capture-queue",
            json={"keywords": queries, "source": "agent"},
            timeout=10,
        )
        response.raise_for_status()
        return {"captureQueuePushed": response.json().get("added", 0)}
    except Exception as exc:
        return {"captureQueueError": f"{type(exc).__name__}: {exc}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="连接 crawler_tool 与 SignalX Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    discovery = commands.add_parser("discover", help="关键词发现或本地种子扩展")
    mode = discovery.add_mutually_exclusive_group(required=True)
    mode.add_argument("--keyword", action="append", help="可重复指定关键词")
    mode.add_argument("--seed", type=Path, help="Event / alert_to_seed_event 输出 JSON")
    discovery.add_argument("--output-dir", type=Path, required=True, help="新目录，拒绝覆盖")
    discovery.add_argument("--platform", action="append", help="不指定时使用采集服务默认来源")
    discovery.add_argument("--max-rounds", type=int, default=3)
    discovery.add_argument("--max-tool-calls", type=int, default=6)
    discovery.add_argument("--entities-file", type=Path, help="主体别名表，默认读取同级 crawler_tool 配置")
    discovery.add_argument("--enrich-time", action="store_true",
                           help="对缺时间条目做有边界详情页补抓（每次运行 ≤5 条，仅提取发布时间）")
    discovery.add_argument("--enrich-content", action="store_true",
                           help="对缺正文条目做有边界详情页补抓（与 --enrich-time 共享预算与请求，南周/央视支持）")
    discovery.add_argument("--push-capture-queue", action="store_true",
                           help="运行后把已执行与未完成查询推送到捕获队列，供小红书/抖音人工捕获闭环")
    discovery.add_argument("--trigger-capture", action="store_true",
                           help="运行结束后同步触发浏览器自动化补采（小红书消费队列 + 头条补正文；"
                                "受脚本内置硬限与即停自锁约束，可能拖长运行数分钟并弹出浏览器窗口）")
    monitor = commands.add_parser("monitor", help="只执行一次采集、监测、告警扩展")
    monitor.add_argument("--config-dir", type=Path, required=True)
    monitor.add_argument("--data-dir", type=Path, required=True, help="持续使用的监测状态目录")
    monitor.add_argument("--crawl-dir", type=Path, required=True)
    for command in (discovery, monitor):
        command.add_argument("--live", action="store_true", required=True,
                             help="确认访问爬虫服务并调用真实模型")
        command.add_argument("--industry", required=True)
        command.add_argument("--base-url", default="http://127.0.0.1:8301")
        command.add_argument("--max-llm-calls", type=int, default=24, help="每次发现循环上限")
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--max-no-progress-rounds", type=int, default=1)
        command.add_argument("--time-fallback", action="store_true",
                             help="模型没给时间锚点时，从证据原文日期/高置信发布时间保守落地事件日期（全部标注待人工复核）")
    args = parser.parse_args(argv)
    try:
        # 共用模型校验，错误参数在采集和状态写入之前拒绝。
        limits = WorkflowRequest(
            mode="BULK_DISCOVERY", industry=args.industry, preset_keywords=["参数校验"],
            max_llm_calls=args.max_llm_calls, per_query_limit=args.limit,
            max_no_progress_rounds=args.max_no_progress_rounds,
        )
        if args.command == "discover":
            seed = Event.model_validate_json(args.seed.read_text(encoding="utf-8")) if args.seed else None
            request = WorkflowRequest(
                mode="EVENT_EXPANSION" if seed else "BULK_DISCOVERY",
                industry=args.industry, preset_keywords=args.keyword or [], seed_event=seed,
                max_rounds=args.max_rounds, max_tool_calls=args.max_tool_calls,
                max_llm_calls=limits.max_llm_calls, per_query_limit=limits.per_query_limit,
                max_no_progress_rounds=limits.max_no_progress_rounds,
                time_fallback=args.time_fallback,
            )
            workflow = build_crawler_workflow(
                DeepSeekLLM(), base_url=args.base_url, platforms=args.platform, entities_file=args.entities_file,
                enrich_time=args.enrich_time, enrich_content=args.enrich_content,
            )
            args.output_dir.mkdir(parents=True, exist_ok=False)
            result = workflow.run(request)
            (args.output_dir / "result.json").write_text(
                result.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8",
            )
            write_timeline(result, args.output_dir / "timeline.md")
            summary: dict[str, object] = {
                "stopReason": result.stop_reason.value, "events": len(result.events),
                "outputDirectory": str(args.output_dir.resolve()),
            }
            if args.push_capture_queue:
                summary.update(push_capture_queue(
                    base_url=args.base_url, queries=_capture_queue_queries(result),
                ))
            if args.trigger_capture:
                from agent.capture_trigger import trigger_browser_capture

                outcome = trigger_browser_capture(base_url=args.base_url)
                summary["captureTrigger"] = outcome
                for entry in outcome:
                    print(f"[补采] {entry.get('platform')}: {entry.get('exit', entry.get('note', ''))}")
            print(json.dumps(summary, ensure_ascii=False))
            return 0 if result.stop_reason == StopReason.COMPLETED else 2

        from crawler_tool.monitoring.scheduled_tick import load_query_plan, run_scheduled_tick
        from crawler_tool.monitoring.config_loader import DEFAULT_CONFIG_DIR

        queries_file = args.config_dir / "queries.json"
        plan = load_query_plan(queries_file)
        workflow = build_crawler_workflow(
            DeepSeekLLM(), base_url=args.base_url, platforms=plan["platforms"],
            entities_file=(args.config_dir / "entities.json") if (args.config_dir / "entities.json").exists()
            else DEFAULT_CONFIG_DIR / "entities.example.json",
        )
        runner = WorkflowDiscoveryRunner(
            workflow, industry=args.industry, max_llm_calls=limits.max_llm_calls,
            per_query_limit=limits.per_query_limit,
            max_no_progress_rounds=limits.max_no_progress_rounds,
            time_fallback=args.time_fallback,
        )
        prior_outputs = set((args.data_dir / "discoveries").glob("*.json"))
        summary = run_scheduled_tick(
            config_dir=args.config_dir, data_dir=args.data_dir, crawl_dir=args.crawl_dir,
            base_url=args.base_url, queries_file=queries_file, search_limit=args.limit,
            agent_runner=runner,
        )
        for path in set((args.data_dir / "discoveries").glob("*.json")) - prior_outputs:
            output = path.with_suffix(".timeline.md")
            if output.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload.get("result"), dict) and "stopReason" in payload["result"]:
                write_timeline(WorkflowResult.model_validate(payload["result"]), output)
        # 摘要不转储上游异常正文或响应内容。
        print(json.dumps({
            "error": bool(summary.get("error")), "crawledItems": summary.get("crawledItems", 0),
            "agentTrigger": summary.get("agentTrigger"),
            "monitoringDirectory": str(args.data_dir.resolve()),
        }, ensure_ascii=False))
        trigger = summary.get("agentTrigger", {})
        failed_query = any(
            row.get("error") or any(r["status"] not in ("success", "empty") for r in row.get("sourceReports", []))
            for row in summary.get("queryReports", [])
        )
        return 2 if (summary.get("error") or summary.get("capturedError") or failed_query
                     or trigger.get("failed") or trigger.get("partial")) else 0
    except Exception as exc:
        print(json.dumps({"errorType": type(exc).__name__,
                          "message": "对接运行失败，请检查依赖、参数、服务及模型配置"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
