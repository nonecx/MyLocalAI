# -*- coding: utf-8 -*-
"""资料收集工具层测试（第三波契约 §6.1/§6.5，恰好 22 例）：collect_research 接进助手工具层。

分组（契约 §6.5，共 22 例，组名即 TestXxx 类）：
   1. 工具注册与校验（5）：默认注册表/元数据、topic 空/超长/非字符串被拒、多余键丢弃、
      resolve_action 结构化通道放行只读工具而拒绝写工具（对照）；
   2. 取消与绝对死线透传（4）：ctx.cancel_check 与 llm_cache.deadline_at() 进
      skills.run、死线随回合范围变化、无预算传 None、run 抛异常 → ok=False 不崩；
   3. 六种状态保真（6）：completed/partial/needs_clarification/failed/cancelled/
      budget_exhausted 逐状态断言 ok/degraded/status/card 口径（card_type 恒为
      research、引文逐字来自 result.data.evidence）；无可引用资料的合法空出口；
   4. 进度事件与服务工厂（3）：on_progress 四步中文阶段进 progress 事件；
      _real_service("research_skills") 的 rules/llm 配置透传；consent 未授权时
      不碰 search/fetch（fail-closed）；
   5. 路由与 allowlist（2）：路由正例、不在 allowlist 降级 clarify；
   6. 报告与引文逐字保留（2）：digest 引文/计数逐字、report 全文 + 不建档 + 无真假
      判定词 + 不返回 claim_answer。

全离线：ResearchSkills 用 SimpleNamespace stub（记录 run 实参后回预设结果），
llm/search/transport 一概不触网（服务工厂测试用 sys.modules 桩 + mock.patch）。
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mylocalai.assistant as assistant_pkg  # noqa: E402
from mylocalai.assistant import router as router_mod  # noqa: E402
from mylocalai.assistant.loop import _CARD_TYPE, AssistantLoop, _real_service  # noqa: E402
from mylocalai.assistant.registry import ToolNotAllowed, ValidationError  # noqa: E402
from mylocalai.llm import llm_cache  # noqa: E402

ASSISTANT_DIR = Path(assistant_pkg.__file__).resolve().parent

TOPIC = "城市绿地降温效应"

#: 四个中文阶段名（pipeline 契约 §4.1：progress 的 component，不暴露内部技能名）。
STAGES = ("确定范围", "检索资料", "阅读摘录", "整理简报")

#: 真假判定词（铁律 1）：任何出口的 digest/report 里一个都不许出现。
_VERDICT_WORDS = ("是真的", "假的", "属实", "谣言为真")


def load_json(name: str) -> dict:
    return json.loads((ASSISTANT_DIR / name).read_text(encoding="utf-8"))


def sample_result(status: str = "completed", task_status: str = "completed",
                   empty: bool = False) -> dict:
    """一份契约 §4.3 形状的运行结果（字段与 skills/research pipeline 实际输出对齐）。

    empty=True = 合法空结果（检索完成但未找到可引用资料）：findings/leads/evidence
    为空列表，不伪造占位。
    """
    return {
        "tool_id": "collect_research",
        "status": status,
        "task_status": task_status,
        "data": {
            "topic": TOPIC,
            "scope": {"question": "城市绿地能降温多少", "time_window": "近5年",
                      "region": "中国", "basis": "绿地降温"},
            "sub_questions": [] if empty else [
                {"sq_id": "SQ1", "text": "绿地覆盖率与地表温度的关系", "status": "completed",
                 "evidence_count": 1},
                {"sq_id": "SQ2", "text": "不同城市规模的差异", "status": "completed",
                 "evidence_count": 1},
            ],
            "key_findings": [] if empty else [
                {"finding_id": "F1", "sq_ids": ["SQ1"],
                 "statement": "建成区绿地覆盖率越高，夏季地表均温越低。",
                 "evidence_ids": ["E1"], "support_level": "authoritative"},
                {"finding_id": "F2", "sq_ids": ["SQ2"],
                 "statement": "大型城市的降温幅度普遍大于中小城市。",
                 "evidence_ids": ["E2"], "support_level": "reliable"},
            ],
            "leads": [] if empty else [
                {"lead_id": "L1", "statement": "某论坛整理了多份未署名观测数据。",
                 "evidence_ids": ["E2"], "reason": "来源未核验，只作线索"},
            ],
            "evidence": [] if empty else [
                {"evidence_id": "E1", "sq_ids": ["SQ1"], "source_id": "SRC-gov-example",
                 "name": "某市生态环境局", "domain": "gov.example", "tier": "官方权威",
                 "source_kind": "government", "authority_class": "official",
                 "verification": "catalog_anchor", "title": "城市绿地降温效应观测通报",
                 "url": "https://gov.example/notice/1",
                 "quote": "连续三年观测显示，建成区绿地覆盖率每提高 5 个百分点，"
                          "夏季地表均温下降约 0.8 摄氏度。",
                 "quote_span": {"start": 0, "end": 20}, "published_at": "2024-06-01",
                 "fetched_at": "2026-11-20T10:00:00+08:00", "role": "fact",
                 "limitations": []},
                {"evidence_id": "E2", "sq_ids": ["SQ2"], "source_id": "CAND-bbs-example",
                 "name": "某论坛", "domain": "bbs.example", "tier": "未登记",
                 "source_kind": "forum", "authority_class": "unverified",
                 "verification": "unverified", "title": "网友观测汇总帖",
                 "url": "https://bbs.example/t/1",
                 "quote": "多个城市网友贴出的体感温度对比，普遍反映绿地片区更凉快。",
                 "quote_span": {"start": 0, "end": 18}, "published_at": "",
                 "fetched_at": "2026-11-20T10:02:00+08:00", "role": "context",
                 "limitations": ["未署名观测，无法定位原始出处"]},
            ],
            "sources": [
                {"source_id": "SRC-gov-example", "name": "某市生态环境局",
                 "domain": "gov.example", "tier": "官方权威",
                 "authority_class": "official", "verification": "catalog_anchor",
                 "verification_basis": "目录锚点", "verified": True},
                {"source_id": "CAND-bbs-example", "name": "某论坛", "domain": "bbs.example",
                 "tier": "未登记", "authority_class": "unverified",
                 "verification": "unverified", "verification_basis": "", "verified": False},
            ],
            "timeline": [] if empty else [
                {"date": "2024", "event": "某市发布绿地降温观测通报",
                 "evidence_ids": ["E1"]}],
            "conflicts": [] if empty else [
                {"summary": "观测口径不同：官方为地表均温，网友贴为体感温度",
                 "evidence_ids": ["E1", "E2"]}],
            "analysis": {} if empty else {
                "text": "两类资料的口径不同，降温幅度的可比性有限。",
                "evidence_ids": ["E1", "E2"], "label": "基于资料的整理/推断"},
        },
        "gaps": ["未找到可引用资料"] if empty else
                ["尚未找到第二个已核验信源", "中小城市的观测数据不足"],
        "errors": [] if empty else [
            {"stage": "search", "category": "engine_unavailable",
             "detail": "某检索后端连续不可用，本轮少了一路结果"}],
        "audit": {"operations": [], "searches": 4, "pages": 3, "llm_calls": 6},
        "budget": {"used": {"max_searches": 4, "max_pages": 3, "max_llm_calls": 6},
                   "limits": {"max_searches": 10, "max_pages": 10, "max_llm_calls": 16}},
        "report": "研究简报（只述资料，不判真假）\n执行状态：%s　任务状态：%s\n"
                  "一、范围\n二、子问题\n三、关键发现\n四、证据\n"
                  "已执行 4 次搜索、3 次读页、6 次模型调用" % (status, task_status),
        "started_at": "2026-11-20T09:59:30+08:00",
        "finished_at": "2026-11-20T10:03:30+08:00",
    }


def stub_skills(result: Any = None, raises: Any = None,
                progress_stages: Any = None) -> Any:
    """ResearchSkills 替身：SimpleNamespace.run 记录实参后回预设结果（全离线口径）。

    progress_stages 非空时，run 内按序回调 on_progress(stage, done, total)——模拟
    流水线的四步中文阶段（events 为 None 时应收到 on_progress=None）。
    """
    calls: list = []

    def _run(topic, *, cancel_check=None, deadline=None, on_progress=None):
        calls.append({"topic": topic, "cancel_check": cancel_check,
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


def stub_module(name: str, **attrs: Any) -> types.ModuleType:
    """sys.modules 桩模块（服务工厂的惰性 import 目标；测试后由 patch.dict 还原）。"""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def record_transport(sink: dict):
    """default_transport 桩：记录调用参数与 fetch 调用次数。

    锁两条口径：(1) 取页上限必须来自 rules.json（不拿进程内单例的硬编码默认值）；
    (2) 未授权时 transport.fetch 一次都不该被调用。
    """
    def _default_transport(**kwargs: Any) -> Any:
        sink["_transport_kwargs"] = dict(kwargs)

        def _fetch(*_a: Any, **_k: Any) -> Dict[str, Any]:
            sink["_fetch_calls"] = sink.get("_fetch_calls", 0) + 1
            return {"status": "ok"}
        return SimpleNamespace(fetch=_fetch)
    return _default_transport


class RecordingEvents:
    """记录 emit 的事件流替身（ctx.events 契约：emit(name, **payload)）。"""

    def __init__(self):
        self.events: list = []

    def emit(self, name: str, **payload: Any) -> None:
        self.events.append({"event": name, **payload})


class ResearchToolTestBase(unittest.TestCase):
    def setUp(self):
        self.registry = AssistantLoop().registry   # 真实注册表（校验器与生产同源）
        self.rules = load_json("assistant_rules.json")
        self.prompts = load_json("prompts.json")

    def ctx(self, services, cancel_check=None, events=None):
        return SimpleNamespace(sessions=None, dossiers=None, events=events,
                               cancel_check=cancel_check, services=services)

    def call(self, services, topic=TOPIC, cancel_check=None, events=None):
        spec = self.registry.get("collect_research")
        assert spec is not None, "collect_research 未注册"
        return spec["handler"](self.ctx(services, cancel_check, events), topic=topic)


# ===========================================================================
# 1. 工具注册与校验（5 例）
# ===========================================================================
class TestRegistrationAndValidation(ResearchToolTestBase):
    def test_registered_in_default_registry_with_metadata(self):
        """进默认注册表：params/permission/cost/est_s 元数据 + 四处接线同源。"""
        spec = self.registry.get("collect_research")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["params"], ["topic"])
        self.assertEqual(spec["permission"], "read")   # 只读调研：不建档
        self.assertEqual(spec["cost"], "heavy")
        self.assertEqual(spec["est_s"], 60.0)
        self.assertIn("资料收集", spec["description"])
        self.assertIn("collect_research", self.registry.names())
        # 四处接线同源：allowlist / 路由枚举 / 路由提示词 + 卡片类型
        self.assertIn("collect_research", self.rules["tool_allowlist"])
        self.assertIn("collect_research", router_mod.TOOL_ACTIONS)
        self.assertIn("collect_research", self.prompts["router_system"])
        self.assertEqual(_CARD_TYPE.get("collect_research"), "research")

    def test_topic_param_validation_rejects_bad_inputs(self):
        """topic 空/超长/非字符串在 registry 校验层就拒（路由层据此降级 clarify）。"""
        for bad in ("", "   ", None, 123, ["x"], "题" * 501):
            with self.subTest(topic=bad):
                with self.assertRaises(ValidationError):
                    self.registry.validate("collect_research", {"topic": bad})
        clean = self.registry.validate("collect_research", {"topic": f"  {TOPIC}  "})
        self.assertEqual(clean, {"topic": TOPIC})   # 首尾空白被剥掉

    def test_validate_drops_undeclared_keys(self):
        """未声明的键一律丢弃（用户消息/路由 JSON 里的多余字段不进工具）。"""
        clean = self.registry.validate(
            "collect_research",
            {"topic": TOPIC, "id": 7, "text": "hack", "reply": "hack"})
        self.assertEqual(clean, {"topic": TOPIC})

    def test_resolve_action_allows_read_only_tool(self):
        """结构化 action 通道放行 collect_research（只读；web 的精确输入通道）。"""
        loop = AssistantLoop(rules=self.rules, prompts=self.prompts)
        plan = loop.resolve_action({"name": "collect_research",
                                    "args": {"topic": TOPIC, "junk": 1}})
        self.assertEqual(plan["kind"], "tools")
        self.assertEqual(plan["steps"],
                         [{"action": "collect_research", "args": {"topic": TOPIC},
                           "step_id": "collect_research:1", "extra_step_ids": []}])

    def test_resolve_action_rejects_write_tools(self):
        """对照：写工具（verify_claim）走结构化通道一律 ToolNotAllowed——写档案必须走
        自然语言路由，action 通道不提供绕过。"""
        loop = AssistantLoop(rules=self.rules, prompts=self.prompts)
        with self.assertRaises(ToolNotAllowed):
            loop.resolve_action({"name": "verify_claim", "args": {"text": TOPIC}})


# ===========================================================================
# 2. 取消与绝对死线透传（4 例）
# ===========================================================================
class TestCancelAndDeadlinePassthrough(ResearchToolTestBase):
    def test_cancel_check_and_deadline_reach_pipeline(self):
        """ctx.cancel_check 与 llm_cache.deadline_at() 原样进 skills.run（§6.1）。"""
        skills = stub_skills(sample_result())

        def cancel_check():
            return False

        with llm_cache.deadline_scope(90):
            expected = llm_cache.deadline_at()
            out = self.call({"research_skills": skills}, cancel_check=cancel_check)
        self.assertTrue(out["ok"])
        call = skills.calls[-1]
        self.assertEqual(call["topic"], TOPIC)
        self.assertIs(call["cancel_check"], cancel_check)   # 同一个可调用对象
        self.assertEqual(call["deadline"], expected)         # 绝对死线，非 None

    def test_deadline_tracks_turn_scope(self):
        """回合预算变化 → 透传死线随范围变化（嵌套取更紧者）。"""
        skills = stub_skills(sample_result())
        with llm_cache.deadline_scope(120):
            wide = llm_cache.deadline_at()
            self.call({"research_skills": skills})
            self.assertEqual(skills.calls[-1]["deadline"], wide)
        with llm_cache.deadline_scope(5):
            tight = llm_cache.deadline_at()
            self.call({"research_skills": skills})
            self.assertEqual(skills.calls[-1]["deadline"], tight)
        self.assertLess(tight, wide)

    def test_no_turn_budget_passes_none_deadline(self):
        """没有回合预算 → 传 None（流水线按 rules.total_deadline_s 自建）。"""
        self.assertIsNone(llm_cache.deadline_at())   # 前提：当前线程无预算
        skills = stub_skills(sample_result())
        self.call({"research_skills": skills})
        self.assertIsNone(skills.calls[-1]["deadline"])

    def test_run_exception_degrades_without_crash(self):
        """run 抛异常 → ok=False + 诚实 digest，handler 永不抛（二道闸）。"""
        skills = stub_skills(raises=RuntimeError("上游检索爆炸"))
        out = self.call({"research_skills": skills})
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIsNone(out["card"])
        self.assertIsNone(out["dossier_id"])
        self.assertEqual(out["cost"], {"requests": 0, "tokens": 0})
        self.assertIn("不可用", out["digest"])
        self.assertIn("RuntimeError", out["digest"])


# ===========================================================================
# 3. 六种状态保真（6 例）
# ===========================================================================
class TestStatusFidelity(ResearchToolTestBase):
    def _run(self, status, task_status="completed", empty=False):
        result = sample_result(status, task_status, empty=empty)
        return result, self.call({"research_skills": stub_skills(result)})

    def _assert_card_fidelity(self, result, out):
        """有数据时卡片口径：card_type 恒 research、引文逐字来自 result.data.evidence。"""
        card = out["card"]
        self.assertEqual(card["card_type"], "research")
        self.assertEqual(card["status"], result["status"])
        self.assertEqual(card["task_status"], result["task_status"])
        self.assertEqual(card["as_of"], result["finished_at"])
        self.assertEqual([e["evidence_id"] for e in card["evidence"]],
                         [e["evidence_id"] for e in result["data"]["evidence"]])
        self.assertEqual([e["quote"] for e in card["evidence"]],
                         [e["quote"] for e in result["data"]["evidence"]])
        self.assertEqual(card["counts"], {"sub_questions": 2, "searches": 4, "pages": 3,
                                          "sources": 2, "evidence": 2, "llm_calls": 6})
        self.assertEqual([s["source_id"] for s in card["sources"]["verified"]],
                         ["SRC-gov-example"])
        self.assertEqual([s["source_id"] for s in card["sources"]["unverified"]],
                         ["CAND-bbs-example"])
        self.assertEqual(card["topic"], TOPIC)
        self.assertEqual(card["scope"]["question"], "城市绿地能降温多少")
        self.assertEqual([q["sq_id"] for q in card["sub_questions"]], ["SQ1", "SQ2"])
        self.assertEqual([f["finding_id"] for f in card["key_findings"]], ["F1", "F2"])
        self.assertEqual([l["lead_id"] for l in card["leads"]], ["L1"])
        self.assertEqual(card["analysis"]["label"], "基于资料的整理/推断")
        self.assertNotIn("claim_answer", out)   # 不返回 claim_answer（§6.1）

    def test_completed_and_legal_empty_are_ok(self):
        """completed 与"检索完成但未找到可引用资料"的合法空出口都 ok=True；
        空结果是空列表，不伪造占位；且不出现真假判定词。"""
        result, out = self._run("completed", "completed")
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["task_status"], "completed")
        self._assert_card_fidelity(result, out)

        empty, out2 = self._run("completed", "completed", empty=True)
        self.assertTrue(out2["ok"])
        self.assertFalse(out2["degraded"])
        self.assertEqual(out2["card"]["key_findings"], [])
        self.assertEqual(out2["card"]["evidence"], [])
        self.assertEqual(out2["card"]["counts"]["evidence"], 0)
        self.assertIn("未找到可引用资料", out2["card"]["gaps"])
        self.assertIn("（本次没有可引用的关键发现）", out2["digest"])
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, out2["digest"])
            self.assertNotIn(word, out2["report"])

    def test_partial_status_fidelity(self):
        """partial：ok=True 但 degraded=True（有产物但有缺口/错误，如实标注）。"""
        result, out = self._run("partial")
        self.assertTrue(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertEqual(out["status"], "partial")
        self.assertTrue(out["card"]["degraded"])
        self.assertEqual(len(out["card"]["evidence"]), 2)
        self._assert_card_fidelity(result, out)

    def test_needs_clarification_status_fidelity(self):
        """needs_clarification：合法出口（主题过宽/预检失败），不算降级。"""
        result, out = self._run("needs_clarification", "needs_clarification")
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["status"], "needs_clarification")
        self.assertEqual(out["task_status"], "needs_clarification")
        self._assert_card_fidelity(result, out)

    def test_failed_status_fidelity(self):
        """failed：ok=False + degraded=True（连阶段 1 都进不去）。"""
        result, out = self._run("failed")
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertEqual(out["status"], "failed")
        self.assertTrue(out["card"]["degraded"])
        self._assert_card_fidelity(result, out)

    def test_cancelled_status_fidelity(self):
        """cancelled：ok=False，但已取得的摘录/证据仍在卡片里（取消不整批丢弃）。"""
        result, out = self._run("cancelled")
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(len(out["card"]["evidence"]), 2)
        self._assert_card_fidelity(result, out)

    def test_budget_exhausted_status_fidelity(self):
        """budget_exhausted：ok=False，同样保留已取得证据。"""
        result, out = self._run("budget_exhausted")
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertEqual(out["status"], "budget_exhausted")
        self.assertEqual(len(out["card"]["evidence"]), 2)
        self._assert_card_fidelity(result, out)


# ===========================================================================
# 4. 进度事件与服务工厂（3 例）
# ===========================================================================
class TestProgressAndServiceFactory(ResearchToolTestBase):
    def test_progress_events_use_chinese_stage_names(self):
        """on_progress 四步中文阶段进 progress 事件；events 缺失时传 None。"""
        events = RecordingEvents()
        skills = stub_skills(sample_result(), progress_stages=STAGES)
        out = self.call({"research_skills": skills}, events=events)
        self.assertTrue(out["ok"])
        progress = [e for e in events.events if e["event"] == "progress"]
        self.assertEqual([e["component"] for e in progress], list(STAGES))
        self.assertEqual([e["done"] for e in progress], [1, 2, 3, 4])
        self.assertEqual([e["total"] for e in progress], [4, 4, 4, 4])
        # events 为 None（MCP/同步调用）→ 流水线收到 on_progress=None
        self.assertIsNotNone(skills.calls[-1]["on_progress"])
        skills2 = stub_skills(sample_result())
        self.call({"research_skills": skills2}, events=None)
        self.assertIsNone(skills2.calls[-1]["on_progress"])

    def test_service_factory_wires_rules_and_llm_config(self):
        """_real_service("research_skills")：rules 走 research 段、llm 用
        temperature=0/max_tokens=900/assistant_research，SearchResult 转 dict。"""
        built: dict = {}

        class StubResearchSkills:
            def __init__(self, **kw):
                built.update(kw)

        rules = {"research_model": {"temperature": 0, "max_tokens": 900,
                                    "cache_namespace": "assistant_research"}}
        search_all = mock.Mock(return_value={
            "results": [SimpleNamespace(to_dict=lambda: {"url": "u", "title": "t",
                                                        "snippet": "s"})],
            "engines": {"e1": {"ok": True}}, "blocked": False, "error": ""})
        chat_json = mock.Mock(return_value=({"ok": True}, ""))
        prefs = SimpleNamespace(external_allowed=lambda kind: True)
        with mock.patch.dict(sys.modules, {
            "mylocalai.assistant.skills.research": stub_module(
                "mylocalai.assistant.skills.research", ResearchSkills=StubResearchSkills),
            "mylocalai.assistant.skills.research.pipeline": stub_module(
                "mylocalai.assistant.skills.research.pipeline",
                load_rules=lambda: {"total_deadline_s": 90, "max_page_bytes": 1500000,
                                    "max_text_chars": 20000, "io_timeout_s": 10.0,
                                    "max_redirect_hops": 3}),
            "mylocalai.assistant.skills.transport": stub_module(
                "mylocalai.assistant.skills.transport",
                default_transport=record_transport(built)),
        }), mock.patch("mylocalai.core.privacy.load_preferences", return_value=prefs), \
                mock.patch("mylocalai.llm.llm_client.chat_json", chat_json), \
                mock.patch("mylocalai.retrieval.search.search_all", search_all):
            skills = _real_service("research_skills", rules, None)()
            self.assertIsInstance(skills, StubResearchSkills)
            self.assertEqual(built["rules"], {"total_deadline_s": 90, "max_page_bytes": 1500000,
                                              "max_text_chars": 20000, "io_timeout_s": 10.0,
                                              "max_redirect_hops": 3})
            # 取页上限必须来自 rules.json（否则运维改配置对真实取页无效）
            self.assertEqual(built["_transport_kwargs"], {"max_bytes": 1500000,
                                                         "max_text_chars": 20000,
                                                         "timeout_s": 10.0,
                                                         "max_hops": 3})
            self.assertTrue(callable(built["search_fn"]))
            self.assertTrue(callable(built["fetch_fn"]))
            # search_fn：SearchResult 形状转 dict（url/title/snippet）
            rows = built["search_fn"]("绿地 降温", k=6)
            self.assertEqual(rows["results"], [{"url": "u", "title": "t", "snippet": "s"}])
            self.assertFalse(rows["degraded"])
            search_all.assert_called_once_with("绿地 降温", k=6)
            # llm_fn：research_model 配置进 chat_json
            built["llm_fn"]("sys", "user")
            self.assertEqual(chat_json.call_args.args, ("sys", "user"))
            self.assertEqual(chat_json.call_args.kwargs,
                             {"temperature": 0.0, "max_tokens": 900,
                              "namespace": "assistant_research"})

    def test_service_factory_consent_denied_blocks_external_io(self):
        """public-search / public-web 未授权 → search_fn/fetch_fn 如实降级，
        不发起任何外发调用（fail-closed；consent 门在流水线之前）。"""
        built: dict = {}

        class StubResearchSkills:
            def __init__(self, **kw):
                built.update(kw)

        rules = {"research_model": {"max_tokens": 900}}
        search_all = mock.Mock(return_value={"results": [], "engines": {}})
        prefs = SimpleNamespace(external_allowed=lambda kind: False)
        with mock.patch.dict(sys.modules, {
            "mylocalai.assistant.skills.research": stub_module(
                "mylocalai.assistant.skills.research", ResearchSkills=StubResearchSkills),
            "mylocalai.assistant.skills.research.pipeline": stub_module(
                "mylocalai.assistant.skills.research.pipeline",
                load_rules=lambda: {"total_deadline_s": 90, "max_page_bytes": 1500000,
                                    "max_text_chars": 20000, "io_timeout_s": 10.0,
                                    "max_redirect_hops": 3}),
            "mylocalai.assistant.skills.transport": stub_module(
                "mylocalai.assistant.skills.transport",
                default_transport=record_transport(built)),
        }), mock.patch("mylocalai.core.privacy.load_preferences", return_value=prefs), \
                mock.patch("mylocalai.retrieval.search.search_all", search_all):
            skills = _real_service("research_skills", rules, None)()
        self.assertIsInstance(skills, StubResearchSkills)
        out = built["search_fn"]("任何词", k=6)
        self.assertEqual(out["results"], [])
        self.assertTrue(out["degraded"])
        self.assertIn("consent_denied", out["error"])
        search_all.assert_not_called()
        fetched = built["fetch_fn"]("https://gov.example/notice/1")
        self.assertEqual(fetched["category"], "consent_denied")
        self.assertEqual(fetched["status"], "error")
        # 未授权时绝不发起取页：transport.fetch 一次都不该被调用
        self.assertEqual(built.get("_fetch_calls", 0), 0)


# ===========================================================================
# 5. 路由与 allowlist（2 例）
# ===========================================================================
class TestRoutingAndAllowlist(ResearchToolTestBase):
    def _route(self, user_text, obj, rules=None):
        return router_mod.route(user_text, self.registry,
                                lambda s, u: (obj, ""),
                                rules or self.rules, self.prompts)

    def test_collect_research_route_positive(self):
        """路由正例：stub route_llm 选 collect_research → 参数经 validate 规整。"""
        out = self._route("帮我调研一下城市绿地的降温效应，整理成简报",
                          {"action": "collect_research", "topic": TOPIC,
                           "hacker_key": "drop table"})
        self.assertEqual(out["action"], "collect_research")
        self.assertEqual(out["args"], {"topic": TOPIC})   # 多余键被丢弃

    def test_collect_research_not_in_allowlist_clarifies(self):
        """不在 allowlist（改 rules 副本）→ 降级 clarify，绝不猜一个工具乱查。"""
        rules = dict(self.rules)
        rules["tool_allowlist"] = [a for a in self.rules["tool_allowlist"]
                                   if a != "collect_research"]
        out = self._route("u", {"action": "collect_research", "topic": TOPIC},
                          rules=rules)
        self.assertEqual(out["action"], "clarify")
        self.assertTrue(out["error"])


# ===========================================================================
# 6. 报告与引文逐字保留（2 例）
# ===========================================================================
class TestReportAndQuoteFidelity(ResearchToolTestBase):
    def test_digest_quotes_are_verbatim(self):
        """digest 引文与 result.data.evidence[].quote 逐字一致 + 计数/缺口/错误/成本行。"""
        result = sample_result()
        out = self.call({"research_skills": stub_skills(result)})
        for item in result["data"]["evidence"]:
            self.assertIn(item["quote"], out["digest"])
        self.assertEqual([e["quote"] for e in out["card"]["evidence"]],
                         [e["quote"] for e in result["data"]["evidence"]])
        # 关键发现逐字、线索只作线索、缺口/错误前三、报告末尾三计数
        self.assertIn("建成区绿地覆盖率越高，夏季地表均温越低。", out["digest"])
        self.assertIn("来源未核验，只作线索", out["digest"])
        self.assertIn("尚未找到第二个已核验信源", out["digest"])
        self.assertIn("[search] engine_unavailable", out["digest"])
        self.assertIn("已执行 4 次搜索、3 次读页、6 次模型调用", out["digest"])
        # 粗口径成本 = llm_calls + searches（M4 真实计数由 loop 的 delta 覆盖）
        self.assertEqual(out["cost"], {"requests": 10, "tokens": 0})

    def test_report_full_text_not_saved_and_no_verdict_words(self):
        """report 全文进业务字段（供导出/复制）；不建档；无真假判定词。"""
        result = sample_result()
        out = self.call({"research_skills": stub_skills(result)})
        self.assertEqual(out["report"], result["report"])
        self.assertIsNone(out["dossier_id"])   # 只读调研：不建档
        self.assertTrue(out["not_saved"])
        self.assertEqual(out["save_reason"], "read_only_capability_no_dossier")
        self.assertNotIn("claim_answer", out)
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, out["report"])
            self.assertNotIn(word, out["digest"])


if __name__ == "__main__":
    unittest.main()
