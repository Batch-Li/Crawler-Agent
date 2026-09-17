"""从本地 Evidence 文件运行发现循环，保存各轮快照；采集仅为文件回放。"""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path

from pydantic import Field, model_validator

from .event_processor import SignalXEventProcessor
from .llm import DeepSeekLLM, ModelBudgetExceeded, ResponsePrefixLLM, model_call_budget
from .models import Evidence, SchemaModel
from .prompts import (
    EVENT_CLASSIFICATION_SYSTEM_PROMPT, EVENT_EXTRACTION_SYSTEM_PROMPT,
    QUERY_PLANNER_SYSTEM_PROMPT,
    EVENT_CHAIN_SYSTEM_PROMPT,
    EVENT_MERGE_SYSTEM_PROMPT,
    RELATION_REVIEW_SYSTEM_PROMPT,
    EVENT_GROUNDING_SYSTEM_PROMPT,
    EVENT_GROUNDING_AUDIT_SYSTEM_PROMPT,
)
from .query_planner import LLMQueryPlanner
from .workflow import EventDiscoveryWorkflow
from .workflow_models import (EventProcessingResult, SearchRequest, StopReason, WorkflowError,
                              WorkflowRequest, WorkflowResult, WorkflowRound)
from .timeline import write_timeline
from .report import write_report
from .tools.evidence_relevance import EvidenceRelevanceTool


class LocalBatch(SchemaModel):
    query: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1, max_length=100)


class LocalDataset(SchemaModel):
    industry: str = Field(min_length=1)
    batches: list[LocalBatch] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_queries(self):
        keys = [" ".join(item.query.split()).casefold() for item in self.batches]
        if len(keys) != len(set(keys)):
            raise ValueError("本地回放批次的 query 必须唯一")
        return self


class FileEvidenceCollector:
    """按预设 query 返回已存文件中的证据，未知查询返回空列表。"""

    name = "local_evidence_replay"
    collection_mode = "saved_evidence"
    complete_after_presets = True

    def __init__(self, dataset: LocalDataset):
        self.batches = {" ".join(item.query.split()).casefold(): item.evidence for item in dataset.batches}

    def search(self, request: SearchRequest) -> list[Evidence]:
        return self.batches.get(" ".join(request.query.split()).casefold(), [])[:request.limit]


class ReplayStep(SchemaModel):
    stage: str
    response: dict


class ReplayLLM:
    """只回放明确提供的模拟响应，不推断事实、不访问网络。"""

    model_name = "offline-fixture-replay"

    def __init__(self, steps: list[dict]):
        self.steps = [ReplayStep.model_validate(step) for step in steps]
        self.index = 0

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        stage = {
            EVENT_EXTRACTION_SYSTEM_PROMPT: "extraction",
            EVENT_CLASSIFICATION_SYSTEM_PROMPT: "classification",
            QUERY_PLANNER_SYSTEM_PROMPT: "planning",
            EVENT_CHAIN_SYSTEM_PROMPT: "chain",
            EVENT_MERGE_SYSTEM_PROMPT: "merge",
            RELATION_REVIEW_SYSTEM_PROMPT: "relation_review",
            EVENT_GROUNDING_SYSTEM_PROMPT: "grounding",
            EVENT_GROUNDING_AUDIT_SYSTEM_PROMPT: "grounding_audit",
        }.get(system_prompt)
        if self.index >= len(self.steps) or self.steps[self.index].stage != stage:
            raise RuntimeError("离线响应已耗尽或阶段不匹配")
        step = self.steps[self.index]
        self.index += 1
        return deepcopy(step.response)


def load_dataset(path: Path, industry: str | None = None) -> LocalDataset:
    if path.is_dir():
        from .bulk_collection import load_crawl_evidence
        payload = [e.model_dump(mode='json', by_alias=True) for e in load_crawl_evidence(path)]
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and 'result' in payload:
        payload = payload['result']
    if isinstance(payload, dict) and 'stopReason' in payload:
        from .workflow_models import WorkflowResult
        saved = WorkflowResult.model_validate(payload)
        industry = industry or saved.topic or saved.industry
        payload = [e.model_dump(mode='json', by_alias=True) for e in saved.evidence]
    if isinstance(payload, list):
        payload = {
            "industry": industry or "未指定行业",
            "batches": [
                {"query": f"本地证据批次 {start // 100 + 1}", "evidence": payload[start:start + 100]}
                for start in range(0, len(payload), 100)
            ],
        }
    if industry:
        payload["industry"] = industry
    return LocalDataset.model_validate(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="从本地 Evidence 运行完整事件处理循环")
    parser.add_argument("input", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", type=Path, help="离线模拟响应 JSON；不会调用外部 API")
    mode.add_argument("--live", action="store_true", help="使用 DEEPSEEK_API_KEY 调用真实模型")
    parser.add_argument("--industry")
    parser.add_argument("--topic", help="限定主题并逐条审查相关性")
    parser.add_argument("--topic-alias", action="append", default=[])
    parser.add_argument("--batches-per-round", type=int, default=1, help="每轮读入批次数；每批最多 100 条，抽取仍分小批")
    parser.add_argument('--refine-events', action='store_true', help='仅复核已完成结果中的事件、分类和关系，不重新采集或抽取')
    parser.add_argument('--reuse-grounding', action='store_true', help='配合 --refine-events，复用未改变节点已完成的证据审查')
    parser.add_argument('--resume-from', type=Path, help='重放同一输入的失败运行目录，再继续未完成步骤')
    parser.add_argument('--resume-response-limit', type=int, help='只重放指定数量的已保存响应，之后按新代码重做；仍逐请求核对指纹')
    parser.add_argument("--output-dir", type=Path, required=True, help="新的输出目录，拒绝覆盖旧运行")
    parser.add_argument("--max-rounds", type=int, default=10)
    parser.add_argument("--max-tool-calls", type=int, default=10)
    parser.add_argument("--max-llm-calls", type=int, default=24)
    args = parser.parse_args()
    if args.reuse_grounding and not args.refine_events:
        parser.error('--reuse-grounding 需要 --refine-events')
    if args.resume_response_limit is not None and not args.resume_from:
        parser.error('--resume-response-limit 需要 --resume-from')

    dataset = load_dataset(args.input, args.industry)
    llm = (ReplayLLM(json.loads(args.replay.read_text(encoding="utf-8")))
           if args.replay else DeepSeekLLM(response_log=args.output_dir / 'model_responses.jsonl'))
    if args.resume_from:
        if not args.live:
            parser.error('--resume-from 仅用于 --live')
        llm = resume_llm(llm, args.resume_from, args.input, args.resume_response_limit)
    request = WorkflowRequest(
        mode="BULK_DISCOVERY", industry=dataset.industry,
        preset_keywords=[batch.query for batch in dataset.batches],
        per_query_limit=100, per_round_query_limit=args.batches_per_round,
        topic=args.topic, topic_aliases=args.topic_alias,
        max_rounds=args.max_rounds, max_tool_calls=args.max_tool_calls,
        max_llm_calls=args.max_llm_calls,
    )
    args.output_dir.mkdir(parents=True, exist_ok=False)
    if args.refine_events:
        result = refine_result(args.input, llm, args.max_llm_calls, force_grounding_review=not args.reuse_grounding)
    else:
        result = EventDiscoveryWorkflow(
            FileEvidenceCollector(dataset), SignalXEventProcessor.from_llm(llm), LLMQueryPlanner(llm),
            relevance_filter=EvidenceRelevanceTool(llm),
        ).run(request)
        result.input_result_sha256 = input_digest(args.input)
    if isinstance(llm, ResponsePrefixLLM):
        result.replayed_llm_calls = llm.replayed_calls
        result.response_replay_sha256 = llm.source_sha256
    (args.output_dir / "result.json").write_text(result.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8")
    write_timeline(result, args.output_dir / "timeline.md")
    write_report(result, args.output_dir / "report.md")
    for item in result.rounds:
        (args.output_dir / f"round-{item.round_index:03d}.json").write_text(
            item.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8",
        )
    print(json.dumps({
        "mode": "offline_replay" if args.replay else "live_model_local_evidence",
        "processingMode": result.processing_mode,
        "stopReason": result.stop_reason.value, "rounds": len(result.rounds),
        "events": len(result.events), "chains": len(result.chains),
        "llmCalls": result.llm_calls, "outputDirectory": str(args.output_dir.resolve()),
        "replayedLlmCalls": result.replayed_llm_calls,
    }, ensure_ascii=False))
    return 0 if result.stop_reason == StopReason.COMPLETED else 2


def resume_llm(delegate, directory, input_path, response_limit=None):
    saved = WorkflowResult.model_validate_json((directory / 'result.json').read_text(encoding='utf-8'))
    if saved.stop_reason == StopReason.COMPLETED:
        raise ValueError('只能恢复未完成的运行')
    if saved.input_result_sha256 != input_digest(input_path):
        raise ValueError('恢复必须使用原来的输入文件，指纹不一致')
    raw = (directory / 'model_responses.jsonl').read_bytes()
    records = [json.loads(line) for line in raw.decode().splitlines()]
    missing_last_transport = (len(records) == saved.llm_calls - 1 and saved.errors
        and saved.errors[-1].error_type in {'APITimeoutError', 'APIConnectionError'})
    if len(records) != saved.llm_calls and not missing_last_transport:
        raise ValueError('保存的模型响应不完整，无法重放')
    if response_limit is not None:
        if not 0 <= response_limit <= len(records):
            raise ValueError('响应重放数量超出已保存范围')
        records = records[:response_limit]
    return ResponsePrefixLLM(delegate, records, hashlib.sha256(raw).hexdigest())


def input_digest(path):
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for source in sorted(path.rglob('search_responses.jsonl')):
        digest.update(str(source.relative_to(path)).encode())
        digest.update(source.read_bytes())
    return digest.hexdigest()


def refine_result(path: Path, llm, max_llm_calls: int, *, force_grounding_review: bool = True) -> WorkflowResult:
    """复核已完成快照；保留来源指纹，异常时返回旧快照和失败状态。"""
    raw = path.read_bytes()
    payload = json.loads(raw)
    source = WorkflowResult.model_validate(payload.get('result', payload))
    if source.unprocessed_evidence_ids or not source.events:
        raise ValueError('仅可复核已有完整事件结果；未处理完的证据请使用普通模式重新抽取')
    snapshot = EventProcessingResult(events=source.events, classifications=source.classifications,
                                     chains=source.chains, unlinked_event_ids=source.unlinked_event_ids,
                                     review_incomplete=source.review_incomplete,
                                     chain_unresolved_questions=source.chain_unresolved_questions,
                                     rejected_relations=source.rejected_relations)
    errors, reason = [], StopReason.COMPLETED
    with model_call_budget(max_llm_calls) as budget:
        try:
            snapshot = SignalXEventProcessor.from_llm(llm).process([], previous=snapshot, industry=source.industry,
                                                                 force_grounding_review=force_grounding_review)
        except Exception as exc:
            reason = StopReason.MODEL_BUDGET_EXHAUSTED if isinstance(exc, ModelBudgetExceeded) else StopReason.FAILED
            errors.append(WorkflowError(stage='processing', error_type=type(exc).__name__,
                                        message='事件复核未完成，保留输入快照与本次模型原始回复。'))
    if reason == StopReason.COMPLETED and snapshot.review_incomplete:
        reason = StopReason.PARTIAL
    completed = reason in (StopReason.COMPLETED, StopReason.PARTIAL)
    return WorkflowResult(
        mode=source.mode, industry=source.industry, topic=source.topic, collection_mode='saved_evidence',
        processing_mode='event_refinement', input_result_sha256=hashlib.sha256(raw).hexdigest(),
        evidence=source.evidence, events=snapshot.events, classifications=snapshot.classifications,
        review_incomplete=snapshot.review_incomplete,
        filtered_evidence=source.filtered_evidence, unreviewed_evidence=source.unreviewed_evidence,
        target_evidence_count=source.target_evidence_count,
        chains=snapshot.chains, unlinked_event_ids=snapshot.unlinked_event_ids,
        rejected_relations=snapshot.rejected_relations, chain_unresolved_questions=snapshot.chain_unresolved_questions,
        unresolved_questions=list(dict.fromkeys([*snapshot.chain_unresolved_questions,
                                                 *(q for c in snapshot.chains for q in c.unresolved_questions)])),
        stop_reason=reason, llm_calls=budget.calls, errors=errors,
        processed_evidence_ids=[e.evidence_id for e in source.evidence] if completed else [],
        unprocessed_evidence_ids=[] if completed else [e.evidence_id for e in source.evidence],
        stop_message=('部分候选复核未完成，保留通过校验的关联。' if reason == StopReason.PARTIAL else
                      '仅复核已有事件与关系；未重新采集或抽取证据。') if completed else errors[0].message,
        rounds=[WorkflowRound(round_index=1, plan_reason='复核已有事件与关系', tool_calls=[],
                              new_evidence_count=0, total_evidence_count=len(source.evidence),
                              event_count=len(snapshot.events), chain_count=len(snapshot.chains),
                              processing_status='completed' if completed else 'failed',
                              llm_calls=budget.calls, errors=errors, snapshot=snapshot)],
    )


if __name__ == "__main__":
    raise SystemExit(main())
