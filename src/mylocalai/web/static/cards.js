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

   本波次新增两张只读卡（信息收集 + 权威核验）：资料收集（research，主动调研 +
   逐字引文）与权威核验（authority，逐主张证据倾向 + 受约束分析）。两张卡都只
   转述后端产物，不新增/改写证据，不显示任何 score / confidence 数值分，不写
   "真/假/属实"固定判定标签（balance 用中文文字徽章）。

    本波次再新增三张只读卡（第五波，按角色绑定）：统计说法核查（statistics，逐主张
    数字口径比对 + 四态判语徽章）、档案对比（comparison，两份档案的只读 diff）、
    事件时间线（timeline，带逐字引文的事件整理）。三张卡同样只转述后端产物：
    不显示任何数值评分，不写真假判定词，外部来源 URL 一律纯文本呈现，as_of 缺省
    明说「未知」。
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

  /* 情绪强度分档（界面去数值）：不展示 0-10 数字分，只给高/中/低档位文案。
     档位线与后端言论四档判定同一条口径——verdict_rules.json 的 speech_tier 段
     （high_score=7 / suspect_score=5）：>=7 与"高强度"判定线一致，>=5 与
     "疑似情绪化谣言"线一致，其余为低。展示层只读这两条线，后端调阈值时
     改 verdict_rules.json 即可，前端此处只保留同一数值作注释口径。
     分数缺失/非法时不出档位（只留情感倾向枚举），不拿 0 冒充"低"。 */
  const EMOTION_BAND_HIGH = 7;   // 与 speech_tier.high_score 同线
  const EMOTION_BAND_SUSPECT = 5; // 与 speech_tier.suspect_score 同线
  function emotionBand(score) {
    const n = Number(score);
    if (score === null || score === undefined || score === "" || !Number.isFinite(n)) return "";
    if (n >= EMOTION_BAND_HIGH) return "高";
    if (n >= EMOTION_BAND_SUSPECT) return "中";
    return "低";
  }

  const HEAT_BAND_CLS = {
    "高关注": "red",
    "中关注": "amber",
    "低关注": "green",
    "冷启动": "gray",
  };

  const CONF_CLS = { high: "green", medium: "amber", low: "gray" };
  /* 置信度是 high/medium/low 三档（档位徽章，不是数值分）：界面只出中文档位
     文案，未知档位原样透传（escapeHtml 后展示），绝不出现数字。 */
  const CONF_LABEL = { high: "高", medium: "中", low: "低" };

  /* 置信度档位徽章（档案对比卡单侧用）：档位缺失/为空时不出徽章，
     不拿默认档冒充；只出中文档位文案，不出任何数值分。 */
  function confTierBadge(side) {
    const v = String((side || {}).confidence || "");
    if (!v) return "";
    return `<span class="badge ${CONF_CLS[v] || "gray"}">置信度 ${escapeHtml(CONF_LABEL[v] || v)}</span>`;
  }

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
     前端不推断、不补造任何证据。
     界面去数值（分层口径）：判定用的 risk_score 数值不在界面展示——载荷里
     可能仍有该字段（后端留档），前端只渲染结论标签 + 置信度档位 + 证据计数，
     字段缺不缺、有没有数值都不崩、都不渲染数字。 */
  function renderConclusion(c) {
    c = c || {};
    const suff = (c.evidence_sufficiency && typeof c.evidence_sufficiency === "object")
      ? c.evidence_sufficiency : {};
    const preview = Array.isArray(c.evidence_preview) ? c.evidence_preview.slice(0, 5) : [];
    const evTotal = Number(c.evidence_total) || 0;
    const conf = String(c.confidence || "");
    const recheck = Array.isArray(c.recheck_conditions) ? c.recheck_conditions : [];
    /* presentation === "simple"（v6 普通用户档案工作区）：换标题并加一行说明，
       保持 layperson 简化语气——简化必须看得见。全角色都只给标签与档位
       （不再有"普通视图才藏分数"的差别），说明行只讲"直接给结论 + 档案库可查"。 */
    const simple = c.presentation === "simple";
    const title = simple ? "🧾 核查结果" : "🧾 核查结论";
    return `
      <div class="chat-card chat-card-conclusion${simple ? " chat-card-simple" : ""}">
        <div class="chat-card-title">${title}${simple ? "" : '<span class="badge gray">结论卡</span>'}${degradedBadge(c)}</div>
        <div class="chat-card-label">${escapeHtml(c.label || "（无标签）")}</div>
        ${simple ? '<p class="chat-simple-note muted">已按普通用户视图简化：直接给结论与证据概况；完整判定细节与证据批次可在档案库查看。</p>' : ""}
        <div class="chat-badges">
          ${conf ? `<span class="badge ${CONF_CLS[conf] || "gray"}">置信度 ${escapeHtml(CONF_LABEL[conf] || conf)}</span>` : ""}
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
          ${e ? `<div class="dim-row"><span class="k">情感倾向</span><span class="v">${escapeHtml(e.emotion_tendency || "未知")}${emotionBand(e.emotion_score) ? `（情绪强度：${emotionBand(e.emotion_score)}）` : ""}</span></div>` : ""}
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
    session_verified: "会话内动态核验",
    unverified_candidate: "未核验候选",
    unverified: "未核验",
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

  /* 条目结论文案（界面去数值）：判定用的 risk_score 数值不在条目行展示——
     有 label（后端 tools_batch 落库的结论标签）就用 label；完成但没拿到 label
     时给「已核查」中性文案；失败/拦截/未跑等状态已有状态徽章，标签位如实
     出「—」，不冒充"已核查"也不留"无标签"空话。字段缺失一律不抛错。 */
  function batchItemLabel(it) {
    const label = String((it || {}).label || "").trim();
    if (label) return label;
    const st = String((it || {}).status || "");
    return (st === "ok" || st === "done") ? "已核查" : "—";
  }

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

    /* 条目表：position + 状态徽章 + 结论标签（+ url）+ 档案链接。
       界面去数值：判定分不在条目行展示，标签列走 batchItemLabel 的兜底口径。 */
    const itemHtml = items.length ? `
        <table class="chat-batch-items-table">
          <thead><tr><th>#</th><th>状态</th><th>结论标签</th><th>档案</th></tr></thead>
          <tbody>${items.map((it) => {
            it = it || {};
            const meta = BATCH_ITEM_STATUS[String(it.status)] || null;
            const statusText = meta ? meta.text : escapeHtml(String(it.status || "未知"));
            const statusCls = meta ? meta.cls : "gray";
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
                <td>${escapeHtml(batchItemLabel(it))}${urlHtml}</td>
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

  /* ---------- 8. 资料收集卡（collect_research：主动调研 + 逐字引文） ----------
     由后端 tools_research 产出：card = {card_type:"research", status, task_status,
     as_of, degraded, topic, scope, sub_questions, key_findings, leads, evidence,
     sources{verified,unverified}, timeline, conflicts, analysis, gaps, errors,
     counts{sub_questions,searches,pages,sources,evidence,llm_calls}, followups}
     （形状见 .tmp/info-skills-contract.md §4.3/§6.1）。
     口径诚实铁律同全文件：引文逐字透传（前端不改写、不补造、不把摘要当结论），
     关键发现必须能追到 evidence_id；未核验来源与未采用线索单独成区并标注"仅供参考"；
     不展示任何数值评分，不写"真/假/属实"固定判定。空结果给空列表，不编造。 */
  const AUTHORITY_CLASS_LABEL = {
    official: "官方",
    international_org: "国际组织",
    academic: "学术",
    standards: "标准",
    factcheck: "事实核查",
    media: "媒体",
    unverified: "未核验",
  };

  const SUPPORT_LEVEL_LABEL = {
    authoritative: "权威来源支撑",
    reliable: "可靠来源支撑",
  };

  /* 卡片字段类型兜底：外部结构可能缺键/给错类型，一律按"没有"处理，不抛不猜 */
  function cardObj(v) {
    return (v && typeof v === "object") ? v : {};
  }

  function cardList(v) {
    return Array.isArray(v) ? v : [];
  }

  function hasNum(v) {
    return v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));
  }

  function cardNum(v) {
    return hasNum(v) ? Number(v) : 0;
  }

  /* 计数展示：后端没给就写"—"，不拿 0 冒充（口径诚实） */
  function countText(v) {
    return hasNum(v) ? String(cardNum(v)) : "—";
  }

  /* 核验方式/来源类别用中文文字徽章，不展示任何数值分 */
  function verificationLabel(v) {
    return VERIFY_META[String(v || "")] || "";
  }

  function authorityClassLabel(v) {
    return AUTHORITY_CLASS_LABEL[String(v || "")] || "";
  }

  function cardIssueFold(gaps, errors) {
    if (!gaps.length && !errors.length) return "";
    return `
        <details class="chat-evidence-fold">
          <summary>缺口 ${gaps.length} 条 · 错误 ${errors.length} 条</summary>
          ${gaps.length ? `<ul class="chat-ev-list">${gaps.map((g) => `<li>${escapeHtml(String(g))}</li>`).join("")}</ul>` : ""}
          ${errors.length ? `<ul class="chat-ev-list">${errors.map((e) => {
            e = cardObj(e);
            return `<li>${escapeHtml(String(e.stage || ""))} · ${escapeHtml(String(e.category || ""))}：${escapeHtml(String(e.detail || ""))}</li>`;
          }).join("")}</ul>` : ""}
        </details>`;
  }

  /* 研究证据条目：引文逐字 blockquote + 来源/类别/核验方式 + 可点链接（只放行 http(s)） */
  function researchEvidenceItem(ev) {
    ev = cardObj(ev);
    const url = safeUrl(ev.url);
    const title = escapeHtml(String(ev.title || ev.url || "（无标题）"));
    const link = url
      ? `<a href="${url}" target="_blank" rel="noopener nofollow">${title}</a>`
      : title;
    const meta = [];
    if (ev.evidence_id) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(ev.evidence_id))}</span>`);
    if (ev.role) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(ev.role))}</span>`);
    if (ev.tier) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(ev.tier))}</span>`);
    const auth = authorityClassLabel(ev.authority_class);
    if (auth) meta.push(`<span class="badge gray">${escapeHtml(auth)}</span>`);
    const ver = verificationLabel(ev.verification);
    if (ver) meta.push(`<span class="chat-ev-stance">${escapeHtml(ver)}</span>`);
    const source = String(ev.name || ev.domain || "");
    if (source) meta.push(`<span class="chat-ev-stance">${escapeHtml(source)}</span>`);
    const limitations = cardList(ev.limitations);
    return `<li class="chat-res-ev">${meta.join("")}${link}` +
      (ev.quote ? `<blockquote class="chat-ev-quote">${escapeHtml(String(ev.quote))}</blockquote>` : "") +
      (ev.published_at ? `<div class="chat-ev-url">发表于 ${escapeHtml(String(ev.published_at))}</div>` : "") +
      (limitations.length ? `<div class="chat-ev-url">限制：${escapeHtml(limitations.map(String).join("；"))}</div>` : "") +
      "</li>";
  }

  /* 信源分两级：已核验（目录锚点/已核验站点链接/会话内动态核验）与未核验候选，
     后者明确标注"只作线索、不进入关键发现"——来源不自我认证。 */
  function researchSourceItem(s) {
    s = cardObj(s);
    const meta = [];
    if (s.tier) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(s.tier))}</span>`);
    const auth = authorityClassLabel(s.authority_class);
    if (auth) meta.push(`<span class="badge gray">${escapeHtml(auth)}</span>`);
    const ver = verificationLabel(s.verification);
    if (ver) meta.push(`<span class="chat-ev-stance">${escapeHtml(ver)}</span>`);
    return `<li>${meta.join("")}${escapeHtml(String(s.name || s.domain || "（未命名信源）"))}` +
      `<div class="chat-ev-url">${escapeHtml(String(s.domain || ""))}` +
      (s.verification_basis ? ` · ${escapeHtml(String(s.verification_basis))}` : "") + "</div></li>";
  }

  function researchSourcesSection(list, title, note) {
    if (!list.length) return "";
    return `<div class="chat-res-section-title">${escapeHtml(title)}</div>` +
      `<ul class="chat-ev-list">${list.map(researchSourceItem).join("")}</ul>` +
      (note ? `<p class="chat-insight-note muted">${escapeHtml(note)}</p>` : "");
  }

  function renderResearch(c) {
    c = cardObj(c);
    const counts = cardObj(c.counts);
    const scope = cardObj(c.scope);
    const analysis = cardObj(c.analysis);
    const sources = cardObj(c.sources);
    const subQuestions = cardList(c.sub_questions);
    const findings = cardList(c.key_findings);
    const leads = cardList(c.leads);
    const evidence = cardList(c.evidence);
    const verified = cardList(sources.verified);
    const unverified = cardList(sources.unverified);
    const timeline = cardList(c.timeline);
    const conflicts = cardList(c.conflicts);
    const gaps = cardList(c.gaps);
    const errors = cardList(c.errors);
    const followups = cardList(c.followups);

    /* 关键发现只能引用 evidence 列表里的条目：先按 evidence_id 建索引，
       引不到的 ID 如实漏出（不补造引文）。 */
    const evidenceById = {};
    evidence.forEach((ev) => {
      ev = cardObj(ev);
      if (ev.evidence_id) evidenceById[String(ev.evidence_id)] = ev;
    });

    const scopeBits = [];
    if (scope.question) scopeBits.push(`<div class="dim-row"><span class="k">研究问题</span><span class="v">${escapeHtml(String(scope.question))}</span></div>`);
    if (scope.time_window) scopeBits.push(`<div class="dim-row"><span class="k">时间范围</span><span class="v">${escapeHtml(String(scope.time_window))}</span></div>`);
    if (scope.region) scopeBits.push(`<div class="dim-row"><span class="k">地域范围</span><span class="v">${escapeHtml(String(scope.region))}</span></div>`);
    if (scope.basis) scopeBits.push(`<div class="dim-row"><span class="k">范围依据</span><span class="v">${escapeHtml(String(scope.basis))}</span></div>`);
    const scopeHtml = scopeBits.length ? `<div class="chat-dims">${scopeBits.join("")}</div>` : "";

    const subQHtml = subQuestions.length ? `
        <div class="chat-res-section-title">子问题（${subQuestions.length} 条）</div>
        <ul class="chat-res-subqs">${subQuestions.map((sq) => {
          sq = cardObj(sq);
          const bits = [];
          if (sq.sq_id) bits.push(`<b>${escapeHtml(String(sq.sq_id))}</b>`);
          bits.push(`<span>${escapeHtml(String(sq.text || "（未提供子问题）"))}</span>`);
          if (sq.status) bits.push(`<span class="chat-ev-stance">${escapeHtml(String(sq.status))}</span>`);
          if (hasNum(sq.evidence_count)) bits.push(`<span class="chat-ev-stance">证据 ${cardNum(sq.evidence_count)} 条</span>`);
          return `<li class="chat-res-subq">${bits.join(" ")}</li>`;
        }).join("")}</ul>` : "";

    const findingHtml = findings.length ? `
        <div class="chat-res-section-title">关键发现（${findings.length} 条）</div>
        <ul class="chat-res-findings">${findings.map((fd) => {
          fd = cardObj(fd);
          const ids = cardList(fd.evidence_ids);
          const refs = ids.map((id) => evidenceById[String(id)]).filter(Boolean)
            .map(researchEvidenceItem).join("");
          const head = [];
          if (fd.finding_id) head.push(`<b>${escapeHtml(String(fd.finding_id))}</b>`);
          const level = SUPPORT_LEVEL_LABEL[String(fd.support_level || "")] || "";
          if (level) head.push(`<span class="badge gray">${escapeHtml(level)}</span>`);
          if (ids.length) head.push(`<span class="chat-ev-stance">证据 ${escapeHtml(ids.map(String).join("、"))}</span>`);
          return `<li class="chat-res-finding"><div class="chat-res-finding-head">${head.join(" ")}</div>` +
            `<div class="chat-res-finding-text">${escapeHtml(String(fd.statement || ""))}</div>` +
            (refs ? `<ul class="chat-ev-list">${refs}</ul>`
              : '<p class="chat-insight-sub muted">该发现没有可定位的引文（以 evidence_ids 为准，不补造）。</p>') +
            "</li>";
        }).join("")}</ul>` : "";

    /* 线索 = 未核验/证据不足，只能作参考；单独成区避免被误当关键发现 */
    const leadHtml = leads.length ? `
        <div class="chat-res-section-title">未核验线索（仅供参考，不进入关键发现）</div>
        <ul class="chat-res-leads">${leads.map((ld) => {
          ld = cardObj(ld);
          const ids = cardList(ld.evidence_ids);
          const head = [];
          if (ld.lead_id) head.push(`<b>${escapeHtml(String(ld.lead_id))}</b>`);
          if (ids.length) head.push(`<span class="chat-ev-stance">证据 ${escapeHtml(ids.map(String).join("、"))}</span>`);
          return `<li class="chat-res-lead">${head.join(" ")}` +
            `<div>${escapeHtml(String(ld.statement || ""))}${ld.reason ? `<span class="chat-res-lead-reason">（${escapeHtml(String(ld.reason))}）</span>` : ""}</div>` +
            "</li>";
        }).join("")}</ul>` : "";

    const timelineHtml = timeline.length ? `
        <div class="chat-res-section-title">时间线（${timeline.length} 条）</div>
        <ul class="chat-res-timeline">${timeline.map((it) => {
          it = cardObj(it);
          const ids = cardList(it.evidence_ids);
          return `<li class="chat-res-timeline-item"><span class="chat-res-date">${escapeHtml(String(it.date || "日期未知"))}</span>` +
            `<span>${escapeHtml(String(it.event || ""))}</span>` +
            (ids.length ? `<span class="chat-ev-stance">证据 ${escapeHtml(ids.map(String).join("、"))}</span>` : "") +
            "</li>";
        }).join("")}</ul>` : "";

    const conflictHtml = conflicts.length ? `
        <div class="chat-res-section-title">资料之间的分歧（${conflicts.length} 条）</div>
        <ul class="chat-res-conflicts">${conflicts.map((cf) => {
          cf = cardObj(cf);
          const ids = cardList(cf.evidence_ids);
          return `<li class="chat-res-conflict">${escapeHtml(String(cf.summary || ""))}` +
            (ids.length ? `<div class="chat-ev-url">涉及证据 ${escapeHtml(ids.map(String).join("、"))}</div>` : "") +
            "</li>";
        }).join("")}</ul>` : "";

    /* 分析是后端受约束产物（label 固定为"基于资料的整理/推断"口径），原文透传 */
    const analysisText = String(analysis.text || "");
    const analysisIds = cardList(analysis.evidence_ids);
    const analysisHtml = analysisText ? `
        <div class="chat-res-section-title">分析</div>
        <div class="chat-res-analysis">
          <span class="chat-res-analysis-label">${escapeHtml(String(analysis.label || "基于资料的整理/推断"))}</span>
          <p>${escapeHtml(analysisText)}</p>
          ${analysisIds.length ? `<p class="chat-ev-url">涉及证据 ${escapeHtml(analysisIds.map(String).join("、"))}</p>` : ""}
        </div>` : "";

    const followupHtml = followups.length ? `
        <div class="chat-res-section-title">可继续追问</div>
        <ul class="chat-res-followups">${followups.map((f) => `<li>${escapeHtml(String(f))}</li>`).join("")}</ul>` : "";

    /* 空结果也是合法出口：如实说"没有"，不留白也不编 */
    const emptyHtml = (!findings.length && !leads.length && !evidence.length
        && !timeline.length && !conflicts.length)
      ? '<p class="chat-insight-sub muted">本轮没有找到可展示的资料条目（合法空结果，不补造）。</p>'
      : "";

    const sourceCount = hasNum(counts.sources) ? cardNum(counts.sources) : verified.length + unverified.length;
    const statusText = EVIDENCE_STATUS[String(c.status || "")] || String(c.status || "未知");
    const taskText = EVIDENCE_TASK[String(c.task_status || "")] || "";

    return `
      <div class="chat-card chat-card-research">
        <div class="chat-card-title">🔎 资料收集<span class="badge gray">只读调研 · 只述资料</span>${degradedBadge(c)}</div>
        <div class="chat-card-label">${escapeHtml(String(c.topic || "（未提供主题）"))}</div>
        <div class="chat-badges">
          <span class="badge gray">${escapeHtml(statusText)}</span>
          ${taskText ? `<span class="badge gray">${escapeHtml(taskText)}</span>` : ""}
          <span class="badge gray">来源 ${sourceCount} 个 · 关键发现 ${findings.length} 条 · 线索 ${leads.length} 条</span>
        </div>
        ${scopeHtml}
        ${subQHtml}
        ${findingHtml}
        ${leadHtml}
        ${emptyHtml}
        ${researchSourcesSection(verified, "已核验来源",
          "目录锚点 / 已核验站点链接 / 会话内动态核验：这些来源的引文才能支撑关键发现。")}
        ${researchSourcesSection(unverified, "未核验候选",
          "陌生域名不自我认证：以下候选只作线索，不进入关键发现。")}
        ${timelineHtml}
        ${conflictHtml}
        ${analysisHtml}
        ${followupHtml}
        ${cardIssueFold(gaps, errors)}
        <p class="chat-insight-note muted">已执行 ${countText(counts.sub_questions)} 个子问题的规划 · ${countText(counts.searches)} 次搜索 · ${countText(counts.pages)} 次读页 · ${countText(counts.llm_calls)} 次模型调用。引文逐字来自实际读取的正文；只述资料，不输出真假结论。</p>
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ---------- 9. 权威核验卡（verify_authority：逐主张证据倾向 + 受约束分析） ----------
     由后端 tools_authority 产出：card = {card_type:"authority", status, task_status,
     as_of, degraded, claims, evidence, sources{verified,unverified}, conflicts, analysis,
     gaps, errors, limitations, counts{claims,searches,pages,evidence,official_sources,
     llm_calls}}（形状见 .tmp/info-skills-contract.md §5.5/§6.2）。
     口径诚实铁律：balance/lean 是代码按证据算出的封闭枚举，这里只做中文文字徽章
     （绿/红/琥珀/灰），不显示任何数值分；conclusion 原文透传（允许按模板出现
     "证明/反驳"，卡片自身不写"真/假/属实"固定标签）；分析标"基于证据的分析"。 */
  const BALANCE_META = {
    supported: { cls: "green", text: "权威证据支持" },
    refuted: { cls: "red", text: "权威证据反驳" },
    contested: { cls: "amber", text: "权威证据冲突" },
    insufficient: { cls: "gray", text: "权威证据不足" },
    not_checkable: { cls: "gray", text: "不可核查" },
  };

  const LEAN_META = {
    supports: "支持",
    refutes: "反驳",
    mixed: "冲突",
    none: "无",
  };

  function balanceMeta(balance) {
    return BALANCE_META[String(balance || "")] || { cls: "gray", text: "权威证据不足" };
  }

  /* 权威证据条目：引文逐字 + 来源等级 + 核验方式 + 独立性分组 + 可点链接 */
  function authorityEvidenceItem(ev) {
    ev = cardObj(ev);
    const url = safeUrl(ev.url);
    const title = escapeHtml(String(ev.title || ev.url || "（无标题）"));
    const link = url
      ? `<a href="${url}" target="_blank" rel="noopener nofollow">${title}</a>`
      : title;
    const meta = [];
    if (ev.evidence_id) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(ev.evidence_id))}</span>`);
    if (ev.relation) {
      const rel = RELATION_META[String(ev.relation)] || { cls: "gray", text: "关系不明" };
      meta.push(`<span class="badge ${rel.cls}">${rel.text}</span>`);
    }
    if (ev.tier) meta.push(`<span class="chat-ev-stance">来源等级 ${escapeHtml(String(ev.tier))}</span>`);
    const auth = authorityClassLabel(ev.authority_class);
    if (auth) meta.push(`<span class="badge gray">${escapeHtml(auth)}</span>`);
    const ver = verificationLabel(ev.verification);
    if (ver) meta.push(`<span class="chat-ev-stance">${escapeHtml(ver)}</span>`);
    const source = String(ev.name || ev.domain || "");
    if (source) meta.push(`<span class="chat-ev-stance">${escapeHtml(source)}</span>`);
    const limitations = cardList(ev.limitations);
    return `<li class="chat-auth-ev">${meta.join("")}${link}` +
      (ev.authority_basis ? `<div class="chat-ev-url">权威依据：${escapeHtml(String(ev.authority_basis))}</div>` : "") +
      (ev.quote ? `<blockquote class="chat-ev-quote">${escapeHtml(String(ev.quote))}</blockquote>` : "") +
      (ev.published_at ? `<div class="chat-ev-url">发表于 ${escapeHtml(String(ev.published_at))}</div>` : "") +
      (ev.independence_group ? `<div class="chat-ev-url">独立性分组：${escapeHtml(String(ev.independence_group))}</div>` : "") +
      (limitations.length ? `<div class="chat-ev-url">限制：${escapeHtml(limitations.map(String).join("；"))}</div>` : "") +
      "</li>";
  }

  function authoritySourcesSection(list, title, note) {
    if (!list.length) return "";
    return `<div class="chat-auth-section-title">${escapeHtml(title)}</div>` +
      `<ul class="chat-ev-list">${list.map(researchSourceItem).join("")}</ul>` +
      (note ? `<p class="chat-insight-note muted">${escapeHtml(note)}</p>` : "");
  }

  function renderAuthority(c) {
    c = cardObj(c);
    const counts = cardObj(c.counts);
    const sources = cardObj(c.sources);
    const claims = cardList(c.claims);
    const evidence = cardList(c.evidence);
    const verified = cardList(sources.verified);
    const unverified = cardList(sources.unverified);
    const conflicts = cardList(c.conflicts);
    const analysis = cardList(c.analysis);
    const gaps = cardList(c.gaps);
    const errors = cardList(c.errors);
    const limitations = cardList(c.limitations);

    const claimIds = claims.map((cl) => String(cardObj(cl).claim_id || ""));
    const analysisFor = (cid) => analysis
      .map(cardObj)
      .filter((a) => cid && String(a.claim_id || "") === cid);
    /* 挂不到任何已列主张上的分析单独成区：丢内容比显示异常更糟 */
    const orphanAnalysis = analysis.filter((a) => {
      a = cardObj(a);
      const id = String(a.claim_id || "");
      return !id || claimIds.indexOf(id) < 0;
    });

    const claimHtml = claims.length ? `<ul class="chat-auth-claims">${claims.map((cl) => {
      cl = cardObj(cl);
      const cid = String(cl.claim_id || "");
      const bmeta = balanceMeta(cl.balance);
      const balanceText = String(cl.balance_label || bmeta.text);
      const leanText = String(cl.lean_label || LEAN_META[String(cl.lean || "")] || "无");
      const riskIds = cardList(cl.supporting_ids);
      const refuteIds = cardList(cl.refuting_ids);
      const contextIds = cardList(cl.context_ids);
      const rows = evidence.map(cardObj).filter((ev) =>
        cardList(ev.claim_ids).map(String).indexOf(cid) >= 0);
      /* 证据按 relation 三分：支持 / 反驳 / 背景与其他。relation 是封闭枚举，
         未知值落"背景/其他"如实展示，不硬塞进支持或反驳。 */
      const groups = { supports: [], refutes: [], context: [] };
      rows.forEach((ev) => {
        const rel = String(ev.relation || "");
        if (rel === "supports") groups.supports.push(ev);
        else if (rel === "refutes") groups.refutes.push(ev);
        else groups.context.push(ev);
      });
      const groupHtml = [
        ["支持证据", groups.supports, "green"],
        ["反驳证据", groups.refutes, "red"],
        ["背景 / 其他证据", groups.context, "gray"],
      ].filter(([, list]) => list.length).map(([title, list, cls]) =>
        `<div class="chat-auth-ev-group-title"><span class="badge ${cls}">${escapeHtml(title)}</span> ${list.length} 条</div>` +
        `<ul class="chat-ev-list">${list.map(authorityEvidenceItem).join("")}</ul>`).join("");
      const analysisHtml = analysisFor(cid).length ? `
          <div class="chat-auth-analysis">${analysisFor(cid).map((a) => {
            const ids = cardList(a.evidence_ids);
            return `<div class="chat-auth-analysis-item"><span class="chat-auth-analysis-label">基于证据的分析</span>` +
              `<p>${escapeHtml(String(a.text || ""))}</p>` +
              (ids.length ? `<p class="chat-ev-url">涉及证据 ${escapeHtml(ids.map(String).join("、"))}</p>` : "") +
              "</div>";
          }).join("")}</div>` : "";
      const limitationList = cardList(cl.limitations);
      const idBits = [];
      if (riskIds.length) idBits.push(`支持 ${escapeHtml(riskIds.map(String).join("、"))}`);
      if (refuteIds.length) idBits.push(`反驳 ${escapeHtml(refuteIds.map(String).join("、"))}`);
      if (contextIds.length) idBits.push(`背景 ${escapeHtml(contextIds.map(String).join("、"))}`);
      return `<li class="chat-auth-claim">
          <div class="chat-auth-claim-head"><b>${escapeHtml(cid)}</b>` +
        `<span class="chat-auth-claim-text">${escapeHtml(String(cl.text || ""))}</span></div>
          <div class="chat-badges">
            <span class="badge ${bmeta.cls}">${escapeHtml(balanceText)}</span>
            <span class="badge gray">倾向：${escapeHtml(leanText)}</span>
            ${idBits.length ? `<span class="chat-ev-stance">${idBits.join(" · ")}</span>` : ""}
          </div>
          ${cl.conclusion ? `<div class="chat-auth-conclusion">${escapeHtml(String(cl.conclusion))}</div>` : ""}
          ${groupHtml ? `<div class="chat-auth-ev-groups">${groupHtml}</div>`
            : '<p class="chat-insight-sub muted">该主张尚未找到可定位的权威证据。</p>'}
          ${analysisHtml}
          ${limitationList.length ? `<ul class="chat-auth-limits">${limitationList.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>` : ""}
        </li>`;
    }).join("")}</ul>` : '<p class="chat-insight-sub muted">本轮没有可核查的事实主张（合法空出口，不强行下判断）。</p>';

    const conflictHtml = conflicts.length ? `
        <div class="chat-auth-section-title">权威资料之间的冲突（${conflicts.length} 条）</div>
        <ul class="chat-auth-conflicts">${conflicts.map((cf) => {
          cf = cardObj(cf);
          const ids = cardList(cf.evidence_ids);
          return `<li class="chat-auth-conflict">${escapeHtml(String(cf.summary || ""))}` +
            (ids.length ? `<div class="chat-ev-url">涉及证据 ${escapeHtml(ids.map(String).join("、"))}</div>` : "") +
            "</li>";
        }).join("")}</ul>` : "";

    const orphanAnalysisHtml = orphanAnalysis.length ? `
        <div class="chat-auth-section-title">其他分析（未挂到已列主张）</div>
        <ul class="chat-ev-list">${orphanAnalysis.map((a) => {
          a = cardObj(a);
          const ids = cardList(a.evidence_ids);
          return `<li><span class="chat-auth-analysis-label">基于证据的分析</span>` +
            `<div>${escapeHtml(String(a.text || ""))}</div>` +
            (a.claim_id ? `<div class="chat-ev-url">主张 ${escapeHtml(String(a.claim_id))}</div>` : "") +
            (ids.length ? `<div class="chat-ev-url">涉及证据 ${escapeHtml(ids.map(String).join("、"))}</div>` : "") +
            "</li>";
        }).join("")}</ul>` : "";

    const limitationHtml = limitations.length ? `
        <div class="chat-auth-section-title">本轮的核验限制</div>
        <ul class="chat-auth-limits">${limitations.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>` : "";

    const sourceCount = hasNum(counts.official_sources) ? cardNum(counts.official_sources)
      : verified.length;
    const statusText = EVIDENCE_STATUS[String(c.status || "")] || String(c.status || "未知");
    const taskText = EVIDENCE_TASK[String(c.task_status || "")] || "";

    return `
      <div class="chat-card chat-card-authority">
        <div class="chat-card-title">⚖️ 权威核验<span class="badge gray">权威证据倾向 · 不显示数值分</span>${degradedBadge(c)}</div>
        <div class="chat-badges">
          <span class="badge gray">${escapeHtml(statusText)}</span>
          ${taskText ? `<span class="badge gray">${escapeHtml(taskText)}</span>` : ""}
          <span class="badge gray">主张 ${claims.length} 条 · 证据 ${evidence.length} 条 · 权威来源 ${sourceCount} 个</span>
        </div>
        ${claimHtml}
        ${authoritySourcesSection(verified, "已核验权威来源",
          "目录锚点 / 已核验站点链接 / 会话内动态核验（需交叉佐证）：这些来源的证据才能算权威证据。")}
        ${authoritySourcesSection(unverified, "未核验候选",
          "陌生域名不自我认证：以下候选只作背景，不进入权威证据。")}
        ${conflictHtml}
        ${orphanAnalysisHtml}
        ${limitationHtml}
        ${cardIssueFold(gaps, errors)}
        <p class="chat-insight-note muted">已执行 ${countText(counts.searches)} 次搜索 · ${countText(counts.pages)} 次读页 · ${countText(counts.llm_calls)} 次模型调用。balance/lean 由代码按证据计算，这里只用文字展示；不显示任何数值评分。</p>
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ---------- 10. 信源速查卡（check_source：单源可信度画像，代码判定） ----------
     由后端 tools_source 产出：card = {card_type:"source", status, task_status, as_of,
     degraded, query, resolved{kind,url,host,name,catalog,catalog_anchor,source_id,tier},
     state, state_label, authority_category, category_label, verified, basis, signals,
     history, summary, limits, gaps, errors, counts{searches,pages,llm_calls}}。
     口径诚实铁律：verified/category/signals 全部由代码算出，这里只做中文文字徽章
     （绿=信源身份已核实 / 琥珀=未核验或仅倾向），不显示任何数值分；"已核实"只指
     信源身份、永远不指内容。 */
  const SOURCE_SIGNAL_META = {
    official_suffix: "官方域名后缀",
    official_marker: "正文官方标记",
    inbound_link: "已核验站点直链",
    cross_mention: "多来源交叉提及",
  };

  function renderSource(c) {
    c = cardObj(c);
    const counts = cardObj(c.counts);
    const resolved = cardObj(c.resolved);
    const signals = cardList(c.signals);
    const limits = cardList(c.limits);
    const gaps = cardList(c.gaps);
    const errors = cardList(c.errors);
    const history = (c.history && typeof c.history === "object"
      && Object.keys(c.history).length) ? c.history : null;
    const verified = !!c.verified;
    const verMeta = verified
      ? { cls: "green", text: "信源身份已核实" }
      : { cls: "amber", text: "未核验 / 仅倾向" };
    const stateText = String(c.state_label || c.state || "未知");
    const categoryText = String(c.category_label || c.authority_category || "未知");
    const statusText = EVIDENCE_STATUS[String(c.status || "")] || String(c.status || "未知");
    const taskText = EVIDENCE_TASK[String(c.task_status || "")] || "";
    const kindText = ({ url: "网址", domain: "域名", name: "已登记机构名" })[String(resolved.kind || "")] || "未识别";
    const url = safeUrl(resolved.url);
    const targetText = escapeHtml(String(resolved.host || resolved.name || "（未识别）"));
    const targetHtml = url
      ? `<a href="${url}" target="_blank" rel="noopener nofollow">${targetText}</a>`
      : targetText;
    const signalHtml = signals.length
      ? `<ul class="chat-ev-list">${signals.map((s) =>
          `<li><span class="chat-ev-stance">${escapeHtml(SOURCE_SIGNAL_META[String(s)] || String(s))}</span></li>`).join("")}</ul>`
      : '<p class="chat-insight-sub muted">无代码信号：陌生来源不自我认证，只作背景不作结论。</p>';
    const historyHtml = history
      ? `<p class="chat-ev-url">历史战绩：${escapeHtml(Object.keys(history).slice(0, 4)
          .map((k) => `${k}=${String(history[k])}`).join("；"))}</p>`
      : '<p class="chat-ev-url">历史战绩：无记录</p>';
    const summaryText = String(c.summary || "");
    const summaryHtml = summaryText
      ? `<div class="chat-auth-analysis"><div class="chat-auth-analysis-item"><span class="chat-auth-analysis-label">流水线判定摘要（原文）</span>` +
        summaryText.split(/\r?\n/).filter((l) => String(l).trim())
          .map((l) => `<p>${escapeHtml(String(l))}</p>`).join("") + "</div></div>"
      : "";
    const limitHtml = limits.length
      ? `<div class="chat-auth-section-title">本轮的限制</div>` +
        `<ul class="chat-auth-limits">${limits.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>`
      : "";

    return `
      <div class="chat-card chat-card-source">
        <div class="chat-card-title">🔎 信源速查<span class="badge gray">只评信源 · 不判内容 · 无分数</span>${degradedBadge(c)}</div>
        <div class="chat-badges">
          <span class="badge gray">${escapeHtml(statusText)}</span>
          ${taskText ? `<span class="badge gray">${escapeHtml(taskText)}</span>` : ""}
          <span class="badge ${verMeta.cls}">${escapeHtml(verMeta.text)}</span>
          <span class="badge gray">权威类别：${escapeHtml(categoryText)}</span>
          <span class="badge gray">读取状态：${escapeHtml(stateText)}</span>
        </div>
        <p class="chat-insight-sub">输入：${escapeHtml(String(c.query || "（未记录）"))}（按${escapeHtml(kindText)}解析 → ${targetHtml}）</p>
        <div class="chat-auth-section-title">代码信号（判定依据）</div>
        ${signalHtml}
        ${c.basis ? `<p class="chat-ev-url">依据：${escapeHtml(String(c.basis))}</p>` : ""}
        ${historyHtml}
        ${summaryHtml}
        ${limitHtml}
        ${cardIssueFold(gaps, errors)}
        <p class="chat-insight-note muted">已执行 ${countText(counts.searches)} 次搜索（本技能不检索）· ${countText(counts.pages)} 次读页 · ${countText(counts.llm_calls)} 次模型调用。判定全部由代码完成；"已核实"只指信源身份、不指内容——要核查具体说法请用事实核查 / 权威核验。</p>
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ---------- 11. 辟谣方法指南卡（guide_fact_check：零检索纯知识讲解） ----------
     由后端 tools_guide 产出：card = {card_type:"guide", status, task_status, as_of,
     degraded, question, state, answer, steps, follow_up_hint, gaps, errors,
     counts{searches,pages,llm_calls}}。
     steps 是代码锁定的查证五步法（不在前端改口径）；answer 逐字透传流水线产物；
     state=degraded 表示模型讲解降级、内置模板兜底，如实标注。 */
  function renderGuide(c) {
    c = cardObj(c);
    const counts = cardObj(c.counts);
    const steps = cardList(c.steps);
    const gaps = cardList(c.gaps);
    const errors = cardList(c.errors);
    const statusText = EVIDENCE_STATUS[String(c.status || "")] || String(c.status || "未知");
    const degradedGuide = String(c.state || "") === "degraded";
    const stepHtml = steps.length
      ? `<ol class="chat-auth-limits">${steps.map((s) => `<li>${escapeHtml(String(s))}</li>`).join("")}</ol>`
      : '<p class="chat-insight-sub muted">（五步法清单缺失，属异常出口）</p>';
    const answerText = String(c.answer || "");
    const answerHtml = answerText
      ? `<div class="chat-auth-section-title">讲解</div>` +
        answerText.split(/\r?\n/).filter((l) => String(l).trim())
          .map((l) => `<p>${escapeHtml(String(l))}</p>`).join("")
      : "";
    return `
      <div class="chat-card chat-card-guide">
        <div class="chat-card-title">🧭 辟谣方法指南<span class="badge gray">只教方法 · 不下结论 · 无分数</span>${degradedBadge(c)}</div>
        <div class="chat-badges">
          <span class="badge gray">${escapeHtml(statusText)}</span>
          ${degradedGuide ? '<span class="badge amber">模型讲解降级（内置五步法模板兜底）</span>' : ""}
        </div>
        <p class="chat-insight-sub">问题：${escapeHtml(String(c.question || "（未记录）"))}</p>
        <div class="chat-auth-section-title">查证五步法</div>
        ${stepHtml}
        ${answerHtml}
        ${c.follow_up_hint ? `<p class="chat-insight-note">下一步：${escapeHtml(String(c.follow_up_hint))}</p>` : ""}
        ${cardIssueFold(gaps, errors)}
        <p class="chat-insight-note muted">已执行 ${countText(counts.llm_calls)} 次模型调用；检索 0 次、读页 0 次（本技能零检索零取页）。本卡只教方法，不对具体说法下真假结论——要核查具体说法请用事实核查。</p>
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ---------- 12. 统计说法核查卡（check_statistics：逐主张数字口径比对，四态判语） ----------
     由后端 tools_statistics 产出：card = {card_type:"statistics", status, task_status,
     as_of, degraded, text, state, state_label, claims[{claim_id, excerpt, number,
     unit, metric, qualifiers{time,place,entity,base}, verdict, verdict_label,
     source_number, source_url, source_title, quote, as_of, basis}], limits, gaps,
     errors, counts{claims,searches,pages,llm_calls}}。
     口径诚实铁律同全文件：四态判语（consistent/inconsistent/caliber_mismatch/
     unverifiable）全部由流水线内的代码算出，这里只做中文文字徽章，不显示任何数值
     分；引文与来源数值逐字透传；来源 URL 是外部内容，只做纯文本展示（与监测卡
     同口径，不造可点链接）；as_of 缺省明说「未知」；digest 里导流 verify_claim
     的话术不进卡。 */
  const STAT_VERDICT_META = {
    consistent: { cls: "green", text: "一致" },
    inconsistent: { cls: "red", text: "不一致" },
    caliber_mismatch: { cls: "amber", text: "口径不符" },
    unverifiable: { cls: "gray", text: "无法核实" },
  };

  /* 四态判语徽章：后端 verdict_label 优先（逐字透传），未知判语落灰色不猜 */
  function statisticsVerdict(cl) {
    cl = cardObj(cl);
    const meta = STAT_VERDICT_META[String(cl.verdict || "")] || { cls: "gray", text: "未知判定" };
    return { cls: meta.cls, text: String(cl.verdict_label || meta.text) };
  }

  function renderStatistics(c) {
    c = cardObj(c);
    const counts = cardObj(c.counts);
    const claims = cardList(c.claims);
    const limits = cardList(c.limits);
    const gaps = cardList(c.gaps);
    const errors = cardList(c.errors);
    const statusText = EVIDENCE_STATUS[String(c.status || "")] || String(c.status || "未知");
    const taskText = EVIDENCE_TASK[String(c.task_status || "")] || "";
    const stateText = String(c.state_label || c.state || "");

    /* 每条数字主张一组：原文摘录 + 数值 | 来源数值 + 引文 | 四态判定徽章 */
    const claimHtml = claims.length ? `<ul class="chat-stat-claims">${claims.map((cl) => {
      cl = cardObj(cl);
      const vmeta = statisticsVerdict(cl);
      const numText = (cl.number === null || cl.number === undefined || cl.number === "")
        ? "未知" : String(cl.number);
      const unit = String(cl.unit || "");
      const metric = String(cl.metric || "");
      const qual = cardObj(cl.qualifiers);
      const qualBits = [];
      if (qual.time) qualBits.push(`时间 ${escapeHtml(String(qual.time))}`);
      if (qual.place) qualBits.push(`地点 ${escapeHtml(String(qual.place))}`);
      if (qual.entity) qualBits.push(`主体 ${escapeHtml(String(qual.entity))}`);
      if (qual.base) qualBits.push(`基数 ${escapeHtml(String(qual.base))}`);
      const srcMeta = [];
      if (cl.source_number) srcMeta.push(`<span class="chat-ev-stance">来源数值 ${escapeHtml(String(cl.source_number))}</span>`);
      if (cl.source_title) srcMeta.push(`<span class="chat-ev-stance">《${escapeHtml(String(cl.source_title))}》</span>`);
      const srcUrlText = monitorUrlText(cl.source_url);
      const srcHtml = (srcMeta.length || srcUrlText || cl.quote)
        ? `<div class="chat-stat-source">${srcMeta.join("")}` +
          (srcUrlText ? `<div class="chat-ev-url">${srcUrlText}（外部链接，纯文本展示）</div>` : "") +
          (cl.quote ? `<blockquote class="chat-ev-quote">${escapeHtml(String(cl.quote))}</blockquote>` : "") +
          "</div>"
        : "";
      return `<li class="chat-stat-claim">
          <div class="chat-stat-claim-head">
            <b>${escapeHtml(String(cl.claim_id || ""))}</b>
            <span class="chat-stat-excerpt">${escapeHtml(String(cl.excerpt || ""))}</span>
          </div>
          <div class="chat-badges">
            <span class="badge ${vmeta.cls}">口径判定：${escapeHtml(vmeta.text)}</span>
            <span class="badge gray">数值 ${escapeHtml(numText)}${escapeHtml(unit)}${metric ? `（${escapeHtml(metric)}）` : ""}</span>
          </div>
          ${qualBits.length ? `<p class="chat-stat-qual">限定条件：${qualBits.join(" · ")}</p>` : ""}
          ${srcHtml}
          ${cl.basis ? `<p class="chat-stat-basis">比对依据：${escapeHtml(String(cl.basis))}</p>` : ""}
          <p class="chat-ev-url">该主张口径截至（as_of）：${escapeHtml(String(cl.as_of || "未知"))}</p>
        </li>`;
    }).join("")}</ul>`
      : '<p class="chat-insight-sub muted">本次没有识别到可核对的数字主张（合法空出口，不强行比对）。</p>';

    const limitHtml = limits.length
      ? `<div class="chat-auth-section-title">本轮的比对限制</div>` +
        `<ul class="chat-auth-limits">${limits.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>`
      : "";

    return `
      <div class="chat-card chat-card-statistics">
        <div class="chat-card-title">📊 统计说法核查<span class="badge gray">只比对口径 · 不判真假 · 无分数</span>${degradedBadge(c)}</div>
        <div class="chat-badges">
          <span class="badge gray">${escapeHtml(statusText)}</span>
          ${taskText ? `<span class="badge gray">${escapeHtml(taskText)}</span>` : ""}
          <span class="badge gray">数字主张 ${claims.length} 条</span>
          ${stateText ? `<span class="badge gray">运行状态：${escapeHtml(stateText)}</span>` : ""}
        </div>
        <p class="chat-insight-sub">输入：${escapeHtml(String(c.text || "（未记录）"))}</p>
        <div class="chat-auth-section-title">逐主张口径比对（判定全部由代码完成，逐字转述）</div>
        ${claimHtml}
        ${limitHtml}
        ${cardIssueFold(gaps, errors)}
        <p class="chat-insight-note muted">已执行 ${countText(counts.searches)} 次搜索 · ${countText(counts.pages)} 次读页 · ${countText(counts.llm_calls)} 次模型调用。判定全部由代码完成：只比对数字与单位口径，不判说法真假；「未核实到」只表示本次没有定位到可比对的来源数字。</p>
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ---------- 13. 档案对比卡（compare_dossiers：两份档案的只读 diff） ----------
     由后端 tools_compare 产出：card = {card_type:"comparison", status, degraded,
     left, right, similarity{suspected_same_source, basis}, conclusion_delta{changed,
     from_label, to_label, from_as_of, to_as_of}, evidence_diff{added_count,
     removed_count, added[{url,title,relation}], removed}, source_diff{added_domains,
     removed_domains}, limits, gaps}。单侧形如 {id, title, label, 置信度档位, as_of,
     versions, evidence_count, domains}，读失败 / 不存在时为 null。
     口径诚实铁律同全文件：置信度只出 high/medium/low 档位徽章（无数值分）；相似度
     只标「疑似同源」徽章，卡内不出任何相似度数值与阈值（只在 data 层留档），也不出
     风险分；标签演进只述「从 X 变为 Y（as_of …）」，不做升级/降级判断；证据与信源
     URL 是外部内容，纯文本展示；任一侧缺结论如实说「尚无结论」，不编造。 */

  /* 单侧档案：缺结论 / 缺置信度 / 缺 as_of 都如实标注，不拿默认值冒充 */
  function comparisonSide(side, name) {
    if (!side || typeof side !== "object") {
      return `<div class="chat-cmp-side">
          <div class="chat-cmp-side-name">${escapeHtml(name)}</div>
          <p class="chat-insight-sub muted">该侧读取失败或不可用（见缺口，不编造）。</p>
        </div>`;
    }
    const label = String(side.label || "");
    return `<div class="chat-cmp-side">
        <div class="chat-cmp-side-name">${escapeHtml(name)}：#${escapeHtml(String(side.id || "未知"))} 《${escapeHtml(String(side.title || "未命名"))}》</div>
        <div class="chat-cmp-side-label">${label ? escapeHtml(label) : '<span class="muted">尚无结论（该档案还没有跑过核查）</span>'}</div>
        <div class="chat-badges">
          ${confTierBadge(side)}
          <span class="badge gray">证据 ${Number(side.evidence_count) || 0} 条</span>
        </div>
        <p class="chat-ev-url">结论 as_of：${escapeHtml(String(side.as_of || "未知"))}</p>
      </div>`;
  }

  /* 证据条目：+/- 增减标记 + 立场原文 + URL 纯文本（外部内容不造可点链接） */
  function comparisonEvidenceEntry(entry, sign) {
    entry = cardObj(entry);
    const urlText = monitorUrlText(entry.url);
    return `<li><span class="chat-ev-stance">${escapeHtml(sign)}</span>` +
      `${escapeHtml(String(entry.title || entry.url || "（无标题）"))}` +
      `<span class="muted">（${escapeHtml(String(entry.relation || "立场未标注"))}）</span>` +
      (urlText ? `<div class="chat-ev-url">${urlText}</div>` : "") + "</li>";
  }

  function renderComparison(c) {
    c = cardObj(c);
    const limits = cardList(c.limits);
    const gaps = cardList(c.gaps);
    const delta = cardObj(c.conclusion_delta);
    const evDiff = cardObj(c.evidence_diff);
    const srcDiff = cardObj(c.source_diff);
    const sim = cardObj(c.similarity);
    const statusText = EVIDENCE_STATUS[String(c.status || "")] || String(c.status || "未知");

    const deltaHtml = delta.changed
      ? `<li class="chat-cmp-diff-item">标签演进：从「${escapeHtml(String(delta.from_label || ""))}」（as_of ${escapeHtml(String(delta.from_as_of || "未知"))}）变为「${escapeHtml(String(delta.to_label || ""))}」（as_of ${escapeHtml(String(delta.to_as_of || "未知"))}）</li>`
      : '<li class="chat-cmp-diff-item">标签演进：两端标签未变（或任一侧缺结论，见缺口）</li>';

    const simBadge = sim.suspected_same_source
      ? '<span class="badge amber">疑似同源（仅标注，不作同一性断言）</span>'
      : '<span class="badge gray">未见明显同源（仅标注，不作同一性断言）</span>';
    const simHtml = `<li class="chat-cmp-diff-item">指纹相似度：${simBadge}</li>` +
      (sim.basis ? `<li class="chat-cmp-diff-item muted">${escapeHtml(String(sim.basis))}</li>` : "");

    const evAdded = cardList(evDiff.added);
    const evRemoved = cardList(evDiff.removed);
    const evHtml = (c.evidence_diff === undefined || c.evidence_diff === null) ? "" : `
        <div class="chat-auth-section-title">证据增减（按 url 去重，以左档案为基准）</div>
        <ul class="chat-ev-list">
          ${evAdded.map((e) => comparisonEvidenceEntry(e, "+")).join("")}
          ${evRemoved.map((e) => comparisonEvidenceEntry(e, "-")).join("")}
        </ul>
        <p class="chat-insight-sub muted">新增 ${Number(evDiff.added_count) || 0} 条 · 减少 ${Number(evDiff.removed_count) || 0} 条</p>`;

    const addDomains = cardList(srcDiff.added_domains);
    const remDomains = cardList(srcDiff.removed_domains);
    const srcHtml = (c.source_diff === undefined || c.source_diff === null) ? "" : `
        <div class="chat-auth-section-title">信源域名增减</div>
        <p class="chat-insight-sub">新增 ${addDomains.length ? escapeHtml(addDomains.join("、")) : "无"} · 减少 ${remDomains.length ? escapeHtml(remDomains.join("、")) : "无"}</p>`;

    const limitHtml = limits.length
      ? `<div class="chat-auth-section-title">本轮的对比限制</div>` +
        `<ul class="chat-auth-limits">${limits.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>`
      : "";

    return `
      <div class="chat-card chat-card-comparison">
        <div class="chat-card-title">🆚 档案对比<span class="badge gray">只读 diff · 不重算 · 无分数</span>${degradedBadge(c)}</div>
        <div class="chat-badges">
          <span class="badge gray">${escapeHtml(statusText)}</span>
        </div>
        <div class="chat-cmp-sides">
          ${comparisonSide(c.left, "左档案")}
          ${comparisonSide(c.right, "右档案")}
        </div>
        <div class="chat-cmp-diff">
          <div class="chat-auth-section-title">结论差异</div>
          <ul class="chat-cmp-diff-list">
            ${deltaHtml}
            ${simHtml}
          </ul>
          ${evHtml}
          ${srcHtml}
        </div>
        ${limitHtml}
        ${cardIssueFold(gaps, [])}
        <p class="chat-insight-note muted">纯读取 diff：只陈述两侧档案已记录的内容，不重算、不判断真假、不做升级/降级判断；相似度只用于标注疑似同源。要核查具体说法请用事实核查 / 权威核验。</p>
        ${asOfLine(c.as_of)}
      </div>`;
  }

  /* ---------- 14. 事件时间线卡（event_timeline：按主题整理带引文事件） ----------
     由后端 tools_timeline 产出：card = {card_type:"timeline", status, task_status,
     as_of, degraded, query, state, state_label, events[{date, precision, event,
     source_url, source_title, quote, evidence_id}], undated[{event, source_url,
     source_title, quote}], range{earliest, latest}, limits, gaps, errors, counts
     {searches,pages,llm_calls,events,dropped}}。
     口径诚实铁律同全文件：引文逐字透传（折叠呈现，不改写、不补造）；日期精度只做
     中文文字徽章（day/month/year 三值封闭枚举，未知原样）；来源 URL 是外部内容，
     只做纯文本展示（与监测卡同口径，不造可点链接）；「时间不详」事件单独成区，
     不混入主线；只述有来源记录的事件，不判真假、不外推趋势。 */
  const TIMELINE_PRECISION_META = {
    day: "精确到日",
    month: "精确到月",
    year: "精确到年",
  };

  /* 时间线条目（有日期 / 时间不详共用）：引文折叠 + 来源纯文本；URL 不做可点链接 */
  function timelineEventItem(ev, dated) {
    ev = cardObj(ev);
    const meta = [];
    if (dated) {
      meta.push(`<span class="chat-timeline-date">${escapeHtml(String(ev.date || "日期未知"))}</span>`);
      meta.push(`<span class="badge gray">${escapeHtml(TIMELINE_PRECISION_META[String(ev.precision || "")] || String(ev.precision || "精度未知"))}</span>`);
    }
    if (ev.evidence_id) meta.push(`<span class="chat-ev-stance">${escapeHtml(String(ev.evidence_id))}</span>`);
    const urlText = monitorUrlText(ev.source_url);
    return `<li class="chat-timeline-item">
        <div class="chat-timeline-head">${meta.join("")}</div>
        <div class="chat-timeline-event">${escapeHtml(String(ev.event || ""))}</div>
        ${ev.quote ? `<details class="chat-evidence-fold"><summary>引文（逐字）</summary><blockquote class="chat-ev-quote">${escapeHtml(String(ev.quote))}</blockquote></details>` : ""}
        ${(ev.source_title || urlText) ? `<div class="chat-ev-url">《${escapeHtml(String(ev.source_title || "（无标题）"))}》${urlText ? ` · ${urlText}` : ""}</div>` : ""}
      </li>`;
  }

  function renderTimeline(c) {
    c = cardObj(c);
    const counts = cardObj(c.counts);
    const events = cardList(c.events);
    const undated = cardList(c.undated);
    const limits = cardList(c.limits);
    const gaps = cardList(c.gaps);
    const errors = cardList(c.errors);
    const range = cardObj(c.range);
    const statusText = EVIDENCE_STATUS[String(c.status || "")] || String(c.status || "未知");
    const taskText = EVIDENCE_TASK[String(c.task_status || "")] || "";
    const stateText = String(c.state_label || c.state || "");

    const eventsHtml = events.length
      ? `<ul class="chat-timeline-events">${events.map((ev) => timelineEventItem(ev, true)).join("")}</ul>`
      : '<p class="chat-insight-sub muted">本次没有带日期的事件（不编造）。</p>';
    const undatedHtml = undated.length ? `
        <div class="chat-timeline-undated-title">时间不详（${undated.length} 条，不混入主线）</div>
        <ul class="chat-timeline-events">${undated.map((u) => timelineEventItem(u, false)).join("")}</ul>` : "";
    const limitHtml = limits.length
      ? `<div class="chat-auth-section-title">本轮的整理限制</div>` +
        `<ul class="chat-auth-limits">${limits.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>`
      : "";

    return `
      <div class="chat-card chat-card-timeline">
        <div class="chat-card-title">🗓️ 事件时间线<span class="badge gray">只述有来源记录的事件 · 不外推</span>${degradedBadge(c)}</div>
        <div class="chat-badges">
          <span class="badge gray">${escapeHtml(statusText)}</span>
          ${taskText ? `<span class="badge gray">${escapeHtml(taskText)}</span>` : ""}
          <span class="badge gray">有日期 ${events.length} 条 · 时间不详 ${undated.length} 条</span>
          ${stateText ? `<span class="badge gray">运行状态：${escapeHtml(stateText)}</span>` : ""}
        </div>
        <p class="chat-insight-sub">主题：${escapeHtml(String(c.query || "（未记录）"))}</p>
        <p class="chat-insight-count">时间范围：${escapeHtml(String(range.earliest || "未知"))} 至 ${escapeHtml(String(range.latest || "未知"))}</p>
        ${eventsHtml}
        ${undatedHtml}
        ${limitHtml}
        ${cardIssueFold(gaps, errors)}
        <p class="chat-insight-note muted">已执行 ${countText(counts.searches)} 次搜索 · ${countText(counts.pages)} 次读页 · ${countText(counts.llm_calls)} 次模型调用；收录 ${countText(counts.events)} 条事件 · 丢弃 ${countText(counts.dropped)} 条。只述有来源记录的事件，不判真假、不外推趋势。</p>
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
    if (type === "research") return renderResearch(payload);
    if (type === "authority") return renderAuthority(payload);
    if (type === "source") return renderSource(payload);
    if (type === "guide") return renderGuide(payload);
    if (type === "statistics") return renderStatistics(payload);
    if (type === "comparison") return renderComparison(payload);
    if (type === "timeline") return renderTimeline(payload);
    if (type === "batch_summary") return renderBatchSummary(payload);
    if (type === "monitor_change") return renderMonitorChange(payload);
    if (type === "conclusion") return renderConclusion(payload);
    if (type === "speech" || type === "classic") return renderSpeech(payload);
    if (type === "heat" || type === "attention") return renderHeat(payload);
    /* 无 card_type 时按字段形态识别（各渲染器互不混淆的判据） */
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
    renderInsights, renderEvidence, renderResearch, renderAuthority,
    renderSource, renderGuide,
    renderStatistics, renderComparison, renderTimeline,
    renderBatchSummary, renderMonitorChange,
    mountCard, escapeHtml,
  };
})();
