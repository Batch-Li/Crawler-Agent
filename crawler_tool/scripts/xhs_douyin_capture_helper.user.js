// ==UserScript==
// @name         SignalX capture helper (xiaohongshu / douyin / wechat)
// @namespace    crawler_tool
// @version      0.3.0
// @description  把你浏览时平台已经返回给你的公开搜索/列表响应，被动转发一份到本地 ingest 接口。只复制"已收到的数据"，绝不代替你向平台发出任何新请求。
// @match        https://www.xiaohongshu.com/*
// @match        https://www.douyin.com/*
// @match        https://mp.weixin.qq.com/*
// @connect      127.0.0.1
// @connect      localhost
// @grant        GM_xmlhttpRequest
// @run-at       document-start
// ==/UserScript==
//
// 用法（任选其一）：
//   1. Tampermonkey：新建脚本粘贴整个文件；建议同时把该脚本的「注入模式」设为即时(Instant)，
//      且浏览器扩展页保持开发者模式开启；
//   2. 控制台：F12 → Console 粘贴整个文件回车（每次刷新后需重跑）;
//   3. 书签栏 javascript 形式。
//
// 边界不变：只复制"已发给你本人看的响应副本"，从不代发新请求。

(function () {
    "use strict";

    var ENDPOINT = "http://127.0.0.1:8301/api/v1/tool/ingest-capture";
    var RULES = [
        // 版本号通配：新版前端已把搜索迁到 v2（so.xiaohongshu.com），规则不再绑死 v1。
        { test: /\/api\/sns\/web\/v\d+\/(search[^?]*|homefeed[^?]*)/, platform: "xiaohongshu" },
        { test: /\/aweme\/v1\/web\/(general\/search|search\/item|aweme\/post)\//, platform: "douyin" },
        // 公众平台后台：发表记录（appmsgpublish sub=list）与超链接对话框文章列表（appmsg action=list_ex）。
        // 两者都是登录者人工打开的 app_msg_list 信封；searchbiz 是账号搜索，不入库故不转发。
        { test: /\/cgi-bin\/(appmsgpublish|appmsg)\?/, platform: "wechat" },
    ];
    var seen = Object.create(null);
    var noticed = Object.create(null);

    // ---------- 转发与识别 ----------

    function currentKeyword() {
        var params = new URLSearchParams(window.location.search);
        var key = params.get("keyword") || params.get("kw") || params.get("q") || params.get("query");
        if (!key || !key.trim()) {
            var match = window.location.pathname.match(/\/search\/([^\/?#]+)/);
            if (match) {
                try {
                    key = decodeURIComponent(match[1]).replace(/\+/g, " ");
                } catch (err) {
                    key = match[1];
                }
            }
        }
        return key && key.trim() ? key.trim() : null;
    }

    function reportResult(platform, result) {
        var count = result.items ? result.items.length : 0;
        console.log("[capture] " + platform + " ingested ->", result.status,
            result.sourceReports && result.sourceReports[0] ? result.sourceReports[0].status : "",
            "items:", count);
        var titles = (result.items || []).slice(0, 5).map(function (item) { return item.title || item.sourceItemId; });
        if (titles.length) {
            console.log("[capture]   样例：", titles.join(" | "));
        }
    }

    function forward(url, text, platform) {
        if (seen[url]) return;
        seen[url] = true;
        var capture;
        try {
            capture = JSON.parse(text);
        } catch (err) {
            return;
        }
        var body = JSON.stringify({ platform: platform, keyword: currentKeyword(), capture: capture });
        if (typeof GM_xmlhttpRequest === "function") {
            GM_xmlhttpRequest({
                method: "POST",
                url: ENDPOINT,
                data: body,
                headers: { "Content-Type": "application/json" },
                onload: function (response) {
                    try {
                        reportResult(platform, JSON.parse(response.responseText));
                    } catch (err) {
                        console.warn("[capture] 本地返回异常：", response.status);
                    }
                },
                onerror: function () {
                    console.warn("[capture] 转发本地接口失败（服务启动了吗？）");
                },
            });
            return;
        }
        fetch(ENDPOINT, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: body,
        })
            .then(function (res) { return res.json(); })
            .then(function (result) { reportResult(platform, result); })
            .catch(function (err) {
                console.warn("[capture] 转发本地接口失败（服务启动了吗？）：", err && err.message);
            });
    }

    function classify(url) {
        for (var i = 0; i < RULES.length; i++) {
            if (RULES[i].test.test(url)) return RULES[i];
        }
        return null;
    }

    function noticeUnmatched(url) {
        try {
            if (!(/xiaohongshu\.com/.test(url) || /\/api\/sns\//.test(url))) return;
            var path = url.split("?")[0];
            if (!noticed[path]) {
                noticed[path] = true;
                console.log("[capture] 发现未覆盖的小红书接口（如需入库请把该路径反馈）：", path);
            }
        } catch (err) { /* 忽略 */ }
    }

    // ---------- 挂钩安装（可作用于多个 JS 世界）----------

    function handleResponseLike(url, textPromise) {
        var rule = classify(url);
        if (rule) {
            textPromise.then(function (text) { forward(url, text, rule.platform); }).catch(function () {});
        } else {
            noticeUnmatched(url);
        }
    }

    function hookWorld(worldRef) {
        if (!worldRef || worldRef.__signalxCaptureInstalled) return false;
        try { worldRef.__signalxCaptureInstalled = true; } catch (err) { /* 只读世界跳过 */ }

        // fetch
        try {
            var originalFetch = worldRef.fetch.bind(worldRef);
            worldRef.fetch = function () {
                var promise = originalFetch.apply(null, arguments);
                try {
                    var input = arguments[0];
                    var url = typeof input === "string" ? input : (input && input.url) || "";
                    if (url) handleResponseLike(url, promise.then(function (r) { return r.clone().text(); }));
                } catch (err) { /* 不影响页面 */ }
                return promise;
            };
        } catch (err) { /* 该世界不可写 */ }

        // XMLHttpRequest
        try {
            var proto = worldRef.XMLHttpRequest.prototype;
            var originalOpen = proto.open;
            var originalSend = proto.send;
            proto.open = function (method, url) {
                this.__captureUrl = typeof url === "string" ? url : String(url);
                return originalOpen.apply(this, arguments);
            };
            proto.send = function () {
                var xhr = this;
                xhr.addEventListener("load", function () {
                    try {
                        if (xhr.__captureUrl) handleResponseLike(xhr.__captureUrl, Promise.resolve(xhr.responseText));
                    } catch (err) { /* 忽略 */ }
                });
                return originalSend.apply(this, arguments);
            };
        } catch (err) { /* 同上 */ }

        return true;
    }

    var worlds = [window];
    try {
        // 用户脚本世界可能是独立沙箱，页面世界需显式补挂。
        if (typeof unsafeWindow !== "undefined" && unsafeWindow && unsafeWindow !== window) {
            worlds.unshift(unsafeWindow);
        }
    } catch (err) { /* 非 TM 环境忽略 */ }

    var installedCount = 0;
    worlds.forEach(function (w) { if (hookWorld(w)) installedCount++; });

    console.log("%c[capture] 已挂载（ worlds=" + installedCount +
        " ）你正常搜索即可，命中的列表数据会自动转投本地 ingest",
        "color:#2e7d32;font-weight:bold");
})();
