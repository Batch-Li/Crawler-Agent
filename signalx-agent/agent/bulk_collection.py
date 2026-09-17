"""先完整采集并保存，再由 Agent 分批分析；模型失败不截断采集。"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

from .crawler_integration import CrawlerCollectionTool
from .workflow_models import SearchRequest


def load_crawl_evidence(directory: Path):
    """直接读取原始响应，可恢复中断的采集；保留首个稳定来源 ID。"""
    from crawler_tool.monitoring.agent_bridge import item_to_evidence
    from .models import Evidence
    from pydantic import ValidationError
    evidence, seen_articles = {}, set()
    for path in sorted(directory.rglob('search_responses.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            for row in json.loads(line)['response'].get('items', []):
                try:
                    item = Evidence.model_validate(item_to_evidence(row))
                except (ValidationError, TypeError, AttributeError):
                    continue
                key = item.dedup_key or item.evidence_id
                # 搜索跳转 URL 可能随查询变动；同渠道、作者、完整标题、发布日期识别同一文章。
                article = (item.source, item.source_name, re.sub(r'\s+', '', item.title or ''), item.published_calendar_date)
                if key in evidence or (item.title and item.source_name and article in seen_articles):
                    continue
                evidence[key] = item
                seen_articles.add(article)
    return list(evidence.values())


def collect(collector: CrawlerCollectionTool, *, queries: list[str], industry: str,
            output_dir: Path, limit: int = 100, delay: float = 1.0) -> dict:
    queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
    if not queries or not industry.strip() or not 1 <= limit <= 100 or not math.isfinite(delay) or delay < 0:
        raise ValueError("关键词、条数或请求间隔不合法")
    output_dir.mkdir(parents=True, exist_ok=False)
    evidence, reports = {}, []
    started = datetime.now(timezone.utc).isoformat()
    for index, query in enumerate(queries):
        result = collector.search(SearchRequest(query=query, industry=industry, round_index=1, limit=limit))
        new = 0
        for item in result.evidence:
            key = item.dedup_key or item.evidence_id
            new += key not in evidence
            if key not in evidence:
                evidence[key] = item
        row = {"query": query, **result.model_dump(mode="json", by_alias=True), "newUniqueItems": new}
        with (output_dir / "query_results.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        reports.append({key: value for key, value in row.items() if key != "evidence"})
        print(json.dumps({"query": query, "uniqueItems": len(evidence), "newItems": new,
                          "sourceRequests": collector.tool.request_count}, ensure_ascii=False), flush=True)
        if collector.tool.request_count >= collector.tool.max_requests:
            break
        if index + 1 < len(queries) and delay:
            sleep(delay)
    summary = {"collectionMode": "live_search", "startedAt": started,
               "finishedAt": datetime.now(timezone.utc).isoformat(), "industry": industry,
               "queries": queries, "completedQueries": len(reports),
               "uniqueItems": len(evidence), "sourceRequests": collector.tool.request_count,
               "blockedSources": collector.tool.blocked_sources,
               "partial": len(reports) != len(queries) or any(r['status'] != 'success' for r in reports),
               "queryReports": reports}
    for filename, payload in (("collection.json", summary),
                              ("evidence.json", [e.model_dump(mode="json", by_alias=True) for e in evidence.values()])):
        with (output_dir / filename).open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return summary


def run(args) -> int:
    from crawler_tool.monitoring import SearchContentCollectionTool
    tool = SearchContentCollectionTool(base_url=args.base_url, platforms=args.platform,
        max_pages=args.max_pages, max_requests=args.max_source_requests,
        archive_path=args.output_dir / "search_responses.jsonl")
    summary = collect(CrawlerCollectionTool(tool), queries=args.keyword, industry=args.industry,
                      output_dir=args.output_dir, limit=args.limit, delay=args.request_delay)
    print(json.dumps({key: summary[key] for key in ("uniqueItems", "sourceRequests", "blockedSources")}, ensure_ascii=False))
    return 0 if summary["uniqueItems"] and not summary["partial"] else 2
