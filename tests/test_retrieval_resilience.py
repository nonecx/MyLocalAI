# -*- coding: utf-8 -*-
"""检索层韧性测试（2026-11-25，A–D 四组能力的离线回归）。

全程 mock、不联网：requests.get/post 一律打桩或断言禁用；冷却/健康账本/查询缓存
在每个用例前后复位（进程级状态，避免用例互相串扰）。

覆盖（对齐任务书 A–E）：
  ①验证页识别：百度 wappass / Bing 同意页 / 正文过短 → blocked + 进冷却；
  ②冷却到期自动恢复（含"冷却期内零外发"）；
  ③API 源：成功 / 超时 / 5xx / 坏 JSON → 健康账本计数、阈值后跳过、退避调用次数可断言；
  ④坏配置（非法 URL、非法协议、类型错）→ 退回默认 + 告警，不崩；
  ⑤RSS 源：正常 XML / Atom / 坏 XML / 空列表 / 单源失败不影响其它源 / pubDate 缺失；
  ⑥六态 status 可区分（ok / partial / empty / all_blocked / no_sources / all_failed）；
  ⑦上层不把 blocked 显示成"没有发现"（rumor.collect_evidence + assistant 接线助手）。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_retrieval_resilience -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.retrieval import search as search_mod  # noqa: E402

BING_CFG = {"id": "bing", "protocol": "bing",
            "base_url": "https://api.bing.microsoft.com/v7.0/search",
            "key_env": "BING_SEARCH_API_KEY", "enabled": True,
            "weight": 1.0, "timeout_s": 10}

#: 一条正常的长页面（正文远超阈值，且不含任何验证信号）——检测器应判"正常"。
_NORMAL_PAGE = ("<html><body>" + ("<div>这是一条正常的搜索结果摘要，讲的是某地通报与辟谣进展。</div>"
                                  * 40) + "</body></html>")
#: 百度 wappass 验证页（302 与 200 两种形态都用它）。
_WAPPASS_PAGE = ('<html><head><title>百度安全验证</title></head><body>'
                 '<div id="wappass">请完成下方验证</div></body></html>')
#: Bing 同意/前置确认页。
_BING_CONSENT_PAGE = ("<html><head><title>Before you continue to Bing</title></head>"
                      "<body><p>We use cookies to improve your experience.</p></body></html>")
#: 正文过短的拦截页（无关键词、无结构特征，只靠长度阈值命中）。
_SHORT_PAGE = "<html><body>拒绝访问</body></html>"

_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>示例政务快讯</title>
  <item><title>官方通报：某说法不实</title>
        <link>https://www.gov.cn/xinwen/2026-11/20/content.htm</link>
        <description>通报称该说法与事实不符</description>
        <pubDate>Fri, 20 Nov 2026 08:00:00 GMT</pubDate></item>
  <item><title>无关的天气预报</title>
        <link>https://www.gov.cn/weather/2026.htm</link>
        <description>明日多云</description></item>
  <item><title>同链接重复条目</title>
        <link>https://www.gov.cn/xinwen/2026-11/20/content.htm</link>
        <description>官方通报：某说法不实（重复）</description></item>
</channel></rss>"""

_RSS_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>示例媒体快讯</title>
  <entry><title>媒体解读某说法</title>
         <link rel="alternate" href="https://xinhuanet.com/2026-11/20/a.htm"/>
         <summary>媒体认为该说法缺乏依据</summary>
         <published>2026-11-20T09:00:00Z</published></entry>
</feed>"""


class FakeResponse:
    """requests.Response 的最小替身：status_code / text / content / json / close。"""

    def __init__(self, status_code: int = 200, text: str = "", payload: object = None,
                 headers: dict = None, content: bytes = None) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = headers or {}
        self._payload = payload
        self.closed = False

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def close(self) -> None:
        self.closed = True


def _reset_state() -> None:
    """复位进程级韧性状态（健康账本 + 冷却表 + 查询缓存 + 设置）。"""
    search_mod.reset_resilience_state()
    search_mod._QUERY_CACHE.clear()
    search_mod._SOURCE_SETTINGS.clear()
    search_mod.BaiduProvider._degraded_until = 0.0


class _ResilienceTestBase(unittest.TestCase):
    """每个用例前后都复位进程级状态（冷却/账本/缓存）。"""

    def setUp(self) -> None:
        _reset_state()
        self.addCleanup(_reset_state)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def write_sources(self, data: object) -> str:
        """临时 search_sources.json（与既有测试同款辅助），返回路径供 monkeypatch。"""
        fd, path = tempfile.mkstemp(suffix=".json", dir=self._tmp.name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if isinstance(data, str):
                fh.write(data)
            else:
                json.dump(data, fh, ensure_ascii=False)
        return path


class _StubProvider:
    """与 provider 契约一致的最小后端：可配置返回结果 / 错误 / 是否"被拦截"。"""

    def __init__(self, name: str, rows: list = None, err: str = None,
                 blocked: bool = False) -> None:
        self.name = name
        self._rows = list(rows or [])
        self._err = err
        self._blocked = blocked
        self.calls = 0

    def search(self, query: str, k: int = 6):
        self.calls += 1
        if self._blocked:
            # 真实 provider 命中验证页时会写共享冷却表：桩按同一契约写入。
            search_mod.mark_source_blocked(self.name, "百度安全验证页（wappass）", seconds=60)
            return [], "百度安全验证页（wappass）（该源冷却 60 秒，期间不再请求）"
        if self._err:
            return [], self._err
        return [search_mod.SearchResult(self.name, i + 1, r.get("title", ""), r["url"],
                                        r.get("snippet", ""), None,
                                        search_mod.classify_domain(r["url"]))
                for i, r in enumerate(self._rows[:max(1, int(k))])], None


# ---------------------------------------------------------------------------
# ① 验证页识别（关键词 + 结构特征 + 正文长度阈值）
# ---------------------------------------------------------------------------

class VerificationPageDetectionTests(_ResilienceTestBase):
    """共用判定 detect_verification_page：三路信号各自可命中，正常页不误伤。"""

    def test_baidu_wappass_keyword(self):
        reason = search_mod.detect_verification_page(_WAPPASS_PAGE)
        self.assertIsNotNone(reason)
        self.assertIn("wappass", reason)

    def test_bing_consent_page(self):
        reason = search_mod.detect_verification_page(_BING_CONSENT_PAGE)
        self.assertIsNotNone(reason)
        self.assertIn("同意", reason)

    def test_short_body_hits_length_threshold(self):
        reason = search_mod.detect_verification_page(_SHORT_PAGE)
        self.assertIsNotNone(reason)
        self.assertIn("正文过短", reason)

    def test_structural_feature_without_keyword(self):
        page = ('<html><body><form id="challenge-form" action="/x"></form>'
                + "<div>内容" * 300 + "</div></body></html>")
        reason = search_mod.detect_verification_page(page)
        self.assertIsNotNone(reason)
        self.assertIn("结构特征", reason)

    def test_normal_long_page_is_not_blocked(self):
        self.assertIsNone(search_mod.detect_verification_page(_NORMAL_PAGE))

    def test_baidu_302_wappass_enters_cooldown(self):
        resp = FakeResponse(302, "", headers={"Location": "https://wappass.baidu.com/?x"})
        with mock.patch.object(search_mod.requests, "get", return_value=resp) as get:
            results, err = search_mod.BaiduProvider().search("q")
        self.assertEqual(results, [])
        self.assertIn("验证码", err)
        self.assertEqual(get.call_count, 1)
        state = search_mod.source_cooldown("baidu")
        self.assertTrue(state["active"], "命中验证页必须进冷却")
        self.assertIn("wappass", state["reason"])
        # 冷却期内第二次调用零外发
        with mock.patch.object(search_mod.requests, "get",
                               side_effect=AssertionError("冷却期不得外发")):
            results2, err2 = search_mod.BaiduProvider().search("q")
        self.assertEqual(results2, [])
        self.assertIn("已降级", err2)

    def test_so360_short_page_blocks_and_search_all_marks_blocked(self):
        with mock.patch.object(search_mod.requests, "get",
                               return_value=FakeResponse(200, _SHORT_PAGE)):
            out = search_mod.search_all("q", providers=[search_mod.So360Provider()])
        self.assertEqual(out["results"], [])
        self.assertEqual(out["status"], "all_blocked")
        self.assertTrue(out["all_blocked"])
        slot = out["engines"]["360"]
        self.assertTrue(slot["blocked"])
        self.assertIn("正文过短", slot["reason"])
        self.assertTrue(search_mod.source_cooldown("360")["active"])

    def test_so360_cooldown_prevents_http(self):
        search_mod.mark_source_blocked("360", "风控页（访问过于频繁）", seconds=60)
        with mock.patch.object(search_mod.requests, "get",
                               side_effect=AssertionError("冷却期不得外发")):
            results, err = search_mod.So360Provider().search("q")
        self.assertEqual(results, [])
        self.assertIn("冷却", err)


# ---------------------------------------------------------------------------
# ② 冷却到期自动恢复
# ---------------------------------------------------------------------------

class CooldownRecoveryTests(_ResilienceTestBase):
    """冷却到期自动恢复：惰性过期，无需后台定时器；到期后重新外发。"""

    def test_cooldown_expires_and_source_recovers(self):
        with mock.patch.object(search_mod, "_SOURCE_SETTINGS",
                               {"cooldown_seconds": 0.05}):
            search_mod.mark_source_blocked("360", "验证码页")
            self.assertTrue(search_mod.source_cooldown("360")["active"])
            self.assertGreater(search_mod.source_cooldown("360")["remaining_s"], 0)
            time.sleep(0.06)
            state = search_mod.source_cooldown("360")
        self.assertFalse(state["active"], "冷却到期必须自动恢复")
        self.assertEqual(state["remaining_s"], 0.0)
        self.assertEqual(search_mod.cooldown_snapshot(), {})

    def test_provider_resumes_http_after_expiry(self):
        with mock.patch.object(search_mod, "_SOURCE_SETTINGS",
                               {"cooldown_seconds": 0.05}):
            search_mod.mark_source_blocked("360", "验证码页")
            time.sleep(0.06)
            with mock.patch.object(search_mod.requests, "get",
                                   return_value=FakeResponse(200, _NORMAL_PAGE)) as get:
                results, err = search_mod.So360Provider().search("q")
        self.assertEqual(get.call_count, 1, "冷却到期后应重新请求")
        self.assertIsNone(err)
        self.assertEqual(results, [])          # 正常页但无结果块 → 0 条，不是 blocked

    def test_cooldown_snapshot_visible_for_health(self):
        search_mod.mark_source_blocked("baidu", "百度安全验证页（wappass）", seconds=30)
        snapshot = search_mod.cooldown_snapshot()
        self.assertIn("baidu", snapshot)
        self.assertGreater(snapshot["baidu"]["remaining_s"], 0)
        self.assertIn("wappass", snapshot["baidu"]["reason"])


# ---------------------------------------------------------------------------
# ③ API 源：健康账本 / 阈值跳过 / 有限重试与退避
# ---------------------------------------------------------------------------

class ApiSourceRetryAndHealthTests(_ResilienceTestBase):
    """失败重试有限且带退避；失败计入健康账本；连续失败达阈值后跳过（零外发）。"""

    def test_retry_with_exponential_backoff(self):
        cfg = dict(BING_CFG, max_retries=2, retry_backoff_s=0.01)
        provider = search_mod.ApiSearchProvider(cfg)
        sleeps: list = []
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=requests.ConnectionError("boom")) as get, \
                mock.patch.object(search_mod.time, "sleep",
                                  side_effect=lambda s: sleeps.append(s)):
            results, err = provider.search("q")
        self.assertEqual(results, [])
        self.assertIn("ConnectionError", err)
        self.assertEqual(get.call_count, 3, "1 次 + 2 次重试")
        self.assertEqual(sleeps, [0.01, 0.02], "退避按 2 的幂递增，不是一口气打完")

    def test_5xx_is_retried_bad_json_is_retried(self):
        cfg = dict(BING_CFG, max_retries=1, retry_backoff_s=0)
        provider = search_mod.ApiSearchProvider(cfg)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(503, "")) as get:
            provider.search("q")
        self.assertEqual(get.call_count, 2)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(200, "", payload=None)) as get2:
            results, err = provider.search("q")
        self.assertEqual(get2.call_count, 2)
        self.assertIn("非JSON", err)

    def test_4xx_is_not_retried(self):
        cfg = dict(BING_CFG, max_retries=3, retry_backoff_s=0)
        provider = search_mod.ApiSearchProvider(cfg)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(401, "")) as get:
            results, err = provider.search("q")
        self.assertEqual(get.call_count, 1, "确定性失败（4xx）不重试")
        self.assertIn("HTTP 401", err)

    def test_health_ledger_counts_and_resets(self):
        provider = search_mod.ApiSearchProvider(BING_CFG)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=requests.ConnectionError("boom")):
            provider.search("q")
            provider.search("q")
        self.assertEqual(search_mod._API_SOURCE_HEALTH.get("bing"), 2)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(200, "", payload={
                                      "webPages": {"value": [
                                          {"name": "t", "url": "https://www.gov.cn/a",
                                           "snippet": "s"}]}})):
            results, err = provider.search("q")
        self.assertIsNone(err)
        self.assertEqual(search_mod._API_SOURCE_HEALTH.get("bing"), 0)

    def test_threshold_skips_unhealthy_source_without_http(self):
        cfg = dict(BING_CFG, failure_skip_threshold=2)
        provider = search_mod.ApiSearchProvider(cfg)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=requests.ConnectionError("boom")):
            provider.search("q")
            provider.search("q")
        healthy = _StubProvider("healthy", [{"title": "t", "url": "https://www.gov.cn/a.htm"}])
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=AssertionError("不健康源不得外发")):
            out = search_mod.search_all("q", providers=[provider, healthy])
        self.assertEqual(out.get("skipped"), 1)
        slot = out["engines"]["bing"]
        self.assertEqual(slot["skipped"], 1)
        self.assertIn("unhealthy:", slot["skip_reason"])
        self.assertEqual(out["status"], "partial")     # 有结果但部分源不可用
        self.assertTrue(out["degraded"])
        self.assertEqual(search_mod._API_SOURCE_HEALTH.get("bing"), 1)   # 跳过一次降温


# ---------------------------------------------------------------------------
# ④ 坏配置：退回默认 + 告警，不崩
# ---------------------------------------------------------------------------

class BadConfigTests(_ResilienceTestBase):
    """非法 URL / 非法协议 / 类型错 → 丢弃或回默认 + 告警，绝不抛异常。"""

    def _warnings(self) -> list:
        from mylocalai.config.rules import load_warnings
        return [w for w in load_warnings() if "search_sources.json" in w]

    def test_illegal_url_and_protocol_dropped_with_warning(self):
        bad_url = dict(BING_CFG, id="bad-url", base_url="ftp://x/y")
        no_host = dict(BING_CFG, id="no-host", base_url="https:///path")
        bad_proto = dict(BING_CFG, id="bad-proto", protocol="serper")
        path = self.write_sources({"schema_version": 1,
                                   "sources": [BING_CFG, bad_url, no_host, bad_proto]})
        cfgs = search_mod.load_api_source_configs(path)
        self.assertEqual([c["id"] for c in cfgs], ["bing"])
        warnings = "；".join(self._warnings())
        self.assertIn("已丢弃", warnings)
        self.assertIn("bad-url", warnings)
        self.assertIn("no-host", warnings)
        self.assertIn("bad-proto", warnings)

    def test_wrong_types_fall_back_to_defaults_with_warning(self):
        row = dict(BING_CFG, weight="很重", timeout_s="十秒",
                   max_retries="两次", retry_backoff_s="慢")
        path = self.write_sources({"schema_version": 1, "sources": [row]})
        cfgs = search_mod.load_api_source_configs(path)
        self.assertEqual(len(cfgs), 1)
        cfg = cfgs[0]
        self.assertEqual(cfg["weight"], 1.0)
        self.assertEqual(cfg["timeout_s"], float(search_mod._SEARCH_TIMEOUT))
        self.assertEqual(cfg["max_retries"], search_mod._API_RETRY_DEFAULT)
        self.assertEqual(cfg["retry_backoff_s"], search_mod._API_RETRY_BACKOFF_DEFAULT)
        warnings = "；".join(self._warnings())
        for field in ("weight", "timeout_s", "max_retries", "retry_backoff_s"):
            self.assertIn(field, warnings, f"{field} 写坏应告警")

    def test_rss_bad_rows_dropped_and_non_list_warns(self):
        path = self.write_sources({"schema_version": 1, "sources": [],
                                   "rss_sources": [{"name": "坏源", "url": "ftp://x/feed"},
                                                   "junk",
                                                   {"name": "好源",
                                                    "url": "https://feed.example.com/rss"}]})
        rss = search_mod.load_rss_source_configs(path)
        self.assertEqual([c["name"] for c in rss], ["好源"])
        self.assertIn("rss_sources", "；".join(self._warnings()))
        bad = self.write_sources({"schema_version": 1, "rss_sources": "不是数组"})
        self.assertEqual(search_mod.load_rss_source_configs(bad), [])
        self.assertIn("必须是数组", "；".join(self._warnings()))

    def test_corrupt_json_does_not_crash_main_chain(self):
        path = self.write_sources("{ 不是 json")
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", path):
            out = search_mod.search_all(
                "q", providers=[_StubProvider("360", [{"title": "t",
                                                       "url": "https://example.com/a"}])])
        self.assertEqual([r.url for r in out["results"]], ["https://example.com/a"])
        self.assertFalse(out["sources_configured"])       # 配置损坏 = 没有结构化源
        self.assertIn("读取失败", "；".join(self._warnings()))

    def test_bad_settings_fall_back_to_defaults(self):
        path = self.write_sources({"schema_version": 1, "failure_skip_threshold": "多",
                                   "query_cache_ttl_s": [], "cooldown_seconds": -5,
                                   "sources": []})
        search_mod.load_api_source_configs(path)
        self.assertEqual(search_mod._SOURCE_SETTINGS["failure_skip_threshold"],
                         search_mod._FAILURE_SKIP_THRESHOLD_DEFAULT)
        self.assertEqual(search_mod._SOURCE_SETTINGS["query_cache_ttl_s"],
                         search_mod._QUERY_CACHE_TTL_DEFAULT)
        self.assertEqual(search_mod._SOURCE_SETTINGS["cooldown_seconds"],
                         search_mod._COOLDOWN_SECONDS_DEFAULT)
        warnings = "；".join(self._warnings())
        for field in ("failure_skip_threshold", "query_cache_ttl_s", "cooldown_seconds"):
            self.assertIn(field, warnings)


# ---------------------------------------------------------------------------
# ⑤ RSS/Atom 源
# ---------------------------------------------------------------------------

class RssProviderTests(_ResilienceTestBase):
    """RSS 源：正常 XML / Atom / 坏 XML / 空列表 / 单源失败不影响其它源 / 日期缺失。"""

    def _provider(self, **cfg) -> "search_mod.RssProvider":
        base = {"name": "gov-rss", "url": "https://feed.example.com/rss",
                "timeout_s": 5}
        base.update(cfg)
        return search_mod.RssProvider(base)

    def test_rss_xml_filters_by_keyword_and_dedups(self):
        provider = self._provider()
        with mock.patch.object(search_mod.requests, "get",
                               return_value=FakeResponse(200, _RSS_XML)):
            results, err = provider.search("某说法", k=5)
        self.assertIsNone(err)
        self.assertEqual(len(results), 1, "同 URL 重复条目去重、无关条目被过滤")
        row = results[0]
        self.assertEqual(row.engine, "gov-rss")
        self.assertEqual(row.rank, 1)
        self.assertEqual(row.title, "官方通报：某说法不实")
        self.assertEqual(row.url, "https://www.gov.cn/xinwen/2026-11/20/content.htm")
        self.assertIn("与事实不符", row.snippet)
        self.assertEqual(row.date, "2026-11-20")      # RFC 822 pubDate → YYYY-MM-DD
        self.assertEqual(row.domain_tier, "官方权威")

    def test_atom_feed_link_href_and_published(self):
        provider = self._provider(name="media-rss")
        with mock.patch.object(search_mod.requests, "get",
                               return_value=FakeResponse(200, _RSS_ATOM)):
            results, err = provider.search("某说法", k=5)
        self.assertIsNone(err)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://xinhuanet.com/2026-11/20/a.htm")
        self.assertEqual(results[0].date, "2026-11-20")

    def test_bad_xml_returns_error_not_raise(self):
        provider = self._provider()
        with mock.patch.object(search_mod.requests, "get",
                               return_value=FakeResponse(200, "<rss><channel><item>")):
            results, err = provider.search("某说法", k=5)
        self.assertEqual(results, [])
        self.assertIn("XML 解析失败", err)

    def test_empty_rss_sources_is_legit_empty(self):
        path = self.write_sources({"schema_version": 1, "sources": [], "rss_sources": []})
        self.assertEqual(search_mod.load_rss_source_configs(path), [])
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", path):
            providers = search_mod.default_providers()
        self.assertEqual([type(p).__name__ for p in providers],
                         ["So360Provider", "BaiduProvider"])

    def test_missing_or_bad_pubdate_keeps_item(self):
        xml = ("<rss version=\"2.0\"><channel><item><title>某说法核查</title>"
               "<link>https://www.gov.cn/a.htm</link><description>内容</description>"
               "<pubDate>不是日期</pubDate></item></channel></rss>")
        provider = self._provider()
        with mock.patch.object(search_mod.requests, "get",
                               return_value=FakeResponse(200, xml)):
            results, err = provider.search("某说法", k=5)
        self.assertIsNone(err)
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0].date, "日期解析失败不抛、只是没有日期")

    def test_single_source_failure_does_not_affect_others(self):
        good = self._provider(name="good-rss", url="https://good.example.com/rss")
        bad = self._provider(name="bad-rss", url="https://bad.example.com/rss")

        def fake_get(url, **kwargs):
            if "bad" in str(url):
                return FakeResponse(500, "")
            return FakeResponse(200, _RSS_XML)

        with mock.patch.object(search_mod.requests, "get", side_effect=fake_get):
            out = search_mod.search_all("某说法", providers=[bad, good])
        self.assertEqual([r.engine for r in out["results"]], ["good-rss"])
        self.assertIn("HTTP 500", out["engines"]["bad-rss"]["error"])
        self.assertTrue(out["engines"]["good-rss"]["ok"])
        self.assertEqual(out["status"], "partial")
        self.assertTrue(out["degraded"])

    def test_deny_domains_filters_entries(self):
        provider = self._provider(deny_domains=["gov.cn"])
        with mock.patch.object(search_mod.requests, "get",
                               return_value=FakeResponse(200, _RSS_XML)):
            results, err = provider.search("某说法", k=5)
        self.assertIsNone(err)
        self.assertEqual(results, [], "命中黑名单的域名条目应被过滤")

    def test_rss_verification_page_blocks_source(self):
        provider = self._provider()
        with mock.patch.object(search_mod.requests, "get",
                               return_value=FakeResponse(200, _WAPPASS_PAGE)):
            results, err = provider.search("某说法", k=5)
        self.assertEqual(results, [])
        self.assertIn("wappass", err)
        self.assertTrue(search_mod.source_cooldown("gov-rss")["active"])

    def test_bundled_rss_examples_are_disabled(self):
        # 包内示例配置位（两条）默认 enabled=false：不进装配、不外发、不写死第三方 URL。
        self.assertEqual(search_mod.load_rss_source_configs(), [])
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "",
                                          "BOCHA_API_KEY": ""}):
            providers = search_mod.default_providers()
        self.assertEqual([type(p).__name__ for p in providers],
                         ["So360Provider", "BaiduProvider"])

    def test_enabled_rss_source_assembled_before_web_sources(self):
        path = self.write_sources({
            "schema_version": 1, "sources": [],
            "rss_sources": [{"name": "gov-rss", "url": "https://feed.example.com/rss",
                             "enabled": True}]})
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", path), \
                mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "",
                                             "BOCHA_API_KEY": ""}):
            providers = search_mod.default_providers()
            summary = search_mod.source_config_summary()
        # 源优先级：结构化 RSS 源排在网页解析源之前
        self.assertEqual([type(p).__name__ for p in providers],
                         ["RssProvider", "So360Provider", "BaiduProvider"])
        self.assertTrue(summary["sources_configured"])
        self.assertEqual(summary["rss_sources"], ["gov-rss"])


# ---------------------------------------------------------------------------
# ⑥ 六态 status 可区分
# ---------------------------------------------------------------------------

class DegradeStatusTests(_ResilienceTestBase):
    """ok / partial / empty / all_blocked / no_sources / all_failed 六态各自可断言。"""

    def _empty_config(self) -> str:
        return self.write_sources({"schema_version": 1, "sources": [], "rss_sources": []})

    def test_ok_all_sources_succeed(self):
        stubs = [_StubProvider("360", [{"title": "a", "url": "https://www.gov.cn/a"}]),
                 _StubProvider("baidu", [{"title": "b", "url": "https://xinhuanet.com/b"}])]
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", self._empty_config()):
            out = search_mod.search_all("q", providers=stubs)
        self.assertEqual(out["status"], "ok")
        self.assertFalse(out["degraded"])
        self.assertFalse(out["all_blocked"])
        self.assertEqual(len(out["results"]), 2)

    def test_empty_is_real_zero_not_degraded(self):
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", self._empty_config()):
            out = search_mod.search_all("q", providers=[_StubProvider("360", [])])
        self.assertEqual(out["status"], "empty")
        self.assertFalse(out["degraded"], "真的有源成功返回 0 条 ≠ 降级")
        self.assertEqual(out["degraded_reason"], "")

    def test_all_blocked_is_distinguishable(self):
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", self._empty_config()):
            out = search_mod.search_all(
                "q", providers=[_StubProvider("360", blocked=True),
                                _StubProvider("baidu", blocked=True)])
        self.assertEqual(out["status"], "all_blocked")
        self.assertTrue(out["all_blocked"])
        self.assertTrue(out["degraded"])
        self.assertIn("反爬拦截", out["degraded_reason"])
        for slot in out["engines"].values():
            self.assertTrue(slot["blocked"])
            self.assertTrue(slot["reason"])

    def test_all_blocked_plus_skipped_sources_still_all_blocked(self):
        # 真实混合场景：网页源被拦截 + API 源缺密钥跳过（有配置但没钥匙）——
        # 仍然是"全被拦截"，不能退化成 all_failed/no_sources。
        path = self.write_sources({"schema_version": 1, "sources": [BING_CFG],
                                   "rss_sources": []})
        provider = search_mod.ApiSearchProvider(dict(BING_CFG, failure_skip_threshold=3))
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", path), \
                mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": ""}):
            out = search_mod.search_all("q", providers=[provider,
                                                        _StubProvider("360", blocked=True)])
        self.assertEqual(out["status"], "all_blocked")
        self.assertTrue(out["sources_configured"])       # 声明了 API 源，只是缺密钥
        self.assertEqual(out["engines"]["bing"]["skipped"], 1)
        self.assertIn("全部检索源被反爬拦截", out["degraded_reason"])

    def test_no_sources_when_nothing_assembled(self):
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", self._empty_config()):
            out = search_mod.search_all("q", providers=[])
        self.assertEqual(out["status"], "no_sources")
        self.assertTrue(out["degraded"])
        self.assertFalse(out["sources_configured"])
        self.assertEqual(out["api_sources"], [])
        self.assertEqual(out["rss_sources"], [])

    def test_no_sources_when_only_web_sources_fail_without_backup(self):
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", self._empty_config()):
            out = search_mod.search_all(
                "q", providers=[_StubProvider("360", err="360请求失败: ConnectionError"),
                                _StubProvider("baidu", err="百度请求失败: Timeout")])
        self.assertEqual(out["status"], "no_sources")
        self.assertFalse(out["sources_configured"])
        self.assertIn("没有备选通道", out["degraded_reason"])

    def test_all_failed_with_structured_sources_configured(self):
        path = self.write_sources({"schema_version": 1, "sources": [BING_CFG],
                                   "rss_sources": []})
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", path):
            out = search_mod.search_all(
                "q", providers=[_StubProvider("360", err="360返回HTTP 500"),
                                _StubProvider("baidu", err="百度返回HTTP 500")])
        self.assertEqual(out["status"], "all_failed")
        self.assertTrue(out["sources_configured"])
        self.assertEqual(out["api_sources"], ["bing"])
        self.assertIn("均失败", out["degraded_reason"])

    def test_partial_keeps_results_and_reports_reason(self):
        stubs = [_StubProvider("360", [{"title": "a", "url": "https://www.gov.cn/a"}]),
                 _StubProvider("baidu", err="百度返回HTTP 500")]
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", self._empty_config()):
            out = search_mod.search_all("q", providers=stubs)
        self.assertEqual(out["status"], "partial")
        self.assertTrue(out["degraded"])
        self.assertIn("baidu", out["degraded_reason"])
        self.assertEqual(len(out["results"]), 1)

    def test_engines_slot_shape_is_stable(self):
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", self._empty_config()):
            out = search_mod.search_all("q", providers=[_StubProvider("360", [])])
        slot = out["engines"]["360"]
        self.assertEqual(set(slot), {"ok", "error", "count", "blocked", "reason"})
        self.assertTrue(slot["ok"])
        self.assertFalse(slot["blocked"])
        self.assertEqual(slot["reason"], "")


# ---------------------------------------------------------------------------
# ⑦ 上层不把 blocked 显示成"没有发现"
# ---------------------------------------------------------------------------

class UpperLayerWiringTests(_ResilienceTestBase):
    """rumor.collect_evidence 与 assistant 接线助手都按 status/all_blocked 如实降级。"""

    def test_collect_evidence_reports_antibot_not_no_findings(self):
        from mylocalai.rumor.evidence import collect_evidence

        def blocked_search(query, k):
            return {"results": [], "engines": {
                        "360": {"ok": False, "error": "正文过短（4 字符），疑似拦截页",
                                "count": 0, "blocked": True,
                                "reason": "反爬拦截（正文过短），剩余 1800 秒"}},
                    "status": "all_blocked", "all_blocked": True, "degraded": True,
                    "sources_configured": False,
                    "degraded_reason": "全部检索源被反爬拦截（360），没有拿到任何结果"}

        book = collect_evidence("某说法是谣言吗", llm_judge=False,
                                search_fn=blocked_search)
        self.assertEqual(book["search_status"], "blocked_by_antibot")
        self.assertIn("反爬拦截", book["error"])
        self.assertNotEqual(book["error"], "搜索无结果", "被拦截不能写成'没有发现'")
        self.assertIn("不等于没有相关发现", book["verdict_hint"])

    def test_collect_evidence_no_sources_is_honest(self):
        from mylocalai.rumor.evidence import collect_evidence

        def no_source_search(query, k):
            return {"results": [], "engines": {}, "status": "no_sources",
                    "all_blocked": False, "degraded": True,
                    "sources_configured": False,
                    "degraded_reason": "未配置 API/RSS 结构化源，网页解析源全部失败"}

        book = collect_evidence("某说法是谣言吗", llm_judge=False,
                                search_fn=no_source_search)
        self.assertEqual(book["search_status"], "search_unavailable")
        self.assertIn("没有可用", book["verdict_hint"])

    def test_collect_evidence_real_empty_still_says_no_result(self):
        from mylocalai.rumor.evidence import collect_evidence

        def empty_search(query, k):
            return {"results": [], "engines": {"360": {"ok": True, "error": "",
                                                       "count": 0, "blocked": False,
                                                       "reason": ""}},
                    "status": "empty", "all_blocked": False, "degraded": False,
                    "sources_configured": False, "degraded_reason": ""}

        book = collect_evidence("某说法是谣言吗", llm_judge=False,
                                search_fn=empty_search)
        self.assertEqual(book["error"], "搜索无结果")     # 真 0 条：口径保持原样
        self.assertNotIn("search_status", book)

    def test_assistant_search_payload_carries_blocked_reason(self):
        from mylocalai.assistant.loop import _search_result_payload

        raw = {"results": [], "engines": {"360": {"ok": False, "blocked": True,
                                                  "error": "验证码", "count": 0,
                                                  "reason": "反爬拦截"}},
               "status": "all_blocked", "all_blocked": True, "degraded": True,
               "sources_configured": False,
               "degraded_reason": "全部检索源被反爬拦截（360）"}
        payload = _search_result_payload(raw)
        self.assertTrue(payload["degraded"])
        self.assertTrue(payload["all_blocked"])
        self.assertEqual(payload["status"], "all_blocked")
        self.assertIn("反爬拦截", payload["error"])
        self.assertFalse(payload["sources_configured"])

    def test_assistant_payload_real_empty_is_not_degraded(self):
        from mylocalai.assistant.loop import _search_result_payload

        raw = {"results": [], "engines": {"360": {"ok": True, "error": "", "count": 0,
                                                  "blocked": False, "reason": ""}},
               "status": "empty", "all_blocked": False, "degraded": False,
               "sources_configured": False, "degraded_reason": ""}
        payload = _search_result_payload(raw)
        self.assertFalse(payload["degraded"])
        self.assertEqual(payload["error"], "")

    def test_health_exposes_cooldowns_and_sources(self):
        from mylocalai.web.app import _retrieval_health

        search_mod.mark_source_blocked("360", "风控页（访问过于频繁）", seconds=60)
        health = _retrieval_health()
        self.assertIn("360", health["cooldowns"])
        self.assertIn("访问过于频繁", health["cooldowns"]["360"]["reason"])
        self.assertIn("sources_configured", health)
        self.assertIn("api_sources", health)


if __name__ == "__main__":
    unittest.main(verbosity=2)
