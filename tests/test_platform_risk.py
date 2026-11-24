# -*- coding: utf-8 -*-
"""传播风险分级测试（P3）：阈值矩阵、四因素、保底规则、**变异测试**。

变异测试的口径（与 test_conclusion_card 同标准）：
  - 篡改任一结构化输入必须改变输出或直接被拒；
  - 把大模型文本/坏类型塞进任何输入通道必须抛异常（分级链只吃确定性数字与
    封闭标签集，LLM 没有写入通道）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.platform.risk import (LEVELS, LEVEL_LABELS,  # noqa: E402
                                     assess_spread_risk, risk_rank)

#: 默认规则（weights: heat .4 / growth .15 / breadth .2 / rumor .25）
RULES = {"risk": {
    "weights": {"heat": 0.4, "growth": 0.15, "breadth": 0.2, "rumor": 0.25},
    "breadth_full_sources": 10,
    "levels": {"suspect": 35, "high": 60, "urgent": 80},
    "floors": [
        {"min_rumor_score": 60, "min_breadth": 50, "level": "high"},
        {"min_growth": 60, "min_heat": 50, "level": "suspect"},
    ],
}}


def _risk(heat=0.0, growth=0.0, breadth=None, rumor=None, rules=RULES):
    return assess_spread_risk(heat, growth, breadth, rumor, rules)


class TestLevelMatrix(unittest.TestCase):
    def test_boundaries_exact(self):
        """score = 0.4·heat：87.5→35.0 恰进 suspect；87.4→34.96 notice。"""
        self.assertEqual(_risk(heat=87.5)["level"], "suspect")
        self.assertEqual(_risk(heat=87.4)["level"], "notice")

    def test_high_and_urgent_boundaries(self):
        # heat 100(40) + rumor 80(20) = 60 → high；rumor 100(25)+breadth 满(20)+heat 100(40)=85 → urgent
        self.assertEqual(_risk(heat=100, rumor={"label": "存疑待核实", "risk_score": 80})["level"],
                         "high")
        self.assertEqual(_risk(heat=100, breadth={"total": 10},
                               rumor={"label": "高风险谣言", "risk_score": 100})["level"],
                         "urgent")

    def test_four_factors_weights(self):
        out = _risk(heat=50, growth=0.5, breadth={"total": 5},
                    rumor={"label": "疑似谣言", "risk_score": 40})
        # 0.4·50 + 0.15·50 + 0.2·50 + 0.25·40 = 20 + 7.5 + 10 + 10 = 47.5
        self.assertAlmostEqual(out["score"], 47.5, places=1)
        self.assertEqual(out["level"], "suspect")

    def test_all_zero_is_notice(self):
        out = _risk()
        self.assertEqual(out["level"], "notice")
        self.assertEqual(out["score"], 0.0)

    def test_none_inputs_are_legal(self):
        out = _risk(heat=None, growth=None, breadth=None, rumor=None)
        self.assertEqual(out["level"], "notice")
        self.assertTrue(any("未深查" in r for r in out["reasons"]))


class TestFloors(unittest.TestCase):
    def test_floor_raises_level(self):
        """rumor 60 + breadth 满值：score=0.25·60+0.2·100=35 → 阈值只到 suspect，
        保底规则抬到 high。"""
        out = _risk(rumor={"label": "疑似谣言", "risk_score": 60},
                    breadth={"total": 10})
        self.assertEqual(out["level"], "high")
        self.assertTrue(any("保底规则" in r for r in out["reasons"]))

    def test_floor_partial_conditions_do_not_trigger(self):
        # rumor 60(15 分) + breadth 2 篇(4 分) = 19：保底条件的 breadth 不满足，
        # 档位停在阈值结果（notice），不会被保底规则抬到 high。
        out = _risk(rumor={"label": "疑似谣言", "risk_score": 60},
                    breadth={"total": 2})
        self.assertEqual(out["level"], "notice")

    def test_floor_never_downgrades(self):
        out = _risk(heat=100, breadth={"total": 10},
                    rumor={"label": "高风险谣言", "risk_score": 100})
        self.assertEqual(out["level"], "urgent")   # 已 urgent，high 保底不降级


class TestMutationGuards(unittest.TestCase):
    """变异测试：坏类型/越界/LLM 文本一律抛，不猜、不纠正。"""

    def test_heat_bad_types_raise(self):
        for bad in ("80", [80], {"e": 80}, True):
            with self.subTest(heat=bad), self.assertRaises((TypeError, ValueError)):
                _risk(heat=bad)

    def test_heat_out_of_range_raises(self):
        for bad in (-1, 101, float("nan"), float("inf")):
            with self.subTest(heat=bad), self.assertRaises(ValueError):
                _risk(heat=bad)

    def test_growth_out_of_range_raises(self):
        for bad in (-0.1, 1.5, "50%"):
            with self.subTest(growth=bad), self.assertRaises((TypeError, ValueError)):
                _risk(growth=bad)

    def test_breadth_bad_shapes_raise(self):
        for bad in (5, "many", {"total": -1}, {"total": "5"}, {"total": 1.5},
                    {"total": True}):
            with self.subTest(breadth=bad), self.assertRaises((TypeError, ValueError)):
                _risk(breadth=bad)

    def test_llm_text_cannot_enter_label_channel(self):
        """大模型生成的判断文本不是合法 label——封闭集外一律拒。"""
        for bad_label in ("这是假的", "high", "", None, 42):
            with self.subTest(label=bad_label), self.assertRaises(ValueError):
                _risk(rumor={"label": bad_label, "risk_score": 50})

    def test_llm_text_cannot_enter_score_channel(self):
        for bad_score in ("high", "62 分", None, [62]):
            with self.subTest(score=bad_score), self.assertRaises((TypeError, ValueError)):
                _risk(rumor={"label": "疑似谣言", "risk_score": bad_score})

    def test_determinism(self):
        args = (66.0, 0.25, {"total": 7, "official": 2},
                {"label": "疑似谣言", "risk_score": 55})
        self.assertEqual(assess_spread_risk(*args, RULES),
                         assess_spread_risk(*args, RULES))

    def test_score_always_in_range(self):
        for heat in (0, 33.3, 100):
            for rumor_score in (0, 55, 100):
                out = _risk(heat=heat,
                            rumor={"label": "低风险", "risk_score": rumor_score})
                self.assertGreaterEqual(out["score"], 0.0)
                self.assertLessEqual(out["score"], 100.0)
                self.assertIn(out["level"], LEVELS)


class TestReasonsAndHelpers(unittest.TestCase):
    def test_reasons_are_strings_and_cover_factors(self):
        out = _risk(heat=70, growth=0.3, breadth={"total": 6, "official": 1},
                    rumor={"label": "疑似谣言", "risk_score": 62})
        self.assertTrue(out["reasons"])
        self.assertTrue(all(isinstance(r, str) and r for r in out["reasons"]))
        joined = " ".join(out["reasons"])
        self.assertIn("热度 E", joined)
        self.assertIn("环比增幅", joined)
        self.assertIn("独立信源", joined)
        self.assertIn("核查结论：疑似谣言", joined)

    def test_risk_rank_order(self):
        ranks = [risk_rank(x) for x in LEVELS]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(risk_rank("bogus"), -1)

    def test_level_labels_cover_all(self):
        self.assertEqual(set(LEVEL_LABELS), set(LEVELS))

    def test_custom_rules_honored(self):
        rules = {"risk": {"weights": {"heat": 1.0, "growth": 0.0, "breadth": 0.0,
                                      "rumor": 0.0},
                          "breadth_full_sources": 5,
                          "levels": {"suspect": 10, "high": 20, "urgent": 30},
                          "floors": []}}
        self.assertEqual(_risk(heat=25, rules=rules)["level"], "high")

    def test_rules_none_uses_defaults(self):
        out = assess_spread_risk(90.0, 0.0, None, None, None)
        self.assertEqual(out["level"], "suspect")     # 0.4·90=36 ≥ 35


if __name__ == "__main__":
    unittest.main(verbosity=2)
