# -*- coding: utf-8 -*-
"""跨档案工作区防护（v6 多 Profile 分家，2026-11-20）。

对话里带档案号的工具（get_dossier / export_card / recheck_dossier / read_opinions /
deep_read）在动手之前都要过这里：档案**不存在**与档案**属于别的工作区**对外是同一种
回答——"档案 #X 不存在"。不泄露别的工作区有哪些档案号，也不让一个工作区的写入路径
（recheck）落到别的工作区的档案上。

ctx.profile_id 由 loop 每回合注入（会话所属工作区）；缺省（stub/旧装配）为 None，
None = 不检查（与加角色分家前的行为逐位一致）。
"""

from __future__ import annotations

from typing import Any, Tuple

__all__ = ["dossier_visible"]


def dossier_visible(ctx: Any, dossier_id: int) -> Tuple[bool, str]:
    """档案存在且属于当前会话所在工作区 → (True, "")。

    返回 (visible, err)：err 非空表示**库故障**（调用方按"读取失败"降级）；
    err 为空且 visible=False 表示"不存在或不属于当前工作区"（调用方按不存在处理）。
    任何形状异常都按"不存在"处理，绝不抛。
    """
    try:
        row = ctx.dossiers.store.get_dossier(int(dossier_id))
    except Exception as exc:  # noqa: BLE001  库故障折成错误串，由调用方降级
        return False, f"{type(exc).__name__}: {exc}"
    if not row:
        return False, ""
    profile_id = getattr(ctx, "profile_id", None)
    if profile_id is None:
        return True, ""   # 未注入工作区（stub/旧装配）：不检查，逐位不变
    row_profile = row.get("profile_id") if isinstance(row, dict) else None
    if row_profile is None:
        return True, ""   # v6 迁移前的存量行（migrate 已回填，这里双保险放行）
    try:
        return int(row_profile) == int(profile_id), ""
    except (TypeError, ValueError):
        return False, ""
