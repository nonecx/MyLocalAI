/* ============================================================
   platform.js —— 监测平台视图前端（P6）
   独立文件：只依赖 index.html 的元素，不碰 chat.js 的契约。
   职责：
     - 顶部导航切换：对话视图 ↔ 监测平台视图（默认对话）；
     - 拉取 /api/platform/{status,feed,warnings,watch,scans} 渲染：
       调度状态、预警横幅、监控词管理、信息流（风险分级徽章）、巡检历史；
     - 手动巡检：POST /api/platform/scan → 轮询 status 直到 scan_in_progress
       结束 → 刷新信息流/预警/历史（单飞：后端忙时 started=false 如实提示）；
     - 未授权引导态：外部检索默认关闭（fail-closed），出引导卡链到设置弹层，
       不静默失败、不假装有数据。
   口径：风险分级只描述传播风险、不判定真假；热度是代理/估计值（徽章随行）。
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
     notice=关注 / suspect=疑似 / high=高危 / urgent=紧急） */
  const RISK_LEVELS = {
    notice: { label: "关注", cls: "gray" },
    suspect: { label: "疑似", cls: "amber" },
    high: { label: "高危", cls: "red" },
    urgent: { label: "紧急", cls: "red" },
  };

  /* 巡检状态 → 中文（与 CLI _STATUS_TEXT 同口径） */
  const SCAN_STATUS = {
    completed: "完成", partial: "部分完成", cancelled: "已取消",
    skipped: "跳过", failed: "失败", running: "进行中",
  };

  const SCAN_POLL_MS = 2000;
  const SCAN_POLL_TIMEOUT_MS = 150000;   // 与 deep_verify deadline 同量级的上限

  let feedRiskFilter = "";                // 当前信息流筛选（"" / "high,urgent" / "suspect"）
  let scanPollTimer = null;

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmtTime(s) {
    return String(s || "").replace("T", " ").slice(0, 19) || "—";
  }

  function fmtE(e) {
    return (e === null || e === undefined || e === "") ? "E=?" : `E=${Number(e).toFixed(1)}`;
  }

  /* ---------- 视图切换 ---------- */
  function showView(which) {
    const isPlatform = which === "platform";
    $("#navChat").classList.toggle("active", !isPlatform);
    $("#navPlatform").classList.toggle("active", isPlatform);
    $("#navChat").setAttribute("aria-current", isPlatform ? "false" : "page");
    $("#navPlatform").setAttribute("aria-current", isPlatform ? "page" : "false");
    $(".chat-layout").classList.toggle("hidden", isPlatform);
    $("#platformView").classList.toggle("hidden", !isPlatform);
    if (isPlatform) refreshAll();
  }

  /* ---------- 数据加载 ---------- */
  async function loadStatus() {
    const resp = await fetch(PLATFORM_API.status);
    if (!resp.ok) return null;
    return resp.json();
  }

  async function loadFeed() {
    const url = feedRiskFilter
      ? `${PLATFORM_API.feed}?risk=${encodeURIComponent(feedRiskFilter)}&limit=50`
      : `${PLATFORM_API.feed}?limit=50`;
    const resp = await fetch(url);
    if (!resp.ok) return [];
    const data = await resp.json();
    return (data && data.entries) || [];
  }

  async function loadWarnings() {
    const resp = await fetch(PLATFORM_API.warnings + "?limit=20");
    if (!resp.ok) return [];
    const data = await resp.json();
    return (data && data.entries) || [];
  }

  async function loadWatch() {
    const resp = await fetch(PLATFORM_API.watch);
    if (!resp.ok) return [];
    const data = await resp.json();
    return (data && data.queries) || [];
  }

  async function loadScans() {
    const resp = await fetch(PLATFORM_API.scans + "?limit=10");
    if (!resp.ok) return [];
    const data = await resp.json();
    return (data && data.scans) || [];
  }

  /* ---------- 渲染：状态 ---------- */
  function renderStatus(status) {
    if (!status) return;
    const authorized = !!status.external_authorized;
    const authChip = $("#platformAuthState");
    authChip.textContent = authorized ? "外部检索已授权" : "外部检索未授权";
    authChip.className = `status-chip ${authorized ? "ok" : "bad"}`;
    $("#platformAuthGuide").classList.toggle("hidden", authorized);

    const sched = $("#platformSchedulerState");
    if (status.running) {
      sched.textContent = `调度器：运行中（间隔 ${status.interval_s}s）`;
    } else if (status.enabled) {
      sched.textContent = "调度器：已开启但未运行";
    } else {
      sched.textContent = "调度器：关闭（platform_rules.json 的 enabled=false）";
    }
    const last = status.last_scan || {};
    const lastEl = $("#platformLastScan");
    if (last.status) {
      const c = last.counters || {};
      lastEl.textContent = `最近巡检：${SCAN_STATUS[last.status] || last.status}`
        + `（候选 ${c.candidates || 0} · 深查 ${c.verified || 0}）`;
    } else {
      lastEl.textContent = "最近巡检：无记录";
    }
  }

  /* ---------- 渲染：预警横幅 ---------- */
  function renderWarnings(entries) {
    const banner = $("#platformWarningBanner");
    if (!entries.length) {
      banner.classList.add("hidden");
      banner.innerHTML = "";
      return;
    }
    const urgent = entries.filter((e) => e.risk_level === "urgent").length;
    const high = entries.length - urgent;
    banner.classList.remove("hidden");
    banner.innerHTML = `
      <div class="platform-warning-title">⚠ ${entries.length} 条信息存在大规模传播风险</div>
      <div class="platform-warning-sub">紧急 ${urgent} · 高危 ${high}
        （传播风险分级，不判定内容真假；点「信息流 → 预警」查看）</div>
      <ul class="platform-warning-list">${entries.slice(0, 3).map((e) => `
        <li><span class="badge ${(RISK_LEVELS[e.risk_level] || RISK_LEVELS.notice).cls}">${
          escapeHtml((RISK_LEVELS[e.risk_level] || RISK_LEVELS.notice).label)}</span>
          ${escapeHtml(String(e.title || "").slice(0, 50))}</li>`).join("")}</ul>`;
  }

  /* ---------- 渲染：监控词 ---------- */
  function renderWatch(queries) {
    $("#platformWatchCount").textContent = String(queries.length);
    const list = $("#platformWatchList");
    if (!queries.length) {
      list.innerHTML = '<li class="empty">暂无监控词；添加后按调度间隔自动巡检'
        + '（也可点「立即巡检一次」）</li>';
      return;
    }
    list.innerHTML = queries.map((q) => `
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
    const verdict = e.verdict_label
      ? `${escapeHtml(e.verdict_label)}${e.risk_score !== null && e.risk_score !== undefined
        ? `（${Number(e.risk_score)}）` : ""}`
      : "未深查";
    const statusBadge = e.status === "verified"
      ? '<span class="badge green">已深查</span>'
      : e.status === "verify_failed"
        ? '<span class="badge amber">深查失败</span>'
        : '<span class="badge gray">未深查</span>';
    const reasons = Array.isArray(e.risk_reasons) ? e.risk_reasons : [];
    const dossier = e.dossier_id
      ? `<span class="platform-feed-dossier">档案 #${Number(e.dossier_id)}（档案库可查）</span>`
      : "";
    return `<li class="platform-feed-item" data-eid="${Number(e.id)}">
      <div class="platform-feed-head">
        <span class="badge ${level.cls}">${escapeHtml(level.label)}</span>
        <span class="platform-feed-title">${escapeHtml(String(e.title || "").slice(0, 60))}</span>
        ${statusBadge}
      </div>
      <div class="platform-feed-meta">
        <span>${fmtE(e.e_score)}<span class="badge gray">代理/估计</span></span>
        <span>结论：${verdict}</span>
        <span>${escapeHtml(e.domain || "")}</span>
        <span>发现：${fmtTime(e.discovered_at || e.created_at)}</span>
        ${dossier}
      </div>
      <div class="platform-feed-reasons">传播风险理由：${
        reasons.map(escapeHtml).join("；") || "—"}</div>
      <a class="platform-feed-url" href="${escapeHtml(e.url || "#")}" target="_blank"
        rel="noopener noreferrer">${escapeHtml(String(e.url || "").slice(0, 90))}</a>
    </li>`;
  }

  function renderFeed(entries) {
    const list = $("#platformFeed");
    if (!entries.length) {
      list.innerHTML = '<li class="empty">暂无信息；完成一次巡检后，'
        + '发现的热门信息会列在这里</li>';
      return;
    }
    list.innerHTML = entries.map(feedEntryHtml).join("");
  }

  /* ---------- 渲染：巡检历史 ---------- */
  function renderScans(scans) {
    const list = $("#platformScans");
    if (!scans.length) {
      list.innerHTML = '<li class="empty">暂无巡检记录</li>';
      return;
    }
    list.innerHTML = scans.map((s) => {
      const text = SCAN_STATUS[s.status] || s.status || "—";
      const cls = s.status === "failed" ? "red" : s.status === "skipped" ? "amber" : "gray";
      return `<li>
        <span class="badge ${cls}">${escapeHtml(text)}</span>
        <span class="platform-scan-time">${fmtTime(s.started_at)}</span>
        <span class="platform-scan-counters">候选 ${Number(s.candidates) || 0}
          · 深查 ${Number(s.verified) || 0} · 降级 ${Number(s.degraded) || 0}</span>
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
    } catch (e) { /* 平台数据加载失败不弹窗；下颗刷新按钮或切视图再试 */ }
  }

  async function addWatch() {
    const input = $("#platformWatchInput");
    const query = input.value.trim();
    if (!query) return;
    try {
      const resp = await fetch(PLATFORM_API.watch, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      if (resp.status === 409) {
        scanHint("该监控词已存在", true);
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
      await loadWatch().then(renderWatch);
    } catch (e) {
      scanHint(`删除失败：${(e && e.message) || e}`, true);
    }
  }

  function scanHint(text, isError) {
    const box = $("#platformScanHint");
    if (!text) {
      box.classList.add("hidden");
      box.textContent = "";
      return;
    }
    box.textContent = text;
    box.classList.remove("hidden");
    box.classList.toggle("platform-scan-hint-error", !!isError);
  }

  async function triggerScan() {
    scanHint("巡检已启动，正在后台执行……", false);
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
      scanHint("已有巡检在进行中（单飞，不叠加），稍后点「刷新」看结果", false);
      return;
    }
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
      const done = status && !status.scan_in_progress;
      const timedOut = Date.now() - startedAt > SCAN_POLL_TIMEOUT_MS;
      if (done || timedOut) {
        scanPollTimer = null;
        scanHint(timedOut ? "巡检仍在进行（超过等待上限），点「刷新」查看进展" : "巡检结束",
                 false);
        await refreshAll();
        return;
      }
      scanPollTimer = setTimeout(tick, SCAN_POLL_MS);
    };
    tick();
  }

  /* ---------- 绑定 ---------- */
  function bind() {
    $("#navChat").addEventListener("click", () => showView("chat"));
    $("#navPlatform").addEventListener("click", () => showView("platform"));
    $("#platformScanBtn").addEventListener("click", triggerScan);
    $("#platformRefreshBtn").addEventListener("click", refreshAll);
    $("#platformWatchAddBtn").addEventListener("click", addWatch);
    $("#platformWatchInput").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") addWatch();
    });
    $("#platformAuthSettingsBtn").addEventListener("click", () => {
      /* 设置弹层由 shell.js/settings.js 管理；点它的入口按钮即可打开 */
      const btn = document.getElementById("settingsBtn");
      if (btn) btn.click();
    });
    document.querySelectorAll(".platform-feed-filter").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".platform-feed-filter")
          .forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        feedRiskFilter = btn.dataset.risk || "";
        loadFeed().then(renderFeed);
      });
    });
  }

  bind();
})();
