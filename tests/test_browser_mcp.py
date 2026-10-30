# -*- coding: utf-8 -*-
"""受控浏览（browser.py）、搜索可配置化（search.py）与 MCP 服务器（mcp.py）的离线测试。

不访问任何真实网站：抓取/搜索全部用假函数或 monkeypatch 替身。

运行（项目根目录下）：  ./.venv/Scripts/python.exe -m unittest tests.test_browser_mcp -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.retrieval.browser import BrowserSession, extract_links# noqa: E402
from mylocalai.retrieval.search import GenericAPIProvider, default_providers# noqa: E402

# ---------------------------------------------------------------------------
# 假网页池：一个辟谣页（piyao），页内含媒体/门户/UGC/恶意四类链接
# ---------------------------------------------------------------------------

PAGE_HTML = (
    '<a href="https://www.xinhuanet.com/politics/2026/a.htm">新华网相关报道全文标题较长</a>'
    '<a href="/rel/link">相对路径链接文字太短不算</a>'
    '<a href="https://news.qq.com/2026/b.htm">腾讯新闻转载的相关报道标题也很长</a>'
    '<a href="http://192.168.1.1/admin">内网管理页面链接标题特别长</a>'
    '<a href="https://evil.com/x">恶意站点伪装链接标题很长很长</a>'
    '<a href="https://baijiahao.baidu.com/s?id=1">百家号文章链接标题也很长很长</a>'
)

PAGES = {
    "https://www.piyao.org.cn/a1": {"text": "辟谣声明正文内容。" * 30, "html": PAGE_HTML},
    "https://www.xinhuanet.com/politics/2026/a.htm": {"text": "新华网正文内容。" * 40, "html": ""},
    "https://news.qq.com/2026/b.htm": {"text": "腾讯新闻正文内容。" * 40, "html": ""},
}


def fake_fetch(url, return_html=False):
    p = PAGES.get(url)
    if p is None:
        return None
    out = {"url": url, "text": p["text"], "final_url": url}
    if return_html:
        out["html"] = p["html"]
    return out


class TestBrowserConstraints(unittest.TestCase):
    """浏览会话的每一条安全约束都必须由代码强制。"""

    def _session(self, max_pages=3):
        return BrowserSession(["https://www.piyao.org.cn/a1"],
                              fetch_fn=fake_fetch, max_pages=max_pages)

    def test_seed_only_tiered_links_enter_allowlist(self):
        """只有官方/媒体域名的一跳链接进入允许集；门户、UGC、恶意站点、内网一律拒绝。"""
        s = self._session()
        ok, r = s.read(0)
        self.assertTrue(ok)
        sites = {p["site"] for p in s.list_pages()}
        self.assertIn("xinhuanet.com", sites)          # 权威媒体 → 允许
        self.assertNotIn("news.qq.com", sites)         # 门户 → 不扩链（防无限扩散）
        self.assertNotIn("evil.com", sites)            # 陌生域名 → 拒绝
        self.assertNotIn("192.168.1.1", sites)         # 内网地址 → 拒绝
        self.assertFalse(any("baidu" in x for x in sites))   # UGC → 拒绝

    def test_index_only_api_no_url_parameter(self):
        """read 只接受整数索引：字符串/布尔/越界一律拒绝且不改状态。"""
        s = self._session()
        for bad in ("0", True, None, 99, -1, 1.5):
            ok, r = s.read(bad)     # type: ignore[arg-type]
            self.assertFalse(ok, f"{bad!r} 应被拒绝")
            self.assertIn("error", r)
        self.assertEqual(s.stats()["opened_pages"], 0)

    def test_depth_one_no_expansion(self):
        """只有证据本种子页（深度0）能扩链，深读的页面不再扩链。"""
        s = self._session()
        s.read(0)
        before = len(s.list_pages())
        idx_news = next(p["index"] for p in s.list_pages() if p["site"] == "xinhuanet.com")
        ok, r = s.read(idx_news)
        self.assertTrue(ok)
        self.assertEqual(r.get("new_links"), [])
        self.assertEqual(len(s.list_pages()), before)

    def test_budget_enforced(self):
        """页数预算用完后，即使页面在允许集里也读不了。"""
        s = self._session(max_pages=1)
        s.read(0)
        self.assertEqual(s.stats()["budget_left"], 0)
        idx = next(p["index"] for p in s.list_pages() if p["site"] == "xinhuanet.com")
        ok, r = s.read(idx)
        self.assertFalse(ok)
        self.assertIn("预算", r["error"])

    def test_cache_and_reread(self):
        """重复读同一页走缓存，不消耗预算、不产生新链接。"""
        s = self._session(max_pages=1)
        ok1, r1 = s.read(0)
        ok2, r2 = s.read(0)
        self.assertTrue(ok1 and ok2)
        self.assertTrue(r2.get("cached"))
        self.assertEqual(r2["text"], r1["text"])
        self.assertEqual(r2.get("new_links"), [])

    def test_unreachable_page_rejected(self):
        """抓取失败的页面返回错误且不消耗预算。"""
        s = BrowserSession(["https://www.piyao.org.cn/none"],
                           fetch_fn=lambda u, return_html=False: None, max_pages=3)
        ok, r = s.read(0)
        self.assertFalse(ok)
        self.assertIn("无法读取", r["error"])
        self.assertEqual(s.stats()["opened_pages"], 0)

    def test_extract_links_relative_and_dedup(self):
        """相对链接按 base_url 解析，重复 URL 去重，短标题过滤。"""
        html = ('<a href="/news/1.htm">新华网第一条相关新闻标题很长</a>'
                '<a href="https://www.xinhuanet.com/news/1.htm">同一URL的重复链接标题</a>'
                '<a href="https://www.xinhuanet.com/news/2.htm">新华网第二条新闻标题也很长</a>')
        out = extract_links(html, "https://www.xinhuanet.com/index.htm", max_links=5)
        urls = [x["url"] for x in out]
        self.assertIn("https://www.xinhuanet.com/news/1.htm", urls)
        self.assertEqual(urls.count("https://www.xinhuanet.com/news/1.htm"), 1)
        self.assertEqual(len([u for u in urls if u.endswith("2.htm")]), 1)


class TestSearchConfig(unittest.TestCase):
    """搜索后端可配置化：环境变量选择后端、专用 JSON API 可插拔。"""

    def test_default_providers_env_selection(self):
        with mock.patch.dict(os.environ, {"RUMOR_SEARCH_BACKENDS": "baidu,so360"}):
            provs = default_providers()
            self.assertEqual([type(p).__name__ for p in provs],
                             ["BaiduProvider", "So360Provider"])

    def test_custom_api_requires_url(self):
        """配了 custom-api 但没配 URL → 静默跳过，回退默认。"""
        env = {"RUMOR_SEARCH_BACKENDS": "custom-api",
               "RUMOR_SEARCH_API_URL": "", "RUMOR_SEARCH_API_KEY": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            provs = default_providers()
            self.assertTrue(len(provs) >= 1)
            self.assertNotIn("GenericAPIProvider", [type(p).__name__ for p in provs])

    def test_generic_api_provider_parses_json(self):
        """专用 API：GET 参数带 q/k，JSON results 数组被解析并做信源分级。"""
        captured = {}

        class FakeResp:
            status_code = 200
            def json(self):
                captured["ok"] = True
                return {"results": [
                    {"title": "辟谣声明", "url": "https://www.piyao.org.cn/2026/x.htm",
                     "snippet": "该说法不实", "date": "2026-01-02"},
                    {"title": "无URL条目应跳过"},
                ]}

        def fake_get(url, params=None, headers=None, timeout=None):
            captured.update(url=url, params=params, headers=headers)
            return FakeResp()

        with mock.patch("mylocalai.retrieval.search.requests.get", side_effect=fake_get):
            p = GenericAPIProvider("https://api.example.com/search", key="k3y", name="test-api")
            results, err = p.search("隔夜茶 辟谣", 5)
        self.assertIsNone(err)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].domain_tier, "官方权威")
        self.assertEqual(results[0].engine, "test-api")
        self.assertEqual(captured["params"]["q"], "隔夜茶 辟谣")
        self.assertEqual(captured["headers"]["X-API-Key"], "k3y")

    def test_generic_api_error_paths(self):
        class Resp500:
            status_code = 500
        class RespBad:
            status_code = 200
            def json(self):
                raise ValueError("not json")
        with mock.patch("mylocalai.retrieval.search.requests.get", return_value=Resp500()):
            results, err = GenericAPIProvider("https://x").search("q", 3)
            self.assertEqual(results, [])
            self.assertIn("500", err)
        with mock.patch("mylocalai.retrieval.search.requests.get", return_value=RespBad()):
            results, err = GenericAPIProvider("https://x").search("q", 3)
            self.assertEqual(results, [])
            self.assertIn("JSON", err)


# ---------------------------------------------------------------------------
# MCP 服务器冒烟（内存传输，不发真实网络请求）
# ---------------------------------------------------------------------------

def fake_search_all(query, k=6):
    """假搜索：返回与 piyao 页一致的证据条目（用真实 SearchResult，走 to_dict 路径）。"""
    from mylocalai.retrieval.search import SearchResult
    return {
        "query": query,
        "results": [
            SearchResult("fake", 1, "辟谣标题1", "https://www.piyao.org.cn/a1",
                         "该说法不实，属于谣言。", "2026-01-02", "官方权威"),
            SearchResult("fake", 2, "相关报道标题2",
                         "https://www.xinhuanet.com/politics/2026/a.htm",
                         "相关报道正文。", "2026-01-02", "权威媒体/辟谣平台"),
        ],
        "engines": {"fake": {"ok": True, "count": 2, "error": None}},
    }


class TestMCPServer(unittest.TestCase):
    def _call(self, coro):
        return asyncio.run(coro)

    async def _with_session(self, fn):
        from mcp import ClientSession
        from mcp.client._memory import InMemoryTransport
        from mylocalai.rumor.mcp import server
        async with InMemoryTransport(server)._connect() as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await fn(session)

    def _tool_json(self, result):
        self.assertFalse(result.is_error, getattr(result, "message", ""))
        return json.loads(result.content[0].text)

    def test_list_tools_has_four(self):
        async def run(session):
            tools = await session.list_tools()
            return sorted(t.name for t in tools.tools)
        self.assertEqual(self._call(self._with_session(run)),
                         ["analyze_rumor", "open_evidence_session", "read_page", "search_web"])

    def test_search_web_tool(self):
        async def run(session):
            with mock.patch("mylocalai.rumor.mcp.search_all", side_effect=fake_search_all):
                return await session.call_tool("search_web", {"query": "隔夜茶 辟谣", "k": 2})
        data = self._tool_json(self._call(self._with_session(run)))
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["results"][0]["domain_tier"], "官方权威")

    def test_open_and_read_session_flow(self):
        """完整流程：开会话 → 目录含 index → read_page 读正文 → 非法索引被拒。

        证据检索与立场判定都打补丁：不打补丁会走真实网络与真实 LLM。
        """

        async def run(session):
            with mock.patch("mylocalai.rumor.evidence.search_all", side_effect=fake_search_all), \
                 mock.patch("mylocalai.rumor.evidence.judge_stances_llm",
                            return_value=(["反驳", "支持"], None)), \
                 mock.patch("mylocalai.rumor.evidence.fetch_article", side_effect=fake_fetch), \
                 mock.patch("mylocalai.retrieval.browser.fetch_article", side_effect=fake_fetch):
                opened = await session.call_tool("open_evidence_session",
                                                 {"text": "听说隔夜茶致癌，速转！"})
                data = self._tool_json(opened)
                sid = data["session_id"]
                self.assertEqual([e["index"] for e in data["entries"]], [0, 1])
                page0 = await session.call_tool("read_page", {"session_id": sid, "index": 0})
                page0_data = self._tool_json(page0)
                oob = await session.call_tool("read_page", {"session_id": sid, "index": 99})
                bad = await session.call_tool("read_page", {"session_id": "nope", "index": 0})
                nonint = await session.call_tool("read_page", {"session_id": sid, "index": "x"})
                return (data, page0_data, self._tool_json(oob), self._tool_json(bad),
                        nonint)

        data, page0, oob, bad, nonint = self._call(self._with_session(run))
        self.assertIn("verdict_hint", data)
        self.assertIn("辟谣声明正文内容", page0["text"])
        self.assertEqual(page0["domain_tier"], "官方权威")
        self.assertIn("error", oob)
        self.assertIn("会话不存在", bad["error"])
        # 非整数索引：要么被 MCP 的参数校验直接拒绝，要么被工具内部拒绝——都不能读到页面
        if nonint.is_error:
            self.assertNotIn("辟谣声明正文", str(nonint.content))
        else:
            self.assertIn("整数", json.loads(nonint.content[0].text)["error"])

    def test_open_session_empty_text(self):
        async def run(session):
            return await session.call_tool("open_evidence_session", {"text": "   "})
        data = self._tool_json(self._call(self._with_session(run)))
        self.assertIn("error", data)

    def test_open_session_no_claim_skips(self):
        """无事实性声明：open_evidence_session 返回说明而不是开会话。"""
        async def run(session):
            with mock.patch("mylocalai.rumor.evidence.search_all", side_effect=fake_search_all), \
                 mock.patch("mylocalai.rumor.evidence.judge_stances_llm", return_value=(None, "x")), \
                 mock.patch("mylocalai.rumor.evidence.rule_stance", return_value="无关"):
                return await session.call_tool("open_evidence_session", {"text": "哈哈哈哈哈"})
        data = self._tool_json(self._call(self._with_session(run)))
        self.assertIn("error", data)

    def test_analyze_rumor_pipeline_mode(self):
        """没有可用大模型时 analyze_rumor 应降级为 pipeline 而不是报错。"""
        class StubAgent:
            def __init__(self, *a, **kw):
                pass
            def analyze(self, text):
                return {"label": "低风险", "risk_score": 5, "confidence": "中",
                        "speech_tier": "理性探讨", "signals": [], "advice": "核对来源",
                        "explanation": "模板解释", "mode": "pipeline", "evidence_hint": "",
                        "degraded_components": [], "evidence_book": None}

        async def run(session):
            with mock.patch("mylocalai.rumor.agent.RumorAgent", StubAgent):
                return await session.call_tool("analyze_rumor", {"text": "测试文本"})
        data = self._tool_json(self._call(self._with_session(run)))
        self.assertEqual(data["label"], "低风险")
        self.assertEqual(data["mode"], "pipeline")

    def test_session_ttl_eviction(self):
        """超过 TTL 的会话在新建时被回收。"""
        from mylocalai.rumor import mcp as M
        book = {"entries": [{"url": "https://www.piyao.org.cn/a1"}], "claim": "x",
                "queries": [], "verdict_hint": "", "error": None, "skipped": None}
        with M._sessions_lock:
            M._sessions.clear()
            M._sessions["old"] = {"browser": None, "book": book,
                                  "created": 0, "touched": 0}   # 远古时间戳
        sid = M._new_session("t", book)
        with M._sessions_lock:
            self.assertNotIn("old", M._sessions)
            self.assertIn(sid, M._sessions)
        M._drop_session(sid)
        self.assertIsNone(M._get_session(sid))


if __name__ == "__main__":
    unittest.main()
