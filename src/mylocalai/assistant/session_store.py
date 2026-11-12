# -*- coding: utf-8 -*-
"""会话域三表读写（sessions / messages / tool_runs，M2）。

与档案域同库（单文件 SQLite + WAL）。会话上下文的裁剪/摘要在 context.py 做，
本模块只做忠实的表读写；所有 JSON 参数落库前 dumps、读出后 loads。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

from mylocalai.core.privacy import PrivacyPreferences, load_preferences
from mylocalai.dossier.store import _expiry, connect, default_db_path

__all__ = ["SessionStore"]

VALID_ROLES = ("user", "assistant", "system_note")
VALID_KINDS = ("text", "card", "tool_event", "refusal")


def _now() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, db_path: Optional[object] = None,
                 privacy: Optional[PrivacyPreferences] = None,
                 persist_input: Optional[bool] = None) -> None:
        self._conn = connect(db_path if db_path is not None else default_db_path())
        self._privacy = privacy or load_preferences()
        self._persist_input_override = persist_input
        self._lock = threading.RLock()
        from mylocalai.dossier.store import migrate
        with self._lock:
            migrate(self._conn)   # 幂等：确保会话域表存在

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _can_persist_input(self) -> bool:
        if self._persist_input_override is not None:
            return bool(self._persist_input_override)
        return self._privacy.raw_input_allowed()

    def _can_persist_result(self) -> bool:
        return self._privacy.result_allowed()

    # ---- sessions ----

    def create_session(self, title: str = "") -> int:
        now = _now()
        persisted_title = str(title or "") if self._can_persist_input() else ""
        raw_expiry = _expiry(self._privacy.retention_days("raw_input")) if persisted_title else None
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO sessions (title, created_at, updated_at, raw_input_expires_at)"
                " VALUES (?, ?, ?, ?)",
                (persisted_title, now, now, raw_expiry))
            return int(cur.lastrowid)

    def get_session(self, session_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id = ?",
                                     (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, include_archived: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sessions"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, (max(1, int(limit)),))]

    def rename_session(self, session_id: int, title: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                               (str(title or ""), _now(), session_id))

    def set_archived(self, session_id: int, archived: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE sessions SET archived = ?, updated_at = ? WHERE id = ?",
                               (1 if archived else 0, _now(), session_id))

    def touch_session(self, session_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?",
                               (_now(), session_id))

    # ---- messages ----

    def add_message(self, session_id: int, role: str, content: str, kind: str = "text",
                    tool_run_id: Optional[int] = None) -> int:
        assert role in VALID_ROLES, f"role 非法: {role}"
        assert kind in VALID_KINDS, f"kind 非法: {kind}"
        if kind == "tool_event":
            category = "operational"
            allowed = True
        else:
            category = "raw_input" if role == "user" or role == "system_note" else "results"
            allowed = self._can_persist_input() if category == "raw_input" else self._can_persist_result()
        persisted = str(content or "") if allowed and category != "operational" else ""
        expires = (_expiry(self._privacy.retention_days(category))
                   if allowed and category in ("raw_input", "results") else None)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO messages (session_id, role, kind, content, tool_run_id, created_at,"
                " raw_input_expires_at, result_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, role, kind, persisted, tool_run_id, _now(),
                 expires if category == "raw_input" else None,
                 expires if category == "results" else None))
            self._conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?",
                               (_now(), session_id))
            return int(cur.lastrowid)

    def messages(self, session_id: int, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """会话消息按时间正序；limit 给定时取最近的 N 条（仍按正序返回）。"""
        if limit is not None:
            rows = self._conn.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE session_id = ?"
                " ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (session_id, max(1, int(limit)))).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,)).fetchall()
        with self._lock:
            return [dict(r) for r in rows]

    # ---- tool_runs ----

    @staticmethod
    def _redact_args(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): SessionStore._redact_args(item) for key, item in value.items()}
        if isinstance(value, list):
            return [SessionStore._redact_args(item) for item in value]
        if isinstance(value, str):
            return ""  # tool args may contain raw user text, URLs, or model payloads
        return value

    def start_run(self, session_id: int, tool: str, args: Optional[Dict[str, Any]]) -> int:
        persisted_args = args or {}
        if not self._can_persist_input():
            persisted_args = self._redact_args(persisted_args)
        raw_expiry = _expiry(self._privacy.retention_days("raw_input")) if self._can_persist_input() else None
        result_expiry = _expiry(self._privacy.retention_days("results")) if self._can_persist_result() else None
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO tool_runs (session_id, tool, args, status, created_at,"
                " input_expires_at, result_expires_at) VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (session_id, str(tool or ""), json.dumps(persisted_args, ensure_ascii=False), _now(),
                 raw_expiry, result_expiry))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, degraded: bool = False,
                   duration_s: float = 0.0, requests: int = 0, tokens: int = 0,
                   dossier_id: Optional[int] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tool_runs SET status = ?, degraded = ?, duration_s = ?, requests = ?,"
                " tokens = ?, dossier_id = COALESCE(?, dossier_id) WHERE id = ?",
                (str(status or ""), 1 if degraded else 0, float(duration_s), int(requests),
                 int(tokens), dossier_id, run_id))

    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tool_runs WHERE id = ?", (run_id,)).fetchone()
        d = dict(row) if row else None
        if d:
            d["args"] = json.loads(d.get("args") or "{}")
        return d

    def runs_for_session(self, session_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tool_runs WHERE session_id = ? ORDER BY id",
                (session_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["args"] = json.loads(d.get("args") or "{}")
            d["degraded"] = bool(d["degraded"])
            out.append(d)
        return out
