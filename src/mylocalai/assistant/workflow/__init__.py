# -*- coding: utf-8 -*-
"""Agent 工作流编排层（《Agent工作流规格-v0.1》§3：Router、Plan 与 Run）。

三个模块的分工（本包不接管回合循环，loop 现有 per-step 模式照旧消费这里的纯函数）：
  registry  能力注册表：能力 ID/版本/分类/角色/通道/入口 action 的唯一机器可读来源；
  plan      Plan 展开层：expand_plan 按已登记能力把 action 展开成固定步骤计划（模型不拼工具）；
  runner    Run 支撑层：拓扑步序、执行状态归并、任务出口推导、三轴拆分、per-step 持久化包装；
  claim     claim-checking 六步执行器（见 workflow/claim.py，经 tools_verify 接线消费）。

依赖方向：assistant.workflow ← {assistant.registry, core, rumor, classic}，不 import web。
"""

from mylocalai.assistant.workflow.registry import (
    CAPABILITIES,
    HORIZONTAL_TOOLS,
    Capability,
    capability_for_action,
    capabilities,
    entry_action_set,
)
from mylocalai.assistant.workflow.plan import (
    CLAIM_STEPS,
    DEFAULT_BUDGET,
    Plan,
    StepSpec,
    expand_plan,
    new_plan_id,
    plan_step_tool,
)
from mylocalai.assistant.workflow.runner import (
    StepRunRecorder,
    aggregate_execution_status,
    derive_task_status,
    ordered_steps,
    shell_to_three_axis,
)

__all__ = [
    "CAPABILITIES", "HORIZONTAL_TOOLS", "Capability",
    "capability_for_action", "capabilities", "entry_action_set",
    "CLAIM_STEPS", "DEFAULT_BUDGET", "Plan", "StepSpec",
    "expand_plan", "new_plan_id", "plan_step_tool",
    "StepRunRecorder", "aggregate_execution_status", "derive_task_status",
    "ordered_steps", "shell_to_three_axis",
]
