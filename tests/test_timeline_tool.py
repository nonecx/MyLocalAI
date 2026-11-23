# -*- coding: utf-8 -*-
"""事件时间线工具层测试（第五波契约 §6，共 20 例）：event_timeline 接进助手工具层。

分组（组名即 TestXxx 类）：
   1. 工具注册与校验（4）：注册元数据（params/permission/cost/est_s/description）、
      query 空/超长/非字符串被拒、首尾空白剥掉 + 未声明键丢弃、200 字边界锁定；
   2. 服务缺失诚实降级（3）：ctx.services 缺失 / 缺 timeline_skills 键 /
      工厂返回不可运行实例 → ok=False + 诚实 digest，handler 永不抛；
   3. 六种状态保真（6）：completed（含零事件合法空出口）/partial/
      needs_clarification/failed/cancelled/budget_exhausted 逐状态断言
      ok/degraded/status/card 口径（card_type 恒为 timeline、引文逐字来自 data）；
   4. 卡片与 digest 逐字（3）：卡片字段逐字透传、digest 只转述卡片内容（不新增
      事件/日期/来源）、report 全文透传；
   5. 不建档（1）：dossier_id=None / not_saved / save_reason / 无 claim_answer；
   6. 死线与取消透传（3）：ctx.cancel_check 与 llm_cache.deadline_at() 进 run、
      无回合预算传 None + 四步中文阶段进 progress 事件、run 抛异常 → 诚实降级不崩。

全离线：TimelineSkills 用 SimpleNamespace stub（记录 run 实参后回预设结果），
llm/search/transport 一概不触网。注意：本任务不碰 loop.py，event_timeline 的
生产接线（_real_service 工厂 / 默认注册表 / 路由枚举）由接线任务负责，故本文件
在**自建 ToolRegistry** 上注册，不断言生产注册表现状。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import tools_timeline  # noqa: E402
from mylocalai.assistant.registry import ToolRegistry, ValidationError  # noqa: E402
from mylocalai.llm import llm_cache  # noqa: E402

QUERY = "2026年杭州余杭区自来水水质争议事件"

#: 四个中文阶段名（pipeline 契约：progress 的 component，不暴露内部技能名）。
STAGES = ("确定主题", "检索来源", "阅读抽取", "整理时间线")

#: 固定真假判定词（铁律 1）：任何出口的 digest/report 里一个都不许出现。
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")

#: digest 里逐字带出的事件条数（与 tools_timeline._EVENT_PREVIEW 同口径）
_EVENT_PREVIEW = 3


def sample_result(status: str = "completed", task_status: str = "completed",
                  empty: bool = False) -> dict:
    """一份契约形状的运行结果（字段与 skills/timeline pipeline 实际输出对齐）。

    empty=True = 合法空结果（检索完成但零事件）：events/undated 为空列表，
    range 为空串，不伪造占位。
    """
    return {
        "tool_id": "event_timeline",
        "status": status,
        "task_status": task_status,
        "data": {
            "query": QUERY,
            "state": "ok",
            "events": [] if empty else [
                {"date": "2025-03-05", "precision": "day",
                 "event": "国家市场监管总局公布抽检结果",
                 "source_url": "https://www.samr.gov.cn/b",
                 "source_title": "抽检通报",
                 "quote": "2025年3月5日，国家市场监管总局公布抽检结果：全部合格。",
                 "evidence_id": "E1"},
                {"date": "2026-11-20", "precision": "day",
                 "event": "某市发布自来水水质通报",
                 "source_url": "https://www.nhc.gov.cn/a",
                 "source_title": "某市自来水水质通报",
                 "quote": "2026年11月20日，某市发布自来水水质通报：水质检测结果合格。",
                 "evidence_id": "E2"},
            ],
            "undated": [] if empty else [
                {"event": "当地疾控中心介入调查",
                 "source_url": "https://www.nhc.gov.cn/a",
                 "source_title": "某市自来水水质通报",
                 "quote": "11月19日，当地疾控中心介入调查。"},
            ],
            "range": {"earliest": "", "latest": ""} if empty
                     else {"earliest": "2025-03-05", "latest": "2026-11-20"},
            "limits": [] if empty else ["1 条候选事件引文未在已读正文中逐字定位，已丢弃"],
            "counts": {"searches": 1, "pages": 2, "llm_calls": 3,
                       "events": 0 if empty else 2, "dropped": 0 if empty else 1},
        },
        "gaps": [] if empty else ["已读取页面但未取得带逐字引文的事件（部分页面）"],
        "errors": [] if empty else [
            {"stage": "extract", "category": "quote_not_anchored",
             "detail": "事件引文未在已读正文中逐字定位，已丢弃"}],
        "audit": {"operations": [], "searches": 1, "pages": 2, "llm_calls": 3},
        "budget": {"used": {"max_searches": 1, "max_pages": 2, "max_llm_calls": 3},
                   "limits": {"max_searches": 6, "max_pages": 8, "max_llm_calls": 10,
                              "max_events": 20, "max_quote_chars": 300}},
        "report": "事件时间线报告（只述有来源记录的事件，不判真假、不外推）\n"
                  "执行状态：%s　任务状态：%s\n一、有日期事件\n二、无日期事件\n"
                  "已执行 1 次搜索、2 次读页、3 次模型调用" % (status, task_status),
        "started_at": "2026-11-23T12:00:00+08:00",
        "finished_at": "2026-11-23T12:00:31+08:00",
    }


def stub_skills(result: Any = None, raises: Any = None,
                progress_stages: Any = None) -> Any:
    """TimelineSkills 替身：SimpleNamespace.run 记录实参后回预设结果（全离线口径）。"""
    calls: list = []

    def _run(query, *, cancel_check=None, deadline=None, on_progress=None):
        calls.append({"query": query, "cancel_check": cancel_check,
                      "deadline": deadline, "on_progress": on_progress})
        if progress_stages and on_progress is not None:
            for i, stage in enumerate(progress_stages, 1):
                on_progress(stage, i, len(progress_stages))
        if raises is not None:
            raise raises
        return result

    def _factory():
        return SimpleNamespace(run=_run)

    _factory.calls = calls
    return _factory


class RecordingEvents:
    """记录 emit 的事件流替身（ctx.events 契约：emit(name, **payload)）。"""

    def __init__(self):
        self.events: list = []

    def emit(self, name: str, **payload: Any) -> None:
        self.events.append({"event": name, **payload})


class TimelineToolTestBase(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        tools_timeline.register_all(self.registry, None)

    def ctx(self, services, cancel_check=None, events=None):
        return SimpleNamespace(sessions=None, dossiers=None, events=events,
                               cancel_check=cancel_check, services=services)

    def call(self, services, query=QUERY, cancel_check=None, events=None):
        spec = self.registry.get("event_timeline")
        assert spec is not None, "event_timeline 未注册"
        return spec["handler"](self.ctx(services, cancel_check, events), query=query)


# ===========================================================================
# 1. 工具注册与校验（4 例）
# ===========================================================================
class TestRegistrationAndValidation(TimelineToolTestBase):
    def test_registered_metadata(self):
        spec = self.registry.get("event_timeline")
        self.assertEqual(spec["params"], ["query"])
        self.assertEqual(spec["permission"], "read")      # 只读整理：不建档
        self.assertEqual(spec["cost"], "medium")
        self.assertEqual(spec["est_s"], 30.0)
        self.assertIn("事件时间线", spec["description"])
        self.assertIn("event_timeline", self.registry.names())

    def test_query_param_rejects_bad_inputs(self):
        for bad in ("", "   ", None, 123, ["x"], "长" * 201):
            with self.subTest(query=bad):
                with self.assertRaises(ValidationError):
                    self.registry.validate("event_timeline", {"query": bad})

    def test_query_param_strips_whitespace_and_drops_undeclared_keys(self):
        clean = self.registry.validate("event_timeline",
                                       {"query": f"  {QUERY}  ", "id": 7, "text": "hack"})
        self.assertEqual(clean, {"query": QUERY})          # 首尾空白剥掉、多余键丢弃

    def test_query_param_200_char_boundary(self):
        """≤200 字上限（registry.topic_param 口径；与 rules.max_input_chars 同值）。"""
        ok = self.registry.validate("event_timeline", {"query": "题" * 200})
        self.assertEqual(ok, {"query": "题" * 200})
        with self.assertRaises(ValidationError):
            self.registry.validate("event_timeline", {"query": "题" * 201})


# ===========================================================================
# 2. 服务缺失诚实降级（3 例）
# ===========================================================================
class TestServiceMissingDegrades(TimelineToolTestBase):
    def _assert_unavailable(self, out):
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIsNone(out["card"])
        self.assertIsNone(out["dossier_id"])
        self.assertEqual(out["cost"], {"requests": 0, "tokens": 0})
        self.assertEqual(out["status"], "failed")
        self.assertIn("不可用", out["digest"])
        self.assertIn("我不会替它编一条时间线", out["digest"])

    def test_services_missing_degrades(self):
        out = self.call(None)
        self._assert_unavailable(out)
        self.assertIn("装配失败", out["digest"])

    def test_services_without_timeline_skills_degrades(self):
        out = self.call({"research_skills": stub_skills(sample_result())})
        self._assert_unavailable(out)
        self.assertIn("装配失败", out["digest"])

    def test_factory_returning_non_runnable_degrades(self):
        for factory in (lambda: None, lambda: SimpleNamespace()):
            with self.subTest(factory=factory):
                out = self.call({"timeline_skills": factory})
                self._assert_unavailable(out)


# ===========================================================================
# 3. 六种状态保真（6 例）
# ===========================================================================
class TestStatusFidelity(TimelineToolTestBase):
    def _run(self, status, task_status="completed", empty=False):
        result = sample_result(status, task_status, empty=empty)
        return result, self.call({"timeline_skills": stub_skills(result)})

    def _assert_card_fidelity(self, result, out):
        """有数据时卡片口径：card_type 恒 timeline、引文逐字来自 result.data。"""
        card = out["card"]
        data = result["data"]
        self.assertEqual(card["card_type"], "timeline")
        self.assertEqual(card["status"], result["status"])
        self.assertEqual(card["task_status"], result["task_status"])
        self.assertEqual(card["as_of"], result["finished_at"])
        self.assertEqual(card["query"], QUERY)
        self.assertEqual(card["state"], "ok")
        self.assertEqual(card["state_label"], "检索与抽取正常完成")
        self.assertEqual([e["evidence_id"] for e in card["events"]],
                         [e["evidence_id"] for e in data["events"]])
        self.assertEqual([e["quote"] for e in card["events"]],
                         [e["quote"] for e in data["events"]])
        self.assertEqual([e["date"] for e in card["events"]],
                         [e["date"] for e in data["events"]])
        self.assertEqual([u["quote"] for u in card["undated"]],
                         [u["quote"] for u in data["undated"]])
        self.assertEqual(card["range"], data["range"])
        self.assertEqual(card["counts"], data["counts"])
        self.assertEqual(card["limits"], data["limits"])
        self.assertNotIn("claim_answer", out)      # 不返回 claim_answer

    def test_completed_is_ok_not_degraded(self):
        result, out = self._run("completed")
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["task_status"], "completed")
        self._assert_card_fidelity(result, out)

    def test_legal_empty_result_is_ok(self):
        """零事件的合法空出口：空列表不伪造占位，ok=True 不算降级。"""
        result, out = self._run("completed", "completed", empty=True)
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["card"]["events"], [])
        self.assertEqual(out["card"]["undated"], [])
        self.assertEqual(out["card"]["range"], {"earliest": "", "latest": ""})
        self.assertEqual(out["card"]["counts"]["events"], 0)
        self.assertIn("（本次没有带日期的事件）", out["digest"])
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, out["digest"])
            self.assertNotIn(word, out["report"])

    def test_partial_is_ok_but_degraded(self):
        result, out = self._run("partial")
        self.assertTrue(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertEqual(out["status"], "partial")
        self.assertTrue(out["card"]["degraded"])
        self.assertEqual(len(out["card"]["events"]), 2)
        self._assert_card_fidelity(result, out)

    def test_needs_clarification_is_ok_not_degraded(self):
        result, out = self._run("needs_clarification", "needs_clarification", empty=True)
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["status"], "needs_clarification")
        self.assertEqual(out["task_status"], "needs_clarification")
        self._assert_card_fidelity(result, out)

    def test_failed_is_not_ok(self):
        result, out = self._run("failed", "completed", empty=True)
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertEqual(out["status"], "failed")
        self.assertTrue(out["card"]["degraded"])

    def test_cancelled_and_budget_exhausted_keep_events(self):
        """取消/预算到顶：ok=False，但已取得的事件仍在卡片里（不整批丢弃）。"""
        for status in ("cancelled", "budget_exhausted"):
            with self.subTest(status=status):
                result, out = self._run(status)
                self.assertFalse(out["ok"])
                self.assertTrue(out["degraded"])
                self.assertEqual(out["status"], status)
                self.assertEqual(len(out["card"]["events"]), 2)
                self.assertEqual(len(out["card"]["undated"]), 1)
                self._assert_card_fidelity(result, out)


# ===========================================================================
# 4. 卡片与 digest 逐字（3 例）
# ===========================================================================
class TestCardAndDigestFidelity(TimelineToolTestBase):
    def test_card_fields_are_verbatim_from_data(self):
        result = sample_result()
        out = self.call({"timeline_skills": stub_skills(result)})
        card = out["card"]
        # 事件三件套 + 精度 + 来源逐字透传（一个字符都不改）
        for source, shown in zip(result["data"]["events"], card["events"]):
            self.assertEqual(shown, {k: source[k] for k in
                                     ("date", "precision", "event", "source_url",
                                      "source_title", "quote", "evidence_id")})
        # 无日期事件键名锁定（不带 evidence_id）
        self.assertEqual(sorted(card["undated"][0]),
                         ["event", "quote", "source_title", "source_url"])
        self.assertEqual(card["gaps"], result["gaps"])
        self.assertEqual([e["category"] for e in card["errors"]],
                         [e["category"] for e in result["errors"]])
        self.assertEqual(card["counts"],
                         {"searches": 1, "pages": 2, "llm_calls": 3,
                          "events": 2, "dropped": 1})

    def test_digest_transcribes_only_card_content(self):
        """digest 只转述卡片内容：引文/来源逐字、三计数行、无真假判定词、
        不新增任何事件、日期或来源（叙述层唯一的素材就是卡片）。"""
        result = sample_result()
        out = self.call({"timeline_skills": stub_skills(result)})
        digest = out["digest"]
        card = out["card"]
        for item in card["events"][:_EVENT_PREVIEW] + card["undated"][:_EVENT_PREVIEW]:
            self.assertIn(item["quote"], digest)
            self.assertIn(item["source_url"], digest)
            self.assertIn(item["event"], digest)
        # 时间范围、限制/缺口/错误前三、三计数
        self.assertIn("2025-03-05 至 2026-11-20", digest)
        self.assertIn("引文未在已读正文中逐字定位，已丢弃", digest)
        self.assertIn("已读取页面但未取得带逐字引文的事件", digest)
        self.assertIn("[extract] quote_not_anchored", digest)
        self.assertIn("已执行 1 次搜索、2 次读页、3 次模型调用", digest)
        self.assertIn("收录 2 条事件、丢弃 1 条", digest)
        self.assertIn("不判真假、不外推", digest)
        self.assertIn("verify_claim", digest)            # 指路核查流程
        # 不新增：卡片里没有的编号/事件不出现在 digest 里
        self.assertNotIn("E9", digest)
        self.assertNotIn("2027", digest)
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, digest)

    def test_report_full_text_passthrough(self):
        result = sample_result("partial")
        out = self.call({"timeline_skills": stub_skills(result)})
        self.assertEqual(out["report"], result["report"])
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, out["report"])
            self.assertNotIn(word, out["digest"])


# ===========================================================================
# 5. 不建档（1 例）
# ===========================================================================
class TestNotSaved(TimelineToolTestBase):
    def test_read_only_capability_never_saves(self):
        result = sample_result()
        out = self.call({"timeline_skills": stub_skills(result)})
        self.assertIsNone(out["dossier_id"])             # 只读整理：不建档
        self.assertTrue(out["not_saved"])
        self.assertEqual(out["save_reason"], "read_only_capability_no_dossier")
        self.assertNotIn("claim_answer", out)
        # 粗口径成本 = 模型调用 + 检索 + 读页（M4 真实计数由 loop 的 delta 覆盖）
        self.assertEqual(out["cost"], {"requests": 6, "tokens": 0})


# ===========================================================================
# 6. 死线与取消透传（2 例）
# ===========================================================================
class TestCancelAndDeadlinePassthrough(TimelineToolTestBase):
    def test_cancel_check_and_deadline_reach_pipeline(self):
        skills = stub_skills(sample_result())

        def cancel_check():
            return False

        with llm_cache.deadline_scope(90):
            expected = llm_cache.deadline_at()
            out = self.call({"timeline_skills": skills}, cancel_check=cancel_check)
        self.assertTrue(out["ok"])
        call = skills.calls[-1]
        self.assertEqual(call["query"], QUERY)
        self.assertIs(call["cancel_check"], cancel_check)   # 同一个可调用对象
        self.assertEqual(call["deadline"], expected)         # 绝对死线，非 None

    def test_no_turn_budget_passes_none_and_progress_events(self):
        """没有回合预算 → 传 None（流水线按 rules.total_deadline_s 自建）；
        on_progress 四步中文阶段进 progress 事件。"""
        self.assertIsNone(llm_cache.deadline_at())           # 前提：当前线程无预算
        events = RecordingEvents()
        skills = stub_skills(sample_result(), progress_stages=STAGES)
        out = self.call({"timeline_skills": skills}, events=events)
        self.assertTrue(out["ok"])
        self.assertIsNone(skills.calls[-1]["deadline"])
        progress = [e for e in events.events if e["event"] == "progress"]
        self.assertEqual([e["component"] for e in progress], list(STAGES))
        self.assertEqual([e["done"] for e in progress], [1, 2, 3, 4])
        self.assertEqual([e["total"] for e in progress], [4, 4, 4, 4])
        # events 为 None（MCP/同步调用）→ 流水线收到 on_progress=None
        skills2 = stub_skills(sample_result())
        self.call({"timeline_skills": skills2}, events=None)
        self.assertIsNone(skills2.calls[-1]["on_progress"])

    def test_run_exception_degrades_without_crash(self):
        skills = stub_skills(raises=RuntimeError("上游检索爆炸"))
        out = self.call({"timeline_skills": skills})
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIsNone(out["card"])
        self.assertEqual(out["cost"], {"requests": 0, "tokens": 0})
        self.assertIn("不可用", out["digest"])
        self.assertIn("RuntimeError", out["digest"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
