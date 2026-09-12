# SignalX Agent 使用说明

输入行业关键词、已有事件或证据文件，输出事件、分类、带来源的时间链及待核实项。DeepSeek 负责分析和串联，`../crawler_tool` 负责采集与监测。适用于不同领域的新闻／行业事件调研，当前通过命令行运行。

第一次调研新主题：完成第 1、2 节，运行后先看输出目录中的 `timeline.md`。本文的“目标行业”“目标公司”“目标产品”均须替换；`my-topic` 是自定主题目录名。

| 想做什么 | 入口 |
|---|---|
| 用关键词发现事件 | 第 2 节：`crawler_cli discover --keyword` |
| 追踪已有事件的后续或关联 | 第 3 节：`crawler_cli discover --seed` |
| 分析已经爬取好的证据文件 | 第 4 节：`discovery_demo --live` |
| 规则监测，触发后扩展事件 | 第 5 节：`crawler_cli monitor`，每次只运行一轮 |
| 不调用真实模型，先验证环境 | 第 8 节：离线演示 |

## 1. 安装与配置

保留两个同级目录：

```text
仓库根目录/
  signalx-agent/  ← 以下命令均在这里执行
  crawler_tool/  ← 采集与监测依赖
```

使用 Python 3.11+。从仓库根目录进入 `signalx-agent` 后执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-agent.txt -e "../crawler_tool[dev]"
```

已有 `.venv` 时跳过创建。Windows 用 `python` 创建环境，PowerShell 用 `.venv\Scripts\Activate.ps1` 激活。每个新终端都需进入同一目录并激活环境。

在本目录的 `.env` 中填写自己的密钥，不覆盖已有有效配置：

```dotenv
DEEPSEEK_API_KEY=
```

可选 `DEEPSEEK_BASE_URL`，默认 `https://api.deepseek.com`；默认模型为 `deepseek-chat`。程序固定读取本目录的 `.env`，同名系统环境变量优先。行业、关键词、输出位置和调用预算通过命令参数设置，不写在 `.env`。

第 2、3、5 节需要先在终端一启动采集服务，并保持开启：

```bash
python -m crawler_tool.interfaces.app --serve
```

分析命令在终端二执行。服务默认地址为 `http://127.0.0.1:8301`；连接其他已部署服务时，在分析命令中加 `--base-url "服务地址"`。证据文件分析和离线演示不需要采集服务。

## 2. 换一个主题调研

### 准备主体别名

在本目录新建 `configs/my-topic/entities.json`，替换主体信息；多个主体就在 `entities` 中追加对象：

```json
{
  "entities": [
    {
      "entityId": "ent_001",
      "canonicalName": "目标公司全称",
      "type": "industry_actor",
      "aliases": ["目标公司简称"],
      "products": [{"name": "目标产品", "aliases": []}]
    }
  ]
}
```

`aliases` 只填同一主体的其他称呼，产品放 `products`，不同机构分别建档。不需要别名归一化时，文件可设为 `{"entities": []}`；这只适用于关键词发现／种子扩展，主体监测仍需实际主体。

通过 `--entities-file` 指定自己的文件；省略时会读取 `../crawler_tool/config/monitoring/entities.json`，其中是原手机场景配置。

### 运行

```bash
python -m agent.crawler_cli discover --live --industry "目标行业" --keyword "目标公司 目标产品 发布" --keyword "目标公司 目标产品 后续进展" --entities-file configs/my-topic/entities.json --platform south_weekend --platform toutiao --max-rounds 3 --max-tool-calls 6 --max-llm-calls 24 --limit 10 --output-dir runs/my-topic-001
```

| 换主题时修改 | 填法 |
|---|---|
| `--industry` | 行业背景，例如“新能源汽车”“企业软件”，供模型理解领域 |
| `--keyword` | 具体主体＋产品／动作，可重复；例如“比亚迪 海豹 发布”“比亚迪 海豹 调价” |
| `--entities-file` | 当前主题的主体别名文件 |
| `--output-dir` | 本次新目录；再次运行改为 `runs/my-topic-002`，旧目录不会被覆盖 |
| `--platform` | 已接通来源，可重复；示例用南方周末和头条，其他来源与爬虫组确认 |

只改行业名不会更换关键词、别名或监测规则。关键词中的年份只是搜索提示；目前没有严格起止日期过滤参数，限定时间范围需检查事件日期。关键词入口没有种子相关性过滤，结果需抽查是否切题。

### 预算参数

以下默认值针对 `crawler_cli discover`，示例不保证用满预算。

| 参数 | 默认值 | 作用 |
|---|---:|---|
| `--max-rounds` | 3 | 最多处理轮数 |
| `--max-tool-calls` | 6 | 最多采集调用次数，含失败；一次调用可访问多个来源 |
| `--max-llm-calls` | 24 | 最多模型调用次数，含规划、审查、抽取、合并、分类、串联及重试 |
| `--limit` | 20 | 每次查询最多接收的证据数，不是整个调研总数 |
| `--max-no-progress-rounds` | 1 | 连续无有效增量达到该值时停止 |

`--live` 会调用真实服务并消耗模型额度。调用次数不是 token／金额上限；扩大预算前先看停止原因。

## 3. 追踪已有事件

新建 `inputs/my-topic/seed.json`，保存一个 Event 对象。以下仅为格式模板，描述和证据须换成真实材料：

```json
{
  "eventId": "seed_001",
  "title": "待扩展事件的简要描述",
  "entities": ["目标公司全称"],
  "evidence": [
    {
      "evidenceId": "evd_001",
      "title": "报道原始标题",
      "snippet": "从报道复制的原文片段",
      "source": "实际来源名称",
      "url": null
    }
  ]
}
```

`url` 有原文链接就填写，没有则保留 `null`。也可从已有 `result.json` 的 `events` 数组中取出一个完整对象保存为种子；整个结果 JSON、告警 JSON 或日报 MD 不能直接传入。

```bash
python -m agent.crawler_cli discover --live --industry "目标行业" --seed inputs/my-topic/seed.json --entities-file configs/my-topic/entities.json --platform south_weekend --platform toutiao --max-rounds 3 --max-tool-calls 6 --max-llm-calls 24 --limit 10 --output-dir runs/my-topic-followup-001
```

`--seed` 与 `--keyword` 二选一。此入口审查补充证据与种子的相关性，无关或不确定材料隔离保存；这是一次新运行，不是恢复上次全部查询和状态。

## 4. 分析已有爬虫数据

将数据归一化为 Evidence 数组，保存为 `inputs/my-topic/evidence.json`。格式就是第 3 节的 `evidence` 数组，单独保存该数组即可；原始网页、爬虫响应或 JSONL 不能直接传入。

每条至少有 `evidenceId`、`source`，以及 `title`／`content`／`snippet` 中的一项。有全文放 `content` 并标记 `hasFullContent: true`；只有摘要放 `snippet`，`content` 留空并标记 `hasFullContent: false`。ID 应唯一且稳定，保留真实来源与链接。完整字段见 [Evidence 模型](agent/models.py)。

```bash
python -m agent.discovery_demo inputs/my-topic/evidence.json --live --industry "目标行业" --max-rounds 10 --max-tool-calls 10 --max-llm-calls 24 --output-dir runs/my-topic-local-001
```

只读文件、不补爬网页，但会调用真实 DeepSeek。数组每 100 条划成一批，默认最多 10 轮／10 次文件批次读取；模型新提出、文件中没有的查询返回空。除最终结果和时间链，还保存 `round-001.json` 等各轮快照。

## 5. 单次监测并触发分析

沿用第 2 节的 `configs/my-topic/entities.json`，再准备两个文件：

- `base_rules.json`：从 `../crawler_tool/config/monitoring/base_rules.json` 复制到该目录，保留结构，按新行业修改 `industryWords` 和 `families` 中的动作词、对象词、排除词及 `appliesTo`。
- `queries.json`：在该目录新建，替换以下关键词。`includeCaptured: false` 表示不混入采集服务会话中其他手动捕获的内容，需要时再开启。

```json
{
  "queries": ["目标公司 目标产品", "目标行业 关键动作"],
  "queriesPerTick": 2,
  "platforms": ["south_weekend", "toutiao"],
  "includeCaptured": false
}
```

主体 `type` 可取 `self`、`competitor`、`regulator`、`industry_actor`，分别为我方、竞品、监管方和其他行业主体，须与规则的 `appliesTo` 匹配。配置不要沿用旧手机主题的名称、行业词及对象词。

在复制的 `base_rules.json` 中设置 `settings`：`agentAutoTrigger` 控制派发，`agentTriggerScore` 是分数门槛，`agentMaxRunsPerDay` 是每日派发上限，`agentMaxRounds`／`agentMaxToolCalls` 是每条告警的扩展预算。未写出的字段使用程序默认值；当前生效值分别为开启、1.5、10、3、6，按本主题需求调整。

```bash
python -m agent.crawler_cli monitor --live --industry "目标行业" --config-dir configs/my-topic --data-dir runs/my-topic/monitoring --crawl-dir runs/my-topic/crawl --max-llm-calls 24 --limit 10
```

每次只运行一轮：轮换查询 → 规则监测 → 达到门槛则派发 Agent → 更新日报，不自动注册定时任务。`--max-llm-calls` 是每条告警的上限，不是整轮总上限；监测初始采集另计。重复监测同一主题沿用原目录以保留去重、冷却和额度状态，同一目录只运行一个进程。不同主题使用不同配置和数据目录。

## 6. 结果在哪里、怎么看

所有相对路径以运行命令时的当前目录为准；按本文操作，均在仓库根目录的 `signalx-agent/` 下。终端只打印摘要，发现入口的 `outputDirectory` 给出绝对路径。打开该文件夹即可拿到结果，无需到网页下载。

### 关键词、种子和证据文件

文件都在各自的 `--output-dir`。以第 2 节为例：

| 文件 | 用途 |
|---|---|
| `runs/my-topic-001/timeline.md` | 优先阅读：事件日期、关系、依据、来源及待核实项；用 Markdown 编辑器／预览打开 |
| `runs/my-topic-001/result.json` | 程序对接和溯源：事件、分类、链、证据、轮次记录、停止原因；用编辑器打开 |

### 监测联动

按第 5 节的目录参数：

| 路径 | 内容 |
|---|---|
| `runs/my-topic/monitoring/discoveries/<告警ID>.timeline.md` | 本轮新派发结果的独立时间链 |
| `runs/my-topic/monitoring/discoveries/<告警ID>.json` | 派发记录；看 `executionStatus`，分析数据在内层 `result` |
| `runs/my-topic/reports/daily_report_YYYY-MM-DD.md` | 当日监测概览，不是完整分析报告；同一天更新同一文件 |
| `runs/my-topic/monitoring/alerts.jsonl`、`observation.jsonl` | 告警和观察记录，有记录时创建；每行一个 JSON 对象 |
| `runs/my-topic/monitoring/state.json` | 去重、冷却、运行额度等状态，不要当结果删除 |
| `runs/my-topic/crawl/crawl_YYYYMMDD.jsonl` | 监测初始搜索的服务响应，不含 Agent 全部补充搜索或模型对话 |

日报实际在 `--crawl-dir` 的上一级目录下的 `reports/`，日期按 UTC 记。未触发 Agent 就没有发现结果／时间链；派发在生成有效结果前失败时也可能没有时间链。

### 读取与溯源

1. 先看 `result.json` 的 `stopReason`／`stopMessage`，确认完成程度；监测结果先进入内层 `result`。
2. 看 `timeline.md` 中的停止说明、事件链和整体待核实项，按原文链接核实关键日期和关系。未关联事件也保留，日期未知不代表发生得更晚。
3. 查 JSON：`events[].evidence` 是事件证据；`chains[].relations` 是关系类型、理由、原文引用及复核状态；`rounds` 是各轮采集与处理记录。
4. 未完成项看 `unresolvedQuestions`、`pendingQueries`、`unprocessedEvidenceIds`、`errors`。种子过滤记录在 `filteredEvidence`，审查失败输入在 `unreviewedEvidence`。

常规 CLI 不自动生成包含所有爬虫原始报文、模型提示词和响应的 `raw_output.json`；历史测试同名文件由专用审计过程额外保存。日常结果 JSON 保存标准化证据和处理记录，不是完整原始调用日志。

已有结果可离线重新导出 Markdown；输出文件须不存在，父目录须已存在，不会重新分析或补全事件：

```bash
python -m agent.timeline runs/my-topic-001/result.json --output runs/my-topic-001/时间链.md
```

## 7. 停止原因与注意事项

| 状态／现象 | 含义与处理 |
|---|---|
| `COMPLETED` | 当前查询计划已处理完，不代表事实全部核实或资料穷尽 |
| `NO_PROGRESS`／`NO_NEW_QUERIES` | 没有有效增量／可用新查询；根据待核实项调整关键词或补充证据 |
| `NO_EVIDENCE` | 没有可用证据；检查来源状态和关键词 |
| `MAX_ROUNDS`／`TOOL_BUDGET_EXHAUSTED`／`MODEL_BUDGET_EXHAUSTED` | 达到对应上限；保留部分结果，按需要调整预算再开新目录运行 |
| `PARTIAL`／`FAILED` | 来源不完整／处理异常；查看 `errors`、各轮来源状态和未处理证据 |
| 没有时间链关系 | 少于两个独立事件或关联依据不足；允许空链，同品牌不自动串联 |
| `FileExistsError` | 输出目录或导出文件已存在；换新名字，保留旧结果 |
| 启动失败／没有结果文件 | 检查当前目录、虚拟环境、输入 JSON、密钥、服务地址；启动前报错可能尚未落盘 |

- `discover` 和证据文件入口仅 `COMPLETED` 返回退出码 0，其他状态返回 2；非零不等于没有可用文件。`monitor` 返回 0 也可能只是未触发，`dispatched` 仅表示派发，完成情况看结果文件。
- 日期保留原文精度：`eventDate` 为年／月／日，`eventTime` 为带时区精确时刻并转 UTC。不补月初或零点；先后不等于因果，`needs_review` 关系需人工复核。
- 摘要不等于全文，检查 `hasFullContent`、`sourceName`、`qualityWarnings`。模型置信度和引用校验不代替事实核查，来源覆盖与全文获取由爬虫能力决定。
- 工作流返回后才落盘，强行终止可能丢失本次进度；不支持自动断点恢复，失败派发不自动重试。
- 分享可读结果用 `timeline.md`，程序对接同时给 `result.json`。分享项目前排除 `.env`、`.venv/` 和缓存，检查结果内的私有数据，不上传密钥。

## 8. 离线演示与其他入口

下面使用固定虚构数据和模拟响应，不联网、不收费；预期为“产品 A 发布 → 调价”，2 个事件、1 条链：

```bash
python -m agent.discovery_demo examples/discovery/evidence.json --replay examples/discovery/responses.json --output-dir runs/demo-001
python -m agent.evaluate runs/demo-001/result.json examples/discovery/expected.json
python -m unittest discover -s tests -q
```

再次运行换新目录。固定 `responses.json` 仅配套该演示数据，不能用它分析任意新材料。

只需单事件竞品分析时可用 `python -m agent.demo inputs/my-topic/seed.json --output runs/competitor-001.json`。它直接调用真实模型，没有 `--live` 开关、不共享发现预算，也不生成时间链；父目录须已存在，输出文件会覆盖同名文件。不指定 `--output` 时完整 JSON 打印到终端。

代码与接口见 [工作流](docs/WORKFLOW.md)，历史验收见 [使用报告](docs/使用报告.md)。仓库保留测试代码和固定样例，运行产物不纳入版本控制；各次结果从指定输出目录获取。
