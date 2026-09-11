# SignalX Agent

主流程：关键词或种子事件 → 采集 → 相关性审查（种子扩展）→ 抽取 → 分类 → 事件链 → 补充查询。爬虫由 `../crawler_tool` 提供；单事件竞品分析是独立入口。

[使用报告](docs/使用报告.md) · [修复前后完整输出](runs/verification-20260911-213349/raw_output.json) · [流程与接口](docs/WORKFLOW.md)

## 目录

```text
agent/       主流程、模型接口及非爬虫 Tool
tests/       离线测试
examples/    虚构样例与模拟响应
docs/        使用报告、流程与接口
runs/        每次运行的原始数据、结果和日报
.env         本机密钥，不分享
```

## 1. 安装与配置

Python 3.11+；本机验证版本为 3.12。以下命令均在 `code/signalx-agent` 中执行。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-agent.txt -e "../crawler_tool[dev]"
```

已有 `.venv` 时跳过创建。Windows 用 `python` 创建环境，PowerShell 用 `.venv\Scripts\Activate.ps1` 激活。

在本目录创建或编辑 `.env`，在等号后填写密钥：

```dotenv
DEEPSEEK_API_KEY=
```

可选 `DEEPSEEK_BASE_URL`，默认 `https://api.deepseek.com`。程序固定读取本目录的 `.env`，优先级：显式参数 → 环境变量 → `.env`。

## 2. 离线验证

```bash
python -m unittest discover -s tests -q
python -m agent.discovery_demo examples/discovery/evidence.json --replay examples/discovery/responses.json --output-dir runs/demo-001
python -m agent.evaluate runs/demo-001/result.json examples/discovery/expected.json
```

不联网、不收费；样例预期为 2 个事件、1 条事件链。重复运行须更换输出目录。

## 3. 真实采集与分析

终端一启动采集服务，运行期间保持开启：

```bash
python -m crawler_tool.interfaces.app --serve
```

终端二进入同一目录、激活同一环境后执行：

```bash
python -m agent.crawler_cli discover --live --industry "智能穿戴" --keyword "华为智能手表" --platform south_weekend --platform toutiao --max-rounds 3 --max-tool-calls 6 --max-llm-calls 12 --limit 5 --output-dir runs/discover-001
```

结果为 `runs/discover-001/result.json`。支持重复 `--keyword`；扩展已有事件时，将关键词参数替换为 `--seed 文件路径`。种子须为 Event JSON，可用 `alert_to_seed_event()` 转换告警；日报 MD 或整个发现结果不能直接作为种子。

默认读取同级 `crawler_tool/config/monitoring/entities.json` 的主体别名，可用 `--entities-file` 指定其他表。种子扩展每轮最多规划 2 个查询；`--max-no-progress-rounds` 默认 1，无有效增量即停止。

## 4. 单次监测联动

采集服务开启时执行：

```bash
python -m agent.crawler_cli monitor --live --industry "智能手机" --config-dir ../crawler_tool/config/monitoring --data-dir runs/monitoring --crawl-dir runs/crawl --max-llm-calls 12 --limit 5
```

只运行一轮，不注册定时任务。配置位于 `../crawler_tool/config/monitoring/`：

- `entities.json`：主体及别名；只对精确别名做归一化，不把产品名当公司别名。
- `queries.json`：关键词、来源、每轮查询数。只保留已启用来源；现有配置含服务默认关闭的 `sogou_wechat`。
- `base_rules.json` 的 `settings`：触发门槛、额度。当前门槛 `1.5`；默认每日最多 10 次 Agent 派发，每次最多 3 轮、6 次补充搜索。
- `--max-llm-calls` 是每条告警的额度，不是整轮总额度；相关性审查也计入。监测初始搜索另外计数。

## 5. 读取结果

| 场景 | 文件 | 重点字段 |
|---|---|---|
| 关键词发现／种子扩展 | 输出目录的 `result.json` | `stopReason`、`events`、`classifications`、`chains`、`rounds` |
| 监测触发 Agent | `runs/monitoring/discoveries/<告警ID>.json` | `executionStatus`、`result` |
| 监测概览 | `runs/reports/daily_report_日期.md` | 告警、观察记录、派发次数 |

`COMPLETED`：按计划处理完毕。`NO_PROGRESS`：无有效增量，待核实问题仍未解决。`MAX_ROUNDS`、`TOOL_BUDGET_EXHAUSTED`、`MODEL_BUDGET_EXHAUSTED`：达到上限。来源不完整可能返回 `PARTIAL`，异常返回 `FAILED`。

未完成时看 `stopMessage`、`unresolvedQuestions`、`pendingQueries`、`errors`。`filteredEvidence` 保存被隔离证据及原因；`unreviewedEvidence` 保存审查失败的输入；轮次中保留采集证据和审查记录。

`discover` 正常完成退出码为 0，其余结果为 2。`monitor` 返回 0 也可能是未触发 Agent；`dispatched` 只表示已派发，成功与否看结果文件。

## 6. 注意事项

- 真实运行消耗模型额度；调用次数不等于 token 或金额上限。一次搜索可访问多个来源。
- `.env*` 已加入 Git 忽略和 Git 归档排除；手动 ZIP 也要排除密钥、`.venv/` 和缓存。运行数据按分享范围选择。
- 发现输出目录须为新目录。监测目录可以持续使用，但同一目录只允许一个进程；失败派发也占额度并落盘，不自动重试。
- 日报不是最终分析。标题、摘要不等于全文，来源成功不等于内容相关；分类置信度不代表新闻真实性。
- 相关性审查用于种子扩展；`uncertain` 与无关证据均隔离待复核，仍需抽查模型判定。主体别名表须人工维护，配置错误会传递到归组结果。
- 单个事件无需生成事件链；链至少含 2 个同主体同类别事件，仅表示分组，不表示因果。
- 常规 CLI 在工作流返回后落盘，强杀可能丢失当前进度。本轮额外调用审计不等于所有 CLI 都默认保存模型原文。

其他入口：本地 Evidence 用 `agent.discovery_demo --live` 分析；单 Event 用 `agent.demo` 分析竞品。后者直接调用真实模型、没有 `--live` 开关，也不共享发现流程额度。参数见各入口 `--help`。
