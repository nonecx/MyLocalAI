/* ============================================================
   cards.js —— 对话卡片渲染器（M2，工作指导 §4.3）
   三个渲染器：结论卡 / 言论土壤卡（classic 四档） / 热度 E 分卡；
   另加我的画像卡（insights 只读聚合）与事实核查答案渲染。
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
      <div class="chat-card chat-card-evidence">
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

  /* ---------- 监测平台卡（platform_feed 工具：预警/信息流/巡检记录） ----------
     由后端 tools_platform 产出：card = {card_type:"platform", kind, as_of,
     warnings|feed|scans}。口径：传播风险分级只描述传播风险、不判定真假；
     热度是代理/估计值；只读零 LLM。放在画像区之外（test_insights_api 的
     渲染区切片以「新事实核查答案」为界，平台卡字段不该混进画像自洽断言）。 */
  const PLATFORM_LEVEL_CLS = { notice: "gray", suspect: "amber", high: "red", urgent: "critical" };

  /* 方案 G 轻量化：条目只有「档位徽章 + 标题 + 单行 meta（E / 结论 / 发现时间）」。
     风险理由链与档案号属于重信息，不进对话卡（要看细节回平台页）。 */
  function renderPlatformEntry(e) {
    e = e || {};
    const cls = PLATFORM_LEVEL_CLS[e.risk_level] || "gray";
    const eVal = (e.e_score === null || e.e_score === undefined) ? "E=?" : `E=${Number(e.e_score).toFixed(1)}`;
    const verdict = e.verdict_label
      ? `${escapeHtml(e.verdict_label)}${e.risk_score !== null && e.risk_score !== undefined ? `（${Number(e.risk_score)}）` : ""}`
      : "未深查";
    const when = String(e.discovered_at || "").replace("T", " ").slice(0, 19) || "未知";
    return `<li class="chat-platform-entry">
      <div class="chat-platform-entry-head">
        <span class="badge ${cls}">${escapeHtml(e.risk_label || e.risk_level || "?")}</span>
        <span class="chat-platform-entry-title">${escapeHtml(String(e.title || "").slice(0, 60))}</span>
      </div>
      <div class="chat-platform-entry-meta">${eVal} · 结论：${verdict} · ${escapeHtml(when)}</div>
    </li>`;
  }

  function renderPlatform(card) {
    card = card || {};
    const kind = String(card.kind || "");
    const titles = { warnings: "监测平台 · 传播风险预警", feed: "监测平台 · 信息流", scans: "监测平台 · 巡检记录" };
    const title = titles[kind] || "监测平台";
    let body = "";
    if (kind === "scans") {
      const scans = Array.isArray(card.scans) ? card.scans : [];
      body = scans.length
        ? `<ul class="chat-platform-list">${scans.map((s) => `
          <li><span class="badge gray">${escapeHtml(s.status || "?")}</span>
          <span class="chat-platform-entry-meta">${escapeHtml(String(s.started_at || "").replace("T", " ").slice(0, 19))}</span>
          <span class="chat-platform-entry-meta">候选 ${Number(s.candidates) || 0} · 深查 ${Number(s.verified) || 0} · 降级 ${Number(s.degraded) || 0}</span>
          ${s.error ? `<div class="chat-platform-entry-meta">${escapeHtml(s.error)}</div>` : ""}</li>`).join("")}</ul>`
        : '<p class="chat-insight-empty">暂无巡检记录</p>';
    } else {
      const rows = Array.isArray(card[kind]) ? card[kind] : [];
      body = rows.length
        ? `<ul class="chat-platform-list">${rows.map(renderPlatformEntry).join("")}</ul>`
        : `<p class="chat-insight-empty">${kind === "warnings" ? "暂无预警（没有高危/紧急档信息）" : "暂无信息"}</p>`;
    }
    return `
      <div class="chat-card chat-card-platform">
        <div class="chat-card-title">${escapeHtml(title)}<span class="badge gray">只读 · 零 LLM</span>${degradedBadge(card)}</div>
        ${body}
        ${asOfLine(card.as_of)}
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
    if (type === "platform") return renderPlatform(payload);
    if (type === "conclusion") return renderConclusion(payload);
    if (type === "speech" || type === "classic") return renderSpeech(payload);
    if (type === "heat" || type === "attention") return renderHeat(payload);
    /* 无 card_type 时按字段形态识别（各渲染器互不混淆的判据） */
    if (payload.score !== undefined || payload.e_score !== undefined) return renderHeat(payload);
    if (payload.emotion || payload.attack || payload.logic) return renderSpeech(payload);
    /* insights 卡即使没带 card_type，也能按聚合字段名识别出来 */
    if (payload.patterns !== undefined || payload.profiles !== undefined
        || (payload.digest !== undefined && payload.kind)) return renderInsights(payload);
    return renderConclusion(payload);
  }

  /* 供 chat.js 调用的唯一入口（不污染全局命名空间） */
  window.ChatCards = {
    renderCard, renderClaimAnswer, renderConclusion, renderSpeech, renderHeat,
    renderInsights, renderEvidence, renderPlatform, escapeHtml,
  };
})();
