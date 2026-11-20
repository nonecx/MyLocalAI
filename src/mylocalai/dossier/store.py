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
import unicodedata
import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["SCHEMA_VERSION", "DEFAULT_DB_PATH", "connect", "migrate",
           "DossierStore", "default_db_path"]

SCHEMA_VERSION = 6

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
    # v3：声明指纹表（M3-1 谣言库沉淀，§2.5/§6 M3-1）。相似说法"先问再查"的存储面：
    # 每条已核查声明一行，normalized 是规整声明、shingles 是 k-shingle 稳定哈希的
    # JSON 数组（写入时算好，比对时直接读，不在 SQL 里算相似度）。
    # 幂等口径（同 dossier 同 normalized 不重插）由应用层保证（add_claim_fingerprint）。
    """
    CREATE TABLE IF NOT EXISTS claim_fingerprints (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        dossier_id  INTEGER NOT NULL REFERENCES dossiers(id) ON DELETE CASCADE,
        normalized  TEXT NOT NULL DEFAULT '',
        shingles    TEXT NOT NULL DEFAULT '[]',
        label       TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_claim_fp_normalized ON claim_fingerprints(normalized);
    """,
    # v4：话术特征 marker 行（L4 参谋）。每档案至多一行（唯一索引），由 record_markers
    # 以 INSERT OR IGNORE 幂等写入；hits 存 JSON（与库内其他 payload 同口径），
    # 分数已在 patterns.scan_markers 扫描时算好，入库只存不算——打分零 LLM、纯函数。
    """
    CREATE TABLE IF NOT EXISTS dossier_markers (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        dossier_id         INTEGER NOT NULL REFERENCES dossiers(id) ON DELETE CASCADE,
        risk_hits          TEXT DEFAULT '{}',
        credibility_hits   TEXT DEFAULT '{}',
        marker_points      INTEGER DEFAULT 0,
        credibility_points INTEGER DEFAULT 0,
        created_at         TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_dossier_markers_dossier ON dossier_markers(dossier_id);
    """,
    # v5: retention classification and first-save expiry. Existing records receive
    # one migration grace period; subsequent writes calculate expiry at insertion.
    """
    ALTER TABLE dossiers ADD COLUMN result_expires_at TEXT;
    ALTER TABLE dossiers ADD COLUMN raw_input_expires_at TEXT;
    ALTER TABLE dossiers ADD COLUMN claim_excerpt TEXT NOT NULL DEFAULT '';
    ALTER TABLE evidence_entries ADD COLUMN cache_expires_at TEXT;
    ALTER TABLE sessions ADD COLUMN raw_input_expires_at TEXT;
    ALTER TABLE messages ADD COLUMN raw_input_expires_at TEXT;
    ALTER TABLE messages ADD COLUMN result_expires_at TEXT;
    ALTER TABLE tool_runs ADD COLUMN input_expires_at TEXT;
    ALTER TABLE tool_runs ADD COLUMN result_expires_at TEXT;
    UPDATE dossiers SET result_expires_at = datetime('now', '+90 days');
    UPDATE dossiers SET raw_input_expires_at = datetime('now', '+30 days')
        WHERE input_text IS NOT NULL AND input_text != '';
    UPDATE evidence_entries SET cache_expires_at = datetime(fetched_at, '+7 days')
        WHERE fetched_at IS NOT NULL AND fetched_at != '';
    UPDATE sessions SET raw_input_expires_at = datetime('now', '+30 days');
    UPDATE messages SET raw_input_expires_at = datetime('now', '+30 days')
        WHERE role IN ('user', 'system_note') AND content IS NOT NULL AND content != '';
    UPDATE messages SET result_expires_at = datetime('now', '+90 days')
        WHERE role = 'assistant' AND content IS NOT NULL AND content != '';
    CREATE INDEX IF NOT EXISTS idx_dossiers_result_expiry ON dossiers(result_expires_at);
    CREATE INDEX IF NOT EXISTS idx_dossiers_input_expiry ON dossiers(raw_input_expires_at);
    CREATE INDEX IF NOT EXISTS idx_evidence_cache_expiry ON evidence_entries(cache_expires_at);
    CREATE INDEX IF NOT EXISTS idx_sessions_input_expiry ON sessions(raw_input_expires_at);
    CREATE INDEX IF NOT EXISTS idx_messages_input_expiry ON messages(raw_input_expires_at);
    CREATE INDEX IF NOT EXISTS idx_messages_result_expiry ON messages(result_expires_at);
    """,
    # v6: 平台域三表（热门监测平台）。watch_queries 是监控词；feed_entries 是巡检
    # 发现的信息流条目（url 唯一约束即去重第一道闸，result_expires_at 走 results
    # 保留期）；scan_runs 记录每次巡检的执行状态。建表顺序：scan_runs 先于
    # feed_entries（后者有 FK 指向前者）。
    """
    CREATE TABLE IF NOT EXISTS scan_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        status      TEXT NOT NULL DEFAULT 'running',
        queries     INTEGER NOT NULL DEFAULT 0,
        candidates  INTEGER NOT NULL DEFAULT 0,
        verified    INTEGER NOT NULL DEFAULT 0,
        degraded    INTEGER NOT NULL DEFAULT 0,
        error       TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS watch_queries (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        query      TEXT NOT NULL UNIQUE,
        enabled    INTEGER NOT NULL DEFAULT 1,
        note       TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS feed_entries (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id      INTEGER NOT NULL REFERENCES watch_queries(id) ON DELETE CASCADE,
        scan_id       INTEGER REFERENCES scan_runs(id) ON DELETE SET NULL,
        title         TEXT NOT NULL DEFAULT '',
        url           TEXT NOT NULL UNIQUE,
        domain        TEXT NOT NULL DEFAULT '',
        domain_tier   TEXT NOT NULL DEFAULT '',
        snippet       TEXT NOT NULL DEFAULT '',
        discovered_at TEXT NOT NULL DEFAULT '',
        e_score       REAL,
        e_prev        REAL,
        risk_level    TEXT NOT NULL DEFAULT 'notice',
        risk_reasons  TEXT NOT NULL DEFAULT '[]',
        verdict_label TEXT NOT NULL DEFAULT '',
        risk_score    REAL,
        dossier_id    INTEGER REFERENCES dossiers(id) ON DELETE SET NULL,
        status        TEXT NOT NULL DEFAULT 'new',
        result_expires_at TEXT,
        created_at    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_feed_query ON feed_entries(query_id, id);
    CREATE INDEX IF NOT EXISTS idx_feed_risk ON feed_entries(risk_level, id);
    CREATE INDEX IF NOT EXISTS idx_feed_expiry ON feed_entries(result_expires_at);
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


def _expiry(days: Optional[int], *, from_time: Optional[str] = None) -> Optional[str]:
    if days is None:
        return None
    from datetime import datetime, timedelta, timezone
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("retention days must be a positive integer")
    if from_time:
        try:
            base = datetime.fromisoformat(str(from_time).replace("Z", "+00:00"))
        except ValueError:
            base = datetime.now().astimezone()
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
    else:
        base = datetime.now().astimezone()
    return (base + timedelta(days=days)).astimezone().isoformat(timespec="seconds")


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


# ---------------------------------------------------------------------------
# 声明指纹的最小实现（M3-1 谣言库，§2.5/§6 M3-1）
# ---------------------------------------------------------------------------
# 算法与 mylocalai/assistant/fingerprint.py 的 normalize_claim/shingles/jaccard
# **逐字节同口径**。不能 import：依赖方向禁 dossier → assistant（见
# tests/test_import_rules.py 的 FORBIDDEN），故在此落一份最小副本；两处一致性由
# tests/test_fingerprint.py 的对拍用例（TestStoreFingerprintParity）锁定，改动必须两处同步。

#: 引导前缀（剥 rumor 话术包装；**按长度降序**——"据网传"先于"网传"尝试）。
_FP_LEAD_PREFIXES = tuple(sorted(
    ("网传", "据网传", "据传", "据说", "听说", "惊爆", "曝"), key=len, reverse=True))
_FP_MAX_PREFIX_STRIPS = 3
#: 缺省 shingle 窗口（与 assistant_rules.json 的 fingerprint.shingle_size 同值）。
FP_DEFAULT_SHINGLE_SIZE = 4


def _fp_normalize(text: Any) -> str:
    """与 assistant.fingerprint.normalize_claim 同口径：NFKC/小写/去标点空白/剥引导前缀。"""
    try:
        s = unicodedata.normalize("NFKC", str(text or ""))
    except Exception:  # noqa: BLE001
        return ""
    s = s.lower()
    s = "".join(ch for ch in s if ch.isalnum())
    for _ in range(_FP_MAX_PREFIX_STRIPS):
        stripped = False
        for prefix in _FP_LEAD_PREFIXES:
            if s.startswith(prefix) and len(s) > len(prefix):
                s = s[len(prefix):]
                stripped = True
                break
        if not stripped:
            break
    return s


def _fp_stable_hash(text: str) -> int:
    """与 assistant.fingerprint._stable_hash 同口径（blake2b 8B，跨进程稳定）。"""
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


def _fp_shingle_hashes(text: str, k: int = FP_DEFAULT_SHINGLE_SIZE) -> frozenset:
    """与 assistant.fingerprint.shingles 同口径（短文本整串一个 shingle，空文本空集）。"""
    s = str(text or "")
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = FP_DEFAULT_SHINGLE_SIZE
    if k <= 0:
        k = FP_DEFAULT_SHINGLE_SIZE
    if not s:
        return frozenset()
    if len(s) < k:
        return frozenset({_fp_stable_hash(s)})
    return frozenset(_fp_stable_hash(s[i:i + k]) for i in range(len(s) - k + 1))


def _fp_jaccard(a: Any, b: Any) -> float:
    """与 assistant.fingerprint.jaccard 同口径（空集按 0.0，坏输入按 0.0，不抛）。"""
    try:
        sa = frozenset(a) if a is not None else frozenset()
        sb = frozenset(b) if b is not None else frozenset()
    except TypeError:
        return 0.0
    if not sa or not sb:
        return 0.0
    union = len(sa | sb)
    if union == 0:
        return 0.0
    return len(sa & sb) / union


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
    """档案域存储的读写门面（七张业务表 + 声明指纹表）。线程安全：内部一把写锁 + 独立连接（WAL 单写者）。

    方法分三类：
      - 档案本体：create / get / touch / list / delete
      - 结论（版本化）：add_conclusion（自动取下一版本号）/ conclusions
      - 章节/批次数据：add_evidence / evidence、add_snapshot / snapshots、
        add_opinions / opinions、add_deep_read / deep_reads、add_run / runs
      - 声明指纹（M3-1 谣言库）：add_claim_fingerprint（幂等沉淀）/
        find_fingerprints（相似召回）/ claims_for_dossier
      - 话术特征（L4 参谋）：record_markers（一档案一行，幂等落 marker 行）
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

    def create_dossier(self, title: str, input_text: str, input_kind: str = "text", *,
                       claim_excerpt: str = "", save_result: bool = True,
                       result_retention_days: Optional[int] = None,
                       save_raw_input: bool = True,
                       raw_input_retention_days: Optional[int] = None) -> int:
        assert input_kind in ("text", "topic"), f"input_kind 非法: {input_kind}"
        now = _now()
        saved_input = str(input_text or "") if save_raw_input else ""
        result_expiry = _expiry(result_retention_days) if save_result else None
        input_expiry = _expiry(raw_input_retention_days) if save_raw_input and saved_input else None
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO dossiers (created_at, updated_at, title, input_text, input_kind,"
                " result_expires_at, raw_input_expires_at, claim_excerpt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, now, str(title or ""), saved_input, input_kind, result_expiry, input_expiry,
                 str(claim_excerpt or "") if save_result else ""))
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

    def purge_expired(self, *, now: Optional[str] = None) -> Dict[str, int]:
        """Idempotently purge expired data by category; preserve other categories.

        Result expiry deletes the dossier and cascaded result rows. Raw-input expiry
        clears only dossier input_text and user message content. External-cache expiry
        clears only evidence snippets, retaining source/stance metadata. Physical WAL
        and backup reclamation remains an operator retention/checkpoint responsibility.
        """
        from datetime import datetime, timezone
        current = _now() if now is None else str(now)
        try:
            parsed = datetime.fromisoformat(current.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("now must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("now must include timezone")
        current_utc = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        stats = {"results_deleted": 0, "input_rows_cleared": 0, "cache_rows_cleared": 0}
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM dossiers WHERE result_expires_at IS NOT NULL "
                "AND julianday(replace(result_expires_at, 'Z', '+00:00')) <= julianday(?)",
                (current_utc,))
            stats["results_deleted"] = cur.rowcount
            cur = self._conn.execute(
                "UPDATE dossiers SET input_text='', raw_input_expires_at=NULL "
                "WHERE raw_input_expires_at IS NOT NULL "
                "AND julianday(replace(raw_input_expires_at, 'Z', '+00:00')) <= julianday(?)",
                (current_utc,))
            stats["input_rows_cleared"] += cur.rowcount
            cur = self._conn.execute(
                "UPDATE messages SET content='', raw_input_expires_at=NULL "
                "WHERE raw_input_expires_at IS NOT NULL "
                "AND julianday(replace(raw_input_expires_at, 'Z', '+00:00')) <= julianday(?)",
                (current_utc,))
            stats["input_rows_cleared"] += cur.rowcount
            cur = self._conn.execute(
                "UPDATE sessions SET raw_input_expires_at=NULL "
                "WHERE raw_input_expires_at IS NOT NULL "
                "AND julianday(replace(raw_input_expires_at, 'Z', '+00:00')) <= julianday(?)",
                (current_utc,))
            stats["input_rows_cleared"] += cur.rowcount
            cur = self._conn.execute(
                "UPDATE messages SET content='', result_expires_at=NULL "
                "WHERE result_expires_at IS NOT NULL "
                "AND julianday(replace(result_expires_at, 'Z', '+00:00')) <= julianday(?)",
                (current_utc,))
            stats["results_deleted"] += cur.rowcount
            cur = self._conn.execute(
                "UPDATE tool_runs SET args='{}', input_expires_at=NULL "
                "WHERE input_expires_at IS NOT NULL "
                "AND julianday(replace(input_expires_at, 'Z', '+00:00')) <= julianday(?)",
                (current_utc,))
            stats["input_rows_cleared"] += cur.rowcount
            cur = self._conn.execute(
                "UPDATE tool_runs SET args='{}', result_expires_at=NULL "
                "WHERE result_expires_at IS NOT NULL "
                "AND julianday(replace(result_expires_at, 'Z', '+00:00')) <= julianday(?)",
                (current_utc,))
            stats["results_deleted"] += cur.rowcount
            cur = self._conn.execute(
                "UPDATE evidence_entries SET snippet='', cache_expires_at=NULL "
                "WHERE cache_expires_at IS NOT NULL "
                "AND julianday(replace(cache_expires_at, 'Z', '+00:00')) <= julianday(?)",
                (current_utc,))
            stats["cache_rows_cleared"] = cur.rowcount
        return stats

    # ---- 结论（版本化） ----

    def add_conclusion(self, dossier_id: int, label: str, risk_score: Optional[float],
                       confidence: str = "", evidence_sufficiency: Optional[Dict] = None,
                       as_of: str = "", recheck_conditions: Optional[Iterable[str]] = None,
                       raw_verdict: Optional[Dict] = None,
                       result_retention_days: Optional[int] = None) -> int:
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
            if result_retention_days is not None:
                expiry = _expiry(result_retention_days)
                self._conn.execute(
                    "UPDATE dossiers SET result_expires_at = COALESCE(result_expires_at, ?) WHERE id = ?",
                    (expiry, dossier_id))
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

    def add_evidence(self, dossier_id: int, batch: int, entries: List[Dict[str, Any]], *,
                     save_external_cache: bool = True,
                     cache_retention_days: Optional[int] = None) -> int:
        rows = []
        for entry in entries:
            fetched_at = str(entry.get("fetched_at", ""))
            snippet = str(entry.get("snippet", "")) if save_external_cache else ""
            expiry = (_expiry(cache_retention_days, from_time=fetched_at)
                      if save_external_cache and cache_retention_days is not None else None)
            rows.append((dossier_id, int(batch), str(entry.get("url", "")),
                         str(entry.get("domain", "")), str(entry.get("domain_tier", "")),
                         str(entry.get("source_kind", "")), str(entry.get("stance", "")),
                         str(entry.get("title", "")), snippet, fetched_at, expiry))
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO evidence_entries (dossier_id, batch, url, domain, domain_tier,"
                " source_kind, stance, title, snippet, fetched_at, cache_expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
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

    # ---- 声明指纹（M3-1 谣言库沉淀；表结构见 MIGRATIONS v3） ----

    def add_claim_fingerprint(self, dossier_id: int, text: str, label: str = "",
                              k: int = FP_DEFAULT_SHINGLE_SIZE) -> int:
        """沉淀一条声明指纹（幂等）：同 dossier 同 normalized 不重插，返回行 id。

        调用方（loop）在核查结论落库成功后调用：dossier_id 是承载结论的档案，
        text 是**原文**（归一化在本方法内做，调用方传原文即可），label 透传结论标签。
        k 须与写入库的 shingles 口径一致（shingle_size 改动只影响之后新沉淀的行，
        旧行不迁移——个人库量级下可接受；必要时删档重查会自动重新沉淀）。
        """
        normalized = _fp_normalize(text)
        if not normalized:
            return 0   # 规整后为空（如纯标点）没有可比对性：如实不沉淀，返回 0
        shingle_list = sorted(_fp_shingle_hashes(normalized, k))
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id FROM claim_fingerprints WHERE dossier_id = ? AND normalized = ?",
                (int(dossier_id), normalized)).fetchone()
            if row:
                return int(row["id"])
            cur = self._conn.execute(
                "INSERT INTO claim_fingerprints (dossier_id, normalized, shingles, label,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (int(dossier_id), normalized, _dump(shingle_list), str(label or ""), _now()))
            return int(cur.lastrowid)

    def find_fingerprints(self, query_text: str, threshold: float, limit: int = 5,
                          k: int = FP_DEFAULT_SHINGLE_SIZE) -> List[Dict[str, Any]]:
        """相似声明召回：返回 Jaccard ≥ threshold 的行，按分数降序、最多 limit 条。

        规模假设（注释即契约）：本库是**单机个人档案库**，已核查声明量级在千条以内，
        全表读出后在内存算 Jaccard 足够（<10ms/千条）；若未来上万条再换倒排索引，
        不预先建。查询侧归一化/shingle 与写入侧同用 _fp_* 最小副本（对拍锁定）。

        返回行形状：{id, dossier_id, normalized, label, created_at, score}；
        库故障由调用方处理（本方法不吞异常——调用方的指纹闸对故障按"不命中"降级）。
        """
        query_norm = _fp_normalize(query_text)
        if not query_norm:
            return []
        query_shingles = _fp_shingle_hashes(query_norm, k)
        if not query_shingles:
            return []
        th = float(threshold)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, dossier_id, normalized, shingles, label, created_at"
                " FROM claim_fingerprints").fetchall()
        hits: List[Dict[str, Any]] = []
        for r in rows:
            stored = _load(r["shingles"], [])
            if not isinstance(stored, list):
                continue
            score = _fp_jaccard(query_shingles, stored)
            if score >= th:
                hits.append({"id": int(r["id"]), "dossier_id": int(r["dossier_id"]),
                             "normalized": str(r["normalized"] or ""),
                             "label": str(r["label"] or ""),
                             "created_at": str(r["created_at"] or ""),
                             "score": score})
        hits.sort(key=lambda h: (-h["score"], h["id"]))
        return hits[:max(1, int(limit))]

    def claims_for_dossier(self, dossier_id: int) -> List[Dict[str, Any]]:
        """某档案沉淀过的全部声明指纹（id 升序；shingles 已还原为 list[int]）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, dossier_id, normalized, shingles, label, created_at"
                " FROM claim_fingerprints WHERE dossier_id = ? ORDER BY id",
                (int(dossier_id),)).fetchall()
        out = []
        for r in rows:
            stored = _load(r["shingles"], [])
            out.append({"id": int(r["id"]), "dossier_id": int(r["dossier_id"]),
                        "normalized": str(r["normalized"] or ""),
                        "shingles": stored if isinstance(stored, list) else [],
                        "label": str(r["label"] or ""),
                        "created_at": str(r["created_at"] or "")})
        return out

    # ---- 话术特征 marker 行（L4 参谋；表结构见 MIGRATIONS v4） ----

    def record_markers(self, dossier_id: int, scan: dict) -> bool:
        """落一档案的话术特征行（一档案一条，唯一索引幂等）。

        scan 是 patterns.scan_markers 的返回形状：risk_hits/credibility_hits 的内层
        为 {"label": 中文名, "hits": [...], "points": int}，原样 JSON 序列化存
        （与库内其他 payload 同口径）；marker_points/credibility_points 是扫描时
        由纯函数算好的分数，入库只存不算——大模型不参与任何打分。

        返回 True 表示本次插入成功；scan 为空 / 无 risk_hits 键时不插入返回 False；
        已存在（INSERT OR IGNORE 命中唯一索引）返回 False。dossier_id 不存在时
        外键约束自然抛 IntegrityError（调用方兜）。
        """
        if not isinstance(scan, dict) or not scan or "risk_hits" not in scan:
            return False
        try:
            marker_points = int(scan.get("marker_points", 0))
        except (TypeError, ValueError):
            marker_points = 0
        try:
            credibility_points = int(scan.get("credibility_points", 0))
        except (TypeError, ValueError):
            credibility_points = 0
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO dossier_markers (dossier_id, risk_hits,"
                " credibility_hits, marker_points, credibility_points, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (int(dossier_id), _dump(scan.get("risk_hits") or {}),
                 _dump(scan.get("credibility_hits") or {}),
                 marker_points, credibility_points, _now()))
            return cur.rowcount > 0
