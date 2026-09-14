"""Wayback Machine CDX 首次收录时间：详情页无果后的第二级兜底（默认关闭）。

语义边界：首次收录只证明「该 URL 不晚于此时间被公开访问」，不是发布时间本身，
证据强度弱——置信度固定 0.6，条目带 ``published_at_from_wayback`` 警告。
纪律：1 GET/URL、不重试、无代理（HttpClient trust_env=False）；
网络连续失败达到熔断阈值即停（国内访问 web.archive.org 常不可达）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

_CDX_URL = "https://web.archive.org/cdx/search/cdx"
_TIMESTAMP = re.compile(r"^\d{14}$")
# 请求内熔断：连续网络失败达到该值后本次调用停用 Wayback。
CIRCUIT_BREAKER_THRESHOLD = 2


class WaybackUnavailable(Exception):
    """网络失败或响应不可解析；与「无收录记录」（返回 None）区分。"""


def earliest_capture(url: str, http_get: Callable[..., Any], *, timeout: float = 10.0) -> str | None:
    """查询 URL 的最早一次 200 收录；无记录返回 None，网络/解析失败抛 WaybackUnavailable。"""
    try:
        response = http_get(_CDX_URL, params={
            "url": url, "output": "json", "fl": "timestamp", "limit": 1,
            "filter": "statuscode:200",
        }, timeout=timeout)
    except Exception as exc:
        raise WaybackUnavailable(f"wayback request failed: {type(exc).__name__}") from exc
    status_code = getattr(response, "status_code", None)
    if status_code is not None and status_code != 200:
        raise WaybackUnavailable(f"wayback returned HTTP {status_code}")
    try:
        rows = response.json()
    except Exception as exc:
        raise WaybackUnavailable("wayback response is not JSON") from exc
    # fl 指定输出时首行是表头，如 [["timestamp"], ["20200515032145"]]；空归档为 []。
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    for row in rows[1:]:
        if isinstance(row, list) and row and _TIMESTAMP.fullmatch(str(row[0])):
            moment = datetime.strptime(str(row[0]), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            return moment.isoformat()
    return None
