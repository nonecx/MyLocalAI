# -*- coding: utf-8 -*-
"""Profile 工作区 API（v6 多 Profile 分家，2026-11-20 角色扩展）。

端点（全部本地、无鉴权——与既有 API 一致的单用户口径）：
  GET    /api/profiles              列表（含计数与激活态）
  POST   /api/profiles              新建 {key, display_name, role}
  PUT    /api/profiles/{id}         改名/改角色 {display_name?, role?}
  POST   /api/profiles/{id}/activate   切换激活档案
  DELETE /api/profiles/{id}         删除空档案（有数据/激活态/最后一个 → 409）

错误语义（沿用本项目 400/404/409 口径，detail 带中文说明）：
  - key 非法/重复、角色越界、名称为空 → 400
  - 档案不存在 → 404
  - 删除被保护（还有数据/是激活态/是最后一个）→ 409

store 经 get_profile_store() 单例取（测试注入点，与 session/dossier 同模式）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mylocalai.dossier.profiles import ROLES, ROLE_DESCRIPTIONS, ROLE_LABELS, \
    DEFAULT_ROLE, ProfileError, ProfileStore

router = APIRouter()

_profile_store: Optional[ProfileStore] = None


def get_profile_store() -> ProfileStore:
    """Profile 工作区存储单例（测试注入点）。"""
    global _profile_store
    if _profile_store is None:
        _profile_store = ProfileStore()
    return _profile_store


class ProfileCreateRequest(BaseModel):
    """POST /api/profiles 请求体。"""
    key: str
    display_name: str
    role: str = DEFAULT_ROLE


class ProfileUpdateRequest(BaseModel):
    """PUT /api/profiles/{id} 请求体（两字段都可选，至少给一个）。"""
    display_name: Optional[str] = None
    role: Optional[str] = None


def _profile_or_404(profile_id: int) -> Dict[str, Any]:
    row = get_profile_store().get_profile(int(profile_id))
    if row is None:
        raise HTTPException(status_code=404, detail=f"档案不存在: {profile_id}")
    return row


@router.get("/api/profiles")
def profiles_list() -> Dict[str, Any]:
    """全部档案工作区（id 升序）+ 当前激活档案 + 角色枚举（含一句能力说明）。

    roles 段的 description 取自 dossier/profiles.py 的 ROLE_DESCRIPTIONS，
    是前端「切换角色」菜单的唯一说明来源——前端不再自留一份文案，
    避免"界面说一套、文档说一套"。
    """
    store = get_profile_store()
    profiles = store.list_profiles()
    active = next((p for p in profiles if p.get("is_active")), store.active())
    return {"profiles": profiles, "active": active,
            "roles": [{"role": r, "label": ROLE_LABELS.get(r, r),
                       "description": ROLE_DESCRIPTIONS.get(r, "")} for r in ROLES]}


@router.post("/api/profiles")
def profiles_create(req: ProfileCreateRequest) -> Dict[str, Any]:
    """新建档案工作区；key/角色/名称非法 → 400。"""
    try:
        pid = get_profile_store().create_profile(req.key, req.display_name, req.role)
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"profile": _profile_or_404(pid)}


@router.put("/api/profiles/{profile_id}")
def profiles_update(profile_id: int, req: ProfileUpdateRequest) -> Dict[str, Any]:
    """改名/改角色（数据不随角色搬迁）。"""
    _profile_or_404(int(profile_id))
    try:
        row = get_profile_store().update_profile(int(profile_id), req.display_name, req.role)
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"profile": row}


@router.post("/api/profiles/{profile_id}/activate")
def profiles_activate(profile_id: int) -> Dict[str, Any]:
    """切换激活档案（之后的会话/档案/画像默认都落这里）。"""
    try:
        row = get_profile_store().set_active(int(profile_id))
    except ProfileError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"profile": row}


@router.delete("/api/profiles/{profile_id}")
def profiles_delete(profile_id: int) -> Dict[str, Any]:
    """删除空档案；被保护（有数据/激活态/最后一个）→ 409，不存在 → 404。"""
    store = get_profile_store()
    if store.get_profile(int(profile_id)) is None:
        raise HTTPException(status_code=404, detail=f"档案不存在: {profile_id}")
    try:
        store.delete_profile(int(profile_id))
    except ProfileError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted": True, "id": int(profile_id)}
