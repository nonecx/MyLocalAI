# -*- coding: utf-8 -*-
"""受限 Agent 循环引擎测试（契约 §7）：离线、脚本化动作，不联网不调模型。

锁定（抓真实缺陷的标准）：
  - 正常收尾：`finish` 与 `apply_action.done=True` 都算 completed，steps 形状/编号稳定；
  - 非法连击：坏 JSON、非 dict、动作越界、参数形状不符 → 记非法；连续 3 次停 partial，
    非法尝试也占一个 n（否则报告里看不出模型卡住了几次）；
  - 重复动作：同一 (action,args) 第二次起拒绝执行（键序不同也算同一个），且计非法；
  - 硬上限：max_steps、budget.remaining、budget.blocked、deadline、cancel 到顶即停，
    stop_reason 与 status 对应正确，已执行步骤一条不丢；
  - 异常降级：llm_fn / apply_action / describe_state / on_step 抛错都不得让引擎抛异常，
    分别折成 step error / 状态不可用 / 静默吞掉；
  - 坏结果形状：apply_action 返回非 dict、finish 执行失败 → 诚实标 partial；
  - 预算只读：引擎不改 budget.used（记账归调用方闭包），避免双重扣费。
全离线：llm_fn / apply_action / budget / state / describe_state 全部用桩，零网络零 LLM。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills.agent_loop import (  # noqa: E402
    AGENT_ACTIONS,
    AGENT_STATUSES,
    ILLEGAL_STREAK_LIMIT,
    run_agent_loop,
)

STEP_KEYS = {"n", "action", "args", "ok", "observation", "error"}


# ---------------------------------------------------------------------------
# 桩：脚本化模型、记录型执行闭包、契约同形预算/运行态
# ---------------------------------------------------------------------------

class ScriptedLLM:
    """按脚本顺序返回决策；脚本用尽后重复最后一条。

    条目可以是 dict（裸 obj）、(obj, err) 元组、Exception（当次调用抛错），
    或零参函数（返回上述任意值，用来测试运行中可变条件）。
    """

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        if not self.decisions:
            return None, "empty_script"
        item = self.decisions[min(len(self.calls) - 1, len(self.decisions) - 1)]
        if callable(item) and not isinstance(item, dict):
            item = item()
        if isinstance(item, Exception):
            raise item
        return item


class RecordingApply:
    """记录 (action,args)；可按动作返回自定义结果/抛错/在调用瞬间改运行态。"""

    def __init__(self, results=None, raisers=(), on_call=None):
        self.calls = []
        self.results = dict(results or {})
        self.raisers = set(raisers)
        self.on_call = on_call

    def __call__(self, action, args):
        self.calls.append((action, dict(args)))
        if self.on_call is not None:
            self.on_call(action, args)
        if action in self.raisers:
            raise RuntimeError("stub boom")
        if action in self.results:
            return self.results[action]
        if action == "finish":
            return {"ok": True, "observation": "已收尾", "done": True,
                    "error": "", "step_label": "收尾"}
        return {"ok": True, "observation": f"已执行{action}", "done": False,
                "error": "", "step_label": f"阶段:{action}"}


class FakeBudget:
    """契约 §4 CollectBudget 同形假件（limits/used/remaining/block）。"""

    def __init__(self, **limits):
        self.limits = dict(limits)
        self.used = {key: 0 for key in limits}
        self.blocked = False
        self.blocked_reason = ""

    def remaining(self, key):
        return max(0, int(self.limits.get(key, 0)) - int(self.used.get(key, 0)))

    def allow(self, key, cost=1):
        return self.remaining(key) >= cost

    def why(self, key):
        return f"{key} 已达上限"

    def spend(self, key, cost=1):
        self.used[key] = int(self.used.get(key, 0)) + cost

    def block(self, reason):
        self.blocked = True
        if not self.blocked_reason:
            self.blocked_reason = str(reason or "")


class FakeState:
    """契约 §4 CollectRunState 的最小同形假件。"""

    def __init__(self, *, deadline=None, cancelled=False, budget=None, cancel_check=None):
        self.deadline = deadline
        self.cancelled = cancelled
        self.budget = budget
        self.cancel_check = cancel_check

    def is_cancelled(self):
        return self.cancelled


def run_loop(llm, apply_fn, *, state=None, budget=None, max_steps=8,
             on_step=None, goal="核实一则说法",
             describe_state=lambda: "候选池：0 条；已读：0 页；已摘录：0 条"):
    """统一入口：只暴露被测参数，其余走默认值。"""
    return run_agent_loop(
        llm_fn=llm, system_prompt="你是研究助理", goal=goal,
        describe_state=describe_state, apply_action=apply_fn,
        max_steps=max_steps, budget=budget, state=state, on_step=on_step,
    )


def search(query, **extra):
    args = {"query": query}
    args.update(extra)
    return {"action": "search", "args": args, "reason": "补充检索"}


def finish(args=None):
    return {"action": "finish", "args": dict(args or {}), "reason": "信息已足够"}


# ---------------------------------------------------------------------------
# 正常出口
# ---------------------------------------------------------------------------

class TestAgentLoopHappyPath(unittest.TestCase):
    def test_finish_completes_and_step_shape_is_exact(self):
        llm = ScriptedLLM([search("自来水 水质"), finish({"reason": "覆盖够了"})])
        apply = RecordingApply()
        result = run_loop(llm, apply, max_steps=5)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stop_reason"], "finish")
        self.assertEqual(set(result), {"steps", "stop_reason", "status"})
        self.assertEqual(len(result["steps"]), 2)
        for index, step in enumerate(result["steps"], start=1):
            self.assertEqual(set(step), STEP_KEYS)
            self.assertEqual(step["n"], index)
            self.assertTrue(step["ok"])
        self.assertEqual(result["steps"][0]["action"], "search")
        self.assertEqual(result["steps"][0]["args"], {"query": "自来水 水质"})
        self.assertEqual(result["steps"][1]["action"], "finish")
        self.assertEqual([call[0] for call in apply.calls], ["search", "finish"])
        self.assertIn(result["status"], AGENT_STATUSES)

    def test_done_flag_ends_loop_without_finish(self):
        llm = ScriptedLLM([{"action": "extract", "args": {"quote": "水质达标"}}])
        apply = RecordingApply(results={
            "extract": {"ok": True, "observation": "已摘录", "done": True,
                        "error": "", "step_label": "摘录"}})
        result = run_loop(llm, apply)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stop_reason"], "done")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(len(apply.calls), 1)

    def test_prompt_carries_system_goal_state_schema_and_budget(self):
        llm = ScriptedLLM([finish()])
        budget = FakeBudget(max_llm_calls=4)
        state = FakeState(budget=budget)
        result = run_loop(llm, RecordingApply(), budget=budget, state=state, max_steps=5)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(llm.calls), 1)
        system, user = llm.calls[0]
        self.assertEqual(system, "你是研究助理")
        self.assertIn("核实一则说法", user)
        self.assertIn("候选池：0 条", user)
        self.assertIn("search", user)
        self.assertIn("finish", user)
        self.assertIn("第 1/5 步", user)
        self.assertIn("剩余 4", user)
        # 引擎只读预算：没有替调用方扣任何账（记账归闭包）——否则会被双重扣费。
        self.assertEqual(budget.used, {"max_llm_calls": 0})

    def test_budget_none_means_unbounded(self):
        llm = ScriptedLLM([finish()])
        result = run_loop(llm, RecordingApply(), budget=None)
        self.assertEqual(result["status"], "completed")
        self.assertIn("未设置预算上限", llm.calls[0][1])

    def test_reason_field_is_not_required(self):
        llm = ScriptedLLM([{"action": "finish"}])
        result = run_loop(llm, RecordingApply())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"][0]["args"], {})


# ---------------------------------------------------------------------------
# 非法动作 / 坏 JSON / 参数形状
# ---------------------------------------------------------------------------

class TestAgentLoopIllegalDecisions(unittest.TestCase):
    def test_unknown_action_three_times_stops_partial(self):
        llm = ScriptedLLM([{"action": "browse", "args": {"url": "https://x"}},  # 越界
                           {"action": "browse", "args": {"url": "https://x"}},
                           {"action": "browse", "args": {"url": "https://y"}}])
        apply = RecordingApply()
        result = run_loop(llm, apply, max_steps=10)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stop_reason"], "illegal_actions")
        self.assertEqual(len(result["steps"]), ILLEGAL_STREAK_LIMIT)
        self.assertEqual([step["error"] for step in result["steps"]],
                         ["unknown_action"] * ILLEGAL_STREAK_LIMIT)
        self.assertFalse(any(step["ok"] for step in result["steps"]))
        self.assertEqual(apply.calls, [])
        self.assertEqual([step["n"] for step in result["steps"]], [1, 2, 3])

    def test_bad_json_three_times_records_llm_error(self):
        llm = ScriptedLLM([(None, "bad_json")] * 3)
        result = run_loop(llm, RecordingApply())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stop_reason"], "illegal_actions")
        self.assertEqual([step["error"] for step in result["steps"]], ["bad_json"] * 3)
        self.assertEqual([step["action"] for step in result["steps"]], ["", "", ""])

    def test_non_dict_llm_output_is_illegal(self):
        llm = ScriptedLLM(["不是 JSON 对象"] * 3)
        result = run_loop(llm, RecordingApply())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stop_reason"], "illegal_actions")
        self.assertEqual([step["error"] for step in result["steps"]],
                         ["bad_llm_shape"] * 3)

    def test_missing_action_and_invalid_args_are_distinct(self):
        llm = ScriptedLLM([
            {"args": {"query": "x"}},                          # 缺 action
            {"action": "search", "args": ["x"]},               # args 非对象
            search("x"),                                       # 合法动作，重置连击
            finish(),
        ])
        result = run_loop(llm, RecordingApply())
        errors = [step["error"] for step in result["steps"]]
        self.assertEqual(errors, ["missing_action", "invalid_args", "", ""])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"][1]["action"], "search")

    def test_non_string_action_is_missing_action(self):
        llm = ScriptedLLM([{"action": 7, "args": {}}, finish()])
        result = run_loop(llm, RecordingApply())
        self.assertEqual([step["error"] for step in result["steps"]], ["missing_action", ""])
        self.assertEqual(result["status"], "completed")

    def test_llm_exception_folded_into_step_error(self):
        llm = ScriptedLLM([RuntimeError("provider down")] * 3)
        result = run_loop(llm, RecordingApply())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stop_reason"], "illegal_actions")
        self.assertEqual([step["error"] for step in result["steps"]],
                         ["llm_exception:RuntimeError"] * 3)

    def test_bad_tuple_shape_is_illegal(self):
        llm = ScriptedLLM([("only-one",), ("only-one",), ("only-one",)])
        result = run_loop(llm, RecordingApply())
        self.assertEqual(result["status"], "partial")
        self.assertEqual([step["error"] for step in result["steps"]],
                         ["bad_llm_shape"] * 3)

    def test_invalid_max_steps_stops_immediately(self):
        llm = ScriptedLLM([finish()])
        result = run_loop(llm, RecordingApply(), max_steps="not-a-number")
        self.assertEqual(result["steps"], [])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stop_reason"], "max_steps")
        self.assertEqual(llm.calls, [])

    def test_missing_llm_or_apply_returns_partial(self):
        no_llm = run_agent_loop(
            llm_fn=None, system_prompt="s", goal="g", describe_state=None,
            apply_action=RecordingApply(), max_steps=3, budget=None, state=None)
        self.assertEqual(no_llm, {"steps": [], "stop_reason": "llm_unavailable",
                                  "status": "partial"})
        no_apply = run_agent_loop(
            llm_fn=ScriptedLLM([finish()]), system_prompt="s", goal="g",
            describe_state=None, apply_action=None, max_steps=3,
            budget=None, state=None)
        self.assertEqual(no_apply["stop_reason"], "apply_action_unavailable")
        self.assertEqual(no_apply["status"], "partial")


# ---------------------------------------------------------------------------
# 重复动作
# ---------------------------------------------------------------------------

class TestAgentLoopRepeatGuard(unittest.TestCase):
    def test_same_action_args_rejected_second_time(self):
        repeat = search("自来水 水质", k=5)
        llm = ScriptedLLM([repeat, dict(repeat), finish()])
        apply = RecordingApply()
        result = run_loop(llm, apply)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["steps"]), 3)
        self.assertTrue(result["steps"][0]["ok"])
        self.assertFalse(result["steps"][1]["ok"])
        self.assertEqual(result["steps"][1]["error"], "repeat_action")
        self.assertIn("重复", result["steps"][1]["observation"])
        self.assertEqual(result["steps"][2]["action"], "finish")
        # 重复的那次没有真的执行闭包：候选池/预算不会被同一动作白烧两遍。
        self.assertEqual([call[0] for call in apply.calls], ["search", "finish"])

    def test_arg_key_order_does_not_evade_repeat_guard(self):
        first = {"action": "search", "args": {"query": "x", "k": 3}}   # 键序 A
        second = {"action": "search", "args": {"k": 3, "query": "x"}}  # 键序 B，语义相同
        result = run_loop(ScriptedLLM([first, second, finish()]), RecordingApply())
        self.assertEqual(result["steps"][1]["error"], "repeat_action")

    def test_three_consecutive_repeats_stop_partial(self):
        llm = ScriptedLLM([search("同一动作")] * 4)
        apply = RecordingApply()
        result = run_loop(llm, apply, max_steps=10)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stop_reason"], "illegal_actions")
        self.assertEqual(len(result["steps"]), 4)      # 1 次执行 + 3 次拒绝
        self.assertEqual([step["ok"] for step in result["steps"]],
                         [True, False, False, False])
        self.assertEqual(len(apply.calls), 1)


# ---------------------------------------------------------------------------
# 硬上限：max_steps / 预算 / 死线 / 取消
# ---------------------------------------------------------------------------

class TestAgentLoopHardLimits(unittest.TestCase):
    def test_max_steps_stops_partial(self):
        llm = ScriptedLLM([search(f"查询{i}") for i in range(5)])
        apply = RecordingApply()
        result = run_loop(llm, apply, max_steps=2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stop_reason"], "max_steps")
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(len(apply.calls), 2)
        self.assertEqual(len(llm.calls), 2)

    def test_zero_max_steps_stops_without_calling_model(self):
        llm = ScriptedLLM([finish()])
        result = run_loop(llm, RecordingApply(), max_steps=0)
        self.assertEqual((result["steps"], result["status"], result["stop_reason"]),
                         ([], "partial", "max_steps"))
        self.assertEqual(llm.calls, [])

    def test_budget_exhausted_before_first_step(self):
        llm = ScriptedLLM([finish()])
        budget = FakeBudget(max_llm_calls=0)
        result = run_loop(llm, RecordingApply(), budget=budget, state=FakeState(budget=budget))
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertEqual(result["steps"], [])
        self.assertEqual(llm.calls, [])

    def test_budget_spent_by_closure_is_observed_next_iteration(self):
        budget = FakeBudget(max_llm_calls=5)

        def on_call(action, _args):
            if action == "search":
                budget.spend("max_llm_calls", 5)   # 闭包里记了账

        llm = ScriptedLLM([search("a"), finish()])
        apply = RecordingApply(on_call=on_call)
        result = run_loop(llm, apply, budget=budget)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertEqual(len(result["steps"]), 1)          # 已执行的检索保留
        self.assertEqual([call[0] for call in apply.calls], ["search"])

    def test_budget_blocked_by_closure_stops(self):
        budget = FakeBudget(max_llm_calls=5)
        state = FakeState(budget=budget)

        def on_call(action, _args):
            if action == "search":
                budget.block("检索上限已用尽")

        llm = ScriptedLLM([search("a"), finish()])
        result = run_loop(llm, RecordingApply(on_call=on_call), budget=budget, state=state)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertEqual(len(result["steps"]), 1)

    def test_budget_object_with_only_remaining_probe(self):
        class OnlyRemaining:
            def remaining(self, key):
                return 0

        llm = ScriptedLLM([finish()])
        result = run_loop(llm, RecordingApply(), budget=OnlyRemaining())
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["steps"], [])

    def test_deadline_already_passed_stops_before_llm(self):
        state = FakeState(deadline=time.monotonic() - 1.0)
        llm = ScriptedLLM([finish()])
        result = run_loop(llm, RecordingApply(), state=state)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["stop_reason"], "deadline")
        self.assertEqual(llm.calls, [])

    def test_deadline_reached_mid_run_keeps_prior_steps(self):
        state = FakeState()

        def on_call(action, _args):
            if action == "search":
                state.deadline = time.monotonic() - 1.0   # 第一步之后死线到了

        llm = ScriptedLLM([search("a"), finish()])
        result = run_loop(llm, RecordingApply(on_call=on_call), state=state)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["stop_reason"], "deadline")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["action"], "search")

    def test_cancelled_before_start(self):
        state = FakeState(cancelled=True)
        llm = ScriptedLLM([finish()])
        result = run_loop(llm, RecordingApply(), state=state)
        self.assertEqual((result["steps"], result["status"], result["stop_reason"]),
                         ([], "cancelled", "cancelled"))
        self.assertEqual(llm.calls, [])

    def test_cancelled_mid_run_keeps_prior_steps(self):
        state = FakeState()

        def on_call(action, _args):
            if action == "search":
                state.cancelled = True

        llm = ScriptedLLM([search("a"), finish()])
        result = run_loop(llm, RecordingApply(on_call=on_call), state=state)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["stop_reason"], "cancelled")
        self.assertEqual(len(result["steps"]), 1)

    def test_cancel_check_exception_is_treated_as_not_cancelled(self):
        class BadCancelCheck:
            deadline = None
            budget = None

            def cancel_check(self):
                raise RuntimeError("cancel probe broken")

        llm = ScriptedLLM([finish()])
        result = run_loop(llm, RecordingApply(), state=BadCancelCheck())
        self.assertEqual(result["status"], "completed")

    def test_plain_mapping_state_is_supported(self):
        state = {"cancelled": False, "deadline": None, "budget": None}
        result = run_loop(ScriptedLLM([finish()]), RecordingApply(), state=state)
        self.assertEqual(result["status"], "completed")


# ---------------------------------------------------------------------------
# apply_action / describe_state / on_step 的异常与坏形状
# ---------------------------------------------------------------------------

class TestAgentLoopDegradation(unittest.TestCase):
    def test_apply_action_exception_is_step_error_and_loop_continues(self):
        llm = ScriptedLLM([search("a"), finish()])
        apply = RecordingApply(raisers={"search"})
        result = run_loop(llm, apply)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"][0]["error"], "apply_exception:RuntimeError")
        self.assertFalse(result["steps"][0]["ok"])
        self.assertEqual(result["steps"][1]["action"], "finish")

    def test_finish_exception_marks_partial(self):
        llm = ScriptedLLM([finish()])
        result = run_loop(llm, RecordingApply(raisers={"finish"}))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stop_reason"], "finish_failed")
        self.assertEqual(result["steps"][0]["error"], "apply_exception:RuntimeError")

    def test_finish_not_ok_marks_partial(self):
        llm = ScriptedLLM([finish()])
        apply = RecordingApply(results={
            "finish": {"ok": False, "observation": "", "done": True,
                       "error": "仍有缺口未覆盖", "step_label": "收尾"}})
        result = run_loop(llm, apply)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stop_reason"], "finish_failed")
        self.assertEqual(result["steps"][0]["error"], "仍有缺口未覆盖")

    def test_bad_apply_result_shape_is_step_error(self):
        llm = ScriptedLLM([search("a"), finish()])

        def apply(action, args):
            if action == "search":
                return "不是 dict"
            return {"ok": True, "observation": "收尾", "done": True,
                    "error": "", "step_label": "收尾"}

        result = run_loop(llm, apply)
        self.assertEqual(result["steps"][0]["error"], "bad_apply_result")
        self.assertEqual(result["status"], "completed")

    def test_describe_state_exception_degrades_and_continues(self):
        def broken_state():
            raise RuntimeError("state probe broken")

        llm = ScriptedLLM([finish()])
        result = run_loop(llm, RecordingApply(), describe_state=broken_state)
        self.assertEqual(result["status"], "completed")
        self.assertIn("状态不可用", llm.calls[0][1])

    def test_on_step_failure_does_not_affect_flow(self):
        seen = []

        def on_step(payload):
            seen.append(dict(payload))
            raise RuntimeError("progress channel broken")

        llm = ScriptedLLM([search("a"), finish()])
        result = run_loop(llm, RecordingApply(), on_step=on_step)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(len(seen), 2)
        self.assertEqual([item["n"] for item in seen], [1, 2])
        self.assertTrue(all("step_label" in item for item in seen))
        self.assertEqual(seen[0]["step_label"], "阶段:search")

    def test_on_step_payload_is_isolated_from_steps(self):
        def on_step(payload):
            payload["args"]["query"] = "被回调改掉了"
            payload["n"] = 999

        llm = ScriptedLLM([search("原检索词"), finish()])
        result = run_loop(llm, RecordingApply(), on_step=on_step)
        self.assertEqual(result["steps"][0]["args"], {"query": "原检索词"})
        self.assertEqual(result["steps"][0]["n"], 1)

    def test_on_step_failure_also_applies_to_illegal_steps(self):
        seen = []

        def on_step(payload):
            seen.append(payload)
            raise RuntimeError("boom")

        llm = ScriptedLLM([{"action": "browse"}] * 3)
        result = run_loop(llm, RecordingApply(), on_step=on_step)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(seen), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)