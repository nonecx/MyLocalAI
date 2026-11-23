# -*- coding: utf-8 -*-
"""统计说法核查的只读工具封装（第五波，服务 layperson 角色）：check_statistics。

改了什么、为什么、锁定什么：
   - 新增 `assistant.tools_statistics`：把 `StatisticsSkills.run`（预检 → 抽数字
     主张 → 逐主张检索读页 → 代码口径比对 → 汇总）接进助手工具层，注册为
     `permission="read"` 的 check_statistics（不建档、无分数——本工具没有任何
     分数字段，也没有任何"真/假/属实"式判定；四态判语全部由流水线内的代码
     数值比对算出，大模型不参与判定）；
   - 死线/取消/进度透传：handler 把 `ctx.cancel_check`、`llm_cache.deadline_at()`
     （回合预算，嵌套取更紧者）与 `_progress_emitter(ctx.events)` 原样交给流水线；
     死线取不到就传 None，由流水线按 rules.total_deadline_s 自建；
   - 卡片即用户可见的判定面：verdict/source_number/quote/basis 全部来自
     `result.data.claims`（逐字透传，不改写）；digest 只做结构化要点转述，
     叙述层不得新增任何判语或数字；
   - 永不抛异常（与 tools_source/tools_authority 的降级文化一致）：服务工厂
     取不到 / 装配炸 / run 炸 / 返回坏形状，一律折成 ok=False + 诚实 digest，
     会话不崩。

三条铁律：不判内容真假（digest/report 不出现真假判定词，「未核实到」绝不说成
「数字为假」）；大模型不参与判定（数值抽取/归一/比对/四态全由代码计算，模型只
许做摘录结构化与段落定位，越界即被流水线丢弃）；每个出口都有降级
（unverifiable/unreachable 是如实标注的合法出口，绝不把"没核到"说成"查过没问题"）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from mylocalai.assistant.registry import ToolRegistry, ValidationError

__all__ = ["register_all"]

#: 合法出口：六种 status 里只有这三个算 ok=False；needs_clarification（输入不可用）
#: 与 no_factual_claim（原文没有数字）都是合法空出口，不算失败。
_FAILED_STATUSES = ("cancelled", "budget_exhausted", "failed")
#: degraded 口径：partial 及以上都如实标注降级；completed/needs_clarification 不算。
_DEGRADED_STATUSES = ("partial", "failed", "cancelled", "budget_exhausted")

#: 输入长度上限（与 statistics 规则 max_input_chars=4000 同口径；registry 的
#: MAX_TEXT_LENGTH=8000 是通用文本上限，语义不同，不共用）。
_MAX_TEXT_CHARS = 4000

#: state 封闭枚举的中文标签（与 statistics/SKILL.md 的输出口径对应）
_STATE_LABELS = {"ok": "已完成口径比对（含「未核实到」的合法结论）",
                 "unclear": "输入为空或超过单次上限，需要补充",
                 "unreachable": "未取得任何可读页面正文（检索未授权或取页全败）"}
#: 四态判语的中文标签（与流水线 VERDICT_LABELS 同口径；工具层自带一份而不
#: import pipeline 的私有映射：渲染层改文案不该静默改掉 digest 口径）
_VERDICT_LABELS = {"consistent": "与来源一致", "inconsistent": "与来源不一致",
                   "caliber_mismatch": "口径不一致", "unverifiable": "未核实到"}


def _text_param(value: Any) -> str:
    """check_statistics 的 text 参数：1-4000 字非空字符串（首尾空白剥掉）。"""
    if not isinstance(value, str):
        raise ValidationError("参数 text 必须是字符串")
    value = value.strip()
    if not value:
        raise ValidationError("参数 text 不能为空")
    if len(value) > _MAX_TEXT_CHARS:
        raise ValidationError(f"参数 text 过长（最多 {_MAX_TEXT_CHARS} 字）")
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

    component 用中文阶段名（pipeline 契约，共 6 步）：解析输入 / 抽取数字主张 /
    检索资料 / 阅读正文 / 口径比对 / 汇总输出——工具层只透传，不改写阶段名；
    events 为 None（MCP/同步调用没有事件流）时返回 None，流水线对 None 回调
    逐位不变。
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
    """经 ctx.services 取统计核查流水线工厂并实例化（服务是工厂，同 source/authority）。"""
    services = getattr(ctx, "services", None)
    if services is None:
        raise KeyError("ctx.services 缺失")
    factory = services["statistics_skills"]
    skills = factory() if callable(factory) else factory
    if skills is None or not hasattr(skills, "run"):
        raise TypeError("statistics_skills 服务未返回可运行的流水线实例")
    return skills


def _unavailable(reason: str) -> Dict[str, Any]:
    """降级出口：装配/执行失败 → ok=False + 诚实 digest，不崩不编。"""
    detail = str(reason or "").strip()[:200] or "原因未知"
    return {"ok": False, "card": None,
            "digest": f"[统计说法核查] 不可用：{detail}。本次未取得任何口径比对，"
                      "我不会替它补一个结论。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True,
            "status": "failed", "task_status": "", "not_saved": True,
            "save_reason": "read_only_capability_no_dossier", "report": ""}


def _claim_view(claim: Dict[str, Any]) -> Dict[str, Any]:
    """主张紧凑视图：verdict/source_number/quote 逐字透传（不改写判语与数字）。"""
    qualifiers = claim.get("qualifiers")
    qualifiers = qualifiers if isinstance(qualifiers, dict) else {}
    verdict = str(claim.get("verdict") or "")
    return {"claim_id": str(claim.get("claim_id") or ""),
            "excerpt": str(claim.get("excerpt") or ""),
            "number": claim.get("number"),
            "unit": str(claim.get("unit") or ""),
            "metric": str(claim.get("metric") or ""),
            "qualifiers": {key: str(qualifiers.get(key) or "")
                           for key in ("time", "place", "entity", "base")},
            "verdict": verdict,
            "verdict_label": _VERDICT_LABELS.get(verdict, verdict),
            "source_number": str(claim.get("source_number") or ""),
            "source_url": str(claim.get("source_url") or ""),
            "source_title": str(claim.get("source_title") or ""),
            "quote": str(claim.get("quote") or ""),
            "as_of": str(claim.get("as_of") or ""),
            "basis": str(claim.get("basis") or "")}


def _statistics_card(result: Dict[str, Any]) -> Dict[str, Any]:
    """statistics 卡：确定性判定的结构化承载，字段全部来自运行结果（逐字透传）。"""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    status = str(result.get("status") or "")
    return {
        "card_type": "statistics",
        "status": status,
        "task_status": str(result.get("task_status") or ""),
        "as_of": str(result.get("finished_at") or ""),
        "degraded": status in _DEGRADED_STATUSES,
        "text": str(data.get("text") or ""),
        "state": str(data.get("state") or ""),
        "state_label": _STATE_LABELS.get(str(data.get("state") or ""), ""),
        "claims": [_claim_view(c) for c in claims],
        "limits": [str(x) for x in (data.get("limits") or [])],
        "gaps": [str(g) for g in (result.get("gaps") or [])],
        "errors": [{"stage": str(e.get("stage") or ""),
                    "category": str(e.get("category") or ""),
                    "detail": str(e.get("detail") or "")} for e in errors],
        "counts": {"claims": int(counts.get("claims") or 0),
                   "searches": int(counts.get("searches") or 0),
                   "pages": int(counts.get("pages") or 0),
                   "llm_calls": int(counts.get("llm_calls") or 0)},
    }


def _statistics_digest(result: Dict[str, Any], card: Dict[str, Any]) -> str:
    """叙述层要点：只转述流水线判语（四态/来源数字/段落/依据），不新增任何判语。

    「未核实到」只表示本次没有定位到可比对的来源数字——digest 逐字转述流水线的
    判语与 basis，叙述模型不得自行补充"数字为真/为假"之类的延伸。
    """
    counts = card["counts"]
    lines: List[str] = ["[统计说法核查] 只比对数字口径，不判内容真假（不建档、无分数）"]
    lines.append(f"- 执行状态：{card['status'] or '（未知）'}；"
                 f"任务状态：{card['task_status'] or '（未知）'}")
    lines.append(f"- 输入：{card['text'] or '（未记录）'}")
    lines.append(f"- 运行状态：{card['state_label'] or card['state'] or '（未知）'}")
    if card["claims"]:
        lines.append("- 逐主张口径比对（判定全部由代码完成，逐字转述）：")
        for claim in card["claims"]:
            lines.append(f"  · {claim['claim_id']}「{claim['excerpt']}」"
                         f"数字 {claim['number']}{claim['unit']} → "
                         f"{claim['verdict_label']}；{claim['basis']}")
            if claim["source_url"]:
                lines.append(f"    来源：《{claim['source_title'] or '（无标题）'}》"
                             f"{claim['source_url']}｜来源数字："
                             f"{claim['source_number'] or '（无）'}"
                             f"｜段落：「{claim['quote']}」")
    else:
        lines.append("- 本次没有识别到可核对的数字主张（合法空出口）")
    limits = card["limits"]
    lines.append("- 限制前 3 条：" + ("；".join(limits[:3]) if limits else "（无）"))
    gaps = card["gaps"]
    lines.append("- 缺口前 3 条：" + ("；".join(gaps[:3]) if gaps else "（无）"))
    errors = card["errors"]
    lines.append("- 错误前 3 条：" + ("；".join(
        f"[{e['stage']}] {e['category']}：{e['detail']}" for e in errors[:3])
        if errors else "（无）"))
    lines.append(f"- 计数：主张 {counts['claims']} 条；检索 {counts['searches']} 次；"
                 f"读页 {counts['pages']} 页；模型调用 {counts['llm_calls']} 次")
    lines.append("- 口径边界：本工具只比对数字与单位口径，不判说法真假；"
                 "「未核实到」只表示本次没有定位到可比对的来源数字")
    lines.append("- 下一步：要判断说法本身能否被权威资料证明/反驳，"
                 "请用 verify_claim / verify_authority")
    return "\n".join(lines)


def _result(result: Dict[str, Any]) -> Dict[str, Any]:
    """运行结果 → 统一工具返回（ok/card/digest/cost/不建档/只读元数据）。"""
    card = _statistics_card(result)
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    status = card["status"]
    # 粗口径成本：模型调用 + 检索 + 读页次数（M4 真实计数由 loop 的 delta 覆盖）
    requests = (int(audit.get("llm_calls") or 0) + int(audit.get("searches") or 0)
                + int(audit.get("pages") or 0))
    return {"ok": status not in _FAILED_STATUSES,
            "card": card,
            "digest": _statistics_digest(result, card),
            "cost": {"requests": requests, "tokens": 0},
            "dossier_id": None,   # 只读核查：不建档（铁律：不扩权）
            "degraded": status in _DEGRADED_STATUSES,
            "status": status,
            "task_status": card["task_status"],
            "not_saved": True,
            "save_reason": "read_only_capability_no_dossier",
            "report": str(result.get("report") or "")}


def _check_statistics(ctx: Any, text: str) -> Dict[str, Any]:
    """check_statistics：统计说法核查（抽数字主张 → 检索读页 → 口径比对），只读。"""
    try:
        skills = _skills(ctx)
    except Exception as exc:  # noqa: BLE001  工厂缺失/装配失败 = 诚实降级，会话不崩
        return _unavailable(f"统计核查流水线装配失败（{type(exc).__name__}）")
    try:
        result = skills.run(str(text),
                            cancel_check=getattr(ctx, "cancel_check", None),
                            deadline=_deadline(),
                            on_progress=_progress_emitter(getattr(ctx, "events", None)))
    except Exception as exc:  # noqa: BLE001  流水线自带"永不抛"外壳，这里是二道闸
        return _unavailable(f"统计核查流水线执行异常（{type(exc).__name__}）")
    if not isinstance(result, dict):
        return _unavailable("统计核查流水线返回形状不符")
    return _result(result)


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册统计说法核查的只读工具（services 形参约定同 tools_source.register_all）。"""
    registry.register("check_statistics", _check_statistics, params=["text"],
                      permission="read", cost="medium", est_s=30.0,
                      description="统计说法核查：贴一段含数字的说法，逐条把数字与已读"
                                  "来源正文做口径比对（一致/不一致/口径不一致/未核实到），"
                                  "不判内容真假；只读、不建档、无分数",
                      validators={"text": _text_param})
