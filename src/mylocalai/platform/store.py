# -*- coding: utf-8 -*-
"""平台域存储（platform/store.py）：watch_queries / feed_entries / scan_runs 三表读写。

表由 dossier store 的顺序迁移建立（平台域三表在 v6，v7 追加用户自建信息源两表
patrol_sources/patrol_query_sources 与领域列）；本模块经同一连接工厂
（mylocalai.dossier.store.connect）打开**同一个库文件**，不另起数据库。

口径与 DossierStore 一致：
  - 单文件 SQLite + WAL，单写者（本模块自带 RLock）；
  - payload 存 JSON 文本，读写经 _dump/_load 往返；
  - 方法**不抛业务异常**给调用方崩溃：重复监控词/重复 URL 以返回值表达
    （None / False），由上层决定"已存在"的呈现方式。外键冲突不在此列——
    那是调用方状态问题，原样抛 IntegrityError（见 add_feed_entry 注释）。

领域（domain）口径：watch_queries.domain 是用户手设的监控词领域（空串 = 自动
推断），inferred_domain/inferred_at 是自动推断缓存（用户手改 domain 即清空，
清缓存动作在 set_watch_query_domain 里做，库里不写触发器）；feed_entries 的
领域落在 topic_domain/topic_domain_source 两列（与 v6 起就有的 domain 列——
URL 主机域——分列存放，互不覆盖）。

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

#: 用户自建信息源类型封闭集合（与 v7 迁移里 patrol_sources.kind 的 CHECK 同口径；
#: 越界值在 add_patrol_source 即 ValueError，不静默纠正）。
PATROL_SOURCE_KINDS = ("rss", "page")


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
            migrate(self._conn)   # 幂等：确保平台域全量表在场（老库自动补建到 v7）

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

    def watch_query_exists(self, query_id: int) -> bool:
        """监控词是否还在（巡检中途可能被用户删掉：删了其下条目会级联消失，
        在跑候选应诚实跳过而不是撞外键）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM watch_queries WHERE id = ?", (int(query_id),)).fetchone()
        return row is not None

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
        feed_entries.dossier_id 是 ON DELETE SET NULL，档案库由档案域自己管；
        patrol_query_sources 的源绑定同样级联消失）。"""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM watch_queries WHERE id = ?", (int(query_id),))
            return cur.rowcount > 0

    # ---- 监控词领域（v7） ----

    def get_watch_query(self, query_id: int) -> Optional[Dict[str, Any]]:
        """单条监控词整行（含 v7 新增的 domain / inferred_domain / inferred_at）；
        不存在返回 None。enabled 转 bool（与 list_watch_queries 同口径）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM watch_queries WHERE id = ?", (int(query_id),)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        return d

    def set_watch_query_domain(self, query_id: int, domain: str) -> bool:
        """写用户手设领域，**同时清空 inferred_domain / inferred_at**——用户手改即废
        自动推断缓存，防陈旧推断反覆盖用户意图。domain 空串 = 恢复自动推断。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE watch_queries SET domain = ?, inferred_domain = '', inferred_at = ''"
                " WHERE id = ?", (str(domain or ""), int(query_id)))
            return cur.rowcount > 0

    def set_inferred_domain(self, query_id: int, domain: str, at: str) -> bool:
        """写自动推断缓存（inferred_domain + inferred_at 时间戳，由调用方给时刻）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE watch_queries SET inferred_domain = ?, inferred_at = ? WHERE id = ?",
                (str(domain or ""), str(at or ""), int(query_id)))
            return cur.rowcount > 0

    # ---- 用户自建信息源（v7：patrol_sources / patrol_query_sources） ----

    def _patrol_binds(self) -> Dict[int, List[int]]:
        """一次查询汇总全部 source_id → [query_id, ...]（调用方持锁；勿 N+1）。"""
        binds: Dict[int, List[int]] = {}
        for r in self._conn.execute(
                "SELECT query_id, source_id FROM patrol_query_sources"
                " ORDER BY source_id, query_id"):
            binds.setdefault(int(r["source_id"]), []).append(int(r["query_id"]))
        return binds

    def _patrol_row(self, row: sqlite3.Row, binds: Dict[int, List[int]]) -> Dict[str, Any]:
        """源行 → 对外形状：enabled 转 bool，query_ids 带出该源的全部绑定。"""
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        d["query_ids"] = list(binds.get(int(d["id"]), []))
        return d

    def add_patrol_source(self, name: str, url: str, kind: str = "rss",
                          note: str = "", query_ids: Optional[List[int]] = None) -> Optional[int]:
        """新增用户自建信息源；url 已存在返回 None（去重闸，调用方提示"已添加"）。

        kind 越界抛 ValueError（调用方 bug，不静默纠正）。query_ids 为空列表/None
        都表示"暂不绑定"（创建后可用 update_patrol_source 改）。绑定到不存在的
        query_id 触发外键冲突时**原样抛 IntegrityError**——与 add_feed_entry 同口径：
        那不是"去重"；源行与绑定在同一事务里，绑定失败则源也不落库。
        """
        u = str(url or "").strip()
        if not u:
            raise ValueError("信息源 URL 不能为空")
        k = str(kind or "rss")
        if k not in PATROL_SOURCE_KINDS:
            raise ValueError(f"信息源类型非法: {kind}")
        bind_ids = [int(q) for q in (query_ids or [])]
        with self._lock, self._conn:
            try:
                cur = self._conn.execute(
                    "INSERT INTO patrol_sources (name, url, kind, enabled, note, created_at)"
                    " VALUES (?, ?, ?, 1, ?, ?)",
                    (str(name or ""), u, k, str(note or ""), _now()))
            except sqlite3.IntegrityError as exc:
                if "foreign key" in str(exc).lower():
                    raise
                return None                # url 唯一冲突 → 去重闸
            source_id = int(cur.lastrowid)
            for qid in bind_ids:
                self._conn.execute(
                    "INSERT INTO patrol_query_sources (query_id, source_id) VALUES (?, ?)",
                    (qid, source_id))
            return source_id

    def list_patrol_sources(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """全部信息源（id 升序），每项带 query_ids（该源绑定的监控词 id 列表）。"""
        sql = "SELECT * FROM patrol_sources"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
            binds = self._patrol_binds()
            return [self._patrol_row(r, binds) for r in rows]

    def get_patrol_source(self, source_id: int) -> Optional[Dict[str, Any]]:
        """单条信息源（同 list 项形状：含 enabled(bool) / query_ids）；不存在 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM patrol_sources WHERE id = ?", (int(source_id),)).fetchone()
            if row is None:
                return None
            return self._patrol_row(row, self._patrol_binds())

    def set_patrol_source_enabled(self, source_id: int, enabled: bool) -> bool:
        """启停信息源；停用后 sources_for_query 对所有词不可见（不删绑定）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE patrol_sources SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, int(source_id)))
            return cur.rowcount > 0

    def update_patrol_source(self, source_id: int, note: Optional[str] = None,
                             query_ids: Optional[List[int]] = None) -> bool:
        """改备注 / 改绑定。note 为 None 不动；query_ids 为 None 不动、为 [] 清空
        全部绑定、为 [id...] 重建绑定（先 DELETE 再 INSERT，同一事务）。
        源不存在返回 False（什么都不改）。"""
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM patrol_sources WHERE id = ?",
                                  (int(source_id),)).fetchone() is None:
                return False
            if note is not None:
                self._conn.execute("UPDATE patrol_sources SET note = ? WHERE id = ?",
                                   (str(note or ""), int(source_id)))
            if query_ids is not None:
                self._conn.execute("DELETE FROM patrol_query_sources WHERE source_id = ?",
                                   (int(source_id),))
                self._conn.executemany(
                    "INSERT INTO patrol_query_sources (query_id, source_id) VALUES (?, ?)",
                    [(int(q), int(source_id)) for q in query_ids])
            return True

    def delete_patrol_source(self, source_id: int) -> bool:
        """删除信息源；patrol_query_sources 里的绑定经外键 CASCADE 一并消失。"""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM patrol_sources WHERE id = ?", (int(source_id),))
            return cur.rowcount > 0

    def sources_for_query(self, query_id: Optional[int]) -> List[Dict[str, Any]]:
        """对某监控词可见的信息源：全局源（在 patrol_query_sources 中无任何绑定的）
        ∪ 绑定到该 query_id 的；仅 enabled=1，按 id 升序。query_id 为 None 时只
        返回全局源。绑定是显式窄授权：绑给别的词的源对本词不可见。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM patrol_sources WHERE enabled = 1 ORDER BY id").fetchall()
            binds = self._patrol_binds()
        qid = None if query_id is None else int(query_id)
        out: List[Dict[str, Any]] = []
        for r in rows:
            sid = int(r["id"])
            if sid in binds and qid not in binds[sid]:
                continue                    # 绑给了别的词 → 对本词不可见
            out.append(self._patrol_row(r, binds))
        return out

    # ---- 信息流 ----

    def add_feed_entry(self, *, query_id: int, title: str, url: str, snippet: str = "",
                       domain: str = "", domain_tier: str = "", topic_domain: str = "",
                       topic_domain_source: str = "", discovered_at: str = "",
                       e_score: Optional[float] = None, status: str = "new",
                       scan_id: Optional[int] = None,
                       result_expires_at: Optional[str] = None) -> Optional[int]:
        """写入信息流条目；URL 已存在返回 None（去重第一道闸，调用方按 deduped 处理）。

        domain 是 v6 起就有的 **URL 主机域**（runner 传 cand.domain，展示用）；
        topic_domain/topic_domain_source 是 v7 新增的**信息流领域**及其来源
        （如 "user"/"inferred"），落 feed_entries.topic_domain/topic_domain_source
        两列——两者分列存放，互不覆盖。

        status 越界抛 ValueError（调用方 bug，不静默纠正）。
        query_id 不存在（监控词被删）触发外键冲突时**原样抛 IntegrityError**——
        那不是"去重"，是调用方状态问题，不许伪装成 None 让上层误记 deduped
        （runner 有前置存在性检查；这里是纵深防御，冒烟测试抓到过该 race）。
        """
        if status not in FEED_STATUSES:
            raise ValueError(f"feed 状态非法: {status}")
        with self._lock, self._conn:
            try:
                cur = self._conn.execute(
                    "INSERT INTO feed_entries (query_id, scan_id, title, url, snippet,"
                    " domain, domain_tier, topic_domain, topic_domain_source,"
                    " discovered_at, e_score, status,"
                    " result_expires_at, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (int(query_id), scan_id, str(title or "")[:200], str(url or ""),
                     str(snippet or "")[:2000], str(domain or ""), str(domain_tier or ""),
                     str(topic_domain or ""), str(topic_domain_source or ""),
                     str(discovered_at or ""), e_score, status, result_expires_at, _now()))
            except sqlite3.IntegrityError as exc:
                if "foreign key" in str(exc).lower():
                    raise                   # 外键冲突不是去重：上层按降级处理
                return None                # URL 唯一冲突 → 去重闸
            return int(cur.lastrowid)

    def update_feed_entry(self, entry_id: int, **fields: Any) -> bool:
        """更新条目（深查回填：dossier_id/verdict_label/risk_score/risk_level/
        risk_reasons/e_prev/status；领域回填：topic_domain/topic_domain_source）。
        白名单字段，其余键忽略（防注入任意列）。"""
        allowed = {"dossier_id", "verdict_label", "risk_score", "risk_level",
                   "risk_reasons", "e_prev", "status", "topic_domain",
                   "topic_domain_source"}
        sets: List[str] = []
        args: List[Any] = []
        for key in ("dossier_id", "verdict_label", "risk_score", "risk_level",
                    "risk_reasons", "e_prev", "status", "topic_domain",
                    "topic_domain_source"):
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

    def domain_histogram(self, limit: int = 200) -> Dict[str, int]:
        """最近 limit 条信息流（id DESC 取窗口）里非空 topic_domain 的计数，
        返回 {domain_id: count}；无非空领域时 {}（空域不计）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT topic_domain FROM feed_entries WHERE topic_domain != ''"
                " ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        out: Dict[str, int] = {}
        for r in rows:
            key = str(r["topic_domain"] or "")
            if not key:
                continue
            out[key] = out.get(key, 0) + 1
        return out

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
