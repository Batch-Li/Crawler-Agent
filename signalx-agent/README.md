# SignalX Agent v1.3

负责事件抽取、分类、事件链和竞品分析；采集组提供标准 Evidence。[流程与接口](docs/WORKFLOW.md)。

目录：`agent/` 源码，`tests/` 测试，`examples/` 样例，`docs/` 流程文档；运行输出放 `runs/`。

## 安装与验证

在 `code/signalx-agent` 目录执行：

```bash
python -m pip install -r requirements-agent.txt
python -m unittest discover -s tests -v
```

## 离线演示

```bash
python -m agent.discovery_demo examples/discovery/evidence.json --replay examples/discovery/responses.json --output-dir runs/demo-001
```

使用虚构证据和模拟响应，无需 API Key。两轮处理得到两个事件、一条事件链；输出目录须为新目录，保存 `result.json` 和各轮 JSON。正常完成退出码为 0，失败或预算停止为 2，仍保留已完成结果。

对照样例检查分组和类别：

```bash
python -m agent.evaluate runs/demo-001/result.json examples/discovery/expected.json
```

离线分数仅验证流程，真实模型质量需独立人工标注数据评测。

## 真实模型

在本目录的 `.env` 中填写 `DEEPSEEK_API_KEY=你的密钥`；文件不存在时自行创建。可选配置 `DEEPSEEK_BASE_URL`。程序自动读取，优先级为显式参数 → 环境变量 → `.env`。

`.env*` 已加入 Git 忽略和 `git archive` 排除规则；另行制作 ZIP 时也须排除 `.env*`。密钥只在本机填写。

```bash
python -m agent.discovery_demo examples/discovery/evidence.json --live --output-dir runs/live-001 --max-llm-calls 12
python -m agent.demo examples/competitor/event.json --output analysis.json
```

第一条处理本地证据，第二条分析单个 Event。文件演示不联网搜索；实际采集需注入 `CollectionTool`。更多参数见 `--help`。

## 对接 crawler_tool

在本目录执行，本机先激活 `.venv`：`source .venv/bin/activate`。双方使用同一 Python 环境。

```bash
python -m pip install -r requirements-agent.txt -e "../crawler_tool[dev]"
python -m crawler_tool.interfaces.app --serve
```

另开终端，激活同一环境，填好 `.env` 后运行：

```bash
python -m agent.crawler_cli discover --live --industry "智能手机" --keyword "折叠屏手机" --platform cctv_news --output-dir runs/crawler-001
python -m agent.crawler_cli monitor --live --industry "智能手机" --config-dir ../crawler_tool/config/monitoring --data-dir runs/monitoring --crawl-dir runs/crawl
```

- `discover` 支持重复 `--keyword`，或改用 `--seed examples/competitor/event.json`；结果含每轮记录，旧输出目录不可覆盖。
- `monitor` 只跑一次：采集 → 监测 → 告警扩展 → 落盘。阈值、平台和查询池沿用配置；示例状态保存在 `runs/`，不改同学提供的历史数据。后台定时任务尚未启用，同一状态目录按单进程串行运行。
- `--live` 会访问爬虫服务及真实模型；`--max-llm-calls` 默认每个告警 24 次，每日告警上限来自监测配置。`maxToolCalls` 统计搜索调用，一次搜索可能访问多个平台。
- 部分来源失败仍保留有效证据和 `sourceReports`；`PARTIAL`/预算停止/失败返回非零退出码。失败告警保留结果且占用额度，不自动重试；`dispatched` 仅表示已派发，完成情况看 `executionStatus`。

Python 注入入口：[crawler_integration.py](agent/crawler_integration.py) 的 `build_crawler_workflow` / `WorkflowDiscoveryRunner`。对接测试：`python -m unittest tests.test_crawler_integration -v`。
