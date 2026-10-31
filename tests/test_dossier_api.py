# -*- coding: utf-8 -*-
"""档案库 API 离线测试（M2 后端先行，M3 档案库页消费）：
   /api/dossiers 列表 / 详情 / Markdown 导出。

数据用 DossierStore 临时库预置，monkeypatch app.get_dossier_service 注入
（工厂是 web 层与默认库路径的解耦点，与 build_assistant_loop 同一模式）；
数据一律经 DossierService/store 读取，不直连引擎。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_dossier_api -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到 web/app.py


class DossierApiTestBase(unittest.TestCase):
    """公共夹具：临时档案库 + 预置数据 + 服务工厂注入。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001  fastapi 缺失时整组跳过而非报错
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.client = TestClient(webapp.app)

    def setUp(self) -> None:
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DossierStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)
        self.service = DossierService(self.store)
        patcher = mock.patch.object(self.webapp, "get_dossier_service",
                                    lambda: self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

        # ---- 预置两个档案 ----
        self.did_tea = self.store.create_dossier(
            "隔夜茶致癌？", "网传喝隔夜茶会致癌，已经有人确诊", input_kind="text")
        self.store.add_conclusion(
            self.did_tea, "存疑待核实", 40.0, confidence="low",
            evidence_sufficiency={"official": 0, "unofficial": 3, "unknown": 1, "total": 4},
            as_of="2026-10-31T10:00:00+08:00",
            recheck_conditions=["出现官方/权威媒体级信源的通报或报道", "证据本独立条目≥5"],
            raw_verdict={"label": "存疑待核实", "risk_score": 40.0})
        self.store.add_evidence(self.did_tea, 1, [
            {"url": "https://example.gov.cn/a", "domain": "example.gov.cn",
             "domain_tier": "官方", "source_kind": "官方（监管部门）", "stance": "反驳",
             "title": "监管部门辟谣", "snippet": "官方通报称无此案", "fetched_at": "2026-10-31T09:00:00+08:00"},
            {"url": "https://news.example.com/b", "domain": "news.example.com",
             "domain_tier": "门户", "source_kind": "非官方（门户）", "stance": "支持",
             "title": "网文流传", "snippet": "仍在传播", "fetched_at": "2026-10-31T09:10:00+08:00"},
        ])
        self.store.add_snapshot(self.did_tea, 3.0, 12000.0, 45.0, 51.2,
                                profile="spec", proxy_flags=["proxy:y"])
        self.did_weather = self.store.create_dossier("今日天气", "今天天气不错", input_kind="topic")

    def get(self, path: str):
        return self.client.get(path)


class TestDossierList(DossierApiTestBase):
    def test_list_query_and_limit(self):
        """列表默认全量（updated_at 倒序）；query 过滤；limit 截断。"""
        resp = self.get("/api/dossiers")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["dossiers"]
        self.assertEqual({d["id"] for d in items}, {self.did_tea, self.did_weather})
        for d in items:
            self.assertIn("title", d)
            self.assertIn("updated_at", d)
            self.assertIn("input_kind", d)

        hits = self.get("/api/dossiers?query=隔夜茶").json()["dossiers"]
        self.assertEqual([d["id"] for d in hits], [self.did_tea])
        self.assertEqual(len(self.get("/api/dossiers?limit=1").json()["dossiers"]), 1)


class TestDossierDetail(DossierApiTestBase):
    def test_detail_shape_and_compare_versions(self):
        """档案头 + conclusions（JSON 已还原）+ 证据批次 + 快照计数 + compare_versions 版本。"""
        resp = self.get(f"/api/dossiers/{self.did_tea}")
        self.assertEqual(resp.status_code, 200)
        d = resp.json()
        self.assertEqual(d["title"], "隔夜茶致癌？")
        self.assertEqual(d["input_kind"], "text")
        self.assertIn("input_text", d)
        self.assertIn("updated_at", d)

        self.assertEqual(len(d["conclusions"]), 1)
        c = d["conclusions"][0]
        self.assertEqual(c["version"], 1)
        self.assertEqual(c["label"], "存疑待核实")
        self.assertEqual(c["evidence_sufficiency"]["total"], 4)
        self.assertEqual(c["recheck_conditions"],
                         ["出现官方/权威媒体级信源的通报或报道", "证据本独立条目≥5"])
        self.assertEqual(c["as_of"], "2026-10-31T10:00:00+08:00")

        self.assertEqual(d["evidence_batches"], {"1": 2})
        self.assertEqual(d["snapshot_count"], 1)
        self.assertEqual([v["version"] for v in d["versions"]], [1])
        self.assertFalse(d["changed"])

    def test_versioned_dossier_reports_changed(self):
        """追加 v2 后：versions 两行、changed=True（recheck 版本化的数据面）。"""
        self.store.add_conclusion(self.did_tea, "疑似谣言", 62.0, confidence="medium",
                                  as_of="2026-10-31T11:00:00+08:00")
        d = self.get(f"/api/dossiers/{self.did_tea}").json()
        self.assertEqual([v["version"] for v in d["versions"]], [1, 2])
        self.assertTrue(d["changed"])
        self.assertEqual(len(d["conclusions"]), 2)

    def test_detail_invalid_id_is_404(self):
        """非法 id（不存在 / 非整数 / 非正数）→ 一律 404。"""
        self.assertEqual(self.get("/api/dossiers/999999").status_code, 404)
        self.assertEqual(self.get("/api/dossiers/abc").status_code, 404)
        self.assertEqual(self.get("/api/dossiers/-1").status_code, 404)


class TestDossierExport(DossierApiTestBase):
    def test_export_md_contains_as_of_and_disclaimer(self):
        """format=md → Markdown 文本：含标题/结论/as_of/复审条件/证据/免责语。"""
        resp = self.get(f"/api/dossiers/{self.did_tea}/export?format=md")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/markdown"))
        text = resp.text
        for needle in ("# 档案：隔夜茶致癌？", "存疑待核实", "as_of",
                       "2026-10-31T10:00:00+08:00", "出现官方/权威媒体级信源的通报或报道",
                       "example.gov.cn", "批次 1", "免责声明", "只述证据"):
            self.assertIn(needle, text, f"导出缺少：{needle}")

    def test_export_rejects_other_formats_and_missing_id(self):
        self.assertEqual(
            self.get(f"/api/dossiers/{self.did_tea}/export?format=html").status_code, 400)
        self.assertEqual(self.get("/api/dossiers/999999/export").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
