# -*- coding: utf-8 -*-
"""权威核验的只读工具封装（第三波契约 §6.2）：verify_authority。

改了什么、为什么、锁定什么：
   - 新增 `assistant.tools_authority`：把 `AuthoritySkills.run`（拆主张 → 权威源
     发现 → 逐主张取证 → 代码算 balance/lean → 受约束分析）接进助手工具层，注册为
     `permission="read"` 的 verify_authority（不建档、无分数——本工具没有任何
     分数字段；balance/lean 全由流水线内的纯函数代码计算，大模型不参与判定）；
   - 死线/取消/进度透传：handler 把 `ctx.cancel_check`、`llm_cache.deadline_at()`
     （回合预算，嵌套取更紧者）与 `_progress_emitter(ctx.events)` 原样交给流水线；
     死线取不到就传 None，由流水线按 rules.total_deadline_s 自建；
   - 卡片即用户可见的逐字证据面：引文逐字来自 `result.data.evidence`（不截断、
     不改写）；digest 只能转述 conclusion/引文/计数，不得新增任何判断；
   - 永不抛异常（与 tools_evidence 的降级文化一致）：服务工厂取不到 / 装配炸 /
     run 炸 / 返回坏形状，一律折成 ok=False + 诚实 digest，会话不崩。

三条铁律：不判真假只述证据（conclusion 只允许契约 §5.4 的模板句式："权威资料
可以证明/直接反驳/不能据此证明或反驳"，digest 不出现"真/假/属实"固定标签）；
大模型不参与判定（来源等级/动态权威信号/balance/lean/状态全由代码计算，LLM 只做
拆解、选源建议、摘录、受约束分析）；每个出口都有降级（六种 status 都是合法出口，
无决定性证据 → insufficient/completed，空 claims 给空列表，绝不为有输出而编）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from mylocalai.assistant.registry import ToolRegistry, text_param

__all__ = ["register_all"]

#: 合法出口（契约 §6.2）：六种 status 里只有这三个算 ok=False；needs_clarification
#: （主张超限等）与 no_factual_claim 都是合法空出口，不算失败。
_FAILED_STATUSES = ("cancelled", "budget_exhausted", "failed")
#: degraded 口径：partial 及以上都如实标注降级；completed/needs_clarification 不算。
_DEGRADED_STATUSES = ("partial", "failed", "cancelled", "budget_exhausted")

#: balance 封闭枚举的中文标签（契约 §5.4 结论模板同族文案）。工具层自带一份而
#: 不 import pipeline 的私有映射：渲染层将来改文案不该静默改掉工具层 digest 口径；
#: 流水线自带 balance_label 时以其为准（管道产物优先，映射只是缺省兜底）。
_BALANCE_LABELS = {"supported": "权威证据支持", "refuted": "权威证据反驳",
                   "contested": "权威证据冲突", "insufficient": "权威证据不足",
                   "not_checkable": "不可核查"}
#: lean 封闭枚举的中文标签（只用于"倾向"展示，不能单独升级为证明/反驳）。
_LEAN_LABELS = {"supports": "支持", "refutes": "反驳", "mixed": "冲突", "none": "无"}

#: 权威类来源（collect_core.AUTHORITY_CLASSES 里除 media/unverified 的子集）：
#: counts.official_sources 的口径——按**代码判定的来源类别**计数，不按页面自称。
_OFFICIAL_CLASSES = ("official", "international_org", "academic", "standards", "factcheck")

_QUOTE_PREVIEW = 3   # digest 里逐字带出的主张/引文条数（契约 §6.2：前 3 条）
_QUOTES_PER_SIDE = 2   # 每条主张在 digest 里逐字带出的支持/反驳引文条数


def _deadline() -> Optional[float]:
    """当前线程的绝对死线（回合预算，time.monotonic 基准）；未设预算返回 None。"""
    try:
        from mylocalai.llm import llm_cache
        return llm_cache.deadline_at()
    except Exception:  # noqa: BLE001  死线是增益约束：取不到就不设，流水线自建
        return None


def _progress_emitter(events: Any) -> Optional[Callable[[str, int, int], None]]:
    """把流水线的 on_progress(stage, done, total) 转成 SSE progress 事件源。

    component 用中文阶段名（pipeline 契约，共 4 步）：拆分主张 / 核验权威来源 /
    阅读取证 / 整理分析——工具层只透传，不改写阶段名；events 为 None（MCP/同步
    调用没有事件流）时返回 None，流水线对 None 回调逐位不变。
    """
    if events is None:
        return None

    def _on_progress(stage: Any, done: Any, total: Any) -> None:
        try:
            events.emit("progress", component=str(stage), done=int(done),
                        total=int(total))
        except Exception:  # noqa: BLE001  进度事件失败不影响主结论
            pass
    return _on_progress


def _skills(ctx: Any) -> Any:
    """经 ctx.services 取权威核验流水线工厂并实例化（服务是工厂，契约 §6.2）。"""
    services = getattr(ctx, "services", None)
    if services is None:
        raise KeyError("ctx.services 缺失")
    factory = services["authority_skills"]
    skills = factory() if callable(factory) else factory
    if skills is None or not hasattr(skills, "run"):
        raise TypeError("authority_skills 服务未返回可运行的流水线实例")
    return skills


def _unavailable(reason: str) -> Dict[str, Any]:
    """降级出口（契约 §6.2）：装配/执行失败 → ok=False + 诚实 digest，不崩不编。"""
    detail = str(reason or "").strip()[:200] or "原因未知"
    return {"ok": False, "card": None,
            "digest": f"[权威核验] 不可用：{detail}。本次未取得任何证据，"
                      "我不会替它补一个倾向。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True,
            "status": "failed", "task_status": "", "not_saved": True,
            "save_reason": "read_only_capability_no_dossier", "report": ""}


def _ids(value: Any) -> List[str]:
    return [str(x) for x in (value or []) if str(x or "").strip()]


def _source_view(source: Dict[str, Any]) -> Dict[str, Any]:
    """来源紧凑视图（cards.js renderAuthority 的字段口径；verified 决定分级区）。"""
    return {"source_id": str(source.get("source_id") or ""),
            "name": str(source.get("name") or ""),
            "domain": str(source.get("domain") or ""),
            "tier": str(source.get("tier") or ""),
            "authority_class": str(source.get("authority_class") or ""),
            "verification": str(source.get("verification") or ""),
            "verification_basis": str(source.get("verification_basis") or ""),
            "verified": bool(source.get("verified"))}


def _evidence_view(item: Dict[str, Any]) -> Dict[str, Any]:
    """证据紧凑视图：quote 逐字透传（截断改写会让"可定位引文"失去意义）。"""
    span = item.get("quote_span")
    return {"evidence_id": str(item.get("evidence_id") or ""),
            "claim_ids": _ids(item.get("claim_ids")),
            "relation": str(item.get("relation") or ""),
            "source_id": str(item.get("source_id") or ""),
            "name": str(item.get("name") or ""),
            "domain": str(item.get("domain") or ""),
            "tier": str(item.get("tier") or ""),
            "authority_class": str(item.get("authority_class") or ""),
            "verification": str(item.get("verification") or ""),
            "authority_basis": str(item.get("authority_basis") or ""),
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "quote": str(item.get("quote") or ""),
            "quote_span": {"start": int(span.get("start") or 0),
                           "end": int(span.get("end") or 0)} if isinstance(span, dict) else None,
            "published_at": str(item.get("published_at") or ""),
            "fetched_at": str(item.get("fetched_at") or ""),
            "independence_group": str(item.get("independence_group") or ""),
            "limitations": [str(x) for x in (item.get("limitations") or [])]}


def _authority_card(result: Dict[str, Any]) -> Dict[str, Any]:
    """authority 卡（契约 §6.2）：确定性报告的结构化承载，字段全部来自运行结果。"""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    sources = [s for s in (data.get("sources") or []) if isinstance(s, dict)]
    evidence = [e for e in (data.get("evidence") or []) if isinstance(e, dict)]
    claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
    conflicts = [c for c in (data.get("conflicts") or []) if isinstance(c, dict)]
    analysis = [a for a in (data.get("analysis") or []) if isinstance(a, dict)]
    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    status = str(result.get("status") or "")
    return {
        "card_type": "authority",
        "status": status,
        "task_status": str(result.get("task_status") or ""),
        "as_of": str(result.get("finished_at") or ""),
        "degraded": status in _DEGRADED_STATUSES,
        "claims": [_claim_view(c) for c in claims],
        "evidence": [_evidence_view(e) for e in evidence],
        "sources": {"verified": [_source_view(s) for s in sources if s.get("verified")],
                    "unverified": [_source_view(s) for s in sources if not s.get("verified")]},
        "conflicts": [{"summary": str(c.get("summary") or ""),
                       "evidence_ids": _ids(c.get("evidence_ids"))} for c in conflicts],
        "analysis": [{"claim_id": str(a.get("claim_id") or ""),
                      "text": str(a.get("text") or ""),
                      "evidence_ids": _ids(a.get("evidence_ids"))} for a in analysis],
        "gaps": [str(g) for g in (result.get("gaps") or [])],
        "errors": [{"stage": str(e.get("stage") or ""),
                    "category": str(e.get("category") or ""),
                    "detail": str(e.get("detail") or "")} for e in errors],
        "limitations": [str(x) for x in (data.get("limitations") or [])],
        "counts": {"claims": len(claims),
                   "searches": int(audit.get("searches") or 0),
                   "pages": int(audit.get("pages") or 0),
                   "evidence": len(evidence),
                   "official_sources": len([s for s in sources
                                            if str(s.get("authority_class") or "")
                                            in _OFFICIAL_CLASSES]),
                   "llm_calls": int(audit.get("llm_calls") or 0)},
    }


def _claim_view(claim: Dict[str, Any]) -> Dict[str, Any]:
    """主张紧凑视图：conclusion 逐字透传（模板句式由流水线代码生成，不得改写）。"""
    balance = str(claim.get("balance") or "")
    lean = str(claim.get("lean") or "")
    span = claim.get("source_span")
    return {"claim_id": str(claim.get("claim_id") or ""),
            "text": str(claim.get("text") or ""),
            "source_span": {"start": int(span.get("start") or 0),
                            "end": int(span.get("end") or 0)} if isinstance(span, dict) else None,
            "balance": balance,
            "balance_label": str(claim.get("balance_label") or "")
                             or _BALANCE_LABELS.get(balance, balance),
            "lean": lean,
            "lean_label": str(claim.get("lean_label") or "")
                          or _LEAN_LABELS.get(lean, lean),
            "conclusion": str(claim.get("conclusion") or ""),
            "supporting_ids": _ids(claim.get("supporting_ids")),
            "refuting_ids": _ids(claim.get("refuting_ids")),
            "context_ids": _ids(claim.get("context_ids")),
            "authority_basis": [str(x) for x in (claim.get("authority_basis") or [])],
            "limitations": [str(x) for x in (claim.get("limitations") or [])]}


def _quote_line(item: Dict[str, Any]) -> str:
    """证据引文行（逐字）：`「quote」——《title》url（来源/抓取时间）。"""
    return (f"「{item['quote']}」——《{item['title'] or '（无标题）'}》{item['url']}"
            f"（{item['name'] or item['domain'] or '未知'}，"
            f"{item['verification'] or '未核验'}，抓取于 {item['fetched_at'] or '未注明'}）")


def _authority_digest(result: Dict[str, Any], card: Dict[str, Any]) -> str:
    """叙述层要点（契约 §6.2 的 digest 口径）：只转述 conclusion/引文/计数。

    不得新增判断：balance/lean/conclusion 全是流水线产物，digest 原样带出；
    引文逐字，叙述模型不得自行补充或改写证据。
    """
    counts = card["counts"]
    lines: List[str] = ["[权威核验] 只述证据倾向，不判真假（不建档、无分数）"]
    lines.append(f"- 执行状态：{card['status'] or '（未知）'}；"
                 f"任务状态：{card['task_status'] or '（未知）'}")
    lines.append(f"- 主张 {counts['claims']} 条；证据 {counts['evidence']} 条；"
                 f"权威类来源 {counts['official_sources']} 个")
    by_id = {e["evidence_id"]: e for e in card["evidence"]}
    if card["claims"]:
        lines.append("- 逐主张结论（模板句式由代码生成，逐字转述）：")
        for claim in card["claims"][:_QUOTE_PREVIEW]:
            lines.append(f"  · {claim['claim_id']}（{claim['balance_label']}"
                         f"／倾向{claim['lean_label']}）：{claim['conclusion']}")
            lines.append(f"    主张原文：{claim['text']}")
            for side, ids in (("支持", claim["supporting_ids"]),
                              ("反驳", claim["refuting_ids"]),
                              ("背景", claim["context_ids"])):
                shown = [by_id[i] for i in ids[:_QUOTES_PER_SIDE] if i in by_id]
                if shown:
                    lines.append(f"    {side}证据：" + "；".join(
                        f"{e['evidence_id']} {_quote_line(e)}" for e in shown))
    else:
        lines.append("- 本次没有可核查的主张（合法空出口）")
    if card["conflicts"]:
        lines.append("- 冲突前 3 条：" + "；".join(
            c["summary"] for c in card["conflicts"][:_QUOTE_PREVIEW]))
    gaps = card["gaps"]
    lines.append("- 缺口前 3 条：" + ("；".join(gaps[:_QUOTE_PREVIEW]) if gaps else "（无）"))
    errors = card["errors"]
    lines.append("- 错误前 3 条：" + ("；".join(
        f"[{e['stage']}] {e['category']}：{e['detail']}" for e in errors[:_QUOTE_PREVIEW])
        if errors else "（无）"))
    limits = card["limitations"]
    lines.append("- 限制前 3 条：" + ("；".join(limits[:_QUOTE_PREVIEW]) if limits else "（无）"))
    lines.append(f"- 已执行 {counts['searches']} 次搜索、{counts['pages']} 次读页、"
                 f"{counts['llm_calls']} 次模型调用")
    return "\n".join(lines)


def _result(result: Dict[str, Any]) -> Dict[str, Any]:
    """运行结果 → 统一工具返回（契约 §6.2：ok/card/digest/cost/不建档/只读元数据）。"""
    card = _authority_card(result)
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    status = card["status"]
    # 粗口径成本：LLM 调用 + 检索次数（M4 真实计数由 loop 的 delta 覆盖）
    requests = int(audit.get("llm_calls") or 0) + int(audit.get("searches") or 0)
    return {"ok": status not in _FAILED_STATUSES,
            "card": card,
            "digest": _authority_digest(result, card),
            "cost": {"requests": requests, "tokens": 0},
            "dossier_id": None,   # 只读核验：不建档（铁律：不扩权）
            "degraded": status in _DEGRADED_STATUSES,
            "status": status,
            "task_status": card["task_status"],
            "not_saved": True,
            "save_reason": "read_only_capability_no_dossier",
            "report": str(result.get("report") or "")}


def _verify_authority(ctx: Any, text: str) -> Dict[str, Any]:
    """verify_authority：权威核验（拆主张 → 权威取证 → 倾向与受约束分析），只读。"""
    try:
        skills = _skills(ctx)
    except Exception as exc:  # noqa: BLE001  工厂缺失/装配失败 = 诚实降级，会话不崩
        return _unavailable(f"权威核验流水线装配失败（{type(exc).__name__}）")
    try:
        result = skills.run(str(text),
                            cancel_check=getattr(ctx, "cancel_check", None),
                            deadline=_deadline(),
                            on_progress=_progress_emitter(getattr(ctx, "events", None)))
    except Exception as exc:  # noqa: BLE001  流水线自带"永不抛"外壳，这里是二道闸
        return _unavailable(f"权威核验流水线执行异常（{type(exc).__name__}）")
    if not isinstance(result, dict):
        return _unavailable("权威核验流水线返回形状不符")
    return _result(result)


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册权威核验的只读工具（services 形参约定同 tools_evidence.register_all）。"""
    registry.register("verify_authority", _verify_authority, params=["text"],
                      permission="read", cost="heavy", est_s=75.0,
                      description="权威核验：找权威资料证明/反驳用户说法，逐主张给证据倾向"
                                  "与受约束分析；只读、不建档、无分数",
                      validators={"text": text_param()})
