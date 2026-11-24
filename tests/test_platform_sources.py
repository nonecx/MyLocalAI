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

from mylocalai.platform.sources import (PatrolSourceProvider,  # noqa: E402
                                        RssDiscoveryProvider,
                                        SearchDiscoveryProvider,
                                        build_providers)
from mylocalai.platform.store import PlatformStore  # noqa: E402

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

#: page 类巡检源的页面 fixture：域名都在 sources_registry 的 link_tiers
#: （官方权威/权威媒体）里，extract_links 才收；锚文本两条命中「自来水」、
#: 两条不命中（含一条同域名的不命中链接，验证是按锚文本而非按页面判定）。
PAGE_FIXTURE = """<html><body>
<a href="https://www.gov.cn/xinwen/2026-11/20/content_1.htm">某市自来水检测异常 官方通报排除投毒</a>
<a href="https://www.xinhuanet.com/2026-11/20/c_12345678.htm">自来水异味事件调查进展通报</a>
<a href="https://www.news.cn/local/20261120/1.htm">管网检修期间水质监测数据公开</a>
<a href="https://www.xinhuanet.com/2026-11/20/c_87654321.htm">财经简讯：股市今日小幅波动</a>
</body></html>"""

#: 页面有链接但锚文本无一命中监控词（区别于"抽不到链接"）。
PAGE_NO_MATCH_FIXTURE = """<html><body>
<a href="https://www.xinhuanet.com/2026-11/20/c_11111111.htm">财经简讯：股市今日小幅波动</a>
</body></html>"""


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


class _StubPatrolStore:
    """桩 store：只实现 PatrolSourceProvider 消费的 sources_for_query，
    并按数据层契约模拟"全局无绑定源 ∪ 绑定本词的源、仅启用"的过滤语义，
    记录调用参数（验证 query_id 透传）。"""

    def __init__(self, sources=None):
        self.sources = list(sources or [])
        self.calls = []

    def sources_for_query(self, query_id):
        self.calls.append(query_id)
        srcs = [s for s in self.sources if s.get("enabled", True)]
        if query_id is None:
            return [s for s in srcs if not s.get("query_ids")]
        return [s for s in srcs
                if not s.get("query_ids") or query_id in s["query_ids"]]


def _page_source(name="测试源", url="https://www.gov.cn/list", **extra):
    src = {"name": name, "url": url, "kind": "page", "enabled": True}
    src.update(extra)
    return src


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


class TestPatrolSourceProvider(unittest.TestCase):
    """巡检信息源：page 链接粗糖提取 + rss 路径复用 + store 源路由 + 降级。"""

    def _provider(self, sources=None, store=None):
        return PatrolSourceProvider(store or _StubPatrolStore(sources))

    # ---- page 类：页面链接粗糖提取 ----

    def test_page_links_matching_terms_become_candidates(self):
        """锚文本命中监控词的链接才成候选；title=锚文本、source=page:源名。"""
        store = _StubPatrolStore([_page_source()])
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=PAGE_FIXTURE.encode("utf-8"))):
            res = self._provider(store=store).discover("自来水", 5, query_id=7)
        self.assertEqual(store.calls, [7])                 # query_id 透传给 store
        self.assertFalse(res.degraded)
        self.assertEqual([c.url for c in res.candidates], [
            "https://www.gov.cn/xinwen/2026-11/20/content_1.htm",
            "https://www.xinhuanet.com/2026-11/20/c_12345678.htm"])
        first = res.candidates[0]
        self.assertEqual(first.title, "某市自来水检测异常 官方通报排除投毒")
        self.assertEqual(first.source, "page:测试源")
        self.assertEqual(first.snippet, "")
        self.assertEqual(first.domain, "gov.cn")
        self.assertEqual(first.domain_tier, "官方权威")
        self.assertEqual(first.published_at, "2026-11-20")   # URL 里的日期

    def test_page_k_respected(self):
        store = _StubPatrolStore([_page_source()])
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=PAGE_FIXTURE.encode("utf-8"))):
            res = self._provider(store=store).discover("自来水", 1, query_id=1)
        self.assertEqual(len(res.candidates), 1)

    def test_page_private_host_rejected_without_fetch(self):
        """SSRF：page 源指向环回地址 → host_not_public，且不外发。"""
        store = _StubPatrolStore([_page_source(url="http://127.0.0.1:8000/list")])
        with mock.patch("requests.get") as m:
            res = self._provider(store=store).discover("自来水", 5, query_id=1)
            m.assert_not_called()
        self.assertTrue(res.degraded)
        self.assertIn("host_not_public", res.error)
        self.assertIn("测试源", res.error)                 # error 带源名

    def test_page_http_error_status_degraded(self):
        store = _StubPatrolStore([_page_source()])
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(status=503)):
            res = self._provider(store=store).discover("自来水", 5, query_id=1)
        self.assertTrue(res.degraded)
        self.assertIn("测试源: http_503", res.error)

    def test_page_too_large_rejected(self):
        big = b"<html><body>" + b"x" * 700_000
        store = _StubPatrolStore([_page_source()])
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(content=big)):
            res = self._provider(store=store).discover("自来水", 5, query_id=1)
        self.assertTrue(res.degraded)
        self.assertIn("too_large", res.error)

    def test_page_empty_content_degraded(self):
        store = _StubPatrolStore([_page_source()])
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(content=b"")):
            res = self._provider(store=store).discover("自来水", 5, query_id=1)
        self.assertTrue(res.degraded)
        self.assertIn("测试源: empty_page", res.error)

    def test_page_no_matching_links_degraded(self):
        """页面有链接但锚文本不命中监控词 → 该源 error（不是静默零候选）。"""
        store = _StubPatrolStore([_page_source()])
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=PAGE_NO_MATCH_FIXTURE.encode("utf-8"))):
            res = self._provider(store=store).discover("自来水", 5, query_id=1)
        self.assertEqual(res.candidates, [])
        self.assertTrue(res.degraded)
        self.assertIn("no_matching_links", res.error)
        self.assertIn("测试源", res.error)

    # ---- store 源路由 ----

    def test_query_none_sees_only_global_sources(self):
        store = _StubPatrolStore([
            _page_source(name="全局源", url="https://www.gov.cn/global"),
            _page_source(name="绑定源", url="https://www.gov.cn/bound", query_ids=[7]),
        ])
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=PAGE_FIXTURE.encode("utf-8"))) as m:
            res = self._provider(store=store).discover("自来水", 5)
        self.assertEqual(store.calls, [None])
        self.assertEqual([c.source for c in res.candidates], ["page:全局源"] * 2)
        fetched = [call.args[0] for call in m.call_args_list]
        self.assertEqual(fetched, ["https://www.gov.cn/global"])   # 绑定源没被碰

    def test_query_id_sees_global_plus_bound_only(self):
        store = _StubPatrolStore([
            _page_source(name="全局源", url="https://www.gov.cn/global"),
            _page_source(name="绑定本词", url="https://www.gov.cn/bound", query_ids=[7]),
            _page_source(name="绑定他词", url="https://www.gov.cn/other", query_ids=[9]),
            _page_source(name="停用源", url="https://www.gov.cn/off", enabled=False),
        ])
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=PAGE_FIXTURE.encode("utf-8"))) as m:
            res = self._provider(store=store).discover("自来水", 5, query_id=7)
        self.assertEqual(store.calls, [7])
        fetched = [call.args[0] for call in m.call_args_list]
        self.assertEqual(fetched, ["https://www.gov.cn/global",
                                   "https://www.gov.cn/bound"])

    def test_no_sources_configured_degraded(self):
        res = self._provider(store=_StubPatrolStore([])).discover("自来水", 5, query_id=1)
        self.assertTrue(res.degraded)
        self.assertIn("no_patrol_sources", res.error)

    def test_store_failure_degrades_not_raises(self):
        class _Boom(_StubPatrolStore):
            def sources_for_query(self, query_id):
                raise RuntimeError("db gone")

        res = self._provider(store=_Boom()).discover("自来水", 5, query_id=1)
        self.assertTrue(res.degraded)
        self.assertIn("patrol_sources_unavailable", res.error)

    def test_all_sources_fail_degraded_with_names(self):
        store = _StubPatrolStore([
            _page_source(name="坏页", url="https://www.gov.cn/bad"),
            {"name": "坏kind", "url": "https://www.gov.cn/x", "kind": "weird"},
        ])
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(status=404)):
            res = self._provider(store=store).discover("自来水", 5, query_id=1)
        self.assertEqual(res.candidates, [])
        self.assertTrue(res.degraded)
        self.assertIn("坏页: http_404", res.error)
        self.assertIn("坏kind: kind_unsupported", res.error)

    def test_partial_failure_keeps_candidates(self):
        """一个源坏、一个好 → degraded=True 但候选照常用（不连坐）。"""
        store = _StubPatrolStore([
            _page_source(name="坏页", url="https://www.gov.cn/bad"),
            _page_source(name="好页", url="https://www.gov.cn/good"),
        ])

        def fake_get(url, **kwargs):
            if url.endswith("/bad"):
                return _FakeResp(status=500)
            return _FakeResp(content=PAGE_FIXTURE.encode("utf-8"))

        with _public_host(), mock.patch("requests.get", side_effect=fake_get):
            res = self._provider(store=store).discover("自来水", 5, query_id=1)
        self.assertTrue(res.degraded)
        self.assertIn("坏页: http_500", res.error)
        self.assertEqual([c.source for c in res.candidates], ["page:好页"] * 2)

    def test_cross_source_dedup_by_url(self):
        """两个源给出同一链接 → 跨源去重只留第一条。"""
        store = _StubPatrolStore([
            _page_source(name="源甲", url="https://www.gov.cn/a"),
            _page_source(name="源乙", url="https://www.gov.cn/b"),
        ])
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=PAGE_FIXTURE.encode("utf-8"))):
            res = self._provider(store=store).discover("自来水", 5, query_id=1)
        self.assertEqual([c.source for c in res.candidates], ["page:源甲"] * 2)

    # ---- rss 类：复用既有 feed 解析路径 ----

    def test_rss_kind_reuses_feed_parsing_path(self):
        store = _StubPatrolStore([{"name": "巡检RSS", "enabled": True, "kind": "rss",
                                   "url": "https://feed.example.com/rss"}])
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=RSS_FIXTURE.encode("utf-8"))):
            res = self._provider(store=store).discover("自来水", 5, query_id=3)
        self.assertEqual(store.calls, [3])
        self.assertEqual(len(res.candidates), 1)
        self.assertEqual(res.candidates[0].url, "https://news.example.com/a1")
        self.assertEqual(res.candidates[0].source, "rss:测试源")   # feed 自带标题

    def test_empty_query_degraded(self):
        store = _StubPatrolStore([_page_source()])
        res = self._provider(store=store).discover("   ", 5, query_id=1)
        self.assertTrue(res.degraded)
        self.assertIn("empty_query", res.error)

    # ---- 单源试运行（平台 /test 端点用）----

    def test_test_source_page(self):
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=PAGE_FIXTURE.encode("utf-8"))):
            res = self._provider().test_source(
                _page_source(name="试运行页"), "自来水", 5)
        self.assertFalse(res.degraded)
        self.assertEqual([c.url for c in res.candidates], [
            "https://www.gov.cn/xinwen/2026-11/20/content_1.htm",
            "https://www.xinhuanet.com/2026-11/20/c_12345678.htm"])

    def test_test_source_rss(self):
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=RSS_FIXTURE.encode("utf-8"))):
            res = self._provider().test_source(
                {"name": "试运行RSS", "url": "https://feed.example.com/rss",
                 "kind": "rss"}, "自来水", 5)
        self.assertEqual(len(res.candidates), 1)
        self.assertEqual(res.candidates[0].url, "https://news.example.com/a1")

    def test_test_source_bad_shape_degraded_not_raise(self):
        res = self._provider().test_source({"name": "坏源", "url": "ftp://x"}, "q", 5)
        self.assertTrue(res.degraded)
        self.assertIn("url_invalid", res.error)
        res2 = self._provider().test_source(
            {"name": "坏类", "url": "https://www.gov.cn/x", "kind": "weird"}, "q", 5)
        self.assertTrue(res2.degraded)
        self.assertIn("kind_unsupported", res2.error)


@unittest.skipUnless(hasattr(PlatformStore, "sources_for_query")
                     and hasattr(PlatformStore, "add_patrol_source"),
                     "数据层 v7（patrol_sources）未落地，跳过真库路由用例")
class TestPatrolRealStoreRouting(unittest.TestCase):
    """真 PlatformStore 的源路由：全局/绑定/停用语义由库层过滤，
    这里锁"provider 只碰生效源、query_id 正确透传"。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PlatformStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)

    def _preset(self):
        q1 = self.store.ensure_watch_query("词甲")
        q2 = self.store.ensure_watch_query("词乙")
        self.store.add_patrol_source("全局页", "https://www.gov.cn/global", kind="page")
        self.store.add_patrol_source("绑定甲的页", "https://www.gov.cn/bound1",
                                     kind="page", query_ids=[q1])
        self.store.add_patrol_source("绑定乙的页", "https://www.gov.cn/bound2",
                                     kind="page", query_ids=[q2])
        off = self.store.add_patrol_source("停用的页", "https://www.gov.cn/off",
                                           kind="page")
        self.store.set_patrol_source_enabled(off, False)
        return q1, q2

    def _discover_urls(self, query, query_id):
        with _public_host(), mock.patch("requests.get", return_value=_FakeResp(
                content=PAGE_FIXTURE.encode("utf-8"))) as m:
            PatrolSourceProvider(self.store).discover(query, 5, query_id=query_id)
        return [call.args[0] for call in m.call_args_list]

    def test_bound_query_sees_global_plus_own_binding(self):
        q1, _ = self._preset()
        self.assertEqual(self._discover_urls("自来水", q1),
                         ["https://www.gov.cn/global", "https://www.gov.cn/bound1"])

    def test_none_query_sees_only_global(self):
        self._preset()
        self.assertEqual(self._discover_urls("自来水", None),
                         ["https://www.gov.cn/global"])


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

    def test_store_appends_patrol_provider_last(self):
        providers, _ = build_providers({"rss_sources": [
            {"name": "s", "url": "https://f.example.com/rss"}]}, store=object())
        self.assertEqual([p.name for p in providers], ["search", "rss", "patrol"])
        self.assertIsInstance(providers[-1], PatrolSourceProvider)

    def test_store_appends_patrol_without_rss_config(self):
        providers, _ = build_providers({}, store=object())
        self.assertEqual([p.name for p in providers], ["search", "patrol"])

    def test_store_none_behaviour_unchanged(self):
        """store=None 时与加参数之前完全一致（含告警文案与条数）。"""
        providers, warnings = build_providers({"rss_sources": "not-a-list"}, store=None)
        self.assertEqual([p.name for p in providers], ["search"])
        self.assertTrue(warnings)
        providers2, _ = build_providers({"rss_sources": [
            {"name": "s", "url": "https://f.example.com/rss"}]})
        self.assertEqual([p.name for p in providers2], ["search", "rss"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
