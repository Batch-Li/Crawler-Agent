"""监测引擎离线测试：归一化、主体匹配、漏斗路由、压制、评分分级。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from crawler_tool.monitoring.config_loader import load_monitor_config
from crawler_tool.monitoring.engine import MonitorEngine
from crawler_tool.monitoring.matching import find_action_hit, match_subjects
from crawler_tool.monitoring.models import parse_utc
from crawler_tool.monitoring.normalize import normalize_text, split_clauses, title_fingerprint
from crawler_tool.monitoring.store import MonitorStore
from crawler_tool.monitoring.triage import TriageVerdict

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


def make_item(**overrides):
    base = {
        "contentId": "c1",
        "dedupKey": "k1",
        "title": "标题",
        "summary": None,
        "url": "https://example.com/1",
        "platform": "toutiao",
        "publishedAt": None,
        "publishedAtConfidence": None,
        "metrics": {},
    }
    base.update(overrides)
    return base


def make_engine(tmp_path: Path, triage_client=None, gray: bool = False):
    config = load_monitor_config()
    config.settings.gray_mode = gray
    store = MonitorStore(tmp_path)
    return MonitorEngine(config, store, triage_client=triage_client)


def hours_before(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


class RecordingTriage:
    available = True

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


# ---- 归一化与切分 ----

def test_normalize_width_case_and_clauses():
    assert normalize_text("ＡＩ客服 ＺＹ！") == "ai客服 zy!"
    assert split_clauses("智言科技发布新品。价格未披露，等待确认") == [
        "智言科技发布新品",
        "价格未披露",
        "等待确认",
    ]
    assert title_fingerprint("  AI 客服！发布 ").startswith("grp_")


# ---- 主体匹配 ----

def test_subject_match_canonical_and_blocklist_veto():
    config = load_monitor_config()
    entities = config.entities

    hits = match_subjects(split_clauses(normalize_text("智言科技发布新一代AI客服")), entities)
    assert hits[0].entity.entity_id == "ent_zhiyan"
    assert hits[0].surface_kind == "canonical"
    assert hits[0].strength == 1.0

    # "智言教育" 覆盖别名"智言" → 否决，不产生命中
    hits = match_subjects(split_clauses(normalize_text("智言教育推出新课程")), entities)
    assert all(hit.entity.entity_id != "ent_zhiyan" for hit in hits)


def test_product_surface_matches_with_lower_strength():
    config = load_monitor_config()
    hits = match_subjects(split_clauses(normalize_text("小智客服上线多模态能力")), config.entities)
    zhiyan = [hit for hit in hits if hit.entity.entity_id == "ent_zhiyan"]
    assert zhiyan and zhiyan[0].surface_kind == "product"


# ---- 宾语窗口 ----

def test_object_window_confirms_product_and_demotes_notice():
    config = load_monitor_config()
    family = config.family_by_id("competitor_launch")
    clauses = split_clauses(normalize_text("智言科技发布新一代AI客服"))
    subject = match_subjects(clauses, config.entities)[0]
    hit = find_action_hit(clauses, subject, family, config.settings.object_window_chars)
    assert hit is not None and hit.object_word == "客服" and not hit.object_unverified

    clauses = split_clauses(normalize_text("智言科技发布公告"))
    subject = match_subjects(clauses, config.entities)[0]
    assert find_action_hit(clauses, subject, family, config.settings.object_window_chars) is None


# ---- 引擎端到端 ----

def test_fast_lane_alert_basic(tmp_path):
    engine = make_engine(tmp_path)
    summary = engine.run_tick([
        make_item(
            contentId="c1", dedupKey="k1",
            title="智言科技发布新一代AI客服，支持多模态",
            publishedAt=hours_before(2),
        )
    ], now=NOW)
    assert summary["routes"].get("fast") == 1
    assert len(summary["alerts"]) == 1
    alert = summary["alerts"][0]
    assert alert["matchedRule"] == "competitor_launch"
    assert alert["subjectName"] == "智言科技"
    assert "智言科技" in alert["why"]


def test_resonance_merge_and_orange_grade(tmp_path):
    engine = make_engine(tmp_path)
    title = "智言科技发布新一代AI客服"
    summary = engine.run_tick([
        make_item(contentId="c1", dedupKey="k1", title=title, platform="toutiao", publishedAt=hours_before(2)),
        make_item(contentId="c2", dedupKey="k2", title=title, platform="cctv_news", publishedAt=hours_before(2)),
    ], now=NOW)
    assert len(summary["alerts"]) == 1
    alert = summary["alerts"][0]
    assert alert["resonance"]["sourceCount"] == 2
    assert len(alert["evidence"]) == 2
    assert alert["priority"] == "orange"
    # 锚定更可信来源（央视 1.2 > 头条 1.0）
    assert alert["evidence"][0]["platform"] == "cctv_news"


def test_gray_mode_forces_yellow(tmp_path):
    engine = make_engine(tmp_path, gray=True)  # 灰度协议：一切告警降为 yellow
    summary = engine.run_tick([
        make_item(title="小信智能App今晨大面积宕机", platform="weibo", publishedAt=hours_before(1))
    ], now=NOW)
    assert summary["alerts"][0]["priority"] == "yellow"


def test_self_negative_incident_scores_red_without_gray(tmp_path):
    engine = make_engine(tmp_path, gray=False)
    summary = engine.run_tick([
        make_item(title="小信智能App今晨大面积宕机", platform="weibo", publishedAt=hours_before(1))
    ], now=NOW)
    alert = summary["alerts"][0]
    assert alert["matchedRule"] == "public_opinion"
    assert alert["priority"] == "red"


def test_same_story_different_titles_merge_into_one_alert(tmp_path):
    """回归：_003/_005 同故事不同措辞的真实标题对，必须合并为一条告警。"""
    engine = make_engine(tmp_path, gray=False)
    title_a = "智言科技发布新一代折叠屏客服终端:三段式结构再进化，玄武架构加持IP58级防护"
    title_b = "智言科技正式发布折叠屏客服终端:展翼三段式，3.5mm机身容纳双屏"
    summary = engine.run_tick([
        make_item(contentId="c1", dedupKey="k1", title=title_a, platform="toutiao", publishedAt=hours_before(2)),
        make_item(contentId="c2", dedupKey="k2", title=title_b, platform="cctv_news", publishedAt=hours_before(2)),
    ], now=NOW)
    assert len(summary["alerts"]) == 1
    alert = summary["alerts"][0]
    assert len(alert["evidence"]) == 2
    assert alert["resonance"]["sourceCount"] == 2
    assert alert["resonance"]["resonanceBonus"] > 0


def test_publish_time_inherited_from_group_member(tmp_path):
    """回归：无发布时间的条目从同故事报道继承时间，衰减随之重算。"""
    engine = make_engine(tmp_path, gray=False)
    summary = engine.run_tick([
        make_item(contentId="c1", dedupKey="k1",
                  title="智言科技发布新一代折叠屏客服终端:三段式结构再进化",
                  platform="toutiao", publishedAt=hours_before(2), publishedAtConfidence=1.0),
        make_item(contentId="c2", dedupKey="k2",
                  title="智言科技正式发布折叠屏客服终端:展翼三段式，3.5mm机身容纳双屏",
                  platform="south_weekend", publishedAt=None),
    ], now=NOW)
    alert = summary["alerts"][0]
    # 锚定南周（可信度更高、timeFallback 衰减 1.0），继承头条的真实发布时间
    assert alert["flags"]["timeInherited"] is True
    assert alert["publishedAt"] == hours_before(2)
    assert alert["flags"]["timeFallback"] is True  # 原始状态仍留痕


def test_self_launch_family_fires_for_self_entity(tmp_path):
    """我方发布（self_launch）：self 主体+发布动作 → 出告警，且不误报为竞品发布。"""
    engine = make_engine(tmp_path, gray=False)
    summary = engine.run_tick([
        make_item(contentId="c1", dedupKey="k1",
                  title="小信智能发布新一代客服平台", platform="toutiao",
                  publishedAt=hours_before(2))
    ], now=NOW)
    alert = summary["alerts"][0]
    assert alert["matchedRule"] == "self_launch"
    assert alert["matchedRule"] != "competitor_launch"
    assert alert["eventTypes"] == ["PRODUCT_LAUNCH"]


def test_family_exclusion_routes_to_recruitment_not_launch(tmp_path):
    engine = make_engine(tmp_path, gray=False)
    summary = engine.run_tick([
        make_item(contentId="c1", dedupKey="k1",
                  title="快答云诚聘AI客服训练师", publishedAt=hours_before(1))
    ], now=NOW)
    assert summary["routes"].get("fast") == 1
    alert = summary["alerts"][0]
    # "诚聘"对 launch 族是排除词，但对 recruitment 族是主体动作——族级而非全局
    assert alert["matchedRule"] == "recruitment"


def test_stale_item_dropped(tmp_path):
    engine = make_engine(tmp_path)
    summary = engine.run_tick([
        make_item(title="智言科技发布新品", publishedAt=hours_before(10), publishedAtConfidence=1.0)
    ], now=NOW)
    assert summary["routes"].get("dropped_stale") == 1
    assert summary["alerts"] == []


def test_missing_publish_time_falls_back_to_first_seen(tmp_path):
    engine = make_engine(tmp_path, gray=False)
    summary = engine.run_tick([
        make_item(title="智言科技宣布完成B轮融资", platform="weibo")
    ], now=NOW)
    alert = summary["alerts"][0]
    assert alert["matchedRule"] == "financing"
    assert alert["flags"]["timeFallback"] is True


def test_dedup_key_cooldown_suppresses_second_tick(tmp_path):
    engine = make_engine(tmp_path, gray=False)
    item = make_item(title="智言科技发布新一代AI客服", publishedAt=hours_before(2))
    first = engine.run_tick([item], now=NOW)
    assert len(first["alerts"]) == 1

    second = engine.run_tick([dict(item)], now=NOW + timedelta(hours=1))
    assert second["suppressed"]["cooldown"] == 1
    assert second["alerts"] == []


def test_event_group_hard_window_and_progress_update(tmp_path):
    engine = make_engine(tmp_path, gray=False)
    title = "智言科技发布新一代AI客服"
    first = engine.run_tick([
        make_item(contentId="c1", dedupKey="k1", title=title, platform="toutiao",
                  publishedAt=hours_before(2), metrics={"commentCount": 100})
    ], now=NOW)
    assert len(first["alerts"]) == 1

    # 2 小时硬窗口内、热度无显著增长 → 压制
    second = engine.run_tick([
        make_item(contentId="c3", dedupKey="k3", title=title, platform="weibo",
                  publishedAt=hours_before(1), metrics={"commentCount": 105})
    ], now=NOW + timedelta(hours=1))
    assert second["suppressed"]["groupWindow"] == 1

    # 热度显著增长 → 进展更新放行
    third = engine.run_tick([
        make_item(contentId="c4", dedupKey="k4", title=title, platform="weibo",
                  publishedAt=hours_before(1), metrics={"commentCount": 500})
    ], now=NOW + timedelta(hours=3))
    assert third["alerts"], "显著增长应放行为进展更新"
    assert third["alerts"][0]["flags"].get("progressUpdate") is True


def test_high_risk_bypass_for_unidentified_subject(tmp_path):
    engine = make_engine(tmp_path)
    summary = engine.run_tick([
        make_item(contentId="c9", dedupKey="k9", title="某无名公司客服数据大规模泄露", platform="weibo")
    ], now=NOW)
    alert = summary["alerts"][0]
    assert alert["matchedRule"] == "high_risk_bypass"
    assert alert["priority"] == "yellow"
    assert alert["flags"]["unidentifiedSubject"] is True
    assert summary["observationAdded"] >= 1


def test_launch_event_noun_guard(tmp_path):
    """回归："小米发布会"里的"发布"是名词成分，不得触发发布告警。"""
    engine = make_engine(tmp_path, gray=False)
    summary = engine.run_tick([
        make_item(contentId="c1", dedupKey="k1",
                  title="陈年回应没去小米发布会", platform="weibo", publishedAt=hours_before(1))
    ], now=NOW)
    matching = [a for a in summary["alerts"] if a["matchedRule"] in ("competitor_launch", "self_launch")]
    assert matching == []


def test_promotion_exclusion_words(tmp_path):
    """回归："年中大促"类促销帖不得触发发布告警。"""
    engine = make_engine(tmp_path, gray=False)
    summary = engine.run_tick([
        make_item(contentId="c1", dedupKey="k1",
                  title="小米618年中大促发布新品优惠", platform="toutiao", publishedAt=hours_before(1))
    ], now=NOW)
    matching = [a for a in summary["alerts"] if a["matchedRule"] in ("competitor_launch", "self_launch")]
    assert matching == []


def test_no_match_item_dropped(tmp_path):
    engine = make_engine(tmp_path)
    summary = engine.run_tick([
        make_item(title="今天天气不错，适合出行")
    ], now=NOW)
    assert summary["routes"].get("dropped_noMatch") == 1
    assert summary["alerts"] == []


def test_triage_slow_lane_rescues_unusual_wording(tmp_path):
    response = (
        '{"results": [{"index": 0, "isEvent": true, "eventTypes": ["PRODUCT_CHANGE"],'
        ' "confidence": 0.8, "why": "竞品显著降价", "aliasHints": [], "newSubjectCandidates": []}]}'
    )
    client = RecordingTriage(response)
    engine = make_engine(tmp_path, triage_client=client, gray=False)
    summary = engine.run_tick([
        make_item(title="智言科技把主力产品价格打到了9块9", publishedAt=hours_before(1))
    ], now=NOW)
    assert summary["degradedTriage"] is False
    assert len(client.prompts) == 1
    assert "<<<CAPTURED_ITEMS" in client.prompts[0]
    alert = summary["alerts"][0]
    assert alert["matchedRule"] == "product_change"
    assert alert["flags"]["llmTriage"] is True
    assert "显著降价" in alert["why"]


def test_triage_disabled_degrades_to_observation(tmp_path):
    engine = make_engine(tmp_path)  # 无 triage client
    summary = engine.run_tick([
        make_item(title="智言科技把主力产品价格打到了9块9", publishedAt=hours_before(1))
    ], now=NOW)
    assert summary["degradedTriage"] is True
    assert summary["alerts"] == []
    assert summary["routes"].get("slow") == 1
    assert summary["observationAdded"] >= 1
    # 回归：观察箱必须落盘，而不是只有计数（曾出现过文件永远为空的 bug）
    store = MonitorStore(tmp_path)
    entries = store.load_observation()
    assert entries and entries[0]["reason"] == "triage_unavailable_or_non_event"


def test_industry_channel_routes_unmatched_subject_to_observation(tmp_path):
    engine = make_engine(tmp_path)  # 分诊不可用 → 观察箱
    summary = engine.run_tick([
        make_item(contentId="c8", dedupKey="k8", title="智能客服行业迎来新的挑战者", publishedAt=hours_before(1))
    ], now=NOW)
    assert summary["routes"].get("industry") == 1
    assert summary["observationAdded"] >= 1
    assert summary["alerts"] == []


def test_observation_dedup_across_ticks(tmp_path):
    """回归：同一条目同原因跨轮次只入观察箱一次。"""
    engine = make_engine(tmp_path)
    item = make_item(title="智言科技把主力产品价格打到了9块9", publishedAt=hours_before(1))
    first = engine.run_tick([item], now=NOW)
    assert first["observationAdded"] == 1
    second = engine.run_tick([dict(item)], now=NOW + timedelta(hours=1))
    assert second["observationAdded"] == 0
    store = MonitorStore(tmp_path)
    assert len(store.load_observation()) == 1
