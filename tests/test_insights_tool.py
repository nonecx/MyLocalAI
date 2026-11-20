# -*- coding: utf-8 -*-
"""insights 工具测试（L4 参谋只读工具）：注册口径 / kind 枚举 / 三类卡片形状 / 降级 / 零 LLM。

全离线：临时 SQLite（DossierStore + DossierService）、零网络、零 LLM。
"不调用大模型"用例把 llm_client 的 chat_json/chat_text 打成"一被调用就失败"的桩，
反向锁定 insights 全程不碰模型（aggregations 全部来自 dossier.insights 的纯函数）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mylocalai.assistant as assistant_pkg  # noqa: E402
from mylocalai.assistant import router as router_mod  # noqa: E402
from mylocalai.assistant import tools_insights  # noqa: E402
from mylocalai.assistant.loop import _CARD_TYPE, AssistantLoop  # noqa: E402
from mylocalai.assistant.registry import ToolRegistry, ValidationError  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402

ASSISTANT_DIR = Path(assistant_pkg.__file__).resolve().parent


def load_json(name: str) -> dict:
    return json.loads((ASSISTANT_DIR / name).read_text(encoding="utf-8"))


class InsightsToolTestBase(unittest.TestCase):
    """公共夹具：独立临时库 + 已注册 insights 的最小注册表 + ctx 构造。"""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DossierStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)
        self.service = DossierService(self.store)
        self.registry = ToolRegistry()
        tools_insights.register_all(self.registry)

    def ctx(self, dossiers=None) -> SimpleNamespace:
        """handler 的 ctx 契约（与 loop 里 SimpleNamespace(...) 同形，只填本工具用到的键）。"""
        return SimpleNamespace(sessions=None, dossiers=dossiers or self.service,
                               events=None, cancel_check=None, services={})

    def run_insights(self, kind: str, dossiers=None):
        """经注册表校验后执行 handler（与 loop 的执行路径一致：validate → handler）。"""
        clean = self.registry.validate("insights", {"kind": kind})
        return self.registry.get("insights")["handler"](self.ctx(dossiers), **clean)

    # ---- 预置数据（走 store 公开 API，与生产管线同一形状） ----

    def seed_dossier(self, title: str, label: str, risk: float, domain: str,
                     stance: str = "反驳") -> int:
        did = self.store.create_dossier(title, title + "的原文", input_kind="text")
        self.store.add_conclusion(did, label, risk, as_of="2026-10-31T10:00:00+08:00")
        self.store.add_evidence(did, 1, [{"url": f"https://{domain}/a", "stance": stance,
                                          "domain_tier": "官方权威",
                                          "fetched_at": "2026-10-31T09:00:00+08:00"}])
        self.store.record_markers(did, {
            "risk_hits": {"source_vague": {"label": "来源模糊", "hits": ["传闻"], "points": 6}},
            "credibility_hits": {},
            "marker_points": 6,
            "credibility_points": 0,
        })
        return did


# ===========================================================================
# 1. 注册口径与接线（read / zero / est_s=0 / 默认注册表 / allowlist / 路由枚举）
# ===========================================================================
class TestInsightsRegistration(InsightsToolTestBase):
    def test_registered_as_read_only_zero_cost(self):
        """只读零 LLM：permission=read、cost=zero、est_s=0.0、params=[kind]。"""
        spec = self.registry.get("insights")
        self.assertIsNotNone(spec, "insights 未注册")
        self.assertEqual(spec["permission"], "read")
        self.assertEqual(spec["cost"], "zero")
        self.assertEqual(spec["est_s"], 0.0)
        self.assertEqual(spec["params"], ["kind"])
        self.assertTrue(str(spec["description"]).strip(), "description 不能为空")

    def test_default_loop_registers_insights_and_card_type(self):
        """AssistantLoop 的缺省注册表含 insights，且 _CARD_TYPE 映射到 insights 卡。"""
        loop = AssistantLoop()
        self.assertIn("insights", loop.registry.names())
        self.assertEqual(_CARD_TYPE.get("insights"), "insights")

    def test_allowlist_router_enum_and_router_prompt_mention_insights(self):
        """三处接线同源：allowlist / TOOL_ACTIONS / router_system 提示词都含 insights。"""
        rules = load_json("assistant_rules.json")
        prompts = load_json("prompts.json")
        self.assertIn("insights", rules["tool_allowlist"])
        self.assertIn("insights", router_mod.TOOL_ACTIONS)
        system = str(prompts["router_system"])
        self.assertIn("insights", system)
        for kind in tools_insights.KINDS:
            self.assertIn(f"kind={kind}", system, "router_system 应写明 kind 封闭枚举")


# ===========================================================================
# 2. kind 封闭枚举（越界/缺失/坏类型一律 ValidationError，合法值规整后放行）
# ===========================================================================
class TestInsightsKindEnum(InsightsToolTestBase):
    def test_valid_kinds_pass_and_are_normalized(self):
        for raw, expected in (("patterns", "patterns"), ("SOURCES", "sources"),
                              (" weekly ", "weekly")):
            self.assertEqual(self.registry.validate("insights", {"kind": raw}),
                             {"kind": expected})

    def test_invalid_kinds_raise_validation_error(self):
        for bad in (None, "", "dossier", "all", 1, ["patterns"]):
            with self.assertRaises(ValidationError, msg=f"kind={bad!r} 应被拒绝"):
                self.registry.validate("insights", {"kind": bad})

    def test_missing_kind_raises(self):
        with self.assertRaises(ValidationError):
            self.registry.validate("insights", {})


# ===========================================================================
# 3. 三类卡片的形状与内容（card_type=insights + kind + as_of + 各自数据键）
# ===========================================================================
class TestInsightsCards(InsightsToolTestBase):
    def setUp(self) -> None:
        super().setUp()
        # 两个高风险档案、同一域名反驳 → sources 里出现 2 次截图战绩
        self.did1 = self.seed_dossier("传闻一", "高风险谣言", 80.0, "bad.example.com")
        self.did2 = self.seed_dossier("传闻二", "疑似谣言", 62.0, "bad.example.com")

    def assert_common(self, out, kind: str, payload_key: str) -> None:
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["degraded"])
        self.assertEqual(out["cost"], {"requests": 0, "tokens": 0})
        self.assertIsNone(out["dossier_id"], "只读工具不得建档")
        self.assertFalse(out["not_saved"], "只读聚合没有『未保存』的失败语义")
        card = out["card"]
        self.assertEqual(card["card_type"], "insights")
        self.assertEqual(card["kind"], kind)
        self.assertTrue(card["as_of"])
        self.assertIn(payload_key, card)
        # 每条 kind 只带自己的数据键，不混入另外两种
        for other in ("patterns", "profiles", "digest"):
            if other != payload_key:
                self.assertNotIn(other, card)
        return card

    def test_patterns_card_shape_and_values(self):
        out = self.run_insights("patterns")
        card = self.assert_common(out, "patterns", "patterns")
        patterns = card["patterns"]
        self.assertEqual(patterns["total_dossiers"], 2)
        self.assertEqual(patterns["with_markers"], 2)
        self.assertEqual([r["key"] for r in patterns["risk_patterns"]], ["source_vague"])
        self.assertEqual(patterns["risk_patterns"][0]["label"], "来源模糊")
        self.assertIn("话术模式库", out["digest"])
        self.assertIn("高风险谣言", out["digest"])

    def test_sources_card_shape_and_values(self):
        out = self.run_insights("sources")
        card = self.assert_common(out, "sources", "profiles")
        profiles = card["profiles"]
        self.assertEqual([p["domain"] for p in profiles], ["bad.example.com"])
        self.assertEqual(profiles[0]["refuted_dossiers"], 2)
        self.assertTrue(profiles[0]["qualified"])
        # 完整数据只进卡片：卡里能看到域名，叙述 digest 里绝不能出现
        self.assertIn("bad.example.com", json.dumps(card, ensure_ascii=False))
        self.assertNotIn("bad.example.com", out["digest"])
        self.assertIn("信源画像", out["digest"])

    def test_weekly_card_shape(self):
        out = self.run_insights("weekly")
        card = self.assert_common(out, "weekly", "digest")
        digest = card["digest"]
        self.assertIn("period", digest)
        self.assertIn("dossiers", digest)
        self.assertIn("as_of", digest)
        self.assertIn("本周简报", out["digest"])
        # 周报计数是数字而不是域名清单；域名同样不进叙述层
        self.assertNotIn("bad.example.com", out["digest"])

    def test_runs_do_not_create_dossiers(self):
        """只读：跑完三种 kind 之后档案库条目不增、无任何写库痕迹。"""
        before = len(self.store.list_dossiers(limit=100))
        for kind in tools_insights.KINDS:
            self.run_insights(kind)
        self.assertEqual(len(self.store.list_dossiers(limit=100)), before)


# ===========================================================================
# 4. 空库与降级（空库是合法输出；异常一律 ok=False + degraded=True，绝不抛）
# ===========================================================================
class TestInsightsDegrade(InsightsToolTestBase):
    def test_empty_store_returns_legal_zero_structures(self):
        """空库：三类都出合法空结构，ok=True（"暂无数据"不是错误）。"""
        patterns = self.run_insights("patterns")
        self.assertTrue(patterns["ok"])
        self.assertEqual(patterns["card"]["patterns"]["total_dossiers"], 0)
        self.assertEqual(patterns["card"]["patterns"]["risk_patterns"], [])
        self.assertIn("暂无", patterns["digest"])

        sources = self.run_insights("sources")
        self.assertTrue(sources["ok"])
        self.assertEqual(sources["card"]["profiles"], [])
        self.assertIn("暂无", sources["digest"])

        weekly = self.run_insights("weekly")
        self.assertTrue(weekly["ok"])
        self.assertEqual(weekly["card"]["digest"]["dossiers"]["new_count"], 0)

    def test_aggregation_exception_degrades_without_raising(self):
        """聚合函数抛异常 → ok=False、degraded=True、digest 如实说明，不向上抛。"""
        boom = mock.Mock(side_effect=RuntimeError("库里没有这张表"))
        with mock.patch("mylocalai.dossier.insights.pattern_library", boom):
            out = self.run_insights("patterns")
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIsNone(out["card"])
        self.assertIn("暂时不可用", out["digest"])
        self.assertIn("库里没有这张表", out["digest"])
        self.assertEqual(out["cost"], {"requests": 0, "tokens": 0})

        with mock.patch("mylocalai.dossier.insights.weekly_digest", boom):
            self.assertTrue(self.run_insights("weekly")["degraded"])

    def test_missing_store_degrades(self):
        """档案库未装配（ctx.dossiers 缺 store）→ 降级而不是 AttributeError 打出回合。"""
        for kind in tools_insights.KINDS:
            out = self.run_insights(kind, dossiers=SimpleNamespace())
            self.assertFalse(out["ok"], kind)
            self.assertTrue(out["degraded"], kind)
            self.assertIn("档案库不可用", out["digest"], kind)


# ===========================================================================
# 5. 零 LLM（把模型客户端打成"调用即失败"，三种 kind 都照常返回）
# ===========================================================================
class TestInsightsNeverCallsLlm(InsightsToolTestBase):
    def test_no_llm_calls(self):
        self.seed_dossier("传闻一", "高风险谣言", 80.0, "bad.example.com")
        chat_json = mock.Mock(side_effect=AssertionError("insights 不得调用 chat_json"))
        chat_text = mock.Mock(side_effect=AssertionError("insights 不得调用 chat_text"))
        with mock.patch("mylocalai.llm.llm_client.chat_json", chat_json), \
                mock.patch("mylocalai.llm.llm_client.chat_text", chat_text):
            for kind in tools_insights.KINDS:
                out = self.run_insights(kind)
                self.assertTrue(out["ok"], out)
        self.assertEqual(chat_json.call_count, 0)
        self.assertEqual(chat_text.call_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
