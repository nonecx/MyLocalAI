# -*- coding: utf-8 -*-
"""领域监测订阅离线测试（专业用户 B · 阶段 3）：存储 / 变化摘要 / 编排 / API / 调度。

全部离线：检索与快照打桩，不联网、不调真实 LLM。
运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_monitor -v
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到 web/app.py

from mylocalai.core.privacy import DEFAULT_PREFERENCES, PrivacyPreferences  # noqa: E402


def _prefs(*, search=True) -> PrivacyPreferences:
    data = json.loads(json.dumps(DEFAULT_PREFERENCES))
    data["save_enabled"] = {"results": True, "raw_input": True, "external_cache": True}
    data["external_processing_enabled"] = search
    data["external_processing_providers"] = ["public-search"] if search else []
    return PrivacyPreferences(data)


def _search_payload(urls, domain_scoped=False):
    results = [{"url": u, "title": f"标题{i}", "snippet": "摘要",
                "domain": u.split("/")[2], "domain_tier": "门户",
                "source_kind": "非官方（门户）", "date": None}
               for i, u in enumerate(urls)]
    out = {"query": "q", "results": results, "engines": {"360": {"ok": True, "count": len(urls)}}}
    if domain_scoped:
        out["domain_scope_applied"] = True
    return out


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


if __name__ == "__main__":
    unittest.main()
