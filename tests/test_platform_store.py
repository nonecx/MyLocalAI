# -*- coding: utf-8 -*-
"""平台域存储测试（P1）：schema 迁移、三表 CRUD、去重闸、保留期清理、状态封闭集。

v7（平台增强）追加覆盖面：
  - v7 迁移建 patrol_sources / patrol_query_sources 两表 + watch_queries 领域三列
    + feed_entries 领域两列与索引；v6 老库升级只补 v7 一段且数据原样保留；
  - 信息源 CRUD：url 唯一去重（None）、kind 封闭集（ValueError）、绑定汇总
    （一次查询无 N+1）、note/绑定局部更新、删除级联、启停过滤；
  - sources_for_query 可见性：全局源 ∀词可见、绑定源只对绑定词可见、停用不可见、
    query_id=None 只见全局；
  - 监控词领域：手改 domain 即清 inferred 缓存、set_inferred_domain 写缓存、
    get_watch_query 读回整行；
  - 信息流领域：add_feed_entry 带 topic_domain/topic_domain_source 往返、
    update_feed_entry 白名单放行、domain_histogram 计数与 limit 窗口；
  - 主机域参数（v6 的 domain=）不回归。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.dossier.store import (MIGRATIONS, SCHEMA_VERSION,  # noqa: E402
                                      connect, migrate)
from mylocalai.platform.store import (FEED_STATUSES, SCAN_STATUSES,  # noqa: E402
                                      PlatformStore)


class TestV6Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Path(self.tmp.name) / "dossier.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_creates_platform_tables(self):
        conn = connect(self.db)
        try:
            self.assertEqual(migrate(conn), SCHEMA_VERSION)
            self.assertEqual(SCHEMA_VERSION, 10)   # v9 平台三表 + v10 信息源/领域列（两线合并）
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ("watch_queries", "feed_entries", "scan_runs"):
                self.assertIn(t, names)
            feed_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
            for c in ("query_id", "scan_id", "url", "e_score", "e_prev", "risk_level",
                      "risk_reasons", "verdict_label", "risk_score", "dossier_id",
                      "status", "result_expires_at"):
                self.assertIn(c, feed_cols)
            idx = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            for i in ("idx_feed_query", "idx_feed_risk", "idx_feed_expiry"):
                self.assertIn(i, idx)
        finally:
            conn.close()

    def test_migration_idempotent(self):
        conn = connect(self.db)
        try:
            self.assertEqual(migrate(conn), SCHEMA_VERSION)
            self.assertEqual(migrate(conn), 0)
        finally:
            conn.close()


class TestV7Migration(unittest.TestCase):
    """v7：平台用户自建信息源两表 + 领域列；老库升级只补一段、数据不丢。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Path(self.tmp.name) / "dossier.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_v7_creates_patrol_tables_and_domain_columns(self):
        conn = connect(self.db)
        try:
            self.assertEqual(migrate(conn), SCHEMA_VERSION)
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ("patrol_sources", "patrol_query_sources"):
                self.assertIn(t, names)
            wq_cols = {r["name"] for r in conn.execute("PRAGMA table_info(watch_queries)")}
            for c in ("domain", "inferred_domain", "inferred_at"):
                self.assertIn(c, wq_cols)
            fe_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
            for c in ("topic_domain", "topic_domain_source"):
                self.assertIn(c, fe_cols)
            self.assertIn("domain", fe_cols)          # v6 主机域列仍在（未被覆盖）
            idx = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn("idx_feed_domain", idx)
        finally:
            conn.close()

    def test_v7_migration_idempotent(self):
        conn = connect(self.db)
        try:
            self.assertEqual(migrate(conn), SCHEMA_VERSION)
            self.assertEqual(migrate(conn), 0)
        finally:
            conn.close()

    def test_v6_database_upgrades_to_v7_keeping_rows(self):
        """platform-monitor 线 v6 存量库（v1-v5 + 平台三表登记 1-6，带监控词与
        信息流数据）升级到统一 v10：_rebase_platform_line 把登记号 6→9（重登记
        不重放），补跑统一 v6/v7/v8 + v10 四段；旧行原样保留，新表新列可用。"""
        conn = connect(self.db)
        try:
            # schema_migrations 由 migrate() 引导创建，不在迁移脚本里——手工补上
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                                version    INTEGER PRIMARY KEY,
                                applied_at TEXT NOT NULL)""")
            for i, script in enumerate(MIGRATIONS[:5], start=1):
                with conn:
                    conn.executescript(script)
                    conn.execute("INSERT INTO schema_migrations (version, applied_at)"
                                 " VALUES (?, '2026-11-01T00:00:00+08:00')", (i,))
            with conn:
                conn.executescript(MIGRATIONS[8])     # 平台三表（旧线 v6 = 统一 v9 原文）
                conn.execute("INSERT INTO schema_migrations (version, applied_at)"
                             " VALUES (6, '2026-11-01T00:00:00+08:00')")
            qid = int(conn.execute(
                "INSERT INTO watch_queries (query, created_at)"
                " VALUES ('旧词', '2026-11-01T00:00:00+08:00')").lastrowid)
            conn.execute(
                "INSERT INTO feed_entries (query_id, url, domain, created_at)"
                " VALUES (?, 'https://a.example.com/1', 'a.example.com',"
                " '2026-11-01T00:00:00+08:00')", (qid,))
            self.assertEqual(migrate(conn), 4)        # 补统一 v6 profiles + v7 batches + v8 订阅 + v10 领域
            self.assertEqual(
                conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                SCHEMA_VERSION)
            versions = {r["version"] for r in conn.execute(
                "SELECT version FROM schema_migrations")}
            self.assertEqual(versions, set(range(1, SCHEMA_VERSION + 1)))
            # 旧行原样：主机域列没被新领域列顶掉
            row = conn.execute("SELECT query, domain, inferred_domain FROM watch_queries"
                               " WHERE id = ?", (qid,)).fetchone()
            self.assertEqual(row["query"], "旧词")
            self.assertEqual(row["domain"], "")        # v6 行补默认空串
            self.assertEqual(row["inferred_domain"], "")
            feed = conn.execute("SELECT domain, topic_domain FROM feed_entries"
                                " WHERE query_id = ?", (qid,)).fetchone()
            self.assertEqual(feed["domain"], "a.example.com")
            self.assertEqual(feed["topic_domain"], "")
        finally:
            conn.close()


class TestWatchQueries(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = PlatformStore(Path(self.tmp.name) / "dossier.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_add_list_enable_delete(self):
        qid = self.store.add_watch_query("话题A", note="n")
        self.assertIsInstance(qid, int)
        self.assertIsNone(self.store.add_watch_query("话题A"))     # 重复 → None
        rows = self.store.list_watch_queries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["query"], "话题A")
        self.assertTrue(rows[0]["enabled"])
        self.assertTrue(self.store.set_watch_query_enabled(qid, False))
        self.assertEqual(self.store.list_watch_queries(enabled_only=True), [])
        self.assertTrue(self.store.delete_watch_query(qid))
        self.assertEqual(self.store.list_watch_queries(), [])

    def test_add_empty_query_raises(self):
        with self.assertRaises(ValueError):
            self.store.add_watch_query("   ")

    def test_ensure_watch_query_idempotent(self):
        """ensure：首调创建、再调返回同一 id（巡检按文本定位 query_id）。"""
        qid = self.store.ensure_watch_query("话题A")
        self.assertEqual(self.store.ensure_watch_query("话题A"), qid)
        self.assertEqual(len(self.store.list_watch_queries()), 1)
        with self.assertRaises(ValueError):
            self.store.ensure_watch_query("")

    def test_enable_missing_id_returns_false(self):
        self.assertFalse(self.store.set_watch_query_enabled(999, True))
        self.assertFalse(self.store.delete_watch_query(999))

    def test_watch_query_exists(self):
        qid = self.store.ensure_watch_query("话题A")
        self.assertTrue(self.store.watch_query_exists(qid))
        self.assertFalse(self.store.watch_query_exists(999))
        self.store.delete_watch_query(qid)
        self.assertFalse(self.store.watch_query_exists(qid))


class TestFeedEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = PlatformStore(Path(self.tmp.name) / "dossier.db")
        self.qid = self.store.add_watch_query("话题A")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _add(self, url="https://a.example.com/1", **kw):
        return self.store.add_feed_entry(query_id=self.qid, title="标题", url=url, **kw)

    def test_add_and_duplicate_url(self):
        eid = self._add()
        self.assertIsInstance(eid, int)
        self.assertIsNone(self._add())                            # URL 唯一 → None
        self.assertIsInstance(self._add(url="https://b.example.com/2"), int)

    def test_get_and_list_filters(self):
        self._add(url="https://a.example.com/1", e_score=12.0)
        e2 = self._add(url="https://b.example.com/2", e_score=70.0)
        self.store.update_feed_entry(e2, risk_level="high")
        self.assertEqual(len(self.store.list_feed_entries(limit=10)), 2)
        highs = self.store.list_feed_entries(risk="high")
        self.assertEqual(len(highs), 1)
        self.assertEqual(highs[0]["id"], e2)
        self.assertEqual(len(self.store.list_feed_entries(risk="high,urgent")), 1)
        self.assertEqual(len(self.store.list_feed_entries(status="new")), 2)

    def test_update_feed_entry_whitelist_and_json(self):
        eid = self._add()
        ok = self.store.update_feed_entry(
            eid, status="verified", verdict_label="疑似谣言", risk_score=62.0,
            risk_level="suspect", risk_reasons=["热度 70", "权威信源 2 个"],
            dossier_id=None, e_prev=30.0, evil_key="x")
        self.assertTrue(ok)
        row = self.store.get_feed_entry(eid)
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["risk_reasons"], ["热度 70", "权威信源 2 个"])
        self.assertEqual(row["e_prev"], 30.0)
        self.assertNotIn("evil_key", row)                          # 白名单外键被忽略
        self.assertFalse(self.store.update_feed_entry(eid))        # 空更新 → False

    def test_update_bad_status_raises(self):
        eid = self._add()
        with self.assertRaises(ValueError):
            self.store.update_feed_entry(eid, status="bogus")

    def test_add_bad_status_raises(self):
        with self.assertRaises(ValueError):
            self._add(status="bogus")

    def test_add_orphaned_query_raises_not_dedup(self):
        """query_id 不存在（监控词被删）→ 外键冲突原样抛，不伪装成去重 None
        （冒烟测试抓到过：误记 deduped 会掩盖"巡检中途删词"的真实情况）。"""
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_feed_entry(query_id=999, title="t",
                                      url="https://a.example.com/1")

    def test_warnings_ordering(self):
        e1 = self._add(url="https://a.example.com/1")
        e2 = self._add(url="https://b.example.com/2")
        e3 = self._add(url="https://c.example.com/3")
        self.store.update_feed_entry(e1, risk_level="high")
        self.store.update_feed_entry(e2, risk_level="urgent")
        self.store.update_feed_entry(e3, risk_level="notice")
        rows = self.store.warnings()
        self.assertEqual([r["id"] for r in rows], [e2, e1])        # urgent 在前

    def test_latest_e_for_query(self):
        """环比基准：同查询词最近一次 E；无记录/他查询词 → None。"""
        other = self.store.add_watch_query("别的词")
        self.assertIsNone(self.store.latest_e_for_query(self.qid))
        e1 = self._add(url="https://a.example.com/1", e_score=30.0)
        self.assertIsNone(self.store.latest_e_for_query(other))     # 他查询词不受影响
        e2 = self._add(url="https://b.example.com/2", e_score=55.0)
        self.assertEqual(self.store.latest_e_for_query(self.qid), 55.0)   # 取最新

    def test_purge_expired_feed(self):
        eid = self._add()
        row = self.store.get_feed_entry(eid)
        self.assertIsNone(row["result_expires_at"])
        self.store.update_feed_entry(eid)                          # 无字段 → 不动
        self.assertEqual(self.store.purge_expired_feed(), 0)
        # 手工置一个过去时间再清理
        with self.store._lock, self.store._conn:
            self.store._conn.execute(
                "UPDATE feed_entries SET result_expires_at = '2020-01-01T00:00:00+08:00'"
                " WHERE id = ?", (eid,))
        self.assertEqual(self.store.purge_expired_feed(), 1)
        self.assertIsNone(self.store.get_feed_entry(eid))

    def test_delete_query_cascades_feed(self):
        self._add()
        self.assertTrue(self.store.delete_watch_query(self.qid))
        self.assertEqual(self.store.list_feed_entries(), [])


class TestScanRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = PlatformStore(Path(self.tmp.name) / "dossier.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_start_finish_list_latest(self):
        sid = self.store.start_scan_run()
        run = self.store.get_scan_run(sid)
        self.assertEqual(run["status"], "running")
        self.assertIsNone(run["finished_at"])
        self.assertTrue(self.store.finish_scan_run(
            sid, "partial", queries=3, candidates=7, verified=1, degraded=2,
            error="search degraded"))
        run = self.store.get_scan_run(sid)
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["queries"], 3)
        self.assertEqual(run["candidates"], 7)
        self.assertEqual(run["verified"], 1)
        self.assertEqual(run["degraded"], 2)
        self.assertEqual(run["error"], "search degraded")
        self.assertIsNotNone(run["finished_at"])
        self.assertEqual(self.store.latest_scan_run()["id"], sid)
        self.assertEqual(len(self.store.list_scan_runs(limit=10)), 1)

    def test_finish_bad_status_raises(self):
        sid = self.store.start_scan_run()
        with self.assertRaises(ValueError):
            self.store.finish_scan_run(sid, "bogus")
        self.assertFalse(self.store.finish_scan_run(999, "completed"))

    def test_status_sets_are_closed(self):
        self.assertEqual(set(FEED_STATUSES), {"new", "screened", "verified",
                                              "verify_failed", "deduped"})
        self.assertEqual(set(SCAN_STATUSES), {"running", "completed", "partial",
                                              "failed", "skipped", "cancelled"})


class TestPatrolSources(unittest.TestCase):
    """用户自建信息源（v7）：CRUD、url 唯一闸、绑定汇总/重建/级联、可见性。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = PlatformStore(Path(self.tmp.name) / "dossier.db")
        self.q1 = self.store.add_watch_query("词1")
        self.q2 = self.store.add_watch_query("词2")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _bind_count(self, source_id):
        with self.store._lock:
            return self.store._conn.execute(
                "SELECT COUNT(*) FROM patrol_query_sources WHERE source_id = ?",
                (source_id,)).fetchone()[0]

    def test_add_and_get_patrol_source(self):
        sid = self.store.add_patrol_source("新华网", "https://www.xinhuanet.com/rss",
                                           kind="rss", note="官方通报")
        self.assertIsInstance(sid, int)
        row = self.store.get_patrol_source(sid)
        self.assertEqual(row["id"], sid)
        self.assertEqual(row["name"], "新华网")
        self.assertEqual(row["url"], "https://www.xinhuanet.com/rss")
        self.assertEqual(row["kind"], "rss")
        self.assertTrue(row["enabled"])
        self.assertEqual(row["note"], "官方通报")
        self.assertIsNotNone(row["created_at"])
        self.assertEqual(row["query_ids"], [])                 # 未绑定

    def test_add_page_kind_ok(self):
        sid = self.store.add_patrol_source("专题页", "https://a.example.com/topic",
                                           kind="page")
        self.assertEqual(self.store.get_patrol_source(sid)["kind"], "page")

    def test_add_duplicate_url_returns_none(self):
        self.assertIsNotNone(self.store.add_patrol_source("A", "https://a.example.com/rss"))
        self.assertIsNone(self.store.add_patrol_source("B", "https://a.example.com/rss"))

    def test_add_bad_kind_raises(self):
        with self.assertRaises(ValueError):
            self.store.add_patrol_source("A", "https://a.example.com/x", kind="atom")

    def test_add_binds_queries(self):
        sid = self.store.add_patrol_source("S", "https://s.example.com/rss",
                                           query_ids=[self.q1, self.q2])
        self.assertEqual(self.store.get_patrol_source(sid)["query_ids"], [self.q1, self.q2])

    def test_add_binding_to_missing_query_raises(self):
        """绑定不存在的 query_id → 外键冲突原样抛（不是去重），源也不落库。"""
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_patrol_source("S", "https://s.example.com/rss", query_ids=[999])
        self.assertEqual(self.store.list_patrol_sources(), [])

    def test_list_aggregates_query_ids(self):
        s1 = self.store.add_patrol_source("S1", "https://s1.example.com/rss",
                                          query_ids=[self.q1, self.q2])
        s2 = self.store.add_patrol_source("S2", "https://s2.example.com/rss",
                                          query_ids=[self.q2])
        rows = self.store.list_patrol_sources()
        self.assertEqual([r["id"] for r in rows], [s1, s2])     # id 升序
        self.assertEqual(rows[0]["query_ids"], [self.q1, self.q2])
        self.assertEqual(rows[1]["query_ids"], [self.q2])
        self.assertTrue(all(r["enabled"] is True for r in rows))

    def test_list_enabled_only(self):
        s1 = self.store.add_patrol_source("S1", "https://s1.example.com/rss")
        s2 = self.store.add_patrol_source("S2", "https://s2.example.com/rss")
        self.assertTrue(self.store.set_patrol_source_enabled(s2, False))
        self.assertEqual([r["id"] for r in self.store.list_patrol_sources(enabled_only=True)],
                         [s1])
        self.assertFalse(self.store.set_patrol_source_enabled(999, True))

    def test_update_note_keeps_bindings(self):
        sid = self.store.add_patrol_source("S", "https://s.example.com/rss",
                                           note="旧备注", query_ids=[self.q1])
        self.assertTrue(self.store.update_patrol_source(sid, note="新备注"))
        row = self.store.get_patrol_source(sid)
        self.assertEqual(row["note"], "新备注")
        self.assertEqual(row["query_ids"], [self.q1])           # 绑定未动

    def test_update_clear_and_rebuild_bindings(self):
        sid = self.store.add_patrol_source("S", "https://s.example.com/rss",
                                           query_ids=[self.q1, self.q2])
        self.assertTrue(self.store.update_patrol_source(sid, query_ids=[]))
        self.assertEqual(self.store.get_patrol_source(sid)["query_ids"], [])
        self.assertEqual(self._bind_count(sid), 0)
        self.assertTrue(self.store.update_patrol_source(sid, query_ids=[self.q2]))
        self.assertEqual(self.store.get_patrol_source(sid)["query_ids"], [self.q2])
        self.assertEqual(self._bind_count(sid), 1)

    def test_update_missing_source_returns_false(self):
        self.assertFalse(self.store.update_patrol_source(999, note="x"))
        self.assertFalse(self.store.update_patrol_source(999, query_ids=[self.q1]))

    def test_delete_cascades_bindings_and_shrinks_visibility(self):
        sid = self.store.add_patrol_source("S", "https://s.example.com/rss",
                                           query_ids=[self.q1])
        before = len(self.store.sources_for_query(self.q1))
        self.assertTrue(self.store.delete_patrol_source(sid))
        self.assertIsNone(self.store.get_patrol_source(sid))
        self.assertEqual(self._bind_count(sid), 0)              # 级联消失
        self.assertEqual(len(self.store.sources_for_query(self.q1)), before - 1)
        self.assertFalse(self.store.delete_patrol_source(sid))

    def test_delete_query_cascades_source_bindings(self):
        sid = self.store.add_patrol_source("S", "https://s.example.com/rss",
                                           query_ids=[self.q1])
        self.assertTrue(self.store.delete_watch_query(self.q1))
        self.assertEqual(self.store.get_patrol_source(sid)["query_ids"], [])

    def test_sources_for_query_visibility(self):
        glob = self.store.add_patrol_source("全局源", "https://g.example.com/rss")
        mine = self.store.add_patrol_source("词1的源", "https://m.example.com/rss",
                                            query_ids=[self.q1])
        theirs = self.store.add_patrol_source("词2的源", "https://t.example.com/rss",
                                              query_ids=[self.q2])
        self.store.set_patrol_source_enabled(theirs, False)     # 停用即不可见
        rows = self.store.sources_for_query(self.q1)
        self.assertEqual([r["id"] for r in rows], [glob, mine])  # 仅全局 + 本词绑定
        self.assertEqual(rows[0]["query_ids"], [])
        self.assertEqual(rows[1]["query_ids"], [self.q1])
        self.assertEqual([r["id"] for r in self.store.sources_for_query(self.q2)], [glob])
        self.assertEqual([r["id"] for r in self.store.sources_for_query(None)], [glob])


class TestWatchQueryDomain(unittest.TestCase):
    """监控词领域（v7）：手改清缓存 / 自动推断缓存 / 读回。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = PlatformStore(Path(self.tmp.name) / "dossier.db")
        self.qid = self.store.add_watch_query("自来水 异味")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_set_domain_clears_inferred_cache(self):
        self.assertTrue(self.store.set_inferred_domain(
            self.qid, "public_health", "2026-11-21T10:00:00+08:00"))
        row = self.store.get_watch_query(self.qid)
        self.assertEqual(row["inferred_domain"], "public_health")
        self.assertEqual(row["inferred_at"], "2026-11-21T10:00:00+08:00")
        self.assertTrue(self.store.set_watch_query_domain(self.qid, "food_safety"))
        row = self.store.get_watch_query(self.qid)
        self.assertEqual(row["domain"], "food_safety")
        self.assertEqual(row["inferred_domain"], "")           # 手改即废缓存
        self.assertEqual(row["inferred_at"], "")
        self.assertTrue(self.store.set_watch_query_domain(self.qid, ""))  # 恢复自动推断
        self.assertEqual(self.store.get_watch_query(self.qid)["domain"], "")

    def test_set_inferred_domain_keeps_user_domain(self):
        self.assertTrue(self.store.set_watch_query_domain(self.qid, "public_health"))
        self.assertTrue(self.store.set_inferred_domain(self.qid, "env_risk", "t2"))
        row = self.store.get_watch_query(self.qid)
        self.assertEqual(row["domain"], "public_health")        # 手设不动
        self.assertEqual(row["inferred_domain"], "env_risk")
        self.assertEqual(row["inferred_at"], "t2")

    def test_get_watch_query_missing_and_shape(self):
        self.assertIsNone(self.store.get_watch_query(999))
        row = self.store.get_watch_query(self.qid)
        for key in ("id", "query", "enabled", "note", "created_at",
                    "domain", "inferred_domain", "inferred_at"):
            self.assertIn(key, row)
        self.assertIs(row["enabled"], True)                     # bool 化
        self.assertEqual(row["domain"], "")                     # 默认空串

    def test_domain_setters_missing_id_return_false(self):
        self.assertFalse(self.store.set_watch_query_domain(999, "x"))
        self.assertFalse(self.store.set_inferred_domain(999, "x", "t"))


class TestFeedDomainFields(unittest.TestCase):
    """信息流领域字段（v7）：往返、白名单更新、直方图；主机域参数不回归。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = PlatformStore(Path(self.tmp.name) / "dossier.db")
        self.qid = self.store.add_watch_query("话题A")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _add(self, url, **kw):
        return self.store.add_feed_entry(query_id=self.qid, title="标题", url=url, **kw)

    def test_add_and_update_feed_entry_domain(self):
        eid = self._add("https://a.example.com/1", topic_domain="public_health",
                        topic_domain_source="user")
        self.assertIsInstance(eid, int)
        row = self.store.get_feed_entry(eid)
        self.assertEqual(row["topic_domain"], "public_health")
        self.assertEqual(row["topic_domain_source"], "user")
        self.assertTrue(self.store.update_feed_entry(
            eid, topic_domain="food_safety", topic_domain_source="inferred"))
        row = self.store.get_feed_entry(eid)
        self.assertEqual(row["topic_domain"], "food_safety")
        self.assertEqual(row["topic_domain_source"], "inferred")

    def test_host_domain_param_unchanged(self):
        """v6 的 domain=（URL 主机域）参数不回归：与新领域列分列存放、互不覆盖。"""
        eid = self._add("https://a.example.com/1", domain="a.example.com",
                        domain_tier="官方权威", topic_domain="public_health")
        row = self.store.get_feed_entry(eid)
        self.assertEqual(row["domain"], "a.example.com")
        self.assertEqual(row["domain_tier"], "官方权威")
        self.assertEqual(row["topic_domain"], "public_health")

    def test_domain_fields_default_empty(self):
        eid = self._add("https://a.example.com/1")
        row = self.store.get_feed_entry(eid)
        self.assertEqual(row["topic_domain"], "")
        self.assertEqual(row["topic_domain_source"], "")

    def test_domain_histogram(self):
        self._add("https://a.example.com/1", topic_domain="public_health")
        self._add("https://a.example.com/2", topic_domain="public_health")
        self._add("https://a.example.com/3", topic_domain="food_safety")
        self._add("https://a.example.com/4")                    # 空域不计
        self.assertEqual(self.store.domain_histogram(),
                         {"public_health": 2, "food_safety": 1})

    def test_domain_histogram_limit_window(self):
        for i in range(5):
            self._add(f"https://a.example.com/{i}", topic_domain=f"d{i}")
        self.assertEqual(self.store.domain_histogram(limit=2), {"d3": 1, "d4": 1})

    def test_domain_histogram_empty(self):
        self.assertEqual(self.store.domain_histogram(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
