"""真实采集发现 / 单次监测联动入口；不注册定时任务。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .crawler_integration import WorkflowDiscoveryRunner, build_crawler_workflow
from .llm import DeepSeekLLM
from .models import Event
from .workflow_models import StopReason, WorkflowRequest
from .workflow_models import WorkflowResult
from .timeline import write_timeline
from .report import write_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="连接 crawler_tool 与 SignalX Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    collection = commands.add_parser("collect", help="先完成批量采集，保存原始响应和 Evidence，不调用模型")
    collection.add_argument("--keyword", action="append", required=True)
    collection.add_argument("--industry", required=True)
    collection.add_argument("--output-dir", type=Path, required=True)
    collection.add_argument("--platform", action="append")
    collection.add_argument("--base-url", default="http://127.0.0.1:8301")
    collection.add_argument("--max-pages", type=int, default=5)
    collection.add_argument("--max-source-requests", type=int, default=200)
    collection.add_argument("--limit", type=int, default=100)
    collection.add_argument("--request-delay", type=float, default=1.0)
    collection.add_argument("--live", action="store_true", required=True)
    discovery = commands.add_parser("discover", help="关键词发现或本地种子扩展")
    mode = discovery.add_mutually_exclusive_group(required=True)
    mode.add_argument("--keyword", action="append", help="可重复指定关键词")
    mode.add_argument("--seed", type=Path, help="Event / alert_to_seed_event 输出 JSON")
    discovery.add_argument("--output-dir", type=Path, required=True, help="新目录，拒绝覆盖")
    discovery.add_argument("--platform", action="append", help="不指定时使用采集服务默认来源")
    discovery.add_argument("--max-rounds", type=int, default=3)
    discovery.add_argument("--max-tool-calls", type=int, default=6)
    discovery.add_argument("--entities-file", type=Path, help="主体别名表，默认读取同级 crawler_tool 配置")
    monitor = commands.add_parser("monitor", help="只执行一次采集、监测、告警扩展")
    monitor.add_argument("--config-dir", type=Path, required=True)
    monitor.add_argument("--data-dir", type=Path, required=True, help="持续使用的监测状态目录")
    monitor.add_argument("--crawl-dir", type=Path, required=True)
    monitor.add_argument("--max-rounds", type=int, default=4)
    monitor.add_argument("--max-tool-calls", type=int, default=12)
    for command in (discovery, monitor):
        command.add_argument("--topic", help="完整主题监测；不以单条告警限定范围")
        command.add_argument("--topic-alias", action="append", default=[])
        command.add_argument("--target-evidence", type=int, default=30, help="相关材料覆盖目标，不足时如实报告")
        command.add_argument("--max-pages", type=int, default=3, help="每查询每来源最多页数")
        command.add_argument("--max-source-requests", type=int, default=60, help="一次工作流的采集 HTTP 请求硬上限")
        command.add_argument("--live", action="store_true", required=True,
                             help="确认访问爬虫服务并调用真实模型")
        command.add_argument("--industry", required=True)
        command.add_argument("--base-url", default="http://127.0.0.1:8301")
        command.add_argument("--max-llm-calls", type=int, default=24, help="每次发现循环上限")
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--max-no-progress-rounds", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            from .bulk_collection import run
            return run(args)
        if not 1 <= args.max_pages <= 10 or not 1 <= args.max_source_requests <= 500:
            raise ValueError("分页或采集请求预算不合法")
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
                topic=args.topic, topic_aliases=args.topic_alias,
                target_evidence_count=args.target_evidence if args.topic else 0,
            )
            workflow = build_crawler_workflow(
                DeepSeekLLM(response_log=args.output_dir / "model_responses.jsonl"),
                base_url=args.base_url, platforms=args.platform, entities_file=args.entities_file,
                max_pages=args.max_pages, max_requests=args.max_source_requests,
                archive_path=args.output_dir / "search_responses.jsonl",
            )
            args.output_dir.mkdir(parents=True, exist_ok=False)
            (args.output_dir / "search_responses.jsonl").touch(exist_ok=False)
            result = workflow.run(request)
            (args.output_dir / "result.json").write_text(
                result.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8",
            )
            write_timeline(result, args.output_dir / "timeline.md")
            write_report(result, args.output_dir / "report.md")
            print(json.dumps({"stopReason": result.stop_reason.value, "events": len(result.events),
                              "outputDirectory": str(args.output_dir.resolve())}, ensure_ascii=False))
            return 0 if result.stop_reason == StopReason.COMPLETED else 2

        from crawler_tool.monitoring.scheduled_tick import load_query_plan, run_scheduled_tick
        from crawler_tool.monitoring.config_loader import DEFAULT_CONFIG_DIR

        queries_file = args.config_dir / "queries.json"
        plan = load_query_plan(queries_file)
        if args.topic:
            return _run_topic_monitor(args, plan)
        workflow = build_crawler_workflow(
            DeepSeekLLM(), base_url=args.base_url, platforms=plan["platforms"],
            max_pages=args.max_pages, max_requests=args.max_source_requests,
            entities_file=(args.config_dir / "entities.json") if (args.config_dir / "entities.json").exists()
            else DEFAULT_CONFIG_DIR / "entities.example.json",
        )
        runner = WorkflowDiscoveryRunner(
            workflow, industry=args.industry, max_llm_calls=limits.max_llm_calls,
            per_query_limit=limits.per_query_limit,
            max_no_progress_rounds=limits.max_no_progress_rounds,
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


def _run_topic_monitor(args, plan: dict) -> int:
    """主题新闻统一处理一次；规则告警为附属输出，不再限制进入分析的新闻。"""
    from crawler_tool.monitoring.agent_bridge import MonitoringTool
    from crawler_tool.monitoring.daily_report import generate_daily_report
    from .crawler_integration import load_entity_aliases

    tool = MonitoringTool(config_dir=args.config_dir, data_dir=args.data_dir)
    entity_path = args.config_dir / "entities.json"
    aliases = load_entity_aliases(entity_path) if entity_path.exists() else {}
    request = WorkflowRequest(
        mode="BULK_DISCOVERY", industry=args.industry, topic=args.topic,
        topic_aliases=[*args.topic_alias, *(k for k, v in aliases.items() if v.casefold() == args.topic.casefold())],
        preset_keywords=plan["queries"], target_evidence_count=args.target_evidence,
        max_rounds=args.max_rounds, max_tool_calls=args.max_tool_calls,
        max_llm_calls=args.max_llm_calls, per_query_limit=args.limit,
        max_no_progress_rounds=args.max_no_progress_rounds,
        per_round_query_limit=3,
    )
    now = datetime.now(timezone.utc)
    output = args.data_dir / "topics" / now.strftime("%Y%m%dT%H%M%S%fZ")
    workflow = build_crawler_workflow(
        DeepSeekLLM(response_log=output / "model_responses.jsonl"), base_url=args.base_url, platforms=plan["platforms"],
        entities_file=entity_path if entity_path.exists() else None,
        max_pages=args.max_pages, max_requests=args.max_source_requests,
        archive_path=output / "search_responses.jsonl",
    )
    output.mkdir(parents=True, exist_ok=False)
    (output / "search_responses.jsonl").touch(exist_ok=False)
    result = workflow.run(request)
    # 先落盘结果，监测附属文件出错也不丢失已经完成的分析。
    with (output / "result.json").open("x", encoding="utf-8") as stream:
        stream.write(result.model_dump_json(by_alias=True, indent=2) + "\n")
    write_timeline(result, output / "timeline.md")
    write_report(result, output / "report.md")
    tool.ingest(list(workflow.collector.tool.collected_items.values()), now=now)
    generate_daily_report(tool.store, out_dir=args.crawl_dir.parent / "reports", day=now.date())
    print(json.dumps({"stopReason": result.stop_reason.value, "relevantItems": len(result.evidence),
                      "events": len(result.events), "outputDirectory": str(output.resolve())}, ensure_ascii=False))
    return 0 if result.stop_reason == StopReason.COMPLETED and len(result.evidence) >= args.target_evidence else 2


if __name__ == "__main__":
    raise SystemExit(main())
