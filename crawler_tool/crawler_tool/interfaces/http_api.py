from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from typing import Callable, Literal

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from crawler_tool.pagination import PaginationError

from crawler_tool.application import CrawlService
from crawler_tool.application.capture_ingest_service import CaptureIngestService
from crawler_tool.application.recent_store import RecentItemsStore
from crawler_tool.domain import CaptureIngestRequest, ContentSearchRequest, WechatKeywordSearchRequest
from crawler_tool.tools import IntelligenceTools

# Platforms eligible for session-view filtering: registered sources plus manual-capture ones.
_VIEW_PLATFORMS = Literal[
    "south_weekend", "toutiao", "weibo", "wechat", "xiaohongshu", "douyin",
    "sogou_wechat", "cctv_news"
]

# Platforms covered by the manual capture queue (browser-helper assisted).
_CAPTURE_QUEUE_PLATFORMS = ("xiaohongshu", "douyin")

# Browser capture helper forwards data from these pages only; scoped narrowly
# so that other web origins can neither call nor preflight the local Tool.
_CAPTURE_ORIGINS = ("https://www.xiaohongshu.com", "https://www.douyin.com")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


def _capture_cors_headers(request: Request) -> dict[str, str]:
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host not in _LOOPBACK_HOSTS:
        return {}
    origin = request.headers.get("origin") or ""
    if origin not in _CAPTURE_ORIGINS:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }


def _captured_items_csv(items: list) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "contentId", "platform", "sourceItemId", "title", "author", "url",
        "publishedAt", "keyword", "likeCount", "commentCount", "shareCount",
        "collectCount", "viewCount",
    ])
    for item in items:
        ingest = item.ext.get("captureIngest") if isinstance(item.ext, dict) else None
        metrics = item.metrics
        writer.writerow([
            item.content_id,
            item.platform,
            item.source_item_id,
            item.title,
            item.author.name if item.author else None,
            str(item.canonical_url or item.url),
            item.published_at.isoformat() if item.published_at else None,
            (ingest or {}).get("keyword"),
            metrics.like_count,
            metrics.comment_count,
            metrics.share_count,
            metrics.collect_count,
            metrics.view_count,
        ])
    # Excel-friendly UTF-8 BOM so Chinese titles survive double-click open.
    return "﻿" + buffer.getvalue()


_VIEWER_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>SignalX 捕获视图</title>
<style>
 body{font-family:system-ui,"Segoe UI",sans-serif;margin:24px;background:#fafafa;color:#222}
 h1{font-size:20px;margin:0 0 16px}.meta{color:#666;font-size:12px;font-weight:normal}
 .bar{display:flex;gap:14px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
 select,input{padding:6px 10px;border:1px solid #ccc;border-radius:6px;font-size:13px}
 button{padding:6px 14px;border:none;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer;font-size:13px}
 a{color:#2563eb;text-decoration:none}
 table{border-collapse:collapse;width:100%;background:#fff}
 th,td{border:1px solid #e5e7eb;padding:8px 10px;text-align:left;font-size:13px;vertical-align:top}
 th{background:#f3f4f6}tr:hover td{background:#f9fafb}
 td.numeric{white-space:nowrap;color:#555;font-size:12px}
</style></head>
<body>
<h1>内容视图 <span class="meta">全来源 · 会话级（重启清空）· 含各来源搜索结果与手动捕获</span></h1>
<div class="bar">
 平台 <select id="platform"><option value="">全部</option><option value="south_weekend">南方周末</option><option value="toutiao">头条</option><option value="weibo">微博</option><option value="wechat">微信公众号</option><option value="xiaohongshu">小红书</option><option value="douyin">抖音</option><option value="sogou_wechat">搜狗微信</option><option value="cctv_news">央视网</option></select>
 过滤 <input id="q" size="24" placeholder="标题/作者包含…（回车生效）">
 条数 <input id="limit" type="number" value="50" min="1" max="200" style="width:70px">
 自动刷新 <select id="refresh"><option value="0">关闭</option><option value="15">15 秒</option><option value="60">60 秒</option></select>
 <button onclick="load()">刷新</button>
 <a href="/capture">→ 捕获队列</a>
 <a id="csv" href="#" download>导出 CSV</a>
 <a id="jsn" href="#" download>导出 JSON</a>
</div>
<p class="meta" id="count"></p>
<table><thead><tr><th>#</th><th>平台</th><th>标题</th><th>作者</th><th>时间(UTC)</th><th>赞/评/转/藏/播</th><th>链接</th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
let timer=null;
function esc(t){const d=document.createElement('div');d.textContent=t==null?'':String(t);return d.innerHTML;}
async function load(){
 const p=document.getElementById('platform').value;
 const q=document.getElementById('q').value.trim().toLowerCase();
 const limit=document.getElementById('limit').value||'50';
 const data=await (await fetch('/api/v1/tool/captured-items?limit='+encodeURIComponent(limit)+(p?'&platform='+p:''))).json();
 const rows=document.getElementById('rows');rows.innerHTML='';
 let shown=0;
 for(const it of data.items){
   const hay=((it.title||'')+((it.author&&it.author.name)||'')).toLowerCase();
   if(q && !hay.includes(q))continue;
   shown++;
   const m=it.metrics||{};
   rows.insertAdjacentHTML('beforeend',
    '<tr><td>'+shown+'</td><td>'+esc(it.platform)+'</td><td>'+esc(it.title)+'</td>'+
    '<td>'+esc(it.author&&it.author.name)+'</td>'+
    '<td>'+esc((it.publishedAt||'').replace('T',' ').slice(0,16))+'</td>'+
    '<td class="numeric">'+[m.likeCount,m.commentCount,m.shareCount,m.collectCount,m.viewCount].map(v=>v==null?'-':v).join(' / ')+'</td>'+
    '<td><a href="'+esc(it.url)+'" target="_blank" rel="noopener">打开</a></td></tr>');
 }
 document.getElementById('count').textContent='匹配 '+shown+' 条 / 库存 '+data.count+' 条';
 document.getElementById('csv').href='/api/v1/tool/captured-items/export?format=csv&limit='+encodeURIComponent(limit)+(p?'&platform='+p:'');
 document.getElementById('jsn').href='/api/v1/tool/captured-items/export?format=json&limit='+encodeURIComponent(limit)+(p?'&platform='+p:'');
}
document.getElementById('platform').onchange=load;
document.getElementById('limit').onchange=load;
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')load();});
document.getElementById('refresh').onchange=e=>{if(timer){clearInterval(timer);timer=null;}const s=+e.target.value;if(s)timer=setInterval(load,s*1000);};
load();
</script></body></html>"""

_CAPTURE_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>SignalX 捕获队列</title>
<style>
 body{font-family:system-ui,"Segoe UI",sans-serif;margin:24px;background:#fafafa;color:#222}
 h1{font-size:20px;margin:0 0 16px}.meta{color:#666;font-size:12px;font-weight:normal}
 .bar{display:flex;gap:14px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
 input,select{padding:6px 10px;border:1px solid #ccc;border-radius:6px;font-size:13px}
 button{padding:6px 14px;border:none;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer;font-size:13px}
 button.ghost{background:#6b7280}
 a{color:#2563eb;text-decoration:none}
 .task{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:10px 14px;margin-bottom:10px;
       display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 .task b{min-width:160px;display:inline-block}
 .badge{font-size:12px;padding:3px 8px;border-radius:10px;background:#eef2ff}
 .badge.ok{background:#dcfce7;color:#166534}
 .badge.stale{background:#fee2e2;color:#991b1b}
 .open{font-size:13px;margin-left:auto}
 .del{background:#9ca3af;padding:2px 8px;font-size:12px}
 .taskhead{display:flex;align-items:center;gap:8px;min-width:220px}
 #count{margin:8px 0}
</style></head>
<body>
<h1>捕获任务队列 <span class="meta">人工浏览的唯一入口页 · 任务存在本浏览器 · 平台收到的一切请求都由你触发</span></h1>
<div class="bar">
 <input id="kw" size="24" placeholder="输入关键词后回车添加">
 <select id="maxage"><option value="1">1 天内算新鲜</option><option value="3" selected>3 天内算新鲜</option><option value="7">7 天内算新鲜</option></select>
 <button onclick="render()">刷新状态</button>
 <button class="ghost" onclick="copyStale()">复制全部未覆盖链接</button>
 <a href="/view">→ 内容视图</a>
</div>
<p class="meta" id="count"></p>
<div id="rows"></div>
<script>
const PLATFORMS=[["xiaohongshu","小红书"],["douyin","抖音"]];
function esc(t){const d=document.createElement('div');d.textContent=t==null?'':String(t);return d.innerHTML;}
function loadTasks(){try{return JSON.parse(localStorage.getItem('signalx_capture_tasks')||'[]');}catch(e){return [];}}
function saveTasks(t){localStorage.setItem('signalx_capture_tasks',JSON.stringify(t));}
function searchUrl(p,kw){const e=encodeURIComponent(kw);
 if(p==='xiaohongshu')return 'https://www.xiaohongshu.com/search_result?keyword='+e+'&source=web_explore_feed';
 return 'https://www.douyin.com/search/'+e+'?type=general';}
function daysAgoText(iso){const d=(Date.now()-new Date(iso).getTime())/86400000;
 if(d<1)return Math.max(1,Math.round(d*24))+'小时前';return Math.round(d)+'天前';}
async function render(){
 const tasks=loadTasks();const days=+document.getElementById('maxage').value||3;
 const box=document.getElementById('rows');box.innerHTML='';
 if(!tasks.length){box.innerHTML='<p class="meta">队列为空：在上方输入关键词后回车添加；任务只保存在本浏览器。</p>';return;}
 const url='/api/v1/tool/capture-status?keywords='+encodeURIComponent(tasks.join(','))+'&max_age_days='+days;
 const data=await (await fetch(url)).json();
 const staleLinks=[];let staleWords=0;
 for(const row of data.keywords){
   const div=document.createElement('div');div.className='task';
   const head=document.createElement('span');head.className='taskhead';
   const name=document.createElement('b');name.textContent=row.keyword;
   const del=document.createElement('button');del.textContent='×';del.className='del';del.title='移出队列';
   del.onclick=()=>{saveTasks(loadTasks().filter(t=>t!==row.keyword));render();};
   head.appendChild(name);head.appendChild(del);div.appendChild(head);
   let wordStale=false;
   for(const [pid,label] of PLATFORMS){
     const st=row.platforms[pid]||{};
     const badge=document.createElement('span');
     badge.className='badge '+(st.fresh?'ok':'stale');
     badge.textContent=label+': '+(st.count?(st.fresh?'✅ 新鲜':'⏰ '+daysAgoText(st.lastCaptured)):'✗ 未捕获');
     div.appendChild(badge);
     const a=document.createElement('a');a.href=searchUrl(pid,row.keyword);
     a.target='_blank';a.rel='noopener';a.textContent='打开'+label;a.className='open';
     div.appendChild(a);
     if(!st.fresh){staleLinks.push(a.href);wordStale=true;}
   }
   if(wordStale)staleWords++;
   box.appendChild(div);
 }
 window.__staleLinks=staleLinks;
 document.getElementById('count').textContent=tasks.length+' 个词 · '+staleWords+' 个词存在未覆盖平台（红/黄项点开即补，助手脚本会自动入库）';
}
function copyStale(){
 const links=window.__staleLinks||[];
 if(!links.length){alert('没有未覆盖的链接，都是新鲜的。');return;}
 navigator.clipboard.writeText(links.join('\\n')).then(()=>alert('已复制 '+links.length+' 个链接，可粘贴到浏览器批量打开。'));
}
document.getElementById('kw').addEventListener('keydown',e=>{
 if(e.key!=='Enter')return;
 const v=e.target.value.trim();if(!v)return;
 const tasks=loadTasks();if(!tasks.includes(v))tasks.push(v);
 saveTasks(tasks);e.target.value='';render();
});
document.getElementById('maxage').onchange=render;
render();
</script></body></html>"""


def create_app(service_factory: Callable[[], CrawlService]) -> FastAPI:
    """Create the focused crawler Tool MVP HTTP application."""
    app = FastAPI(title="SignalX Crawler Tool", version="0.1.0")
    service = service_factory()
    tool = IntelligenceTools(service)
    # 全来源共享的会话视图：网络搜索、微信关键词、手动捕获写入同一实例；
    # 组合根未接好时就地补装，保证 /view 与导出总有数据源。
    store = getattr(service, "recent_store", None)
    if store is None:
        store = RecentItemsStore()
        service.recent_store = store
    capture_service = getattr(service, "capture_ingest_service", None)
    if capture_service is None:
        capture_service = CaptureIngestService(store=store)
        service.capture_ingest_service = capture_service

    @app.exception_handler(PaginationError)
    async def pagination_error(_: Request, exc: PaginationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": {"code": exc.code, "message": "invalid pagination cursor"}})

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "crawler_tool"}

    @app.get("/api/v1/tool/source-health")
    def source_health():
        payload = tool.get_source_health().model_dump_json()
        return Response(content=payload, media_type="application/json; charset=utf-8")

    @app.post("/api/v1/tool/search-content")
    def search_content(request: ContentSearchRequest):
        payload = tool.search_content(request).model_dump_json(by_alias=True)
        return Response(content=payload, media_type="application/json; charset=utf-8")

    @app.post("/api/v1/tool/search-wechat-articles")
    def search_wechat_articles(request: WechatKeywordSearchRequest):
        payload = tool.search_wechat_articles(request).model_dump_json(by_alias=True)
        return Response(content=payload, media_type="application/json; charset=utf-8")

    @app.post("/api/v1/tool/ingest-capture")
    def ingest_capture(capture_request: CaptureIngestRequest, request: Request):
        payload = capture_service.ingest(capture_request).model_dump_json(by_alias=True)
        return Response(content=payload, media_type="application/json; charset=utf-8", headers=_capture_cors_headers(request))

    @app.api_route("/api/v1/tool/ingest-capture", methods=["OPTIONS"])
    def ingest_capture_preflight(request: Request) -> Response:
        return Response(status_code=204, headers=_capture_cors_headers(request))

    # Backward-compatible alias; the xiaohongshu envelope is auto-detected.
    @app.post("/api/v1/tool/ingest-xhs-capture")
    def ingest_xhs_capture(capture_request: CaptureIngestRequest, request: Request):
        payload = capture_service.ingest(capture_request).model_dump_json(by_alias=True)
        return Response(content=payload, media_type="application/json; charset=utf-8", headers=_capture_cors_headers(request))

    @app.api_route("/api/v1/tool/ingest-xhs-capture", methods=["OPTIONS"])
    def ingest_xhs_capture_preflight(request: Request) -> Response:
        return Response(status_code=204, headers=_capture_cors_headers(request))

    @app.get("/api/v1/tool/captured-items")
    def captured_items(
        platform: _VIEW_PLATFORMS | None = None,
        keyword: str | None = Query(default=None, min_length=1, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        items = store.query(platform=platform, keyword=keyword, limit=limit)
        payload = {
            "count": len(items),
            "items": [item.model_dump(mode="json", by_alias=True) for item in items],
        }
        return JSONResponse(content=payload)

    @app.get("/view")
    def viewer() -> HTMLResponse:
        return HTMLResponse(content=_VIEWER_HTML)

    @app.get("/api/v1/tool/capture-status")
    def capture_status(
        keywords: str = Query(..., min_length=1, max_length=2000, description="逗号分隔的关键词列表"),
        max_age_days: int = Query(default=3, ge=1, le=30),
    ):
        """Per-keyword, per-platform freshness over the full session store.

        Backs the /capture task queue: the page shows which keyword/platform
        pairs still need a manual search today.
        """
        now = datetime.now(timezone.utc)
        threshold = timedelta(days=max_age_days)
        words = [word.strip() for word in keywords.split(",") if word.strip()][:50]
        report = []
        for word in words:
            platforms = {}
            for platform in _CAPTURE_QUEUE_PLATFORMS:
                items = store.query(platform=platform, keyword=word, limit=200)
                latest = items[0].collected_at if items else None
                platforms[platform] = {
                    "count": len(items),
                    "lastCaptured": latest.isoformat() if latest else None,
                    "fresh": bool(latest and (now - latest) <= threshold),
                }
            report.append({"keyword": word, "platforms": platforms})
        return JSONResponse(content={"maxAgeDays": max_age_days, "keywords": report})

    @app.get("/capture")
    def capture_queue() -> HTMLResponse:
        return HTMLResponse(content=_CAPTURE_HTML)

    @app.get("/api/v1/tool/captured-items/export")
    def exported_items(
        format: Literal["json", "csv"] = "json",
        platform: _VIEW_PLATFORMS | None = None,
        keyword: str | None = Query(default=None, min_length=1, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        items = store.query(platform=platform, keyword=keyword, limit=limit)
        if format == "csv":
            return Response(
                content=_captured_items_csv(items),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=captured_items.csv"},
            )
        payload = {
            "count": len(items),
            "items": [item.model_dump(mode="json", by_alias=True) for item in items],
        }
        return JSONResponse(
            content=payload,
            headers={"Content-Disposition": "attachment; filename=captured_items.json"},
        )

    return app
