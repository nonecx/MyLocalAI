# -*- coding: utf-8 -*-
"""平台 API 与前端自洽测试（P6 + 方案 G 改版）：
   端点契约（状态/信息流/预警/监控词 CRUD/巡检触发/巡检记录）+
   前后端自洽（platform.js 的每个接口路径、元素 id、platform-* class 都有落点）。

方案 G（热榜/新闻信息流）改版后，前端自洽断言在「动态提取」之外另加**点名契约**：
index.html 平台视图的 23 个核心元素 id、通栏 Tab（main-tabs / mainTabs）、
筛选 chips（platformFilterRow 的 data-risk）、管理抽屉（platformDrawer 家族），
并断言旧版「面板栈 + 预警横幅」遗留标记一处不留。改 UI 骨架必须同步改这里。

数据用 PlatformStore 临时库，经 platform_api.set_store/set_scheduler 注入
（与 app.get_dossier_service 同一工厂注入模式）；引擎不参与。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_platform_api -v
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

STATIC = ROOT / "src" / "mylocalai" / "web" / "static"

#: 方案 G 新契约：index.html 监测平台视图点名的核心元素 id（共 23 个）。
#: 结构 = 通栏 Tab（对话｜监测平台）+ 状态摘要条 + 筛选 chips + 热榜信息流
#: + 空态双引导 + 右侧管理抽屉（监控词 / 巡检记录 / 授权引导）。
#: 与 README「监测平台」章节、docs/监测平台规格-2026-11-20.md §7 同口径；
#: 改 UI 骨架必须三处一起改（此清单、README、规格 §7）。
PLATFORM_CORE_IDS = (
    # 通栏视图切换 Tab
    "navChat", "navPlatform", "mainTabs",
    # 平台视图容器 + 状态摘要条
    "platformView", "platformStatusBar", "platformStatusDot", "platformSummary",
    # 摘要条动作 + 巡检提示行
    "platformScanBtn", "platformManageBtn", "platformScanHint",
    # 筛选 chips 行 + 信息流
    "platformFilterRow", "platformFeed",
    # 空态双引导
    "platformEmpty", "platformEmptyBtn",
    # 管理抽屉（遮罩 / 面板 / 关闭）
    "platformDrawer", "platformDrawerMask", "platformDrawerClose",
    # 抽屉：监控词管理
    "platformWatchInput", "platformWatchAddBtn", "platformWatchList",
    # 抽屉：巡检记录时间线
    "platformScans",
    # 抽屉：未授权引导卡（fail-closed）
    "platformDrawerAuth", "platformAuthSettingsBtn",
)

#: 方案 G 之前的旧版遗留标记（面板栈 + 预警横幅）：新契约里一处都不该留。
PLATFORM_LEGACY_MARKERS = ("site-nav", "platformWarningBanner", "platform-warning")

#: 信息流筛选 chips 的 data-risk 取值（"" = 全部；high,urgent = 预警档）。
PLATFORM_FILTER_RISKS = ("", "high,urgent", "suspect", "notice")


def _read_static(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


class StubScheduler:
    """status/trigger 全桩（不真跑巡检）。"""

    def __init__(self, status=None, started=True):
        self._status = status or {
            "enabled": False, "interval_s": 900, "running": False,
            "scan_in_progress": False, "last_scan": {}, "last_error": "",
            "rule_warnings": [],
        }
        self._started = started
        self.triggers = 0

    def status(self):
        return dict(self._status)

    def trigger_async(self, queries=None):
        self.triggers += 1
        return self._started


class PlatformApiTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
            from mylocalai.web import platform_api
        except Exception as exc:  # noqa: BLE001  fastapi 缺失时整组跳过
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.platform_api = platform_api
        cls.client = TestClient(webapp.app)

    def setUp(self) -> None:
        from mylocalai.platform.store import PlatformStore
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PlatformStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)
        self.scheduler = StubScheduler()
        # 直接换模块级单例（set_store/set_scheduler；退出时置空恢复惰性创建）
        self.platform_api.set_store(self.store)
        self.platform_api.set_scheduler(self.scheduler)
        self.addCleanup(self.platform_api.set_store, None)
        self.addCleanup(self.platform_api.set_scheduler, None)


class TestPlatformEndpoints(PlatformApiTestBase):
    def test_status_shape(self):
        resp = self.client.get("/api/platform/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        for key in ("enabled", "interval_s", "running", "scan_in_progress",
                    "last_scan", "external_authorized"):
            self.assertIn(key, data)

    def test_feed_empty_and_filtered(self):
        resp = self.client.get("/api/platform/feed?limit=10")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["entries"], [])
        qid = self.store.add_watch_query("话题")
        eid = self.store.add_feed_entry(query_id=qid, title="t",
                                        url="https://a.example.com/1",
                                        e_score=44.0, status="verified")
        self.store.update_feed_entry(eid, risk_level="high")
        feed = self.client.get("/api/platform/feed").json()["entries"]
        self.assertEqual(len(feed), 1)
        warnings = self.client.get("/api/platform/warnings").json()["entries"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["risk_level"], "high")
        self.assertEqual(self.client.get(
            "/api/platform/feed?risk=notice").json()["entries"], [])

    def test_watch_crud(self):
        created = self.client.post("/api/platform/watch",
                                   json={"query": "自来水 投毒"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["query"], "自来水 投毒")
        dup = self.client.post("/api/platform/watch", json={"query": "自来水 投毒"})
        self.assertEqual(dup.status_code, 409)
        empty = self.client.post("/api/platform/watch", json={"query": "  "})
        self.assertEqual(empty.status_code, 400)
        too_long = self.client.post("/api/platform/watch",
                                    json={"query": "x" * 201})
        self.assertEqual(too_long.status_code, 400)
        listed = self.client.get("/api/platform/watch").json()["queries"]
        self.assertEqual(len(listed), 1)
        qid = listed[0]["id"]
        deleted = self.client.delete(f"/api/platform/watch/{qid}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get("/api/platform/watch").json()["queries"], [])
        missing = self.client.delete("/api/platform/watch/999")
        self.assertEqual(missing.status_code, 404)
        bad_id = self.client.delete("/api/platform/watch/0")
        self.assertEqual(bad_id.status_code, 400)

    def test_scan_trigger(self):
        resp = self.client.post("/api/platform/scan")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["started"])
        self.assertEqual(self.scheduler.triggers, 1)

    def test_scan_trigger_busy(self):
        self.scheduler._started = False
        resp = self.client.post("/api/platform/scan")
        self.assertFalse(resp.json()["started"])     # 单飞：不叠加，如实告知

    def test_scans_list_and_detail(self):
        sid = self.store.start_scan_run()
        self.assertTrue(self.store.finish_scan_run(sid, "completed", queries=1,
                                                   candidates=2, verified=1))
        listed = self.client.get("/api/platform/scans").json()["scans"]
        self.assertEqual(len(listed), 1)
        detail = self.client.get(f"/api/platform/scans/{sid}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["scan"]["status"], "completed")
        missing = self.client.get("/api/platform/scans/999")
        self.assertEqual(missing.status_code, 404)

    def test_start_scheduler_disabled_no_side_effect(self):
        """enabled=false 时启动钩子不构造调度器（import 期零副作用）。"""
        with mock.patch("mylocalai.platform.rules.load_rules",
                        return_value=({"enabled": False}, [])):
            self.assertFalse(self.platform_api.start_scheduler_if_enabled())


class TestPlatformFrontendConsistency(unittest.TestCase):
    """前后端自洽（沿 test_website_chat 模式）：platform.js 的每个接口路径/
    元素 id/动态 class 都有落点；index.html 引用齐全。

    方案 G（热榜/新闻信息流）之后另锁结构契约：通栏 Tab、筛选 chips、
    管理抽屉、23 个点名核心 id，以及旧版「面板栈 + 预警横幅」零残留。
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.js = _read_static("platform.js")
        cls.css = _read_static("platform.css")
        cls.html = _read_static("index.html")
        cls.cards_js = _read_static("cards.js")

    def test_api_paths_exist_in_routes(self):
        """platform.js 的每个 /api/* 路径都在后端路由表里。

        路由收集走 app.openapi()：FastAPI 0.163 的 include_router 只挂
        _IncludedRouter 对象（无 path/routes 属性），app.routes 内查看不到
        included router 的端点；openapi 的 paths 是全量权威来源。
        """
        paths = set(re.findall(r'"(/api/[a-z/{}/]+)"', self.js))
        self.assertTrue(paths, "没从 platform.js 里解析到 API 路径")
        spec = self.webapp.app.openapi()
        routes = set(spec.get("paths", {}))
        missing = [p for p in sorted(paths) if p not in routes]
        self.assertEqual(missing, [], f"platform.js 引用了后端不存在的接口：{missing}")

    def test_element_ids_exist_in_html(self):
        """platform.js 动态取用的元素 id 都在 index.html 有落点；同时点名锁定
        方案 G 新契约的 23 个核心 id（Tab + 摘要条 + 筛选 + 信息流 + 空态 + 抽屉）。

        动态提取式保留（新增 id 不必回来改测试），点名清单是**契约下限**：
        少一个核心 id 或某个核心 id 根本没被 JS 接线，都算契约被破坏。
        """
        used = set(re.findall(r'\$\("#([A-Za-z0-9_]+)"\)', self.js))
        used |= set(re.findall(r'getElementById\("([A-Za-z0-9_]+)"\)', self.js))
        self.assertTrue(used, "platform.js 没引用任何元素 id")
        html_ids = set(re.findall(r'id="([A-Za-z0-9_]+)"', self.html))
        missing = sorted(i for i in used if i not in html_ids)
        self.assertEqual(missing, [], f"platform.js 引用了 HTML 里不存在的元素 id: {missing}")
        # 点名契约：核心 id 一个不少，且都被 platform.js 真正接线（不许摆死 id）
        absent = sorted(i for i in PLATFORM_CORE_IDS if i not in html_ids)
        self.assertEqual(absent, [], f"方案 G 核心 id 在 index.html 缺失: {absent}")
        unwired = sorted(i for i in PLATFORM_CORE_IDS if i not in used)
        self.assertEqual(unwired, [], f"方案 G 核心 id 没被 platform.js 接线: {unwired}")
        self.assertEqual(len(PLATFORM_CORE_IDS), 23,
                         "核心 id 契约数量变了：请同步 PLATFORM_CORE_IDS 注释里的"
                         "README「监测平台」章节与规格 §7 描述")

    def test_index_html_references_platform_assets(self):
        for ref in ("static/platform.js", "static/platform.css"):
            self.assertIn(ref, self.html)
        for name in ("platform.js", "platform.css"):
            self.assertTrue((STATIC / name).is_file(), f"缺少静态文件 {name}")

    def test_dynamic_platform_classes_have_css(self):
        """动态拼出的平台 class 必须在 platform.css 有定义。

        两个来源同式断言：platform.js 的 `platform-*`（平台页信息流/抽屉）与
        cards.js 的 `chat-platform-*`（对话内轻量化平台卡）——卡片与平台页共用
        同一份平台样式文件，别让类名漂移成无样式裸奔。
        """
        used = set(re.findall(r'platform-[a-z-]+', self.js))
        used |= set(re.findall(r'chat-platform-[a-z-]+', self.cards_js))
        defined = set(re.findall(r'\.((?:chat-)?platform-[a-z-]+)', self.css))
        missing = sorted(c for c in used if c not in defined)
        self.assertEqual(missing, [], f"JS 用了 platform.css 未定义的类：{missing}")
        # 两边都该真的拼了类，否则这条断言是空转
        self.assertIn("chat-platform-entry", used)
        self.assertIn("platform-feed-item", used)

    def test_risk_levels_match_backend(self):
        """前端档位集合 = 后端 risk.LEVELS（换档位必须两端同步）。

        按 platform.js 里 RISK_LEVELS 的实际写法取键与中文名：
        `notice: { label: "关注", cls: "gray" },`。展示名是 risk.LEVEL_LABELS
        的口径，也不许前端悄悄改字。
        """
        from mylocalai.platform.risk import LEVELS, LEVEL_LABELS
        block = re.search(r"const RISK_LEVELS\s*=\s*\{(.*?)\};", self.js, re.S)
        self.assertIsNotNone(block, "platform.js 里找不到 RISK_LEVELS 定义")
        pairs = re.findall(r"(\w+)\s*:\s*\{\s*label:\s*\"([^\"]+)\"", block.group(1))
        js_levels = {key for key, _ in pairs}
        js_labels = dict(pairs)
        self.assertEqual(js_levels, set(LEVELS),
                         f"platform.js RISK_LEVELS 键集合与后端不一致：{sorted(js_levels)}")
        self.assertEqual(js_labels, dict(LEVEL_LABELS),
                         f"platform.js RISK_LEVELS 展示名与 risk.LEVEL_LABELS 不一致：{js_labels}")

    def test_platform_view_structure(self):
        """方案 G 结构契约 + 通栏 Tab 切换接线。

        结构：通栏 Tab（main-tabs / mainTabs）→ 状态摘要条 → 筛选 chips
        （platformFilterRow 的 data-risk）→ 热榜信息流 → 空态双引导 → 右侧
        管理抽屉（监控词 / 巡检记录 / 授权引导）；样式在 platform.css。
        旧版「面板栈 + 预警横幅」遗留标记一处不留。
        切换：导航按钮 + 两个视图容器都在 HTML 里，showView 在 JS 里；两个
        视图是显隐切换（都留在 DOM），不是销毁重建。
        """
        # ① 通栏主 Tab（header 下方，对话｜监测平台；视觉类在 platform.css）
        self.assertIn('class="main-tabs"', self.html)
        self.assertIn('id="mainTabs"', self.html)
        css_classes = set(re.findall(r'\.([a-z][a-z0-9-]*)', self.css))
        for cls in ("main-tabs", "main-tab"):
            self.assertIn(cls, css_classes, f"platform.css 缺 .{cls} 定义")
        # ② 平台视图主容器 + 状态摘要条 + 信息流 + 空态
        for element_id in ("platformView", "platformStatusBar", "platformFeed",
                           "platformEmpty", "platformEmptyBtn"):
            self.assertIn(f'id="{element_id}"', self.html)
        # ③ 筛选 chips 行：data-risk 取值 = 全部 / 预警(high,urgent) / 疑似 / 关注
        risks = re.findall(r'data-risk="([^"]*)"', self.html)
        self.assertEqual(sorted(risks), sorted(PLATFORM_FILTER_RISKS),
                         f"platformFilterRow 的 data-risk 取值不符合契约：{risks}")
        self.assertIn('id="platformFilterRow"', self.html)
        # ④ 管理抽屉：面板 + 遮罩 + 关闭 + 监控词 + 巡检记录 + 授权引导
        for element_id in ("platformDrawer", "platformDrawerMask", "platformDrawerClose",
                           "platformWatchInput", "platformWatchAddBtn",
                           "platformWatchList", "platformScans", "platformDrawerAuth"):
            self.assertIn(f'id="{element_id}"', self.html)
        # ⑤ 旧版遗留标记（面板栈 / 预警横幅）零残留
        leftovers = [m for m in PLATFORM_LEGACY_MARKERS if m in self.html]
        self.assertEqual(leftovers, [], f"index.html 还留着旧版遗留标记: {leftovers}")
        # ⑥ 通栏 Tab 切换接线
        for element_id in ("navChat", "navPlatform", "mainTabs", "platformView"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("function showView", self.js)
        self.assertIn('class="chat-layout"', self.html)
        self.assertEqual(len(re.findall(r'<main\b', self.html)), 2,
                         "单页应只剩两个 <main> 视图容器（chat-layout + platform-layout）")
        # 骨架不完整（导航缺失）时安静退出，不抛错
        self.assertIn('if (!tabs) return;', self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
