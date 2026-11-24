# -*- coding: utf-8 -*-
"""检索多源融合测试（P1-5）：健康度动态降权 / 跨源结果融合 / 查询级缓存。

全程打桩不联网：requests.get/post 一律 mock 或断言禁用；默认装配路径的授权
用 mock 的 load_preferences 放行（与 test_monitor._prefs 同一构造）。覆盖任务书
7 组：
  ①双源结果去重合并（同 URL 保留一份、先到者优先、同 URL 不同摘要不重复计）
  ②单源失败另一源顶上（结果非 0）
  ③全部源失败 → 如实 0 + 全引擎 error 记录（上层按 engines 槽位标 degraded）
  ④健康度跳过（3 连败源被跳过、成功后回归）
  ⑤缓存命中（第二次同查询零外发——打桩计数器验证）
  ⑥TTL 过期后重新外发
  ⑦守卫逐位不变（未授权连缓存都不查、未配置 API 源形状不变、显式 providers 不缓存）

设计取舍：查询级缓存只挂默认装配路径（providers=None）。显式 providers 是调用
方自有传输（测试/本地适配器），缓存它们会冻结其语义，也会让不同用例间同签名
打桩互相串扰（test_monitor 的 domains 直测就共用 ("_StubProvider:stub", 某查询,
("gov.cn",)) 这类键）——所以显式路径永不读写缓存，用例⑦专门钉住这一点。
"""
from __future__ import annotations

import copy
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

from mylocalai.core.privacy import DEFAULT_PREFERENCES, PrivacyPreferences, _validated  # noqa: E402
from mylocalai.retrieval import search as search_mod  # noqa: E402

BING_CFG = {"id": "bing", "protocol": "bing",
            "base_url": "https://api.bing.microsoft.com/v7.0/search",
            "key_env": "BING_SEARCH_API_KEY", "enabled": True,
            "weight": 1.0, "timeout_s": 10}

_BING_PAYLOAD = {"webPages": {"value": [
    {"name": "官方辟谣：某说法不实", "url": "https://www.gov.cn/xinwen/2026-11/20/content.htm",
     "snippet": "某说法与事实不符"},
    {"name": "第二条", "url": "https://unknown.example.com/page", "snippet": "其它内容"},
]}}


class FakeResponse:
    """requests.Response 的最小替身（status_code + json()）。"""

    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


def _allowed_prefs() -> PrivacyPreferences:
    """放行 public-search 的偏好（默认装配路径授权打桩，同 test_monitor._prefs）。"""
    data = json.loads(json.dumps(DEFAULT_PREFERENCES))
    data["save_enabled"] = {"results": True, "raw_input": True, "external_cache": True}
    data["external_processing_enabled"] = True
    data["external_processing_providers"] = ["public-search"]
    return PrivacyPreferences(data)


class _CountingStub:
    """打桩后端（与 provider 契约一致）：计数 search() 调用——缓存命中断言的
    "外发计数器"。truncate=False 时不按 k 截断（用于验证融合后的 limit 口径）。"""

    def __init__(self, name: str, rows: list, err: str = None,
                 truncate: bool = True) -> None:
        self.name = name
        self._rows = rows
        self._err = err
        self._truncate = truncate
        self.calls = 0
        self.queries = []

    def search(self, query: str, k: int = 6):
        self.calls += 1
        self.queries.append(query)
        if self._err:
            return [], self._err
        rows = self._rows[:max(1, int(k))] if self._truncate else list(self._rows)
        out = [search_mod.SearchResult(self.name, i + 1, r.get("title", ""), r["url"],
                                       r.get("snippet", ""), None,
                                       search_mod.classify_domain(r["url"]))
               for i, r in enumerate(rows)]
        return out, None


def _reset_fusion_state() -> None:
    """清进程级融合状态（健康账本 / 查询缓存）：用例互不串扰、不外溢到其它模块。"""
    search_mod._API_SOURCE_HEALTH.clear()
    search_mod._QUERY_CACHE.clear()


def _write_tmp_sources(tmpdir: str, data: object) -> str:
    """临时 search_sources.json（与 test_search_api_source 同款辅助）。"""
    fd, path = tempfile.mkstemp(suffix=".json", dir=tmpdir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return path


class FusionTestBase(unittest.TestCase):
    """每个用例前后都清空健康账本与查询缓存。"""

    def setUp(self):
        _reset_fusion_state()
        self.addCleanup(_reset_fusion_state)


# ---------------------------------------------------------------------------
# ① 跨源结果融合：URL 归一化去重合并
# ---------------------------------------------------------------------------

class FusionDedupTests(FusionTestBase):
    """①双源去重合并：同 URL 保留一份、先到者优先、同 URL 不同摘要不重复计。"""

    def test_dual_sources_merge_with_url_dedup(self):
        a = _CountingStub("src-a", [
            {"title": "A1", "url": "https://www.gov.cn/xinwen/2026/a.htm", "snippet": "官方一"},
            {"title": "A2", "url": "https://xinhuanet.com/2026/b.htm", "snippet": "媒体一"},
        ])
        # B1 是同 URL 异摘要（还带 fragment），B2 是同 URL 尾斜杠变体——都按既有
        # _dedup_url 归一口径（去 fragment、去尾斜杠）与 A1 归并为一条。
        b = _CountingStub("src-b", [
            {"title": "B1", "url": "https://www.gov.cn/xinwen/2026/a.htm#frag",
             "snippet": "官方二（同URL异摘要）"},
            {"title": "B2", "url": "https://www.gov.cn/xinwen/2026/a.htm/",
             "snippet": "官方三（尾斜杠变体）"},
            {"title": "B3", "url": "https://news.example.com/c.htm", "snippet": "其它"},
        ])
        with mock.patch.object(search_mod.requests, "get",
                               side_effect=AssertionError("不得联网")), \
                mock.patch.object(search_mod.requests, "post",
                                  side_effect=AssertionError("不得联网")):
            out = search_mod.search_all("某说法", k=5, providers=[a, b])
        self.assertEqual([r.url for r in out["results"]],
                         ["https://www.gov.cn/xinwen/2026/a.htm",
                          "https://xinhuanet.com/2026/b.htm",
                          "https://news.example.com/c.htm"])
        # 先到者优先：同 URL 保留 src-a 那份（标题/摘要都是先到者的，B1 异摘要丢弃）
        first = out["results"][0]
        self.assertEqual((first.engine, first.title, first.snippet),
                         ("src-a", "A1", "官方一"))
        # 每源 count 记融合前原始条数；融合后总数去重（2+3-2 重复 = 3）
        self.assertEqual(out["engines"]["src-a"]["count"], 2)
        self.assertEqual(out["engines"]["src-b"]["count"], 3)
        self.assertEqual(len(out["results"]), 3)

    def test_merged_total_respects_limit(self):
        a = _CountingStub("src-a", [{"title": f"t{i}", "url": f"https://a.example.com/{i}"}
                                    for i in range(4)], truncate=False)
        out = search_mod.search_all("q", k=2, providers=[a])
        self.assertEqual(len(out["results"]), 2)      # 融合后总数尊重既有 limit（k 条）


# ---------------------------------------------------------------------------
# ② 单源失败另一源顶上
# ---------------------------------------------------------------------------

class FusionFailoverTests(FusionTestBase):
    """②单源失败另一源顶上（结果非 0），失败源留 error 记录。"""

    def test_error_source_failover_to_healthy_source(self):
        dead = _CountingStub("dead", [], err="dead源请求失败: ConnectionError")
        alive = _CountingStub("alive", [{"title": "t", "url": "https://www.gov.cn/a.htm"}])
        out = search_mod.search_all("q", providers=[dead, alive])
        self.assertEqual(len(out["results"]), 1)          # 另一源顶上，结果非 0
        self.assertFalse(out["engines"]["dead"]["ok"])
        self.assertIn("ConnectionError", out["engines"]["dead"]["error"])
        self.assertTrue(out["engines"]["alive"]["ok"])

    def test_raising_source_does_not_kill_search(self):
        class Boom:
            name = "boom"

            def search(self, query, k=6):
                raise RuntimeError("解析器炸了")

        alive = _CountingStub("alive", [{"title": "t", "url": "https://xinhuanet.com/b.htm"}])
        out = search_mod.search_all("q", providers=[Boom(), alive])
        self.assertEqual(len(out["results"]), 1)          # 异常源不拖垮检索
        self.assertIn("RuntimeError", out["engines"]["boom"]["error"])


# ---------------------------------------------------------------------------
# ③ 全部源失败：如实 0 + degraded 记录
# ---------------------------------------------------------------------------

class FusionAllFailedTests(FusionTestBase):
    """③全部源失败 → 如实 0 条 + 每个引擎槽位 ok=False + error（上层
    SearchDiscoveryProvider 按此标 degraded），绝不编造结果、不报错。"""

    def test_all_failed_reports_zero_with_engine_errors(self):
        a = _CountingStub("src-a", [], err="src-a请求失败: Timeout")
        b = _CountingStub("src-b", [], err="src-b返回HTTP 500")
        with mock.patch.object(search_mod.requests, "get",
                               side_effect=AssertionError("不得联网")), \
                mock.patch.object(search_mod.requests, "post",
                                  side_effect=AssertionError("不得联网")):
            out = search_mod.search_all("q", providers=[a, b])
        self.assertEqual(out["results"], [])              # 如实 0
        self.assertFalse(out.get("blocked", False))       # 不是授权闸拦截
        self.assertNotIn("error", out)                    # 不是整条检索失败
        self.assertEqual(set(out["engines"]), {"src-a", "src-b"})
        for slot in out["engines"].values():              # 每个引擎都留下 degraded 记录
            self.assertFalse(slot["ok"])
            self.assertTrue(slot["error"])
        self.assertNotIn("skipped", out)                  # 失败 ≠ 跳过


# ---------------------------------------------------------------------------
# ④ 健康度动态降权：3 连败源被跳过、成功后回归
# ---------------------------------------------------------------------------

class FusionHealthSkipTests(FusionTestBase):
    """④连续失败 ≥ 阈值的源本次 search_all 被跳过（记原因、零外发）；
    跳过给账本降温（暂时跳过、不会永久出局）；成功一次清零、自动回归。"""

    def test_unhealthy_source_skipped_and_recovers_after_success(self):
        provider = search_mod.ApiSearchProvider(dict(BING_CFG, failure_skip_threshold=3))
        healthy = _CountingStub("healthy", [{"title": "t", "url": "https://www.gov.cn/a.htm"}])
        # 3 连败（直连 provider.search，HTTP 打桩失败）→ 实例计数与健康账本同写
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=requests.ConnectionError("boom")):
            for _ in range(3):
                results, err = provider.search("q")
                self.assertEqual(results, [])
        self.assertEqual(provider.consecutive_failures, 3)
        self.assertEqual(search_mod._API_SOURCE_HEALTH.get("bing"), 3)
        # 下一次 search_all：bing 被健康度跳过（零外发、记 skipped 原因），另一源照常
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=AssertionError("健康跳过不得外发")), \
                mock.patch.object(search_mod.requests, "post",
                                  side_effect=AssertionError("健康跳过不得外发")):
            out = search_mod.search_all("q", providers=[provider, healthy])
        self.assertEqual(out.get("skipped"), 1)
        slot = out["engines"]["bing"]
        self.assertEqual(slot["skipped"], 1)
        self.assertEqual(slot["error"], "")               # 跳过 ≠ 报错
        self.assertTrue(slot["skip_reason"].startswith("unhealthy:"))
        self.assertIn("consecutive_failures=3", slot["skip_reason"])
        self.assertIn(">=3", slot["skip_reason"])
        self.assertEqual([r.engine for r in out["results"]], ["healthy"])
        # 跳过本身降温 1（账本 3→2）：源不会永久出局——"暂时跳过并记数"
        self.assertEqual(search_mod._API_SOURCE_HEALTH.get("bing"), 2)
        # 成功一次即清零（直连成功打桩）……
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(200, _BING_PAYLOAD)):
            results, err = provider.search("q")
        self.assertIsNone(err)
        self.assertEqual(provider.consecutive_failures, 0)
        self.assertEqual(search_mod._API_SOURCE_HEALTH.get("bing"), 0)
        # ……随后 search_all 里 bing 自动回归（无需重启、无需人工干预）
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  return_value=FakeResponse(200, _BING_PAYLOAD)):
            out2 = search_mod.search_all("q", providers=[provider])
        self.assertNotIn("skipped", out2)
        self.assertTrue(out2["engines"]["bing"]["ok"])
        self.assertEqual(len(out2["results"]), 2)

    def test_threshold_configurable_lower_threshold_skips_earlier(self):
        provider = search_mod.ApiSearchProvider(dict(BING_CFG, failure_skip_threshold=2))
        healthy = _CountingStub("healthy", [{"title": "t", "url": "https://www.gov.cn/a.htm"}])
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=requests.ConnectionError("boom")):
            provider.search("q")
            provider.search("q")
        self.assertEqual(provider.consecutive_failures, 2)
        with mock.patch.dict(os.environ, {"BING_SEARCH_API_KEY": "test-key"}), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=AssertionError("不得外发")):
            out = search_mod.search_all("q", providers=[provider, healthy])
        self.assertEqual(out.get("skipped"), 1)
        self.assertIn(">=2", out["engines"]["bing"]["skip_reason"])


# ---------------------------------------------------------------------------
# ⑤⑥ 查询级缓存：命中零外发 / TTL 过期重发 / 只缓存成功
# ---------------------------------------------------------------------------

class FusionQueryCacheTests(FusionTestBase):
    """查询级缓存只挂默认装配路径（providers=None）：授权 mock 放行 +
    default_providers 打桩；stub 计数器即"外发计数器"。"""

    def _run_default(self, query: str, stubs, domains=None, k: int = 6):
        """默认装配路径跑一次 search_all（全程禁 requests，零外发由 stub 承担）。

        stubs 可以是单个 stub 或其列表。注意 domains 会构造 site: 变体，provider
        每变体各发一次（既有行为），计数断言要按变体数算。
        """
        stub_list = list(stubs) if isinstance(stubs, (list, tuple)) else [stubs]
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_allowed_prefs()), \
                mock.patch.object(search_mod, "default_providers",
                                  return_value=stub_list), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=AssertionError("不得联网")), \
                mock.patch.object(search_mod.requests, "post",
                                  side_effect=AssertionError("不得联网")):
            return search_mod.search_all(query, k=k, domains=domains)

    def test_cache_hit_zero_redispatch(self):
        stub = _CountingStub("only", [
            {"title": "t1", "url": "https://www.gov.cn/a.htm"},
            {"title": "t2", "url": "https://xinhuanet.com/b.htm"},
        ])
        first = self._run_default("某说法 辟谣", stub)
        self.assertEqual(stub.calls, 1)                   # 首次：真实外发（打桩计数）
        self.assertFalse(first.get("blocked", False))
        second = self._run_default("某说法 辟谣", stub)
        self.assertEqual(stub.calls, 1)                   # 命中：零外发（计数器未增）
        self.assertEqual(set(second), set(first))         # 命中返回与首次形状一致
        self.assertEqual([r.url for r in second["results"]],
                         [r.url for r in first["results"]])
        self.assertEqual(second["engines"], first["engines"])
        # 命中返回的是深拷贝：调用方改动不污染缓存
        second["results"].append("junk")
        third = self._run_default("某说法 辟谣", stub)
        self.assertEqual(stub.calls, 1)
        self.assertEqual(len(third["results"]), 2)

    def test_cache_key_covers_query_sources_and_domains(self):
        rows = [{"title": "t", "url": "https://www.gov.cn/a.htm"}]
        a = _CountingStub("src-a", rows)
        self._run_default("融合键位", [a])
        self.assertEqual(a.calls, 1)
        self._run_default("融合键位", [a])                # 同 (query, sources) → 命中
        self.assertEqual(a.calls, 1)
        self._run_default("融合键位", [a], domains=["gov.cn"])   # domains 变 → miss
        self.assertEqual(a.calls, 3)                      # miss 后重发：原 query + site: 变体共 2 次
        self._run_default("融合键位", [a], domains=["gov.cn"])
        self.assertEqual(a.calls, 3)                      # 同 (query, domains) 键 → 命中零外发
        b = _CountingStub("src-b", rows)
        self._run_default("融合键位", [a, b])             # 源集合变 → miss：两源各 1 变体
        self.assertEqual((a.calls, b.calls), (4, 1))
        self._run_default("融合键位", [a, b])             # 新键命中 → 零外发
        self.assertEqual((a.calls, b.calls), (4, 1))
        self._run_default("融合键位", [a])                # 旧键的缓存互不影响
        self.assertEqual(a.calls, 4)

    def test_only_success_results_are_cached(self):
        dead = _CountingStub("dead", [], err="dead请求失败: Timeout")
        self._run_default("失败查询", [dead])
        self._run_default("失败查询", [dead])
        self.assertEqual(dead.calls, 2)                   # 失败结果不入缓存
        empty = _CountingStub("empty", [])
        self._run_default("空查询", [empty])
        self._run_default("空查询", [empty])
        self.assertEqual(empty.calls, 2)                  # 空结果不入缓存

    def test_ttl_expiry_redispatch(self):
        stub = _CountingStub("only", [{"title": "t", "url": "https://www.gov.cn/a.htm"}])
        with mock.patch.object(search_mod, "_SOURCE_SETTINGS",
                               {"query_cache_ttl_s": 0.1}):
            self._run_default("TTL查询", stub)
            self.assertEqual(stub.calls, 1)
            self._run_default("TTL查询", stub)            # TTL 内：命中零外发
            self.assertEqual(stub.calls, 1)
            time.sleep(0.15)                              # TTL 过期
            self._run_default("TTL查询", stub)            # 重新外发
            self.assertEqual(stub.calls, 2)

    def test_zero_ttl_disables_cache(self):
        stub = _CountingStub("only", [{"title": "t", "url": "https://www.gov.cn/a.htm"}])
        with mock.patch.object(search_mod, "_SOURCE_SETTINGS",
                               {"query_cache_ttl_s": 0}):
            self._run_default("关闭缓存", stub)
            self._run_default("关闭缓存", stub)
        self.assertEqual(stub.calls, 2)                   # ≤0 = 显式关闭，逐次外发


# ---------------------------------------------------------------------------
# ⑦ 守卫：未授权零外发（连缓存都不查）/ 形状逐位不变 / 显式 providers 不缓存
# ---------------------------------------------------------------------------

class FusionGuardTests(FusionTestBase):
    """守卫逐位不变：授权闸语义、未配置 API 源形状、显式 providers 语义全保留。"""

    def test_unauthorized_returns_blocked_before_cache_lookup(self):
        prefs = PrivacyPreferences(_validated(copy.deepcopy(DEFAULT_PREFERENCES)))
        with mock.patch("mylocalai.core.privacy.load_preferences", return_value=prefs), \
                mock.patch.object(search_mod, "default_providers",
                                  side_effect=AssertionError("未授权不得构建 provider")), \
                mock.patch.object(search_mod.requests, "get",
                                  side_effect=AssertionError("未授权不得外发")), \
                mock.patch.object(search_mod.requests, "post",
                                  side_effect=AssertionError("未授权不得外发")):
            out = search_mod.search_all("守卫查询")       # providers=None → 默认装配路径
        self.assertTrue(out["blocked"])
        self.assertEqual(out["results"], [])
        self.assertEqual(out["error"], "external_processing_not_authorized")
        self.assertEqual(search_mod._QUERY_CACHE, {})     # 连缓存都没查、没写

    def test_explicit_providers_never_cached(self):
        stubs = [_CountingStub("360", [{"title": "a", "url": "https://example.com/1"}]),
                 _CountingStub("baidu", [{"title": "b", "url": "https://example.com/2"}])]
        out = search_mod.search_all("守卫查询", providers=stubs)
        self.assertEqual(set(out), {"query", "results", "engines"})   # 形状逐位不变
        for slot in out["engines"].values():
            self.assertEqual(set(slot), {"ok", "error", "count"})     # 槽位键逐位不变
        out2 = search_mod.search_all("守卫查询", providers=stubs)
        self.assertEqual((stubs[0].calls, stubs[1].calls), (2, 2))    # 显式路径逐次外发
        self.assertEqual([r.url for r in out2["results"]],
                         [r.url for r in out["results"]])

    def test_default_assembly_unchanged_without_api_sources(self):
        # 未配置 API 源（空源清单 + 默认后端环境）：default_providers 仍返回爬虫两源，
        # 装配契约与加融合逻辑前逐位一致。
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_tmp_sources(tmp, {"schema_version": 1, "sources": []})
            with mock.patch.object(search_mod, "_SEARCH_SOURCES_PATH", path), \
                    mock.patch.dict(os.environ) as env:
                env.pop("RUMOR_SEARCH_BACKENDS", None)
                env.pop("RUMOR_SEARCH_API_URL", None)
                providers = search_mod.default_providers()
        self.assertEqual([type(p).__name__ for p in providers],
                         ["So360Provider", "BaiduProvider"])


if __name__ == "__main__":
    unittest.main()
