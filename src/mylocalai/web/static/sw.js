/* ============================================================
   sw.js —— Service Worker（只服务"快捷入口"这一件事）
   目标：装到桌面/主屏后，点图标立刻出界面壳，断网或后端还没起时
   也不会看到浏览器的空白错误页。

   两条纪律（都与本项目的本地优先定位有关）：
     1) 一律 network-first：本机服务就在旁边，网络几乎总是可用，
        优先拿最新文件，避免升级后吃到旧 CSS/JS（同 /static 的 no-cache 约定）；
        只有网络失败时才回落到缓存。
     2) 绝不碰 /api/*：对话与档案是实时数据，缓存它们会给出过期结论，
        比"打不开"更危险。非 GET、跨域、非 http(s) 一律直接放行。

   缓存清单只用相对路径之外的同源绝对路径，避免 scope 推断出错。
   ============================================================ */
"use strict";

const CACHE_NAME = "mylocalai-shell-v1";

/* 界面壳（离线时也要能画出来的那几张）：页面、样式、脚本、图标 */
const SHELL_ASSETS = [
  "/",
  "/manifest.webmanifest",
  "/static/style.css",
  "/static/chat.css",
  "/static/platform.css",
  "/static/settings.css",
  "/static/profiles.css",
  "/static/ui-polish.css",
  "/static/mobile.css",
  "/static/shell.js",
  "/static/install.js",
  "/static/settings.js",
  "/static/cards.js",
  "/static/chat.js",
  "/static/profiles.js",
  "/static/batch.js",
  "/static/monitor.js",
  "/static/platform.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png"
];

/* 预缓存用 allSettled：任何一个资源缺失都不该让整个安装失败
   （否则 Service Worker 永远装不上，快捷入口也就没了） */
self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await Promise.allSettled(
      SHELL_ASSETS.map((url) => cache.add(new Request(url, { cache: "reload" })))
    );
    await self.skipWaiting();
  })());
});

/* 新版本接管：清掉旧壳缓存，立即 claim 已有页面 */
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter((name) => name.startsWith("mylocalai-shell-") && name !== CACHE_NAME)
      .map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

function _isCacheable(request, url) {
  if (request.method !== "GET") return false;
  if (url.origin !== self.location.origin) return false;
  if (url.pathname.startsWith("/api/")) return false;   // 实时数据永不缓存
  if (url.pathname.startsWith("/docs")) return false;   // 接口文档不进壳
  if (url.pathname.startsWith("/openapi.json")) return false;
  return true;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  let url;
  try {
    url = new URL(request.url);
  } catch (e) {
    return;
  }
  if (!_isCacheable(request, url)) return;             // 交给浏览器默认行为

  event.respondWith((async () => {
    try {
      const fresh = await fetch(request);
      /* 200 才写缓存：重定向/错误页不能污染壳 */
      if (fresh && fresh.ok && fresh.type === "basic") {
        const copy = fresh.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {});
      }
      return fresh;
    } catch (err) {
      const cached = await caches.match(request, { ignoreSearch: true });
      if (cached) return cached;
      /* 导航请求（地址栏/桌面图标点进来）兜底到缓存的首页壳 */
      if (request.mode === "navigate") {
        const shell = await caches.match("/");
        if (shell) return shell;
      }
      throw err;
    }
  })());
});
