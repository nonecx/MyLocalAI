# -*- coding: utf-8 -*-
"""领域监测订阅存储（专业用户 B · 阶段 3，schema v8）。

与 BatchStore/SessionStore 同模式：独立连接 + migrate 幂等。订阅归档案工作区
（v6 profile_id 分家），监测证据落订阅专属档案（dossiers.input_kind='topic'），
每轮运行一行 subscription_runs（new_count 与 URL 差集在 summary JSON 里）。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

from mylocalai.dossier.store import _now, connect, default_db_path, migrate

__all__ = ["SubscriptionStore", "MIN_FREQUENCY_S", "MAX_DOMAINS"]

MIN_FREQUENCY_S = 300          # 频率下限 5 分钟（防把搜索引擎打爆）
MAX_DOMAINS = 3                # 每轮 site: 查询变体上限（检索层同口径）
MAX_TOPIC = 200
MAX_KEYWORDS = 10
MAX_KEYWORD = 50
VALID_RUN_STATUS = ("running", "ok", "partial", "blocked", "error")


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _row_to_sub(row: Any) -> Dict[str, Any]:
    out = dict(row)
    out["keywords"] = _load(out.get("keywords"), [])
    out["domains"] = _load(out.get("domains"), [])
    out["enabled"] = bool(out.get("enabled"))
    return out


class SubscriptionStore:
    """订阅域读写门面（subscriptions + subscription_runs；线程安全：RLock + WAL）。"""

    def __init__(self, db_path: Optional[object] = None) -> None:
        self._conn = connect(db_path if db_path is not None else default_db_path())
        self._lock = threading.RLock()
        with self._lock:
            migrate(self._conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def active_profile_id(self) -> int:
        from mylocalai.dossier.profiles import active_profile_id
        try:
            with self._lock:
                return active_profile_id(self._conn)
        except Exception:  # noqa: BLE001
            return 0

    # ---- 订阅 ----

    def create_subscription(self, topic: str, keywords: List[str], domains: List[str],
                            frequency_s: int, profile_id: Optional[int] = None,
                            dossier_id: Optional[int] = None) -> int:
        topic = str(topic or "").strip()
        if not topic:
            raise ValueError("监测主题不能为空")
        if len(topic) > MAX_TOPIC:
            raise ValueError(f"监测主题过长（最多 {MAX_TOPIC} 字）")
        if profile_id is None:
            profile_id = self.active_profile_id() or None
        now = _now()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO subscriptions (profile_id, topic, keywords, domains,"
                " frequency_s, enabled, dossier_id, next_run_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (int(profile_id) if profile_id is not None else None, topic,
                 _dump(list(keywords or [])), _dump(list(domains or [])[:MAX_DOMAINS]),
                 int(frequency_s),
                 int(dossier_id) if dossier_id is not None else None,
                 now, now, now))
            return int(cur.lastrowid)

    def get_subscription(self, sub_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM subscriptions WHERE id = ?",
                                     (int(sub_id),)).fetchone()
        return _row_to_sub(row) if row else None

    def list_subscriptions(self, profile_id: Optional[int] = None,
                           limit: int = 100) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM subscriptions"
        args: List[Any] = []
        if profile_id is not None:
            sql += " WHERE profile_id = ?"
            args.append(int(profile_id))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        with self._lock:
            return [_row_to_sub(r) for r in self._conn.execute(sql, args)]

    def update_subscription(self, sub_id: int, **fields: Any) -> Dict[str, Any]:
        """更新订阅（topic/keywords/domains/frequency_s/enabled）；返回更新后的行。"""
        allowed = {"topic", "keywords", "domains", "frequency_s", "enabled",
                   "next_run_at", "last_run_at", "last_status", "paused_reason",
                   "dossier_id"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise KeyError(f"不允许更新的字段: {key}")
            if key in ("keywords", "domains"):
                value = _dump(list(value or [])[:MAX_DOMAINS if key == "domains"
                                               else MAX_KEYWORDS])
            elif key == "enabled":
                value = 1 if value else 0
            elif key == "frequency_s":
                value = int(value)
            sets.append(f"{key} = ?")
            args.append(value)
        if not sets:
            row = self.get_subscription(int(sub_id))
            if row is None:
                raise ValueError(f"订阅不存在: {sub_id}")
            return row
        sets.append("updated_at = ?")
        args.append(_now())
        args.append(int(sub_id))
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE subscriptions SET {', '.join(sets)} WHERE id = ?", args)
        row = self.get_subscription(int(sub_id))
        if row is None:
            raise ValueError(f"订阅不存在: {sub_id}")
        return row

    def delete_subscription(self, sub_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM subscriptions WHERE id = ?", (int(sub_id),))

    def due_subscriptions(self, now: str, limit: int = 5) -> List[Dict[str, Any]]:
        """到期订阅：enabled 且 next_run_at <= now（ISO 字符串可比，同库同源时戳）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subscriptions WHERE enabled = 1 AND next_run_at IS NOT NULL"
                " AND next_run_at <= ? ORDER BY next_run_at ASC, id ASC LIMIT ?",
                (str(now), max(1, int(limit)))).fetchall()
        return [_row_to_sub(r) for r in rows]

    def subscription_profile_id(self, sub_id: int) -> Optional[int]:
        with self._lock:
            row = self._conn.execute("SELECT profile_id FROM subscriptions WHERE id = ?",
                                     (int(sub_id),)).fetchone()
        return int(row["profile_id"]) if row and row["profile_id"] is not None else None

    # ---- 运行记录 ----

    def start_run(self, sub_id: int) -> int:
        now = _now()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO subscription_runs (subscription_id, started_at, status,"
                " created_at) VALUES (?, ?, 'running', ?)",
                (int(sub_id), now, now))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, *, results_count: int = 0,
                   new_count: int = 0, summary: Optional[Dict[str, Any]] = None,
                   error: str = "") -> None:
        if status not in VALID_RUN_STATUS:
            raise ValueError(f"run status 非法: {status}")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE subscription_runs SET finished_at = ?, status = ?,"
                " results_count = ?, new_count = ?, summary = ?, error = ? WHERE id = ?",
                (_now(), status, int(results_count), int(new_count),
                 _dump(summary or {}), str(error or "")[:500], int(run_id)))

    def runs(self, sub_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subscription_runs WHERE subscription_id = ?"
                " ORDER BY id DESC LIMIT ?", (int(sub_id), max(1, int(limit)))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = _load(d.get("summary"), {})
            out.append(d)
        return out
