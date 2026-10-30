# -*- coding: utf-8 -*-
"""my_website 关注度（Attention）功能离线测试：不联网、不调用大模型。

覆盖三件事：
  1. 后端 /api/attention 与 /api/attention/search 的行为（含非法输入、检索失败降级）
  2. mylocalai.attention.estimate 的参数估计口径（x/y/z 的来源、estimated 标记、解析边界）
  3. 前端三件套与后端接口的自洽性：HTML 里的元素 id 必须被 app.js 引用、
     接口路径必须与后端路由一致——这类"改了后端忘了改前端"的错只有这种测试能抓到。

运行（项目根目录下）：
    ./.venv/Scripts/python.exe -m unittest tests.test_website_attention -v
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到包化后的 web/app.py

from mylocalai.attention import attention as A# noqa: E402
from mylocalai.attention.estimate import estimate_from_search# noqa: E402

STATIC = ROOT / "src" / "mylocalai" / "web" / "static"
APP_JS = STATIC / "app.js"
INDEX_HTML = STATIC / "index.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1. 后端接口
class TestAttentionApi(unittest.TestCase):
    """/api/attention 与 /api/attention/search 的行为。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            from app import app
        except Exception as exc:  # noqa: BLE001  fastapi 缺失时整组跳过而非报错
            raise unittest.SkipTest(f"无法导入 my_website.app: {type(exc).__name__}: {exc}")
        cls.client = TestClient(app)

    def test_manual_score_matches_module(self):
        """接口返回的分数必须与 attn_agent 模块逐位一致（不能前端算一套后端算一套）。"""
        resp = self.client.post("/api/attention", json={"x": 30, "y": 48316000, "z": 149})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertAlmostEqual(data["score"], A.attention_score(30, 48316000, 149), places=12)
        self.assertEqual(data["band"], "中关注")
        self.assertFalse(data["deep_read"]["triggered"])
        self.assertEqual(data["threshold"], 66.0)
        self.assertAlmostEqual(data["threshold_s"], 1.541157, places=6)

    def test_manual_score_returns_all_terms(self):
        """分项必须齐全，否则前端画不出拆解图。"""
        data = self.client.post("/api/attention",
                                json={"x": 100, "y": 100000, "z": 1000}).json()
        for key in ("x", "y", "z", "term_x", "term_y", "term_z", "S",
                    "exponent", "score", "band", "deep_read", "params"):
            self.assertIn(key, data)
        self.assertAlmostEqual(data["S"], 1.0 + 1e-6, places=12)

    def test_zero_input_is_valid(self):
        """全零输入合法：E 极小但不为 0（epsilon 的意义）。"""
        data = self.client.post("/api/attention", json={}).json()
        self.assertGreater(data["score"], 0.0)
        self.assertLess(data["score"], 1e-3)
        self.assertEqual(data["band"], "冷启动")

    def test_high_attention_triggers_deep_read(self):
        """亿级阅读量级应触发深读。"""
        data = self.client.post("/api/attention",
                                json={"x": 1000, "y": 1e8, "z": 1e6}).json()
        self.assertGreater(data["score"], 66.0)
        self.assertTrue(data["deep_read"])
        self.assertEqual(data["band"], "高关注")

    def test_negative_input_is_400(self):
        """负数回 400，不是 500——这是用户填错，不是服务故障。"""
        resp = self.client.post("/api/attention", json={"x": -1, "y": 1, "z": 1})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不合法", resp.json()["detail"])

    def test_nan_input_is_400(self):
        """NaN/Inf 必须被拦下（float 能表示但不是有限实数）。"""
        resp = self.client.post("/api/attention",
                                content='{"x": 1, "y": NaN, "z": 1}',
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)

    def test_search_empty_query_is_400(self):
        resp = self.client.post("/api/attention/search", json={"query": "  "})
        self.assertEqual(resp.status_code, 400)

    def test_search_too_long_query_is_400(self):
        resp = self.client.post("/api/attention/search", json={"query": "x" * 300})
        self.assertEqual(resp.status_code, 400)

    def test_search_uses_real_search_and_marks_estimated(self):
        """话题模式必须走真实检索，并把 estimated/quality 显式带回。"""
        fake = {
            "query": "测试话题",
            "results": [
                {"engine": "fake", "rank": 1, "title": "测试话题引发关注",
                 "url": "https://a.example.com/1", "snippet": "话题阅读量4831.6万，评论 287",
                 "date": None, "domain_tier": "权威媒体", "full_text": None},
                {"engine": "fake", "rank": 2, "title": "又有媒体报道",
                 "url": "https://b.example.com/2", "snippet": "热度持续",
                 "date": None, "domain_tier": "权威媒体", "full_text": None},
            ],
            "engines": {"fake": {"ok": True, "count": 2, "error": None}},
        }
        with mock.patch("mylocalai.retrieval.search.search_all", return_value=fake):
            resp = self.client.post("/api/attention/search", json={"query": "测试话题"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["x"], 2)
        self.assertAlmostEqual(data["y"], 48316000.0, places=6)
        self.assertAlmostEqual(data["z"], 287.0, places=6)
        self.assertTrue(data["estimated"])
        self.assertIn(data["quality"], ("exact", "estimated"))
        self.assertIn("engines", data)
        self.assertGreaterEqual(len(data["sources"]), 2)

    def test_search_dedups_urls(self):
        """同一 URL 重复出现只算一篇文章。"""
        fake = {"query": "q", "results": [
            {"url": "https://a.example.com/1", "title": "t", "snippet": "s"},
            {"url": "https://a.example.com/1/", "title": "t", "snippet": "s"},   # 尾斜杠
            {"url": "https://A.EXAMPLE.COM/1", "title": "t", "snippet": "s"},   # 大小写
        ], "engines": {}}
        with mock.patch("mylocalai.retrieval.search.search_all", return_value=fake):
            data = self.client.post("/api/attention/search", json={"query": "q"}).json()
        self.assertEqual(data["x"], 1)

    def test_search_empty_results_is_missing_not_error(self):
        """检索没结果时按缺失处理，不报错、不编数字。"""
        with mock.patch("mylocalai.retrieval.search.search_all", return_value={"results": [], "engines": {}}):
            resp = self.client.post("/api/attention/search", json={"query": "不存在的话题"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["quality"], "missing")
        self.assertEqual((data["x"], data["y"], data["z"]), (0, 0.0, 0.0))
        self.assertTrue(data["notes"])

    def test_search_failure_degrades_gracefully(self):
        """检索整体抛异常也必须 200 + 缺失标记，不能让页面 500。"""
        with mock.patch("mylocalai.retrieval.search.search_all", side_effect=RuntimeError("网络不可达")):
            resp = self.client.post("/api/attention/search", json={"query": "任何话题"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["quality"], "missing")
        self.assertTrue(any("检索失败" in n for n in data["notes"]))

    def test_search_surrogate_query_is_400(self):
        """孤立代理项（无效 Unicode）回 400，与 /api/analyze 口径一致。"""
        resp = self.client.post("/api/attention/search",
                                content=json.dumps({"query": "ok\ud800bad"}),
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)

    def test_health_and_static_still_ok(self):
        """新端点不能把既有的首页/健康检查/静态资源搞坏。"""
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/static/style.css").status_code, 200)

    def test_attention_profiles_endpoint(self):
        """GET /api/attention/profiles 必须返回可用档位，默认档为 spec。"""
        resp = self.client.get("/api/attention/profiles")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["default"], "spec")
        names = [p["name"] for p in data["profiles"]]
        self.assertIn("spec", names)
        self.assertIn("reachable", names)
        for p in data["profiles"]:
            self.assertIn("params", p)
            self.assertIn("comment", p)

    def test_manual_score_with_profile(self):
        """profile 参数必须真正改变分数，且返回生效档位名。"""
        base = self.client.post("/api/attention",
                                json={"x": 1000, "y": 1e8, "z": 1e6}).json()
        alt = self.client.post(
            "/api/attention",
            json={"x": 1000, "y": 1e8, "z": 1e6, "profile": "reachable"}).json()
        self.assertEqual(base["profile"], "spec")
        self.assertEqual(alt["profile"], "reachable")
        self.assertNotEqual(base["score"], alt["score"])

    def test_manual_score_with_bad_profile_falls_back(self):
        """非法 profile 应回退默认档并在 profile_warnings 中说明。"""
        resp = self.client.post(
            "/api/attention",
            json={"x": 100, "y": 100000, "z": 1000, "profile": "nope"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["profile"], "spec")
        self.assertTrue(data["profile_warnings"])

    def test_search_endpoint_accepts_profile(self):
        fake = {"query": "q", "results": [
            {"url": "https://a.example/1", "title": "t", "snippet": "s"}],
            "engines": {}}
        with mock.patch("mylocalai.retrieval.search.search_all", return_value=fake):
            resp = self.client.post("/api/attention/search",
                                    json={"query": "q", "profile": "reachable"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["profile"], "reachable")


# ---------------------------------------------------------------- 2. 参数估计
class TestEstimate(unittest.TestCase):
    """mylocalai.attention.estimate 的口径。"""

    def test_extracts_reads_and_comments(self):
        est = estimate_from_search("q", [
            {"url": "https://a.example/1", "title": "话题火了",
             "snippet": "该话题阅读量4831.6万，评论 287 条"},
        ])
        self.assertEqual(est.x, 1)
        self.assertAlmostEqual(est.y, 48316000.0, places=6)
        self.assertAlmostEqual(est.z, 287.0, places=6)
        # quality=exact 表示 y/z 都有页面公开值可查；estimated 仍恒为 True（见下）
        self.assertEqual(est.quality, "exact")
        self.assertTrue(est.estimated)

    def test_estimated_is_always_true(self):
        """即使 y/z 都抽到页面公开值，estimated 也必须为 True。

        摘要里的「阅读量」可能是平台热度指数，且只覆盖检索命中的那几篇，
        不是全网真实阅读量。恒为 True 才能保证前端一律标注「代理 / 估计值」，
        不让用户把 E 当成绝对结论。
        """
        est = estimate_from_search("q", [
            {"url": "https://a.example/1", "title": "t",
             "snippet": "阅读量4831.6万，评论 287 条"},
        ])
        self.assertTrue(est.estimated)
        self.assertEqual(est.quality, "exact")
        self.assertTrue(any("代理" in n for n in est.notes))

    def test_reads_without_comments_is_estimated(self):
        """只抽到阅读量、没抽到评论数 → estimated=True，z=0 且说明是缺失。"""
        est = estimate_from_search("q", [
            {"url": "https://a.example/1", "title": "t", "snippet": "阅读量1200万"},
        ])
        self.assertAlmostEqual(est.y, 1.2e7, places=6)
        self.assertEqual(est.z, 0.0)
        self.assertTrue(est.estimated)
        self.assertEqual(est.quality, "estimated")
        self.assertTrue(any("评论" in n for n in est.notes))

    def test_no_public_numbers_falls_back(self):
        """摘要里没有任何公开数值 → y 按每篇兜底，estimated=True。"""
        est = estimate_from_search("q", [
            {"url": "https://a.example/1", "title": "t", "snippet": "普通报道"},
            {"url": "https://a.example/2", "title": "t", "snippet": "普通报道"},
        ])
        self.assertEqual(est.x, 2)
        self.assertEqual(est.y, 2e5)          # per_article_reads=1e5 × 2 篇
        self.assertTrue(est.estimated)

    def test_empty_results_is_missing(self):
        est = estimate_from_search("q", [])
        self.assertEqual(est.quality, "missing")
        self.assertEqual((est.x, est.y, est.z), (0, 0.0, 0.0))

    def test_search_result_objects_supported(self):
        """既接受 dict 也接受 SearchResult 对象（真实链路传的是对象）。"""
        from mylocalai.retrieval.search import SearchResult
        est = estimate_from_search("q", [
            SearchResult(engine="e", rank=1, title="t",
                         url="https://a.example/1", snippet="阅读量 300 万"),
        ])
        self.assertEqual(est.x, 1)
        self.assertAlmostEqual(est.y, 3e6, places=6)

    def test_takes_max_not_first(self):
        """多篇时取最大量级，而不是第一篇（避免被小数字拉低）。"""
        est = estimate_from_search("q", [
            {"url": "https://a/1", "title": "t", "snippet": "阅读量 10 万"},
            {"url": "https://a/2", "title": "t", "snippet": "阅读量 900 万"},
        ])
        self.assertAlmostEqual(est.y, 9e6, places=6)

    def test_ignores_bare_numbers(self):
        """正文里的孤立大数不能被当成阅读量（避免把"3 亿元"读成阅读量）。"""
        est = estimate_from_search("q", [
            {"url": "https://a/1", "title": "某公司营收 3 亿元", "snippet": "正文没有任何阅读量字样"},
        ])
        self.assertEqual(est.y, 1e5)   # 走兜底，而不是 3e8

    def test_time_phrase_is_not_a_comment_count(self):
        """「22小时前」不能被读成 22 条评论（真实检索摘要里最常见的假阳性）。"""
        est = estimate_from_search("q", [
            {"url": "https://a/1", "title": "老干妈回应AI裁员传闻",
             "snippet": "22小时前 - 凤凰网报道"},
            {"url": "https://a/2", "title": "相关讨论", "snippet": "3天前 - 另一篇"},
        ])
        self.assertEqual(est.z, 0.0, "时间表述被误当成评论数")

    def test_number_before_hint_is_recognized(self):
        """中文里"数值在前"的写法也要认（「287 条跟贴」「890 万次浏览」）。"""
        est = estimate_from_search("q", [
            {"url": "https://a/1", "title": "t", "snippet": "已有 287 条跟贴"},
        ])
        self.assertAlmostEqual(est.z, 287.0, places=6)
        est2 = estimate_from_search("q", [
            {"url": "https://a/1", "title": "t", "snippet": "浏览次数 890 万"},
        ])
        self.assertAlmostEqual(est2.y, 8.9e6, places=6)

    def test_score_from_estimate_is_deterministic(self):
        """估计出的参数代入公式必须与直接调 attention_score 一致。"""
        est = estimate_from_search("q", [
            {"url": "https://a/1", "title": "t", "snippet": "阅读量 1000 万，评论 500"},
        ])
        self.assertAlmostEqual(
            A.attention_score(est.x, est.y, est.z),
            A.attention_score(1, 1e7, 500), places=12)

    def test_to_dict_roundtrip(self):
        """to_dict 的键必须覆盖前端渲染需要的全部字段。"""
        est = estimate_from_search("q", [
            {"url": "https://a/1", "title": "t", "snippet": "阅读量 100 万"}])
        d = est.to_dict()
        for key in ("x", "y", "z", "quality", "estimated", "query",
                    "article_count", "sources", "notes"):
            self.assertIn(key, d)
        json.dumps(d, ensure_ascii=False)   # 必须可 JSON 序列化（要过 HTTP）


# ---------------------------------------------------------------- 3. 前后端自洽
class TestFrontendBackendConsistency(unittest.TestCase):
    """抓"改了后端忘了改前端"这类错：id 引用、接口路径、元素是否存在。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = _read(APP_JS)
        cls.html = _read(INDEX_HTML)

    def test_all_api_paths_exist_in_backend(self):
        """前端 API 对象里的每个路径，后端都必须真有这个路由。"""
        from app import app
        routes = {getattr(r, "path", "") for r in app.routes}
        paths = re.findall(r'^\s{2}(\w+):\s*"([^"]+)"', self.js, re.M)
        self.assertTrue(paths, "没从 app.js 里解析到 API 路径")
        for name, path in paths:
            self.assertIn(path, routes, f"前端引用了后端不存在的接口：{name} -> {path}")

    def test_attention_ids_used_by_js_exist_in_html(self):
        """app.js 里 $("#attnXxx") 引用的每个 id，index.html 里都必须有。"""
        used = set(re.findall(r'\$\("#(attn[A-Za-z]+)"\)', self.js))
        used |= set(re.findall(r'getElementById\("(attn[A-Za-z]+)"\)', self.js))
        self.assertTrue(used, "app.js 里没引用任何 attn* 元素")
        missing = sorted(i for i in used if f'id="{i}"' not in self.html)
        self.assertEqual(missing, [], f"app.js 引用了 HTML 里不存在的元素 id: {missing}")

    def test_inline_attention_ids_exist(self):
        """分析记录里挂关注度的容器必须可达。

        classicAttention 是静态 HTML 里的；agentAttention 是 renderAgentResult
        动态拼进 #agentResult 的模板字符串里的——两者都必须存在，但来源不同，
        所以分开校验（否则会漏掉"HTML 里找不到"这种假告警）。
        """
        self.assertIn('id="classicAttention"', self.html, "HTML 缺少 classicAttention")
        self.assertIn('id="agentAttention"', self.js,
                      "app.js 的 agent 模板里缺少 agentAttention 容器")
        # 两处容器都要被 renderXxxResult 真的填内容
        self.assertIn("classicAttention", self.js)
        self.assertIn("agentAttention", self.js)
        self.assertIn("attentionInline", self.js)

    def test_frontend_has_profile_selector(self):
        """前端必须暴露档位选择控件并被 JS 引用。"""
        self.assertIn('id="attnProfile"', self.html)
        self.assertIn("attnProfileDesc", self.html)
        self.assertIn("loadAttnProfiles", self.js)
        self.assertIn("attentionProfiles", self.js)
        css = _read(STATIC / "style.css")
        self.assertIn(".attn-profile-row", css)
        self.assertIn(".attn-profile-desc", css)

    def test_deep_read_rendering_wired(self):
        """深读渲染链路必须接通：JS 有渲染函数、CSS 有对应样式。"""
        self.assertIn("renderDeepRead", self.js)
        self.assertIn("d.deep_read", self.js)
        css = _read(STATIC / "style.css")
        for cls in ("attn-deep-body", "attn-deep-text", "attn-deep-head"):
            self.assertIn(f".{cls}", css, f"style.css 缺少 .{cls} 定义")

    def test_band_labels_match_backend(self):
        """前端档位配色表必须覆盖后端 DEFAULT_BANDS 的全部 label。"""
        labels = {b["label"] for b in A.DEFAULT_BANDS}
        for label in labels:
            self.assertIn(f'"{label}"', self.js,
                          f"前端 ATTN_BAND_CLS 缺少后端档位：{label}")

    def test_threshold_constant_matches_backend(self):
        """后端深读阈值常量必须仍是 66（spec 档默认口径，改造不许悄悄改掉）。"""
        self.assertIn(f'DEEP_READ_THRESHOLD: float = {A.DEEP_READ_THRESHOLD}',
                      _read(ROOT / "src" / "mylocalai" / "attention" / "attention.py"))
        self.assertEqual(A.DEEP_READ_THRESHOLD, 66.0)

    def test_frontend_does_not_hardcode_threshold(self):
        """前端不得写死深读阈值。

        触发口径已外置到 deep_read_rules.json（可为绝对阈值，也可为同批分位），
        前端写死 66 会在 quantile 模式下谎称一个并不存在的绝对阈值。
        """
        self.assertNotIn("触发深读（E", self.js)
        self.assertNotIn("E &gt; 66，已触发深读", self.js)
        self.assertIn("触发深读", self.js)   # 徽章本身要在

    def test_frontend_renders_new_deep_read_badges(self):
        """前端必须渲染新 schema 的三个徽章：未触发解读 / 降级模板 / 代理估计值。"""
        for marker in ("未触发深读解读", "降级：本地模板", "代理/估计值"):
            self.assertIn(marker, self.js, f"app.js 缺少徽章文案：{marker}")

    def test_frontend_renders_structured_result(self):
        """前端必须能渲染深读的结构化产出（议题标签/张力/现象/反方/不确定性）。"""
        self.assertIn("renderDeepReadResult", self.js)
        self.assertIn("deepReadBadges", self.js)
        for field in ("issue_tags", "tensions", "phenomena", "counter_reading", "uncertainty"):
            self.assertIn(field, self.js, f"app.js 未渲染深读字段：{field}")
        css = _read(STATIC / "style.css")
        self.assertIn(".attn-deep-result", css, "style.css 缺少 .attn-deep-result 定义")

    def test_attention_panel_present_in_html(self):
        """关注度面板本体必须在页面里（否则功能等于没上线）。"""
        for marker in ('id="attentionPanel"', 'id="attnTabs"',
                       'id="attnManual"', 'id="attnSearch"', 'id="attnResult"'):
            self.assertIn(marker, self.html, f"页面缺少关注度面板组成：{marker}")

    def test_css_classes_used_by_js_are_defined(self):
        """app.js 用到的主要 attn-* class 必须在 style.css 里有定义（否则样式丢）。"""
        css = _read(STATIC / "style.css")
        for cls in ("attn-intro", "attn-tab", "attn-fields", "attn-result",
                    "attn-score", "attn-band", "attn-grid", "attn-col",
                    "attn-deep", "attn-notes", "attn-inline"):
            self.assertIn(f".{cls}", css, f"style.css 缺少 .{cls} 定义")

    def test_rules_json_bands_match_module(self):
        """外置 attn_rules.json 的档位表必须与模块 DEFAULT_BANDS 一致。"""
        import json as _json
        data = _json.loads(_read(ROOT / "src" / "mylocalai" / "attention" / "attn_rules.json"))
        self.assertEqual(data["bands"], A.DEFAULT_BANDS)
        self.assertEqual(data["deep_read_threshold"], A.DEEP_READ_THRESHOLD)

    def test_no_hardcoded_drive_paths_in_frontend(self):
        """前端不允许出现写死的 D:\\ 路径（换机器就废）。"""
        self.assertNotRegex(self.html, r"[A-Za-z]:\\\\")
        self.assertNotRegex(self.js, r"[A-Za-z]:\\\\")


if __name__ == "__main__":
    unittest.main(verbosity=2)
