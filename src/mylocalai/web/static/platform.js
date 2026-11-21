/* ============================================================
   platform.js —— 监测平台视图前端（方案 G：状态摘要条 + 筛选 chips + 卡片流 + 管理抽屉）
   独立文件：只依赖 index.html 的元素，不碰 chat.js 的契约。
   职责：
     - 通栏 Tab 切换：对话视图 ↔ 监测平台视图（默认对话；platform.js 接管）；
     - 拉取 /api/platform/{status,feed,warnings,watch,scan,scans,domains,sources} 渲染：
       状态摘要条（状态点三态 + 诚实话术）、筛选后信息流（风险分级徽章 +
       领域徽章）、监控词管理（右侧抽屉，含领域下拉与行内编辑）、
       信息源管理（rss/page，全局/按词绑定，启停与单源试运行）、巡检时间线；
     - 手动巡检：POST /api/platform/scan → 轮询 status 直到 scan_in_progress
       结束 → 刷新全部（单飞：后端忙时 started=false 如实提示）；
     - 空态双引导：没有监控词 → 引导去抽屉添加；有词无信息 → 引导立即巡检；
     - 未授权引导态：外部检索默认关闭（fail-closed），抽屉里出引导卡链到设置弹层，
       不静默失败、不假装有数据；
     - 加载失败如实降级：状态/信息流/监控词/巡检记录/信息源任一路失败，都在对应
       区域渲染「数据加载失败 + 重试」错误行（.platform-load-error，纯 DOM），
       不把网络故障演成「暂无数据」——错误态与空态分开说，空态引导只在真没数据时出。
   口径：风险分级只描述传播风险、不判定真假；热度是代理/估计值（徽章随行）；
        领域只来自用户手设或后端推断链（user / inferred_*），none 与空一律不画徽章。

   【innerHTML 白名单声明】本文件登记为全站 innerHTML 例外渲染层（白名单成员；
   卡片渲染的唯一事实源在 cards.js，处理用户输入的 settings.js / profiles.js
   一律禁用 innerHTML 且有静态扫描守护）。豁免边界——只许收紧、不许扩大，
   每次改动重审一遍：
     1. 所有插值先过 escapeHtml：唯一实现取自 window.ChatCards 导出（U0 收口），
        本文件不留本地副本；
     2. 所有动态 href 一律过 safeUrl（只接受 http/https，javascript:/data: 等
        伪协议落 "#"）；外部文本截断用 clipped（超长如实缀「…（已截断）」，
        对齐 cards.js 口径）；
     3. 无外部数据插值的 UI（加载失败行/重试按钮等）一律 createElement +
        textContent 纯 DOM 构建，不走 innerHTML；
     4. 新增渲染点能用纯 DOM 就不用 innerHTML；前后端自洽守护在
        tests/test_platform_api.py（API 路径 / 元素 id / platform-* class 三向断言）。
   ============================================================ */
"use strict";

(function () {
  /* file:// 双击打开时 shell.js 已给出启动引导，这里静默退出 */
  if (location.protocol === "file:") return;

  /* escapeHtml 取 U0 收口后的唯一实现（cards.js 先于本文件加载，见 index.html
     脚本顺序）。依赖缺失时与「骨架不完整安静退出」同口径，不另留本地副本。 */
  if (!window.ChatCards || typeof window.ChatCards.escapeHtml !== "function") return;
  const escapeHtml = window.ChatCards.escapeHtml;

  const $ = (sel) => document.querySelector(sel);

  /* 后端接口清单（与 web/platform_api.py 路由表一一对应，测试做前后端自洽断言） */
  const PLATFORM_API = {
    status: "/api/platform/status",
    feed: "/api/platform/feed",
    warnings: "/api/platform/warnings",
    watch: "/api/platform/watch",
    scan: "/api/platform/scan",
    scans: "/api/platform/scans",
    domains: "/api/platform/domains",
    sources: "/api/platform/sources",
  };

  /* 风险档位 → 展示名与徽章色（后端 risk.LEVELS/LEVEL_LABELS 同口径；
     notice=关注 / suspect=疑似 / high=高危 / urgent=紧急；urgent 用深红 critical） */
  const RISK_LEVELS = {
    notice: { label: "关注", cls: "gray" },
    suspect: { label: "疑似", cls: "amber" },
    high: { label: "高危", cls: "red" },
    urgent: { label: "紧急", cls: "critical" },
  };

  /* 巡检状态 → 中文（与 CLI _STATUS_TEXT 同口径） */
  const SCAN_STATUS = {
    completed: "完成", partial: "部分完成", cancelled: "已取消",
    skipped: "跳过", failed: "失败", running: "进行中",
  };

  const SCAN_POLL_MS = 2000;
  const SCAN_POLL_TIMEOUT_MS = 150000;   // 与 deep_verify deadline 同量级的上限
  const WARN_LIMIT = 200;                // 摘要「高风险 N 条」要如实计数，不截在 20

  let feedRiskFilter = "";                // 当前信息流筛选（"" / "high,urgent" / "suspect" / "notice"）
  let scanPollTimer = null;
  let emptyKind = "no-watch";             // 空态类型：no-watch（无监控词）/ no-entries（有词无信息）
  let watchEditId = null;                 // 监控词编辑态：null=新增态，否则为被编辑的 query_id
  let lastDomains = [];                   // 领域表（GET /domains）：徽章中文名与下拉的唯一数据源
  let lastSources = [];                   // 信息源清单（GET /sources）
  /* 上一轮加载结果：摘要话术要跨 render 函数取数（状态点/摘要/空态口径都依赖） */
  let lastStatus = null;
  let lastWarnings = [];
  let lastWatch = [];
  let lastFeed = [];
  let lastScans = [];
  let feedLoadFailed = false;             // 信息流加载失败态：失败时不渲染空态引导（那是空态的活）

  /* 安全外链（对照 cards.js 的 safeUrl 口径）：原文链接来自外部检索内容，必须
     按不可信输入处理——只接受 http/https；javascript:/data: 等伪协议一律落 "#"
     （空值也归 "#"，与旧口径 e.url || "#" 一致）。返回值已转义，直接进 href。 */
  function safeUrl(u) {
    const s = String(u || "").trim();
    if (!s) return "#";
    return /^https?:\/\//i.test(s) ? escapeHtml(s) : "#";
  }

  /* 截断标注统一口径（对齐 cards.js）：超长被截时如实缀「…（已截断）」，
     不让用户把摘要当全文。返回已转义文本，直接进模板插值。 */
  function clipped(s, n) {
    const str = String(s || "");
    const cut = str.length > n;
    return escapeHtml(cut ? str.slice(0, n) : str) + (cut ? "…（已截断）" : "");
  }

  function fmtTime(s) {
    return String(s || "").replace("T", " ").slice(0, 19) || "—";
  }

  /* 取 HH:MM：巡检时间只存在于 /scans 记录里（status.last_scan 只带摘要不带时间），
     取不到就返回空串——摘要里不编时间 */
  function hmOf(s) {
    const t = fmtTime(s);
    return /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(t) ? t.slice(11, 16) : "";
  }

  function eScoreValid(e) {
    return e !== null && e !== undefined && e !== "" && !Number.isNaN(Number(e));
  }

  function fmtE(e) {
    return eScoreValid(e) ? `E=${Number(e).toFixed(1)}` : "E=?";
  }

  /* ---------- 视图切换 ---------- */
  function showView(which) {
    const isPlatform = which === "platform";
    $("#navChat").classList.toggle("active", !isPlatform);
    $("#navPlatform").classList.toggle("active", isPlatform);
    $("#navChat").setAttribute("aria-current", isPlatform ? "false" : "page");
    $("#navPlatform").setAttribute("aria-current", isPlatform ? "page" : "false");
    document.querySelector(".chat-layout").classList.toggle("hidden", isPlatform);
    $("#platformView").classList.toggle("hidden", !isPlatform);
    if (isPlatform) refreshAll();
  }

  /* ---------- 数据加载（任一路失败自行降级，不拖垮其余几路） ----------
     失败返回 null（与「真没数据」的 [] 严格区分）：render* 收到 null 就渲染
     失败行 + 重试按钮，收到 [] 才渲染空态——不把网络故障演成"没有数据"。 */
  async function loadStatus() {
    try {
      const resp = await fetch(PLATFORM_API.status);
      if (!resp.ok) return null;      // null = 加载失败（renderStatus 落失败态）
      return resp.json();
    } catch (e) { return null; }
  }

  async function loadFeed() {
    try {
      const url = feedRiskFilter
        ? `${PLATFORM_API.feed}?risk=${encodeURIComponent(feedRiskFilter)}&limit=50`
        : `${PLATFORM_API.feed}?limit=50`;
      const resp = await fetch(url);
      if (!resp.ok) return null;
      const data = await resp.json();
      return (data && data.entries) || [];
    } catch (e) { return null; }
  }

  /* 预警只并入摘要计数、无独立区域：失败退 []（计数按 0 显示）是已知取舍，
     区域级如实报错由状态/信息流/监控词/巡检/信息源五路承担 */
  async function loadWarnings() {
    try {
      const resp = await fetch(`${PLATFORM_API.warnings}?limit=${WARN_LIMIT}`);
      if (!resp.ok) return [];
      const data = await resp.json();
      return (data && data.entries) || [];
    } catch (e) { return []; }
  }

  async function loadWatch() {
    try {
      const resp = await fetch(PLATFORM_API.watch);
      if (!resp.ok) return null;
      const data = await resp.json();
      return (data && data.queries) || [];
    } catch (e) { return null; }
  }

  async function loadScans() {
    try {
      const resp = await fetch(`${PLATFORM_API.scans}?limit=10`);
      if (!resp.ok) return null;
      const data = await resp.json();
      return (data && data.scans) || [];
    } catch (e) { return null; }
  }

  async function loadSources() {
    try {
      const resp = await fetch(PLATFORM_API.sources);
      if (!resp.ok) return null;      // null = 加载失败（与"空清单"区分，如实提示）
      const data = await resp.json();
      return (data && data.sources) || [];
    } catch (e) { return null; }
  }

  async function loadDomains() {
    try {
      const resp = await fetch(PLATFORM_API.domains);
      if (!resp.ok) return [];
      const data = await resp.json();
      return (data && data.domains) || [];
    } catch (e) { return []; }
  }

  /* ---------- 领域（GET /api/platform/domains 是唯一数据源） ---------- */
  function domainName(id) {
    const did = String(id || "").trim();
    if (!did) return "";
    const hit = lastDomains.find((d) => d && d.id === did);
    return hit ? (hit.name || did) : did;      // 表里查不到时如实显示 id，不编中文名
  }

  /* 领域来源 → 徽章：user=实色淡底；inferred_text/inferred_history=描边+「推」角标；
     none/空/未知来源 → 不渲染（不画空徽章、不猜领域） */
  function feedDomainBadgeHtml(topicDomain, source) {
    const did = String(topicDomain || "").trim();
    if (!did) return "";
    const src = String(source || "").trim();
    if (src === "user") {
      return `<span class="platform-feed-domain-user">${escapeHtml(domainName(did))}</span>`;
    }
    if (src === "inferred_text" || src === "inferred_history") {
      return `<span class="platform-feed-domain-inferred">${escapeHtml(domainName(did))}`
        + `<em class="platform-feed-domain-infer-mark">推</em></span>`;
    }
    return "";
  }

  /* 监控词行的领域徽章：用户手设=主色淡底；只有推断缓存=虚线描边+「推」角标；都没有=不显示 */
  function watchDomainBadgeHtml(q) {
    const user = String((q || {}).domain || "").trim();
    if (user) {
      return `<span class="platform-watch-domain" title="手设领域">`
        + `${escapeHtml(domainName(user))}</span>`;
    }
    const inferred = String((q || {}).inferred_domain || "").trim();
    if (inferred) {
      return `<span class="platform-watch-domain platform-watch-domain-inferred" `
        + `title="自动推断领域">${escapeHtml(domainName(inferred))}`
        + `<em class="platform-feed-domain-infer-mark">推</em></span>`;
    }
    return "";
  }

  /* 领域数据落两处下拉：监控词领域选择（自动推断 + 12 领域）与信息源绑定多选 */
  function fillDomainSelects() {
    const watchSel = $("#platformWatchDomain");
    if (watchSel) {
      watchSel.innerHTML = '<option value="">自动推断</option>'
        + lastDomains.map((d) => `<option value="${escapeHtml(d.id)}">`
          + `${escapeHtml(d.name || d.id)}</option>`).join("");
      /* 编辑态下回填被编辑行的领域（renderWatch 之后调也要能还原） */
      const row = watchEditId === null ? null
        : lastWatch.find((q) => Number(q.id) === watchEditId);
      watchSel.value = row ? String(row.domain || "") : "";
    }
    const bindSel = $("#platformSourceBindings");
    if (bindSel) {
      bindSel.innerHTML = lastWatch.map((q) =>
        `<option value="${Number(q.id)}">${escapeHtml(q.query || "")}</option>`).join("");
    }
  }

  /* ---------- 渲染：状态摘要条 ---------- */
  function renderSummary() {
    const el = $("#platformSummary");
    if (!el || !lastStatus) return;
    const watchN = lastWatch.length;
    const warnN = lastWarnings.length;
    const last = lastStatus.last_scan || {};
    /* 状态口径优先取 status.last_scan；调度器重启后它为空、但库里有巡检记录时，
       如实退回最新一条记录，不谎称「尚未巡检」 */
    const newest = lastScans[0] || null;
    const rawStatus = last.status || (newest && newest.status) || "";
    let text;
    if (rawStatus) {
      let hm = "";
      if (newest) {
        const hit = (last.scan_id !== null && last.scan_id !== undefined)
          ? lastScans.find((s) => Number(s.id) === Number(last.scan_id))
          : null;
        hm = hmOf((hit || newest).started_at || (hit || newest).finished_at);
      }
      const statusText = SCAN_STATUS[rawStatus] || String(rawStatus);
      const timePart = hm ? ` ${hm}` : "";
      text = `监测中 · ${watchN} 个话题 · 最近巡检${timePart}（${statusText}）`
        + ` · 高风险 ${warnN} 条`;
    } else {
      text = `尚未巡检 · ${watchN} 个话题`;
    }
    if (!lastStatus.external_authorized) text += " · 外部检索未授权";
    el.textContent = text;
  }

  /* 状态加载失败：摘要条如实报错 + 重试按钮，不再死在骨架的初始「加载中…」。
     重试按区调对应加载函数（loadStatus）；成功后 renderStatus 会清掉失败钮。 */
  function renderStatusLoadError() {
    const bar = $("#platformStatusBar");
    if (!bar) return;
    const summary = $("#platformSummary");
    if (summary) summary.textContent = "状态获取失败";
    const dot = $("#platformStatusDot");
    if (dot) dot.classList.remove("platform-statusdot-run", "platform-statusdot-warn");
    const actions = bar.querySelector(".platform-statusbar-actions");
    if (actions && !actions.querySelector(".platform-load-retry")) {
      const btn = document.createElement("button");   // 纯 DOM：无插值，不进 innerHTML
      btn.type = "button";
      btn.className = "platform-load-retry";
      btn.textContent = "重试";
      btn.addEventListener("click", () => loadStatus().then(renderStatus));
      actions.appendChild(btn);
    }
  }

  function renderStatus(status) {
    const bar = $("#platformStatusBar");
    if (!bar) return;
    if (!status) { renderStatusLoadError(); return; }   // null = 拉取失败（见 loadStatus）
    lastStatus = status;
    /* 成功即清掉上一次失败留下的重试钮（若有），摘要话术由 renderSummary 重写 */
    const staleRetry = bar.querySelector(".platform-statusbar-actions .platform-load-retry");
    if (staleRetry) staleRetry.remove();
    const authorized = !!status.external_authorized;
    const running = !!status.running;
    const busy = running || !!status.scan_in_progress;

    /* 状态点三态：未授权（warn）> 调度器运行（run）> 默认灰（基色，不加类） */
    const dot = $("#platformStatusDot");
    if (dot) {
      dot.classList.toggle("platform-statusdot-warn", !authorized);
      dot.classList.toggle("platform-statusdot-run", authorized && running);
    }
    /* 巡检按钮 busy 态：调度器在跑或巡检锁占用即禁用（轮询中也走这里刷新） */
    const scanBtn = $("#platformScanBtn");
    if (scanBtn) {
      scanBtn.disabled = busy;
      scanBtn.textContent = busy ? "巡检中…" : "立即巡检";
    }
    /* 抽屉授权引导卡：授权后收起（fail-closed 的反面是不画蛇添足） */
    $("#platformDrawerAuth").classList.toggle("hidden", authorized);
    renderSummary();
  }

  /* ---------- 渲染：预警（只并入摘要计数；新骨架无独立横幅） ---------- */
  function renderWarnings(entries) {
    lastWarnings = Array.isArray(entries) ? entries : [];
    renderSummary();
  }

  /* ---------- 加载失败行（信息流/监控词/巡检记录/信息源四区共用） ----------
     纯 DOM 构建（无插值、不进 innerHTML），文案脱敏（不带原始异常细节）；
     「重试」按区调对应加载函数。样式 .platform-load-error / .platform-load-retry
     在 platform.css（引用 :root 令牌）。 */
  function renderLoadErrorRow(list, retry) {
    if (!list) return;
    const li = document.createElement("li");
    li.className = "platform-load-error";
    const text = document.createElement("span");
    text.textContent = "数据加载失败（网络或服务未就绪）";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "platform-load-retry";
    btn.textContent = "重试";
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      retry();
    });
    li.appendChild(text);
    li.appendChild(btn);
    list.replaceChildren(li);
  }

  /* ---------- 渲染：监控词（抽屉） ---------- */
  function renderWatch(queries) {
    lastWatch = Array.isArray(queries) ? queries : [];
    renderSummary();
    const list = $("#platformWatchList");
    if (!list) return;
    if (queries === null) {
      /* 加载失败与"空清单"分开说：不把故障演成"你没有监控词"（口径同信息源区）。
         编辑行已不可见：先退编辑态，避免卡在只读输入框上等一个看不见的保存对象 */
      cancelWatchEdit();
      renderLoadErrorRow(list, () => reloadWatch());
      return;
    }
    if (!lastWatch.length) {
      list.innerHTML = '<li class="empty">暂无监控词；添加后按调度间隔自动巡检</li>';
      watchEditId = null;               // 列表空了：编辑态失去对象，回到新增态
      syncWatchEditUi();
      return;
    }
    list.innerHTML = lastWatch.map((q) => `
      <li data-qid="${Number(q.id)}" tabindex="0" role="button"
        aria-label="编辑监控词 ${escapeHtml(q.query || "")} 的领域" class="${
        Number(q.id) === watchEditId ? "platform-watch-editing" : ""}">
        <span class="platform-watch-query">${escapeHtml(q.query || "")}</span>
        ${watchDomainBadgeHtml(q)}
        ${q.note ? `<span class="platform-watch-note">${escapeHtml(q.note)}</span>` : ""}
        <button class="platform-watch-del" type="button" title="删除监控词">删除</button>
      </li>`).join("");
    list.querySelectorAll("li[data-qid]").forEach((li) => {
      const qid = Number(li.dataset.qid);
      li.querySelector(".platform-watch-del")
        .addEventListener("click", (ev) => {
          ev.stopPropagation();         // 删除不顺带进入编辑态
          deleteWatch(qid);
        });
      /* 行点击（非删除按钮）→ 编辑态：回填 input + 领域下拉，保存走 PUT watch/{id} */
      li.addEventListener("click", () => startWatchEdit(qid));
      /* 键盘同权：行聚焦后 Enter/Space 进编辑态（target 限定行本身，
         避免行内删除按钮的按键冒泡误触编辑） */
      li.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        if (ev.target !== li) return;
        ev.preventDefault();
        startWatchEdit(qid);
      });
    });
    syncWatchEditUi();
  }

  /* 编辑态三按钮显隐 + 表单回填（watchEditId=null 即新增态）。
     监控词名在编辑态置只读：本契约只改领域（PUT watch/{id} {domain}），
     不给人"改了名字却存不下去"的假象。 */
  function syncWatchEditUi() {
    const input = $("#platformWatchInput");
    const sel = $("#platformWatchDomain");
    const addBtn = $("#platformWatchAddBtn");
    const saveBtn = $("#platformWatchSaveBtn");
    const cancelBtn = $("#platformWatchCancelEditBtn");
    if (!input || !addBtn || !saveBtn || !cancelBtn) return;
    const row = watchEditId === null ? null
      : lastWatch.find((q) => Number(q.id) === watchEditId);
    addBtn.classList.toggle("hidden", watchEditId !== null);
    saveBtn.classList.toggle("hidden", watchEditId === null);
    cancelBtn.classList.toggle("hidden", watchEditId === null);
    if (watchEditId === null) {
      input.readOnly = false;
      input.title = "";
      if (sel) sel.value = "";
      return;
    }
    input.readOnly = true;
    input.title = "监控词名称不可改；这里改的是它的领域";
    if (row) input.value = row.query || "";
    if (sel) sel.value = row ? String(row.domain || "") : "";
  }

  function startWatchEdit(qid) {
    watchEditId = qid;
    const list = $("#platformWatchList");
    if (list) {
      list.querySelectorAll("li[data-qid]").forEach((li) => {
        li.classList.toggle("platform-watch-editing", Number(li.dataset.qid) === qid);
      });
    }
    const input = $("#platformWatchInput");
    if (input) input.focus();
    syncWatchEditUi();
  }

  function cancelWatchEdit() {
    watchEditId = null;
    const input = $("#platformWatchInput");
    if (input) {
      input.value = "";
      input.readOnly = false;
      input.title = "";
    }
    const list = $("#platformWatchList");
    if (list) {
      list.querySelectorAll("li[data-qid]").forEach((li) => {
        li.classList.remove("platform-watch-editing");
      });
    }
    syncWatchEditUi();
  }

  async function saveWatchEdit() {
    if (watchEditId === null) return;
    const sel = $("#platformWatchDomain");
    const domain = sel ? String(sel.value || "") : "";
    try {
      const resp = await fetch(`${PLATFORM_API.watch}/${watchEditId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ domain }),
      });
      if (resp.status === 400) {
        const data = await resp.json().catch(() => ({}));
        scanHint(`保存失败：${data.detail || resp.status}`, true);
        return;
      }
      if (!resp.ok && resp.status !== 404) {
        scanHint(`保存失败：HTTP ${resp.status}`, true);
        return;
      }
      /* 404 = 监控词已被删（多半在别处删的）：视为保存对象消失，刷新列表即可 */
      scanHint("", false);
      watchEditId = null;
      await reloadWatch();
    } catch (e) {
      scanHint(`保存失败：${(e && e.message) || e}`, true);
    }
  }

  /* ---------- 渲染：信息流 ---------- */
  function feedEntryHtml(e) {
    const level = RISK_LEVELS[e.risk_level] || RISK_LEVELS.notice;
    const riskCls = e.risk_level === "high" ? " platform-risk-high"
      : e.risk_level === "urgent" ? " platform-risk-urgent" : "";
    const statusBadge = e.status === "verified"
      ? '<span class="badge green">已深查</span>'
      : e.status === "verify_failed"
        ? '<span class="badge amber">深查失败</span>'
        : '<span class="badge gray">未深查</span>';
    const hasScore = e.risk_score !== null && e.risk_score !== undefined
      && !Number.isNaN(Number(e.risk_score));
    const verdict = e.verdict_label
      ? `${escapeHtml(e.verdict_label)}${hasScore ? `（${Number(e.risk_score)}）` : ""}`
      : "未深查";
    const eOk = eScoreValid(e.e_score);
    const reasons = Array.isArray(e.risk_reasons) ? e.risk_reasons : [];
    const hasDossier = e.dossier_id !== null && e.dossier_id !== undefined
      && e.dossier_id !== "";
    const dossierId = hasDossier ? Number(e.dossier_id) : 0;
    return `<li class="platform-feed-item${riskCls}" data-eid="${Number(e.id)}"
      tabindex="0" role="button" aria-expanded="false">
      <div class="platform-feed-head">
        <span class="badge ${level.cls}">${escapeHtml(level.label)}</span>
        ${feedDomainBadgeHtml(e.topic_domain, e.topic_domain_source)}
        <span class="platform-feed-title">${clipped(e.title, 60)}</span>
        ${statusBadge}
      </div>
      <div class="platform-feed-summary">${clipped(e.snippet, 120)}</div>
      <div class="platform-feed-meta">
        <span>${escapeHtml(e.domain || "")}</span>
        <span>${escapeHtml(fmtTime(e.discovered_at || e.created_at))}</span>
        <span>${fmtE(e.e_score)}${eOk ? '<span class="badge gray">代理/估计</span>' : ""}</span>
        <span>结论：${verdict}</span>
        ${hasDossier ? `<span class="platform-feed-dossier">档案 #${dossierId}</span>` : ""}
      </div>
      <div class="platform-feed-detail hidden">
        <div class="platform-feed-reasons">传播风险理由：${
          reasons.length
            ? `<ol>${reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ol>`
            : "—"}</div>
        <a class="platform-feed-url" href="${safeUrl(e.url)}" target="_blank"
          rel="noopener noreferrer">${clipped(e.url, 90)}</a>
        ${hasDossier ? `<p class="platform-feed-dossier">档案 #${dossierId} 已在档案库，`
          + `可到对话页档案库查看</p>` : ""}
      </div>
    </li>`;
  }

  /* 空态双引导：无监控词 → 文案「还没有监控词，添加后自动巡检」+ 按钮「去添加」；
     有词无信息 → 文案「还没有发现信息」+ 按钮「立即巡检一次」 */
  function renderFeedEmpty() {
    const box = $("#platformEmpty");
    const btn = $("#platformEmptyBtn");
    if (!box || !btn) return;
    emptyKind = lastWatch.length ? "no-entries" : "no-watch";
    const textEl = box.querySelector(".platform-empty-text");
    if (textEl) {
      textEl.textContent = emptyKind === "no-watch"
        ? "还没有监控词，添加后自动巡检" : "还没有发现信息";
    }
    btn.textContent = emptyKind === "no-watch" ? "去添加" : "立即巡检一次";
    box.classList.remove("hidden");
  }

  function renderFeed(entries) {
    lastFeed = Array.isArray(entries) ? entries : [];
    const list = $("#platformFeed");
    if (!list) return;
    if (entries === null) {
      /* 加载失败要落在信息流区域（覆盖骨架的「加载中…」占位），且不出空态引导——
         「去添加/立即巡检」是空态的活，故障时引导反而误导 */
      feedLoadFailed = true;
      const box = $("#platformEmpty");
      if (box) box.classList.add("hidden");
      renderLoadErrorRow(list, () => loadFeed().then(renderFeed));
      return;
    }
    feedLoadFailed = false;
    if (!lastFeed.length) {
      list.innerHTML = '<li class="empty">暂无信息</li>';
      renderFeedEmpty();
      return;
    }
    const box = $("#platformEmpty");
    if (box) box.classList.add("hidden");
    list.innerHTML = lastFeed.map(feedEntryHtml).join("");
    /* 点击卡片展开/收起详情（点原文链接放行：照常开新窗口，不误切详情）；
       键盘同权：条目 tabindex=0 role=button，Enter/Space 展开/收起（Space
       拦掉默认滚动），aria-expanded 跟随，读屏器能感知展开态 */
    list.querySelectorAll("li[data-eid]").forEach((li) => {
      const toggle = () => {
        const detail = li.querySelector(".platform-feed-detail");
        if (!detail) return;
        const nowOpen = !detail.classList.toggle("hidden");
        li.setAttribute("aria-expanded", nowOpen ? "true" : "false");
      };
      li.addEventListener("click", (ev) => {
        if (ev.target.closest("a")) return;
        toggle();
      });
      li.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        if (ev.target.closest("a")) return;
        ev.preventDefault();
        toggle();
      });
    });
  }

  /* ---------- 渲染：信息源（抽屉） ---------- */
  function renderSources(sources) {
    lastSources = Array.isArray(sources) ? sources : [];
    const list = $("#platformSourceList");
    if (!list) return;
    if (sources === null) {
      /* 加载失败与"空清单"分开说：不把故障演成"你没有信息源"；重试不用再切视图 */
      renderLoadErrorRow(list, () => loadSources().then(renderSources));
      return;
    }
    if (!lastSources.length) {
      list.innerHTML = '<li class="empty">暂无自建信息源；默认用平台规则里的 RSS 源与搜索引擎</li>';
      return;
    }
    list.innerHTML = lastSources.map((s) => {
      const sid = Number(s.id);
      const kind = String(s.kind || "") === "page" ? "page" : "rss";
      const kindLabel = kind === "page" ? "网页" : "RSS";
      const binds = Array.isArray(s.query_ids) ? s.query_ids : [];
      const bindLabel = binds.length ? `绑定: ${binds.length} 个词` : "全局";
      const on = !!s.enabled;
      return `<li data-sid="${sid}" class="${on ? "" : "platform-source-off"}">
        <span class="platform-source-name">${escapeHtml(s.name || s.url || "未命名源")}</span>
        <span class="badge gray">${kindLabel}</span>
        <span class="badge gray">${bindLabel}</span>
        <span class="platform-source-url">${clipped(s.url, 120)}</span>
        <div class="platform-source-op">
          <button type="button" data-op="toggle">${on ? "停用" : "启用"}</button>
          <button type="button" data-op="test" title="对该源单独试跑一次发现">试一下</button>
          <button type="button" data-op="del" title="删除信息源">删除</button>
        </div>
      </li>`;
    }).join("");
    list.querySelectorAll("li[data-sid]").forEach((li) => {
      const sid = Number(li.dataset.sid);
      li.querySelector('[data-op="toggle"]').addEventListener("click", () => toggleSource(sid));
      li.querySelector('[data-op="test"]').addEventListener("click", () => testSource(sid));
      li.querySelector('[data-op="del"]').addEventListener("click", () => deleteSource(sid));
    });
  }

  /* 信息源区提示行：添加/启停/删除/试运行的反馈都走这里（不弹窗） */
  function sourceHint(text, isError) {
    const box = $("#platformSourceHint");
    if (!box) return;
    box.textContent = text || "";
    box.classList.toggle("platform-source-hint-error", !!isError);
  }

  async function addSource() {
    const nameEl = $("#platformSourceName");
    const urlEl = $("#platformSourceUrl");
    const kindEl = $("#platformSourceKind");
    const bindEl = $("#platformSourceBindings");
    if (!nameEl || !urlEl || !kindEl) return;
    const name = nameEl.value.trim();
    const url = urlEl.value.trim();
    if (!name || !url) {
      sourceHint("来源名称与 URL 都要填", true);
      return;
    }
    const query_ids = bindEl
      ? Array.from(bindEl.selectedOptions).map((o) => Number(o.value))
        .filter((n) => !Number.isNaN(n))
      : [];                              // 不选 = 全局源（对所有监控词生效）
    try {
      const resp = await fetch(PLATFORM_API.sources, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, url, kind: kindEl.value, note: "", query_ids }),
      });
      if (resp.status === 409) {
        sourceHint("该来源 URL 已存在（同一 URL 只收一个源）", true);
        return;
      }
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        sourceHint(`添加失败：${data.detail || resp.status}`, true);
        return;
      }
      nameEl.value = "";
      urlEl.value = "";
      sourceHint("", false);
      await Promise.all([loadSources(), loadWatch()]).then(([sources, watch]) => {
        renderSources(sources);
        renderWatch(watch);              // 绑定多选选项跟着监控词清单走
      });
      fillDomainSelects();
    } catch (e) {
      sourceHint(`添加失败：${(e && e.message) || e}`, true);
    }
  }

  /* 改信息源（PUT /sources/{id}）：启停/备注/绑定都走这一个入口，返回原始 resp
     供调用方分别处理 404/4xx。当前 UI 只有启停用它（备注与绑定在添加时定）。 */
  async function updateSource(sid, payload) {
    return fetch(`${PLATFORM_API.sources}/${sid}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  async function toggleSource(sid) {
    const row = lastSources.find((s) => Number(s.id) === sid);
    const next = row ? !row.enabled : true;
    try {
      const resp = await updateSource(sid, { enabled: next });
      if (resp.status === 404) {
        sourceHint("信息源已不存在，列表已刷新", true);
        await loadSources().then(renderSources);
        return;
      }
      if (!resp.ok) {
        sourceHint(`操作失败：HTTP ${resp.status}`, true);
        return;
      }
      sourceHint(next ? "已启用" : "已停用（绑定保留，巡检时对该词不可见）", false);
      await loadSources().then(renderSources);
    } catch (e) {
      sourceHint(`操作失败：${(e && e.message) || e}`, true);
    }
  }

  async function deleteSource(sid) {
    try {
      const resp = await fetch(`${PLATFORM_API.sources}/${sid}`, { method: "DELETE" });
      if (!resp.ok && resp.status !== 404) {
        sourceHint(`删除失败：HTTP ${resp.status}`, true);
        return;
      }
      sourceHint("已删除", false);
      await loadSources().then(renderSources);
    } catch (e) {
      sourceHint(`删除失败：${(e && e.message) || e}`, true);
    }
  }

  async function testSource(sid) {
    const row = lastSources.find((s) => Number(s.id) === sid);
    const query = row ? String(row.name || row.note || "") : "";
    sourceHint("试运行中（同步抓取，稍候）…", false);
    try {
      const resp = await fetch(`${PLATFORM_API.sources}/${sid}/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        sourceHint(`试运行失败：${data.detail || resp.status}`, true);
        return;
      }
      const titles = Array.isArray(data.sample_titles) ? data.sample_titles : [];
      const sample = titles.length
        ? `（如：${titles.map((t) => String(t).slice(0, 24)).join("；")}）` : "";
      sourceHint(data.ok
        ? `试运行成功：${data.item_count} 条匹配${sample}`
        : `试运行未取到结果：${data.error || "未知原因"}`, !data.ok);
    } catch (e) {
      sourceHint(`试运行失败：${(e && e.message) || e}`, true);
    }
  }

  /* ---------- 渲染：巡检时间线（抽屉） ---------- */
  function renderScans(scans) {
    lastScans = Array.isArray(scans) ? scans : [];
    renderSummary();   // 摘要的「最近巡检 HH:MM」取自这里的时间戳
    const list = $("#platformScans");
    if (!list) return;
    if (scans === null) {
      /* 加载失败与"空清单"分开说：不把故障演成"从没巡检过" */
      renderLoadErrorRow(list, () => loadScans().then(renderScans));
      return;
    }
    if (!lastScans.length) {
      list.innerHTML = '<li class="empty">暂无巡检记录</li>';
      return;
    }
    list.innerHTML = lastScans.map((s) => {
      const raw = String(s.status || "");
      const text = SCAN_STATUS[raw] || raw || "—";
      const cls = raw === "failed" ? "red"
        : raw === "partial" || raw === "skipped" ? "amber" : "gray";
      return `<li>
        <span class="badge ${cls}">${escapeHtml(text)}</span>
        <span class="platform-scan-time">${escapeHtml(fmtTime(s.started_at))}</span>
        <span class="platform-scan-counters">候选 ${Number(s.candidates) || 0}`
          + ` · 深查 ${Number(s.verified) || 0} · 降级 ${Number(s.degraded) || 0}</span>
        ${s.error ? `<div class="platform-scan-error">${escapeHtml(s.error)}</div>` : ""}
      </li>`;
    }).join("");
  }

  /* ---------- 动作 ---------- */
  /* 监控词清单重取 + 重画 + 绑定多选同步（新增/删除/保存领域后都走这里） */
  async function reloadWatch() {
    const watch = await loadWatch();
    renderWatch(watch);
    fillDomainSelects();
  }

  async function refreshAll() {
    try {
      /* 领域表先取：徽章中文名与领域下拉都吃它；失败只退成"显示原始 id + 只有自动推断项" */
      lastDomains = await loadDomains();
      /* 六路并行：状态 / 预警 / 监控词 / 信息流 / 巡检记录 / 信息源；单路失败
         自行降级成对应区域的失败行（render* 收 null 落错误态），不再演成空数据 */
      const [status, warnings, watch, feed, scans, sources] = await Promise.all(
        [loadStatus(), loadWarnings(), loadWatch(), loadFeed(), loadScans(), loadSources()]);
      renderStatus(status);
      renderWarnings(warnings);
      renderWatch(watch);
      renderFeed(feed);
      renderScans(scans);
      renderSources(sources);
      fillDomainSelects();     // 绑定多选选项来自刚拿到的监控词清单
    } catch (e) { /* 平台数据加载失败不弹窗；切走再切回本视图再试 */
    }
  }

  async function addWatch() {
    const input = $("#platformWatchInput");
    if (!input) return;
    const query = input.value.trim();
    if (!query) return;
    try {
      const resp = await fetch(PLATFORM_API.watch, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      if (resp.status === 409) {
        scanHint("该监控词已存在", false);
        return;
      }
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        scanHint(`添加失败：${data.detail || resp.status}`, true);
        return;
      }
      input.value = "";
      scanHint("", false);
      await reloadWatch();
      /* 刚补上第一个监控词会改变空态口径（无词 → 有词无信息）：空态跟着重画；
         信息流正处失败态时不出空态引导（故障与空数据分开说） */
      if (!lastFeed.length && !feedLoadFailed) renderFeedEmpty();
    } catch (e) {
      scanHint(`添加失败：${(e && e.message) || e}`, true);
    }
  }

  async function deleteWatch(qid) {
    if (watchEditId === qid) cancelWatchEdit();   // 删的正是编辑行：先退编辑态
    try {
      const resp = await fetch(`${PLATFORM_API.watch}/${qid}`, { method: "DELETE" });
      if (!resp.ok && resp.status !== 404) {
        scanHint(`删除失败：HTTP ${resp.status}`, true);
        return;
      }
      scanHint("", false);
      /* 404 视为已删；删除会级联清掉该词下的信息流条目，信息流一起刷新，不摆陈旧数据 */
      await Promise.all([reloadWatch(), loadFeed()]).then(([, feed]) => {
        renderFeed(feed);
      });
    } catch (e) {
      scanHint(`删除失败：${(e && e.message) || e}`, true);
    }
  }

  function scanHint(text, isError) {
    const box = $("#platformScanHint");
    if (!box) return;
    if (!text) {
      box.classList.add("hidden");
      box.classList.remove("platform-scan-hint-error");
      box.textContent = "";
      return;
    }
    box.textContent = text;
    box.classList.remove("hidden");
    box.classList.toggle("platform-scan-hint-error", !!isError);
  }

  async function triggerScan() {
    let resp;
    try {
      resp = await fetch(PLATFORM_API.scan, { method: "POST" });
    } catch (e) {
      scanHint(`触发失败：${(e && e.message) || e}`, true);
      return;
    }
    if (!resp.ok) {
      scanHint(`触发失败：HTTP ${resp.status}`, true);
      return;
    }
    const data = await resp.json().catch(() => ({}));
    if (!data.started) {
      scanHint("已有巡检在进行中（单飞，不叠加）", false);
      return;
    }
    scanHint("巡检已启动…", false);
    pollScanDone();
  }

  function pollScanDone() {
    if (scanPollTimer) clearTimeout(scanPollTimer);
    const startedAt = Date.now();
    const tick = async () => {
      let status = null;
      try {
        status = await loadStatus();
      } catch (e) { /* 轮询失败：下次继续 */ }
      if (status) renderStatus(status);   // 轮询中也刷新状态（按钮 busy 态/摘要）
      const done = status && !status.scan_in_progress;
      const timedOut = Date.now() - startedAt > SCAN_POLL_TIMEOUT_MS;
      if (done || timedOut) {
        scanPollTimer = null;
        scanHint(timedOut
          ? "巡检仍在进行（超过等待上限）；切走再切回本视图可查看进展"
          : "巡检结束", false);
        await refreshAll();
        return;
      }
      scanPollTimer = setTimeout(tick, SCAN_POLL_MS);
    };
    tick();
  }

  /* ---------- 管理抽屉 ---------- */
  /* 抽屉内可聚焦元素（可见且未禁用）：开抽屉的 focus 落点与 Tab 循环共用；
     offsetParent 过滤掉 .hidden 区块（如授权后收起的引导卡）里的死焦点 */
  function drawerFocusables() {
    const drawer = $("#platformDrawer");
    if (!drawer) return [];
    return Array.from(drawer.querySelectorAll(
      "button, input, select, textarea, a[href], [tabindex]"))
      .filter((el) => !el.disabled && el.offsetParent !== null
        && el.getAttribute("tabindex") !== "-1");
  }

  function openDrawer() {
    const drawer = $("#platformDrawer");
    if (!drawer) return;
    drawer.classList.remove("hidden");
    $("#platformDrawerMask").classList.remove("hidden");
    const closeBtn = $("#platformDrawerClose");
    if (closeBtn) closeBtn.focus();       // 焦点进抽屉，键盘/读屏用户不被留在背景页
  }

  function closeDrawer() {
    const drawer = $("#platformDrawer");
    if (!drawer || drawer.classList.contains("hidden")) return;   // 已关：不重复动作、不抢焦点
    drawer.classList.add("hidden");
    $("#platformDrawerMask").classList.add("hidden");
    const manage = $("#platformManageBtn");
    if (manage) manage.focus();           // 焦点还给「管理」按钮（打开它的入口）
  }

  function toggleDrawer() {
    const drawer = $("#platformDrawer");
    if (drawer.classList.contains("hidden")) openDrawer();
    else closeDrawer();
  }

  /* ---------- 绑定 ---------- */
  function bind() {
    const tabs = $("#mainTabs");
    if (!tabs) return;   // 骨架不完整（导航缺失）时安静退出，不抛错
    $("#navChat").addEventListener("click", () => showView("chat"));
    $("#navPlatform").addEventListener("click", () => showView("platform"));
    $("#platformScanBtn").addEventListener("click", triggerScan);
    $("#platformManageBtn").addEventListener("click", toggleDrawer);
    $("#platformWatchAddBtn").addEventListener("click", addWatch);
    $("#platformWatchInput").addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter") return;
      /* 编辑态（输入框只读、只改领域）Enter=保存；新增态 Enter=添加 */
      if (watchEditId !== null) saveWatchEdit();
      else addWatch();
    });
    /* 监控词编辑态：保存 / 取消（保存走 PUT watch/{id} {domain}） */
    $("#platformWatchSaveBtn").addEventListener("click", saveWatchEdit);
    $("#platformWatchCancelEditBtn").addEventListener("click", cancelWatchEdit);
    /* 信息源：添加 / 试一下都在表单里回车即可触发 */
    $("#platformSourceAddBtn").addEventListener("click", addSource);
    $("#platformSourceName").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") addSource();
    });
    $("#platformSourceUrl").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") addSource();
    });
    $("#platformAuthSettingsBtn").addEventListener("click", () => {
      /* 设置弹层由 shell.js/settings.js 管理；点它的入口按钮即可打开 */
      const btn = document.getElementById("settingsBtn");
      if (btn) btn.click();
    });
    $("#platformDrawerMask").addEventListener("click", closeDrawer);
    $("#platformDrawerClose").addEventListener("click", closeDrawer);
    $("#platformEmptyBtn").addEventListener("click", () => {
      /* 按当前空态类型动作：无监控词 → 开抽屉并聚焦输入框；否则触发一次巡检 */
      if (emptyKind === "no-watch") {
        openDrawer();
        const input = $("#platformWatchInput");
        if (input) input.focus();
      } else {
        triggerScan();
      }
    });
    /* 筛选 chips：行内委托（属性选择器对空 data-risk 同样命中）；
       aria-pressed 与 active 类同步切（读屏器靠它感知当前档位） */
    const filterRow = $("#platformFilterRow");
    if (filterRow) {
      filterRow.addEventListener("click", (ev) => {
        const btn = ev.target.closest("button[data-risk]");
        if (!btn || !filterRow.contains(btn)) return;
        filterRow.querySelectorAll("button[data-risk]")
          .forEach((b) => {
            b.classList.remove("active");
            b.setAttribute("aria-pressed", "false");
          });
        btn.classList.add("active");
        btn.setAttribute("aria-pressed", "true");
        feedRiskFilter = btn.dataset.risk || "";
        loadFeed().then(renderFeed);
      });
    }
    /* 焦点陷阱：抽屉开着时 Tab/Shift+Tab 在抽屉内循环，不漏到背景页 */
    document.addEventListener("keydown", (ev) => {
      if (ev.key !== "Tab") return;
      const drawer = $("#platformDrawer");
      if (!drawer || drawer.classList.contains("hidden")) return;
      const items = drawerFocusables();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      const inside = drawer.contains(active);
      if (ev.shiftKey) {
        if (!inside || active === first) { ev.preventDefault(); last.focus(); }
      } else if (!inside || active === last) {
        ev.preventDefault(); first.focus();
      }
    });
    /* Esc 关抽屉（仅开着时） */
    document.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      const drawer = $("#platformDrawer");
      if (drawer && !drawer.classList.contains("hidden")) closeDrawer();
    });
    showView("chat");   // 初始默认对话视图（HTML 已是该态，这里归一化 active/aria-current）
  }

  bind();
})();
