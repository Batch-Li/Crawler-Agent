"""详情页时间提取真实验准（双门禁，固定预算，输出脱敏）。

直接走生产路径 adapter.search + adapter.fetch_detail + Wayback，检验真实来源
的时间信号。用法（在 crawler_tool 目录）：

    python scripts/calibrate_detail_time.py --online --confirm-real

预算（写死，不受参数影响）：南周 1 搜索 + 1 详情 + 1 Wayback；头条 1 搜索 +
最多 2 详情。无重试、无代理；输出只含命中统计与时间值，不含页面正文。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from crawler_tool.domain import ContentSearchRequest
from crawler_tool.infrastructure.http_client import HttpClient
from crawler_tool.infrastructure.wayback_time import WaybackUnavailable, earliest_capture
from crawler_tool.sources import SouthWeekendAdapter, ToutiaoAdapter


def _detail_summary(result) -> dict[str, object]:
    metadata = result.response_metadata if isinstance(result.response_metadata, dict) else {}
    return {
        "status": result.status.value,
        "timeFound": bool(metadata.get("publishedAt")),
        "detectedBy": metadata.get("detectedBy"),
        "publishedAt": metadata.get("publishedAt"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="允许真实网络请求")
    parser.add_argument("--confirm-real", action="store_true", help="确认执行受限真实请求")
    args = parser.parse_args()
    if not (args.online and args.confirm_real):
        print(json.dumps({"outcome": "blocked", "reason": "需要 --online --confirm-real 双门禁"}))
        return 2

    client = HttpClient()
    clock = datetime.now(timezone.utc)
    report: dict[str, object] = {"clock": clock.isoformat()}

    south = SouthWeekendAdapter(http_get=client.get, clock=clock)
    search = south.search(ContentSearchRequest(query="人工智能", limit=5))
    section: dict[str, object] = {"searchStatus": search.status.value, "items": len(search.items)}
    if search.items:
        url = str(search.items[0].payload["url"])
        section["firstUrl"] = url
        section["detail"] = _detail_summary(south.fetch_detail(url))
        try:
            capture = earliest_capture(url, client.get)
            section["wayback"] = {"timeFound": capture is not None, "firstSeen": capture}
        except WaybackUnavailable as exc:
            section["wayback"] = {"timeFound": False, "error": type(exc).__name__}
    report["south_weekend"] = section

    toutiao = ToutiaoAdapter(http_get=client.get, clock=clock)
    search = toutiao.search(ContentSearchRequest(query="人工智能", limit=10))
    raws = [item.payload.get("ext", {}).get("publishedAtRaw") for item in search.items]
    section = {
        "searchStatus": search.status.value,
        "items": len(search.items),
        "publishedCount": sum(1 for item in search.items if item.payload.get("publishedAt")),
        "unparsedRaws": [raw for raw in raws if raw and not any(
            other.payload.get("publishedAt") and other.payload.get("ext", {}).get("publishedAtRaw") == raw
            for other in search.items
        )][:5],
    }
    details = []
    for item in search.items[:2]:
        try:
            summary = _detail_summary(toutiao.fetch_detail(str(item.payload["url"])))
            summary["url"] = str(item.payload["url"])[:80]
        except Exception as exc:
            summary = {"error": type(exc).__name__}
        details.append(summary)
    section["details"] = details
    report["toutiao"] = section

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
