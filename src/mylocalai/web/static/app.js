/* ============================================================
   网络言论智能分析系统 —— 前端交互逻辑（原生 JS，无框架依赖）
   两种分析模式：
     classic  三维分析 + 规则引擎（10-30 秒）
     agent    谣言识别 Agent，联网检索证据本（30-120 秒）
   ============================================================ */
"use strict";

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
    <pre>&lt;项目目录&gt;/.venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000</pre>
    <p>然后访问 <a href="http://127.0.0.1:8000">http://127.0.0.1:8000</a></p>`;
  document.body.prepend(banner);
  throw new Error("file:// 协议下无法运行，请通过后端服务访问（见页面提示）");
}

const $ = (sel) => document.querySelector(sel);

const API = {
  analyze: "/api/analyze",
  history: "/api/history",
  health: "/api/health",
  attention: "/api/attention",
  attentionSearch: "/api/attention/search",
  attentionProfiles: "/api/attention/profiles",
};

/* ---------- 示例文本（与测试用例同源） ---------- */
const SAMPLES = [
  { label: "辱骂攻击", text: "你真是个蠢货，滚开！别在这丢人现眼。" },
  { label: "群体煽动", text: "这些人就是社会的垃圾，全都该滚出去！大家都这么说！" },
  { label: "情绪化谣言", text: "气死了！听说自来水被人下毒了，所有人都被骗了，官方根本不敢说！" },
  { label: "健康谣言", text: "网传喝隔夜茶会致癌，我朋友在医院上班说已经有好几个人因此确诊了，大家千万别再喝了，转给家人看看！" },
  { label: "理性论证", text: "根据统计数据，因为样本量超过一万份，所以这个结论具有较高的可信度，证据比较充分。" },
];

/* ---------- 两种模式的展示配置 ---------- */
const MODES = {
  classic: {
    steps: ["情感分析", "攻击性分析", "逻辑分析", "生成报告"],
    delays: [300, 4000, 9000, 16000],
    hint: "大模型分析约需 10–30 秒，请稍候…",
    tip: "三维分析 + 规则引擎，约 10–30 秒",
    btn: "🔍 开始分析",
  },
  agent: {
    steps: ["扫描话术特征", "提取事实声明", "联网搜索证据", "汇总风险判定"],
    delays: [500, 4000, 10000, 40000],
    hint: "Agent 需联网搜索证据并多轮调用大模型，约需 30–120 秒，请耐心等待…",
    tip: "联网检索证据本辅助谣言判定，约 30–120 秒",
    btn: "🛰️ Agent 分析",
  },
};
let currentMode = "classic";

/* ---------- 经典模式四档结论的展示配置 ---------- */
const CONCLUSION_META = {
  "高度疑似水军/煽动性言论": { cls: "danger", icon: "🚨", desc: "高情绪强度 + 高攻击性 + 弱逻辑，符合水军/煽动性言论的特征组合" },
  "疑似情绪化谣言": { cls: "warn", icon: "⚠️", desc: "情绪强度较高且论证逻辑薄弱，存在传播情绪化谣言的特征" },
  "理性探讨": { cls: "good", icon: "✅", desc: "论证逻辑清晰，且无明显攻击性" },
  "情绪化宣泄/普通网络争论": { cls: "info", icon: "💬", desc: "未命中高风险特征组合，属一般性表达" },
};

/* ---------- Agent 模式五档结论的展示配置 ---------- */
const AGENT_LABEL_META = {
  "高风险谣言": { cls: "danger", icon: "🚨", desc: "谣言话术特征集中，或已有独立可靠来源辟谣，请勿转发" },
  "疑似谣言": { cls: "warn", icon: "⚠️", desc: "谣言特征明显或已有可靠来源反驳，转发前务必核实原始信源" },
  "存疑待核实": { cls: "amber", icon: "❓", desc: "存在一定风险特征，暂不建议传播，建议交叉验证" },
  "低风险": { cls: "good", icon: "✅", desc: "未发现明显谣言特征，但这不等于内容属实，重要信息仍建议核对来源" },
  "无可核实陈述": { cls: "info", icon: "💬", desc: "文本没有可被核实的事实性陈述，不属于谣言类内容" },
};

/* ---------- 等级徽章 ----------
   kind: "attack"（攻击性等级 高/中/低）| "logic"（逻辑关联性 强/中/弱） */
function badge(value, kind) {
  const cls = { "高": "red", "中": "amber", "低": "green", "强": "green", "弱": "red" }[value] || "gray";
  const text = kind === "attack" ? `攻击性${value}` : `关联性${value}`;
  return `<span class="badge ${cls}">${text}</span>`;
}

/* ---------- 服务状态 ---------- */
async function checkHealth() {
  const chip = $("#apiStatus");
  try {
    const resp = await fetch(API.health);
    if (!resp.ok) throw new Error("bad status");
    chip.textContent = "● 服务运行中";
    chip.className = "status-chip ok";
  } catch (e) {
    chip.textContent = "● 服务不可用";
    chip.className = "status-chip bad";
  }
}

/* ---------- 示例按钮 ---------- */
function buildSamples() {
  const bar = $("#sampleBar");
  SAMPLES.forEach((s) => {
    const btn = document.createElement("button");
    btn.className = "sample-btn";
    btn.textContent = s.label;
    btn.addEventListener("click", () => {
      $("#inputText").value = s.text;
      updateCount();
    });
    bar.appendChild(btn);
  });
}

/* ---------- 输入字数 ---------- */
function updateCount() {
  $("#charCount").textContent = `${$("#inputText").value.length} / 8000`;
}

/* ---------- 模式切换 ---------- */
function setMode(mode) {
  if (!MODES[mode]) return;
  currentMode = mode;
  document.querySelectorAll("#modeSwitch .mode-btn")
    .forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#modeTip").textContent = MODES[mode].tip;
  $("#analyzeBtn").textContent = MODES[mode].btn;
}

/* ---------- 加载步骤动画 ----------
   按当前模式与经验耗时近似推进各步骤，纯视觉指示。 */
let stepTimers = [];

function startSteps() {
  const cfg = MODES[currentMode];
  $("#steps").innerHTML = cfg.steps
    .map((s) => `<div class="step"><span class="dot"></span>${s}</div>`).join("");
  $(".loading-hint").textContent = cfg.hint;
  const steps = document.querySelectorAll("#steps .step");
  cfg.delays.forEach((ms, i) => {
    stepTimers.push(setTimeout(() => {
      if (i > 0) steps[i - 1].classList.replace("active", "done");
      steps[i].classList.add("active");
    }, ms));
  });
}

function stopSteps() {
  stepTimers.forEach(clearTimeout);
  stepTimers = [];
  document.querySelectorAll("#steps .step").forEach((el) => {
    el.classList.remove("active");
    el.classList.add("done");
  });
}

/* ---------- 分析流程 ---------- */
function showError(msg) {
  const box = $("#errorBox");
  box.textContent = msg;
  box.classList.remove("hidden");
}

/* 当前请求的取消句柄：上游挂掉时单次分析最差要挂几分钟，用户必须能中断。 */
let activeController = null;

function cancelAnalysis() {
  if (activeController) {
    activeController.abort();
  }
}

async function analyze() {
  const text = $("#inputText").value.trim();
  const btn = $("#analyzeBtn");
  const cancelBtn = $("#cancelBtn");
  if (!text) { showError("请先输入要分析的文本，或点击上方示例一键填充。"); return; }

  $("#errorBox").classList.add("hidden");
  $("#resultPanel").classList.add("hidden");
  btn.disabled = true;
  btn.textContent = "分析中…";
  cancelBtn.classList.remove("hidden");
  $("#loading").classList.remove("hidden");
  startSteps();

  activeController = new AbortController();
  try {
    const resp = await fetch(API.analyze, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, mode: currentMode }),
      signal: activeController.signal,
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `请求失败（HTTP ${resp.status}）`);
    renderResult(data);
    await loadHistory();
  } catch (err) {
    if (err.name === "AbortError") {
      showError("已取消本次分析（服务端仍会把这一条跑完并计入历史，可以稍后回看）。");
    } else {
      showError(`分析失败：${err.message}。请确认服务已启动、.env 已配置 API Key。`);
    }
  } finally {
    activeController = null;
    stopSteps();
    $("#loading").classList.add("hidden");
    cancelBtn.classList.add("hidden");
    btn.disabled = false;
    btn.textContent = MODES[currentMode].btn;
  }
}

/* ---------- 结果渲染（按记录类型分发） ---------- */
function renderResult(rec) {
  if (rec.mode === "agent") renderAgentResult(rec);
  else renderClassicResult(rec);
  $("#resultPanel").classList.remove("hidden");
  $("#resultPanel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderClassicResult(rec) {
  $("#agentResult").classList.add("hidden");
  $("#classicResult").classList.remove("hidden");

  /* 结论横幅 */
  const meta = CONCLUSION_META[rec.conclusion] || { cls: "info", icon: "💬", desc: "" };
  const banner = $("#conclusionBanner");
  banner.className = `conclusion ${meta.cls}`;
  banner.innerHTML = `
    <div class="c-title">${meta.icon} 该言论有较高可能性为【${rec.conclusion}】</div>
    <div class="c-desc">${meta.desc}</div>
    <div class="c-meta">快速分析 · 耗时 ${rec.elapsed}s · ${rec.analyzed_at} · 结果由 AI 初步分析生成，最终判断权在用户</div>
  `;

  renderEmotion($("#dimEmotion"), rec.emotion || {});
  renderAttack($("#dimAttack"), rec.attack || {});
  renderLogic($("#dimLogic"), rec.logic || {});

  $("#reportText").textContent = rec.report || "（无报告内容）";

  /* 关注度：分析链路附带的确定性分数（估计口径） */
  const inline = attentionInline(rec.attention);
  if (inline) {
    const box = $("#classicAttention");
    if (box) { box.innerHTML = inline; box.classList.remove("hidden"); }
  }
}

function renderAgentResult(rec) {
  $("#classicResult").classList.add("hidden");
  $("#agentResult").classList.remove("hidden");

  const meta = AGENT_LABEL_META[rec.conclusion] || { cls: "info", icon: "💬", desc: "" };
  const banner = $("#conclusionBanner");
  banner.className = `conclusion ${meta.cls}`;
  banner.innerHTML = `
    <div class="c-title">${meta.icon} 谣言判定：【${rec.conclusion}】</div>
    <div class="c-desc">${meta.desc}</div>
    <div class="c-meta">Agent 联网取证 · 耗时 ${rec.elapsed}s · ${rec.analyzed_at} · 标签与风险分由确定性规则计算，大模型仅调度工具并撰写解释</div>
  `;

  const score = Number(rec.risk_score) || 0;
  const bd = rec.score_breakdown || {};
  const ev = rec.evidence_book || {};
  const evSummary = ev.stance_summary || {};

  let html = `
    ${(rec.emotion || rec.attack || rec.logic) ? `
    <div class="dims" id="agentDims">
      <article class="dim-card" id="agentDimEmotion"></article>
      <article class="dim-card" id="agentDimAttack"></article>
      <article class="dim-card" id="agentDimLogic"></article>
    </div>` : ""}
    <div id="agentAttention" class="attn-inline-box hidden"></div>
    <div class="agent-sec">
      <h3>📊 谣言风险分</h3>
      <div class="score-line">
        <div class="score-num">风险分 <b>${score}</b> / 100 · 置信度${escapeHtml(rec.confidence || "—")} · 言论分层：${escapeHtml(rec.speech_tier || "—")}</div>
        <div class="score-bar"><i style="width: ${Math.min(100, score)}%"></i></div>
      </div>
      ${Object.keys(bd).length ? `<div class="bd-chips">${Object.entries(bd)
        .map(([k, v]) => `<span class="kw">${escapeHtml(k)} ${Number(v) >= 0 ? "+" : ""}${Number(v)}</span>`)
        .join("")}</div>` : ""}
    </div>
    <div class="agent-sec">
      <h3>🚩 风险信号</h3>
      ${Array.isArray(rec.signals) && rec.signals.length
        ? `<ul class="sig-list">${rec.signals.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ul>`
        : '<p class="muted">（无）</p>'}
    </div>
  `;

  /* 待核实的事实声明 */
  const claims = Array.isArray(rec.claims) ? rec.claims : [];
  if (claims.length) {
    html += `
      <div class="agent-sec">
        <h3>📌 待核实的事实声明</h3>
        <ul class="claim-list">${claims.map((c) => `
          <li>
            <span class="badge ${c.has_source ? "green" : "red"}">${c.has_source ? "有来源" : "无来源"}</span>
            <span class="claim-text">${escapeHtml(c.claim || "")}</span>
            <span class="claim-ver">${escapeHtml(c.verifiability || "")}</span>
          </li>`).join("")}</ul>
      </div>`;
  }

  /* 联网证据本 */
  html += evidenceSection(ev, evSummary);

  /* Agent 解释与建议 */
  html += `
    <div class="agent-sec">
      <h3>💬 Agent 解释</h3>
      <p class="agent-expl">${escapeHtml(rec.explanation || "")}</p>
      ${String(rec.explanation_source || "").startsWith("template")
        ? '<p class="dim-error">⚠ 大模型解释与确定性结论不一致（或不可用），已改用模板解释。</p>'
        : ""}
    </div>
    <div class="agent-sec advice-sec">
      <h3>✅ 核实建议</h3>
      <p>${escapeHtml(rec.advice || "")}</p>
    </div>
    <p class="agent-meta">模式 ${escapeHtml(rec.agent_mode || "agent")} · 调用工具 ${rec.tools_called || 0} 次 · LLM 轮次 ${rec.llm_turns || 0}${Array.isArray(rec.degraded_components) && rec.degraded_components.length
      ? ` · 降级组件：${escapeHtml(rec.degraded_components.join("、"))}` : ""}${rec.truncated ? " · 文本过长已截断" : ""}</p>
  `;
  $("#agentResult").innerHTML = html;

  /* 三维分析：复用 classic 的同一套渲染函数（2026-10-22 修复 F8）。
     后端没给组件结果（例如组件全部降级）时整块隐藏，不显示空卡片。 */
  if (rec.emotion || rec.attack || rec.logic) {
    renderEmotion($("#agentDimEmotion"), rec.emotion || {});
    renderAttack($("#agentDimAttack"), rec.attack || {});
    renderLogic($("#agentDimLogic"), rec.logic || {});
  }

  /* 关注度：Agent 模式有联网证据本，x 取自证据本独立来源数，比 classic 实在些 */
  const inline = attentionInline(rec.attention);
  if (inline) {
    const box = $("#agentAttention");
    if (box) { box.innerHTML = inline; box.classList.remove("hidden"); }
  }
}

/* ---------- 信源标注 ----------
   README 承诺的展示口径是"官方/非官方"两分（source_kind），页面原先只渲染 5 级
   domain_tier，用户看不到"官方"这个关键信号。这里两个都显示：
   source_kind 是主口径，domain_tier 作为等级补充（两者都有时写成"官方（新华网）· 权威媒体/辟谣平台"）。 */
function sourceKindLabel(entry) {
  const kind = String(entry.source_kind || "").trim();
  const tier = String(entry.domain_tier || "").trim();
  if (kind && tier) return `${kind} · ${tier}`;
  return kind || tier || "未知来源";
}

function evidenceSection(ev, evSummary) {
  if (!ev || typeof ev !== "object" || (!ev.entries && !ev.skipped && !ev.error)) {
    return `<div class="agent-sec"><h3>📚 联网证据本</h3><p class="muted">（本次分析未产生证据本）</p></div>`;
  }
  if (ev.skipped) {
    return `<div class="agent-sec"><h3>📚 联网证据本</h3><p class="muted">${escapeHtml(ev.skipped)}</p></div>`;
  }
  const entries = Array.isArray(ev.entries) ? ev.entries : [];
  if (!entries.length) {
    return `<div class="agent-sec"><h3>📚 联网证据本</h3><p class="muted">未能获取搜索证据（${escapeHtml(ev.error || "无结果")}）</p></div>`;
  }
  const queries = (ev.queries || [])
    .map((q) => (q && typeof q === "object" ? q.query : q))
    .filter(Boolean);
  let html = `
    <div class="agent-sec">
      <h3>📚 联网证据本</h3>
      <div class="ev-hint">证据倾向：<b>${escapeHtml(ev.verdict_hint || "无证据")}</b>${evSummary.reliable_refute
        ? ` · 可靠反驳来源 ${evSummary.reliable_refute} 个独立域名` : ""}${evSummary.reliable_support
        ? ` · 可靠支持来源 ${evSummary.reliable_support} 条` : ""}</div>
      ${queries.length ? `<div class="query-row">检索词：${queries
        .map((q) => `<span class="kw">${escapeHtml(q)}</span>`).join("")}</div>` : ""}
      <ul class="ev-list">${entries.map((e) => {
        const stanceCls = { "反驳": "red", "支持": "green", "无关": "gray" }[e.stance] || "gray";
        const url = String(e.url || "");
        const safeUrl = /^https?:\/\//i.test(url) ? escapeHtml(url) : "";
        const shortUrl = url.length > 90 ? url.slice(0, 90) + "…" : url;
        return `<li class="ev-entry">
          <div class="ev-top">
            <span class="badge ${stanceCls}">${escapeHtml(e.stance || "无关")}</span>
            <span class="ev-tier">${escapeHtml(sourceKindLabel(e))}</span>
            ${e.date ? `<span class="ev-date">${escapeHtml(e.date)}</span>` : ""}
            <span class="ev-title">${escapeHtml(String(e.title || "").slice(0, 60))}</span>
          </div>
          ${safeUrl ? `<a class="ev-url" href="${safeUrl}" target="_blank" rel="noopener nofollow">${escapeHtml(shortUrl)}</a>` : ""}
          ${e.snippet ? `<p class="ev-snippet">${escapeHtml(String(e.snippet).slice(0, 140))}</p>` : ""}
        </li>`;
      }).join("")}</ul>
      ${ev.llm_error ? `<p class="dim-error">部分证据立场由关键词规则判定（LLM 判定部分失败：${escapeHtml(ev.llm_error)}）</p>` : ""}
    </div>`;
  return html;
}

function dimErrorNote(obj) {
  return obj.error ? `<div class="dim-error">⚠ 该维度分析降级：${obj.error}</div>` : "";
}

function renderEmotion(card, e) {
  const score = Number(e.emotion_score) || 0;
  const kws = Array.isArray(e.keywords) && e.keywords.length
    ? e.keywords.map((k) => `<span class="kw">${escapeHtml(String(k))}</span>`).join("")
    : '<span class="kw">（无）</span>';
  card.innerHTML = `
    <h3>🎭 情感特征</h3>
    <div class="dim-row"><span class="k">情感倾向</span><span class="v">${escapeHtml(e.emotion_tendency || "未知")}</span></div>
    <div class="score-line">
      <div class="score-num">情绪化强度 <b>${score}</b> / 10</div>
      <div class="score-bar"><i style="width: ${Math.min(100, score * 10)}%"></i></div>
    </div>
    <div class="kw-wrap">${kws}</div>
    ${dimErrorNote(e)}
  `;
}

function renderAttack(card, a) {
  card.innerHTML = `
    <h3>🎯 攻击性分析</h3>
    <div class="dim-row"><span class="k">攻击对象</span><span class="v">${escapeHtml(a.attack_target || "未知")}</span></div>
    <div class="dim-row"><span class="k">攻击等级</span><span class="v">${badge(a.aggression_level, "attack")}</span></div>
    <div class="dim-row"><span class="k">攻击类型</span><span class="v">${escapeHtml(a.attack_type || "未知")}</span></div>
    ${dimErrorNote(a)}
  `;
}

function renderLogic(card, l) {
  const fallacy = l.logic_fallacy || "未知";
  const fallacyView = fallacy === "无"
    ? '<span class="badge green">未发现明显谬误</span>'
    : fallacy === "未知"
      ? '<span class="badge gray">未知</span>'
      : `<span class="badge red">${escapeHtml(fallacy)}</span>`;
  card.innerHTML = `
    <h3>⛓️ 逻辑链路</h3>
    <div class="dim-row"><span class="k">论据</span><span class="v">${escapeHtml(l.argument || "未知")}</span></div>
    <div class="dim-row"><span class="k">结论</span><span class="v">${escapeHtml(l.conclusion || "未知")}</span></div>
    <div class="dim-row"><span class="k">关联性</span><span class="v">${badge(l.logic_relevance, "logic")}</span></div>
    <div class="dim-row"><span class="k">谬误类型</span><span class="v">${fallacyView}</span></div>
    ${dimErrorNote(l)}
  `;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* ---------- 历史记录 ---------- */
let historyCache = [];

async function loadHistory() {
  try {
    const resp = await fetch(API.history);
    if (!resp.ok) return;
    historyCache = await resp.json();
    const list = $("#historyList");
    $("#historyCount").textContent = String(historyCache.length);

    if (!historyCache.length) {
      list.innerHTML = '<li class="empty">暂无记录，去分析第一条言论吧</li>';
      return;
    }
    list.innerHTML = historyCache.map((rec, idx) => `
      <li data-idx="${idx}">
        <div class="h-conclusion"><span class="mode-chip ${rec.mode === "agent" ? "agent" : "classic"}">${rec.mode === "agent" ? "Agent" : "快速"}</span>${conclusionIcon(rec.conclusion)} ${escapeHtml(rec.conclusion)}</div>
        <div class="h-text">${escapeHtml(rec.text)}</div>
        <div class="h-meta">${rec.analyzed_at} · ${rec.elapsed}s</div>
      </li>
    `).join("");

    list.querySelectorAll("li[data-idx]").forEach((li) => {
      li.addEventListener("click", () => {
        list.querySelectorAll("li").forEach((x) => x.classList.remove("active"));
        li.classList.add("active");
        renderResult(historyCache[Number(li.dataset.idx)]);
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
    });
  } catch (e) { /* 历史加载失败不影响主流程 */ }
}

function conclusionIcon(c) {
  return (CONCLUSION_META[c] || AGENT_LABEL_META[c] || {}).icon || "💬";
}

/* ============================================================
   关注度（Attention）评估
   公式 E = 100*(1-exp(-λS))，分数由后端确定性计算，不调用大模型。
   两种取数方式：
     manual  用户在页面填 x/y/z
     search  输入话题，后端联网估出 x/y/z（y/z 是代理值，必须标注）
   ============================================================ */

const ATTN_DEFAULTS = { x: 100, y: 100000, z: 1000 };

/* 档位 → 徽章配色。与后端 DEFAULT_BANDS 的 label 一一对应。 */
const ATTN_BAND_CLS = {
  "高关注": "red",
  "中关注": "amber",
  "低关注": "green",
  "冷启动": "gray",
};

/* 质量标记 → 说明文字。estimated=true 时一定要让用户看见「这是代理值」。 */
function attnQualityNote(d) {
  if (d.quality === "missing") {
    return '<span class="badge gray">未取到数据</span>';
  }
  return d.estimated
    ? '<span class="badge amber">代理 / 估计值</span>'
    : '<span class="badge green">页面公开值</span>';
}

function attnError(msg) {
  const box = $("#attnError");
  box.textContent = msg;
  box.classList.remove("hidden");
}

function attnClearError() {
  $("#attnError").classList.add("hidden");
}

/* 手动模式：读三个输入框，POST /api/attention */
async function calcAttention() {
  const x = Number($("#attnX").value);
  const y = Number($("#attnY").value);
  const z = Number($("#attnZ").value);
  if (![x, y, z].every((v) => Number.isFinite(v))) {
    attnError("x/y/z 都必须是数字，请检查输入。");
    return;
  }
  attnClearError();
  await postAttention(API.attention, { x, y, z, profile: currentProfile() });
}

/* 话题模式：POST /api/attention/search */
async function searchAttention() {
  const query = $("#attnQuery").value.trim();
  if (!query) {
    attnError("请输入要评估的话题关键词。");
    return;
  }
  attnClearError();
  await postAttention(API.attentionSearch, { query, profile: currentProfile() });
}

function currentProfile() {
  const sel = $("#attnProfile");
  return sel && sel.value ? sel.value : "";
}

/* 拉取并渲染参数档位下拉框 */
async function loadAttnProfiles() {
  const sel = $("#attnProfile");
  if (!sel) return;
  try {
    const resp = await fetch(API.attentionProfiles);
    if (!resp.ok) return;
    const data = await resp.json();
    sel.innerHTML = "";
    (data.profiles || []).forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = p.name + (p.name === data.default ? "（默认）" : "");
      opt.dataset.comment = p.comment || "";
      sel.appendChild(opt);
    });
    sel.value = data.default || "";
    updateProfileDesc();
  } catch (e) { /* 档位列表加载失败不影响主流程，用空下拉框 */ }
}

function updateProfileDesc() {
  const sel = $("#attnProfile");
  const desc = $("#attnProfileDesc");
  if (!sel || !desc) return;
  const opt = sel.selectedOptions[0];
  desc.textContent = opt && opt.dataset.comment ? opt.dataset.comment : "";
}

async function postAttention(url, body) {
  const btn = url === API.attentionSearch ? $("#attnSearchBtn") : $("#attnCalcBtn");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "计算中…";
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `请求失败（HTTP ${resp.status}）`);
    renderAttention(data);
  } catch (err) {
    attnError(`关注度计算失败：${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

/* 渲染关注度结果 */
function renderAttention(d) {
  const score = Number(d.score) || 0;
  const bandCls = ATTN_BAND_CLS[d.band] || "gray";
  const deep = !!d.deep_read;

  const rows = [
    ["x · 文章数量", fmtNum(d.x)],
    ["y · 文章阅读量", fmtNum(d.y)],
    ["z · 文章留言量", fmtNum(d.z)],
  ];

  let html = `
    <div class="attn-score">
      <div class="score-num">关注度 <b>E = ${score.toFixed(2)}</b> / 100</div>
      <div class="score-bar"><i style="width: ${Math.min(100, score)}%"></i></div>
      <div class="attn-band">
        <span class="badge ${bandCls}">${escapeHtml(d.band || "未知")}</span>
        ${attnQualityNote(d)}
        ${deep ? '<span class="badge red">触发深读</span>' : ""}
        ${d.profile ? '<span class="badge gray">档位 ' + escapeHtml(d.profile) + "</span>" : ""}
      </div>
    </div>
    <div class="attn-grid">
      <div class="attn-col">
        <h3>输入</h3>
        ${rows.map(([k, v]) => `<div class="dim-row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join("")}
      </div>
      <div class="attn-col">
        <h3>分项与合成</h3>
        <div class="dim-row"><span class="k">term_x（权重 ${d.params.w_x}）</span><span class="v">${(d.term_x || 0).toFixed(6)}</span></div>
        <div class="dim-row"><span class="k">term_y（权重 ${d.params.w_y}）</span><span class="v">${(d.term_y || 0).toFixed(6)}</span></div>
        <div class="dim-row"><span class="k">term_z（权重 ${d.params.w_z}）</span><span class="v">${(d.term_z || 0).toFixed(6)}</span></div>
        <div class="dim-row"><span class="k">ε</span><span class="v">${d.params.epsilon}</span></div>
        <div class="dim-row"><span class="k">S（加权和）</span><span class="v"><b>${(d.S || 0).toFixed(6)}</b></span></div>
        <div class="dim-row"><span class="k">触发阈值 S</span><span class="v">${d.threshold_s}</span></div>
      </div>
    </div>
  `;

  if (deep) {
    html += `
      <div class="attn-deep">
        <b>已触发深读。</b>
        ${renderDeepRead(d.deep_read)}
      </div>`;
  }

  const notes = Array.isArray(d.notes) ? d.notes : [];
  if (notes.length) {
    html += `<div class="attn-notes"><h3>取数说明</h3><ul>${notes.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul></div>`;
  }

  const sources = Array.isArray(d.sources) ? d.sources : [];
  if (sources.length) {
    html += `
      <div class="attn-notes">
        <h3>证据来源（${sources.length}）</h3>
        <ul class="attn-src">${sources.map((u) => {
          const safe = /^https?:\/\//i.test(u) ? escapeHtml(u) : "";
          return safe ? `<li><a href="${safe}" target="_blank" rel="noopener nofollow">${escapeHtml(u.length > 90 ? u.slice(0, 90) + "…" : u)}</a></li>` : "";
        }).join("")}</ul>
      </div>`;
  }

  const box = $("#attnResult");
  box.innerHTML = html;
  box.classList.remove("hidden");
}

function fmtNum(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  if (n !== 0 && Math.abs(n) < 0.001) return n.toExponential(3);
  return n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

/* 深读解读文本。
   - source=llm 是受控生成的模型解读，source=template 是降级模板，两者必须分清；
   - applicable=false 是「未触发深读解读」出口（纯情绪/纯事实/证据不足），显示原因；
   - degraded=true 显示「降级：本地模板」；
   - proxy_flags 非空显示「代理/估计值」；
   - result 是新 schema 的结构化产出（议题标签/张力/现象/反方/边界/不确定性）。 */
function renderDeepRead(dr) {
  if (!dr || !dr.triggered) return "";
  if (dr.applicable === false) {
    return `
    <div class="attn-deep-body">
      <div class="attn-deep-head"><span class="badge gray">未触发深读解读</span></div>
      <p class="dim-error">${escapeHtml(dr.reason || "该话题不适合做社会议题分析")}</p>
    </div>`;
  }
  if (!dr.text) return "";
  const badge = dr.source === "llm"
    ? '<span class="badge green">大模型解读</span>'
    : '<span class="badge amber">降级：本地模板</span>';
  const warn = dr.degraded
    ? `<p class="dim-error">⚠ ${escapeHtml(dr.degraded_reason || "大模型解读不可用，已回退本地模板")}；以下为框架性提示，不是实证分析。</p>`
    : "";
  const notes = Array.isArray(dr.warnings) && dr.warnings.length
    ? `<p class="dim-error">${escapeHtml(dr.warnings.join("；"))}</p>`
    : "";
  return `
    <div class="attn-deep-body">
      <div class="attn-deep-head">${badge}${deepReadBadges(dr)}</div>
      ${warn}${notes}
      ${renderDeepReadResult(dr.result)}
      <pre class="attn-deep-text">${escapeHtml(dr.text)}</pre>
    </div>`;
}

/* 深读附加徽章：代理/估计值、护栏丢弃提示。proxy_flags 来自结构化信号。 */
function deepReadBadges(dr) {
  const out = [];
  const flags = (dr.signals && Array.isArray(dr.signals.proxy_flags))
    ? dr.signals.proxy_flags : [];
  if (flags.length) out.push('<span class="badge amber">代理/估计值</span>');
  const dropped = Array.isArray(dr.guard_dropped) ? dr.guard_dropped : [];
  if (dropped.length) {
    out.push(`<span class="badge gray">护栏丢弃 ${dropped.length} 条</span>`);
  }
  return out.join("");
}

/* 深读结构化产出：议题标签 / 张力 / 现象 / 反方 / 边界 / 不确定性。
   旧结构（只有 text）没有 result，此时整块不渲染，保持向后兼容。 */
function renderDeepReadResult(res) {
  if (!res || typeof res !== "object") return "";
  const tags = Array.isArray(res.issue_tags) ? res.issue_tags : [];
  const tensions = Array.isArray(res.tensions) ? res.tensions : [];
  const phenomena = Array.isArray(res.phenomena) ? res.phenomena : [];
  const counter = res.counter_reading || null;
  const uncertainty = Array.isArray(res.uncertainty) ? res.uncertainty : [];
  if (!tags.length && !tensions.length && !phenomena.length) return "";

  let html = '<div class="attn-deep-result">';
  if (tags.length) {
    html += `<div class="dim-row"><span class="k">议题标签</span><span class="v">${tags
      .map((t) => `${escapeHtml(t.id || "")}<i class="attn-conf">${escapeHtml(t.confidence || "")}</i>`)
      .join("、")}</span></div>`;
  }
  tensions.forEach((t) => {
    html += `<div class="dim-row"><span class="k">张力（${escapeHtml(t.level || "")}）</span>` +
      `<span class="v">${escapeHtml(t.a || "")} 与 ${escapeHtml(t.b || "")}</span></div>`;
  });
  phenomena.forEach((p) => {
    const cnt = p.count ? `（${escapeHtml(p.count)}）` : "";
    html += `<div class="dim-row"><span class="k">现象（${escapeHtml(p.level || "")}）${cnt}</span>` +
      `<span class="v">${escapeHtml(p.text || "")}</span></div>`;
  });
  if (counter && counter.text) {
    html += `<div class="dim-row"><span class="k">反方解读</span>` +
      `<span class="v">${escapeHtml(counter.text)}</span></div>`;
  }
  if (uncertainty.length) {
    html += `<div class="dim-row"><span class="k">显式不确定性</span>` +
      `<span class="v">${uncertainty.map((u) => escapeHtml(u)).join("；")}</span></div>`;
  }
  html += "</div>";
  return html;
}

/* 分析记录里附带的关注度（classic / agent 两种模式都有） */
function attentionInline(attn) {
  if (!attn || typeof attn !== "object" || attn.score === undefined) return "";
  const score = Number(attn.score) || 0;
  const bandCls = ATTN_BAND_CLS[attn.band] || "gray";
  return `
    <div class="attn-inline">
      <span class="k">关注度</span>
      <span class="score-num">E = <b>${score.toFixed(2)}</b> / 100</span>
      <span class="badge ${bandCls}">${escapeHtml(attn.band || "未知")}</span>
      <span class="badge amber">估计值</span>
      <span class="attn-inline-tip">本次分析未联网取数，y/z 为保守兜底；要看真实量级请用下方「关注度评估」</span>
    </div>`;
}

/* ---------- 关注度面板：页签切换 ---------- */
function setAttnTab(tab) {
  document.querySelectorAll("#attnTabs .attn-tab")
    .forEach((b) => b.classList.toggle("active", b.dataset.attnTab === tab));
  $("#attnManual").classList.toggle("hidden", tab !== "manual");
  $("#attnSearch").classList.toggle("hidden", tab !== "search");
  attnClearError();
}

/* ---------- 初始化 ---------- */
document.addEventListener("DOMContentLoaded", () => {
  buildSamples();
  updateCount();
  $("#inputText").addEventListener("input", updateCount);
  $("#analyzeBtn").addEventListener("click", analyze);
  $("#cancelBtn").addEventListener("click", cancelAnalysis);
  document.querySelectorAll("#modeSwitch .mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });
  $("#clearBtn").addEventListener("click", () => {
    $("#inputText").value = "";
    updateCount();
    $("#errorBox").classList.add("hidden");
  });
  checkHealth();
  loadHistory();

  /* 关注度面板 */
  loadAttnProfiles();
  document.querySelectorAll("#attnTabs .attn-tab").forEach((btn) => {
    btn.addEventListener("click", () => setAttnTab(btn.dataset.attnTab));
  });
  $("#attnCalcBtn").addEventListener("click", calcAttention);
  $("#attnSearchBtn").addEventListener("click", searchAttention);
  $("#attnResetBtn").addEventListener("click", () => {
    $("#attnX").value = ATTN_DEFAULTS.x;
    $("#attnY").value = ATTN_DEFAULTS.y;
    $("#attnZ").value = ATTN_DEFAULTS.z;
    attnClearError();
  });
  $("#attnProfile")?.addEventListener("change", updateProfileDesc);
  ["attnX", "attnY", "attnZ"].forEach((id) => {
    document.getElementById(id).addEventListener("keydown", (e) => {
      if (e.key === "Enter") calcAttention();
    });
  });
  $("#attnQuery").addEventListener("keydown", (e) => {
    if (e.key === "Enter") searchAttention();
  });
});
