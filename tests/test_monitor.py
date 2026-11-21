# -*- coding: utf-8 -*-
"""领域监测订阅离线测试（专业用户 B · 阶段 3）：存储 / 变化摘要 / 编排 / API / 调度。

全部离线：检索与快照打桩，不联网、不调真实 LLM；search_all(domains=...) 直测
按 provider 契约打桩（显式 providers 即视为已授权），调度线程生命周期用缩短
的 TICK_S 注入，不真等 60 秒。
运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_monitor -v
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到 web/app.py

from mylocalai.core.privacy import DEFAULT_PREFERENCES, PrivacyPreferences  # noqa: E402
from mylocalai.retrieval.search import SearchResult, classify_domain  # noqa: E402


def _prefs(*, search=True) -> PrivacyPreferences:
    data = json.loads(json.dumps(DEFAULT_PREFERENCES))
    data["save_enabled"] = {"results": True, "raw_input": True, "external_cache": True}
    data["external_processing_enabled"] = search
    data["external_processing_providers"] = ["public-search"] if search else []
    return PrivacyPreferences(data)


def _search_payload(urls, domain_scoped=False, scope_degraded=False):
    results = [{"url": u, "title": f"标题{i}", "snippet": "摘要",
                "domain": u.split("/")[2], "domain_tier": "门户",
                "source_kind": "非官方（门户）", "date": None}
               for i, u in enumerate(urls)]
    out = {"query": "q", "results": results, "engines": {"360": {"ok": True, "count": len(urls)}}}
    if domain_scoped:
        out["domain_scope_applied"] = True
    if scope_degraded:
        out["domain_scope_applied"] = False
        out["scope_degraded"] = True
    return out


class _StubProvider:
    """search_all 直测的打桩后端：query → URL 清单的固定映射，记录收到的查询。

    与 provider 契约一致（.search(query, k) → (结果, err)）；search_all 传显式
    providers 即视为已授权（caller-owned transport），全程不联网。
    """

    name = "stub"

    def __init__(self, by_query):
        self.by_query = dict(by_query)
        self.queries = []

    def search(self, query, k=6):
        self.queries.append(query)
        urls = list(self.by_query.get(query, []))[:max(1, int(k))]
        return [SearchResult(self.name, i + 1, f"标题{i}", u, "摘要", None,
                             classify_domain(u)) for i, u in enumerate(urls)], None


class MonitorTestBase(unittest.TestCase):
    def setUp(self):
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        from mylocalai.dossier.subscriptions import SubscriptionStore
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "app.db"
        self.store = SubscriptionStore(db)
        self.addCleanup(self.store.close)
        self.dossiers = DossierService(DossierStore(db), privacy=_prefs())
        self.addCleanup(self.dossiers.close)
        self.profile_id = self.store.active_profile_id()

    def make_sub(self, topic="某地自来水", keywords=None, domains=None, freq=3600):
        return self.store.create_subscription(topic, keywords or [], domains or [], freq,
                                              profile_id=self.profile_id)


# ---------------------------------------------------------------------------
# A. 存储
# ---------------------------------------------------------------------------

class TestSubscriptionStore(MonitorTestBase):
    def test_crud_and_due(self):
        """建/查/改/列/删 + due 计算（ISO 字符串可比）。"""
        sub_id = self.make_sub()
        row = self.store.get_subscription(sub_id)
        self.assertEqual((row["topic"], row["enabled"], row["frequency_s"]),
                         ("某地自来水", True, 3600))
        self.assertIsNotNone(row["next_run_at"])
        # next_run_at = created + freq：立即可到期（created 即 now）
        self.assertEqual(self.store.due_subscriptions("2999-01-01T00:00:00+08:00"), [row])
        self.assertEqual(self.store.due_subscriptions("2000-01-01T00:00:00+08:00"), [])
        updated = self.store.update_subscription(sub_id, frequency_s=600, enabled=False)
        self.assertEqual((updated["frequency_s"], updated["enabled"]), (600, False))
        self.assertEqual(self.store.due_subscriptions("2999-01-01T00:00:00+08:00"), [])
        listed = self.store.list_subscriptions(profile_id=self.profile_id)
        self.assertEqual([s["id"] for s in listed], [sub_id])
        self.store.delete_subscription(sub_id)
        self.assertIsNone(self.store.get_subscription(sub_id))

    def test_profile_scoping(self):
        """订阅按工作区分家。"""
        from mylocalai.dossier.profiles import ProfileStore
        profiles = ProfileStore(Path(self.tmp.name) / "app.db")
        self.addCleanup(profiles.close)
        other = profiles.create_profile("other-mon", "别区", "monitor")
        mine = self.make_sub()
        theirs = self.make_sub(topic="别区主题")
        # update 不放行 profile_id，直接改库造一个别区订阅（等价迁移/内部路径）
        with self.store._lock, self.store._conn:
            self.store._conn.execute("UPDATE subscriptions SET profile_id = ? WHERE id = ?",
                                     (other, theirs))
        self.assertEqual([s["id"] for s in self.store.list_subscriptions(
            profile_id=self.profile_id)], [mine])
        self.assertEqual([s["id"] for s in self.store.list_subscriptions(
            profile_id=other)], [theirs])
        self.assertEqual(self.store.subscription_profile_id(theirs), other)

    def test_runs_record(self):
        """运行记录 start/finish + summary 还原。"""
        sub_id = self.make_sub()
        run_id = self.store.start_run(sub_id)
        self.store.finish_run(run_id, "ok", results_count=3, new_count=1,
                              summary={"new_urls": ["https://a.com/1"]})
        runs = self.store.runs(sub_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["status"], runs[0]["results_count"], runs[0]["new_count"]),
                         ("ok", 3, 1))
        self.assertEqual(runs[0]["summary"]["new_urls"], ["https://a.com/1"])


# ---------------------------------------------------------------------------
# B. 变化摘要（纯函数）
# ---------------------------------------------------------------------------

class TestChangeSummary(unittest.TestCase):
    def test_url_diff_and_e_delta(self):
        """URL 差集 + E 分首末差；缺 E 时 delta 为 None（不编）。"""
        from mylocalai.assistant.tools_monitor import change_summary
        s = change_summary(["https://a.com/1", "https://b.com/2"],
                           ["https://b.com/2", "https://c.com/3"],
                           prev_e=40.0, new_e=55.5)
        self.assertEqual(s["new_urls"], ["https://c.com/3"])
        self.assertEqual(s["removed_urls"], ["https://a.com/1"])
        self.assertEqual(s["e_delta"], 15.5)
        s2 = change_summary(None, ["https://a.com/1"])
        self.assertEqual(s2["new_urls"], ["https://a.com/1"])
        self.assertIsNone(s2["e_delta"])

    def test_normalizes_urls(self):
        """URL 归一（去尾斜杠/小写主机）后比对，避免同一页面算两条。"""
        from mylocalai.assistant.tools_monitor import change_summary
        s = change_summary(["https://A.com/1/"], ["https://a.com/1"])
        self.assertEqual((s["new_urls"], s["removed_urls"]), ([], []))


# ---------------------------------------------------------------------------
# C. 编排（run_subscription）
# ---------------------------------------------------------------------------

class TestRunSubscription(MonitorTestBase):
    def test_blocked_without_authorization_zero_outbound(self):
        """未授权：blocked + 订阅暂停 + search_all 零调用（fail-closed）。"""
        sub_id = self.make_sub()
        sub = self.store.get_subscription(sub_id)
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs(search=False)), \
             mock.patch("mylocalai.retrieval.search.search_all") as search:
            from mylocalai.assistant.tools_monitor import run_subscription
            out = run_subscription(self.store, sub, self.dossiers)
        self.assertEqual(out["status"], "blocked")
        search.assert_not_called()
        row = self.store.get_subscription(sub_id)
        self.assertFalse(row["enabled"])
        self.assertIn("未授权", row["paused_reason"])
        runs = self.store.runs(sub_id)
        self.assertEqual(runs[0]["status"], "blocked")

    def test_authorized_run_stores_evidence_and_advances(self):
        """授权后跑检：证据落监测档案、运行记录、next_run_at 推进、变化摘要。"""
        sub_id = self.make_sub(keywords=["下毒"], domains=["gov.cn"])
        sub = self.store.get_subscription(sub_id)
        urls = ["https://gov.cn/a", "https://news.example/b"]
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(urls, domain_scoped=True)):
            from mylocalai.assistant.tools_monitor import run_subscription
            out = run_subscription(self.store, sub, self.dossiers)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["results_count"], 2)
        self.assertEqual(out["new_count"], 2)
        row = self.store.get_subscription(sub_id)
        self.assertTrue(row["enabled"])
        self.assertNotEqual(row["next_run_at"], row["last_run_at"])   # 推进了
        dossier_id = row["dossier_id"]
        self.assertIsNotNone(dossier_id)
        evidence = self.dossiers.store.evidence(int(dossier_id))
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0]["batch"], 1)
        # 快照落了（best-effort，保存开关开着）
        self.assertTrue(self.dossiers.store.snapshots(int(dossier_id)))

    def test_second_run_counts_only_new(self):
        """第二轮只把新增 URL 计 new（同一批不再算新）。"""
        sub_id = self.make_sub()
        urls = ["https://gov.cn/a", "https://news.example/b"]
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(urls)):
            from mylocalai.assistant.tools_monitor import run_subscription
            run_subscription(self.store, self.store.get_subscription(sub_id), self.dossiers)
            out2 = run_subscription(self.store, self.store.get_subscription(sub_id),
                                    self.dossiers)
        self.assertEqual(out2["new_count"], 0)
        third_urls = urls + ["https://gov.cn/c"]
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(third_urls)):
            out3 = run_subscription(self.store, self.store.get_subscription(sub_id),
                                    self.dossiers)
        self.assertEqual(out3["new_count"], 1)
        self.assertEqual(out3["summary"]["new_urls"], ["https://gov.cn/c"])

    def test_search_failure_records_error_and_advances(self):
        """检索炸了：error 记录 + next_run_at 仍推进（失败不卡死订阅）。"""
        sub_id = self.make_sub()
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        side_effect=RuntimeError("网络炸了")):
            from mylocalai.assistant.tools_monitor import run_subscription
            out = run_subscription(self.store, self.store.get_subscription(sub_id),
                                   self.dossiers)
        self.assertEqual(out["status"], "error")
        self.assertIn("网络炸了", out["summary"]["reason"])
        row = self.store.get_subscription(sub_id)
        self.assertIsNotNone(row["last_run_at"])

    def test_advance_does_not_reenable_paused_subscription(self):
        """R8：暂停（enabled=0）的订阅跑一轮后保持暂停，不隐式恢复启用。

        手动/直接跑一轮只写终态与推进 next_run_at，启停与暂停原因不动——
        否则"跑一次→恢复→下一轮授权闸又暂停"会来回抖动。
        """
        from mylocalai.assistant.tools_monitor import run_subscription
        sub_id = self.make_sub()
        reason = "外部检索未授权（设置 → 隐私与授权 → 外部处理），订阅已暂停"
        self.store.update_subscription(sub_id, enabled=False, paused_reason=reason)
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(["https://gov.cn/a"])):
            out = run_subscription(self.store, self.store.get_subscription(sub_id),
                                   self.dossiers)
        self.assertEqual(out["status"], "ok")
        row = self.store.get_subscription(sub_id)
        self.assertFalse(row["enabled"])                # 启停不动
        self.assertEqual(row["paused_reason"], reason)  # 暂停原因保留
        self.assertEqual(row["last_status"], "ok")      # 终态照写
        self.assertIsNotNone(row["next_run_at"])        # next_run_at 照常推进

    def test_scope_annotations_in_summary(self):
        """R9：site: 两个标注都进 run.summary——兜底降级 applied=False +
        degraded=True，正常限定 applied=True。"""
        from mylocalai.assistant.tools_monitor import run_subscription
        sub_id = self.make_sub(domains=["gov.cn"])
        # 第一轮：检索层报告"变体全空兜底"（scope_degraded=True）
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(["https://news.example/b"],
                                                     scope_degraded=True)):
            out = run_subscription(self.store, self.store.get_subscription(sub_id),
                                   self.dossiers)
        self.assertFalse(out["summary"]["domain_scope_applied"])
        self.assertTrue(out["summary"]["scope_degraded"])
        self.assertTrue(self.store.runs(sub_id)[0]["summary"]["scope_degraded"])
        # 第二轮：正常限定 → applied=True、degraded=False
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(["https://gov.cn/a"],
                                                     domain_scoped=True)):
            out2 = run_subscription(self.store, self.store.get_subscription(sub_id),
                                    self.dossiers)
        self.assertTrue(out2["summary"]["domain_scope_applied"])
        self.assertFalse(out2["summary"]["scope_degraded"])

    def test_change_card_shape(self):
        """变化卡字段齐全（前端消费契约）。"""
        from mylocalai.assistant.tools_monitor import change_card
        card = change_card({"id": 3, "topic": "某地自来水"},
                           {"id": 9, "status": "ok", "results_count": 2, "new_count": 1,
                            "summary": {"new_urls": ["https://a.com/1"], "e_score": 55.0,
                                        "prev_e_score": 40.0, "e_delta": 15.0},
                            "finished_at": "2026-11-20T10:00:00+08:00"})
        self.assertEqual(card["card_type"], "monitor_change")
        self.assertEqual((card["subscription_id"], card["run_id"]), (3, 9))
        self.assertEqual(card["new_urls"], ["https://a.com/1"])
        self.assertEqual(card["e_delta"], 15.0)


# ---------------------------------------------------------------------------
# D. Web API + 调度
# ---------------------------------------------------------------------------

class TestMonitorApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.client = TestClient(webapp.app)

    def setUp(self):
        from mylocalai.dossier.profiles import ProfileStore
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        from mylocalai.dossier.subscriptions import SubscriptionStore
        from mylocalai.web import monitor_api
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "dossier.db"
        self.store = SubscriptionStore(db)
        self.addCleanup(self.store.close)
        self.service = DossierService(DossierStore(db), privacy=_prefs())
        self.addCleanup(self.service.close)
        self.profiles = ProfileStore(db)
        self.addCleanup(self.profiles.close)
        for module, attr, value in (
                (monitor_api, "get_subscription_store", lambda: self.store),
                (monitor_api, "_dossiers_factory", lambda: self.service),
                (self.webapp, "get_dossier_service", lambda: self.service)):
            patcher = mock.patch.object(module, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_create_validation_and_crud(self):
        """新建校验（主题空/频率低/域名多 → 400）+ CRUD + 404。"""
        bad = [
            ({"topic": "", "frequency_s": 3600}, "空主题"),
            ({"topic": "x", "frequency_s": 60}, "频率低"),
            ({"topic": "x", "domains": ["a.com", "b.com", "c.com", "d.com"]}, "域名多"),
            ({"topic": "x", "keywords": [f"k{i}" for i in range(11)]}, "关键词多"),
        ]
        for body, label in bad:
            resp = self.client.post("/api/subscriptions", json=body)
            self.assertEqual(resp.status_code, 400, f"{label} 应 400：{resp.text}")

        created = self.client.post("/api/subscriptions", json={
            "topic": "某地自来水", "keywords": ["下毒"], "domains": ["gov.cn"],
            "frequency_s": 1800}).json()["subscription"]
        self.assertEqual(created["topic"], "某地自来水")
        sid = created["id"]
        listed = self.client.get("/api/subscriptions").json()["subscriptions"]
        self.assertEqual([s["id"] for s in listed], [sid])

        updated = self.client.put(f"/api/subscriptions/{sid}",
                                  json={"frequency_s": 3600}).json()["subscription"]
        self.assertEqual(updated["frequency_s"], 3600)
        paused = self.client.put(f"/api/subscriptions/{sid}",
                                 json={"enabled": False}).json()["subscription"]
        self.assertFalse(paused["enabled"])
        self.assertEqual(self.client.delete(f"/api/subscriptions/{sid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/subscriptions/{sid}").status_code, 404)
        self.assertEqual(self.client.post("/api/subscriptions", json={
            "topic": "x"}).status_code, 200)

    def test_cross_profile_404(self):
        """订阅属于别的工作区 → 404（详情/运行/删都是）。"""
        other = self.profiles.create_profile("other-mon", "别区", "monitor")
        sid = self.store.create_subscription("别区主题", [], [], 3600, profile_id=other)
        self.assertEqual(self.client.get(f"/api/subscriptions/{sid}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/subscriptions/{sid}/runs").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/subscriptions/{sid}").status_code, 404)

    def test_run_now_and_detail_card(self):
        """立即运行（202）→ 后台跑完 → 详情带变化卡。"""
        created = self.client.post("/api/subscriptions", json={
            "topic": "某地自来水", "domains": ["gov.cn"], "frequency_s": 1800}).json()["subscription"]
        sid = created["id"]
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(["https://gov.cn/a"])):
            resp = self.client.post(f"/api/subscriptions/{sid}/run-now")
            self.assertEqual(resp.status_code, 202)
            # 同步等待后台线程收尾（_running 清空即完成）
            for _ in range(100):
                row = self.store.get_subscription(sid) or {}
                if row.get("last_status") in ("ok", "error", "blocked", "partial"):
                    break
                threading.Event().wait(0.05)
        detail = self.client.get(f"/api/subscriptions/{sid}").json()
        self.assertEqual(detail["subscription"]["last_status"], "ok")
        self.assertEqual(len(detail["runs"]), 1)
        self.assertEqual(detail["card"]["card_type"], "monitor_change")
        self.assertEqual(detail["card"]["results_count"], 1)
        # 重入保护：手动占位后 409
        from mylocalai.web import monitor_api
        with monitor_api._running_lock:
            monitor_api._running.add(sid)
        try:
            self.assertEqual(
                self.client.post(f"/api/subscriptions/{sid}/run-now").status_code, 409)
        finally:
            with monitor_api._running_lock:
                monitor_api._running.discard(sid)

    def test_run_now_marks_running_then_terminal(self):
        """R2：run-now 起跑即 last_status='running'（响应体带 run_id），收尾恢复终态。

        用 stub 执行器卡在运行中间态，模拟"上一轮已 ok、点立即运行后首轮
        轮询"的竞态现场：起跑不写 running 就会读到旧终态误报完成。
        """
        created = self.client.post("/api/subscriptions", json={
            "topic": "某地自来水", "frequency_s": 1800}).json()["subscription"]
        sid = created["id"]
        self.store.update_subscription(sid, last_status="ok")   # 造上一轮旧终态
        from mylocalai.assistant import tools_monitor
        real_run = tools_monitor.run_subscription
        entered = threading.Event()
        gate = threading.Event()

        def slow_run(store, sub, dossiers, **kwargs):
            entered.set()
            gate.wait(5)                          # 卡在运行中间态
            return real_run(store, sub, dossiers, **kwargs)

        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(["https://gov.cn/a"])), \
             mock.patch("mylocalai.assistant.tools_monitor.run_subscription",
                        side_effect=slow_run):
            resp = self.client.post(f"/api/subscriptions/{sid}/run-now")
            self.assertEqual(resp.status_code, 202)
            body = resp.json()
            self.assertTrue(body["started"])
            run_id = body["run_id"]
            self.assertIsInstance(run_id, int)
            # 起跑即 running：不等收尾，首轮轮询就必须看到进行中
            detail = self.client.get(f"/api/subscriptions/{sid}").json()
            self.assertEqual(detail["subscription"]["last_status"], "running")
            self.assertTrue(entered.wait(5))      # 后台线程已进执行器
            gate.set()                            # 放行 → 真执行器照常写终态
            for _ in range(100):
                row = self.store.get_subscription(sid) or {}
                if row.get("last_status") == "ok":
                    break
                threading.Event().wait(0.05)
        self.assertEqual(self.store.get_subscription(sid)["last_status"], "ok")
        # 复用预建运行行，不另建：runs 里恰一行且 id 即响应体里的 run_id
        runs = self.store.runs(sid)
        self.assertEqual([r["id"] for r in runs], [run_id])
        self.assertEqual(runs[0]["status"], "ok")

    def test_run_now_rejects_paused_subscription(self):
        """R8：enabled=0 的订阅 run-now → 409（中文 detail 带暂停原因）。

        不执行、不建运行行、不隐式恢复；显式恢复启用后才能手动跑。
        """
        created = self.client.post("/api/subscriptions", json={
            "topic": "某地自来水", "frequency_s": 1800}).json()["subscription"]
        sid = created["id"]
        reason = "外部检索未授权（设置 → 隐私与授权 → 外部处理），订阅已暂停"
        self.store.update_subscription(sid, enabled=False, paused_reason=reason)
        with mock.patch("mylocalai.retrieval.search.search_all") as search:
            resp = self.client.post(f"/api/subscriptions/{sid}/run-now")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("已暂停", resp.json()["detail"])
        self.assertIn("外部检索未授权", resp.json()["detail"])
        search.assert_not_called()                       # 零执行
        row = self.store.get_subscription(sid)
        self.assertFalse(row["enabled"])                 # 不隐式恢复
        self.assertEqual(row["paused_reason"], reason)   # 暂停原因保留
        self.assertEqual(self.store.runs(sid), [])       # 没起跑（无运行行）
        # 用户显式恢复启用后，手动运行照常可用
        self.client.put(f"/api/subscriptions/{sid}", json={"enabled": True})
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(["https://gov.cn/a"])):
            resp2 = self.client.post(f"/api/subscriptions/{sid}/run-now")
        self.assertEqual(resp2.status_code, 202)
        self.assertIsInstance(resp2.json()["run_id"], int)
        for _ in range(100):
            row = self.store.get_subscription(sid) or {}
            if row.get("last_status") in ("ok", "error", "blocked", "partial"):
                break
            threading.Event().wait(0.05)
        self.assertEqual(self.store.get_subscription(sid)["last_status"], "ok")

    def test_scheduler_tick_runs_due_only(self):
        """调度 tick：只跑到期订阅；不到期不跑。"""
        from mylocalai.web import monitor_api
        mine = self.store.create_subscription("到期主题", [], [], 3600,
                                              profile_id=self.store.active_profile_id())
        future = self.store.create_subscription("未来主题", [], [], 3600,
                                                profile_id=self.store.active_profile_id())
        self.store.update_subscription(future, next_run_at="2999-01-01T00:00:00+08:00")
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(["https://gov.cn/x"])):
            started = monitor_api.scheduler_tick()
        self.assertEqual(started, 1)
        self.assertEqual(self.store.get_subscription(mine)["last_status"], "ok")
        self.assertEqual(self.store.get_subscription(future)["last_status"], "")
        # 再 tick：没有新到期的（刚推进过）
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(["https://gov.cn/x"])):
            self.assertEqual(monitor_api.scheduler_tick(), 0)

    def test_scheduler_lifecycle(self):
        """调度线程生命周期：start 后线程活着；stop 置位 Event 并在超时内退出。"""
        from mylocalai.web import monitor_api
        # TICK_S 缩短注入（模块常量在 loop 里即时读取，patch 即生效）：不真等 60 秒
        with mock.patch.object(monitor_api, "TICK_S", 0.02):
            thread = monitor_api.start_scheduler()
            try:
                self.assertTrue(thread.is_alive())
                self.assertFalse(monitor_api._scheduler_stop.is_set())
            finally:
                monitor_api.stop_scheduler()
            thread.join(timeout=5)               # 带超时断言：停了就必须真退出
            self.assertFalse(thread.is_alive())
            self.assertTrue(monitor_api._scheduler_stop.is_set())

    def test_scheduler_loop_ticks_due(self):
        """起线程后 loop 按 TICK_S 扫 due：到期订阅被真正跑一轮（缩短 tick）。"""
        from mylocalai.web import monitor_api
        sid = self.store.create_subscription("到期主题", [], [], 3600,
                                             profile_id=self.store.active_profile_id())
        with mock.patch.object(monitor_api, "TICK_S", 0.05), \
             mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.search_all",
                        return_value=_search_payload(["https://gov.cn/x"])):
            thread = monitor_api.start_scheduler()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    row = self.store.get_subscription(sid) or {}
                    if row.get("last_status") == "ok":
                        break
                    threading.Event().wait(0.05)
            finally:
                monitor_api.stop_scheduler()
            thread.join(timeout=5)
        self.assertEqual(self.store.get_subscription(sid)["last_status"], "ok")
        self.assertFalse(thread.is_alive())


# ---------------------------------------------------------------------------
# E. search_all(domains=...) 直测（打桩 providers，不联网）
# ---------------------------------------------------------------------------

class TestSearchAllDomains(unittest.TestCase):
    """检索层 domains 参数直测：变体构造、合并去重、后置过滤、全空兜底。

    此前 test_monitor 全程 stub search_all，domains 参数（site: 变体 ≤3、
    合并去重、兜底、domain_scope_applied）没有任何直接测试，这里补齐。
    """

    def test_variants_capped_at_three(self):
        """site: 变体最多构造 3 个（与订阅层 MAX_DOMAINS 同口径），原 query 永远在首位。"""
        from mylocalai.retrieval.search import search_all
        q = "某地自来水"
        p = _StubProvider({})
        search_all(q, k=5, providers=[p],
                   domains=["a.com", "b.com", "c.com", "d.com", "e.com"])
        self.assertEqual(p.queries,
                         [q, "site:a.com " + q, "site:b.com " + q, "site:c.com " + q])

    def test_merge_dedup_and_domain_filter(self):
        """变体结果与原 query 结果合并去重，并按域名后置过滤（限定生效）。"""
        from mylocalai.retrieval.search import search_all
        q = "某地自来水"
        p = _StubProvider({
            q: ["https://gov.cn/2", "https://news.example/1"],
            "site:gov.cn " + q: ["https://gov.cn/2", "https://gov.cn/3"],
        })
        out = search_all(q, k=5, providers=[p], domains=["gov.cn"])
        # gov.cn/2 在两个查询里都出现只计一次；域外结果被后置过滤
        self.assertEqual([r.url for r in out["results"]],
                         ["https://gov.cn/2", "https://gov.cn/3"])
        self.assertEqual(out["domain_scope_applied"], True)
        self.assertNotIn("scope_degraded", out)

    def test_fallback_marks_degraded(self):
        """R9：site: 变体全空 → 兜底保留非限定结果，如实标注未限定 + 降级。"""
        from mylocalai.retrieval.search import search_all
        q = "某地自来水"
        p = _StubProvider({q: ["https://news.example/1"]})   # site: 变体无结果
        out = search_all(q, k=5, providers=[p], domains=["gov.cn"])
        self.assertEqual([r.url for r in out["results"]], ["https://news.example/1"])
        self.assertEqual(out["domain_scope_applied"], False)
        self.assertEqual(out["scope_degraded"], True)

    def test_no_domains_unchanged(self):
        """不传 domains：逐位不变——只查原 query、结果截断 k 条、无 domain 标注键。"""
        from mylocalai.retrieval.search import search_all
        q = "某地自来水"
        urls = [f"https://news.example/{i}" for i in range(5)]
        p = _StubProvider({q: urls})
        out = search_all(q, k=3, providers=[p])
        self.assertEqual(set(out.keys()), {"query", "results", "engines"})
        self.assertEqual(p.queries, [q])
        self.assertEqual([r.url for r in out["results"]], urls[:3])


if __name__ == "__main__":
    unittest.main()
