/* ============================================================
   shell.js —— 页面外壳（单页对话）
   原 app.js 只保留两件与"页面能不能用"有关的事：
     1) file:// 双击打开时的启动引导（否则用户看到的是一片无数据的裸页面）；
     2) GET /api/health 点亮顶栏服务状态 chip（id=apiStatus）。
   另外承担顶栏「关于」与「设置」两个弹层的开合——它们属于页面外壳交互，
   不属于对话业务，因此不塞进 chat.js（chat.js 只处理消息流与 SSE）；
   设置面板的内容与状态在 settings.js（window.SettingsPanel），这里只做绑定。
   工具模式（分析页）与参谋 Tab 已合并进对话单页，相关逻辑随之删除。
   ============================================================ */
"use strict";

(function () {
  /* ---------- file:// 双击打开检测 ----------
     页面资源用相对路径引入，因此直接双击 index.html 时本脚本仍会执行；
     但所有接口都依赖后端服务，此时给出明确的启动引导而不是一片裸页面。 */
  if (location.protocol === "file:") {
    const banner = document.createElement("div");
    banner.className = "file-protocol-banner";
    banner.innerHTML = `
      <h2>⚠️ 检测到你直接双击打开了本页面</h2>
      <p>本网站需要后端服务支持（分析与历史数据都来自接口），请任选其一：</p>
      <p><b>方法一（推荐）：</b>双击运行 <b>my_website/start.bat</b>，它会自动启动服务并打开浏览器；</p>
      <p><b>方法二：</b>在命令行进入项目的 my_website 目录后执行（把下面的项目路径换成你的实际位置）：</p>
      <pre>&lt;项目目录&gt;/.venv/Scripts/python.exe -m uvicorn mylocalai.web.app:app --host 127.0.0.1 --port 8000</pre>
      <p>然后访问 <a href="http://127.0.0.1:8000">http://127.0.0.1:8000</a></p>`;
    document.body.prepend(banner);
    /* 原 app.js 在 file:// 下 throw 终止整个脚本；这里 return 只结束本文件，
       等价地不再发起健康检查——cards.js / chat.js 各自有 file: 守卫，不会误发请求。 */
    return;
  }

  const $ = (sel) => document.querySelector(sel);

  const API = { health: "/api/health" };

  /* ---------- 服务状态 ----------
     两维健康度（P0-2 密钥健康度显性化）：/api/health 除服务活性外还带 llm 段
     （{usable, provider, model, base_url, error}）。服务活着但 llm.usable=false
     （密钥没配好）时，所有分析都会静默降级——只报"服务运行中"会让用户误以为
     一切就绪，所以这里把 chip 变成琥珀警示态（.status-chip.warn，样式在 chat.css，
     跨文件约定）并在 title 里说明后果。纯 textContent/className 更新：
     chip 上不拼 HTML，既有可访问性属性（结构在 index.html）原样保留。 */
  async function checkHealth() {
    const chip = $("#apiStatus");
    if (!chip) return;
    try {
      const resp = await fetch(API.health);
      if (!resp.ok) throw new Error("bad status");
      const health = await resp.json().catch(() => null);
      const llm = (health && health.llm && typeof health.llm === "object") ? health.llm : null;
      if (llm && llm.usable === false) {
        chip.textContent = "● 服务运行中 · 模型未配置";
        chip.className = "status-chip warn";
        chip.title = "分析将以降级模式运行：证据发现不可用";
        return;
      }
      chip.textContent = "● 服务运行中" + (llm && llm.usable && llm.provider ? " · " + llm.provider : "");
      chip.className = "status-chip ok";
      chip.removeAttribute("title");
    } catch (e) {
      chip.textContent = "● 服务不可用";
      chip.className = "status-chip bad";
      chip.removeAttribute("title");
    }
  }

  /* ---------- 顶栏「关于」弹层 ----------
     关闭途径三条：关闭按钮 / 点遮罩 / Esc——弹层是说明文本，不做焦点陷阱，
     但 aria-hidden 与 aria-expanded 跟着状态走，读屏器能感知开合。 */
  function setAboutOpen(open) {
    const dialog = $("#aboutDialog");
    const btn = $("#aboutBtn");
    if (!dialog) return;
    dialog.classList.toggle("hidden", !open);
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      const close = $("#aboutCloseBtn");
      if (close) close.focus();
    } else if (btn) {
      btn.focus();
    }
  }

  function bindAbout() {
    const btn = $("#aboutBtn");
    const dialog = $("#aboutDialog");
    const close = $("#aboutCloseBtn");
    if (!btn || !dialog) return;
    btn.setAttribute("aria-expanded", "false");
    btn.addEventListener("click", () => setAboutOpen(dialog.classList.contains("hidden")));
    if (close) close.addEventListener("click", () => setAboutOpen(false));
    /* 点遮罩空白处关闭（点卡片本体不关） */
    dialog.addEventListener("click", (e) => {
      if (e.target === dialog) setAboutOpen(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !dialog.classList.contains("hidden")) setAboutOpen(false);
    });
  }

  /* ---------- 顶栏「设置」弹层 ----------
     设置面板的内容、渲染与开合状态都属于 settings.js（它才知道面板里画了什么），
     这里只照 bindAbout 的写法把三条关闭途径和顶栏按钮接上去：
     按钮 / 关闭按钮 / 点遮罩 / Esc。故意不在这里碰 #settingsPane，
     免得"外壳"和"面板"两处同时维护同一份状态。 */
  function bindSettings() {
    const btn = $("#settingsBtn");
    const dialog = $("#settingsDialog");
    const close = $("#settingsCloseBtn");
    if (!btn || !dialog) return;
    /* 每次都从 window 上取：脚本顺序变化时不会绑到一个还不存在的对象 */
    const panel = () => window.SettingsPanel;
    btn.setAttribute("aria-expanded", "false");

    btn.addEventListener("click", () => {
      const api = panel();
      if (!api) return;
      if (api.isOpen()) api.close();
      else api.open();
    });
    if (close) {
      close.addEventListener("click", () => {
        const api = panel();
        if (api) api.close();
      });
    }
    /* 点遮罩空白处关闭（点卡片本体不关），与 aboutDialog 一致 */
    dialog.addEventListener("click", (e) => {
      if (e.target !== dialog) return;
      const api = panel();
      if (api) api.close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const api = panel();
      if (api && api.isOpen()) api.close();
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    checkHealth();
    bindAbout();
    bindSettings();
  });
})();
