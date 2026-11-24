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
