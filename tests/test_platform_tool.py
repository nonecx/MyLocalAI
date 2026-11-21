# -*- coding: utf-8 -*-
"""平台只读工具测试（P7）：platform_feed 的参数封闭枚举、三视图、降级、接线。"""

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
from mylocalai.assistant.registry import ValidationError  # noqa: E402
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
        reg = AssistantLoop().registry
        spec = reg.get("platform_feed")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["permission"], "read")
        self.assertEqual(spec["cost"], "zero")
        self.assertEqual(spec["est_s"], 0.0)


class TestPlatformToolWiring(unittest.TestCase):
    """枚举 / allowlist / 注册名 / router_system 四方一致 + loop 路由正例。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "app.db"
        self.sessions = SessionStore(db)
        self.addCleanup(self.sessions.close)
        self.dossiers = DossierService(DossierStore(db))
        self.addCleanup(self.dossiers.close)
        self.platform_store = PlatformStore(db)
        self.addCleanup(self.platform_store.close)
        tp.set_store(self.platform_store)
        self.addCleanup(tp.set_store, None)
        self.rules = _pkg_json("assistant_rules.json")
        self.prompts = _pkg_json("prompts.json")
        self.session_id = self.sessions.create_session("平台工具测试")

    def test_four_way_consistency(self):
        reg = AssistantLoop().registry
        name = "platform_feed"
        self.assertIsNotNone(reg.get(name))
        self.assertIn(name, self.rules["tool_allowlist"])
        self.assertIn(name, router_mod.TOOL_ACTIONS)
        self.assertIn(name, self.prompts["router_system"],
                      "router_system 说明缺 platform_feed")

    def test_routes_with_kind(self):
        reg = AssistantLoop().registry
        obj = {"action": "platform_feed", "kind": "warnings"}
        r = router_mod.route("最近有什么预警", reg,
                             lambda s, u, o=obj: (o, ""),
                             self.rules, self.prompts)
        self.assertEqual(r["action"], "platform_feed")
        self.assertEqual(r["args"], {"kind": "warnings"})
        # 枚举外 action 仍 clarify（封闭枚举不被新工具松动）
        r_bad = router_mod.route("u", reg,
                                 lambda s, u: ({"action": "platform_alerts"}, ""),
                                 self.rules, self.prompts)
        self.assertEqual(r_bad["action"], "clarify")

    def test_through_loop_emits_card(self):
        qid = self.platform_store.add_watch_query("话题A")
        e1 = self.platform_store.add_feed_entry(query_id=qid, title="自来水通报",
                                                url="https://news.example.com/1",
                                                e_score=44.2, status="verified")
        self.platform_store.update_feed_entry(e1, risk_level="urgent",
                                              verdict_label="高风险谣言",
                                              risk_score=88.0,
                                              risk_reasons=["热度 E 44/100"])

        def route_llm(system, user):
            return ({"action": "platform_feed", "kind": "warnings"}, "")

        def narrate_llm(system, user):
            self.assertIn("[本轮工具结果]", user)
            return ("平台上有 1 条紧急预警，传播风险分级不代表真假判定。", "")

        loop = AssistantLoop(sessions=self.sessions, dossiers=self.dossiers,
                             services={"route_llm": route_llm,
                                       "narrate_llm": narrate_llm},
                             rules=self.rules, prompts=self.prompts)
        events = EventStream()
        out = loop.handle(self.session_id, "最近有什么谣言预警", events, None)
        self.assertEqual(out["action"], "platform_feed")
        card_events = [e for e in events.snapshot()
                       if e.get("event") == "card" or e.get("type") == "card"]
        payloads = json.dumps(events.snapshot(), ensure_ascii=False)
        self.assertIn('"card_type": "platform"', payloads)
        self.assertIn("紧急", payloads)
        self.assertTrue(card_events or '"platform"' in payloads)


if __name__ == "__main__":
    unittest.main(verbosity=2)
