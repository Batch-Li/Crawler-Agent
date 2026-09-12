# 采集 Tool 对接指南（写给 agent 组）

Agent 已完成接口接线；运行与结果获取见 [Agent README](../signalx-agent/README.md)，测试范围见 [使用报告](../signalx-agent/docs/使用报告.md)。背景见 [发现循环规范](DISCOVERY_WORKFLOW.md) 和 [监测层手册](crawler_tool/monitoring/MONITORING_DESIGN.md)。

## 1. 一分钟概览

```text
你们需要：CollectionTool（真实爬虫）+ 真实告警触发的种子事件
我们提供：① SearchContentCollectionTool（CollectionTool 协议实现，开箱可用）
          ② 告警 → seedEvent 的转换函数（EVENT_EXPANSION §6.2 形态）
          ③ 自动触发器（达标告警转种子，Agent monitor 入口已注入执行器）
```

以下采集侧路径相对仓库的 `crawler_tool/`；Agent 代码在同级 `signalx-agent/`：

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

历史告警节选，仅展示结构；实际输入使用完整告警或 [Agent README](../signalx-agent/README.md) 中的 Event 格式：

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
  "evidence": [{"evidenceId": "cnt_...", "title": "...", "source": "sogou_wechat"}],
  "alertMeta": {"priority": "yellow", "score": 0.634, "matchedRule": "competitor_launch", "platforms": ["sogou_wechat"]}
}
```

组成 `WorkflowRequest`：`{"mode": "EVENT_EXPANSION", "industry": "智能手机", "seedEvent": <上面的对象>, "maxRounds": 3, "maxToolCalls": 6}`。

## 4. 自动触发器：执行点注入

`agent.crawler_cli monitor` 每次执行一轮采集监测，并注入 `DiscoveryRunner` 扩展达标告警。阈值取自 `agentTriggerScore`，仓库配置为 1.5。结果写入指定 `--data-dir/discoveries/`，新结果同时导出 `.timeline.md`。

直接运行 `crawler_tool.monitoring.scheduled_tick` 或 `scripts/run_monitor_tick.cmd` 只做采集监测；未注入执行器时显示 `no_runner_configured`，不会产生 Agent 结果。定期执行需另行配置调度器。

手动接线示例（在 `signalx-agent/` 目录，已安装两个项目依赖并配置模型密钥）：

```python
from agent import DeepSeekLLM
from agent.crawler_integration import build_crawler_workflow, WorkflowDiscoveryRunner

workflow = build_crawler_workflow(DeepSeekLLM(), platforms=["south_weekend", "toutiao", "cctv_news"])
runner = WorkflowDiscoveryRunner(workflow, industry="智能手机", max_llm_calls=24)

# 处理当前已落盘的达标告警；本片段不采集、不注册定时任务：
from crawler_tool.monitoring import AgentTrigger, MonitoringTool
tool = MonitoringTool(config_dir="../crawler_tool/config/monitoring", data_dir="runs/my-topic/monitoring")
settings = tool.config.settings
trigger = AgentTrigger(tool, runner=runner, trigger_score=settings.agent_trigger_score,
                       max_runs_per_day=settings.agent_max_runs_per_day,
                       max_rounds=settings.agent_max_rounds, max_tool_calls=settings.agent_max_tool_calls)
report = trigger.run_due_triggers()
```

已内置的纪律：同一 alertId 只触发一次；每日上限来自配置（仓库配置为 10 次）；
结果持久化在监测数据目录的 `discoveries/{alertId}.json`（含种子、预算和 Agent 结果）；
无 runner 时管道空转不影响人工判读。

`search_with_reports()` 保留来源状态，Agent 用 `CrawlerCollectionTool` 接收。种子内同 ID 证据合并，原始告警不变。结果以 JSON 兼容字典返回；`executionStatus` 区分完成、部分完成、失败。执行失败同样落盘、占用每日额度，不自动重试。

## 5. 采集侧现况与边界（联调前知悉）

- 查询、每轮数量和平台取自 `queries.json`；仓库配置为南周/头条/央视网/微博/搜狗微信，每轮 2 个查询；
- 小红书/抖音/微信：网络不可达（签名墙/平台限制），数据经**手动捕获投喂**进会话视图，
  `includeCaptured` 已把它们并入监测——但 CollectionTool 的 search **不包含**这三个平台的实时采集；
- 搜狗微信默认禁用：需在爬虫服务启动环境配置 `SOGOU_WECHAT_MODE=anonymous_best_effort`，或从监测平台列表移除。返回**包装链接**（非原文 URL）、无正文、验证码即停；
- 部分条目 `publishedAt` 为空（来源未给）——已有组内时间继承机制，但 Evidence 的
  publishedAt 可能为 null，你们的排序逻辑请容忍空值；
- 失败语义：CollectionTool 的上游任何来源失败都会体现在 search-content 的
  sourceReports 里（status 字段），证据条目缺失 ≠ 工具故障。

## 6. 联调步骤建议

1. 先运行 Agent README 的离线演示，确认环境与文件输出；
2. 启动爬虫服务，用实际 Event 执行 `discover --seed`，显式设置搜索与模型预算；
3. 检查 Evidence 全部通过你们 Schema、轮次记录完整；
4. 使用 Agent 的 `monitor` 入口检查告警扩展；未达阈值时不会触发。

## 7. 环境

```powershell
# 从仓库根目录进入；先按 Agent README 安装并激活环境
cd crawler_tool
python -m pytest -q            # 默认离线测试
python -m crawler_tool.interfaces.app --serve   # 服务，127.0.0.1:8301
```

默认采集无需 Cookie；授权来源凭据只在爬虫服务环境配置，不交给 Agent、不提交仓库。
