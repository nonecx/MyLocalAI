# -*- coding: utf-8 -*-
"""attn_agent.deep_read 离线测试：不联网、不调用大模型。

覆盖三件事：
  1. 触发判定必须由确定性代码决定（deep_read_triggered），解读文本不改动任何分数
  2. LLM 不可用时必须回退本地模板并标 degraded（而不是抛异常或返回空）
  3. 外置口径写坏时必须退回内置默认并留告警
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from attn_agent import attention as A  # noqa: E402
from attn_agent import deep_read as D  # noqa: E402


def detail_of(x, y, z):
    d = A.score_detail(x, y, z)
    d["threshold"] = A.DEEP_READ_THRESHOLD
    return d


class TestTriggerDecision(unittest.TestCase):
    """触发判定与分数的一致性。"""

    def test_below_threshold_generates_nothing(self):
        r = D.deep_read("某话题", detail_of(30, 48316000, 149), use_llm=False)
        self.assertFalse(r["triggered"])
        self.assertEqual(r["text"], "")
        self.assertFalse(r["degraded"])
        self.assertIn("未超过深读阈值", r["reason"])

    def test_exactly_66_does_not_trigger(self):
        """严格大于才算：66.0 归「高关注」档但不触发深读。"""
        d = detail_of(1000, 1e8, 1e6)
        self.assertEqual(A.classify(66.0), "高关注")
        self.assertFalse(A.deep_read_triggered(66.0))
        r = D.deep_read("t", {**d, "score": 66.0}, use_llm=False)
        self.assertFalse(r["triggered"])

    def test_above_threshold_triggers(self):
        r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=False)
        self.assertTrue(r["triggered"])
        self.assertTrue(r["text"])
        self.assertTrue(r["degraded"])
        self.assertEqual(r["source"], "template")

    def test_score_is_untouched_by_deep_read(self):
        """解读绝不能改动分数：这是「大模型不参与打分」的底线。"""
        d = detail_of(1000, 1e8, 1e6)
        before = dict(d)
        D.deep_read("t", d, use_llm=False)
        self.assertEqual(d, before)


class TestLlmPath(unittest.TestCase):
    """LLM 可用/不可用两条路。"""

    def test_llm_success_uses_model_text(self):
        """模型给出合规 JSON 时，解读文本必须来自模型（不是模板）。"""
        good = json.dumps({
            "applicable": True,
            "issue_tags": [{"id": "info_transparency_trust", "confidence": "medium",
                            "evidence_ids": ["e1", "e2"]}],
            "tensions": [{"a": "信息发布节奏", "b": "公众知情需求",
                          "evidence_ids": ["e1"], "level": "L1"}],
            "phenomena": [{"text": "多条发言指向通报时间晚于网络传闻", "level": "L1",
                           "evidence_ids": ["e1"], "count": "7/12"}],
            "counter_reading": {"text": "3 条发言认为质疑源于误解", "evidence_ids": ["e2"]},
            "boundary": {"scope": "sample", "sample_size": 12, "proxy_flags": []},
            "uncertainty": [],
        }, ensure_ascii=False)
        evidence = [{"text": f"发言{i}", "stance": "质疑"} for i in range(12)]
        with mock.patch("shared_core.llm_client.chat_text", return_value=(good, "")):
            r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True,
                            evidence=evidence)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["source"], "llm")
        self.assertFalse(r["degraded"])
        # 文本由结构化字段渲染，含模型给出的张力与现象
        self.assertIn("信息发布节奏", r["text"])
        self.assertIn("通报时间晚于网络传闻", r["text"])

    def test_llm_free_text_is_not_trusted(self):
        """模型返回自由文本（非 JSON）时必须走模板回退，不能把自由发挥当解读。"""
        with mock.patch("shared_core.llm_client.chat_text",
                        return_value=("一、为什么关注\n因为与就业相关。", "")):
            r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["source"], "template")
        self.assertTrue(r["degraded"])
        self.assertTrue(any("未通过护栏" in w for w in r["warnings"]))

    def test_llm_failure_falls_back_to_template(self):
        with mock.patch("shared_core.llm_client.chat_text",
                        return_value=("", "HTTP 401: Authentication Fails")):
            r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["source"], "template")
        self.assertTrue(r["degraded"])
        self.assertTrue(any("大模型解读不可用" in w for w in r["warnings"]))

    def test_llm_exception_does_not_propagate(self):
        with mock.patch("shared_core.llm_client.chat_text",
                        side_effect=RuntimeError("网络炸了")):
            r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=True)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["source"], "template")

    def test_proxy_value_note_is_appended(self):
        """estimated=True 时必须把口径提示带进 prompt 和输出。"""
        seen = {}

        def fake(system, user, **kw):
            seen["user"] = user
            return "解读内容", ""

        with mock.patch("shared_core.llm_client.chat_text", side_effect=fake):
            r = D.deep_read("t", {**detail_of(1000, 1e8, 1e6), "estimated": True},
                            use_llm=True)
        self.assertIn("代理", seen["user"])
        self.assertIn("数据口径提示", r["text"])

    def test_prompt_contains_controlled_generation_constraints(self):
        """给模型的 system 必须包含受控生成的硬约束（议题封闭 / 张力 / 证据 / 层级）。"""
        seen = {}

        def fake(system, user, **kw):
            seen["system"] = system
            return "x", ""

        with mock.patch("shared_core.llm_client.chat_text", side_effect=fake):
            D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=True)
        for kw in ("不得自创", "张力", "evidence_id", "L1", "L3",
                   "applicable", "counter_reading", "严格 JSON"):
            self.assertIn(kw, seen["system"], f"system 缺少受控生成约束：{kw}")

    def test_prompt_contains_fewshot_contrast(self):
        """few-shot 必须含正反对比（坏例原句 + 好例 JSON）。"""
        seen = {}

        def fake(system, user, **kw):
            seen["system"] = system
            return "x", ""

        with mock.patch("shared_core.llm_client.chat_text", side_effect=fake):
            D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=True)
        self.assertIn("这反映了社会信任危机", seen["system"])
        self.assertIn("坏在", seen["system"])
        self.assertIn('"applicable": true', seen["system"])

    def test_prompt_contains_attention_data(self):
        seen = {}

        def fake(system, user, **kw):
            seen["user"] = user
            return "x", ""

        with mock.patch("shared_core.llm_client.chat_text", side_effect=fake):
            D.deep_read("测试话题", detail_of(1000, 1e8, 1e6), use_llm=True)
        self.assertIn("测试话题", seen["user"])
        self.assertIn("关注度 E", seen["user"])


class TestRulesLoading(unittest.TestCase):
    """外置口径的加载与回退。"""

    def test_default_rules_load_cleanly(self):
        rules, warnings = D._load_rules()
        self.assertEqual(warnings, [])
        self.assertTrue(rules["system_prompt"])
        self.assertTrue(rules["template"])

    def test_json_file_matches_module_defaults(self):
        """外置 JSON 的两段口径必须与模块内置旧版常量严格一致（逐字节）。"""
        data = json.loads(D.RULES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["system_prompt"], D._LEGACY_DEEP_READ_SYSTEM_PROMPT)
        self.assertEqual(data["template"], D.DEEP_READ_TEMPLATE)

    def test_template_placeholders_are_substituted(self):
        """模板占位符必须被真实数值替换，且分数保留 2 位小数。"""
        r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=False)
        self.assertIn("E = 69.56 / 100", r["text"])
        self.assertIn("高关注", r["text"])
        self.assertIn("E > 66", r["text"])
        self.assertNotIn("{score}", r["text"])
        self.assertNotIn("{band}", r["text"])
        self.assertNotIn("{threshold}", r["text"])

    def test_broken_json_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with mock.patch.object(D, "RULES_PATH", bad):
                rules, warnings = D._load_rules()
        self.assertEqual(rules["system_prompt"], D._LEGACY_DEEP_READ_SYSTEM_PROMPT)
        self.assertTrue(warnings)

    def test_missing_file_falls_back(self):
        with mock.patch.object(D, "RULES_PATH", Path("/nonexistent/x.json")):
            rules, warnings = D._load_rules()
        self.assertEqual(rules["template"], D.DEEP_READ_TEMPLATE)
        self.assertTrue(warnings)

    def test_non_string_field_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({"system_prompt": 123, "template": None}),
                         encoding="utf-8")
            with mock.patch.object(D, "RULES_PATH", p):
                rules, warnings = D._load_rules()
        self.assertEqual(rules["system_prompt"], D._LEGACY_DEEP_READ_SYSTEM_PROMPT)
        self.assertEqual(rules["template"], D.DEEP_READ_TEMPLATE)
        self.assertEqual(len(warnings), 2)

    def test_template_placeholders_are_substituted(self):
        """模板占位符必须被真实数值替换，且分数保留 2 位小数。"""
        r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=False)
        self.assertIn("E = 69.56 / 100", r["text"])
        self.assertIn("高关注", r["text"])
        self.assertIn("E > 66", r["text"])
        self.assertNotIn("{score}", r["text"])
        self.assertNotIn("{band}", r["text"])
        self.assertNotIn("{threshold}", r["text"])

    def test_broken_rules_do_not_break_deep_read(self):
        """口径写坏时深读仍要出文本（走内置默认），不能抛异常。"""
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("[[[", encoding="utf-8")
            with mock.patch.object(D, "RULES_PATH", bad):
                r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=False)
        self.assertTrue(r["triggered"])
        self.assertTrue(r["text"])
        self.assertTrue(r["warnings"])


class TestCli(unittest.TestCase):
    """命令行入口。"""

    def run_main(self, argv):
        import io
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = D.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_below_threshold_reports_no_trigger(self):
        rc, out, _ = self.run_main(["--x", "30", "--y", "48316000", "--z", "149", "--no-llm"])
        self.assertEqual(rc, 0)
        self.assertIn("54.8130", out)
        self.assertIn("触发深读：否", out)

    def test_above_threshold_prints_interpretation(self):
        rc, out, _ = self.run_main(
            ["--query", "某话题", "--x", "1000", "--y", "100000000",
             "--z", "1000000", "--no-llm"])
        self.assertEqual(rc, 0)
        self.assertIn("69.5632", out)
        self.assertIn("本地模板", out)
        self.assertIn("为什么人们会关注", out)

    def test_missing_args_return_nonzero(self):
        rc, _, err = self.run_main(["--x", "1", "--no-llm"])
        self.assertNotEqual(rc, 0)
        self.assertIn("缺少必填参数", err)

    def test_non_numeric_returns_nonzero(self):
        rc, _, err = self.run_main(["--x", "abc", "--y", "1", "--z", "1", "--no-llm"])
        self.assertNotEqual(rc, 0)
        self.assertIn("float", err)

    def test_negative_returns_nonzero(self):
        rc, _, err = self.run_main(["--x", "-1", "--y", "1", "--z", "1", "--no-llm"])
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
