"""Agent 适配桥测试：Evidence/seedEvent 转换、CollectionTool、门面与配置加载。"""

from __future__ import annotations

import json
from pathlib import Path

from crawler_tool.monitoring.agent_bridge import (
    MonitoringTool,
    SearchContentCollectionTool,
    alert_to_seed_event,
    item_to_evidence,
)
from crawler_tool.monitoring.config_loader import DEFAULT_CONFIG_DIR, load_monitor_config
from crawler_tool.monitoring.store import MonitorStore


# ---- 契约转换 ----

def test_item_to_evidence_maps_workflow_contract():
    evidence = item_to_evidence({
        "contentId": "c1", "title": "标题", "platform": "toutiao",
        "url": "https://example.com/1", "publishedAt": "2026-09-06T00:00:00+00:00",
        "summary": "摘要", "dedupKey": "k1",
    })
    assert evidence["evidenceId"] == "c1"
    assert evidence["source"] == "toutiao"
    assert evidence["snippet"] == "摘要"
    assert evidence["dedupKey"] == "k1"


def test_alert_to_seed_event_shape():
    alert = {
        "alertId": "alr_20260906_001",
        "why": "竞品发布新品",
        "eventTypes": ["PRODUCT_LAUNCH"],
        "subjectName": "智言科技",
        "priority": "red",
        "score": 3.3,
        "matchedRule": "competitor_launch",
        "resonance": {"platforms": ["toutiao", "cctv_news"]},
        "evidence": [
            {"contentId": "c1", "title": "智言科技发布新品", "platform": "toutiao",
             "url": "https://example.com/1", "publishedAt": None, "dedupKey": "k1"}
        ],
    }
    seed = alert_to_seed_event(alert)
    assert seed["eventId"] == "alr_20260906_001"
    assert seed["title"] == "智言科技发布新品"
    assert seed["eventTypes"] == ["PRODUCT_LAUNCH"]
    assert seed["entities"] == ["智言科技"]
    assert seed["evidence"][0]["evidenceId"] == "c1"
    assert seed["alertMeta"]["priority"] == "red"


# ---- CollectionTool ----

def test_alert_seed_deduplicates_ids_and_preserves_available_text():
    seed = alert_to_seed_event({
        "alertId": "a1", "why": "发布新品", "evidence": [
            {"contentId": "c1", "title": "新品", "platform": "toutiao", "summary": "摘要"},
            {"contentId": "c1", "title": "新品", "platform": "toutiao", "summary": None, "content": "正文"},
        ],
    })
    assert len(seed["evidence"]) == 1
    assert seed["evidence"][0]["snippet"] == "摘要"
    assert seed["evidence"][0]["content"] == "正文"

def test_collection_tool_with_injected_search_fn():
    captured = {}

    def fake_search(query: str, limit: int):
        captured["query"] = query
        captured["limit"] = limit
        return {
            "items": [
                {"contentId": "c1", "title": "结果一", "platform": "toutiao",
                 "url": "https://example.com/1", "summary": "摘要一", "dedupKey": "k1"},
                {"broken": True},
            ]
        }

    tool = SearchContentCollectionTool(search_fn=fake_search)
    evidence = tool.search({"query": "智言科技 AI客服 价格", "limit": 5})
    assert captured == {"query": "智言科技 AI客服 价格", "limit": 5}
    assert len(evidence) == 1
    assert evidence[0]["evidenceId"] == "c1"


# ---- 配置加载 ----

def test_packaged_defaults_load_with_example_entities():
    config = load_monitor_config()
    assert config.settings.gray_mode is True
    assert any(entity.entity_id == "ent_zhiyan" for entity in config.entities)
    assert config.family_by_id("competitor_launch") is not None
    assert (DEFAULT_CONFIG_DIR / "base_rules.json").exists()


def test_overlay_config_overrides_settings_and_entities(tmp_path: Path):
    override_dir = tmp_path / "monitoring"
    override_dir.mkdir()
    (override_dir / "base_rules.json").write_text(
        json.dumps({"settings": {"grayMode": False, "redThreshold": 2.5}}), encoding="utf-8"
    )
    (override_dir / "entities.json").write_text(
        json.dumps({"entities": [
            {"entityId": "ent_x", "canonicalName": "某公司", "type": "competitor"}
        ]}), encoding="utf-8"
    )
    config = load_monitor_config(override_dir)
    assert config.settings.gray_mode is False
    assert config.settings.red_threshold == 2.5
    assert [entity.entity_id for entity in config.entities] == ["ent_x"]
    # families 仍来自包内默认
    assert config.family_by_id("competitor_launch") is not None


def test_invalid_override_rejected(tmp_path: Path):
    override_dir = tmp_path / "monitoring"
    override_dir.mkdir()
    (override_dir / "base_rules.json").write_text(
        json.dumps({"settings": {"redThreshold": "很高"}}), encoding="utf-8"
    )
    import pydantic
    try:
        load_monitor_config(override_dir)
        raise AssertionError("应当拒绝非法阈值")
    except pydantic.ValidationError:
        pass


# ---- MonitoringTool 门面端到端 ----

def test_ingest_accepts_real_contentitem_shape(tmp_path: Path):
    """回归：真实 search-content 响应的 dedupKey 在嵌套 dedup 对象里，metrics 可能含 null。"""
    tool = MonitoringTool(data_dir=tmp_path / "mon")
    real_shape = {
        "schemaVersion": "content.v1", "contentId": "cid_1",
        "title": "华为发布新款折叠屏手机", "summary": "摘要", "content": None,
        "url": "https://example.com/1", "canonicalUrl": "https://example.com/1",
        "platform": "toutiao", "sourceType": "news_media", "contentType": "article",
        "sourceItemId": "123456", "publishedAt": None, "author": None,
        "metrics": {"commentCount": None, "likeCount": 5},
        "media": [], "language": "zh", "collectedAt": "2026-09-07T11:41:01+00:00",
        "rawDataRef": "raw/toutiao/123456.html",
        "dedup": {"dedupKey": "sha256:abc123", "contentHash": "sha256:abc123"},
        "quality": {"warnings": []}, "collection": {"method": "http", "adapterVersion": "0.1.0"},
        "ext": {},
    }
    summary = tool.ingest([real_shape, {"no_dedup_at_all": True}, real_shape.copy()])
    assert summary["processed"] == 2          # 真实形态 + 其复制（同 dedupKey）
    assert summary["skippedInvalid"] == 1     # 缺 dedupKey 的条目留痕不静默

    # 证据转换同样认嵌套 dedupKey
    evidence = item_to_evidence(real_shape)
    assert evidence["evidenceId"] == "cid_1"
    assert evidence["dedupKey"] == "sha256:abc123"


def test_item_to_evidence_passes_time_confidence_and_raw():
    """时间补全链路：置信度与原始串必须透传（agent 侧 Evidence 契约测试见 signalx-agent）。"""
    item = {
        "contentId": "cid_9", "title": "标题", "platform": "toutiao",
        "url": "https://example.com/9", "publishedAt": "2026-08-23T02:20:00+00:00",
        "summary": "摘要", "dedupKey": "k9",
        "quality": {"warnings": ["published_at_time_missing"], "publishedAtConfidence": 0.85},
        "ext": {"publishedAtRaw": "2026-08-23", "sourceName": "科技媒体"},
    }
    evidence = item_to_evidence(item)
    assert evidence["publishedAt"] == "2026-08-23T02:20:00+00:00"
    assert evidence["publishedAtConfidence"] == 0.85
    assert evidence["publishedAtRaw"] == "2026-08-23"


def test_monitoring_tool_end_to_end(tmp_path: Path):
    tool = MonitoringTool(data_dir=tmp_path / "mon")
    summary = tool.ingest([
        {"contentId": "c1", "dedupKey": "k1", "title": "智言科技发布新一代AI客服",
         "platform": "toutiao", "publishedAt": "2026-09-06T10:00:00+00:00", "metrics": {}},
        {"broken": "entry"},
    ])
    assert summary["processed"] == 1
    assert summary["alertsWritten"] >= 0

    pending = tool.pending_alerts()
    assert isinstance(pending, list)

    # 状态落盘：第二次 ingest 不应因状态文件缺失而崩
    summary2 = tool.ingest([
        {"contentId": "c1", "dedupKey": "k1", "title": "智言科技发布新一代AI客服",
         "platform": "toutiao", "publishedAt": "2026-09-06T10:00:00+00:00", "metrics": {}},
    ])
    assert summary2["processed"] == 1

    if pending:
        alert_id = pending[0]["alertId"]
        assert tool.update_alert_status(alert_id, "accepted", "确认竞品发布") is True
        assert all(alert["alertId"] != alert_id for alert in tool.pending_alerts())


def test_store_round_trip_and_prune(tmp_path: Path):
    store = MonitorStore(tmp_path)
    store.append_alerts([{"alertId": "a1", "status": "pending"}])
    store.append_alerts([{"alertId": "a2", "status": "pending"}])
    assert len(store.load_alerts()) == 2

    store.append_observation([{"reason": "industry_channel_unresolved", "title": "t"}])
    assert store.load_observation()[0]["reason"] == "industry_channel_unresolved"

    state = {
        "items": {
            "fresh": {"lastSeenAt": "2026-09-06T00:00:00+00:00"},
            "stale": {"lastSeenAt": "2026-08-01T00:00:00+00:00"},
        },
        "groups": {"g1": {"lastSeenAt": "2026-08-01T00:00:00+00:00", "lastAlertAt": "2026-08-01T00:00:00+00:00"}},
        "heat": {"toutiao": [1.0, 2.0]},
    }
    from datetime import datetime, timezone

    pruned = store.prune_state(state, retention_days=14, now=datetime(2026, 9, 6, tzinfo=timezone.utc))
    assert "fresh" in pruned["items"] and "stale" not in pruned["items"]
    assert "g1" not in pruned["groups"]
    assert pruned["heat"] == {"toutiao": [1.0, 2.0]}
