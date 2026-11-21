# -*- coding: utf-8 -*-
"""领域检索提示（domain_hints）线程测试：RumorAgent.analyze → AnalysisState →
collect_evidence 的透传，与"不传逐位不变"守卫。

先例是 source_profile（tests/test_source_profile_lift.py）：可选参数、默认不启用、
不传/None/{} 输出与没有此参数时逐位一致。domain_hints 是同一形状的第二次落地：
   1. 逐位守卫——不传 / None / [] 三者输出全字典相等，且不含 domain_round 键；
   2. 生效路径——非空提示时在既有轮次之后追加一轮 site: 定向搜索，book["domain_round"]
      为 True，轮次查询形如 "{查询词} site:h1 OR site:h2"（host 归一、最多 3 个）；
   3. 透传链——AnalysisState(domain_hints=...) 经默认 evidence 闭包进 collect_evidence，
      RumorAgent.analyze(domain_hints=...) 进 AnalysisState。

全程离线：搜索/抓取/立场判定全部打桩（stub search_fn/fetch_fn + llm_judge=False），
绝不联网、不调 LLM。

运行（项目根目录下）：  ./.venv/Scripts/python.exe -m unittest tests.test_rumor_domain_hints -v
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

#: 泛现象类主张（无时间/地点/事件锚点 → 规则判泛现象，不触发 LLM）
TEXT = "网传喝隔夜茶会致癌，赶紧告诉家人！"

#: 领域提示（public_health 的 search_hints 前两项）
HINTS = ["nhc.gov.cn", "piyao.org.cn"]


def _claims(claim="喝隔夜茶会致癌"):
    return {"has_factual_claim": True, "unsourced_unverifiable": 1,
            "claims": [{"claim": claim, "has_source": False, "verifiability": "无法核实"}]}


def _result(url, title, tier, snippet="官方通报称隔夜茶亚硝酸盐远低于致癌剂量，属于误读，请勿轻信。"):
    return {"url": url, "title": title, "domain_tier": tier, "date": None, "snippet": snippet}


def _search(behavior):
    """按查询词子串返回预制结果的假搜索引擎（键按声明顺序匹配，先中先得）。"""
    def search_fn(query, k):
        for key, results in behavior.items():
            if key in query:
                return {"results": [dict(r) for r in results],
                        "engines": {"stub": {"ok": True, "count": len(results)}}}
        return {"results": [], "engines": {"stub": {"ok": True, "count": 0}}}
    return search_fn


#: 普通轮命中 gov.cn（带反驳信号 → 不再补搜辟谣轮）；定向轮命中 nhc/piyao
BEHAVIOR = {
    "site:": [_result("https://www.nhc.gov.cn/a", "辟谣：隔夜茶致癌说法不实", "官方权威"),
              _result("https://www.piyao.org.cn/b", "联合辟谣：隔夜茶致癌是误读", "官方权威")],
    "": [_result("https://www.gov.cn/x/a", "辟谣：隔夜茶致癌说法不实", "官方权威")],
}


def _collect(**kwargs):
    """离线跑 collect_evidence：立场判定走规则（llm_judge=False），抓取打桩。"""
    kwargs.setdefault("claims", _claims())
    kwargs.setdefault("search_fn", _search(BEHAVIOR))
    kwargs.setdefault("fetch_fn", lambda u: None)
    kwargs.setdefault("llm_judge", False)
    return E.collect_evidence(TEXT, **kwargs)


class TestBitwiseGuard(unittest.TestCase):
    def test_no_hints_none_and_empty_all_equal(self):
        """逐位守卫：不传 / None / [] 三者输出全字典相等，且不含 domain_round 键。"""
        base = _collect()
        none = _collect(domain_hints=None)
        empty = _collect(domain_hints=[])
        self.assertEqual(base, none)
        self.assertEqual(base, empty)
        self.assertNotIn("domain_round", base)
        self.assertEqual([q["query"] for q in base["queries"]], ["喝隔夜茶会致癌"])
        self.assertEqual(len(base["entries"]), 1)


class TestDomainRound(unittest.TestCase):
    def test_hints_append_one_site_round(self):
        book = _collect(domain_hints=HINTS)
        self.assertIs(book.get("domain_round"), True)
        queries = [q["query"] for q in book["queries"]]
        self.assertEqual(queries, ["喝隔夜茶会致癌",
                                   "喝隔夜茶会致癌 site:nhc.gov.cn OR site:piyao.org.cn"])
        self.assertFalse(book["queries"][-1]["extra"], "定向轮不是辟谣补搜轮")
        urls = [e["url"] for e in book["entries"]]
        self.assertEqual(urls, ["https://www.gov.cn/x/a",
                                "https://www.nhc.gov.cn/a",
                                "https://www.piyao.org.cn/b"],
                         "定向轮结果按同一套 URL 去重进证据本")
        self.assertEqual(book["queries"][-1]["query"].count("site:"), 2)

    def test_hints_normalized_to_hosts(self):
        """提示归一：去空白、转小写、去协议前缀、只留 host（www 保留）。"""
        book = _collect(domain_hints=["  HTTPS://www.NHC.gov.cn/x?y ", "http://piyao.org.cn/"])
        self.assertEqual(book["queries"][-1]["query"],
                         "喝隔夜茶会致癌 site:www.nhc.gov.cn OR site:piyao.org.cn")

    def test_hints_capped_at_three(self):
        book = _collect(domain_hints=["a.gov.cn", "b.gov.cn", "c.gov.cn", "d.gov.cn"])
        self.assertEqual(book["queries"][-1]["query"],
                         "喝隔夜茶会致癌 site:a.gov.cn OR site:b.gov.cn OR site:c.gov.cn")

    def test_blank_hints_are_dropped(self):
        book = _collect(domain_hints=["", "   ", None, "gov.cn"])
        self.assertEqual(book["queries"][-1]["query"], "喝隔夜茶会致癌 site:gov.cn")

    def test_hints_are_not_wrongly_typed(self):
        """非列表输入（脏数据）不当成提示用：整段跳过，逐位不变。"""
        base = _collect()
        for bad in ("nhc.gov.cn", {"h": 1}, 42):
            self.assertEqual(_collect(domain_hints=bad), base)

    def test_domain_round_skipped_when_search_blocked(self):
        """外部搜索未授权时不追加定向轮（不再发起任何检索）。"""
        def blocked(query, k):
            return {"results": [], "engines": {}, "blocked": True}
        book = _collect(search_fn=blocked, domain_hints=HINTS)
        self.assertEqual(book.get("search_status"), "blocked")
        self.assertNotIn("domain_round", book)
        self.assertEqual(book["queries"], [])

    def test_domain_round_survives_search_exception(self):
        """定向轮搜索异常只让这一轮落空，不影响已拿到的证据。"""
        def flaky(query, k):
            if "site:" in query:
                raise RuntimeError("net down")
            return {"results": [dict(r) for r in BEHAVIOR[""]],
                    "engines": {"stub": {"ok": True, "count": 1}}}
        book = _collect(search_fn=flaky, domain_hints=HINTS)
        self.assertIs(book.get("domain_round"), True, "轮次已登记")
        self.assertEqual(len(book["queries"]), 1, "异常轮的查询不落账")
        self.assertEqual(len(book["entries"]), 1, "已有证据不受影响")
        self.assertIsNone(book["error"])

    def test_domain_round_result_goes_through_same_filters(self):
        """定向轮搜到的条目与普通条目走同一套立场/信息过滤（这里都是反驳，全保留）。"""
        book = _collect(domain_hints=HINTS)
        self.assertEqual([e["stance"] for e in book["entries"]], ["反驳"] * 3)
        self.assertEqual(book["stance_summary"]["reliable_refute"], 3)
        self.assertIn("已有可靠来源反驳", book["verdict_hint"])


class TestPassThrough(unittest.TestCase):
    def test_state_passes_hints_to_collect_evidence(self):
        """AnalysisState(domain_hints=...) 经默认 evidence 闭包透传（mock 捕获参数）。"""
        captured = {}

        def fake_collect(text, **kwargs):
            captured.update(kwargs)
            return {"error": "stub"}

        claims_stub = {"claims": lambda t: {"has_factual_claim": False}}
        with mock.patch("mylocalai.rumor.evidence.collect_evidence", side_effect=fake_collect):
            state = AnalysisState(TEXT, funcs=dict(claims_stub), domain_hints=HINTS)
            state.ensure("evidence")
        self.assertEqual(captured.get("domain_hints"), HINTS)
        self.assertEqual(captured.get("claims"), {"has_factual_claim": False})

        captured.clear()
        with mock.patch("mylocalai.rumor.evidence.collect_evidence", side_effect=fake_collect):
            state2 = AnalysisState(TEXT, funcs=dict(claims_stub))
            state2.ensure("evidence")
        self.assertIsNone(captured.get("domain_hints"), "不传 → 透传 None")

    def test_agent_passes_hints_to_analysis_state(self):
        """RumorAgent.analyze(domain_hints=...) → 构造 AnalysisState 时透传；
        不传时不多传该参数（只认旧参数名的替身也不会被打破）。"""
        recorded = []

        class _StubState:
            def __init__(self, text, funcs=None, on_component_done=None,
                         source_profile=None, domain_hints=None):
                recorded.append({"domain_hints": domain_hints})
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
            RumorAgent(llm=None, use_default_llm=False).analyze(TEXT, domain_hints=HINTS)
        self.assertEqual(recorded[0]["domain_hints"], HINTS)

        recorded.clear()
        with mock.patch("mylocalai.rumor.agent.AnalysisState", _StubState):
            RumorAgent(llm=None, use_default_llm=False).analyze(TEXT)
        self.assertIsNone(recorded[0]["domain_hints"], "不传 → 透传 None")


if __name__ == "__main__":
    unittest.main(verbosity=2)
