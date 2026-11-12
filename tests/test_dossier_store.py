# -*- coding: utf-8 -*-
"""档案库 SQLite 存储测试（M1-3）：CRUD、迁移、JSON 往返、单写者并发、重启持久。"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.dossier import store as ds  # noqa: E402
from mylocalai.dossier.store import (DossierStore, SCHEMA_VERSION,  # noqa: E402
                                     connect, migrate)


class TestMigrations(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Path(self.tmp.name) / "dossier.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_migrate_creates_all_tables(self):
        conn = connect(self.db)
        n = migrate(conn)
        self.assertEqual(n, SCHEMA_VERSION)
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("dossiers", "conclusions", "evidence_entries", "attention_snapshots",
                  "opinion_profiles", "deep_reads", "analysis_runs", "schema_migrations"):
            self.assertIn(t, names)
        dossier_cols = {r["name"] for r in conn.execute("PRAGMA table_info(dossiers)")}
        message_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        evidence_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evidence_entries)")}
        self.assertTrue({"result_expires_at", "raw_input_expires_at"}.issubset(dossier_cols))
        self.assertTrue({"raw_input_expires_at", "result_expires_at"}.issubset(message_cols))
        self.assertIn("cache_expires_at", evidence_cols)
        conn.close()

    def test_migrate_is_idempotent(self):
        conn = connect(self.db)
        self.assertEqual(migrate(conn), SCHEMA_VERSION)
        self.assertEqual(migrate(conn), 0)
        conn.close()

    def test_wal_mode_is_on(self):
        conn = connect(self.db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")
        conn.close()

    def test_foreign_keys_enforced(self):
        conn = connect(self.db)
        migrate(conn)
        with self.assertRaises(sqlite_integrity_error()):
            conn.execute("INSERT INTO conclusions (dossier_id, version, created_at)"
                         " VALUES (999, 1, 'now')")
        conn.close()


def sqlite_integrity_error():
    import sqlite3
    return sqlite3.IntegrityError


class TestDossierCRUD(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = DossierStore(Path(self.tmp.name) / "dossier.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_create_and_get(self):
        did = self.store.create_dossier("某谣言核查", "听说自来水被下毒了", input_kind="text")
        d = self.store.get_dossier(did)
        self.assertIsNotNone(d)
        self.assertEqual(d["title"], "某谣言核查")
        self.assertEqual(d["input_kind"], "text")
        self.assertIsNone(self.store.get_dossier(424242))

    def test_input_kind_whitelist(self):
        with self.assertRaises(AssertionError):
            self.store.create_dossier("t", "x", input_kind="audio")

    def test_list_and_query(self):
        a = self.store.create_dossier("隔夜茶致癌？", "网传喝隔夜茶会致癌")
        self.store.create_dossier("天气", "今天天气不错")
        names = [d["id"] for d in self.store.list_dossiers()]
        self.assertIn(a, names)
        hits = [d["id"] for d in self.store.list_dossiers(query="隔夜茶")]
        self.assertEqual(hits, [a])
        self.assertEqual(len(self.store.list_dossiers(limit=1)), 1)

    def test_delete_cascades(self):
        did = self.store.create_dossier("t", "x")
        self.store.add_conclusion(did, "疑似谣言", 62)
        self.assertTrue(self.store.delete_dossier(did))
        self.assertIsNone(self.store.get_dossier(did))
        self.assertEqual(self.store.conclusions(did), [])

    def test_conclusion_versioning(self):
        did = self.store.create_dossier("t", "x")
        v1 = self.store.add_conclusion(did, "存疑待核实", 40, confidence="medium",
                                       evidence_sufficiency={"official": 0, "total": 3},
                                       as_of="2026-10-30T23:00:00+08:00",
                                       recheck_conditions=["出现官方通报"],
                                       raw_verdict={"label": "存疑待核实"})
        v2 = self.store.add_conclusion(did, "疑似谣言", 62, confidence="high")
        self.assertEqual((v1, v2), (1, 2))
        cs = self.store.conclusions(did)
        self.assertEqual([c["version"] for c in cs], [1, 2])
        self.assertEqual(cs[-1]["label"], "疑似谣言")
        self.assertEqual(cs[0]["evidence_sufficiency"]["total"], 3)
        self.assertEqual(cs[0]["recheck_conditions"], ["出现官方通报"])
        self.assertEqual(self.store.latest_conclusion(did)["version"], 2)


class TestJsonRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = DossierStore(Path(self.tmp.name) / "dossier.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_evidence_roundtrip(self):
        did = self.store.create_dossier("t", "x")
        n = self.store.add_evidence(did, 1, [
            {"url": "https://example.gov.cn/a", "domain": "example.gov.cn",
             "domain_tier": "官方", "source_kind": "official", "stance": "支持",
             "title": "通报", "snippet": "官方通报称……", "fetched_at": "2026-10-30T22:00:00+08:00"},
            {"url": "https://news.example.com/b", "domain_tier": "门户"},
        ])
        self.assertEqual(n, 2)
        rows = self.store.evidence(did)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["domain_tier"], "官方")
        self.assertEqual(rows[0]["fetched_at"], "2026-10-30T22:00:00+08:00")
        self.assertEqual(self.store.evidence(did, batch=2), [])

    def test_snapshot_and_opinions_and_deepread(self):
        did = self.store.create_dossier("t", "x")
        self.store.add_snapshot(did, 10.0, 100.0, 10.0, 55.1, profile="spec",
                                proxy_flags=["proxy:y"])
        self.store.add_opinions(did, 1, {"支持": 3, "反对": 2, "中立": 1},
                                summary="两极分化", degraded=False)
        self.store.add_deep_read(did, {"sections": []}, degraded=True, guard_dropped=["x"])
        self.store.add_run(did, "verify", "ok", degraded=False, duration_s=42.5,
                           requests=9, tokens=1234)
        snap = self.store.snapshots(did)[0]
        self.assertEqual((snap["x"], snap["e_score"], snap["proxy_flags"]),
                         (10.0, 55.1, ["proxy:y"]))
        op = self.store.opinions(did)[0]
        self.assertEqual(op["distribution"], {"支持": 3, "反对": 2, "中立": 1})
        self.assertFalse(op["degraded"])
        dr = self.store.deep_reads(did)[0]
        self.assertTrue(dr["degraded"])
        self.assertEqual(dr["guard_dropped"], ["x"])
        run = self.store.runs(did)[0]
        self.assertEqual((run["kind"], run["status"], run["requests"], run["tokens"]),
                         ("verify", "ok", 9, 1234))


class TestPersistenceAndConcurrency(unittest.TestCase):
    def test_data_survives_reopen(self):
        """服务重启后数据还在（新连接重开同一文件）。"""
        tmp = TemporaryDirectory()
        try:
            db = Path(tmp.name) / "dossier.db"
            s1 = DossierStore(db)
            did = s1.create_dossier("重启还在", "正文")
            s1.add_conclusion(did, "疑似谣言", 62)
            s1.close()
            s2 = DossierStore(db)
            try:
                d = s2.get_dossier(did)
                self.assertEqual(d["title"], "重启还在")
                self.assertEqual(s2.latest_conclusion(did)["label"], "疑似谣言")
            finally:
                s2.close()
        finally:
            tmp.cleanup()

    def test_single_writer_concurrent_threads(self):
        """多线程同时写：WAL + busy_timeout 下不丢不崩，行数精确。"""
        tmp = TemporaryDirectory()
        try:
            store = DossierStore(Path(tmp.name) / "dossier.db")
            did = store.create_dossier("并发", "x")
            errors: list = []

            def worker(idx: int) -> None:
                try:
                    for _ in range(5):
                        store.add_conclusion(did, f"标签{idx}", float(idx))
                        store.add_run(did, "verify", "ok")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{type(exc).__name__}: {exc}")

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(store.conclusions(did)), 20)
            self.assertEqual(len(store.runs(did)), 20)
            store.close()
        finally:
            tmp.cleanup()


class TestMarkersStore(unittest.TestCase):
    """L4 参谋：schema v4 迁移与 dossier_markers 读写（record_markers 一档案一条幂等）。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Path(self.tmp.name) / "dossier.db"

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _scan():
        """手工构造的 scan_markers 返回形状（确定性，不依赖词表内容）。"""
        return {
            "risk_hits": {"source_vague": {"label": "来源模糊", "hits": ["听说"], "points": 10},
                          "fear_claim": {"label": "恐吓诉求", "hits": ["根本不敢说"], "points": 12}},
            "credibility_hits": {"official_channel": {"label": "官方渠道线索",
                                                      "hits": ["gov.cn"], "points": -8}},
            "marker_points": 22, "credibility_points": -8, "assertion_signal": True,
        }

    def test_fresh_migrate_reaches_v4_with_markers_table(self):
        """全新库 migrate：应用 4 段迁移、schema_migrations 记到 v4、
        dossier_markers 表与唯一索引都在。"""
        conn = connect(self.db)
        try:
            self.assertEqual(migrate(conn), 4)
            self.assertEqual(SCHEMA_VERSION, 4)
            self.assertEqual(
                conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 4)
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("dossier_markers", names)
            idx = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn("idx_dossier_markers_dossier", idx)
        finally:
            conn.close()   # 先关连接再让 tearDown 清临时目录（Windows 文件占用）

    def test_upgrade_from_v3_keeps_history(self):
        """v3 老库升级路径：手工只跑 MIGRATIONS[0:3] 建库、登记版本 1-3（真实 v3 库
        的 schema_migrations 状态）并写入历史数据，再 migrate 只补 v4 一段——
        历史档案与结论原样保留。"""
        conn = connect(self.db)
        try:
            # schema_migrations 由 migrate() 引导创建，不在迁移脚本里——手工补上
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                                version    INTEGER PRIMARY KEY,
                                applied_at TEXT NOT NULL)""")
            for i, script in enumerate(ds.MIGRATIONS[:3], start=1):
                with conn:
                    conn.executescript(script)
                    conn.execute("INSERT INTO schema_migrations (version, applied_at)"
                                 " VALUES (?, '2026-10-01T00:00:00+08:00')", (i,))
            did = conn.execute(
                "INSERT INTO dossiers (created_at, updated_at, title, input_text)"
                " VALUES ('2026-10-01T08:00:00+08:00', '2026-10-01T08:00:00+08:00',"
                " '旧档', '老正文')").lastrowid
            conn.execute(
                "INSERT INTO conclusions (dossier_id, version, label, created_at)"
                " VALUES (?, 1, '疑似谣言', '2026-10-01T08:00:00+08:00')", (did,))
            self.assertEqual(migrate(conn), 1)      # 只补跑 v4
            self.assertEqual(
                conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 4)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM dossiers").fetchone()[0], 1)
            row = conn.execute("SELECT label FROM conclusions WHERE dossier_id = ?",
                               (did,)).fetchone()
            self.assertEqual(row["label"], "疑似谣言")
        finally:
            conn.close()

    def test_migrate_twice_is_idempotent(self):
        """重复 migrate 幂等：第二次返回 0，schema_migrations 仍恰好 4 行。"""
        conn = connect(self.db)
        try:
            self.assertEqual(migrate(conn), 4)
            self.assertEqual(migrate(conn), 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 4)
        finally:
            conn.close()

    def test_record_markers_insert_idempotent_and_roundtrip(self):
        """record_markers：首插 True、二插 False（一档案一条）；risk/credibility hits
        JSON 读写往返还原、分数原样入库；档案不存在让外键约束自然抛。"""
        store = DossierStore(self.db)
        try:
            did = store.create_dossier("隔夜茶", "听说隔夜茶致癌")
            self.assertTrue(store.record_markers(did, self._scan()))
            self.assertFalse(store.record_markers(did, self._scan()))
            rows = store._conn.execute("SELECT * FROM dossier_markers").fetchall()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["dossier_id"], did)
            self.assertEqual(json.loads(row["risk_hits"]), self._scan()["risk_hits"])
            self.assertEqual(json.loads(row["credibility_hits"]),
                             self._scan()["credibility_hits"])
            self.assertEqual(row["marker_points"], 22)
            self.assertEqual(row["credibility_points"], -8)
            self.assertTrue(row["created_at"])
            with self.assertRaises(sqlite_integrity_error()):
                store.record_markers(424242, self._scan())
        finally:
            store.close()

    def test_record_markers_empty_scan_not_inserted(self):
        """空 scan / 无 risk_hits 键：返回 False 且不落行。"""
        store = DossierStore(self.db)
        try:
            did = store.create_dossier("t", "x")
            self.assertFalse(store.record_markers(did, {}))
            self.assertFalse(store.record_markers(did, {"marker_points": 0}))
            self.assertEqual(
                store._conn.execute("SELECT COUNT(*) FROM dossier_markers").fetchone()[0], 0)
        finally:
            store.close()

    def test_markers_survive_reopen(self):
        """重开连接（模拟服务重启）：marker 行还在。"""
        store = DossierStore(self.db)
        did = store.create_dossier("t", "x")
        store.record_markers(did, self._scan())
        store.close()
        store2 = DossierStore(self.db)
        try:
            row = store2._conn.execute(
                "SELECT marker_points, credibility_points FROM dossier_markers"
                " WHERE dossier_id = ?", (did,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual((row["marker_points"], row["credibility_points"]), (22, -8))
        finally:
            store2.close()

    def test_delete_dossier_cascades_markers(self):
        """删档案级联删 marker 行（外键 ON DELETE CASCADE）。"""
        store = DossierStore(self.db)
        try:
            did = store.create_dossier("t", "x")
            store.record_markers(did, self._scan())
            self.assertTrue(store.delete_dossier(did))
            self.assertEqual(
                store._conn.execute("SELECT COUNT(*) FROM dossier_markers").fetchone()[0], 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
