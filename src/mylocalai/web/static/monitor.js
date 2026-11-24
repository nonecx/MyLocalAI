/* ============================================================
   monitor.js —— 左栏「领域监测」面板（专业用户 · 领域监测）

   定位：role=monitor 的档案工作区才显示（window.ProfilesPanel.snapshot()
   .active.role === "monitor"；快照为空时隐藏）。订阅话题（关键词/域名/频率），
   立即运行或等调度线程到期跑检，看每轮运行历史与变化卡。

   接口契约（与 web/monitor_api.py 逐条对应；路径参数与后端同名 {sub_id}，
   生成的 URL 形状逐字一致）：
     - GET  /api/subscriptions        → {"subscriptions":[{id,topic,keywords[],
        domains[],frequency_s,enabled,last_run_at,next_run_at,last_status,
        paused_reason,created_at}, ...], "active_profile_id", "min_frequency_s",
        "max_domains"}（当前档案工作区；两个限制元数据喂给表单下方的
        「限制口径」行——见 LIMITS）
     - POST /api/subscriptions        JSON {topic,keywords[],domains[],frequency_s}
        → 200 {"subscription":{...}} / 400 {"detail":"..."}（主题空/过长、关键词>10、
        域名>3、频率<300——detail 原文照抄显示，不替后端改写）
     - PUT  /api/subscriptions/{id}   JSON {topic?,keywords?,domains?,frequency_s?,
        enabled?} → 200 {"subscription":{...}}；404 跨工作区
     - DELETE /api/subscriptions/{id} → 200 {"deleted":true,"id":N}；404 跨工作区
     - POST /api/subscriptions/{id}/run-now → 202 {"started":true,"id":N,"run_id":R}
        （R=预建运行行 id，轮询按它核对本轮终态）；409 {"detail":...} 两种：
        "该订阅正在运行中，请稍候"（正在跑）/
        "订阅已暂停，请先恢复启用后再手动运行（暂停原因：…）"（enabled=0，
        不隐式恢复）——detail 原文照抄显示；404 跨工作区
     - GET  /api/subscriptions/{id}   → {"subscription":{...}, "runs":[{id,
        started_at,finished_at,status,results_count,new_count,summary{...},
        error}], "card":{...}}（card 为最新一轮变化卡，无已完成轮次时 {}；
        summary 带 domain_scope_applied / scope_degraded 标注——site: 变体全空
        兜底时 scope_degraded=true，运行行据此显示灰色徽章）
     - GET  /api/subscriptions/{id}/runs → {"runs":[...]}

   运行是**后台线程**：run-now 返回 202 后本文件以 2s 间隔轮询详情（最多约 30
   次）。完成判据（R2 配合）：响应带 run_id 时优先核对"该轮运行行"状态已到终态
   才算完成；取不到 run_id 回退旧判据（subscription.last_status ∈ 终态——现在
   起跑即写 running，旧判据也不会把上一轮旧终态误读为本轮完成）。轮询期间订阅
   行会随 last_status='running' 显示「进行中」徽章 +「运行中…」按钮。超上限不
   无限等，如实提示等待时长并给下一步指引。

   U0 收口对接（window.ChatCards，cards.js 在 index.html 里先于本文件加载）：
     - apiPath —— URL 占位符拼装（本文件不再留本地副本）；
     - MONITOR_STATUS —— 单轮运行状态映射（本地 RUN_STATUS 只取引用）；
     - startProfilesPoll —— 等档案快照就绪的轮询（句柄存 state 防重入）；
     - mountCard —— 变化卡挂载（innerHTML 只在 cards.js 渲染层）。
     保留本地的：LAST_STATUS（订阅 last_status 映射，cards.js 无共享副本）、
     when（textContent 版；导出的 when 是 escape 版，语义不同）。

   纪律（同 settings.js / profiles.js / batch.js）：
     - 纯 vanilla JS、IIFE、"use strict"、无 ES module、无框架；
     - 用户输入一律 createElement + textContent；本文件没有任何 HTML 注入点
       （变化卡经 window.ChatCards.mountCard 挂载，innerHTML 只存在于
       cards.js 的渲染层，与 batch.js 挂载 batch_summary 同一模式）；
     - 外链不产生：监测结果 URL 是外部内容，变化卡里只做纯文本展示（cards.js
       renderMonitorChange 同口径），本文件更不生成任何链接；
     - 口径诚实：错误信息照抄后端 detail 原文；没有任何字段时不编造、不显示；
       删除等不可逆操作先 window.confirm（与 profiles.js 同一口径）。
   ============================================================ */
"use strict";

(function () {
  /* file:// 双击打开时 shell.js 已给出启动引导，这里静默退出 */
  if (location.protocol === "file:") return;

  const $ = (sel) => document.querySelector(sel);

  /* 后端接口清单（与 web/monitor_api.py 的路由一一对应；
      {sub_id} 与后端路径参数同名，生成的 URL 形状逐字一致） */
  const API = {
    subscriptions: "/api/subscriptions",
    detail: "/api/subscriptions/{sub_id}",
    runs: "/api/subscriptions/{sub_id}/runs",
    runNow: "/api/subscriptions/{sub_id}/run-now",
  };

  /* 运行频率 → 人类可读（选项值与 index.html 的 select 一致；未知值按秒数原样显示） */
  const FREQ_LABEL = { 1800: "30 分钟", 3600: "1 小时", 21600: "6 小时", 86400: "24 小时" };

  /* 表单限制口径的单一来源（index.html 的静态提示不改，动态行以这里的值为准）：
      默认值与后端 dossier/subscriptions.py 的 MIN_FREQUENCY_S=300 / MAX_DOMAINS=3 /
      MAX_KEYWORDS=10 / MAX_TOPIC=200 对齐；GET /api/subscriptions 已带
      min_frequency_s / max_domains 元数据，取到即覆盖默认值。后端未暴露
      max_keywords / max_topic，这两项只能本地维护（对齐后端常量，见最终报告）。 */
  const LIMITS = { minFrequencyS: 300, maxDomains: 3, maxKeywords: 10, maxTopic: 200 };

  /* run-now 轮询：2s 一次、最多 30 次（约 60s）。后台线程跑多久不取决于前端，
     超上限只如实提示，不假装完成、不无限等。 */
  const RUN_POLL_INTERVAL_MS = 2000;
  const RUN_POLL_MAX_TICKS = 30;

  /* 完成态集合：运行行 / last_status 变成这四个值之一即本轮收尾（契约口径） */
  const DONE_STATUS = ["ok", "error", "blocked", "partial"];

  /* 订阅 last_status → 徽章文案与配色（后端封闭枚举的客户端映射；未知值原样显示）。
      cards.js 没有这份映射的共享副本（U0 只收口了单轮运行的 MONITOR_STATUS），
      故保留本地——唯一保留的状态映射副本。 */
  const LAST_STATUS = {
    "": { text: "未运行", cls: "gray" },
    running: { text: "进行中", cls: "gray" },
    ok: { text: "已完成", cls: "green" },
    partial: { text: "部分完成", cls: "amber" },
    blocked: { text: "已拦截", cls: "amber" },
    error: { text: "失败", cls: "red" },
  };

  /* 单轮运行 status → 徽章文案与配色：U0 收口后唯一定义在 cards.js 的
      MONITOR_STATUS（5 键 ok/partial/blocked/error/running 同口径），这里只取
      引用不再留副本；收口层缺席时 statusMeta 的未知值兜底顶上。 */
  const RUN_STATUS = (window.ChatCards && window.ChatCards.MONITOR_STATUS) || {};

  const state = {
    visible: false,        // 面板当前是否可见（role=monitor）
    subs: [],              // 当前档案工作区的订阅列表
    selectedId: null,      // 详情里打开的订阅 id
    runningIds: new Set(), // 本端正被 run-now 轮询盯着的订阅（禁重复触发）
    pollTimers: {},        // sub_id → setTimeout 句柄（run-now 完成轮询）
    profilesPoll: null,    // 等档案快照就绪的轮询句柄（导出版返回的 interval id）
    creating: false,       // 新建表单提交中（防重复提交）
  };

  /* ---------- 小工具 ---------- */
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== null && text !== undefined) node.textContent = String(text);
    return node;
  }

  function apiPath(tpl, id) {
    /* U0 收口：统一走 window.ChatCards.apiPath（单值模式替换路径里全部 {..}
       占位符，{sub_id} 兼容，值一律 encodeURIComponent）；本文件不再留本地副本 */
    return window.ChatCards.apiPath(tpl, id);
  }

  function when(s) {
    return String(s || "").replace("T", " ") || "—";
  }

  function freqLabel(seconds) {
    const n = Number(seconds);
    if (!Number.isFinite(n) || n <= 0) return "—";
    return FREQ_LABEL[n] || `${n} 秒`;
  }

  function statusMeta(map, status) {
    return (map && map[String(status || "")]) || { text: String(status || "未知"), cls: "gray" };
  }

  /* 可点行的键盘支持（Enter / Space 与点击同效；Space 会滚页，必须吞默认行为） */
  function bindRowActivation(node, activate) {
    node.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      activate();
    });
  }

  /* 行当按钮用（tabindex + role；文案说明放 title，不盖 aria-label 以免
      屏幕阅读器读不到行内真实的按钮们） */
  function makeRowActivatable(node, title) {
    node.tabIndex = 0;
    node.setAttribute("role", "button");
    if (title) node.title = title;
  }

  /* 逗号分隔输入 → 去空白、去空项的字符串数组（中英文逗号都认；后端仍会再校验） */
  function splitList(raw) {
    return String(raw || "")
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function showError(msg) {
    const box = $("#monError");
    if (!box) return;
    box.textContent = msg;
    box.classList.remove("hidden");
  }

  function clearError() {
    const box = $("#monError");
    if (!box) return;
    box.textContent = "";
    box.classList.add("hidden");
  }

  function setCreating(creating) {
    const btn = $("#monCreateSubmit");
    if (btn) btn.disabled = creating;
  }

  /* ---------- 限制口径行（表单下方动态渲染，数值单一来源是 LIMITS） ---------- */
  function renderLimitsHint() {
    const form = $("#monCreateForm");
    if (!form || !form.parentNode) return;
    let node = document.getElementById("monLimitsHint");
    if (!node) {
      node = el("p", "chat-monitor-hint muted");
      node.id = "monLimitsHint";
      form.parentNode.insertBefore(node, form.nextSibling);   // 表单之下、列表之上
    }
    node.textContent = "限制口径（以后端为准）：话题最长 " + LIMITS.maxTopic
      + " 字；关键词最多 " + LIMITS.maxKeywords
      + " 个；每轮域名限定最多 " + LIMITS.maxDomains
      + " 个 site:；频率不低于 " + LIMITS.minFrequencyS + " 秒（保护搜索引擎）。";
  }

  /* ---------- 显隐规则（按档案角色） ---------- */
  function snapshot() {
    const panel = window.ProfilesPanel;
    return (panel && typeof panel.snapshot === "function") ? panel.snapshot() : null;
  }

  function isMonitor() {
    const snap = snapshot();
    const active = (snap && snap.active) || null;
    return Boolean(active) && active.role === "monitor";
  }

  function clearSelection() {
    state.selectedId = null;
    const detail = $("#monDetail");
    if (detail) {
      detail.textContent = "";
      detail.classList.add("hidden");
    }
  }

  function applyVisibility() {
    const panel = $("#chatMonitorPanel");
    if (!panel) return false;
    const visible = isMonitor();
    panel.classList.toggle("hidden", !visible);
    if (visible && !state.visible) reload();   // 首次显示时才拉列表
    state.visible = visible;
    if (!visible) {
      /* 藏起来时：轮询与选择都属于刚切走的档案工作区，如实清掉 */
      stopAllRunPolls();
      state.runningIds.clear();
      clearSelection();
      clearError();
    }
    return visible;
  }

  /* profiles.js 与本文件同在 DOMContentLoaded 初始化，而 loadProfiles 是异步的：
      首屏快照可能还是空。轮询等它就绪再定显隐（空快照按规格隐藏；10s 等不到
      就保持隐藏，不无限轮询）——U0 收口后走 window.ChatCards.startProfilesPoll
      （250ms × 最多 40 tick，快照出现 active 或到顶自停）；句柄存 state 防重入，
      导出版没有完成回调，句柄留在 state 里无妨（本函数只在 init 调一次）。 */
  function startProfilesPoll() {
    if (state.profilesPoll) return;
    const cc = window.ChatCards;
    if (!(cc && typeof cc.startProfilesPoll === "function")) return;
    state.profilesPoll = cc.startProfilesPoll(applyVisibility, snapshot);
  }

  /* ---------- 新建订阅 ---------- */
  function bindCreateForm() {
    const form = $("#monCreateForm");
    if (form) form.addEventListener("submit", createSubscription);
  }

  async function createSubscription(event) {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    if (state.creating) return;              // 一次未回包不叠加请求
    clearError();
    /* 前端基本校验：话题非空（其余上限交给后端 400，detail 原文照抄） */
    const topicBox = $("#monTopic");
    const topic = String((topicBox && topicBox.value) || "").trim();
    if (!topic) { showError("请先填写话题（要持续监测的主题）。"); return; }
    const keywords = splitList(($("#monKeywords") || {}).value);
    const domains = splitList(($("#monDomains") || {}).value);
    const frequency = Number(($("#monFrequency") || {}).value) || 3600;

    state.creating = true;
    setCreating(true);
    try {
      const resp = await fetch(API.subscriptions, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic, keywords, domains, frequency_s: frequency }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      /* 提交成功：话题/关键词/域名清空（频率选择保留），重载后直接打开新订阅 */
      if (topicBox) topicBox.value = "";
      if ($("#monKeywords")) $("#monKeywords").value = "";
      if ($("#monDomains")) $("#monDomains").value = "";
      await reload();
      const newId = Number((data && data.subscription && data.subscription.id) || 0);
      if (newId > 0) await loadDetail(newId);
    } catch (err) {
      /* 后端 400 的 detail 原文照抄（如"频率过低（最少 300 秒，保护搜索引擎）"） */
      showError(String((err && err.message) || err));
    } finally {
      state.creating = false;
      setCreating(false);
    }
  }

  /* ---------- 订阅列表 ---------- */
  /* 列表加载失败不再静默：渲染错误行 + 重试（DOM 结构对齐 chat.js renderListError /
     batch.js 的同名口径，样式复用 chat.css 的 .chat-list-error，含
     .chat-monitor-list li.chat-list-error 的虚线中和规则）。 */
  function renderListLoadError() {
    const list = $("#monList");
    if (!list) return;
    list.textContent = "";
    const li = el("li", "chat-list-error");
    li.appendChild(el("span", "", "订阅列表加载失败（网络或服务未就绪）"));
    const retry = el("button", "", "点击重试");
    retry.type = "button";
    retry.addEventListener("click", () => { reload(); });
    li.appendChild(retry);
    list.appendChild(li);
  }

  async function reload() {
    let data = null;
    try {
      const resp = await fetch(API.subscriptions);
      if (resp.ok) data = await resp.json();
    } catch (e) {
      data = null;   // 网络异常与 HTTP 非 ok 同口径：错误行 + 重试，不阻塞面板其余交互
    }
    if (data === null) {
      renderListLoadError();
      return;
    }
    state.subs = Array.isArray(data.subscriptions) ? data.subscriptions : [];
    /* 限制元数据（后端给了就用后端值刷新口径行；没有就维持 LIMITS 默认值） */
    const minFreq = Number(data.min_frequency_s);
    const maxDom = Number(data.max_domains);
    if (Number.isFinite(minFreq) && minFreq > 0) LIMITS.minFrequencyS = minFreq;
    if (Number.isFinite(maxDom) && maxDom > 0) LIMITS.maxDomains = maxDom;
    renderLimitsHint();
    renderList();
    /* 选择若已不在新列表里（被删 / 换工作区），清掉详情，避免留着旧数据 */
    if (state.selectedId
        && !state.subs.some((s) => Number(s.id) === state.selectedId)) {
      clearSelection();
    }
  }

  function renderList() {
    const list = $("#monList");
    if (!list) return;
    list.textContent = "";
    if (!state.subs.length) {
      list.appendChild(el("li", "empty", "暂无订阅；填好话题后点「新建订阅」。"));
      return;
    }
    state.subs.forEach((s) => list.appendChild(subRow(s)));
  }

  function subRow(sub) {
    sub = sub || {};
    const id = Number(sub.id) || 0;
    const li = el("li", "chat-monitor-item");
    if (state.selectedId === id) li.classList.add("active");
    li.dataset.sid = String(id);

    const head = el("div", "chat-monitor-item-head");
    head.appendChild(el("span", "chat-monitor-item-title", sub.topic || `订阅 #${id}`));
    /* R2 后 last_status 起跑即 'running'（不再是死代码）：轮询期间该行徽章显示
        「进行中」、按钮显示「运行中…」并禁用（本端盯着的或调度在跑的都算） */
    const running = state.runningIds.has(id) || String(sub.last_status || "") === "running";
    if (!sub.enabled) {
      head.appendChild(el("span", "badge amber", "已暂停"));
    } else {
      const meta = statusMeta(LAST_STATUS, sub.last_status);
      head.appendChild(el("span", `badge ${meta.cls}`, meta.text));
    }
    li.appendChild(head);

    const bits = [`#${id}`, `频率 ${freqLabel(sub.frequency_s)}`];
    const keywords = Array.isArray(sub.keywords) ? sub.keywords : [];
    const domains = Array.isArray(sub.domains) ? sub.domains : [];
    if (keywords.length) bits.push(`关键词：${keywords.join("、")}`);
    if (domains.length) bits.push(`域名限定：${domains.join("、")}`);
    if (sub.last_run_at) bits.push(`上次运行 ${when(sub.last_run_at)}`);
    if (sub.enabled && sub.next_run_at) bits.push(`下次运行 ${when(sub.next_run_at)}`);
    li.appendChild(el("div", "chat-monitor-item-meta muted", bits.join(" · ")));

    const actions = el("div", "chat-monitor-item-actions");
    const runBtn = el("button", "chat-monitor-btn", running ? "运行中…" : "立即运行");
    runBtn.type = "button";
    runBtn.disabled = running;
    /* R8：已暂停的订阅点「立即运行」会被后端 409 拒绝（不隐式恢复）。按钮保持
        可点（点后把暂停原因原样显示出来），悬停文案先给一步提示。 */
    if (!sub.enabled) {
      runBtn.title = "该订阅已暂停：请先点「恢复」启用后再立即运行";
    }
    runBtn.addEventListener("click", (e) => { e.stopPropagation(); runNow(id); });
    actions.appendChild(runBtn);
    const toggleBtn = el("button", "chat-monitor-btn", sub.enabled ? "暂停" : "恢复");
    toggleBtn.type = "button";
    toggleBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleEnabled(sub); });
    actions.appendChild(toggleBtn);
    const delBtn = el("button", "chat-monitor-btn chat-monitor-btn-danger", "删除");
    delBtn.type = "button";
    delBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      /* 不可逆操作先确认（与 profiles.js 删除档案同一口径），文案带订阅主题 */
      const label = sub.topic || `订阅 #${id}`;
      if (!window.confirm(`删除订阅「${label}」？删除后不可恢复。`)) return;
      removeSub(id);
    });
    actions.appendChild(delBtn);
    li.appendChild(actions);

    li.addEventListener("click", () => loadDetail(id));
    /* 行本身也可键盘触达：Enter / Space 与点击同效（打开该订阅详情） */
    makeRowActivatable(li, "查看该订阅详情（运行历史与变化卡）");
    bindRowActivation(li, () => loadDetail(id));
    return li;
  }

  /* ---------- 订阅详情（头部 + 变化卡 + 运行历史） ---------- */
  async function loadDetail(id) {
    if (!(Number(id) > 0)) return;
    const detail = $("#monDetail");
    if (!detail) return;
    state.selectedId = Number(id);
    renderList();   // 高亮当前选择
    detail.textContent = "";
    detail.classList.remove("hidden");
    detail.appendChild(el("p", "muted", `正在加载订阅 #${Number(id)}…`));
    try {
      const resp = await fetch(apiPath(API.detail, id));
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      /* 运行历史优先取专用端点（limit=50，比详情内嵌的 20 条更全）；
         取不到就回落详情内嵌的 runs——不因附属请求失败整块不显示 */
      let runs = Array.isArray(data.runs) ? data.runs : [];
      try {
        const runsResp = await fetch(apiPath(API.runs, id));
        if (runsResp.ok) {
          const runsData = await runsResp.json();
          if (Array.isArray(runsData.runs) && runsData.runs.length) runs = runsData.runs;
        }
      } catch (e) { /* 历史附属请求失败：回落详情内嵌 runs */ }
      renderDetail((data && data.subscription) || {}, runs, (data && data.card) || {});
    } catch (err) {
      detail.textContent = "";
      detail.appendChild(el("p", "chat-monitor-error",
        `订阅详情加载失败：${(err && err.message) || err}`));
    }
  }

  function renderDetail(sub, runs, card) {
    const detail = $("#monDetail");
    if (!detail) return;
    detail.textContent = "";
    detail.classList.remove("hidden");

    /* 头部容器只做分组（无样式）；标题 / 徽章行 / 元信息行各自带类 */
    const head = el("div");
    head.appendChild(el("div", "chat-monitor-detail-title",
      sub.topic || `订阅 #${Number(sub.id) || 0}`));
    const badges = el("div", "chat-badges");
    if (!sub.enabled) badges.appendChild(el("span", "badge amber", "已暂停"));
    const smeta = statusMeta(LAST_STATUS, sub.last_status);
    badges.appendChild(el("span", `badge ${smeta.cls}`, smeta.text));
    head.appendChild(badges);
    const bits = [`#${Number(sub.id) || 0}`, `频率 ${freqLabel(sub.frequency_s)}`];
    const keywords = Array.isArray(sub.keywords) ? sub.keywords : [];
    const domains = Array.isArray(sub.domains) ? sub.domains : [];
    if (keywords.length) bits.push(`关键词：${keywords.join("、")}`);
    if (domains.length) bits.push(`域名限定：${domains.join("、")}`);
    if (sub.last_run_at) bits.push(`上次运行 ${when(sub.last_run_at)}`);
    if (sub.enabled && sub.next_run_at) bits.push(`下次运行 ${when(sub.next_run_at)}`);
    if (sub.created_at) bits.push(`创建于 ${when(sub.created_at)}`);
    head.appendChild(el("div", "chat-monitor-detail-meta muted", bits.join(" · ")));
    detail.appendChild(head);

    /* 暂停原因（后端 blocked 时写明授权说明；没有就不显示） */
    if (sub.paused_reason) {
      detail.appendChild(el("p", "chat-monitor-notice", String(sub.paused_reason)));
    }

    /* 变化卡：最新一轮的 monitor_change 卡。无已完成轮次时后端给 {}，
        如实显示"还没有已完成的运行轮次"，不挂空卡、不编造。
        （想看更早轮次的卡：点下面运行历史里对应的那一行即可切换。） */
    if (card && card.card_type && window.ChatCards
        && typeof window.ChatCards.mountCard === "function") {
      const slot = el("div", "chat-monitor-card-slot");
      window.ChatCards.mountCard(slot, card);
      detail.appendChild(slot);
    } else {
      detail.appendChild(el("p", "muted",
        "还没有已完成的运行轮次（跑完一轮后这里出变化卡）。"));
    }

    if (runs.length) {
      detail.appendChild(el("div", "chat-insight-rank-title",
        `运行历史（最近 ${runs.length} 轮）`));
      const ul = el("ul", "chat-monitor-runs");
      runs.forEach((r) => ul.appendChild(runRow(r, sub)));
      detail.appendChild(ul);
    }
  }

  function runRow(run, sub) {
    run = run || {};
    const li = el("li", "chat-monitor-run");
    const meta = statusMeta(RUN_STATUS, run.status);
    li.appendChild(el("span", `badge ${meta.cls}`, meta.text));
    /* R9：site: 变体全空兜底时后端把 scope_degraded=true 写进 run.summary——
        灰色小徽章如实标注"这一轮没限定住"，只有带该标注的行显示 */
    const summary = (run.summary && typeof run.summary === "object") ? run.summary : {};
    if (summary.scope_degraded === true) {
      li.appendChild(el("span", "badge gray",
        "域名限定未生效（引擎不支持 site: 检索，已用非限定结果）"));
    }
    const span = [`#${Number(run.id) || 0}`, when(run.started_at)];
    if (run.finished_at) span.push(`→ ${when(run.finished_at)}`);
    span.push(`检到 ${Number(run.results_count) || 0} 条 · 新增 ${Number(run.new_count) || 0} 条`);
    li.appendChild(el("span", "chat-monitor-run-meta muted", span.join(" · ")));
    if (run.error) {
      li.appendChild(el("div", "chat-monitor-run-error", String(run.error)));
    }
    /* 终态行可点 / 可键盘激活：就地挂该轮的变化卡（上方卡槽随之切换，最新一轮
        即列表顶部行）。运行中的行没有可挂的卡，保持只读不激活。 */
    if (DONE_STATUS.indexOf(String(run.status || "")) >= 0) {
      li.dataset.pickable = "1";
      makeRowActivatable(li, "查看这一轮的变化卡");
      const pick = () => showRunCard(sub, run);
      li.addEventListener("click", pick);
      bindRowActivation(li, pick);
    }
    return li;
  }

  /* 用运行行字段就地合成 monitor_change 载荷并挂进详情卡槽。字段口径与后端
      tools_monitor.change_card 完全一致（card_type / subscription_id / topic /
      run_id / status / results_count / new_count / new_urls / removed_urls /
      e_score / prev_e_score / e_delta / as_of / error），全部取自 run 行与订阅
      主题，不编造字段；挂载走 window.ChatCards.mountCard（innerHTML 只在
      cards.js 渲染层，本文件仍零注入点）。 */
  function runCardPayload(sub, run) {
    const summary = (run.summary && typeof run.summary === "object") ? run.summary : {};
    return {
      card_type: "monitor_change",
      subscription_id: Number((sub && sub.id) || 0),
      topic: String((sub && sub.topic) || ""),
      run_id: Number(run.id) || 0,
      status: String(run.status || ""),
      results_count: Number(run.results_count) || 0,
      new_count: Number(run.new_count) || 0,
      new_urls: Array.isArray(summary.new_urls) ? summary.new_urls : [],
      removed_urls: Array.isArray(summary.removed_urls) ? summary.removed_urls : [],
      e_score: summary.e_score,
      prev_e_score: summary.prev_e_score,
      e_delta: summary.e_delta,
      as_of: String(run.finished_at || ""),
      error: String(run.error || ""),
    };
  }

  function showRunCard(sub, run) {
    const detail = $("#monDetail");
    if (!detail) return;
    const slot = detail.querySelector(".chat-monitor-card-slot");
    if (!slot) return;   // 详情里没有卡槽（无已完成轮次）就不挂
    if (!(window.ChatCards && typeof window.ChatCards.mountCard === "function")) return;
    window.ChatCards.mountCard(slot, runCardPayload(sub, run));
  }

  /* ---------- 立即运行（202 + 后台线程 → 轮询等收尾） ---------- */
  async function runNow(id) {
    if (!(Number(id) > 0)) return;
    if (state.runningIds.has(Number(id))) return;   // 本端正盯着它，不重复触发
    clearError();
    state.runningIds.add(Number(id));
    renderList();   // 按钮即时变「运行中…」并禁用
    try {
      const resp = await fetch(apiPath(API.runNow, id), { method: "POST" });
      const data = await resp.json().catch(() => ({}));
      if (resp.status === 409) {
        /* 409 两种（后端 detail 原文照抄，不改写、不猜测）：
            "该订阅正在运行中，请稍候" / "订阅已暂停，请先恢复启用后再手动运行
            （暂停原因：…）"——暂停订阅的按钮本就可点，点后原因就显示在这里 */
        showError(String(data.detail || "该订阅正在运行中，请稍候"));
        state.runningIds.delete(Number(id));
        renderList();
        return;
      }
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      /* R2：202 响应带 run_id（预建运行行 id）——轮询优先按它核对本轮终态；
          旧后端 / 响应异常取不到时 startRunPoll 自己回退旧判据 */
      startRunPoll(Number(id), Number(data.run_id) || 0);
    } catch (err) {
      showError(`立即运行失败：${(err && err.message) || err}`);
      state.runningIds.delete(Number(id));
      renderList();
    }
  }

  /* 轮询详情等本轮收尾。完成判据：响应带 run_id 时优先核对"该轮运行行"状态
     已到终态（比 last_status 更精确，不会被并发下一轮覆盖；运行行是 202 之前
     预建的，正常必然出现在详情 runs 列表里）；取不到 run_id、或该行意外缺席
     时回退旧判据 subscription.last_status ∈ 终态——R2 后起跑即写 running，
     旧判据也不会把上一轮旧终态误读为本轮完成。
     用 setTimeout 链（不用 setInterval）：上一次 fetch 没回来不叠下一轮。 */
  function startRunPoll(id, runId) {
    stopRunPoll(id);
    const wantRunId = Number(runId) > 0 ? Number(runId) : 0;
    let ticks = 0;
    const tick = async () => {
      ticks += 1;
      let sub = null;
      let runs = [];
      try {
        const resp = await fetch(apiPath(API.detail, id));
        if (resp.ok) {
          const data = await resp.json();
          sub = (data && data.subscription) || null;
          runs = Array.isArray((data && data.runs)) ? data.runs : [];
        }
      } catch (e) { /* 网络抖动：下一 tick 再试，不谎报完成 */ }
      if (state.pollTimers[id] === undefined) return;   // 等待期间已被取消
      const status = String((sub && sub.last_status) || "");
      const row = wantRunId
        ? runs.find((r) => Number(r && r.id) === wantRunId) : null;
      const done = row
        ? DONE_STATUS.indexOf(String(row.status || "")) >= 0
        : DONE_STATUS.indexOf(status) >= 0;
      if (done) {
        finishRunPoll(id);
        await reload();
        if (state.selectedId === id) await loadDetail(id);
        return;
      }
      if (ticks >= RUN_POLL_MAX_TICKS) {
        /* 后台线程跑多久不取决于前端：超上限只如实提示等待时长 + 给下一步指引，
            不假装完成、不无限等 */
        finishRunPoll(id);
        const waited = Math.round((RUN_POLL_MAX_TICKS * RUN_POLL_INTERVAL_MS) / 1000);
        showError(`仍在运行（已等待 ${waited} 秒）：可稍后刷新查看结果，或到运行历史确认。`);
        await reload();
        return;
      }
      state.pollTimers[id] = window.setTimeout(tick, RUN_POLL_INTERVAL_MS);
    };
    state.pollTimers[id] = window.setTimeout(tick, RUN_POLL_INTERVAL_MS);
  }

  function finishRunPoll(id) {
    stopRunPoll(id);
    state.runningIds.delete(id);
    renderList();
  }

  function stopRunPoll(id) {
    const timer = state.pollTimers[id];
    if (timer) {
      window.clearTimeout(timer);
      delete state.pollTimers[id];
    }
  }

  function stopAllRunPolls() {
    Object.keys(state.pollTimers).forEach((key) => stopRunPoll(Number(key)));
  }

  /* ---------- 暂停 / 恢复 / 删除 ---------- */
  async function toggleEnabled(sub) {
    const id = Number(sub && sub.id) || 0;
    if (!(id > 0)) return;
    clearError();
    try {
      const resp = await fetch(apiPath(API.detail, id), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !sub.enabled }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      await reload();
      if (state.selectedId === id) await loadDetail(id);
    } catch (err) {
      showError(`操作失败：${(err && err.message) || err}`);
    }
  }

  async function removeSub(id) {
    if (!(Number(id) > 0)) return;
    clearError();
    try {
      const resp = await fetch(apiPath(API.detail, id), { method: "DELETE" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      stopRunPoll(Number(id));
      state.runningIds.delete(Number(id));
      if (state.selectedId === Number(id)) clearSelection();
      await reload();
    } catch (err) {
      showError(`删除失败：${(err && err.message) || err}`);
    }
  }

  /* ---------- 对外（chat.js 只调 init / onProfileChanged） ---------- */
  window.MonitorPanel = {
    init,
    onProfileChanged,
    reload,
  };

  function init() {
    bindCreateForm();
    renderLimitsHint();   // 限制口径行先按本地 LIMITS 落位，reload 后用后端值刷新
    applyVisibility();
    startProfilesPoll();
  }

  function onProfileChanged() {
    /* 换工作区 = 换数据范围：run-now 轮询属于旧工作区的订阅，选择与详情清空 */
    stopAllRunPolls();
    state.runningIds.clear();
    clearSelection();
    clearError();
    applyVisibility();
    if (isMonitor()) reload();
  }
})();
