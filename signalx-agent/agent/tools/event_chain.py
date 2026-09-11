"""基于事件主分类、主要主体和时间顺序构建事件链。"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ..models import Event
from ..entity_resolution import EntityResolver
from ..tool_models import EventChainRequest, EventChainResult
from ..workflow_models import EventChain


_CATEGORY_NAMES = {
    "PRODUCT_LAUNCH": "产品发布",
    "PRODUCT_UPDATE": "产品更新",
    "PRICE_CHANGE": "价格变化",
    "MARKET_ENTRY": "市场进入",
    "PARTNERSHIP": "合作",
    "FINANCING": "融资",
    "MERGER_ACQUISITION": "并购",
    "PERSONNEL_CHANGE": "人事变动",
    "POLICY_REGULATION": "政策监管",
    "FINANCIAL_PERFORMANCE": "经营业绩",
    "RISK_INCIDENT": "风险事件",
    "MARKETING_CAMPAIGN": "营销活动",
    "TECHNOLOGY_RELEASE": "技术发布",
    "OTHER": "其他",
}


class EventChainTool:
    """确定性事件链 Tool；第一版不再额外消耗一次 LLM 调用。"""

    name = "event_chain"
    description = "按主分类和主要主体聚合同类 Event，并按事件时间排序。"
    input_model = EventChainRequest
    output_model = EventChainResult

    def __init__(self, min_chain_size: int = 2, entity_aliases: dict[str, str] | None = None) -> None:
        if min_chain_size < 2:
            raise ValueError("min_chain_size 必须大于等于 2")
        self.min_chain_size = min_chain_size
        self.entity_resolver = EntityResolver(entity_aliases)

    def invoke(
        self,
        request: EventChainRequest | dict[str, Any],
    ) -> EventChainResult:
        normalized = (
            request
            if isinstance(request, EventChainRequest)
            else EventChainRequest.model_validate(request)
        )
        if not normalized.events:
            return EventChainResult()

        event_by_id = {item.event_id: item for item in normalized.events}
        grouped: dict[tuple[str, str], list[Event]] = defaultdict(list)
        group_display_name: dict[tuple[str, str], str] = {}

        for classification in normalized.classifications:
            event = event_by_id[classification.event_id]
            if not event.entities:
                continue
            primary_entity = self.entity_resolver.canonical_name(event.entities[0])
            key = (classification.category, _normalize_key(primary_entity))
            grouped[key].append(event)
            group_display_name.setdefault(key, primary_entity)

        chains: list[EventChain] = []
        for (category, entity_key), events in sorted(grouped.items()):
            if len(events) < self.min_chain_size:
                continue
            ordered_events = sorted(events, key=_event_sort_key)
            entity_name = group_display_name[(category, entity_key)]
            category_name = _CATEGORY_NAMES[category]
            chain_id = self._chain_id(category, entity_key)
            chains.append(
                EventChain(
                    chain_id=chain_id,
                    category=category,
                    title=f"{entity_name} · {category_name}事件链",
                    summary=(
                        f"按主要主体和主分类聚合，共包含 {len(ordered_events)} 个事件。"
                    ),
                    event_ids=[event.event_id for event in ordered_events],
                )
            )

        return EventChainResult(chains=chains)

    @staticmethod
    def _chain_id(category: str, entity_key: str) -> str:
        signature = f"{category}|{entity_key}"
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]
        return f"chain_{digest}"


def _normalize_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _event_sort_key(event: Event) -> tuple[bool, datetime, str]:
    return (
        event.event_time is None,
        event.event_time or datetime.max.replace(tzinfo=timezone.utc),
        event.event_id,
    )
