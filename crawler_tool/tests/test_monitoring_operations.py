"""监测层增补测试：配置守卫、LLM client 凭据纪律、运行时故障降级。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crawler_tool.monitoring.config_loader import entities_source, load_monitor_config
from crawler_tool.monitoring.engine import MonitorEngine
from crawler_tool.monitoring.llm_clients import DeepSeekTriage
from crawler_tool.monitoring.store import MonitorStore

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


def test_missing_config_dir_raises_instead_of_silent_fallback(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_monitor_config(tmp_path / "not_exist")


def test_entities_source_reports_which_file_is_in_effect(tmp_path: Path):
    assert entities_source(None) == "packaged_example"
    assert entities_source(tmp_path) == "packaged_example"  # 目录存在但没有 entities.json
    override = tmp_path / "monitoring"
    override.mkdir()
    (override / "entities.json").write_text('{"entities": []}', encoding="utf-8")
    assert entities_source(override) == "config_dir"


def test_deepseek_triage_never_runs_without_key():
    client = DeepSeekTriage(api_key="")  # 环境未设置键
    assert client.available is True
    with pytest.raises(RuntimeError):
        client.complete("任何 prompt 都不允许在无键时发出网络请求")


class ExplodingTriage:
    """运行时才爆炸的 client：engine 必须按批降级而不是让 tick 崩掉。"""

    available = True

    def complete(self, prompt: str) -> str:
        raise RuntimeError("llm gateway down")


def test_runtime_triage_failure_degrades_without_crashing(tmp_path: Path):
    config = load_monitor_config()
    config.settings.gray_mode = False
    engine = MonitorEngine(config, MonitorStore(tmp_path), triage_client=ExplodingTriage())
    summary = engine.run_tick([
        # 快车道条目：不依赖分诊，必须照常出告警
        {"contentId": "c1", "dedupKey": "k1", "title": "智言科技发布新一代AI客服",
         "platform": "toutiao", "publishedAt": (NOW - timedelta(hours=1)).isoformat(), "metrics": {}},
        # 慢车道条目：分诊失败 → 观察箱留档
        {"contentId": "c2", "dedupKey": "k2", "title": "智言科技把主力产品价格打到了9块9",
         "platform": "toutiao", "publishedAt": (NOW - timedelta(hours=1)).isoformat(), "metrics": {}},
    ], now=NOW)
    assert summary["degradedTriage"] is False  # client 在场，只是运行失败
    assert any(alert["matchedRule"] == "competitor_launch" for alert in summary["alerts"])
    assert summary["observationAdded"] >= 1  # 慢车道条目留档不丢
