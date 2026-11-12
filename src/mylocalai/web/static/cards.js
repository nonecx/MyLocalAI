/* ============================================================
   cards.js —— 对话卡片渲染器（M2，工作指导 §4.3）
   三个渲染器：结论卡 / 言论土壤卡（classic 四档） / 热度 E 分卡。
   标记从 app.js 现有卡片"复制 + 参数化"而来，不修改 app.js；
   由 chat.js 调用（chat.js 不 import app.js 的任何符号）。

   口径诚实铁律：数字旁必须展示 as_of 与徽章（置信度 / 降级 / 代理估计 /
   verdict_basis）——分数是纯函数从证据推导的，展示层不替它"说满"。
   ============================================================ */
"use strict";

(function () {
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmtNum(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    if (n !== 0 && Math.abs(n) < 0.001) return n.toExponential(3);
    return n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  }

  /* classic 四档展示配置（与 app.js CONCLUSION_META 同一份口径，复制而来；
     两边只读不写，改档位文案要同步） */
  const SPEECH_META = {
    "高度疑似水军/煽动性言论": { cls: "danger", icon: "🚨", desc: "高情绪强度 + 高攻击性 + 弱逻辑，符合水军/煽动性言论的特征组合" },
    "疑似情绪化谣言": { cls: "warn", icon: "⚠️", desc: "情绪强度较高且论证逻辑薄弱，存在传播情绪化谣言的特征" },
    "理性探讨": { cls: "good", icon: "✅", desc: "论证逻辑清晰，且无明显攻击性" },
    "情绪化宣泄/普通网络争论": { cls: "info", icon: "💬", desc: "未命中高风险特征组合，属一般性表达" },
  };

  const HEAT_BAND_CLS = {
    "高关注": "red",
    "中关注": "amber",
    "低关注": "green",
    "冷启动": "gray",
  };

  const CONF_CLS = { high: "green", medium: "amber", low: "gray" };

  function degradedBadge(p) {
    return p && p.degraded ? '<span class="badge amber">降级输出</span>' : "";
  }

  /* as_of 是每张卡的"证据截至时间"，缺省也要明说未知，不许静默省略 */
  function asOfLine(asOf) {
    return `<div class="chat-asof">证据截至（as_of）：${escapeHtml(asOf || "未知")}</div>`;
  }

  /* ---------- 1. 结论卡（§4.5：标签 + 置信度 + 证据充分度 + as_of + 复审条件） ---------- */
  function renderConclusion(c) {
    c = c || {};
    const suff = (c.evidence_sufficiency && typeof c.evidence_sufficiency === "object")
      ? c.evidence_sufficiency : {};
    const riskRaw = c.risk_score;
    const risk = Number(riskRaw);
    const hasRisk = riskRaw !== null && riskRaw !== undefined && Number.isFinite(risk);
    const conf = String(c.confidence || "");
    const recheck = Array.isArray(c.recheck_conditions) ? c.recheck_conditions : [];
    return `
      <div class="chat-card chat-card-conclusion">
        <div class="chat-card-title">🧾 核查结论<span class="badge gray">结论卡</span>${degradedBadge(c)}</div>
        <div class="chat-card-label">${escapeHtml(c.label || "（无标签）")}</div>
        ${hasRisk ? `
        <div class="score-line">
          <div class="score-num">风险分 <b>${risk}</b> / 100</div>
          <div class="score-bar"><i style="width: ${Math.min(100, Math.max(0, risk))}%"></i></div>
        </div>` : ""}
        <div class="chat-badges">
          ${conf ? `<span class="badge ${CONF_CLS[conf] || "gray"}">置信度 ${escapeHtml(conf)}</span>` : ""}
          <span class="badge gray">证据充分度：官方 ${Number(suff.official) || 0} · 非官方 ${Number(suff.unofficial) || 0} · 未知 ${Number(suff.unknown) || 0} / 共 ${Number(suff.total) || 0} 条</span>
        </div>
        ${asOfLine(c.as_of)}
        ${recheck.length ? `
        <div class="chat-recheck">
          <b>何时该再看一眼（复审条件）：</b>
          <ul>${recheck.map((r) => `<li>${escapeHtml(String(r))}</li>`).join("")}</ul>
        </div>` : ""}
        ${c.verdict_basis ? `<p class="chat-basis">${escapeHtml(String(c.verdict_basis))}</p>` : ""}
      </div>`;
  }

  /* ---------- 2. 言论土壤卡（classic 四档） ---------- */
  function renderSpeech(p) {
    p = p || {};
    const label = String(p.label || p.conclusion || "");
    const meta = SPEECH_META[label] || { cls: "info", icon: "💬", desc: "" };
    /* 三维分项（analyze_speech 附带时展示；复制 app.js dim-row 参数化） */
    const e = (p.emotion && typeof p.emotion === "object") ? p.emotion : null;
    const a = (p.attack && typeof p.attack === "object") ? p.attack : null;
    const l = (p.logic && typeof p.logic === "object") ? p.logic : null;
    const dims = (e || a || l) ? `
        <div class="chat-dims">
          ${e ? `<div class="dim-row"><span class="k">情感倾向</span><span class="v">${escapeHtml(e.emotion_tendency || "未知")}（强度 ${Number(e.emotion_score) || 0}/10）</span></div>` : ""}
          ${a ? `<div class="dim-row"><span class="k">攻击等级</span><span class="v">${escapeHtml(a.aggression_level || "未知")} · 对象 ${escapeHtml(a.attack_target || "未知")}</span></div>` : ""}
          ${l ? `<div class="dim-row"><span class="k">逻辑关联</span><span class="v">${escapeHtml(l.logic_relevance || "未知")}${l.logic_fallacy && l.logic_fallacy !== "无" ? ` · 谬误：${escapeHtml(l.logic_fallacy)}` : ""}</span></div>` : ""}
        </div>` : "";
    return `
      <div class="chat-card chat-card-speech">
        <div class="chat-card-title">🗣️ 言论土壤<span class="badge gray">classic 四档</span>${degradedBadge(p)}</div>
        <div class="chat-speech-banner ${meta.cls}">
          <span class="chat-speech-icon">${meta.icon}</span>
          <div>
            <div class="chat-speech-label">${escapeHtml(label || "（无标签）")}</div>
            <div class="chat-speech-desc">${escapeHtml(meta.desc)}</div>
          </div>
        </div>
        ${dims}
        ${asOfLine(p.as_of)}
      </div>`;
  }

  /* ---------- 3. 热度 / E 分卡（确定性公式；y/z 必须标注代理估计口径） ---------- */
  function renderHeat(p) {
    p = p || {};
    const scoreRaw = (p.score !== undefined && p.score !== null) ? p.score : p.e_score;
    const score = Number(scoreRaw);
    const hasScore = scoreRaw !== null && scoreRaw !== undefined && Number.isFinite(score);
    const band = String(p.band || "");
    const profile = String(p.profile || "");
    const flags = Array.isArray(p.proxy_flags) ? p.proxy_flags : [];
    return `
      <div class="chat-card chat-card-heat">
        <div class="chat-card-title">📈 热度 / E 分<span class="badge gray">确定性公式</span>${degradedBadge(p)}</div>
        <div class="score-line">
          <div class="score-num">关注度 <b>E = ${hasScore ? score.toFixed(2) : "—"}</b> / 100</div>
          <div class="score-bar"><i style="width: ${hasScore ? Math.min(100, Math.max(0, score)) : 0}%"></i></div>
        </div>
        <div class="chat-badges">
          ${band ? `<span class="badge ${HEAT_BAND_CLS[band] || "gray"}">${escapeHtml(band)}</span>` : ""}
          ${profile ? `<span class="badge gray">档位 ${escapeHtml(profile)}</span>` : ""}
          ${flags.length
            ? `<span class="badge amber">代理/估计值：${escapeHtml(flags.join("、"))}</span>`
            : '<span class="badge amber">代理 / 估计值</span>'}
        </div>
        <div class="chat-dims">
          <div class="dim-row"><span class="k">x · 文章数量</span><span class="v">${fmtNum(p.x)}</span></div>
          <div class="dim-row"><span class="k">y · 文章阅读量</span><span class="v">${fmtNum(p.y)}</span></div>
          <div class="dim-row"><span class="k">z · 文章留言量</span><span class="v">${fmtNum(p.z)}</span></div>
        </div>
        ${asOfLine(p.as_of)}
      </div>`;
  }

  /* ---------- 新事实核查答案（双维、逐 claim、默认不泄露旧评分） ---------- */
  const RUMOR_LABELS = {
    propagation_insufficient: "传播证据不足",
    awaiting_review: "尚待核查",
    suspected_rumor: "疑似谣言（尚未证实）",
    refuted_rumor: "已证伪的谣言",
    no_specific_issue_found: "未发现具体问题",
  };

  function renderClaimAnswer(answer) {
    answer = answer && typeof answer === "object" ? answer : {};
    const claims = Array.isArray(answer.claims) ? answer.claims : [];
    if (answer.answer_status !== "validated") {
      const rows = claims.map((claim) => {
        const limitations = Array.isArray(claim.limitations) ? claim.limitations : [];
        return `<li><b>${escapeHtml(claim.claim_excerpt || "待核查主张")}</b>` +
          `<div>真实性判断：尚未形成经过完整校验的答案。</div>` +
          `<div>谣言状态：暂不下五态结论。</div>` +
          `${limitations.length ? `<ul>${limitations.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>` : ""}</li>`;
      }).join("");
      return `<section class="chat-card chat-card-claim-answer" data-answer-status="${escapeHtml(answer.answer_status || "insufficient_data")}">` +
        `<div class="chat-card-title">事实核查</div>` +
        `<p>本轮没有足够的已核验材料形成完整判断；旧风险标签不代表真假或谣言状态。</p>` +
        `${rows ? `<ol>${rows}</ol>` : ""}` +
        `${Array.isArray(answer.limitations) && answer.limitations.length ? `<ul>${answer.limitations.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>` : ""}` +
        `</section>`;
    }
    const rows = claims.map((claim) => {
      const truth = claim.truth_assessment || {};
      const rumor = claim.rumor_assessment || {};
      const evidence = Array.isArray(claim.key_evidence) ? claim.key_evidence : [];
      const records = Array.isArray(claim.propagation_records) ? claim.propagation_records : [];
      const refs = evidence.map((item) => `<li><span>${escapeHtml(item.title || item.source_id || "来源")}</span>` +
        `${item.url ? ` <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">打开来源</a>` : ""}` +
        `${item.quote ? `<blockquote>${escapeHtml(item.quote)}</blockquote>` : ""}</li>`).join("");
      const propagation = records.map((item) => `<li><a href="${escapeHtml(item.url || "")}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.url || "公开发布记录")}</a>` +
        `${item.verified_at ? `（核验于 ${escapeHtml(item.verified_at)}）` : ""}</li>`).join("");
      const limitations = [...(rumor.limitations || []), ...(truth.limitations || [])];
      return `<li class="claim-answer-item"><b>${escapeHtml(claim.claim_excerpt || "待核查主张")}</b>` +
        `<p>真实性判断：${escapeHtml(truth.statement || "未提供")}</p>` +
        `<p>谣言状态：<strong>${escapeHtml(RUMOR_LABELS[rumor.state] || "状态未知")}</strong></p>` +
        `${refs ? `<details open><summary>关键证据</summary><ul>${refs}</ul></details>` : ""}` +
        `${propagation ? `<details><summary>公开传播记录</summary><ul>${propagation}</ul></details>` : ""}` +
        `${limitations.length ? `<ul>${limitations.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>` : ""}` +
        `</li>`;
    }).join("");
    return `<section class="chat-card chat-card-claim-answer" data-answer-status="validated">` +
      `<div class="chat-card-title">事实核查</div><ol>${rows}</ol>` +
      `<div class="chat-asof">核查时间：${escapeHtml(answer.checked_at || "未知")}</div></section>`;
  }

  /* ---------- 分发：优先 claim_answer，其次保留既有业务卡片兼容 ---------- */
  function renderCard(payload) {
    if (!payload || typeof payload !== "object") return "";
    if (payload.claim_answer && typeof payload.claim_answer === "object") {
      return renderClaimAnswer(payload.claim_answer);
    }
    const type = String(payload.card_type || payload.type || "").toLowerCase();
    if (type === "conclusion") return renderConclusion(payload);
    if (type === "speech" || type === "classic") return renderSpeech(payload);
    if (type === "heat" || type === "attention") return renderHeat(payload);
    /* 无 card_type 时按字段形态识别（三个渲染器互不混淆的判据） */
    if (payload.score !== undefined || payload.e_score !== undefined) return renderHeat(payload);
    if (payload.emotion || payload.attack || payload.logic) return renderSpeech(payload);
    return renderConclusion(payload);
  }

  /* 供 chat.js 调用的唯一入口（不污染全局命名空间） */
  window.ChatCards = { renderCard, renderClaimAnswer, renderConclusion, renderSpeech, renderHeat, escapeHtml };
})();
