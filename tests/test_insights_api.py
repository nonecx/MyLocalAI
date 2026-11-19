# -*- coding: utf-8 -*-
"""L4 参谋 Web 层测试：三个只读聚合端点 + 前后端自洽（零 LLM、零网络）。

验收口径（中文 docstring 即契约）：
  - GET /api/insights/patterns|sources|weekly 预置数据后 200，顶层键分别为
    patterns / profiles / digest，形状与数值对拍 dossier.insights 纯函数口径；
  - 空库 → 三个端点都 200 且返回合法零结构（"暂无数据"是合法输出）；
  - weekly 的 days 用字符串手工解析：0 / 91 / abc / -1 → 400 {"error": "invalid_days"}，
    缺省 7 天（不用 FastAPI 类型校验，保持 400 语义）；
  - 前后端自洽（照 test_website_chat / test_dossier_api 先例）：单页化后渲染层
    在 static/cards.js 的「我的画像」区（renderInsights），用正则从该区提取它点号
    访问的 snake_case 响应字段，断言每个都真实出现在对应端点的响应 JSON 里，
    并点名核对 risk_patterns / common_pairs / qualified / top_refute / by_label /
    period 六个关键字段；入口在 index.html 左栏「我的画像」（id=chatInsights +
    三个 data-insights-kind 按钮），经 chat.js 的 insights action 走 /api/chat。

数据用 DossierStore 临时库预置，mock.patch.object 注入 app.get_dossier_service
（与 tests/test_dossier_api.py 的 DossierApiTestBase 同一模式）。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_insights_api -v
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到 web/app.py

STATIC_DIR = ROOT / "src" / "mylocalai" / "web" / "static"


def _read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _deep_keys(obj: object, acc: set = None) -> set:
    """递归收集 dict 键名（含 list 内层元素），供"前端消费的字段后端真的给"断言用。"""
    if acc is None:
        acc = set()
    if isinstance(obj, dict):
        for key, val in obj.items():
            acc.add(str(key))
            _deep_keys(val, acc)
    elif isinstance(obj, list):
        for item in obj:
            _deep_keys(item, acc)
    return acc


class InsightsApiTestBase(unittest.TestCase):
    """公共夹具：临时档案库 + 服务工厂注入 + 三套预置助手（marker/证据/快照）。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001  fastapi 缺失时整组跳过而非报错
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.client = TestClient(webapp.app)

    def setUp(self) -> None:
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DossierStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)
        self.service = DossierService(self.store)
        patcher = mock.patch.object(self.webapp, "get_dossier_service",
                                    lambda: self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, path: str):
        return self.client.get(path)

    # ---- 预置助手（走 store 公开 API；marker 行用 record_markers 幂等落库） ----

    def _dossier(self, title: str, label: str = None, risk: float = 60.0) -> int:
        did = self.store.create_dossier(title, title + "的原文", input_kind="text")
        if label is not None:
            self.store.add_conclusion(did, label, risk, as_of="2026-10-31T10:00:00+08:00")
        return did

    def _evidence(self, did: int, url: str, stance: str, tier: str = "",
                  fetched_at: str = "2026-10-31T09:00:00+08:00") -> None:
        self.store.add_evidence(did, 1, [{"url": url, "domain_tier": tier,
                                          "stance": stance, "fetched_at": fetched_at}])

    def _markers(self, did: int, risk_keys, cred_keys=()) -> None:
        """按 patterns.scan_markers 的返回形状落一行 marker（key→中文标签与 patterns.py 同名）。"""
        labels = {"source_vague": "来源模糊", "fear_claim": "恐吓性断言",
                  "virality": "诱导传播", "conspiracy": "阴谋叙事",
                  "source_cited": "引用了来源", "official": "官方渠道"}

        def hits(keys) -> dict:
            return {k: {"label": labels.get(k, k), "hits": [k + "片段"], "points": 6}
                    for k in keys}

        self.store.record_markers(did, {"risk_hits": hits(risk_keys),
                                        "credibility_hits": hits(cred_keys),
                                        "marker_points": 12, "credibility_points": 0})

    def _seed_pattern_library(self) -> None:
        """三档案：d1 高风险（两类话术）、d2 疑似（三类话术+可信度线索）、d3 低风险无标记。"""
        d1 = self._dossier("传闻一", "高风险谣言", 80.0)
        self._markers(d1, ["source_vague", "fear_claim"])
        d2 = self._dossier("传闻二", "疑似谣言", 62.0)
        self._markers(d2, ["source_vague", "fear_claim", "virality"], cred_keys=["official"])
        self._dossier("今日天气", "低风险", 20.0)   # 无 marker 行

    def _seed_source_profiles(self) -> None:
        """bad.example.com 反驳两个高风险/疑似档案（达标）；other.example.net 只反驳低风险（不计战绩）。"""
        da = self._dossier("档案A", "高风险谣言", 80.0)
        self._evidence(da, "https://bad.example.com/a", "反驳", tier="官方权威")
        db = self._dossier("档案B", "疑似谣言", 62.0)
        self._evidence(db, "https://bad.example.com/b?x=1", "反驳", tier="自媒体/UGC")
        dc = self._dossier("档案C", "低风险", 20.0)
        self._evidence(dc, "https://bad.example.com/c", "支持", tier="官方权威")
        dd = self._dossier("档案D", "低风险", 20.0)
        self._evidence(dd, "https://other.example.net/d", "反驳")

    def _seed_window(self) -> None:
        """一个"刚刚"的档案：结论 + 反驳证据 + marker 行 + 热度快照（全落在周报窗口内）。"""
        did = self._dossier("隔夜茶致癌？", "疑似谣言", 62.0)
        self._evidence(did, "https://bad.example.com/a", "反驳", tier="官方权威")
        self._markers(did, ["source_vague"])
        self.store.add_snapshot(did, 3.0, 12000.0, 45.0, 51.2)


class TestInsightsPatternsApi(InsightsApiTestBase):
    """GET /api/insights/patterns：话术模式库的形状与数值。"""

    def test_patterns_shape_and_values(self):
        """预置三档案两份 marker → 200、顶层 patterns、计数/分布/榜单/组合数值正确。"""
        self._seed_pattern_library()
        resp = self.get("/api/insights/patterns")
        self.assertEqual(resp.status_code, 200)
        p = resp.json()["patterns"]
        self.assertEqual(p["total_dossiers"], 3)
        self.assertEqual(p["with_markers"], 2)
        self.assertEqual(p["by_label"], {"高风险谣言": 1, "疑似谣言": 1, "低风险": 1})
        # risk_patterns 按 dossiers 降序、key 升序；share = dossiers/with_markers
        self.assertEqual([row["key"] for row in p["risk_patterns"]],
                         ["fear_claim", "source_vague", "virality"])
        top = p["risk_patterns"][0]
        self.assertEqual(top["label"], "恐吓性断言")
        self.assertEqual(top["dossiers"], 2)
        self.assertEqual(top["share"], 1.0)
        self.assertEqual(top["by_label"], {"高风险谣言": 1, "疑似谣言": 1})
        # 可信度线索单独一榜
        self.assertEqual([row["key"] for row in p["credibility_patterns"]], ["official"])
        # common_pairs：同一对 key 同时命中 ≥2 档案才收录
        self.assertEqual(p["common_pairs"],
                         [{"keys": ["fear_claim", "source_vague"],
                           "labels": ["恐吓性断言", "来源模糊"], "dossiers": 2}])


class TestInsightsSourcesApi(InsightsApiTestBase):
    """GET /api/insights/sources：信源画像的形状与数值。"""

    def test_sources_shape_and_values(self):
        """反驳史达标域名 qualified=True 排最前；只反驳低风险档案的域名不计战绩。"""
        self._seed_source_profiles()
        resp = self.get("/api/insights/sources")
        self.assertEqual(resp.status_code, 200)
        profiles = resp.json()["profiles"]
        self.assertEqual([p["domain"] for p in profiles],
                         ["bad.example.com", "other.example.net"])   # refuted 降序
        bad = profiles[0]
        self.assertEqual(bad["tier"], "官方权威")   # 众数档位
        self.assertEqual((bad["total_entries"], bad["refute_entries"], bad["support_entries"]),
                         (3, 2, 1))
        self.assertEqual(bad["refuted_dossiers"], 2)      # 高风险 + 疑似
        self.assertEqual(bad["supporting_dossiers"], 1)   # 低风险
        self.assertTrue(bad["qualified"])
        self.assertTrue(bad["last_seen"])
        other = profiles[1]
        self.assertEqual(other["refuted_dossiers"], 0)    # 反驳低风险档案不算战绩
        self.assertFalse(other["qualified"])


class TestInsightsWeeklyApi(InsightsApiTestBase):
    """GET /api/insights/weekly：周报形状、days 参数语义。"""

    def test_weekly_shape_and_values(self):
        """窗口内档案/结论/快照/marker 计数正确；runs/tool_runs 成本为零结构、as_of 非空。"""
        self._seed_window()
        resp = self.get("/api/insights/weekly")
        self.assertEqual(resp.status_code, 200)
        d = resp.json()["digest"]
        self.assertEqual(d["period"]["days"], 7)
        self.assertLess(d["period"]["start"], d["period"]["end"])
        self.assertEqual(d["dossiers"]["new_count"], 1)
        self.assertEqual(d["conclusions"]["count"], 1)
        self.assertEqual(d["conclusions"]["by_label"], {"疑似谣言": 1})
        self.assertEqual(d["heat"]["snapshot_count"], 1)
        self.assertEqual([row["key"] for row in d["patterns"]["top"]], ["source_vague"])
        self.assertEqual(d["sources"]["top_refute"],
                         [{"domain": "bad.example.com", "refute_entries": 1}])
        self.assertEqual(d["runs"]["requests"], 0)
        self.assertEqual(d["runs"]["tokens"], 0)
        self.assertEqual(d["tool_runs"]["requests"], 0)
        self.assertEqual(d["tool_runs"]["tokens"], 0)
        self.assertTrue(d["as_of"])

    def test_weekly_days_custom_window(self):
        """days=3 → period.days=3，窗口仍覆盖刚预置的数据。"""
        self._seed_window()
        d = self.get("/api/insights/weekly?days=3").json()["digest"]
        self.assertEqual(d["period"]["days"], 3)
        self.assertEqual(d["dossiers"]["new_count"], 1)

    def test_weekly_default_days_is_seven(self):
        """缺省不带 days 参数 → 7 天（端点层缺省，非框架校验）。"""
        d = self.get("/api/insights/weekly").json()["digest"]
        self.assertEqual(d["period"]["days"], 7)

    def test_weekly_invalid_days_are_400(self):
        """days=0 / 91 / abc / -1 → 一律 400 {"error": "invalid_days"}（保持 400 语义）。"""
        for query in ("days=0", "days=91", "days=abc", "days=-1"):
            resp = self.get(f"/api/insights/weekly?{query}")
            self.assertEqual(resp.status_code, 400, query)
            self.assertEqual(resp.json(), {"error": "invalid_days"})


class TestInsightsEmptyStore(InsightsApiTestBase):
    """空库：三端点都 200 且返回合法零结构（前端"暂无数据"是合法输出）。"""

    def test_empty_store_returns_legal_zero_structures(self):
        p = self.get("/api/insights/patterns").json()["patterns"]
        self.assertEqual(p, {"total_dossiers": 0, "with_markers": 0, "by_label": {},
                             "risk_patterns": [], "credibility_patterns": [],
                             "common_pairs": []})
        self.assertEqual(self.get("/api/insights/sources").json()["profiles"], [])
        d = self.get("/api/insights/weekly").json()["digest"]
        self.assertEqual(d["period"]["days"], 7)
        self.assertEqual(d["dossiers"]["new_count"], 0)
        self.assertEqual(d["conclusions"], {"count": 0, "by_label": {}})
        self.assertEqual(d["runs"]["requests"], 0)
        self.assertEqual(d["tool_runs"]["tokens"], 0)
        self.assertEqual(d["patterns"]["top"], [])
        self.assertEqual(d["sources"]["top_refute"], [])
        self.assertEqual(d["heat"]["snapshot_count"], 0)


class TestInsightsFrontendConsistency(InsightsApiTestBase):
    """前后端自洽（单页化：原 static/insights.js 的渲染逻辑迁进 static/cards.js）。

    单页化把「我的画像」从独立参谋 Tab 改成左栏入口 + 消息流里的 insights 卡：
      - 入口：index.html 的 id=chatInsights 面板 + 三个 data-insights-kind 按钮
        （patterns / sources / weekly），点击经 chat.js 发 /api/chat 的 insights action；
      - 渲染：cards.js 的 renderInsights（card_type==="insights" 分发分支）；
      - 后端：/api/insights/* 三个只读端点契约不变（本模块前四组用例照旧全量覆盖）。
    因此本类断言的不再是 insights.js 源码，而是 cards.js「我的画像」渲染区 +
    chat.js action 通道 + index.html 入口三者与后端响应的自洽。
    """

    #: 点名核对的字段 → 各自归属端点（cards.js renderInsights 的三个消费块）
    REQUIRED = {
        "patterns": ("risk_patterns", "common_pairs", "by_label"),
        "sources": ("qualified",),
        "weekly": ("top_refute", "period"),
    }

    #: 左栏「我的画像」三个按钮的 kind（后端 tools_insights.KINDS 的封闭枚举）
    KINDS = ("patterns", "sources", "weekly")

    #: cards.js 里「我的画像」渲染区的物理边界（"4. 我的画像卡" → 下一节"新事实核查答案"）
    RENDER_SECTION_START = "4. 我的画像卡"
    RENDER_SECTION_END = "新事实核查答案"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.cards_js = _read_static("cards.js")
        cls.index_html = _read_static("index.html")
        cls.chat_css = _read_static("chat.css")
        cls.chat_js = _read_static("chat.js")

    def _insights_renderer_source(self) -> str:
        """切出 cards.js 里「我的画像」渲染区，避免把 claim_answer 等无关字段算进来。"""
        parts = self.cards_js.split(self.RENDER_SECTION_START, 1)
        self.assertEqual(len(parts), 2,
                         "cards.js 缺少「4. 我的画像卡」渲染区标记（渲染逻辑被挪走了？）")
        body = parts[1].split(self.RENDER_SECTION_END, 1)[0]
        self.assertIn("function renderInsights", body,
                      "cards.js 画像渲染区里没有 renderInsights")
        return body

    def test_consumed_fields_exist_in_api_responses(self):
        """cards.js 画像渲染区消费的每个响应字段都真实出现在 API 响应里（含六个关键字段点名）。"""
        self._seed_pattern_library()
        self._seed_source_profiles()
        self._seed_window()
        pat = self.get("/api/insights/patterns").json()
        prof = self.get("/api/insights/sources").json()
        dig = self.get("/api/insights/weekly").json()
        union = _deep_keys(pat) | _deep_keys(prof) | _deep_keys(dig)
        src = self._insights_renderer_source()
        # 渲染层只以 .snake_case 形式取响应字段（方法/属性全是驼峰，不会误伤）
        consumed = set(re.findall(r"\.([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", src))
        self.assertTrue(consumed, "提取不到 cards.js 画像渲染区消费的字段：正则或文件失效")
        missing = sorted(consumed - union)
        self.assertEqual(missing, [], f"cards.js 画像渲染区消费了 API 不提供的字段：{missing}")
        # 六个关键字段逐点名：源码里真的点号访问了 + 对应端点响应里真的有
        self.assertRegex(src, r"\.risk_patterns\b")
        self.assertRegex(src, r"\.common_pairs\b")
        self.assertRegex(src, r"\.by_label\b")
        self.assertRegex(src, r"\.qualified\b")
        self.assertRegex(src, r"\.top_refute\b")
        self.assertRegex(src, r"\.period\b")
        for field in self.REQUIRED["patterns"]:
            self.assertIn(field, _deep_keys(pat["patterns"]), field)
        for field in self.REQUIRED["sources"]:
            self.assertIn(field, _deep_keys(prof["profiles"]), field)
        for field in self.REQUIRED["weekly"]:
            self.assertIn(field, _deep_keys(dig["digest"]), field)

    def test_api_paths_match_backend_routes(self):
        """三个 /api/insights/* 端点仍在 FastAPI 路由表里；前端只走 /api/chat 的 action 通道。

        单页化契约：卡片由 /api/chat 的 insights action 产出（SSE card 事件），
        前端不直连 /api/insights/*——直连会绕开会话史、事件流与取消链。
        """
        routes = {getattr(r, "path", "") for r in self.webapp.app.routes}
        for path in ("/api/insights/patterns", "/api/insights/sources",
                     "/api/insights/weekly"):
            self.assertIn(path, routes, path)
        self.assertIn('chat: "/api/chat"', self.chat_js)
        self.assertIn("body.action = action", self.chat_js)
        self.assertNotIn("/api/insights", self.chat_js)
        self.assertNotIn("/api/insights", self.cards_js)

    def test_insights_entry_and_renderer_wired(self):
        """index.html 入口 + chat.js action 发送 + cards.js 渲染分支，三段接线齐全。"""
        # 1) 左栏「我的画像」面板：id 在位，三个按钮的 kind 正好是后端封闭枚举
        self.assertIn('id="chatInsights"', self.index_html)
        panel = self.index_html.split('id="chatInsights"', 1)[1].split("</section>", 1)[0]
        kinds = re.findall(r'data-insights-kind="([A-Za-z]+)"', panel)
        self.assertEqual(kinds, list(self.KINDS),
                         "左栏画像按钮的 kind 与后端 tools_insights.KINDS 不一致")
        self.assertIn("我的画像", panel)
        # 2) chat.js：左栏按钮点击 → insights action（message 由 describeAction 生成）
        self.assertIn('document.querySelectorAll("#chatInsights [data-insights-kind]")',
                      self.chat_js)
        self.assertIn("sendInsights(btn.dataset.insightsKind)", self.chat_js)
        self.assertIn('{ name: "insights", args: { kind } }', self.chat_js)
        self.assertIn("function sendInsights(kind)", self.chat_js)
        # 3) cards.js：渲染器 + card_type 分发分支 + 导出（未导出 chat.js 调不到）
        self.assertIn("function renderInsights(card)", self.cards_js)
        self.assertIn('if (type === "insights") return renderInsights(payload);', self.cards_js)
        self.assertIn("renderInsights,", self.cards_js)   # window.ChatCards 导出名单
        # 4) 渲染器用到的 chat-insight-* class 在 chat.css 有定义（否则画像卡裸奔）
        used = set()
        for chunk in re.findall(r'class="([^"]+)"', panel + self._insights_renderer_source()):
            used.update(re.findall(r"[A-Za-z][\w-]*", chunk))
        insight_classes = sorted(c for c in used if c.startswith("chat-insight-"))
        self.assertTrue(insight_classes, "画像区没有解析到 chat-insight-* class")
        css_missing = [c for c in insight_classes if f".{c}" not in self.chat_css]
        self.assertEqual(css_missing, [], f"chat.css 缺少定义：{css_missing}")
        self.assertIn(".chat-card-insights", self.chat_css)

    def test_old_frontend_files_removed(self):
        """单页化后 static/app.js 与 static/insights.js 必须已删除且页面不再引用。"""
        for name in ("app.js", "insights.js"):
            self.assertFalse((STATIC_DIR / name).exists(), f"旧前端文件仍在：static/{name}")
            self.assertNotIn(f"static/{name}", self.index_html)
        for name in ("shell.js", "cards.js", "chat.js"):
            self.assertTrue((STATIC_DIR / name).is_file(), f"新前端文件缺失：static/{name}")
            self.assertIn(f'src="static/{name}"', self.index_html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
