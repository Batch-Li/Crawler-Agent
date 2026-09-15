"""小红书浏览器自动化捕获实验（实验性质，与油猴通道并存的双通道之二）。

数据流与油猴助手完全同构，下游管线零改动：

    Playwright 有头浏览器（专用 profile，人工首次登录）
      → 逐查询打开搜索结果页（页面自己发 XHR）
      → 拦截 search 响应信封 → POST /api/v1/tool/ingest-capture
      → recent_store / dedup / agent 消费（与捕获投喂同路）

实验边界（红线，代码强制）：
- 有头浏览器 + 专用 profile，绝不 headless、绝无任何反检测/指纹伪装；
- 无重试：转发失败只记录；命中 461/验证码/登录墙/频控 → 即停退出，不对抗；
- 低量硬限：单次运行最多 MAX_QUERIES_PER_RUN 个查询、间隔不低于
  MIN_INTERVAL_SECONDS 秒——量级对齐"人手动会做的事"；
- 一次只开一个页面，顺序执行，无并发。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

# 只转发内容信封：主搜索 notes + 推荐流 homefeed。search/filter、search/onebox、
# search/recommend（筛选面板/聚合卡片/联想词）不含笔记内容，转发只会制造解析失败噪声。
# （油猴助手沿用更宽的历史规则，辅助接口在 ingest 侧被拒绝，属已知噪声。）
CONTENT_API_SEGMENTS = ("/search/notes", "/homefeed")
SEARCH_PAGE_URL = "https://www.xiaohongshu.com/search_result?keyword={}"
LOGIN_MARKERS = ("/login", "login?")
BLOCK_BODY_MARKERS = ("captcha", "验证码", "challenge", "security-check", '"code":461')
DEFAULT_BASE_URL = "http://127.0.0.1:8301"
MAX_QUERIES_PER_RUN = 5
MIN_INTERVAL_SECONDS = 60
DAILY_QUERY_MAX = 10
KEYWORD_DAILY_MAX = 2
SETTLE_SECONDS = 3
FORWARD_TIMEOUT_SECONDS = 10


def is_search_api(url: str) -> bool:
    """是否为该转发的搜索响应：/api/sns/web/vN/ 且为 search/notes 或 homefeed。"""
    if "/api/sns/web/v" not in url:
        return False
    path = url.split("?", 1)[0]
    return any(segment in path for segment in CONTENT_API_SEGMENTS)


def build_search_url(keyword: str) -> str:
    return SEARCH_PAGE_URL.format(quote(keyword, safe=""))


def build_forward_payload(keyword: str, capture_text: str) -> dict:
    """与油猴 forward() 相同的请求体；capture 必须是合法 JSON 信封。"""
    try:
        capture = json.loads(capture_text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"响应不是合法 JSON 信封：{exc}") from exc
    if not isinstance(capture, dict):
        raise ValueError("响应 JSON 不是对象，疑似非数据响应")
    return {"platform": "xiaohongshu", "keyword": keyword, "capture": capture}


def detect_block(status_code: int | None, body_text: str, page_url: str) -> str | None:
    """命中风控/验证码/登录墙即返回原因（即停信号）；正常返回 None。

    刻意保守：只认明确的对抗信号，误报的代价只是提前停止（安全侧）。
    """
    if status_code in (461, 429):
        return f"HTTP {status_code}（频控/风控）"
    if status_code in (401, 403):
        return f"HTTP {status_code}（登录/鉴权墙）"
    if any(marker in page_url for marker in LOGIN_MARKERS):
        return "页面跳转到登录墙"
    lowered = body_text[:4000].lower()
    if any(marker.lower() in lowered for marker in BLOCK_BODY_MARKERS):
        return "响应包含验证码/风控标记"
    return None


def build_plan(
    queries: list[str], interval_seconds: int, done_today: int = 0,
    keyword_counts: dict[str, int] | None = None,
) -> tuple[list[str], int]:
    """运行计划：去重、硬限（数量/间隔/当日总量/同词频次）；超限直接拒绝而非悄悄截断。"""
    deduped = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
    if not deduped:
        raise ValueError("没有可用查询词")
    if len(deduped) > MAX_QUERIES_PER_RUN:
        raise ValueError(f"单次运行最多 {MAX_QUERIES_PER_RUN} 个查询（实验协议硬限），收到 {len(deduped)} 个")
    if interval_seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(f"查询间隔不得低于 {MIN_INTERVAL_SECONDS}s（实验协议硬限），收到 {interval_seconds}s")
    if done_today + len(deduped) > DAILY_QUERY_MAX:
        raise ValueError(f"当日预算不足：已跑 {done_today}/{DAILY_QUERY_MAX} 个查询，本次还要 {len(deduped)} 个")
    counts = keyword_counts or {}
    for keyword in deduped:
        already = counts.get(keyword, 0)
        if already >= KEYWORD_DAILY_MAX:
            raise ValueError(
                f"关键词「{keyword}」今日已搜 {already} 次（同词每日上限 {KEYWORD_DAILY_MAX}），明天再搜"
            )
    return deduped, interval_seconds


def consume_keyword(base_url: str, keyword: str) -> bool:
    """成功处理后把关键词移出捕获队列（失败/即停时保留，供下次重试）。"""
    try:
        response = httpx.delete(
            f"{base_url}/api/v1/tool/capture-queue",
            params={"keyword": keyword}, timeout=FORWARD_TIMEOUT_SECONDS,
        )
        return response.json().get("removed") is True
    except (httpx.HTTPError, ValueError):
        return False


def daily_done(log_dir: Path, today: str) -> int:
    """当日已真实打开过的查询数（dry-run 也计：平台侧流量已发生）。"""
    return sum(keyword_daily_counts(log_dir, today).values())


def blocked_today(log_dir: Path, today: str) -> bool:
    """即停当日自锁：当日日志出现过 blocked，则今天不再运行（无人值守安全前提）。"""
    path = log_dir / f"runs-{today}.jsonl"
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("status") == "blocked":
                return True
        except json.JSONDecodeError:
            continue
    return False


def keyword_daily_counts(log_dir: Path, today: str) -> dict[str, int]:
    """当日各关键词的真实打开次数（含 dry-run）。"""
    path = log_dir / f"runs-{today}.jsonl"
    if not path.exists():
        return {}
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") == "forwarded" and record.get("query"):
            counts[record["query"]] = counts.get(record["query"], 0) + 1
    return counts


def _extract_keywords(entries) -> list[str]:
    """队列条目（字典或字符串）→ 纯关键词列表。"""
    keywords: list[str] = []
    for item in entries or []:
        keyword = str(item.get("keyword") if isinstance(item, dict) else item or "").strip()
        if keyword:
            keywords.append(keyword)
    return keywords


def queue_keywords(base_url: str) -> list[str]:
    """拉取捕获队列待处理关键词（与 --push-capture-queue 闭环对接）。"""
    response = httpx.get(f"{base_url}/api/v1/tool/capture-queue", timeout=FORWARD_TIMEOUT_SECONDS)
    response.raise_for_status()
    return _extract_keywords(response.json().get("keywords"))


def forward_capture(base_url: str, payload: dict, *, dry_run: bool) -> dict:
    """单次转发，无重试；dry_run 只回显计划转发的载荷。"""
    if dry_run:
        return {"dryRun": True, "keyword": payload["keyword"],
                "envelopeKeys": sorted(payload["capture"].keys())}
    response = httpx.post(
        f"{base_url}/api/v1/tool/ingest-capture", json=payload, timeout=FORWARD_TIMEOUT_SECONDS,
    )
    return response.json()


def logged_in(context) -> bool:
    """以 web_session cookie 是否存在判断登录态（登录浮层不改 URL，URL 判定不可靠）。"""
    cookies = context.cookies("https://www.xiaohongshu.com")
    return any(c.get("name") == "web_session" and c.get("value") for c in cookies)


def _dump_envelope(log_dir: Path, keyword: str, url: str, status: int, text: str) -> Path:
    """失败信封落盘（诊断解析器/登录态用）；注意：内含会话数据，不入 git。"""
    import hashlib

    dump_dir = log_dir / "envelopes"
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    slug = "".join(ch if ch.isalnum() else "_" for ch in keyword)[:20]
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    path = dump_dir / f"{stamp}-{slug}-{status}-{digest}.json"
    header = json.dumps({"keyword": keyword, "url": url, "httpStatus": status}, ensure_ascii=False)
    path.write_text(header + "\n" + text, encoding="utf-8")
    return path


def _append_log(log_dir: Path, record: dict) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": datetime.now(timezone.utc).isoformat(), **record}, ensure_ascii=False)
    with (log_dir / f"runs-{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def run(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        print("缺少 playwright：先在虚拟环境执行 pip install playwright && playwright install chromium", file=sys.stderr)
        return 3
    queries: list[str] = []
    interval = args.interval_seconds
    log_dir = Path(args.data_dir) / "browser_auto"
    today = datetime.now().strftime("%Y%m%d")
    if blocked_today(log_dir, today):
        print("今日已命中即停条件（当日日志有 blocked 记录），按纪律今天不再运行。")
        return 2
    keyword_counts = keyword_daily_counts(log_dir, today)
    done_today = sum(keyword_counts.values())
    if not args.login:
        try:
            queries, interval = build_plan(args.queries, args.interval_seconds, done_today, keyword_counts)
        except ValueError as exc:
            print(f"运行计划被拒绝：{exc}", file=sys.stderr)
            return 3

    print(f"实验计划：{len(queries)} 个查询，间隔 {interval}s，今日已跑 {done_today}/{DAILY_QUERY_MAX}，dry_run={args.dry_run}")
    blocked_reason: str | None = None
    with sync_playwright() as p:
        launch_kwargs: dict = {"headless": False, "viewport": {"width": 1440, "height": 900}}
        if args.proxy:
            launch_kwargs["proxy"] = {"server": args.proxy}
        else:
            # 本机常驻系统代理（见交接文档 5.4）：国内站点被路由到远端节点会导致
            # 加载挂起。默认绕过系统代理直连；确需代理用 --proxy 显式给出。
            launch_kwargs["args"] = ["--no-proxy-server"]
        context = p.chromium.launch_persistent_context(args.profile_dir, **launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        if args.login:
            page.goto("https://www.xiaohongshu.com/")
            input("请在浏览器中用【实验小号】完成登录，然后回到终端按回车…")
            context.close()
            print("登录态已保存在专用 profile，后续运行无需再登录。")
            return 0

        buffer: list = []
        diagnostics: dict[str, int] = {}

        def _observe(response) -> None:
            url = response.url
            if is_search_api(url):
                buffer.append(response)
            elif "xiaohongshu" in url and "/api/" in url:
                diagnostics[url.split("?", 1)[0]] = response.status

        page.on("response", _observe)
        if queries:
            # 先开首页建立站点状态（更像人：先有会话再搜索），也避开冷 profile
            # 直进搜索深链的边缘情况。只需要网络响应，不解析 DOM。
            try:
                page.goto("https://www.xiaohongshu.com/", wait_until="commit", timeout=30_000)
                page.wait_for_timeout(2_000)
            except PlaywrightTimeout:
                print("  首页打开超时（30s），继续尝试搜索页。")
        for index, keyword in enumerate(queries):
            buffer.clear()
            diagnostics.clear()
            forwarded_any = False
            print(f"[{index + 1}/{len(queries)}] {keyword}")
            try:
                # wait_until=commit：本脚本只拦截网络响应、不解析 DOM，
                # 页面开始返回即算导航完成；渲染卡顿不再拖垮运行。
                page.goto(build_search_url(keyword), wait_until="commit", timeout=30_000)
            except PlaywrightTimeout:
                # 无重试纪律：导航超时记录后跳过该查询，继续后面的（互相独立）。
                print("  导航超时（30s）：网络/代理问题？默认已绕过系统代理直连。")
                _append_log(log_dir, {"query": keyword, "status": "nav_timeout",
                                      "observedApis": dict(diagnostics)})
                continue
            page.wait_for_timeout(SETTLE_SECONDS * 1000)
            if not buffer and logged_in(context):
                # 首屏资源冷加载可能拖慢 XHR：多等一轮再判。
                page.wait_for_timeout(5 * 1000)
            if not buffer and not logged_in(context):
                print("  登录态无效（无 web_session cookie）。先运行一次：")
                print("    python scripts/browser_auto/xhs_browser_capture.py --login")
                _append_log(log_dir, {"query": keyword, "status": "not_logged_in"})
                context.close()
                return 3
            if not buffer:
                reason = detect_block(None, "", page.url)
                print(f"  未拦截到搜索响应（当前页面：{page.url}）")
                if reason:
                    print(f"  命中即停条件：{reason}")
                    _append_log(log_dir, {"query": keyword, "status": "blocked", "reason": reason,
                                          "pageUrl": page.url})
                    blocked_reason = reason
                    break
                if diagnostics:
                    print("  页面实际调用的接口（供核对拦截规则，把下面内容发维护者）：")
                    for path, status in sorted(diagnostics.items()):
                        print(f"    [{status}] {path}")
                _append_log(log_dir, {"query": keyword, "status": "no_response", "pageUrl": page.url,
                                      "observedApis": diagnostics})
                continue
            for response in buffer:
                try:
                    text = response.text()
                except Exception:
                    continue  # 响应体已释放（页面跳转），跳过这一条
                reason = detect_block(response.status, text, page.url)
                if reason:
                    blocked_reason = reason
                    _append_log(log_dir, {"query": keyword, "status": "blocked", "reason": reason,
                                          "httpStatus": response.status, "url": response.url})
                    break
                try:
                    payload = build_forward_payload(keyword, text)
                except ValueError as exc:
                    _append_log(log_dir, {"query": keyword, "status": "bad_envelope", "error": str(exc)})
                    continue
                result = forward_capture(args.base_url, payload, dry_run=args.dry_run)
                outcome = result.get("status")
                print(f"  转发 ingest：{json.dumps(result, ensure_ascii=False)[:120]}")
                if outcome != "success":
                    dump = _dump_envelope(log_dir, keyword, response.url, response.status, text)
                    print(f"  ↳ 未成功信封已存盘：{dump}（供诊断登录态/解析器）")
                else:
                    forwarded_any = True
                _append_log(log_dir, {"query": keyword, "status": "forwarded",
                                      "dryRun": args.dry_run, "url": response.url,
                                      "result": result})
            if blocked_reason:
                print(f"命中即停条件：{blocked_reason}。按实验协议当日停止，不做任何对抗。")
                break
            if args.from_queue and forwarded_any:
                # 消费语义：成功转发才出队；失败/即停保留在队列里供下次重试。
                removed = consume_keyword(args.base_url, keyword)
                print(f"  已从捕获队列移除该关键词：{removed}")
            if index < len(queries) - 1:
                print(f"  等待 {interval}s 后下一个查询（分钟级间隔）…")
                time.sleep(interval)
        context.close()
    _append_log(log_dir, {"query": "__run_end__", "status": "blocked" if blocked_reason else "done"})
    return 2 if blocked_reason else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="小红书浏览器自动化捕获实验（有头、低量、即停）")
    parser.add_argument("--queries", nargs="+", help="本次运行的查询词（≤5 个）")
    parser.add_argument("--from-queue", action="store_true",
                        help="改从服务端捕获队列取待处理关键词（与 --push-capture-queue 闭环）")
    parser.add_argument("--interval-seconds", type=int, default=120, help="查询间隔（≥60s）")
    parser.add_argument("--login", action="store_true", help="只做一次性登录，保存专用 profile 后退出")
    parser.add_argument("--dry-run", action="store_true", help="拦截信封但不转发，用于首次验证")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--proxy", default=None,
                        help="显式代理（如 http://127.0.0.1:7890）；缺省绕过系统代理直连")
    parser.add_argument("--profile-dir", default="data/browser_auto/xhs-profile")
    parser.add_argument("--data-dir", default="data", help="运行日志目录（默认 data/browser_auto）")
    args = parser.parse_args()
    if args.login:
        args.queries = []
    if not args.queries and not args.from_queue and not args.login:
        parser.error("需要 --queries、--from-queue 或 --login 之一")
    if args.from_queue:
        args.queries = queue_keywords(args.base_url)[:MAX_QUERIES_PER_RUN]
        if not args.queries:
            print("捕获队列为空。")
            return 0
        print(f"从捕获队列取到 {len(args.queries)} 个关键词：{args.queries}")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
