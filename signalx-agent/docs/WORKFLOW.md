# Agent 工作流

入口：行业关键词（BULK_DISCOVERY）或种子事件（EVENT_EXPANSION）。Python 控制顺序和校验；DeepSeek 负责相关性、抽取、证据复核、分类、串联及查询规划。

```mermaid
flowchart TD
    A["关键词或种子事件"] --> B["查询队列与预算检查"]
    B --> C["调用外部采集，取得 Evidence"]
    C --> D{"证据新增或变化？"}
    D -->|是| E["抽取事件，匹配已有 ID"]
    E --> F["节点修订 → 证据与时间复核 → 分类 → 串联与关系复核"]
    F --> G["提交快照"]
    D -->|否| G
    G --> H{"继续查询且预算允许？"}
    H -->|是| B
    H -->|否| I["输出 JSON、独立时间链 MD 及未完成项"]
    E -->|失败| J["保留上次成功快照"]
    F -->|失败| J
```

预设关键词按队列执行，不因每轮上限丢弃。种子即使无需查询，也会完成事件处理；规划失败同样返回已有快照。

## 接口

| 组件 | 输入 → 输出 |
|---|---|
| CollectionTool.search | SearchRequest → Evidence 列表或含来源状态的 CollectionResult |
| EvidenceRelevanceTool.review | 种子事件、补充 Evidence → 相关／无关／不确定及原因 |
| EventExtractionTool.invoke | 增量 Evidence、existingEvents → 完整 Event 列表 |
| EventMergeTool.invoke | Event 列表 → 纠正或拆分混合节点、合并重复节点，保留证据及 ID 沿革 |
| EventGroundingTool.invoke | Event 列表 → 逐条证据立场、时间角色与原文引用；程序计算日期 |
| EventClassificationTool.invoke | 待分类 Event → 分类、标签、置信度 |
| EventChainTool.invoke | Event、分类、previousChains → 候选关系 → 语义复核 → 保留关系、排除原因及未关联事件 |
| SignalXEventProcessor.process | 增量证据、previous、industry → 完整事件快照 |

字段定义见 [models.py](../agent/models.py)、[tool_models.py](../agent/tool_models.py) 和 [workflow_models.py](../agent/workflow_models.py)。

```python
from agent import DeepSeekLLM, EventDiscoveryWorkflow, LLMQueryPlanner, SignalXEventProcessor

llm = DeepSeekLLM()
workflow = EventDiscoveryWorkflow(
    collector=crawler_team_tool,  # 采集组提供
    processor=SignalXEventProcessor.from_llm(llm),
    planner=LLMQueryPlanner(llm),
)
result = workflow.run({
    "mode": "BULK_DISCOVERY",
    "industry": "智能客服",
    "presetKeywords": ["AI 客服", "客服机器人"],
})
```

## 关键规则

- 同 ID 的证据无变化则跳过；新增或变更证据进入抽取，仅重新分类相关事件。
- 对接工厂为种子扩展启用相关性审查；只有相关证据进入处理，其他证据保留在 filteredEvidence，审查失败的输入保留在 unreviewedEvidence。所有审查调用计入模型额度。
- 主体按配置中的公司别名精确归一化；产品名不映射成公司，相似字符串不自动合并。
- 新事件 ID 由证据原文锚点生成；matchedEventId 引用已有事件时保留旧 ID、合并证据。跨来源语义匹配仍需评测。
- 引用必须存在；仅容忍连续空白和汉字之间的排版空格，回写当前证据的连续原文。词语、数字或否定词变化仍拒绝；重试反馈给出具体证据 ID 及原文。
- 证据更新时复核继承的时间引用，失效则清除旧时间并提示；旧快照不变，新模型引用仍严格校验。
- 抽取后由模型复核同一事件：合并置信度至少 0.85、引用各成员证据且时间相容；保留最早输入 ID 和 mergedEventIds。不同代产品及官宣、发布、预订、调价等不同动作不合并。
- 复核可通过 revisions 拆分混合节点、修正标题和时间引用；只重分该节点原有证据且不得丢失。计划、预测、传闻、观点和辟谣通过 claimType 区分，不能合并成已发生的事实。
- 每个事件恰好一个主分类；DeepSeek 决定哪些事件关联，不按公司和类别硬分组。链至少两个事件；无关联依据允许空链，单事件不调用串联模型。
- 每条关系引用两端证据及连续原文；程序拦截未知 ID、自环、重复关系、断开的链、有向循环和已知时间倒置。另一次模型调用逐边检查语义；排除项记入 rejectedRelations，删边后重算分支，独立事件仍保留。
- 关系语义与日历精度分开判断。原文明示的回应、延续关系可在缺日期时成立，`temporalBasis=text`；语义复核保留且置信度至少 0.85 才展示，否则 `needs_review`。链内先满足已复核的相对顺序，再按日期排列并列节点。
- `timeline` 只从已有 Event 生成；新链类别为 `STORYLINE`。模型用 `matchedChainId` 延续旧链 ID，同一成员集改名也保持 ID。
- 逐条检查全部事件证据：`evidenceAssessments` 区分 support／contradict／context／mixed；展示引用只计支持材料，原始证据全部保留。
- 独立反例审查再检查概念混淆、借用背景日期及混合动作；只修正错项。修订后重新核对来源，调用计入总预算。
- `timeCandidates` 分 occurrence／target／report，保留所有日期口径；程序计算 `eventDate`、`targetDate`、`reportDate`。相容来源补全精度，冲突仅保留共同年月；年份上下文须来自同条证据，相对时间须有可靠发布时间。完整带时区 ISO 转 UTC，缺年份保留月日或月份，不以采集时间填空。
- 处理成功才提交完整快照；失败不覆盖上次结果。事件复核可通过 `--resume-from` 重放同一输入的完整响应日志再继续，重放也走校验；进程强杀后不自动恢复。

## 预算与结果

| 参数 | 默认值 | 限制 |
|---|---:|---|
| maxRounds | 3 | 循环轮数 |
| maxToolCalls | 6 | 采集调用次数，含失败 |
| maxLlmCalls | 24 | 模型调用次数，含失败和格式修正重试 |
| perRoundQueryLimit | 5 | 每轮查询数 |
| perQueryLimit | 20 | 每次接收的证据数 |
| maxNoProgressRounds | 1 | 连续无有效增量轮数；尚未搜索的预设关键词不跳过 |

模型预算按每次 run() 独立计数，包含同一事件复核、串联和重试，不是 token/金额上限。自定义组件须通过 `agent.llm.generate_json()` 调用才会纳入计数；工作流结束后的独立分析需另设预算。

结果包含 evidence、events、classifications、chains、rounds 和 stopReason。`timeline.md` 按故事线展示关键节点和已支持关系，计划、预测等标注类型；其余节点及全部新闻在 `report.md`，缺失说明集中在末尾。完整引用、日期分歧和待核验关系保留在 JSON。未完成项看 pendingQueries、unprocessedEvidenceIds、unresolvedQuestions、errors；rounds 保存证据增量和成功快照。

`NO_PROGRESS` 表示无有效增量，保留 `unresolvedQuestions` 和 `stopMessage`，不当作信息已核实。对接工厂每轮最多规划 2 个查询；单事件无需为了形成事件链继续搜索。

## 单事件竞品分析

`CompetitorAgent.analyze(Event)`：校验输入 → 调用模型 → 校验结构与证据 ID → 补充固定元信息 → 输出 CompetitorAnalysis。引用存在不等于结论已得到语义验证。

## 爬虫与监测接入

[crawler_integration.py](../agent/crawler_integration.py) 连接采集桥和 `AgentTrigger`，启动命令见 [README](../README.md)。

- Evidence 保留 `snippet`、`dedupKey`、`hasFullContent`、`sourceName`、`qualityWarnings`；仅摘要不填正文 `content`。证据去重仍以稳定 evidenceId 为准。
- Event 保留 `alertMeta`；告警类型作为线索，主分类仍由 Agent 的受控枚举产生。
- 轮次采集记录新增 `status`、`sourceReports`、`droppedCount`；来源错误正文不进入 Agent 结果。
- 来源失败时有效证据继续处理；正常结束但采集不完整返回 `PARTIAL`，无可用事件则 `FAILED`。预算停止保留原停止原因。
