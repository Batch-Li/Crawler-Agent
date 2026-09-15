"""头条文章正文补全（浏览器自动化实验，与小红书通道同构、同边界）。

定位：头条搜索由 HttpClient 适配器承担且可用；本文只补**正文**——
文章页有 JS 虚拟机挑战墙，直连 HTTP 拿不到，真实浏览器里挑战自然通过。

    逐篇打开已有条目的文章页（页面自己过挑战、自己渲染）
      → DOM 提取标题/正文/发布时间 → POST /api/v1/tool/ingest-capture
      → store 按 content_id 替换旧条目，正文回填

速度红线（用户明确要求"较为保险的打开文章速度"，代码强制）：
- 单次运行 ≤ MAX_ARTICLES_PER_RUN 篇、间隔 ≥ MIN_INTERVAL_SECONDS 秒；
- 每日预算 ≤ DAILY_ARTICLE_MAX 篇（按当日 JSONL 日志计数，跨运行生效）；
- 顺序单页、无并发、无重试；命中挑战墙/错误桩 → 即停退出。

无登录流程：匿名浏览文章即可；如遇登录墙按即停处理。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8301"
MAX_ARTICLES_PER_RUN = 5
MIN_INTERVAL_SECONDS = 60
DEFAULT_INTERVAL_SECONDS = 120
DAILY_ARTICLE_MAX = 20
SETTLE_SECONDS = 8
FORWARD_TIMEOUT_SECONDS = 10
MIN_BODY_CHARS = 40
_WALL_MARKERS = ("jsvmprt", "<body>error</body>")
_TIME_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})[日]?\s*(\d{1,2}):(\d{2})")
_CST = timezone(timedelta(hours=8))

# DOM 提取（校准点：首次真跑后按实际页面调整选择器）。
_EXTRACT_JS = """
() => {
  const h1 = document.querySelector('h1');
  const title = (h1 && h1.innerText || document.title || '').trim();
  const node = document.querySelector('.article-content')
    || document.querySelector('[class*="article-content"]')
    || document.querySelector('article');
  const content = node ? node.innerText.trim() : '';
  let timeText = null;
  const scope = document.querySelector('.article-meta,[class*="article-meta"]') || document.body;
  const m = scope && scope.innerText.match(/20\\d{2}[-\\/年.]\\d{1,2}[-\\/月.]\\d{1,2}日?\\s*\\d{1,2}:\\d{2}/);
  if (m) timeText = m[0];
  const author = (document.querySelector('[class*="name"] a, [class*="author"]') || {}).innerText || null;
  return {title, content, timeText, author: author && author.trim()};
}
"""


def is_article_url(url: str) -> bool:
    return bool(re.search(r"toutiao\.com/(?:a|article/)\d{6,}", url))


def article_id_of(url: str) -> str | None:
    # 与 crawler_tool.sources.toutiao._source_id 同一逻辑（/a{id} 与 /article/{id}）。
    match = re.search(r"/(?:a|article/)(\d{6,})(?:[/?#]|$)", url)
    if match:
        return match.group(1)
    match = re.search(r"/(\d{6,})(?:[/?#]|$)", url)
    return match.group(1) if match else None


def parse_time_text(text: str | None) -> str | None:
    """页面展示时间（"2026年09月12日 08:30" 等）→ UTC ISO；北京时间语义。"""
    if not text:
        return None
    match = _TIME_RE.search(text)
    if not match:
        return None
    year, month, day, hour, minute = (int(part) for part in match.groups())
    try:
        moment = datetime(year, month, day, hour, minute, tzinfo=_CST)
    except ValueError:
        return None
    return moment.astimezone(timezone.utc).isoformat()


def build_plan(articles: list[tuple[str, str]], interval_seconds: int, done_today: int) -> tuple[list[tuple[str, str]], int]:
    """运行计划：硬限（单次数量/间隔）+ 当日预算**裁剪**（做不完的部分留明天，不整体拒绝）。"""
    deduped = list(dict.fromkeys(articles))
    if not deduped:
        raise ValueError("没有待补正文的头条文章")
    if len(deduped) > MAX_ARTICLES_PER_RUN:
        raise ValueError(f"单次运行最多 {MAX_ARTICLES_PER_RUN} 篇（协议硬限），收到 {len(deduped)} 篇")
    if interval_seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(f"打开间隔不得低于 {MIN_INTERVAL_SECONDS}s（协议硬限），收到 {interval_seconds}s")
    remaining = DAILY_ARTICLE_MAX - done_today
    if remaining <= 0:
        raise ValueError(f"当日预算已用完：{done_today}/{DAILY_ARTICLE_MAX} 篇，明天再补")
    if len(deduped) > remaining:
        deduped = deduped[:remaining]
    return deduped, interval_seconds


def build_forward_payload(url: str, extracted: dict) -> dict:
    article_id = article_id_of(url)
    if not article_id:
        raise ValueError(f"URL 中无法提取文章 ID：{url}")
    if len("".join((extracted.get("content") or "").split())) < MIN_BODY_CHARS:
        raise ValueError("提取正文不足 40 字符（疑似挑战墙/结构变化）")
    return {
        "platform": "toutiao",
        "capture": {
            "captureType": "toutiao_article_detail",
            "articleId": article_id,
            "url": url,
            "title": extracted.get("title") or "",
            "content": extracted["content"],
            "publishTime": parse_time_text(extracted.get("timeText")) or "",
            "author": extracted.get("author") or "",
        },
    }


def needs_content(item: dict) -> bool:
    """缺正文判定：以 quality.hasFullContent 为权威；缺失时退回 content 判空。

    注意头条搜索条目的 content 是 85 字摘要（非空），不能用"有无 content"判断。
    """
    quality = item.get("quality") or {}
    full = quality.get("hasFullContent")
    if full is None:
        return not item.get("content")
    return full is False


def store_articles_missing_content(base_url: str) -> list[tuple[str, str]]:
    """从会话库挑出缺正文的头条条目 (article_id, url)，旧者优先。"""
    response = httpx.get(
        f"{base_url}/api/v1/tool/captured-items",
        params={"platform": "toutiao", "limit": 200}, timeout=FORWARD_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    articles: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in reversed(response.json().get("items") or []):
        if not needs_content(item):
            continue
        url = str(item.get("url") or "")
        # 头条搜索结果含外部站点转载（如博客站），只补本站文章页。
        if not is_article_url(url):
            continue
        article_id = article_id_of(url)
        if not article_id or article_id in seen:
            continue
        seen.add(article_id)
        articles.append((article_id, url))
    return articles


def daily_done(log_dir: Path, today: str) -> int:
    path = log_dir / f"toutiao-runs-{today}.jsonl"
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("status") == "forwarded":
                count += 1
        except json.JSONDecodeError:
            continue
    return count


def blocked_today(log_dir: Path, today: str) -> bool:
    """即停当日自锁：当日日志出现过 blocked，则今天不再运行（无人值守安全前提）。"""
    path = log_dir / f"toutiao-runs-{today}.jsonl"
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("status") == "blocked":
                return True
        except json.JSONDecodeError:
            continue
    return False


def _append_log(log_dir: Path, record: dict) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": datetime.now(timezone.utc).isoformat(), **record}, ensure_ascii=False)
    with (log_dir / f"toutiao-runs-{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def forward_capture(base_url: str, payload: dict, *, dry_run: bool) -> dict:
    if dry_run:
        capture = payload["capture"]
        return {"dryRun": True, "articleId": capture["articleId"],
                "contentChars": len(capture["content"]),
                "publishTime": capture["publishTime"] or None}
    response = httpx.post(
        f"{base_url}/api/v1/tool/ingest-capture", json=payload, timeout=FORWARD_TIMEOUT_SECONDS,
    )
    return response.json()


def run(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        print("缺少 playwright：先在虚拟环境执行 pip install playwright && playwright install chromium", file=sys.stderr)
        return 3
    log_dir = Path(args.data_dir) / "browser_auto"
    today = datetime.now().strftime("%Y%m%d")
    if blocked_today(log_dir, today):
        print("今日已命中即停条件（当日日志有 blocked 记录），按纪律今天不再运行。")
        return 2
    done_today = daily_done(log_dir, today)
    try:
        articles, interval = build_plan(args.articles, args.interval_seconds, done_today)
    except ValueError as exc:
        print(f"运行计划被拒绝：{exc}", file=sys.stderr)
        return 3
    print(f"实验计划：{len(articles)} 篇文章，间隔 {interval}s，今日已补 {done_today}/{DAILY_ARTICLE_MAX}，dry_run={args.dry_run}")
    stop_reason: str | None = None
    forwarded = 0
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            args.profile_dir, headless=False, viewport={"width": 1440, "height": 900},
            args=["--no-proxy-server"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        for index, (article_id, url) in enumerate(articles):
            print(f"[{index + 1}/{len(articles)}] {article_id}")
            try:
                page.goto(url, wait_until="commit", timeout=30_000)
            except PlaywrightTimeout:
                print("  导航超时（30s）：按即停纪律当日停止。")
                _append_log(log_dir, {"articleId": article_id, "status": "nav_timeout"})
                stop_reason = "nav_timeout"
                break
            page.wait_for_timeout(SETTLE_SECONDS * 1000)
            html = page.content()
            if any(marker in html for marker in _WALL_MARKERS):
                print("  命中挑战墙/错误桩：按即停纪律当日停止，不做对抗。")
                _append_log(log_dir, {"articleId": article_id, "status": "blocked", "reason": "challenge_wall"})
                stop_reason = "challenge_wall"
                break
            extracted = page.evaluate(_EXTRACT_JS) or {}
            try:
                payload = build_forward_payload(url, extracted)
            except ValueError as exc:
                print(f"  提取失败：{exc}")
                _append_log(log_dir, {"articleId": article_id, "status": "extract_failed", "error": str(exc)})
                continue
            result = forward_capture(args.base_url, payload, dry_run=args.dry_run)
            forwarded += 1
            print(f"  转发 ingest：{json.dumps(result, ensure_ascii=False)[:120]}")
            _append_log(log_dir, {"articleId": article_id, "status": "forwarded",
                                  "dryRun": args.dry_run, "result": result})
            if index < len(articles) - 1:
                print(f"  等待 {interval}s 后下一篇（保守节奏）…")
                time.sleep(interval)
        context.close()
    _append_log(log_dir, {"articleId": "__run_end__", "status": "blocked" if stop_reason else "done",
                          "forwarded": forwarded})
    return 2 if stop_reason else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="头条文章正文补全（有头、低量、保守节奏、即停）")
    parser.add_argument("--urls", nargs="+", help="要补正文的头条文章 URL（≤5 个）")
    parser.add_argument("--from-store", action="store_true",
                        help="从会话库挑缺正文的头条条目（旧者优先）")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS, help="打开间隔（≥60s）")
    parser.add_argument("--dry-run", action="store_true", help="提取并回显，不转发")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--profile-dir", default="data/browser_auto/toutiao-profile")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()
    if not args.urls and not args.from_store:
        parser.error("需要 --urls 或 --from-store 之一")
    if args.from_store:
        stored = store_articles_missing_content(args.base_url)
        args.articles = stored[:MAX_ARTICLES_PER_RUN]
        if not args.articles:
            print("会话库里没有缺正文的头条条目。")
            return 0
        print(f"会话库中缺正文的头条文章（取前 {len(args.articles)} 篇）：")
        for article_id, url in args.articles:
            print(f"  {article_id}  {url[:70]}")
    else:
        bad = [url for url in args.urls if not is_article_url(url)]
        if bad:
            print(f"以下不是头条文章 URL：{bad}", file=sys.stderr)
            return 3
        args.articles = [(article_id_of(url) or "", url) for url in args.urls]
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
