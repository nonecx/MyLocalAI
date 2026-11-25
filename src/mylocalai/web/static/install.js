/* ============================================================
   install.js —— 「添加到桌面」快捷入口（PWA 安装引导）
   手机端 / 笔记本用户的快捷入口就靠这一层：
     1) 页面里常驻一个「＋ 添加到桌面」按钮（已装到桌面/全屏运行时自动隐藏）；
     2) 浏览器给了 beforeinstallprompt（Chrome/Edge 系、localhost 或 HTTPS）时
        点按钮直接弹系统安装框；
     3) 没有该事件时（iOS Safari、或手机通过局域网 http 打开）弹一份分平台的
        手动引导——照样能加到主屏，只是走浏览器菜单；
     4) 顺带注册 Service Worker（sw.js），让装好的图标冷启动更快、断网也有壳。

   为什么不在没有 HTTPS 时也强推安装：PWA 的"可安装"是浏览器按安全上下文
   判定的，局域网 http 地址只能走「添加到主屏幕」的普通快捷方式，
   这里如实说明而不是假装成功（本系统本地优先，不引入自签证书那套坑）。
   ============================================================ */
"use strict";

(function () {
  const $ = (sel) => document.querySelector(sel);

  /* 浏览器交给我们的一次性安装事件（用掉即失效） */
  let deferredPrompt = null;

  /* ---------- 环境判定 ---------- */
  function isStandalone() {
    /* Android/桌面 Chrome 看 display-mode；iOS 用 navigator.standalone */
    if (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) return true;
    if (window.matchMedia && window.matchMedia("(display-mode: fullscreen)").matches) return true;
    return window.navigator.standalone === true;
  }

  function ua() {
    return (navigator.userAgent || "").toLowerCase();
  }

  function isIOS() {
    const u = ua();
    if (/iphone|ipod/.test(u)) return true;
    /* iPadOS 13+ 默认伪装成 macOS，靠触摸点区分 */
    return /ipad/.test(u) || (/macintosh/.test(u) && (navigator.maxTouchPoints || 0) > 1);
  }

  function isAndroid() {
    return /android/.test(ua());
  }

  function isDesktopBrowser() {
    return !isIOS() && !isAndroid();
  }

  /* ---------- 弹层 ---------- */
  function setOpen(open) {
    const dialog = $("#installDialog");
    const btn = $("#installBtn");
    if (!dialog) return;
    dialog.classList.toggle("hidden", !open);
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      const close = $("#installCloseBtn");
      if (close) close.focus();
    } else if (btn) {
      btn.focus();
    }
  }

  /* 引导正文：分平台给步骤，全部 textContent 渲染（与其它弹层同一纪律） */
  function fillGuide() {
    const body = $("#installBody");
    if (!body) return;
    while (body.firstChild) body.removeChild(body.firstChild);

    const add = (tag, text, cls) => {
      const node = document.createElement(tag);
      if (text) node.textContent = text;
      if (cls) node.className = cls;
      body.appendChild(node);
      return node;
    };

    const current = add("p", null, "install-lead");
    current.appendChild(document.createTextNode("当前地址："));
    const code = document.createElement("code");
    code.textContent = location.origin;
    current.appendChild(code);

    let steps = [];
    if (isIOS()) {
      steps = [
        "在 Safari 底部点「分享」按钮（方框加向上箭头）。",
        "在弹出菜单里往下滑，选「添加到主屏幕」。",
        "确认名称后点右上角「添加」——桌面会出现图标，点开即是全屏应用。"
      ];
    } else if (isAndroid()) {
      steps = [
        "点浏览器右上角的「⋮」菜单。",
        "选「添加到主屏幕」或「安装应用」。",
        "确认后桌面会出现图标，点开即是全屏应用，无需再输网址。"
      ];
    } else {
      steps = [
        "看地址栏右侧是否有安装图标（⊕ 或显示器形状），有就直接点。",
        "没有的话打开浏览器菜单（Chrome/Edge 的「⋮」），选「安装 MyLocalAI」或「应用 → 安装此站点」。",
        "装好后从开始菜单/程序坞启动，打开即是独立窗口，不用再找网址。"
      ];
    }

    const ol = add("ol", null, "install-steps");
    steps.forEach((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      ol.appendChild(li);
    });

    /* 手机端最容易卡住的一步：服务只监听本机地址，手机根本连不上。
       这里把"该怎么办"讲清楚，而不是让用户对着 127.0.0.1 发呆。 */
    add("p", "手机 / 平板上第一次打开：需要电脑和手机连同一个 Wi-Fi，"
      + "并用 my_website/start.bat --lan（或 start.sh --lan）启动服务，"
      + "然后在手机浏览器输入控制台打印的局域网地址（形如 http://192.168.x.x:8000）。"
      + "加进主屏之后，以后点图标即可，不必再输。", "install-note");

    add("p", "提示：iOS Safari 与局域网 http 地址只支持「添加到主屏幕」这种快捷方式；"
      + "在 localhost 或 HTTPS 下，Chrome/Edge 还会把它装成完整的 PWA（可离线打开外壳）。",
      "install-note");
  }

  /* ---------- 按钮状态 ---------- */
  function syncButton() {
    const btn = $("#installBtn");
    if (!btn) return;
    /* 已经是全屏应用（从桌面图标点进来的）→ 不再提供"添加到桌面" */
    if (isStandalone()) {
      btn.classList.add("hidden");
      return;
    }
    btn.classList.remove("hidden");
    btn.textContent = deferredPrompt ? "＋ 一键添加桌面" : "＋ 添加到桌面";
  }

  function onClick() {
    if (deferredPrompt) {
      const prompt = deferredPrompt;
      deferredPrompt = null;               /* 事件只能用一次 */
      prompt.prompt();
      prompt.userChoice.then((choice) => {
        syncButton();
        if (choice && choice.outcome === "accepted") {
          /* 系统会自动装好，成功后 appinstalled 会把按钮藏掉 */
          setOpen(false);
        }
      }).catch(() => syncButton());
      return;
    }
    fillGuide();
    setOpen(true);
  }

  function bind() {
    const btn = $("#installBtn");
    const dialog = $("#installDialog");
    const close = $("#installCloseBtn");
    if (btn) {
      btn.setAttribute("aria-expanded", "false");
      btn.addEventListener("click", onClick);
    }
    if (close) close.addEventListener("click", () => setOpen(false));
    if (dialog) {
      /* 点遮罩空白关闭，点卡片本体不关（与 about/settings 弹层一致） */
      dialog.addEventListener("click", (e) => {
        if (e.target === dialog) setOpen(false);
      });
    }
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (dialog && !dialog.classList.contains("hidden")) setOpen(false);
    });

    window.addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault();                  /* 拦下，改由我们的按钮触发 */
      deferredPrompt = e;
      syncButton();
    });

    window.addEventListener("appinstalled", () => {
      deferredPrompt = null;
      setOpen(false);
      const b = $("#installBtn");
      if (b) b.classList.add("hidden");
    });

    /* 从桌面图标启动/切回时重算一次（用户可能刚装好） */
    if (window.matchMedia) {
      const mq = window.matchMedia("(display-mode: standalone)");
      if (mq.addEventListener) mq.addEventListener("change", syncButton);
      else if (mq.addListener) mq.addListener(syncButton);
    }

    syncButton();
  }

  /* ---------- Service Worker ---------- */
  function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    if (location.protocol !== "http:" && location.protocol !== "https:") return;
    /* 先等页面空闲再注册：不抢首屏带宽，也不影响首屏可交互时间 */
    const start = () => {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {
        /* 注册失败只是少了离线壳，不影响任何功能，静默即可 */
      });
    };
    if (document.readyState === "complete") {
      setTimeout(start, 0);
    } else {
      window.addEventListener("load", () => setTimeout(start, 0), { once: true });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    bind();
    registerServiceWorker();
  });
})();
