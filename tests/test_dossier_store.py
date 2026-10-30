# -*- coding: utf-8 -*-
"""档案库 SQLite 存储测试（M1-3）：CRUD、迁移、JSON 往返、单写者并发、重启持久。"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
