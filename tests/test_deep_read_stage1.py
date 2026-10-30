# -*- coding: utf-8 -*-
"""关注度深读（Deep Read）阶段一测试：触发规则 + 输入信号结构化。离线，不调大模型。

覆盖（对齐任务书的阶段一验收）：
   1. `should_deep_read`：absolute/quantile 两模式 × spec/reachable 两档全组合
   2. `derive_signals`：除零保护、proxy_flags、与 attention 公式逐点一致
   3. 规则加载：JSON 写坏/结构不合法 → 退回内置默认 + 告警，不崩
   4. CLI：旧参数向后兼容 + 新参数 --profile / --rules-path / --json
   5. 行为兼容：未指定 profile 时与改造前逐位一致

运行：.venv/Scripts/python.exe -m unittest tests.test_deep_read_stage1 -v
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.attention import attention as A# noqa: E402
from mylocalai.attention import deep_read as D# noqa: E402


def detail_of(x, y, z, params=None):
    d = A.score_detail(x, y, z, params)
    d["threshold"] = A.DEEP_READ_THRESHOLD
    return d


# 2026-10-26 实测的五个真实头部话题（x, y, z）——与 test_attention_profiles 同源
TOPICS = [
    ("老干妈回应AI裁员传闻", 30, 48316000, 149),
    ("台湾艺人阿雅公开表态", 5, 12506705, 302),
    ("长鑫存储硬气拒绝苹果", 11, 13822047, 42),
    ("承包户花生地遭村民捡拾", 3, 2067344, 129),
    ("男子出狱砍死妻子二审", 6, 3766947, 1),
]


# ===========================================================================
# 1. 触发规则 should_deep_read
# ===========================================================================

class TestShouldDeepReadAbsolute(unittest.TestCase):
    """absolute 模式：E 严格大于阈值。"""

    def setUp(self):
        self.rules = {"trigger": D.DEFAULT_TRIGGER_RULES,
                      "deep_read": D.DEFAULT_DEEP_READ_PARAMS,
                      "banned_patterns": D.DEFAULT_BANNED_PATTERNS}

    def test_spec_profile_absolute_above_threshold(self):
        """spec 档 absolute：E=69.56 > 66 → 触发。"""
        self.assertTrue(D.should_deep_read(detail_of(1000, 1e8, 1e6),
                                           profile="spec", rules=self.rules))

    def test_spec_profile_absolute_below_threshold(self):
        """spec 档 absolute：E=54.81 < 66 → 不触发。"""
        self.assertFalse(D.should_deep_read(detail_of(30, 48316000, 149),
                                            profile="spec", rules=self.rules))

    def test_exactly_threshold_does_not_trigger(self):
        """严格大于：E 恰等于 66.0 不算触发。"""
        d = {**detail_of(1000, 1e8, 1e6), "score": 66.0}
        self.assertFalse(D.should_deep_read(d, profile="spec", rules=self.rules))

    def test_absolute_ignores_batch(self):
        """absolute 模式只看当前分数，同批其他分数不影响判定。"""
        high = {"score": 69.56}
        low = {"score": 10.0}
        self.assertTrue(D.should_deep_read(high, profile="spec", rules=self.rules))
        # 当前话题低分，即使同批有高分也不触发（absolute 不比相对位置）
        self.assertFalse(D.should_deep_read(low, profile="spec", rules=self.rules))

    def test_custom_threshold_from_rules(self):
        """规则里的阈值必须真的生效（不是硬编码 66）。"""
        rules = {"trigger": {"mode": "absolute", "absolute_threshold": 50.0},
                 "deep_read": D.DEFAULT_DEEP_READ_PARAMS,
                 "banned_patterns": D.DEFAULT_BANNED_PATTERNS}
        # E=54.81 > 50 → 触发
        self.assertTrue(D.should_deep_read(detail_of(30, 48316000, 149),
                                           rules=rules))

    def test_absolute_matches_legacy_trigger(self):
        """绝对口径必须与改造前的 deep_read_triggered 逐位一致。"""
        for _, x, y, z in TOPICS:
            d = detail_of(x, y, z)
            self.assertEqual(
                D.should_deep_read(d, profile="spec", rules=self.rules),
                A.deep_read_triggered(d["score"], 66.0),
                f"话题 ({x},{y},{z}) 触发判定与改造前不一致")


class TestShouldDeepReadQuantile(unittest.TestCase):
    """quantile 模式：同批 Top-K%。"""

    def _rules(self, ratio=0.1):
        return {"trigger": {"mode": "quantile", "quantile_top_ratio": ratio},
                "deep_read": D.DEFAULT_DEEP_READ_PARAMS,
                "banned_patterns": D.DEFAULT_BANNED_PATTERNS}

    def _judged(self, batch, index, ratio):
        """把 batch[index] 当作"当前话题"判定（约定：当前话题放末尾，多重集合不变）。"""
        others = [v for i, v in enumerate(batch) if i != index]
        return D.should_deep_read(others + [batch[index]], profile="reachable",
                                  rules=self._rules(ratio))

    def test_top_of_batch_triggers(self):
        """同批最高分排进 Top 10% → 触发（即使它低于绝对阈值）。"""
        batch = [10.0, 20.0, 30.0, 40.0, 55.0]
        self.assertTrue(self._judged(batch, 4, 0.1))

    def test_bottom_of_batch_does_not_trigger(self):
        """同批最低分不在 Top 10% → 不触发（即使它自己很高）。"""
        batch = [70.0, 71.0, 72.0, 73.0, 80.0]
        self.assertFalse(self._judged(batch, 0, 0.1))

    def test_ratio_selects_expected_count(self):
        """Top 10% of 10 条 = 1 条；只有最高分触发。"""
        scores = [float(v) for v in range(1, 11)]     # 1..10
        triggered = [s for i, s in enumerate(scores) if self._judged(scores, i, 0.1)]
        self.assertEqual(triggered, [10.0])

    def test_ratio_half_selects_top_five(self):
        """Top 50% of 10 条 = 5 条。"""
        scores = [float(v) for v in range(1, 11)]
        triggered = sorted(s for i, s in enumerate(scores)
                           if self._judged(scores, i, 0.5))
        self.assertEqual(triggered, [6.0, 7.0, 8.0, 9.0, 10.0])

    def test_small_batch_at_least_one(self):
        """小批量下 K 至少取 1，否则谁都触发不了（10% of 5 = 0.5 → 1）。"""
        scores = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertTrue(self._judged(scores, 4, 0.1))
        self.assertFalse(self._judged(scores, 0, 0.1))

    def test_ties_are_inclusive(self):
        """同分并列时按"不小于分界值"处理（保守，不漏头部话题）。"""
        scores = [50.0, 50.0, 50.0]
        for i in range(3):
            self.assertTrue(self._judged(scores, i, 0.34))

    def test_empty_batch_returns_false(self):
        """空输入不触发、不崩。"""
        self.assertFalse(D.should_deep_read([], profile="reachable", rules=self._rules()))

    def test_plain_number_batch(self):
        """批量元素也可以是纯数字（不必都是 dict）。"""
        self.assertTrue(D.should_deep_read([1.0, 2.0, 3.0], profile="reachable",
                                           rules=self._rules(0.34)))
        # Top 1% of 3 条 = 1 条，最低分那条不触发
        self.assertFalse(D.should_deep_read([3.0, 2.0, 1.0], profile="reachable",
                                            rules=self._rules(0.01)))


class TestShouldDeepReadModes(unittest.TestCase):
    """change_rate 与 any 模式。"""

    def _rules(self, mode, **extra):
        trigger = {"mode": mode}
        trigger.update(extra)
        return {"trigger": trigger, "deep_read": D.DEFAULT_DEEP_READ_PARAMS,
                "banned_patterns": D.DEFAULT_BANNED_PATTERNS}

    def test_change_rate_is_reserved_and_never_triggers(self):
        """change_rate 是预留模式：当前实现为空，恒不触发。"""
        rules = self._rules("change_rate", change_rate_threshold=0.5)
        self.assertFalse(D.should_deep_read(detail_of(1000, 1e8, 1e6), rules=rules))
        self.assertFalse(D.should_deep_read({"score": 99.0}, rules=rules))

    def test_any_mode_triggers_on_either(self):
        """any 模式：absolute 或 quantile 任一满足即触发。"""
        rules = self._rules("any", absolute_threshold=66.0, quantile_top_ratio=0.1)
        # absolute 不满足（54.81 < 66），但 quantile 满足（同批最高，放末尾）
        batch = [10.0, 20.0, 30.0, 40.0]
        self.assertTrue(D.should_deep_read(batch + [54.81], rules=rules))
        # 两者都不满足（当前话题最低分且低于阈值）
        low_batch = [70.0, 71.0, 72.0]
        self.assertFalse(D.should_deep_read(low_batch + [50.0], rules=rules))

    def test_any_mode_triggers_on_absolute_alone(self):
        """any 模式下 absolute 单独满足也要触发。"""
        rules = self._rules("any", absolute_threshold=66.0, quantile_top_ratio=0.01)
        # 单条高分：absolute 满足，quantile 也满足（自己是唯一一条）
        self.assertTrue(D.should_deep_read({"score": 90.0}, rules=rules))
        # 单条低分：absolute 不满足、quantile 满足（唯一一条即 Top 100%）
        self.assertTrue(D.should_deep_read({"score": 10.0}, rules=rules))

    def test_unknown_mode_falls_back_to_absolute(self):
        """非法 mode 在加载时已被纠正为 absolute（resolve 后仍可用）。"""
        rules = self._rules("not-a-mode", absolute_threshold=66.0)
        fixed, warnings = D._normalize_rules(rules)
        self.assertEqual(fixed["trigger"]["mode"], "absolute")
        self.assertTrue(any("不合法" in w for w in warnings))
        # 纠正后 absolute 口径：69.56 > 66 → 触发
        self.assertTrue(D.should_deep_read(detail_of(1000, 1e8, 1e6), rules=fixed))

    def test_unknown_mode_without_normalization_does_not_trigger(self):
        """未经纠正的非法模式：absolute/quantile 都不满足 → False（不误触发）。"""
        rules = self._rules("not-a-mode", absolute_threshold=66.0)
        self.assertFalse(D.should_deep_read(detail_of(1000, 1e8, 1e6), rules=rules))


class TestProfileRulesCombination(unittest.TestCase):
    """spec/reachable 两档 × absolute/quantile 两模式全组合。"""

    def _rules_for(self, spec_mode="absolute", reach_mode="quantile", **kw):
        return {
            "trigger": {
                "mode": "absolute", "absolute_threshold": 66.0,
                "quantile_top_ratio": 0.1,
                "by_profile": {
                    "spec": {"mode": spec_mode, "absolute_threshold": 66.0},
                    "reachable": {"mode": reach_mode, "quantile_top_ratio": 0.1},
                },
            },
            "deep_read": D.DEFAULT_DEEP_READ_PARAMS,
            "banned_patterns": D.DEFAULT_BANNED_PATTERNS,
        }

    def test_spec_absolute_high_topic_triggers(self):
        rules = self._rules_for()
        self.assertTrue(D.should_deep_read(detail_of(1000, 1e8, 1e6),
                                           profile="spec", rules=rules))

    def test_spec_absolute_low_topic_does_not(self):
        """spec + absolute：5 个真实头部话题全部低于 66 → 0/5 触发（问题 A 的现状）。"""
        rules = self._rules_for()
        hits = sum(1 for _, x, y, z in TOPICS
                   if D.should_deep_read(detail_of(x, y, z), profile="spec", rules=rules))
        self.assertEqual(hits, 0, "spec 档绝对阈值下这批话题应 0/5 触发")

    def test_spec_quantile_lets_head_topic_trigger(self):
        """spec + quantile（配置可为单档覆盖模式）：同批最高分可触发。

        约定：待判定的"当前话题"放 batch 末尾（`_current_and_batch` 取最后一个元素）。
        """
        rules = self._rules_for(spec_mode="quantile")
        batch = [detail_of(x, y, z) for _, x, y, z in TOPICS]
        top = max(batch, key=lambda d: d["score"])
        others = [d for d in batch if d is not top]
        self.assertTrue(D.should_deep_read(others + [top], profile="spec", rules=rules))
        # 最低分那条不触发
        bottom = min(batch, key=lambda d: d["score"])
        rest = [d for d in batch if d is not bottom]
        self.assertFalse(D.should_deep_read(rest + [bottom], profile="spec",
                                            rules=rules))

    def test_reachable_quantile_head_topic_triggers(self):
        """reachable + quantile：默认配置下同批头部话题可触发。"""
        rules = self._rules_for()
        params = A.PROFILES["reachable"]["params"]
        batch = [detail_of(x, y, z, params) for _, x, y, z in TOPICS]
        top = max(batch, key=lambda d: d["score"])
        others = [d for d in batch if d is not top]
        self.assertTrue(D.should_deep_read(others + [top], profile="reachable",
                                           rules=rules))
        # 最低分那条不触发
        bottom = min(batch, key=lambda d: d["score"])
        rest = [d for d in batch if d is not bottom]
        self.assertFalse(D.should_deep_read(rest + [bottom], profile="reachable",
                                            rules=rules))

    def test_reachable_absolute_still_fails_on_head_topics(self):
        """reachable + absolute：本批话题仍低于 66 → 0/5（证明必须换模式而非只调参）。"""
        rules = self._rules_for(reach_mode="absolute")
        params = A.PROFILES["reachable"]["params"]
        hits = sum(1 for _, x, y, z in TOPICS
                   if D.should_deep_read(detail_of(x, y, z, params),
                                         profile="reachable", rules=rules))
        self.assertEqual(hits, 0)

    def test_no_profile_uses_top_level_absolute(self):
        """未指定 profile 时用顶层规则（absolute/66），与改造前一致。"""
        rules = self._rules_for()
        self.assertTrue(D.should_deep_read(detail_of(1000, 1e8, 1e6), rules=rules))
        self.assertFalse(D.should_deep_read(detail_of(30, 48316000, 149), rules=rules))

    def test_unknown_profile_falls_back_to_top_level(self):
        """未在 by_profile 里列出的档位沿用顶层规则（不崩、不误判）。"""
        rules = self._rules_for()
        self.assertTrue(D.should_deep_read(detail_of(1000, 1e8, 1e6),
                                           profile="no-such-profile", rules=rules))


class TestRulesLoading(unittest.TestCase):
    """规则加载与降级。"""

    def test_default_rules_load_cleanly(self):
        rules, warnings = D.load_rules()
        self.assertEqual(warnings, [])
        self.assertEqual(rules["trigger"]["mode"], "absolute")
        self.assertEqual(rules["trigger"]["absolute_threshold"], 66.0)
        self.assertEqual(rules["deep_read"]["max_words"], 300)
        self.assertTrue(rules["banned_patterns"])

    def test_rules_json_file_matches_module_defaults(self):
        """外置 JSON 的触发口径必须与模块内置默认一致（防止文件与代码漂移）。"""
        data = json.loads(D.DEEP_READ_RULES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["trigger"]["mode"], D.DEFAULT_TRIGGER_RULES["mode"])
        self.assertEqual(data["trigger"]["absolute_threshold"],
                         D.DEFAULT_TRIGGER_RULES["absolute_threshold"])
        self.assertEqual(data["trigger"]["quantile_top_ratio"],
                         D.DEFAULT_TRIGGER_RULES["quantile_top_ratio"])
        # 忽略 _comment 说明键后，生成参数段必须逐键一致
        params = {k: v for k, v in data["deep_read"].items() if not k.startswith("_")}
        self.assertEqual(params, D.DEFAULT_DEEP_READ_PARAMS)
        # 档位覆盖：spec 保留 absolute/66，reachable 改 quantile
        by_profile = data["trigger"]["by_profile"]
        self.assertEqual(by_profile["spec"]["mode"], "absolute")
        self.assertEqual(by_profile["spec"]["absolute_threshold"], 66)
        self.assertEqual(by_profile["reachable"]["mode"], "quantile")

    def test_broken_json_falls_back_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            rules, warnings = D.load_rules(str(bad))
        self.assertEqual(rules["trigger"]["mode"], "absolute")
        self.assertEqual(rules["trigger"]["absolute_threshold"], 66.0)
        self.assertTrue(warnings)
        self.assertTrue(any("退回" in w or "内置默认" in w for w in warnings))

    def test_missing_file_falls_back(self):
        rules, warnings = D.load_rules("/nonexistent/deep_read_rules.json")
        self.assertEqual(rules["trigger"]["absolute_threshold"], 66.0)
        self.assertTrue(warnings)

    def test_bad_trigger_mode_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({"trigger": {"mode": "nonsense"},
                                     "deep_read": {}}), encoding="utf-8")
            rules, warnings = D.load_rules(str(p))
        self.assertEqual(rules["trigger"]["mode"], "absolute")
        self.assertTrue(any("不合法" in w for w in warnings))

    def test_bad_threshold_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({"trigger": {"mode": "absolute",
                                                 "absolute_threshold": "abc"},
                                     "deep_read": {}}), encoding="utf-8")
            rules, warnings = D.load_rules(str(p))
        self.assertEqual(rules["trigger"]["absolute_threshold"], 66.0)
        self.assertTrue(warnings)

    def test_bad_ratio_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({"trigger": {"mode": "quantile",
                                                 "quantile_top_ratio": 5.0},
                                     "deep_read": {}}), encoding="utf-8")
            rules, warnings = D.load_rules(str(p))
        self.assertEqual(rules["trigger"]["quantile_top_ratio"], 0.1)
        self.assertTrue(warnings)

    def test_missing_deep_read_section_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({"trigger": {"mode": "absolute"}}), encoding="utf-8")
            rules, warnings = D.load_rules(str(p))
        self.assertEqual(rules["deep_read"], D.DEFAULT_DEEP_READ_PARAMS)
        self.assertTrue(warnings)

    def test_bad_deep_read_fields_fall_back_individually(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({
                "trigger": {"mode": "absolute"},
                "deep_read": {"max_words": -5, "require_counter_reading": "yes",
                              "max_issue_tags": 0, "allow_other_tag": 1},
            }), encoding="utf-8")
            rules, warnings = D.load_rules(str(p))
        self.assertEqual(rules["deep_read"], D.DEFAULT_DEEP_READ_PARAMS)
        self.assertEqual(len(warnings), 4)

    def test_valid_custom_rules_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({
                "trigger": {"mode": "quantile", "quantile_top_ratio": 0.25,
                            "by_profile": {"spec": {"mode": "absolute",
                                                    "absolute_threshold": 40.0}}},
                "deep_read": {"max_words": 500, "require_counter_reading": False,
                              "max_issue_tags": 5, "allow_other_tag": False},
            }), encoding="utf-8")
            rules, warnings = D.load_rules(str(p))
        self.assertEqual(warnings, [])
        self.assertEqual(rules["trigger"]["mode"], "quantile")
        self.assertEqual(rules["deep_read"]["max_words"], 500)
        self.assertFalse(rules["deep_read"]["require_counter_reading"])
        self.assertEqual(rules["trigger"]["by_profile"]["spec"]["absolute_threshold"], 40.0)

    def test_broken_rules_do_not_break_deep_read(self):
        """规则写坏时 deep_read 仍要出文本（走内置默认），不抛异常。"""
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("[[[", encoding="utf-8")
            r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=False,
                            rules_path=str(bad))
        self.assertTrue(r["triggered"])
        self.assertTrue(r["text"])
        self.assertTrue(r["warnings"])

    def test_banned_patterns_loaded_from_file(self):
        rules, _ = D.load_rules()
        ids = {p["id"] for p in rules["banned_patterns"]}
        for expect in ("subject_blame", "universal_claim", "value_judgment", "prediction"):
            self.assertIn(expect, ids)


# ===========================================================================
# 2. 输入信号结构化 derive_signals
# ===========================================================================

class TestDeriveSignals(unittest.TestCase):
    """derive_signals 的字段、除零保护与公式一致性。"""

    def test_all_fields_present(self):
        s = A.derive_signals(1000, 100000000, 1000000)
        for key in ("x", "y", "z", "engagement_ratio", "spread", "resonance",
                    "share_x", "share_y", "share_z", "profile", "proxy_flags"):
            self.assertIn(key, s)

    def test_ratios_are_correct(self):
        s = A.derive_signals(1000, 100000000, 1000000)
        self.assertEqual(s["engagement_ratio"], 1e6 / 1e8)     # z / y = 0.01
        self.assertEqual(s["resonance"], 1e6 / 1000)           # z / x = 1000
        self.assertEqual(s["spread"], 1000.0)                   # x

    def test_zero_y_protected(self):
        """y=0 时 engagement_ratio 给 0.0，不是 inf/异常。"""
        s = A.derive_signals(1000, 0, 0)
        self.assertEqual(s["engagement_ratio"], 0.0)
        self.assertEqual(s["resonance"], 0.0)

    def test_zero_x_protected(self):
        """x=0 时 resonance 给 0.0，不是 inf/异常。"""
        s = A.derive_signals(0, 100000000, 1000000)
        self.assertEqual(s["resonance"], 0.0)
        self.assertEqual(s["spread"], 0.0)
        self.assertEqual(s["engagement_ratio"], 1e6 / 1e8)

    def test_all_zero_protected(self):
        """全零输入合法（epsilon 保证分数 > 0），比值与占比全为 0（term 全零）。"""
        s = A.derive_signals(0, 0, 0)
        self.assertEqual(s["engagement_ratio"], 0.0)
        self.assertEqual(s["resonance"], 0.0)
        self.assertEqual(s["share_x"], 0.0)
        self.assertEqual(s["share_y"], 0.0)
        self.assertEqual(s["share_z"], 0.0)
        # 分数仍严格 > 0（epsilon 的作用），故 S > 0
        self.assertGreater(A.attention_score(0, 0, 0), 0.0)

    def test_shares_match_attention_formula(self):
        """share_* 必须与 contributions() 逐点一致（含 epsilon 分母）。"""
        for _, x, y, z in TOPICS:
            s = A.derive_signals(x, y, z)
            c = A.contributions(x, y, z)
            self.assertAlmostEqual(s["share_x"], c["term_x"] / c["S"], places=15)
            self.assertAlmostEqual(s["share_y"], c["term_y"] / c["S"], places=15)
            self.assertAlmostEqual(s["share_z"], c["term_z"] / c["S"], places=15)

    def test_shares_sum_to_at_most_one(self):
        s = A.derive_signals(1000, 1e8, 1e6)
        total = s["share_x"] + s["share_y"] + s["share_z"]
        self.assertLessEqual(total, 1.0)
        # epsilon 计入分母，故三者之和略小于 1（约 1 - 6e-7）
        self.assertAlmostEqual(total, 1.0, places=5)

    def test_proxy_flags_default_empty(self):
        s = A.derive_signals(1000, 1e8, 1e6)
        self.assertEqual(s["proxy_flags"], [])

    def test_proxy_flags_passed_through(self):
        s = A.derive_signals(1000, 1e8, 1e6, proxy_flags=["y_proxy", "z_proxy"])
        self.assertEqual(s["proxy_flags"], ["y_proxy", "z_proxy"])

    def test_proxy_flags_deduped_and_stripped(self):
        s = A.derive_signals(1000, 1e8, 1e6,
                             proxy_flags=["y_proxy", " y_proxy ", "", "z_proxy"])
        self.assertEqual(s["proxy_flags"], ["y_proxy", "z_proxy"])

    def test_profile_echoed(self):
        self.assertEqual(A.derive_signals(1, 1, 1, profile="reachable")["profile"],
                         "reachable")
        # 未传 profile 时给默认档名
        self.assertEqual(A.derive_signals(1, 1, 1)["profile"], A.DEFAULT_PROFILE)

    def test_invalid_input_raises(self):
        with self.assertRaises(A.AttentionInputError):
            A.derive_signals(-1, 100, 100)
        with self.assertRaises(A.AttentionInputError):
            A.derive_signals(True, 100, 100)
        with self.assertRaises(A.AttentionInputError):
            A.derive_signals(float("nan"), 100, 100)

    def test_invalid_params_raises(self):
        with self.assertRaises(A.AttentionParamError):
            A.derive_signals(1, 1, 1, params={"x0": 100.0})   # 缺键

    def test_ratios_reflect_real_topics(self):
        """真实头部话题的信号应能区分"高参与"与"高传播低参与"。"""
        high_engage = A.derive_signals(30, 48316000, 149)      # 老干妈：阅读多留言少
        low_spread = A.derive_signals(3, 2067344, 129)         # 花生地：篇少留言不少
        self.assertLess(high_engage["engagement_ratio"],
                        low_spread["engagement_ratio"])
        self.assertGreater(high_engage["spread"], low_spread["spread"])


class TestDeepReadSignalsWiring(unittest.TestCase):
    """信号必须真的进 prompt（否则等于没做）。"""

    def test_signals_in_user_prompt(self):
        seen = {}

        def fake(system, user, **kw):
            seen["user"] = user
            return "解读", ""

        with mock.patch("mylocalai.llm.llm_client.chat_text", side_effect=fake):
            D.deep_read("t", {**detail_of(1000, 1e8, 1e6), "estimated": True},
                        use_llm=True, profile="spec")
        for key in ("engagement_ratio", "spread", "resonance", "share_x",
                    "share_y", "share_z", "profile", "proxy_flags"):
            self.assertIn(key, seen["user"], f"prompt 缺少信号字段 {key}")
        self.assertIn("y_proxy", seen["user"])

    def test_signals_returned_in_result(self):
        r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=False)
        self.assertIn("signals", r)
        self.assertEqual(r["signals"]["engagement_ratio"], 0.01)
        self.assertIn("trigger_mode", r)
        self.assertIn("profile", r)

    def test_proxy_flags_empty_without_estimated(self):
        r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=False)
        self.assertEqual(r["signals"]["proxy_flags"], [])

    def test_proxy_flags_set_when_estimated(self):
        r = D.deep_read("t", {**detail_of(1000, 1e8, 1e6), "estimated": True},
                        use_llm=False)
        self.assertEqual(r["signals"]["proxy_flags"], ["y_proxy", "z_proxy"])

    def test_not_triggered_still_returns_signals(self):
        """未触发时也返回信号（前端要展示口径，不因未触发就丢信息）。"""
        r = D.deep_read("t", detail_of(30, 48316000, 149), use_llm=False)
        self.assertFalse(r["triggered"])
        self.assertIn("signals", r)


# ===========================================================================
# 3. 行为兼容与 CLI
# ===========================================================================

class TestBackwardCompatibility(unittest.TestCase):
    """改造不得改变既有对外行为。"""

    def test_old_three_positional_call_still_works(self):
        """旧调用签名 deep_read(query, detail, use_llm) 仍可用。"""
        r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=False)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["source"], "template")
        self.assertTrue(r["degraded"])

    def test_untriggered_reason_mentions_threshold(self):
        r = D.deep_read("某话题", detail_of(30, 48316000, 149), use_llm=False)
        self.assertFalse(r["triggered"])
        self.assertIn("未超过深读阈值", r["reason"])

    def test_default_trigger_matches_legacy(self):
        """不传 profile/rules 时触发判定与改造前一致（问题 A 的兼容底线）。"""
        for _, x, y, z in TOPICS:
            d = detail_of(x, y, z)
            r = D.deep_read("t", d, use_llm=False)
            self.assertEqual(r["triggered"], A.deep_read_triggered(d["score"]),
                             f"({x},{y},{z}) 默认触发判定与改造前不一致")

    def test_score_untouched_by_deep_read(self):
        d = detail_of(1000, 1e8, 1e6)
        before = dict(d)
        D.deep_read("t", d, use_llm=False, batch=[d], profile="spec")
        self.assertEqual(d, before)

    def test_llm_success_path_unchanged(self):
        """模型给出合规 JSON 时 source=llm、degraded=False（LLM 可用路径不回退）。"""
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
        evidence = [{"text": f"发言{i}"} for i in range(12)]
        with mock.patch("mylocalai.llm.llm_client.chat_text", return_value=(good, "")):
            r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True,
                            evidence=evidence)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["source"], "llm")
        self.assertFalse(r["degraded"])
        self.assertIn("信息发布节奏", r["text"])

    def test_llm_failure_falls_back(self):
        with mock.patch("mylocalai.llm.llm_client.chat_text",
                        return_value=("", "HTTP 401")):
            r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True)
        self.assertEqual(r["source"], "template")
        self.assertTrue(r["degraded"])
        self.assertTrue(any("大模型解读不可用" in w for w in r["warnings"]))

    def test_llm_exception_does_not_propagate(self):
        with mock.patch("mylocalai.llm.llm_client.chat_text",
                        side_effect=RuntimeError("网络炸了")):
            r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=True)
        self.assertEqual(r["source"], "template")


class TestCli(unittest.TestCase):
    """命令行入口：旧参数兼容 + 新参数。"""

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = D.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_old_args_still_work(self):
        rc, out, _ = self.run_main(
            ["--query", "某话题", "--x", "1000", "--y", "100000000",
             "--z", "1000000", "--no-llm"])
        self.assertEqual(rc, 0)
        self.assertIn("69.5632", out)
        self.assertIn("本地模板", out)
        self.assertIn("为什么人们会关注", out)

    def test_old_args_below_threshold(self):
        rc, out, _ = self.run_main(["--x", "30", "--y", "48316000", "--z", "149", "--no-llm"])
        self.assertEqual(rc, 0)
        self.assertIn("54.8130", out)
        self.assertIn("触发深读：否", out)

    def test_missing_args_return_nonzero(self):
        rc, _, err = self.run_main(["--x", "1", "--no-llm"])
        self.assertNotEqual(rc, 0)
        self.assertIn("缺少必填参数", err)

    def test_non_numeric_returns_nonzero(self):
        rc, _, err = self.run_main(["--x", "abc", "--y", "1", "--z", "1", "--no-llm"])
        self.assertNotEqual(rc, 0)
        self.assertIn("float", err)

    def test_negative_returns_nonzero(self):
        rc, _, _ = self.run_main(["--x", "-1", "--y", "1", "--z", "1", "--no-llm"])
        self.assertNotEqual(rc, 0)

    def test_profile_flag_accepted(self):
        rc, out, _ = self.run_main(
            ["--query", "t", "--x", "30", "--y", "48316000", "--z", "149",
             "--no-llm", "--profile", "reachable"])
        self.assertEqual(rc, 0)
        self.assertIn("reachable", out)
        # quantile 模式下单条自身即整批，最高分 → 触发
        self.assertIn("触发深读：是", out)

    def test_json_flag_outputs_new_schema(self):
        rc, out, _ = self.run_main(
            ["--query", "t", "--x", "1000", "--y", "100000000", "--z", "1000000",
             "--no-llm", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        for key in ("triggered", "text", "source", "degraded", "warnings",
                    "signals", "profile", "trigger_mode"):
            self.assertIn(key, data)
        self.assertTrue(data["triggered"])

    def test_rules_path_flag_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({"trigger": {"mode": "absolute",
                                                 "absolute_threshold": 40.0},
                                     "deep_read": {}}), encoding="utf-8")
            rc, out, _ = self.run_main(
                ["--x", "30", "--y", "48316000", "--z", "149", "--no-llm",
                 "--rules-path", str(p)])
        self.assertEqual(rc, 0)
        # E=54.81 > 40 → 自定义阈值下应触发
        self.assertIn("触发深读：是", out)

    def test_unknown_arg_returns_nonzero(self):
        rc, _, err = self.run_main(["--nope", "1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("无法识别的参数", err)

    def test_help_returns_zero(self):
        rc, out, _ = self.run_main(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("--profile", out)
        self.assertIn("--rules-path", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
