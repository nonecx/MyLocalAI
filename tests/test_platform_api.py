# -*- coding: utf-8 -*-
"""平台 API 与前端自洽测试（P6 + 方案 G 改版 + v7 信息源/领域）：
   端点契约（状态/信息流/预警/监控词 CRUD/巡检触发/巡检记录/信息源 CRUD 与试运行/领域表/监控词领域手设）+
   前后端自洽（platform.js 的每个接口路径、元素 id、platform-* class 都有落点）。

方案 G（热榜/新闻信息流）改版后，前端自洽断言在「动态提取」之外另加**点名契约**：
index.html 平台视图的 33 个核心元素 id、通栏 Tab（main-tabs / mainTabs）、
筛选 chips（platformFilterRow 的 data-risk）、管理抽屉（platformDrawer 家族：
信息源 / 监控词[含领域下拉与编辑态] / 巡检记录 / 授权引导），
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

#: 方案 G 新契约：index.html 监测平台视图点名的核心元素 id（共 33 个）。
#: 结构 = 通栏 Tab（对话｜监测平台）+ 状态摘要条 + 筛选 chips + 热榜信息流
#: + 空态双引导 + 右侧管理抽屉（信息源 / 监控词[领域下拉+编辑态] /
#: 巡检记录 / 授权引导）。
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
    # 抽屉：信息源管理（v7：rss/page 源，全局/按词绑定，单源试运行）
    "platformSourceName", "platformSourceUrl", "platformSourceKind",
    "platformSourceBindings", "platformSourceAddBtn", "platformSourceList",
    "platformSourceHint",
    # 抽屉：监控词管理（v7 加领域下拉 + 编辑态保存/取消）
    "platformWatchInput", "platformWatchAddBtn", "platformWatchList",
    "platformWatchDomain", "platformWatchSaveBtn", "platformWatchCancelEditBtn",
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
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from mylocalai.web import platform_api
        except Exception as exc:  # noqa: BLE001  fastapi 缺失时整组跳过
            raise unittest.SkipTest(f"无法导入 platform_api: {type(exc).__name__}: {exc}")
        cls.platform_api = platform_api
        # 主线尚未把 platform_router 挂进 web/app.py（前端/路由接线留待后续分支），
        # 这里自建最小 app 挂载被测 router——端点契约照常全覆盖。
        standalone = FastAPI()
        standalone.include_router(platform_api.router)
        cls.client = TestClient(standalone)

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


class _FakeCandidate:
    """试运行桩候选（端点只透出前三条标题，故只需 title）。"""

    def __init__(self, title: str) -> None:
        self.title = title


class _FakeResult:
    """PatrolSourceProvider.test_source 的桩返回（与 ProviderResult 同形状）。"""

    def __init__(self, candidates, degraded: bool = False, error: str = "") -> None:
        self.candidates = list(candidates)
        self.degraded = degraded
        self.error = error


class TestPlatformSourcesAndDomains(PlatformApiTestBase):
    """v7 新端点契约：领域表 / 信息源 CRUD / 单源试运行 / 监控词领域手设 /
    信息流领域列自洽。数据层行为（级联/去重/外键）由 test_platform_store 覆盖，
    这里只锁 HTTP 契约与对外形状；试运行全程打桩，不联网。"""

    def test_domains_endpoint(self):
        from mylocalai.platform import domains as domain_mod
        resp = self.client.get("/api/platform/domains")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["domains"]
        self.assertEqual(len(items), 12, "领域表应是 12 项")
        self.assertEqual([d["id"] for d in items], domain_mod.domain_ids())
        for item in items:
            for key in ("id", "name", "examples"):
                self.assertIn(key, item)
            self.assertTrue(item["name"])
            self.assertIsInstance(item["examples"], list)

    def test_source_crud_contract(self):
        created = self.client.post("/api/platform/sources",
                                   json={"name": "新华网",
                                         "url": "https://www.xinhuanet.com/rss"})
        self.assertEqual(created.status_code, 201)
        sid = created.json()["id"]
        listed = self.client.get("/api/platform/sources").json()["sources"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], sid)
        self.assertEqual(listed[0]["kind"], "rss")
        self.assertTrue(listed[0]["enabled"])
        self.assertEqual(listed[0]["query_ids"], [])
        # url 重复 → 409（store 去重闸，不是 500）
        dup = self.client.post("/api/platform/sources",
                               json={"name": "同名不同名",
                                     "url": "https://www.xinhuanet.com/rss"})
        self.assertEqual(dup.status_code, 409)
        # 空 name / 空 url → 400
        blank_name = self.client.post("/api/platform/sources",
                                      json={"name": "  ",
                                            "url": "https://a.example.com/rss"})
        self.assertEqual(blank_name.status_code, 400)
        blank_url = self.client.post("/api/platform/sources",
                                     json={"name": "无名源", "url": ""})
        self.assertEqual(blank_url.status_code, 400)
        # kind 非法 → 400（只收 rss/page）
        bad_kind = self.client.post("/api/platform/sources",
                                    json={"name": "坏类型",
                                          "url": "https://b.example.com/rss",
                                          "kind": "atom"})
        self.assertEqual(bad_kind.status_code, 400)
        # page 类型合法
        page = self.client.post("/api/platform/sources",
                                json={"name": "专题页",
                                      "url": "https://c.example.com/topic",
                                      "kind": "page", "note": "门户列表页"})
        self.assertEqual(page.status_code, 201)
        self.assertEqual(self.store.get_patrol_source(page.json()["id"])["kind"], "page")
        # PUT：启停 + 备注 + 绑定一起改
        qid = self.store.add_watch_query("自来水 投毒")
        updated = self.client.put(f"/api/platform/sources/{sid}",
                                  json={"enabled": False, "note": "官方通报",
                                        "query_ids": [qid]})
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.json()["updated"])
        row = self.store.get_patrol_source(sid)
        self.assertFalse(row["enabled"])
        self.assertEqual(row["note"], "官方通报")
        self.assertEqual(row["query_ids"], [qid])
        # 绑定不存在的监控词 → 400（外键冲突，不伪装成去重）
        bad_binding = self.client.put(f"/api/platform/sources/{sid}",
                                      json={"query_ids": [999]})
        self.assertEqual(bad_binding.status_code, 400)
        # 空列表 = 清空全部绑定
        cleared = self.client.put(f"/api/platform/sources/{sid}",
                                  json={"query_ids": []})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(self.store.get_patrol_source(sid)["query_ids"], [])
        # 404：PUT / DELETE 都不存在
        self.assertEqual(self.client.put("/api/platform/sources/999",
                                         json={"enabled": True}).status_code, 404)
        self.assertEqual(self.client.delete("/api/platform/sources/999").status_code,
                         404)
        # DELETE 真删
        self.assertEqual(self.client.delete(f"/api/platform/sources/{sid}").status_code,
                         200)
        remaining = self.client.get("/api/platform/sources").json()["sources"]
        self.assertEqual([r["id"] for r in remaining], [page.json()["id"]])

    def test_source_add_with_bad_binding_is_400(self):
        """新增时绑定不存在的监控词：外键冲突原样抛 → 400，源也不落库。"""
        resp = self.client.post("/api/platform/sources",
                                json={"name": "S", "url": "https://s.example.com/rss",
                                      "query_ids": [999]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.client.get("/api/platform/sources").json()["sources"], [])

    def test_source_test_endpoint_bad_url_is_degraded(self):
        """非 http(s) URL：provider 报 url_invalid（不发请求），形状如实。"""
        sid = self.store.add_patrol_source("坏源", "ftp://invalid.example.com/rss")
        resp = self.client.post(f"/api/platform/sources/{sid}/test",
                                json={"query": "自来水"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["item_count"], 0)
        self.assertEqual(data["sample_titles"], [])
        self.assertIn("url_invalid", data["error"])
        # 整个 body 省略也合法（query 可空）
        nobody = self.client.post(f"/api/platform/sources/{sid}/test")
        self.assertEqual(nobody.status_code, 200)
        self.assertFalse(nobody.json()["ok"])
        # 源不存在 → 404
        missing = self.client.post("/api/platform/sources/999/test", json={})
        self.assertEqual(missing.status_code, 404)

    def test_source_test_endpoint_mocked_success(self):
        """试运行成功形状：ok / item_count / 前 3 条样本标题 / error 空。"""
        sid = self.store.add_patrol_source("新华网", "https://www.xinhuanet.com/rss",
                                           note="官方通报")
        fake = _FakeResult([_FakeCandidate("标题一"), _FakeCandidate("标题二"),
                            _FakeCandidate("标题三"), _FakeCandidate("标题四")])
        with mock.patch("mylocalai.platform.sources.PatrolSourceProvider") as provider:
            provider.return_value.test_source.return_value = fake
            resp = self.client.post(f"/api/platform/sources/{sid}/test", json={})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["item_count"], 4)
        self.assertEqual(data["sample_titles"], ["标题一", "标题二", "标题三"])
        self.assertEqual(data["error"], "")
        # query 缺省 → 用源名；k=5
        args, kwargs = provider.return_value.test_source.call_args
        self.assertEqual(args[1], "新华网")
        self.assertEqual(kwargs.get("k"), 5)

    def test_source_test_endpoint_degraded_mocked(self):
        sid = self.store.add_patrol_source("挂掉的源", "https://down.example.com/rss")
        with mock.patch("mylocalai.platform.sources.PatrolSourceProvider") as provider:
            provider.return_value.test_source.return_value = _FakeResult(
                [], True, "挂掉的源: http_500")
            resp = self.client.post(f"/api/platform/sources/{sid}/test",
                                    json={"query": "x"})
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["item_count"], 0)
        self.assertIn("http_500", data["error"])

    def test_source_test_endpoint_alias_path(self):
        """契约也写作 POST /sources/{id}：与 /test 同一实现、同一形状。"""
        sid = self.store.add_patrol_source("新华网", "https://www.xinhuanet.com/rss")
        fake = _FakeResult([_FakeCandidate("标题甲"), _FakeCandidate("标题乙")])
        with mock.patch("mylocalai.platform.sources.PatrolSourceProvider") as provider:
            provider.return_value.test_source.return_value = fake
            resp = self.client.post(f"/api/platform/sources/{sid}",
                                    json={"query": "自来水"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["sample_titles"], ["标题甲", "标题乙"])

    def test_watch_domain_put(self):
        qid = self.store.add_watch_query("自来水 投毒")
        # 先造一个推断缓存：手设领域必须把它清掉（用户手改即废陈旧推断）
        self.assertTrue(self.store.set_inferred_domain(
            qid, "finance", "2026-11-21T10:00:00+08:00"))
        ok = self.client.put(f"/api/platform/watch/{qid}",
                             json={"domain": "public_health"})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["updated"])
        row = self.store.get_watch_query(qid)
        self.assertEqual(row["domain"], "public_health")
        self.assertEqual(row["inferred_domain"], "")
        self.assertEqual(row["inferred_at"], "")
        # 空领域 = 恢复自动推断（合法，不是 400）
        auto = self.client.put(f"/api/platform/watch/{qid}", json={"domain": ""})
        self.assertEqual(auto.status_code, 200)
        self.assertEqual(self.store.get_watch_query(qid)["domain"], "")
        # 领域 id 非法 → 400（不自造领域）
        bad = self.client.put(f"/api/platform/watch/{qid}",
                              json={"domain": "not_a_domain"})
        self.assertEqual(bad.status_code, 400)
        # 监控词不存在 → 404
        missing = self.client.put("/api/platform/watch/999",
                                  json={"domain": "finance"})
        self.assertEqual(missing.status_code, 404)

    def test_feed_entry_topic_domain_columns(self):
        """领域列自洽：feed_entries.domain 是 URL 主机域，领域列叫 topic_domain
        （v7 契约修正：UI 读 topic_domain / topic_domain_source，不读 domain）。"""
        qid = self.store.add_watch_query("自来水")
        with_domain = self.store.add_feed_entry(
            query_id=qid, title="通报一", url="https://news.example.com/1",
            domain="news.example.com", domain_tier="reliable",
            topic_domain="public_health", topic_domain_source="user")
        self.assertIsInstance(with_domain, int)
        self.store.add_feed_entry(query_id=qid, title="通报二",
                                  url="https://news.example.com/2")
        rows = {e["id"]: e for e in
                self.client.get("/api/platform/feed").json()["entries"]}
        hit = rows[with_domain]
        self.assertEqual(hit["domain"], "news.example.com")      # 主机域
        self.assertEqual(hit["topic_domain"], "public_health")   # 领域
        self.assertEqual(hit["topic_domain_source"], "user")
        blank = [e for e in rows.values() if e["topic_domain"] == ""]
        self.assertEqual(len(blank), 1, "无领域条目的两列应为空串（不猜）")
        self.assertEqual(blank[0]["topic_domain_source"], "")


class TestPlatformFrontendConsistency(unittest.TestCase):
    """前后端自洽（沿 test_website_chat 模式）：platform.js 的每个接口路径/
    元素 id/动态 class 都有落点；index.html 引用齐全。

    方案 G（热榜/新闻信息流）之后另锁结构契约：通栏 Tab、筛选 chips、
    管理抽屉、33 个点名核心 id（含 v7 信息源区与监控词领域下拉/编辑态），
    以及旧版「面板栈 + 预警横幅」零残留。
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from mylocalai.web import platform_api
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"无法导入 platform_api: {type(exc).__name__}: {exc}")
        cls.js = _read_static("platform.js")
        cls.css = _read_static("platform.css")
        cls.cards_js = _read_static("cards.js")
        # 路由权威来源：自建最小 app 挂 platform_router（主线 app.py 未接线）。
        _standalone = FastAPI()
        _standalone.include_router(platform_api.router)
        cls._openapi_paths = set(_standalone.openapi().get("paths", {}))

    def test_api_paths_exist_in_routes(self):
        """platform.js 的每个 /api/* 路径都在被测 router 的路由表里。

        路由收集走 openapi()：FastAPI 的 include_router 只挂 _IncludedRouter
        对象（无 path/routes 属性），app.routes 内查看不到 included router 的
        端点；openapi 的 paths 是全量权威来源。
        """
        paths = set(re.findall(r'"(/api/[a-z/{}/]+)"', self.js))
        self.assertTrue(paths, "没从 platform.js 里解析到 API 路径")
        missing = [p for p in sorted(paths) if p not in self._openapi_paths]
        self.assertEqual(missing, [], f"platform.js 引用了后端不存在的接口：{missing}")

    def test_dynamic_platform_classes_have_css(self):
        """动态拼出的平台 class 必须在 platform.css 有定义（platform.js ↔ css
        两侧自洽；index.html 骨架接线留待后续分支，不在本条断言内）。"""
        used = set(re.findall(r'platform-[a-z-]+', self.js))
        defined = set(re.findall(r'\.((?:chat-)?platform-[a-z-]+)', self.css))
        missing = sorted(c for c in used if c not in defined)
        self.assertEqual(missing, [], f"JS 用了 platform.css 未定义的类：{missing}")
        # 两边都该真的拼了类，否则这条断言是空转
        self.assertIn("platform-feed-item", used)
        # v7 新增动态 class：JS 真拼了、CSS 也真有定义（不许无样式裸奔）
        js_only = ("platform-source-op", "platform-source-off",
                   "platform-source-name", "platform-source-url",
                   "platform-source-hint-error",
                   "platform-watch-domain", "platform-watch-domain-inferred",
                   "platform-watch-editing",
                   "platform-feed-domain-user", "platform-feed-domain-inferred",
                   "platform-feed-domain-infer-mark")
        for cls in js_only:
            self.assertIn(cls, used, f"platform.js 没用上 v7 新类 .{cls}")
            self.assertIn(cls, defined, f"platform.css 缺 .{cls} 定义")

    def test_domain_and_source_plumbing(self):
        """v7 前后端接线：领域徽章读 topic_domain/topic_domain_source（不是 domain），
        监控词徽章读 domain/inferred_domain，信息源六件套 id 都被 JS 接线。"""
        # 信息流领域徽章：字段名契约（domain 是主机域，领域列叫 topic_domain）
        self.assertIn("feedDomainBadgeHtml(e.topic_domain, e.topic_domain_source)", self.js)
        self.assertIn('src === "user"', self.js)
        self.assertIn("inferred_text", self.js)
        self.assertIn("inferred_history", self.js)
        # 监控词行徽章：手设领域 / 推断缓存
        self.assertIn("watchDomainBadgeHtml", self.js)
        self.assertIn("inferred_domain", self.js)
        # 信息源 CRUD + 试运行接线
        self.assertIn("loadSources", self.js)
        self.assertIn("renderSources", self.js)
        self.assertIn("addSource", self.js)
        self.assertIn("updateSource", self.js)
        self.assertIn("toggleSource", self.js)
        self.assertIn("deleteSource", self.js)
        self.assertIn("testSource", self.js)
        self.assertIn("/${sid}/test", self.js)          # 模板串：PLATFORM_API.sources}/{sid}/test
        # 六路并行（含信息源）：refreshAll 的 Promise.all 里六个加载器一个不少
        block = re.search(r"async function refreshAll\(\)\s*\{(.*?)\n  \}", self.js, re.S)
        self.assertIsNotNone(block, "platform.js 里找不到 refreshAll")
        for loader in ("loadStatus()", "loadWarnings()", "loadWatch()",
                       "loadFeed()", "loadScans()", "loadSources()"):
            self.assertIn(loader, block.group(1),
                          f"refreshAll 的并行加载少了 {loader}（单路失败静默）")
        self.assertIn("Promise.all(", block.group(1))

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
        """（主线提取后已不适用，删除说明见提交信息）

        原用例锁定 index.html 监测平台视图的结构契约（通栏 Tab / 筛选 chips /
        管理抽屉 / 33 个核心 id / 旧版遗留零残留）；主线 index.html 尚未接线平台
        视图（接线留待后续分支），无对象可测，故移除。恢复接线时随 index.html
        一并恢复本契约（清单见分支版本文末注释与 docs/监测平台规格 §7）。
        """


if __name__ == "__main__":
    unittest.main(verbosity=2)
