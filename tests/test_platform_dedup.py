# -*- coding: utf-8 -*-
"""候选归一化与去重测试（P2）：URL 去重键、批内去重、标题指纹近似去重。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.platform.candidates import (FeedCandidate,  # noqa: E402
                                           candidate_from_search_result,
                                           dedup_candidates, near_duplicate,
                                           url_dedup_key)


class _ObjResult:
    """模拟检索层的 SearchResult（属性形状，非 dict）。"""

    def __init__(self, url, title, snippet="", date=None, domain_tier=""):
        self.url = url
        self.title = title
        self.snippet = snippet
        self.date = date
        self.domain_tier = domain_tier


class TestUrlDedupKey(unittest.TestCase):
    def test_same_url_variants_share_key(self):
        base = "https://News.Example.com/a/b"
        for variant in ("https://news.example.com/a/b",
                        "https://news.example.com/a/b/",
                        "https://news.example.com/a/b#section",
                        "https://news.example.com/a/b?utm_source=x"):
            self.assertEqual(url_dedup_key(variant), url_dedup_key(base), variant)

    def test_different_paths_differ(self):
        self.assertNotEqual(url_dedup_key("https://a.example.com/1"),
                            url_dedup_key("https://a.example.com/2"))

    def test_garbage_url_does_not_raise(self):
        self.assertIsInstance(url_dedup_key(""), str)
        self.assertIsInstance(url_dedup_key(None), str)
        self.assertIsInstance(url_dedup_key("::::"), str)


class TestCandidateFromSearchResult(unittest.TestCase):
    def test_dict_shape(self):
        c = candidate_from_search_result(
            {"url": "https://gov.example.com/1", "title": "标题", "snippet": "摘要",
             "date": "2026-11-20", "domain_tier": "官方权威"})
        self.assertEqual((c.title, c.domain, c.domain_tier, c.published_at),
                         ("标题", "gov.example.com", "官方权威", "2026-11-20"))

    def test_object_shape_with_tier_fallback(self):
        c = candidate_from_search_result(
            _ObjResult("https://www.gov.cn/x.htm", "标题", "摘要"))
        # normalize_host 剥 www：域名取注册域名口径
        self.assertEqual(c.domain, "gov.cn")
        self.assertTrue(c.domain_tier)          # 无档位时 classify_domain 补

    def test_bad_url_returns_none(self):
        self.assertIsNone(candidate_from_search_result({"url": "", "title": "t"}))
        self.assertIsNone(candidate_from_search_result({"url": "ftp://x", "title": "t"}))
        self.assertIsNone(candidate_from_search_result({"title": "no url"}))


class TestDedupCandidates(unittest.TestCase):
    def _c(self, url, title="t"):
        return FeedCandidate(title=title, url=url)

    def test_keeps_first_occurrence_order(self):
        out = dedup_candidates([self._c("https://a.example.com/1", "一"),
                                self._c("https://a.example.com/2", "二"),
                                self._c("https://a.example.com/1/", "重复"),
                                None])
        self.assertEqual([c.title for c in out], ["一", "二"])

    def test_empty_and_none_safe(self):
        self.assertEqual(dedup_candidates([]), [])
        self.assertEqual(dedup_candidates(None), [])


class TestNearDuplicate(unittest.TestCase):
    def test_reposted_story_detected(self):
        # 归一化（剥"网传"引导词、去标点）后两条标题逐字相同 → 相似度 1.0，
        # 这正是指纹去重的目的：同一事件换引导词/标点必须判为同一事件。
        a = FeedCandidate(title="某市自来水检测异常 官方通报排除投毒可能",
                          url="https://a.example.com/1")
        b = FeedCandidate(title="网传某市自来水检测异常，官方通报排除投毒可能",
                          url="https://b.example.com/2")
        self.assertTrue(near_duplicate(a, [b], 0.72))
        self.assertTrue(near_duplicate(a, [b], 1.0))

    def test_different_stories_not_flagged(self):
        a = FeedCandidate(title="甲地明天举办音乐节", url="https://a.example.com/1")
        b = FeedCandidate(title="乙公司发布新款新能源汽车", url="https://b.example.com/2")
        self.assertFalse(near_duplicate(a, [b], 0.72))
        self.assertFalse(near_duplicate(a, [b], 0.99))

    def test_empty_title_never_matches(self):
        a = FeedCandidate(title="", url="https://a.example.com/1")
        b = FeedCandidate(title="", url="https://b.example.com/2")
        self.assertFalse(near_duplicate(a, [b], 0.0))

    def test_bad_threshold_does_not_raise(self):
        a = FeedCandidate(title="同一事件的不同报道标题", url="https://a.example.com/1")
        b = FeedCandidate(title="同一事件的不同报道标题", url="https://b.example.com/2")
        self.assertTrue(near_duplicate(a, [b], "abc"))    # 坏阈值按 0，全等即命中
        self.assertTrue(near_duplicate(a, [b], None))

    def test_self_excluded(self):
        a = FeedCandidate(title="某标题", url="https://a.example.com/1")
        self.assertFalse(near_duplicate(a, [a], 1.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
