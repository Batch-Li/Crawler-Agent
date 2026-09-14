"""详情页正文提取：纯函数、零网络、有长度上限。

与 detail_time 同 philosophy：优先结构化标记，逐级降级到内嵌脚本/可见容器。
三级优先级：
1. JSON-LD ``articleBody``（schema.org 标准字段）；
2. 央视网 2026 版契约 ``var contentdate = '<p>…</p>'``（正文以 HTML 片段
   存于内嵌脚本变量，前端注入 content_area；SPA 页面源码里容器是空的）；
3. 可见文章容器（常见中文新闻站正文选择器，仅统计 <p> 段落）。

SPA 站点（如南方周末真页）正文完全依赖状态脚本渲染、结构未公开，
第 2/3 级均取不到时诚实返回 None，由调用方加条目级 warning，待真页校准。
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass

from lxml import html

_SCRIPT_LD_JSON = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I
)
# 央视网 contentdate：单引号或双引号包裹的 HTML 片段，收在引号+分号。
_CCTV_CONTENT_VAR = re.compile(r"var\s+contentdate\s*=\s*[\"'](.*?)[\"']\s*;", re.S)
_ARTICLE_SELECTORS = (
    "//div[contains(@class,'nfzm-content')]",
    "//*[@id='content_area' or contains(@class,'content_area')]",
    "//article",
)
_PARAGRAPH = re.compile(r"<(p|div)[^>]*>(.*?)</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_MAX_CHARS = 20000
_MIN_CHARS = 40


@dataclass(frozen=True)
class DetailContent:
    text: str
    detected_by: str  # "json_ld" | "embedded_variable" | "visible_article"


def parse_detail_content(page: str) -> DetailContent | None:
    """按优先级提取详情页正文；无任何有效信号返回 None。"""
    for text in _json_ld_article_bodies(page):
        if _qualified(text):
            return DetailContent(_clean(text), "json_ld")
    match = _CCTV_CONTENT_VAR.search(page)
    if match:
        text = _clean(match.group(1))
        if _qualified(text):
            return DetailContent(text, "embedded_variable")
    for tree in _trees(page):
        for expression in _ARTICLE_SELECTORS:
            for node in tree.xpath(expression):
                paragraphs = " ".join(html.tostring(p, encoding="unicode") for p in node.xpath(".//p"))
                text = _clean(paragraphs)
                if _qualified(text):
                    return DetailContent(text, "visible_article")
    return None


def _qualified(text: str) -> bool:
    # 过滤导航/版权壳：正文至少要有 _MIN_CHARS 个非空白字符。
    return len("".join(text.split())) >= _MIN_CHARS


def _json_ld_article_bodies(page: str) -> list[str]:
    bodies: list[str] = []
    for block in _SCRIPT_LD_JSON.findall(page):
        try:
            payload = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        bodies.extend(_walk_article_body(payload))
    return bodies


def _walk_article_body(payload: object) -> list[str]:
    if isinstance(payload, dict):
        value = payload.get("articleBody")
        found = [value] if isinstance(value, str) and value.strip() else []
        for child in payload.values():
            found.extend(_walk_article_body(child))
        return found
    if isinstance(payload, list):
        found: list[str] = []
        for child in payload:
            found.extend(_walk_article_body(child))
        return found
    return []


def _trees(page: str):
    try:
        yield html.fromstring(page)
    except ValueError:
        return


def _clean(fragment: str) -> str:
    """HTML 片段 → 纯文本：剥标签、解实体、按段落换行、截断到上限。"""
    if not _TAG.search(fragment):
        text = fragment
    else:
        paras = [inner for _, inner in _PARAGRAPH.findall(fragment)]
        if paras:
            lines = [_html.unescape(_TAG.sub("", para)).strip() for para in paras]
            text = "\n".join(line for line in lines if line)
        else:
            text = _html.unescape(_TAG.sub("", fragment)).strip()
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text[:_MAX_CHARS]
