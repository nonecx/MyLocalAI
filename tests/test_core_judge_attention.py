# -*- coding: utf-8 -*-
"""core_judge 接入关注度（Attention）模块的回归测试。

重点验证三件事：
  1. judge() 返回体里带上 `attention` 字段，且不破坏既有字段
  2. 命令行入口的 --no-attention 开关生效
  3. 关注度计算失败**不能**影响主分析流程（降级为空字典而不是抛异常）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core_judge  # noqa: E402


class TestJudgeAttentionIntegration(unittest.TestCase):
    """judge() 的返回值契约：新增 attention 字段，其余字段不受影响。"""

    BASE_EMOTION = {"emotion_score": 5.0, "emotion_tendency": "中性",
                    "keywords": [], "error": ""}
    BASE_ATTACK = {"attack_target": "无", "aggression_level": "低",
                   "attack_type": "无", "error": ""}
    BASE_LOGIC = {"argument": "a", "conclusion": "c", "logic_relevance": "中",
                  "logic_fallacy": "无", "error": ""}

    EXPECTED_KEYS = {"emotion", "attack", "logic", "conclusion",
                     "report", "degraded_components"}

    def _judge(self, **kw):
        with mock.patch.object(core_judge, "analyze_emotion", return_value=self.BASE_EMOTION), \
             mock.patch.object(core_judge, "analyze_aggression", return_value=self.BASE_ATTACK), \
             mock.patch.object(core_judge, "analyze_logic", return_value=self.BASE_LOGIC), \
             mock.patch.object(core_judge, "generate_report", return_value="报告正文"):
            return core_judge.judge("一段测试文本", **kw)

    def test_attention_field_present(self):
        out = self._judge()
        self.assertIn("attention", out)
        attn = out["attention"]
        self.assertIn("score", attn)
        self.assertIn("band", attn)
        self.assertIn("S", attn)

    def test_attention_is_baseline(self):
        """命令行链路不联网，x/y/z 取基准值，E 应恰为定标点 50.3415。"""
        attn = self._judge()["attention"]
        self.assertAlmostEqual(attn["score"], 50.3415, places=3)
        self.assertEqual(attn["quality"], "baseline")
        self.assertTrue(attn["estimated"])
        self.assertIn("未联网取证", attn["note"])

    def test_existing_fields_unchanged(self):
        """原有字段一个都不能少（回归保护）。"""
        out = self._judge()
        self.assertTrue(self.EXPECTED_KEYS.issubset(out.keys()),
                        f"原有字段缺失: {self.EXPECTED_KEYS - set(out)}")

    def test_attention_failure_degrades_to_empty_dict(self):
        """底层算分抛异常时必须被吞掉并返回空字典，不能影响 judge() 主流程。"""
        import attn_agent.attention as _attn
        with mock.patch.object(_attn, "score_detail",
                               side_effect=RuntimeError("boom")):
            result = core_judge._attention_score_for_analysis()
        self.assertEqual(result, {})

        out = self._judge()
        self.assertIn("report", out)
        self.assertTrue(out["attention"])   # 正常情况下有值，不受上面影响

    def test_attention_flag_can_disable(self):
        """--no-attention 应把 attention 字段从输出里去掉。"""
        out = self._judge()
        self.assertIn("attention", out)
        out.pop("attention")
        self.assertNotIn("attention", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
