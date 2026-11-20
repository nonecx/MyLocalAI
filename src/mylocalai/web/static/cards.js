/* ============================================================
   cards.js —— 对话卡片渲染器（M2，工作指导 §4.3）
   三个渲染器：结论卡 / 言论土壤卡（classic 四档） / 热度 E 分卡；
   另加我的画像卡（insights 只读聚合）、事实核查答案渲染、材料批次汇总卡
   与领域监测变化卡（monitor_change 订阅跑检）。
   标记从原分析页卡片"复制 + 参数化"而来（原 app.js 已在单页化中删除，
   卡片渲染逻辑现由本文件独家承载）。
   由 chat.js 调用（chat.js 不直接拼卡片 HTML）。

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

  /* classic 四档展示配置（与引擎 verdict 标签同一份口径，复制而来；
     改档位文案要同步后端，展示层只读不写） */
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

  /* 安全外链：只接受 http(s)，其余（javascript: / data: 等）一律不生成 href。
     证据链接来自联网检索结果，属于外部内容，必须按不可信输入处理。 */
  function safeUrl(u) {
    const s = String(u || "").trim();
    return /^https?:\/\//i.test(s) ? escapeHtml(s) : "";
  }

  /* 证据本折叠（结论卡内）：title/stance/source_kind + 可点链接。
     「前 N 条」与「共 M 条」都照后端给的数字写，缺 evidence_total 就只说前 N 条，
     不编造总数（口径诚实）。 */
  function renderEvidencePreview(preview, total) {
    if (!Array.isArray(preview) || !preview.length) return "";
    const items = preview.map((en) => {
      en = en || {};
      const url = safeUrl(en.url);
      const meta = [];
      if (en.stance) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(en.stance))}</span>`);
      if (en.source_kind) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(en.source_kind))}</span>`);
      if (en.domain) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(en.domain))}</span>`);
      const title = escapeHtml(String(en.title || en.url || "（无标题）"));
      const body = url
        ? `<a href="${url}" target="_blank" rel="noopener nofollow">${title}</a>`
        : title;
      return `<li>${meta.join("")}${body}</li>`;
    }).join("");
    const totalTxt = total > preview.length ? `（共 ${total} 条）` : "";
    return `
        <details class="chat-evidence-fold chat-card-evidence">
          <summary>证据本前 ${preview.length} 条${totalTxt}</summary>
          <ul class="chat-ev-list">${items}</ul>
        </details>`;
  }

  /* ---------- 1. 结论卡（§4.5：标签 + 置信度 + 证据充分度 + as_of + 复审条件） ----------
     新增 evidence_preview / evidence_total（M5 单页化）：把联网证据本的前 5 条折叠
     呈现在卡里，用户不必翻档案就能看到支撑材料；只渲染后端给的条数与链接，
     前端不推断、不补造任何证据。 */
  function renderConclusion(c) {
    c = c || {};
    const suff = (c.evidence_sufficiency && typeof c.evidence_sufficiency === "object")
      ? c.evidence_sufficiency : {};
    const preview = Array.isArray(c.evidence_preview) ? c.evidence_preview.slice(0, 5) : [];
    const evTotal = Number(c.evidence_total) || 0;
    const riskRaw = c.risk_score;
    const risk = Number(riskRaw);
    const hasRisk = riskRaw !== null && riskRaw !== undefined && Number.isFinite(risk);
    const conf = String(c.confidence || "");
    const recheck = Array.isArray(c.recheck_conditions) ? c.recheck_conditions : [];
    /* presentation === "simple"（v6 普通用户档案工作区）：后端已隐去 risk_score
       数值（Agent工作流规格 §1.6 方向），这里换标题并加一行说明——简化必须看得见。 */
    const simple = c.presentation === "simple";
    const title = simple ? "🧾 核查结果" : "🧾 核查结论";
    return `
      <div class="chat-card chat-card-conclusion${simple ? " chat-card-simple" : ""}">
        <div class="chat-card-title">${title}${simple ? "" : '<span class="badge gray">结论卡</span>'}${degradedBadge(c)}</div>
        <div class="chat-card-label">${escapeHtml(c.label || "（无标签）")}</div>
        ${hasRisk ? `
        <div class="score-line">
          <div class="score-num">风险分 <b>${risk}</b> / 100</div>
          <div class="score-bar"><i style="width: ${Math.min(100, Math.max(0, risk))}%"></i></div>
        </div>` : ""}
        ${simple ? '<p class="chat-simple-note muted">已按普通用户视图简化：不展示风险分数值；完整数值与证据批次可在档案库查看。</p>' : ""}
        <div class="chat-badges">
          ${conf ? `<span class="badge ${CONF_CLS[conf] || "gray"}">置信度 ${escapeHtml(conf)}</span>` : ""}
          <span class="badge gray">证据充分度：官方 ${Number(suff.official) || 0} · 非官方 ${Number(suff.unofficial) || 0} · 未知 ${Number(suff.unknown) || 0} / 共 ${Number(suff.total) || 0} 条</span>
        </div>
        ${asOfLine(c.as_of)}
        ${renderEvidencePreview(preview, evTotal)}
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
    /* 三维分项（analyze_speech 附带时展示；沿用原分析页 dim-row 的排版） */
    const e = (p.emotion && typeof p.emotion === "object") ? p.emotion : null;
    const a = (p.attack && typeof p.attack === "object") ? p.attack : null;
    const l = (p.logic && typeof p.logic === "object") ? p.logic : null;
    const dims = (e || a || l) ? `
        <div class="chat-dims">
          ${e ? `<div class="dim-row"><span class="k">情感倾向</span><span class="v">${escapeHtml(e.emotion_tendency || "未知")}（强度 ${Number(e.emotion_score) || 0}/10）</span></div>` : ""}
          ${a ? `<div class="dim-row"><span class="k">攻击等级</span><span class="v">${escapeHtml(a.aggression_level || "未知")} · 对象 ${escapeHtml(a.attack_target || "未知")}</span></div>` : ""}
          ${l ? `<div class="dim-row"><span class="k">逻辑关联</span><span class="v">${escapeHtml(l.logic_relevance || "未知")}${l.logic_fallacy && l.logic_fallacy !== "无" ? ` · 谬误：${escapeHtml(l.logic_fallacy)}` : ""}</span></div>` : ""}
        </div>` : "";
    /* 完整分析报告（后端 analyze_speech 附带 card.report）：折叠呈现，
       超长被后端截断时如实标注「已截断」，不让用户以为看到的是全文。 */
    const report = String(p.report || "");
    const reportFold = report ? `
        <details class="chat-evidence-fold chat-card-report">
          <summary>完整分析报告${p.report_truncated ? "（原文过长，后端已截断）" : ""}</summary>
          <pre class="chat-report-text">${escapeHtml(report)}</pre>
        </details>` : "";
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
        ${reportFold}
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

  /* ---------- 4. 我的画像卡（insights 只读聚合：话术模式库 / 信源画像 / 本周简报） ----------
     由后端 insights 工具产出：card = {card_type:"insights", kind, as_of, patterns|profiles|digest}。
     渲染逻辑从原参谋页 insights.js 迁移而来（class 换 chat- 前缀，随对话流内嵌）。
     口径不变：只给计数与排序，不对任何消息或站点判定真假；空数据一律「暂无数据」。 */
  const INSIGHTS_META = {
    patterns: { icon: "🧩", title: "话术模式库" },
    sources: { icon: "🌐", title: "信源画像" },
    weekly: { icon: "🗓️", title: "本周简报" },
  };

  function insightsEmpty() {
    return '<p class="chat-insight-empty">暂无数据</p>';
  }

  function fmtPct(share) {
    const n = Number(share);
    return Number.isFinite(n) ? `${Math.round(n * 100)}%` : "—";
  }

  /* 时间戳展示：后端给 ISO（T 分隔），这里只做可读化，不改数值 */
  function when(s) {
    return escapeHtml(String(s || "").replace("T", " ") || "—");
  }

  /* 高风险类目口径：与引擎 verdict 标签一致，by_label 里命中的才计入 */
  const HIGH_RISK_LABELS = ["高风险谣言", "疑似谣言"];

  /* 4.1 话术模式库：结论分布 + 高风险话术榜 + 常一起出现的话术组合 */
  function renderInsightPatterns(p) {
    if (!p || typeof p !== "object") return insightsEmpty();
    const risks = Array.isArray(p.risk_patterns) ? p.risk_patterns : [];
    const pairs = Array.isArray(p.common_pairs) ? p.common_pairs : [];
    if (!Number(p.total_dossiers) && !Number(p.with_markers) && !risks.length && !pairs.length) {
      return insightsEmpty();
    }
    const labelMap = (p.by_label && typeof p.by_label === "object") ? p.by_label : {};
    const labelBits = Object.keys(labelMap).map((k) => `${escapeHtml(k)} ×${Number(labelMap[k]) || 0}`);
    let html = `<p class="chat-insight-count">有结论档案 <b>${Number(p.total_dossiers) || 0}</b> 篇 · 含话术标记 <b>${Number(p.with_markers) || 0}</b> 篇</p>
      <p class="chat-insight-sub muted">结论分布：${labelBits.length ? labelBits.join(" · ") : "暂无数据"}</p>`;
    if (risks.length) {
      const items = risks.map((rp) => {
        rp = rp || {};
        const lab = (rp.by_label && typeof rp.by_label === "object") ? rp.by_label : {};
        const hot = HIGH_RISK_LABELS.reduce((sum, name) => sum + (Number(lab[name]) || 0), 0);
        return `<li><span class="chat-insight-rank-label">${escapeHtml(rp.label || rp.key || "（未命名类目）")}</span>
          <span class="chat-insight-rank-num">${Number(rp.dossiers) || 0} 条 · 占 ${fmtPct(rp.share)} · 其中高风险/疑似 ${hot} 条</span></li>`;
      }).join("");
      html += `<div class="chat-insight-rank-title">高风险话术榜单（按命中档案数）</div>
        <ul class="chat-insight-rank">${items}</ul>`;
    }
    if (pairs.length) {
      const pairItems = pairs.map((pair) => {
        pair = pair || {};
        const labels = Array.isArray(pair.labels) ? pair.labels : [];
        const keys = Array.isArray(pair.keys) ? pair.keys : [];
        const a = labels[0] || keys[0] || "？";
        const b = labels[1] || keys[1] || "？";
        return `<li>${escapeHtml(a)} + ${escapeHtml(b)}
          <span class="chat-insight-rank-num">${Number(pair.dossiers) || 0} 条</span></li>`;
      }).join("");
      html += `<div class="chat-insight-rank-title">常一起出现的话术组合</div>
        <ul class="chat-insight-rank">${pairItems}</ul>`;
    }
    /* 可信度线索榜（credibility_patterns）：卡片载荷里本来就有这份聚合，旧参谋页
       漏渲染了；单页化顺手补上——同样只列计数与占比，不给任何站点/消息判真假。 */
    const creds = Array.isArray(p.credibility_patterns) ? p.credibility_patterns : [];
    if (creds.length) {
      const credItems = creds.map((cp) => {
        cp = cp || {};
        const lab = (cp.by_label && typeof cp.by_label === "object") ? cp.by_label : {};
        const bits = Object.keys(lab).map((k) => `${escapeHtml(k)} ×${Number(lab[k]) || 0}`);
        return `<li><span class="chat-insight-rank-label">${escapeHtml(cp.label || cp.key || "（未命名类目）")}</span>
          <span class="chat-insight-rank-num">${Number(cp.dossiers) || 0} 条 · 占 ${fmtPct(cp.share)}${bits.length ? ` · ${bits.join(" · ")}` : ""}</span></li>`;
      }).join("");
      html += `<div class="chat-insight-rank-title">可信度线索榜（按命中档案数）</div>
        <ul class="chat-insight-rank">${credItems}</ul>`;
    }
    return html;
  }

  /* 4.2 信源画像：域名 / 档位 / 条目计数 / 增信是否达标 */
  function renderInsightSources(profiles) {
    if (!Array.isArray(profiles) || !profiles.length) return insightsEmpty();
    const rows = profiles.map((r) => {
      r = r || {};
      const badge = r.qualified
        ? '<span class="badge green">个人增信生效</span>'
        : '<span class="badge gray">未达标</span>';
      return `<tr><td>${escapeHtml(r.domain || "—")}</td>
        <td>${escapeHtml(r.tier || "—")}</td>
        <td>${Number(r.total_entries) || 0}</td>
        <td>${Number(r.refute_entries) || 0}</td>
        <td>${Number(r.refuted_dossiers) || 0}</td>
        <td>${when(r.last_seen)}</td>
        <td>${badge}</td></tr>`;
    }).join("");
    return `<table class="chat-insight-table">
        <thead><tr><th>域名</th><th>档位</th><th>条目数</th><th>反驳条目</th><th>证伪档案数</th><th>最近出现</th><th>增信</th></tr></thead>
        <tbody>${rows}</tbody></table>
      <p class="chat-insight-note muted">画像来自你自己的档案史，仅在历史证伪≥阈值时参与抬分；只述计数，不给任何站点打真假分。</p>`;
  }

  /* 4.3 本周简报：周期 + 新档案/结论 + 运行成本 + 高频话术 + 反驳最多的信源 */
  function renderInsightWeekly(d) {
    if (!d || typeof d !== "object") return insightsEmpty();
    const period = d.period || {};
    const doss = d.dossiers || {};
    const concl = d.conclusions || {};
    const runs = d.runs || {};
    const tool = d.tool_runs || {};
    const heat = d.heat || {};
    if (!Number(period.days) && !Number(doss.new_count) && !Number(heat.snapshot_count)) {
      return insightsEmpty();
    }
    const cmap = (concl.by_label && typeof concl.by_label === "object") ? concl.by_label : {};
    const conBits = Object.keys(cmap).map((k) => `${escapeHtml(k)} ×${Number(cmap[k]) || 0}`);
    let html = `<p class="chat-insight-count">${when(period.start)} ~ ${when(period.end)}（${Number(period.days) || 0} 天）</p>
      <ul class="chat-insight-facts">
        <li>新档案 <b>${Number(doss.new_count) || 0}</b> 篇；新结论：${conBits.length ? conBits.join(" · ") : "暂无数据"}</li>
        <li>分析运行 <b>${Number(runs.count) || 0}</b> 次 · 成本 ${Number(runs.requests) || 0} 次调用 / ${Number(runs.tokens) || 0} tokens；工具调用 <b>${Number(tool.count) || 0}</b> 次 · 成本 ${Number(tool.requests) || 0} / ${Number(tool.tokens) || 0} tokens</li>
        <li>热度快照 <b>${Number(heat.snapshot_count) || 0}</b> 次</li>
      </ul>
      ${d.as_of ? `<p class="chat-insight-sub muted">数据截至 ${when(d.as_of)}</p>` : ""}`;
    const top = (d.patterns && Array.isArray(d.patterns.top)) ? d.patterns.top : [];
    if (top.length) {
      html += `<div class="chat-insight-rank-title">本周高频话术</div><ul class="chat-insight-rank">${top.map((tp) => {
        tp = tp || {};
        return `<li><span class="chat-insight-rank-label">${escapeHtml(tp.label || tp.key || "（未命名类目）")}</span>
          <span class="chat-insight-rank-num">${Number(tp.dossiers) || 0} 条</span></li>`;
      }).join("")}</ul>`;
    }
    const refutes = (d.sources && Array.isArray(d.sources.top_refute)) ? d.sources.top_refute : [];
    if (refutes.length) {
      html += `<div class="chat-insight-rank-title">本周反驳条目最多的信源</div><ul class="chat-insight-rank">${refutes.map((tr) => {
        tr = tr || {};
        return `<li>${escapeHtml(tr.domain || "—")}
          <span class="chat-insight-rank-num">${Number(tr.refute_entries) || 0} 条反驳</span></li>`;
      }).join("")}</ul>`;
    }
    return html;
  }

  function renderInsights(card) {
    card = card || {};
    const kind = String(card.kind || "");
    const meta = INSIGHTS_META[kind] || { icon: "🧩", title: "我的画像" };
    let body;
    if (kind === "patterns") body = renderInsightPatterns(card.patterns);
    else if (kind === "sources") body = renderInsightSources(card.profiles);
    else if (kind === "weekly") body = renderInsightWeekly(card.digest);
    /* 未知 kind 不猜：如实「暂无数据」，避免把聚合结构错当另一类画像展示 */
    else body = insightsEmpty();
    const kindBadge = INSIGHTS_META[kind] ? `<span class="badge gray">只读聚合 · 零 LLM</span>` : "";
    const asOf = card.as_of || (card.digest && card.digest.as_of) || "";
    return `
      <div class="chat-card chat-card-insights">
        <div class="chat-card-title">${meta.icon} ${escapeHtml(meta.title)}${kindBadge}${degradedBadge(card)}</div>
        ${body}
        ${asOfLine(asOf)}
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

  /* ---------- 5. 证据研究卡（collect_evidence：取证三技能，只读取证） ----------
     由后端 collect_evidence 工具产出：card = {card_type:"evidence", status, task_status,
     as_of, degraded, domain, sources{verified,unverified}, claims, evidence, counts,
     gaps, errors}（形状见 .tmp/skills-contract-wave2.md §2.2，主代理集成时落地）。
     口径诚实铁律同全文件：引文逐字来自后端"实际读取过的正文"（前端不推断、不补造、
     不改写），关系用封闭枚举徽章；不展示任何真假判定与数值评分；六种执行状态如实
     映射中文，"已取消/预算用尽"必须带"保留已取得证据"，不伪装成完整成功。 */
  const EVIDENCE_STATUS = {
    completed: "已完成",
    partial: "部分完成（有缺口/错误）",
    needs_clarification: "待补充信息",
    failed: "执行失败",
    cancelled: "已取消（保留已取得证据）",
    budget_exhausted: "预算用尽（保留已取得证据）",
  };
  const EVIDENCE_TASK = {
    completed: "已完成",
    no_factual_claim: "没有可核查事实主张",
    needs_clarification: "待补充信息",
  };
  const RELATION_META = {
    supports: { cls: "green", text: "支持" },
    refutes: { cls: "red", text: "反驳" },
    partial: { cls: "amber", text: "部分" },
    background: { cls: "gray", text: "背景" },
    unclear: { cls: "gray", text: "关系不明" },
  };
  const VERIFY_META = {
    catalog_anchor: "目录锚点",
    verified_site_link: "已核验站点链接",
    unverified_candidate: "未核验候选",
  };

  function renderEvidence(c) {
    c = c || {};
    const counts = (c.counts && typeof c.counts === "object") ? c.counts : {};
    const domain = (c.domain && typeof c.domain === "object") ? c.domain : {};
    const sources = (c.sources && typeof c.sources === "object") ? c.sources : {};
    const verified = Array.isArray(sources.verified) ? sources.verified : [];
    const unverified = Array.isArray(sources.unverified) ? sources.unverified : [];
    const claims = Array.isArray(c.claims) ? c.claims : [];
    const evidence = Array.isArray(c.evidence) ? c.evidence : [];
    const gaps = Array.isArray(c.gaps) ? c.gaps : [];
    const errors = Array.isArray(c.errors) ? c.errors : [];
    const labels = Array.isArray(domain.labels) ? domain.labels : [];

    /* 信源分两级呈现：已核验（目录锚点/已核验站点链接）与未核验候选分开，
       后者明确标注"不进入证据检索"——来源不自我认证（总览 §5.4）。 */
    const srcHtml = (list, title, note) => {
      if (!list.length) return "";
      const rows = list.map((s) => {
        s = s || {};
        const ver = VERIFY_META[s.verification] || "";
        return `<li><span class="chat-ev-stance">${escapeHtml(s.tier || "")}</span>` +
          `${escapeHtml(s.name || s.domain || "（未命名信源）")}` +
          `<div class="chat-ev-url">${escapeHtml(s.domain || "")}${ver ? " · " + escapeHtml(ver) : ""}</div></li>`;
      }).join("");
      return `<div class="chat-ev-src-title">${escapeHtml(title)}</div>` +
        `<ul class="chat-ev-list">${rows}</ul>` +
        (note ? `<p class="chat-insight-note muted">${escapeHtml(note)}</p>` : "");
    };

    /* 主张 → 证据分组：证据按 claim_ids 归组；挂不上已列主张的（理论不出现，
       真出现时）单独归组展示，不静默丢弃——丢证据比显示异常更糟。 */
    const matchedIds = {};
    const claimItems = claims.map((cl) => {
      cl = cl || {};
      matchedIds[cl.claim_id] = true;
      const rows = evidence.filter((ev) => Array.isArray((ev || {}).claim_ids)
        && ev.claim_ids.indexOf(cl.claim_id) >= 0);
      const evHtml = rows.length ? `<ul class="chat-ev-list">${rows.map((ev) => {
        ev = ev || {};
        const rel = RELATION_META[ev.relation] || { cls: "gray", text: "关系不明" };
        const url = safeUrl(ev.url);
        const meta = [];
        if (ev.evidence_id) meta.push(`<span class="chat-ev-stance">${escapeHtml(ev.evidence_id)}</span>`);
        meta.push(`<span class="badge ${rel.cls}">${rel.text}</span>`);
        if (ev.source_id) meta.push(`<span class="chat-ev-stance">${escapeHtml(ev.source_id)}</span>`);
        const title = escapeHtml(ev.title || ev.url || "（无标题）");
        const link = url
          ? `<a href="${url}" target="_blank" rel="noopener nofollow">${title}</a>`
          : title;
        return `<li>${meta.join("")}${link}` +
          (ev.quote ? `<blockquote class="chat-ev-quote">${escapeHtml(ev.quote)}</blockquote>` : "") +
          (ev.fetched_at ? `<div class="chat-ev-url">读取于 ${escapeHtml(ev.fetched_at)}</div>` : "") +
          "</li>";
      }).join("")}</ul>` : '<p class="chat-insight-sub muted">该主张尚未找到可定位证据。</p>';
      return `<li class="chat-ev-claim"><b>${escapeHtml(cl.claim_id || "")}</b>` +
        `<span>${escapeHtml(cl.text || "")}</span>${evHtml}</li>`;
    }).join("");
    const orphans = evidence.filter((ev) => !Array.isArray((ev || {}).claim_ids)
      || !ev.claim_ids.some((id) => matchedIds[id]));
    if (orphans.length) {
      claimItems += `<li class="chat-ev-claim"><b>（未关联）</b>` +
        `<span>以下证据未挂到已列出的主张：</span><ul class="chat-ev-list">` +
        orphans.map((ev) => `<li>${escapeHtml((ev || {}).evidence_id || "")} ` +
          `${escapeHtml((ev || {}).title || (ev || {}).url || "")}</li>`).join("") +
        "</ul></li>";
    }

    const issueHtml = (gaps.length || errors.length) ? `
        <details class="chat-evidence-fold">
          <summary>缺口 ${gaps.length} 条 · 错误 ${errors.length} 条</summary>
          ${gaps.length ? `<ul class="chat-ev-list">${gaps.map((g) => `<li>${escapeHtml(String(g))}</li>`).join("")}</ul>` : ""}
          ${errors.length ? `<ul class="chat-ev-list">${errors.map((e) => {
            e = e || {};
            return `<li>${escapeHtml(e.stage || "")} · ${escapeHtml(e.category || "")}：${escapeHtml(e.detail || "")}</li>`;
          }).join("")}</ul>` : ""}
        </details>` : "";

    const statusText = EVIDENCE_STATUS[c.status] || escapeHtml(String(c.status || "未知"));
    const taskText = EVIDENCE_TASK[c.task_status] || "";
    return `
      <div class="chat-card chat-card-evresearch">
        <div class="chat-card-title">🔎 证据研究<span class="badge gray">只读取证 · 不判真假</span>${degradedBadge(c)}</div>
        <div class="chat-badges">
          <span class="badge gray">${escapeHtml(statusText)}</span>
          ${taskText ? `<span class="badge gray">${escapeHtml(taskText)}</span>` : ""}
          ${labels.length ? `<span class="badge gray">领域：${escapeHtml(labels.join("、"))}</span>` : ""}
          <span class="badge gray">主张 ${Number(counts.claims) || 0} · 已核验信源 ${Number(counts.verified_sources) || 0} · 证据 ${Number(counts.evidence) || 0} 条</span>
        </div>
        ${srcHtml(verified, "已核验信源", "两类来路：信源目录锚点，或已核验站点页面直接给出的链接。")}
        ${srcHtml(unverified, "未核验候选", "陌生域名不自我认证：以下候选不进入证据检索。")}
        ${claimItems ? `<ul class="chat-ev-claims">${claimItems}</ul>`
          : '<p class="chat-insight-sub muted">本轮没有拆出可核查主张。</p>'}
        ${issueHtml}
        <p class="chat-insight-note muted">已执行 ${Number(counts.searches) || 0} 次搜索 · ${Number(counts.pages) || 0} 次读页 · ${Number(counts.llm_calls) || 0} 次模型调用。引文逐字来自实际读取的正文；只述证据，不输出真假结论。</p>
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ---------- 6. 材料批次汇总卡（batch_summary：批量核查的计数 / 分布 / 同源组 / 条目） ----------
     由后端 /api/batch/{id} 的 summary 与批次 SSE 的 card 事件产出：
     card = {card_type:"batch_summary", batch_id, title, source, as_of, total, ok,
     failed, blocked, not_run, by_label, groups, items, sources_top, degraded}。
     口径诚实铁律同全文件：计数旁一律带 as_of；没有任何字段的块整块不显示，
     不编造、不补造；代表文本超长截断并标注；条目状态用封闭枚举映射，未知原样。 */
  const BATCH_ITEM_STATUS = {
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

  const BATCH_SOURCE_LABEL = { text: "文本", file: "文件", urls: "链接" };

  /* 档案链接：只接受正整数 id（同 safeUrl 的"白名单才放行"思路），
     产出站内相对路径 /api/dossiers/{id}，不产生任何外链 */
  function batchDossierHref(id) {
    const n = Number(id);
    if (!Number.isInteger(n) || n <= 0) return "";
    return `/api/dossiers/${n}`;
  }

  function renderBatchSummary(c) {
    c = c || {};
    const total = Number(c.total) || 0;
    const ok = Number(c.ok) || 0;
    const failed = Number(c.failed) || 0;
    const blocked = Number(c.blocked) || 0;
    const notRun = Number(c.not_run) || 0;
    const byLabel = (c.by_label && typeof c.by_label === "object") ? c.by_label : {};
    const groups = Array.isArray(c.groups) ? c.groups : [];
    const items = Array.isArray(c.items) ? c.items : [];
    const sourcesTop = Array.isArray(c.sources_top) ? c.sources_top : [];
    const degraded = Array.isArray(c.degraded) ? c.degraded : [];
    const source = BATCH_SOURCE_LABEL[String(c.source || "")] || "";

    /* 计数徽章：四项恒显示；not_run > 0 时才标「未跑」（--warn 色，amber 徽章同色系） */
    const countBadges = `
        <span class="badge gray">共 ${total} 条</span>
        <span class="badge green">完成 ${ok}</span>
        <span class="badge red">失败 ${failed}</span>
        <span class="badge amber">已拦截 ${blocked}</span>
        ${notRun > 0 ? `<span class="badge amber">未跑 ${notRun}</span>` : ""}`;

    /* 来源与批次号一行（缺 source 时只说批次号，不猜来源） */
    const metaBits = [];
    if (source) metaBits.push(`来源：${escapeHtml(source)}`);
    if (c.batch_id !== undefined && c.batch_id !== null) {
      metaBits.push(`批次 #${escapeHtml(String(c.batch_id))}`);
    }
    const metaLine = metaBits.length
      ? `<p class="chat-batch-summary-meta muted">${metaBits.join(" · ")}</p>` : "";

    /* 标签分布一行（缺 by_label 时不编造分布） */
    const labelBits = Object.keys(byLabel).map((k) =>
      `${escapeHtml(k)} ×${Number(byLabel[k]) || 0}`);
    const labelLine = labelBits.length
      ? `<p class="chat-batch-labels muted">标签分布：${labelBits.join(" · ")}</p>` : "";

    /* 疑似同源组：代表文本截 60 字，截断要标注，不让用户以为看到的是全文 */
    const groupHtml = groups.length ? `
        <div class="chat-batch-group-title">疑似同源（${groups.length} 组）</div>
        <ul class="chat-batch-groups">${groups.map((g) => {
          g = g || {};
          const rep = String(g.representative || "");
          const cut = rep.length > 60;
          const text = escapeHtml(cut ? rep.slice(0, 60) : rep) + (cut ? "…（已截断）" : "");
          const positions = Array.isArray(g.positions) ? g.positions : [];
          const size = Number(g.size) || positions.length;
          /* 组内标签分布（后端 group_similar 后的聚合；没有就不显示） */
          const labels = (g.labels && typeof g.labels === "object") ? g.labels : {};
          const labelBits = Object.keys(labels).map((k) =>
            `${escapeHtml(k)} ×${Number(labels[k]) || 0}`);
          return `<li><span class="badge gray">${escapeHtml(String(g.group_id || ""))} · ${size} 条</span>`
            + `<span class="chat-batch-group-rep">${text || "（无代表文本）"}</span>`
            + (labelBits.length
              ? `<span class="chat-batch-group-labels muted">${labelBits.join(" · ")}</span>` : "")
            + `</li>`;
        }).join("")}</ul>` : "";

    /* 条目表：position + 状态徽章 + label（+ url）+ 风险分 + 档案链接 */
    const itemHtml = items.length ? `
        <table class="chat-batch-items-table">
          <thead><tr><th>#</th><th>状态</th><th>标签</th><th>风险分</th><th>档案</th></tr></thead>
          <tbody>${items.map((it) => {
            it = it || {};
            const meta = BATCH_ITEM_STATUS[String(it.status)] || null;
            const statusText = meta ? meta.text : escapeHtml(String(it.status || "未知"));
            const statusCls = meta ? meta.cls : "gray";
            const risk = Number(it.risk_score);
            const riskTxt = (it.risk_score !== null && it.risk_score !== undefined
              && Number.isFinite(risk)) ? String(risk) : "—";
            /* url 是外部内容：只放行 http(s)（safeUrl 同款正则），不给伪协议链接 */
            const url = safeUrl(it.url);
            const urlHtml = url
              ? `<div class="chat-batch-item-url"><a href="${url}" target="_blank" rel="noopener nofollow">${url}</a></div>`
              : "";
            const href = batchDossierHref(it.dossier_id);
            const dossier = href
              ? `<a href="${href}">档案 #${escapeHtml(String(it.dossier_id))}</a>`
              : "—";
            return `<tr>
                <td>${Number(it.position) || 0}</td>
                <td><span class="badge ${statusCls}">${statusText}</span></td>
                <td>${escapeHtml(it.label || "（无标签）")}${urlHtml}</td>
                <td>${escapeHtml(riskTxt)}</td>
                <td>${dossier}</td></tr>`;
          }).join("")}</tbody>
        </table>` : "";

    /* 信源域名列表：只列域名与反驳条目数，不给任何站点判真假 */
    const sourceHtml = sourcesTop.length ? `
        <div class="chat-batch-source-title">出现最多的信源</div>
        <ul class="chat-batch-sources">${sourcesTop.map((s) => {
          s = s || {};
          return `<li><span class="chat-batch-source-domain">${escapeHtml(s.domain || "—")}</span>`
            + `<span class="chat-batch-source-num">反驳条目 ${Number(s.refute_entries) || 0}</span></li>`;
        }).join("")}</ul>` : "";

    /* 降级 / 异常记录逐条如实显示（后端原文，如"第3条：超时"） */
    const degradedHtml = degraded.length ? `
        <div class="chat-batch-degraded">
          <div class="chat-batch-degraded-title">降级 / 异常记录（${degraded.length} 条）</div>
          <ul>${degraded.map((d) => `<li>${escapeHtml(String(d))}</li>`).join("")}</ul>
        </div>` : "";

    return `
      <div class="chat-card chat-card-batch-summary">
        <div class="chat-card-title">📦 材料批次汇总<span class="badge gray">批量核查</span>${degradedBadge(c)}</div>
        <div class="chat-card-label">${escapeHtml(c.title || `批次 #${Number(c.batch_id) || 0}`)}</div>
        <div class="chat-badges">${countBadges}</div>
        ${metaLine}
        ${labelLine}
        ${groupHtml}
        ${itemHtml}
        ${sourceHtml}
        ${degradedHtml}
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ---------- 7. 领域监测变化卡（monitor_change：订阅跑检一轮的 URL 差集 + E 分变化） ----------
      由后端 tools_monitor.change_card 产出（monitor_api 详情接口的 card 段）：
      card = {card_type:"monitor_change", subscription_id, topic, run_id, status,
      results_count, new_count, new_urls, removed_urls, e_score, prev_e_score,
      e_delta, as_of, error}。
      口径诚实铁律同全文件：监测结果 URL 是外部内容，只做纯文本展示（截断 80 字并
      标注），不生成任何可点链接；E 分是确定性公式输出（代理/估计口径，照实注明）；
      没有任何字段的块整块不显示，不编造、不补造。 */
  const MONITOR_STATUS = {
    ok: { text: "已完成", cls: "green" },
    partial: { text: "部分完成", cls: "amber" },
    blocked: { text: "已拦截", cls: "amber" },
    error: { text: "失败", cls: "red" },
    running: { text: "进行中", cls: "gray" },
  };

  /* URL 只做纯文本：截断 80 字并标注，不生成 <a>（监测结果是外部内容，保持只读） */
  function monitorUrlText(url) {
    const s = String(url || "").trim();
    if (!s) return "";
    const cut = s.length > 80;
    return escapeHtml(cut ? s.slice(0, 80) : s) + (cut ? "…（已截断）" : "");
  }

  /* 一个 URL 小节：字段缺失（undefined/null）整块不显示；有空数组时如实说"没有" */
  function monitorUrlSection(urls, title, emptyText) {
    if (urls === undefined || urls === null) return "";
    const list = Array.isArray(urls) ? urls.filter((u) => String(u || "").trim()) : [];
    const head = `<div class="chat-monitor-urls-title">${escapeHtml(title)} ${list.length} 条 · 纯文本展示，不做可点链接</div>`;
    if (!list.length) {
      return head + `<p class="chat-insight-sub muted">${escapeHtml(emptyText)}</p>`;
    }
    return head
      + `<ul class="chat-monitor-urls">${list.map((u) => `<li>${monitorUrlText(u)}</li>`).join("")}</ul>`;
  }

  /* 数值字段：null/undefined/空串/非有限数一律当"没有"，不拿 0 冒充 */
  function monitorNum(v) {
    if (v === null || v === undefined || v === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function renderMonitorChange(c) {
    c = c || {};
    const smeta = MONITOR_STATUS[String(c.status || "")] || null;
    const statusText = smeta ? smeta.text : escapeHtml(String(c.status || "未知"));
    const statusCls = smeta ? smeta.cls : "gray";
    const results = Number(c.results_count) || 0;
    const fresh = Number(c.new_count) || 0;
    const eScore = monitorNum(c.e_score);
    const prevE = monitorNum(c.prev_e_score);
    const eDelta = monitorNum(c.e_delta);
    const hasE = eScore !== null || prevE !== null || eDelta !== null;
    const runId = monitorNum(c.run_id);

    /* 错误 / 授权说明：error 非空时如实显示（blocked 轮次它就是授权说明） */
    const errorTxt = String(c.error || "").trim();
    const errorTitle = String(c.status || "") === "blocked" ? "授权说明" : "错误";
    const errorHtml = errorTxt
      ? `<p class="chat-monitor-notice"><b>${errorTitle}：</b>${escapeHtml(errorTxt)}</p>`
      : "";

    return `
      <div class="chat-card chat-card-monitor-change">
        <div class="chat-card-title">📡 监测变化<span class="badge gray">订阅跑检</span></div>
        <div class="chat-card-label">${escapeHtml(c.topic || "（无话题）")}</div>
        <div class="chat-badges">
          <span class="badge ${statusCls}">${statusText}</span>
          ${runId !== null && runId > 0 ? `<span class="badge gray">第 ${runId} 轮</span>` : ""}
        </div>
        <p class="chat-insight-count">本轮检到 ${results} 条 · 新增 ${fresh} 条</p>
        ${monitorUrlSection(c.new_urls, "新增 URL", "本轮没有新增 URL。")}
        ${monitorUrlSection(c.removed_urls, "消失 URL", "本轮没有消失的 URL。")}
        ${hasE ? `
        <div class="chat-dims">
          ${eScore !== null ? `<div class="dim-row"><span class="k">本轮关注度 E</span><span class="v">${eScore.toFixed(2)}</span></div>` : ""}
          ${prevE !== null ? `<div class="dim-row"><span class="k">上轮关注度 E</span><span class="v">${prevE.toFixed(2)}</span></div>` : ""}
          ${eDelta !== null ? `<div class="dim-row"><span class="k">E 分变化</span><span class="v">${eDelta > 0 ? "+" : ""}${eDelta.toFixed(2)}</span></div>` : ""}
        </div>
        <p class="chat-insight-note muted">关注度 E 为确定性公式输出，代理估计值：只述首末两点之差，不预测趋势。</p>` : ""}
        ${errorHtml}
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ============================================================
     8~10. 信息收集 / 权威核验 / 复合卡（契约 .tmp/info-skills-contract.md §5.1/§6.2）
     三张卡的共同口径：
       - 只述证据，不替用户下结论；不展示任何数值评分/置信度；
       - 引文逐字（只做 HTML 转义，不截断、不改写）；
       - 未核验来源与已核验信源分块，前者只作线索；
       - 六种执行状态如实映射中文（取消/预算用尽必须带"保留已取得资料"）；
       - 空结果是合法输出：没有的块整块不显示，不硬凑"0 条"。
     ============================================================ */

  /* §2 统一运行外壳六态：与证据研究卡共用同一份中文映射（同一状态只能有一种说法）。 */
  const RUN_STATUS = EVIDENCE_STATUS;

  /* 统一状态徽章：status / task_status / 增量补查基线（缺省字段不显示）。 */
  function runBadges(c) {
    c = c || {};
    const statusText = RUN_STATUS[String(c.status || "")] || String(c.status || "未知");
    /* task_status 只在"没主张/待补充"这类需要用户知道的结论上单独成徽章；
       completed 与 status 的"已完成"重复，不重复展示。 */
    const taskKey = String(c.task_status || "");
    const taskText = (taskKey === "no_factual_claim" || taskKey === "needs_clarification")
      ? (EVIDENCE_TASK[taskKey] || "") : "";
    const incremental = c.incremental
      ? `<span class="badge amber">增量补查 · 基线 ${escapeHtml(String(c.base_as_of || "未知"))}</span>`
      : "";
    return `<span class="badge gray">${escapeHtml(statusText)}</span>`
      + (taskText ? `<span class="badge gray">${escapeHtml(taskText)}</span>` : "")
      + incremental;
  }

  /* 引文逐字展示：只转义，不截断/改写（后端保证 quote 锚定已读正文）。 */
  function verbatimQuote(quote) {
    const text = String(quote || "");
    if (!text) return "";
    return `<blockquote class="chat-ev-quote chat-quote-verbatim">${escapeHtml(text)}</blockquote>`;
  }

  /* id 串（证据 id / 子问题 id）：机器串，逐字转义后顿号连排。 */
  function idLine(ids, label) {
    const list = Array.isArray(ids) ? ids.filter((x) => String(x || "").trim()) : [];
    if (!list.length) return "";
    return `<div class="chat-ev-url">${escapeHtml(label)}：${list.map((x) => escapeHtml(String(x))).join("、")}</div>`;
  }

  /* 时间戳一行：发布/读取分开写，缺哪个就少哪个。 */
  function timeLine(published, fetched) {
    const bits = [];
    if (published) bits.push(`发布 ${escapeHtml(String(published))}`);
    if (fetched) bits.push(`读取 ${escapeHtml(String(fetched))}`);
    return bits.length ? `<div class="chat-ev-url">${bits.join(" · ")}</div>` : "";
  }

  /* 信源一行元信息：名称 / 域名 / 核验方式（都不存在就整行不显示）。 */
  function sourceMetaLine(name, domain, verify) {
    const bits = [];
    if (name) bits.push(escapeHtml(String(name)));
    if (domain) bits.push(escapeHtml(String(domain)));
    if (verify) bits.push(escapeHtml(String(verify)));
    return bits.length ? `<div class="chat-ev-url">${bits.join(" · ")}</div>` : "";
  }

  /* 核验方式（SourcePolicy.verification 的封闭枚举，§4）。 */
  const AUTHORITY_VERIFY = {
    registry_anchor: "目录锚点",
    verified_site_link: "已核验站点链接",
    session_verified: "会话内动态核验",
    unverified: "未核验",
  };

  /* 权威类别（authority/authorities.json 的 authority_class 封闭枚举，§8）。 */
  const AUTHORITY_CLASS = {
    official: "官方权威",
    international_org: "国际组织",
    academic: "学术机构",
    standards: "标准机构",
    factcheck: "事实核查",
    media: "媒体",
    unverified: "未核验来源",
  };

  function sourceList(list) {
    if (!Array.isArray(list) || !list.length) return "";
    return `<ul class="chat-ev-list">${list.map((s) => {
      s = s || {};
      const tier = s.tier ? `<span class="chat-ev-stance">${escapeHtml(String(s.tier))}</span>` : "";
      const verify = AUTHORITY_VERIFY[String(s.verification || "")] || "";
      return `<li>${tier}${escapeHtml(String(s.name || s.domain || "（未命名信源）"))}`
        + sourceMetaLine("", s.domain, verify) + "</li>";
    }).join("")}</ul>`;
  }

  /* 信源两级：已核验 / 未核验候选永远分块呈现（来源不自我认证）。 */
  function sourceGroups(sources) {
    const box = (sources && typeof sources === "object") ? sources : {};
    const verified = Array.isArray(box.verified) ? box.verified : [];
    const unverified = Array.isArray(box.unverified) ? box.unverified : [];
    if (!verified.length && !unverified.length) return "";
    let html = "";
    if (verified.length) {
      html += `<div class="chat-src-title">已核验信源（${verified.length}）</div>${sourceList(verified)}`;
    }
    if (unverified.length) {
      html += `<div class="chat-src-title">未核验来源（${unverified.length}）</div>`
        + '<p class="chat-insight-note muted">未核验来源只作线索，不进入关键发现与权威判断。</p>'
        + sourceList(unverified);
    }
    return html;
  }

  /* 缺口 / 错误折叠：没有就整块不显示（空结果是合法输出）。 */
  function issuesFold(gaps, errors) {
    const gl = Array.isArray(gaps) ? gaps : [];
    const el = Array.isArray(errors) ? errors : [];
    if (!gl.length && !el.length) return "";
    return `
        <details class="chat-evidence-fold">
          <summary>缺口 ${gl.length} 条 · 错误 ${el.length} 条</summary>
          ${gl.length ? `<ul class="chat-ev-list">${gl.map((g) => `<li>${escapeHtml(String(g))}</li>`).join("")}</ul>` : ""}
          ${el.length ? `<ul class="chat-ev-list">${el.map((e) => {
            e = e || {};
            return `<li>${escapeHtml(String(e.stage || ""))} · ${escapeHtml(String(e.category || ""))}：${escapeHtml(String(e.detail || ""))}</li>`;
          }).join("")}</ul>` : ""}
        </details>`;
  }

  /* 运行审计：只写"跑了多少次"，不是评价分；三个键都没有就整行不显示。
     研究卡 / 权威卡 / 复合卡共用（计数键同属 §2 audit 口径）。 */
  function auditLine(counts) {
    const box = (counts && typeof counts === "object") ? counts : {};
    const bits = [];
    if (box.searches !== undefined) bits.push(`搜索 ${Number(box.searches) || 0} 次`);
    if (box.pages !== undefined) bits.push(`读页 ${Number(box.pages) || 0} 次`);
    if (box.llm_calls !== undefined) bits.push(`模型调用 ${Number(box.llm_calls) || 0} 次`);
    return bits.length
      ? `<p class="chat-insight-note muted chat-run-audit">已执行 ${bits.join(" · ")}。</p>` : "";
  }

  /* 执行模式 / 步数 / 停止原因：后端 execution 三件套，缺省不显示。 */
  function executionLine(exec) {
    const e = (exec && typeof exec === "object") ? exec : null;
    if (!e) return "";
    const bits = [];
    if (e.mode) {
      const mode = String(e.mode) === "agent"
        ? "受限 Agent 循环"
        : `固定流水线（${escapeHtml(String(e.mode))}）`;
      bits.push(`执行模式：${mode}`);
    }
    if (Number(e.steps)) bits.push(`步数：${Number(e.steps)}`);
    if (e.stop_reason) bits.push(`停止原因：${escapeHtml(String(e.stop_reason))}`);
    if (!bits.length) return "";
    return `<p class="chat-insight-note muted chat-run-exec">${bits.join(" · ")}</p>`;
  }

  /* 冲突块：研究卡与权威卡共用（summary 逐字转义，证据 id 另起一行）。 */
  function conflictsBlock(conflicts) {
    if (!Array.isArray(conflicts) || !conflicts.length) return "";
    return `
        <div class="chat-research-conflict-title">证据冲突（${conflicts.length}）</div>
        <ul class="chat-research-conflicts">${conflicts.map((x) => {
          x = x || {};
          return `<li><span>${escapeHtml(String(x.summary || ""))}</span>`
            + idLine(x.evidence_ids, "涉及证据") + "</li>";
        }).join("")}</ul>`;
  }

  /* ---------- 8. 资料收集卡（collect_research：信息收集技能） ----------
     由后端 collect_research 工具产出：card = {card_type:"research", status,
     task_status, as_of, degraded, topic, scope, sub_questions, key_findings,
     leads, evidence, sources{verified,unverified}, timeline, conflicts, analysis,
     gaps, errors, counts, execution, incremental, base_as_of, handoff_skipped}
     （形状见契约 §5.1）。关键发现必须带逐字 source_quote；没有引文的内容只能进
     leads（待核线索）——前端照后端给的层级展示，不把线索升格成发现。 */
  const SQ_STATUS = {
    covered: { text: "已覆盖", cls: "green" },
    partial: { text: "部分覆盖", cls: "amber" },
    uncovered: { text: "未覆盖", cls: "gray" },
  };
  const ROLE_TEXT = { fact: "事实", context: "背景", conflict: "冲突" };

  /* 分析块：label 由后端固定（"基于资料的整理/推断"），前端不自行改写。 */
  function analysisBlock(analysis) {
    if (!analysis || typeof analysis !== "object") return "";
    const text = String(analysis.text || "").trim();
    if (!text) return "";
    return `
        <div class="chat-analysis">
          <div class="chat-analysis-label">${escapeHtml(String(analysis.label || "基于资料的整理/推断"))}</div>
          <p class="chat-analysis-text">${escapeHtml(text)}</p>
          ${idLine(analysis.evidence_ids, "引用证据")}
        </div>`;
  }

  /* 资料收集卡正文（复合卡的 research 子卡复用同一份渲染，避免嵌套重复标题）。 */
  function researchBody(c) {
    c = c || {};
    const scope = (c.scope && typeof c.scope === "object") ? c.scope : {};
    const subQuestions = Array.isArray(c.sub_questions) ? c.sub_questions : [];
    const findings = Array.isArray(c.key_findings) ? c.key_findings : [];
    const leads = Array.isArray(c.leads) ? c.leads : [];
    const evidence = Array.isArray(c.evidence) ? c.evidence : [];
    const timeline = Array.isArray(c.timeline) ? c.timeline : [];
    const followups = Array.isArray(c.followups) ? c.followups : [];

    /* 范围：题目/时间窗/地域分开写；没有就不显示该行，不替用户猜范围。 */
    const scopeRows = [];
    if (scope.question) {
      scopeRows.push(`<div class="dim-row"><span class="k">研究问题</span><span class="v">${escapeHtml(String(scope.question))}</span></div>`);
    }
    if (scope.time_window) {
      scopeRows.push(`<div class="dim-row"><span class="k">时间范围</span><span class="v">${escapeHtml(String(scope.time_window))}</span></div>`);
    }
    if (scope.region) {
      scopeRows.push(`<div class="dim-row"><span class="k">地域</span><span class="v">${escapeHtml(String(scope.region))}</span></div>`);
    }
    const basis = Array.isArray(scope.basis) ? scope.basis : [];
    if (basis.length) {
      scopeRows.push(`<div class="chat-research-basis muted">范围依据：${basis.map((b) => escapeHtml(String(b))).join(" · ")}</div>`);
    }
    const scopeHtml = scopeRows.length ? `<div class="chat-research-scope">${scopeRows.join("")}</div>` : "";

    /* 子问题：covered/partial/uncovered 封闭枚举；未知状态原样转义展示。 */
    const sqHtml = subQuestions.length ? `
        <div class="chat-research-sq-title">子问题（${subQuestions.length}）</div>
        <ul class="chat-research-sqs">${subQuestions.map((q) => {
          q = q || {};
          const meta = SQ_STATUS[String(q.status || "")];
          const statusText = meta ? meta.text : String(q.status || "未标注");
          const statusCls = meta ? meta.cls : "gray";
          const evCount = (q.evidence_count === undefined || q.evidence_count === null)
            ? "" : `<span class="chat-ev-url">证据 ${Number(q.evidence_count) || 0} 条</span>`;
          return `<li class="chat-research-sq">
            <span class="chat-ev-stance">${escapeHtml(String(q.sq_id || ""))}</span>
            <span class="chat-research-sq-text">${escapeHtml(String(q.text || ""))}</span>
            <span class="badge ${statusCls}">${escapeHtml(statusText)}</span>${evCount}
          </li>`;
        }).join("")}</ul>` : "";

    /* 关键发现：statement + 逐字 source_quote + evidence_ids；checkable=false 明说。 */
    const findingHtml = findings.length ? `
        <div class="chat-research-find-title">关键发现（${findings.length}）</div>
        <ol class="chat-research-findings">${findings.map((f) => {
          f = f || {};
          const meta = [];
          if (f.finding_id) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(f.finding_id))}</span>`);
          if (f.support_level) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(f.support_level))}</span>`);
          if (f.checkable === false) meta.push('<span class="chat-ev-stance">不可核查</span>');
          return `<li class="chat-research-finding">
            ${meta.join("")}<span class="chat-research-finding-text">${escapeHtml(String(f.statement || ""))}</span>
            ${idLine(f.sq_ids, "对应子问题")}
            ${idLine(f.evidence_ids, "依据证据")}
            ${verbatimQuote(f.source_quote)}
          </li>`;
        }).join("")}</ol>` : "";

    /* 待核线索：没有逐字引文支撑的内容只能在这里（不得进关键发现）。 */
    const leadHtml = leads.length ? `
        <div class="chat-research-lead-title">待核线索（${leads.length}）</div>
        <ul class="chat-research-leads">${leads.map((l) => {
          l = l || {};
          return `<li class="chat-research-lead">
            <span class="chat-ev-stance">${escapeHtml(String(l.lead_id || ""))}</span>
            <span class="chat-research-lead-text">${escapeHtml(String(l.statement || ""))}</span>
            ${l.reason ? `<div class="chat-research-lead-reason muted">${escapeHtml(String(l.reason))}</div>` : ""}
            ${idLine(l.evidence_ids, "关联证据")}
          </li>`;
        }).join("")}</ul>` : "";

    /* 证据本：外部内容（title/url/quote），url 只放行 http(s)，引文逐字。 */
    const evidenceHtml = evidence.length ? `
        <div class="chat-research-ev-title">证据（${evidence.length}）</div>
        <ul class="chat-ev-list chat-research-evidence">${evidence.map((ev) => {
          ev = ev || {};
          const role = ROLE_TEXT[String(ev.role || "")] || "";
          const url = safeUrl(ev.url);
          const title = escapeHtml(String(ev.title || ev.url || "（无标题）"));
          const link = url
            ? `<a href="${url}" target="_blank" rel="noopener nofollow">${title}</a>`
            : title;
          const meta = [];
          if (ev.evidence_id) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(ev.evidence_id))}</span>`);
          if (role) meta.push(`<span class="chat-ev-stance">${escapeHtml(role)}</span>`);
          if (ev.tier) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(ev.tier))}</span>`);
          const verify = AUTHORITY_VERIFY[String(ev.verification || "")] || "";
          const limits = Array.isArray(ev.limitations) ? ev.limitations : [];
          const limitsHtml = limits.length
            ? `<ul class="chat-ev-list">${limits.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>`
            : "";
          return `<li class="chat-research-ev">${meta.join("")}${link}`
            + sourceMetaLine(ev.name, ev.domain, verify)
            + verbatimQuote(ev.quote)
            + timeLine(ev.published_at, ev.fetched_at)
            + limitsHtml + "</li>";
        }).join("")}</ul>` : "";

    const timelineHtml = timeline.length ? `
        <div class="chat-research-tl-title">时间线（${timeline.length}）</div>
        <ul class="chat-research-timeline">${timeline.map((t) => {
          t = t || {};
          return `<li><span class="chat-research-tl-date">${escapeHtml(String(t.date || "—"))}</span>
            <span class="chat-research-tl-event">${escapeHtml(String(t.event || ""))}</span>
            ${idLine(t.evidence_ids, "证据")}</li>`;
        }).join("")}</ul>` : "";

    const followupHtml = followups.length ? `
        <div class="chat-research-followup-title">可以继续追问</div>
        <ul class="chat-research-followups">${followups.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>` : "";

    /* 自动接力被跳过时如实说明原因（不是静默省略）。 */
    const handoffHtml = c.handoff_skipped
      ? `<p class="chat-research-handoff muted">未自动核验：${escapeHtml(String(c.handoff_skipped))}</p>` : "";

    return scopeHtml + sqHtml + findingHtml + leadHtml + evidenceHtml + timelineHtml
      + conflictsBlock(c.conflicts) + analysisBlock(c.analysis) + followupHtml
      + sourceGroups(c.sources) + issuesFold(c.gaps, c.errors) + auditLine(c.counts)
      + executionLine(c.execution) + handoffHtml;
  }

  function renderResearch(c) {
    c = c || {};
    return `
      <div class="chat-card chat-card-research">
        <div class="chat-card-title">📚 资料收集<span class="badge gray">只述证据 · 不判结论</span>${degradedBadge(c)}</div>
        <div class="chat-card-label">${escapeHtml(String(c.topic || "（未命名主题）"))}</div>
        <div class="chat-badges">${runBadges(c)}</div>
        ${researchBody(c)}
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ---------- 9. 权威核验卡（verify_authority：权威核验技能） ----------
     由后端 verify_authority 工具产出：card = {card_type:"authority", status,
     task_status, as_of, degraded, claims, evidence, sources{verified,unverified},
     conflicts, analysis, limitations, gaps, errors, counts, execution, incremental,
     base_as_of}（形状见契约 §6.2）。
     口径：balance 由代码计算，前端只用封闭枚举中文徽章；lean 单独标"证据倾向"
     （全部可靠证据的倾向，不等于权威判断）；conclusion 由后端模板逐字给出；
     引文逐字；不显示任何数值评分；未核验来源与已核验信源分开。 */
  const BALANCE_META = {
    supported: { text: "权威证据支持", cls: "green" },
    refuted: { text: "权威证据反驳", cls: "red" },
    contested: { text: "权威证据冲突", cls: "amber" },
    insufficient: { text: "权威证据不足", cls: "gray" },
    not_checkable: { text: "不可核查", cls: "gray" },
  };
  const LEAN_TEXT = { supports: "倾向支持", refutes: "倾向反驳", mixed: "混杂", none: "无" };
  const ORIGIN_TEXT = {
    user: "用户提出",
    auto_from_research: "研究卡自动接力",
    user_link: "用户提供链接",
  };

  /* balance 徽章只认封闭枚举；后端 balance_label 落在枚举里才采信，其余一律"未标注"。 */
  function balanceBadge(cl) {
    cl = cl || {};
    const meta = BALANCE_META[String(cl.balance || "")];
    if (meta) return `<span class="badge ${meta.cls}">${meta.text}</span>`;
    const label = String(cl.balance_label || "");
    const key = Object.keys(BALANCE_META).filter((k) => BALANCE_META[k].text === label)[0];
    if (key) return `<span class="badge ${BALANCE_META[key].cls}">${BALANCE_META[key].text}</span>`;
    return '<span class="badge gray">未标注</span>';
  }

  /* 证据倾向：与 balance 分开展示（倾向 ≠ 权威判断）。不可核查时明说"不适用"。 */
  function leanBadge(cl) {
    cl = cl || {};
    const notCheckable = String(cl.balance || "") === "not_checkable"
      || String(cl.claim_status || "") === "not_checkable";
    const text = notCheckable ? "不适用" : (LEAN_TEXT[String(cl.lean || "")] || "未标注");
    return `<span class="badge gray">证据倾向：${escapeHtml(text)}</span>`;
  }

  /* 单条权威证据：relation / authority_class / verification 三枚封闭枚举徽章 + 逐字引文。 */
  function renderAuthorityEvidence(ev) {
    ev = ev || {};
    const rel = RELATION_META[String(ev.relation || "")] || { cls: "gray", text: "关系不明" };
    const cls = AUTHORITY_CLASS[String(ev.authority_class || "")] || "";
    const verify = AUTHORITY_VERIFY[String(ev.verification || "")] || "";
    const url = safeUrl(ev.url);
    const title = escapeHtml(String(ev.title || ev.url || "（无标题）"));
    const link = url
      ? `<a href="${url}" target="_blank" rel="noopener nofollow">${title}</a>`
      : title;
    const meta = [];
    if (ev.evidence_id) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(ev.evidence_id))}</span>`);
    meta.push(`<span class="badge ${rel.cls}">${rel.text}</span>`);
    if (cls) meta.push(`<span class="chat-ev-stance">${escapeHtml(cls)}</span>`);
    if (verify) meta.push(`<span class="chat-ev-stance">${escapeHtml(verify)}</span>`);
    const basis = Array.isArray(ev.authority_basis) ? ev.authority_basis : [];
    const limits = Array.isArray(ev.limitations) ? ev.limitations : [];
    return `<li class="chat-auth-ev">${meta.join("")}${link}`
      + sourceMetaLine(ev.name, ev.domain, "")
      + verbatimQuote(ev.quote)
      + timeLine(ev.published_at, ev.fetched_at)
      + (basis.length ? `<div class="chat-auth-basis">权威依据：${basis.map((b) => escapeHtml(String(b))).join(" · ")}</div>` : "")
      + (limits.length ? `<ul class="chat-ev-list">${limits.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>` : "")
      + "</li>";
  }

  /* 权威核验卡正文（复合卡的 authority 子卡复用同一份渲染）。 */
  function authorityBody(c) {
    c = c || {};
    const claims = Array.isArray(c.claims) ? c.claims : [];
    const evidence = Array.isArray(c.evidence) ? c.evidence : [];
    const analysis = Array.isArray(c.analysis) ? c.analysis : [];
    const limitations = Array.isArray(c.limitations) ? c.limitations : [];

    /* 主张 → 证据分组：证据按 claim_ids 归组；挂不上的单独列，不静默丢弃。 */
    const matched = {};
    const claimItems = claims.map((cl) => {
      cl = cl || {};
      matched[cl.claim_id] = true;
      const rows = evidence.filter((ev) => Array.isArray((ev || {}).claim_ids)
        && ev.claim_ids.indexOf(cl.claim_id) >= 0);
      const origin = ORIGIN_TEXT[String(cl.origin || "")] || "";
      const basis = Array.isArray(cl.authority_basis) ? cl.authority_basis : [];
      const claimLimits = Array.isArray(cl.limitations) ? cl.limitations : [];
      const evHtml = rows.length
        ? `<ul class="chat-ev-list">${rows.map(renderAuthorityEvidence).join("")}</ul>`
        : '<p class="chat-insight-sub muted">该主张尚未找到可定位证据。</p>';
      return `<li class="chat-auth-claim">
        <div class="chat-auth-claim-head"><b>${escapeHtml(String(cl.claim_id || ""))}</b><span class="chat-auth-claim-text">${escapeHtml(String(cl.text || ""))}</span></div>
        <div class="chat-badges">${balanceBadge(cl)}${leanBadge(cl)}${origin ? `<span class="badge gray">${escapeHtml(origin)}</span>` : ""}</div>
        ${cl.conclusion ? `<p class="chat-auth-conclusion">${escapeHtml(String(cl.conclusion))}</p>` : ""}
        ${idLine(cl.supporting_ids, "支持证据")}
        ${idLine(cl.refuting_ids, "反驳证据")}
        ${idLine(cl.context_ids, "背景证据")}
        ${basis.length ? `<div class="chat-auth-basis">权威依据：${basis.map((b) => escapeHtml(String(b))).join(" · ")}</div>` : ""}
        ${claimLimits.length ? `<ul class="chat-ev-list">${claimLimits.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>` : ""}
        ${evHtml}
      </li>`;
    }).join("");

    const orphans = evidence.filter((ev) => !Array.isArray((ev || {}).claim_ids)
      || !ev.claim_ids.some((id) => matched[id]));
    const orphanHtml = orphans.length
      ? `<div class="chat-auth-orphan-title">未关联主张的证据</div>`
        + `<ul class="chat-ev-list">${orphans.map(renderAuthorityEvidence).join("")}</ul>`
      : "";

    const analysisHtml = analysis.length ? `
        <div class="chat-analysis">
          <div class="chat-analysis-label">分析（只引用已列出的证据）</div>
          <ul class="chat-analysis-list">${analysis.map((a) => {
            a = a || {};
            const cid = a.claim_id
              ? `<span class="chat-ev-stance">${escapeHtml(String(a.claim_id))}</span>` : "";
            return `<li>${cid}${escapeHtml(String(a.text || ""))}${idLine(a.evidence_ids, "引用证据")}</li>`;
          }).join("")}</ul>
        </div>` : "";

    const limitsHtml = limitations.length
      ? `<div class="chat-auth-limits-title">局限说明</div>`
        + `<ul class="chat-ev-list">${limitations.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>`
      : "";

    return (claimItems
      ? `<ul class="chat-ev-claims">${claimItems}</ul>`
      : '<p class="chat-insight-sub muted">本轮没有拆出可核查主张。</p>')
      + orphanHtml + analysisHtml + conflictsBlock(c.conflicts) + limitsHtml
      + sourceGroups(c.sources) + issuesFold(c.gaps, c.errors)
      + auditLine(c.counts) + executionLine(c.execution);
  }

  function renderAuthority(c) {
    c = c || {};
    const claims = Array.isArray(c.claims) ? c.claims : [];
    const evidence = Array.isArray(c.evidence) ? c.evidence : [];
    const counts = (c.counts && typeof c.counts === "object") ? c.counts : {};
    const verifiedCount = counts.verified_sources === undefined
      ? "" : `<span class="badge gray">已核验信源 ${Number(counts.verified_sources) || 0}</span>`;
    return `
      <div class="chat-card chat-card-authority">
        <div class="chat-card-title">🛡️ 权威核验<span class="badge gray">只列证据 · 按来源分级</span>${degradedBadge(c)}</div>
        <div class="chat-badges">${runBadges(c)}<span class="badge gray">主张 ${claims.length} · 证据 ${evidence.length}</span>${verifiedCount}</div>
        ${authorityBody(c)}
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ---------- 10. 复合卡（collect_and_verify：资料收集 + 权威核验） ----------
     card = {card_type:"composite", status, task_status, as_of, degraded, research,
     authority, summary, gaps, errors, counts, execution, incremental, base_as_of}。
     复合卡只做两段结果的并列展示：正文复用 researchBody / authorityBody，
     不再套一层卡片标题（避免嵌套重复标题）；两段互不吞并，缺哪段就如实说没有。 */
  function subAsOf(sub) {
    const asOf = (sub && typeof sub === "object") ? sub.as_of : "";
    return `<span class="chat-card-submeta">子卡证据截至 ${escapeHtml(String(asOf || "未知"))}</span>`;
  }

  /* 子卡状态徽章：复合卡的两段各自带自己的六态（一段 partial 一段 completed 时看得见）。 */
  function subStatus(sub) {
    if (!sub || typeof sub !== "object") return "";
    const text = RUN_STATUS[String(sub.status || "")] || String(sub.status || "未知");
    return `<span class="badge gray">${escapeHtml(text)}</span>`;
  }

  function renderComposite(c) {
    c = c || {};
    const research = (c.research && typeof c.research === "object") ? c.research : null;
    const authority = (c.authority && typeof c.authority === "object") ? c.authority : null;
    const summary = String(c.summary || "").trim();
    const topic = research ? String(research.topic || "") : "";
    return `
      <div class="chat-card chat-card-composite">
        <div class="chat-card-title">🧭 复合核验<span class="badge gray">资料收集 + 权威核验</span>${degradedBadge(c)}</div>
        <div class="chat-card-label">${escapeHtml(topic || "资料收集 + 权威核验")}</div>
        <div class="chat-badges">${runBadges(c)}</div>
        ${summary ? `<p class="chat-composite-summary">${escapeHtml(summary)}</p>` : ""}
        <div class="chat-card-sub chat-card-sub-research">
          <div class="chat-card-subtitle">📚 资料收集${research ? subStatus(research) + subAsOf(research) : ""}</div>
          ${research ? researchBody(research) : '<p class="chat-insight-note muted">本次没有生成资料收集结果。</p>'}
        </div>
        <div class="chat-card-sub chat-card-sub-authority">
          <div class="chat-card-subtitle">🛡️ 权威核验${authority ? subStatus(authority) + subAsOf(authority) : ""}</div>
          ${authority ? authorityBody(authority) : '<p class="chat-insight-note muted">本次没有生成权威核验结果。</p>'}
        </div>
        ${issuesFold(c.gaps, c.errors)}
        ${auditLine(c.counts)}
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ---------- 分发：优先 claim_answer，其次保留既有业务卡片兼容 ---------- */
  function renderCard(payload) {
    if (!payload || typeof payload !== "object") return "";
    if (payload.claim_answer && typeof payload.claim_answer === "object") {
      return renderClaimAnswer(payload.claim_answer);
    }
    const type = String(payload.card_type || payload.type || "").toLowerCase();
    if (type === "insights") return renderInsights(payload);
    if (type === "evidence") return renderEvidence(payload);
    if (type === "batch_summary") return renderBatchSummary(payload);
    if (type === "monitor_change") return renderMonitorChange(payload);
    /* 信息收集 / 权威核验 / 复合（card_type 由后端 loop._CARD_TYPE 与工具层给出；
       历史回放走整包 payload，card_type 在顶层，按它分发） */
    if (type === "research" || type === "collect_research") return renderResearch(payload);
    if (type === "authority" || type === "verify_authority") return renderAuthority(payload);
    if (type === "composite" || type === "collect_and_verify") return renderComposite(payload);
    if (type === "conclusion") return renderConclusion(payload);
    if (type === "speech" || type === "classic") return renderSpeech(payload);
    if (type === "heat" || type === "attention") return renderHeat(payload);
    /* 无 card_type 时按字段形态识别（各渲染器互不混淆的判据）。
       实时路径 chat.js 的 onCard 会把 d.card 解出来再交给 renderCard，而 card_type
       只挂在事件顶层——这三张卡必须能只靠字段形态认出来，否则实时与回放不一致。 */
    if (payload.research !== undefined && payload.authority !== undefined) {
      return renderComposite(payload);
    }
    if (payload.key_findings !== undefined || payload.sub_questions !== undefined
        || payload.timeline !== undefined || payload.leads !== undefined) {
      return renderResearch(payload);
    }
    if (Array.isArray(payload.claims)
        && (payload.limitations !== undefined
            || payload.claims.some((cl) => cl && typeof cl === "object"
                && cl.balance !== undefined))) {
      return renderAuthority(payload);
    }
    if (payload.score !== undefined || payload.e_score !== undefined) return renderHeat(payload);
    if (payload.emotion || payload.attack || payload.logic) return renderSpeech(payload);
    /* insights 卡即使没带 card_type，也能按聚合字段名识别出来 */
    if (payload.patterns !== undefined || payload.profiles !== undefined
        || (payload.digest !== undefined && payload.kind)) return renderInsights(payload);
    /* batch_summary 卡即使没带 card_type（如 /api/batch/{id} 的 summary 段），
       也能按汇总字段名识别：计数四件套 + 标签分布/同源组，与其它卡型不混淆 */
    if ((payload.by_label !== undefined || payload.groups !== undefined)
        && (payload.total !== undefined || payload.blocked !== undefined
            || payload.not_run !== undefined)) return renderBatchSummary(payload);
    return renderConclusion(payload);
  }

  /* 供 chat.js / batch.js 挂载卡片用：把 renderCard 的产出写进容器。
     innerHTML 只出现在本文件（cards.js 是唯一允许模板字符串的渲染层，
     与 renderConclusion 同一模式）；调用方（batch.js）因此全文件没有
     HTML 注入点，纪律同 settings.js / profiles.js。 */
  function mountCard(container, payload) {
    if (!container) return;
    container.innerHTML = renderCard(payload);
  }

  /* 供 chat.js 调用的唯一入口（不污染全局命名空间） */
  window.ChatCards = {
    renderCard, renderClaimAnswer, renderConclusion, renderSpeech, renderHeat,
    renderInsights, renderEvidence, renderBatchSummary, renderMonitorChange,
    renderResearch, renderAuthority, renderComposite,
    mountCard, escapeHtml,
  };
})();
