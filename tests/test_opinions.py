# -*- coding: utf-8 -*-
"""rumor_agent 民间观点聚合分析离线测试（不联网、不调真实 API）。

运行（项目根目录下）：  ./.venv/Scripts/python.exe -m unittest tests.test_opinions -v
"""

from __future__ import annotations

import io
import json
import random
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rumor_agent import opinions as O  # noqa: E402
from rumor_agent import sources as SRC  # noqa: E402


def entry(url, stance, tier=SRC.TIER_UGC, title="标题", snippet="摘要内容", **kw):
    """构造一条证据本条目（形状与 collect_evidence 一致）。"""
    return {"url": url, "title": title, "snippet": snippet, "stance": stance,
            "domain_tier": tier, "source_kind": SRC.source_kind(url),
            "date": None, "full_text": None, **kw}


def article(url, args, tier=SRC.TIER_UGC, stance="支持", title="标题"):
    """构造一篇 per_article 记录。args 是论据字典列表。"""
    return {"url": url, "title": title, "stance": stance, "domain_tier": tier,
            "source_kind": SRC.source_kind(url), "arguments": args,
            "read_from": "全文"}


def arg(text, basis="", source="", channel="未知", verifiable="无法核实"):
    return {"argument": text, "basis": basis, "source": source,
            "channel": channel, "verifiable": verifiable}


class FakeLLM:
    """按 system prompt 分派的假 chat_json：抽取/聚类/矛盾各给不同回复。"""

    def __init__(self, arguments=None, clusters=None, contradictions=None, err=""):
        self.arguments = arguments if arguments is not None else [
            arg("自来水被人下毒", channel="网络传言")]
        self.clusters = clusters
        self.contradictions = contradictions if contradictions is not None else []
        self.err = err
        self.calls = []

    def __call__(self, system_prompt, user_text, **kwargs):
        self.calls.append(system_prompt)
        if self.err:
            return None, self.err
        if "论据抽取" in system_prompt:
            return {"arguments": self.arguments}, ""
        if "聚类" in system_prompt:
            n = user_text.count("[")
            cl = self.clusters if self.clusters is not None else [[i] for i in range(n)]
            return {"clusters": cl}, ""
        if "矛盾" in system_prompt:
            return {"contradictions": self.contradictions}, ""
        return {}, ""


class TestShouldTrigger(unittest.TestCase):
    def test_reliable_source_does_not_trigger(self):
        book = {"entries": [entry("https://www.xinhuanet.com/a", "反驳", SRC.TIER_MEDIA)],
                "skipped": None, "error": None}
        ok, reason = O.should_trigger(book)
        self.assertFalse(ok)
        self.assertIn("官方或权威媒体", reason)

    def test_empty_book_does_not_trigger(self):
        self.assertFalse(O.should_trigger(None)[0])
        self.assertFalse(O.should_trigger({})[0])
        self.assertFalse(O.should_trigger({"entries": []})[0])
        ok, reason = O.should_trigger({"skipped": "无可核实的事实性声明，未搜索", "entries": []})
        self.assertFalse(ok)
        self.assertIn("无可核实", reason)

    def test_private_opinions_trigger(self):
        book = {"entries": [entry("https://zhihu.com/a", "支持"),
                            entry("https://weibo.com/b", "反驳"),
                            entry("https://tieba.baidu.com/c", "无关")],
                "skipped": None, "error": None}
        ok, reason = O.should_trigger(book)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_only_neutral_does_not_trigger(self):
        book = {"entries": [entry("https://zhihu.com/a", "无关")], "skipped": None}
        ok, reason = O.should_trigger(book)
        self.assertFalse(ok)
        self.assertIn("没有可分析的私人看法", reason)


class TestSplitByStance(unittest.TestCase):
    def test_two_sided(self):
        entries = [entry("https://a.com/1", "支持"), entry("https://a.com/2", "反驳"),
                   entry("https://a.com/3", "无关"), entry("https://a.com/4", "")]
        g = O.split_by_stance(entries)
        self.assertEqual(len(g["支持"]), 1)
        self.assertEqual(len(g["反驳"]), 1)
        self.assertEqual(g["neutral"], 2)
        self.assertFalse(g["single_sided"])
        self.assertEqual(g["group_mode"], "two-sided")

    def test_single_sided(self):
        g = O.split_by_stance([entry("https://a.com/1", "支持")])
        self.assertTrue(g["single_sided"])
        self.assertEqual(g["group_mode"], "single-sided")
        self.assertEqual(g["反驳"], [])

    def test_all_neutral_is_single_sided(self):
        g = O.split_by_stance([entry("https://a.com/1", "无关")])
        self.assertTrue(g["single_sided"])
        self.assertEqual(g["neutral"], 1)


class TestSampling(unittest.TestCase):
    def _entries(self, n):
        return [entry(f"https://a.com/{i}", "支持") for i in range(n)]

    def test_sample_four_of_six(self):
        entries = self._entries(6)
        got = O.sample_articles(entries, n=4, rng=random.Random(1))
        self.assertEqual(len(got), 4)
        all_urls = [e["url"] for e in entries]
        picked = [e["url"] for e in got]
        # 抽出的 URL 必须属于该组，且是不重复的真子集（不是全选）
        self.assertEqual(len(set(picked)), 4)
        self.assertTrue(set(picked) <= set(all_urls))
        self.assertNotEqual(set(picked), set(all_urls))
        for u in picked:
            self.assertIn(u, all_urls)

    def test_fewer_than_n_selects_all(self):
        entries = self._entries(2)
        got = O.sample_articles(entries, n=4, rng=random.Random(1))
        self.assertEqual([e["url"] for e in got], [e["url"] for e in entries])

    def test_same_seed_is_reproducible_and_seed_matters(self):
        entries = self._entries(6)
        a = O.sample_articles(entries, n=4, rng=random.Random(7))
        b = O.sample_articles(entries, n=4, rng=random.Random(7))
        self.assertEqual([e["url"] for e in a], [e["url"] for e in b])
        # 采样确实是随机的：不是简单取前 4 篇（否则随机化形同虚设）
        self.assertNotEqual([e["url"] for e in a], [e["url"] for e in entries[:4]])
        # 不同 seed 应给出不同的抽样集合（在此数据/参数下确定成立）
        c = O.sample_articles(entries, n=4, rng=random.Random(3))
        self.assertNotEqual([e["url"] for e in a], [e["url"] for e in c])

    def test_result_sorted_by_original_index(self):
        entries = self._entries(6)
        got = O.sample_articles(entries, n=4, rng=random.Random(3))
        idx = [int(e["url"].rsplit("/", 1)[1]) for e in got]
        self.assertEqual(idx, sorted(idx))

    def test_sample_n_edge_cases(self):
        entries = self._entries(6)
        self.assertEqual(O.sample_articles(entries, n=0, rng=random.Random(1)), [])
        self.assertEqual(O.sample_articles(entries, n=-3, rng=random.Random(1)), [])
        self.assertEqual(len(O.sample_articles(entries, n=6, rng=random.Random(1))), 6)
        self.assertEqual(len(O.sample_articles(entries, n=99, rng=random.Random(1))), 6)
        # 空输入不抛异常
        self.assertEqual(O.sample_articles([], n=4), [])
        self.assertEqual(O.sample_articles(None, n=4), [])


class TestOverlap(unittest.TestCase):
    def test_article_count_and_ratio(self):
        articles = [
            article("https://a/0", [arg("自来水被人下毒"), arg("自来水被人下毒")]),  # 同篇重复→算1篇
            article("https://a/1", [arg("自来水被人下毒")]),
            article("https://a/2", [arg("自来水被人下毒")]),
            article("https://a/3", [arg("自来水很干净")]),
        ]
        with mock.patch("shared_core.llm_client.chat_json",
                        side_effect=FakeLLM(clusters=[[0, 1, 2, 3], [4]])):
            merged = O.merge_arguments(articles, "支持")
        self.assertEqual(merged["method"], "llm")
        self.assertIsNone(merged["error"])
        top = merged["items"][0]
        self.assertEqual(top["article_count"], 3)
        self.assertEqual(top["article_ratio"], 0.75)
        self.assertEqual(top["articles"], [0, 1, 2])

    def test_fallback_cluster_groups_similar(self):
        clusters = O.fallback_cluster(["自来水被人下毒", "自来水被人下毒了", "今天天气不错"])
        self.assertEqual(clusters, [[0, 1], [2]])

    def test_fallback_cluster_follows_spec_overlap_only(self):
        # 规格 §三 只以 2 字滑窗重合率为准：不额外加否定词护栏（那会拆散同义改写）
        self.assertEqual(O.fallback_cluster(["自来水非常干净", "自来水很干净"]), [[0, 1]])
        self.assertEqual(O.fallback_cluster(["接种疫苗免疫有效", "接种疫苗有效"]), [[0, 1]])
        # 字面高度重叠但差一个否定的两条按规格仍会合并（规格口径；矛盾交由 LLM/矛盾检测处理）
        self.assertEqual(O.fallback_cluster(["自来水被人下毒", "自来水没下毒"]), [[0, 1]])

    def test_same_claim_with_trailing_clause_is_one_cluster(self):
        # 4 篇同一论据（3 篇多一个从句）应合并为一条 4 篇 / ratio 1.0
        articles = [article(f"https://a/{i}", [arg("自来水被人下毒，不能喝" if i < 3 else "自来水被人下毒")])
                    for i in range(4)]
        merged = O.merge_arguments(articles, "支持", llm=False)
        self.assertEqual(len(merged["items"]), 1)
        self.assertEqual(merged["items"][0]["article_count"], 4)
        self.assertEqual(merged["items"][0]["article_ratio"], 1.0)

    def test_overlap_ratio_definition(self):
        self.assertEqual(O._overlap_ratio("abcd", "abcd"), 1.0)
        self.assertGreaterEqual(O._overlap_ratio("自来水被人下毒", "自来水被人下毒了"), O.OVERLAP_THRESHOLD)
        self.assertLess(O._overlap_ratio("abcdef", "uvwxyz"), O.OVERLAP_THRESHOLD)


class TestMergeFallback(unittest.TestCase):
    def test_non_dict_article_element_does_not_raise(self):
        # 对外函数：per_article 元素非 dict 时不能抛 AttributeError
        merged = O.merge_arguments([["not a dict"], {"arguments": "x"}, None], "支持")
        self.assertEqual(merged["items"], [])
        self.assertEqual(merged["method"], "rule")

    def test_out_of_range_clusters_fall_back_to_rule(self):
        articles = [article("https://a/0", [arg("论据一")]),
                    article("https://a/1", [arg("论据二")])]
        with mock.patch("shared_core.llm_client.chat_json",
                        return_value=({"clusters": [[0, 1, 9]]}, "")):
            merged = O.merge_arguments(articles, "支持")
        self.assertEqual(merged["method"], "rule")
        self.assertIsNotNone(merged["error"])
        self.assertEqual(sum(len(it["articles"]) for it in merged["items"]), 2)

    def test_duplicate_index_falls_back(self):
        articles = [article("https://a/0", [arg("论据一")]),
                    article("https://a/1", [arg("论据二")])]
        with mock.patch("shared_core.llm_client.chat_json",
                        return_value=({"clusters": [[0, 0], [1]]}, "")):
            merged = O.merge_arguments(articles, "反驳")
        self.assertEqual(merged["method"], "rule")

    def test_missing_index_falls_back(self):
        articles = [article("https://a/0", [arg("论据一")]),
                    article("https://a/1", [arg("论据二")])]
        with mock.patch("shared_core.llm_client.chat_json",
                        return_value=({"clusters": [[0]]}, "")):
            merged = O.merge_arguments(articles, "支持")
        self.assertEqual(merged["method"], "rule")

    def test_llm_false_skips_call(self):
        articles = [article("https://a/0", [arg("自来水被人下毒")]),
                    article("https://a/1", [arg("自来水被人下毒了")])]
        with mock.patch("shared_core.llm_client.chat_json") as m:
            merged = O.merge_arguments(articles, "支持", llm=False)
        self.assertEqual(m.call_count, 0)
        self.assertEqual(merged["method"], "rule")
        self.assertEqual(merged["items"][0]["article_count"], 2)


class TestCredibility(unittest.TestCase):
    def _merged(self, articles, members):
        return {"items": [{"argument": "论据", "article_count": len(articles),
                           "article_ratio": 1.0, "articles": list(range(len(articles))),
                           "sources": [], "channels": [], "members": members}]}

    def test_ugc_no_source_unverifiable_is_low(self):
        articles = [article("https://zhihu.com/x", [], tier=SRC.TIER_UGC)]
        members = [arg("听说下毒", basis="听说", source="", verifiable="无法核实")]
        got = O.score_credibility(articles, self._merged(articles, members))
        self.assertEqual(got[0]["level"], "低")
        self.assertIn("渠道 自媒体/UGC", got[0]["reasons"][0])
        self.assertIn("未提具体来源", got[0]["reasons"][1])

    def test_official_source_verifiable_is_high(self):
        articles = [article("https://www.gov.cn/x", [], tier=SRC.TIER_OFFICIAL)]
        members = [arg("通报称属实", basis="卫健委通报", source="卫健委", verifiable="可核实")]
        got = O.score_credibility(articles, self._merged(articles, members))
        self.assertEqual(got[0]["level"], "高")
        self.assertAlmostEqual(got[0]["score"], 1.0, places=3)

    def test_middle_case_is_medium(self):
        articles = [article("https://news.sina.com.cn/x", [], tier=SRC.TIER_PORTAL)]
        members = [arg("某研究指出", basis="研究显示", source="某论文", verifiable="无法核实")]
        got = O.score_credibility(articles, self._merged(articles, members))
        self.assertEqual(got[0]["level"], "中")

    def test_sorted_desc_and_reasons_are_human_readable(self):
        articles = [article("https://zhihu.com/x", [], tier=SRC.TIER_UGC),
                    article("https://www.gov.cn/y", [], tier=SRC.TIER_OFFICIAL)]
        merged = {"items": [
            {"argument": "低分", "article_count": 1, "article_ratio": 1.0, "articles": [0],
             "members": [arg("低分论据")], "sources": [], "channels": []},
            {"argument": "高分", "article_count": 1, "article_ratio": 1.0, "articles": [1],
             "members": [arg("高分论据", source="通报", verifiable="可核实")], "sources": [], "channels": []},
        ]}
        got = O.score_credibility(articles, merged)
        self.assertEqual([r["argument"] for r in got], ["高分", "低分"])
        for r in got:
            self.assertTrue(all(isinstance(x, str) and x for x in r["reasons"]))


class TestExtractArguments(unittest.TestCase):
    def test_success(self):
        with mock.patch("shared_core.llm_client.chat_json",
                        return_value=({"arguments": [arg("论据", source="专家", verifiable="可核实")]}, "")):
            res = O.extract_arguments("正文内容")
        self.assertEqual(res["error"], None)
        self.assertEqual(res["arguments"][0]["argument"], "论据")

    def test_failure_returns_empty_with_error(self):
        with mock.patch("shared_core.llm_client.chat_json", return_value=(None, "超时")):
            res = O.extract_arguments("正文内容")
        self.assertEqual(res["arguments"], [])
        self.assertEqual(res["error"], "超时")

    def test_empty_text(self):
        res = O.extract_arguments("")
        self.assertEqual(res["arguments"], [])
        self.assertIsNotNone(res["error"])

    def test_caps_max_arguments(self):
        many = [arg(f"论据{i}") for i in range(10)]
        with mock.patch("shared_core.llm_client.chat_json", return_value=({"arguments": many}, "")):
            res = O.extract_arguments("正文")
        self.assertEqual(len(res["arguments"]), O.MAX_ARGUMENTS_PER_ARTICLE)


class TestContradictions(unittest.TestCase):
    def test_parses_and_normalizes(self):
        articles = [article("https://a/0", [arg("论据A"), arg("论据B")])]
        merged = {"items": [{"argument": "A"}, {"argument": "B"}]}
        with mock.patch("shared_core.llm_client.chat_json",
                        return_value=({"contradictions": [
                            {"kind": "互相矛盾", "a": "A", "b": "B", "why": "冲突"}]}, "")):
            res = O.find_contradictions(articles, merged)
        self.assertEqual(res["error"], None)
        self.assertEqual(res["contradictions"][0]["kind"], "互相矛盾")

    def test_failure_is_not_raised(self):
        with mock.patch("shared_core.llm_client.chat_json", return_value=(None, "超时")):
            res = O.find_contradictions([], {"items": [{"argument": "A"}]})
        self.assertEqual(res["contradictions"], [])
        self.assertEqual(res["error"], "超时")

    def test_prompt_includes_per_article_arguments(self):
        # 自相矛盾要靠同一篇内的多条论据检出，prompt 必须带上按文章列出的论据原文
        articles = [article("https://a/0", [arg("水管流黑水"), arg("水其实很干净")])]
        merged = {"items": [{"argument": "水管流黑水"}]}
        seen = {}

        def fake(system_prompt, user_text, **kwargs):
            seen["user"] = user_text
            return {"contradictions": []}, ""

        with mock.patch("shared_core.llm_client.chat_json", side_effect=fake):
            O.find_contradictions(articles, merged)
        self.assertIn("水管流黑水", seen["user"])
        self.assertIn("水其实很干净", seen["user"])
        self.assertIn("文章0", seen["user"])

    def test_llm_false_skips_call(self):
        with mock.patch("shared_core.llm_client.chat_json") as m:
            res = O.find_contradictions([], {"items": [{"argument": "A"}]}, llm=False)
        self.assertEqual(m.call_count, 0)
        self.assertEqual(res["contradictions"], [])


class TestCollectPipeline(unittest.TestCase):
    def _book(self):
        return {"claim": "自来水被下毒", "queries": [], "skipped": None, "error": None,
                "entries": [entry("https://zhihu.com/a", "支持", title="自来水真的被下毒了"),
                            entry("https://weibo.com/b", "反驳", title="自来水下毒是谣言")]}

    def test_two_sided_full_run(self):
        fake = FakeLLM()
        with mock.patch("shared_core.llm_client.chat_json", side_effect=fake):
            res = O.collect_opinion_analysis("自来水被人下毒了", evidence_book=self._book(),
                                             fetch_fn=lambda u: None, llm=True)
        self.assertTrue(res["triggered"])
        self.assertEqual(res["group_mode"], "two-sided")
        self.assertEqual(res["error"], None)
        self.assertIn("支持", res["per_group"])
        self.assertIn("反驳", res["per_group"])
        self.assertEqual(res["per_group"]["支持"]["overlap"]["method"], "llm")

    def test_module_fetch_and_search_are_patchable_when_no_book(self):
        # 规格 §复用 的工程约定：evidence_book=None 且不传 search_fn/fetch_fn 时，
        # 调用期解析为模块级 search_all/fetch_article —— 必须能被 mock.patch 替换。
        with mock.patch.object(O, "collect_evidence", return_value=self._book()) as ce, \
             mock.patch("shared_core.llm_client.chat_json", side_effect=FakeLLM()):
            res = O.collect_opinion_analysis("自来水被人下毒了")
        self.assertTrue(ce.called)
        self.assertTrue(res["triggered"])
        # 模块级 fetch_article 被用作默认抓取实现（patch 后返回 None → 走摘要兜底）
        with mock.patch.object(O, "fetch_article", return_value=None) as mf, \
             mock.patch("shared_core.llm_client.chat_json", side_effect=FakeLLM()):
            res2 = O.collect_opinion_analysis("自来水被人下毒了", evidence_book=self._book())
        self.assertTrue(mf.called, "未显式传 fetch_fn 时应调用模块级 fetch_article")
        self.assertEqual(res2["per_article"][0]["read_from"], "摘要")

    def test_llm_false_forwards_llm_judge_false_to_evidence(self):
        # llm=False 时自己采集证据本也必须关掉立场判定的 LLM（否则仍打 API）
        captured = {}

        def fake_collect(text, **kwargs):
            captured.update(kwargs)
            return self._book()

        with mock.patch.object(O, "collect_evidence", side_effect=fake_collect), \
             mock.patch("shared_core.llm_client.chat_json") as m:
            O.collect_opinion_analysis("自来水被下毒", llm=False,
                                       search_fn=lambda q, k: {"results": [], "engines": {}},
                                       fetch_fn=lambda u: None)
        self.assertEqual(captured.get("llm_judge"), False)
        self.assertEqual(m.call_count, 0)

    def test_single_sided_per_group_has_both_keys(self):
        book = {"entries": [entry("https://zhihu.com/a", "支持")], "skipped": None, "error": None}
        with mock.patch("shared_core.llm_client.chat_json", side_effect=FakeLLM()):
            res = O.collect_opinion_analysis("自来水被下毒", evidence_book=book,
                                             fetch_fn=lambda u: None)
        self.assertEqual(res["group_mode"], "single-sided")
        # 单边也要有形状一致的两键，消费方按 per_group["反驳"] 取值不炸
        for key in ("支持", "反驳"):
            self.assertIn(key, res["per_group"])
            self.assertIn("overlap", res["per_group"][key])
        self.assertEqual(res["per_group"]["反驳"]["credibility"], [])

    def test_llm_failure_degrades_and_still_overlaps(self):
        with mock.patch("shared_core.llm_client.chat_json", return_value=(None, "超时")):
            res = O.collect_opinion_analysis("自来水被人下毒了", evidence_book=self._book(),
                                             fetch_fn=lambda u: None, llm=True)
        self.assertTrue(res["triggered"])
        self.assertIsNone(res["error"])
        self.assertTrue(res["degraded"])
        ov = res["per_group"]["支持"]["overlap"]
        self.assertEqual(ov["method"], "rule")
        self.assertTrue(ov["items"], "规则回退仍应给出重合度条目")

    def test_not_triggered_returns_skip(self):
        book = {"entries": [entry("https://www.xinhuanet.com/a", "反驳", SRC.TIER_MEDIA)],
                "skipped": None, "error": None}
        res = O.collect_opinion_analysis("某说法", evidence_book=book)
        self.assertFalse(res["triggered"])
        self.assertIn("官方或权威媒体", res["skip_reason"])

    def test_fetch_failure_uses_snippet_fallback(self):
        book = {"entries": [entry("https://zhihu.com/a", "支持",
                                  title="自来水真的有问题", snippet="我亲眼见到水管流出黑水")],
                "skipped": None, "error": None}
        with mock.patch("shared_core.llm_client.chat_json", side_effect=FakeLLM()):
            res = O.collect_opinion_analysis("自来水被下毒", evidence_book=book,
                                             fetch_fn=lambda u: None)
        self.assertTrue(res["triggered"])
        art = res["per_article"][0]
        self.assertEqual(art["read_from"], "摘要")
        # 降级记录既含原因，也含出问题的文章 URL（规格 §六.3）
        self.assertTrue(any(isinstance(d, dict) and "抓取失败" in d.get("reason", "")
                            and d.get("url") == "https://zhihu.com/a" for d in res["degraded"]))

    def test_fetch_success_reads_full_text(self):
        book = {"entries": [entry("https://zhihu.com/a", "支持")], "skipped": None, "error": None}
        with mock.patch("shared_core.llm_client.chat_json", side_effect=FakeLLM()):
            res = O.collect_opinion_analysis("自来水被下毒", evidence_book=book,
                                             fetch_fn=lambda u: {"url": u, "text": "正文" * 20})
        self.assertEqual(res["per_article"][0]["read_from"], "全文")

    def test_same_seed_reproducible(self):
        book = {"entries": [entry(f"https://zhihu.com/{i}", "支持") for i in range(8)],
                "skipped": None, "error": None}
        with mock.patch("shared_core.llm_client.chat_json", side_effect=FakeLLM()):
            a = O.collect_opinion_analysis("x", evidence_book=book, seed=42, fetch_fn=lambda u: None)
            b = O.collect_opinion_analysis("x", evidence_book=book, seed=42, fetch_fn=lambda u: None)
        self.assertEqual(a["sampled"]["支持"], b["sampled"]["支持"])
        self.assertEqual(len(a["sampled"]["支持"]), 4)

    def test_llm_false_makes_no_llm_calls(self):
        # llm=False 必须完全不调用大模型（离线/无密钥部署），且仍给出规则聚类结果
        with mock.patch("shared_core.llm_client.chat_json") as m:
            res = O.collect_opinion_analysis("自来水被下毒了", evidence_book=self._book(),
                                             fetch_fn=lambda u: None, llm=False)
        self.assertEqual(m.call_count, 0)
        self.assertTrue(res["triggered"])
        self.assertEqual(res["per_group"]["支持"]["overlap"]["method"], "rule")
        self.assertTrue(res["per_group"]["支持"]["overlap"]["items"])
        self.assertTrue(any(isinstance(d, dict) and "未启用大模型" in d.get("reason", "")
                            for d in res["degraded"]))

    def test_entry_with_none_url_does_not_raise(self):
        # 外部/手工构造的证据本可能缺 URL：classify_domain(None) 曾抛 TypeError
        book = {"entries": [{"title": "听说下毒", "snippet": "内容", "stance": "支持",
                             "domain_tier": "", "url": None}],
                "skipped": None, "error": None}
        with mock.patch("shared_core.llm_client.chat_json", side_effect=FakeLLM()):
            res = O.collect_opinion_analysis("自来水被下毒", evidence_book=book,
                                             fetch_fn=lambda u: None)
        self.assertTrue(res["triggered"])
        self.assertEqual(res["per_article"][0]["domain_tier"], "未知来源")


class TestScoreCredibilitySchemaOnly(unittest.TestCase):
    def test_works_without_members_field(self):
        # 按规格 §三 的 schema 构造 merged（无内部 members）也要能正确打分
        articles = [article("https://www.gov.cn/x",
                            [arg("通报称属实", source="卫健委", verifiable="可核实")],
                            tier=SRC.TIER_OFFICIAL)]
        merged = {"items": [{"argument": "通报称属实", "article_count": 1,
                             "article_ratio": 1.0, "articles": [0],
                             "sources": ["卫健委"], "channels": ["官方通报"]}]}
        got = O.score_credibility(articles, merged)
        self.assertEqual(got[0]["level"], "高")
        self.assertGreaterEqual(got[0]["score"], 0.9)

    def test_negative_verifiable_is_not_treated_as_verifiable(self):
        # "不可核实" 不能被子串判断误当"可核实"
        self.assertEqual(O._norm_arg({"argument": "x", "verifiable": "不可核实"})["verifiable"],
                         "无法核实")
        self.assertEqual(O._norm_arg({"argument": "x", "verifiable": "可核实"})["verifiable"],
                         "可核实")
        # 门户渠道 + 有来源 + 不可核实：0.3+0.25=0.55 → 中（曾被误当高）
        articles = [article("https://news.sina.com.cn/x", [arg("某来源")], tier=SRC.TIER_PORTAL)]
        merged = {"items": [{"argument": "某来源", "article_count": 1, "article_ratio": 1.0,
                             "articles": [0], "sources": ["某来源"], "channels": ["未知"]}]}
        got = O.score_credibility(articles, merged)
        self.assertEqual(got[0]["level"], "中")

    def test_reason_text_matches_spec_sample(self):
        # 规格 §五 示例："渠道 自媒体/UGC 记 1.0/3.0"
        articles = [article("https://zhihu.com/x", [arg("听说下毒", verifiable="无法核实")],
                            tier=SRC.TIER_UGC)]
        merged = {"items": [{"argument": "听说下毒", "article_count": 1, "article_ratio": 1.0,
                             "articles": [0], "sources": [], "channels": []}]}
        got = O.score_credibility(articles, merged)
        self.assertIn("渠道 自媒体/UGC 记 1.0/3.0", got[0]["reasons"][0])


class TestMain(unittest.TestCase):
    def test_main_json_output(self):
        fake_res = {"triggered": False, "skip_reason": "无", "group_mode": "",
                    "groups": {"支持": [], "反驳": []}, "sampled": {"支持": [], "反驳": []},
                    "per_article": [], "per_group": {}, "degraded": [], "error": None}
        buf = io.StringIO()
        with mock.patch.object(O, "collect_opinion_analysis", return_value=fake_res), \
             mock.patch("sys.stdout", buf):
            rc = O.main(["--json", "自来水被下毒"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())     # --json 必须输出可解析的完整 JSON
        self.assertFalse(out["triggered"])

    def test_main_text_output_is_human_readable(self):
        fake_res = {"triggered": False, "skip_reason": "证据本没有条目", "group_mode": "",
                    "groups": {"支持": [], "反驳": []}, "sampled": {"支持": [], "反驳": []},
                    "per_article": [], "per_group": {}, "degraded": [], "error": None}
        buf = io.StringIO()
        with mock.patch.object(O, "collect_opinion_analysis", return_value=fake_res), \
             mock.patch("sys.stdout", buf):
            rc = O.main(["某文本"])
        self.assertEqual(rc, 0)
        self.assertIn("未触发", buf.getvalue())
        self.assertIn("证据本没有条目", buf.getvalue())

    def test_main_no_llm_flag_passed_through(self):
        calls = []

        def fake(text, **kwargs):
            calls.append(kwargs)
            return {"triggered": False, "skip_reason": "x", "degraded": []}

        buf = io.StringIO()
        with mock.patch.object(O, "collect_opinion_analysis", side_effect=fake), \
             mock.patch("sys.stdout", buf):
            O.main(["--no-llm", "--seed", "5", "--sample", "2", "文本"])
        self.assertEqual(calls[0]["llm"], False)
        self.assertEqual(calls[0]["seed"], 5)
        self.assertEqual(calls[0]["sample_n"], 2)

    def test_main_missing_text_returns_error(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(O.main([]), 2)

    def test_main_non_utf8_file_does_not_raise(self):
        # 记事本存成 ANSI/GBK 是常见场景：UnicodeDecodeError 是 ValueError 子类，不是 OSError
        import tempfile
        path = Path(tempfile.gettempdir()) / "_opinions_gbk.txt"
        path.write_bytes("下毒说法".encode("gbk"))
        try:
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                rc = O.main(["--file", str(path)])
            self.assertEqual(rc, 2)
            self.assertIn("UTF-8", err.getvalue())
        finally:
            path.unlink(missing_ok=True)

    def test_main_file_is_directory_does_not_raise(self):
        import tempfile
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            rc = O.main(["--file", tempfile.gettempdir()])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
