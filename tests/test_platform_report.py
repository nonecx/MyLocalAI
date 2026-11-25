# -*- coding: utf-8 -*-
"""巡检报告技能测试（report-core）：聚合、LLM 叙述、安全闸、截断、降级、坏输入。

全离线：stub llm_fn / 注入 rules / 临时 JSON 文件，不起网络、不碰真实 LLM
（默认 llm_fn 用 sys.modules 注入假 llm_client 验证 chat_json 适配参数）。
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.platform import rules as pr  # noqa: E402
from mylocalai.platform.report import PatrolReport  # noqa: E402
from mylocalai.platform.report.pipeline import (  # noqa: E402
    REPORT_LLM_STATUSES, _has_url, _overclaim_unsafe, _truth_verdict_unsafe)

#: payload 冻结键（契约）
PAYLOAD_KEYS = {"scan_id", "as_of", "scan_status", "llm_status", "title", "summary",
                "counts", "topic_stats", "highlights", "changes", "topic_notes",
                "analysis", "risks", "followups", "gaps", "errors"}

#: 一份合法的 LLM 返回（键名锁定契约）
GOOD_LLM = {
    "title": "第 7 轮巡检报告：技术领域热度攀升",
    "summary": "本轮覆盖 1 个监控话题，发现 6 条候选信息，完成深查 4 条；"
               "高危 2 条、紧急 1 条，风险集中在技术与财经领域。",
    "topic_notes": [{"query": "话题A", "note": "热度 55，环比上升 25%，候选 6 条。"}],
    "analysis": [{"point": "技术领域多条条目同期出现，可按同一事件的多源报道观察",
                  "entry_ids": [101, 102]}],
    "risks": "高危与紧急条目集中在技术领域，建议持续观察档位变化。",
    "followups": ["继续观察话题A的热度变化", "补充权威信源交叉验证"],
}

#: runner 记账（六条候选、四条已深查、一条跨轮去重、零降级）
COUNTERS = {"queries": 1, "candidates": 6, "verified": 4, "deduped": 1, "degraded": 0}

#: _run 的"未传"哨兵：区分"用默认输入"与"显式传 None/坏值"
_UNSET = object()


def _entry(**kw):
    """一条信息流条目（runner 现状形状；字段可缺）。"""
    base = {"query_id": 1, "title": "某平台传言条目", "url": "https://a.example/x",
            "domain": "a.example", "domain_tier": "media",
            "topic_domain": "technology", "topic_domain_source": "inferred_text",
            "discovered_at": "2026-11-24T10:00:00+08:00", "e_score": 50.0,
            "e_prev": 40.0, "risk_level": "suspect", "risk_reasons": ["热度 E 50/100"],
            "status": "verified", "verdict_label": "存疑待核实", "risk_score": 42.0,
            "dossier_id": None, "markers": 2}
    base.update(kw)
    return base


def _query(**kw):
    """一条监控词统计（runner 现状形状；growth 可能缺）。"""
    base = {"id": 1, "query": "话题A", "e": 55.0, "quality": "estimated",
            "candidates": 6, "domain": "technology", "domain_source": "inferred_text"}
    base.update(kw)
    return base


def _entries():
    """六条条目：notice×2、suspect×1、high×2、urgent×1（同属 query_id=1）。"""
    return [
        _entry(id=101, title="条目一", risk_level="suspect", e_score=50.0,
               verdict_label="存疑待核实", risk_score=42.0, topic_domain="technology"),
        _entry(id=102, title="条目二", risk_level="high", e_score=60.0,
               verdict_label="高风险谣言", risk_score=80.0, topic_domain="finance"),
        _entry(id=103, title="条目三", risk_level="urgent", e_score=90.0,
               verdict_label="疑似谣言", risk_score=70.0, topic_domain="technology"),
        _entry(id=104, title="条目四", risk_level="notice", e_score=10.0,
               verdict_label="", risk_score=None, topic_domain=""),
        _entry(id=105, title="条目五", risk_level="high", e_score=60.0,
               verdict_label="高风险谣言", risk_score=80.0, topic_domain="technology"),
        _entry(id=106, title="条目六", risk_level="notice", e_score=99.0,
               verdict_label="", risk_score=None, topic_domain=""),
    ]


class StubLLM:
    """记录调用的 llm_fn stub：(obj, err) 或抛异常。"""

    def __init__(self, obj=None, err="", exc=None):
        self.obj = obj
        self.err = err
        self.exc = exc
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        if self.exc is not None:
            raise self.exc
        return self.obj, self.err


def _run(entries=_UNSET, queries=_UNSET, counters=_UNSET, llm=None, **kw):
    """便捷 build：默认一套合法输入；返回 (payload, stub)。"""
    stub = llm or StubLLM(obj=GOOD_LLM, err="")
    report = PatrolReport(rules=kw.pop("rules", None), llm_fn=stub,
                          clock=kw.pop("clock", None))
    payload = report.build(
        scan_id=kw.pop("scan_id", 7),
        scan_status=kw.pop("scan_status", "completed"),
        entries=_entries() if entries is _UNSET else entries,
        queries=[_query()] if queries is _UNSET else queries,
        counters=COUNTERS if counters is _UNSET else counters,
        prev_scan=kw.pop("prev_scan", None),
        warnings=kw.pop("warnings", None),
        cancel_check=kw.pop("cancel_check", None),
    )
    return payload, stub


class TestAggregate(unittest.TestCase):
    """_aggregate：纯代码汇总（零 LLM）。"""

    def test_counts_distributions(self):
        payload, _ = _run()
        counts = payload["counts"]
        self.assertEqual(counts["queries"], 1)
        self.assertEqual(counts["candidates"], 6)
        self.assertEqual(counts["verified"], 4)
        self.assertEqual(counts["deduped"], 1)
        self.assertEqual(counts["degraded"], 0)
        self.assertEqual(counts["risk"],
                         {"notice": 2, "suspect": 1, "high": 2, "urgent": 1})
        self.assertEqual(counts["verdicts"],
                         {"存疑待核实": 1, "高风险谣言": 2, "疑似谣言": 1})
        self.assertEqual(counts["domains"], {"technology": 3, "finance": 1})

    def test_growth_from_queries(self):
        payload, _ = _run(queries=[_query(growth=12.5)])
        self.assertEqual(payload["topic_stats"][0]["growth"], 12.5)

    def test_growth_computed_from_entries(self):
        # queries 无 growth：用条目 e=50 / e_prev=40 现算 → (50-40)/40*100 = 25.0
        payload, _ = _run(queries=[_query()])
        self.assertEqual(payload["topic_stats"][0]["growth"], 25.0)

    def test_growth_e_prev_none_is_zero(self):
        payload, _ = _run(entries=[_entry(id=101, e_score=50.0, e_prev=None)])
        self.assertEqual(payload["topic_stats"][0]["growth"], 0.0)

    def test_growth_e_none_is_zero(self):
        payload, _ = _run(entries=[_entry(id=101, e_score=None, e_prev=40.0)])
        self.assertEqual(payload["topic_stats"][0]["growth"], 0.0)

    def test_max_risk_level_no_entries(self):
        payload, _ = _run(queries=[_query(id=9, query="空话题", candidates=None)])
        stat = payload["topic_stats"][0]
        self.assertEqual(stat["max_risk_level"], "notice")
        self.assertEqual(stat["top_titles"], [])
        self.assertEqual(stat["candidates"], 0)

    def test_top_titles_order_and_cap(self):
        payload, _ = _run()
        stat = payload["topic_stats"][0]
        # urgent(条目三) > high(条目二/条目五，稳定序) > suspect(条目一)
        self.assertEqual(stat["top_titles"], ["条目三", "条目二", "条目五"])
        self.assertTrue(all(len(t) <= 40 for t in stat["top_titles"]))

    def test_top_titles_long_title_truncated(self):
        payload, _ = _run(entries=[_entry(id=101, title="长" * 60,
                                          risk_level="urgent")])
        titles = payload["topic_stats"][0]["top_titles"]
        self.assertEqual(len(titles), 1)
        self.assertEqual(len(titles[0]), 40)

    def test_highlights_order_and_limit(self):
        many = _entries() + [
            _entry(id=107, title="条目七", risk_level="urgent", e_score=95.0),
            _entry(id=108, title="条目八", risk_level="urgent", e_score=96.0),
        ]
        payload, _ = _run(entries=many)
        highlights = payload["highlights"]
        self.assertEqual(len(highlights), 5)
        # urgent 三条按 E 降序 108(96)>107(95)>103(90)，再 high 60 两条（稳定序）
        self.assertEqual([h["entry_id"] for h in highlights],
                         [108, 107, 103, 102, 105])
        for item in highlights:
            self.assertEqual(set(item), {"entry_id", "title", "risk_level", "e_score",
                                         "verdict_label", "topic_domain"})

    def test_changes_four_keys(self):
        prev = {"id": 6, "candidates": 5, "verified": 3, "degraded": 2}
        payload, _ = _run(prev_scan=prev)
        # 本轮 degraded=0、上轮 2 → -2
        self.assertEqual(payload["changes"],
                         {"prev_scan_id": 6, "candidates_delta": 1,
                          "verified_delta": 1, "degraded_delta": -2})

    def test_changes_empty_without_prev(self):
        payload, _ = _run(prev_scan=None)
        self.assertEqual(payload["changes"], {})

    def test_aggregate_gaps_degraded(self):
        payload, _ = _run(counters=dict(COUNTERS, degraded=1))
        self.assertIn("本轮有 1 次降级", payload["gaps"])

    def test_aggregate_gaps_unverified(self):
        payload, _ = _run()
        self.assertIn("本轮 6 条候选中仅 4 条完成深查，其余 2 条未进入深查",
                      payload["gaps"])


class TestNarrateOk(unittest.TestCase):
    """LLM 正常组织：全字段落值 + 白名单。"""

    def test_ok_all_fields(self):
        payload, stub = _run()
        self.assertEqual(payload["llm_status"], "ok")
        self.assertEqual(payload["title"], GOOD_LLM["title"])
        self.assertEqual(payload["summary"], GOOD_LLM["summary"])
        self.assertEqual(payload["topic_notes"],
                         [{"query": "话题A",
                           "note": GOOD_LLM["topic_notes"][0]["note"]}])
        self.assertEqual(payload["analysis"],
                         [{"point": GOOD_LLM["analysis"][0]["point"],
                           "entry_ids": [101, 102]}])
        self.assertEqual(payload["risks"], GOOD_LLM["risks"])
        self.assertEqual(payload["followups"], GOOD_LLM["followups"])
        self.assertEqual(len(stub.calls), 1)
        system, user = stub.calls[0]
        self.assertIn("patrol-report", system)
        self.assertIn("entry_ids/query 只能来自传入清单", system)
        self.assertIn("监控词清单：", user)
        self.assertIn("计数：", user)
        self.assertIn("条目清单", user)
        self.assertIn("[E101]", user)
        self.assertIn("深查：存疑待核实(42)", user)

    def test_unknown_entry_id_removed_with_error(self):
        obj = dict(GOOD_LLM)
        obj["analysis"] = [{"point": "技术领域集中出现", "entry_ids": [101, 999]}]
        payload, _ = _run(llm=StubLLM(obj=obj))
        self.assertEqual(payload["llm_status"], "ok")
        self.assertEqual(payload["analysis"][0]["entry_ids"], [101])
        self.assertTrue(any(e["category"] == "unknown_entry_id"
                            for e in payload["errors"]))

    def test_unknown_query_note_dropped_with_error(self):
        obj = dict(GOOD_LLM)
        obj["topic_notes"] = [{"query": "不在清单的话题", "note": "观察"}]
        payload, _ = _run(llm=StubLLM(obj=obj))
        self.assertEqual(payload["topic_notes"], [])
        self.assertTrue(any(e["category"] == "unknown_query"
                            for e in payload["errors"]))

    def test_entry_without_id_placeholder_not_whitelisted(self):
        entries = [_entry(id=101, title="落库条目"), _entry(title="未落库条目")]
        obj = dict(GOOD_LLM)
        obj["analysis"] = [{"point": "两条条目并列观察", "entry_ids": [101, 2]}]
        stub = StubLLM(obj=obj)
        payload, _ = _run(entries=entries, llm=stub)
        user = stub.calls[0][1]
        self.assertIn("[E101]", user)
        self.assertIn("[E2(未落库)]", user)
        # 序号 2 只是占位、不进白名单 → 移除并记 error
        self.assertEqual(payload["analysis"][0]["entry_ids"], [101])
        self.assertTrue(any(e["category"] == "unknown_entry_id"
                            for e in payload["errors"]))


class TestSafetyGate(unittest.TestCase):
    """安全闸：真假判定词 / 过度断言 / URL 命中即整条丢弃并记 error。"""

    def test_point_truth_verdict_dropped(self):
        obj = dict(GOOD_LLM)
        obj["analysis"] = [{"point": "该事件是真的，多源口径一致", "entry_ids": [101]}]
        payload, _ = _run(llm=StubLLM(obj=obj))
        self.assertEqual(payload["analysis"], [])
        self.assertTrue(any(e["category"] == "unsafe_text" for e in payload["errors"]))

    def test_point_overclaim_dropped(self):
        obj = dict(GOOD_LLM)
        obj["analysis"] = [{"point": "这足以说明风险已经很大", "entry_ids": [101]}]
        payload, _ = _run(llm=StubLLM(obj=obj))
        self.assertEqual(payload["analysis"], [])
        self.assertTrue(any(e["category"] == "unsafe_text" for e in payload["errors"]))

    def test_summary_with_url_dropped(self):
        obj = dict(GOOD_LLM)
        obj["summary"] = "详见 https://evil.example/report"
        payload, _ = _run(llm=StubLLM(obj=obj))
        self.assertNotIn("https://", payload["summary"])
        self.assertTrue(payload["summary"].startswith("本轮巡检覆盖"))
        self.assertTrue(any(e["category"] == "unsafe_text" for e in payload["errors"]))
        # 其他字段照常落值，状态仍按"模型正常组织"记账
        self.assertEqual(payload["llm_status"], "ok")
        self.assertEqual(payload["title"], GOOD_LLM["title"])


class TestTruncation(unittest.TestCase):
    """截断上限：title 80 / summary 600 / note 300 / point 400 / risks 600 / followup 120。"""

    def test_title_and_summary_truncated(self):
        obj = dict(GOOD_LLM)
        obj["title"] = "标" * 200
        obj["summary"] = "摘" * 1000
        payload, _ = _run(llm=StubLLM(obj=obj))
        self.assertEqual(len(payload["title"]), 80)
        self.assertEqual(len(payload["summary"]), 600)

    def test_note_point_risks_followup_truncated(self):
        obj = dict(GOOD_LLM)
        obj["topic_notes"] = [{"query": "话题A", "note": "观" * 500}]
        obj["analysis"] = [{"point": "析" * 500, "entry_ids": [101]}]
        obj["risks"] = "险" * 900
        obj["followups"] = ["做" * 300]
        payload, _ = _run(llm=StubLLM(obj=obj))
        self.assertEqual(len(payload["topic_notes"][0]["note"]), 300)
        self.assertEqual(len(payload["analysis"][0]["point"]), 400)
        self.assertEqual(len(payload["risks"]), 600)
        self.assertEqual(payload["followups"], ["做" * 120])

    def test_followups_capped_at_five(self):
        obj = dict(GOOD_LLM)
        obj["followups"] = [f"建议{idx}" for idx in range(8)]
        payload, _ = _run(llm=StubLLM(obj=obj))
        self.assertEqual(len(payload["followups"]), 5)


class TestDegradation(unittest.TestCase):
    """降级出口：前置门零调用；LLM 失败记 error + gaps，不外抛。"""

    def test_disabled_zero_calls(self):
        payload, stub = _run(rules={"enabled": False})
        self.assertEqual(payload["llm_status"], "disabled")
        self.assertEqual(stub.calls, [])
        self.assertTrue(any("报告功能未启用" in g for g in payload["gaps"]))
        # 代码汇总不受影响
        self.assertEqual(payload["counts"]["candidates"], 6)
        self.assertTrue(payload["title"].startswith("第 7 轮"))

    def test_empty_entries_zero_calls(self):
        payload, stub = _run(entries=[])
        self.assertEqual(payload["llm_status"], "skipped_empty")
        self.assertEqual(stub.calls, [])

    def test_cancelled_zero_calls(self):
        probed = {"n": 0}

        def cancel():
            probed["n"] += 1
            return True

        payload, stub = _run(cancel_check=cancel)
        self.assertEqual(payload["llm_status"], "cancelled")
        self.assertEqual(stub.calls, [])
        self.assertGreaterEqual(probed["n"], 1)

    def test_budget_err(self):
        payload, stub = _run(llm=StubLLM(obj=None, err="llm budget exhausted"))
        self.assertEqual(payload["llm_status"], "budget_exhausted")
        self.assertEqual(len(stub.calls), 1)
        self.assertTrue(any("预算" in g for g in payload["gaps"]))
        self.assertTrue(any(e["category"] == "llm_budget" for e in payload["errors"]))

    def test_other_err_unavailable(self):
        payload, _ = _run(llm=StubLLM(obj=None, err="boom"))
        self.assertEqual(payload["llm_status"], "unavailable")
        self.assertTrue(any("模型不可用" in g for g in payload["gaps"]))

    def test_list_return_unavailable(self):
        payload, _ = _run(llm=StubLLM(obj=[1, 2, 3], err=""))
        self.assertEqual(payload["llm_status"], "unavailable")

    def test_missing_all_keys_unavailable(self):
        payload, _ = _run(llm=StubLLM(obj={"foo": "bar"}, err=""))
        self.assertEqual(payload["llm_status"], "unavailable")
        self.assertTrue(any(e["category"] == "llm_no_keys" for e in payload["errors"]))

    def test_exception_is_error_and_no_raise(self):
        payload, stub = _run(llm=StubLLM(exc=RuntimeError("boom")))
        self.assertEqual(payload["llm_status"], "error")
        self.assertEqual(len(stub.calls), 1)
        self.assertTrue(any(e["category"] == "llm_exception" for e in payload["errors"]))
        # title/summary 代码兜底
        self.assertEqual(payload["title"], "第 7 轮巡检报告")
        self.assertTrue(payload["summary"].startswith("本轮巡检覆盖"))

    def test_fallback_summary_and_counts_complete(self):
        payload, _ = _run(llm=StubLLM(obj=None, err="boom"))
        self.assertEqual(payload["title"], "第 7 轮巡检报告")
        self.assertIn("6 条候选", payload["summary"])
        self.assertIn("高危 2 条", payload["summary"])
        self.assertIn("紧急 1 条", payload["summary"])
        self.assertEqual(payload["counts"]["risk"]["urgent"], 1)
        self.assertEqual(payload["counts"]["verdicts"]["高风险谣言"], 2)

    def test_unexpected_exception_never_raises(self):
        """build 主体意外异常 → 兜底出口（合法空结果 + 如实标注）。"""
        import mylocalai.platform.report.pipeline as pipeline
        original = pipeline._counts

        def _boom(*_args, **_kw):
            raise RuntimeError("炸")

        pipeline._counts = _boom
        try:
            payload, _ = _run()
        finally:
            pipeline._counts = original
        self.assertEqual(set(payload), PAYLOAD_KEYS)
        self.assertEqual(payload["llm_status"], "error")
        self.assertTrue(any(e["category"] == "unexpected_exception"
                            for e in payload["errors"]))
        self.assertTrue(any("未预期异常" in g for g in payload["gaps"]))


class TestUserTextCaps(unittest.TestCase):
    """max_entries / max_chars_per_entry 生效（stub 记录 user 文本）。"""

    def test_max_entries_and_chars(self):
        stub = StubLLM(obj=GOOD_LLM)
        payload, _ = _run(rules={"max_entries": 2, "max_chars_per_entry": 60}, llm=stub)
        self.assertEqual(payload["llm_status"], "ok")
        user = stub.calls[0][1]
        entry_lines = [ln for ln in user.splitlines() if ln.startswith("[E")]
        self.assertEqual(len(entry_lines), 2)
        for line in entry_lines:
            self.assertLessEqual(len(line), 60)
        self.assertIn("（仅展示前 2 条，共 6 条）", user)


class TestBadInputs(unittest.TestCase):
    """坏输入不外抛、有合法形状、如实记 error。"""

    def test_entries_not_list(self):
        payload, _ = _run(entries="not-a-list")
        self.assertEqual(set(payload), PAYLOAD_KEYS)
        self.assertEqual(payload["llm_status"], "skipped_empty")
        self.assertTrue(any(e["category"] == "bad_entries" for e in payload["errors"]))

    def test_counters_none(self):
        # None = "没带记账"（合法缺席，同 prev_scan=None 口径）：按零值处理，
        # queries 兜底 len(queries)，不外抛、形状合法
        payload, _ = _run(counters=None)
        self.assertEqual(set(payload), PAYLOAD_KEYS)
        self.assertEqual(payload["counts"]["queries"], 1)
        self.assertEqual(payload["counts"]["candidates"], 0)
        self.assertEqual(payload["counts"]["risk"]["notice"], 2)

    def test_queries_not_list(self):
        payload, _ = _run(queries="bad")
        self.assertEqual(payload["topic_stats"], [])
        self.assertTrue(any(e["category"] == "bad_queries" for e in payload["errors"]))

    def test_entry_missing_fields(self):
        payload, _ = _run(entries=[{"query_id": 1}, {"query_id": 1, "title": "只有标题"}])
        self.assertEqual(set(payload), PAYLOAD_KEYS)
        self.assertEqual(payload["counts"]["risk"]["notice"], 2)
        self.assertIsNone(payload["highlights"][0]["entry_id"])
        self.assertEqual(payload["topic_stats"][0]["top_titles"], ["只有标题"])

    def test_bad_scan_id(self):
        # LLM 也降级，才能看到代码兜底标题里的 scan_id
        payload, _ = _run(scan_id="abc", llm=StubLLM(obj=None, err="boom"))
        self.assertEqual(payload["scan_id"], 0)
        self.assertTrue(any(e["category"] == "bad_scan_id" for e in payload["errors"]))
        self.assertEqual(payload["title"], "第 0 轮巡检报告")

    def test_bad_prev_scan(self):
        payload, _ = _run(prev_scan="上一轮")
        self.assertEqual(payload["changes"], {})
        self.assertTrue(any(e["category"] == "bad_prev_scan" for e in payload["errors"]))

    def test_warnings_merged_into_gaps(self):
        payload, _ = _run(warnings=["源 A 降级", "", None])
        self.assertIn("巡检告警：源 A 降级", payload["gaps"])


class TestPayloadShape(unittest.TestCase):
    """payload 形状冻结 + llm_status 封闭集 + 时钟注入。"""

    def test_shape_and_status_closed_set(self):
        payload, _ = _run()
        self.assertEqual(set(payload), PAYLOAD_KEYS)
        self.assertIn(payload["llm_status"], REPORT_LLM_STATUSES)
        self.assertEqual(set(payload["counts"]),
                         {"queries", "candidates", "verified", "deduped", "degraded",
                          "risk", "verdicts", "domains"})
        self.assertEqual(set(payload["counts"]["risk"]),
                         {"notice", "suspect", "high", "urgent"})
        self.assertEqual(set(payload["topic_stats"][0]),
                         {"query", "e", "growth", "candidates", "max_risk_level",
                          "top_titles"})
        # 默认不传 prev_scan → changes 为空对象（首轮不假装有对比）
        self.assertEqual(payload["changes"], {})
        for item in payload["errors"]:
            self.assertEqual(set(item), {"stage", "category", "detail"})
            self.assertEqual(item["stage"], "report")

    def test_statuses_match_store(self):
        from mylocalai.platform.store import REPORT_LLM_STATUSES as store_statuses
        self.assertEqual(REPORT_LLM_STATUSES, store_statuses)

    def test_clock_injected(self):
        payload, _ = _run(clock=lambda: "2026-11-24T12:00:00+08:00")
        self.assertEqual(payload["as_of"], "2026-11-24T12:00:00+08:00")

    def test_scan_status_passthrough(self):
        payload, _ = _run(scan_status="partial")
        self.assertEqual(payload["scan_status"], "partial")


class TestDefaultLLMFn(unittest.TestCase):
    """缺省 llm_fn：chat_json 适配（注入假模块，不起网络）。"""

    def test_chat_json_adapter(self):
        fake = types.ModuleType("mylocalai.llm.llm_client")
        seen = {}

        def fake_chat_json(system, user, **kw):
            seen["kw"] = kw
            return {"title": "模型标题", "summary": "模型摘要"}, ""

        fake.chat_json = fake_chat_json
        original = sys.modules.get("mylocalai.llm.llm_client")
        sys.modules["mylocalai.llm.llm_client"] = fake
        try:
            report = PatrolReport()
            payload = report.build(scan_id=7, scan_status="completed",
                                   entries=_entries(), queries=[_query()],
                                   counters=COUNTERS)
        finally:
            if original is None:
                sys.modules.pop("mylocalai.llm.llm_client", None)
            else:
                sys.modules["mylocalai.llm.llm_client"] = original
        self.assertEqual(payload["llm_status"], "ok")
        self.assertEqual(payload["title"], "模型标题")
        self.assertEqual(seen["kw"]["temperature"], 0)
        self.assertEqual(seen["kw"]["max_tokens"], 1200)
        self.assertEqual(seen["kw"]["namespace"], "platform_report")

    def test_import_failure_returns_unavailable(self):
        """llm_client 模块缺失 → (None, err) → unavailable，不外抛。"""
        fake = types.ModuleType("mylocalai.llm.llm_client")

        def _missing(_system, _user, **_kw):
            raise ImportError("no module")

        fake.chat_json = _missing
        original = sys.modules.get("mylocalai.llm.llm_client")
        sys.modules["mylocalai.llm.llm_client"] = fake
        try:
            payload = PatrolReport().build(scan_id=7, scan_status="completed",
                                           entries=_entries(), queries=[_query()],
                                           counters=COUNTERS)
        finally:
            if original is None:
                sys.modules.pop("mylocalai.llm.llm_client", None)
            else:
                sys.modules["mylocalai.llm.llm_client"] = original
        self.assertEqual(payload["llm_status"], "unavailable")


class TestReportRules(unittest.TestCase):
    """rules.py 的 report 段规整（与 platform_rules.json 同口径）。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, data) -> Path:
        path = Path(self.tmp.name) / "rules.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_default_report_section(self):
        self.assertEqual(pr.DEFAULT_RULES["report"],
                         {"enabled": True, "max_llm_calls": 1, "max_entries": 20,
                          "max_chars_per_entry": 300, "deadline_s": 60,
                          "max_tokens": 1200})

    def test_package_file_report_valid(self):
        loaded, warnings = pr.load_rules()
        self.assertEqual(warnings, [])
        self.assertEqual(loaded["report"], pr.DEFAULT_RULES["report"])

    def test_missing_report_no_warning(self):
        path = self._write({"enabled": True,
                            "screen": {"heat_screen_e": 40},
                            "deep_verify": {"max_per_scan": 3, "deadline_s": 120},
                            "risk": {"weights": {"heat": 0.4, "growth": 0.15,
                                                 "breadth": 0.2, "rumor": 0.25},
                                     "breadth_full_sources": 10,
                                     "levels": {"suspect": 35, "high": 60,
                                                "urgent": 80},
                                     "floors": []},
                            "watch_queries": [], "rss_sources": []})
        loaded, warnings = pr.load_rules(str(path))
        self.assertEqual(loaded["report"], pr.DEFAULT_RULES["report"])
        self.assertEqual(warnings, [])

    def test_report_not_object(self):
        path = self._write({"report": [1, 2]})
        loaded, warnings = pr.load_rules(str(path))
        self.assertEqual(loaded["report"], pr.DEFAULT_RULES["report"])
        self.assertTrue(any("report" in w for w in warnings))

    def test_report_bad_values(self):
        path = self._write({"report": {"enabled": "yes", "max_llm_calls": 99,
                                       "max_entries": 0,
                                       "max_chars_per_entry": 10,
                                       "deadline_s": 5, "max_tokens": 100}})
        loaded, warnings = pr.load_rules(str(path))
        self.assertEqual(loaded["report"],
                         {"enabled": True,           # 坏类型退默认
                          "max_llm_calls": 5,        # 99 夹取到 5
                          "max_entries": 1,          # 0 夹取到 1
                          "max_chars_per_entry": 50,  # 10 夹取到 50
                          "deadline_s": 10,          # 5 夹取到 10
                          "max_tokens": 200})        # 100 夹取到 200
        self.assertTrue(any("report.enabled" in w for w in warnings))
        self.assertTrue(any("report.max_llm_calls" in w for w in warnings))

    def test_report_valid_values_pass_through(self):
        path = self._write({"report": {"enabled": False, "max_llm_calls": 3,
                                       "max_entries": 5, "max_chars_per_entry": 120,
                                       "deadline_s": 30, "max_tokens": 800}})
        loaded, warnings = pr.load_rules(str(path))
        self.assertEqual(loaded["report"],
                         {"enabled": False, "max_llm_calls": 3, "max_entries": 5,
                          "max_chars_per_entry": 120, "deadline_s": 30,
                          "max_tokens": 800})
        self.assertFalse(any("report" in w for w in warnings))

    def test_patrol_report_coerces_rules(self):
        report = PatrolReport(rules={"max_llm_calls": 99, "max_entries": 0,
                                     "max_chars_per_entry": 10, "enabled": "x"})
        self.assertEqual(report.rules["max_llm_calls"], 5)
        self.assertEqual(report.rules["max_entries"], 1)
        self.assertEqual(report.rules["max_chars_per_entry"], 50)
        self.assertTrue(report.rules["enabled"])   # 坏类型退默认 True


class TestValidators(unittest.TestCase):
    """代码校验器纯函数（与 assistant/skills/research/pipeline.py 同口径）。"""

    def test_truth_verdict_words(self):
        for word in ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实"):
            self.assertTrue(_truth_verdict_unsafe(f"结论{word}"), word)

    def test_overclaim_words(self):
        for word in ("证明", "足以说明", "无疑是", "铁证", "实锤", "盖棺定论"):
            self.assertTrue(_overclaim_unsafe(f"这{word}没问题"), word)

    def test_url(self):
        self.assertTrue(_has_url("见 https://a.example"))
        self.assertTrue(_has_url("见 http://a.example"))
        self.assertFalse(_has_url("没有链接"))

    def test_clean_text_passes(self):
        self.assertFalse(_truth_verdict_unsafe("热度环比上升"))
        self.assertFalse(_overclaim_unsafe("热度环比上升"))
        self.assertFalse(_has_url("热度环比上升"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
