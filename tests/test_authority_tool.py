# -*- coding: utf-8 -*-
"""权威核验工具层测试（第三波契约 §6.2/§6.5，恰好 22 例）：verify_authority 接进助手工具层。

分组（契约 §6.5，共 22 例，组名即 TestXxx 类）：
   1. 工具注册与校验（5）：默认注册表/元数据、text 空/超长/非字符串被拒、多余键丢弃、
      resolve_action 结构化通道放行只读工具而拒绝写工具（对照）；
   2. 取消与绝对死线透传（4）：ctx.cancel_check 与 llm_cache.deadline_at() 进
      skills.run、死线随回合范围变化、无预算传 None、run 抛异常 → ok=False 不崩；
   3. 六种状态保真（6）：completed/partial/needs_clarification/failed/cancelled/
      budget_exhausted 逐状态断言 ok/degraded/status/card 口径（card_type 恒为
      authority、conclusion/引文逐字来自 result.data）；无 checkable 主张的合法空出口；
   4. 进度事件与服务工厂（3）：on_progress 四步中文阶段进 progress 事件；
      _real_service("authority_skills") 的 rules/authority_config/llm 配置透传；
      consent 未授权时不碰 search/fetch（fail-closed）；
   5. 路由与 allowlist（2）：路由正例、不在 allowlist 降级 clarify；
   6. 结论与引文逐字保留（2）：digest 只转述 conclusion/引文/计数、卡片主张字段与
      pipeline data 一致、report 全文 + 不建档 + 无固定真假标签 + 不返回 claim_answer。

全离线：AuthoritySkills 用 SimpleNamespace stub（记录 run 实参后回预设结果），
llm/search/transport 一概不触网（服务工厂测试用 sys.modules 桩 + mock.patch）。
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
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

TEXT = "网传某疫苗会导致绝缘，劝大家别打。"

#: 四个中文阶段名（pipeline 契约 §5.1：progress 的 component，不暴露内部技能名）。
STAGES = ("拆分主张", "核验权威来源", "阅读取证", "整理分析")

#: 固定真假判定词（铁律 1）：任何出口的 digest/report 里一个都不许出现。
_VERDICT_WORDS = ("是真的", "假的", "属实", "谣言为真")


def load_json(name: str) -> dict:
    return json.loads((ASSISTANT_DIR / name).read_text(encoding="utf-8"))


def sample_result(status: str = "completed", task_status: str = "completed",
                   empty: bool = False) -> dict:
    """一份契约 §5.5 形状的运行结果（字段与 skills/authority pipeline 实际输出对齐）。

    empty=True = 合法空结果（无 checkable 主张）：claims/evidence 为空列表，
    不伪造占位。
    """
    return {
        "tool_id": "verify_authority",
        "status": status,
        "task_status": task_status,
        "data": {
            "claims": [] if empty else [
                {"claim_id": "C1", "text": "某疫苗会导致绝缘",
                 "source_span": {"start": 4, "end": 10},
                 "balance": "refuted", "balance_label": "权威证据反驳",
                 "lean": "refutes", "lean_label": "反驳",
                 "conclusion": "在可核查范围内，权威资料可以直接反驳该说法："
                               "世卫组织澄清说明。",
                 "supporting_ids": [], "refuting_ids": ["E1"], "context_ids": ["E2"],
                 "authority_basis": ["目录锚点：who.int"],
                 "limitations": ["仅覆盖 2024 年后的公开资料"]},
            ],
            "evidence": [] if empty else [
                {"evidence_id": "E1", "claim_ids": ["C1"], "relation": "refutes",
                 "source_id": "SRC-who", "name": "世界卫生组织", "domain": "who.int",
                 "tier": "国际权威", "authority_class": "international_org",
                 "verification": "catalog_anchor",
                 "authority_basis": "目录锚点：skills/sources.json",
                 "title": "疫苗安全澄清", "url": "https://who.int/example",
                 "quote": "目前没有证据表明该疫苗会导致绝缘。",
                 "quote_span": {"start": 0, "end": 10}, "published_at": "2024-05-01",
                 "fetched_at": "2026-11-20T10:00:00+08:00",
                 "independence_group": "who.int", "limitations": []},
                {"evidence_id": "E2", "claim_ids": ["C1"], "relation": "background",
                 "source_id": "CAND-bbs-example", "name": "某论坛", "domain": "bbs.example",
                 "tier": "未登记", "authority_class": "unverified",
                 "verification": "unverified", "authority_basis": "",
                 "title": "网友转述的接种经验帖", "url": "https://bbs.example/t/1",
                 "quote": "楼主自称接种后左手无力，评论区多人跟评类似体感。",
                 "quote_span": {"start": 0, "end": 15}, "published_at": "",
                 "fetched_at": "2026-11-20T10:02:00+08:00",
                 "independence_group": "bbs.example", "limitations": ["自述经历，无法核验"]},
            ],
            "sources": [
                {"source_id": "SRC-who", "name": "世界卫生组织", "domain": "who.int",
                 "tier": "国际权威", "authority_class": "international_org",
                 "verification": "catalog_anchor",
                 "verification_basis": "目录锚点：skills/sources.json", "verified": True},
                {"source_id": "CAND-bbs-example", "name": "某论坛", "domain": "bbs.example",
                 "tier": "未登记", "authority_class": "unverified",
                 "verification": "unverified", "verification_basis": "", "verified": False},
            ],
            "conflicts": [] if empty else [
                {"summary": "权威澄清与网友自述经历指向相反", "evidence_ids": ["E1", "E2"]}],
            "analysis": [] if empty else [
                {"claim_id": "C1",
                 "text": "该说法的可核验部分与权威资料冲突；网友自述不构成证据。",
                 "evidence_ids": ["E1"]}],
            "limitations": [] if empty else ["本次未检索到中文官方通报"],
        },
        "gaps": [] if empty else ["尚未找到第二个独立权威来源"],
        "errors": [] if empty else [
            {"stage": "fetch", "category": "network",
             "detail": "某权威站点本轮读取失败，少了一路取证"}],
        "audit": {"operations": [], "searches": 5, "pages": 4, "llm_calls": 7},
        "budget": {"used": {"max_claims": 1, "max_searches": 5, "max_pages": 4},
                   "limits": {"max_claims": 4, "max_searches": 12, "max_pages": 12}},
        "report": "权威核验报告（只述证据倾向，不判真假）\n执行状态：%s　任务状态：%s\n"
                  "一、主张\n二、权威来源\n三、证据\n四、倾向与结论\n"
                  "已执行 5 次搜索、4 次读页、7 次模型调用" % (status, task_status),
        "started_at": "2026-11-20T09:59:30+08:00",
        "finished_at": "2026-11-20T10:04:30+08:00",
    }


def stub_skills(result: Any = None, raises: Any = None,
                progress_stages: Any = None) -> Any:
    """AuthoritySkills 替身：SimpleNamespace.run 记录实参后回预设结果（全离线口径）。

    progress_stages 非空时，run 内按序回调 on_progress(stage, done, total)——模拟
    流水线的四步中文阶段（events 为 None 时应收到 on_progress=None）。
    """
    calls: list = []

    def _run(text, *, cancel_check=None, deadline=None, on_progress=None):
        calls.append({"text": text, "cancel_check": cancel_check,
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


class AuthorityToolTestBase(unittest.TestCase):
    def setUp(self):
        self.registry = AssistantLoop().registry   # 真实注册表（校验器与生产同源）
        self.rules = load_json("assistant_rules.json")
        self.prompts = load_json("prompts.json")

    def ctx(self, services, cancel_check=None, events=None):
        return SimpleNamespace(sessions=None, dossiers=None, events=events,
                               cancel_check=cancel_check, services=services)

    def call(self, services, text=TEXT, cancel_check=None, events=None):
        spec = self.registry.get("verify_authority")
        assert spec is not None, "verify_authority 未注册"
        return spec["handler"](self.ctx(services, cancel_check, events), text=text)


# ===========================================================================
# 1. 工具注册与校验（5 例）
# ===========================================================================
class TestRegistrationAndValidation(AuthorityToolTestBase):
    def test_registered_in_default_registry_with_metadata(self):
        """进默认注册表：params/permission/cost/est_s 元数据 + 四处接线同源。"""
        spec = self.registry.get("verify_authority")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["params"], ["text"])
        self.assertEqual(spec["permission"], "read")   # 只读核验：不建档
        self.assertEqual(spec["cost"], "heavy")
        self.assertEqual(spec["est_s"], 75.0)
        self.assertIn("权威核验", spec["description"])
        self.assertIn("verify_authority", self.registry.names())
        # 四处接线同源：allowlist / 路由枚举 / 路由提示词 + 卡片类型
        self.assertIn("verify_authority", self.rules["tool_allowlist"])
        self.assertIn("verify_authority", router_mod.TOOL_ACTIONS)
        self.assertIn("verify_authority", self.prompts["router_system"])
        self.assertEqual(_CARD_TYPE.get("verify_authority"), "authority")

    def test_text_param_validation_rejects_bad_inputs(self):
        """text 空/超长/非字符串在 registry 校验层就拒（路由层据此降级 clarify）。"""
        for bad in ("", "   ", None, 123, ["x"], "长" * 8001):
            with self.subTest(text=bad):
                with self.assertRaises(ValidationError):
                    self.registry.validate("verify_authority", {"text": bad})
        clean = self.registry.validate("verify_authority", {"text": f"  {TEXT}  "})
        self.assertEqual(clean, {"text": TEXT})   # 首尾空白被剥掉

    def test_validate_drops_undeclared_keys(self):
        """未声明的键一律丢弃（用户消息/路由 JSON 里的多余字段不进工具）。"""
        clean = self.registry.validate(
            "verify_authority",
            {"text": TEXT, "id": 7, "deep": True, "reply": "hack"})
        self.assertEqual(clean, {"text": TEXT})

    def test_resolve_action_allows_read_only_tool(self):
        """结构化 action 通道放行 verify_authority（只读；web 的精确输入通道）。"""
        loop = AssistantLoop(rules=self.rules, prompts=self.prompts)
        plan = loop.resolve_action({"name": "verify_authority",
                                    "args": {"text": TEXT, "junk": 1}})
        self.assertEqual(plan["kind"], "tools")
        self.assertEqual(plan["steps"],
                         [{"action": "verify_authority", "args": {"text": TEXT}}])

    def test_resolve_action_rejects_write_tools(self):
        """对照：写工具（verify_claim）走结构化通道一律 ToolNotAllowed——写档案必须走
        自然语言路由，action 通道不提供绕过。"""
        loop = AssistantLoop(rules=self.rules, prompts=self.prompts)
        with self.assertRaises(ToolNotAllowed):
            loop.resolve_action({"name": "verify_claim", "args": {"text": TEXT}})


# ===========================================================================
# 2. 取消与绝对死线透传（4 例）
# ===========================================================================
class TestCancelAndDeadlinePassthrough(AuthorityToolTestBase):
    def test_cancel_check_and_deadline_reach_pipeline(self):
        """ctx.cancel_check 与 llm_cache.deadline_at() 原样进 skills.run（§6.2）。"""
        skills = stub_skills(sample_result())

        def cancel_check():
            return False

        with llm_cache.deadline_scope(90):
            expected = llm_cache.deadline_at()
            out = self.call({"authority_skills": skills}, cancel_check=cancel_check)
        self.assertTrue(out["ok"])
        call = skills.calls[-1]
        self.assertEqual(call["text"], TEXT)
        self.assertIs(call["cancel_check"], cancel_check)   # 同一个可调用对象
        self.assertEqual(call["deadline"], expected)         # 绝对死线，非 None

    def test_deadline_tracks_turn_scope(self):
        """回合预算变化 → 透传死线随范围变化（嵌套取更紧者）。"""
        skills = stub_skills(sample_result())
        with llm_cache.deadline_scope(120):
            wide = llm_cache.deadline_at()
            self.call({"authority_skills": skills})
            self.assertEqual(skills.calls[-1]["deadline"], wide)
        with llm_cache.deadline_scope(5):
            tight = llm_cache.deadline_at()
            self.call({"authority_skills": skills})
            self.assertEqual(skills.calls[-1]["deadline"], tight)
        self.assertLess(tight, wide)

    def test_no_turn_budget_passes_none_deadline(self):
        """没有回合预算 → 传 None（流水线按 rules.total_deadline_s 自建）。"""
        self.assertIsNone(llm_cache.deadline_at())   # 前提：当前线程无预算
        skills = stub_skills(sample_result())
        self.call({"authority_skills": skills})
        self.assertIsNone(skills.calls[-1]["deadline"])

    def test_run_exception_degrades_without_crash(self):
        """run 抛异常 → ok=False + 诚实 digest，handler 永不抛（二道闸）。"""
        skills = stub_skills(raises=RuntimeError("上游取证爆炸"))
        out = self.call({"authority_skills": skills})
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
class TestStatusFidelity(AuthorityToolTestBase):
    def _run(self, status, task_status="completed", empty=False):
        result = sample_result(status, task_status, empty=empty)
        return result, self.call({"authority_skills": stub_skills(result)})

    def _assert_card_fidelity(self, result, out):
        """有数据时卡片口径：card_type 恒 authority、conclusion/引文逐字来自 data。"""
        card = out["card"]
        self.assertEqual(card["card_type"], "authority")
        self.assertEqual(card["status"], result["status"])
        self.assertEqual(card["task_status"], result["task_status"])
        self.assertEqual(card["as_of"], result["finished_at"])
        self.assertEqual([c["claim_id"] for c in card["claims"]],
                         [c["claim_id"] for c in result["data"]["claims"]])
        self.assertEqual([c["conclusion"] for c in card["claims"]],
                         [c["conclusion"] for c in result["data"]["claims"]])
        self.assertEqual([e["quote"] for e in card["evidence"]],
                         [e["quote"] for e in result["data"]["evidence"]])
        self.assertEqual(card["counts"], {"claims": 1, "searches": 5, "pages": 4,
                                          "evidence": 2, "official_sources": 1,
                                          "llm_calls": 7})
        self.assertEqual([s["source_id"] for s in card["sources"]["verified"]],
                         ["SRC-who"])
        self.assertEqual([s["source_id"] for s in card["sources"]["unverified"]],
                         ["CAND-bbs-example"])
        self.assertEqual(card["claims"][0]["balance"], "refuted")
        self.assertEqual(card["claims"][0]["balance_label"], "权威证据反驳")
        self.assertEqual(card["claims"][0]["lean"], "refutes")
        self.assertEqual(card["claims"][0]["lean_label"], "反驳")
        self.assertEqual(card["claims"][0]["refuting_ids"], ["E1"])
        self.assertEqual(card["claims"][0]["context_ids"], ["E2"])
        self.assertEqual(card["limitations"], ["本次未检索到中文官方通报"])
        self.assertNotIn("claim_answer", out)   # 不返回 claim_answer（§6.2）

    def test_completed_and_no_factual_claim_are_ok(self):
        """completed 与 no_factual_claim（无 checkable 主张的合法出口）都 ok=True；
        空结果是空列表，不伪造占位；且不出现固定真假判定词。"""
        result, out = self._run("completed", "completed")
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["task_status"], "completed")
        self._assert_card_fidelity(result, out)

        empty, out2 = self._run("completed", "no_factual_claim", empty=True)
        self.assertTrue(out2["ok"])
        self.assertFalse(out2["degraded"])
        self.assertEqual(out2["card"]["claims"], [])
        self.assertEqual(out2["card"]["evidence"], [])
        self.assertEqual(out2["card"]["counts"]["claims"], 0)
        self.assertIn("本次没有可核查的主张", out2["digest"])
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
        """needs_clarification：合法出口（主张超限等），ok=True 不算降级。"""
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
# 4. 进度事件与服务工厂（3 例）
# ===========================================================================
class TestProgressAndServiceFactory(AuthorityToolTestBase):
    def test_progress_events_use_chinese_stage_names(self):
        """on_progress 四步中文阶段进 progress 事件；events 缺失时传 None。"""
        events = RecordingEvents()
        skills = stub_skills(sample_result(), progress_stages=STAGES)
        out = self.call({"authority_skills": skills}, events=events)
        self.assertTrue(out["ok"])
        progress = [e for e in events.events if e["event"] == "progress"]
        self.assertEqual([e["component"] for e in progress], list(STAGES))
        self.assertEqual([e["done"] for e in progress], [1, 2, 3, 4])
        self.assertEqual([e["total"] for e in progress], [4, 4, 4, 4])
        # events 为 None（MCP/同步调用）→ 流水线收到 on_progress=None
        self.assertIsNotNone(skills.calls[-1]["on_progress"])
        skills2 = stub_skills(sample_result())
        self.call({"authority_skills": skills2}, events=None)
        self.assertIsNone(skills2.calls[-1]["on_progress"])

    def test_service_factory_wires_rules_config_and_authority_config(self):
        """_real_service("authority_skills")：rules 走 authority 段、authority_config
        默认加载、llm 用 temperature=0/max_tokens=900/assistant_authority。"""
        built: dict = {}

        class StubAuthoritySkills:
            def __init__(self, **kw):
                built.update(kw)

        rules = {"authority_model": {"temperature": 0, "max_tokens": 900,
                                     "cache_namespace": "assistant_authority"}}
        chat_json = mock.Mock(return_value=({"ok": True}, ""))
        prefs = SimpleNamespace(external_allowed=lambda kind: True)
        with mock.patch.dict(sys.modules, {
            "mylocalai.assistant.skills.authority": stub_module(
                "mylocalai.assistant.skills.authority", AuthoritySkills=StubAuthoritySkills),
            "mylocalai.assistant.skills.authority.pipeline": stub_module(
                "mylocalai.assistant.skills.authority.pipeline",
                load_rules=lambda: {"total_deadline_s": 90, "max_page_bytes": 1500000,
                                    "max_text_chars": 20000, "io_timeout_s": 10.0,
                                    "max_redirect_hops": 3}),
            "mylocalai.assistant.skills.transport": stub_module(
                "mylocalai.assistant.skills.transport",
                default_transport=record_transport(built)),
        }), mock.patch("mylocalai.core.privacy.load_preferences", return_value=prefs), \
                mock.patch("mylocalai.llm.llm_client.chat_json", chat_json), \
                mock.patch("mylocalai.assistant.skills.collect_core.load_authority_config",
                           return_value={"decisive_classes": ["official"]}):
            skills = _real_service("authority_skills", rules, None)()
            self.assertIsInstance(skills, StubAuthoritySkills)
            self.assertEqual(built["rules"], {"total_deadline_s": 90, "max_page_bytes": 1500000,
                                              "max_text_chars": 20000, "io_timeout_s": 10.0,
                                              "max_redirect_hops": 3})
            self.assertEqual(built["authority_config"], {"decisive_classes": ["official"]})
            # 取页上限必须来自 rules.json（否则运维改配置对真实取页无效）
            self.assertEqual(built["_transport_kwargs"], {"max_bytes": 1500000,
                                                         "max_text_chars": 20000,
                                                         "timeout_s": 10.0,
                                                         "max_hops": 3})
            self.assertTrue(callable(built["search_fn"]))
            self.assertTrue(callable(built["fetch_fn"]))
            built["llm_fn"]("sys", "user")
            self.assertEqual(chat_json.call_args.args, ("sys", "user"))
            self.assertEqual(chat_json.call_args.kwargs,
                             {"temperature": 0.0, "max_tokens": 900,
                              "namespace": "assistant_authority"})

    def test_service_factory_consent_denied_blocks_external_io(self):
        """public-search / public-web 未授权 → search_fn/fetch_fn 如实降级，
        不发起任何外发调用（fail-closed；consent 门在流水线之前）。"""
        built: dict = {}

        class StubAuthoritySkills:
            def __init__(self, **kw):
                built.update(kw)

        rules = {"authority_model": {"max_tokens": 900}}
        search_all = mock.Mock(return_value={"results": [], "engines": {}})
        prefs = SimpleNamespace(external_allowed=lambda kind: False)
        with mock.patch.dict(sys.modules, {
            "mylocalai.assistant.skills.authority": stub_module(
                "mylocalai.assistant.skills.authority", AuthoritySkills=StubAuthoritySkills),
            "mylocalai.assistant.skills.authority.pipeline": stub_module(
                "mylocalai.assistant.skills.authority.pipeline",
                load_rules=lambda: {"total_deadline_s": 90, "max_page_bytes": 1500000,
                                    "max_text_chars": 20000, "io_timeout_s": 10.0,
                                    "max_redirect_hops": 3}),
            "mylocalai.assistant.skills.transport": stub_module(
                "mylocalai.assistant.skills.transport",
                default_transport=record_transport(built)),
        }), mock.patch("mylocalai.core.privacy.load_preferences", return_value=prefs), \
                mock.patch("mylocalai.retrieval.search.search_all", search_all), \
                mock.patch("mylocalai.assistant.skills.collect_core.load_authority_config",
                           return_value={}):
            skills = _real_service("authority_skills", rules, None)()
        self.assertIsInstance(skills, StubAuthoritySkills)
        out = built["search_fn"]("任何词", k=6)
        self.assertEqual(out["results"], [])
        self.assertTrue(out["degraded"])
        self.assertIn("consent_denied", out["error"])
        search_all.assert_not_called()
        fetched = built["fetch_fn"]("https://who.int/example")
        self.assertEqual(fetched["category"], "consent_denied")
        self.assertEqual(fetched["status"], "error")
        # 未授权时绝不发起取页：transport.fetch 一次都不该被调用
        self.assertEqual(built.get("_fetch_calls", 0), 0)


# ===========================================================================
# 5. 路由与 allowlist（2 例）
# ===========================================================================
class TestRoutingAndAllowlist(AuthorityToolTestBase):
    def _route(self, user_text, obj, rules=None):
        return router_mod.route(user_text, self.registry,
                                lambda s, u: (obj, ""),
                                rules or self.rules, self.prompts)

    def test_verify_authority_route_positive(self):
        """路由正例：stub route_llm 选 verify_authority → 参数经 validate 规整。"""
        out = self._route("帮我找权威资料看看这个说法能不能被证明或反驳",
                          {"action": "verify_authority", "text": TEXT,
                           "hacker_key": "drop table"})
        self.assertEqual(out["action"], "verify_authority")
        self.assertEqual(out["args"], {"text": TEXT})   # 多余键被丢弃

    def test_verify_authority_not_in_allowlist_clarifies(self):
        """不在 allowlist（改 rules 副本）→ 降级 clarify，绝不猜一个工具乱查。"""
        rules = dict(self.rules)
        rules["tool_allowlist"] = [a for a in self.rules["tool_allowlist"]
                                   if a != "verify_authority"]
        out = self._route("u", {"action": "verify_authority", "text": TEXT},
                          rules=rules)
        self.assertEqual(out["action"], "clarify")
        self.assertTrue(out["error"])


# ===========================================================================
# 6. 结论与引文逐字保留（2 例）
# ===========================================================================
class TestConclusionAndQuoteFidelity(AuthorityToolTestBase):
    def test_digest_transcribes_conclusions_and_quotes(self):
        """digest 只转述 conclusion/引文/计数：模板句式逐字、引文逐字、三计数行。"""
        result = sample_result()
        out = self.call({"authority_skills": stub_skills(result)})
        for claim in result["data"]["claims"]:
            self.assertIn(claim["conclusion"], out["digest"])
        for item in result["data"]["evidence"]:
            self.assertIn(item["quote"], out["digest"])
        self.assertEqual([e["quote"] for e in out["card"]["evidence"]],
                         [e["quote"] for e in result["data"]["evidence"]])
        # balance/lean 中文标签、支持/反驳证据分组、冲突、缺口/错误/限制、三计数
        self.assertIn("权威证据反驳", out["digest"])
        self.assertIn("倾向反驳", out["digest"])
        self.assertIn("反驳证据：E1", out["digest"])
        self.assertIn("权威澄清与网友自述经历指向相反", out["digest"])
        self.assertIn("尚未找到第二个独立权威来源", out["digest"])
        self.assertIn("[fetch] network", out["digest"])
        self.assertIn("本次未检索到中文官方通报", out["digest"])
        self.assertIn("已执行 5 次搜索、4 次读页、7 次模型调用", out["digest"])
        # 粗口径成本 = llm_calls + searches（M4 真实计数由 loop 的 delta 覆盖）
        self.assertEqual(out["cost"], {"requests": 12, "tokens": 0})

    def test_report_full_text_not_saved_and_no_verdict_labels(self):
        """report 全文进业务字段；不建档；无固定真假标签；不得返回 claim_answer。"""
        result = sample_result()
        out = self.call({"authority_skills": stub_skills(result)})
        self.assertEqual(out["report"], result["report"])
        self.assertIsNone(out["dossier_id"])   # 只读核验：不建档
        self.assertTrue(out["not_saved"])
        self.assertEqual(out["save_reason"], "read_only_capability_no_dossier")
        self.assertNotIn("claim_answer", out)
        # 模板允许"证明/反驳"限定词，但不允许固定真假标签
        self.assertIn("可以直接反驳", out["digest"])
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, out["report"])
            self.assertNotIn(word, out["digest"])


if __name__ == "__main__":
    unittest.main()
