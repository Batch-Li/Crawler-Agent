"""定时监测层：爬虫输出 → 规则漏斗 → 分级告警 → agent 发现循环种子。

边界约定：
- 本层只消费 ContentItem 形态的字典，不回写 CrawlService；
- 检测链路确定性优先：LLM 仅出现在慢车道分诊（TriageClient），且必须可降级；
- 所有告警可审计：规则 id + 命中词 + 证据引用；
- 持久化只有本地文件（JSONL/JSON），重启不丢告警。
"""

from crawler_tool.monitoring.models import (
    Alert,
    EntityProfile,
    MonitoredItem,
    MonitorConfig,
    RuleFamily,
    WatchSettings,
)
from crawler_tool.monitoring.config_loader import load_monitor_config
from crawler_tool.monitoring.engine import MonitorEngine
from crawler_tool.monitoring.store import MonitorStore
from crawler_tool.monitoring.agent_bridge import (
    MonitoringTool,
    SearchContentCollectionTool,
    alert_to_seed_event,
    item_to_evidence,
)
from crawler_tool.monitoring.agent_trigger import AgentTrigger, DiscoveryRunner

__all__ = [
    "Alert",
    "EntityProfile",
    "MonitoredItem",
    "MonitorConfig",
    "RuleFamily",
    "WatchSettings",
    "load_monitor_config",
    "MonitorEngine",
    "MonitorStore",
    "MonitoringTool",
    "SearchContentCollectionTool",
    "AgentTrigger",
    "DiscoveryRunner",
    "alert_to_seed_event",
    "item_to_evidence",
]
