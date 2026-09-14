"""对接同学提供的采集桥和 DiscoveryRunner；不包含平台爬虫逻辑。"""

from __future__ import annotations

from typing import TYPE_CHECKING
import json
from pathlib import Path

from pydantic import ValidationError

from .event_processor import SignalXEventProcessor
from .llm import StructuredLLM
from .models import Event, Evidence
from .query_planner import LLMQueryPlanner
from .workflow import EventDiscoveryWorkflow
from .workflow_models import CollectionResult, CollectionSourceReport, SearchRequest, WorkflowRequest
from .tools.evidence_relevance import EvidenceRelevanceTool

if TYPE_CHECKING:
    from crawler_tool.monitoring import SearchContentCollectionTool


class CrawlerCollectionTool:
    name = "signalx_crawler_collection"

    def __init__(self, tool: SearchContentCollectionTool):
        self.tool = tool

    def search(self, request: SearchRequest) -> CollectionResult:
        payload = self.tool.search_with_reports(request)
        reports = [CollectionSourceReport.model_validate({
            key: row[key] for key in ("platform", "status", "count", "retryable") if key in row
        }) for row in payload["sourceReports"]]
        evidence = []
        dropped = payload["droppedCount"]
        for row in payload["evidence"]:
            try:
                evidence.append(Evidence.model_validate(row))
            except ValidationError:
                dropped += 1
        status = payload["status"]
        failed_sources = [r for r in reports if r.status not in ("success", "empty")]
        if not evidence and (dropped or (reports and len(failed_sources) == len(reports))):
            status = "failed"
        elif status != "failed" and (failed_sources or dropped or payload["partial"]):
            status = "partial"
        return CollectionResult(
            evidence=evidence[:request.limit], status=status,
            source_reports=reports, dropped_count=dropped,
        )


class WorkflowDiscoveryRunner:
    """注入 AgentTrigger；行业显式指定，每次告警都有独立模型预算。"""

    def __init__(self, workflow: EventDiscoveryWorkflow, *, industry: str,
                 max_llm_calls: int = 24, per_query_limit: int = 20, max_no_progress_rounds: int = 1,
                 time_fallback: bool = False):
        self.workflow = workflow
        self.industry = industry
        self.max_llm_calls = max_llm_calls
        self.per_query_limit = per_query_limit
        self.max_no_progress_rounds = max_no_progress_rounds
        self.time_fallback = time_fallback

    def run(self, seed_event: dict, budget: dict[str, int]) -> dict:
        request = WorkflowRequest(
            mode="EVENT_EXPANSION", industry=self.industry,
            seed_event=Event.model_validate(seed_event),
            max_rounds=budget["maxRounds"], max_tool_calls=budget["maxToolCalls"],
            max_llm_calls=min(self.max_llm_calls, budget.get("maxLlmCalls", self.max_llm_calls)),
            per_query_limit=self.per_query_limit,
            max_no_progress_rounds=self.max_no_progress_rounds,
            time_fallback=self.time_fallback,
        )
        return self.workflow.run(request).model_dump(mode="json", by_alias=True)


def load_entity_aliases(entities_file: str | Path | None = None) -> dict[str, str]:
    from crawler_tool.monitoring.models import EntityProfile

    path = Path(entities_file) if entities_file is not None else (
        Path(__file__).resolve().parents[2] / "crawler_tool/config/monitoring/entities.json"
    )
    if entities_file is None and not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    aliases: dict[str, str] = {}
    for row in payload["entities"]:
        profile = EntityProfile.model_validate(row)
        if not profile.enabled:
            continue
        for alias in [profile.canonical_name, *profile.aliases]:
            if alias in aliases and aliases[alias] != profile.canonical_name:
                raise ValueError("主体别名配置有冲突")
            aliases[alias] = profile.canonical_name
    return aliases


def build_crawler_workflow(llm: StructuredLLM, *, base_url: str = "http://127.0.0.1:8301",
                           platforms: list[str] | None = None,
                           entities_file: str | Path | None = None,
                           enrich_time: bool = False) -> EventDiscoveryWorkflow:
    from crawler_tool.monitoring import SearchContentCollectionTool

    return EventDiscoveryWorkflow(
        collector=CrawlerCollectionTool(SearchContentCollectionTool(
            base_url=base_url, platforms=platforms, enrich_time=enrich_time,
        )),
        processor=SignalXEventProcessor.from_llm(llm, entity_aliases=load_entity_aliases(entities_file)),
        planner=LLMQueryPlanner(llm, max_queries_per_round=2),
        relevance_filter=EvidenceRelevanceTool(llm),
    )
