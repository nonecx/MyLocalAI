/* ============================================================
   batch.js —— 左栏「材料批次」面板（专业用户 · 材料分析）

   定位：role=analyst 的档案工作区才显示（window.ProfilesPanel.snapshot()
   .active.role === "analyst"；快照为空时隐藏）。把多条材料一次丢进去批量
   核查：三种输入模式 text / file / urls，进度实时刷新，结束后出汇总卡。

   接口契约（与 web/batch_api.py、web/materials_api.py 逐条对应；路径参数
   与后端同名 {batch_id}，生成的 URL 形状逐字一致）：
     - POST /api/materials/upload   multipart 字段 file
       → 200 {"items":[...], "count":N, "filename":"..."} / 400 {"detail":"..."}
       前端在 file 模式选中文件后立即调用，items 只暂存在内存变量里；
     - POST /api/batch   JSON {"items":[...] | [{"text","url"}], "title", "source"}
       → 202 {"batch":{"id":N,"status":"running",...}} / 400|503 {"detail":"..."}
     - GET  /api/batch                → {"batches":[{id,title,source,item_count,
        ok_count,failed_count,status,created_at}, ...]}（当前档案工作区，最新在前）
     - GET  /api/batch/{id}           → {"batch":{...}, "items":[...], "summary":{...}}（summary 在顶层：纯汇总 dict、不带 card_type，挂载时补路由字段）
     - GET  /api/batch/{id}/stream    → SSE（LF 帧，与 chat.js 同格式）：
         meta{batch_id} → status{stage} →
        progress{done,total,position,status}* → card{card_type:"batch_summary", ...平铺}
        narrative{delta} → done{cost}；
         异常出口 error{code,message}；已完结批次一次性重放 meta/status/card/done
     - 停止 = 前端 AbortController 断开 stream（后端协作取消，无 DELETE 端点）

   纪律（同 settings.js / profiles.js）：
     - 纯 vanilla JS、IIFE、"use strict"、无 ES module、无框架；
     - 用户输入一律 createElement + textContent；本文件没有任何 HTML 注入点
       （汇总卡经 window.ChatCards.mountCard 挂载，innerHTML 只存在于
        cards.js 的渲染层，与 renderConclusion 同一模式）；
     - 外链只允许 http(s)：本文件产生的链接只有站内 /api/dossiers/{id}，
       且 id 过正整数白名单（同 safeUrl 的"白名单才放行"思路）；
     - 口径诚实：错误信息照抄后端 detail 原文；没有任何字段时不编造、不显示。
   ============================================================ */
"use strict";

(function () {
  /* file:// 双击打开时 shell.js 已给出启动引导，这里静默退出 */
  if (location.protocol === "file:") return;

  const $ = (sel) => document.querySelector(sel);

  /* 后端接口清单（与 web/batch_api.py、web/materials_api.py 的路由一一对应；
      {batch_id} 与后端路径参数同名，生成的 URL 形状逐字一致） */
  const API = {
    upload: "/api/materials/upload",
    batches: "/api/batch",
    batchDetail: "/api/batch/{batch_id}",
    batchStream: "/api/batch/{batch_id}/stream",
  };

  const MODES = ["text", "file", "urls"];
  const DEFAULT_FILE_HINT = "尚未选择文件（支持 .txt / .md / .csv / .json，选中即解析）。";

  /* 条目状态 → 徽章文案与配色（后端封闭枚举的客户端映射；未知值原样显示，不猜） */
  const ITEM_STATUS = {
    ok: { text: "完成", cls: "green" },
    done: { text: "完成", cls: "green" },
    failed: { text: "失败", cls: "red" },
    error: { text: "失败", cls: "red" },
    blocked: { text: "已拦截", cls: "amber" },
    not_run: { text: "未跑", cls: "amber" },
    pending: { text: "排队中", cls: "gray" },
    running: { text: "进行中", cls: "gray" },
    cancelled: { text: "已取消", cls: "gray" },
    canceled: { text: "已取消", cls: "gray" },
  };

  /* 批次状态 → 徽章文案与配色（与 dossier/batches.py VALID_BATCH_STATUS 同口径；
      未知值原样显示，不猜） */
  const BATCH_STATUS = {
    running: { text: "进行中", cls: "gray" },
    pending: { text: "排队中", cls: "gray" },
    done: { text: "已完成", cls: "green" },
    completed: { text: "已完成", cls: "green" },
    partial: { text: "部分完成", cls: "amber" },
    failed: { text: "失败", cls: "red" },
    cancelled: { text: "已取消", cls: "gray" },
    canceled: { text: "已取消", cls: "gray" },
  };

  const SOURCE_LABEL = { text: "文本", file: "文件", urls: "链接" };

  const state = {
    mode: "text",
    fileItems: null,      // 上传解析得到的条目（内存暂存，不落盘）
    visible: false,       // 面板当前是否可见（role=analyst）
    batches: [],
    activeBatchId: null,
    controller: null,     // 本轮 SSE 的取消句柄
    running: false,
    itemEls: {},          // position → {status: 徽章元素}，供 progress 事件就地刷新
    profilesPoll: null,
  };

  /* ---------- 小工具 ---------- */
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== null && text !== undefined) node.textContent = String(text);
    return node;
  }

  function apiPath(tpl, id) {
    return String(tpl).replace(/\{batch_id\}/g, encodeURIComponent(String(id)));
  }

  function when(s) {
    return String(s || "").replace("T", " ") || "—";
  }

  function sourceLabel(source) {
    return SOURCE_LABEL[String(source || "")] || String(source || "未知来源");
  }

  function statusMeta(map, status) {
    return map[String(status || "")] || { text: String(status || "未知"), cls: "gray" };
  }

  /* 档案链接：只接受正整数 id（白名单才放行），产出站内相对路径，不产生外链 */
  function dossierHref(id) {
    const n = Number(id);
    if (!Number.isInteger(n) || n <= 0) return "";
    return `/api/dossiers/${n}`;
  }

  function showError(msg) {
    const box = $("#batchError");
    if (!box) return;
    box.textContent = msg;
    box.classList.remove("hidden");
  }

  function clearError() {
    const box = $("#batchError");
    if (!box) return;
    box.textContent = "";
    box.classList.add("hidden");
  }

  function showProgress(text, percent) {
    const box = $("#batchProgress");
    if (!box) return;
    box.classList.remove("hidden");
    const line = box.querySelector(".chat-batch-progress-text");
    if (line) line.textContent = text;
    /* percent 为 null = 只更新文字、不动进度条（避免状态事件把已有进度抹成 0） */
    const fill = box.querySelector(".chat-batch-progress-bar > i");
    if (fill && percent !== null && percent !== undefined && Number.isFinite(Number(percent))) {
      fill.style.width = `${Math.max(0, Math.min(100, Number(percent)))}%`;
    }
  }

  function hideProgress() {
    const box = $("#batchProgress");
    if (box) box.classList.add("hidden");
  }

  function setRunning(running) {
    state.running = running;
    const start = $("#batchStartBtn");
    const stop = $("#batchStopBtn");
    if (start) start.disabled = running;
    if (stop) stop.classList.toggle("hidden", !running);
    /* 一批未结束时不换模式、不改输入，避免事件串流 */
    document.querySelectorAll("[data-batch-mode]").forEach((btn) => {
      btn.disabled = running;
    });
  }

  /* ---------- 显隐规则（按档案角色） ---------- */
  function snapshot() {
    const panel = window.ProfilesPanel;
    return (panel && typeof panel.snapshot === "function") ? panel.snapshot() : null;
  }

  function isAnalyst() {
    const snap = snapshot();
    const active = (snap && snap.active) || null;
    return Boolean(active) && active.role === "analyst";
  }

  function clearDetail() {
    state.activeBatchId = null;
    state.itemEls = {};
    const detail = $("#batchDetail");
    if (detail) {
      detail.textContent = "";
      detail.classList.add("hidden");
    }
  }

  function applyVisibility() {
    const panel = $("#chatBatchPanel");
    if (!panel) return false;
    const visible = isAnalyst();
    panel.classList.toggle("hidden", !visible);
    if (visible && !state.visible) reload();   // 首次显示时才拉列表
    state.visible = visible;
    if (!visible) {
      /* 藏起来时：在跑的批次属于刚切走的档案工作区，如实断开 */
      abortRun();
      clearDetail();
      hideProgress();
      clearError();
    }
    return visible;
  }

  /* profiles.js 与本文件同在 DOMContentLoaded 初始化，而 loadProfiles 是异步的：
     首屏快照可能还是空。轮询等它就绪再定显隐（空快照按规格隐藏；10s 等不到
     就保持隐藏，不无限轮询）。 */
  function startProfilesPoll() {
    if (state.profilesPoll) return;
    let ticks = 0;
    state.profilesPoll = window.setInterval(() => {
      ticks += 1;
      applyVisibility();
      const snap = snapshot();
      if ((snap && snap.active) || ticks >= 40) {
        window.clearInterval(state.profilesPoll);
        state.profilesPoll = null;
      }
    }, 250);
  }

  /* ---------- 输入模式 ---------- */
  function setMode(mode) {
    if (MODES.indexOf(mode) < 0) mode = "text";
    state.mode = mode;
    document.querySelectorAll("[data-batch-mode]").forEach((btn) => {
      const active = btn.dataset.batchMode === mode;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
    document.querySelectorAll("[data-batch-pane]").forEach((pane) => {
      pane.classList.toggle("hidden", pane.dataset.batchPane !== mode);
    });
    clearError();
  }

  function bindModeButtons() {
    document.querySelectorAll("[data-batch-mode]").forEach((btn) => {
      btn.addEventListener("click", () => setMode(btn.dataset.batchMode));
    });
  }

  /* ---------- file 模式：选中即上传解析 ---------- */
  function bindFileInput() {
    const input = $("#batchFileInput");
    if (!input) return;
    input.addEventListener("change", () => {
      uploadFile(input.files && input.files[0]);
    });
  }

  async function uploadFile(file) {
    const hint = $("#batchFileHint");
    state.fileItems = null;
    if (!hint) return;
    if (!file) {
      hint.textContent = DEFAULT_FILE_HINT;
      return;
    }
    hint.textContent = `正在解析「${file.name}」…`;
    try {
      const form = new FormData();
      form.append("file", file);           // 契约字段名：file
      const resp = await fetch(API.upload, { method: "POST", body: form });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      const items = Array.isArray(data.items) ? data.items : [];
      state.fileItems = items;
      const count = Number(data.count) || items.length;
      hint.textContent = `${count} 条材料已解析（${data.filename || file.name}）`;
    } catch (err) {
      state.fileItems = null;
      hint.textContent = `解析失败：${(err && err.message) || err}`;
    }
  }

  /* ---------- 收集条目（text/urls 按行 split 去空行；file 用上传解析结果） ---------- */
  function collectItems() {
    if (state.mode === "file") {
      const items = Array.isArray(state.fileItems) ? state.fileItems : [];
      return {
        items,
        error: items.length
          ? ""
          : "请先选择文件，并等待解析完成（解析结果只暂存在本页内存里）。",
      };
    }
    const box = state.mode === "urls" ? $("#batchUrlInput") : $("#batchTextInput");
    const lines = String((box && box.value) || "")
      .split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (!lines.length) {
      return {
        items: [],
        error: state.mode === "urls"
          ? "请先输入至少一个 URL（每行一个）。"
          : "请先输入至少一条材料（每行一条）。",
      };
    }
    if (state.mode === "urls") {
      /* 外链只允许 http(s)：前端先拦一道，后端仍会再校验 */
      const badIndex = lines.findIndex((u) => !/^https?:\/\//i.test(u));
      if (badIndex >= 0) {
        return { items: [], error: `URL 只支持 http(s)：第 ${badIndex + 1} 行不是合法外链。` };
      }
      /* urls 模式走契约里的对象形态 [{"url": "..."}] */
      return { items: lines.map((url) => ({ url })), error: "" };
    }
    return { items: lines, error: "" };
  }

  function suggestTitle(items) {
    const first = items[0];
    const raw = (first && typeof first === "object") ? (first.text || first.url || "") : first;
    const snippet = String(raw || "").replace(/\s+/g, " ").trim().slice(0, 24);
    return `材料批次 ${items.length} 条${snippet ? ` · ${snippet}` : ""}`;
  }

  /* ---------- 开始 / 停止 ---------- */
  function bindActions() {
    const start = $("#batchStartBtn");
    const stop = $("#batchStopBtn");
    if (start) start.addEventListener("click", () => startBatch());
    if (stop) stop.addEventListener("click", () => abortRun());
  }

  async function startBatch() {
    if (state.running) return;              // 一批未结束时不叠加请求
    clearError();
    const { items, error } = collectItems();
    if (error) { showError(error); return; }

    setRunning(true);
    hideProgress();
    try {
      const resp = await fetch(API.batches, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items, title: suggestTitle(items), source: state.mode }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      const id = Number((data && data.batch && data.batch.id) || 0);
      if (!(id > 0)) throw new Error("后端未返回批次 id");
      state.activeBatchId = id;
      clearDetail();
      showProgress("批次已创建，等待首条进度…", 0);
      await consumeStream(id);
    } catch (err) {
      if (err && err.name === "AbortError") {
        /* abort 已断开连接：后端检测到断开即协作取消 */
        showProgress("已停止：连接已断开，后端据此协作取消（最终状态以列表为准）。", null);
      } else {
        showError(`提交失败：${(err && err.message) || err}`);
        hideProgress();
      }
    } finally {
      setRunning(false);
      state.controller = null;
      if (isAnalyst()) reload();
    }
  }

  function abortRun() {
    if (state.controller) {
      state.controller.abort();
      state.controller = null;
    }
  }

  /* ---------- SSE 消费（POST/GET + ReadableStream 手工拆帧，照抄 chat.js 写法） ---------- */
  function parseFrame(frame) {
    let name = "message";
    const dataLines = [];
    frame.split("\n").forEach((line) => {
      const trimmed = line.replace(/\r$/, "");
      if (trimmed.startsWith("event:")) name = trimmed.slice(6).trim();
      else if (trimmed.startsWith("data:")) dataLines.push(trimmed.slice(5).replace(/^ /, ""));
    });
    if (!dataLines.length) return null;
    try {
      return { event: name, data: JSON.parse(dataLines.join("\n")) };
    } catch (e) {
      return null;   // 半截 JSON / 非 JSON 帧直接丢弃，不让整条流崩掉
    }
  }

  async function readSseStream(resp, handlers) {
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const evt = parseFrame(frame);
        if (evt && handlers[evt.event]) handlers[evt.event](evt.data);
      }
    }
    /* 流尾残余（正常实现每帧以空行结尾，这里只是兜底） */
    const tail = parseFrame(buf);
    if (tail && handlers[tail.event]) handlers[tail.event](tail.data);
  }

  async function consumeStream(id) {
    state.controller = new AbortController();
    const resp = await fetch(apiPath(API.batchStream, id), {
      signal: state.controller.signal,
    });
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const j = await resp.json();
        if (j && j.detail) detail = j.detail;
      } catch (e) { /* 非 JSON 错误体 */ }
      throw new Error(detail);
    }
    await readSseStream(resp, {
      meta: onStreamMeta,
      status: onStreamStatus,
      progress: onStreamProgress,
      card: onStreamCard,
      narrative: onStreamNarrative,
      done: onStreamDone,
      error: onStreamError,
    });
  }

  /* ---------- SSE 事件处理器 ---------- */
  function onStreamMeta(d) {
    const id = Number(d && d.batch_id);
    if (id > 0) state.activeBatchId = id;
  }

  function onStreamStatus(d) {
    const stage = (d && d.stage) || "running";
    /* running 是直播；其它值（含已完结批次的回放 stage=done/partial/...）如实标注 */
    showProgress(stage === "running" ? "批次运行中…" : `批次状态：${stage}`, null);
  }

  function onStreamProgress(d) {
    d = d || {};
    const done = Math.max(0, Number(d.done) || 0);
    const total = Math.max(1, Number(d.total) || 0);
    const position = Number(d.position);
    const status = String(d.status || "");
    /* position 是 0 基序号：给人看的口径与后端 degraded 文案一致（第 1 条起） */
    const head = (Number.isFinite(position) && position >= 0)
      ? `第 ${position + 1} 条 · ${status || "进行中"}`
      : "正在核查…";
    showProgress(`${head}（${done}/${total}）`, Math.min(100, Math.round(done / total * 100)));
    syncItemStatus(position, status);
  }

  function onStreamCard(d) {
    /* 载荷可能是卡片本体或 {card:{...}} 包装（与 chat.js onCard 同判据） */
    const card = (d && typeof d === "object" && d.card && typeof d.card === "object") ? d.card : d;
    if (!window.ChatCards || !card || typeof card !== "object") return;
    const detail = $("#batchDetail");
    if (!detail) return;
    detail.classList.remove("hidden");
    let slot = detail.querySelector(".chat-batch-card-slot");
    if (!slot) {
      slot = el("div", "chat-batch-card-slot");
      detail.appendChild(slot);
    }
    /* 挂载走 ChatCards.mountCard（innerHTML 只存在于 cards.js 的渲染层），
       本文件其余 DOM 全部 createElement + textContent */
    window.ChatCards.mountCard(slot, card);
  }

  function onStreamNarrative(d) {
    const delta = String((d && d.delta) || "");
    if (!delta) return;
    const detail = $("#batchDetail");
    if (!detail) return;
    detail.classList.remove("hidden");
    let box = detail.querySelector(".chat-batch-narrative");
    if (!box) {
      box = el("div", "chat-batch-narrative");
      detail.appendChild(box);
    }
    box.appendChild(document.createTextNode(delta));   // textNode 防注入，不解析 HTML
  }

  function onStreamDone(d) {
    const cost = (d && d.cost) || {};
    showProgress(
      `批次完成（API 调用 ${Number(cost.requests) || 0} 次 · ${Number(cost.tokens) || 0} tokens）`,
      100);
    /* 汇总以服务端详情为准：拉一次详情补条目列表（卡片已在流里出过）；
       同批次的叙述要留着，不能被刷新吞掉 */
    loadBatch(state.activeBatchId, true);
  }

  function onStreamError(d) {
    const msg = (d && d.message) ? String(d.message) : "批次运行出错";
    showError(`批次出错：${msg}`);
    hideProgress();
  }

  /* progress 事件就地刷新已加载条目的状态徽章（没加载详情就不刷新，不补造） */
  function syncItemStatus(position, status) {
    const entry = state.itemEls[Number(position)];
    if (!entry || !entry.status) return;
    const meta = statusMeta(ITEM_STATUS, status);
    entry.status.className = `badge ${meta.cls}`;
    entry.status.textContent = meta.text;
  }

  /* ---------- 批次列表 ---------- */
  async function reload() {
    let data = {};
    try {
      const resp = await fetch(API.batches);
      if (!resp.ok) return;
      data = await resp.json();
    } catch (e) {
      return;   // 列表加载失败不阻塞面板其余交互
    }
    state.batches = Array.isArray(data.batches) ? data.batches : [];
    renderList();
  }

  function renderList() {
    const list = $("#batchList");
    if (!list) return;
    list.textContent = "";
    if (!state.batches.length) {
      list.appendChild(el("li", "empty", "暂无批次；填好材料后点「开始分析」"));
      return;
    }
    state.batches.forEach((b) => list.appendChild(batchRow(b)));
  }

  function batchRow(b) {
    b = b || {};
    const id = Number(b.id);
    const li = el("li", "chat-batch-item");
    li.dataset.bid = String(id);

    const head = el("div", "chat-batch-item-head");
    head.appendChild(el("span", "chat-batch-item-title", b.title || `批次 #${id}`));
    const bmeta = statusMeta(BATCH_STATUS, b.status);
    head.appendChild(el("span", `badge ${bmeta.cls}`, bmeta.text));

    const meta = el("div", "chat-batch-item-meta muted");
    const notRun = Number(b.not_run_count) || 0;
    const counts = `共 ${Number(b.item_count) || 0} 条 · 成功 ${Number(b.ok_count) || 0}`
      + ` · 失败 ${Number(b.failed_count) || 0}` + (notRun > 0 ? ` · 未跑 ${notRun}` : "");
    meta.textContent = `#${id} · ${sourceLabel(b.source)} · ${counts} · ${when(b.created_at)}`;

    li.appendChild(head);
    li.appendChild(meta);
    li.addEventListener("click", () => loadBatch(id));
    return li;
  }

  /* ---------- 批次详情 ---------- */
  async function loadBatch(id, keepNarrative) {
    if (!(Number(id) > 0)) return;
    const detail = $("#batchDetail");
    if (!detail) return;
    /* keepNarrative：刷新同一个批次时保留流里已产出的叙述（不被吞掉）；
       用户点开别的批次不保留——别批的叙述挂在这批下面会是误导 */
    const narrative = keepNarrative ? detail.querySelector(".chat-batch-narrative") : null;
    detail.textContent = "";
    detail.classList.remove("hidden");
    detail.appendChild(el("p", "muted", `正在加载批次 #${Number(id)}…`));
    try {
      const resp = await fetch(apiPath(API.batchDetail, id));
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      /* 契约：{"batch", "items", "summary"}——summary 在顶层（空汇总时由后端
         现算）；batch.summary 也带一份，这里顶层优先 */
      const topSummary = (data && typeof data.summary === "object") ? data.summary : null;
      renderBatchDetail((data && data.batch) || {},
        Array.isArray(data.items) ? data.items : [], narrative, topSummary);
    } catch (err) {
      detail.textContent = "";
      detail.appendChild(el("p", "chat-batch-error",
        `批次详情加载失败：${(err && err.message) || err}`));
    }
  }

  function renderBatchDetail(batch, items, narrative, topSummary) {
    const detail = $("#batchDetail");
    if (!detail) return;
    detail.textContent = "";
    detail.classList.remove("hidden");
    state.itemEls = {};

    const head = el("div", "chat-batch-detail-head");
    head.appendChild(el("div", "chat-batch-detail-title",
      batch.title || `批次 #${Number(batch.id)}`));
    const bmeta = statusMeta(BATCH_STATUS, batch.status);
    const counts = `共 ${Number(batch.item_count) || 0} 条 · 成功 ${Number(batch.ok_count) || 0}`
      + ` · 失败 ${Number(batch.failed_count) || 0}`;
    const notRun = Number(batch.not_run_count) || 0;
    head.appendChild(el("div", "chat-batch-detail-meta muted",
      `状态：${bmeta.text} · ${sourceLabel(batch.source)} · ${counts}`
      + (notRun > 0 ? ` · 未跑 ${notRun}` : "") + ` · ${when(batch.created_at)}`));
    detail.appendChild(head);

    /* 汇总卡：顶层 summary 优先，回落 batch.summary；都没有就不显示（不编造）。
       详情接口的 summary 是纯汇总 dict（不带 card_type / batch_id / title /
       source）——这几项用 batch 行自带的真实字段补齐（与后端 summary_card
       的拼法一致），as_of 汇总里没有就照实显示"未知"，不拿时间冒充证据截至 */
    const summary = (topSummary && typeof topSummary === "object" && Object.keys(topSummary).length)
      ? topSummary
      : ((batch.summary && typeof batch.summary === "object"
          && Object.keys(batch.summary).length) ? batch.summary : null);
    if (summary && window.ChatCards && typeof window.ChatCards.mountCard === "function") {
      const slot = el("div", "chat-batch-card-slot");
      window.ChatCards.mountCard(slot, Object.assign({
        card_type: "batch_summary",
        batch_id: batch.id,
        title: batch.title,
        source: batch.source,
      }, summary));
      detail.appendChild(slot);
    }

    if (items.length) {
      const ul = el("ul", "chat-batch-items");
      items.forEach((it) => ul.appendChild(itemRow(it)));
      detail.appendChild(ul);
    } else {
      detail.appendChild(el("p", "muted", "该批次还没有条目数据。"));
    }
    /* 叙述放在最后（流里它本就出现在卡片之后） */
    if (narrative) detail.appendChild(narrative);
  }

  function itemRow(it) {
    it = it || {};
    const li = el("li", "chat-batch-item-row");
    li.appendChild(el("span", "chat-batch-item-pos", `#${Number(it.position) || 0}`));
    const smeta = statusMeta(ITEM_STATUS, it.status);
    const status = el("span", `badge ${smeta.cls}`, smeta.text);
    li.appendChild(status);
    li.appendChild(el("span", "chat-batch-item-label", it.label || "（无标签）"));

    /* urls 批次的原始链接（外部内容只做纯文本展示，不生成可点外链） */
    const url = String(it.url || "").trim();
    if (url) li.appendChild(el("span", "chat-batch-item-url muted", url));

    const risk = Number(it.risk_score);
    if (it.risk_score !== null && it.risk_score !== undefined && Number.isFinite(risk)) {
      li.appendChild(el("span", "chat-batch-item-risk muted", `风险分 ${risk}`));
    }
    if (it.group_id) {
      li.appendChild(el("span", "chat-batch-item-group", `同源组 ${it.group_id}`));
    }
    if (it.dossier_id) {
      const href = dossierHref(it.dossier_id);
      if (href) {
        const link = el("a", "chat-batch-item-dossier", `档案 #${Number(it.dossier_id)}`);
        link.href = href;
        li.appendChild(link);
      } else {
        li.appendChild(el("span", "muted", `档案 #${it.dossier_id}`));
      }
    }
    if (it.error) {
      li.appendChild(el("div", "chat-batch-item-error", String(it.error)));
    }
    const position = Number(it.position);
    if (Number.isFinite(position)) state.itemEls[position] = { status };
    return li;
  }

  /* ---------- 对外（chat.js 只调 init / onProfileChanged） ---------- */
  window.BatchPanel = {
    init,
    onProfileChanged,
    start: startBatch,
    stop: abortRun,
    reload,
  };

  function init() {
    bindModeButtons();
    bindFileInput();
    bindActions();
    applyVisibility();
    startProfilesPoll();
  }

  function onProfileChanged() {
    /* 换工作区 = 换数据范围：在跑的流先断（批次 id 属于旧工作区），选择与详情清空 */
    abortRun();
    clearDetail();
    hideProgress();
    clearError();
    applyVisibility();
    if (isAnalyst()) reload();
  }
})();
