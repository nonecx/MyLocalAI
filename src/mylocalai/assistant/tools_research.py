# -*- coding: utf-8 -*-
"""collect_research 工具：资料收集（只读、不建档、不出真假结论）。

契约：`assistant.skills.research.pipeline.ResearchSkills` 返回统一运行外壳；
本模块只做参数校验、偏好/prior 解析、服务装配、卡/digest 组装与降级。
自动接力：研究卡有 `key_findings[].source_quote` 且偏好开启时，把**原文直接引文**
（最多 2 条）交给 AuthoritySkills 核验，合成 composite 卡；不核验助手综述句。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from mylocalai.assistant.registry import ToolRegistry, ValidationError
from mylocalai.assistant.skills.continuation import (find_prior_card, looks_like_followup,
                                                     looks_like_rerun, prior_topic)
from mylocalai.assistant.skills.preferences import load_preferences
from mylocalai.assistant.tools_skill_cards import (authority_card, composite_card,
                                                   composite_digest, research_card,
                                                   research_digest)

__all__ = ["register_all", "topic_param", "followup_param"]

_FAILED = ("failed", "cancelled", "budget_exhausted")
_MAX_TOPIC = 500
_MAX_HANDOFF_QUOTES = 2


def _v_str_optional(value: Any, name: str, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"参数 {name} 必须是字符串")
    value = value.strip()
    if len(value) > max_len:
        raise ValidationError(f"参数 {name} 过长（最多 {max_len} 字）")
    return value


def topic_param(value: Any) -> str:
    return _v_str_optional(value, "topic", _MAX_TOPIC)


def followup_param(value: Any) -> bool:
    if value is None or value is False:
        return False
    if not isinstance(value, bool):
        raise ValidationError("参数 followup 必须是布尔值")
    return value


def _prefs() -> Dict[str, Any]:
    try:
        return load_preferences()
    except Exception:  # noqa: BLE001  偏好只影响模式/接力，读不到按默认
        return {"research": {"workflow_mode": "pipeline", "auto_verify_findings": True},
                "authority": {"workflow_mode": "pipeline"},
                "composite": {"enabled": True}, "warning": ""}


def _deadline() -> Optional[float]:
    try:
        from mylocalai.llm import llm_cache
        return llm_cache.deadline_at()
    except Exception:  # noqa: BLE001
        return None


def _service(ctx: Any, name: str) -> Any:
    services = getattr(ctx, "services", None)
    if services is None:
        raise KeyError("ctx.services 缺失")
    factory = services[name]
    return factory() if callable(factory) else factory


def _progress(ctx: Any) -> Optional[Callable[[str, int, int], None]]:
    events = getattr(ctx, "events", None)
    if events is None:
        return None

    def _emit(label: str, done: int, total: int) -> None:
        try:
            events.emit("progress", component=str(label), done=int(done), total=int(total))
        except Exception:  # noqa: BLE001  进度失败不影响主结果
            pass
    return _emit


def _unavailable(reason: str) -> Dict[str, Any]:
    detail = str(reason or "").strip()[:200] or "原因未知"
    return {"ok": False, "card": None, "card_type": "research",
            "digest": f"[资料收集] 不可用：{detail}。本次未取得资料，不替它补结论。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True,
            "status": "failed", "task_status": "", "not_saved": True,
            "save_reason": "read_only"}


def _clarify(reason: str) -> Dict[str, Any]:
    return {"ok": True, "card": None, "card_type": "research",
            "digest": f"[资料收集] 需要补充信息：{reason}",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": False,
            "status": "needs_clarification", "task_status": "needs_clarification",
            "not_saved": True, "save_reason": "read_only"}


def _handoff_quotes(card: Dict[str, Any]) -> List[str]:
    """只取 key_findings 里带原文直接引文、来源可核的条目；不核验综述句。"""
    out: List[str] = []
    for item in card.get("key_findings") or []:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("source_quote") or "").strip()
        level = str(item.get("support_level") or "")
        if not quote or level not in ("authoritative", "reliable"):
            continue
        if item.get("checkable") is False:
            continue
        if quote not in out:
            out.append(quote)
        if len(out) >= _MAX_HANDOFF_QUOTES:
            break
    return out


def _failed_authority(reason: str) -> Dict[str, Any]:
    return {"card_type": "authority", "status": "failed", "task_status": "",
            "as_of": "", "degraded": True, "claims": [], "evidence": [],
            "sources": {"verified": [], "unverified": []}, "conflicts": [],
            "analysis": [], "limitations": [], "gaps": [],
            "errors": [{"stage": "handoff", "category": "unavailable", "detail": reason}],
            "counts": {"claims": 0, "evidence": 0, "searches": 0, "pages": 0,
                       "llm_calls": 0, "official_sources": 0},
            "execution": {"mode": "pipeline", "steps": 0,
                          "stop_reason": "authority_unavailable"},
            "incremental": False, "base_as_of": ""}


def _maybe_handoff(ctx: Any, card: Dict[str, Any], topic: str) -> Optional[Dict[str, Any]]:
    prefs = _prefs()
    if not bool((prefs.get("composite") or {}).get("enabled", True)):
        return None
    if not bool((prefs.get("research") or {}).get("auto_verify_findings", True)):
        return None
    quotes = _handoff_quotes(card)
    if not quotes:
        card["handoff_skipped"] = "没有可直接核验的原文引文，未自动核验"
        return None
    try:
        authority = _service(ctx, "authority_skills")
        mode = str((prefs.get("authority") or {}).get("workflow_mode", "pipeline"))
        result = authority.run("\n".join(quotes), mode=mode, origin="auto_from_research",
                               claim_context=str(topic or ""),
                               cancel_check=getattr(ctx, "cancel_check", None),
                               deadline=_deadline(), on_progress=_progress(ctx))
        if not isinstance(result, dict):
            raise TypeError("authority 返回非对象")
        auth_card = authority_card(result)
    except Exception as exc:  # noqa: BLE001  自动接力失败不吞研究结果
        auth_card = _failed_authority(f"{type(exc).__name__}: {exc}")
    return composite_card(card, auth_card,
                          {"auto": True,
                           "reason": "研究卡直接引文自动核验（非用户原话）",
                           "quotes": quotes})


def _collect_research(ctx: Any, topic: str = "", followup: bool = False) -> Dict[str, Any]:
    prefs = _prefs()
    prior = None
    text = str(topic or "").strip()
    user_text = str(getattr(ctx, "user_text", "") or "")
    try:
        if getattr(ctx, "session_id", None) is not None:
            prior = find_prior_card(getattr(ctx, "sessions", None),
                                    getattr(ctx, "session_id"), ("research", "composite"))
    except Exception:  # noqa: BLE001  prior 只是增量增益，读不到按首轮
        prior = None
    is_followup = bool(followup) or (prior is not None
                                     and looks_like_followup(user_text)
                                     and not looks_like_rerun(user_text))
    if not text and prior is not None:
        text = prior_topic(prior)
    if not text:
        return _clarify("请说明要收集/调研的主题（例如“某地自来水水质争议的最新资料”）。")
    try:
        skills = _service(ctx, "research_skills")
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"资料收集流水线装配失败（{type(exc).__name__}）")

    mode = str((prefs.get("research") or {}).get("workflow_mode", "pipeline"))
    try:
        result = skills.run(text, mode=mode, prior=prior if is_followup else None,
                            followup=is_followup,
                            cancel_check=getattr(ctx, "cancel_check", None),
                            deadline=_deadline(), on_progress=_progress(ctx))
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"资料收集执行异常（{type(exc).__name__}）")
    if not isinstance(result, dict):
        return _unavailable("资料收集返回形状不符")

    card = research_card(result)
    status = str(card.get("status") or "")
    composite = _maybe_handoff(ctx, card, text)
    if composite is not None:
        card = composite
        status = str(card.get("status") or "")
        digest = composite_digest(card)
        card_type = "composite"
    else:
        digest = research_digest(card)
        card_type = "research"
    cost = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    requests = int(cost.get("llm_calls") or 0) + int(cost.get("searches") or 0)
    return {"ok": status not in _FAILED, "card": card, "card_type": card_type,
            "digest": digest, "cost": {"requests": requests, "tokens": 0},
            "dossier_id": None, "degraded": status in ("partial", "failed", "cancelled",
                                                        "budget_exhausted"),
            "status": status, "task_status": str(card.get("task_status") or ""),
            "not_saved": True, "save_reason": "read_only",
            "incremental": bool(card.get("incremental")),
            "handoff": card.get("handoff") if isinstance(card.get("handoff"), dict) else None}


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    registry.register("collect_research", _collect_research, params=["topic", "followup"],
                      permission="read", cost="heavy", est_s=60.0,
                      description="按主题主动收集资料：拆子问题、多轮检索、读页摘录、"
                                  "产出带逐字引文的资料简报（只读、不建档、不判真假）",
                      validators={"topic": topic_param, "followup": followup_param})
