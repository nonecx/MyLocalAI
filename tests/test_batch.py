# -*- coding: utf-8 -*-
"""材料批次离线测试（专业用户 A · 阶段 2）：存储 / 同源分组 / 汇总 / 编排 / Web API。

全部离线：verify 服务用 stub，不联网、不调真实 LLM；URL 抓取打桩。
运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_batch -v
"""

from __future__ import annotations

import asyncio
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

    def test_group_labels_accumulate_within_group(self):
        """R3：组内标签计数按成员累加——同组 3 条同标签如实 ×3（此前 dict 推导
        同键互相覆盖恒显示 ×1，与 cards.js 照实渲染的口径不符）。"""
        from mylocalai.dossier.batch_summary import summarize
        items = [{"position": i, "status": "ok", "label": "疑似谣言",
                  "risk_score": 60, "dossier_id": i + 1, "group_id": 1}
                 for i in range(3)]
        s = summarize(items)
        self.assertEqual(s["groups"][0]["labels"], {"疑似谣言": 3})
        # 混合标签组：各标签分别累加
        items.append({"position": 3, "status": "ok", "label": "存疑待核实",
                      "risk_score": 40, "dossier_id": 4, "group_id": 1})
        s = summarize(items)
        self.assertEqual(s["groups"][0]["labels"], {"疑似谣言": 3, "存疑待核实": 1})

    def test_summarize_three_elements_with_evidence(self):
        """规格 §5 三要素：立场矩阵 / 证据重合 Top / 信源战绩（纯函数，零 LLM）。"""
        from mylocalai.dossier.batch_summary import summarize
        items = [
            {"position": 0, "status": "ok", "label": "疑似谣言", "risk_score": 62,
             "dossier_id": 1},
            {"position": 1, "status": "ok", "label": "疑似谣言", "risk_score": 58,
             "dossier_id": 2},
            {"position": 2, "status": "ok", "label": "低风险", "risk_score": 10,
             "dossier_id": 3},
        ]
        evidence = {
            1: [{"url": "https://gov.example/1", "domain": "gov.example", "stance": "反驳"},
                {"url": "https://news.example/1", "domain": "news.example",
                 "stance": "支持"}],
            2: [{"url": "https://gov.example/1", "domain": "gov.example", "stance": "反驳"}],
            3: [{"url": "https://quiet.example/x", "domain": "quiet.example", "stance": ""}],
        }
        s = summarize(items, evidence_by_dossier=evidence)
        # 立场矩阵：结论标签 × 证据立场计数；无立场的数据行如实省略（低风险行）
        self.assertEqual(s["stance_matrix"], {"疑似谣言": {"反驳": 2, "支持": 1}})
        # 信源战绩：出现次数降序、同数按域名升序；反驳条目按 stance 原值计数
        self.assertEqual(s["sources_top"], [
            {"domain": "gov.example", "refute_entries": 2},
            {"domain": "news.example", "refute_entries": 0},
            {"domain": "quiet.example", "refute_entries": 0}])
        # cards.js 契约：sources_top 数组、元素恰有 domain(str)/refute_entries(int)
        for entry in s["sources_top"]:
            self.assertEqual(set(entry.keys()), {"domain", "refute_entries"})
            self.assertIsInstance(entry["domain"], str)
            self.assertIsInstance(entry["refute_entries"], int)
        # 证据重合：条目 0 与 1 同引 gov.example/1——URL 重合已隐含域名重合，
        # 去重后 shared 只记 URL，不重复计域名
        self.assertEqual(s["evidence_overlap_top"], [
            {"a": 0, "b": 1, "shared": ["https://gov.example/1"], "count": 1}])

    def test_three_elements_empty_without_evidence(self):
        """不给证据原料：三要素如实空结构（不编造、不抛）。"""
        from mylocalai.dossier.batch_summary import summarize
        s = summarize([{"position": 0, "status": "ok", "label": "疑似谣言",
                        "dossier_id": 9}])
        self.assertEqual((s["stance_matrix"], s["evidence_overlap_top"], s["sources_top"]),
                         ({}, [], []))

    def test_overlap_top_capped_at_five_and_empty_when_no_overlap(self):
        """重合 Top-N ≤5；0 重合给空列表（前端判空整块不渲染）。"""
        from mylocalai.dossier.batch_summary import summarize
        items = [{"position": i, "status": "ok", "label": "疑似谣言",
                  "dossier_id": i + 1} for i in range(4)]
        evidence = {
            1: [{"url": "", "domain": f"d{i}.example", "stance": "反驳"}
                for i in range(3)],
            2: [{"url": "", "domain": "d0.example", "stance": ""}],
            3: [{"url": "", "domain": "d0.example", "stance": ""},
                {"url": "", "domain": "d1.example", "stance": ""}],
            4: [{"url": "", "domain": "d0.example", "stance": ""},
                {"url": "", "domain": "d1.example", "stance": ""},
                {"url": "", "domain": "d2.example", "stance": ""}],
        }
        s = summarize(items, evidence_by_dossier=evidence)
        top = s["evidence_overlap_top"]
        # 6 对里 6 对重合：count 降序 [3,2,2,1,1,1]，Top-N 只留前 5
        self.assertEqual([p["count"] for p in top], [3, 2, 2, 1, 1])
        self.assertEqual(top[0], {"a": 0, "b": 3,
                                  "shared": ["d0.example", "d1.example", "d2.example"],
                                  "count": 3})
        # >5 对时只留前 5
        many = [{"position": i, "status": "ok", "label": "疑似谣言",
                 "dossier_id": i + 1} for i in range(8)]
        ev_many = {i + 1: [{"url": "", "domain": "same.example", "stance": ""}]
                   for i in range(8)}
        s_many = summarize(many, evidence_by_dossier=ev_many)
        self.assertEqual(len(s_many["evidence_overlap_top"]), 5)
        # 完全无重合 → 空列表
        s2 = summarize([{"position": 0, "status": "ok", "label": "L", "dossier_id": 1},
                        {"position": 1, "status": "ok", "label": "L", "dossier_id": 2}],
                       evidence_by_dossier={
                           1: [{"url": "https://a.example/1", "domain": "a.example",
                                "stance": ""}],
                           2: [{"url": "https://b.example/1", "domain": "b.example",
                                "stance": ""}]})
        self.assertEqual(s2["evidence_overlap_top"], [])


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

    def test_all_blocked_batch_is_partial_not_failed(self):
        """R5：全 blocked 批次不再落 failed——status=partial、failed_count=0，
        blocked 走 summary JSON 派生的 blocked_count（get/list 同口径）。"""
        bid = self.store.create_batch("URL 批次", "urls",
                                      [{"text": "", "url": f"https://example.com/{i}"}
                                       for i in range(3)])
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs(external=False)):
            self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                           services={"verify": _stub_verify()}, events=None,
                           rules=_rules())
        batch = self.store.get_batch(bid)
        self.assertEqual(batch["status"], "partial")
        self.assertEqual(batch["failed_count"], 0)
        self.assertEqual(batch["blocked_count"], 3)
        listed = self.store.list_batches()
        self.assertEqual((listed[0]["failed_count"], listed[0]["blocked_count"]),
                         (0, 3))

    def test_blocked_split_from_failed_in_store_counts(self):
        """R5：blocked 与 failed 分列——条目 error 记 failed_count，blocked 不再并入。"""
        bid = self.store.create_batch("混合批次", "urls",
                                      [{"text": "", "url": "https://example.com/a"},
                                       {"text": "纯文本材料", "url": ""}])
        with mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_prefs(external=False)):
            # services 空 → 文本条目 error（核查服务未装配）；URL 条目 blocked
            self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                           services={}, events=None, rules=_rules())
        items = self.store.items(bid)
        self.assertEqual([it["status"] for it in items], ["blocked", "error"])
        batch = self.store.get_batch(bid)
        self.assertEqual((batch["status"], batch["failed_count"], batch["blocked_count"]),
                         ("partial", 1, 1))

    def test_summary_includes_three_elements_from_stored_evidence(self):
        """编排收尾把条目档案证据喂给汇总：落库 summary 含信源战绩/立场矩阵。"""
        bid = self.make_batch(["材料甲"])
        self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                       services={"verify": _stub_verify()}, events=None,
                       rules=_rules())
        summary = self.store.get_batch(bid)["summary"]
        self.assertEqual(summary["sources_top"],
                         [{"domain": "gov.example", "refute_entries": 1}])
        self.assertEqual(summary["stance_matrix"], {"疑似谣言": {"反驳": 1}})
        self.assertEqual(summary["evidence_overlap_top"], [])

    def test_done_event_reports_real_cost_delta(self):
        """R7：done.cost = llm_client 计数器段前/段后快照差值（不再硬编码 0），
        且随 summary 落库——完结批次重放的 done 也如实。"""
        from mylocalai.assistant.events import EventStream
        counter = {"requests": 5, "tokens": 100}

        def fake_snapshot():
            counter["requests"] += 3
            counter["tokens"] += 150
            return dict(counter)

        bid = self.make_batch(["甲", "乙"])
        events = EventStream()
        with mock.patch("mylocalai.llm.llm_client.usage_snapshot", fake_snapshot):
            self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                           services={"verify": _stub_verify()}, events=events,
                           rules=_rules())
        done = next(e for e in events.snapshot() if e["event"] == "done")
        self.assertEqual(done["cost"], {"requests": 3, "tokens": 150})
        self.assertEqual(self.store.get_batch(bid)["summary"]["cost"],
                         {"requests": 3, "tokens": 150})

    def test_done_cost_degrades_to_zero_when_counter_unavailable(self):
        """R7：计数面不可用（读取抛错）→ done.cost 如实退 0（既有降级口径）。"""
        from mylocalai.assistant.events import EventStream
        bid = self.make_batch(["甲"])
        events = EventStream()
        with mock.patch("mylocalai.llm.llm_client.usage_snapshot",
                        side_effect=RuntimeError("counter unavailable")):
            self.run_batch(bid, store=self.store, dossiers=self.dossiers,
                           services={"verify": _stub_verify()}, events=events,
                           rules=_rules())
        done = next(e for e in events.snapshot() if e["event"] == "done")
        self.assertEqual(done["cost"], {"requests": 0, "tokens": 0})


# ---------------------------------------------------------------------------
# D. Web API
# ---------------------------------------------------------------------------

class _FakeStreamRequest:
    """batch_stream/_drain 的最小 request 替身：is_disconnected 恒 False。
    客户端断开用生成器 aclose 模拟（TestClient 提前关流同样走生成器 finally）。"""

    async def is_disconnected(self) -> bool:
        return False


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
        # 登记表是模块级单例：跨测试清场（各测试用各自的临时库，批次 id 会撞号）
        with batch_api._streams_lock:
            batch_api._streams.clear()
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

    def test_replay_done_cost_and_three_elements_from_stored_summary(self):
        """重放契约：done.cost 用编排期落库的真实值；卡片带三要素（sources_top）。"""
        from mylocalai.assistant.events import EventStream
        from mylocalai.assistant.tools_batch import run_batch
        counter = {"requests": 0, "tokens": 0}

        def fake_snapshot():
            counter["requests"] += 2
            counter["tokens"] += 7
            return dict(counter)

        bid = self.store.create_batch("重放成本", "text", [{"text": "甲", "url": ""}])
        events = EventStream()
        with mock.patch("mylocalai.llm.llm_client.usage_snapshot", fake_snapshot):
            run_batch(bid, store=self.store, dossiers=self.service,
                      services={"verify": _stub_verify()}, events=events,
                      rules=_rules())
        with self.client.stream("GET", f"/api/batch/{bid}/stream") as resp:
            body = "".join(chunk for chunk in resp.iter_text())
        done_data, card_data = None, None
        for frame in body.strip().split("\n\n"):
            lines = frame.split("\n")
            name = lines[0][len("event: "):]
            payload = json.loads(lines[1][len("data: "):])
            if name == "done":
                done_data = payload
            elif name == "card":
                card_data = payload
        self.assertEqual(done_data["cost"], {"requests": 2, "tokens": 7})
        self.assertEqual(card_data["sources_top"],
                         [{"domain": "gov.example", "refute_entries": 1}])

    def test_stream_disconnect_cancels_running_batch(self):
        """R1：SSE 客户端断开（"停止"按钮）→ request_cancel → 剩余条目 not_run、
        批次 cancelled；已完成的条目结论保留。"""
        from mylocalai.web import batch_api
        gate = threading.Event()
        release = threading.Event()

        def gated_verify(text, deep=False, on_component_done=None, cancel_check=None):
            gate.set()
            release.wait(timeout=10.0)   # 卡在"核查中"，模拟进行中的批次
            return {"label": "疑似谣言", "risk_score": 50.0, "signals": [],
                    "degraded_components": [], "agent_error": "", "llm_turns": 0,
                    "evidence_book": {"claim": text, "entries": []}}

        stub_loop = mock.Mock()
        stub_loop.dossiers = self.service
        stub_loop.services = {"verify": gated_verify}
        patcher = mock.patch.object(batch_api, "_loop_factory", lambda: stub_loop)
        patcher.start()
        self.addCleanup(patcher.stop)

        resp = self.client.post("/api/batch",
                                json={"items": ["甲", "乙", "丙"], "source": "text"})
        self.assertEqual(resp.status_code, 202)
        bid = resp.json()["batch"]["id"]
        self.assertTrue(gate.wait(timeout=10.0))   # 第一条已进入核查（批次在跑）

        async def _open_and_close():
            # 直播路径：取到事件流生成器后立刻关闭——即客户端断开 SSE
            stream = await batch_api.batch_stream(str(bid), _FakeStreamRequest())
            agen = stream.body_iterator
            first = await agen.__anext__()   # 至少吐出 meta 帧（证明是直播非重放）
            await agen.aclose()              # 断开 → finally → events.request_cancel()
            return first

        first = asyncio.run(_open_and_close())
        self.assertIn("event: meta", first)
        release.set()
        batch = self._wait_done(bid)
        self.assertEqual(batch["status"], "cancelled")
        statuses = [it["status"] for it in self.store.items(bid)]
        self.assertEqual(statuses, ["ok", "not_run", "not_run"])

    def test_stream_registry_cleaned_after_live_stream_ends(self):
        """R6：直播流收尾（见到 done）后，登记表条目被摘除（长驻进程防泄漏）。"""
        from mylocalai.web import batch_api
        resp = self.client.post("/api/batch",
                                json={"items": ["甲"], "source": "text"})
        bid = resp.json()["batch"]["id"]
        with batch_api._streams_lock:
            self.assertIn(bid, batch_api._streams)   # 开跑即登记
        with self.client.stream("GET", f"/api/batch/{bid}/stream") as resp:
            body = "".join(chunk for chunk in resp.iter_text())
        self.assertIn("event: done", body)
        with batch_api._streams_lock:
            self.assertNotIn(bid, batch_api._streams)

    def test_stream_registry_cleaned_on_replay(self):
        """R6：已完结批次（登记条目残留）访问一次流即被清理——重放不依赖登记表。"""
        from mylocalai.assistant.events import EventStream
        from mylocalai.assistant.tools_batch import run_batch
        from mylocalai.web import batch_api
        bid = self.store.create_batch("残留清理", "text", [{"text": "甲", "url": ""}])
        run_batch(bid, store=self.store, dossiers=self.service,
                  services={"verify": _stub_verify()}, events=None, rules=_rules())
        with batch_api._streams_lock:
            batch_api._streams[bid] = EventStream()   # 模拟历史泄漏残留
        with self.client.stream("GET", f"/api/batch/{bid}/stream") as resp:
            self.assertEqual(resp.status_code, 200)
            body = "".join(chunk for chunk in resp.iter_text())
        self.assertIn("event: done", body)
        with batch_api._streams_lock:
            self.assertNotIn(bid, batch_api._streams)

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
        """扩展名/坏编码/空内容 → 400。"""
        self.assertEqual(self._upload("a.exe", "x").status_code, 400)
        self.assertEqual(self._upload("a.txt", b"\xff\xfe\x00bad").status_code, 400)
        self.assertEqual(self._upload("a.txt", "   ").status_code, 400)
        self.assertEqual(self._upload("a.json", '{"not": "a list"}').status_code, 400)

    def test_oversize_upload_413_without_full_read(self):
        """R11：超限 413（中文 detail），且分块累计——不在内存整载后才拒；
        贴线（上限内 1 字节）不误伤。"""
        big = "x" * (2 * 1024 * 1024 + 10)
        resp = self._upload("a.txt", big)
        self.assertEqual(resp.status_code, 413)
        self.assertIn("过大", resp.json()["detail"])
        # 上限内贴线：2MB-1 字节，正常解析（单段 1 条）
        ok = self._upload("a.txt", "x" * (2 * 1024 * 1024 - 1))
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["count"], 1)


if __name__ == "__main__":
    unittest.main()
