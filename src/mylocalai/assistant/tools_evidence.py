# -*- coding: utf-8 -*-
"""取证三技能流水线的只读工具封装（总览 §5 / 第二波契约 §2）：collect_evidence。

改了什么、为什么、锁定什么：
  - 新增 `assistant.tools_evidence`：把 `EvidenceSkills.run`（领域识别 → 信源发现 →
    证据检索三阶段）接进助手工具层，注册为 `permission="read"` 的 collect_evidence
    （不建档、不出真假分——本工具没有任何分数字段）；
  - 死线/取消透传（总览 §4.3"等不及随时取消 + 死线"）：handler 把 `ctx.cancel_check`
    与 `llm_cache.deadline_at()`（回合预算，嵌套取更紧者）原样交给流水线；死线取不到
    就传 None，由流水线按 rules.total_deadline_s 自建；
  - 卡片即用户可见的逐字证据面：evidence 卡的引文逐字来自 `result.data.evidence`
    （不截断、不改写）；叙述层不得新增证据——digest 只做结构化要点转述；
  - 永不抛异常（与 tools_verify 的降级文化一致）：服务工厂取不到 / 装配炸 / run 炸 /
    返回坏形状，一律折成 ok=False + 诚实 digest，会话不崩。

三条铁律：不判真假只述证据（本工具输出无任何真假判定词）；大模型不参与打分（分类/选站/
拆主张/抽引文全部由流水线内的代码白名单与定位校验收口）；每个出口都有降级（六种 status
都是合法出口，空 claims / 空 evidence 给空列表，绝不为有输出而编）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mylocalai.assistant.registry import ToolRegistry, text_param

__all__ = ["register_all"]

#: 合法出口（契约 §2.4）：六种 status 里只有这三个算 ok=False；no_factual_claim 是
#: task_status（伴随 status=completed），不是失败。
_FAILED_STATUSES = ("cancelled", "budget_exhausted", "failed")
#: degraded 口径：partial 及以上都如实标注降级；completed/needs_clarification 不算。
_DEGRADED_STATUSES = ("partial", "failed", "cancelled", "budget_exhausted")

#: 证据关系封闭枚举的中文标签（与 skills.pipeline 的渲染映射同文）。工具层自带一份而
#: 不 import pipeline 的私有映射：渲染层将来改文案不该静默改掉工具层 digest 口径。
_RELATION_LABELS = {"supports": "支持", "refutes": "反驳", "partial": "部分一致",
                    "background": "背景", "unclear": "关系不明"}

_QUOTE_PREVIEW = 3   # digest 里逐字带出的引文条数（契约 §2.4：前 3 条）


def _relation_label(relation: str) -> str:
    return _RELATION_LABELS.get(str(relation or ""), str(relation or "") or "关系不明")


def _deadline() -> Optional[float]:
    """当前线程的绝对死线（回合预算，time.monotonic 基准）；未设预算返回 None。"""
    try:
        from mylocalai.llm import llm_cache
        return llm_cache.deadline_at()
    except Exception:  # noqa: BLE001  死线是增益约束：取不到就不设，流水线自建
        return None


def _skills(ctx: Any) -> Any:
    """经 ctx.services 取取证流水线工厂并实例化（服务是工厂，契约 §2.4-1）。"""
    services = getattr(ctx, "services", None)
    if services is None:
        raise KeyError("ctx.services 缺失")
    factory = services["evidence_skills"]
    skills = factory() if callable(factory) else factory
    if skills is None or not hasattr(skills, "run"):
        raise TypeError("evidence_skills 服务未返回可运行的流水线实例")
    return skills


def _unavailable(reason: str) -> Dict[str, Any]:
    """降级出口（契约 §2.5）：装配/执行失败 → ok=False + 诚实 digest，不崩不编。"""
    detail = str(reason or "").strip()[:200] or "原因未知"
    return {"ok": False, "card": None,
            "digest": f"[取证流水线] 不可用：{detail}。本次未取得任何证据，"
                      "我不会替它补一个结论。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True,
            "status": "failed", "task_status": "", "evidence_count": 0,
            "claims": [], "report": ""}


def _source_view(source: Dict[str, Any]) -> Dict[str, Any]:
    """信源紧凑视图（cards.js renderEvidence 的字段口径）。"""
    return {"source_id": str(source.get("source_id") or ""),
            "name": str(source.get("name") or ""),
            "domain": str(source.get("domain") or ""),
            "tier": str(source.get("tier") or ""),
            "verification": str(source.get("verification") or "")}


def _evidence_view(item: Dict[str, Any]) -> Dict[str, Any]:
    """证据紧凑视图：quote 逐字透传（截断改写会让"可定位引文"失去意义）。"""
    return {"evidence_id": str(item.get("evidence_id") or ""),
            "claim_ids": [str(c) for c in (item.get("claim_ids") or [])],
            "relation": str(item.get("relation") or ""),
            "source_id": str(item.get("source_id") or ""),
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "quote": str(item.get("quote") or ""),
            "fetched_at": str(item.get("fetched_at") or "")}


def _evidence_card(result: Dict[str, Any]) -> Dict[str, Any]:
    """evidence 卡（契约 §2.4-4）：确定性报告的结构化承载，字段全部来自运行结果。"""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    domain = data.get("domain") if isinstance(data.get("domain"), dict) else {}
    sources = [s for s in (data.get("sources") or []) if isinstance(s, dict)]
    claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
    evidence = [e for e in (data.get("evidence") or []) if isinstance(e, dict)]
    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    status = str(result.get("status") or "")
    return {
        "card_type": "evidence",
        "status": status,
        "task_status": str(result.get("task_status") or ""),
        "as_of": str(result.get("finished_at") or ""),
        "degraded": status in _DEGRADED_STATUSES,
        "domain": {"primary": str(domain.get("primary") or ""),
                   "secondary": [str(x) for x in (domain.get("secondary") or [])],
                   "region": str(domain.get("region") or ""),
                   "labels": [str(x) for x in (domain.get("labels") or [])]},
        "sources": {"verified": [_source_view(s) for s in sources if s.get("verified")],
                    "unverified": [_source_view(s) for s in sources if not s.get("verified")]},
        "claims": [{"claim_id": str(c.get("claim_id") or ""),
                    "text": str(c.get("text") or "")} for c in claims],
        "evidence": [_evidence_view(e) for e in evidence],
        "counts": {"claims": len(claims),
                   "verified_sources": len([s for s in sources if s.get("verified")]),
                   "evidence": len(evidence),
                   "searches": int(audit.get("searches") or 0),
                   "pages": int(audit.get("pages") or 0),
                   "llm_calls": int(audit.get("llm_calls") or 0)},
        "gaps": [str(g) for g in (result.get("gaps") or [])],
        "errors": [{"stage": str(e.get("stage") or ""),
                    "category": str(e.get("category") or ""),
                    "detail": str(e.get("detail") or "")} for e in errors],
    }


def _evidence_digest(result: Dict[str, Any], card: Dict[str, Any]) -> str:
    """叙述层要点（契约 §2.4-4 的 digest 口径）：只转述流水线产物，不新增证据。

    前 3 条引文逐字带出——叙述模型唯一的证据来源就是这里 + 卡片，它不得自行补充。
    """
    counts = card["counts"]
    lines: List[str] = ["[取证流水线] 只述证据，不判真假（不建档、无分数）"]
    lines.append(f"- 执行状态：{card['status'] or '（未知）'}；"
                 f"任务状态：{card['task_status'] or '（未知）'}")
    domain = card["domain"]
    label = domain["primary"] or "（未识别）"
    if domain["labels"]:
        label += "（标签：" + "、".join(domain["labels"]) + "）"
    lines.append(f"- 领域：{label}")
    lines.append(f"- 主张 {counts['claims']} 条；已核验信源 {counts['verified_sources']} 个；"
                 f"证据 {counts['evidence']} 条")
    if card["evidence"]:
        grouped: Dict[str, int] = {}
        for item in card["evidence"]:
            key = str(item.get("relation") or "unclear")
            grouped[key] = grouped.get(key, 0) + 1
        lines.append("- 证据关系分布：" + "、".join(
            f"{_relation_label(k)} {v} 条" for k, v in sorted(grouped.items())))
    gaps = card["gaps"]
    lines.append("- 缺口前 3 条：" + ("；".join(gaps[:_QUOTE_PREVIEW]) if gaps else "（无）"))
    errors = card["errors"]
    lines.append("- 错误前 3 条：" + ("；".join(
        f"[{e['stage']}] {e['category']}：{e['detail']}" for e in errors[:_QUOTE_PREVIEW])
        if errors else "（无）"))
    lines.append(f"- 已执行 {counts['searches']} 次搜索、{counts['pages']} 次读页、"
                 f"{counts['llm_calls']} 次模型调用")
    lines.append(f"- 前 {_QUOTE_PREVIEW} 条证据引文（逐字；叙述层不得新增证据）：")
    if card["evidence"]:
        for item in card["evidence"][:_QUOTE_PREVIEW]:
            claim_ids = "、".join(str(c) for c in (item.get("claim_ids") or [])) or "（未关联主张）"
            lines.append(
                f"  · {item['evidence_id']}（主张 {claim_ids}，关系 "
                f"{_relation_label(item.get('relation'))}，信源 {item['source_id'] or '未知'}）："
                f"「{item['quote']}」——《{item['title'] or '（无标题）'}》{item['url']}"
                f"（抓取于 {item['fetched_at'] or '未注明'}）")
    else:
        lines.append("  （本次没有可定位引文）")
    return "\n".join(lines)


def _result(result: Dict[str, Any]) -> Dict[str, Any]:
    """运行结果 → 统一工具返回（与 tools_verify._result 同构 + 业务字段）。"""
    card = _evidence_card(result)
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
    status = card["status"]
    # 粗口径成本：LLM 调用 + 检索次数（M4 真实计数由 loop 的 delta 覆盖）
    requests = int(audit.get("llm_calls") or 0) + int(audit.get("searches") or 0)
    return {"ok": status not in _FAILED_STATUSES,
            "card": card,
            "digest": _evidence_digest(result, card),
            "cost": {"requests": requests, "tokens": 0},
            "dossier_id": None,   # 只读取证：不建档（铁律：不扩权）
            "degraded": status in _DEGRADED_STATUSES,
            "status": status,
            "task_status": card["task_status"],
            "evidence_count": card["counts"]["evidence"],
            "claims": [{"claim_id": str(c.get("claim_id") or ""),
                        "text": str(c.get("text") or "")[:120]} for c in claims],
            "report": str(result.get("report") or "")}


def _collect_evidence(ctx: Any, text: str) -> Dict[str, Any]:
    """collect_evidence：三技能流水线（领域 → 信源 → 证据），只读、不建档、不出真假分。"""
    try:
        skills = _skills(ctx)
    except Exception as exc:  # noqa: BLE001  工厂缺失/装配失败 = 诚实降级，会话不崩
        return _unavailable(f"取证流水线装配失败（{type(exc).__name__}）")
    try:
        result = skills.run(str(text),
                            cancel_check=getattr(ctx, "cancel_check", None),
                            deadline=_deadline())
    except Exception as exc:  # noqa: BLE001  流水线自带"永不抛"外壳，这里是二道闸
        return _unavailable(f"取证流水线执行异常（{type(exc).__name__}）")
    if not isinstance(result, dict):
        return _unavailable("取证流水线返回形状不符")
    return _result(result)


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册取证三技能流水线的只读工具（services 形参约定同 tools_verify.register_all）。"""
    registry.register("collect_evidence", _collect_evidence, params=["text"],
                      permission="read", cost="heavy", est_s=60.0,
                      description="只读取证：把一段内容拆成主张，逐个去已核验信源找可定位引文"
                                  "（不建档、不出真假分）",
                      validators={"text": text_param()})
