# -*- coding: utf-8 -*-
"""平台热度评估测试（P2）：零额外搜索的估计、降级口径、环比定义。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.platform.heat import estimate_heat, relative_change  # noqa: E402


class TestEstimateHeat(unittest.TestCase):
    def test_results_yield_positive_e(self):
        results = [
            {"url": "https://gov.example.com/1", "title": "通报",
             "snippet": "阅读量 4831.6万"},
            {"url": "https://news.example.com/2", "title": "报道",
             "snippet": "290 条评论"},
        ]
        out = estimate_heat("某话题", results)
        self.assertEqual(out["x"], 2)
        self.assertGreater(out["e"], 0.0)
        self.assertTrue(out["estimated"])               # 代理口径恒为 True
        self.assertIn(out["quality"], ("exact", "estimated"))
        self.assertEqual(out["error"], "")

    def test_empty_results_is_missing(self):
        out = estimate_heat("某话题", [])
        self.assertEqual(out["x"], 0)
        # 公式含 ε=1e-6：x/y/z 全 0 时 E≈7e-05（不是精确 0，也不是"不热"的信号）
        self.assertAlmostEqual(out["e"], 0.0, delta=0.001)
        self.assertEqual(out["quality"], "missing")
        self.assertEqual(out["error"], "")              # 空结果是合法估计，不是错误

    def test_never_raises_on_bad_results(self):
        out = estimate_heat("q", [None, 42, {"url": None}])
        self.assertEqual(out["x"], 0)
        self.assertEqual(out["quality"], "missing")


class TestRelativeChange(unittest.TestCase):
    def test_basic(self):
        # 对称相对变化口径（与 reminders 快照变化幅度一致）：|new−old|/max(|new|,|old|)
        self.assertAlmostEqual(relative_change(150, 100), 1 / 3)
        self.assertAlmostEqual(relative_change(50, 100), 0.5)
        self.assertEqual(relative_change(0, 100), 1.0)
        self.assertEqual(relative_change(100, 100), 0.0)
        self.assertEqual(relative_change(0, 0), 0.0)

    def test_no_previous(self):
        self.assertEqual(relative_change(80, None), 0.0)   # 无旧值 → 无环比

    def test_bad_inputs(self):
        self.assertEqual(relative_change("abc", 10), 0.0)
        self.assertEqual(relative_change(float("nan"), 10), 0.0)
        self.assertEqual(relative_change(float("inf"), 10), 0.0)
        self.assertEqual(relative_change(10, float("nan")), 0.0)

    def test_capped_at_one(self):
        self.assertEqual(relative_change(-500, 100), 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
