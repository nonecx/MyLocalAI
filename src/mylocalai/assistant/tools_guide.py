# -*- coding: utf-8 -*-
"""辟谣方法指南的只读工具封装（通用技能，第四波）：guide_fact_check。

改了什么、为什么、锁定什么：
   - 新增 `assistant.tools_guide`：把 `GuideSkills.run`（解析问题 → 一次受约束
     讲解 → 代码校验 → 五步法组装）接进助手工具层，注册为 `permission="read"`
     的 guide_fact_check（不建档、无分数、不联网——本工具没有任何分数字段，
     也没有任何"真/假/属实"式判定；五步法口径与 follow_up_hint 由流水线代码
     锁定生成，大模型只写受约束讲解）；
   - 死线/取消/进度透传：handler 把 `ctx.cancel_check`、`llm_cache.deadline_at()`
     （回合预算，嵌套取更紧者）与 `_progress_emitter(ctx.events)` 原样交给流水线；
     死线取不到就传 None，由流水线按 rules.total_deadline_s 自建；
   - 卡片即用户可见的讲解面：answer/steps/follow_up_hint 逐字来自 `result.data`
     （不改写）；digest 只做结构化要点转述，叙述层不得新增方法之外的"判断"；
   - 永不抛异常（与 tools_research/tools_authority 的降级文化一致）：服务工厂
     取不到 / 装配炸 / run 炸 / 返回坏形状，一律折成 ok=False + 诚实 digest，
     会话不崩。

三条铁律：只教方法不判真假（不对具体事件说法给真假结论，digest/report 不出现
真假判定词）；大模型不参与口径（五步法清单/下一步提示由代码生成，模型讲解越界
即被流水线丢弃并用内置模板降级）；零检索零取页（流水线模块内没有检索/取页的
注入位，本工具不发外发检索请求）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from mylocalai.assistant.registry import ToolRegistry, ValidationError

__all__ = ["register_all"]

#: 合法出口：六种 status 里只有这三个算 ok=False；needs_clarification（问题为空/
#: 超长）是合法空出口，不算失败；讲解降级（degraded）落在 data.state 里如实标注。
_FAILED_STATUSES = ("cancelled", "budget_exhausted", "failed")
#: degraded 口径：partial 及以上都如实标注降级；completed/needs_clarification 不算。
_DEGRADED_STATUSES = ("partial", "failed", "cancelled", "budget_exhausted")

#: 问题长度上限（与 guide 规则 max_input_chars=2000 同口径；registry 的
#: MAX_TEXT_LENGTH=8000 是通用文本上限，语义不同，不共用）。
_MAX_QUESTION_CHARS = 2000


def _question_param(value: Any) -> str:
    """guide_fact_check 的 question 参数：1-2000 字非空字符串（首尾空白剥掉）。"""
    if not isinstance(value, str):
        raise ValidationError("参数 question 必须是字符串")
    value = value.strip()
    if not value:
        raise ValidationError("参数 question 不能为空")
    if len(value) > _MAX_QUESTION_CHARS:
        raise ValidationError(f"参数 question 过长（最多 {_MAX_QUESTION_CHARS} 字）")
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

    component 用中文阶段名（pipeline 契约，共 3 步）：解析问题 / 组织讲解 /
    整理输出——工具层只透传，不改写阶段名；events 为 None（MCP/同步调用没有
    事件流）时返回 None，流水线对 None 回调逐位不变。
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
    """经 ctx.services 取辟谣指南流水线工厂并实例化（服务是工厂，同 research/authority）。"""
    services = getattr(ctx, "services", None)
    if services is None:
        raise KeyError("ctx.services 缺失")
    factory = services["guide_skills"]
    skills = factory() if callable(factory) else factory
    if skills is None or not hasattr(skills, "run"):
        raise TypeError("guide_skills 服务未返回可运行的流水线实例")
    return skills


def _unavailable(reason: str) -> Dict[str, Any]:
    """降级出口：装配/执行失败 → ok=False + 诚实 digest，不崩不编。"""
    detail = str(reason or "").strip()[:200] or "原因未知"
    return {"ok": False, "card": None,
            "digest": f"[辟谣方法指南] 不可用：{detail}。本次未产出任何讲解，"
                      "我不会替它补一份方法。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True,
            "status": "failed", "task_status": "", "not_saved": True,
            "save_reason": "read_only_capability_no_dossier", "report": ""}


def _ids(value: Any) -> List[str]:
    return [str(x) for x in (value or []) if str(x or "").strip()]


def _guide_card(result: Dict[str, Any]) -> Dict[str, Any]:
    """guide 卡：确定性讲解的结构化承载，字段全部来自运行结果（逐字透传）。"""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    status = str(result.get("status") or "")
    return {
        "card_type": "guide",
        "status": status,
        "task_status": str(result.get("task_status") or ""),
        "as_of": str(result.get("finished_at") or ""),
        "degraded": status in _DEGRADED_STATUSES,
        "question": str(data.get("question") or ""),
        "state": str(data.get("state") or ""),
        "answer": str(data.get("answer") or ""),
        "steps": [str(s) for s in (data.get("steps") or [])],
        "follow_up_hint": str(data.get("follow_up_hint") or ""),
        "gaps": [str(g) for g in (result.get("gaps") or [])],
        "errors": [{"stage": str(e.get("stage") or ""),
                    "category": str(e.get("category") or ""),
                    "detail": str(e.get("detail") or "")} for e in errors],
        "counts": {"searches": int(audit.get("searches") or 0),
                   "pages": int(audit.get("pages") or 0),
                   "llm_calls": int(audit.get("llm_calls") or 0)},
    }


def _guide_digest(result: Dict[str, Any], card: Dict[str, Any]) -> str:
    """叙述层要点：只转述流水线产物（五步法/讲解/下一步提示），不新增任何判断。

    讲解正文逐字转述；叙述模型不得在此之外补"所以那条消息是真的/假的"之类
    的延伸——本工具只教方法。
    """
    counts = card["counts"]
    lines: List[str] = ["[辟谣方法指南] 只教方法，不对具体说法下真假结论（不建档、无分数）"]
    lines.append(f"- 执行状态：{card['status'] or '（未知）'}；"
                 f"任务状态：{card['task_status'] or '（未知）'}；"
                 f"讲解状态：{card['state'] or '（未知）'}"
                 + ("（模型讲解降级，内置模板兜底）" if card["state"] == "degraded" else ""))
    lines.append(f"- 问题：{card['question'] or '（未记录）'}")
    lines.append(f"- 查证五步法（口径代码锁定，共 {len(card['steps'])} 步）：")
    for index, step in enumerate(card["steps"], start=1):
        lines.append(f"  {index}. {step}")
    if card["answer"]:
        lines.append("- 讲解（流水线产物原文，逐字）：")
        for line in card["answer"].splitlines():
            lines.append(f"  {line}")
    else:
        lines.append("- 讲解：（无）")
    lines.append(f"- 下一步：{card['follow_up_hint'] or '（无）'}")
    gaps = card["gaps"]
    lines.append("- 缺口前 3 条：" + ("；".join(gaps[:3]) if gaps else "（无）"))
    errors = card["errors"]
    lines.append("- 错误前 3 条：" + ("；".join(
        f"[{e['stage']}] {e['category']}：{e['detail']}" for e in errors[:3])
        if errors else "（无）"))
    lines.append(f"- 已执行 {counts['llm_calls']} 次模型调用；检索 0 次、读页 0 次"
                 "（本技能零检索零取页）")
    return "\n".join(lines)


def _result(result: Dict[str, Any]) -> Dict[str, Any]:
    """运行结果 → 统一工具返回（ok/card/digest/cost/不建档/只读元数据）。"""
    card = _guide_card(result)
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    status = card["status"]
    # 粗口径成本：模型调用次数（零检索零取页，没有其他外发；M4 真实计数由 loop 的 delta 覆盖）
    requests = int(audit.get("llm_calls") or 0)
    return {"ok": status not in _FAILED_STATUSES,
            "card": card,
            "digest": _guide_digest(result, card),
            "cost": {"requests": requests, "tokens": 0},
            "dossier_id": None,   # 纯知识讲解：不建档（铁律：不扩权）
            "degraded": status in _DEGRADED_STATUSES,
            "status": status,
            "task_status": card["task_status"],
            "not_saved": True,
            "save_reason": "read_only_capability_no_dossier",
            "report": str(result.get("report") or "")}


def _guide_fact_check(ctx: Any, question: str) -> Dict[str, Any]:
    """guide_fact_check：辟谣方法指南（受约束讲解 + 固定五步法口径），只读零检索。"""
    try:
        skills = _skills(ctx)
    except Exception as exc:  # noqa: BLE001  工厂缺失/装配失败 = 诚实降级，会话不崩
        return _unavailable(f"辟谣方法指南流水线装配失败（{type(exc).__name__}）")
    try:
        result = skills.run(str(question),
                            cancel_check=getattr(ctx, "cancel_check", None),
                            deadline=_deadline(),
                            on_progress=_progress_emitter(getattr(ctx, "events", None)))
    except Exception as exc:  # noqa: BLE001  流水线自带"永不抛"外壳，这里是二道闸
        return _unavailable(f"辟谣方法指南流水线执行异常（{type(exc).__name__}）")
    if not isinstance(result, dict):
        return _unavailable("辟谣方法指南流水线返回形状不符")
    return _result(result)


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册辟谣指南的只读工具（services 形参约定同 tools_research.register_all）。"""
    registry.register("guide_fact_check", _guide_fact_check, params=["question"],
                      permission="read", cost="light", est_s=10.0,
                      description="辟谣方法指南：教用户自行识别与查证谣言的方法"
                                  "（查证五步法）；只讲方法、不对具体说法下真假结论；"
                                  "零检索零取页、不建档、无分数",
                      validators={"question": _question_param})
