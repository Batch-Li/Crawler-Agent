# 浏览器自动化捕获（实验）

**实验性质**：在"人工捕获投喂"之外增加**浏览器自动化**通道。产出与油猴信封同构、走同一个 ingest 入口，下游管线/agent 零改动。目前两个脚本：

| 脚本 | 作用 | 定位 |
|---|---|---|
| `xhs_browser_capture.py` | 小红书：自动搜索并转发结果信封 | 补搜索覆盖面（内容=标题级，正文靠详情钩子） |
| `toutiao_browser_capture.py` | 头条：打开已有条目的文章页，回填正文与发布时间 | 补内容厚度（搜索由 HttpClient 适配器承担且可用——文章页有 JS 虚拟机挑战墙，直连拿不到正文，只有真浏览器能过） |

## 双通道架构

```text
通道 A（现有）：人工在浏览器搜索 → 油猴助手被动转发信封
通道 B（本实验）：Playwright 有头浏览器（独立 profile）逐查询/逐文章打开页面（页面自己发请求）
                → 拦截响应信封 → POST /api/v1/tool/ingest-capture
两通道汇合 → 采集服务 ingest → recent_store/dedup → agent 消费
```

## 安装与首次登录

```powershell
# 在虚拟环境里（crawler_tool 目录）
pip install playwright
playwright install chromium

# 小红书一次性登录（用实验小号！不要用主号）
python scripts/browser_auto/xhs_browser_capture.py --login

# 头条无需登录（匿名浏览文章即可）
```

登录态保存在 `data/browser_auto/<平台>-profile`（gitignore 内，不上库）。

## 小红书运行

```powershell
# 首次验证：拦截但不转发，确认信封正常
python scripts/browser_auto/xhs_browser_capture.py --queries "大模型 发布" --dry-run

# 单查询试运行（协议第 1 步）
python scripts/browser_auto/xhs_browser_capture.py --queries "大模型 发布"

# 消费捕获队列（与 agent --push-capture-queue 闭环）
python scripts/browser_auto/xhs_browser_capture.py --from-queue
```

只转发 `search/notes` 与 `homefeed`；`search/recommend|onebox|filter`（联想词/聚合卡片/筛选面板）不含内容，已排除。

## 头条运行

```powershell
# 从会话库挑缺正文的头条条目（旧者优先，≤5 篇/次）
python scripts/browser_auto/toutiao_browser_capture.py --from-store

# 指定文章 URL
python scripts/browser_auto/toutiao_browser_capture.py --urls "https://www.toutiao.com/a7680.../?channel=" --dry-run
```

说明：详情信封 `captureType=toutiao_article_detail` 由 `sources/toutiao.py`（0.4.0）识别；入库按 content_id **替换**旧的摘要条目，正文回填自然生效。DOM 提取选择器是校准点——首次真跑若提取失败，按日志提示把页面结构反馈维护者。

## 由 agent 触发（--trigger-capture）

```powershell
# agent 运行结束后自动触发：小红书消费队列 + 头条补正文（同步，约数分钟）
python -m agent.crawler_cli discover ... --push-capture-queue --trigger-capture
```

两个脚本被 agent 拉起时受**全部相同硬限**约束（预算/同词/间隔/即停自锁），且一个平台撞墙（exit=2）即不再触发另一个。手工运行与 agent 触发完全等价、共用同一套日志与预算。

## 实验协议（红线，代码强制）

| 条目 | 规则 |
|---|---|
| 量级 | 单次运行 ≤5 个查询/文章（硬限，超限直接拒绝）；建议每天 ≤3 次运行 |
| 间隔 | ≥60s 硬限，默认 120s；分钟级，量级对齐"人手动会做的事" |
| **每日预算** | 小红书 ≤10 个查询 / 头条 ≤20 篇（按当日 JSONL 日志跨运行计数，超预算拒绝启动；dry-run 也计入——真实页面流量已发生。对齐重度真人用户 20~50 篇/天的阅读量下沿） |
| **同词频次** | 小红书：同一关键词每日 ≤2 次（重复刷同词是最像机器人的特征） |
| **队列消费** | 小红书 `--from-queue`：关键词**成功转发后才出队**；失败/即停保留在队列里供下次重试 |
| 形态 | 有头浏览器、专用 profile、单页面顺序执行；**绝不 headless、绝无反检测/指纹伪装** |
| 重试 | 无。转发失败只记录；导航超时跳过当前继续 |
| **即停** | 小红书：461/403/429/登录墙/验证码 → 当日停止。头条：挑战墙/错误桩 → 当日停止。均退出码 2，**不做任何对抗** |
| 日志 | `data/browser_auto/`：`runs-*.jsonl`（小红书）/ `toutiao-runs-*.jsonl`（头条），失败信封自动落盘 `envelopes/` |

## 判定标准

- **通过**：连续 5 天低量运行无风控且入库成功 → 可纳入日常，与油猴通道并存
- **不通过**：频繁触发即停 → 实验结束，删除 `data/browser_auto/`，回到纯人工捕获路线（脚本即弃，不污染主干）

## 风险与边界（开始前必读）

1. **账号风险自担**：平台 ToS 禁止自动化采集，风控识别可能导封号——**务必用小号**，主号绝不入实验
2. 本实验不改红线的其余部分：不做签名逆向、不做无头、不做反检测、验证码即停、无重试、无代理
3. 红线例外已留痕：`交接文档.md` 第 6 节（有头实验、小号、即停、无反检测、保守节奏硬限）
4. 任何时侯停止实验：删 profile 目录即彻底退出，账号 cookies 不落仓库

## 常见问题速查

| 现象 | 处理 |
|---|---|
| `缺少 playwright` | 依赖装到了别的 Python——确认用 `..\signalx-agent\.venv\Scripts\python.exe` 全路径 |
| `导航超时（30s）` | 脚本默认**绕过系统代理直连**（本机常驻代理会把国内站点路由到远端节点）；若你的网络必须走代理，小红书脚本加 `--proxy` |
| `登录态无效（无 web_session cookie）`（小红书） | 重跑 `--login`，确认页面右上角出现头像再回终端按回车 |
| `当日预算不足`（头条） | 设计行为——明天再跑，或检查当日日志确认篇数 |
| 转发 ingest 返回 `parser_changed` | 看失败信封落盘 `envelopes/`，把结构反馈维护者校准解析器 |
| `命中即停条件：...` | **按协议当日停止**，别再跑；记下原因，明天观察或决定结束实验 |
| "最多 5 个查询 / 间隔不得低于 60s" | 硬限生效，这是设计行为 |

## 常见问题速查

| 现象 | 处理 |
|---|---|
| `缺少 playwright` | 依赖装到了别的 Python——确认用 `..\signalx-agent\.venv\Scripts\python.exe` 全路径 |
| `导航超时（30s）` | 脚本默认**绕过系统代理直连**（本机常驻代理会把国内站点路由到远端节点）；若你的网络必须走代理，加 `--proxy http://127.0.0.1:7890` |
| `登录态无效（无 web_session cookie）` | 重跑 `--login`，确认页面右上角出现头像再回终端按回车 |
| `未拦截到搜索响应` + 接口诊断列表 | 搜索接口改版，把打印的接口列表发给维护者校准拦截规则 |
| `命中即停条件：...` | **按协议当日停止**，别再跑；记下原因，明天观察或决定结束实验 |
| "最多 5 个查询 / 间隔不得低于 60s" | 硬限生效，这是设计行为 |
