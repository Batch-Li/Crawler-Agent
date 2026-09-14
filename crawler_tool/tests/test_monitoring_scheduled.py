"""一站式定时 tick 测试：查询轮换、响应存档、监测串联、服务不可达降级。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from crawler_tool.monitoring.scheduled_tick import (
    load_query_plan,
    rotate_queries,
    run_scheduled_tick,
)

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)


def write_queries_file(tmp_path: Path) -> Path:
    path = tmp_path / "queries.json"
    path.write_text(json.dumps({
        "queries": ["华为", "小米", "折叠屏 手机"],
        "queriesPerTick": 2,
        "platforms": ["south_weekend", "toutiao"],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def fake_search_factory():
    """按 query 返回固定响应的假爬虫服务；同时记录调用参数。"""
    calls: list[tuple[str, list[str], int]] = []

    def poster(query: str, platforms: list[str], limit: int) -> dict:
        calls.append((query, list(platforms), limit))
        title = f"{query}发布新一代旗舰手机"
        return {
            "status": "partial",
            "items": [
                {"contentId": f"c_{len(calls)}", "dedupKey": f"k_{len(calls)}",
                 "title": title, "platform": "toutiao", "publishedAt": None, "metrics": {}}
            ],
            "sourceReports": [{"platform": "toutiao", "status": "success"}],
        }

    return poster, calls


def test_rotation_covers_pool_without_repeats(tmp_path: Path):
    queries = ["a", "b", "c"]
    assert rotate_queries(queries, 2, 0) == ["a", "b"]
    assert rotate_queries(queries, 2, 1) == ["b", "c"]
    assert rotate_queries(queries, 2, 2) == ["c", "a"]  # 环绕
    assert rotate_queries(queries, 5, 0) == ["a", "b", "c"]  # per_tick 超过池子时不重复


def test_load_query_plan_defaults_and_validation(tmp_path: Path):
    path = write_queries_file(tmp_path)
    plan = load_query_plan(path)
    assert plan["queries"] == ["华为", "小米", "折叠屏 手机"]
    assert plan["queries_per_tick"] == 2
    assert plan["platforms"] == ["south_weekend", "toutiao"]

    empty = tmp_path / "empty.json"
    empty.write_text('{"queries": []}', encoding="utf-8")
    import pytest
    with pytest.raises(ValueError):
        load_query_plan(empty)


def test_scheduled_tick_archives_feeds_monitor_and_rotates(tmp_path: Path):
    queries_file = write_queries_file(tmp_path)
    poster, calls = fake_search_factory()

    summary = run_scheduled_tick(
        config_dir=None,
        data_dir=tmp_path / "mon",
        crawl_dir=tmp_path / "crawl",
        queries_file=queries_file,
        poster=poster,
        now=NOW,
    )
    # 第一轮：tickCount=0 → 池子前两个查询，每个查 2 个平台
    assert summary["queriesRun"] == ["华为", "小米"]
    assert summary["requestBudget"] == 4
    assert [(q, p, l) for q, p, l in calls][:2] == [
        ("华为", ["south_weekend", "toutiao"], 10),
        ("小米", ["south_weekend", "toutiao"], 10),
    ]
    assert summary["crawledItems"] == 2
    assert "error" not in summary

    # 响应带 query 标记整行存档
    archive = Path(summary["crawlArchive"])
    lines = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2 and lines[0]["query"] == "华为"

    # 条目进入监测：标题含主体+动作词 → 快车道告警
    assert summary["monitor"]["alertsWritten"] == 2

    # 第二轮：tickCount 已被第一轮推进到 1 → 从池子第 2 个查询继续（仍无重复、继续覆盖）
    summary2 = run_scheduled_tick(
        config_dir=None,
        data_dir=tmp_path / "mon",
        crawl_dir=tmp_path / "crawl",
        queries_file=queries_file,
        poster=poster,
        now=NOW,
    )
    assert summary2["queriesRun"] == ["小米", "折叠屏 手机"]
    assert len(calls) == 4


def test_service_unreachable_returns_hint(tmp_path: Path, monkeypatch):
    # 不依赖真实端口行为：本机代理可能拦截任意 loopback 端口（502 不抛异常），
    # 确定性注入连接失败，保证走"服务不可达"分支。
    import crawler_tool.monitoring.scheduled_tick as scheduled_tick

    def refused(*args, **kwargs):
        raise ConnectionError("connection refused by test")

    monkeypatch.setattr(scheduled_tick.httpx, "get", refused)
    summary = run_scheduled_tick(
        config_dir=None,
        data_dir=tmp_path / "mon",
        crawl_dir=tmp_path / "crawl",
        base_url="http://127.0.0.1:9",
        queries_file=write_queries_file(tmp_path),
        now=NOW,
    )
    assert "error" in summary
    assert "ConnectionError" in summary["error"]
    assert "--serve" in summary["hint"]


def test_query_failure_degrades_without_killing_tick(tmp_path: Path):
    queries_file = write_queries_file(tmp_path)

    def flaky(query: str, platforms: list[str], limit: int) -> dict:
        if query == "华为":
            raise RuntimeError("network glitch")
        return {"items": [], "sourceReports": []}

    summary = run_scheduled_tick(
        config_dir=None,
        data_dir=tmp_path / "mon",
        crawl_dir=tmp_path / "crawl",
        queries_file=queries_file,
        poster=flaky,
        now=NOW,
    )
    assert "error" not in summary
    failed = [r for r in summary["queryReports"] if "error" in r]
    assert len(failed) == 1 and "华为" in failed[0]["query"]


def test_captured_items_flow_into_monitor(tmp_path: Path):
    """includeCaptured 开启时，会话视图的捕获条目进入监测，且与本轮爬取结果按 dedupKey 去重。"""
    path = tmp_path / "queries.json"
    path.write_text(json.dumps({
        "queries": ["华为"],
        "queriesPerTick": 1,
        "platforms": ["south_weekend"],
        "includeCaptured": True,
    }, ensure_ascii=False), encoding="utf-8")

    crawled = {"contentId": "crawl_1", "dedupKey": "dup_k", "title": "小信智能发布新一代折叠屏手机",
               "platform": "south_weekend", "publishedAt": None, "metrics": {}}

    def poster(query: str, platforms: list[str], limit: int) -> dict:
        return {"items": [crawled], "sourceReports": [{"platform": "south_weekend", "status": "success"}]}

    def captured_fetcher(limit: int) -> dict:
        assert limit == 100
        return {"items": [
            # 会话视图会包含本轮刚爬过的条目（嵌套 dedup 形态）——必须被排除
            {"contentId": "dup_view", "dedup": {"dedupKey": "dup_k"}, "title": crawled["title"],
             "platform": "south_weekend", "publishedAt": None, "metrics": {}},
            # 真正的新捕获（抖音信封）
            {"contentId": "cap_1", "dedupKey": "cap_k1", "title": "小信智能新品正式开售",
             "platform": "douyin", "publishedAt": None, "metrics": {}},
        ]}

    summary = run_scheduled_tick(
        config_dir=None,
        data_dir=tmp_path / "mon",
        crawl_dir=tmp_path / "crawl",
        queries_file=path,
        poster=poster,
        captured_fetcher=captured_fetcher,
        now=NOW,
    )
    assert summary["capturedItems"] == 1          # 视图里的重复条目被排除
    assert summary["crawledItems"] == 2           # 爬取 1 + 新捕获 1
    assert summary["monitor"]["processed"] == 2
    assert summary["monitor"]["alertsWritten"] == 2  # 两条都走 self_launch 漏斗


def test_captured_fetch_failure_does_not_kill_tick(tmp_path: Path):
    path = tmp_path / "queries.json"
    path.write_text(json.dumps({
        "queries": ["华为"], "queriesPerTick": 1,
        "platforms": ["south_weekend"], "includeCaptured": True,
    }, ensure_ascii=False), encoding="utf-8")

    def bad_fetcher(limit: int) -> dict:
        raise RuntimeError("session view down")

    summary = run_scheduled_tick(
        config_dir=None,
        data_dir=tmp_path / "mon",
        crawl_dir=tmp_path / "crawl",
        queries_file=path,
        poster=lambda q, p, l: {"items": [], "sourceReports": []},
        captured_fetcher=bad_fetcher,
        now=NOW,
    )
    assert summary["capturedItems"] == 0
    assert "RuntimeError" in summary["capturedError"]
    assert "error" not in summary


def test_daily_report_generated_each_tick(tmp_path: Path):
    queries_file = write_queries_file(tmp_path)
    summary = run_scheduled_tick(
        config_dir=None,
        data_dir=tmp_path / "mon",
        crawl_dir=tmp_path / "crawl",
        queries_file=queries_file,
        poster=lambda q, p, l: {"items": [], "sourceReports": []},
        now=NOW,
    )
    report = Path(summary["dailyReport"])
    assert report.exists()
    content = report.read_text(encoding="utf-8")
    assert "监测日报 2026-09-07" in content
    # 幂等：同一天重跑刷新同一份文件
    summary2 = run_scheduled_tick(
        config_dir=None,
        data_dir=tmp_path / "mon",
        crawl_dir=tmp_path / "crawl",
        queries_file=queries_file,
        poster=lambda q, p, l: {"items": [], "sourceReports": []},
        now=NOW,
    )
    assert Path(summary2["dailyReport"]) == report
