# -*- coding: utf-8 -*-
"""材料批次存储（专业用户 A · 材料分析，schema v7，2026-11-20 阶段 2）。

与 SessionStore 同模式：独立连接 + migrate 幂等；批次/条目全部读写经本模块，
编排（assistant/tools_batch.py）与 web（web/batch_api.py）都不直连 SQL。

分家：batches.profile_id 归属档案工作区（v6），list/get 按 profile_id 过滤；
跨工作区访问由 web 层按"不存在"处理（不泄露存在性）。

R5 语义分列（2026-11）：failed_count 只记真失败（条目 error），blocked 落在
summary JSON、由 get/list 读侧派生 blocked_count（表结构不动）。另开
evidence_for_dossiers 只读口，供批次汇总三要素取档案证据。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

from mylocalai.dossier.store import _now, connect, default_db_path, migrate

__all__ = ["BatchStore"]

VALID_SOURCES = ("text", "file", "urls")
VALID_BATCH_STATUS = ("running", "done", "partial", "failed", "cancelled")
VALID_ITEM_STATUS = ("pending", "running", "ok", "error", "blocked", "not_run")

#: R5 语义分列后的批次终态集合（登记表清理等处共用口径）
TERMINAL_BATCH_STATUS = ("done", "partial", "failed", "cancelled")


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _blocked_of(summary: Any) -> int:
    """从 summary JSON 派生 blocked 计数（R5 读写对齐的读侧）。

    batches 表没有 blocked_count 列（schema 不动），blocked 落在 summary JSON 里；
    get/list 两处出口在此统一派生，web 列表"已拦截 N"与汇总卡同口径。
    """
    try:
        value = (summary or {}).get("blocked")
        return max(0, int(value)) if value is not None else 0
    except (TypeError, ValueError, AttributeError):
        return 0


class BatchStore:
    """批次域读写门面（batches + batch_items 两表；线程安全：RLock + WAL 单写者）。"""

    def __init__(self, db_path: Optional[object] = None) -> None:
        self._conn = connect(db_path if db_path is not None else default_db_path())
        self._lock = threading.RLock()
        with self._lock:
            migrate(self._conn)   # 幂等：确保 v7 表存在

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def active_profile_id(self) -> int:
        """当前激活的档案工作区 id（app_state 单行，与 batches 同库）。"""
        from mylocalai.dossier.profiles import active_profile_id
        try:
            with self._lock:
                return active_profile_id(self._conn)
        except Exception:  # noqa: BLE001  激活态读失败返 0（调用方按不过滤降级）
            return 0

    # ---- 批次 ----

    def create_batch(self, title: str, source: str, items: List[Dict[str, Any]],
                     profile_id: Optional[int] = None) -> int:
        """建批次 + 条目（一条 INSERT 批次 + executemany 条目）；返回 batch_id。

        items 每项 {"text"?, "url"?}：text 模式只有 text；urls 模式只有 url
        （原文由编排期抓取后回填 raw_text）。
        """
        assert source in VALID_SOURCES, f"source 非法: {source}"
        now = _now()
        if profile_id is None:
            # v6 分家：未显式指定 → 归当前激活档案（与 sessions/dossiers 同口径）
            profile_id = self.active_profile_id() or None
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO batches (profile_id, title, source, item_count,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (int(profile_id) if profile_id is not None else None,
                 str(title or "")[:100], source, len(items), now, now))
            batch_id = int(cur.lastrowid)
            self._conn.executemany(
                "INSERT INTO batch_items (batch_id, position, raw_text, url,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(batch_id, i, str(it.get("text") or ""), str(it.get("url") or ""),
                  now, now) for i, it in enumerate(items)])
            return batch_id

    def get_batch(self, batch_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM batches WHERE id = ?",
                                     (int(batch_id),)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["summary"] = _load(out.get("summary"), {})
        out["blocked_count"] = _blocked_of(out["summary"])   # R5 读侧分列
        return out

    def list_batches(self, profile_id: Optional[int] = None,
                     limit: int = 50) -> List[Dict[str, Any]]:
        """批次列表（created_at 倒序）；profile_id 非 None 时只列该工作区。

        R5：failed_count 只记真失败（error），blocked 从 summary JSON 派生为
        blocked_count 一并列出——列表页"失败/已拦截"从此分列。
        """
        sql = ("SELECT id, profile_id, title, source, item_count, ok_count,"
               " failed_count, not_run_count, status, summary, created_at,"
               " updated_at FROM batches")
        args: List[Any] = []
        if profile_id is not None:
            sql += " WHERE profile_id = ?"
            args.append(int(profile_id))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(sql, args)]
        for row in rows:
            row["blocked_count"] = _blocked_of(_load(row.get("summary"), {}))
            row.pop("summary", None)   # 列表不背 JSON 正文（与旧形状兼容）
        return rows

    def set_batch_status(self, batch_id: int, status: str, *,
                         ok_count: Optional[int] = None,
                         failed_count: Optional[int] = None,
                         not_run_count: Optional[int] = None,
                         summary: Optional[Dict[str, Any]] = None,
                         error: str = "") -> None:
        assert status in VALID_BATCH_STATUS, f"status 非法: {status}"
        sets = ["status = ?", "updated_at = ?"]
        args: List[Any] = [status, _now()]
        if ok_count is not None:
            sets.append("ok_count = ?")
            args.append(int(ok_count))
        if failed_count is not None:
            sets.append("failed_count = ?")
            args.append(int(failed_count))
        if not_run_count is not None:
            sets.append("not_run_count = ?")
            args.append(int(not_run_count))
        if summary is not None:
            sets.append("summary = ?")
            args.append(_dump(summary))
        if error:
            sets.append("error = ?")
            args.append(str(error)[:500])
        args.append(int(batch_id))
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE batches SET {', '.join(sets)} WHERE id = ?", args)

    def batch_profile_id(self, batch_id: int) -> Optional[int]:
        with self._lock:
            row = self._conn.execute("SELECT profile_id FROM batches WHERE id = ?",
                                     (int(batch_id),)).fetchone()
        return int(row["profile_id"]) if row and row["profile_id"] is not None else None

    def evidence_for_dossiers(self, dossier_ids: List[Any]) -> Dict[int, List[Dict[str, Any]]]:
        """按档案 id 批量读证据行（规格 §5 三要素的原料；只读）。

        同库 evidence_entries 表——批次条目带 dossier_id，汇总层的立场矩阵/
        证据重合/信源战绩都从这里取数。编排（assistant/tools_batch.py）与 web
        层都只持有本门面、不直连 SQL（文件头同口径），所以读取口放在这里。
        返回 {dossier_id: [证据 dict]}；id 非法/无证据的键不出现。
        """
        ids: List[int] = []
        for raw in (dossier_ids or []):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                ids.append(value)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM evidence_entries"
                f" WHERE dossier_id IN ({placeholders}) ORDER BY dossier_id, batch, id",
                ids).fetchall()
        out: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(int(row["dossier_id"]), []).append(dict(row))
        return out

    # ---- 条目 ----

    def items(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batch_items WHERE batch_id = ? ORDER BY position",
                (int(batch_id),)).fetchall()
        return [dict(r) for r in rows]

    def item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM batch_items WHERE id = ?",
                                     (int(item_id),)).fetchone()
        return dict(row) if row else None

    def update_item(self, item_id: int, **fields: Any) -> None:
        """更新条目字段（status/raw_text/dossier_id/label/risk_score/error/group_id）。"""
        allowed = {"status", "raw_text", "dossier_id", "label", "risk_score",
                   "error", "group_id"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise KeyError(f"不允许更新的字段: {key}")
            if key == "status":
                assert value in VALID_ITEM_STATUS, f"item status 非法: {value}"
            if key == "dossier_id" and value is not None:
                value = int(value)
            if key == "risk_score" and value is not None:
                value = float(value)
            sets.append(f"{key} = ?")
            args.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        args.append(_now())
        args.append(int(item_id))
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE batch_items SET {', '.join(sets)} WHERE id = ?", args)
