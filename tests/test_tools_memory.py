# -*- coding: utf-8 -*-
"""记忆类工具测试（工作指导 §5.2/§7.2）：list/get/export 与档案库一致性。

对着真实事故形态锁定：
  - list/get/export 正反例（档案不存在、没有结论、format 非法都不崩、都诚实说）；
  - 缺章如实 absent（懒加载是常态，不编造章节）；
  - export md 含卡片字段 + as_of + 免责语（快照式断言，M3-6 做文件落盘时不漂移）；
  - 与 store 直读对拍：工具不是第二个事实源，只是 store 的读封装。
全离线：:memory: 库 + tempfile 兜底，零网络。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.loop import AssistantLoop  # noqa: E402
from mylocalai.assistant.registry import ValidationError  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402

OFFICIAL = {"url": "https://gov.example/1", "domain": "gov.example",
            "domain_tier": "官方权威", "source_kind": "官方（新华网）",
            "stance": "反驳", "title": "官方辟谣", "snippet": "不实信息",
            "fetched_at": "2026-10-31T08:00:00+08:00"}
UNOFFICIAL = {"url": "https://bbs.example/2", "domain": "bbs.example",
              "source_kind": "非官方（微博）", "stance": "支持",
              "title": "网帖", "snippet": "听说……"}


def verify_result(label="疑似谣言"):
    return {"label": label, "risk_score": 62, "degraded_components": [], "agent_error": "",
            "llm_turns": 2,
            "evidence_book": {"claim": "自来水被下毒", "entries": [OFFICIAL, UNOFFICIAL]}}


class ToolsMemoryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = DossierService(DossierStore(":memory:"))
        self.addCleanup(self.service.close)
        self.registry = AssistantLoop().registry

    def ctx(self):
        return SimpleNamespace(sessions=None, dossiers=self.service, events=None,
                               cancel_check=None, services={})

    def call(self, name, **args):
        # 经注册表调用（与 loop 的真实调用路径同源，锁定"注册的 handler 就是测试的 handler"）
        spec = self.registry.get(name)
        assert spec is not None, name
        return spec["handler"](self.ctx(), **args)

    def make_dossier(self, title="自来水下毒核查", with_conclusion=True):
        # input_text 跟着 title 走：list_dossiers 对 title OR input_text 做 LIKE，
        # 固定文本会让"按 title 筛选"的用例互相串（这里测的就是筛选语义）。
        did = self.service.start_dossier(f"关于{title}的待核文本", title=title)
        if with_conclusion:
            self.service.record_analysis(did, verify_result(), kind="verify")
        return did


class TestListDossiers(ToolsMemoryTestBase):
    def test_lists_all_with_fields(self):
        a = self.make_dossier("第一条")
        b = self.make_dossier("第二条", with_conclusion=False)
        out = self.call("list_dossiers")
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 2)
        ids = [it["id"] for it in out["items"]]
        self.assertIn(a, ids)
        self.assertIn(b, ids)
        self.assertEqual({it["title"] for it in out["items"]}, {"第一条", "第二条"})
        self.assertEqual(out["cost"], {"requests": 0, "tokens": 0})

    def test_query_filter_and_limit(self):
        self.make_dossier("自来水核查")
        self.make_dossier("疫苗核查", with_conclusion=False)
        out = self.call("list_dossiers", query="自来水")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["items"][0]["title"], "自来水核查")
        out_all = self.call("list_dossiers", limit=1)
        self.assertEqual(out_all["count"], 1)   # limit 截断
        out_empty = self.call("list_dossiers", query="不存在的关键词")
        self.assertEqual((out_empty["ok"], out_empty["count"], out_empty["items"]),
                         (True, 0, []))

    def test_digest_lists_lines(self):
        self.make_dossier("第一条")
        out = self.call("list_dossiers")
        self.assertIn("共 1 个", out["digest"])
        self.assertIn("#1", out["digest"])
        self.assertIn("第一条", out["digest"])


class TestGetDossier(ToolsMemoryTestBase):
    def test_full_get_with_conclusion_and_evidence(self):
        did = self.make_dossier()
        out = self.call("get_dossier", id=did)
        self.assertTrue(out["ok"])
        self.assertEqual(out["dossier_id"], did)
        # 最新结论卡（库字段还原，不重算）
        card = out["conclusion"]
        self.assertEqual(card["label"], "疑似谣言")
        self.assertEqual(card["risk_score"], 62)
        self.assertTrue(card["as_of"])
        self.assertTrue(card["recheck_conditions"])
        # 证据前 5 条 + 计数
        ev_ch = out["chapters"]["evidence"]
        self.assertEqual(ev_ch["total"], 2)
        self.assertEqual(len(ev_ch["entries"]), 2)
        self.assertEqual(ev_ch["entries"][0]["url"], OFFICIAL["url"])
        # digest 带结论要点与复审条件
        self.assertIn("疑似谣言", out["digest"])
        self.assertIn("复审条件", out["digest"])

    def test_absent_chapters_are_honest(self):
        """没生成的章节如实 {"chapter_status": "absent"}（懒加载常态，不编造）。"""
        did = self.make_dossier()
        out = self.call("get_dossier", id=did)
        chapters = out["chapters"]
        self.assertEqual(chapters["opinions"], {"chapter_status": "absent"})
        self.assertEqual(chapters["deep_read"], {"chapter_status": "absent"})
        self.assertEqual(chapters["attention"], {"chapter_status": "absent"})
        self.assertIn("未生成章节", out["digest"])
        self.assertIn("按需生成", out["digest"])

    def test_evidence_preview_caps_at_five(self):
        did = self.service.start_dossier("多条证据")
        result = verify_result()
        result["evidence_book"]["entries"] = [
            dict(OFFICIAL, url=f"https://gov.example/{i}") for i in range(8)]
        self.service.record_analysis(did, result)
        out = self.call("get_dossier", id=did)
        self.assertEqual(out["chapters"]["evidence"]["total"], 8)
        self.assertEqual(len(out["chapters"]["evidence"]["entries"]), 5)

    def test_missing_dossier_not_found(self):
        out = self.call("get_dossier", id=424242)
        self.assertFalse(out["ok"])
        self.assertIn("不存在", out["digest"])
        self.assertTrue(out.get("not_found"))


class TestExportCard(ToolsMemoryTestBase):
    def test_md_contains_fields_as_of_and_disclaimer(self):
        did = self.make_dossier()
        latest = self.service.store.conclusions(did)[-1]
        out = self.call("export_card", id=did)
        self.assertTrue(out["ok"])
        self.assertEqual(out["format"], "md")
        content = out["content"]
        # 卡片字段 + as_of + 免责语（M3-6 做文件导出时这份快照不能漂移）
        self.assertIn("# 核查结论卡片", content)
        self.assertIn("疑似谣言", content)
        self.assertIn("62", content)
        self.assertIn(latest["confidence"], content)
        self.assertIn(latest["as_of"], content)
        self.assertIn("免责声明", content)
        self.assertIn("不构成真假判定", content)
        # 不写文件：M2 只返回文本
        self.assertFalse(list(Path(self.tmp.name).iterdir()), "M2 不应产生任何文件")

    def test_missing_dossier_and_no_conclusion(self):
        out = self.call("export_card", id=424242)
        self.assertFalse(out["ok"])
        did = self.make_dossier(with_conclusion=False)
        out2 = self.call("export_card", id=did)
        self.assertFalse(out2["ok"])
        self.assertIn("没有结论", out2["digest"])

    def test_non_md_format_rejected_at_both_layers(self):
        """注册表校验器拦 ValidationError（路由层降级 clarify）；handler 直调也兜底。"""
        with self.assertRaises(ValidationError):
            self.registry.validate("export_card", {"id": 1, "format": "pdf"})
        did = self.make_dossier()
        out = self.call("export_card", id=did, format="pdf")
        self.assertFalse(out["ok"])
        self.assertIn("md", out["digest"])


class TestConsistencyWithStore(ToolsMemoryTestBase):
    def test_get_matches_store_direct_read(self):
        """工具读数与 store 直读逐字段一致（工具不是第二个事实源）。"""
        did = self.make_dossier()
        out = self.call("get_dossier", id=did)
        store = self.service.store
        latest = store.conclusions(did)[-1]
        self.assertEqual(out["conclusion"]["label"], latest["label"])
        self.assertEqual(out["conclusion"]["as_of"], latest["as_of"])
        self.assertEqual(out["chapters"]["evidence"]["total"], len(store.evidence(did)))
        row = store.get_dossier(did)
        self.assertEqual(out["title"], row["title"])
        self.assertEqual(out["input_text"], row["input_text"])

    def test_list_order_matches_store(self):
        """list 结果与 store.list_dossiers 同序同集（updated_at 倒序）。"""
        ids = [self.make_dossier(f"档案{i}") for i in range(3)]
        out = self.call("list_dossiers")
        self.assertEqual([it["id"] for it in out["items"]],
                         [r["id"] for r in self.service.store.list_dossiers(limit=50)])
        self.assertEqual(sorted(it["id"] for it in out["items"]), sorted(ids))


if __name__ == "__main__":
    unittest.main()
