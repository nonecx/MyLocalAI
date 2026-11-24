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
        ok_count,failed_count,blocked_count,not_run_count,status,created_at}, ...]}
        （当前档案工作区，最新在前；blocked_count/not_run_count 为读侧派生，
        blocked 已从 failed 分列——W2 R5）
     - GET  /api/batch/{id}           → {"batch":{... 含读侧派生 blocked_count},
        "items":[...], "summary":{...}}（summary 在顶层：纯汇总 dict、不带
        card_type，挂载时补路由字段）
     - GET  /api/batch/{id}/stream    → SSE（LF 帧，与 chat.js 同格式）：
         meta{batch_id} → status{stage} →
        progress{done,total,position,status}* → card{card_type:"batch_summary", ...平铺}
        narrative{delta} → done{cost}；
         异常出口 error{code,message}；已完结批次一次性重放 meta/status/card/done
     - 停止 = 前端 AbortController 断开 stream；后端检测到 SSE 客户端断开后
       request_cancel，批次落 cancelled、未跑条目标 not_run（W2 已接线，
       无 DELETE 端点）——「已取消」徽章路径因此真实可达

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

  /* ---------- U0 公共助手收口（cards.js 经 window.ChatCards 导出，本文件不留重复副本） ----------
     脚本顺序锁（tests/test_models_ui.py）保证 cards.js 先于本文件执行，别名在
     IIFE 求值时即已就绪；调用点签名与原本地副本逐字一致，调用处零改动。
     - BATCH_ITEM_STATUS：批次条目状态映射（与原本地映射逐键一致，含
       cancelled/canceled →「已取消」——真取消链路落地后的徽章即走这里）；
       未知值原样显示，不猜（statusMeta 兜底）。
     - apiPath：路径占位符拼装（单值模式替换全部 {...}，兼容 {batch_id}）。
     - readSseStream：SSE 读流（内含 parseFrame 拆帧，原逐字重复副本已删）。
     注意：本文件自己的 when 是 textContent 版，与 cards.js 导出的 escape 版
     语义不同（本文件无 innerHTML），保留本地实现，不混用。 */
  const cards = window.ChatCards || {};
  const ITEM_STATUS = cards.BATCH_ITEM_STATUS || {};
  const apiPath = cards.apiPath;
  const readSseStream = cards.readSseStream;

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
    stopRequested: false, // 停止闩：SSE 尚未连上时点停止，等流一连上就立刻断开（触发后端取消）
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
    /* percent 为 null = 只更新文字、不动进度条（避免状态事件把已有进度抹成 0，
       aria-valuenow 同样保持不动）。
       进度条可访问性：外层容器骨架在 index.html（role=status），内部进度条的
       role/aria 由这里接线（index.html / chat.css 不归本文件改）；percent 按
       已完成条目数/总数现算（调用方传入），aria-valuenow 取 0-100 整数。 */
    const bar = box.querySelector(".chat-batch-progress-bar");
    if (bar) {
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
    }
    const percentNum = (percent !== null && percent !== undefined) ? Number(percent) : NaN;
    if (Number.isFinite(percentNum)) {
      const pct = Math.max(0, Math.min(100, Math.round(percentNum)));
      if (bar) bar.setAttribute("aria-valuenow", String(pct));
      const fill = box.querySelector(".chat-batch-progress-bar > i");
      if (fill) fill.style.width = `${pct}%`;
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
      /* 藏起来时：在跑的批次属于刚切走的档案工作区，如实断开
         （断开即触发后端取消链路：批次 cancelled、未跑条目 not_run） */
      abortRun();
      clearDetail();
      hideProgress();
      clearError();
    }
    return visible;
  }

  /* profiles.js 与本文件同在 DOMContentLoaded 初始化，而 loadProfiles 是异步的：
     首屏快照可能还是空。轮询等它就绪再定显隐（空快照按规格隐藏；10s 等不到
     就保持隐藏，不无限轮询）。轮询实现收口到 cards.js 的导出版（U0：250ms
     × 40 tick，到点自停，返回定时器句柄）；这里只保留防重入与句柄归属 state
     ——init 只跑一次，句柄到点停摆后留在 state 里仅作防重入标记。 */
  function startProfilesPoll() {
    if (state.profilesPoll) return;
    if (typeof cards.startProfilesPoll !== "function") return;
    state.profilesPoll = cards.startProfilesPoll(applyVisibility, snapshot);
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
    const snippet = String(raw || "").replace(/\s+/g, " ").trim();
    /* 截 24 字并标注「已截断」（口径对齐 cards.js 的截断标注），不让用户
       以为首条材料只有这一点内容 */
    const cut = snippet.length > 24;
    const shown = cut ? snippet.slice(0, 24) : snippet;
    return `材料批次 ${items.length} 条${shown ? ` · ${shown}${cut ? "…（已截断）" : ""}` : ""}`;
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
    state.stopRequested = false;
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
        /* abort 断开进度流：后端检测到 SSE 客户端断开后 request_cancel，
           批次落 cancelled、未跑条目标 not_run（W2 已接线，最终状态以
           列表/详情为准） */
        showProgress(
          "已停止：进度流已断开，已通知后端取消本批；"
          + "已跑条目的结论保留，未跑条目标记为未跑（最终状态以列表为准）。", null);
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
    } else if (state.running) {
      /* 竞态窗口：POST /api/batch 已 202 但 SSE 还没连上（controller 未建）——
         此刻没有流可断，记下停止意图，等 consumeStream 连上后立即断开，
         否则这次"停止"会扑空、批次在后端继续跑完（E2E 走查发现）。 */
      state.stopRequested = true;
    }
    /* 停止后延迟刷新一次列表：批次落 cancelled 需要后端收尾一点时间，
       不刷的话列表徽章会停在"进行中"（E2E 走查发现）。 */
    setTimeout(() => {
      if (isAnalyst() && !state.running) reload();
    }, 2500);
  }

  /* ---------- SSE 消费（POST/GET + ReadableStream；读流/拆帧实现收口到
     cards.js 导出的 readSseStream，本文件只管事件分发） ---------- */
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
    /* 流刚连上就发现停止闩已被按下（停止点击落在 POST 202 与连接建立之间）：
       立即断开——服务端 _drain 检测到断开即 request_cancel，批次照常落 cancelled。 */
    if (state.stopRequested) {
      state.stopRequested = false;
      state.controller.abort();
      state.controller = null;
      return;
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
    /* 后端 R7 起 done.cost 为真实值；0 既可能是真的没有模型调用，也可能是
       计数降级（前端分不出来）——统一用降级口径文案，不显示"0 次"冒充
       精确计数 */
    const requests = Number(cost.requests);
    const tokens = Number(cost.tokens);
    const reqOk = Number.isFinite(requests) && requests > 0;
    const tokOk = Number.isFinite(tokens) && tokens > 0;
    const costText = (reqOk || tokOk)
      ? `API 调用 ${reqOk ? requests : 0} 次 · ${tokOk ? tokens : 0} tokens`
      : "计数不可用或无模型调用";
    showProgress(`批次完成（${costText}）`, 100);
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
  /* 列表加载失败行：结构与 chat.js 的 renderListError 一致（li.chat-list-error
     > span 文案 + button「点击重试」；纯 createElement/textContent，不进
     innerHTML）。样式用 chat.css 已有的 .chat-list-error（U0 同批新增）；
     该类的列表行细化选择器目前只覆盖会话/档案两个列表，批次列表只有基础
     flex 排版生效——如需虚线框样式，CSS 层补 .chat-batch-list li.chat-list-error */
  function renderListError(list) {
    list.textContent = "";
    const li = el("li", "chat-list-error");
    li.appendChild(el("span", "", "批次列表加载失败"));
    const btn = el("button", "", "点击重试");
    btn.type = "button";
    btn.addEventListener("click", () => reload());
    li.appendChild(btn);
    list.appendChild(li);
  }

  async function reload() {
    const list = $("#batchList");
    let data = null;
    try {
      const resp = await fetch(API.batches);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      data = await resp.json();
    } catch (e) {
      /* 列表加载失败不再静默吞掉：就地渲染错误行 + 点击重试
         （仍不阻塞面板其余交互） */
      if (list) renderListError(list);
      return;
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
    /* blocked 已从 failed 分列（W2 R5）：列表行带读侧派生的 blocked_count；
       与未跑同口径，>0 才显示，不让每行都背着 0 计数 */
    const blocked = Number(b.blocked_count) || 0;
    const notRun = Number(b.not_run_count) || 0;
    const counts = `共 ${Number(b.item_count) || 0} 条 · 成功 ${Number(b.ok_count) || 0}`
      + ` · 失败 ${Number(b.failed_count) || 0}`
      + (blocked > 0 ? ` · 已拦截 ${blocked}` : "")
      + (notRun > 0 ? ` · 未跑 ${notRun}` : "");
    meta.textContent = `#${id} · ${sourceLabel(b.source)} · ${counts} · ${when(b.created_at)}`;

    li.appendChild(head);
    li.appendChild(meta);

    /* 键盘可达性：li 可点开详情，键盘用户同样要能进——tabindex + role=button，
       Enter/Space 与 click 走同一 open 动作。:focus-visible 焦点框样式在
       chat.css（不归本文件改），这里用 focus/blur 内联 outline 兜底，
       :focus-visible 不被支持时退化为"聚焦即显示"。 */
    const open = () => loadBatch(id);
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.setAttribute("aria-label", `${b.title || `批次 #${id}`}，点按查看批次详情`);
    li.addEventListener("click", open);
    li.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
        ev.preventDefault();   // 空格默认滚屏 / 回车默认提交，都不留给浏览器
        open();
      }
    });
    li.addEventListener("focus", () => {
      let focusVisible = false;
      try { focusVisible = li.matches(":focus-visible"); } catch (e) { focusVisible = true; }
      if (focusVisible) {
        li.style.outline = "2px solid var(--primary)";
        li.style.outlineOffset = "-2px";
      }
    });
    li.addEventListener("blur", () => {
      li.style.outline = "";
      li.style.outlineOffset = "";
    });
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

    /* 汇总取数提前：blocked 分列与汇总卡共用同一份（顶层 summary 优先，
       回落 batch.summary；都没有就不显示，不编造） */
    const summary = (topSummary && typeof topSummary === "object" && Object.keys(topSummary).length)
      ? topSummary
      : ((batch.summary && typeof batch.summary === "object"
          && Object.keys(batch.summary).length) ? batch.summary : null);

    const head = el("div", "chat-batch-detail-head");
    head.appendChild(el("div", "chat-batch-detail-title",
      batch.title || `批次 #${Number(batch.id)}`));
    const bmeta = statusMeta(BATCH_STATUS, batch.status);
    const counts = `共 ${Number(batch.item_count) || 0} 条 · 成功 ${Number(batch.ok_count) || 0}`
      + ` · 失败 ${Number(batch.failed_count) || 0}`;
    const notRun = Number(batch.not_run_count) || 0;
    /* blocked 已从 failed 分列（W2 R5）：详情行自带读侧派生的 blocked_count，
       行字段缺失时回落汇总里的 blocked；与未跑同口径，>0 才显示 */
    const rowBlocked = Number(batch.blocked_count);
    const blockedCount = Number.isFinite(rowBlocked)
      ? rowBlocked
      : (Number(summary && summary.blocked) || 0);
    head.appendChild(el("div", "chat-batch-detail-meta muted",
      `状态：${bmeta.text} · ${sourceLabel(batch.source)} · ${counts}`
      + (blockedCount > 0 ? ` · 已拦截 ${blockedCount}` : "")
      + (notRun > 0 ? ` · 未跑 ${notRun}` : "") + ` · ${when(batch.created_at)}`));
    detail.appendChild(head);

    /* 汇总卡：详情接口的 summary 是纯汇总 dict（不带 card_type / batch_id /
       title / source）——这几项用 batch 行自带的真实字段补齐（与后端
       summary_card 的拼法一致），as_of 汇总里没有就照实显示"未知"，
       不拿时间冒充证据截至 */
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
    /* 结论文案（界面去数值）：判定用的 risk_score 数值不在条目行展示——
       有 label（后端 tools_batch 落库的结论标签）就用 label；完成但没拿到
       label 时给「已核查」中性文案；失败/拦截/未跑等状态已有状态徽章，
       标签位不再渲染（不冒充"已核查"），字段缺失一律不抛错。 */
    const itemLabel = String(it.label || "").trim();
    if (itemLabel) {
      li.appendChild(el("span", "chat-batch-item-label", itemLabel));
    } else if (String(it.status || "") === "ok" || String(it.status || "") === "done") {
      li.appendChild(el("span", "chat-batch-item-label", "已核查"));
    }

    /* urls 批次的原始链接（外部内容只做纯文本展示，不生成可点外链） */
    const url = String(it.url || "").trim();
    if (url) li.appendChild(el("span", "chat-batch-item-url muted", url));
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
