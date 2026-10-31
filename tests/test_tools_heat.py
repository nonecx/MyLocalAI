# -*- coding: utf-8 -*-
"""热度类工具测试（工作指导 §5.2/§7.2）：compute_heat 对拍 attention 模块、check_heat 落快照。

锁定（抓真实缺陷的标准）：
  - compute_heat 的公式值与 attention 模块逐点一致（含定标点 50.3415 / 91.9385），
    零 API、不建档——两处口径分叉时这里红；
  - 参数校验：负数 / bool（int 子类必须显式排除）/ 非有限数全部拒绝；
  - check_heat：stub 热度服务 → 话题建档 + 快照逐字段落库，degraded 如实透传；
  - 服务返回坏形状（None）→ 降级输出而非崩溃（降级文化）。
全离线：heat 服务注入 stub；compute_heat 用真实 attention 纯函数（只用 math，零网络）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.loop import AssistantLoop, _real_service  # noqa: E402
from mylocalai.assistant.registry import ValidationError  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402
from mylocalai.attention import attention as attn  # noqa: E402


def stub_heat_service(detail):
    calls = []

    def _svc(topic):
        calls.append(topic)
        if isinstance(detail, Exception):
            raise detail
        return detail

    _svc.calls = calls
    return _svc


class ToolsHeatTestBase(unittest.TestCase):
    def setUp(self):
        self.service = DossierService(DossierStore(":memory:"))
        self.addCleanup(self.service.close)
        self.registry = AssistantLoop().registry

    def ctx(self, services):
        return SimpleNamespace(sessions=None, dossiers=self.service, events=None,
                               cancel_check=None, services=services)

    def call(self, name, services, **args):
        spec = self.registry.get(name)
        assert spec is not None, name
        return spec["handler"](self.ctx(services), **args)


class TestComputeHeat(ToolsHeatTestBase):
    def test_matches_attention_module(self):
        """compute_heat 与 attention 模块逐点一致（含定标点），两处口径不许分叉。"""
        real_formula = _real_service("heat_formula", {})
        points = [(0, 0, 0),                      # → ≈7.0e-05（严格 > 0）
                  (100, 100000, 1000),            # 定标点 ≈ 50.3415
                  (1e12, 1e12, 1e12),             # 定标点 ≈ 91.9385（严格 < 100）
                  (5, 100, 10), (1, 1, 1)]
        for x, y, z in points:
            with self.subTest(point=(x, y, z)):
                out = self.call("compute_heat", {"heat_formula": real_formula}, x=x, y=y, z=z)
                self.assertTrue(out["ok"])
                self.assertAlmostEqual(out["score"], attn.attention_score(x, y, z), places=12)
                self.assertAlmostEqual(out["card"]["score"],
                                       attn.score_detail(x, y, z)["score"], places=12)

    def test_calibration_points(self):
        """定标点回归：E(100,1e5,1e3)≈50.3415；E(1e12,1e12,1e12)≈91.9385。"""
        real_formula = _real_service("heat_formula", {})
        out = self.call("compute_heat", {"heat_formula": real_formula},
                        x=100, y=100000, z=1000)
        self.assertAlmostEqual(out["score"], 50.3415, places=3)
        out2 = self.call("compute_heat", {"heat_formula": real_formula},
                         x=1e12, y=1e12, z=1e12)
        self.assertAlmostEqual(out2["score"], 91.9385, places=3)

    def test_zero_api_no_dossier(self):
        """纯公式零 API：cost 全零、不建档（秒答车道）。"""
        real_formula = _real_service("heat_formula", {})
        out = self.call("compute_heat", {"heat_formula": real_formula}, x=1, y=2, z=3)
        self.assertEqual(out["cost"], {"requests": 0, "tokens": 0})
        self.assertIsNone(out["dossier_id"])
        self.assertEqual(self.service.store.list_dossiers(limit=50), [])

    def test_card_shape(self):
        """热度卡字段齐全：x/y/z/score/band/profile/proxy_flags/degraded/as_of。"""
        real_formula = _real_service("heat_formula", {})
        out = self.call("compute_heat", {"heat_formula": real_formula}, x=0, y=0, z=0)
        card = out["card"]
        for key in ("x", "y", "z", "score", "band", "profile", "proxy_flags", "degraded", "as_of"):
            self.assertIn(key, card)
        self.assertEqual(card["profile"], "spec")
        self.assertEqual(card["x"], 0.0)
        self.assertTrue(card["as_of"])
        self.assertIn("E =", out["digest"])
        self.assertIn("大模型不参与", out["digest"])

    def test_param_validation_rejects_bad_inputs(self):
        """负数 / bool（int 子类）/ 非有限数在 registry 校验层就拒绝（路由层降级 clarify）。"""
        for bad_x in (-1, True, False, float("nan"), float("inf"), "5", None):
            with self.subTest(x=bad_x):
                with self.assertRaises(ValidationError):
                    self.registry.validate("compute_heat", {"x": bad_x, "y": 0, "z": 0})
        # 合法输入（int/float 非负）放行
        clean = self.registry.validate("compute_heat", {"x": 3, "y": 0.5, "z": 0})
        self.assertEqual(clean, {"x": 3.0, "y": 0.5, "z": 0.0})


class TestCheckHeat(ToolsHeatTestBase):
    def test_snapshot_written_field_by_field(self):
        """stub 热度服务 → 话题建档（input_kind=topic）+ 快照逐字段落库（与 store 对拍）。"""
        detail = {"x": 12, "y": 250000, "z": 800, "score": 47.2, "band": "低关注",
                  "profile": "spec", "proxy_flags": ["y_proxy"], "quality": "exact"}
        heat = stub_heat_service(detail)
        out = self.call("check_heat", {"heat": heat}, topic="某地自来水")
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(heat.calls, ["某地自来水"])
        did = out["dossier_id"]
        self.assertIsNotNone(did)
        dossier = self.service.store.get_dossier(did)
        self.assertEqual(dossier["input_kind"], "topic")
        self.assertEqual(dossier["title"], "某地自来水")
        snaps = self.service.store.snapshots(did)
        self.assertEqual(len(snaps), 1)
        self.assertEqual((snaps[0]["x"], snaps[0]["y"], snaps[0]["z"], snaps[0]["e_score"]),
                         (12.0, 250000.0, 800.0, 47.2))
        self.assertEqual(snaps[0]["profile"], "spec")
        self.assertEqual(snaps[0]["proxy_flags"], ["y_proxy"])
        # digest 带口径标注（代理值诚实展示）
        self.assertIn("y_proxy", out["digest"])
        self.assertIn("47.20", out["digest"])

    def test_degraded_passthrough(self):
        """服务报 degraded → 结果/卡片/tool_end 全链如实透传（不硬撑）。"""
        detail = {"x": 0, "y": 0, "z": 0, "score": 50.3, "band": "中关注",
                  "profile": "spec", "proxy_flags": [], "quality": "baseline",
                  "degraded": True, "note": "未联网取证，x/y/z 取参考基准值"}
        out = self.call("check_heat", {"heat": stub_heat_service(detail)}, topic="冷话题")
        self.assertTrue(out["degraded"])
        self.assertTrue(out["card"]["degraded"])
        self.assertIn("降级", out["digest"])
        self.assertIn("参考基准值", out["digest"])
        # 降级输出不虚报网络成本
        self.assertEqual(out["cost"]["requests"], 0)

    def test_card_shape_and_type(self):
        detail = {"x": 1, "y": 2, "z": 3, "score": 8.6, "band": "冷启动",
                  "profile": "spec", "proxy_flags": ["z_proxy"], "degraded": False}
        out = self.call("check_heat", {"heat": stub_heat_service(detail)}, topic="话题A")
        card = out["card"]
        for key in ("x", "y", "z", "score", "band", "profile", "proxy_flags", "degraded", "as_of"):
            self.assertIn(key, card)
        self.assertEqual(card["band"], "冷启动")
        self.assertEqual(out["band"], "冷启动")

    def test_service_returns_garbage_degrades_without_crash(self):
        """服务返回 None（LongCat 抽风同型故障）→ 降级输出，不崩、不编数。"""
        out = self.call("check_heat", {"heat": stub_heat_service(None)}, topic="话题B")
        self.assertTrue(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertEqual(out["card"]["score"], 0.0)
        self.assertIn("热度服务无输出", out["digest"])

    def test_topic_validation(self):
        """topic 必填且 ≤200 字（registry 校验层口径）。"""
        with self.assertRaises(ValidationError):
            self.registry.validate("check_heat", {"topic": ""})
        with self.assertRaises(ValidationError):
            self.registry.validate("check_heat", {"topic": "长" * 201})
        with self.assertRaises(ValidationError):
            self.registry.validate("check_heat", {"topic": None})
        clean = self.registry.validate("check_heat", {"topic": " 自来水 "})
        self.assertEqual(clean, {"topic": "自来水"})


if __name__ == "__main__":
    unittest.main()
