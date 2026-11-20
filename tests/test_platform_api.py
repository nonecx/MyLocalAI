# -*- coding: utf-8 -*-
"""平台 API 与前端自洽测试（P6）：
   端点契约（状态/信息流/预警/监控词 CRUD/巡检触发/巡检记录）+
   前后端自洽（platform.js 的每个接口路径、元素 id、platform-* class 都有落点）。

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
    元素 id/动态 class 都有落点；index.html 引用齐全。"""

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
        used = set(re.findall(r'\$\("#([A-Za-z0-9_]+)"\)', self.js))
        used |= set(re.findall(r'getElementById\("([A-Za-z0-9_]+)"\)', self.js))
        self.assertTrue(used, "platform.js 没引用任何元素 id")
        html_ids = set(re.findall(r'id="([A-Za-z0-9_]+)"', self.html))
        missing = sorted(i for i in used if i not in html_ids)
        self.assertEqual(missing, [], f"platform.js 引用了 HTML 里不存在的元素 id: {missing}")

    def test_index_html_references_platform_assets(self):
        for ref in ("static/platform.js", "static/platform.css"):
            self.assertIn(ref, self.html)
        for name in ("platform.js", "platform.css"):
            self.assertTrue((STATIC / name).is_file(), f"缺少静态文件 {name}")

    def test_dynamic_platform_classes_have_css(self):
        """platform.js 动态拼出的 platform-* class 必须在 platform.css 有定义。"""
        used = set(re.findall(r'platform-[a-z-]+', self.js))
        defined = set(re.findall(r'\.(platform-[a-z-]+)', self.css))
        missing = sorted(c for c in used if c not in defined)
        self.assertEqual(missing, [], f"platform.js 用了 platform.css 未定义的类：{missing}")

    def test_risk_levels_match_backend(self):
        """前端档位集合 = 后端 risk.LEVELS（换档位必须两端同步）。"""
        from mylocalai.platform.risk import LEVELS
        js_levels = set(re.findall(r'^\s{4}(\w+): \{ label:', self.js, re.M))
        self.assertEqual(js_levels, set(LEVELS))

    def test_nav_switching_wired(self):
        """导航按钮 + 两个视图容器都在 HTML 里，切换函数在 JS 里。"""
        for element_id in ("navChat", "navPlatform", "platformView"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("function showView", self.js)
        self.assertIn('class="chat-layout"', self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
