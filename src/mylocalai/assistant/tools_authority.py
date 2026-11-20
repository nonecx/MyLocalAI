# -*- coding: utf-8 -*-
"""verify_authority 工具：权威核验（只读、不建档、无真假标签/数值分）。

pipeline 负责拆主张、找权威源、锚定引文、代码算 balance/lean；本工具只做参数校验、
偏好/prior 解析、服务装配、卡/digest 组装与降级。结论文案由 pipeline 代码模板逐字生成。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from mylocalai.assistant.registry import ToolRegistry, ValidationError
from mylocalai.assistant.skills.continuation import (find_prior_card, looks_like_followup,
                                                     looks_like_rerun, prior_text)
from mylocalai.assistant.skills.preferences import load_preferences
from mylocalai.assistant.tools_skill_cards import authority_card, authority_digest

__all__ = ["register_all", "text_param", "followup_param"]

_FAILED = ("failed", "cancelled", "budget_exhausted")
_MAX_TEXT = 4000


def _v_str_optional(value: Any, name: str, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"参数 {name} 必须是字符串")
    value = value.strip()
    if len(value) > max_len:
        raise ValidationError(f"参数 {name} 过长（最多 {max_len} 字）")
    return value


def text_param(value: Any) -> str:
    return _v_str_optional(value, "text", _MAX_TEXT)


def followup_param(value: Any) -> bool:
    if value is None or value is False:
        return False
    if not isinstance(value, bool):
        raise ValidationError("参数 followup 必须是布尔值")
    return value


def _prefs() -> Dict[str, Any]:
    try:
        return load_preferences()
    except Exception:  # noqa: BLE001
        return {"authority": {"workflow_mode": "pipeline"}, "warning": ""}


def _deadline() -> Optional[float]:
    try:
        from mylocalai.llm import llm_cache
        return llm_cache.deadline_at()
    except Exception:  # noqa: BLE001
        return None


def _service(ctx: Any) -> Any:
    services = getattr(ctx, "services", None)
    if services is None:
        raise KeyError("ctx.services 缺失")
    factory = services["authority_skills"]
    return factory() if callable(factory) else factory


def _progress(ctx: Any) -> Optional[Callable[[str, int, int], None]]:
    events = getattr(ctx, "events", None)
    if events is None:
        return None

    def _emit(label: str, done: int, total: int) -> None:
        try:
            events.emit("progress", component=str(label), done=int(done), total=int(total))
        except Exception:  # noqa: BLE001
            pass
    return _emit


def _unavailable(reason: str) -> Dict[str, Any]:
    detail = str(reason or "").strip()[:200] or "原因未知"
    return {"ok": False, "card": None, "card_type": "authority",
            "digest": f"[权威核验] 不可用：{detail}。本次未取得权威证据，不替它补结论。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True,
            "status": "failed", "task_status": "", "not_saved": True,
            "save_reason": "read_only"}


def _clarify(reason: str) -> Dict[str, Any]:
    return {"ok": True, "card": None, "card_type": "authority",
            "digest": f"[权威核验] 需要补充信息：{reason}",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": False,
            "status": "needs_clarification", "task_status": "needs_clarification",
            "not_saved": True, "save_reason": "read_only"}


def _verify_authority(ctx: Any, text: str = "", followup: bool = False) -> Dict[str, Any]:
    prefs = _prefs()
    prior = None
    value = str(text or "").strip()
    user_text = str(getattr(ctx, "user_text", "") or "")
    try:
        if getattr(ctx, "session_id", None) is not None:
            prior = find_prior_card(getattr(ctx, "sessions", None),
                                    getattr(ctx, "session_id"), ("authority", "composite"))
    except Exception:  # noqa: BLE001
        prior = None
    is_followup = bool(followup) or (prior is not None
                                     and looks_like_followup(user_text)
                                     and not looks_like_rerun(user_text))
    if not value and prior is not None:
        value = prior_text(prior)
    if not value:
        return _clarify("请给出要核验的说法原文（也可以给出链接；我会先抓取正文并标明来源）。")
    try:
        skills = _service(ctx)
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"权威核验流水线装配失败（{type(exc).__name__}）")

    mode = str((prefs.get("authority") or {}).get("workflow_mode", "pipeline"))
    try:
        result = skills.run(value, mode=mode, prior=prior if is_followup else None,
                            followup=is_followup, origin="user",
                            cancel_check=getattr(ctx, "cancel_check", None),
                            deadline=_deadline(), on_progress=_progress(ctx))
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"权威核验执行异常（{type(exc).__name__}）")
    if not isinstance(result, dict):
        return _unavailable("权威核验返回形状不符")
    card = authority_card(result)
    status = str(card.get("status") or "")
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    requests = int(audit.get("llm_calls") or 0) + int(audit.get("searches") or 0)
    return {"ok": status not in _FAILED, "card": card, "card_type": "authority",
            "digest": authority_digest(card), "cost": {"requests": requests, "tokens": 0},
            "dossier_id": None, "degraded": status in ("partial", "failed", "cancelled",
                                                        "budget_exhausted"),
            "status": status, "task_status": str(card.get("task_status") or ""),
            "not_saved": True, "save_reason": "read_only",
            "incremental": bool(card.get("incremental"))}


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    registry.register("verify_authority", _verify_authority, params=["text", "followup"],
                      permission="read", cost="heavy", est_s=75.0,
                      description="找权威资料证明或反驳一段说法：拆主张、核验权威源、"
                                  "逐字引文、证据倾向/有条件证明或反驳（只读、不建档）",
                      validators={"text": text_param, "followup": followup_param})
