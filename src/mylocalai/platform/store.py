# -*- coding: utf-8 -*-
"""平台域存储（platform/store.py）：watch_queries / feed_entries / scan_runs 三表读写。

表由 dossier store 的 v6 迁移建立（schema_version=6）；本模块经同一连接工厂
（mylocalai.dossier.store.connect）打开**同一个库文件**，不另起数据库。

口径与 DossierStore 一致：
  - 单文件 SQLite + WAL，单写者（本模块自带 RLock）；
  - payload 存 JSON 文本，读写经 _dump/_load 往返；
  - 方法**不抛业务异常**给调用方崩溃：重复监控词/重复 URL 以返回值表达
    （None / False），由上层决定"已存在"的呈现方式。

保留期：feed_entries.result_expires_at 走「核查结果」类别（默认 90 天，从首次
写入起算、读取不续期），purge_expired_feed() 幂等清理；库内其他类别的清理仍由
DossierStore.purge_expired() 负责，两边互不越界。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from mylocalai.dossier.store import connect

__all__ = ["PlatformStore"]

#: 预警档位（与 platform/risk.py 的 LEVELS 同口径；此处只用于查询过滤）。
WARNING_LEVELS = ("high", "urgent")

#: feed 状态封闭集合（越界值写入即 ValueError，绝不猜）。
FEED_STATUSES = ("new", "screened", "verified", "verify_failed", "deduped")

#: scan 状态封闭集合。
SCAN_STATUSES = ("running", "completed", "partial", "failed", "skipped", "cancelled")


def _now() -> str:
    """本地时间戳（与库内其他表同格式同时区偏移；不跨包借私有助手）。"""
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


class PlatformStore:
    """平台域三表读写门面。线程安全：内部一把写锁 + 独立连接（WAL 单写者）。"""

    def __init__(self, db_path: Optional[os.PathLike | str] = None) -> None:
        from mylocalai.dossier.store import migrate
        self._conn = connect(db_path)
        self._lock = threading.RLock()
        with self._lock:
            migrate(self._conn)   # 幂等：确保 v6 平台域三表在场（老库自动补建）

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 监控词 ----

    def add_watch_query(self, query: str, note: str = "") -> Optional[int]:
        """新增监控词；query 已存在返回 None（调用方据此提示"已添加"）。"""
        q = str(query or "").strip()
        if not q:
            raise ValueError("监控词不能为空")
        with self._lock, self._conn:
            try:
                cur = self._conn.execute(
                    "INSERT INTO watch_queries (query, note, created_at) VALUES (?, ?, ?)",
                    (q, str(note or ""), _now()))
            except sqlite3.IntegrityError:
                return None
            return int(cur.lastrowid)

    def ensure_watch_query(self, query: str, note: str = "") -> int:
        """有则返回现有 id，无则创建（巡检按监控词文本定位 query_id 的入口）。"""
        q = str(query or "").strip()
        if not q:
            raise ValueError("监控词不能为空")
        existing = self.add_watch_query(q, note)
        if existing is not None:
            return existing
        for row in self.list_watch_queries():
            if row["query"] == q:
                return int(row["id"])
        raise RuntimeError(f"监控词并发创建冲突: {q}")   # 理论不可达（单写者）

    def list_watch_queries(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM watch_queries"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self._lock:
            out = []
            for r in self._conn.execute(sql):
                d = dict(r)
                d["enabled"] = bool(d["enabled"])
                out.append(d)
            return out

    def set_watch_query_enabled(self, query_id: int, enabled: bool) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE watch_queries SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, int(query_id)))
            return cur.rowcount > 0

    def delete_watch_query(self, query_id: int) -> bool:
        """删除监控词；其下 feed_entries 经外键 CASCADE 一并删除（档案不删：
        feed_entries.dossier_id 是 ON DELETE SET NULL，档案库由档案域自己管）。"""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM watch_queries WHERE id = ?", (int(query_id),))
            return cur.rowcount > 0

    # ---- 信息流 ----

    def add_feed_entry(self, *, query_id: int, title: str, url: str, snippet: str = "",
                       domain: str = "", domain_tier: str = "", discovered_at: str = "",
                       e_score: Optional[float] = None, status: str = "new",
                       scan_id: Optional[int] = None,
                       result_expires_at: Optional[str] = None) -> Optional[int]:
        """写入信息流条目；URL 已存在返回 None（去重第一道闸，调用方按 deduped 处理）。

        status 越界抛 ValueError（调用方 bug，不静默纠正）。
        """
        if status not in FEED_STATUSES:
            raise ValueError(f"feed 状态非法: {status}")
        with self._lock, self._conn:
            try:
                cur = self._conn.execute(
                    "INSERT INTO feed_entries (query_id, scan_id, title, url, snippet,"
                    " domain, domain_tier, discovered_at, e_score, status,"
                    " result_expires_at, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (int(query_id), scan_id, str(title or "")[:200], str(url or ""),
                     str(snippet or "")[:2000], str(domain or ""), str(domain_tier or ""),
                     str(discovered_at or ""), e_score, status, result_expires_at, _now()))
            except sqlite3.IntegrityError:
                return None
            return int(cur.lastrowid)

    def update_feed_entry(self, entry_id: int, **fields: Any) -> bool:
        """更新条目（深查回填：dossier_id/verdict_label/risk_score/risk_level/
        risk_reasons/e_prev/status）。白名单字段，其余键忽略（防注入任意列）。"""
        allowed = {"dossier_id", "verdict_label", "risk_score", "risk_level",
                   "risk_reasons", "e_prev", "status"}
        sets: List[str] = []
        args: List[Any] = []
        for key in ("dossier_id", "verdict_label", "risk_score", "risk_level",
                    "risk_reasons", "e_prev", "status"):
            if key not in fields:
                continue
            if key not in allowed:      # 理论上到不了（键名来自上面的元组），双保险
                continue
            value = fields[key]
            if key == "risk_reasons":
                sets.append("risk_reasons = ?")
                args.append(_dump(list(value or [])))
            elif key == "status":
                if value not in FEED_STATUSES:
                    raise ValueError(f"feed 状态非法: {value}")
                sets.append("status = ?")
                args.append(value)
            else:
                sets.append(f"{key} = ?")
                args.append(value)
        if not sets:
            return False
        args.append(int(entry_id))
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE feed_entries SET {', '.join(sets)} WHERE id = ?", args)
            return cur.rowcount > 0

    def _feed_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["risk_reasons"] = _load(d["risk_reasons"], [])
        return d

    def get_feed_entry(self, entry_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM feed_entries WHERE id = ?", (int(entry_id),)).fetchone()
        return self._feed_row(row) if row else None

    def list_feed_entries(self, *, risk: Optional[str] = None, status: Optional[str] = None,
                          limit: int = 50) -> List[Dict[str, Any]]:
        """信息流（created_at 倒序）；risk 可按单档或逗号分隔多档过滤。"""
        sql = "SELECT * FROM feed_entries"
        args: List[Any] = []
        clauses: List[str] = []
        if risk:
            levels = [r.strip() for r in str(risk).split(",") if r.strip()]
            if levels:
                clauses.append(f"risk_level IN ({','.join('?' * len(levels))})")
                args.extend(levels)
        if status:
            clauses.append("status = ?")
            args.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        with self._lock:
            return [self._feed_row(r) for r in self._conn.execute(sql, args)]

    def warnings(self, limit: int = 50) -> List[Dict[str, Any]]:
        """预警列表：risk_level ∈ {high, urgent}，按风险降序 +  id 倒序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM feed_entries WHERE risk_level IN (?, ?)"
                " ORDER BY CASE risk_level WHEN 'urgent' THEN 0 ELSE 1 END, id DESC"
                " LIMIT ?", (*WARNING_LEVELS, max(1, int(limit)))).fetchall()
        return [self._feed_row(r) for r in rows]

    def latest_e_for_query(self, query_id: int) -> Optional[float]:
        """同监控词最近一次记录的热度 E（环比基准）；从无记录返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT e_score FROM feed_entries WHERE query_id = ?"
                " AND e_score IS NOT NULL ORDER BY id DESC LIMIT 1", (int(query_id),)).fetchone()
        return float(row["e_score"]) if row else None

    def purge_expired_feed(self, *, now: Optional[str] = None) -> int:
        """清理过期的 feed 条目（results 类别）；返回删除行数。幂等。"""
        current = _now() if now is None else str(now)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM feed_entries WHERE result_expires_at IS NOT NULL"
                " AND julianday(replace(result_expires_at, 'Z', '+00:00')) <= julianday(?)",
                (current,))
            return cur.rowcount

    # ---- 巡检记录 ----

    def start_scan_run(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO scan_runs (started_at, status) VALUES (?, 'running')", (_now(),))
            return int(cur.lastrowid)

    def finish_scan_run(self, scan_id: int, status: str, *, queries: int = 0,
                        candidates: int = 0, verified: int = 0, degraded: int = 0,
                        error: str = "") -> bool:
        if status not in SCAN_STATUSES:
            raise ValueError(f"scan 状态非法: {status}")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE scan_runs SET finished_at = ?, status = ?, queries = ?,"
                " candidates = ?, verified = ?, degraded = ?, error = ? WHERE id = ?",
                (_now(), status, int(queries), int(candidates), int(verified),
                 int(degraded), str(error or "")[:500], int(scan_id)))
            return cur.rowcount > 0

    def get_scan_run(self, scan_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scan_runs WHERE id = ?", (int(scan_id),)).fetchone()
        return dict(row) if row else None

    def list_scan_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (max(1, int(limit)),))]

    def latest_scan_run(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
