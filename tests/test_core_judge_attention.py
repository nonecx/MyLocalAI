# -*- coding: utf-8 -*-
"""关注度（Attention）与 classic 链路的装配回归测试。

2026-10-30 M1-2 装配倒转后本模块验证三件事：
  1. classic.judge() 返回体**不再**带 attention 字段（classic 与 attention 互不 import）
  2. 入口层挂载器 mylocalai.attention.attach 的行为：基准值占位、失败降级为空字典、
     联网失败退回基准、档位参数生效
  3. 挂载失败**不能**影响主分析流程
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mylocalai.classic.judge as core_judge  # noqa: E402
from mylocalai.attention import attach  # noqa: E402


class TestJudgeNoAttention(unittest.TestCase):
    """judge() 的返回值契约：不再带 attention 字段，其余字段不受影响。"""

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

    def test_attention_field_absent(self):
        out = self._judge()
        self.assertNotIn("attention", out)

    def test_judge_has_no_attention_import(self):
        """classic 与 attention 互不 import：judge.py 的导入（含函数内）不得指向 attention。"""
        import ast
        src = (ROOT / "src" / "mylocalai" / "classic" / "judge.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for m in mods:
                self.assertFalse(m.startswith("mylocalai.attention"),
                                 f"judge.py 出现 attention 导入: {m}")

    def test_existing_fields_unchanged(self):
        """原有字段一个都不能少（回归保护）。"""
        out = self._judge()
        self.assertTrue(self.EXPECTED_KEYS.issubset(out.keys()),
                        f"原有字段缺失: {self.EXPECTED_KEYS - set(out)}")


class TestAttachAttention(unittest.TestCase):
    """入口层挂载器 attention_for_text 的行为契约。"""

    def test_baseline_by_default(self):
        """不联网，x/y/z 取基准值，E 应恰为定标点 50.3415。"""
        attn = attach.attention_for_text("一段测试文本")
        self.assertAlmostEqual(attn["score"], 50.3415, places=3)
        self.assertEqual(attn["quality"], "baseline")
        self.assertTrue(attn["estimated"])
        self.assertIn("未联网取证", attn["note"])

    def test_failure_degrades_to_empty_dict(self):
        """底层算分抛异常时必须被吞掉并返回空字典，不能影响调用方主流程。"""
        import mylocalai.attention.attention as _attn
        with mock.patch.object(_attn, "score_detail",
                               side_effect=RuntimeError("boom")):
            result = attach.attention_for_text()
        self.assertEqual(result, {})

    def test_profile_flag_changes_score(self):
        """显式档位 reachable：同一组基准 x/y/z 下分数应与默认档不同。"""
        base = attach.attention_for_text("文本")
        prof = attach.attention_for_text("文本", profile="reachable")
        self.assertIn("score", prof)
        self.assertEqual(prof.get("profile"), "reachable")
        self.assertNotAlmostEqual(base["score"], prof["score"], places=6)

    def test_bad_profile_falls_back_to_default_score(self):
        """非法档位名：load_profile 回退默认档，分数应与不传 profile 一致。"""
        base = attach.attention_for_text("文本")
        bad = attach.attention_for_text("文本", profile="no-such-profile")
        self.assertAlmostEqual(base["score"], bad["score"], places=9)

    def test_network_failure_falls_back_to_baseline(self):
        """联网模式检索失败：退回基准值并标注 quality=fallback，不抛异常。"""
        with mock.patch("mylocalai.retrieval.search.search_all",
                        side_effect=RuntimeError("net down")):
            attn = attach.attention_for_text("某话题", allow_network=True)
        self.assertEqual(attn["quality"], "fallback")
        self.assertAlmostEqual(attn["score"], 50.3415, places=3)
        self.assertIn("联网取数失败", attn["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
