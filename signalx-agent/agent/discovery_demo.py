"""从本地 Evidence 文件运行发现循环，保存各轮快照；采集仅为文件回放。"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path

from pydantic import Field, model_validator

from .event_processor import SignalXEventProcessor
from .llm import DeepSeekLLM
from .models import Evidence, SchemaModel
from .prompts import (
    EVENT_CLASSIFICATION_SYSTEM_PROMPT, EVENT_EXTRACTION_SYSTEM_PROMPT,
    QUERY_PLANNER_SYSTEM_PROMPT,
    EVENT_CHAIN_SYSTEM_PROMPT,
)
from .query_planner import LLMQueryPlanner
from .workflow import EventDiscoveryWorkflow
from .workflow_models import SearchRequest, StopReason, WorkflowRequest


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
        }.get(system_prompt)
        if self.index >= len(self.steps) or self.steps[self.index].stage != stage:
            raise RuntimeError("离线响应已耗尽或阶段不匹配")
        step = self.steps[self.index]
        self.index += 1
        return deepcopy(step.response)


def load_dataset(path: Path, industry: str | None = None) -> LocalDataset:
    payload = json.loads(path.read_text(encoding="utf-8"))
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
    parser.add_argument("--output-dir", type=Path, required=True, help="新的输出目录，拒绝覆盖旧运行")
    parser.add_argument("--max-rounds", type=int, default=10)
    parser.add_argument("--max-tool-calls", type=int, default=10)
    parser.add_argument("--max-llm-calls", type=int, default=24)
    args = parser.parse_args()

    dataset = load_dataset(args.input, args.industry)
    llm = (ReplayLLM(json.loads(args.replay.read_text(encoding="utf-8")))
           if args.replay else DeepSeekLLM())
    request = WorkflowRequest(
        mode="BULK_DISCOVERY", industry=dataset.industry,
        preset_keywords=[batch.query for batch in dataset.batches],
        per_query_limit=100, per_round_query_limit=1,
        max_rounds=args.max_rounds, max_tool_calls=args.max_tool_calls,
        max_llm_calls=args.max_llm_calls,
    )
    args.output_dir.mkdir(parents=True, exist_ok=False)
    result = EventDiscoveryWorkflow(
        FileEvidenceCollector(dataset), SignalXEventProcessor.from_llm(llm), LLMQueryPlanner(llm),
    ).run(request)
    (args.output_dir / "result.json").write_text(result.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8")
    for item in result.rounds:
        (args.output_dir / f"round-{item.round_index:03d}.json").write_text(
            item.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8",
        )
    print(json.dumps({
        "mode": "offline_replay" if args.replay else "live_model_local_evidence",
        "stopReason": result.stop_reason.value, "rounds": len(result.rounds),
        "events": len(result.events), "chains": len(result.chains),
        "llmCalls": result.llm_calls, "outputDirectory": str(args.output_dir.resolve()),
    }, ensure_ascii=False))
    return 0 if result.stop_reason == StopReason.COMPLETED else 2


if __name__ == "__main__":
    raise SystemExit(main())
