# 新闻信号发现与触发器

输入采集端提供的新闻，输出可追溯信号，达标后调用外部 Agent。

## 安装与接入

Python 3.11+，解压目录执行 `python -m pip install .`。

```python
from news_signals import NewsSignalDiscovery

# news_items 为采集端返回的 ContentItem / Evidence 数组。
# agent_runner 由 Agent 组实现，提供 run(seed_event, budget)。
discovery = NewsSignalDiscovery('configs/news.json', 'data/news-signals', runner=agent_runner)
summary = discovery.run(news_items)
```

只发现信号：不传 `runner`。随后可独立触发：

```python
discovery.trigger.runner = agent_runner
report = discovery.trigger.run_due_triggers()
```

## 两个入口

- `news_signals/discovery.py`：业务筛选、规则评分、归组、信号生成。
- `news_signals/trigger.py`：达标判断、去重、过期检查、每日额度、调用外部 Agent。
- `_core/`：上述功能使用的匹配、配置、状态保存；`schema.py` 为数据结构。

## 对接约定

输入 `ContentItem` 至少含 `contentId`、`platform`、`title`；`dedupKey` 可放在 `dedup.dedupKey`。链接、正文、摘要按实际数据传入。

默认要求原文时间：`ext.publication`（Evidence 为 `publication`）含 `status=verified`、`value`、`raw`、`precision`、`sourceUrl`、`locator`。`value` 是原文日期或带时区时间；只有日期时 `publishedAt` 留空。缺原文时间的条目进入观察，不估算。

Agent 接收 `run(seed_event, budget)`：种子含事件 ID、主体、完整证据和告警信息；预算含 `maxRounds`、`maxToolCalls`、`maxLlmCalls`，由外部 Agent 限制实际调用。返回 JSON 字典，使用 `stopReason`：`COMPLETED`、`FAILED` 或其他停止原因；分别保存为完成、失败、部分完成。

不传执行器时信号保持待处理；失败也计每日额度，不重复热重试。采集和调查实现由各自模块提供，包内没有网络采集、模型客户端或调查流程。

## 配置与输出

`configs/news.json` 修改企业、业务词、主体、规则、时间窗、触发分数和额度；默认手机行业。

指定目录保存 `ticks/<轮次>/input.json`、`screening.json`、`signals.json`、`summary.json`；`discoveries/<信号ID>.json` 保存派发记录，`state.json`、`alerts.jsonl` 保存跨轮状态。一次调用处理一轮，同目录串行调用；换企业使用新目录。
