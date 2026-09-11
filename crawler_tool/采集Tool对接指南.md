# 采集 Tool 对接指南（写给 agent 组）

> 更新时间：2026-09-09。Agent 已完成接口接线；启动命令见 [Agent README](../signalx-agent/README.md)。当前验证为离线协议联调，真实平台与模型验收尚未执行。
>
> 本文是爬虫侧交给 agent 组的**唯一必读对接文档**；
> 背景规范见《DISCOVERY_WORKFLOW.md》，监测层细节见 `crawler_tool/monitoring/MONITORING_DESIGN.md`。

## 1. 一分钟概览

```text
你们需要：CollectionTool（真实爬虫）+ 真实告警触发的种子事件
我们提供：① SearchContentCollectionTool（CollectionTool 协议实现，开箱可用）
          ② 告警 → seedEvent 的转换函数（EVENT_EXPANSION §6.2 形态）
          ③ 自动触发器（score≥1.5 的告警自动转种子，执行点等你注入）
```

两侧代码位置（都在 `F:\monitor\crawler_tool`）：

| 组件 | 位置 |
|---|---|
| CollectionTool 实现 | `crawler_tool/monitoring/agent_bridge.py → SearchContentCollectionTool` |
| Evidence/seedEvent 转换 | 同文件 → `item_to_evidence` / `alert_to_seed_event` |
| 自动触发器 | `crawler_tool/monitoring/agent_trigger.py → AgentTrigger` |
| Agent 执行器（已实现） | `agent.crawler_integration.WorkflowDiscoveryRunner`，注入 `AgentTrigger(runner=...)` |

## 2. CollectionTool：两种接法

```python
# 方式 A：HTTP（agent 是独立进程）——前提：爬虫服务已 --serve 常驻
from crawler_tool.monitoring import SearchContentCollectionTool
collection = SearchContentCollectionTool(base_url="http://127.0.0.1:8301")

# 方式 B：进程内直调（测试或同进程部署）
def my_search(query: str, limit: int) -> dict:
    from crawler_tool.monitoring import MonitoringTool
    ...  # 或直接调 crawler_tool.tools.IntelligenceTools
collection = SearchContentCollectionTool(search_fn=my_search)

# 然后按 DISCOVERY_WORKFLOW §5.1 使用
evidence_list = collection.search({"query": "华为 Mate XT2 价格", "industry": "智能手机", "roundIndex": 1, "limit": 20})
```

返回的 Evidence 字段映射（已实现，勿重复转换）：

| Evidence 字段 | 来源（ContentItem） |
|---|---|
| `evidenceId` | `contentId` |
| `title` / `snippet` | `title` / `summary` |
| `content` | 已有正文，与摘要分开保留；不额外抓详情 |
| `source` | `platform`（toutiao/south_weekend/cctv_news/weibo/sogou_wechat/douyin…） |
| `url` / `publishedAt` | `url` / `publishedAt` |
| `dedupKey` | `dedup.dedupKey`（嵌套结构，已展平） |

## 3. seedEvent：告警长什么样、转出来是什么

真实样例（来自线上告警 `alr_20260907115257_002`，可直接当 EVENT_EXPANSION 联调用例）：

```json
{
  "alertId": "alr_20260907115257_002",
  "priority": "yellow",
  "score": 0.634,
  "matchedRule": "competitor_launch",
  "why": "规则[竞品发布]，主体\"小米\"，动作\"发布\"",
  "resonance": {"sourceCount": 1, "platforms": ["sogou_wechat"]},
  "evidence": [{
    "contentId": "cnt_...",
    "title": "华为、小米同日发布 折叠屏 手机:华为Mate XT 2 非凡大师19999元起",
    "platform": "sogou_wechat",
    "url": "https://weixin.sogou.com/link?url=...",
    "publishedAt": null,
    "dedupKey": "sg_xxxxx"
  }]
}
```

                ↓ `alert_to_seed_event(alert)`

```json
{
  "eventId": "alr_20260907115257_002",
  "title": "华为、小米同日发布 折叠屏 手机:...",
  "summary": "规则[竞品发布]，主体\"小米\"，动作\"发布\"",
  "eventTypes": ["PRODUCT_LAUNCH"],
  "entities": ["小米科技"],
  "evidence": [{"evidenceId": "cnt_...", "title": "...", "source": "sogou_wechat", ...}],
  "alertMeta": {"priority": "yellow", "score": 0.634, "matchedRule": "competitor_launch", "platforms": ["sogou_wechat"]}
}
```

组成 `WorkflowRequest`：`{"mode": "EVENT_EXPANSION", "industry": "智能手机", "seedEvent": <上面的对象>, "maxRounds": 3, "maxToolCalls": 6}`。

## 4. 自动触发器：执行点注入

触发管道已上线：每 30 分钟的定时 tick 里，**score ≥ 1.5（可配）的 pending 告警会自动排队**，
当前因无执行器而记 `agentTrigger: {due: N, skippedReason: "no_runner_configured"}`，状态不动。

Agent 侧已实现 `DiscoveryRunner`；`agent.crawler_cli monitor` 会按监测配置注入。手动接线：

```python
from agent import DeepSeekLLM
from agent.crawler_integration import build_crawler_workflow, WorkflowDiscoveryRunner

workflow = build_crawler_workflow(DeepSeekLLM(), platforms=["south_weekend", "toutiao", "cctv_news"])
runner = WorkflowDiscoveryRunner(workflow, industry="智能手机", max_llm_calls=24)

# 注入后，每次定时 tick 自动派发达标告警：
from crawler_tool.monitoring import AgentTrigger, MonitoringTool
tool = MonitoringTool(config_dir="config/monitoring", data_dir="data/monitoring")
settings = tool.config.settings
trigger = AgentTrigger(tool, runner=runner, trigger_score=settings.agent_trigger_score,
                       max_runs_per_day=settings.agent_max_runs_per_day,
                       max_rounds=settings.agent_max_rounds, max_tool_calls=settings.agent_max_tool_calls)
report = trigger.run_due_triggers()
```

已内置的纪律（你们无需重复实现）：同一 alertId 只触发一次；每天最多 10 次；
结果持久化在 `data/monitoring/discoveries/{alertId}.json`（含种子、预算、你们的结果）；
无 runner 时管道空转不影响人工判读。

`search_with_reports()` 保留来源状态，Agent 用 `CrawlerCollectionTool` 接收。种子内同 ID 证据合并，原始告警不变。结果以 JSON 兼容字典返回；`executionStatus` 区分完成、部分完成、失败。执行失败同样落盘、占用每日额度，不自动重试。

## 5. 采集侧现况与边界（联调前知悉）

- 轮询平台：南周/头条/央视网/微博/搜狗微信（每 30 分钟，5 平台 × 2 查询）；
- 小红书/抖音/微信：网络不可达（签名墙/平台限制），数据经**手动捕获投喂**进会话视图，
  `includeCaptured` 已把它们并入监测——但 CollectionTool 的 search **不包含**这三个平台的实时采集；
- 搜狗微信处于试用期（至 2026-09-14）：返回**包装链接**（非原文 URL）、无正文、验证码即停；
- 部分条目 `publishedAt` 为空（来源未给）——已有组内时间继承机制，但 Evidence 的
  publishedAt 可能为 null，你们的排序逻辑请容忍空值；
- 失败语义：CollectionTool 的上游任何来源失败都会体现在 search-content 的
  sourceReports 里（status 字段），证据条目缺失 ≠ 工具故障。

## 6. 联调步骤建议

1. 用本文 §3 的真实样例 seedEvent 跑一次 EVENT_EXPANSION（离线、fake CollectionTool）；
2. 启动爬虫服务 → 用真实 SearchContentCollectionTool 跑同一 seedEvent（预算 6 次爬取）；
3. 检查 Evidence 全部通过你们 Schema、轮次记录完整；
4. 注入 DiscoveryRunner → 观察一次自动触发全链路。

## 7. 环境

```powershell
cd F:\monitor\crawler_tool
F:\py311\python.exe -m pytest -q            # 离线测试（186+ 用例）
F:\py311\python.exe -m crawler_tool.interfaces.app --serve   # 服务，127.0.0.1:8301
```

凭据类来源（微博授权/微信）当前全部停用，环境无需任何 Cookie。
