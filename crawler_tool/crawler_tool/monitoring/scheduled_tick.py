"""一站式定时 tick：查询轮换调用本机爬虫服务 → 条目存档 → 就地跑监测。

这是 30 分钟调度任务实际要调用的唯一命令（爬取与监测的粘合层）：

    python -m crawler_tool.monitoring.scheduled_tick ^
        --config-dir config/monitoring --data-dir data/monitoring

前提：爬虫服务已用 `--serve` 启动。查询清单与轮换大小来自
`config/monitoring/queries.json`（queries / queriesPerTick / platforms）。

请求预算 = queriesPerTick × len(platforms)，默认 2 × 2 = 4 请求/轮，
约 96 请求/天/来源——保持项目"低频、可审计"的纪律。
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from crawler_tool.monitoring.agent_bridge import MonitoringTool
from crawler_tool.monitoring.store import MonitorStore

QUERIES_SCHEMA_HINT = 'queries.json 形如 {"queries": ["华为", "小米"], "queriesPerTick": 2, "platforms": ["south_weekend", "toutiao"]}'


def load_query_plan(queries_file: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(queries_file).read_text(encoding="utf-8"))
    queries = [str(q) for q in (payload.get("queries") or []) if str(q).strip()]
    if not queries:
        raise ValueError(f"queries.json 没有可用查询。{QUERIES_SCHEMA_HINT}")
    return {
        "queries": queries,
        "queries_per_tick": max(1, int(payload.get("queriesPerTick") or 2)),
        "platforms": [str(p) for p in (payload.get("platforms") or ["south_weekend", "toutiao"])],
        "include_captured": bool(payload.get("includeCaptured", False)),
    }


def rotate_queries(queries: list[str], per_tick: int, tick_index: int) -> list[str]:
    """按 tick 序号轮换取查询，跨轮覆盖整个池子且不重复。"""
    size = min(per_tick, len(queries))
    start = tick_index % len(queries)
    return [queries[(start + offset) % len(queries)] for offset in range(size)]


def _search_via_service(
    base_url: str,
    query: str,
    platforms: list[str],
    limit: int,
    timeout: float,
) -> dict[str, Any]:
    response = httpx.post(
        f"{base_url.rstrip('/')}/api/v1/tool/search-content",
        json={"query": query, "platforms": platforms, "limit": limit, "freshness": "prefer_fresh"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def run_scheduled_tick(
    config_dir: str | Path | None = None,
    data_dir: str | Path = "data/monitoring",
    crawl_dir: str | Path = "data/crawl",
    base_url: str = "http://127.0.0.1:8301",
    queries_file: str | Path = "config/monitoring/queries.json",
    search_limit: int = 10,
    poster: Callable[[str, list[str], int], dict[str, Any]] | None = None,
    captured_fetcher: Callable[..., dict[str, Any]] | None = None,
    agent_runner: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    plan = load_query_plan(queries_file)
    store = MonitorStore(data_dir)
    state = store.load_state()
    tick_index = int(state.get("tickCount", 0))

    # 服务可达性检查（注入 poster 的离线测试跳过）。
    if poster is None:
        try:
            httpx.get(f"{base_url.rstrip('/')}/api/v1/health", timeout=3)
        except Exception as exc:
            return {
                "error": f"crawler service unreachable at {base_url}: {type(exc).__name__}",
                "hint": "先启动爬虫服务：python -m crawler_tool.interfaces.app --serve",
            }

    selected = rotate_queries(plan["queries"], plan["queries_per_tick"], tick_index)
    crawl_path = Path(crawl_dir) / f"crawl_{now.strftime('%Y%m%d')}.jsonl"
    crawl_path.parent.mkdir(parents=True, exist_ok=True)

    all_items: list[dict[str, Any]] = []
    query_reports: list[dict[str, Any]] = []
    with crawl_path.open("a", encoding="utf-8") as archive:
        for query in selected:
            entry: dict[str, Any] = {"query": query}
            try:
                if poster is not None:
                    payload = poster(query, plan["platforms"], search_limit)
                else:
                    payload = _search_via_service(base_url, query, plan["platforms"], search_limit, timeout=30)
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
                query_reports.append(entry)
                continue
            items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
            all_items.extend(items)
            # 存档整轮响应（带 query 标记）；run_tick 的加载器兼容这种行。
            archive.write(json.dumps({**payload, "query": query}, ensure_ascii=False) + "\n")
            entry["itemCount"] = len(items)
            entry["sourceReports"] = [
                {"platform": report.get("platform"), "status": report.get("status")}
                for report in (payload.get("sourceReports") or [])
            ]
            query_reports.append(entry)

    # 手动捕获条目（小红书/抖音/微信信封）并入监测视野——网络不可达来源的唯一数据通道。
    # 会话视图包含本轮自己刚爬的条目和历史已见条目，按 dedupKey 排除，避免重复喂养。
    captured_count = 0
    captured_error = None
    if plan["include_captured"]:
        try:
            if captured_fetcher is not None:
                payload = captured_fetcher(limit=100)
            else:
                resp = httpx.get(
                    f"{base_url.rstrip('/')}/api/v1/tool/captured-items",
                    params={"limit": 100},
                    timeout=15,
                )
                resp.raise_for_status()
                payload = resp.json()
            seen_keys = {item.get("dedupKey") for item in all_items}
            seen_keys |= set((state.get("items") or {}).keys())
            captured = []
            for item in (payload.get("items") or []):
                if not isinstance(item, dict):
                    continue
                flat = MonitoringTool._flatten_content_item(item)
                key = flat.get("dedupKey")
                if key and key in seen_keys:
                    continue
                seen_keys.add(key)
                captured.append(item)
            all_items.extend(captured)
            captured_count = len(captured)
        except Exception as exc:
            captured_error = f"{type(exc).__name__}: {exc}"

    tool = MonitoringTool(config_dir=config_dir, data_dir=data_dir)
    monitor_summary = tool.ingest(all_items, now=now)

    # Agent 自动触发：score ≥ 阈值的 pending 告警 → 种子事件 → 发现循环执行点。
    # 未注入 runner 时只报告待触发数量，不改变告警状态。
    agent_report: dict[str, Any] = {"due": 0, "dispatched": 0}
    settings = tool.config.settings
    if settings.agent_auto_trigger:
        from crawler_tool.monitoring.agent_trigger import AgentTrigger

        trigger = AgentTrigger(
            tool,
            runner=agent_runner,
            trigger_score=settings.agent_trigger_score,
            max_runs_per_day=settings.agent_max_runs_per_day,
            max_rounds=settings.agent_max_rounds,
            max_tool_calls=settings.agent_max_tool_calls,
        )
        agent_report = trigger.run_due_triggers(now=now.date() if now else None).to_payload()

    return {
        "generatedAt": now.isoformat(),
        "queriesRun": selected,
        "requestBudget": len(selected) * len(plan["platforms"]),
        "queryReports": query_reports,
        "crawledItems": len(all_items),
        "capturedItems": captured_count,
        "capturedError": captured_error,
        "crawlArchive": str(crawl_path),
        "monitor": monitor_summary,
        "agentTrigger": agent_report,
        "dailyReport": str(_refresh_daily_report(tool.store, crawl_dir, now.date())),
    }


def _refresh_daily_report(store: MonitorStore, crawl_dir: str | Path, day: date) -> Path:
    from crawler_tool.monitoring.daily_report import generate_daily_report

    return generate_daily_report(store, out_dir=Path(crawl_dir).parent / "reports", day=day)


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl (rotating queries) then monitor, in one tick")
    parser.add_argument("--config-dir", default=None, help="监测配置目录（entities.json / base_rules.json）")
    parser.add_argument("--data-dir", default="data/monitoring", help="监测落盘目录")
    parser.add_argument("--crawl-dir", default="data/crawl", help="爬取响应存档目录")
    parser.add_argument("--base-url", default="http://127.0.0.1:8301", help="本机爬虫服务地址")
    parser.add_argument("--queries-file", default="config/monitoring/queries.json")
    parser.add_argument("--limit", type=int, default=10, help="每个查询取回条数上限")
    args = parser.parse_args()

    summary = run_scheduled_tick(
        config_dir=args.config_dir,
        data_dir=args.data_dir,
        crawl_dir=args.crawl_dir,
        base_url=args.base_url,
        queries_file=args.queries_file,
        search_limit=args.limit,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary.get("error"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
