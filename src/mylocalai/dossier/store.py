# -*- coding: utf-8 -*-
"""档案库 SQLite 存储（M1-3）。

设计要点（工作指导 §4.4）：
  - 单文件 SQLite + WAL，单进程使用，不额外加锁层；
  - payload 一律存 JSON 文本，读写经由本模块做 json.dumps/loads 往返；
  - ``schema_version`` + 顺序迁移脚本从第一天就有：每个迁移是一个 (version, sql)
    条目，按序在单个事务里执行并登记到 ``schema_migrations``，重复执行幂等；
  - 旧内存历史 ``_history`` 无可迁移数据（进程内存、重启即失），档案库从零开始。

默认库文件在 ``<项目根>/data/dossier.db``，环境变量 ``DOSSIER_DB_PATH`` 可整路径覆盖；
``data/`` 已进 .gitignore，不入库。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["SCHEMA_VERSION", "DEFAULT_DB_PATH", "connect", "migrate",
           "DossierStore", "default_db_path"]

SCHEMA_VERSION = 2

#: 顺序迁移：索引 i 的脚本把库从版本 i 升到 i+1。永不修改已发布的条目，只允许追加。
MIGRATIONS: List[str] = [
    # v1：档案域七表（会话域 sessions/messages/tool_runs 在 M2 的第二个迁移里建）
    """
    CREATE TABLE IF NOT EXISTS dossiers (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        title          TEXT NOT NULL DEFAULT '',
        input_text     TEXT NOT NULL DEFAULT '',
        input_kind     TEXT NOT NULL DEFAULT 'text' CHECK (input_kind IN ('text', 'topic')),
        schema_version INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS conclusions (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        dossier_id           INTEGER NOT NULL REFERENCES dossiers(id) ON DELETE CASCADE,
        version              INTEGER NOT NULL,
        label                TEXT NOT NULL DEFAULT '',
        risk_score           REAL,
        confidence           TEXT NOT NULL DEFAULT '',
        evidence_sufficiency TEXT NOT NULL DEFAULT '{}',
        as_of                TEXT NOT NULL DEFAULT '',
        recheck_conditions   TEXT NOT NULL DEFAULT '[]',
        raw_verdict          TEXT NOT NULL DEFAULT '{}',
        created_at           TEXT NOT NULL,
        UNIQUE (dossier_id, version)
    );
    CREATE TABLE IF NOT EXISTS evidence_entries (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        dossier_id  INTEGER NOT NULL REFERENCES dossiers(id) ON DELETE CASCADE,
        batch       INTEGER NOT NULL DEFAULT 1,
        url         TEXT NOT NULL DEFAULT '',
        domain      TEXT NOT NULL DEFAULT '',
        domain_tier TEXT NOT NULL DEFAULT '',
        source_kind TEXT NOT NULL DEFAULT '',
        stance      TEXT NOT NULL DEFAULT '',
        title       TEXT NOT NULL DEFAULT '',
        snippet     TEXT NOT NULL DEFAULT '',
        fetched_at  TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_evidence_dossier ON evidence_entries(dossier_id, batch);
    CREATE TABLE IF NOT EXISTS attention_snapshots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        dossier_id  INTEGER NOT NULL REFERENCES dossiers(id) ON DELETE CASCADE,
        taken_at    TEXT NOT NULL,
        x           REAL NOT NULL DEFAULT 0,
        y           REAL NOT NULL DEFAULT 0,
        z           REAL NOT NULL DEFAULT 0,
        e_score     REAL NOT NULL DEFAULT 0,
        profile     TEXT NOT NULL DEFAULT '',
        proxy_flags TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX IF NOT EXISTS idx_attention_dossier ON attention_snapshots(dossier_id, taken_at);
    CREATE TABLE IF NOT EXISTS opinion_profiles (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        dossier_id   INTEGER NOT NULL REFERENCES dossiers(id) ON DELETE CASCADE,
        batch        INTEGER NOT NULL DEFAULT 1,
        distribution TEXT NOT NULL DEFAULT '{}',
        summary      TEXT NOT NULL DEFAULT '',
        degraded     INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS deep_reads (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        dossier_id    INTEGER NOT NULL REFERENCES dossiers(id) ON DELETE CASCADE,
        payload       TEXT NOT NULL DEFAULT '{}',
        degraded      INTEGER NOT NULL DEFAULT 0,
        guard_dropped TEXT NOT NULL DEFAULT '[]',
        created_at    TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS analysis_runs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        dossier_id INTEGER NOT NULL REFERENCES dossiers(id) ON DELETE CASCADE,
        kind       TEXT NOT NULL DEFAULT '',
        status     TEXT NOT NULL DEFAULT '',
        degraded   INTEGER NOT NULL DEFAULT 0,
        duration_s REAL NOT NULL DEFAULT 0,
        requests   INTEGER NOT NULL DEFAULT 0,
        tokens     INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_runs_dossier ON analysis_runs(dossier_id, created_at);
    """,
    # v2：会话域三表（M2）。sessions/messages/tool_runs 与档案域同库不同域。
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived   INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system_note')),
        kind        TEXT NOT NULL DEFAULT 'text',
        content     TEXT NOT NULL DEFAULT '',
        tool_run_id INTEGER,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
    CREATE TABLE IF NOT EXISTS tool_runs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        dossier_id INTEGER REFERENCES dossiers(id) ON DELETE SET NULL,
        tool       TEXT NOT NULL DEFAULT '',
        args       TEXT NOT NULL DEFAULT '{}',
        status     TEXT NOT NULL DEFAULT '',
        degraded   INTEGER NOT NULL DEFAULT 0,
        duration_s REAL NOT NULL DEFAULT 0,
        requests   INTEGER NOT NULL DEFAULT 0,
        tokens     INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tool_runs_session ON tool_runs(session_id, id);
    """,
]


def default_db_path() -> Path:
    """项目根/data/dossier.db（DOSSIER_DB_PATH 可整路径覆盖）。"""
    env = os.environ.get("DOSSIER_DB_PATH")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "data" / "dossier.db"


def connect(db_path: Optional[os.PathLike | str] = None) -> sqlite3.Connection:
    """连接工厂：WAL、外键开启、忙等 5s。跨线程共用时由调用方持锁（DossierStore 已带）。

    传 ":memory:" 得到内存库（测试用）；传目录则自动建目录。
    """
    path = Path(db_path) if db_path is not None else default_db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _now() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else ({} if isinstance(value, dict) else []),
                      ensure_ascii=False)


def _load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def migrate(conn: sqlite3.Connection) -> int:
    """把库迁到 SCHEMA_VERSION；返回应用了的迁移数。幂等，可重复调用。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                        version    INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL)""")
    done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = 0
    for i, script in enumerate(MIGRATIONS, start=1):
        if i in done:
            continue
        with conn:
            conn.executescript(script)
            conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                         (i, _now()))
        applied += 1
    return applied


class DossierStore:
    """档案域七表的读写门面。线程安全：内部一把写锁 + 独立连接（WAL 单写者）。

    方法分三类：
      - 档案本体：create / get / touch / list / delete
      - 结论（版本化）：add_conclusion（自动取下一版本号）/ conclusions
      - 章节/批次数据：add_evidence / evidence、add_snapshot / snapshots、
        add_opinions / opinions、add_deep_read / deep_reads、add_run / runs
    """

    def __init__(self, db_path: Optional[os.PathLike | str] = None) -> None:
        self._conn = connect(db_path)
        self._lock = threading.RLock()
        with self._lock:
            migrate(self._conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 档案本体 ----

    def create_dossier(self, title: str, input_text: str, input_kind: str = "text") -> int:
        assert input_kind in ("text", "topic"), f"input_kind 非法: {input_kind}"
        now = _now()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO dossiers (created_at, updated_at, title, input_text, input_kind)"
                " VALUES (?, ?, ?, ?, ?)",
                (now, now, str(title or ""), str(input_text or ""), input_kind))
            return int(cur.lastrowid)

    def get_dossier(self, dossier_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM dossiers WHERE id = ?", (dossier_id,)).fetchone()
        return dict(row) if row else None

    def touch_dossier(self, dossier_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE dossiers SET updated_at = ? WHERE id = ?",
                               (_now(), dossier_id))

    def list_dossiers(self, query: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """按 updated_at 倒序列档案；query 非空时对 title/input_text 做 LIKE 过滤。"""
        like = f"%{query}%"
        sql = ("SELECT id, created_at, updated_at, title, input_kind, schema_version"
               " FROM dossiers")
        args: List[Any] = []
        if query:
            sql += " WHERE title LIKE ? OR input_text LIKE ?"
            args += [like, like]
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args)]

    def delete_dossier(self, dossier_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM dossiers WHERE id = ?", (dossier_id,))
            return cur.rowcount > 0

    # ---- 结论（版本化） ----

    def add_conclusion(self, dossier_id: int, label: str, risk_score: Optional[float],
                       confidence: str = "", evidence_sufficiency: Optional[Dict] = None,
                       as_of: str = "", recheck_conditions: Optional[Iterable[str]] = None,
                       raw_verdict: Optional[Dict] = None) -> int:
        """追加下一版本结论；返回版本号。调用方随后应 touch 档案。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM conclusions WHERE dossier_id = ?",
                (dossier_id,)).fetchone()
            version = int(row["v"]) + 1
            self._conn.execute(
                "INSERT INTO conclusions (dossier_id, version, label, risk_score, confidence,"
                " evidence_sufficiency, as_of, recheck_conditions, raw_verdict, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (dossier_id, version, str(label or ""), risk_score, str(confidence or ""),
                 _dump(evidence_sufficiency), str(as_of or ""),
                 _dump(list(recheck_conditions or [])), _dump(raw_verdict), _now()))
            return version

    def conclusions(self, dossier_id: int) -> List[Dict[str, Any]]:
        """全部版本，version 升序；JSON 字段已还原。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conclusions WHERE dossier_id = ? ORDER BY version",
                (dossier_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence_sufficiency"] = _load(d["evidence_sufficiency"], {})
            d["recheck_conditions"] = _load(d["recheck_conditions"], [])
            d["raw_verdict"] = _load(d["raw_verdict"], {})
            out.append(d)
        return out

    def latest_conclusion(self, dossier_id: int) -> Optional[Dict[str, Any]]:
        cs = self.conclusions(dossier_id)
        return cs[-1] if cs else None

    # ---- 章节与批次数据 ----

    def add_evidence(self, dossier_id: int, batch: int, entries: List[Dict[str, Any]]) -> int:
        rows = [(dossier_id, int(batch), str(e.get("url", "")), str(e.get("domain", "")),
                 str(e.get("domain_tier", "")), str(e.get("source_kind", "")),
                 str(e.get("stance", "")), str(e.get("title", "")), str(e.get("snippet", "")),
                 str(e.get("fetched_at", ""))) for e in entries]
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO evidence_entries (dossier_id, batch, url, domain, domain_tier,"
                " source_kind, stance, title, snippet, fetched_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            return len(rows)

    def evidence(self, dossier_id: int, batch: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM evidence_entries WHERE dossier_id = ?"
        args: List[Any] = [dossier_id]
        if batch is not None:
            sql += " AND batch = ?"
            args.append(int(batch))
        sql += " ORDER BY batch, id"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args)]

    def add_snapshot(self, dossier_id: int, x: float, y: float, z: float, e_score: float,
                     profile: str = "", proxy_flags: Optional[List] = None,
                     taken_at: str = "") -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO attention_snapshots (dossier_id, taken_at, x, y, z, e_score,"
                " profile, proxy_flags) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (dossier_id, taken_at or _now(), float(x), float(y), float(z), float(e_score),
                 str(profile or ""), _dump(list(proxy_flags or []))))
            return int(cur.lastrowid)

    def snapshots(self, dossier_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM attention_snapshots WHERE dossier_id = ? ORDER BY taken_at, id",
                (dossier_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["proxy_flags"] = _load(d["proxy_flags"], [])
            out.append(d)
        return out

    def add_opinions(self, dossier_id: int, batch: int, distribution: Dict[str, Any],
                     summary: str = "", degraded: bool = False) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO opinion_profiles (dossier_id, batch, distribution, summary,"
                " degraded, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (dossier_id, int(batch), _dump(distribution), str(summary or ""),
                 1 if degraded else 0, _now()))
            return int(cur.lastrowid)

    def opinions(self, dossier_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM opinion_profiles WHERE dossier_id = ? ORDER BY batch, id",
                (dossier_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["distribution"] = _load(d["distribution"], {})
            d["degraded"] = bool(d["degraded"])
            out.append(d)
        return out

    def add_deep_read(self, dossier_id: int, payload: Dict[str, Any], degraded: bool = False,
                      guard_dropped: Optional[List] = None) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO deep_reads (dossier_id, payload, degraded, guard_dropped, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (dossier_id, _dump(payload), 1 if degraded else 0, _dump(list(guard_dropped or [])),
                 _now()))
            return int(cur.lastrowid)

    def deep_reads(self, dossier_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM deep_reads WHERE dossier_id = ? ORDER BY id",
                (dossier_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = _load(d["payload"], {})
            d["guard_dropped"] = _load(d["guard_dropped"], [])
            d["degraded"] = bool(d["degraded"])
            out.append(d)
        return out

    def add_run(self, dossier_id: int, kind: str, status: str, degraded: bool = False,
                duration_s: float = 0.0, requests: int = 0, tokens: int = 0) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO analysis_runs (dossier_id, kind, status, degraded, duration_s,"
                " requests, tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (dossier_id, str(kind or ""), str(status or ""), 1 if degraded else 0,
                 float(duration_s), int(requests), int(tokens), _now()))
            return int(cur.lastrowid)

    def runs(self, dossier_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM analysis_runs WHERE dossier_id = ? ORDER BY id",
                (dossier_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["degraded"] = bool(d["degraded"])
            out.append(d)
        return out
