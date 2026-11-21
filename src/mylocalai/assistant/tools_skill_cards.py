# -*- coding: utf-8 -*-
"""三个新技能工具共用的卡片/digest 组装（纯函数，不碰 IO/模型）。

为什么单独一层：research / authority / composite 三个工具都要把 pipeline 的
统一运行外壳转成前端卡片；把字段口径收口到一处，避免三份实现漂移。
铁律：引文逐字透传（不截断改写）；不新增 pipeline 没有的证据；不显示数值分；
balance/conclusion 只用 pipeline 代码生成的值，这里不改判定。
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping

__all__ = ["research_card", "authority_card", "composite_card",
           "research_digest", "authority_digest", "composite_digest"]

_BALANCE_LABELS = {
    "supported": "权威证据支持",
    "refuted": "权威证据反驳",
    "contested": "权威证据冲突",
    "insufficient": "权威证据不足",
    "not_checkable": "不可核查",
}
_LEAN_LABELS = {"supports": "倾向支持", "refutes": "倾向反驳", "mixed": "混杂", "none": "无"}
_STATUS_LABELS = {
    "completed": "已完成", "partial": "部分完成", "needs_clarification": "待补充信息",
    "failed": "执行失败", "cancelled": "已取消（保留已取得结果）",
    "budget_exhausted": "预算用尽（保留已取得结果）",
}
_TASK_LABELS = {
    "completed": "已完成", "no_factual_claim": "没有可核查事实主张",
    "needs_clarification": "待补充信息",
}


def _s(value: Any, limit: int = 0) -> str:
    text = str(value or "").strip()
    return text[:limit] if limit > 0 else text


def _list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _counts(result: Mapping[str, Any], data: Mapping[str, Any]) -> Dict[str, int]:
    audit = _dict(result.get("audit"))
    return {
        "sub_questions": len(_list(data.get("sub_questions"))),
        "claims": len(_list(data.get("claims"))),
        "evidence": len(_list(data.get("evidence"))),
        "searches": int(audit.get("searches") or 0),
        "pages": int(audit.get("pages") or 0),
        "llm_calls": int(audit.get("llm_calls") or 0),
    }


def research_card(result: Mapping[str, Any]) -> Dict[str, Any]:
    result = result if isinstance(result, Mapping) else {}
    data = _dict(result.get("data"))
    counts = _counts(result, data)
    counts["verified_sources"] = len(_list(_dict(data.get("sources")).get("verified")))
    return {
        "card_type": "research",
        "status": _s(result.get("status")),
        "task_status": _s(result.get("task_status")),
        "as_of": _s(result.get("finished_at")),
        "degraded": _s(result.get("status")) in ("partial", "failed", "cancelled",
                                                  "budget_exhausted"),
        "topic": _s(data.get("topic")),
        "scope": _dict(data.get("scope")),
        "sub_questions": _list(data.get("sub_questions")),
        "key_findings": _list(data.get("key_findings")),
        "leads": _list(data.get("leads")),
        "evidence": _list(data.get("evidence")),
        "sources": {"verified": _list(_dict(data.get("sources")).get("verified")),
                     "unverified": _list(_dict(data.get("sources")).get("unverified"))},
        "timeline": _list(data.get("timeline")),
        "conflicts": _list(data.get("conflicts")),
        "analysis": _dict(data.get("analysis")),
        "followups": _list(data.get("followups")),
        "gaps": _list(result.get("gaps")),
        "errors": _list(result.get("errors")),
        "counts": counts,
        "execution": _dict(result.get("execution")),
        "incremental": bool(result.get("incremental")),
        "base_as_of": _s(result.get("base_as_of")),
    }


def authority_card(result: Mapping[str, Any]) -> Dict[str, Any]:
    result = result if isinstance(result, Mapping) else {}
    data = _dict(result.get("data"))
    counts = _counts(result, data)
    counts["verified_sources"] = len(_list(_dict(data.get("sources")).get("verified")))
    counts["official_sources"] = len([
        s for s in _list(_dict(data.get("sources")).get("verified"))
        if _s(_dict(s).get("authority_class")) in
        ("official", "international_org", "academic", "standards", "factcheck")])
    return {
        "card_type": "authority",
        "status": _s(result.get("status")),
        "task_status": _s(result.get("task_status")),
        "as_of": _s(result.get("finished_at")),
        "degraded": _s(result.get("status")) in ("partial", "failed", "cancelled",
                                                  "budget_exhausted"),
        "claims": _list(data.get("claims")),
        "evidence": _list(data.get("evidence")),
        "sources": {"verified": _list(_dict(data.get("sources")).get("verified")),
                     "unverified": _list(_dict(data.get("sources")).get("unverified"))},
        "conflicts": _list(data.get("conflicts")),
        "analysis": _list(data.get("analysis")),
        "limitations": _list(data.get("limitations")),
        "gaps": _list(result.get("gaps")),
        "errors": _list(result.get("errors")),
        "counts": counts,
        "execution": _dict(result.get("execution")),
        "incremental": bool(result.get("incremental")),
        "base_as_of": _s(result.get("base_as_of")),
    }


def _merge_status(research: Mapping[str, Any], authority: Mapping[str, Any]) -> str:
    statuses = [_s(research.get("status")), _s(authority.get("status"))]
    if all(s == "completed" for s in statuses):
        return "completed"
    if all(s == "failed" for s in statuses):
        return "failed"
    if all(s == "cancelled" for s in statuses):
        return "cancelled"
    if all(s == "budget_exhausted" for s in statuses):
        return "budget_exhausted"
    if all(s == "needs_clarification" for s in statuses):
        return "needs_clarification"
    return "partial"


def _merge_task_status(research: Mapping[str, Any], authority: Mapping[str, Any]) -> str:
    tasks = [_s(research.get("task_status")), _s(authority.get("task_status"))]
    if "needs_clarification" in tasks:
        return "needs_clarification"
    if all(t == "no_factual_claim" for t in tasks):
        return "no_factual_claim"
    return "completed"


def composite_card(research: Mapping[str, Any], authority: Mapping[str, Any],
                   handoff: Any = None) -> Dict[str, Any]:
    research = research if isinstance(research, Mapping) else {}
    authority = authority if isinstance(authority, Mapping) else {}
    status = _merge_status(research, authority)
    task_status = _merge_task_status(research, authority)
    as_of = max(_s(research.get("as_of")), _s(authority.get("as_of")))
    counts = {
        "searches": int(_dict(research.get("counts")).get("searches") or 0)
                    + int(_dict(authority.get("counts")).get("searches") or 0),
        "pages": int(_dict(research.get("counts")).get("pages") or 0)
                 + int(_dict(authority.get("counts")).get("pages") or 0),
        "llm_calls": int(_dict(research.get("counts")).get("llm_calls") or 0)
                     + int(_dict(authority.get("counts")).get("llm_calls") or 0),
        "evidence": int(_dict(research.get("counts")).get("evidence") or 0)
                    + int(_dict(authority.get("counts")).get("evidence") or 0),
    }
    topic = _s(research.get("topic"))
    claims = _list(authority.get("claims"))
    balances = [_s(_dict(c).get("balance")) for c in claims]
    summary = f"资料收集主题：{topic or '（未命名）'}；权威核验主张 {len(claims)} 条"
    if balances:
        summary += "（" + "、".join(_BALANCE_LABELS.get(b, b) for b in balances) + "）"
    return {
        "card_type": "composite",
        "kind": "research_authority",
        "status": status,
        "task_status": task_status,
        "as_of": as_of,
        "degraded": status in ("partial", "failed", "cancelled", "budget_exhausted"),
        "summary": summary,
        "research": research,
        "authority": authority,
        "handoff": _dict(handoff),
        "gaps": _list(research.get("gaps")) + _list(authority.get("gaps")),
        "errors": _list(research.get("errors")) + _list(authority.get("errors")),
        "counts": counts,
        "incremental": bool(research.get("incremental") or authority.get("incremental")),
        "base_as_of": _s(research.get("base_as_of") or authority.get("base_as_of")),
    }


def research_digest(card: Mapping[str, Any]) -> str:
    card = card if isinstance(card, Mapping) else {}
    counts = _dict(card.get("counts"))
    lines = ["[资料收集] 只述资料，不判真假（只读、不建档）",
             f"- 执行状态：{_STATUS_LABELS.get(_s(card.get('status')), _s(card.get('status')) or '未知')}；"
             f"任务状态：{_TASK_LABELS.get(_s(card.get('task_status')), _s(card.get('task_status')) or '未知')}",
             f"- 主题：{_s(card.get('topic')) or '（未命名）'}；"
             f"子问题 {counts.get('sub_questions', 0)} 个；关键发现 {len(_list(card.get('key_findings')))} 条；"
             f"证据 {counts.get('evidence', 0)} 条"]
    if card.get("incremental"):
        lines.append(f"- 增量结果：基于 {_s(card.get('base_as_of')) or '上一张卡'}")
    for finding in _list(card.get("key_findings"))[:3]:
        finding = _dict(finding)
        lines.append(f"- 发现 {_s(finding.get('finding_id'))}：{_s(finding.get('statement'), 160)}"
                     f"（证据 {'、'.join(_s(x) for x in _list(finding.get('evidence_ids'))) or '无'}）")
    gaps = _list(card.get("gaps"))
    if gaps:
        lines.append("- 缺口前 3 条：" + "；".join(_s(g, 120) for g in gaps[:3]))
    lines.append(f"- 已执行 {int(counts.get('searches') or 0)} 次搜索、"
                 f"{int(counts.get('pages') or 0)} 次读页、"
                 f"{int(counts.get('llm_calls') or 0)} 次模型调用")
    return "\n".join(lines)


def authority_digest(card: Mapping[str, Any]) -> str:
    card = card if isinstance(card, Mapping) else {}
    counts = _dict(card.get("counts"))
    lines = ["[权威核验] 只列权威证据与证据倾向；结论附范围/时点，不显示数值分",
             f"- 执行状态：{_STATUS_LABELS.get(_s(card.get('status')), _s(card.get('status')) or '未知')}；"
             f"任务状态：{_TASK_LABELS.get(_s(card.get('task_status')), _s(card.get('task_status')) or '未知')}",
             f"- 主张 {len(_list(card.get('claims')))} 条；证据 {counts.get('evidence', 0)} 条；"
             f"权威来源 {counts.get('official_sources', 0)} 个"]
    for claim in _list(card.get("claims"))[:3]:
        claim = _dict(claim)
        lines.append(f"- {_s(claim.get('claim_id'))}：{_s(claim.get('text'), 100)} → "
                     f"{_s(claim.get('conclusion'), 180)}")
    gaps = _list(card.get("gaps"))
    if gaps:
        lines.append("- 缺口前 3 条：" + "；".join(_s(g, 120) for g in gaps[:3]))
    lines.append(f"- 已执行 {int(counts.get('searches') or 0)} 次搜索、"
                 f"{int(counts.get('pages') or 0)} 次读页、"
                 f"{int(counts.get('llm_calls') or 0)} 次模型调用")
    return "\n".join(lines)


def composite_digest(card: Mapping[str, Any]) -> str:
    card = card if isinstance(card, Mapping) else {}
    lines = ["[资料收集 + 权威核验] 复合结果（只读、不建档）",
             f"- 执行状态：{_STATUS_LABELS.get(_s(card.get('status')), _s(card.get('status')) or '未知')}",
             f"- {_s(card.get('summary'))}"]
    handoff = _dict(card.get("handoff"))
    if handoff.get("auto"):
        lines.append("- 自动接力：" + (_s(handoff.get("reason")) or "研究引文自动核验"))
    gaps = _list(card.get("gaps"))
    if gaps:
        lines.append("- 缺口前 3 条：" + "；".join(_s(g, 120) for g in gaps[:3]))
    return "\n".join(lines)
