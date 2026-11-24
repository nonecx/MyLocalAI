# -*- coding: utf-8 -*-
"""collect_and_verify 工具：一句话同时做资料收集 + 权威核验的复合能力。

口径：
  - 只读、不建档；两段各自跑完整 pipeline，互不吞并；任一失败保留另一段。
  - 研究阶段**不做自动接力**（避免同一轮二次核验）；权威段用用户明确给出的 claim。
  - 复合卡 `card_type="composite"`，含 research/authority 两个子卡；状态聚合见
    tools_skill_cards.composite_card。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from mylocalai.assistant.registry import ToolRegistry, ValidationError
from mylocalai.assistant.skills.preferences import load_preferences
from mylocalai.assistant.tools_skill_cards import (authority_card, composite_card,
                                                   composite_digest, research_card)

__all__ = ["register_all", "topic_param", "claim_param"]

_FAILED = ("failed", "cancelled", "budget_exhausted")
_MAX_TOPIC = 500
_MAX_CLAIM = 4000


def _required(value: Any, name: str, max_len: int) -> str:
    if value is None:
        raise ValidationError(f"缺少参数 {name}")
    if not isinstance(value, str):
        raise ValidationError(f"参数 {name} 必须是字符串")
    value = value.strip()
    if not value:
        raise ValidationError(f"参数 {name} 不能为空")
    if len(value) > max_len:
        raise ValidationError(f"参数 {name} 过长（最多 {max_len} 字）")
    return value


def topic_param(value: Any) -> str:
    return _required(value, "topic", _MAX_TOPIC)


def claim_param(value: Any) -> str:
    return _required(value, "claim", _MAX_CLAIM)


def _prefs() -> Dict[str, Any]:
    try:
        return load_preferences()
    except Exception:  # noqa: BLE001
        return {"research": {"workflow_mode": "pipeline"},
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


def _progress(ctx: Any):
    events = getattr(ctx, "events", None)
    if events is None:
        return None

    def _emit(label: str, done: int, total: int) -> None:
        try:
            events.emit("progress", component=str(label), done=int(done), total=int(total))
        except Exception:  # noqa: BLE001
            pass
    return _emit


def _failed_research(reason: str) -> Dict[str, Any]:
    return {"card_type": "research", "status": "failed", "task_status": "",
            "as_of": "", "degraded": True, "topic": "", "scope": {},
            "sub_questions": [], "key_findings": [], "leads": [], "evidence": [],
            "sources": {"verified": [], "unverified": []}, "timeline": [],
            "conflicts": [], "analysis": {}, "followups": [], "gaps": [],
            "errors": [{"stage": "research", "category": "unavailable", "detail": reason}],
            "counts": {"sub_questions": 0, "searches": 0, "pages": 0, "llm_calls": 0,
                       "evidence": 0},
            "execution": {"mode": "pipeline", "steps": 0, "stop_reason": "unavailable"},
            "incremental": False, "base_as_of": ""}


def _failed_authority(reason: str) -> Dict[str, Any]:
    return {"card_type": "authority", "status": "failed", "task_status": "",
            "as_of": "", "degraded": True, "claims": [], "evidence": [],
            "sources": {"verified": [], "unverified": []}, "conflicts": [],
            "analysis": [], "limitations": [], "gaps": [],
            "errors": [{"stage": "authority", "category": "unavailable", "detail": reason}],
            "counts": {"claims": 0, "evidence": 0, "searches": 0, "pages": 0,
                       "llm_calls": 0, "official_sources": 0},
            "execution": {"mode": "pipeline", "steps": 0, "stop_reason": "unavailable"},
            "incremental": False, "base_as_of": ""}


def _collect_and_verify(ctx: Any, topic: str, claim: str) -> Dict[str, Any]:
    prefs = _prefs()
    if not bool((prefs.get("composite") or {}).get("enabled", True)):
        return {"ok": True, "card": None, "card_type": "composite", "digest":
                "[复合核验] 已在设置中关闭“允许一句话同时收集+核验”。"
                "请分别说“收集资料”或“权威核验”。",
                "cost": {"requests": 0, "tokens": 0}, "dossier_id": None,
                "degraded": False, "status": "needs_clarification",
                "task_status": "needs_clarification", "not_saved": True,
                "save_reason": "read_only"}
    try:
        research_skills = _service(ctx, "research_skills")
    except Exception as exc:  # noqa: BLE001
        research_skills = None
        research_error = f"{type(exc).__name__}: {exc}"
    else:
        research_error = ""
    try:
        authority_skills = _service(ctx, "authority_skills")
    except Exception as exc:  # noqa: BLE001
        authority_skills = None
        authority_error = f"{type(exc).__name__}: {exc}"
    else:
        authority_error = ""

    if research_skills is None:
        research = _failed_research(research_error)
    else:
        try:
            result = research_skills.run(str(topic), mode=str(
                (prefs.get("research") or {}).get("workflow_mode", "pipeline")),
                cancel_check=getattr(ctx, "cancel_check", None),
                deadline=_deadline(), on_progress=_progress(ctx))
            research = research_card(result) if isinstance(result, dict) else _failed_research(
                "research 返回非对象")
        except Exception as exc:  # noqa: BLE001
            research = _failed_research(f"{type(exc).__name__}: {exc}")

    if authority_skills is None:
        authority = _failed_authority(authority_error)
    else:
        try:
            result = authority_skills.run(str(claim), mode=str(
                (prefs.get("authority") or {}).get("workflow_mode", "pipeline")),
                origin="user", cancel_check=getattr(ctx, "cancel_check", None),
                deadline=_deadline(), on_progress=_progress(ctx))
            authority = authority_card(result) if isinstance(result, dict) else _failed_authority(
                "authority 返回非对象")
        except Exception as exc:  # noqa: BLE001
            authority = _failed_authority(f"{type(exc).__name__}: {exc}")

    card = composite_card(research, authority, {"auto": False, "reason": "用户显式双请求"})
    status = str(card.get("status") or "")
    requests = int((card.get("counts") or {}).get("searches") or 0) + int(
        (card.get("counts") or {}).get("llm_calls") or 0)
    return {"ok": status not in _FAILED, "card": card, "card_type": "composite",
            "digest": composite_digest(card), "cost": {"requests": requests, "tokens": 0},
            "dossier_id": None, "degraded": status in ("partial", "failed", "cancelled",
                                                        "budget_exhausted"),
            "status": status, "task_status": str(card.get("task_status") or ""),
            "not_saved": True, "save_reason": "read_only"}


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    registry.register("collect_and_verify", _collect_and_verify, params=["topic", "claim"],
                      permission="read", cost="heavy", est_s=120.0,
                      description="一句话同时收集某主题资料并核验其中某说法：只读、不建档；"
                                  "研究段+权威段各出子卡，合成一张复合卡",
                      validators={"topic": topic_param, "claim": claim_param})
