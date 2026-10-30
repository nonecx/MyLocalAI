# -*- coding: utf-8 -*-
"""关注度深读（Deep Read）受控生成测试：阶段二 + 阶段三验收。离线，不调大模型。

覆盖（对齐任务书 3.5 的用例表）：
    触发规则      absolute/quantile × spec/reachable 全组合
    信号派生      除零、proxy_flags、与 attention 公式一致
    议题封闭性    清单外 id → other/low 或被丢
    证据覆盖      无 evidence_id 条目被丢
    L3 挂靠       全篇无 L1/L2 时 L3 被丢/降级
    禁词/禁归因   红队集（如"某部门肯定在瞒"）输出不含具体主体归因
    样本→普遍     输出不含"全社会/普遍/必然"
    出口          纯情绪输入 → applicable=false
    反方必填      缺 counter_reading → 不合格
    字数上限      超限截断 + uncertainty 追加
    物理隔离      mock verdict 后 deep_read 可跑；deep_read 抛异常不影响主结论
    降级          LLM 不可用 → degraded=true 且模板字段完整
    配置告警      议题清单/规则写坏 → 退回默认 + 告警，不崩
    向后兼容      旧 CLI 参数与旧返回字段仍可用

运行：.venv/Scripts/python.exe -m unittest tests.test_deep_read -v
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mylocalai.attention import attention as A# noqa: E402
from mylocalai.attention import deep_read as D# noqa: E402
from mylocalai.attention import deep_read_guard as G# noqa: E402
from mylocalai.llm import llm_client


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

# 合规的模型输出（好例）：各测试在此基础上改一个字段来验证对应护栏
GOOD_OUTPUT = {
    "applicable": True,
    "issue_tags": [{"id": "info_transparency_trust", "confidence": "medium",
                    "evidence_ids": ["e3", "e5", "e7"]}],
    "tensions": [{"a": "信息发布节奏", "b": "公众知情需求",
                  "evidence_ids": ["e3", "e7", "e9"], "level": "L3"}],
    "phenomena": [{"text": "多条发言指向通报时间晚于网络传闻", "level": "L1",
                   "evidence_ids": ["e3", "e5", "e7"], "count": "7/12"}],
    "counter_reading": {"text": "3 条发言认为质疑源于误解", "evidence_ids": ["e11"]},
    "boundary": {"scope": "sample", "sample_size": 12, "proxy_flags": ["y_proxy"]},
    "uncertainty": ["代理阅读量口径为估计值，E 仅同口径可比"],
    "degraded": False,
}

EVIDENCE = [{"text": f"发言{i}: 通报比网友爆料晚了快一天", "stance": "质疑"}
            for i in range(1, 13)]
EVIDENCE_IDS = [f"e{i}" for i in range(1, 13)]
ISSUES = {"issues": [{"id": "info_transparency_trust"}, {"id": "other"}],
          "fallback": "other"}


def _rules(**over):
    params = dict(D.DEFAULT_DEEP_READ_PARAMS)
    params.update(over)
    return {"trigger": D.DEFAULT_TRIGGER_RULES, "deep_read": params,
            "banned_patterns": D.DEFAULT_BANNED_PATTERNS}


def _sanitize(raw, rules=None, ev=EVIDENCE_IDS, issues=ISSUES):
    return G.validate_and_sanitize(raw, issues=issues, evidence_ids=ev,
                                   rules=rules or _rules())


# ===========================================================================
# 1. 触发规则：absolute/quantile × spec/reachable 全组合
# ===========================================================================

class TestTriggerMatrix(unittest.TestCase):
    """absolute/quantile 两模式 × spec/reachable 两档，四种组合各有用例。"""

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

    def test_spec_absolute_high_topic(self):
        self.assertTrue(D.should_deep_read(detail_of(1000, 1e8, 1e6),
                                           profile="spec", rules=self._rules_for()))

    def test_spec_absolute_real_head_topics_all_miss(self):
        """问题 A 的现状：spec 档绝对阈值下 5 个真实头部话题 0/5 触发。"""
        rules = self._rules_for()
        hits = [name for name, x, y, z in TOPICS
                if D.should_deep_read(detail_of(x, y, z), profile="spec", rules=rules)]
        self.assertEqual(hits, [])

    def test_spec_quantile_head_topic_triggers(self):
        """spec 档也能配 quantile：同批最高分触发、最低分不触发。"""
        rules = self._rules_for(spec_mode="quantile")
        batch = [detail_of(x, y, z) for _, x, y, z in TOPICS]
        top = max(batch, key=lambda d: d["score"])
        others = [d for d in batch if d is not top]
        self.assertTrue(D.should_deep_read(others + [top], profile="spec", rules=rules))
        bottom = min(batch, key=lambda d: d["score"])
        rest = [d for d in batch if d is not bottom]
        self.assertFalse(D.should_deep_read(rest + [bottom], profile="spec", rules=rules))

    def test_reachable_quantile_head_topic_triggers(self):
        rules = self._rules_for()
        params = A.PROFILES["reachable"]["params"]
        batch = [detail_of(x, y, z, params) for _, x, y, z in TOPICS]
        top = max(batch, key=lambda d: d["score"])
        others = [d for d in batch if d is not top]
        self.assertTrue(D.should_deep_read(others + [top], profile="reachable", rules=rules))

    def test_reachable_absolute_still_misses(self):
        """reachable 档若仍用 absolute，本批话题仍 0/5：证明要换模式而非只调参。"""
        rules = self._rules_for(reach_mode="absolute")
        params = A.PROFILES["reachable"]["params"]
        hits = [n for n, x, y, z in TOPICS
                if D.should_deep_read(detail_of(x, y, z, params),
                                      profile="reachable", rules=rules)]
        self.assertEqual(hits, [])

    def test_no_profile_matches_legacy(self):
        """未指定 profile 时与改造前的 deep_read_triggered 逐位一致。"""
        for _, x, y, z in TOPICS:
            d = detail_of(x, y, z)
            self.assertEqual(
                D.should_deep_read(d, rules=self._rules_for()),
                A.deep_read_triggered(d["score"], 66.0))

    def test_exactly_threshold_does_not_trigger(self):
        d = {**detail_of(1000, 1e8, 1e6), "score": 66.0}
        self.assertFalse(D.should_deep_read(d, profile="spec", rules=self._rules_for()))

    def test_change_rate_is_reserved(self):
        rules = {"trigger": {"mode": "change_rate", "change_rate_threshold": 0.5},
                 "deep_read": D.DEFAULT_DEEP_READ_PARAMS,
                 "banned_patterns": D.DEFAULT_BANNED_PATTERNS}
        self.assertFalse(D.should_deep_read({"score": 99.0}, rules=rules))


# ===========================================================================
# 2. 信号派生
# ===========================================================================

class TestDeriveSignals(unittest.TestCase):
    """除零保护、proxy_flags、与 attention 公式逐点一致。"""

    def test_all_fields_present(self):
        s = A.derive_signals(1000, 100000000, 1000000)
        for key in ("x", "y", "z", "engagement_ratio", "spread", "resonance",
                    "share_x", "share_y", "share_z", "profile", "proxy_flags"):
            self.assertIn(key, s)

    def test_ratios_match_spec(self):
        s = A.derive_signals(1000, 100000000, 1000000)
        self.assertEqual(s["engagement_ratio"], 0.01)
        self.assertEqual(s["spread"], 1000.0)
        self.assertEqual(s["resonance"], 1000.0)

    def test_zero_denominators_protected(self):
        s = A.derive_signals(0, 0, 0)
        self.assertEqual((s["engagement_ratio"], s["resonance"]), (0.0, 0.0))
        self.assertEqual((s["share_x"], s["share_y"], s["share_z"]), (0.0, 0.0, 0.0))
        s2 = A.derive_signals(0, 100000000, 1000000)
        self.assertEqual(s2["resonance"], 0.0)
        self.assertEqual(s2["engagement_ratio"], 0.01)

    def test_shares_match_attention_formula(self):
        for _, x, y, z in TOPICS:
            s = A.derive_signals(x, y, z)
            c = A.contributions(x, y, z)
            self.assertAlmostEqual(s["share_x"], c["term_x"] / c["S"], places=15)
            self.assertAlmostEqual(s["share_y"], c["term_y"] / c["S"], places=15)
            self.assertAlmostEqual(s["share_z"], c["term_z"] / c["S"], places=15)

    def test_proxy_flags_deduped(self):
        s = A.derive_signals(1, 1, 1, proxy_flags=["y_proxy", " y_proxy ", "", "z_proxy"])
        self.assertEqual(s["proxy_flags"], ["y_proxy", "z_proxy"])

    def test_profile_echoed(self):
        self.assertEqual(A.derive_signals(1, 1, 1, profile="reachable")["profile"], "reachable")
        self.assertEqual(A.derive_signals(1, 1, 1)["profile"], A.DEFAULT_PROFILE)

    def test_invalid_input_raises(self):
        for bad in ((-1, 1, 1), (True, 1, 1), (float("nan"), 1, 1)):
            with self.assertRaises(A.AttentionInputError):
                A.derive_signals(*bad)


# ===========================================================================
# 3. 议题封闭性
# ===========================================================================

class TestIssueClosedSet(unittest.TestCase):
    """清单外 id → other/low；other 也不允许 → 丢弃。"""

    def test_unknown_id_becomes_other_with_low_confidence(self):
        raw = {**GOOD_OUTPUT,
               "issue_tags": [{"id": "not_in_list", "confidence": "high",
                               "evidence_ids": ["e3"]}]}
        r = _sanitize(raw)
        self.assertEqual([t["id"] for t in r["data"]["issue_tags"]], ["other"])
        self.assertEqual(r["data"]["issue_tags"][0]["confidence"], "low")
        self.assertTrue(any("不在封闭清单" in w for w in r["warnings"]))

    def test_unknown_id_dropped_when_other_not_allowed(self):
        raw = {**GOOD_OUTPUT,
               "issue_tags": [{"id": "not_in_list", "confidence": "high",
                               "evidence_ids": ["e3"]}]}
        with self.assertRaises(G.GuardRejected):
            _sanitize(raw, rules=_rules(allow_other_tag=False))

    def test_all_tags_dropped_is_rejected(self):
        raw = {**GOOD_OUTPUT,
               "issue_tags": [{"id": "not_in_list", "confidence": "high",
                               "evidence_ids": ["e3", "e5"]}]}
        with self.assertRaises(G.GuardRejected):
            _sanitize(raw, rules=_rules(allow_other_tag=False))

    def test_max_issue_tags_respected(self):
        raw = {**GOOD_OUTPUT, "issue_tags": [
            {"id": f"t{i}", "confidence": "high", "evidence_ids": ["e3"]} for i in range(5)]}
        r = _sanitize(raw, issues={"issues": [{"id": f"t{i}"} for i in range(5)],
                                   "fallback": "other"})
        self.assertEqual(len(r["data"]["issue_tags"]), 3)   # 默认 max_issue_tags=3

    def test_illegal_confidence_downgraded(self):
        raw = {**GOOD_OUTPUT,
               "issue_tags": [{"id": "info_transparency_trust", "confidence": "很高",
                               "evidence_ids": ["e3"]}]}
        r = _sanitize(raw)
        self.assertEqual(r["data"]["issue_tags"][0]["confidence"], "low")

    def test_default_issues_contain_required_two(self):
        ids = {i["id"] for i in D.DEFAULT_ISSUES["issues"]}
        self.assertIn("info_transparency_trust", ids)
        self.assertIn("other", ids)


# ===========================================================================
# 4. 证据覆盖
# ===========================================================================

class TestEvidenceCoverage(unittest.TestCase):
    """无 evidence_id 的条目被丢；无效 id 被剔除。"""

    def test_entry_without_evidence_dropped(self):
        raw = {**GOOD_OUTPUT,
               "issue_tags": [{"id": "info_transparency_trust", "confidence": "high",
                               "evidence_ids": []}]}
        with self.assertRaises(G.GuardRejected):
            _sanitize(raw)

    def test_invalid_evidence_ids_filtered(self):
        raw = {**GOOD_OUTPUT,
               "issue_tags": [{"id": "info_transparency_trust", "confidence": "high",
                               "evidence_ids": ["e3", "e999", "e1000"]}]}
        r = _sanitize(raw)
        self.assertEqual(r["data"]["issue_tags"][0]["evidence_ids"], ["e3"])
        self.assertTrue(any("e999" in d for d in r["guard_dropped"]))

    def test_phenomenon_without_evidence_dropped(self):
        raw = {**GOOD_OUTPUT,
               "phenomena": [{"text": "无证据现象", "level": "L1", "evidence_ids": []}]}
        r = _sanitize(raw)
        self.assertEqual(r["data"]["phenomena"], [])
        self.assertTrue(r["degraded"])

    def test_tension_without_evidence_dropped(self):
        raw = {**GOOD_OUTPUT,
               "tensions": [{"a": "A", "b": "B", "evidence_ids": [], "level": "L1"}]}
        r = _sanitize(raw)
        self.assertEqual(r["data"]["tensions"], [])

    def test_valid_evidence_set_none_skips_check(self):
        """调用方没有证据集时（ev=None）不校验，保持宽松兼容。"""
        r = _sanitize(GOOD_OUTPUT, ev=None)
        self.assertEqual(r["data"]["phenomena"][0]["evidence_ids"], ["e3", "e5", "e7"])

    def test_all_dropped_marks_degraded(self):
        raw = {**GOOD_OUTPUT,
               "phenomena": [{"text": "x", "level": "L1", "evidence_ids": ["e999"]}]}
        r = _sanitize(raw)
        self.assertTrue(r["degraded"])
        self.assertTrue(r["guard_dropped"])


# ===========================================================================
# 5. L3 挂靠
# ===========================================================================

class TestL3Support(unittest.TestCase):
    """全篇无 L1/L2 时 L3 不得单独存在。"""

    def test_l3_alone_is_downgraded(self):
        raw = {**GOOD_OUTPUT,
               "tensions": [{"a": "A", "b": "B", "evidence_ids": ["e3"], "level": "L3"}],
               "phenomena": [{"text": "推断现象", "level": "L3",
                              "evidence_ids": ["e3"], "count": "3/12"}]}
        r = _sanitize(raw)
        self.assertEqual(r["data"]["tensions"], [])
        self.assertEqual(r["data"]["phenomena"], [])
        # 降级成显式不确定性，判断保留但不当结论
        self.assertTrue(any("缺少可直接引用的具体证据" in u
                            for u in r["data"]["uncertainty"]))
        self.assertTrue(r["degraded"])

    def test_l3_with_l1_support_survives(self):
        r = _sanitize(GOOD_OUTPUT)   # tensions 是 L3，phenomena 是 L1
        self.assertEqual(len(r["data"]["tensions"]), 1)
        self.assertEqual(r["data"]["tensions"][0]["level"], "L3")
        self.assertFalse(r["degraded"])

    def test_l2_also_supports_l3(self):
        raw = {**GOOD_OUTPUT,
               "tensions": [{"a": "A", "b": "B", "evidence_ids": ["e3"], "level": "L3"}],
               "phenomena": [{"text": "检索证据现象", "level": "L2",
                              "evidence_ids": ["e3"], "count": "5/12"}]}
        r = _sanitize(raw)
        self.assertEqual(len(r["data"]["tensions"]), 1)

    def test_illegal_level_falls_back_to_l1(self):
        raw = {**GOOD_OUTPUT,
               "phenomena": [{"text": "x", "level": "L9", "evidence_ids": ["e3"]}]}
        r = _sanitize(raw)
        self.assertEqual(r["data"]["phenomena"][0]["level"], "L1")


# ===========================================================================
# 6. 禁词 / 禁归因（红队）
# ===========================================================================

class TestBannedPatterns(unittest.TestCase):
    """红队集：具体主体归因、样本推普遍、价值判断、预测。"""

    def _run(self, text, field="phenomena"):
        raw = json.loads(json.dumps(GOOD_OUTPUT, ensure_ascii=False))
        if field == "phenomena":
            raw["phenomena"] = [{"text": text, "level": "L1", "evidence_ids": ["e3"]}]
        else:
            raw["tensions"] = [{"a": text, "b": "B", "evidence_ids": ["e3"], "level": "L1"}]
        return _sanitize(raw)

    def test_subject_blame_dropped(self):
        r = self._run("某部门肯定在瞒，公众极度不满")
        self.assertEqual(r["data"]["phenomena"], [])
        self.assertTrue(any("subject_blame" in d for d in r["guard_dropped"]))

    def test_subject_blame_in_tension_dropped(self):
        """张力端写成"主体 + 负面谓词"必须整段丢弃。"""
        r = self._run("某部门肯定在瞒", field="tensions")
        self.assertEqual(r["data"]["tensions"], [])

    def test_neutral_subject_enumeration_allowed(self):
        """只列不评的中性列举是允许的（提示词第 6 条明确鼓励），不能误杀。"""
        r = self._run("有关部门", field="tensions")
        self.assertEqual(len(r["data"]["tensions"]), 1)

    def test_universal_claim_dropped(self):
        for text in ("全社会都不满", "这是普遍现象", "必然导致更大矛盾", "所有人都在质疑"):
            r = self._run(text)
            self.assertEqual(r["data"]["phenomena"], [],
                             f"禁词未拦住：{text}")

    def test_value_judgment_dropped(self):
        for text in ("真的很可悲", "这种想法太愚昧", "属实无知", "表现十分麻木"):
            r = self._run(text)
            self.assertEqual(r["data"]["phenomena"], [], f"禁词未拦住：{text}")

    def test_prediction_dropped(self):
        for text in ("长此以往将引发更大矛盾", "迟早会爆发", "必将引发信任危机"):
            r = self._run(text)
            self.assertEqual(r["data"]["phenomena"], [], f"禁词未拦住：{text}")

    def test_banned_in_counter_reading_dropped(self):
        """counter_reading 命中禁词 → 置空；规则不强制时结果里就是 None。"""
        raw = {**GOOD_OUTPUT,
               "counter_reading": {"text": "某部门肯定在瞒", "evidence_ids": ["e3"]}}
        r = _sanitize(raw, rules=_rules(require_counter_reading=False))
        self.assertIsNone(r["data"]["counter_reading"])
        self.assertTrue(r["degraded"])

    def test_counter_reading_banned_and_required_rejects(self):
        """counter_reading 命中禁词 + 规则要求必填 → 整体不合格（走模板回退）。"""
        raw = {**GOOD_OUTPUT,
               "counter_reading": {"text": "长此以往将引发更大矛盾", "evidence_ids": ["e3"]}}
        with self.assertRaises(G.GuardRejected):
            _sanitize(raw)

    def test_output_never_contains_subject_attribution(self):
        """红队集整体过一遍：输出文本里不得出现具体主体归因。"""
        redteam = ["某部门肯定在瞒", "官方在刻意压制", "这家公司在造假",
                   "某地人都在闹", "全社会都不满", "长此以往将引发更大矛盾"]
        for text in redteam:
            r = self._run(text)
            body = r["data"]["body"]
            self.assertNotIn(text, body, f"输出里仍有禁词内容：{text}")


# ===========================================================================
# 7. 出口 applicable=false
# ===========================================================================

class TestApplicableExit(unittest.TestCase):
    """纯情绪/纯事实/证据不足 → applicable=false，不得为"有输出"而编。"""

    def test_applicable_false_passes_through(self):
        r = _sanitize({"applicable": False, "reason": "纯情绪宣泄，无社会议题可分析"})
        self.assertFalse(r["applicable"])
        self.assertEqual(r["reason"], "纯情绪宣泄，无社会议题可分析")
        self.assertEqual(r["data"], {})

    def test_applicable_false_default_reason(self):
        r = _sanitize({"applicable": False})
        self.assertFalse(r["applicable"])
        self.assertTrue(r["reason"])

    def test_deep_read_returns_not_applicable(self):
        with mock.patch("mylocalai.llm.llm_client.chat_text",
                        return_value=(json.dumps({"applicable": False,
                                                  "reason": "纯情绪宣泄"},
                                                 ensure_ascii=False), "")):
            r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True,
                            evidence=EVIDENCE)
        self.assertTrue(r["triggered"])
        self.assertFalse(r["applicable"])
        self.assertEqual(r["text"], "")
        self.assertEqual(r["reason"], "纯情绪宣泄")

    def test_free_text_output_is_not_trusted(self):
        """模型返回自由文本（非 JSON）→ 判不合格 → 走模板回退。"""
        with mock.patch("mylocalai.llm.llm_client.chat_text",
                        return_value=("这反映了社会信任危机。", "")):
            r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True,
                            evidence=EVIDENCE)
        self.assertEqual(r["source"], "template")
        self.assertTrue(r["degraded"])
        self.assertTrue(any("未通过护栏" in w for w in r["warnings"]))


# ===========================================================================
# 8. 反方必填 + 字数上限
# ===========================================================================

class TestCounterReadingAndWordLimit(unittest.TestCase):
    def test_missing_counter_reading_rejected(self):
        raw = {k: v for k, v in GOOD_OUTPUT.items() if k != "counter_reading"}
        with self.assertRaises(G.GuardRejected):
            _sanitize(raw)

    def test_empty_counter_reading_rejected(self):
        raw = {**GOOD_OUTPUT, "counter_reading": {"text": "", "evidence_ids": ["e3"]}}
        with self.assertRaises(G.GuardRejected):
            _sanitize(raw)

    def test_counter_reading_not_required_when_disabled(self):
        raw = {k: v for k, v in GOOD_OUTPUT.items() if k != "counter_reading"}
        r = _sanitize(raw, rules=_rules(require_counter_reading=False))
        self.assertIsNone(r["data"]["counter_reading"])

    def test_no_counter_evidence_found_is_allowed(self):
        """模型明说"未发现反方证据"也算给了 counter_reading（有 text 即可）。"""
        raw = {**GOOD_OUTPUT,
               "counter_reading": {"text": "未发现反方证据：本样本 12 条发言均指向同一方向",
                                   "evidence_ids": []}}
        r = _sanitize(raw)
        self.assertIn("未发现反方证据", r["data"]["counter_reading"]["text"])

    def test_word_limit_truncates_and_notes(self):
        raw = {**GOOD_OUTPUT,
               "phenomena": [{"text": "超" * 500, "level": "L1",
                              "evidence_ids": ["e3"], "count": "1/12"}]}
        r = _sanitize(raw, rules=_rules(max_words=60))
        self.assertTrue(r["data"]["truncated"])
        self.assertLessEqual(len(r["data"]["body"]), 60)
        self.assertIn("输出因长度上限截断", r["data"]["uncertainty"])

    def test_under_word_limit_not_truncated(self):
        r = _sanitize(GOOD_OUTPUT, rules=_rules(max_words=300))
        self.assertFalse(r["data"]["truncated"])
        self.assertNotIn("输出因长度上限截断", r["data"]["uncertainty"])


# ===========================================================================
# 9. 端到端（mock LLM）
# ===========================================================================

class TestEndToEnd(unittest.TestCase):
    """给定固定 raw_output，sanitize 后字段与预期一致。"""

    def test_good_output_end_to_end(self):
        with mock.patch("mylocalai.llm.llm_client.chat_text",
                        return_value=(json.dumps(GOOD_OUTPUT, ensure_ascii=False), "")):
            r = D.deep_read("某地通报事件", detail_of(1000, 1e8, 1e6),
                            use_llm=True, evidence=EVIDENCE)
        self.assertEqual(r["source"], "llm")
        self.assertTrue(r["applicable"])
        self.assertFalse(r["degraded"])
        self.assertEqual([t["id"] for t in r["result"]["issue_tags"]],
                         ["info_transparency_trust"])
        self.assertEqual(r["result"]["tensions"][0]["level"], "L3")
        self.assertEqual(r["result"]["phenomena"][0]["count"], "7/12")
        self.assertIn("信息发布节奏", r["text"])
        self.assertIn("反方", r["text"])
        self.assertEqual(r["guard_dropped"], [])

    def test_prompt_contains_evidence_and_issues(self):
        seen = {}

        def cap(system, user, **kw):
            seen["system"], seen["user"] = system, user
            return json.dumps(GOOD_OUTPUT, ensure_ascii=False), ""

        with mock.patch("mylocalai.llm.llm_client.chat_text", side_effect=cap):
            D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True, evidence=EVIDENCE)
        # 编号证据与议题清单必须进 prompt（受控生成的地基）
        self.assertIn("e1:", seen["user"])
        self.assertIn("e12:", seen["user"])
        self.assertIn("info_transparency_trust", seen["user"])
        self.assertIn("不得自创", seen["system"])

    def test_signals_in_prompt(self):
        seen = {}

        def cap(system, user, **kw):
            seen["user"] = user
            return json.dumps(GOOD_OUTPUT, ensure_ascii=False), ""

        with mock.patch("mylocalai.llm.llm_client.chat_text", side_effect=cap):
            D.deep_read("t", {**detail_of(1000, 1e8, 1e6), "estimated": True},
                        use_llm=True, evidence=EVIDENCE)
        for key in ("engagement_ratio", "spread", "resonance", "share_x",
                    "share_y", "share_z", "profile", "proxy_flags"):
            self.assertIn(key, seen["user"])
        self.assertIn("y_proxy", seen["user"])

    def test_opinions_signals_in_prompt(self):
        seen = {}

        def cap(system, user, **kw):
            seen["user"] = user
            return json.dumps(GOOD_OUTPUT, ensure_ascii=False), ""

        with mock.patch("mylocalai.llm.llm_client.chat_text", side_effect=cap):
            D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=True, evidence=EVIDENCE,
                        opinions_signals={"appeals": ["要求公开完整时间线"],
                                          "attribution_targets": ["信息发布方"],
                                          "action_tendencies": ["持续关注"]})
        self.assertIn("核心诉求", seen["user"])
        self.assertIn("要求公开完整时间线", seen["user"])

    def test_no_evidence_prompt_asks_for_not_applicable(self):
        seen = {}

        def cap(system, user, **kw):
            seen["user"] = user
            return json.dumps(GOOD_OUTPUT, ensure_ascii=False), ""

        with mock.patch("mylocalai.llm.llm_client.chat_text", side_effect=cap):
            D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=True, evidence=None)
        self.assertIn("未提供编号证据", seen["user"])


# ===========================================================================
# 10. 物理隔离
# ===========================================================================

class TestPhysicalIsolation(unittest.TestCase):
    """深读不得 import verdict；不得影响主结论；异常不外泄。"""

    def test_deep_read_does_not_import_verdict(self):
        """源码里不得 import verdict 相关模块（AST 检查，注释/文档串不算）。"""
        import ast
        tree = ast.parse((ROOT / "src" / "mylocalai" / "attention" / "deep_read.py").read_text(encoding="utf-8"))
        imported: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for name in imported:
            self.assertNotIn("verdict", name,
                             f"deep_read.py 不得 import verdict 相关模块，实际 import 了 {name}")
            self.assertFalse(name.startswith("mylocalai.rumor."),
                             f"deep_read.py 不得 import rumor_agent 的任何模块，实际 import 了 {name}")

    def test_deep_read_runs_with_verdict_mocked_away(self):
        """mock 掉 verdict 模块后 deep_read 仍能跑（证明不依赖它）。"""
        import sys as _sys
        saved = _sys.modules.pop("mylocalai.rumor.verdict", None)
        try:
            with mock.patch("mylocalai.llm.llm_client.chat_text",
                            return_value=("", "无密钥")):
                r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=True)
            self.assertTrue(r["triggered"])
        finally:
            if saved is not None:
                _sys.modules["mylocalai.rumor.verdict"] = saved

    def test_deep_read_uses_independent_cache_namespace(self):
        """深读必须用自己的缓存分区，且该分区与主分析的分区算出的键不同。

        这里验的是**真实的缓存键**，不是"stats 返回了 dict"那种空断言：
        同一段 system + user，深读的键必须与主分析（无分区名）的键不同，
        否则两边会互相读到对方的结果。
        """
        from mylocalai.llm import llm_client
        prompts, _ = D.load_prompts()
        system = prompts["system"] + "\n\n" + prompts["fewshot"]
        user = "同一段用户文本"
        deep_key = llm_client._cache_key(system, user, 0.3, 1600, False,
                                        D.CACHE_NAMESPACE)
        main_key = llm_client._cache_key(system, user, 0.3, 1600, False, "")
        self.assertNotEqual(deep_key, main_key,
                            "深读与主分析的缓存键相同，两边会互相串结果")
        self.assertTrue(deep_key and main_key)

    def test_deep_read_actually_passes_namespace_to_llm(self):
        """深读调用 chat_text 时必须把分区名传下去（光有常量不算隔离）。"""
        seen = {}

        def fake(system, user, **kw):
            seen["namespace"] = kw.get("namespace", "")
            return json.dumps(GOOD_OUTPUT, ensure_ascii=False), ""

        with mock.patch("mylocalai.llm.llm_client.chat_text", side_effect=fake):
            D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True,
                        evidence=EVIDENCE)
        self.assertEqual(seen.get("namespace"), D.CACHE_NAMESPACE,
                         "深读没有把缓存分区名传给 chat_text，隔离没有真正生效")

    def test_deep_read_cache_write_does_not_leak_to_main(self):
        """端到端：深读写进缓存的结果，主分析链路读不到。

        做法：让深读真的走一次 `llm_cache.call`（不 mock chat_text，而是 mock 更底层的
        `_chat_raw_providers`，这样缓存链路是真实执行的），再用主分析的口径
        （无分区名）去查**同一段** (system, user) —— 必须查不到。
        """
        from mylocalai.llm import llm_cache
        prompts, _ = D.load_prompts()
        system = prompts["system"] + "\n\n" + prompts["fewshot"]
        calls = {"n": 0}
        captured = {}

        def fake_providers(sys_p, user_p, *a, **kw):
            calls["n"] += 1
            captured["system"], captured["user"] = sys_p, user_p
            return json.dumps(GOOD_OUTPUT, ensure_ascii=False), ""

        llm_cache.clear()
        try:
            with mock.patch("mylocalai.llm.llm_client._chat_raw_providers",
                            side_effect=fake_providers):
                # 第一次：深读，真实走缓存写入
                D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True,
                            evidence=EVIDENCE)
            self.assertEqual(calls["n"], 1, "深读应该真的调了一次上游")
            self.assertEqual(captured["system"], system, "传给上游的 system 与预期不符")

            # 用**实际传入的同一段** (system, user) 分别按两种分区名算键
            main_key = llm_client._cache_key(captured["system"], captured["user"],
                                             0.3, 1600, False, "")
            deep_key = llm_client._cache_key(captured["system"], captured["user"],
                                             0.3, 1600, False, D.CACHE_NAMESPACE)
            # 主分析口径（无分区名）必须 miss：深读的结果污染不到主分析
            hit, _ = llm_cache.get(main_key)
            self.assertFalse(hit, "深读的缓存结果被主分析读到了，隔离失效")
            # 深读自己的分区名必须 hit：缓存确实写进去了，隔离不是靠"不缓存"实现的
            hit2, value = llm_cache.get(deep_key)
            self.assertTrue(hit2, "深读自己的分区没有命中，缓存白写了")
            self.assertEqual(value[0], json.dumps(GOOD_OUTPUT, ensure_ascii=False))
        finally:
            llm_cache.clear()

    def test_cache_namespace_empty_keeps_legacy_key(self):
        """分区名为空时缓存键必须与加参数之前一致（不能悄悄换掉全部缓存键）。"""
        import hashlib
        from mylocalai.llm import llm_client
        from mylocalai.util import text_safety
        blob = json.dumps([llm_client.get_provider(), llm_client.get_base_url(),
                           llm_client.get_model(), "S", "U", 0.3, 1600, False],
                          ensure_ascii=False)
        legacy = hashlib.sha256(text_safety.sanitize_text(blob)
                                .encode("utf-8", "replace")).hexdigest()
        self.assertEqual(llm_client._cache_key("S", "U", 0.3, 1600, False, ""), legacy)
        self.assertEqual(llm_client._cache_key("S", "U", 0.3, 1600, False), legacy)

    def test_cache_namespace_changes_key(self):
        """分区名必须真的改变键（否则隔离是空话）。"""
        from mylocalai.llm import llm_client
        base = llm_client._cache_key("S", "U", 0.3, 1600, False)
        self.assertNotEqual(base, llm_client._cache_key("S", "U", 0.3, 1600, False, "ns_a"))
        self.assertNotEqual(llm_client._cache_key("S", "U", 0.3, 1600, False, "ns_a"),
                            llm_client._cache_key("S", "U", 0.3, 1600, False, "ns_b"))

    def test_cache_namespace_survives_surrogates(self):
        """分区名参与键计算时不能因孤立代理项抛异常（键算不出就静默失去隔离）。"""
        from mylocalai.llm import llm_client
        key = llm_client._cache_key("S", "ok\ud800bad", 0.3, 1600, False,
                                    D.CACHE_NAMESPACE)
        self.assertIsInstance(key, str)
        self.assertTrue(key)

    def test_deep_read_exception_does_not_affect_main_conclusion(self):
        """深读内部炸掉时，主分析结论（score/band）一个字节都不能变。"""
        detail = detail_of(1000, 1e8, 1e6)
        before_score, before_band = detail["score"], detail["band"]
        with mock.patch("mylocalai.llm.llm_client.chat_text",
                        side_effect=RuntimeError("上游炸了")):
            r = D.deep_read("t", detail, use_llm=True, evidence=EVIDENCE)
        self.assertEqual(detail["score"], before_score)
        self.assertEqual(detail["band"], before_band)
        self.assertEqual(r["source"], "template")   # 深读自己降级，主流程无感

    def test_deep_read_never_raises_on_broken_detail(self):
        """detail 缺字段/类型怪也不许抛（深读是附加信息，不能拖垮主流程）。"""
        for bad in ({}, {"score": "abc"}, {"score": None}, {"x": "z"}):
            r = D.deep_read("t", bad, use_llm=False)
            self.assertIn("triggered", r)


# ===========================================================================
# 11. 降级
# ===========================================================================

class TestDegradation(unittest.TestCase):
    """LLM 不可用 → degraded=true 且模板字段完整。"""

    def test_llm_unavailable_degrades_with_reason(self):
        with mock.patch("mylocalai.llm.llm_client.chat_text",
                        return_value=("", "HTTP 401: Authentication Fails")):
            r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True,
                            evidence=EVIDENCE)
        self.assertEqual(r["source"], "template")
        self.assertTrue(r["degraded"])
        self.assertIn("401", r["degraded_reason"])
        self.assertTrue(any("大模型解读不可用" in w for w in r["warnings"]))

    def test_llm_exception_degrades(self):
        with mock.patch("mylocalai.llm.llm_client.chat_text",
                        side_effect=RuntimeError("网络炸了")):
            r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=True, evidence=EVIDENCE)
        self.assertEqual(r["source"], "template")
        self.assertTrue(r["degraded"])

    def test_template_fallback_fields_complete(self):
        """模板回退也必须给全字段，前端才能统一渲染。"""
        r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=False)
        for key in ("triggered", "text", "source", "degraded", "degraded_reason",
                    "warnings", "signals", "applicable", "result", "guard_dropped"):
            self.assertIn(key, r)
        self.assertTrue(r["triggered"])
        self.assertTrue(r["text"])
        self.assertTrue(r["degraded"])
        self.assertTrue(r["applicable"])
        self.assertEqual(r["guard_dropped"], [])

    def test_template_text_keeps_three_sections(self):
        """模板仍是旧三段式框架提示（向后兼容：旧字段不消失）。"""
        r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=False)
        self.assertIn("为什么人们会关注", r["text"])
        self.assertIn("反映了什么社会问题", r["text"])
        self.assertIn("担忧和思想", r["text"])
        self.assertIn("E = 69.56", r["text"])

    def test_guard_rejection_reason_recorded(self):
        """护栏全丢弃也要记录是"护栏丢弃"而不是"LLM 失败"。"""
        bad = json.dumps({"applicable": True,
                          "issue_tags": [{"id": "info_transparency_trust",
                                          "confidence": "high",
                                          "evidence_ids": ["e1"]}],
                          "tensions": [{"a": "全社会", "b": "B",
                                        "evidence_ids": ["e1"], "level": "L1"}],
                          "phenomena": [{"text": "很可悲", "level": "L1",
                                         "evidence_ids": ["e1"]}],
                          "counter_reading": {"text": "迟早爆发", "evidence_ids": ["e1"]}},
                         ensure_ascii=False)
        with mock.patch("mylocalai.llm.llm_client.chat_text", return_value=(bad, "")):
            r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=True, evidence=EVIDENCE)
        self.assertEqual(r["source"], "template")
        self.assertIn("护栏", r["degraded_reason"])


# ===========================================================================
# 12. 配置告警
# ===========================================================================

class TestConfigWarnings(unittest.TestCase):
    """议题清单/规则/prompt 写坏 → 退回默认 + 告警，不崩。"""

    def test_broken_issues_json_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("{not json", encoding="utf-8")
            issues, warnings = D.load_issues(str(p))
        ids = {i["id"] for i in issues["issues"]}
        self.assertIn("info_transparency_trust", ids)
        self.assertIn("other", ids)
        self.assertTrue(any("退回" in w or "内置" in w for w in warnings))

    def test_missing_issues_file_falls_back(self):
        issues, warnings = D.load_issues("/nonexistent/society_issues.json")
        self.assertIn("other", {i["id"] for i in issues["issues"]})
        self.assertTrue(warnings)

    def test_empty_issues_list_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({"issues": []}), encoding="utf-8")
            issues, warnings = D.load_issues(str(p))
        self.assertIn("other", {i["id"] for i in issues["issues"]})
        self.assertTrue(any("issues" in w and "内置" in w for w in warnings),
                        f"空 issues 数组必须留告警，实际：{warnings}")

    def test_malformed_issue_entries_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({"issues": [
                {"id": "", "label": "无 id"},                      # 缺 id
                {"id": "no_label"},                                # 缺 label
                "不是对象",                                          # 非对象
                {"id": "good_one", "label": "好议题",
                 "tension_a_hint": "A", "tension_b_hint": "B"},
            ]}), encoding="utf-8")
            issues, warnings = D.load_issues(str(p))
        ids = {i["id"] for i in issues["issues"]}
        self.assertIn("good_one", ids)
        self.assertNotIn("", ids)
        self.assertNotIn("no_label", ids)

    def test_duplicate_issue_ids_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({"issues": [
                {"id": "dup", "label": "一"}, {"id": "dup", "label": "二"}]}),
                encoding="utf-8")
            issues, warnings = D.load_issues(str(p))
        self.assertEqual(len([i for i in issues["issues"] if i["id"] == "dup"]), 1)
        self.assertTrue(any("重复" in w for w in warnings))

    def test_broken_issues_do_not_break_deep_read(self):
        """议题清单写坏时深读仍要出文本（用内置最小默认），不抛异常。"""
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("[[[", encoding="utf-8")
            with mock.patch.object(D, "ISSUES_PATH", bad):
                with mock.patch("mylocalai.llm.llm_client.chat_text",
                                return_value=("", "无密钥")):
                    r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=True)
        self.assertTrue(r["triggered"])
        self.assertTrue(r["text"])
        self.assertTrue(r["degraded"])

    def test_broken_prompts_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("{oops", encoding="utf-8")
            prompts, warnings = D.load_prompts(str(p))
        self.assertIn("不得自创", prompts["system"])
        self.assertTrue(warnings)

    def test_broken_rules_do_not_break_deep_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("[[[", encoding="utf-8")
            r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=False,
                            rules_path=str(bad))
        self.assertTrue(r["triggered"])
        self.assertTrue(r["text"])
        self.assertTrue(r["warnings"])

    def test_prompts_json_has_required_sections(self):
        """外置 prompts.json 必须有两段且含关键约束（防漂移）。"""
        prompts, warnings = D.load_prompts()
        self.assertEqual(warnings, [])
        self.assertIn("不得自创", prompts["system"])
        self.assertIn("applicable", prompts["system"])
        self.assertIn("counter_reading", prompts["system"])
        self.assertIn("这反映了社会信任危机", prompts["fewshot"])
        self.assertIn('"applicable": true', prompts["fewshot"])

    def test_society_issues_json_structure(self):
        """外置 society_issues.json 必须结构合规（30-50 项、字段齐全）。"""
        data = json.loads((ROOT / "src" / "mylocalai" / "attention" / "society_issues.json")
                          .read_text(encoding="utf-8"))
        issues = data["issues"]
        self.assertGreaterEqual(len(issues), 30)
        self.assertLessEqual(len(issues), 50)
        ids = [i["id"] for i in issues]
        self.assertEqual(len(ids), len(set(ids)), "议题 id 必须唯一")
        for item in issues:
            self.assertTrue(item.get("label"), f"{item.get('id')} 缺 label")
            self.assertTrue(item.get("tension_a_hint"), f"{item['id']} 缺 tension_a_hint")
            self.assertTrue(item.get("tension_b_hint"), f"{item['id']} 缺 tension_b_hint")
            self.assertGreaterEqual(len(item.get("include") or []), 2,
                                    f"{item['id']} 的 include 少于 2 条")
            self.assertGreaterEqual(len(item.get("exclude") or []), 2,
                                    f"{item['id']} 的 exclude 少于 2 条")
        self.assertIn("info_transparency_trust", ids)
        self.assertIn("other", ids)

    def test_deep_read_rules_json_matches_defaults(self):
        """外置 deep_read_rules.json 的生效口径必须与模块默认一致（防漂移）。"""
        data = json.loads(D.DEEP_READ_RULES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["trigger"]["mode"], D.DEFAULT_TRIGGER_RULES["mode"])
        self.assertEqual(data["trigger"]["absolute_threshold"],
                         D.DEFAULT_TRIGGER_RULES["absolute_threshold"])
        params = {k: v for k, v in data["deep_read"].items() if not k.startswith("_")}
        self.assertEqual(params, D.DEFAULT_DEEP_READ_PARAMS)


# ===========================================================================
# 13. 向后兼容
# ===========================================================================

class TestBackwardCompatibility(unittest.TestCase):
    """旧 CLI 参数与旧返回字段仍可用。"""

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

    def test_profile_flag_accepted(self):
        rc, out, _ = self.run_main(
            ["--query", "t", "--x", "30", "--y", "48316000", "--z", "149",
             "--no-llm", "--profile", "reachable"])
        self.assertEqual(rc, 0)
        self.assertIn("reachable", out)
        self.assertIn("触发深读：是", out)

    def test_json_flag_outputs_new_schema(self):
        rc, out, _ = self.run_main(
            ["--query", "t", "--x", "1000", "--y", "100000000", "--z", "1000000",
             "--no-llm", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        # 旧字段
        for key in ("triggered", "text", "source", "degraded", "warnings",
                    "signals", "profile", "trigger_mode"):
            self.assertIn(key, data)
        # 新字段
        for key in ("applicable", "result", "guard_dropped", "degraded_reason"):
            self.assertIn(key, data)
        self.assertTrue(data["triggered"])
        self.assertTrue(data["applicable"])

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

    def test_score_untouched_by_deep_read(self):
        d = detail_of(1000, 1e8, 1e6)
        before = dict(d)
        D.deep_read("t", d, use_llm=False, batch=[d], profile="spec")
        self.assertEqual(d, before)

    def test_old_module_constants_still_exist(self):
        """旧常量与旧文本口径加载函数必须还在（既有调用方按名字取）。"""
        self.assertTrue(D.DEEP_READ_SYSTEM_PROMPT)          # 新版受控生成提示词
        self.assertTrue(D._LEGACY_DEEP_READ_SYSTEM_PROMPT)  # 旧版三段式
        self.assertTrue(D.DEEP_READ_TEMPLATE)
        self.assertTrue(D.RULES_PATH.name == "attn_deep_read.json")
        rules, warnings = D._load_rules()
        self.assertEqual(warnings, [])
        self.assertTrue(rules["system_prompt"])
        self.assertTrue(rules["template"])

    def test_attn_deep_read_json_matches_builtin(self):
        """旧文本口径 JSON 必须与内置旧版常量逐字一致（防漂移）。"""
        data = json.loads(D.RULES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["system_prompt"], D._LEGACY_DEEP_READ_SYSTEM_PROMPT)
        self.assertEqual(data["template"], D.DEEP_READ_TEMPLATE)

    def test_legacy_prompt_constant_is_the_old_three_sections(self):
        """旧常量必须仍是三段式（对外行为不变），且与新版受控提示词不是同一个。"""
        self.assertIn("为什么人们会关注", D._LEGACY_DEEP_READ_SYSTEM_PROMPT)
        self.assertIn("反映了什么社会问题", D._LEGACY_DEEP_READ_SYSTEM_PROMPT)
        self.assertNotEqual(D._LEGACY_DEEP_READ_SYSTEM_PROMPT, D.DEEP_READ_SYSTEM_PROMPT)
        self.assertIn("不得自创", D.DEEP_READ_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
