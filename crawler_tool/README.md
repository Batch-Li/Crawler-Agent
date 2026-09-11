# crawler_tool

SignalX 公共爬虫底座与自研 Agent Tool 新项目：**统一采集 Tool + 定时监测系统**。

> 本 README 自包含：各来源配置与使用要点已直接写入正文，列出文档只作更细的背景阅读。
> 深入阅读：`交接文档.md`（项目状态/决策/踩坑）、`文件功能导览.md`（每个文件是干嘛的）、
> `crawler_tool/monitoring/MONITORING_DESIGN.md`（监测系统设计与手册）、`采集Tool对接指南.md`（agent 组对接必读）。

## 当前状态

- `ContentItem(content.v1)` 严格 Pydantic 模型；URL 规范化（去追踪参数）、去重键、稳定 `contentId`、质量告警；
- `SourceAdapter`/`SourceRegistry` + 标准来源状态/错误码（success/empty/rate_limited/authentication_required/parser_changed/source_unavailable/network_error）；
- 统一 `CrawlService`：内存缓存（600s TTL，暂态失败不缓存）、cache-only、去重、adapter close；
- opaque cursor 分页协议（HMAC + TTL + 请求指纹绑定），续页必须显式单平台；
- **定时监测系统**（`crawler_tool/monitoring/`，2026-09 建）：5 平台查询轮询 → 规则漏斗（主体通道 + LLM 慢车道分诊可降级 + 行业通道 + 高危旁路）→ 事件组聚合并共振打分 → 三级分级 → 告警落盘 + 每日 markdown 日报；**score≥1.5 的告警自动转 EVENT_EXPANSION 种子事件**（幂等 + 每日上限，执行点 `DiscoveryRunner` 等 agent 组注入）；
- 手动捕获投喂 + 全来源共享会话视图（内存态）+ 浏览器查看页/捕获队列页；
- Agent Tool 门面：`search_content`、`search_wechat_articles`、`ingest_capture`、`get_source_health`；
- 测试数量以 `F:\py311\python.exe -m pytest -q` 实际输出为准（不在 README 维护静态数字）。

当前 MVP 不包含：详情抓取、异步 Job、Redis/MySQL、浏览器 Worker、MCP、Event/Agent 分析层、告警跨重启持久化之外的数据库。

## 来源矩阵

| 来源 | 状态 | 默认可见性 | 监测轮询 | 启用方式 |
|---|---|---|---|---|
| 南方周末 `south_weekend` | 真实首屏可用；分页 unverified 不签发 cursor | 默认联搜成员 | ✅ | 无需配置 |
| 头条 `toutiao` | 首屏可用；分页 fixture 闭环、真实两页未证；时间解析 0.2.0（纯日期/相对时间） | 默认联搜成员 | ✅ | 无需配置 |
| 央视网 `cctv_news` | 官方栏目列表直读，秒级时间+关键词+稳定 ID，字段质量最高 | 显式指定（灰度） | ✅ | 无需配置 |
| 微博 `weibo` | 匿名尽力而为（近期 parser_changed 偏多，待修）；可选临时授权 | 必须显式指定 | ✅ | 可选 `WEIBO_MODE`+`WEIBO_SESSION_COOKIE` |
| **搜狗微信** `sogou_wechat` | **微信文章发现主渠道**（试用期至 2026-09-14，观察验证码频率）；只有发现（包装链接不解析、无正文）、验证码即停 | 需显式开启 | ✅ | `SOGOU_WECHAT_MODE=anonymous_best_effort`（已设用户级持久变量） |
| 微信 `wechat` | 授权通道**停用**（跨号列表 200013 平台限制；代码保留）；数据走手动捕获 | 必须显式指定 | 🔗 经捕获通道 | `WECHAT_MODE`+`WECHAT_SESSION_COOKIE`（当前未配置） |
| 小红书 `xiaohongshu` | 接口需动态签名（461），网络禁用；走捕获投喂 | 默认 disabled | 🔗 经捕获通道 | `XHS_MODE`+`XHS_SESSION_COOKIE` |
| 抖音 `douyin` | Parser-only，不做网络自动化 | registry 可见 | 🔗 经捕获通道 | 投喂即可用 |

## 快速上手：定时监测系统（日常主路径）

```powershell
# ① 启动爬虫服务（或配置 ONLOGON 计划任务自动启动）
cd F:\monitor\crawler_tool
F:\py311\python.exe -m crawler_tool.interfaces.app --serve

# ② 注册 30 分钟定时任务（爬取+监测一条命令）
schtasks /Create /TN "SignalX监控" /SC MINUTE /MO 30 ^
  /TR "F:\monitor\crawler_tool\scripts\run_monitor_tick.cmd"

# ③ 每天看自动生成的日报（data\reports\daily_report_日期.md），
#    对 pending 告警标注：
F:\py311\python.exe -m crawler_tool.monitoring.run_tick `
  --data-dir data\monitoring --set-status <告警ID> accepted --note "备注"
```

**查询清单/平台/触发阈值**都在 `config\monitoring\queries.json` 和 `base_rules.json`（业务可改，
每轮自动生效）；**主体档案**（我方/竞品/别名/产品名）在 `config\monitoring\entities.json`。

**agent 自动触发**：score ≥ `agentTriggerScore`（当前 1.5，业务可调）的 pending 告警自动转
EVENT_EXPANSION 种子事件，结果落 `data\monitoring\discoveries\`。发现循环执行点
（`DiscoveryRunner` 协议）等 agent 组注入——对接细节见《采集Tool对接指南.md》。

**数据都在哪**：

| 想看什么 | 位置 |
|---|---|
| 告警 + 观察箱（最常用） | `data\reports\daily_report_日期.md` 或 `run_tick --report` |
| 告警完整记录 / 观察箱 / 运行日志 | `data\monitoring\alerts.jsonl` / `observation.jsonl` / `tick_log.txt` |
| 爬到的原始内容 | 浏览器 `http://127.0.0.1:8301/view`；原始响应 `data\crawl\` |
| agent 发现结果 | `data\monitoring\discoveries\` |

## 安装与测试

```powershell
F:\py311\python.exe -m pip install -e .          # 运行
F:\py311\python.exe -m pip install -e ".[dev]"   # 开发
F:\py311\python.exe -m pytest -q                 # 全部离线，不访问公网
```

## API 运行方式

```powershell
$env:CRAWLER_CURSOR_SECRET="replace-with-a-random-secret"   # 分页 cursor 需要
F:\py311\python.exe -m crawler_tool.interfaces.app --serve   # 仅绑定 127.0.0.1:8301
```

端点全表：

```text
页面：
GET  /view                                # 会话视图浏览器页（平台筛选/搜索/导出）
GET  /capture                             # 捕获任务队列页

系统：
GET  /api/v1/health
GET  /api/v1/tool/source-health
GET  /api/v1/tool/capture-status

搜索：
POST /api/v1/tool/search-content          # 统一搜索；显式 wechat/xiaohongshu/douyin
                                          # 时改读本会话历史
POST /api/v1/tool/search-wechat-articles  # 微信关键词实时检索（当前 200013 受限）

捕获投喂与视图：
POST /api/v1/tool/ingest-capture          # 手动捕获投喂（零网络，信封自动探测）
POST /api/v1/tool/ingest-xhs-capture      # 兼容别名
GET  /api/v1/tool/captured-items          # 会话视图（?platform=&keyword=&limit=）
GET  /api/v1/tool/captured-items/export   # 导出 csv|json
```

查询示例（默认联搜只有南方周末+头条；其余来源必须显式指定）：

```powershell
Invoke-RestMethod -Method POST `
  -Uri http://127.0.0.1:8301/api/v1/tool/search-content `
  -ContentType "application/json" `
  -Body '{"query":"AI客服","platforms":["south_weekend"],"limit":5,"freshness":"prefer_fresh"}'
```

开发用单次 smoke：`F:\py311\python.exe -m crawler_tool.interfaces.app --smoke "AI客服" --limit 5`

## 各来源启用与配置要点

### 微博：匿名 + 临时授权

匿名开箱即用（显式 `platforms:["weibo"]`）。授权模式（匿名不稳时的过渡方案）：

```powershell
$env:WEIBO_MODE="legacy_authorized"
$env:WEIBO_SESSION_COOKIE="你的cookie"     # 只从运行环境注入，绝不写入文件
F:\py311\python.exe -m crawler_tool.interfaces.app --serve
```

只抓首屏；不分页/详情/自动登录/代理。多人部署时只在服务所在机器配一次。错误分类：
未配置→`source_unavailable`；过期/验证码→`authentication_required`；429→`rate_limited`；
结构变化→`parser_changed`。更新会话后重启服务并用 `freshness:"prefer_fresh"` 验证。

### 微信：授权通道停用，搜狗发现源为主渠道

- `WECHAT_MODE` 不配置即为停用（代码保留不删）；实时通道自 2026-07-30 起被平台 200013 能力控制；
- **主渠道为搜狗发现源**（`SOGOU_WECHAT_MODE=anonymous_best_effort`，已设持久变量）：
  只有文章发现（标题/公众号/时间/摘要/包装链），包装链不解析、无正文、验证码即停不重试；
- 微信内容的另一通道是**手动捕获投喂**（浏览器助手 v0.3.0 支持公众号后台），
  `platforms:["wechat"]` 的统一搜索会返回会话历史中的捕获条目；
- 通道规划见《微信数据通道改造方案.md》。

### 小红书：签名墙，走捕获投喂

`XHS_MODE=disabled`（默认，零网络）/ `authorized_first_page`（+`XHS_SESSION_COOKIE`，显式查询时
一次 POST）。**真实验证已确认接口必须浏览器动态签名**（无签名请求被网关 461 拒绝）——按红线
不逆向签名，**当前不要期待该模式返回真实数据**。推荐用法：手动捕获投喂，`platforms:["xiaohongshu"]`
统一搜索会返回捕获历史。

### 抖音：Parser-only + 手动投喂

适配器零网络。识别三类信封：`{"aweme_list":[...]}`、`{"data":[{"aweme_info":{}},...]}`、
`{"data":{"aweme_details":[...]}}`；其余一律 `parser_changed`。字段：`desc`→标题、
`create_time`→时间、URL 固定 `https://www.douyin.com/video/{aweme_id}`。

### 搜狗微信：公开索引发现（微信主渠道，试用期至 2026-09-14）

```powershell
$env:SOGOU_WECHAT_MODE="anonymous_best_effort"
```

单请求首屏（`weixin.sogou.com/weixin?type=2`），每页约 10 条：标题/公众号名/发布时间
（unix 秒→UTC，置信 1.0）/摘要/搜狗包装链。纪律：包装链不解析（一次解析=一次请求）；
验证码页归类 `rate_limited` 且 `retryable=false` 即停；条目带
`search_result_summary_only`/`article_url_is_sogou_wrapped`/`synthetic_source_id` 警告。

### 央视网：官方媒体新闻列表

零配置，显式 `platforms:["cctv_news"]`。栏目列表静态 JSONP 直读（秒级时间/摘要/关键词/
稳定 ARTI_ID/配图，全场质量最高）。当前固定 china 栏目（7 栏目可配，多栏目合并未接线）；
`focus_date` 北京时间→UTC；**栏目列表不认识搜索词**——拉最新列表后本地关键词过滤，
无命中如实 `empty`（`diagnostics.keywordFiltered` 有明细），不是故障；站内搜索被 robots
禁止，不得实现。

## 手动捕获投喂（通用工作流）

适用小红书/抖音/微信。原则：**请求由人触发，工具只解析平台已发给人看的数据**——零网络、
无模式开关、默认可用。

```powershell
$raw = Get-Content .\xhs_capture.json -Raw
Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8301/api/v1/tool/ingest-capture `
  -ContentType "application/json" -Body $raw
```

请求体三等价：裸信封（自动探测平台）/ 显式 `{"platform":"douyin","keyword":"...","capture":{...}}` /
仅 `{"keyword":"...","capture":{...}}`。条目以 `ext.captureIngest.origin=manual_capture` 留痕；
会话视图最多 500 条、重启清空（持久化属 P3）。浏览器助手
`scripts/xhs_douyin_capture_helper.user.js`（v0.3.0，小红书/抖音/公众号）自动转发；
**只转发平台发给你本人的数据，搜索动作必须由人完成**。配合 `includeCaptured`，
捕获条目自动进入监测漏斗。

## 受控真实验证

双门禁：`--online --confirm-real` 缺一不可。每来源最多 2 次 GET（微博 1 次），无
Cookie/token/代理/浏览器/重试/自动重定向；输出脱敏（只有状态、条数、ID 哈希）。失败即停。

```powershell
F:\py311\python.exe -m crawler_tool.interfaces.app --live-smoke-toutiao "AI客服" --online --confirm-real
F:\py311\python.exe -m crawler_tool.interfaces.app --live-smoke-weibo "AI客服" --online --confirm-real
F:\py311\python.exe -m crawler_tool.interfaces.app --live-smoke-xhs "人工智能" --online --confirm-real
F:\py311\python.exe -m crawler_tool.interfaces.app --verify-south-weekend-pagination "AI客服" --online --confirm-real
```

常规测试默认跳过真实验证模块；需要 `CRAWLER_LIVE_SMOKE=1` + `CRAWLER_CONFIRM_REAL=1`
后 `pytest -m real_source_validation`。

## 架构与边界

- 定时任务、Agent Tool、监测层共用 `CrawlService`；Agent 优先 Python Typed Tool SDK，
  HTTP API 是跨进程和 Java 后端边界；
- Agent 不接触 Cookie、token、代理、浏览器和平台 Parser；分页只给 opaque cursor；
- 输出只能是 `ContentItem(content.v1)`；监测层的告警/发现结果是消费侧产物，不回灌采集层；
- 失败不重试（`retryable=false` 服务端判定）；empty 只表示合法空结果，登录页/验证码/429/
  结构变化绝不伪装成 empty；
- 当前不接入 MCP、详情查询、异步 Job、持久化数据库、Event/Agent 分析层。

## 相关文档索引

| 文档 | 内容 |
|---|---|
| `交接文档.md` | 项目状态、来源现状、关键决策、踩坑清单（接手先读） |
| `文件功能导览.md` | 每个文件是干嘛的、Agent 调用链路示例 |
| `crawler_tool/monitoring/MONITORING_DESIGN.md` | 监测系统设计 + 运行手册 |
| `采集Tool对接指南.md` | **agent 组对接必读**（CollectionTool/seedEvent/触发器） |
| `DISCOVERY_WORKFLOW.md` | agent 发现循环工作流规范（对方的系统） |
| `微博临时授权模式配置说明.md` / `微信关键词搜索Tool说明.md` / `小红书首屏Tool说明.md` / `抖音手动投喂Tool说明.md` | 各来源细节（要点已收入本文） |
| `微信数据通道改造方案.md` | 微信通道方案 v2（路线 A~E 状态） |
| `实施方案.md` / `实施方案-微博授权模式后续改造.md` | 历史实施计划（存档） |
