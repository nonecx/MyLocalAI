# -*- coding: utf-8 -*-
"""把 RumorAgent 的结构化结果格式化成终端可读的报告。"""

from __future__ import annotations

from typing import Any, Dict

DISCLAIMER = (
    "本系统结合谣言特征分析与联网证据检索给出风险参考；证据以搜索结果摘要为主、"
    "原文抓取为辅，可能遗漏或误判来源，风险分未经标注数据校准。"
    "结论仅供参考，重要信息请以权威渠道核实为准。"
)

_MODE_NOTE = {
    "agent": "",
    "pipeline": "（降级模式：大模型 Agent 不可用，已改用直接流水线）",
    "timeout": "（降级模式：Agent 超时，仅使用已完成的分析）",
    "none": "",
}


def _evidence_lines(r: Dict[str, Any]) -> list:
    """证据本段落：无证据本/跳过时返回空列表。"""
    book = r.get("evidence_book")
    if not book:
        return []
    if book.get("skipped"):
        return ["", "📚 【证据本】 " + str(book["skipped"])]
    entries = book.get("entries") or []
    if not entries:
        return ["", "📚 【证据本】 未能获取搜索证据（" + str(book.get("error") or "无结果") + "）"]
    queries = "、".join(str(q.get("query") if isinstance(q, dict) else q)
                        for q in (book.get("queries") or []))
    lines = ["", f"📚 【证据本】 检索：{queries}"]
    if book.get("scope") == "event":
        lines.append(f"   检索范围：具体事件"
                     + (f"「{book['scope_event']}」" if book.get("scope_event") else "")
                     + f"（{book.get('scope_filter') or '已按事件范围过滤'}）")
    elif book.get("scope") == "phenomenon":
        lines.append("   检索范围：泛现象（不做事件范围限制）")
    lines.append(f"   证据倾向：{book.get('verdict_hint', '')}")
    if book.get("excluded_not_event"):
        lines.append(f"   （已排除 {len(book['excluded_not_event'])} 条同主题但非该事件的资料）")
    if book.get("filtered_out"):
        lines.append(f"   （已过滤 {book['filtered_out']} 条与主张无关的信息）")
    for e in entries[:6]:
        kind = e.get("source_kind") or e.get("domain_tier") or ""
        date = f" · {e['date']}" if e.get("date") else ""
        lines.append(f"  · [{e.get('stance')}]（{kind}{date}）{(e.get('title') or '')[:44]}")
        lines.append(f"      {e.get('url', '')[:80]}")
    return lines


def format_report(r: Dict[str, Any]) -> str:
    lines = [
        f"🛡️ 【谣言识别结论】 {r['label']}（风险分 {r['risk_score']}/100，置信度{r['confidence']}）"
        + _MODE_NOTE.get(r.get("mode", ""), ""),
        f"   言论分层：{r['speech_tier']}",
        "",
        "🔍 【风险信号】",
    ]
    lines += [f"  · {s}" for s in r["signals"]] or ["  · （无）"]
    if r.get("claims"):
        lines += ["", "📌 【待核实的事实声明】"]
        for c in r["claims"]:
            src = "有来源" if c.get("has_source") else "无来源"
            lines.append(f"  · {c['claim']}（{src}，{c['verifiability']}）")
    lines += _evidence_lines(r)
    bd = r.get("score_breakdown") or {}
    if bd:
        lines += ["", "📐 【评分构成】 " + "，".join(f"{k} {v:+d}" if k != "封顶前合计" else f"{k} {v}" for k, v in bd.items())]
    lines += ["", "💬 【智能体解释】", f"  {r['explanation']}", "", f"✅ 【核实建议】 {r['advice']}"]
    if r.get("degraded_components"):
        lines += ["", "⚠️ 降级组件：" + "、".join(r["degraded_components"])]
    if r.get("truncated"):
        lines += ["", "⚠️ 文本过长，已截断后分析。"]
    lines += ["", f"⚠️ 【免责声明】 {DISCLAIMER}",
              f"（模式 {r['mode']} · LLM 轮次 {r['llm_turns']} · 调用工具 {len(r['tools_called'])} 次 · 耗时 {r['elapsed_s']}s）"]
    return "\n".join(lines)
