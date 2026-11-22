# -*- coding: utf-8 -*-
"""Run 支撑层（《Agent工作流规格-v0.1》§3.3）：纯函数 + 薄包装，不接管回合循环。

loop 现有 per-step 模式照旧消费这里：
  ordered_steps             计划步骤的拓扑序（依赖校验）；
  aggregate_execution_status 步骤状态 → 计划执行状态（与 core.claim_workflow._aggregate 同口径）；
  derive_task_status        从计划与步骤结果推导任务语义出口（task_status 分轴）；
  shell_to_three_axis       skills 流水线外壳/普通工具结果 → 三轴（执行/任务/领域判断）；
  StepRunRecorder           per-step 持久化包装（session_store 扩参版 start/finish_run）。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from mylocalai.assistant.workflow.plan import Plan

__all__ = ["ordered_steps", "aggregate_execution_status", "derive_task_status",
           "shell_to_three_axis", "StepRunRecorder"]

#: 终态步骤状态集合（与 core.claim_workflow._TERMINAL 同口径，含 not_* 三个"未发生"态）。
_TERMINAL = frozenset({"completed", "partial", "failed", "cancelled", "budget_exhausted",
                       "not_triggered", "not_applicable", "not_run"})


# ---------------------------------------------------------------- 步序与状态归并

def _plan_steps(plan: Any) -> List[Any]:
    if isinstance(plan, Plan):
        return list(plan.steps)
    if isinstance(plan, dict):
        return list(plan.get("steps") or [])
    return list(getattr(plan, "steps", None) or [])


def _field(step: Any, name: str) -> Any:
    if isinstance(step, dict):
        return step.get(name)
    return getattr(step, name, None)


def _step_id(step: Any) -> str:
    return str(_field(step, "step_id") or "")


def ordered_steps(plan: Any) -> List[Any]:
    """计划步骤的拓扑序（Kahn，输入序稳定：同时就绪的步按声明顺序先出）。

    未知依赖 / 重复 step_id / 循环依赖 → ValueError：计划展开错误要当场暴露，
    不能留到运行期静默跳步。
    """
    steps = _plan_steps(plan)
    ids = [_step_id(step) for step in steps]
    if len(set(ids)) != len(ids):
        raise ValueError("plan has duplicate step_id")
    deps: Dict[str, List[str]] = {}
    for step in steps:
        raw = _field(step, "depends_on") or ()
        unknown = [str(dep) for dep in raw if str(dep) not in set(ids)]
        if unknown:
            raise ValueError(f"step {_step_id(step)} depends on unknown step: {unknown[0]}")
        deps[_step_id(step)] = [str(dep) for dep in raw]
    out: List[Any] = []
    done: set = set()
    rest = list(steps)
    while rest:
        progressed = False
        for step in list(rest):
            if all(dep in done for dep in deps[_step_id(step)]):
                out.append(step)
                done.add(_step_id(step))
                rest.remove(step)
                progressed = True
        if not progressed:
            raise ValueError("plan has circular dependency")
    return out


def aggregate_execution_status(step_statuses: Iterable[str]) -> str:
    """步骤状态 → 计划执行状态（与 core.claim_workflow._aggregate 同口径的归并优先级）：
    cancelled > budget_exhausted > failed（归并为 partial，required 失败挡下游但计划级
    不说 failed）> partial > 全部终态 → completed > 其余 running。空输入按"尚无上报"返
    running。接受可迭代或 {step_id: status} dict。"""
    values = [str(value) for value in (
        step_statuses.values() if isinstance(step_statuses, dict) else step_statuses) if value]
    if not values:
        return "running"
    if any(value == "cancelled" for value in values):
        return "cancelled"
    if any(value == "budget_exhausted" for value in values):
        return "budget_exhausted"
    if any(value == "failed" for value in values):
        return "partial"
    if any(value == "partial" for value in values):
        return "partial"
    if all(value in _TERMINAL for value in values):
        return "completed"
    return "running"


# ---------------------------------------------------------------- 任务出口推导

def _results_iter(step_results: Any) -> List[Any]:
    if step_results is None:
        return []
    if isinstance(step_results, dict):
        return list(step_results.values())
    if isinstance(step_results, (list, tuple)):
        return list(step_results)
    return []


def _collect_step_statuses(plan: Any, step_results: Any) -> List[str]:
    """步骤状态收集：运行期 ClaimCheckingPlan.to_dict 的 steps 是 {name: {status}}，
    plan.py 静态形状是 [StepSpec/dict]（无状态，从 step_results 补）。"""
    out: List[str] = []
    steps = _plan_steps(plan)
    if steps and isinstance(steps[0], dict) and "status" in steps[0]:
        out.extend(str(step.get("status") or "") for step in steps)  # type: ignore[union-attr]
    elif isinstance(plan, dict) and isinstance(plan.get("steps"), dict):
        out.extend(str((step or {}).get("status") or "")
                   for step in (plan.get("steps") or {}).values()
                   if isinstance(step, dict))
    for row in _results_iter(step_results):
        if isinstance(row, dict) and row.get("status"):
            out.append(str(row["status"]))
    return out


def derive_task_status(plan: Any = None, step_results: Any = None) -> str:
    """任务语义出口推导（task_status 分轴，规格 §3.3）。

    优先级：
      1. 计划级显式 task_status / no_factual_claim（运行期 ClaimCheckingPlan 的 status，
         或 claim 短出口结果）；
      2. 计划 claim_ids 为空列表（claim-checking 无主张）→ no_factual_claim；
      3. 步骤结果里的显式 task_status；
      4. 步骤状态全部 not_applicable / not_triggered → 同名出口；
      5. 其余 → "none"（无特殊任务出口，不代表失败——执行轴另记）。
    """
    if isinstance(plan, dict):
        explicit = str(plan.get("task_status") or "")
        if explicit:
            return explicit
        if plan.get("status") == "no_factual_claim":
            return "no_factual_claim"
        claim_ids = plan.get("claim_ids")
        if isinstance(claim_ids, list) and not claim_ids:
            return "no_factual_claim"
    for row in _results_iter(step_results):
        if isinstance(row, dict):
            explicit = str(row.get("task_status") or "")
            if explicit:
                return explicit
    statuses = [value for value in _collect_step_statuses(plan, step_results) if value]
    if statuses and all(value == "not_applicable" for value in statuses):
        return "not_applicable"
    if statuses and all(value == "not_triggered" for value in statuses):
        return "not_triggered"
    return "none"


# ---------------------------------------------------------------- 三轴拆分

#: skills 流水线外壳的执行态集合（"六种 status"：research/authority 等流水线的统一出口）。
_SHELL_EXECUTION = frozenset({"completed", "partial", "failed", "cancelled", "budget_exhausted"})


def shell_to_three_axis(result: Any) -> Dict[str, Any]:
    """结果 → {execution_status, task_status, assessment} 三轴（规格 §3.3：执行/任务/领域
    判断彼此独立，不用单一 degraded 覆盖取消、超时、失败与合法空结果）。

    - 外壳判据：dict 且带 "status" 与 "tool_id"/"task_status"（research/authority 流水线
      的统一出口形状）。needs_clarification 是任务语义出口（执行本身正常收尾）→ 执行轴
      completed；未知状态原样透传（归并口径是聚合层的决定，不在适配层擅改）。
      assessment 取外壳的领域判断载荷（data/report/conclusion/gaps/errors 中存在的键）。
    - 非外壳（普通工具 dict，如 verify 结果）→ 从 ok/degraded 合成执行轴；assessment 即
      原结果对象。
    - 非 dict → failed + 空判断（坏形状如实降级）。
    """
    if not isinstance(result, dict):
        return {"execution_status": "failed", "task_status": "", "assessment": {}}
    if "status" in result and ("tool_id" in result or "task_status" in result):
        status = str(result.get("status") or "")
        task = str(result.get("task_status") or "")
        if status == "needs_clarification":
            execution = "completed"
            task = task or "needs_clarification"
        elif status in _SHELL_EXECUTION:
            execution = status
        else:
            execution = status or "partial"
        assessment = {key: result[key] for key in
                      ("data", "report", "conclusion", "gaps", "errors") if key in result}
        return {"execution_status": execution, "task_status": task, "assessment": assessment}
    ok = bool(result.get("ok", True))
    if not ok:
        execution = "failed"
    elif bool(result.get("degraded")):
        execution = "partial"
    else:
        execution = "completed"
    return {"execution_status": execution,
            "task_status": str(result.get("task_status") or ""),
            "assessment": result}


# ---------------------------------------------------------------- per-step 持久化

class StepRunRecorder:
    """per-step 持久化薄包装（§3.3"每个 Tool 的运行状态独立记录"）。

    内部走 session_store 扩参版 start_run/finish_run（v9 新列 plan_id/step_id/
    execution_status/task_status）。不吞异常：库故障按 loop 既有语义暴露
    （loop 层对库故障自有 error 兜底），本类只做参数对位。

    主线程波次 2 的调用片段（per-step 循环内）：
        recorder = StepRunRecorder(sessions, session_id)
        run_id = recorder.start(plan_step_tool(step), step_args,
                                plan_id=plan.plan_id, step_id=step_id)
        ...
        recorder.finish(run_id, status, degraded=..., duration_s=...,
                        execution_status=three["execution_status"],
                        task_status=three["task_status"] or None)
    """

    def __init__(self, sessions: Any, session_id: int) -> None:
        self._sessions = sessions
        self._session_id = int(session_id)

    def start(self, tool: str, args: Optional[Dict[str, Any]] = None, *,
              plan_id: Optional[str] = None, step_id: Optional[str] = None) -> int:
        return self._sessions.start_run(self._session_id, str(tool or ""), args or {},
                                        plan_id=plan_id, step_id=step_id)

    def finish(self, run_id: int, status: str, *, degraded: bool = False,
               duration_s: float = 0.0, requests: int = 0, tokens: int = 0,
               dossier_id: Optional[int] = None,
               execution_status: Optional[str] = None,
               task_status: Optional[str] = None) -> None:
        self._sessions.finish_run(run_id, str(status or ""), degraded=degraded,
                                  duration_s=duration_s, requests=requests, tokens=tokens,
                                  dossier_id=dossier_id,
                                  execution_status=execution_status, task_status=task_status)
