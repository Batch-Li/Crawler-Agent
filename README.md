# SignalX：数据采集与事件分析

面向行业新闻与动态调研：采集证据，由 DeepSeek 抽取、合并、分类和串联事件，输出带来源的时间链与结构化结果。

## 目录与入口

| 目录／文档 | 用途 |
|---|---|
| [signalx-agent](signalx-agent/README.md) | Agent 主体；通用使用说明、更换主题、参数和结果读取 |
| [crawler_tool](crawler_tool/README.md) | 采集服务、来源适配、监测与告警 |
| [工作流与接口](signalx-agent/docs/WORKFLOW.md) | 处理流程、数据模型、Tool 接口 |
| [使用报告](signalx-agent/docs/使用报告.md) | 已完成的修复、验证记录与当前限制 |

两个项目保持同级。以下路径均以仓库根目录为起点。

## 快速开始

在仓库根目录执行，使用 Python 3.11+：

```bash
cd signalx-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-agent.txt -e "../crawler_tool[dev]"
python -m agent.discovery_demo examples/discovery/evidence.json --replay examples/discovery/responses.json --output-dir runs/demo-001
```

已有环境时跳过创建。Windows 用 `python` 创建环境，PowerShell 用 `.venv\Scripts\Activate.ps1` 激活。

演示分析使用固定样例，不调用真实采集或模型 API；结果在 `signalx-agent/runs/demo-001/`，先打开 `timeline.md`。重复运行换新输出目录。

真实调研按 [通用使用说明](signalx-agent/README.md) 操作：在 `signalx-agent/.env` 配置自己的 DeepSeek 密钥，启动采集服务，再设置行业、关键词、主体别名和调用预算。已有事件、证据文件、监测联动也使用该说明中的对应入口。

## 结果在哪里

| 结果 | 保存位置 |
|---|---|
| 可读时间链 | `--output-dir` 下的 `timeline.md` |
| 结构化结果与证据 | 同目录 `result.json` |
| 监测触发的分析 | `--data-dir/discoveries/` 下的告警 JSON 和 `.timeline.md` |
| 监测日报 | `--crawl-dir` 的上一级目录下的 `reports/` |

相对路径以执行命令的目录为准。先看结果的 `stopReason` 确认完成程度；具体字段、停止原因和报错处理见通用使用说明第 6、7 节。监测入口每次只运行一轮，未触发 Agent 时没有发现结果。

## 提交与分享

仓库保留源码、文档、测试代码和固定样例；运行数据、测试输出、密钥、虚拟环境和缓存不纳入版本控制。团队成员克隆后配置各自环境，按上述步骤生成结果；需要共享结果时单独交付 `timeline.md` 和 `result.json`，先检查敏感内容。
