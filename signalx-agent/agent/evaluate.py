"""比较输出与标注的证据分组及类别；模型质量评测需使用真实人工标注数据。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from pydantic import Field

from .models import SchemaModel
from .workflow_models import EventCategory, WorkflowResult


class ExpectedEvent(SchemaModel):
    evidence_ids: list[str] = Field(min_length=1)
    category: EventCategory


class ExpectedResult(SchemaModel):
    annotation_source: str
    events: list[ExpectedEvent]


def evaluate(result: WorkflowResult, expected: ExpectedResult) -> dict:
    category_by_id = {item.event_id: item.category for item in result.classifications}
    actual = Counter((tuple(sorted({item.evidence_id for item in event.evidence})), category_by_id.get(event.event_id))
                     for event in result.events)
    gold = Counter((tuple(sorted(set(item.evidence_ids))), item.category) for item in expected.events)
    actual_groups, gold_groups = Counter(), Counter()
    for (group, _), count in actual.items():
        actual_groups[group] += count
    for (group, _), count in gold.items():
        gold_groups[group] += count
    matched_groups = sum((actual_groups & gold_groups).values())
    correct_categories = sum((actual & gold).values())
    return {
        "annotationSource": expected.annotation_source,
        "eventGroupingPrecision": matched_groups / len(result.events) if result.events else None,
        "eventGroupingRecall": matched_groups / len(expected.events) if expected.events else None,
        "classificationAccuracyOnMatchedGroups": correct_categories / matched_groups if matched_groups else None,
        "expectedEvents": len(expected.events), "predictedEvents": len(result.events),
        "matchedGroups": matched_groups, "correctCategories": correct_categories,
        "stopReason": result.stop_reason.value,
        "unprocessedEvidenceCount": len(result.unprocessed_evidence_ids),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="按证据精确分组评估事件输出")
    parser.add_argument("result", type=Path)
    parser.add_argument("expected", type=Path)
    args = parser.parse_args()
    result = WorkflowResult.model_validate_json(args.result.read_text(encoding="utf-8"))
    expected = ExpectedResult.model_validate_json(args.expected.read_text(encoding="utf-8"))
    print(json.dumps(evaluate(result, expected), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
