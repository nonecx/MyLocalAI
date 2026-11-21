# -*- coding: utf-8 -*-
"""辟谣方法指南测试（skills/guide/guide.py + tools_guide）：mock llm，全离线零检索。

分组：
   1. 正常讲解路径（3）：llm 正常 → state=ok/completed、steps 五步法代码锁定、
      follow_up_hint 指路 verify_claim、system 提示 = SKILL.md+契约装配；
   2. 降级路径（6）：llm 未注入/异常/坏形状/空讲解/含 URL/含真假判定词 →
      state=degraded、answer 用内置五步法模板兜底（五步清单照常完整输出）、
      error/缺口如实记录、绝不冒充讲解成功；
   3. 预检与预算（4）：空输入/超长 → needs_clarification 且不调模型、死线已过 →
      budget_exhausted、取消 → cancelled；
   4. 零外发锁定（2）：模块内没有任何检索/取页 import、构造签名没有 search/fetch/
      transport 注入位、audit 恒 0 检索 0 读页；
   5. 纪律锁定（3）：steps 与 follow_up_hint 恒为代码产物（模型输出无法污染）、
      出口零真假判定词、data 键名锁定；
   6. 工具层（4）：注册元数据、参数校验、死线/取消透传、run 异常降级不崩、
      digest 转述五步法与讲解原文。

全离线：llm_fn 注入假件，零网络（除注入的 llm_fn 桩）、零检索、零取页、零档案。
"""

from __future__ import annotations

import inspect
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import tools_guide  # noqa: E402
from mylocalai.assistant.registry import ToolRegistry, ValidationError  # noqa: E402
from mylocalai.assistant.skills.guide import GuideSkills, guide_fact_check  # noqa: E402
from mylocalai.assistant.skills.guide import guide as G  # noqa: E402
from mylocalai.llm import llm_cache  # noqa: E402

CLOCK = "2026-11-21T00:00:00+00:00"
QUESTION = "怎么辨别健康养生类的谣言？"
GOOD_ANSWER = ("先看出处：作者与发布机构是否可识别、可追责；再做交叉验证，看多个独立信源"
               "是否一致；然后查原始引用，对照原始出处；接着识别情绪化标题与断言强度；"
               "最后理解证据等级，官方文件高于自媒体转述。")

#: 固定真假判定词（铁律 1）：任何出口一个都不许出现
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")


def _ok_llm(answer: str = GOOD_ANSWER):
    """正常讲解替身：记录 (system, user) 后回契约形状。"""
    calls: list = []

    def _llm(system: str, user: str):
        calls.append({"system": system, "user": user})
        return {"answer": answer}, ""

    _llm.calls = calls
    return _llm


def _skills(llm=None, **kw) -> GuideSkills:
    return GuideSkills(llm_fn=llm, clock=lambda: CLOCK, **kw)


# ===========================================================================
# 1. 正常讲解路径（3）
# ===========================================================================
class TestHappyPath(unittest.TestCase):
    def test_ok_answer_with_locked_five_steps(self):
        llm = _ok_llm()
        result = _skills(llm).run(QUESTION)
        data = result["data"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(data["state"], "ok")
        self.assertEqual(data["answer"], GOOD_ANSWER)          # 逐字透传
        self.assertEqual(data["steps"], list(G.FIVE_STEPS))    # 五步法口径代码锁定
        self.assertEqual(len(data["steps"]), 5)
        for title in G.FIVE_STEP_TITLES:                       # 五个短标题都在
            self.assertIn(title, "\n".join(data["steps"]))
        self.assertIn("verify_claim", data["follow_up_hint"])  # 指路核查流程
        self.assertIn("verify_authority", data["follow_up_hint"])
        self.assertEqual(result["audit"]["llm_calls"], 1)
        self.assertEqual(result["audit"]["searches"], 0)
        self.assertEqual(result["audit"]["pages"], 0)
        self.assertEqual(llm.calls[-1]["user"].split("：")[0], "用户问题")

    def test_system_prompt_is_skill_md_assembly(self):
        """system 提示 = load_skill_prompt 装配的 SKILL.md + 机器契约。"""
        llm = _ok_llm()
        _skills(llm).run(QUESTION)
        system = llm.calls[-1]["system"]
        self.assertIn("技能名：fact-check-guide", system)      # SKILL.md 头部
        self.assertIn("查证五步法", system)
        self.assertIn("answer", system)                        # JSON 输出契约
        self.assertIn("分类：通用", system)
        self.assertIn("服务能力：fact-check-guide", system)

    def test_convenience_function_same_shell(self):
        result = guide_fact_check(QUESTION, llm_fn=_ok_llm(), clock=lambda: CLOCK)
        self.assertEqual(result["tool_id"], "guide_fact_check")
        self.assertEqual(result["data"]["state"], "ok")


# ===========================================================================
# 2. 降级路径（6）
# ===========================================================================
class TestDegradedPaths(unittest.TestCase):
    def _assert_template_fallback(self, result: Dict[str, Any]) -> None:
        """统一断言：state=degraded、answer=内置模板、五步清单完整、缺口如实。"""
        data = result["data"]
        self.assertEqual(data["state"], "degraded")
        self.assertEqual(result["status"], "partial")          # 有产物但有缺口
        self.assertEqual(data["steps"], list(G.FIVE_STEPS))
        self.assertEqual(data["answer"], G._template_answer())
        for title in G.FIVE_STEP_TITLES:
            self.assertIn(title, data["answer"])
        self.assertTrue(result["gaps"])
        self.assertTrue(result["errors"])

    def test_llm_not_injected_degrades(self):
        self._assert_template_fallback(_skills(None).run(QUESTION))
        self.assertEqual(_skills(None).run(QUESTION)["audit"]["llm_calls"], 0)

    def test_llm_error_tuple_degrades(self):
        self._assert_template_fallback(
            _skills(lambda s, u: (None, "boom: backend down")).run(QUESTION))

    def test_llm_exception_degrades(self):
        def _boom(system, user):
            raise RuntimeError("模型炸了")
        result = _skills(_boom).run(QUESTION)
        self._assert_template_fallback(result)
        self.assertTrue(any(e["category"] == "llm_exception" for e in result["errors"]))

    def test_llm_bad_shape_degrades(self):
        self._assert_template_fallback(_skills(lambda s, u: "不是 dict").run(QUESTION))

    def test_answer_with_url_rejected(self):
        """讲解里塞 URL → 整条丢弃（指南零外发：不引导用户点开未知站）。"""
        result = _skills(lambda s, u: ({"answer": "方法如下，详见 https://x.example/a"}, "")).run(QUESTION)
        self._assert_template_fallback(result)
        self.assertTrue(any(e["category"] == "answer_new_url" for e in result["errors"]))
        self.assertEqual(result["audit"]["llm_calls"], 1)      # 预算如实记账

    def test_answer_with_verdict_word_rejected(self):
        """讲解含固定真假判定词 → 整条丢弃（判定权不在模型手里）。"""
        for word in ("这消息是真的", "官方说是假的", "基本属实"):
            with self.subTest(word=word):
                result = _skills(lambda s, u, w=word: ({"answer": f"先看出处。{w}。"}, "")).run(QUESTION)
                self._assert_template_fallback(result)
                self.assertTrue(any(e["category"] == "answer_verdict_word"
                                    for e in result["errors"]))

    def test_empty_answer_rejected(self):
        self._assert_template_fallback(_skills(lambda s, u: ({"answer": "   "}, "")).run(QUESTION))


# ===========================================================================
# 3. 预检与预算（4）
# ===========================================================================
class TestPrecheckAndBudget(unittest.TestCase):
    def test_empty_question_needs_clarification_without_llm(self):
        llm = _ok_llm()
        result = _skills(llm).run("   ")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["task_status"], "needs_clarification")
        self.assertEqual(llm.calls, [])                        # 不为空问题调模型
        self.assertTrue(any(e["category"] == "empty_input" for e in result["errors"]))

    def test_too_long_question_needs_clarification(self):
        result = _skills(_ok_llm()).run("问" * 2001)
        self.assertEqual(result["status"], "needs_clarification")
        self.assertTrue(any(e["category"] == "input_too_long" for e in result["errors"]))

    def test_expired_deadline_budget_exhausted(self):
        llm = _ok_llm()
        result = _skills(llm).run(QUESTION, deadline=time.monotonic() - 1.0)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(llm.calls, [])                        # 死线已过，不调模型
        self.assertEqual(result["data"]["steps"], list(G.FIVE_STEPS))  # 清单保留
        self.assertEqual(result["data"]["answer"], G._template_answer())

    def test_cancelled_preserves_step_list(self):
        llm = _ok_llm()
        result = _skills(llm).run(QUESTION, cancel_check=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(llm.calls, [])
        self.assertEqual(result["data"]["steps"], list(G.FIVE_STEPS))
        self.assertIn("取消", "\n".join(result["gaps"]))


# ===========================================================================
# 4. 零外发锁定（2）
# ===========================================================================
class TestZeroOutbound(unittest.TestCase):
    def test_no_fetch_or_search_imports_in_module(self):
        """模块内禁止 import 检索/取页函数（保证零外发请求的结构性约束）。"""
        source = inspect.getsource(G)
        banned = ("transport", "retrieval", "search_all", "default_transport",
                  "fetch_fn", "search_fn")
        import_lines = [ln.strip() for ln in source.splitlines()
                        if ln.strip().startswith(("import ", "from "))]
        self.assertTrue(import_lines)
        for line in import_lines:
            for word in banned:
                self.assertNotIn(word, line.lower(), f"禁止的依赖: {line}")
        # 整个模块源码里也不许出现取页/检索函数的调用形态
        self.assertNotIn("search_all(", source)
        self.assertNotIn(".fetch(", source)

    def test_constructor_has_no_io_injection_and_audit_stays_zero(self):
        """构造签名没有 search/fetch/transport 注入位；audit 恒 0 检索 0 读页。"""
        params = inspect.signature(GuideSkills.__init__).parameters
        for banned in ("search_fn", "fetch_fn", "transport"):
            self.assertNotIn(banned, params)
        result = _skills(_ok_llm()).run(QUESTION)
        self.assertEqual(result["audit"]["searches"], 0)
        self.assertEqual(result["audit"]["pages"], 0)


# ===========================================================================
# 5. 纪律锁定（3）
# ===========================================================================
class TestDiscipline(unittest.TestCase):
    def test_steps_and_followup_are_code_owned(self):
        """steps/follow_up_hint 恒为代码产物：模型返回什么都无法污染这两个键。"""
        def _evil(system, user):
            return {"answer": GOOD_ANSWER, "steps": ["模型改的五步"],
                    "follow_up_hint": "别用核查流程了"}, ""
        result = _skills(_evil).run(QUESTION)
        self.assertEqual(result["data"]["steps"], list(G.FIVE_STEPS))
        self.assertEqual(result["data"]["follow_up_hint"], G.FOLLOW_UP_HINT)
        self.assertEqual(result["data"]["answer"], GOOD_ANSWER)

    def test_no_verdict_words_in_any_exit(self):
        for llm, label in ((_ok_llm(), "ok路径"), (None, "降级路径")):
            with self.subTest(label=label):
                result = _skills(llm).run(QUESTION)
                for text in (result["data"]["answer"],
                             result["data"]["follow_up_hint"], result["report"],
                             "\n".join(result["gaps"])):
                    for word in _VERDICT_WORDS:
                        self.assertNotIn(word, text)

    def test_data_keys_locked_and_progress_stages(self):
        """data 键名锁定；on_progress 三步中文阶段。"""
        seen: list = []
        result = _skills(_ok_llm(),
                         on_progress=lambda s, d, t: seen.append((s, d, t))).run(QUESTION)
        self.assertEqual(tuple(sorted(result["data"])),
                         ("answer", "follow_up_hint", "question", "state", "steps"))
        self.assertEqual([s[0] for s in seen], list(G._PROGRESS_STAGES))
        self.assertEqual([s[1] for s in seen], [1, 2, 3])


# ===========================================================================
# 6. 工具层（4）
# ===========================================================================
def _tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    tools_guide.register_all(reg, None)
    return reg


def _ctx(services: Any, cancel_check=None, events=None) -> SimpleNamespace:
    return SimpleNamespace(sessions=None, dossiers=None, events=events,
                           cancel_check=cancel_check, services=services)


def _stub_skills(result: Any, raises: Any = None):
    """GuideSkills 替身：记录 run 实参后回预设结果（工具层透传口径锁定）。"""
    calls: list = []

    def _run(question, *, cancel_check=None, deadline=None, on_progress=None):
        calls.append({"question": question, "cancel_check": cancel_check,
                      "deadline": deadline, "on_progress": on_progress})
        if raises is not None:
            raise raises
        return result

    def _factory():
        return SimpleNamespace(run=_run)

    _factory.calls = calls
    return _factory


class TestToolLayer(unittest.TestCase):
    def _call(self, services, question=QUESTION, cancel_check=None, events=None):
        reg = _tool_registry()
        spec = reg.get("guide_fact_check")
        assert spec is not None, "guide_fact_check 未注册"
        return spec["handler"](_ctx(services, cancel_check, events), question=question)

    def _sample_result(self, status="completed", state="ok") -> Dict[str, Any]:
        return {
            "tool_id": "guide_fact_check", "status": status, "task_status": "completed",
            "data": {"question": QUESTION, "state": state, "answer": GOOD_ANSWER,
                     "steps": list(G.FIVE_STEPS), "follow_up_hint": G.FOLLOW_UP_HINT},
            "gaps": [], "errors": [],
            "audit": {"operations": [], "searches": 0, "pages": 0, "llm_calls": 1},
            "budget": {"used": {"max_llm_calls": 1}, "limits": {}},
            "report": "辟谣方法指南报告（只教方法，不对具体说法下结论）",
            "started_at": CLOCK, "finished_at": CLOCK,
        }

    def test_registered_metadata(self):
        reg = _tool_registry()
        spec = reg.get("guide_fact_check")
        self.assertEqual(spec["params"], ["question"])
        self.assertEqual(spec["permission"], "read")      # 纯知识讲解：不建档
        self.assertIn("guide_fact_check", reg.names())
        self.assertIn("指南", spec["description"])

    def test_question_param_validation(self):
        reg = _tool_registry()
        for bad in ("", "   ", None, 123, ["x"], "问" * 2001):
            with self.subTest(question=bad):
                with self.assertRaises(ValidationError):
                    reg.validate("guide_fact_check", {"question": bad})
        clean = reg.validate("guide_fact_check", {"question": f"  {QUESTION}  "})
        self.assertEqual(clean, {"question": QUESTION})
        junk = reg.validate("guide_fact_check", {"question": QUESTION, "junk": 1})
        self.assertEqual(junk, {"question": QUESTION})

    def test_handler_transcribes_steps_and_answer(self):
        out = self._call({"guide_skills": _stub_skills(self._sample_result())})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["dossier_id"])              # 不建档
        self.assertTrue(out["not_saved"])
        self.assertEqual(out["card"]["card_type"], "guide")
        self.assertEqual(out["card"]["steps"], list(G.FIVE_STEPS))
        self.assertIn(GOOD_ANSWER, out["digest"])         # 讲解逐字转述
        self.assertIn("查证五步法", out["digest"])
        self.assertIn("verify_claim", out["digest"])
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, out["digest"])

    def test_degraded_digest_notes_fallback_and_deadline_passthrough(self):
        degraded = self._sample_result(status="partial", state="degraded")
        degraded["gaps"] = ["模型讲解不可用，使用内置五步法模板"]
        out = self._call({"guide_skills": _stub_skills(degraded)})
        self.assertTrue(out["ok"])                        # partial 是合法出口
        self.assertTrue(out["degraded"])
        self.assertIn("降级", out["digest"])
        self.assertIn("模型讲解不可用", out["digest"])
        # 死线/取消透传
        skills = _stub_skills(self._sample_result())

        def cancel_check():
            return False

        with llm_cache.deadline_scope(30):
            expected = llm_cache.deadline_at()
            self._call({"guide_skills": skills}, cancel_check=cancel_check)
        call = skills.calls[-1]
        self.assertEqual(call["question"], QUESTION)
        self.assertIs(call["cancel_check"], cancel_check)
        self.assertEqual(call["deadline"], expected)
        # run 抛异常 → ok=False + 诚实 digest，handler 永不抛（二道闸）
        broken = _stub_skills(None, raises=RuntimeError("指南爆炸"))
        out2 = self._call({"guide_skills": broken})
        self.assertFalse(out2["ok"])
        self.assertTrue(out2["degraded"])
        self.assertIsNone(out2["card"])
        self.assertIn("不可用", out2["digest"])


if __name__ == "__main__":
    unittest.main()
