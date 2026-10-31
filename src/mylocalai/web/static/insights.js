/* ============================================================
   insights.js —— 参谋 Tab（L4）：话术模式库 / 信源画像 / 本周简报
   独立文件，不 import 其他文件的符号：escapeHtml 在本文件内重写一份，
   不引用 ChatCards / chat.js；渲染目标只有 index.html 的
   #insightPatterns / #insightSources / #insightWeekly 三个容器。
   数据来源（零 LLM、纯只读聚合，后端 dossier.insights 纯函数产出）：
     GET /api/insights/patterns | /api/insights/sources | /api/insights/weekly?days=7
   任一端点 fetch 失败 / 非 200 → 对应块显示"暂无数据"（不弹错、不阻塞其他块）。
   由 chat.js 的 setTab 切入参谋 Tab 时调 window.InsightsPanel.refresh()
   （调用方带存在性守卫）。
   ============================================================ */
"use strict";

(function () {
  /* file:// 双击打开时没有后端，静默退出（与 chat.js 同一口径） */
  if (location.protocol === "file:") return;

  /* 后端接口清单（与 web/app.py 路由表一一对应，测试做前后端自洽断言） */
  var INSIGHTS_API = {
    patterns: "/api/insights/patterns",
    sources: "/api/insights/sources",
    weekly: "/api/insights/weekly?days=7"
  };

  /* 与 cards.js 同一份实现，按"独立文件"约定在本文件内重写（不引用外部符号） */
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function box(id) {
    return document.getElementById(id);
  }

  function renderInto(id, html) {
    var el = box(id);
    if (el) el.innerHTML = html;
  }

  function emptyBox() {
    return '<p class="insight-empty">暂无数据</p>';
  }

  function loadingBox() {
    return '<p class="insight-empty">加载中…</p>';
  }

  function fmtPct(share) {
    var n = Number(share);
    return Number.isFinite(n) ? Math.round(n * 100) + "%" : "—";
  }

  function when(s) {
    return escapeHtml(String(s || "").replace("T", " ") || "—");
  }

  /* 高风险类目标签口径（与引擎 verdict 的标签一致；by_label 里命中的才计入） */
  var HIGH_RISK_LABELS = ["高风险谣言", "疑似谣言"];

  /* ---------- 1. 话术模式库 ---------- */
  function renderPatterns(p) {
    p = (p && typeof p === "object") ? p : null;
    if (!p) return emptyBox();
    var risks = Array.isArray(p.risk_patterns) ? p.risk_patterns : [];
    var pairs = Array.isArray(p.common_pairs) ? p.common_pairs : [];
    if (!Number(p.total_dossiers) && !Number(p.with_markers) && !risks.length && !pairs.length) {
      return emptyBox();
    }
    var labelMap = p.by_label || {};
    var labelBits = Object.keys(labelMap).map(function (k) {
      return escapeHtml(k) + " ×" + Number(labelMap[k] || 0);
    });
    var html = '<p class="insight-count">有结论档案 <b>' + Number(p.total_dossiers) +
      '</b> 篇 · 含话术标记 <b>' + Number(p.with_markers) + '</b> 篇</p>' +
      '<p class="insight-sub muted">结论分布：' +
      (labelBits.length ? labelBits.join(" · ") : "暂无数据") + '</p>';
    if (risks.length) {
      var items = risks.map(function (rp) {
        var lab = rp.by_label || {};
        var hot = HIGH_RISK_LABELS.reduce(function (sum, name) {
          return sum + (Number(lab[name]) || 0);
        }, 0);
        return '<li><span class="insight-rank-label">' + escapeHtml(rp.label || rp.key || "（未命名类目）") +
          '</span><span class="insight-rank-num">' + Number(rp.dossiers) + ' 条 · 占' +
          fmtPct(rp.share) + ' · 其中高风险/疑似 ' + hot + ' 条</span></li>';
      }).join("");
      html += '<div class="insight-rank-title">高风险话术榜单（按命中档案数）</div>' +
        '<ul class="insight-rank">' + items + '</ul>';
    }
    if (pairs.length) {
      var pairItems = pairs.map(function (pair) {
        var labels = Array.isArray(pair.labels) ? pair.labels : [];
        var keys = Array.isArray(pair.keys) ? pair.keys : [];
        var a = labels[0] || keys[0] || "？";
        var b = labels[1] || keys[1] || "？";
        return '<li>' + escapeHtml(a) + ' + ' + escapeHtml(b) +
          '<span class="insight-rank-num">' + Number(pair.dossiers) + ' 条</span></li>';
      }).join("");
      html += '<div class="insight-rank-title">常一起出现的话术组合</div>' +
        '<ul class="insight-rank">' + pairItems + '</ul>';
    }
    return html;
  }

  /* ---------- 2. 信源画像 ---------- */
  function renderSources(profiles) {
    if (!Array.isArray(profiles) || !profiles.length) return emptyBox();
    var rows = profiles.map(function (r) {
      var badge = r.qualified ? '<span class="badge green">个人增信生效</span>' : '<span class="badge gray">未达标</span>';
      return '<tr><td>' + escapeHtml(r.domain || "—") + '</td>' +
        '<td>' + escapeHtml(r.tier || "—") + '</td>' +
        '<td>' + Number(r.total_entries) + '</td>' +
        '<td>' + Number(r.refute_entries) + '</td>' +
        '<td>' + Number(r.refuted_dossiers) + '</td>' +
        '<td>' + when(r.last_seen) + '</td>' +
        '<td>' + badge + '</td></tr>';
    }).join("");
    return '<table class="insight-table"><thead><tr>' +
      '<th>域名</th><th>档位</th><th>条目数</th><th>反驳条目</th><th>证伪档案数</th><th>最近出现</th><th>增信</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table>' +
      '<p class="insight-note muted">画像来自你自己的档案史，仅在历史证伪≥阈值时参与抬分；只述计数，不给任何站点打真假分。</p>';
  }

  /* ---------- 3. 本周简报 ---------- */
  function renderWeekly(d) {
    d = (d && typeof d === "object") ? d : null;
    if (!d) return emptyBox();
    var period = d.period || {};
    var doss = d.dossiers || {};
    var concl = d.conclusions || {};
    var runs = d.runs || {};
    var tool = d.tool_runs || {};
    var heat = d.heat || {};
    if (!Number(period.days) && !Number(doss.new_count) && !Number(heat.snapshot_count)) {
      return emptyBox();
    }
    var cmap = concl.by_label || {};
    var conBits = Object.keys(cmap).map(function (k) {
      return escapeHtml(k) + " ×" + Number(cmap[k] || 0);
    });
    var html = '<p class="insight-count">' + when(period.start) + ' ~ ' + when(period.end) +
      '（' + Number(period.days) + ' 天）</p>' +
      '<ul class="insight-facts">' +
      '<li>新档案 <b>' + Number(doss.new_count) + '</b> 篇；新结论：' +
      (conBits.length ? conBits.join(" · ") : "暂无数据") + '</li>' +
      '<li>分析运行 <b>' + Number(runs.count) + '</b> 次 · 成本 ' + Number(runs.requests) +
      ' 次调用 / ' + Number(runs.tokens) + ' tokens；工具调用 <b>' + Number(tool.count) +
      '</b> 次 · 成本 ' + Number(tool.requests) + ' / ' + Number(tool.tokens) + ' tokens</li>' +
      '<li>热度快照 <b>' + Number(heat.snapshot_count) + '</b> 次</li>' +
      '</ul>' +
      (d.as_of ? '<p class="insight-sub muted">数据截至 ' + when(d.as_of) + '</p>' : "");
    var top = (d.patterns || {}).top || [];
    if (top.length) {
      html += '<div class="insight-rank-title">本周高频话术</div><ul class="insight-rank">' +
        top.map(function (tp) {
          return '<li><span class="insight-rank-label">' + escapeHtml(tp.label || tp.key || "（未命名类目）") +
            '</span><span class="insight-rank-num">' + Number(tp.dossiers) + ' 条</span></li>';
        }).join("") + '</ul>';
    }
    var refutes = (d.sources || {}).top_refute || [];
    if (refutes.length) {
      html += '<div class="insight-rank-title">本周反驳条目最多的信源</div><ul class="insight-rank">' +
        refutes.map(function (tr) {
          return '<li>' + escapeHtml(tr.domain || "—") +
            '<span class="insight-rank-num">' + Number(tr.refute_entries) + ' 条反驳</span></li>';
        }).join("") + '</ul>';
    }
    return html;
  }

  /* ---------- 拉取与装配 ---------- */
  function fetchJson(url) {
    return fetch(url).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    });
  }

  function refresh() {
    renderInto("insightPatterns", loadingBox());
    renderInto("insightSources", loadingBox());
    renderInto("insightWeekly", loadingBox());
    return Promise.all([
      fetchJson(INSIGHTS_API.patterns).then(function (data) {
        renderInto("insightPatterns", renderPatterns(data && data.patterns));
      }).catch(function () {
        renderInto("insightPatterns", emptyBox());
      }),
      fetchJson(INSIGHTS_API.sources).then(function (data) {
        renderInto("insightSources", renderSources(data && data.profiles));
      }).catch(function () {
        renderInto("insightSources", emptyBox());
      }),
      fetchJson(INSIGHTS_API.weekly).then(function (data) {
        renderInto("insightWeekly", renderWeekly(data && data.digest));
      }).catch(function () {
        renderInto("insightWeekly", emptyBox());
      }),
    ]);
  }

  /* 供 chat.js 的 setTab 调用的唯一入口（不污染全局命名空间） */
  window.InsightsPanel = { refresh: refresh };
})();
