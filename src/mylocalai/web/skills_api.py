# -*- coding: utf-8 -*-
"""技能工作流偏好 API（设置页“技能”）：两个新技能的模式与协同开关。

- GET  /api/skills/preferences → 当前配置（含 warning）
- PUT  /api/skills/preferences → 全量校验后原子写；非法 400
只读写本机配置，不涉及密钥、不外发、不触碰用户内容。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException

from mylocalai.assistant.skills.preferences import load_preferences, save_preferences

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("/preferences")
def get_skills_preferences() -> Dict[str, Any]:
    return load_preferences()


@router.put("/preferences")
def put_skills_preferences(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        return save_preferences(body)
    except (TypeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
