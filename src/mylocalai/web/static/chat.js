/* ============================================================
   chat.js —— 对话 Tab 前端（M2，工作指导 §4.3/§5.3）
   独立文件：不 import app.js 的任何符号（旧前端契约不动）。
   职责：
     - fetch POST /api/chat + ReadableStream 手工解析 SSE 帧
       （EventSource 不支持 POST，必须手工拆帧）；
     - AbortController 取消：abort 即断开连接，后端据此走协作取消链；
     - 渲染 §5.3 全部事件：meta/status/tool_start/scan/progress/card/
       narrative/tool_end/done/error（集合 ⊆ events.py EVENT_NAMES，测试锁定）；
     - 会话列表加载/切换/新建（左侧栏）+ 档案库入口（简单区块，完整页在 M3）。
   ============================================================ */
"use strict";

(function () {
  /* file:// 双击打开时 app.js 已给出启动引导，这里静默退出 */
  if (location.protocol === "file:") return;

  const $ = (sel) => document.querySelector(sel);

  /* 后端接口清单（与 web/app.py 路由表一一对应，测试做前后端自洽断言） */
  const CHAT_API = {
    chat: "/api/chat",
    sessions: "/api/sessions",
    sessionMessages: "/api/sessions/{sid}/messages",
    dossiers: "/api/dossiers",
    dossierDetail: "/api/dossiers/{id}",
    dossierExport: "/api/dossiers/{id}/export",
  };

  /* SSE 事件名封闭枚举的客户端子集（来源 mylocalai.assistant.events.EVENT_NAMES，
     只监听不新增；新增事件名必须先改协议，再改这里） */
  const SSE_EVENTS = ["meta", "status", "tool_start", "scan", "progress",
                      "card", "narrative", "tool_end", "done", "error"];

  const MAX_MESSAGE_LENGTH = 8000;   // 与后端 /api/chat 校验一致

  let currentSessionId = null;   // 当前会话；null = 下一条消息自动新建
  let activeController = null;   // 本轮请求的取消句柄
  let currentTurnEl = null;      // 本轮助手回复的容器（事件往里追加）

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function apiPath(tpl, id) {
    return String(tpl).replace(/\{sid\}|\{id\}/g, encodeURIComponent(String(id)));
  }

  /* ---------- Tab 切换 ---------- */
  function setTab(tab) {
    document.querySelectorAll("#topTabs .top-tab")
      .forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    const chat = $("#chatTab");
    const tools = $("#toolsTab");
    if (chat) chat.classList.toggle("hidden", tab !== "chat");
    if (tools) tools.classList.toggle("hidden", tab !== "tools");
  }

  /* ---------- 错误与提示 ---------- */
  function chatError(msg) {
    const box = $("#chatError");
    box.textContent = msg;
    box.classList.remove("hidden");
  }

  function chatClearError() {
    $("#chatError").classList.add("hidden");
  }

  function scrollBottom() {
    const box = $("#chatMessages");
    box.scrollTop = box.scrollHeight;
  }

  function updateWelcome() {
    const box = $("#chatMessages");
    const welcome = box.querySelector(".chat-welcome");
    if (welcome) welcome.classList.toggle("hidden", box.querySelectorAll(".msg").length > 0);
  }

  function clearConversation() {
    $("#chatMessages").querySelectorAll(".msg").forEach((n) => n.remove());
    currentTurnEl = null;
    updateWelcome();
    chatClearError();
  }

  /* ---------- 消息容器 ---------- */
  function appendUserMessage(text) {
    const wrap = document.createElement("div");
    wrap.className = "msg msg-user";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);
    $("#chatMessages").appendChild(wrap);
    updateWelcome();
    scrollBottom();
  }

  function turnEl() {
    if (!currentTurnEl || !document.body.contains(currentTurnEl)) {
      const wrap = document.createElement("div");
      wrap.className = "msg msg-assistant";
      wrap.innerHTML = `
        <div class="bubble chat-turn">
          <div class="chat-status"></div>
          <div class="chat-scan hidden"></div>
          <div class="chat-progress hidden"></div>
          <div class="chat-card-slot"></div>
          <div class="chat-narrative"></div>
          <div class="chat-notes"></div>
        </div>`;
      $("#chatMessages").appendChild(wrap);
      currentTurnEl = wrap.querySelector(".chat-turn");
      updateWelcome();
    }
    return currentTurnEl;
  }

  function partEl(cls) {
    return turnEl().querySelector(cls);
  }

  function appendNote(html, cls) {
    const p = document.createElement("div");
    p.className = "chat-note" + (cls ? " " + cls : "");
    p.innerHTML = html;
    partEl(".chat-notes").appendChild(p);
    scrollBottom();
  }

  /* ---------- SSE 事件处理器（键集合 = SSE_EVENTS，测试锁定 ⊆ EVENT_NAMES） ---------- */
  const SSE_HANDLERS = {
    meta: onMeta,
    status: onStatus,
    tool_start: onToolStart,
    scan: onScan,
    progress: onProgress,
    card: onCard,
    narrative: onNarrative,
    tool_end: onToolEnd,
    done: onDone,
    error: onError,
  };

  function onMeta(d) {
    /* 自动新建的会话 id 在这里绑定；此后本轮与后续消息都落到该会话 */
    if (d && Number.isFinite(Number(d.session_id)) && Number(d.session_id) > 0) {
      currentSessionId = Number(d.session_id);
      loadSessions();
    }
  }

  function onStatus(d) {
    partEl(".chat-status").textContent = `正在判断如何处理…（${(d && d.stage) || "routing"}）`;
  }

  function onToolStart(d) {
    d = d || {};
    const est = Number(d.est_s);
    const estTxt = Number.isFinite(est) && est > 0 ? `预估 ${Math.round(est)}s` : "预估耗时未知";
    const tool = String(d.tool || "");
    const label = /verify|recheck|search/.test(tool) ? "取证" : (tool || "处理");
    partEl(".chat-status").textContent = `🛰️ 已开始${label}…${estTxt}`;
    scrollBottom();
  }

  function onScan(d) {
    const markers = Array.isArray(d && d.markers) ? d.markers : [];
    const box = partEl(".chat-scan");
    box.innerHTML = `
      <div class="chat-scan-title">本地初扫（零成本，结果以下方联网核查为准）</div>
      <div class="chat-chips">${markers.length
        ? markers.map((m) => `<span class="kw">${escapeHtml(String(m))}</span>`).join("")
        : '<span class="kw">未命中已知话术特征</span>'}</div>`;
    box.classList.remove("hidden");
    scrollBottom();
  }

  function onProgress(d) {
    d = d || {};
    const done = Math.max(0, Number(d.done) || 0);
    const total = Math.max(1, Number(d.total) || 0);
    const box = partEl(".chat-progress");
    box.innerHTML = `
      <div class="chat-progress-row">
        <span>${escapeHtml(String(d.component || "组件"))} ${done}/${total}</span>
        <div class="chat-progress-bar"><i style="width: ${Math.min(100, Math.round(done / total * 100))}%"></i></div>
      </div>`;
    box.classList.remove("hidden");
    scrollBottom();
  }

  function onCard(d) {
    /* 卡片永远在叙述前（协议保证）；载荷可能是卡片本体或 {card: {...}} 包装 */
    const card = (d && typeof d === "object" && d.card && typeof d.card === "object") ? d.card : d;
    partEl(".chat-card-slot").innerHTML = window.ChatCards.renderCard(card);
    scrollBottom();
  }

  function onNarrative(d) {
    /* 逐句追加；用 textNode 防注入，不解析 HTML */
    partEl(".chat-narrative").appendChild(document.createTextNode(String((d && d.delta) || "")));
    scrollBottom();
  }

  function onToolEnd(d) {
    d = d || {};
    const bits = [`✔ ${escapeHtml(String(d.tool || "工具"))} 完成 · ${Number(d.duration_s) || 0}s`];
    if (d.degraded) bits.push('<span class="badge amber">降级</span>');
    if (d.dossier_id) bits.push(`档案 #${escapeHtml(String(d.dossier_id))}`);
    appendNote(bits.join(" "));
  }

  function onDone(d) {
    const cost = (d && d.cost) || {};
    appendNote(`<span class="badge gray">本轮成本：API 调用 ${Number(cost.requests) || 0} 次 · ${Number(cost.tokens) || 0} tokens</span>`, "chat-cost");
    const status = partEl(".chat-status");
    if (status.textContent.startsWith("正在判断") || status.textContent.startsWith("🛰️")) {
      status.textContent = "";
    }
  }

  function onError(d) {
    const msg = (d && d.message) ? String(d.message) : "发生未知错误";
    const retry = d && d.recoverable ? "（可重试）" : "";
    chatError(`出错了${retry}：${msg}`);
    appendNote(`<span class="badge red">错误${retry}</span> ${escapeHtml(msg)}`, "chat-note-error");
  }

  /* ---------- SSE 帧解析（POST + ReadableStream，EventSource 不支持 POST） ---------- */
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

  async function readSseStream(resp) {
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
        if (evt && SSE_HANDLERS[evt.event]) SSE_HANDLERS[evt.event](evt.data);
      }
    }
    /* 流尾残余（正常实现每帧以空行结尾，这里只是兜底） */
    const tail = parseFrame(buf);
    if (tail && SSE_HANDLERS[tail.event]) SSE_HANDLERS[tail.event](tail.data);
  }

  /* ---------- 发送 / 取消 ---------- */
  function setSending(sending) {
    $("#chatSendBtn").disabled = sending;
    $("#chatSendBtn").textContent = sending ? "回答中…" : "发送";
    $("#chatCancelBtn").classList.toggle("hidden", !sending);
  }

  async function sendMessage() {
    if (activeController) return;   // 一轮未结束时不叠加请求
    const input = $("#chatInput");
    const text = input.value.trim();
    if (!text) { chatError("请先输入要核查的消息。"); return; }
    if (text.length > MAX_MESSAGE_LENGTH) {
      chatError(`消息过长（最多 ${MAX_MESSAGE_LENGTH} 字）。`);
      return;
    }
    chatClearError();
    appendUserMessage(text);
    input.value = "";
    updateCharCount();
    currentTurnEl = null;   // 新起一轮容器
    turnEl();

    const body = { message: text };
    if (currentSessionId) body.session_id = currentSessionId;

    activeController = new AbortController();
    setSending(true);
    try {
      const resp = await fetch(CHAT_API.chat, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: activeController.signal,
      });
      if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
          const j = await resp.json();
          if (j && j.detail) detail = j.detail;
        } catch (e) { /* 非 JSON 错误体 */ }
        throw new Error(detail);
      }
      await readSseStream(resp);
    } catch (err) {
      if (err && err.name === "AbortError") {
        /* abort 已断开连接：后端检测到断开即置 cancelled，协作取消 */
        appendNote("已停止本轮回答（后端已收到取消信号）。");
      } else {
        chatError(`发送失败：${(err && err.message) || err}。请确认服务已启动。`);
      }
    } finally {
      setSending(false);
      activeController = null;
      currentTurnEl = null;
      loadSessions();   // 会话 updated_at 变了，刷新排序
    }
  }

  /* ---------- 会话列表 ---------- */
  function highlightActiveSession() {
    document.querySelectorAll("#chatSessionList li[data-sid]").forEach((li) => {
      li.classList.toggle("active", Number(li.dataset.sid) === currentSessionId);
    });
  }

  async function loadSessions() {
    try {
      const resp = await fetch(CHAT_API.sessions);
      if (!resp.ok) return;
      const data = await resp.json();
      const list = $("#chatSessionList");
      const sessions = (data && data.sessions) || [];
      if (!sessions.length) {
        list.innerHTML = '<li class="empty">暂无会话，发消息即可自动创建</li>';
        return;
      }
      list.innerHTML = sessions.map((s) => `
        <li data-sid="${Number(s.id)}">
          <div class="chat-session-title">${escapeHtml(s.title || "（未命名会话）")}</div>
          <div class="chat-session-meta">${escapeHtml(String(s.updated_at || "").replace("T", " "))}</div>
        </li>`).join("");
      list.querySelectorAll("li[data-sid]").forEach((li) => {
        li.addEventListener("click", () => selectSession(Number(li.dataset.sid)));
      });
      highlightActiveSession();
    } catch (e) { /* 会话列表加载失败不阻塞主流程 */ }
  }

  async function selectSession(sid) {
    if (activeController) return;   // 一轮进行中不切换，避免事件串流
    currentSessionId = sid;
    clearConversation();
    highlightActiveSession();
    try {
      const resp = await fetch(apiPath(CHAT_API.sessionMessages, sid));
      if (!resp.ok) return;
      const data = await resp.json();
      ((data && data.messages) || []).forEach(renderHistoryMessage);
    } catch (e) { /* 历史加载失败保持空白会话 */ }
  }

  function renderHistoryMessage(m) {
    if (!m) return;
    if (m.role === "user") { appendUserMessage(m.content || ""); return; }
    if (m.role === "system_note") return;
    if (m.kind === "card" && window.ChatCards) {
      let payload = null;
      try { payload = JSON.parse(m.content || ""); } catch (e) { payload = null; }
      if (payload && typeof payload === "object") {
        currentTurnEl = null;
        turnEl().querySelector(".chat-card-slot").innerHTML = window.ChatCards.renderCard(payload);
        return;
      }
    }
    currentTurnEl = null;
    turnEl().querySelector(".chat-narrative").textContent = m.content || "";
  }

  async function newSession() {
    if (activeController) return;
    try {
      const resp = await fetch(CHAT_API.sessions, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "" }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const s = await resp.json();
      currentSessionId = Number(s.id);
      clearConversation();
      await loadSessions();
    } catch (e) {
      chatError(`新建会话失败：${(e && e.message) || e}`);
    }
  }

  /* ---------- 档案库入口（简单区块；完整档案库页在 M3） ---------- */
  async function loadDossiers() {
    try {
      const resp = await fetch(CHAT_API.dossiers + "?limit=20");
      if (!resp.ok) return;
      const data = await resp.json();
      const items = (data && data.dossiers) || [];
      $("#chatDossierCount").textContent = String(items.length);
      const list = $("#chatDossierList");
      if (!items.length) {
        list.innerHTML = '<li class="empty">暂无档案；完成一次核查后自动沉淀</li>';
        return;
      }
      list.innerHTML = items.map((d) => `
        <li data-did="${Number(d.id)}">
          <div class="chat-dossier-title">${escapeHtml(d.title || "未命名核查")}</div>
          <div class="chat-session-meta">${escapeHtml(String(d.updated_at || "").replace("T", " "))}</div>
          <div class="chat-dossier-detail hidden"></div>
        </li>`).join("");
      list.querySelectorAll("li[data-did]").forEach((li) => {
        li.addEventListener("click", () => toggleDossierDetail(Number(li.dataset.did), li));
      });
    } catch (e) { /* 档案列表加载失败不阻塞主流程 */ }
  }

  async function toggleDossierDetail(did, li) {
    const box = li.querySelector(".chat-dossier-detail");
    if (!box) return;
    if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    if (box.dataset.loaded === "1") return;
    box.textContent = "加载中…";
    try {
      const resp = await fetch(apiPath(CHAT_API.dossierDetail, did));
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      const versions = (d.versions || []).map((v) => `
        <div class="dim-row"><span class="k">v${escapeHtml(String(v.version))}</span>
          <span class="v">${escapeHtml(v.label || "—")} · 截至 ${escapeHtml(String(v.as_of || "未知"))}</span></div>`).join("");
      const batches = Object.entries(d.evidence_batches || {})
        .map(([b, n]) => `批次${escapeHtml(String(b))}×${escapeHtml(String(n))}`).join(" · ");
      box.innerHTML = `
        ${versions || '<div class="muted">（无结论版本）</div>'}
        <div class="muted">证据：${batches || "0 条"} · 快照 ${Number(d.snapshot_count) || 0} 次</div>
        <a class="chat-dossier-export" href="${apiPath(CHAT_API.dossierExport, did)}?format=md" target="_blank" rel="noopener">导出 Markdown</a>`;
      box.dataset.loaded = "1";
    } catch (e) {
      box.textContent = `档案详情加载失败：${(e && e.message) || e}`;
    }
  }

  /* ---------- 输入框 ---------- */
  function updateCharCount() {
    $("#chatCharCount").textContent = `${$("#chatInput").value.length} / ${MAX_MESSAGE_LENGTH}`;
  }

  /* ---------- 初始化 ---------- */
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("#topTabs .top-tab").forEach((b) => {
      b.addEventListener("click", () => setTab(b.dataset.tab));
    });
    $("#chatSendBtn").addEventListener("click", sendMessage);
    $("#chatCancelBtn").addEventListener("click", () => {
      if (activeController) activeController.abort();
    });
    $("#chatNewSession").addEventListener("click", newSession);
    const input = $("#chatInput");
    input.addEventListener("input", updateCharCount);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
    loadSessions();
    loadDossiers();
  });
})();
