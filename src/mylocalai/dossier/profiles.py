# -*- coding: utf-8 -*-
"""用户 Profile 工作区（2026-11-20 角色扩展，schema v6）。

定位：本系统是**单用户本地工具**（无账号、无鉴权，见工作指导 §2.4 非目标），
"普通用户 / 专业用户"因此不做成账号体系，而做成同一产品内的多个**工作区
（Profile）**——每个工作区有自己的会话、档案与（后续阶段的）订阅数据，
顶栏一键切换，数据按 profile_id 分家。

角色（profiles.role，封闭枚举）只决定三件事：
  - layperson  普通用户：结论卡简化呈现（无风险分数值），能力不裁剪；
  - analyst    专业用户 A：材料批量分析入口（阶段 2 的面板与批量编排）；
  - monitor    专业用户 B：领域监测订阅入口（阶段 3 的面板与调度）；
  - full       不按角色裁剪任何能力（v6 迁移给存量数据用的默认档，
                保证旧行为逐位不变；也是"我全都要"用户的显式选择）。

边界（与全项目文化一致）：
  - 任何失败都抛**带中文说明的 ValueError/PermissionError**，由 web 层映射
    400/409，绝不静默；
  - 删除保护：最后一个档案、当前激活的档案、仍有会话/档案/订阅的档案一律拒删
    （删除不是本产品的常规操作，见"删除档案永远不是助手工具"同一取向）；
  - 读路径永不抛：list/get 失败由调用方按"空列表/None"降级。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from mylocalai.dossier.store import _now, connect, default_db_path, migrate

__all__ = ["ROLES", "ROLE_LABELS", "DEFAULT_ROLE", "ProfileError", "ProfileStore",
           "active_profile_id"]

#: 角色封闭枚举（store.py v6 迁移的 CHECK 约束与此一致，改必须两处同步）。
ROLES = ("layperson", "analyst", "monitor", "full")

#: 角色中文名（前端切换器与设置弹层共用这一份口径）。
ROLE_LABELS = {
    "layperson": "普通用户",
    "analyst": "专业用户 · 材料分析",
    "monitor": "专业用户 · 领域监测",
    "full": "全部能力",
}

#: v6 迁移与"新建档案"按钮之外的兜底档：不裁剪能力。
DEFAULT_ROLE = "full"

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_MAX_DISPLAY_NAME = 40

#: 各角色的一句话说明（前端切换器 subtitle；纯展示，不进任何 prompt）。
ROLE_DESCRIPTIONS = {
    "layperson": "只做言论核查：输入一条消息，拿回 simplified 的结论卡",
    "analyst": "分析自行搜集的多条材料：批量核查 + 跨条汇总",
    "monitor": "按领域自动搜集信息：订阅话题，定时运行，变化摘要",
    "full": "不按角色裁剪能力，全部工具可用",
}


class ProfileError(ValueError):
    """Profile 操作的参数/业务错误（web 层映射 400/409）。"""


def active_profile_id(conn: sqlite3.Connection) -> int:
    """当前激活的档案工作区 id（app_state 单行）；缺失/损坏时退最老档案。"""
    row = conn.execute("SELECT value FROM app_state WHERE key = 'active_profile_id'").fetchone()
    if row is not None:
        try:
            pid = int(str(row["value"]))
        except (TypeError, ValueError):
            pid = 0
        if pid > 0 and conn.execute("SELECT 1 FROM profiles WHERE id = ?",
                                    (pid,)).fetchone() is not None:
            return pid
    oldest = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()
    return int(oldest["id"]) if oldest is not None else 0


class ProfileStore:
    """Profile 工作区 CRUD + 激活态（与档案/会话域同库，v6）。

    线程安全沿用 store 的模式：内部一把 RLock + 独立连接（WAL 单写者）。
    """

    def __init__(self, db_path: Optional[object] = None) -> None:
        self._conn = connect(db_path if db_path is not None else default_db_path())
        self._lock = threading.RLock()
        with self._lock:
            migrate(self._conn)   # 幂等：确保 v6 表存在 + 默认档案已回填

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 读 ----

    def list_profiles(self) -> List[Dict[str, Any]]:
        """全部档案（id 升序），各带会话/档案计数与是否激活。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, key, display_name, role, created_at, updated_at"
                " FROM profiles ORDER BY id").fetchall()
            out: List[Dict[str, Any]] = []
            active = active_profile_id(self._conn)
            for r in rows:
                pid = int(r["id"])
                sessions = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions WHERE profile_id = ?",
                    (pid,)).fetchone()["n"]
                dossiers = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM dossiers WHERE profile_id = ?",
                    (pid,)).fetchone()["n"]
                out.append({
                    "id": pid,
                    "key": str(r["key"] or ""),
                    "display_name": str(r["display_name"] or ""),
                    "role": str(r["role"] or DEFAULT_ROLE),
                    "role_label": ROLE_LABELS.get(str(r["role"] or ""), str(r["role"] or "")),
                    "role_description": ROLE_DESCRIPTIONS.get(str(r["role"] or ""), ""),
                    "created_at": str(r["created_at"] or ""),
                    "updated_at": str(r["updated_at"] or ""),
                    "session_count": int(sessions or 0),
                    "dossier_count": int(dossiers or 0),
                    "is_active": pid == active,
                })
            return out

    def get_profile(self, profile_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, key, display_name, role, created_at, updated_at"
                " FROM profiles WHERE id = ?", (int(profile_id),)).fetchone()
        if row is None:
            return None
        return {"id": int(row["id"]), "key": str(row["key"] or ""),
                "display_name": str(row["display_name"] or ""),
                "role": str(row["role"] or DEFAULT_ROLE),
                "role_label": ROLE_LABELS.get(str(row["role"] or ""), ""),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or "")}

    def get_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM profiles WHERE key = ?", (str(key or ""),)).fetchone()
        return self.get_profile(int(row["id"])) if row else None

    def active(self) -> Dict[str, Any]:
        """当前激活档案（完整行）；库里一个档案都没有时返回空 dict（调用方降级）。"""
        with self._lock:
            pid = active_profile_id(self._conn)
        return self.get_profile(pid) if pid > 0 else {}

    def active_id(self) -> int:
        with self._lock:
            return active_profile_id(self._conn)

    # ---- 写 ----

    def create_profile(self, key: str, display_name: str,
                       role: str = DEFAULT_ROLE) -> int:
        """新建档案工作区；key 非法/重复、角色越界 → ProfileError（400）。"""
        key = str(key or "").strip()
        display_name = str(display_name or "").strip()
        role = str(role or "").strip() or DEFAULT_ROLE
        if not _KEY_RE.match(key):
            raise ProfileError(
                "档案 key 只能用小写字母/数字/连字符/下划线，1-32 字符，且以字母或数字开头")
        if not display_name:
            raise ProfileError("档案显示名称不能为空")
        if len(display_name) > _MAX_DISPLAY_NAME:
            raise ProfileError(f"档案显示名称过长（最多 {_MAX_DISPLAY_NAME} 字）")
        if role not in ROLES:
            raise ProfileError(f"未知角色: {role}（可选 {'/'.join(ROLES)}）")
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM profiles WHERE key = ?",
                                  (key,)).fetchone() is not None:
                raise ProfileError(f"档案 key 已存在: {key}")
            now = _now()
            cur = self._conn.execute(
                "INSERT INTO profiles (key, display_name, role, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)", (key, display_name, role, now, now))
            return int(cur.lastrowid)

    def update_profile(self, profile_id: int, display_name: Optional[str] = None,
                       role: Optional[str] = None) -> Dict[str, Any]:
        """改名/改角色（数据不随角色搬迁：历史会话与档案保留原样）。"""
        row = self.get_profile(int(profile_id))
        if row is None:
            raise ProfileError(f"档案不存在: {profile_id}")
        sets: List[str] = []
        args: List[Any] = []
        if display_name is not None:
            name = str(display_name).strip()
            if not name:
                raise ProfileError("档案显示名称不能为空")
            if len(name) > _MAX_DISPLAY_NAME:
                raise ProfileError(f"档案显示名称过长（最多 {_MAX_DISPLAY_NAME} 字）")
            sets.append("display_name = ?")
            args.append(name)
        if role is not None:
            new_role = str(role).strip()
            if new_role not in ROLES:
                raise ProfileError(f"未知角色: {role}（可选 {'/'.join(ROLES)}）")
            sets.append("role = ?")
            args.append(new_role)
        if not sets:
            return row
        sets.append("updated_at = ?")
        args.append(_now())
        args.append(int(profile_id))
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE profiles SET {', '.join(sets)} WHERE id = ?", args)
        return self.get_profile(int(profile_id)) or row

    def set_active(self, profile_id: int) -> Dict[str, Any]:
        """切换激活档案（app_state 单行）；档案不存在 → ProfileError。"""
        row = self.get_profile(int(profile_id))
        if row is None:
            raise ProfileError(f"档案不存在: {profile_id}")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO app_state (key, value) VALUES ('active_profile_id', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(int(profile_id)),))
        return row

    def delete_profile(self, profile_id: int) -> None:
        """删除空档案；拒删：不存在 / 是最后一个 / 是激活态 / 仍有数据（409 语义）。"""
        row = self.get_profile(int(profile_id))
        if row is None:
            raise ProfileError(f"档案不存在: {profile_id}")
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM profiles").fetchone()["n"]
            if int(total) <= 1:
                raise ProfileError("至少保留一个档案工作区，不能删除最后一个")
            if active_profile_id(self._conn) == int(profile_id):
                raise ProfileError("不能删除当前激活的档案，请先切换到别的档案")
            for table, label in (("sessions", "会话"), ("dossiers", "档案")):
                n = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE profile_id = ?",
                    (int(profile_id),)).fetchone()["n"]
                if int(n) > 0:
                    raise ProfileError(
                        f"该档案还有 {int(n)} 条{label}数据，为保护历史记录不支持删除；"
                        "可以改用改名或改角色")
            with self._conn:
                self._conn.execute("DELETE FROM profiles WHERE id = ?", (int(profile_id),))

    # ---- 归属查询（跨工作区防护用） ----

    def session_profile_id(self, session_id: int) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT profile_id FROM sessions WHERE id = ?",
                (int(session_id),)).fetchone()
        return int(row["profile_id"]) if row and row["profile_id"] is not None else None

    def dossier_profile_id(self, dossier_id: int) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT profile_id FROM dossiers WHERE id = ?",
                (int(dossier_id),)).fetchone()
        return int(row["profile_id"]) if row and row["profile_id"] is not None else None

    def role_of(self, profile_id: Optional[int]) -> str:
        """档案角色；档案不存在/为 None → DEFAULT_ROLE（不裁剪）。"""
        if profile_id is None:
            return DEFAULT_ROLE
        row = self.get_profile(int(profile_id))
        return str((row or {}).get("role") or DEFAULT_ROLE)
