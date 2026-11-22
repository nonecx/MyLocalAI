# -*- coding: utf-8 -*-
"""取证三技能工具层测试（第二波契约 §5，恰好 22 例）：collect_evidence 接进助手工具层。

分组（契约 §5，共 22 例，组名即 TestXxx 类）：
  1. 工具注册与校验（5）：默认注册表/元数据、text 空/超长/非字符串被拒、多余键丢弃、
     resolve_action 结构化通道放行只读工具而拒绝写工具（对照）；
  2. 取消与绝对死线透传（4）：ctx.cancel_check 与 llm_cache.deadline_at() 进 skills.run、
     死线随回合范围变化、无预算传 None、run 抛异常 → ok=False 不崩；
  3. 六种状态保真（6）：completed/partial/needs_clarification/failed/cancelled/
     budget_exhausted 逐状态断言 ok/degraded/status/card 口径（card_type 恒为 evidence、
     引文逐字来自 result.data.evidence）；no_factual_claim 是合法空出口；
  4. 路由与 allowlist（3）：路由正例、不在 allowlist 降级 clarify、mixed_choice 混合表达
     返回选择话术且不调 LLM（单一意图不触发、缺 prompts 键有内置兜底）；
  5. 上下文不伪造档案号（2）：活跃档案卡只含档案库真实存在的 id、id_fallback 只取
     tool_runs 记录不取用户消息里的数字；
  6. 报告引用逐字保留（2）：digest 引文与 result.data.evidence[].quote 逐字一致、
     report 全文进业务字段且不含真假判定词。

全离线：EvidenceSkills 用 SimpleNamespace stub（记录 run 实参后回预设结果），
llm/search/transport 一概不触网；会话/档案库用临时文件库（tool_runs.dossier_id 有外键，
必须同库）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mylocalai.assistant as assistant_pkg  # noqa: E402
from mylocalai.assistant import router as router_mod  # noqa: E402
from mylocalai.assistant.loop import _CARD_TYPE, AssistantLoop  # noqa: E402
from mylocalai.assistant.registry import ToolNotAllowed, ValidationError  # noqa: E402
from mylocalai.assistant.session_store import SessionStore  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402
from mylocalai.llm import llm_cache  # noqa: E402

ASSISTANT_DIR = Path(assistant_pkg.__file__).resolve().parent

EVIDENCE_TEXT = "网传某地自来水被投毒，请大家扩散。"

#: 真假判定词（铁律 1）：任何出口的 digest/report 里一个都不许出现。
_VERDICT_WORDS = ("是真的", "假的", "属实", "谣言为真")


def load_json(name: str) -> dict:
    return json.loads((ASSISTANT_DIR / name).read_text(encoding="utf-8"))


def sample_result(status: str = "completed", task_status: str = "completed",
                  empty: bool = False) -> dict:
    """一份契约 §3.3 形状的运行结果（字段与 skills/pipeline 实际输出对齐）。

    empty=True = 合法空结果（无可核查主张）：claims/evidence 为空列表，不伪造占位。
    """
    return {
        "tool_id": "collect_evidence",
        "status": status,
        "task_status": task_status,
        "data": {
            "domain": {"primary": "public_health", "secondary": [], "region": "CN",
                       "time": "", "labels": ["公共卫生"], "basis": ["自来水"]},
            "sources": [
                {"source_id": "SRC-gov-example", "name": "某市卫健委", "domain": "gov.example",
                 "tier": "官方权威", "verified": True, "verification": "catalog_anchor",
                 "basis": "首页备案信息", "scope_note": "本市自来水水质通报"},
                {"source_id": "CAND-bbs-example", "name": "某论坛", "domain": "bbs.example",
                 "tier": "未登记", "verified": False,
                 "verification": "unverified_candidate", "basis": "",
                 "scope_note": "既非目录锚点，也未从已核验站点页面直接链接到"},
            ],
            "claims": [] if empty else [
                {"claim_id": "C1", "text": "自来水被投毒",
                 "source_span": {"start": 0, "end": 6},
                 "qualifiers": {"time": None, "place": None, "entity": None},
                 "claim_status": "checkable"}],
            "evidence": [] if empty else [
                {"evidence_id": "E1", "claim_ids": ["C1"], "relation": "refutes",
                 "source_id": "SRC-gov-example", "url": "https://gov.example/notice/1",
                 "title": "水质检测情况通报",
                 "quote": "经连续检测，出厂水与管网水各项指标均符合国家生活饮用水卫生标准。",
                 "quote_span": {"start": 12, "end": 38}, "published_at": None,
                 "fetched_at": "2026-11-20T10:00:00+08:00", "limitations": [],
                 "independence_group": "gov.example", "domain": "gov.example"},
                {"evidence_id": "E2", "claim_ids": ["C1"], "relation": "background",
                 "source_id": "SRC-gov-example", "url": "https://gov.example/notice/2",
                 "title": "供水系统日常运维说明",
                 "quote": "水厂实行 24 小时在线监测，异常数据即时上报并启动复核程序。",
                 "quote_span": {"start": 0, "end": 27}, "published_at": None,
                 "fetched_at": "2026-11-20T10:01:00+08:00", "limitations": [],
                 "independence_group": "gov.example", "domain": "gov.example"},
            ],
            "chains": [],
        },
        "gaps": ["尚未找到第二个已核验信源", "主张的时间限定未在原文中明确"],
        "errors": [{"stage": "sources", "category": "homepage_unavailable",
                    "detail": "bbs.example 首页未读取，身份依据缺失"}],
        "audit": {"operations": [], "searches": 3, "pages": 2, "llm_calls": 5},
        "budget": {"used": {"max_claims": 1, "max_sources": 1},
                   "limits": {"max_claims": 5, "max_sources": 6}},
        "report": "取证报告（只述证据，不判真假）\n执行状态：%s　任务状态：%s\n"
                  "一、领域识别\n二、信源\n三、主张\n四、证据\n"
                  "已执行 3 次搜索、2 次读页、5 次模型调用" % (status, task_status),
        "started_at": "2026-11-20T09:59:30+08:00",
        "finished_at": "2026-11-20T10:01:30+08:00",
    }


def stub_skills(result: Any = None, raises: Any = None) -> Any:
    """EvidenceSkills 替身：SimpleNamespace.run 记录实参后回预设结果（契约 §5 全离线口径）。"""
    calls: list = []

    def _run(text, *, cancel_check=None, deadline=None):
        calls.append({"text": text, "cancel_check": cancel_check, "deadline": deadline})
        if raises is not None:
            raise raises
        return result

    def _factory():
        return SimpleNamespace(run=_run)

    _factory.calls = calls
    return _factory


class EvidenceToolTestBase(unittest.TestCase):
    def setUp(self):
        self.registry = AssistantLoop().registry   # 真实注册表（校验器与生产同源）
        self.rules = load_json("assistant_rules.json")
        self.prompts = load_json("prompts.json")

    def ctx(self, services, cancel_check=None):
        return SimpleNamespace(sessions=None, dossiers=None, events=None,
                               cancel_check=cancel_check, services=services)

    def call(self, services, text=EVIDENCE_TEXT, cancel_check=None):
        spec = self.registry.get("collect_evidence")
        assert spec is not None, "collect_evidence 未注册"
        return spec["handler"](self.ctx(services, cancel_check), text=text)


# ===========================================================================
# 1. 工具注册与校验（5 例）
# ===========================================================================
class TestRegistrationAndValidation(EvidenceToolTestBase):
    def test_registered_in_default_registry_with_metadata(self):
        """进默认注册表：params/permission/cost/est_s 元数据 + 三处接线同源。"""
        spec = self.registry.get("collect_evidence")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["params"], ["text"])
        self.assertEqual(spec["permission"], "read")   # 只读取证：不建档
        self.assertEqual(spec["cost"], "heavy")
        self.assertEqual(spec["est_s"], 60.0)
        self.assertIn("只读取证", spec["description"])
        self.assertIn("collect_evidence", self.registry.names())
        # 三处接线同源：allowlist / 路由枚举 / 路由提示词 + 卡片类型
        self.assertIn("collect_evidence", self.rules["tool_allowlist"])
        self.assertIn("collect_evidence", router_mod.TOOL_ACTIONS)
        self.assertIn("collect_evidence", self.prompts["router_system"])
        self.assertEqual(_CARD_TYPE.get("collect_evidence"), "evidence")

    def test_text_param_validation_rejects_bad_inputs(self):
        """text 空/超长/非字符串在 registry 校验层就拒（路由层据此降级 clarify）。"""
        for bad in ("", "   ", None, 123, ["x"], "长" * 8001):
            with self.subTest(text=bad):
                with self.assertRaises(ValidationError):
                    self.registry.validate("collect_evidence", {"text": bad})
        clean = self.registry.validate("collect_evidence",
                                       {"text": f"  {EVIDENCE_TEXT}  "})
        self.assertEqual(clean, {"text": EVIDENCE_TEXT})   # 首尾空白被剥掉

    def test_validate_drops_undeclared_keys(self):
        """未声明的键一律丢弃（用户消息/路由 JSON 里的多余字段不进工具）。"""
        clean = self.registry.validate(
            "collect_evidence",
            {"text": EVIDENCE_TEXT, "id": 7, "deep": True, "reply": "hack"})
        self.assertEqual(clean, {"text": EVIDENCE_TEXT})

    def test_resolve_action_allows_read_only_tool(self):
        """结构化 action 通道放行 collect_evidence（只读；web 的精确输入通道）。"""
        loop = AssistantLoop(rules=self.rules, prompts=self.prompts)
        plan = loop.resolve_action({"name": "collect_evidence",
                                    "args": {"text": EVIDENCE_TEXT, "junk": 1}})
        self.assertEqual(plan["kind"], "tools")
        self.assertEqual(plan["steps"],
                         [{"action": "collect_evidence", "args": {"text": EVIDENCE_TEXT},
                           "step_id": "collect_evidence:1", "extra_step_ids": []}])

    def test_resolve_action_rejects_write_tools(self):
        """对照：写工具（verify_claim）走结构化通道一律 ToolNotAllowed——写档案必须走
        自然语言路由，action 通道不提供绕过。"""
        loop = AssistantLoop(rules=self.rules, prompts=self.prompts)
        with self.assertRaises(ToolNotAllowed):
            loop.resolve_action({"name": "verify_claim", "args": {"text": EVIDENCE_TEXT}})


# ===========================================================================
# 2. 取消与绝对死线透传（4 例）
# ===========================================================================
class TestCancelAndDeadlinePassthrough(EvidenceToolTestBase):
    def test_cancel_check_and_deadline_reach_pipeline(self):
        """ctx.cancel_check 与 llm_cache.deadline_at() 原样进 skills.run（§4.3）。"""
        skills = stub_skills(sample_result())

        def cancel_check():
            return False

        with llm_cache.deadline_scope(90):
            expected = llm_cache.deadline_at()
            out = self.call({"evidence_skills": skills}, cancel_check=cancel_check)
        self.assertTrue(out["ok"])
        call = skills.calls[-1]
        self.assertEqual(call["text"], EVIDENCE_TEXT)
        self.assertIs(call["cancel_check"], cancel_check)   # 同一个可调用对象
        self.assertEqual(call["deadline"], expected)         # 绝对死线，非 None

    def test_deadline_tracks_turn_scope(self):
        """回合预算变化 → 透传死线随范围变化（嵌套取更紧者）。"""
        skills = stub_skills(sample_result())
        with llm_cache.deadline_scope(120):
            wide = llm_cache.deadline_at()
            self.call({"evidence_skills": skills})
            self.assertEqual(skills.calls[-1]["deadline"], wide)
        with llm_cache.deadline_scope(5):
            tight = llm_cache.deadline_at()
            self.call({"evidence_skills": skills})
            self.assertEqual(skills.calls[-1]["deadline"], tight)
        self.assertLess(tight, wide)

    def test_no_turn_budget_passes_none_deadline(self):
        """没有回合预算 → 传 None（流水线按 rules.total_deadline_s 自建）。"""
        self.assertIsNone(llm_cache.deadline_at())   # 前提：当前线程无预算
        skills = stub_skills(sample_result())
        self.call({"evidence_skills": skills})
        self.assertIsNone(skills.calls[-1]["deadline"])

    def test_run_exception_degrades_without_crash(self):
        """run 抛异常 → ok=False + 诚实 digest，handler 永不抛（二道闸）。"""
        skills = stub_skills(raises=RuntimeError("上游取证爆炸"))
        out = self.call({"evidence_skills": skills})
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
class TestStatusFidelity(EvidenceToolTestBase):
    def _run(self, status, task_status="completed", empty=False):
        result = sample_result(status, task_status, empty=empty)
        return result, self.call({"evidence_skills": stub_skills(result)})

    def _assert_card_fidelity(self, result, out):
        """有数据时卡片口径：card_type 恒 evidence、引文逐字来自 result.data.evidence。"""
        card = out["card"]
        self.assertEqual(card["card_type"], "evidence")
        self.assertEqual(card["status"], result["status"])
        self.assertEqual(card["task_status"], result["task_status"])
        self.assertEqual(card["as_of"], result["finished_at"])
        self.assertEqual([e["evidence_id"] for e in card["evidence"]],
                         [e["evidence_id"] for e in result["data"]["evidence"]])
        self.assertEqual([e["quote"] for e in card["evidence"]],
                         [e["quote"] for e in result["data"]["evidence"]])
        self.assertEqual(card["counts"], {"claims": 1, "verified_sources": 1, "evidence": 2,
                                          "searches": 3, "pages": 2, "llm_calls": 5})
        self.assertEqual([s["source_id"] for s in card["sources"]["verified"]],
                         ["SRC-gov-example"])
        self.assertEqual([s["source_id"] for s in card["sources"]["unverified"]],
                         ["CAND-bbs-example"])
        self.assertEqual(card["domain"]["primary"], "public_health")
        self.assertEqual(card["domain"]["labels"], ["公共卫生"])

    def test_completed_and_no_factual_claim_are_ok(self):
        """completed 与 no_factual_claim（无 checkable 主张的合法出口）都 ok=True；
        空结果是空列表，不伪造占位；且不出现真假判定词。"""
        result, out = self._run("completed", "completed")
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["task_status"], "completed")
        self._assert_card_fidelity(result, out)

        empty, out2 = self._run("completed", "no_factual_claim", empty=True)
        self.assertTrue(out2["ok"])
        self.assertFalse(out2["degraded"])
        self.assertEqual(out2["evidence_count"], 0)
        self.assertEqual(out2["claims"], [])
        self.assertEqual(out2["card"]["claims"], [])
        self.assertEqual(out2["card"]["evidence"], [])
        self.assertEqual(out2["card"]["counts"]["evidence"], 0)
        self.assertIn("（本次没有可定位引文）", out2["digest"])
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
        self.assertEqual(out["evidence_count"], 2)
        self._assert_card_fidelity(result, out)

    def test_needs_clarification_status_fidelity(self):
        """needs_clarification：合法出口（预检失败/领域四态/主张超限），不算降级。"""
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
        """cancelled：ok=False，但已取得的证据仍在卡片里（取消不整批丢弃）。"""
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
# 4. 路由与 allowlist（3 例）
# ===========================================================================
class TestRoutingAndAllowlist(EvidenceToolTestBase):
    def _route(self, user_text, obj, rules=None):
        return router_mod.route(user_text, self.registry,
                                lambda s, u: (obj, ""),
                                rules or self.rules, self.prompts)

    def test_collect_evidence_route_positive(self):
        """路由正例：stub route_llm 选 collect_evidence → 参数经 validate 规整。"""
        out = self._route("帮我把这段内容的主张找出来，只述证据",
                          {"action": "collect_evidence", "text": EVIDENCE_TEXT,
                           "hacker_key": "drop table"})
        self.assertEqual(out["action"], "collect_evidence")
        self.assertEqual(out["args"], {"text": EVIDENCE_TEXT})   # 多余键被丢弃

    def test_collect_evidence_not_in_allowlist_clarifies(self):
        """不在 allowlist（改 rules 副本）→ 降级 clarify，绝不猜一个工具乱查。"""
        rules = dict(self.rules)
        rules["tool_allowlist"] = [a for a in self.rules["tool_allowlist"]
                                   if a != "collect_evidence"]
        out = self._route("u", {"action": "collect_evidence", "text": EVIDENCE_TEXT},
                          rules=rules)
        self.assertEqual(out["action"], "clarify")
        self.assertTrue(out["error"])

    def test_mixed_choice_asks_instead_of_chaining(self):
        """混合表达（总览 §6.3）：同时要"核查真假"与"解释热度" → 选择话术，不调 LLM。"""
        calls = []

        def _llm(system, user):
            calls.append(1)
            return ({"action": "verify_claim", "text": "x"}, "")

        mixed_text = "这事儿有这么火吗？是真的还是假的？"
        out = router_mod.route(mixed_text, self.registry, _llm, self.rules, self.prompts)
        self.assertEqual(out["action"], "clarify")
        self.assertEqual(out["args"], {})
        self.assertEqual(out["reply"], self.prompts["mixed_choose_reply"])
        self.assertEqual(calls, [], "命中混合表达闸时路由 LLM 零调用")

        # 单一意图不触发："热搜"是话题名词不是热度疑问短语
        self.assertIsNone(
            router_mod.mixed_choice("帮我核实这个热搜是不是真的", self.prompts))
        # prompts 缺 mixed_choose_reply → 内置兜底（与 JSON 同文）
        fallback = router_mod.mixed_choice(mixed_text, {})
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback["reply"], router_mod._MIXED_CHOICE_FALLBACK)
        self.assertEqual(fallback["reply"], self.prompts["mixed_choose_reply"])
        # 怪异输入不抛异常（与 pre_refuse 同族）
        self.assertIsNone(router_mod.mixed_choice(None, self.prompts))


# ===========================================================================
# 5. 上下文不伪造档案号（2 例）
# ===========================================================================
class TestRouteContextNoFabrication(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 会话域与档案域同库（tool_runs.dossier_id 有外键，分库会撞 FK）
        db = Path(self.tmp.name) / "app.db"
        self.sessions = SessionStore(db)
        self.addCleanup(self.sessions.close)
        self.dossiers = DossierService(DossierStore(db))
        self.addCleanup(self.dossiers.close)
        self.rules = load_json("assistant_rules.json")
        self.prompts = load_json("prompts.json")
        self.session_id = self.sessions.create_session("取证会话")
        self.dossier_id = self.dossiers.start_dossier(EVIDENCE_TEXT, input_kind="text")
        # 当前用户消息提到"档案 #999"——它只是文本，不该变成背景或兜底
        self.current = [{"role": "user", "content": "档案 #999 的结论是什么？",
                         "kind": "text"}]

    def loop(self, sessions=None):
        return AssistantLoop(sessions=sessions or self.sessions,
                             dossiers=self.dossiers, rules=self.rules,
                             prompts=self.prompts)

    def _record_run(self, dossier_id):
        run_id = self.sessions.start_run(self.session_id, "collect_evidence",
                                         {"text": EVIDENCE_TEXT})
        self.sessions.finish_run(run_id, "ok", dossier_id=dossier_id)

    def test_active_dossier_card_only_contains_real_ids(self):
        """活跃档案卡只含档案库真实存在的 id；用户消息里的 #999 不进 history_hint。"""
        self._record_run(self.dossier_id)
        hint, fallback = self.loop()._route_context(self.session_id, self.current)
        self.assertIn(f"#{self.dossier_id}《", hint)
        self.assertNotIn("999", hint)
        self.assertEqual(fallback, self.dossier_id)

    def test_id_fallback_comes_from_tool_runs_only(self):
        """id_fallback 只取 tool_runs 记录；没有 run → 无背景无兜底（用户随口说的
        档案号不进兜底）；指向不存在档案的记录不伪造卡片。"""
        hint, fallback = self.loop()._route_context(self.session_id, self.current)
        self.assertEqual(hint, "")
        self.assertIsNone(fallback)

        stub_sessions = SimpleNamespace(runs_for_session=lambda sid: [
            {"id": 7, "dossier_id": 424242, "tool": "collect_evidence", "args": {}},
            {"id": 8, "dossier_id": self.dossier_id, "tool": "collect_evidence",
             "args": {}},
        ])
        hint2, fallback2 = self.loop(sessions=stub_sessions)._route_context(
            self.session_id, self.current)
        self.assertIn(f"#{self.dossier_id}《", hint2)
        self.assertNotIn("424242", hint2)   # 库里没有的档案：不伪造卡片
        self.assertEqual(fallback2, self.dossier_id)


# ===========================================================================
# 6. 报告引用逐字保留（2 例）
# ===========================================================================
class TestReportAndQuoteFidelity(EvidenceToolTestBase):
    def test_digest_quotes_are_verbatim(self):
        """digest 引文与 result.data.evidence[].quote 逐字一致 + 计数/缺口/错误/成本行。"""
        result = sample_result()
        out = self.call({"evidence_skills": stub_skills(result)})
        for item in result["data"]["evidence"]:
            self.assertIn(item["quote"], out["digest"])
        self.assertEqual([e["quote"] for e in out["card"]["evidence"]],
                         [e["quote"] for e in result["data"]["evidence"]])
        # 关系分组计数、缺口前 3、错误前 3、报告末尾三计数
        self.assertIn("反驳 1 条", out["digest"])
        self.assertIn("背景 1 条", out["digest"])
        self.assertIn("尚未找到第二个已核验信源", out["digest"])
        self.assertIn("[sources] homepage_unavailable", out["digest"])
        self.assertIn("已执行 3 次搜索、2 次读页、5 次模型调用", out["digest"])
        # 粗口径成本 = llm_calls + searches（M4 真实计数由 loop 的 delta 覆盖）
        self.assertEqual(out["cost"], {"requests": 8, "tokens": 0})

    def test_report_full_text_and_no_verdict_words(self):
        """report 全文进业务字段（供导出/复制）且不含真假判定词；不建档。"""
        result = sample_result()
        out = self.call({"evidence_skills": stub_skills(result)})
        self.assertEqual(out["report"], result["report"])
        self.assertEqual(out["evidence_count"], 2)
        self.assertEqual([c["claim_id"] for c in out["claims"]], ["C1"])
        self.assertIn("自来水被投毒", out["claims"][0]["text"])
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, out["report"])
            self.assertNotIn(word, out["digest"])
        self.assertIsNone(out["dossier_id"])   # 只读取证：不建档


if __name__ == "__main__":
    unittest.main()
