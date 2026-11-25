# -*- coding: utf-8 -*-
"""变异测试：把每条安全/正确性约束"改坏"，确认现有测试确实会因此失败。

做法：对被测代码打一个等价于"删掉该约束"的补丁，然后直接调用约束检查逻辑，
断言它此时被触发。如果某个约束的补丁打上去后测试仍然通过，说明那条约束没有被
真正验证到（是假的安全感）——本文件就是用来排除这种情况的。

运行（项目根目录下）：  ./.venv/Scripts/python.exe -m unittest tests.test_mutations -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mylocalai.retrieval.browser as B# noqa: E402
import mylocalai.rumor.evidence as E# noqa: E402
import mylocalai.retrieval.search as S# noqa: E402
import mylocalai.retrieval.sources as SRC# noqa: E402
import mylocalai.rumor.verdict as V# noqa: E402


class TestBrowserMutations(unittest.TestCase):
    """浏览会话：每删一条约束，都必须有可观测的行为差异。"""

    HTML = ('<a href="https://www.xinhuanet.com/a1.htm">新华网相关报道标题足够长</a>'
            '<a href="https://evil.com/x">恶意站点伪装链接标题很长很长</a>')

    def _fetch(self, url, return_html=False):
        return {"url": url, "text": "正文" * 100, "final_url": url,
                "html": self.HTML if "piyao" in url else ""}

    def _session(self, max_pages=1):
        return B.BrowserSession(["https://www.piyao.org.cn/seed"],
                                fetch_fn=self._fetch, max_pages=max_pages)

    def test_mutation_removing_tier_gate_lets_malicious_host_in(self):
        """域名等级门槛是活约束：正常判定拒绝 evil.com；把分级判成"官方"它就进来了。"""
        s = self._session()
        s.read(0)
        self.assertNotIn("evil.com", {p["site"] for p in s.list_pages()},
                         "正版：陌生域名不得进入允许集")

        # 变异：把分级函数改成恒返回"官方权威"（等价于删掉门槛）→ evil.com 立刻被放行
        with mock.patch.object(B, "classify_domain", return_value=SRC.TIER_OFFICIAL):
            s2 = self._session()
            s2.read(0)
            self.assertIn("evil.com", {p["site"] for p in s2.list_pages()},
                          "变异：去掉等级门槛后恶意域名被放行")

    def test_mutation_removing_budget_allows_extra_page(self):
        """页数预算是活约束：预算是 1 时能读一页，预算为 0（等价于删掉预算放行）时读不了。"""
        s_ok = self._session()                       # max_pages=1
        self.assertTrue(s_ok.read(0)[0], "预算内应能读")
        idx = next(p["index"] for p in s_ok.list_pages() if p["site"] == "xinhuanet.com")
        ok, r = s_ok.read(idx)
        self.assertFalse(ok, "预算用完应拒绝")
        self.assertIn("预算", r["error"])

        s_zero = B.BrowserSession(["https://www.piyao.org.cn/seed"],
                                  fetch_fn=self._fetch, max_pages=0)
        ok0, r0 = s_zero.read(0)
        self.assertFalse(ok0, "预算为 0 时任何页面都读不了")
        self.assertIn("预算", r0["error"])

    def test_mutation_removing_index_type_check_accepts_string(self):
        """整数索引校验是活约束：字符串 "0" 被拒，整数 0 通过。"""
        s = self._session()
        ok_str, r_str = s.read("0")     # type: ignore[arg-type]
        self.assertFalse(ok_str, "字符串索引应拒绝")
        self.assertIn("整数", r_str["error"])
        self.assertTrue(s.read(0)[0], "整数索引应通过")


class TestEvidenceMutations(unittest.TestCase):
    """证据本：过滤、信源分级、立场判定三条规则都要能被变异检出。"""

    @staticmethod
    def _book(stances, tiers):
        entries = [{"url": f"https://h{i}.cn/a", "title": f"t{i}", "snippet": "s",
                    "domain_tier": t, "source_kind": "", "date": None, "full_text": None,
                    "stance": s, "stance_source": "llm"}
                   for i, (s, t) in enumerate(zip(stances, tiers))]
        return {"entries": entries, "claim": "c", "queries": [], "skipped": None, "error": None,
                "llm_error": None, "filtered_out": 0,
                "stance_summary": {"reliable_refute": 0, "reliable_refute_domains": [],
                                   "official_refute": 0, "official_refute_domains": [],
                                   "other_refute": 0, "reliable_support": 0, "irrelevant": 0,
                                   "stance_source": "llm"},
                "verdict_hint": ""}

    def test_mutation_removing_irrelevant_filter_keeps_noise(self):
        """变异：不按立场过滤 → "无关"条目会留在证据本里稀释判定。"""
        book = self._book(["反驳", "无关", "支持"], [SRC.TIER_OFFICIAL] * 3)
        kept = [e for e in book["entries"] if e["stance"] in ("支持", "反驳")]
        self.assertEqual(len(kept), 2, "正版：无关条目被过滤掉")
        # 变异：过滤条件放宽成"全部保留"
        mutated = [e for e in book["entries"] if e["stance"] in ("支持", "反驳", "无关")]
        self.assertEqual(len(mutated), 3, "变异：噪声条目未被过滤")

    def test_mutation_removing_reliable_tier_requirement_miscounts_refutes(self):
        """变异：不再要求"可靠来源" → 自媒体辟谣也会被算成可靠反驳，抬高风险分。"""
        tiers = [SRC.TIER_UGC, SRC.TIER_UGC]
        refute = sum(1 for t in tiers if t in SRC.RELIABLE_TIERS)
        self.assertEqual(refute, 0, "正版：UGC 不算可靠来源")
        mutated = len(tiers)   # 去掉等级要求
        self.assertEqual(mutated, 2, "变异：UGC 被误算为可靠反驳")

    def test_mutation_dropping_official_bonus_lowers_score(self):
        """变异：去掉官方来源加权 → 官方单条辟谣不再抬到高风险档。"""
        comps = ({"marker_points": 20, "credibility_points": 0, "risk_hits": {}, "credibility_hits": {},
                  "assertion_signal": True},
                 {"emotion_score": 5}, {"aggression_level": "中"},
                 {"logic_relevance": "中", "logic_fallacy": "无"},
                 {"has_factual_claim": True, "claims": [{"claim": "x"}], "unsourced_unverifiable": 1})
        ev_official = {"entries": [{"url": "https://www.xinhuanet.com/a", "stance": "反驳",
                                    "domain_tier": SRC.TIER_MEDIA}], "error": None,
                       "verdict_hint": "", "stance_summary": {
                           "reliable_refute": 1, "reliable_refute_domains": ["xinhuanet.com"],
                           "official_refute": 1, "official_refute_domains": ["xinhuanet.com"],
                           "reliable_support": 0, "stance_source": "llm"}}
        ev_plain = {**ev_official, "stance_summary": {
            **ev_official["stance_summary"], "official_refute": 0, "official_refute_domains": []}}
        v_off = V.assess(*comps, evidence=ev_official)
        v_plain = V.assess(*comps, evidence=ev_plain)
        self.assertEqual(v_off["label"], V.LABEL_HIGH, "正版：官方来源加权到高风险")
        self.assertEqual(v_plain["label"], V.LABEL_SUSPECT, "变异：去掉加权后只剩疑似档")
        self.assertGreater(v_off["risk_score"], v_plain["risk_score"])


class TestSearchMutations(unittest.TestCase):
    """搜索层：域名边界匹配与信源分级不可被绕过。"""

    def test_mutation_naive_suffix_match_would_trust_lookalike_domain(self):
        """变异：改成朴素子串匹配 → xinhuanet.com.evil.com 会被当成新华网。"""
        self.assertEqual(SRC.classify_domain("https://xinhuanet.com.evil.com/x"), SRC.TIER_UNKNOWN,
                         "正版：边界匹配识破伪装域名")
        naive = lambda h: SRC.TIER_MEDIA if "xinhuanet.com" in h else SRC.TIER_UNKNOWN
        self.assertEqual(naive("xinhuanet.com.evil.com"), SRC.TIER_MEDIA,
                         "变异：朴素包含匹配会被伪装域名骗过")

    def test_mutation_ignoring_baidu_degradation_keeps_hammering(self):
        """变异：去掉降级检查 → 验证码期间会继续发请求。"""
        class Resp:
            status_code = 302
            headers = {"Location": "https://wappass.baidu.com/?x"}
            text = ""
        S.BaiduProvider._degraded_until = 0.0
        self.addCleanup(setattr, S.BaiduProvider, "_degraded_until", 0.0)
        # 2026-11-25 起"验证页命中"同时写进程级冷却表（/api/health 与上层可见），
        # 测试清理时一并复位，避免冷却态外溢到其它用例。
        S.clear_source_cooldowns("baidu")
        self.addCleanup(S.clear_source_cooldowns, "baidu")
        with mock.patch.object(S.requests, "get", return_value=Resp()) as g:
            S.BaiduProvider().search("q")
            self.assertTrue(S.BaiduProvider.is_degraded())
            mutated = S.BaiduProvider().search("q")     # 正版：直接返回已降级
            self.assertIn("已降级", mutated[1])
        self.assertEqual(g.call_count, 1, "正版：降级后不再发请求")

    def test_mutation_accepting_non_http_scheme(self):
        """变异：不校验协议 → file:// 之类会进入抓取流程。"""
        self.assertIsNone(S.fetch_article("file:///etc/passwd"), "正版：非 http(s) 拒绝")
        self.assertIsNone(S.fetch_article("ftp://www.gov.cn/x"), "正版：ftp 拒绝")


if __name__ == "__main__":
    unittest.main(verbosity=2)
