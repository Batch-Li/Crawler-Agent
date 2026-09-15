"""头条浏览器正文补全：详情信封解析、速度硬限、时间解析、载荷构造。"""

import importlib.util
import sys
from pathlib import Path

import pytest

from crawler_tool.domain import SourceStatus
from crawler_tool.sources import ToutiaoAdapter

SCRIPT = Path(__file__).parent.parent / "scripts" / "browser_auto" / "toutiao_browser_capture.py"
_spec = importlib.util.spec_from_file_location("toutiao_browser_capture", SCRIPT)
tbc = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("toutiao_browser_capture", tbc)
_spec.loader.exec_module(tbc)


def detail_envelope(**overrides):
    envelope = {
        "captureType": "toutiao_article_detail",
        "articleId": "7680172487316963866",
        "url": "https://www.toutiao.com/article/7680172487316963866/",
        "title": "我国化工行业首个大模型发布新版本",
        "content": "记者从中科学院获悉，智能化工大模型3.0 Pro正式发布，" * 5,
        "publishTime": "2026-08-31T01:30:00+00:00",
        "author": "环球网",
    }
    envelope.update(overrides)
    return envelope


class TestToutiaoDetailEnvelope:
    def test_valid_detail_envelope_yields_full_content_item(self):
        result = ToutiaoAdapter().parse_payload(detail_envelope())
        assert result.status is SourceStatus.SUCCESS
        assert len(result.items) == 1
        payload = result.items[0].payload
        assert payload["sourceItemId"] == "7680172487316963866"
        assert payload["method"] == "browser"
        assert payload["hasFullContent"] is True
        assert "智能化工大模型" in payload["content"]
        assert payload["publishedAt"].startswith("2026-08-31T01:30")
        assert payload["publishedAtConfidence"] == 0.9
        assert "content_from_browser_auto" in payload["warnings"]

    def test_article_id_falls_back_to_url_extraction(self):
        envelope = detail_envelope(articleId="", url="https://www.toutiao.com/a7681110928078766644/?channel=")
        result = ToutiaoAdapter().parse_payload(envelope)
        assert result.items[0].payload["sourceItemId"] == "7681110928078766644"

    def test_unrecognized_envelope_is_parser_changed(self):
        result = ToutiaoAdapter().parse_payload({"data": {"items": []}})
        assert result.status is SourceStatus.PARSER_CHANGED
        assert ToutiaoAdapter().parse_payload("not-a-dict").status is SourceStatus.PARSER_CHANGED

    def test_missing_fields_are_parser_changed(self):
        assert ToutiaoAdapter().parse_payload(detail_envelope(content="  ")).status is SourceStatus.PARSER_CHANGED
        assert ToutiaoAdapter().parse_payload(detail_envelope(articleId="", url="https://x.com/none")).status is SourceStatus.PARSER_CHANGED

    def test_short_body_is_empty_not_parser_changed(self):
        result = ToutiaoAdapter().parse_payload(detail_envelope(content="太短"))
        assert result.status is SourceStatus.EMPTY

    def test_invalid_publish_time_is_dropped_not_fatal(self):
        result = ToutiaoAdapter().parse_payload(detail_envelope(publishTime="发布于刚刚"))
        assert result.items[0].payload["publishedAt"] is None


class TestToutiaoScriptPureLogic:
    URL = "https://www.toutiao.com/a7680172487316963866/?channel="

    def test_article_url_and_id(self):
        assert tbc.is_article_url(self.URL)
        assert tbc.is_article_url("https://www.toutiao.com/article/7680172487316963866/")
        assert not tbc.is_article_url("https://www.toutiao.com/tech/")
        assert tbc.article_id_of(self.URL) == "7680172487316963866"

    def test_parse_time_text_cst_to_utc(self):
        assert tbc.parse_time_text("2026年9月12日 08:30") == "2026-09-12T00:30:00+00:00"
        assert tbc.parse_time_text("2026-09-12 08:30") == "2026-09-12T00:30:00+00:00"
        assert tbc.parse_time_text("刚刚") is None
        assert tbc.parse_time_text(None) is None

    def test_build_plan_hard_limits(self):
        articles = [("1", "https://www.toutiao.com/a1/"), ("2", "https://www.toutiao.com/a2/")]
        plan, interval = tbc.build_plan(articles + articles, 120, done_today=0)
        assert len(plan) == 2 and interval == 120
        with pytest.raises(ValueError, match="最多 5 篇"):
            tbc.build_plan([(str(i), f"https://www.toutiao.com/a{i}00{i}/") for i in range(6)], 120, 0)
        with pytest.raises(ValueError, match="不得低于 60"):
            tbc.build_plan(articles, 30, 0)
        with pytest.raises(ValueError, match="没有待补"):
            tbc.build_plan([], 120, 0)

    def test_build_plan_trims_to_daily_budget(self):
        """预算只剩 1 篇时裁剪到 1 篇执行（做不完的留明天），不再整体拒绝。"""
        plan, _ = tbc.build_plan(articles := [("1", "https://www.toutiao.com/a1/"),
                                              ("2", "https://www.toutiao.com/a2/")], 120, done_today=19)
        assert len(plan) == 1
        with pytest.raises(ValueError, match="当日预算已用完"):
            tbc.build_plan(articles, 120, done_today=20)

    def test_build_forward_payload(self):
        extracted = {"title": "标题", "content": "正文内容足够长，包含充分的事实细节与背景。" * 3,
                     "timeText": "2026-09-12 08:30", "author": "环球网"}
        payload = tbc.build_forward_payload(self.URL, extracted)
        assert payload["platform"] == "toutiao"
        capture = payload["capture"]
        assert capture["captureType"] == "toutiao_article_detail"
        assert capture["articleId"] == "7680172487316963866"
        assert capture["publishTime"] == "2026-09-12T00:30:00+00:00"
        with pytest.raises(ValueError, match="不足 40"):
            tbc.build_forward_payload(self.URL, {"title": "标题", "content": "太短", "timeText": None})
        with pytest.raises(ValueError, match="无法提取"):
            tbc.build_forward_payload("https://www.toutiao.com/tech/", extracted)

    def test_needs_content_uses_quality_marker(self):
        """头条搜索条目的 content 是摘要（非空），必须看 quality.hasFullContent。"""
        snippet = {"content": "85 字摘要非空", "quality": {"hasFullContent": False}}
        full = {"content": "全文", "quality": {"hasFullContent": True}}
        no_marker = {"content": None, "quality": {}}
        assert tbc.needs_content(snippet) is True
        assert tbc.needs_content(full) is False
        assert tbc.needs_content(no_marker) is True

    def test_blocked_today_lock(self, tmp_path):
        assert tbc.blocked_today(tmp_path, "20260115") is False
        log = tmp_path / "toutiao-runs-20260115.jsonl"
        log.write_text('{"articleId": "1", "status": "forwarded"}\n', encoding="utf-8")
        assert tbc.blocked_today(tmp_path, "20260115") is False
        with log.open("a", encoding="utf-8") as stream:
            stream.write('{"articleId": "1", "status": "blocked", "reason": "challenge_wall"}\n')
        assert tbc.blocked_today(tmp_path, "20260115") is True
        assert tbc.blocked_today(tmp_path, "20260116") is False
