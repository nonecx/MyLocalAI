# -*- coding: utf-8 -*-
"""平台只读工具测试（P7 + v12 巡检报告视图）：platform_feed 的参数封闭枚举、
四视图（预警/信息流/巡检记录/巡检报告）、降级、接线。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import router as router_mod  # noqa: E402
from mylocalai.assistant import tools_platform as tp  # noqa: E402
from mylocalai.assistant.loop import AssistantLoop  # noqa: E402
from mylocalai.assistant.registry import ToolRegistry, ValidationError  # noqa: E402
from mylocalai.assistant.session_store import SessionStore  # noqa: E402
from mylocalai.assistant.events import EventStream  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402
from mylocalai.platform.store import PlatformStore  # noqa: E402


def _pkg_json(name: str):
    path = Path(__file__).resolve().parent.parent / "src" / "mylocalai" / "assistant" / name
    return json.loads(path.read_text(encoding="utf-8"))


class TestKindParam(unittest.TestCase):
    def test_closed_enum(self):
        self.assertEqual(tp.kind_param("warnings"), "warnings")
        self.assertEqual(tp.kind_param("FEED"), "feed")
        self.assertEqual(tp.kind_param(" scans "), "scans")
        self.assertEqual(tp.kind_param("REPORT"), "report")
        self.assertEqual(tp.KINDS, ("warnings", "feed", "scans", "report"))
        for bad in (None, 42, "", "alerts"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                tp.kind_param(bad)


class TestPlatformFeedTool(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PlatformStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)
        tp.set_store(self.store)
        self.addCleanup(tp.set_store, None)

    def _seed(self):
        qid = self.store.add_watch_query("话题A")
        e1 = self.store.add_feed_entry(query_id=qid, title="自来水通报",
                                       url="https://news.example.com/1",
                                       e_score=44.2, status="verified")
        self.store.update_feed_entry(e1, risk_level="high",
                                     verdict_label="疑似谣言", risk_score=62.0,
                                     risk_reasons=["热度 E 44/100", "权威信源 1 个"])
        self.store.add_feed_entry(query_id=qid, title="无关简讯",
                                  url="https://news.example.com/2", e_score=12.0)
        sid = self.store.start_scan_run()
        self.store.finish_scan_run(sid, "completed", queries=1, candidates=2, verified=1)

    def test_warnings_view(self):
        self._seed()
        out = tp._platform_feed(None, "warnings")
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        card = out["card"]
        self.assertEqual(card["card_type"], "platform")
        self.assertEqual(card["kind"], "warnings")
        self.assertEqual(len(card["warnings"]), 1)
        entry = card["warnings"][0]
        self.assertEqual(entry["risk_label"], "高危")
        self.assertEqual(entry["verdict_label"], "疑似谣言")
        self.assertTrue(entry["risk_reasons"])
        self.assertIn("预警", out["digest"])

    def test_feed_view(self):
        self._seed()
        out = tp._platform_feed(None, "feed")
        self.assertEqual(len(out["card"]["feed"]), 2)

    def test_scans_view(self):
        self._seed()
        out = tp._platform_feed(None, "scans")
        self.assertEqual(len(out["card"]["scans"]), 1)
        self.assertEqual(out["card"]["scans"][0]["status"], "completed")

    def _seed_report(self, llm_status="ok"):
        sid = self.store.start_scan_run()
        self.store.finish_scan_run(sid, "completed", queries=1, candidates=2,
                                   verified=1)
        self.store.save_report(
            sid, title="第 1 轮巡检报告", llm_status=llm_status, status="completed",
            payload={"summary": "本轮巡检覆盖 1 个监控话题。",
                     "counts": {"queries": 1, "candidates": 2, "verified": 1,
                                "risk": {"notice": 1, "suspect": 0, "high": 1,
                                         "urgent": 0}},
                     "topic_notes": [{"query": "话题A", "note": "热度上升"}],
                     "analysis": [{"point": "要点一", "entry_ids": [1, 2]}],
                     "risks": "存在传播风险",
                     "followups": ["继续观察"],
                     "gaps": ["本轮有 1 次降级"]})
        return sid

    def test_report_view_shape(self):
        """report 视图：卡形状 + 视图字段（scan_id/title/llm_status/counts/
        topic_notes/analysis/risks/followups/gaps）+ digest 带口径行。"""
        sid = self._seed_report()
        out = tp._platform_feed(None, "report")
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        card = out["card"]
        self.assertEqual(card["card_type"], "platform")
        self.assertEqual(card["kind"], "report")
        view = card["report"]
        self.assertIsInstance(view, dict)
        self.assertEqual(view["scan_id"], sid)
        self.assertEqual(view["title"], "第 1 轮巡检报告")
        self.assertEqual(view["llm_status"], "ok")
        self.assertEqual(view["status"], "completed")
        self.assertTrue(view["as_of"])               # = 报告行 created_at
        self.assertEqual(view["counts"]["candidates"], 2)
        self.assertEqual(view["counts"]["risk"]["high"], 1)
        self.assertEqual(view["topic_notes"],
                         [{"query": "话题A", "note": "热度上升"}])
        self.assertEqual(view["analysis"][0]["entry_ids"], [1, 2])
        self.assertEqual(view["risks"], "存在传播风险")
        self.assertEqual(view["followups"], ["继续观察"])
        self.assertEqual(view["gaps"], ["本轮有 1 次降级"])
        self.assertIn("第 1 轮巡检报告", out["digest"])
        self.assertIn("传播风险分级不判定真假", out["digest"])
        self.assertIn("巡检报告不做真假判断", out["digest"])

    def test_report_view_non_ok_reason_in_digest(self):
        """llm_status 非 ok：digest 如实带中文原因（不演成"已生成"）。"""
        self._seed_report(llm_status="unavailable")
        out = tp._platform_feed(None, "report")
        self.assertEqual(out["card"]["report"]["llm_status"], "unavailable")
        self.assertIn("模型不可用", out["digest"])

    def test_report_view_empty_store_is_legal(self):
        """空库是合法输出：空卡（report=[]）+ 如实摘要，不是降级。"""
        out = tp._platform_feed(None, "report")
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["card"]["report"], [])
        self.assertIn("暂无巡检报告", out["digest"])

    def test_report_view_broken_store_degrades(self):
        class Boom:
            def latest_report(self):
                raise RuntimeError("db gone")

        tp.set_store(Boom())
        out = tp._platform_feed(None, "report")
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIn("db gone", out["digest"] or "")
        self.assertIsNone(out["card"])

    def test_report_view_bad_payload_shape_is_tolerated(self):
        """坏 payload（非对象/缺键）不炸：视图按缺键兜底，digest 照常出。"""
        sid = self.store.start_scan_run()
        self.store.save_report(sid, title="坏形状报告", llm_status="ok",
                               status="completed", payload=None)
        out = tp._platform_feed(None, "report")
        self.assertTrue(out["ok"])
        view = out["card"]["report"]
        self.assertEqual(view["title"], "坏形状报告")
        self.assertEqual(view["counts"], {})
        self.assertEqual(view["analysis"], [])
        self.assertEqual(view["gaps"], [])

    def test_empty_store_is_legal(self):
        """空库是合法输出：ok=True + 空卡，不是降级。"""
        out = tp._platform_feed(None, "warnings")
        self.assertTrue(out["ok"])
        self.assertEqual(out["card"]["warnings"], [])
        self.assertIn("暂无预警", out["digest"])

    def test_broken_store_degrades(self):
        class Boom:
            def warnings(self, limit=20):
                raise RuntimeError("db gone")

        tp.set_store(Boom())
        out = tp._platform_feed(None, "warnings")
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIn("db gone", out["digest"] or "")
        self.assertIsNone(out["card"])

    def test_registry_metadata(self):
        # 主线 loop 尚未注册 tools_platform（接线留待后续分支），这里自建
        # registry 直测注册契约：permission/cost/est_s 与只读零成本口径。
        reg = ToolRegistry()
        tp.register_all(reg, None)
        spec = reg.get("platform_feed")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["permission"], "read")
        self.assertEqual(spec["cost"], "zero")
        self.assertEqual(spec["est_s"], 0.0)


class TestPlatformToolWiring(unittest.TestCase):
    """独立注册契约（主线未接线版）。

    原用例断言"枚举 / allowlist / 注册名 / router_system 四方一致 + loop 路由
    正例"；主线 router 枚举、assistant_rules allowlist 与 loop 注册表尚未纳入
    platform_feed（接线留待后续分支），四方一致与 loop 路由无对象可测，已移除。
    保留等价直测：自建 registry 注册后的参数校验器与工具名一致性。
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PlatformStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)
        tp.set_store(self.store)
        self.addCleanup(tp.set_store, None)
        self.reg = ToolRegistry()
        tp.register_all(self.reg, None)

    def test_register_name_and_validators(self):
        spec = self.reg.get("platform_feed")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["params"], ["kind"])
        # validators 与 kind_param 同一可调用：坏值在注册层就被挡
        self.assertIs(spec["validators"]["kind"], tp.kind_param)

    def test_handler_dispatch_with_registered_validator(self):
        # validators（kind_param）归一化后的合法值直接驱动三视图；
        # 归一化本身由 TestKindParam 覆盖，这里只验注册表与 handler 对得上。
        out = tp._platform_feed(None, tp.kind_param("FEED"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["card"]["kind"], "feed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
