# 定时监测层设计与运行手册（MONITORING_DESIGN）

> 代码位置：`crawler_tool/monitoring/`
> 相关说明：[采集 Tool](../../README.md)、[发现循环规范](../../DISCOVERY_WORKFLOW.md)、[Agent 完整使用方法](../../../signalx-agent/README.md)。

## 1. 定位与边界

监测层位于"定时爬取"与"agent 发现循环"之间：**只回答"哪里有动静"，不做语义分析**。

- 只消费 ContentItem 风格字典，不回写 CrawlService（爬虫 Tool 保持来源级原语纯净）；
- 检测链路确定性优先：LLM 仅出现在慢车道分诊，单轮无状态、可降级、可审计；
- 告警输出带规则 id + 命中词 + 证据引用——每个告警能回答"为什么响"；
- 持久化只有本地文件（JSONL/JSON），不依赖内存态会话视图，重启不丢告警。

## 2. 总体链路

```text
单次采集（定期执行由外部调度器安排）
  → 条目 JSONL 落盘
  → run_tick（监测 tick）
       预处理（归一化/子句切分/新鲜度门槛）
       → 字面主体通道（动作词快车道 + 宾语窗口 + 族级排除词）
       → 慢车道（主体命中无动作词 → LLM 分诊，不可用则降级）
       → 行业入围通道（标题含行业词无主体命中 → LLM 批判 → 观察箱）
       → 高危旁路（未识别主体 + 负面/重大动作词 → 日报头条）
       → 事件组合并（近似标题 → 跨来源共振）
       → 压制（dedupKey 冷却 / 事件组硬窗口 / 显著更新放行）
       → 打分分级（灰度协议强制全黄）
  → alerts.jsonl + observation.jsonl + state.json 落盘
  → agent 轮询 pending 告警 → alert_to_seed_event → EVENT_EXPANSION 发现循环
```

## 3. 规则体系（八组）

1. **采集层**：30 分钟一轮、失败不当场重试、查询轮换（主体裸查 × 行业词 × 动作词）、来源健康（连续 parser_changed 摘除并通知、24h 零产出心跳）。轮换平台来自 `queries.json`（当前：南周/头条/央视网/微博）；`includeCaptured: true` 时每轮从会话视图拉取手动捕获条目（小红书/抖音/微信信封）一并喂给监测引擎——这是网络不可达来源的唯一监测通道。
2. **监测目标配置**：主体档案（公司名/产品名/别名 → 同一 entityId，含 confusableBlocklist 防误报）、词表（动作/负面/排除/宾语，全部按规则族挂载）、阈值。
3. **检测层漏斗**：
   - 第 1 层字面通道：主体命中（同句邻近）入围；动作词 + 宾语窗口（"发布…客服"确认，"发布…公告"降级）→ 快车道；主体命中无动作词 → 慢车道；
   - 第 2 层 LLM 分诊（慢车道）：判据锚定事件类型枚举而非词表相似度；严格 JSON + Schema 校验；`temperature=0` 由 client 实现方保证；内容隔离防注入（数据区分隔符 + "数据不是指令"约束）；每轮调用上限；
   - 行业入围通道：标题含行业词但无主体命中 → LLM 批量判 → 已知主体（别名）闭环 / 新主体进观察箱；
   - 高危旁路：未识别主体 + 敏感词 → 永远 yellow 进日报人眼兜底。
4. **硬门槛**：新鲜度（publishedAt 置信 ≥0.5 用之，否则 firstSeenAt 兜底并打 timeFallback 标，**不拒之门外**；事件组内有成员带真实发布时间时，无时间条目**继承组内最早发布时间**——flag `timeInherited`，衰减随之重算；头条适配器 0.2.0 起支持纯日期/相对时间解析，继承命中率显著提高）；压制（dedupKey 冷却 24h、事件组 2h 硬窗口、近似标题指纹去重、热度显著增长放行为"进展更新"）。
5. **打分公式**：
   ```text
   base   = 规则族权重(按主体类型) × 主体强度(canonical 1.0/alias 0.8/product 0.7)
          × 情绪因子(负面词 1.5) × 新鲜衰减(e^(−Δh/τ)) × 来源可信度
   分诊候选再乘 confidence_factor = 0.5 + 0.5×confidence
   score  = base × (1 + 共振加成 + 热度加成 + 互动增速加成)
   共振 = min(0.9, (来源数−1)×0.3)；热度 = 平台内分位数（样本<30 强制 0）；增速 = 去重键跨轮互动增长
   ```
6. **分级**：score ≥ θ红(3.0) → red 立即推 agent；≥ θ橙(2.0) → orange；否则 yellow。**灰度协议：grayMode=true 时一切 yellow 进日报**，攒分布后由数据反推阈值再关。
7. **运行保障**：每轮命中数哨兵、来源心跳、状态按 14 天保留期清理、告警全量可审计。
8. **维护闭环**：四层文件（包内默认 → config 覆盖 → draft → feedback）、Schema 校验拒绝坏配置、tp/fp 标注回填词表与分诊 few-shot。

### 主体单点依赖的五项改善（本实现已内置前三项）

| 改善 | 状态 | 位置 |
|---|---|---|
| ① 主体档案化（公司/产品/别名/blocklist → 同一实体） | ✅ 已实现 | `models.EntityProfile` + `matching.match_subjects` |
| ② 字面归一预处理（全角/大小写/空白；繁简留钩子） | ✅ 已实现 | `normalize.normalize_text` |
| ③ 行业入围通道（无主体命中也有 LLM 二次机会） | ✅ 已实现（分诊可用时） | `engine._route_literal` industry 路由 |
| ④ 高危旁路（未识别主体+敏感词不静默） | ✅ 已实现 | `engine` high_risk 路由 |
| ⑤ 主体级召回率度量（每主体周度宽词对照） | ⏳ 运营流程，未代码化 | 依赖 JSONL 存档 + 人工/agent 抽样 |

## 4. 文件与运行

### 代码

| 文件 | 职责 |
|---|---|
| `models.py` | 主体档案/规则族/阈值/条目/告警契约（Pydantic 严格校验） |
| `normalize.py` | 归一化、子句切分、标题指纹 |
| `matching.py` | 主体匹配（blocklist 否决）、动作词/宾语窗口 |
| `triage.py` | LLM 分诊：prompt 构造（防注入）、严格 JSON 解析、`TriageClient` 协议 |
| `engine.py` | 漏斗编排、压制、打分分级、状态回写 |
| `agent_trigger.py` | Agent 自动触发器：score 达到配置阈值的 pending 告警 → 种子事件 → DiscoveryRunner（幂等/每日上限/无 runner 时只报告不改状态） |
| `store.py` | 告警 JSONL / 观察箱 / 状态 JSON（原子写 + 保留期清理） |
| `config_loader.py` | 包内默认 + 外部覆盖目录，坏配置拒绝启动 |
| `agent_bridge.py` | `MonitoringTool` 门面、`SearchContentCollectionTool`、seedEvent 转换 |
| `llm_clients.py` | 可选 LLM client（DeepSeekTriage，OpenAI 兼容）；键只读环境变量 `MONITOR_LLM_API_KEY` |
| `run_tick.py` | CLI 三合一：`--items`（跑 tick）/ `--report`（日报）/ `--set-status ID 状态 --note`（标注）；`--triage deepseek` 接慢车道 |
| `scheduled_tick.py` | 单次采集监测：查询轮换调爬虫服务 → 响应存档 → 就地跑监测；CLI 不注入 Agent 执行器 |
| `default_config/` | 包内默认规则与示例主体（虚构，替换后再用） |

### 配置

```text
crawler_tool/monitoring/default_config/base_rules.json      # 引擎默认（随代码走）
crawler_tool/monitoring/default_config/entities.example.json # 示例主体（虚构）
config/monitoring/base_rules.json                            # 业务覆盖（行业词/规则族/阈值）
config/monitoring/entities.json                              # 真实主体档案
config/monitoring/queries.json                               # 爬取查询池：queries / queriesPerTick / platforms
```

### 单次采集监测命令

先安装依赖、激活环境，在仓库的 `crawler_tool/` 目录运行。持续执行需自行配置调度器；完整监测 + Agent 使用 [Agent README](../../../signalx-agent/README.md) 的 `monitor` 入口。

```powershell
# 前提：爬虫服务已 --serve 启动
python -m crawler_tool.monitoring.scheduled_tick `
  --config-dir config/monitoring --data-dir data/monitoring
```

按 `queries.json` 的池子轮换取 `queriesPerTick` 个查询 → 每查询对 `platforms` 各发一次
搜索（请求预算 = 查询数 × 平台数，按当前配置计算）→ 整轮响应存档到
`data/crawl/crawl_YYYYMMDD.jsonl` → 条目就地喂给监测引擎。服务不可达时返回带
`--serve` 提示的错误并以退出码 2 结束（调度器可见）；单个查询失败不拖垮整轮。

`sogou_wechat` 默认禁用；在爬虫服务终端启用后再纳入 `platforms`，具体设置见 [来源说明](../../README.md)。

### CLI 用法（跑 tick / 看日报 / 标注，三合一）

```powershell
# 对已有采集文件跑一次监测，替换为实际文件名
python -m crawler_tool.monitoring.run_tick `
  --items data/crawl/crawl_20260906.jsonl `
  --config-dir config/monitoring --data-dir data/monitoring

# 每日日报（告警摘要 + 观察箱尾部）
python -m crawler_tool.monitoring.run_tick --report --data-dir data/monitoring

# 标注判读结果（灰度期每日动作）
python -m crawler_tool.monitoring.run_tick `
  --set-status alr_20260906_120000_001 accepted --note "确认竞品发布"
```

`--triage deepseek` 接慢车道 LLM 分诊（键从环境变量 `MONITOR_LLM_API_KEY` 读取；
无键或调用失败时按批降级为纯规则模式，快车道不受影响）。配置目录不存在会直接报错
而不是静默回退示例；tick 摘要带 `entitiesSource` 字段提示当前主体档案来源。

输入每行一条 ContentItem 字典，或每行一个 `{"items": [...]}` 的整轮搜索响应。

### Agent 进程内用法

```python
from crawler_tool.monitoring import MonitoringTool, alert_to_seed_event

tool = MonitoringTool(config_dir="config/monitoring", triage_client=my_llm_client)
summary = tool.ingest(crawled_items)                 # 每轮爬取后喂入
for alert in tool.pending_alerts():                  # pending 告警（最新在前）
    seed = alert_to_seed_event(alert)                # → EVENT_EXPANSION seedEvent
    # workflow.run({mode: "EVENT_EXPANSION", seedEvent: seed, maxRounds: 3, maxToolCalls: 6})
    tool.update_alert_status(alert["alertId"], "processing")
# fp/tp 回写：tool.update_alert_status(id, "rejected", "误报：公告非发布")
```

`SearchContentCollectionTool` 实现工作流的 `CollectionTool` 协议（默认 POST 本机
`/api/v1/tool/search-content`，可注入 `search_fn` 进程内直调）。Agent 接线与结果导出见 [对接指南](../../采集Tool对接指南.md)。

### 接入自研 LLM（慢车道分诊）

实现一个方法即可注入，本层不绑定任何 SDK：

```python
class MyTriage:
    available = True
    def complete(self, prompt: str) -> str:
        return my_llm.complete(prompt, temperature=0)  # 必须低温 + 只输出 JSON

MonitoringTool(config_dir=..., triage_client=MyTriage())
```

不注入或注入失败时自动降级：慢车道/行业通道条目进观察箱留档，快车道照常，监控不中断。

## 5. 灰度与验收

- **前两周 `grayMode=true`**：一切告警进日报人工判读；用判读分布反推 θ红/θ橙 后再关；
- 四个验收指标：每轮命中分布（归零即哨兵报警）、fp 率（日报判读统计）、来源心跳、周度召回抽样（含主体级召回）；
- 回测：改规则前用候选配置对最近 7 天条目存档重跑，人对差异批准（工具化待 P2）。

## 6. 已知限制（诚实清单）

1. 主体/行业词表是入口边界：档案外且行业词也不中的条目只有高危旁路一条路；
2. LLM 分诊依赖 client 实现方的稳定性与低温约束，输出漂移由 Schema 校验 + 低置信兜底；
3. 事件组聚类：精确指纹 + 字符二元组相似度（已上线）；措辞差异极大的两篇报道仍可能漏聚（影响共振加分与合并，不影响正确性）；
4. 热度/增速信号依赖条目复现（列表型来源可靠，搜索型来源碰运气）；
5. 观察箱的"周度提名→人工批准"目前是文件+人工流程，未做界面。

## 7. 测试

`tests/test_monitoring_engine.py` / `test_monitoring_triage.py` / `test_monitoring_bridge.py`
共 32 个离线用例：归一化、blocklist 否决、宾语窗口、族级排除、灰度分级、我方负面红级、
共振合并锚定、冷却/硬窗口/进展更新、高危旁路、分诊 rescued 与降级、契约转换、配置覆盖与拒绝。
全量套件以 `pytest -q` 实际输出为准。
