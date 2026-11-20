# -*- coding: utf-8 -*-
"""平台域存储测试（P1）：v6 迁移、三表 CRUD、去重闸、保留期清理、状态封闭集。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.dossier.store import SCHEMA_VERSION, connect, migrate  # noqa: E402
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
            self.assertEqual(SCHEMA_VERSION, 6)
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

    def test_enable_missing_id_returns_false(self):
        self.assertFalse(self.store.set_watch_query_enabled(999, True))
        self.assertFalse(self.store.delete_watch_query(999))


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

    def test_warnings_ordering(self):
        e1 = self._add(url="https://a.example.com/1")
        e2 = self._add(url="https://b.example.com/2")
        e3 = self._add(url="https://c.example.com/3")
        self.store.update_feed_entry(e1, risk_level="high")
        self.store.update_feed_entry(e2, risk_level="urgent")
        self.store.update_feed_entry(e3, risk_level="notice")
        rows = self.store.warnings()
        self.assertEqual([r["id"] for r in rows], [e2, e1])        # urgent 在前

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
                                              "failed", "skipped"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
