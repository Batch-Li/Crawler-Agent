"""浏览器自动化捕获实验的纯逻辑：信封识别、即停判定、硬限、载荷构造。"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "browser_auto" / "xhs_browser_capture.py"
_spec = importlib.util.spec_from_file_location("xhs_browser_capture", SCRIPT)
xhs = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("xhs_browser_capture", xhs)
_spec.loader.exec_module(xhs)


def test_is_search_api_matches_content_endpoints_only():
    assert xhs.is_search_api("https://www.xiaohongshu.com/api/sns/web/v1/search/notes?query=大模型")
    assert xhs.is_search_api("https://so.xiaohongshu.com/api/sns/web/v2/search/notes?keyword=AI")
    assert xhs.is_search_api("https://www.xiaohongshu.com/api/sns/web/v1/homefeed?num=20")
    # 辅助接口（联想词/聚合卡片/筛选面板）与静态资源、详情接口不转发
    assert not xhs.is_search_api("https://edith.xiaohongshu.com/api/sns/web/v1/search/recommend?keyword=AI")
    assert not xhs.is_search_api("https://edith.xiaohongshu.com/api/sns/web/v1/search/onebox?keyword=AI")
    assert not xhs.is_search_api("https://edith.xiaohongshu.com/api/sns/web/v1/search/filter?keyword=AI")
    assert not xhs.is_search_api("https://www.xiaohongshu.com/api/sns/web/v1/feed/6651")
    assert not xhs.is_search_api("https://www.xiaohongshu.com/static/logo.png")
    assert not xhs.is_search_api("https://www.xiaohongshu.com/explore")


def test_build_search_url_encodes_keyword():
    url = xhs.build_search_url("大模型 发布")
    assert url.startswith("https://www.xiaohongshu.com/search_result?keyword=")
    assert " " not in url and "大模型" not in url
    from urllib.parse import unquote, urlparse, parse_qs
    assert parse_qs(urlparse(url).query)["keyword"] == ["大模型 发布"]


def test_build_forward_payload_requires_json_object():
    payload = xhs.build_forward_payload("大模型", '{"success": true, "data": {"items": []}}')
    assert payload == {"platform": "xiaohongshu", "keyword": "大模型",
                       "capture": {"success": True, "data": {"items": []}}}
    with pytest.raises(ValueError):
        xhs.build_forward_payload("大模型", "<html>登录页</html>")
    with pytest.raises(ValueError):
        xhs.build_forward_payload("大模型", "[1, 2, 3]")


def test_detect_block_signals():
    assert xhs.detect_block(461, "", "https://www.xiaohongshu.com/search_result") is not None
    assert xhs.detect_block(429, "", "https://www.xiaohongshu.com/search_result") is not None
    assert xhs.detect_block(403, "", "https://www.xiaohongshu.com/search_result") is not None
    assert xhs.detect_block(None, "", "https://www.xiaohongshu.com/login?redirect=1") is not None
    assert xhs.detect_block(200, "请完成验证码", "https://www.xiaohongshu.com/search_result") is not None
    assert xhs.detect_block(200, '{"code":461,"success":false}', "https://www.xiaohongshu.com/search_result") is not None
    # 正常数据响应与登录页 URL 之外的一切都不误判
    assert xhs.detect_block(200, '{"success": true, "data": {"items": []}}', "https://www.xiaohongshu.com/search_result") is None


def test_build_plan_hard_limits():
    queries, interval = xhs.build_plan(["大模型", "大模型", " AI发布 "], 120)
    assert queries == ["大模型", "AI发布"] and interval == 120
    with pytest.raises(ValueError, match="最多 5 个查询"):
        xhs.build_plan([f"词{i}" for i in range(6)], 120)
    with pytest.raises(ValueError, match="不得低于 60"):
        xhs.build_plan(["大模型"], 30)
    with pytest.raises(ValueError, match="没有可用查询"):
        xhs.build_plan(["  ", ""], 120)


def test_build_plan_daily_budget():
    queries, _ = xhs.build_plan(["大模型", "AI发布"], 120, done_today=8)
    assert len(queries) == 2
    with pytest.raises(ValueError, match="当日预算不足"):
        xhs.build_plan(["大模型", "AI发布"], 120, done_today=9)


def test_build_plan_keyword_daily_cap():
    counts = {"大模型": 2}
    with pytest.raises(ValueError, match="「大模型」今日已搜 2 次"):
        xhs.build_plan(["大模型"], 120, done_today=0, keyword_counts=counts)
    # 另一个词不受影响
    queries, _ = xhs.build_plan(["AI发布"], 120, done_today=0, keyword_counts=counts)
    assert queries == ["AI发布"]


def test_blocked_today_lock(tmp_path):
    assert xhs.blocked_today(tmp_path, "20260115") is False  # 无日志
    log = tmp_path / "runs-20260115.jsonl"
    log.write_text('{"query": "词", "status": "forwarded"}\n', encoding="utf-8")
    assert xhs.blocked_today(tmp_path, "20260115") is False
    with log.open("a", encoding="utf-8") as stream:
        stream.write('{"query": "词", "status": "blocked", "reason": "HTTP 461"}\n')
    assert xhs.blocked_today(tmp_path, "20260115") is True
    assert xhs.blocked_today(tmp_path, "20260116") is False  # 次日自动解锁


def test_extract_keywords_from_queue_entries():
    entries = [
        {"keyword": "AI大模型", "addedAt": "...", "source": "manual"},
        {"keyword": "  大模型 发布  ", "source": "agent"},
        {"keyword": "", "source": "agent"},
        "纯字符串词",
        None,
    ]
    assert xhs._extract_keywords(entries) == ["AI大模型", "大模型 发布", "纯字符串词"]
    assert xhs._extract_keywords(None) == []
