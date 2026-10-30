# -*- coding: utf-8 -*-
"""2026-10-24 复核后的补修（P0/P1/P2 遗留）回归测试。

覆盖：F2 报告一致性（_report_matches_conclusion / temperature=0 / REPORT_MODE）、
F6 empty_after_filter 真实路径（此前 evidence.py 的标记写在覆盖 entries 之后、
条件恒假，被手工构造 dict 的旧测试掩盖）、tiering.__all__ 与实际定义一致。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mylocalai.classic.report as report_generator# noqa: E402
from mylocalai.rumor import evidence as E
from mylocalai.core import tiering
from mylocalai.rumor import verdict


EMOTION = {"emotion_score": 6.0, "emotion_tendency": "情绪化宣泄", "keywords": ["气死"]}
ATTACK = {"attack_target": "无/未知", "aggression_level": "低", "attack_type": "无"}
LOGIC = {"logic_relevance": "弱", "logic_fallacy": "诉诸情绪"}


class TestReportConclusionConsistency(unittest.TestCase):
    """F2：报告是第 4 次 LLM 调用，输出不得与前三步的结论相矛盾。"""

    CONCLUSION = "疑似情绪化谣言"

    def _generate(self, content, err=None):
        with mock.patch.object(report_generator, "chat_text",
                               return_value=(content, err)) as ct:
            out = report_generator.generate_report(
                self.CONCLUSION, dict(EMOTION), dict(ATTACK), dict(LOGIC))
        return out, ct

    def test_matching_report_is_returned_as_is(self):
        content = "📊 【分析结论】\n该言论有较高可能性为【疑似情绪化谣言】。其余正文略。"
        out, ct = self._generate(content)
        self.assertEqual(out, content)
        ct.assert_called_once()

    def test_contradicting_report_falls_back_to_template(self):
        content = "📊 【分析结论】\n该言论属于理性探讨，不存在情绪化谣言问题。"
        out, _ = self._generate(content)
        self.assertNotEqual(out, content)
        self.assertIn("本地模板回退", out)
        self.assertIn(self.CONCLUSION, out)

    def test_negated_conclusion_is_not_a_match(self):
        out, _ = self._generate("该言论不是疑似情绪化谣言，请放心。")
        self.assertIn("本地模板回退", out)

    def test_empty_conclusion_falls_back_to_template(self):
        with mock.patch.object(report_generator, "chat_text",
                               return_value=("模型自由发挥的报告", None)):
            out = report_generator.generate_report("", dict(EMOTION), dict(ATTACK), dict(LOGIC))
        self.assertIn("本地模板回退", out)

    def test_temperature_is_zero(self):
        _, ct = self._generate("该言论有较高可能性为【疑似情绪化谣言】。")
        self.assertEqual(ct.call_args.kwargs.get("temperature"), 0.0)

    def test_report_mode_template_skips_llm(self):
        with mock.patch.object(report_generator, "chat_text") as ct, \
             mock.patch.dict("os.environ", {"REPORT_MODE": "template"}):
            ct.return_value = ("不应被调用", None)
            out = report_generator.generate_report(
                self.CONCLUSION, dict(EMOTION), dict(ATTACK), dict(LOGIC))
        ct.assert_not_called()
        self.assertIn("本地模板回退", out)
        self.assertIn("REPORT_MODE=template", out)


class TestEvidenceEmptyAfterFilter(unittest.TestCase):
    """F6：搜到了条目、却全被判为无关 → empty_after_filter（走真实代码路径）。"""

    CLAIMS = {"claims": [{"claim": "某说法"}], "has_factual_claim": True}

    @staticmethod
    def _search_fn(*results):
        def fn(query, limit):
            return {"results": list(results), "engines": {}}
        return fn

    @staticmethod
    def _result(url, title, snippet):
        return {"url": url, "title": title, "snippet": snippet}

    @staticmethod
    def _components():
        return ({"marker_points": 0, "credibility_points": 0, "assertion_signal": True},
                {"emotion_score": 3.0}, {"aggression_level": "中"},
                {"logic_relevance": "弱"},
                {"has_factual_claim": True, "unsourced_unverifiable": 0})

    def test_all_irrelevant_marks_flag_and_degrades(self):
        fn = self._search_fn(self._result("https://www.xinhuanet.com/a", "标题", "内容"))
        with mock.patch.object(E, "judge_stances_llm", return_value=(["无关"], None)):
            book = E.collect_evidence("某说法", claims=self.CLAIMS, search_fn=fn,
                                      fetch_fn=lambda url: None, llm_judge=True)
        self.assertEqual(book["entries"], [])
        self.assertEqual(book["filtered_out"], 1)
        self.assertTrue(book.get("empty_after_filter"),
                        "全被判为无关时应标记 empty_after_filter（此前恒假）")
        v = verdict.assess(*self._components(), evidence=book)
        self.assertIn("evidence", v["degraded_components"])
        self.assertNotEqual(v["confidence"], "高")

    def test_kept_entries_do_not_set_flag(self):
        fn = self._search_fn(self._result("https://www.xinhuanet.com/a", "标题", "内容"),
                             self._result("https://www.people.com.cn/b", "标题2", "内容2"))
        with mock.patch.object(E, "judge_stances_llm", return_value=(["无关", "反驳"], None)):
            book = E.collect_evidence("某说法", claims=self.CLAIMS, search_fn=fn,
                                      fetch_fn=lambda url: None, llm_judge=True)
        self.assertEqual(len(book["entries"]), 1)
        self.assertFalse(book.get("empty_after_filter"))
        v = verdict.assess(*self._components(), evidence=book)
        self.assertNotIn("evidence", v["degraded_components"])

    def test_no_results_do_not_set_flag(self):
        # 一条都没搜到走"搜索无结果"分支，不是"全被过滤"，不该标
        fn = self._search_fn()
        book = E.collect_evidence("某说法", claims=self.CLAIMS, search_fn=fn,
                                  fetch_fn=lambda url: None, llm_judge=False)
        self.assertEqual(book["entries"], [])
        self.assertIn("搜索无结果", book.get("error") or "")
        self.assertFalse(book.get("empty_after_filter"))


class TestTieringExports(unittest.TestCase):
    """tiering.__all__ 曾引用未定义的 ALL_TIERS（from tiering import * 会 AttributeError）。"""

    def test_all_exports_exist(self):
        for name in tiering.__all__:
            self.assertTrue(hasattr(tiering, name), name)


if __name__ == "__main__":
    unittest.main()
