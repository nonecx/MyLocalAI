/* ============================================================
   platform.js —— 监测平台视图前端（方案 G：状态摘要条 + 筛选 chips + 卡片流 + 管理抽屉）
   独立文件：只依赖 index.html 的元素，不碰 chat.js 的契约。
   职责：
     - 通栏 Tab 切换：对话视图 ↔ 监测平台视图（默认对话；platform.js 接管）；
     - 拉取 /api/platform/{status,feed,warnings,watch,scan,scans} 渲染：
       状态摘要条（状态点三态 + 诚实话术）、筛选后信息流（风险分级徽章）、
       监控词管理（右侧抽屉）、巡检时间线；
     - 手动巡检：POST /api/platform/scan → 轮询 status 直到 scan_in_progress
       结束 → 刷新全部（单飞：后端忙时 started=false 如实提示）；
     - 空态双引导：没有监控词 → 引导去抽屉添加；有词无信息 → 引导立即巡检；
     - 未授权引导态：外部检索默认关闭（fail-closed），抽屉里出引导卡链到设置弹层，
       不静默失败、不假装有数据。
   口径：风险分级只描述传播风险、不判定真假；热度是代理/估计值（徽章随行）；
        所有外部文本一律 escapeHtml 后再进 innerHTML，判空再渲染，不猜数据。
   ============================================================ */
"use strict";

(function () {
  /* file:// 双击打开时 shell.js 已给出启动引导，这里静默退出 */
  if (location.protocol === "file:") return;

  const $ = (sel) => document.querySelector(sel);

  /* 后端接口清单（与 web/platform_api.py 路由表一一对应，测试做前后端自洽断言） */
  const PLATFORM_API = {
    status: "/api/platform/status",
    feed: "/api/platform/feed",
    warnings: "/api/platform/warnings",
    watch: "/api/platform/watch",
    scan: "/api/platform/scan",
    scans: "/api/platform/scans",
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
  /* 上一轮加载结果：摘要话术要跨 render 函数取数（状态点/摘要/空态口径都依赖） */
  let lastStatus = null;
  let lastWarnings = [];
  let lastWatch = [];
  let lastFeed = [];
  let lastScans = [];

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
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

  /* ---------- 数据加载（任一路失败自行降级成空壳，不拖垮其余四路） ---------- */
  async function loadStatus() {
    try {
      const resp = await fetch(PLATFORM_API.status);
      if (!resp.ok) return null;
      return resp.json();
    } catch (e) { return null; }
  }

  async function loadFeed() {
    try {
      const url = feedRiskFilter
        ? `${PLATFORM_API.feed}?risk=${encodeURIComponent(feedRiskFilter)}&limit=50`
        : `${PLATFORM_API.feed}?limit=50`;
      const resp = await fetch(url);
      if (!resp.ok) return [];
      const data = await resp.json();
      return (data && data.entries) || [];
    } catch (e) { return []; }
  }

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
      if (!resp.ok) return [];
      const data = await resp.json();
      return (data && data.queries) || [];
    } catch (e) { return []; }
  }

  async function loadScans() {
    try {
      const resp = await fetch(`${PLATFORM_API.scans}?limit=10`);
      if (!resp.ok) return [];
      const data = await resp.json();
      return (data && data.scans) || [];
    } catch (e) { return []; }
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

  function renderStatus(status) {
    const bar = $("#platformStatusBar");
    if (!status || !bar) return;
    lastStatus = status;
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

  /* ---------- 渲染：监控词（抽屉） ---------- */
  function renderWatch(queries) {
    lastWatch = Array.isArray(queries) ? queries : [];
    renderSummary();
    const list = $("#platformWatchList");
    if (!list) return;
    if (!lastWatch.length) {
      list.innerHTML = '<li class="empty">暂无监控词；添加后按调度间隔自动巡检</li>';
      return;
    }
    list.innerHTML = lastWatch.map((q) => `
      <li data-qid="${Number(q.id)}">
        <span class="platform-watch-query">${escapeHtml(q.query || "")}</span>
        ${q.note ? `<span class="platform-watch-note">${escapeHtml(q.note)}</span>` : ""}
        <button class="platform-watch-del" type="button" title="删除监控词">删除</button>
      </li>`).join("");
    list.querySelectorAll("li[data-qid]").forEach((li) => {
      li.querySelector(".platform-watch-del")
        .addEventListener("click", () => deleteWatch(Number(li.dataset.qid)));
    });
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
    return `<li class="platform-feed-item${riskCls}" data-eid="${Number(e.id)}">
      <div class="platform-feed-head">
        <span class="badge ${level.cls}">${escapeHtml(level.label)}</span>
        <span class="platform-feed-title">${escapeHtml(String(e.title || "").slice(0, 60))}</span>
        ${statusBadge}
      </div>
      <div class="platform-feed-summary">${escapeHtml(String(e.snippet || "").slice(0, 120))}</div>
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
        <a class="platform-feed-url" href="${escapeHtml(e.url || "#")}" target="_blank"
          rel="noopener noreferrer">${escapeHtml(String(e.url || "").slice(0, 90))}</a>
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
    if (!lastFeed.length) {
      list.innerHTML = '<li class="empty">暂无信息</li>';
      renderFeedEmpty();
      return;
    }
    const box = $("#platformEmpty");
    if (box) box.classList.add("hidden");
    list.innerHTML = lastFeed.map(feedEntryHtml).join("");
    /* 点击卡片展开/收起详情（点原文链接放行：照常开新窗口，不误切详情） */
    list.querySelectorAll("li[data-eid]").forEach((li) => {
      li.addEventListener("click", (ev) => {
        if (ev.target.closest("a")) return;
        const detail = li.querySelector(".platform-feed-detail");
        if (detail) detail.classList.toggle("hidden");
      });
    });
  }

  /* ---------- 渲染：巡检时间线（抽屉） ---------- */
  function renderScans(scans) {
    lastScans = Array.isArray(scans) ? scans : [];
    renderSummary();   // 摘要的「最近巡检 HH:MM」取自这里的时间戳
    const list = $("#platformScans");
    if (!list) return;
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
  async function refreshAll() {
    try {
      const [status, warnings, watch, feed, scans] = await Promise.all(
        [loadStatus(), loadWarnings(), loadWatch(), loadFeed(), loadScans()]);
      renderStatus(status);
      renderWarnings(warnings);
      renderWatch(watch);
      renderFeed(feed);
      renderScans(scans);
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
      await loadWatch().then(renderWatch);
      /* 刚补上第一个监控词会改变空态口径（无词 → 有词无信息）：空态跟着重画 */
      if (!lastFeed.length) renderFeedEmpty();
    } catch (e) {
      scanHint(`添加失败：${(e && e.message) || e}`, true);
    }
  }

  async function deleteWatch(qid) {
    try {
      const resp = await fetch(`${PLATFORM_API.watch}/${qid}`, { method: "DELETE" });
      if (!resp.ok && resp.status !== 404) {
        scanHint(`删除失败：HTTP ${resp.status}`, true);
        return;
      }
      scanHint("", false);
      /* 404 视为已删；删除会级联清掉该词下的信息流条目，信息流一起刷新，不摆陈旧数据 */
      await Promise.all([loadWatch(), loadFeed()]).then(([watch, feed]) => {
        renderWatch(watch);
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
  function openDrawer() {
    $("#platformDrawer").classList.remove("hidden");
    $("#platformDrawerMask").classList.remove("hidden");
  }

  function closeDrawer() {
    $("#platformDrawer").classList.add("hidden");
    $("#platformDrawerMask").classList.add("hidden");
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
      if (ev.key === "Enter") addWatch();
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
    /* 筛选 chips：行内委托（属性选择器对空 data-risk 同样命中） */
    const filterRow = $("#platformFilterRow");
    if (filterRow) {
      filterRow.addEventListener("click", (ev) => {
        const btn = ev.target.closest("button[data-risk]");
        if (!btn || !filterRow.contains(btn)) return;
        filterRow.querySelectorAll("button[data-risk]")
          .forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        feedRiskFilter = btn.dataset.risk || "";
        loadFeed().then(renderFeed);
      });
    }
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
