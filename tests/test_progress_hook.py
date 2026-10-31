# -*- coding: utf-8 -*-
"""AnalysisState 进度回调钩子测试（工作指导 §5.3 / §6 M2-5）。

进度钩子是 M2 对引擎的唯一侵入：AnalysisState 加可选 on_component_done(name)，
组件算完时回调一次组件名。这里锁两件事：
  1. 回调按组件**完成**顺序收到组件名，且与 state.timings 的键一致（SSE progress 事件的数据源）；
  2. 守卫契约——同一输入下"不传回调"与"传记录型回调"的最终结果逐位相等，
     即不传回调时行为逐位不变（§6 M2-5 的验收原文）。
全部离线：组件函数用桩（手法与 tests/test_rumor_agent_offline.py 一致），Agent 走 llm=None
的 pipeline 模式，不发起任何网络/LLM 请求。
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.rumor.agent import RumorAgent  # noqa: E402
from mylocalai.rumor.patterns import scan_markers  # noqa: E402
from mylocalai.rumor.state import COMPONENTS, AnalysisState  # noqa: E402
from mylocalai.rumor.verdict import ALL_LABELS  # noqa: E402

RUMOR_TEXT = "气死了！听说自来水被人下毒了，所有人都被骗了，官方根本不敢说！速转！"

# 中性证据桩：无条目、无错误，与 test_rumor_agent_offline.py 同款（不抬分、不降级）。
STUB_EVIDENCE = {
    "claim": "自来水被下毒",
    "queries": [{"query": "自来水被下毒", "extra": False,
                 "engines": {"360": {"ok": True, "count": 0}}}],
    "entries": [],
    "skipped": None, "error": None, "llm_error": None,
    "stance_summary": {"reliable_refute": 0, "reliable_refute_domains": [],
                       "reliable_support": 0, "neutral": 0, "stance_source": ""},
    "verdict_hint": "无证据",
}


class Counter:
    """给桩函数计数：Agent 的并行补算下每个组件只许真算一次（compute-once）。"""

    def __init__(self, result):
        self.result, self.calls = result, 0
        self._lock = threading.Lock()

    def __call__(self, text):
        with self._lock:
            self.calls += 1
        return dict(self.result)


def stub_funcs():
    """六个组件全部用确定性桩替换：结果固定，两次运行可逐位比较。"""
    emotion = {"emotion_tendency": "情绪化宣泄", "emotion_score": 8.0, "keywords": ["气死了"]}
    attack = {"attack_target": "针对事件", "aggression_level": "中", "attack_type": "煽动对立"}
    logic = {"argument": "听说下毒", "conclusion": "官方隐瞒", "logic_relevance": "弱",
             "logic_fallacy": "诉诸情绪"}
    claims = {"has_factual_claim": True, "unsourced_unverifiable": 1,
              "claims": [{"claim": "自来水被下毒", "has_source": False, "verifiability": "无法核实"}]}
    return {
        "markers": lambda t: scan_markers(t),
        "emotion": Counter(emotion), "aggression": Counter(attack),
        "logic": Counter(logic), "claims": Counter(claims),
        "evidence": Counter(dict(STUB_EVIDENCE)),
    }


class TestProgressHook(unittest.TestCase):
    def test_callback_order_matches_completion_and_timings(self):
        """① 回调按组件完成顺序收到组件名，且与 state.timings 的键集合一致。

        顺序 ensure 时"完成顺序"就是调用顺序；每个组件只回调一次（重复 ensure
        命中缓存不再回调——SSE 的 done/total 计数靠这一点不出双份）。
        """
        seen: list = []
        state = AnalysisState(RUMOR_TEXT, funcs=stub_funcs(), on_component_done=seen.append)
        for name in COMPONENTS:
            state.ensure(name)
        self.assertEqual(seen, list(COMPONENTS))
        # 与 timings 的键一致：SSE progress 事件里 component/done/total 的口径就是它
        self.assertEqual(set(seen), set(state.timings.keys()))
        self.assertEqual(len(seen), len(set(seen)))
        # 再 ensure 一次已完成的组件：不重复回调
        state.ensure("markers")
        self.assertEqual(seen, list(COMPONENTS))

    def test_guard_results_identical_with_and_without_callback(self):
        """② 守卫：不传回调 vs 传记录型回调，最终结果逐位相等。

        分两层锁：AnalysisState 各组件结果、RumorAgent.analyze() 的完整返回。
        analyze 返回里的 elapsed_s / component_timings / component_spans 是墙钟耗时，
        本来就会变，比较时排除；其余字段（含 label/risk_score/evidence_book/mode…）
        必须逐位相等——这就是"不传回调行为不变"的契约。
        """
        # 第一层：AnalysisState 组件结果
        seen: list = []
        plain = AnalysisState(RUMOR_TEXT, funcs=stub_funcs())
        hooked = AnalysisState(RUMOR_TEXT, funcs=stub_funcs(), on_component_done=seen.append)
        for name in COMPONENTS:
            plain.ensure(name)
            hooked.ensure(name)
        self.assertEqual(hooked.completed(), plain.completed())
        for name in COMPONENTS:
            self.assertEqual(hooked.peek(name), plain.peek(name), name)
        self.assertEqual(set(hooked.timings), set(plain.timings))

        # 第二层：RumorAgent.analyze() 返回（llm=None 走 pipeline，全程离线）
        r1 = RumorAgent(llm=None, funcs=stub_funcs(), use_default_llm=False).analyze(RUMOR_TEXT)
        r2 = RumorAgent(llm=None, funcs=stub_funcs(), use_default_llm=False,
                        on_component_done=seen.append).analyze(RUMOR_TEXT)
        self.assertEqual(set(r1), set(r2))
        timing_keys = {"elapsed_s", "component_timings", "component_spans"}
        for key in sorted(set(r1) - timing_keys):
            self.assertEqual(r1[key], r2[key], f"字段 {key} 因传回调而改变")

    def test_callback_exception_swallowed(self):
        """③ 回调抛异常被吞，分析结果不受影响（进度钩子绝不影响分析本身）。"""
        calls = {"n": 0}

        def bad_callback(_name: str) -> None:
            calls["n"] += 1
            raise RuntimeError("SSE 连接断了")

        state = AnalysisState(RUMOR_TEXT, funcs=stub_funcs(), on_component_done=bad_callback)
        # 回调炸了，组件结果仍要原样返回
        markers = state.ensure("markers")
        clean = AnalysisState(RUMOR_TEXT, funcs=stub_funcs()).ensure("markers")
        self.assertEqual(markers, clean)
        self.assertIn("markers", state.timings)
        # 其余组件照常可算
        self.assertIsInstance(state.ensure("claims"), dict)
        self.assertGreaterEqual(calls["n"], 1)

        # Agent 层同样不抛、结论照常产出
        agent = RumorAgent(llm=None, funcs=stub_funcs(), use_default_llm=False,
                           on_component_done=bad_callback)
        result = agent.analyze(RUMOR_TEXT)
        self.assertEqual(result["mode"], "pipeline")
        self.assertIn(result["label"], ALL_LABELS)

    def test_agent_passes_callback_through_to_state(self):
        """④ RumorAgent 把 on_component_done 透传给每次 analyze 的 AnalysisState。

        llm=None 的 pipeline 模式会补算全部六个组件，因此回调恰好覆盖 COMPONENTS
        全集、且每个组件恰好一次（并行补算下也不重复——compute-once 仍然成立）。
        """
        seen: list = []
        funcs = stub_funcs()
        agent = RumorAgent(llm=None, funcs=funcs, use_default_llm=False,
                           on_component_done=seen.append)
        result = agent.analyze(RUMOR_TEXT)
        self.assertEqual(set(seen), set(COMPONENTS))
        self.assertEqual(len(seen), len(COMPONENTS))
        # 透传的不是"代理回调"而是同一个机制：组件结果照常进 analyze 返回
        self.assertEqual(set(result.get("component_timings") or {}), set(COMPONENTS))
        for counter in (funcs["emotion"], funcs["evidence"]):
            self.assertEqual(counter.calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
