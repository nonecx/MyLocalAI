# -*- coding: utf-8 -*-
"""资料收集（主动调研）的只读工具封装（第三波契约 §6.1）：collect_research。

改了什么、为什么、锁定什么：
   - 新增 `assistant.tools_research`：把 `ResearchSkills.run`（范围与子问题 → 多轮
     检索 → 读页摘录 → 综述）接进助手工具层，注册为 `permission="read"` 的
     collect_research（不建档、不出真假结论——本工具没有任何分数字段，也没有
     任何"真/假/属实"式判定）；
   - 死线/取消/进度透传：handler 把 `ctx.cancel_check`、`llm_cache.deadline_at()`
     （回合预算，嵌套取更紧者）与 `_progress_emitter(ctx.events)` 原样交给流水线；
     死线取不到就传 None，由流水线按 rules.total_deadline_s 自建；
   - 卡片即用户可见的逐字资料面：evidence 卡的引文逐字来自 `result.data.evidence`
     （不截断、不改写）；digest 只做结构化要点转述，叙述层不得新增证据；
   - 永不抛异常（与 tools_evidence 的降级文化一致）：服务工厂取不到 / 装配炸 /
     run 炸 / 返回坏形状，一律折成 ok=False + 诚实 digest，会话不崩。

三条铁律：不判真假只述证据（digest/report 不出现真假判定词）；大模型不参与判定
（分类/选站/拆解/摘录全部由流水线内的代码白名单与定位校验收口）；每个出口都有
降级（六种 status 都是合法出口，空结果给空列表，绝不为有输出而编）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from mylocalai.assistant.registry import ToolRegistry, ValidationError

__all__ = ["register_all"]

#: 合法出口（契约 §6.1）：六种 status 里只有这三个算 ok=False；needs_clarification
#: （主题过宽/预检失败）与 no_factual_claim 都是合法空出口，不算失败。
_FAILED_STATUSES = ("cancelled", "budget_exhausted", "failed")
#: degraded 口径：partial 及以上都如实标注降级；completed/needs_clarification 不算。
_DEGRADED_STATUSES = ("partial", "failed", "cancelled", "budget_exhausted")

#: 主题长度上限（与 research 规则 max_input_chars=500 同口径；registry 的
#: MAX_TOPIC_LENGTH=200 是热度话题的上限，语义不同，不共用）。
_MAX_TOPIC_CHARS = 500

_QUOTE_PREVIEW = 3   # digest 里逐字带出的关键发现/引文条数（契约 §6.1：前 3 条）
_QUOTES_PER_FINDING = 2   # 每条关键发现在 digest 里逐字带出的引文条数


def _topic_param(value: Any) -> str:
    """collect_research 的主题参数：1-500 字非空字符串（首尾空白剥掉）。"""
    if not isinstance(value, str):
        raise ValidationError("参数 topic 必须是字符串")
    value = value.strip()
    if not value:
        raise ValidationError("参数 topic 不能为空")
    if len(value) > _MAX_TOPIC_CHARS:
        raise ValidationError(f"参数 topic 过长（最多 {_MAX_TOPIC_CHARS} 字）")
    return value


def _deadline() -> Optional[float]:
    """当前线程的绝对死线（回合预算，time.monotonic 基准）；未设预算返回 None。"""
    try:
        from mylocalai.llm import llm_cache
        return llm_cache.deadline_at()
    except Exception:  # noqa: BLE001  死线是增益约束：取不到就不设，流水线自建
        return None


def _progress_emitter(events: Any) -> Optional[Callable[[str, int, int], None]]:
    """把流水线的 on_progress(stage, done, total) 转成 SSE progress 事件源。

    component 用中文阶段名（pipeline 契约，共 4 步）：确定范围 / 检索资料 /
    阅读摘录 / 整理简报——工具层只透传，不改写阶段名（改写会让流水线与前端
    对不上）；events 为 None（MCP/同步调用没有事件流）时返回 None，流水线对
    None 回调逐位不变。
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
    """经 ctx.services 取资料收集流水线工厂并实例化（服务是工厂，契约 §6.1）。"""
    services = getattr(ctx, "services", None)
    if services is None:
        raise KeyError("ctx.services 缺失")
    factory = services["research_skills"]
    skills = factory() if callable(factory) else factory
    if skills is None or not hasattr(skills, "run"):
        raise TypeError("research_skills 服务未返回可运行的流水线实例")
    return skills


def _unavailable(reason: str) -> Dict[str, Any]:
    """降级出口（契约 §6.1）：装配/执行失败 → ok=False + 诚实 digest，不崩不编。"""
    detail = str(reason or "").strip()[:200] or "原因未知"
    return {"ok": False, "card": None,
            "digest": f"[资料收集] 不可用：{detail}。本次未取得任何资料，"
                      "我不会替它补一份简报。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True,
            "status": "failed", "task_status": "", "not_saved": True,
            "save_reason": "read_only_capability_no_dossier", "report": ""}


def _ids(value: Any) -> List[str]:
    return [str(x) for x in (value or []) if str(x or "").strip()]


def _source_view(source: Dict[str, Any]) -> Dict[str, Any]:
    """来源紧凑视图（cards.js renderResearch 的字段口径；verified 决定分级区）。"""
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
            "sq_ids": _ids(item.get("sq_ids")),
            "source_id": str(item.get("source_id") or ""),
            "name": str(item.get("name") or ""),
            "domain": str(item.get("domain") or ""),
            "tier": str(item.get("tier") or ""),
            "source_kind": str(item.get("source_kind") or ""),
            "authority_class": str(item.get("authority_class") or ""),
            "verification": str(item.get("verification") or ""),
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "quote": str(item.get("quote") or ""),
            "quote_span": {"start": int(span.get("start") or 0),
                           "end": int(span.get("end") or 0)} if isinstance(span, dict) else None,
            "published_at": str(item.get("published_at") or ""),
            "fetched_at": str(item.get("fetched_at") or ""),
            "role": str(item.get("role") or ""),
            "limitations": [str(x) for x in (item.get("limitations") or [])]}


def _scope_view(scope: Any) -> Dict[str, str]:
    """范围视图（契约 §4.3 的 scope 四键；basis 是逐字片段，原样带出）。"""
    scope = scope if isinstance(scope, dict) else {}
    return {"question": str(scope.get("question") or ""),
            "time_window": str(scope.get("time_window") or ""),
            "region": str(scope.get("region") or ""),
            "basis": str(scope.get("basis") or "")}


def _research_card(result: Dict[str, Any]) -> Dict[str, Any]:
    """research 卡（契约 §6.1）：确定性报告的结构化承载，字段全部来自运行结果。"""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    sources = [s for s in (data.get("sources") or []) if isinstance(s, dict)]
    evidence = [e for e in (data.get("evidence") or []) if isinstance(e, dict)]
    sub_questions = [q for q in (data.get("sub_questions") or []) if isinstance(q, dict)]
    findings = [f for f in (data.get("key_findings") or []) if isinstance(f, dict)]
    leads = [l for l in (data.get("leads") or []) if isinstance(l, dict)]
    timeline = [t for t in (data.get("timeline") or []) if isinstance(t, dict)]
    conflicts = [c for c in (data.get("conflicts") or []) if isinstance(c, dict)]
    analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    status = str(result.get("status") or "")
    card: Dict[str, Any] = {
        "card_type": "research",
        "status": status,
        "task_status": str(result.get("task_status") or ""),
        "as_of": str(result.get("finished_at") or ""),
        "degraded": status in _DEGRADED_STATUSES,
        "topic": str(data.get("topic") or ""),
        "scope": _scope_view(data.get("scope")),
        "sub_questions": [{"sq_id": str(q.get("sq_id") or ""),
                           "text": str(q.get("text") or ""),
                           "status": str(q.get("status") or ""),
                           "evidence_count": int(q.get("evidence_count") or 0)}
                          for q in sub_questions],
        "key_findings": [{"finding_id": str(f.get("finding_id") or ""),
                          "sq_ids": _ids(f.get("sq_ids")),
                          "statement": str(f.get("statement") or ""),
                          "evidence_ids": _ids(f.get("evidence_ids")),
                          "support_level": str(f.get("support_level") or "")}
                         for f in findings],
        "leads": [{"lead_id": str(l.get("lead_id") or ""),
                   "statement": str(l.get("statement") or ""),
                   "evidence_ids": _ids(l.get("evidence_ids")),
                   "reason": str(l.get("reason") or "")} for l in leads],
        "evidence": [_evidence_view(e) for e in evidence],
        "sources": {"verified": [_source_view(s) for s in sources if s.get("verified")],
                    "unverified": [_source_view(s) for s in sources if not s.get("verified")]},
        "timeline": [{"date": str(t.get("date") or ""),
                      "event": str(t.get("event") or ""),
                      "evidence_ids": _ids(t.get("evidence_ids"))} for t in timeline],
        "conflicts": [{"summary": str(c.get("summary") or ""),
                       "evidence_ids": _ids(c.get("evidence_ids"))} for c in conflicts],
        "analysis": {"text": str(analysis.get("text") or ""),
                     "evidence_ids": _ids(analysis.get("evidence_ids")),
                     "label": str(analysis.get("label") or "") or "基于资料的整理/推断"},
        "gaps": [str(g) for g in (result.get("gaps") or [])],
        "errors": [{"stage": str(e.get("stage") or ""),
                    "category": str(e.get("category") or ""),
                    "detail": str(e.get("detail") or "")} for e in errors],
        "counts": {"sub_questions": len(sub_questions),
                   "searches": int(audit.get("searches") or 0),
                   "pages": int(audit.get("pages") or 0),
                   "sources": len(sources),
                   "evidence": len(evidence),
                   "llm_calls": int(audit.get("llm_calls") or 0)},
    }
    card["followups"] = _followups(card)
    return card


def _followups(card: Dict[str, Any]) -> List[str]:
    """后续建议（确定性派生，不新增事实）：只按状态/缺口/线索给"下一步可以做什么"。

    建议是行动选项，不是对资料内容的评价——不出现真假判定，也不承诺结果。
    """
    out: List[str] = []
    if card["status"] == "needs_clarification":
        out.append("按收窄建议改写主题后，再收集一轮")
    if card["gaps"]:
        out.append(f"还有未覆盖的点（{str(card['gaps'][0])[:40]}）："
                   "可指定时间窗或地区再收集一轮")
    if card["leads"]:
        out.append("线索里的来源未核验：可先对其中一条做权威核验，再决定是否采信")
    if card["status"] in _DEGRADED_STATUSES:
        out.append("本轮未跑完：稍后重试，或把主题拆小一点")
    if not out:
        out.append("可以换个角度（时间窗/地区/子主题）再收集一轮")
    return out[:3]


def _research_digest(result: Dict[str, Any], card: Dict[str, Any]) -> str:
    """叙述层要点（契约 §6.1 的 digest 口径）：只转述流水线产物，不新增证据。

    关键发现前 3 条逐字带出引文——叙述模型唯一的证据来源就是这里 + 卡片，
    它不得自行补充。
    """
    counts = card["counts"]
    lines: List[str] = ["[资料收集] 只述资料，不判真假（不建档、无分数）"]
    lines.append(f"- 执行状态：{card['status'] or '（未知）'}；"
                 f"任务状态：{card['task_status'] or '（未知）'}")
    lines.append(f"- 主题：{card['topic'] or '（未识别）'}")
    scope = card["scope"]
    if scope["question"]:
        extra = "；".join(x for x in (scope["time_window"], scope["region"]) if x)
        lines.append(f"- 范围：{scope['question']}"
                     + (f"（{extra}）" if extra else ""))
    lines.append(f"- 子问题 {counts['sub_questions']} 条；来源 {counts['sources']} 个"
                 f"（已核验 {len(card['sources']['verified'])}）；证据 {counts['evidence']} 条")
    by_id = {e["evidence_id"]: e for e in card["evidence"]}
    lines.append(f"- 关键发现前 {_QUOTE_PREVIEW} 条（引文逐字；叙述层不得新增证据）：")
    if card["key_findings"]:
        for finding in card["key_findings"][:_QUOTE_PREVIEW]:
            ev_ids = "、".join(finding["evidence_ids"]) or "（无证据引用）"
            lines.append(f"  · {finding['finding_id']}（子问题 "
                         f"{'、'.join(finding['sq_ids']) or '未关联'}，证据 {ev_ids}，"
                         f"支持等级 {finding['support_level'] or '未标注'}）："
                         f"{finding['statement']}")
            for ev_id in finding["evidence_ids"][:_QUOTES_PER_FINDING]:
                item = by_id.get(ev_id)
                if item is None:
                    continue
                lines.append(
                    f"    「{item['quote']}」——《{item['title'] or '（无标题）'}》"
                    f"{item['url']}（来源 {item['name'] or item['domain'] or '未知'}，"
                    f"抓取于 {item['fetched_at'] or '未注明'}）")
    else:
        lines.append("  （本次没有可引用的关键发现）")
    if card["leads"]:
        lines.append(f"- 线索前 {_QUOTE_PREVIEW} 条（来源未核验，只作线索）：")
        for lead in card["leads"][:_QUOTE_PREVIEW]:
            lines.append(f"  · {lead['lead_id']}：{lead['statement']}"
                         f"（{lead['reason'] or '未说明'}）")
    gaps = card["gaps"]
    lines.append("- 缺口前 3 条：" + ("；".join(gaps[:_QUOTE_PREVIEW]) if gaps else "（无）"))
    errors = card["errors"]
    lines.append("- 错误前 3 条：" + ("；".join(
        f"[{e['stage']}] {e['category']}：{e['detail']}" for e in errors[:_QUOTE_PREVIEW])
        if errors else "（无）"))
    lines.append(f"- 已执行 {counts['searches']} 次搜索、{counts['pages']} 次读页、"
                 f"{counts['llm_calls']} 次模型调用")
    return "\n".join(lines)


def _result(result: Dict[str, Any]) -> Dict[str, Any]:
    """运行结果 → 统一工具返回（契约 §6.1：ok/card/digest/cost/不建档/只读元数据）。"""
    card = _research_card(result)
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    status = card["status"]
    # 粗口径成本：LLM 调用 + 检索次数（M4 真实计数由 loop 的 delta 覆盖）
    requests = int(audit.get("llm_calls") or 0) + int(audit.get("searches") or 0)
    return {"ok": status not in _FAILED_STATUSES,
            "card": card,
            "digest": _research_digest(result, card),
            "cost": {"requests": requests, "tokens": 0},
            "dossier_id": None,   # 只读调研：不建档（铁律：不扩权）
            "degraded": status in _DEGRADED_STATUSES,
            "status": status,
            "task_status": card["task_status"],
            "not_saved": True,
            "save_reason": "read_only_capability_no_dossier",
            "report": str(result.get("report") or "")}


def _collect_research(ctx: Any, topic: str) -> Dict[str, Any]:
    """collect_research：主动资料收集（子问题 → 检索 → 摘录 → 简报），只读不建档。"""
    try:
        skills = _skills(ctx)
    except Exception as exc:  # noqa: BLE001  工厂缺失/装配失败 = 诚实降级，会话不崩
        return _unavailable(f"资料收集流水线装配失败（{type(exc).__name__}）")
    try:
        result = skills.run(str(topic),
                            cancel_check=getattr(ctx, "cancel_check", None),
                            deadline=_deadline(),
                            on_progress=_progress_emitter(getattr(ctx, "events", None)))
    except Exception as exc:  # noqa: BLE001  流水线自带"永不抛"外壳，这里是二道闸
        return _unavailable(f"资料收集流水线执行异常（{type(exc).__name__}）")
    if not isinstance(result, dict):
        return _unavailable("资料收集流水线返回形状不符")
    return _result(result)


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册资料收集的只读工具（services 形参约定同 tools_evidence.register_all）。"""
    registry.register("collect_research", _collect_research, params=["topic"],
                      permission="read", cost="heavy", est_s=60.0,
                      description="主动资料收集：按主题拆子问题、多轮检索、读页摘录，"
                                  "产出带逐字引文的研究简报（只读、不建档、不出真假结论）",
                      validators={"topic": _topic_param})
