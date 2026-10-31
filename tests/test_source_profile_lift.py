# -*- coding: utf-8 -*-
"""信源画像反哺抬分测试（L4 参谋 → 引擎注入，最小侵入）。

锁定三件事：
  1. 逐位守卫——collect_evidence 不传 source_profile / 传 None / 传 {} 三者输出
     全字典相等：不传画像时引擎输出与没有此参数时逐位一致（变异测试的靶子）；
  2. 抬分路径——非可靠档位域名带"反驳"条目 + 画像 → reliable_refute+1、
     profile_refute_domains 含该域名、other_refute 相应变化、official 口径不变；
     画像域名无当前反驳条目 / 本就在可靠档位时不重复计数；
  3. 反哺真实进结论——画像并入后 refute_weight 跨过单来源阈值，verdict 标签
     抬到疑似/高风险；以及 AnalysisState / RumorAgent 的 source_profile 透传链。

全程离线：搜索/抓取/立场判定全部打桩（stub search_fn/fetch_fn + mock
judge_stances_llm），绝不联网、不调 LLM。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.rumor import evidence as E  # noqa: E402
from mylocalai.rumor.agent import RumorAgent  # noqa: E402
from mylocalai.rumor.state import AnalysisState  # noqa: E402
from mylocalai.rumor.verdict import (  # noqa: E402
    LABEL_HIGH, LABEL_LOW, LABEL_SUSPECT, assess,
)

#: 泛现象类主张（无时间/地点/事件锚点 → 规则判泛现象，不触发 LLM）
TEXT = "网传喝隔夜茶会致癌，赶紧告诉家人！"

#: 达标画像：rumor-mill.example.com 反驳过 2 个档案（形状与
#: dossier.insights.qualified_source_profile 的产出一致）
PROFILE = {"rumor-mill.example.com": {"refutes": 2}}


def _search(results):
    """搜索桩：任何查询都返回同一批结果（第二轮补搜靠 url 去重，天然幂等）。"""
    def search_fn(query, k):
        return {"results": [dict(r) for r in results],
                "engines": {"stub": {"ok": True, "count": len(results)}}}
    return search_fn


def _result(url, title, tier):
    return {"url": url, "title": title, "domain_tier": tier, "date": None,
            "snippet": "官方通报称隔夜茶亚硝酸盐远低于致癌剂量，属于误读，请勿轻信。"}


def _markers():
    """手工 markers（has_claim 走 claims 声明路径）：低分起点，便于观察抬分。"""
    return {"risk_hits": {}, "credibility_hits": {}, "marker_points": 10,
            "credibility_points": 0, "assertion_signal": True}


class TestBitwiseGuard(unittest.TestCase):
    def test_no_profile_none_and_empty_all_equal(self):
        """逐位守卫：不传 / None / {} 三者输出全字典相等，且不含 profile 新键。"""
        results = [_result("https://www.gov.cn/x/a", "辟谣：隔夜茶致癌说法不实", "官方权威"),
                   _result("https://weibo.com/u/123", "网友讨论隔夜茶问题", "自媒体/UGC")]
        base = E.collect_evidence(TEXT, search_fn=_search(results),
                                  fetch_fn=lambda u: None, llm_judge=False)
        none = E.collect_evidence(TEXT, search_fn=_search(results),
                                  fetch_fn=lambda u: None, llm_judge=False,
                                  source_profile=None)
        empty = E.collect_evidence(TEXT, search_fn=_search(results),
                                   fetch_fn=lambda u: None, llm_judge=False,
                                   source_profile={})
        self.assertEqual(base, none)
        self.assertEqual(base, empty)
        self.assertNotIn("profile_refute", base["stance_summary"])
        self.assertNotIn("profile_refute_domains", base["stance_summary"])


class TestProfileLift(unittest.TestCase):
    def _collect(self, results, stances, profile=None):
        """立场判定打桩后离线跑 collect_evidence（llm 路径，条目立场可控）。"""
        with mock.patch.object(E, "judge_stances_llm", return_value=(stances, None)):
            return E.collect_evidence(TEXT, search_fn=_search(results),
                                      fetch_fn=lambda u: None, llm_judge=True,
                                      source_profile=profile)

    def test_nonreliable_refute_domain_lifted(self):
        """抬分路径：非可靠档位域名带"反驳"条目 + 画像 → reliable_refute+1、
        profile_refute_domains 含该域名、other_refute 相应变化、official_refute 不变。"""
        results = [_result("https://rumor-mill.example.com/post/1", "隔夜茶真相", "自媒体/UGC")]
        base = self._collect(results, ["反驳"])
        self.assertEqual(base["stance_summary"]["reliable_refute"], 0)
        lifted = self._collect(results, ["反驳"], profile=PROFILE)
        s = lifted["stance_summary"]
        self.assertEqual(s["reliable_refute"], 1)
        self.assertIn("rumor-mill.example.com", s["reliable_refute_domains"])
        self.assertEqual(s["profile_refute_domains"], ["rumor-mill.example.com"])
        self.assertEqual(s["profile_refute"], 1)
        self.assertEqual(s["other_refute"], 1)       # 基线是 0：画像域名计入 other
        self.assertEqual(s["official_refute"], 0)    # official 口径不受画像影响

    def test_profile_key_normalized_before_match(self):
        """画像键也过 normalize_host：www/大小写/带路径的键与条目主机名对得上。"""
        results = [_result("https://rumor-mill.example.com/post/1", "隔夜茶真相", "自媒体/UGC")]
        lifted = self._collect(results, ["反驳"],
                               profile={"https://www.Rumor-Mill.example.com/x": {"refutes": 2}})
        s = lifted["stance_summary"]
        self.assertEqual(s["profile_refute_domains"], ["rumor-mill.example.com"])
        self.assertEqual(s["reliable_refute"], 1)

    def test_profile_domain_without_refute_entry(self):
        """画像域名当前无反驳条目：reliable_refute 不变，profile 键仍在但为空/0。"""
        results = [_result("https://www.gov.cn/x/a", "辟谣：隔夜茶致癌说法不实", "官方权威")]
        book = self._collect(results, ["反驳"],
                             profile={"ghost.example.com": {"refutes": 5}})
        s = book["stance_summary"]
        self.assertEqual(s["reliable_refute"], 1)
        self.assertEqual(s["profile_refute_domains"], [])
        self.assertEqual(s["profile_refute"], 0)

    def test_profile_domain_already_reliable_not_double_counted(self):
        """画像域名本就在可靠档位：域名已计入 reliable 反驳集合，不重复计数、
        不进 profile_refute_domains。"""
        results = [_result("https://www.gov.cn/x/a", "辟谣：隔夜茶致癌说法不实", "官方权威")]
        book = self._collect(results, ["反驳"], profile={"gov.cn": {"refutes": 2}})
        s = book["stance_summary"]
        self.assertEqual(s["reliable_refute"], 1)     # 不变成 2
        self.assertEqual(s["profile_refute_domains"], [])
        self.assertEqual(s["profile_refute"], 0)

    def test_verdict_end_to_end_lift(self):
        """端到端反哺：has_claim + 画像并入后 refute_weight 跨过单来源阈值 →
        标签抬到疑似/高风险（基线无画像时保持低风险）。"""
        results = [_result("https://rumor-mill.example.com/post/1", "隔夜茶真相", "自媒体/UGC")]
        claims = {"has_factual_claim": True, "unsourced_unverifiable": 0, "claims": []}
        base = assess(_markers(), {}, {}, {}, claims, evidence=self._collect(results, ["反驳"]))
        self.assertEqual(base["label"], LABEL_LOW)
        lifted = assess(_markers(), {}, {}, {}, claims,
                        evidence=self._collect(results, ["反驳"], profile=PROFILE))
        self.assertIn(lifted["label"], (LABEL_SUSPECT, LABEL_HIGH))
        self.assertGreater(lifted["risk_score"], base["risk_score"])


class TestProfilePassThrough(unittest.TestCase):
    def test_state_passes_profile_to_collect_evidence(self):
        """AnalysisState(source_profile=...) 经默认 evidence 闭包透传到
        collect_evidence（mock 捕获参数；claims 用注入桩避免真跑 LLM 组件）。"""
        captured = {}

        def fake_collect(text, **kwargs):
            captured.update(kwargs)
            return {"error": "stub"}

        claims_stub = {"claims": lambda t: {"has_factual_claim": False}}
        # 必须在构造 AnalysisState 前打桩：default_funcs 的惰性 import 发生在构造时
        with mock.patch("mylocalai.rumor.evidence.collect_evidence", side_effect=fake_collect):
            state = AnalysisState(TEXT, funcs=dict(claims_stub), source_profile=PROFILE)
            state.ensure("evidence")
        self.assertEqual(captured.get("source_profile"), PROFILE)
        self.assertEqual(captured.get("claims"), {"has_factual_claim": False})

        captured.clear()
        with mock.patch("mylocalai.rumor.evidence.collect_evidence", side_effect=fake_collect):
            state2 = AnalysisState(TEXT, funcs=dict(claims_stub))
            state2.ensure("evidence")
        self.assertIsNone(captured.get("source_profile"))    # 不传 → 透传 None

    def test_agent_passes_profile_to_analysis_state(self):
        """RumorAgent(source_profile=...) → analyze() 构造 AnalysisState 时透传
        （monkeypatch AnalysisState 捕获参数，不真跑引擎）。"""
        recorded = []

        class _StubState:
            def __init__(self, text, funcs=None, on_component_done=None, source_profile=None):
                recorded.append({"funcs": funcs, "source_profile": source_profile})
                self.text = text
                self.timings = {}
                self.spans = {}

            def peek(self, name):
                return None

            def ensure(self, name):
                return {"error": "stub"}

            def completed(self):
                return []

        with mock.patch("mylocalai.rumor.agent.AnalysisState", _StubState):
            agent = RumorAgent(llm=None, use_default_llm=False, source_profile=PROFILE)
            agent.analyze(TEXT)
        self.assertEqual(recorded[0]["source_profile"], PROFILE)

        with mock.patch("mylocalai.rumor.agent.AnalysisState", _StubState):
            agent2 = RumorAgent(llm=None, use_default_llm=False)
            agent2.analyze(TEXT)
        self.assertIsNone(recorded[1]["source_profile"])     # 不传 → 透传 None


if __name__ == "__main__":
    unittest.main(verbosity=2)
