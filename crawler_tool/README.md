# crawler_tool

SignalX 统一采集 Tool 与监测层。完整的事件发现、分析和时间链使用 [Agent 使用说明](../signalx-agent/README.md)。

## 当前状态

- `ContentItem(content.v1)` 严格 Pydantic 模型；URL 规范化（去追踪参数）、去重键、稳定 `contentId`、质量告警；
- `SourceAdapter`/`SourceRegistry` + 标准来源状态/错误码（success/empty/rate_limited/authentication_required/parser_changed/source_unavailable/network_error）；
- 统一 `CrawlService`：内存缓存（600s TTL，暂态失败不缓存）、cache-only、去重、adapter close；
- opaque cursor 分页协议（HMAC + TTL + 请求指纹绑定），续页必须显式单平台；
- **监测层**（`crawler_tool/monitoring/`）：按配置查询轮询 → 规则漏斗（主体通道 + 可选 LLM 分诊 + 行业通道 + 高危旁路）→ 事件组聚合打分 → 告警落盘 + 每日 Markdown 日报；Agent 的 `monitor` 入口已注入 `DiscoveryRunner`，可扩展达标告警；
- 手动捕获投喂 + 全来源共享会话视图（内存态）+ 浏览器查看页/捕获队列页；
- Agent Tool 门面：`search_content`、`search_wechat_articles`、`ingest_capture`、`get_source_health`；
- 测试数量以 `python -m pytest -q` 实际输出为准。

当前 MVP 不包含：详情抓取、异步 Job、Redis/MySQL、浏览器 Worker、MCP、Event/Agent 分析层、告警跨重启持久化之外的数据库。

## 来源矩阵

| 来源 | 状态 | 默认可见性 | 监测轮询 | 启用方式 |
|---|---|---|---|---|
| 南方周末 `south_weekend` | 真实首屏可用；分页 unverified 不签发 cursor | 默认联搜成员 | ✅ | 无需配置 |
| 头条 `toutiao` | 首屏可用；分页 fixture 闭环、真实两页未证；时间解析 0.2.0（纯日期/相对时间） | 默认联搜成员 | ✅ | 无需配置 |
| 央视网 `cctv_news` | 官方栏目列表直读，秒级时间+关键词+稳定 ID，字段质量最高 | 显式指定（灰度） | ✅ | 无需配置 |
| 微博 `weibo` | **匿名已停用**（2026-09 实测基本不可用，已从监测配置 `queries.json` 移除；代码保留，显式指定仍可机会性使用）；可选临时授权 | 必须显式指定 | ❌ 已停用 | 可选 `WEIBO_MODE`+`WEIBO_SESSION_COOKIE` |
| **搜狗微信** `sogou_wechat` | 文章索引发现；包装链接不解析、无正文、验证码即停。**实测稳定** | 默认启用 | ✅ 轮换固定成员 | 默认启用；`SOGOU_WECHAT_MODE=disabled` 显式关闭 |
| 微信 `wechat` | 授权通道受跨号列表 200013 平台限制；数据走手动捕获 | 必须显式指定 | 🔗 经捕获通道 | 授权模式需 `WECHAT_MODE`+`WECHAT_SESSION_COOKIE`；默认停用 |
| 小红书 `xiaohongshu` | 接口需动态签名（461），网络禁用；走捕获投喂 | 默认 disabled | 🔗 经捕获通道 | `XHS_MODE`+`XHS_SESSION_COOKIE` |
| 抖音 `douyin` | Parser-only，不做网络自动化 | registry 可见 | 🔗 经捕获通道 | 投喂即可用 |

## 快速上手：采集与监测

先按 [Agent 使用说明](../signalx-agent/README.md) 安装依赖并激活环境。以下命令在仓库的 `crawler_tool/` 目录执行；所有数据路径都相对此目录。

```powershell
# ① 从仓库根目录进入，启动爬虫服务
cd crawler_tool
python -m crawler_tool.interfaces.app --serve

# ② 另一个已激活环境的终端，在同一目录运行一次采集监测
python -m crawler_tool.monitoring.scheduled_tick --config-dir config/monitoring --data-dir data/monitoring

# ③ 看告警摘要；标注时将“实际告警ID”换成输出中的 ID
python -m crawler_tool.monitoring.run_tick --data-dir data/monitoring --report
python -m crawler_tool.monitoring.run_tick --data-dir data/monitoring --set-status "实际告警ID" accepted --note "备注"
```

**查询清单/平台/触发阈值**都在 `config\monitoring\queries.json` 和 `base_rules.json`（业务可改，
每轮自动生效）；**主体档案**（我方/竞品/别名/产品名）在 `config\monitoring\entities.json`。

仓库的监测配置包含 `sogou_wechat`（2026-09 实测稳定，服务端已默认启用，作为轮换固定成员；微博匿名同月停用移除）。换行业时同时调整查询、主体和规则；来源失败见 `queryReports`。

上述命令与 Windows 的 `scripts/run_monitor_tick.cmd` **只做采集监测，不执行 Agent**。脚本优先使用同级 `signalx-agent/.venv/Scripts/python.exe`，否则使用 PATH 的 `python`。定期执行需自行配置调度器，不会自动注册任务。

需要告警扩展、完整结果和时间链时，使用 [Agent README 的监测模式](../signalx-agent/README.md)。其 `monitor` 入口会注入执行器，按 `agentTriggerScore` 和预算处理达标告警。

**数据都在哪**：

| 想看什么 | 位置 |
|---|---|
| 告警 + 观察箱（最常用） | `data\reports\daily_report_日期.md` 或 `run_tick --report` |
| 告警完整记录 / 观察箱 | `data\monitoring\alerts.jsonl` / `observation.jsonl` |
| Windows 脚本日志 | `data\monitoring\tick_log.txt`（直接运行 Python 命令不生成） |
| 爬到的原始内容 | 浏览器 `http://127.0.0.1:8301/view`；原始响应 `data\crawl\` |
| Agent 发现结果 | Agent `monitor` 指定的 `--data-dir/discoveries/`，含 JSON 与 `.timeline.md`；未触发则没有发现结果 |

## 安装与测试

```powershell
python -m pip install -e .          # 运行
python -m pip install -e ".[dev]"   # 开发
python -m pytest -q                 # 默认离线，真实验证需另行启用
```

## API 运行方式

```powershell
$env:CRAWLER_CURSOR_SECRET="replace-with-a-random-secret"   # 分页 cursor 需要
python -m crawler_tool.interfaces.app --serve   # 仅绑定 127.0.0.1:8301
# 可选：--wayback-fallback 启用 Wayback CDX 首次收录时间兜底（默认关；
# 仅作为详情页补时间的第二级，国内访问 web.archive.org 常不可达，连续失败 2 次即熔断）
```

端点全表：

```text
页面：
GET  /view                                # 会话视图浏览器页（平台筛选/搜索/导出）
GET  /capture                             # 捕获任务队列页（队列存服务端，agent 可推送）

系统：
GET  /api/v1/health
GET  /api/v1/tool/source-health
GET  /api/v1/tool/capture-status

搜索：
POST /api/v1/tool/search-content          # 统一搜索；显式 wechat/xiaohongshu/douyin
                                          # 时改读本会话历史；body 加 "enrichTime":true
                                          # 或 "enrichContent":true 启用详情页补全
                                          # （一次 GET 同时取时间与正文，共享 ≤5 条/轮预算；南周/央视支持）
                                          # 实时结果会按 content_id 用会话库全文版自动升级；
                                          # 全文条目不被后来的摘要捕获降级
POST /api/v1/tool/search-wechat-articles  # 微信关键词实时检索（当前 200013 受限）

捕获队列（服务端持久化 data/capture_queue.json）：
GET    /api/v1/tool/capture-queue         # 队列列表
POST   /api/v1/tool/capture-queue         # 追加 {"keywords":[...],"source":"agent|manual"}
DELETE /api/v1/tool/capture-queue?keyword=词

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

开发用单次 smoke：`python -m crawler_tool.interfaces.app --smoke "AI客服" --limit 5`（会访问真实来源）

## 各来源启用与配置要点

### 微博：匿名已停用，授权模式保留

**2026-09 决定：匿名模式停用**——实测基本不可用（`parser_changed` 频发），已从监测配置
`queries.json` 移除；代码保留，显式 `platforms:["weibo"]` 仍可机会性使用，但不要依赖。
授权模式（过渡方案）：

```powershell
$env:WEIBO_MODE="legacy_authorized"
$env:WEIBO_SESSION_COOKIE="你的cookie"     # 只从运行环境注入，绝不写入文件
python -m crawler_tool.interfaces.app --serve
```

只抓首屏；不分页/详情/自动登录/代理。多人部署时只在服务所在机器配一次。错误分类：
未配置→`source_unavailable`；过期/验证码→`authentication_required`；429→`rate_limited`；
结构变化→`parser_changed`。更新会话后重启服务并用 `freshness:"prefer_fresh"` 验证。

### 微信：授权通道停用，搜狗发现源为主渠道

- `WECHAT_MODE` 不配置即为停用（代码保留不删）；实时通道自 2026-07-30 起被平台 200013 能力控制；
- **搜狗发现源：服务端默认启用**（`SOGOU_WECHAT_MODE=disabled` 可显式关闭）：
  只有文章发现（标题/公众号/时间/摘要/包装链），包装链不解析、无正文、验证码即停不重试；
- 微信内容的另一通道是**手动捕获投喂**（浏览器助手 v0.3.0 支持公众号后台），
  `platforms:["wechat"]` 的统一搜索会返回会话历史中的捕获条目；

### 小红书：签名墙，走捕获投喂

`XHS_MODE=disabled`（默认，零网络）/ `authorized_first_page`（+`XHS_SESSION_COOKIE`，显式查询时
一次 POST）。**真实验证已确认接口必须浏览器动态签名**（无签名请求被网关 461 拒绝）——按红线
不逆向签名，**当前不要期待该模式返回真实数据**。推荐用法：手动捕获投喂，`platforms:["xiaohongshu"]`
统一搜索会返回捕获历史。

### 抖音：Parser-only + 手动投喂

适配器零网络。识别三类信封：`{"aweme_list":[...]}`、`{"data":[{"aweme_info":{}},...]}`、
`{"data":{"aweme_details":[...]}}`；其余一律 `parser_changed`。字段：`desc`→标题、
`create_time`→时间、URL 固定 `https://www.douyin.com/video/{aweme_id}`。

### 搜狗微信：公开索引发现

服务端默认启用（2026-09 起，实测稳定）。如需关闭，在启动服务的终端设
`SOGOU_WECHAT_MODE=disabled`（已有服务需重启）；关闭后该来源返回 `source_unavailable`，
需从 `queries.json` 的 `platforms` 临时移除。

```powershell
$env:SOGOU_WECHAT_MODE="anonymous_best_effort"
```

macOS/Linux 使用 `export SOGOU_WECHAT_MODE=anonymous_best_effort`。

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
python -m crawler_tool.interfaces.app --live-smoke-toutiao "AI客服" --online --confirm-real
python -m crawler_tool.interfaces.app --live-smoke-weibo "AI客服" --online --confirm-real
python -m crawler_tool.interfaces.app --live-smoke-xhs "人工智能" --online --confirm-real
python -m crawler_tool.interfaces.app --verify-south-weekend-pagination "AI客服" --online --confirm-real
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
| [Agent 使用说明](../signalx-agent/README.md) | 完整运行命令、配置与结果位置 |
| [监测层手册](crawler_tool/monitoring/MONITORING_DESIGN.md) | 监测系统设计与运行 |
| [采集 Tool 对接指南](采集Tool对接指南.md) | CollectionTool、种子事件与触发器 |
| [发现循环规范](DISCOVERY_WORKFLOW.md) | 发现工作流契约 |
