# -*- coding: utf-8 -*-
"""workflow 编排引擎层测试（registry/plan/runner + v9 迁移 + session_store 扩参）。

锁定（Agent工作流规格 §2/§3）：
  - 能力注册表：入口 action ↔ capability 映射（11 入口 + 6 横向工具不注册）；
  - Plan 展开：单步/claim 六步计划形状、plan_id、to_dict、plan_step_tool；
  - ordered_steps 拓扑序与依赖校验；aggregate_execution_status 归并口径；
  - derive_task_status / shell_to_three_axis 三轴拆分；
  - dossier.store v9 迁移（新列存在 + 幂等重跑，仿 test_dossier_retention_schema 模板）；
  - SessionStore.start_run/finish_run 扩参（新列写入、老调用点默认行为逐位不变）。
全部离线（内存库 + 注入 stub，零网络零 LLM）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.workflow import (  # noqa: E402
    CAPABILITIES,
    HORIZONTAL_TOOLS,
    StepRunRecorder,
    aggregate_execution_status,
    capability_for_action,
    capabilities,
    derive_task_status,
    entry_action_set,
    expand_plan,
    ordered_steps,
    plan_step_tool,
    shell_to_three_axis,
)
from mylocalai.assistant.session_store import SessionStore  # noqa: E402
from mylocalai.core.claim_workflow import PLAN_STEPS, _DEPS  # noqa: E402
from mylocalai.dossier.store import SCHEMA_VERSION, connect, migrate  # noqa: E402

#: 规格清单：11 个入口 action（按能力分组）+ 6 个横向工具（对话跟进，不注册）。
_ENTRY_ACTIONS = {
    "claim-checking": ("verify_claim", "recheck_dossier"),
    "evidence-research": ("collect_evidence",),
    "information-collection": ("collect_research",),
    "authority-verification": ("verify_authority",),
    "topic-understanding": ("check_heat", "compute_heat", "deep_read"),
    "speech-understanding": ("analyze_speech",),
    "source-credibility": ("check_source",),
    "fact-check-guide": ("guide_fact_check",),
}


class CapabilityRegistryTests(unittest.TestCase):
    """能力注册表：机器可读契约的封闭性。"""

    def test_all_seventeen_actions_covered(self):
        """17 个 action 全覆盖：11 个入口 → 能力 ID；6 个横向工具 → None。"""
        entry_total = 0
        for capability_id, actions in _ENTRY_ACTIONS.items():
            for action in actions:
                self.assertEqual(capability_for_action(action), capability_id, action)
                entry_total += 1
        for action in HORIZONTAL_TOOLS:
            self.assertIsNone(capability_for_action(action), action)
        self.assertEqual(entry_total, 11)
        self.assertEqual(len(HORIZONTAL_TOOLS), 6)
        # 未注册名同样 None（不猜能力）
        self.assertIsNone(capability_for_action("not_a_tool"))

    def test_entry_action_set_is_closed_and_matches_registry(self):
        """入口集合封闭且与注册表互洽：反查表/集合/能力清单三方一致。"""
        self.assertEqual(entry_action_set(),
                         frozenset(action for actions in _ENTRY_ACTIONS.values()
                                   for action in actions))
        for capability_id, capability in CAPABILITIES.items():
            for action in capability.entry_actions:
                self.assertEqual(capability_for_action(action), capability_id)
        self.assertEqual(len(CAPABILITIES), 10)

    def test_capabilities_machine_view_is_copy_safe(self):
        """机器可读视图是副本：改返回值不影响注册表内部。"""
        view = capabilities()
        view["claim-checking"]["roles"] = ("hacked",)
        self.assertEqual(CAPABILITIES["claim-checking"].roles, ("layperson", "full"))
        # 复合能力的通道/角色口径（面板复合形态不占对话路由枚举）
        self.assertTrue(CAPABILITIES["material-analysis"].composite)
        self.assertEqual(CAPABILITIES["material-analysis"].channel, "面板")
        self.assertEqual(CAPABILITIES["material-analysis"].entry_actions, ())
        self.assertEqual(CAPABILITIES["domain-monitoring"].roles, ("monitor", "full"))
        self.assertEqual(CAPABILITIES["claim-checking"].version, "2")


class ExpandPlanTests(unittest.TestCase):
    """Plan 展开层：单步/claim 计划形状与顶层约定接口。"""

    def test_claim_plan_expands_six_phase_steps_matching_core_deps(self):
        """verify_claim → 六阶段计划：step 序/依赖与 core.claim_workflow 同构，
        每步承载工具都是 verify_claim（kind=phase）。"""
        plan = expand_plan("verify_claim", {"text": "网传……"})
        self.assertEqual(plan.capability_id, "claim-checking")
        self.assertEqual(plan.capability_version, "2")
        self.assertEqual([step.step_id for step in plan.steps], list(PLAN_STEPS))
        for step in plan.steps:
            self.assertEqual(plan_step_tool(step), "verify_claim")
            self.assertEqual(step.kind, "phase")
            self.assertEqual(list(step.depends_on), list(_DEPS[step.step_id]))
        # recheck_dossier 同构（复用同一 builder，承载工具换成 recheck_dossier）
        recheck = expand_plan("recheck_dossier", {"id": 3})
        self.assertEqual([step.step_id for step in recheck.steps], list(PLAN_STEPS))
        self.assertEqual({plan_step_tool(step) for step in recheck.steps}, {"recheck_dossier"})
        # 预算：与 build_claim_checking_plan 缺省及 assistant_rules.turn_deadline_s 同口径
        self.assertEqual(plan.to_dict()["budget"],
                         {"queries": 8, "pages": 8, "deadline_s": 180.0})

    def test_claim_plan_id_prefix_and_to_dict_shape(self):
        """claim 计划 plan_id 前缀 capability；to_dict 形状对齐（schema_version/steps/预算）。"""
        plan = expand_plan("verify_claim", {})
        self.assertTrue(plan.plan_id.startswith("claim-checking:"))
        self.assertNotEqual(plan.plan_id, expand_plan("verify_claim", {}).plan_id)
        payload = plan.to_dict()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["capability_id"], "claim-checking")
        self.assertEqual([step["step_id"] for step in payload["steps"]], list(PLAN_STEPS))
        self.assertEqual(payload["role"], "full")

    def test_single_step_capability_plan_and_horizontal_tool_plan(self):
        """普通能力 → 单步计划（capability 版本来自注册表）；横向工具 → capability_id=None。"""
        plan = expand_plan("check_heat", {"topic": "某话题"})
        self.assertEqual(plan.capability_id, "topic-understanding")
        self.assertEqual(plan.capability_version, "1")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan_step_tool(plan.steps[0]), "check_heat")
        self.assertEqual(plan.steps[0].kind, "tool")
        self.assertEqual(plan.steps[0].depends_on, ())
        self.assertEqual(set(plan.to_dict()["budget"]),
                         {"queries", "pages", "deadline_s"})
        adhoc = expand_plan("search_web", {"query": "x"})
        self.assertIsNone(adhoc.capability_id)
        self.assertIsNone(adhoc.capability_version)
        self.assertEqual([plan_step_tool(step) for step in adhoc.steps], ["search_web"])
        self.assertTrue(adhoc.plan_id.startswith("search_web:"))

    def test_plan_step_tool_accepts_dict_and_spec(self):
        """plan_step_tool 对 StepSpec 与 to_dict 后的 dict 都认（波次 2 消费形状）。"""
        step = expand_plan("collect_evidence", {}).steps[0]
        self.assertEqual(plan_step_tool(step), "collect_evidence")
        self.assertEqual(plan_step_tool(step.to_dict()), "collect_evidence")
        self.assertEqual(plan_step_tool({"action": "get_dossier"}), "get_dossier")
        self.assertEqual(plan_step_tool({}), "")


class OrderedStepsTests(unittest.TestCase):
    """拓扑步序与依赖校验。"""

    def test_claim_plan_topological_order(self):
        """claim 计划拓扑序 = PLAN_STEPS 序（提取 → 两路并行采集 → 评估 → 校验 → 落库）。"""
        plan = expand_plan("verify_claim", {})
        self.assertEqual([step.step_id for step in ordered_steps(plan)], list(PLAN_STEPS))

    def test_dependency_order_respected_for_custom_plan(self):
        """乱序声明的依赖计划也能排出合法序（同时就绪按输入序稳定）。"""
        plan = {
            "steps": [
                {"step_id": "c", "action": "t3", "depends_on": ["b"]},
                {"step_id": "a", "action": "t1", "depends_on": []},
                {"step_id": "b", "action": "t2", "depends_on": ["a"]},
            ]
        }
        self.assertEqual([step["step_id"] for step in ordered_steps(plan)], ["a", "b", "c"])

    def test_unknown_dependency_and_cycle_raise(self):
        """未知依赖/循环依赖当场 ValueError（计划展开错误不静默留到运行期）。"""
        with self.assertRaisesRegex(ValueError, "unknown step"):
            ordered_steps({"steps": [{"step_id": "a", "action": "t",
                                      "depends_on": ["ghost"]}]})
        with self.assertRaisesRegex(ValueError, "circular"):
            ordered_steps({"steps": [
                {"step_id": "a", "action": "t", "depends_on": ["b"]},
                {"step_id": "b", "action": "t", "depends_on": ["a"]}]})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ordered_steps({"steps": [
                {"step_id": "a", "action": "t", "depends_on": []},
                {"step_id": "a", "action": "t", "depends_on": []}]})


class AggregateStatusTests(unittest.TestCase):
    """执行状态归并：与 core.claim_workflow._aggregate 同口径。"""

    def test_merge_priority_matches_claim_workflow(self):
        """cancelled > budget_exhausted > failed(→partial) > partial > 全终态 → completed。"""
        self.assertEqual(aggregate_execution_status([]), "running")
        self.assertEqual(aggregate_execution_status(["completed"]), "completed")
        self.assertEqual(aggregate_execution_status(["completed", "not_applicable"]),
                         "completed")
        self.assertEqual(aggregate_execution_status(["completed", "partial"]), "partial")
        self.assertEqual(aggregate_execution_status(["completed", "failed"]), "partial")
        self.assertEqual(aggregate_execution_status(["failed", "budget_exhausted"]),
                         "budget_exhausted")
        self.assertEqual(aggregate_execution_status(["failed", "cancelled"]), "cancelled")
        self.assertEqual(aggregate_execution_status(["completed", "running"]), "running")
        # dict 输入（{step_id: status}）同样接受
        self.assertEqual(aggregate_execution_status({"a": "completed", "b": "not_run"}),
                         "completed")


class ThreeAxisTests(unittest.TestCase):
    """三轴拆分（执行/任务/领域判断）。"""

    def test_skills_shell_maps_needs_clarification_to_task_axis(self):
        """流水线外壳：needs_clarification 是任务出口，执行轴 completed。"""
        shell = {"tool_id": "collect_research", "status": "needs_clarification",
                 "task_status": "needs_clarification", "data": {"sub_questions": []},
                 "report": "", "gaps": ["需要用户补充"]}
        three = shell_to_three_axis(shell)
        self.assertEqual(three["execution_status"], "completed")
        self.assertEqual(three["task_status"], "needs_clarification")
        self.assertEqual(three["assessment"]["gaps"], ["需要用户补充"])

    def test_shell_execution_statuses_pass_through(self):
        """外壳六态的执行态原样映射；未知状态透传不在适配层归并。"""
        for status in ("completed", "partial", "failed", "cancelled", "budget_exhausted"):
            shell = {"tool_id": "t", "status": status, "task_status": ""}
            self.assertEqual(shell_to_three_axis(shell)["execution_status"], status)
        self.assertEqual(shell_to_three_axis({"tool_id": "t", "status": "blocked"})["execution_status"],
                         "blocked")

    def test_plain_tool_result_synthesized_from_ok_and_degraded(self):
        """非外壳（普通工具 dict）从 ok/degraded 合成执行轴；assessment 即原对象。"""
        self.assertEqual(shell_to_three_axis({"ok": True, "label": "x"})["execution_status"],
                         "completed")
        out = shell_to_three_axis({"ok": True, "degraded": True})
        self.assertEqual(out["execution_status"], "partial")
        self.assertEqual(out["assessment"]["degraded"], True)
        self.assertEqual(shell_to_three_axis({"ok": False})["execution_status"], "failed")
        self.assertEqual(shell_to_three_axis(None)["execution_status"], "failed")


class DeriveTaskStatusTests(unittest.TestCase):
    """任务语义出口推导（task_status 分轴）。"""

    def test_runtime_claim_plan_no_factual_claim(self):
        """运行期 ClaimCheckingPlan.to_dict（claim_ids 空）→ no_factual_claim。"""
        from mylocalai.core.claim_workflow import build_claim_checking_plan
        payload = build_claim_checking_plan("p", []).to_dict()
        self.assertEqual(derive_task_status(payload), "no_factual_claim")

    def test_explicit_and_step_result_statuses(self):
        """计划级显式 task_status 优先；其次步骤结果显式值；全 not_applicable → 同名出口。"""
        self.assertEqual(derive_task_status({"task_status": "needs_clarification"}),
                         "needs_clarification")
        self.assertEqual(
            derive_task_status({"steps": []},
                               {"extract_claims": {"task_status": "not_applicable"}}),
            "not_applicable")
        plan = {"steps": [{"step_id": "s", "status": "not_applicable"}]}
        self.assertEqual(derive_task_status(plan), "not_applicable")
        plan_not_triggered = {"steps": [{"step_id": "s", "status": "not_triggered"}]}
        self.assertEqual(derive_task_status(plan_not_triggered), "not_triggered")
        self.assertEqual(derive_task_status({"steps": [{"step_id": "s", "status": "completed"}]}),
                         "none")


class MigrationV9Tests(unittest.TestCase):
    """dossier.store v9 迁移（仿 test_dossier_retention_schema 模板，内存库）。"""

    def test_v9_adds_orchestration_columns_and_is_idempotent(self):
        conn = connect(":memory:")
        try:
            self.assertEqual(SCHEMA_VERSION, 9)
            self.assertEqual(migrate(conn), SCHEMA_VERSION)
            self.assertEqual(migrate(conn), 0)   # 幂等：重跑零迁移
            got = {row["name"] for row in conn.execute("PRAGMA table_info(tool_runs)")}
            self.assertTrue({"plan_id", "step_id", "execution_status", "task_status"}
                            .issubset(got))
            # 旧库（v8）迁移到 v9：不破坏既有行
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0], 0)
        finally:
            conn.close()


class SessionStoreExtendedRunTests(unittest.TestCase):
    """session_store 扩参：新列写入 + 老调用点默认行为逐位不变。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = SessionStore(Path(self.tmp.name) / "chat.db")
        self.session_id = self.store.create_session("t")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_start_finish_with_orchestration_columns(self):
        """start_run(plan_id=..., step_id=...) / finish_run(execution_status=..., task_status=...)
        写入新列（v9），get_run 读回保真。"""
        run_id = self.store.start_run(self.session_id, "verify_claim", {"text": "x"},
                                      plan_id="claim-checking:abc123",
                                      step_id="collect_fact_evidence")
        run = self.store.get_run(run_id)
        self.assertEqual(run["plan_id"], "claim-checking:abc123")
        self.assertEqual(run["step_id"], "collect_fact_evidence")
        self.assertIsNone(run["execution_status"])
        self.store.finish_run(run_id, "ok", degraded=True, duration_s=1.5,
                              execution_status="partial", task_status="no_factual_claim")
        run = self.store.get_run(run_id)
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["execution_status"], "partial")
        self.assertEqual(run["task_status"], "no_factual_claim")

    def test_legacy_callsites_keep_old_behavior(self):
        """老调用点（不带新参）行为逐位不变：新列保持 NULL；COALESCE 不抹已有值。"""
        legacy_run = self.store.start_run(self.session_id, "check_heat", {"topic": "x"})
        run = self.store.get_run(legacy_run)
        self.assertIsNone(run["plan_id"])
        self.assertIsNone(run["step_id"])
        self.store.finish_run(legacy_run, "ok")
        run = self.store.get_run(legacy_run)
        self.assertEqual(run["status"], "ok")
        self.assertIsNone(run["execution_status"])
        self.assertIsNone(run["task_status"])
        # 重复 finish：不带两轴的收尾不抹掉之前写入的值（与 dossier_id 同 COALESCE 语义）
        self.store.finish_run(legacy_run, "ok", execution_status="completed",
                              task_status="not_applicable")
        self.store.finish_run(legacy_run, "ok")
        run = self.store.get_run(legacy_run)
        self.assertEqual(run["execution_status"], "completed")
        self.assertEqual(run["task_status"], "not_applicable")

    def test_step_run_recorder_writes_plan_and_step(self):
        """StepRunRecorder：per-step 持久化包装把 plan_id/step_id/两轴写进 tool_runs。"""
        recorder = StepRunRecorder(self.store, self.session_id)
        run_id = recorder.start("verify_claim", {"text": "y"},
                                plan_id="claim-checking:p1", step_id="validate_answer")
        recorder.finish(run_id, "ok", duration_s=0.2,
                        execution_status="completed", task_status="")
        run = self.store.get_run(run_id)
        self.assertEqual(run["tool"], "verify_claim")
        self.assertEqual((run["plan_id"], run["step_id"]),
                         ("claim-checking:p1", "validate_answer"))
        self.assertEqual(run["execution_status"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
