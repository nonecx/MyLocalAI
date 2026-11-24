# -*- coding: utf-8 -*-
"""API 型搜索源测试（P0-1 检索源结构化升级）。

全程打桩不联网：requests.get/post 一律 mock；密钥只在测试进程的环境变量里
造出（值是占位串，不落盘、不进快照）。覆盖任务书 6 组：
  ①bing/bocha 响应 normalize 正确（含 domain 归一）
  ②密钥缺失 → 源跳过 + skipped 计数 + 零外发
  ③授权未开 → API 源零 HTTP 请求（对齐 test_retrieval_consent 的断言风格）
  ④配置 JSON 损坏 → 退回无 API 源 + 告警，主链路照常
  ⑤未配置 API 源时逐位不变守卫
  ⑥consecutive_failures 计数正确
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.core.privacy import DEFAULT_PREFERENCES, PrivacyPreferences, _validated  # noqa: E402
from mylocalai.retrieval import search as search_mod  # noqa: E402

BING_CFG = {"id": "bing", "protocol": "bing",
            "base_url": "https://api.bing.microsoft.com/v7.0/search",
            "key_env": "BING_SEARCH_API_KEY", "enabled": True,
            "weight": 1.0, "timeout_s": 10}
BOCHA_CFG = {"id": "bocha", "protocol": "bocha",
             "base_url": "https://api.bochaai.com/v1/web-search",
             "key_env": "BOCHA_API_KEY", "enabled": True,
             "weight": 1.0, "timeout_s": 10}

_BING_PAYLOAD = {"webPages": {"value": [
    {"name": "<b>官方</b>辟谣：某说法不实", "url": "https://www.gov.cn/xinwen/2026-11/20/content.htm",
     "snippet": "某说法与事实不符", "dateLastCrawled": "2026-11-20T08:00:00.0000000Z"},
    {"name": "无日期条目", "url": "https://unknown.example.com/page", "snippet": "其它内容"},
]}}
_BOCHA_PAYLOAD = {"code": 200, "data": {"webPages": {"value": [
    {"name": "权威媒体解读", "url": "https://xinhuanet.com/2026-11/20/a.htm",
     "summary": "<p>摘要<em>带标签</em></p>", "dateLastCrawled": "2026-11-20T09:00:00Z"},
]}}}


class FakeResponse:
    """requests.Response 的最小替身（status_code + json()）。"""

    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


def _write_tmp_sources(tmpdir: str, data: object) -> str:
    """临时 search_sources.json（或故意写坏的内容），返回路径供 monkeypatch。"""
    fd, path = tempfile.mkstemp(suffix=".json", dir=tmpdir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if isinstance(data, str):
            fh.write(data)                       # 故意损坏的 JSON
        else:
            json.dump(data, fh, ensure_ascii=False)
    return path


def _source_warnings() -> list:
    from mylocalai.config.rules import load_warnings
    return [w for w in load_warnings() if "search_sources.json" in w]


class SearchApiSourceNormalizeTests(unittest.TestCase):
    """①响应 normalize 正确（含 domain 归一、请求形状正确）。"""

    def test_bing_normalize_and_request_shape(self):
        provider = search_mod.ApiSearchProvider(BING_CFG)
        self.assertEqual(provider.name, "bing")
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(200, _BING_PAYLOAD)) as get, \
                mock.patch.object(search_mod.requests, "post",
                                  side_effect=AssertionError("bing 不得发 POST")):
            results, err = provider.search("某说法 辟谣", k=5)
        self.assertIsNone(err)
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertEqual((first.engine, first.rank), ("bing", 1))
        self.assertEqual(first.title, "官方辟谣：某说法不实")       # 标签已剥
        self.assertEqual(first.url, "https://www.gov.cn/xinwen/2026-11/20/content.htm")
        self.assertEqual(first.snippet, "某说法与事实不符")
        self.assertEqual(first.date, "2026-11-20")                 # ISO 串走既有 extract_date
        self.assertEqual(first.domain_tier, "官方权威")            # classify_domain 口径归一
        self.assertEqual(results[1].domain_tier, "未知来源")
        self.assertEqual(results[1].date, None)
        # 请求形状：GET + Ocp-Apim-Subscription-Key 头 + q/count 参数
        args, kwargs = get.call_args
        self.assertEqual(args[0], BING_CFG["base_url"])
        self.assertEqual(kwargs["headers"]["Ocp-Apim-Subscription-Key"], "test-key")
        self.assertEqual(kwargs["params"]["q"], "某说法 辟谣")
        self.assertEqual(kwargs["params"]["count"], 5)
        # to_dict 形状与既有 SearchResult 相同
        self.assertEqual(set(first.to_dict()),
                         {"engine", "rank", "title", "url", "snippet", "date",
                          "domain_tier", "full_text"})

    def test_bocha_normalize_and_request_shape(self):
        provider = search_mod.ApiSearchProvider(BOCHA_CFG)
        self.assertEqual(provider.name, "bocha")
        with mock.patch.dict(os.environ, {"BOCHA_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "post",
                                  return_value=FakeResponse(200, _BOCHA_PAYLOAD)) as post, \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=AssertionError("bocha 不得发 GET")):
            results, err = provider.search("某说法 辟谣", k=3)
        self.assertIsNone(err)
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row.engine, "bocha")
        self.assertEqual(row.title, "权威媒体解读")
        self.assertEqual(row.snippet, "摘要带标签")                # summary 字段 + 去标签
        self.assertEqual(row.date, "2026-11-20")
        self.assertEqual(row.domain_tier, "权威媒体/辟谣平台")
        args, kwargs = post.call_args
        self.assertEqual(args[0], BOCHA_CFG["base_url"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(kwargs["json"], {"query": "某说法 辟谣", "count": 3, "summary": True})

    def test_http_error_and_bad_json_fail_soft(self):
        provider = search_mod.ApiSearchProvider(BING_CFG)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(401, {})):
            results, err = provider.search("q")
        self.assertEqual(results, [])
        self.assertIn("HTTP 401", err)
        self.assertNotIn("test-key", err)                          # 密钥绝不进错误串
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(200, {"webPages": {"value": []}})):
            results, err = provider.search("q")
        self.assertEqual((results, err), ([], None))               # 空结果 ≠ 失败

    def test_missing_webpages_section_yields_empty(self):
        provider = search_mod.ApiSearchProvider(BING_CFG)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(200, {"unexpected": True})):
            results, err = provider.search("q")
        self.assertEqual((results, err), ([], None))


class SearchApiSourceSkipTests(unittest.TestCase):
    """②密钥缺失 → 源跳过 + skipped 计数 + 零外发。"""

    def test_skip_reason_without_key(self):
        provider = search_mod.ApiSearchProvider(BING_CFG)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": ""}):
            reason = provider.skip_reason()
        self.assertIn("BING_SEARCH_API_KEY", reason)
        self.assertNotIn("test-key", reason)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}):
            self.assertEqual(provider.skip_reason(), "")

    def test_missing_key_counts_skipped_and_never_sends_http(self):
        stub = _StubProvider("stub", [{"title": "t", "url": "https://example.com/a"}])
        provider = search_mod.ApiSearchProvider(BING_CFG)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": ""}), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=AssertionError("密钥缺失仍发请求")), \
                mock.patch.object(search_mod.requests, "post",
                                  side_effect=AssertionError("密钥缺失仍发请求")):
            out = search_mod.search_all("q", providers=[provider, stub])
        self.assertEqual(out.get("skipped"), 1)
        bing_slot = out["engines"]["bing"]
        self.assertEqual(bing_slot["skipped"], 1)
        self.assertIn("BING_SEARCH_API_KEY", bing_slot["skip_reason"])
        self.assertEqual(bing_slot["error"], "")                   # 跳过 ≠ 报错
        self.assertEqual(bing_slot["count"], 0)
        self.assertEqual([r.url for r in out["results"]], ["https://example.com/a"])
        self.assertEqual(provider.consecutive_failures, 0)         # 未发请求，不计失败


class _StubProvider:
    """显式 providers 打桩后端（与 provider 契约一致，无 skip_reason）。"""

    def __init__(self, name: str, rows: list) -> None:
        self.name = name
        self._rows = rows

    def search(self, query: str, k: int = 6):
        out = [search_mod.SearchResult(self.name, i + 1, r["title"], r["url"],
                                       r.get("snippet", ""), None,
                                       search_mod.classify_domain(r["url"]))
               for i, r in enumerate(self._rows)]
        return out, None


class SearchApiSourceConsentTests(unittest.TestCase):
    """③授权未开 → API 源零 HTTP 请求（public-search 闸语义完全保留）。"""

    def test_unauthorized_blocks_before_any_provider_or_http(self):
        prefs = PrivacyPreferences(_validated(copy.deepcopy(DEFAULT_PREFERENCES)))
        provider = search_mod.ApiSearchProvider(BING_CFG)
        with mock.patch("mylocalai.core.privacy.load_preferences", return_value=prefs), \
                mock.patch.object(search_mod, "default_providers",
                                  side_effect=AssertionError("未授权不得构建 provider")), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=AssertionError("未授权仍有 HTTP 外发")), \
                mock.patch.object(search_mod.requests, "post",
                                  side_effect=AssertionError("未授权仍有 HTTP 外发")):
            out = search_mod.search_all("query")                   # providers=None → 走默认装配
        self.assertTrue(out["blocked"])
        self.assertEqual(out["results"], [])
        self.assertEqual(out["error"], "external_processing_not_authorized")
        self.assertEqual(provider.consecutive_failures, 0)

    def test_authorized_api_source_runs_through_same_gate(self):
        # authorized=True（显式放行）时 API 源正常工作：与爬虫源同一条闸的不同分支。
        provider = search_mod.ApiSearchProvider(BING_CFG)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(200, _BING_PAYLOAD)):
            out = search_mod.search_all("q", providers=[provider])
        self.assertFalse(out.get("blocked", False))
        self.assertEqual([r.engine for r in out["results"]], ["bing", "bing"])


class SearchApiSourceConfigTests(unittest.TestCase):
    """④配置 JSON 损坏 → 退回无 API 源 + 告警，主链路照常。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_corrupt_json_returns_empty_with_warning(self):
        path = _write_tmp_sources(self._tmp.name, "{ 不是 json")
        self.assertEqual(search_mod.load_api_source_configs(path), [])
        warnings = _source_warnings()
        self.assertTrue(any("读取失败" in w for w in warnings))

    def test_schema_version_mismatch_returns_empty_with_warning(self):
        path = _write_tmp_sources(self._tmp.name,
                                  {"schema_version": 99, "sources": [BING_CFG]})
        self.assertEqual(search_mod.load_api_source_configs(path), [])
        self.assertTrue(any("schema_version" in w for w in _source_warnings()))

    def test_sources_section_not_a_list_warns(self):
        path = _write_tmp_sources(self._tmp.name,
                                  {"schema_version": 1, "sources": "bing"})
        self.assertEqual(search_mod.load_api_source_configs(path), [])
        self.assertTrue(any("必须是数组" in w for w in _source_warnings()))

    def test_bad_rows_dropped_good_rows_kept(self):
        broken = dict(BING_CFG, protocol="serper")              # 未知协议
        no_url = dict(BOCHA_CFG, base_url="ftp://x")            # 非 http(s)
        no_env = {"id": "orphan", "protocol": "bing",
                  "base_url": "https://api.bing.microsoft.com/v7.0/search"}  # 缺 key_env
        path = _write_tmp_sources(self._tmp.name,
                                  {"schema_version": 1,
                                   "sources": [BING_CFG, broken, no_url, no_env, "junk"]})
        cfgs = search_mod.load_api_source_configs(path)
        self.assertEqual([c["id"] for c in cfgs], ["bing"])
        warnings = _source_warnings()
        self.assertTrue(any("已丢弃" in w and "protocol 未知" in w for w in warnings))

    def test_disabled_source_filtered_out(self):
        path = _write_tmp_sources(self._tmp.name,
                                  {"schema_version": 1,
                                   "sources": [dict(BING_CFG, enabled=False), BOCHA_CFG]})
        cfgs = search_mod.load_api_source_configs(path)
        self.assertEqual([c["id"] for c in cfgs], ["bocha"])

    def test_missing_file_is_legit_empty_without_warning(self):
        # 先制造一条告警，再读一个不存在的文件：告警必须被清掉（只反映本次加载）。
        bad = _write_tmp_sources(self._tmp.name, "{ 不是 json")
        self.assertEqual(search_mod.load_api_source_configs(bad), [])
        self.assertTrue(_source_warnings())
        self.assertEqual(search_mod.load_api_source_configs(
            os.path.join(self._tmp.name, "nope.json")), [])
        self.assertEqual(_source_warnings(), [])

    def test_bundled_config_loads_two_sources(self):
        # 包内默认文件：恰好声明 bing/bocha 两个源（schema 校验通过）。
        cfgs = search_mod.load_api_source_configs()             # 默认路径 = 包内文件
        self.assertEqual([c["id"] for c in cfgs], ["bing", "bocha"])

    def test_main_chain_immune_to_corrupt_config(self):
        # 配置损坏期间，检索主链路照常（显式 provider 路径不受任何影响）。
        path = _write_tmp_sources(self._tmp.name, "{ 不是 json")
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", path):
            stub = _StubProvider("stub", [{"title": "t", "url": "https://example.com/a"}])
            out = search_mod.search_all("q", providers=[stub])
        self.assertEqual([r.url for r in out["results"]], ["https://example.com/a"])
        self.assertNotIn("skipped", out)


class SearchApiSourceBitwiseGuardTests(unittest.TestCase):
    """⑤未配置任何 API 源时，search_all / default_providers 行为逐位不变。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_default_providers_unchanged_without_api_sources(self):
        path = _write_tmp_sources(self._tmp.name, {"schema_version": 1, "sources": []})
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", path), \
                mock.patch.dict(os.environ) as env:
            env.pop("RUMOR_SEARCH_BACKENDS", None)
            env.pop("RUMOR_SEARCH_API_URL", None)
            providers = search_mod.default_providers()
        self.assertEqual([type(p).__name__ for p in providers],
                         ["So360Provider", "BaiduProvider"])

    def test_search_all_output_shape_unchanged_without_api_sources(self):
        path = _write_tmp_sources(self._tmp.name, {"schema_version": 1, "sources": []})
        stubs = [_StubProvider("360", [{"title": "a", "url": "https://example.com/1"}]),
                 _StubProvider("baidu", [{"title": "b", "url": "https://example.com/2"}])]
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", path), \
                mock.patch.object(search_mod, "default_providers", return_value=stubs):
            out = search_mod.search_all("q")
        self.assertEqual(set(out), {"query", "results", "engines"})   # 无 skipped/scope 键
        self.assertEqual(set(out["engines"]), {"360", "baidu"})
        for slot in out["engines"].values():
            self.assertEqual(set(slot), {"ok", "error", "count"})     # 槽位键逐位不变
        self.assertEqual(len(out["results"]), 2)
        self.assertFalse(out.get("blocked", False))

    def test_default_providers_fallback_unchanged_when_backends_empty(self):
        # RUMOR_SEARCH_BACKENDS 显式置空且无 API 源：兜底 [So360, Baidu] 与从前一致。
        path = _write_tmp_sources(self._tmp.name, {"schema_version": 1, "sources": []})
        with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", path), \
                mock.patch.dict(os.environ, {"RUMOR_SEARCH_BACKENDS": " "}):
            providers = search_mod.default_providers()
        self.assertEqual([type(p).__name__ for p in providers],
                         ["So360Provider", "BaiduProvider"])

    def test_bundled_sources_without_keys_not_assembled(self):
        # 包内文件声明了 bing/bocha，但密钥缺失 → "enabled + 密钥共同决定"的启用
        # 口径下不进默认装配：default_providers 与从前逐位一致（既有装配契约不变）。
        # patch.dict 显式置空两个密钥（覆盖宿主环境可能已配置的真实值），
        # 同时断言零 HTTP——打桩纪律不依赖宿主环境的巧合。
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "",
                                          "BOCHA_API_KEY": ""}), \
                mock.patch.dict(os.environ) as env:
            env.pop("RUMOR_SEARCH_BACKENDS", None)
            with mock.patch.object(search_mod.requests, "get",
                                   side_effect=AssertionError("不应发请求")), \
                    mock.patch.object(search_mod.requests, "post",
                                      side_effect=AssertionError("不应发请求")):
                providers = search_mod.default_providers()
        self.assertEqual([type(p).__name__ for p in providers],
                         ["So360Provider", "BaiduProvider"])

    def test_bundled_sources_with_keys_assembled_first(self):
        # 两个密钥都配上 → API 源进默认装配且排在爬虫源前面（专用 API 优先口径）。
        # 只装配不执行，无 HTTP。
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key",
                                          "BOCHA_API_KEY": "test-key"}), \
                mock.patch.dict(os.environ) as env:
            env.pop("RUMOR_SEARCH_BACKENDS", None)
            providers = search_mod.default_providers()
        self.assertEqual([type(p).__name__ for p in providers],
                         ["ApiSearchProvider", "ApiSearchProvider",
                          "So360Provider", "BaiduProvider"])
        self.assertEqual([p.name for p in providers[:2]], ["bing", "bocha"])
        # 只配一把钥匙：只有对应的源进装配。
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key",
                                          "BOCHA_API_KEY": ""}), \
                mock.patch.dict(os.environ) as env:
            env.pop("RUMOR_SEARCH_BACKENDS", None)
            providers = search_mod.default_providers()
        self.assertEqual([type(p).__name__ for p in providers],
                         ["ApiSearchProvider", "So360Provider", "BaiduProvider"])


class SearchApiSourceFailureCounterTests(unittest.TestCase):
    """⑥consecutive_failures 计数正确（只计数与日志，不做跳过策略）。"""

    def test_counter_increments_on_failures_and_resets_on_success(self):
        provider = search_mod.ApiSearchProvider(BING_CFG)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=requests.ConnectionError("boom")) as get:
            for expected in (1, 2, 3):
                results, err = provider.search("q")
                self.assertEqual(results, [])
                self.assertIn("ConnectionError", err)
                self.assertEqual(provider.consecutive_failures, expected)
        self.assertEqual(get.call_count, 3)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(200, _BING_PAYLOAD)):
            results, err = provider.search("q")
            self.assertIsNone(err)
            self.assertEqual(provider.consecutive_failures, 0)    # 成功清零

    def test_counter_isolated_per_source(self):
        a = search_mod.ApiSearchProvider(BING_CFG)
        b = search_mod.ApiSearchProvider(BOCHA_CFG)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(500, {})), \
                mock.patch.dict(os.environ, {"BOCHA_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "post",
                                  return_value=FakeResponse(200, _BOCHA_PAYLOAD)):
            a.search("q")
            a.search("q")
            b.search("q")
        self.assertEqual(a.consecutive_failures, 2)
        self.assertEqual(b.consecutive_failures, 0)


if __name__ == "__main__":
    unittest.main()
