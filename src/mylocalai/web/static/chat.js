/* ============================================================
   chat.js —— 对话 Tab 前端（M2，工作指导 §4.3/§5.3）
   独立文件：不 import app.js 的任何符号（旧前端契约不动）。
   职责：
     - fetch POST /api/chat + ReadableStream 手工解析 SSE 帧
       （EventSource 不支持 POST，必须手工拆帧）；
     - AbortController 取消：abort 即断开连接，后端据此走协作取消链；
     - 渲染 §5.3 全部事件：meta/status/tool_start/scan/progress/card/
       narrative/tool_end/done/error（集合 ⊆ events.py EVENT_NAMES，测试锁定）；
     - 会话列表加载/切换/新建（左侧栏）+ 档案库视图（M3：列表 → 详情六章节区块
       + Markdown 导出）+ 被动提醒一行呈现（M3-7，§2.2 不制造新压力）。
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
    sessionReminders: "/api/sessions/{sid}/reminders",
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
    const insights = $("#insightsPanel");
    if (chat) chat.classList.toggle("hidden", tab !== "chat");
    if (tools) tools.classList.toggle("hidden", tab !== "tools");
    if (insights) {
      insights.classList.toggle("hidden", tab !== "insights");
      insights.hidden = tab !== "insights";
    }
    /* 参谋 Tab（L4）：每次切进来刷新一次只读聚合；InsightsPanel 由 insights.js
       提供，带存在性守卫——组件文件缺失时只影响刷新，不挡 Tab 切换 */
    if (tab === "insights" && window.InsightsPanel) InsightsPanel.refresh();
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
    $("#chatMessages").querySelectorAll(".chat-reminder").forEach((n) => n.remove());
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
    /* New answer contract takes precedence over legacy risk cards. */
    if (d && typeof d === "object" && d.claim_answer) {
      partEl(".chat-card-slot").innerHTML = window.ChatCards.renderClaimAnswer(d.claim_answer);
      scrollBottom();
      return;
    }
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
      list.innerHTML = sessions.map((s) => {
        /* M4 会话级累计成本徽章：/api/sessions 每项带 cost 汇总（tool_runs 真实落库值） */
        const cost = (s && s.cost) || {};
        const costTxt = `成本 ${Number(cost.requests) || 0} 次 · ${Number(cost.tokens) || 0} tokens`;
        return `
        <li data-sid="${Number(s.id)}">
          <div class="chat-session-title">${escapeHtml(s.title || "（未命名会话）")}</div>
          <div class="chat-session-meta">${escapeHtml(String(s.updated_at || "").replace("T", " "))}</div>
          <div class="chat-session-meta">${escapeHtml(costTxt)}</div>
        </li>`;
      }).join("");
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
    /* 被动提醒（M3-7）：打开会话/加载消息时取一次，消息流顶部一行浅色呈现；
       取不到就不显示——不弹窗、不打断（§2.2 不制造新压力） */
    loadSessionReminders(sid);
  }

  /* ---------- 被动提醒（M3-7，§2.2"不制造新压力"） ---------- */
  function renderReminderLine(lines) {
    const box = $("#chatMessages");
    if (!box) return;
    const old = box.querySelector(".chat-reminder");
    if (old) old.remove();
    if (!Array.isArray(lines) || !lines.length) return;
    const el = document.createElement("div");
    el.className = "chat-reminder";
    el.textContent = "⏰ " + lines.join("　");
    box.insertBefore(el, box.firstChild);
  }

  async function loadSessionReminders(sid) {
    try {
      const resp = await fetch(apiPath(CHAT_API.sessionReminders, sid));
      if (!resp.ok) { renderReminderLine([]); return; }
      const data = await resp.json();
      renderReminderLine((data && data.lines) || []);
    } catch (e) { /* 提醒加载失败静默：不提示、不重试、不打断会话 */ }
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

  /* ---------- 档案库视图（M3：列表 → 详情六章节区块） ----------
     数据来自 GET /api/dossiers/{id} 的 chapters 字段（service.chapters() 聚合），
     缺章如实显示"本节暂无数据"，不编造（§2.2 口径诚实）。 */

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

  function chapterBlock(title, body) {
    return `<div class="chat-dossier-chapter">
      <div class="chat-dossier-chapter-title">${title}</div>${body}</div>`;
  }

  function chapterAbsent(note) {
    return `<p class="chat-dossier-absent">${escapeHtml(
      note || "本节暂无数据（章节按需生成，不预生成、不编造）")}</p>`;
  }

  /* 分数变化判定：null/undefined 视为"无分"，避免 Number(null)=0 的假相同 */
  function scoreChanged(a, b) {
    const na = (a === null || a === undefined) ? null : Number(a);
    const nb = (b === null || b === undefined) ? null : Number(b);
    if (na === null && nb === null) return false;
    if (na === null || nb === null) return true;
    return na !== nb;
  }

  /* 1. 结论版本对比表：标签/分数/as_of 相对上一版有变化的行高亮 */
  function renderConclusionChapter(c) {
    c = c || {};
    const compare = c.compare || {};
    const versions = compare.versions || [];
    if (!versions.length) return chapterAbsent("尚无结论版本（还没有跑过核查）");
    const rows = versions.map((v, i) => {
      const prev = i > 0 ? versions[i - 1] : null;
      const changed = prev && (prev.label !== v.label
        || scoreChanged(prev.risk_score, v.risk_score)
        || String(prev.as_of || "") !== String(v.as_of || ""));
      const risk = (v.risk_score === null || v.risk_score === undefined)
        ? "—" : Number(v.risk_score);
      return `<tr${changed ? ' class="chat-version-changed"' : ""}>
        <td>v${escapeHtml(String(v.version))}</td>
        <td>${escapeHtml(v.label || "—")}</td>
        <td>${escapeHtml(String(risk))}</td>
        <td>${escapeHtml(String(v.as_of || "未知").replace("T", " "))}</td></tr>`;
    }).join("");
    const badges = compare.changed ? ' <span class="badge amber">标签有变化</span>' : "";
    const degraded = c.degraded ? ' <span class="badge amber">含降级运行</span>' : "";
    return `<table class="chat-versions-table">
        <thead><tr><th>版本</th><th>标签</th><th>风险分</th><th>as_of</th></tr></thead>
        <tbody>${rows}</tbody></table>
      <div class="chat-badges">${badges}${degraded}</div>`;
  }

  /* 2. 证据批次折叠列表（details 原生折叠） */
  function renderEvidenceChapter(e) {
    e = e || {};
    const batches = e.batches || [];
    if (!batches.length) return chapterAbsent("证据本还没有条目（尚未联网取证）");
    const folds = batches.map((b) => {
      const items = (b.entries || []).map((en) => `
        <li><span class="chat-ev-stance">${escapeHtml(en.stance || "无关")}</span>${escapeHtml(en.title || en.url || "（无标题）")}${en.url ? `<div class="chat-ev-url">${escapeHtml(String(en.url))}</div>` : ""}</li>`).join("");
      return `<details class="chat-evidence-fold">
        <summary>批次 ${escapeHtml(String(b.batch))}（${Number(b.count) || 0} 条）</summary>
        <ul class="chat-ev-list">${items || "<li>（无条目）</li>"}</ul></details>`;
    }).join("");
    return `<div class="muted">共 ${Number(e.total) || 0} 条证据</div>${folds}`;
  }

  /* 3. 言论土壤四档卡：形状与 cards.js renderSpeech 的载荷一致，直接复用 */
  function renderSpeechChapter(s) {
    if (!s || s.chapter_status === "absent") return chapterAbsent();
    if (window.ChatCards && window.ChatCards.renderSpeech) {
      return window.ChatCards.renderSpeech({
        label: s.conclusion || s.label || "",
        emotion: s.emotion, attack: s.attack, logic: s.logic, as_of: s.as_of,
      });
    }
    return chapterAbsent("言论土壤渲染组件未就绪");
  }

  /* 4. 热度趋势：最新 E 分 + 代理徽章 + 趋势 SVG 内嵌（组件未收口时占位） */
  function renderHeatChapter(h) {
    h = h || {};
    const snaps = h.snapshots || [];
    if (!snaps.length) return chapterAbsent("还没有热度快照（查过热度才有）");
    const last = snaps[snaps.length - 1];
    const eVal = Number(last.e_score);
    const flags = Array.isArray(last.proxy_flags) ? last.proxy_flags : [];
    const trend = h.trend || {};
    let trendHtml = "";
    if (trend.svg && String(trend.svg).trim().toLowerCase().startsWith("<svg")) {
      trendHtml = `<div class="chat-trend-box">${trend.svg}</div>`;
    } else if (trend.trend_status === "pending") {
      trendHtml = '<div class="chat-trend-pending">⏳ 趋势组件加载中（组件收口后此处自动出趋势图）</div>';
    }
    const summary = (trend.summary && typeof trend.summary === "object")
      ? Object.entries(trend.summary).map(([k, v]) => `${k} ${v}`).join(" · ") : "";
    return `<div class="chat-dims">
        <div class="dim-row"><span class="k">快照次数</span><span class="v">${snaps.length}</span></div>
        <div class="dim-row"><span class="k">最新 E 分</span><span class="v">${Number.isFinite(eVal) ? eVal.toFixed(2) : "—"}（${escapeHtml(String(last.taken_at || "").replace("T", " "))}）</span></div>
      </div>
      ${flags.length ? `<div class="chat-badges"><span class="badge amber">代理值：${escapeHtml(flags.join("、"))}</span></div>` : ""}
      ${trendHtml}
      ${summary ? `<div class="chat-spectrum-legend muted">${escapeHtml(summary)}</div>` : ""}`;
  }

  /* 5. 观点光谱分布条（颜色取自固定表，分布数据只决定宽度） */
  const SPECTRUM_COLORS = ["#34d399", "#fbbf24", "#60a5fa", "#9ca3af", "#f97316"];

  function renderOpinionsChapter(o) {
    if (!o || o.chapter_status === "absent") return chapterAbsent();
    const latest = (o.rows && o.rows.length) ? o.rows[o.rows.length - 1] : {};
    const dist = (latest.distribution && typeof latest.distribution === "object")
      ? latest.distribution : {};
    const entries = Object.entries(dist);
    const total = entries.reduce((s, [, v]) => s + (Number(v) || 0), 0);
    const segs = entries.map(([k, v], i) => {
      const n = Number(v) || 0;
      const pct = total > 0 ? Math.round(n / total * 100) : 0;
      return `<span class="chat-spectrum-seg" style="width:${pct}%;background:${SPECTRUM_COLORS[i % SPECTRUM_COLORS.length]}" title="${escapeHtml(k)} ${n}"></span>`;
    }).join("");
    const legend = entries.map(([k, v]) => `${k} ${Number(v) || 0}`).join(" · ");
    return `<div class="chat-spectrum-bar">${segs || '<span class="muted">（无分布数据）</span>'}</div>
      <div class="chat-spectrum-legend muted">${escapeHtml(legend || "—")}</div>
      ${latest.summary ? `<div class="chat-deepread-text">${escapeHtml(String(latest.summary))}</div>` : ""}
      ${latest.degraded ? '<div class="chat-badges"><span class="badge amber">降级输出</span></div>' : ""}`;
  }

  /* 6. 背景解读文本 + 降级/护栏丢弃徽章 */
  function renderDeepReadChapter(dr) {
    if (!dr || dr.chapter_status === "absent") return chapterAbsent();
    const payload = (dr.payload && typeof dr.payload === "object") ? dr.payload : {};
    const text = String(payload.text || payload.body || "").trim();
    const badges = [];
    if (dr.degraded || payload.degraded) badges.push('<span class="badge amber">降级输出</span>');
    const dropped = Array.isArray(dr.guard_dropped) ? dr.guard_dropped : [];
    if (dropped.length) {
      badges.push(`<span class="badge gray">护栏丢弃 ${dropped.length} 项</span>`);
    }
    return `${badges.length ? `<div class="chat-badges">${badges.join("")}</div>` : ""}
      ${text ? `<div class="chat-deepread-text">${escapeHtml(text)}</div>`
             : '<p class="chat-dossier-absent">解读正文为空：模型判定不适用或输出被护栏丢弃，如实呈现，不代写。</p>'}`;
  }

  function renderDossierChapters(d) {
    const ch = (d && d.chapters) || {};
    return [
      chapterBlock("🧾 结论版本对比", renderConclusionChapter(ch.conclusion)),
      chapterBlock("📑 证据批次", renderEvidenceChapter(ch.evidence)),
      chapterBlock("🗣️ 言论土壤（classic 四档）", renderSpeechChapter(ch.speech)),
      chapterBlock("📈 热度快照与趋势", renderHeatChapter(ch.heat)),
      chapterBlock("⚖️ 观点光谱", renderOpinionsChapter(ch.opinions)),
      chapterBlock("🔭 背景解读（深读）", renderDeepReadChapter(ch.deep_read)),
    ].join("");
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
      const batches = Object.entries(d.evidence_batches || {})
        .map(([b, n]) => `批次${escapeHtml(String(b))}×${escapeHtml(String(n))}`).join(" · ");
      box.innerHTML = `
        <div class="chat-dossier-view">
          ${renderDossierChapters(d)}
          <div class="muted">证据：${batches || "0 条"} · 快照 ${Number(d.snapshot_count) || 0} 次</div>
          <a class="chat-dossier-export" href="${apiPath(CHAT_API.dossierExport, did)}?format=md" download="dossier-${Number(did)}.md">导出 Markdown</a>
        </div>`;
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
    /* 欢迎态示例：点击填入输入框，不自动发送（纯前端便利） */
    document.querySelectorAll(".chat-welcome-sample").forEach((btn) => {
      btn.addEventListener("click", () => {
        const inp = $("#chatInput");
        inp.value = btn.dataset.sample || "";
        inp.focus();
        updateCharCount();
      });
    });
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
