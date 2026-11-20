# -*- coding: utf-8 -*-
"""材料批次离线测试（专业用户 A · 阶段 2）：存储 / 同源分组 / 汇总 / 编排 / Web API。

全部离线：verify 服务用 stub，不联网、不调真实 LLM；URL 抓取打桩。
运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_batch -v
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


def _prefs(*, results=True, raw=True, external=True) -> PrivacyPreferences:
    data = json.loads(json.dumps(DEFAULT_PREFERENCES))
    data["save_enabled"] = {"results": results, "raw_input": raw, "external_cache": raw}
    data["external_processing_enabled"] = external
    data["external_processing_providers"] = ["public-search", "public-web"]
    return PrivacyPreferences(data)


def _stub_verify(label="疑似谣言", risk=62.0, fail_positions=(), turns=3):
    """verify 服务 stub：fail_positions 里的第 N 次调用抛错（按 text 内容识别不了，
    用调用序计数——测试的 items 顺序即调用序）。"""
    state = {"calls": 0}

    def _svc(text, deep=False, on_component_done=None, cancel_check=None):
        index = state["calls"]
        state["calls"] += 1
        if index in fail_positions:
            raise RuntimeError("上游取证爆炸")
        if on_component_done:
            on_component_done("emotion")
        return {"label": label, "risk_score": risk, "signals": ["来源模糊"],
                "degraded_components": [], "agent_error": "", "llm_turns": turns,
                "evidence_book": {"claim": text, "entries": [
                    {"url": "https://gov.example/1", "domain": "gov.example",
                     "domain_tier": "官方权威", "source_kind": "官方（新华网）",
                     "stance": "反驳", "title": "官方辟谣", "snippet": "不实信息",
                     "fetched_at": "2026-11-20T08:00:00+08:00"}]}}

    _svc.calls = state
    return _svc


def _rules(**batch) -> dict:
    rules = {"batch": {"max_items": 8, "max_item_chars": 8000, "max_total_chars": 64000,
                       "item_deadline_s": 90, "deadline_s": 900,
                       "similarity_threshold": 0.72, "max_concurrent": 2}}
    rules["batch"].update(batch)
    return rules


class BatchTestBase(unittest.TestCase):
    def setUp(self):
        from mylocalai.assistant.tools_batch import run_batch
        from mylocalai.dossier.batches import BatchStore
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        self.run_batch = run_batch
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "app.db"
        self.store = BatchStore(db)
        self.addCleanup(self.store.close)
        self.dossiers = DossierService(DossierStore(db), privacy=_prefs())
        self.addCleanup(self.dossiers.close)

    def make_batch(self, texts, source="text", profile_id=None):
        items = [{"text": t, "url": ""} for t in texts]
        return self.store.create_batch("测试批次", source, items, profile_id=profile_id)


# ---------------------------------------------------------------------------
# A. 存储
# ---------------------------------------------------------------------------

class TestBatchStore(BatchTestBase):
    def test_create_list_items_and_status(self):
        """建批次/列条目/改状态/计数回写。"""
        bid = self.make_batch(["甲材料", "乙材料"])
        batch = self.store.get_batch(bid)
        self.assertEqual((batch["source"], batch["item_count"], batch["status"]),
                         ("text", 2, "running"))
        items = self.store.items(bid)
        self.assertEqual([it["raw_text"] for it in items], ["甲材料", "乙材料"])
        self.assertEqual([it["position"] for it in items], [0, 1])
        self.store.update_item(items[0]["id"], status="ok", label="疑似谣言",
                               risk_score=62.0, dossier_id=None)
        self.store.set_batch_status(bid, "partial", ok_count=1, failed_count=1,
                                    not_run_count=0, summary={"ok": 1})
        batch = self.store.get_batch(bid)
        self.assertEqual((batch["status"], batch["ok_count"], batch["failed_count"]),
                         ("partial", 1, 1))
        self.assertEqual(batch["summary"], {"ok": 1})
        listed = self.store.list_batches()
        self.assertEqual([b["id"] for b in listed], [bid])

    def test_profile_scoping_and_active(self):
        """批次按工作区分家；active_profile_id 可读。"""
        self.assertGreater(self.store.active_profile_id(), 0)
        default_id = self.store.active_profile_id()
        second = self.store.create_profile = None  # 占位防误用（见下）
        del second
        from mylocalai.dossier.profiles import ProfileStore
        profiles = ProfileStore(Path(self.tmp.name) / "app.db")
        self.addCleanup(profiles.close)
        other = profiles.create_profile("other-ws", "别区", "analyst")
        mine = self.make_batch(["甲"], profile_id=default_id)
        theirs = self.make_batch(["乙"], profile_id=other)
        self.assertEqual([b["id"] for b in self.store.list_batches(
            profile_id=default_id)], [mine])
        self.assertEqual([b["id"] for b in self.store.list_batches(
            profile_id=other)], [theirs])
        self.assertEqual(self.store.batch_profile_id(theirs), other)

    def test_item_update_rejects_unknown_field(self):
        """update_item 只放行白名单字段（防误写）。"""
        bid = self.make_batch(["甲"])
        item = self.store.items(bid)[0]
        with self.assertRaises(KeyError):
            self.store.update_item(item["id"], batch_id=999)


# ---------------------------------------------------------------------------
# B. 同源分组与汇总（纯函数）
# ---------------------------------------------------------------------------

class TestBatchSummary(unittest.TestCase):
    def test_group_similar_exact_duplicates_only(self):
        """精确重复才同组；松散近义不组（阈值与指纹闸同口径，保守不擅自合并）。"""
        from mylocalai.assistant.tools_batch import group_similar
        a = "网传某地自来水被下毒，官方不敢通报"
        groups = group_similar([a, a, "今天天气不错"], 0.72)
        self.assertEqual(groups[0], groups[1])
        self.assertGreater(groups[0], 0)
        self.assertEqual(groups[2], 0)

    def test_summarize_counts_labels_groups_degraded(self):
        """汇总：状态计数/标签分布/同源组/degraded 清单。"""
        from mylocalai.dossier.batch_summary import summarize
        items = [
            {"position": 0, "status": "ok", "label": "疑似谣言", "risk_score": 62,
             "dossier_id": 1, "group_id": 1},
            {"position": 1, "status": "ok", "label": "疑似谣言", "risk_score": 58,
             "dossier_id": 2, "group_id": 1},
            {"position": 2, "status": "ok", "label": "存疑待核实", "risk_score": 40,
             "dossier_id": 3, "group_id": 0},
            {"position": 3, "status": "error", "error": "引擎超时", "group_id": 0},
            {"position": 4, "status": "blocked", "error": "未授权抓取", "group_id": 0},
            {"position": 5, "status": "not_run", "error": "预算用尽", "group_id": 0},
        ]
        s = summarize(items)
        self.assertEqual((s["total"], s["ok"], s["failed"], s["blocked"], s["not_run"]),
                         (6, 3, 1, 1, 1))
        self.assertEqual(s["by_label"], {"疑似谣言": 2, "存疑待核实": 1})
        self.assertEqual(len(s["groups"]), 1)
        self.assertEqual(s["groups"][0]["positions"], [0, 1])
        self.assertEqual(len(s["degraded"]), 2)

    def test_summary_card_shape(self):
        """卡片载荷：card_type/计数字段/items 明细齐全。"""
        from mylocalai.dossier.batch_summary import summary_card
        items = [{"position": 0, "status": "ok", "label": "低风险", "risk_score": 10,
                  "dossier_id": 5, "group_id": 0}]
        card = summary_card({"id": 9, "title": "T", "source": "urls"}, items)
        self.assertEqual(card["card_type"], "batch_summary")
        self.assertEqual(card["batch_id"], 9)
        self.assertEqual(card["total"], 1)
        self.assertEqual(card["items"][0]["dossier_id"], 5)
        self.assertIn("as_of", card)


# ---------------------------------------------------------------------------
# C. 编排（run_batch）
# ---------------------------------------------------------------------------

class TestRunBatch(BatchTestBase):
    def test_all_ok_creates_dossiers_and_card(self):
        """全成功：逐条建档、卡片事件含汇总、批次 done。"""
        from mylocalai.assistant.events import EventStream
        bid = self.make_batch(["材料甲", "材料乙"])
        events = EventStream()
        out = self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                             services={"verify": _stub_verify()}, events=events,
                             rules=_rules())
        self.assertEqual(out["summary"]["ok"], 2)
        batch = self.store.get_batch(bid)
        self.assertEqual(batch["status"], "done")
        items = self.store.items(bid)
        self.assertTrue(all(it["status"] == "ok" for it in items))
        self.assertTrue(all(it["dossier_id"] for it in items))
        self.assertTrue(all(it["label"] == "疑似谣言" for it in items))
        names = [e["event"] for e in events.snapshot()]
        self.assertIn("meta", names)
        self.assertIn("card", names)
        self.assertEqual(names[-1], "done")
        card = next(e for e in events.snapshot() if e["event"] == "card")
        self.assertEqual(card["card_type"], "batch_summary")
        self.assertEqual(card["ok"], 2)

    def test_single_failure_does_not_block_others(self):
        """单条失败记 error，其余照跑；批次 partial。"""
        bid = self.make_batch(["甲", "乙", "丙"])
        self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                       services={"verify": _stub_verify(fail_positions=(1,))},
                       events=None, rules=_rules())
        items = self.store.items(bid)
        self.assertEqual([it["status"] for it in items], ["ok", "error", "ok"])
        self.assertIn("上游取证爆炸", items[1]["error"])
        self.assertEqual(self.store.get_batch(bid)["status"], "partial")

    def test_verify_service_missing_marks_error(self):
        """服务未装配：每条 error，不崩。"""
        bid = self.make_batch(["甲"])
        self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                       services={}, events=None, rules=_rules())
        items = self.store.items(bid)
        self.assertEqual(items[0]["status"], "error")
        self.assertEqual(items[0]["error"], "核查服务未装配")

    def test_budget_exhausted_marks_not_run(self):
        """总预算为 0：全部条目标 not_run（绝不把"没跑"显示成"没发现"）。"""
        bid = self.make_batch(["甲", "乙"])
        self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                       services={"verify": _stub_verify()}, events=None,
                       rules=_rules(deadline_s=0))
        items = self.store.items(bid)
        self.assertTrue(all(it["status"] == "not_run" for it in items))
        self.assertEqual(self.store.get_batch(bid)["status"], "failed")

    def test_cancel_marks_remaining_not_run(self):
        """取消：后续条目标 not_run，已完成的保留。"""
        bid = self.make_batch(["甲", "乙", "丙"])
        state = {"n": 0}

        def cancel():
            state["n"] += 1
            return state["n"] > 1     # 第一条后取消

        self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                       services={"verify": _stub_verify()}, events=None,
                       rules=_rules(), cancel_check=cancel)
        items = self.store.items(bid)
        self.assertEqual(items[0]["status"], "ok")
        self.assertEqual([it["status"] for it in items[1:]], ["not_run", "not_run"])

    def test_similar_items_grouped_but_judged_separately(self):
        """同源材料各自判定（组只影响汇总呈现，不合并结论）。"""
        text = "网传某地自来水被下毒，官方不敢通报"
        bid = self.make_batch([text, text, "完全不同的另一条材料"])
        self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                       services={"verify": _stub_verify()}, events=None,
                       rules=_rules())
        items = self.store.items(bid)
        self.assertEqual(items[0]["group_id"], items[1]["group_id"])
        self.assertEqual(items[2]["group_id"], 0)
        # 两条同源材料各自建档、各自有结论
        self.assertNotEqual(items[0]["dossier_id"], items[1]["dossier_id"])
        summary = self.store.get_batch(bid)["summary"]
        self.assertEqual(len(summary["groups"]), 1)
        self.assertEqual(summary["groups"][0]["size"], 2)

    def test_url_item_blocked_without_authorization(self):
        """URL 条目未授权外部抓取 → blocked（fail-closed，零外发）。"""
        bid = self.store.create_batch("URL 批次", "urls",
                                      [{"text": "", "url": "https://example.com/a"}])
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs(external=False)):
            self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                           services={"verify": _stub_verify()}, events=None,
                           rules=_rules())
        item = self.store.items(bid)[0]
        self.assertEqual(item["status"], "blocked")
        self.assertIn("未授权", item["error"])

    def test_url_item_fetches_with_authorization(self):
        """URL 条目授权后抓取正文并核查（fetch 打桩，不联网）。"""
        bid = self.store.create_batch("URL 批次", "urls",
                                      [{"text": "", "url": "https://example.com/a"}])
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs()), \
             mock.patch("mylocalai.retrieval.search.fetch_article",
                        return_value={"text": "抓取到的正文内容", "url": "https://example.com/a"}):
            self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                           services={"verify": _stub_verify()}, events=None,
                           rules=_rules())
        item = self.store.items(bid)[0]
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["raw_text"], "抓取到的正文内容")


# ---------------------------------------------------------------------------
# D. Web API
# ---------------------------------------------------------------------------

class TestBatchApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001  fastapi 缺失整组跳过
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.client = TestClient(webapp.app)

    def setUp(self):
        from mylocalai.dossier.batches import BatchStore
        from mylocalai.dossier.profiles import ProfileStore
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        from mylocalai.web import batch_api
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "dossier.db"
        self.store = BatchStore(db)
        self.addCleanup(self.store.close)
        self.service = DossierService(DossierStore(db), privacy=_prefs())
        self.addCleanup(self.service.close)
        self.profiles = ProfileStore(db)
        self.addCleanup(self.profiles.close)
        from mylocalai.web import batch_api, profiles_api
        for module, attr, value in (
                (batch_api, "get_batch_store", lambda: self.store),
                (self.webapp, "get_dossier_service", lambda: self.service),
                (profiles_api, "get_profile_store", lambda: self.profiles)):
            patcher = mock.patch.object(module, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # 批次 worker 用注入的 stub loop（不碰真实默认库）
        stub_loop = mock.Mock()
        stub_loop.dossiers = self.service
        stub_loop.services = {"verify": _stub_verify()}
        patcher = mock.patch.object(batch_api, "_loop_factory", lambda: stub_loop)
        patcher.start()
        self.addCleanup(patcher.stop)
        # worker 是 daemon 线程：收尾前 join，避免清理临时目录时连接仍被占用
        self.addCleanup(self._join_workers)

    @staticmethod
    def _join_workers(timeout: float = 10.0) -> None:
        for thread in threading.enumerate():
            if thread.name.startswith("batch-") and thread.is_alive():
                thread.join(timeout)

    def _wait_done(self, batch_id, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            batch = self.store.get_batch(batch_id) or {}
            if batch.get("status") != "running":
                return batch
            time.sleep(0.05)
        self.fail(f"批次 {batch_id} 未在 {timeout}s 内完成")

    def test_create_validation_errors(self):
        """条目形状/超限/空 → 400。"""
        cases = [
            ({"items": []}, "空"),
            ({"items": ["x"] * 9}, "超限"),
            ({"items": [123]}, "形状"),
            ({"items": ["", ""]}, "空材料"),
            ({"items": [{"url": "ftp://x"}]}, "URL 协议"),
        ]
        for body, label in cases:
            resp = self.client.post("/api/batch", json=body)
            self.assertEqual(resp.status_code, 400, f"{label} 应 400：{resp.text}")

    def test_create_runs_and_completes(self):
        """202 → 后台跑完 → 详情/列表/汇总/卡片齐全。"""
        resp = self.client.post("/api/batch", json={
            "items": ["材料甲", "材料乙"], "title": "冒烟批次", "source": "text"})
        self.assertEqual(resp.status_code, 202)
        bid = resp.json()["batch"]["id"]
        batch = self._wait_done(bid)
        self.assertEqual(batch["status"], "done")
        self.assertEqual(batch["ok_count"], 2)

        detail = self.client.get(f"/api/batch/{bid}").json()
        self.assertEqual(len(detail["items"]), 2)
        self.assertEqual(detail["summary"]["ok"], 2)
        listed = self.client.get("/api/batch").json()["batches"]
        self.assertIn(bid, [b["id"] for b in listed])

    def test_stream_replays_finished_batch(self):
        """已完结批次的 SSE 一次性重放（meta/card/done）。"""
        from mylocalai.assistant.tools_batch import run_batch
        bid = self.store.create_batch("重放", "text",
                                      [{"text": "甲", "url": ""}])
        run_batch(bid, store=self.store, dossiers=self.service,
                  services={"verify": _stub_verify()}, events=None,
                  rules=_rules())
        with self.client.stream("GET", f"/api/batch/{bid}/stream") as resp:
            self.assertEqual(resp.status_code, 200)
            body = "".join(chunk for chunk in resp.iter_text())
        self.assertIn("event: meta", body)
        self.assertIn("event: card", body)
        self.assertIn("batch_summary", body)
        self.assertIn("event: done", body)

    def test_cross_profile_batch_404(self):
        """批次属于别的工作区 → 404（详情与流都是）。"""
        other = self.profiles.create_profile("other-ws", "别区", "analyst")
        bid = self.store.create_batch("别区批次", "text", [{"text": "甲", "url": ""}],
                                      profile_id=other)
        self.assertEqual(self.client.get(f"/api/batch/{bid}").status_code, 404)
        resp = self.client.get(f"/api/batch/{bid}/stream")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.client.get("/api/batch/99999").status_code, 404)

    def test_list_scoped_by_active_profile(self):
        """列表按激活档案过滤。"""
        other = self.profiles.create_profile("other-ws", "别区", "analyst")
        mine = self.store.create_batch("我的", "text", [{"text": "甲", "url": ""}])
        self.store.create_batch("别区的", "text", [{"text": "乙", "url": ""}],
                                profile_id=other)
        listed = self.client.get("/api/batch").json()["batches"]
        self.assertEqual([b["id"] for b in listed], [mine])
        self.client.post(f"/api/profiles/{other}/activate")
        listed = self.client.get("/api/batch").json()["batches"]
        self.assertNotIn(mine, [b["id"] for b in listed])


class TestMaterialsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.client = TestClient(webapp.app)

    def _upload(self, filename, content):
        return self.client.post("/api/materials/upload",
                                files={"file": (filename, content,
                                                "application/octet-stream")})

    def test_txt_paragraphs(self):
        """txt 按空行分段。"""
        resp = self._upload("a.txt", "第一条材料\n\n第二条材料\n\n\n第三条")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 3)

    def test_markdown_paragraphs(self):
        """md 同 txt 口径。"""
        resp = self._upload("a.md", "# 标题\n\n正文一段\n\n正文二段")
        self.assertEqual(resp.json()["count"], 3)

    def test_json_strings_and_objects(self):
        """json：字符串数组 / 对象数组取 text 字段。"""
        resp = self._upload("a.json", json.dumps(["甲", "乙"], ensure_ascii=False))
        self.assertEqual(resp.json()["count"], 2)
        resp = self._upload("b.json", json.dumps(
            [{"text": "甲", "x": 1}, {"content": "乙"}], ensure_ascii=False))
        self.assertEqual([i for i in resp.json()["items"]], ["甲", "乙"])

    def test_csv_column(self):
        """csv：有 text 列取该列；否则取第一个非空单元格。"""
        resp = self._upload("a.csv", "id,text\n1,甲\n2,乙\n")
        self.assertEqual(resp.json()["items"], ["甲", "乙"])
        resp = self._upload("b.csv", "甲\n乙\n")
        self.assertEqual(resp.json()["items"], ["甲", "乙"])

    def test_rejections(self):
        """扩展名/大小/坏编码/空内容 → 400。"""
        self.assertEqual(self._upload("a.exe", "x").status_code, 400)
        big = "x" * (2 * 1024 * 1024 + 10)
        self.assertEqual(self._upload("a.txt", big).status_code, 400)
        self.assertEqual(self._upload("a.txt", b"\xff\xfe\x00bad").status_code, 400)
        self.assertEqual(self._upload("a.txt", "   ").status_code, 400)
        self.assertEqual(self._upload("a.json", '{"not": "a list"}').status_code, 400)


if __name__ == "__main__":
    unittest.main()
