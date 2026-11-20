# -*- coding: utf-8 -*-
"""发现源测试（P2）：搜索源授权门、RSS 解析/过滤/安全、坏配置装配。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.platform.sources import (RssDiscoveryProvider,  # noqa: E402
                                        SearchDiscoveryProvider,
                                        build_providers)

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>测试源</title>
<item><title>某市自来水检测异常 官方通报排除投毒</title>
<link>https://news.example.com/a1</link>
<description>通报称检测数据正常，网民反映的异味系管道检修。</description>
<pubDate>Thu, 20 Nov 2026 10:00:00 +0800</pubDate></item>
<item><title>无关的财经新闻</title>
<link>https://news.example.com/a2</link>
<description>股市今日小幅波动。</description></item>
</channel></rss>"""

ATOM_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom 源</title>
<entry><title>辟谣：隔夜茶致癌说法不实</title>
<link href="https://fact.example.com/b1"/>
<summary>专家表示隔夜茶亚硝酸盐含量远低于限值。</summary>
<published>2026-11-20T09:00:00Z</published></entry>
</feed>"""

BROKEN_XML = "<rss><channel><title>坏"

DOCTYPE_XML = """<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<rss version="2.0"><channel><title>&xxe;</title>
<item><title>ENTITY 注入尝试</title><link>https://x.example.com/e1</link>
<description>正文</description></item></channel></rss>"""


class _FakeResp:
    def __init__(self, status=200, content=b"", url=""):
        self.status_code = status
        self.content = content
        self.url = url

    def close(self):
        pass


def _public_host():
    """解析用例专用：放行主机校验（测试域名不可解析，SSRF 防护按失败拒绝；
    真实 SSRF 行为由 test_private_host_rejected 用 IP 字面量覆盖）。"""
    return mock.patch("mylocalai.retrieval.search._host_is_public", return_value=True)


class TestSearchProviderAuthGate(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.prefs = Path(self.tmp.name) / "privacy.json"
        self._write_prefs(False)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_prefs(self, authorized):
        self.prefs.write_text(json.dumps({
            "schema_version": 1,
            "retention_days": {"results": 90, "raw_input": 30, "external_cache": 7},
            "save_enabled": {"results": False, "raw_input": False, "external_cache": False},
            "external_processing_enabled": authorized,
            "external_processing_providers": ["public-search"] if authorized else [],
        }), encoding="utf-8")

    def test_unauthorized_returns_degraded_not_error(self):
        """未授权：degraded=True + external_processing_not_authorized，零候选、不抛。"""
        with mock.patch.dict("os.environ",
                             {"MYLOCALAI_PRIVACY_PREFERENCES_PATH": str(self.prefs)}):
            res = SearchDiscoveryProvider().discover("某话题", 5)
        self.assertEqual(res.candidates, [])
        self.assertTrue(res.degraded)
        self.assertIn("external_processing_not_authorized", res.error)

    def test_authorized_uses_search_all(self):
        self._write_prefs(True)
        fake = {"query": "某话题", "results": [
            {"url": "https://gov.example.com/1", "title": "官方通报", "snippet": "内容",
             "domain_tier": "官方权威"},
            {"url": "https://bbs.example.com/2", "title": "网友讨论", "snippet": "内容"},
        ], "engines": {"so360": {"ok": True, "count": 2, "error": None}}}
        with mock.patch.dict("os.environ",
                             {"MYLOCALAI_PRIVACY_PREFERENCES_PATH": str(self.prefs)}):
            provider = SearchDiscoveryProvider()
            with mock.patch("mylocalai.retrieval.search.search_all", return_value=fake):
                res = provider.discover("某话题", 5)
        self.assertEqual(len(res.candidates), 2)
        self.assertFalse(res.degraded)
        self.assertEqual(res.candidates[0].domain_tier, "官方权威")
        self.assertTrue(res.candidates[1].domain_tier)   # 无档位时 classify_domain 补
        self.assertEqual(res.candidates[0].source, "search")

    def test_all_engines_failed_marks_degraded(self):
        self._write_prefs(True)
        fake = {"query": "q", "results": [],
                "engines": {"so360": {"ok": False, "count": 0, "error": "captcha"}}}
        with mock.patch.dict("os.environ",
                             {"MYLOCALAI_PRIVACY_PREFERENCES_PATH": str(self.prefs)}):
            provider = SearchDiscoveryProvider()
            with mock.patch("mylocalai.retrieval.search.search_all", return_value=fake):
                res = provider.discover("q", 5)
        self.assertEqual(res.candidates, [])
        self.assertTrue(res.degraded)


class TestRssProvider(unittest.TestCase):
    def _provider(self, feeds=None):
        return RssDiscoveryProvider(feeds if feeds is not None
                                    else [{"name": "测试源",
                                           "url": "https://feed.example.com/rss",
                                           "enabled": True}])

    def test_parse_rss_and_filter_by_query(self):
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=RSS_FIXTURE.encode("utf-8"))):
            res = self._provider().discover("自来水", 5)
        self.assertEqual(len(res.candidates), 1)
        c = res.candidates[0]
        self.assertEqual(c.url, "https://news.example.com/a1")
        self.assertTrue(c.title.startswith("某市自来水"))
        self.assertEqual(c.source, "rss:测试源")
        self.assertFalse(res.degraded)

    def test_parse_atom_feed(self):
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=ATOM_FIXTURE.encode("utf-8"))):
            res = self._provider().discover("隔夜茶", 5)
        self.assertEqual(len(res.candidates), 1)
        self.assertEqual(res.candidates[0].url, "https://fact.example.com/b1")

    def test_multi_term_query_matches_any(self):
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=RSS_FIXTURE.encode("utf-8"))):
            res = self._provider().discover("财经 自来水", 5)
        self.assertEqual(len(res.candidates), 1)          # 任一关键词命中即收

    def test_broken_xml_is_degraded_not_raise(self):
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=BROKEN_XML.encode("utf-8"))):
            res = self._provider().discover("任何词", 5)
        self.assertEqual(res.candidates, [])
        self.assertTrue(res.degraded)

    def test_doctype_entity_reference_fails_closed(self):
        """DOCTYPE/ENTITY 声明被剥掉后，未定义的实体引用（&xxe;）让解析失败：
        fail-closed——整个源被拒、零候选，/etc/passwd 内容不会以任何形式进入结果。"""
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=DOCTYPE_XML.encode("utf-8"))):
            res = self._provider().discover("注入", 5)
        self.assertEqual(res.candidates, [])
        self.assertTrue(res.degraded)
        self.assertIn("xml_parse_error", res.error)

    def test_private_host_rejected(self):
        with mock.patch("requests.get") as m:
            res = RssDiscoveryProvider(
                [{"name": "内网", "url": "http://127.0.0.1:8000/feed"}]).discover("x", 5)
            m.assert_not_called()
        self.assertTrue(res.degraded)
        self.assertIn("host_not_public", res.error)

    def test_http_error_status(self):
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(status=403)):
            res = self._provider().discover("x", 5)
        self.assertTrue(res.degraded)
        self.assertIn("http_403", res.error)

    def test_too_large_rejected(self):
        big = b"<?xml version='1.0'?><rss>" + b"x" * 1_100_000
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(content=big)):
            res = self._provider().discover("x", 5)
        self.assertIn("too_large", res.error)

    def test_no_feeds_configured(self):
        res = self._provider([]).discover("x", 5)
        self.assertTrue(res.degraded)
        self.assertIn("no_rss_sources", res.error)

    def test_disabled_feeds_skipped(self):
        provider = RssDiscoveryProvider(
            [{"name": "s", "url": "https://f.example.com/rss", "enabled": False}])
        with mock.patch("requests.get") as m:
            res = provider.discover("x", 5)
            m.assert_not_called()
        self.assertIn("no_rss_sources", res.error)


class TestBuildProviders(unittest.TestCase):
    def test_search_always_present(self):
        providers, warnings = build_providers({})
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0].name, "search")
        self.assertEqual(warnings, [])

    def test_rss_config_adds_provider(self):
        providers, _ = build_providers({"rss_sources": [
            {"name": "s", "url": "https://f.example.com/rss"}]})
        self.assertEqual([p.name for p in providers], ["search", "rss"])

    def test_bad_rss_config_warns(self):
        providers, warnings = build_providers({"rss_sources": "not-a-list"})
        self.assertEqual(len(providers), 1)
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
