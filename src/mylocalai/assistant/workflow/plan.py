# -*- coding: utf-8 -*-
"""Plan 展开层（《Agent工作流规格-v0.1》§3.2）：计划由代码按已登记能力展开。

"不拼工具"（铁律）：模型/路由只表达 action 与参数，执行图一律由 expand_plan 按注册表
展开成固定步骤；模型不能提交任意工具步骤列表。claim-checking 复用
core.build_claim_checking_plan 的六步固定图（运行期状态推进在 workflow/claim.py）。

顶层约定（主线程波次 2 接线的精确接口）：
  expand_plan(action, args, *, role="full") -> Plan
  Plan.plan_id / Plan.steps（列表） / Plan.to_dict()
  plan_step_tool(step) -> str   # 该步在 registry/loop 层的工具名
"""

from __future__ import annotations

import hashlib
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from mylocalai.assistant.workflow.registry import CAPABILITIES, capability_for_action

__all__ = ["CLAIM_STEPS", "DEFAULT_BUDGET", "StepSpec", "Plan",
           "expand_plan", "new_plan_id", "plan_step_tool"]

#: claim-checking 六阶段静态展开（与 core.claim_workflow.PLAN_STEPS/_DEPS 同序同依赖；
#: 此处只声明静态计划形状，运行期状态推进在 core.ClaimCheckingPlan，两处一致性由
#: tests/test_workflow_engine.py 的对拍用例锁定）。
CLAIM_STEPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("extract_claims", ()),
    ("collect_fact_evidence", ("extract_claims",)),
    ("collect_propagation", ("extract_claims",)),
    ("assess_claims", ("collect_fact_evidence", "collect_propagation")),
    ("validate_answer", ("assess_claims",)),
    ("commit_result", ("validate_answer",)),
)

#: claim-checking 计划的缺省预算：deadline_s=180 与 assistant_rules["turn_deadline_s"]
#: 缺省同口径；queries/pages=8 与 core.build_claim_checking_plan 的缺省一致。
#: 可经 expand_plan 的调用方（波次 2）按 rules 覆盖——本模块不改读配置文件。
DEFAULT_BUDGET: Dict[str, Any] = {"queries": 8, "pages": 8, "deadline_s": 180.0}

#: 非取证类单步计划的预算形状（与 claim 计划 to_dict 对齐）：query/page 预算由各工具
#: 自带口径管理（registry 的 est_s/cost、skills 流水线内预算），计划层如实置 None。
_LIGHT_BUDGET: Dict[str, Any] = {"queries": None, "pages": None, "deadline_s": 180.0}


@dataclass
class StepSpec:
    """计划内一步。action 是 registry/loop 层的工具名；kind 区分单步工具与能力内阶段。"""

    step_id: str
    action: str                      # plan_step_tool 取的就是它
    depends_on: Tuple[str, ...] = ()
    kind: str = "tool"               # "tool"=直接执行的工具步；"phase"=由承载工具一次跑完的阶段

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "action": self.action,
                "depends_on": list(self.depends_on), "kind": self.kind}


@dataclass
class Plan:
    """展开后的执行计划（静态形状；运行期步骤状态在 core.ClaimCheckingPlan / loop）。"""

    plan_id: str
    capability_id: Optional[str]          # 横向工具/未注册 action → None（单步直执）
    capability_version: Optional[str]
    role: str
    budget: Dict[str, Any]
    steps: List[StepSpec] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": 1, "plan_id": self.plan_id,
                "capability_id": self.capability_id,
                "capability_version": self.capability_version,
                "role": self.role, "budget": dict(self.budget),
                "steps": [step.to_dict() for step in self.steps]}


_PLAN_SEQ = itertools.count(1)   # 进程内序号：Windows 时钟粒度粗，纯时间戳会撞号


def new_plan_id(capability_id: str) -> str:
    """plan_id：'<capability>:<短哈希>'。哈希含纳秒时间戳 + 进程内序号，重跑不撞号。"""
    token = hashlib.blake2b(f"{capability_id}|{time.time_ns()}|{next(_PLAN_SEQ)}".encode("utf-8"),
                            digest_size=6).hexdigest()[:12]
    return f"{capability_id}:{token}"


def _claim_plan(action: str, role: str, plan_id: str) -> Plan:
    """claim-checking 六阶段静态展开：承载工具就是本次入口 action（verify_claim /
    recheck_dossier）。每个阶段在运行期由 workflow.claim 执行器经 core.ClaimCheckingPlan
    推进状态；波次 2 的 per-step 循环对同一承载工具的连续 phase 步合并为一次 handler
    调用（六步在工具内部一次跑完，不在 loop 层拆成六次工具调用）。"""
    steps = [StepSpec(step_id=step_id, action=action, depends_on=deps, kind="phase")
             for step_id, deps in CLAIM_STEPS]
    return Plan(plan_id=plan_id, capability_id="claim-checking",
                capability_version=str(CAPABILITIES["claim-checking"].version),
                role=role, budget=dict(DEFAULT_BUDGET), steps=steps)


def expand_plan(action: object, args: Optional[Dict[str, Any]] = None, *,
                role: str = "full") -> Plan:
    """把路由给出的 action 展开成执行计划（args 仅作形状保留，展开只按 action 编程）。

    - 已登记能力的入口 action → 能力计划（claim-checking 六阶段；其余单步）；
    - 横向工具/未注册名（capability_for_action → None）→ 单步 plan（capability_id=None），
      对话跟进类工具由 loop 现有 per-step 模式直接执行。
    角色只作为计划元数据带出（呈现/裁剪的决策在路由与 loop 层，不在展开层）。
    """
    action = str(action or "")
    capability_id = capability_for_action(action)
    role = str(role or "full")
    if capability_id is None:
        return Plan(plan_id=new_plan_id(action or "adhoc"),
                    capability_id=None, capability_version=None, role=role,
                    budget=dict(_LIGHT_BUDGET),
                    steps=[StepSpec(step_id=f"{action or 'tool'}:1", action=action, kind="tool")])
    if capability_id == "claim-checking":
        return _claim_plan(action, role, new_plan_id(capability_id))
    capability = CAPABILITIES[capability_id]
    return Plan(plan_id=new_plan_id(capability_id), capability_id=capability_id,
                capability_version=str(capability.version), role=role,
                budget=dict(_LIGHT_BUDGET),
                steps=[StepSpec(step_id=f"{action}:1", action=action, kind="tool")])


def plan_step_tool(step: Any) -> str:
    """该步在 registry/loop 层的工具名（StepSpec 或 to_dict 后的 dict 都认）。"""
    if isinstance(step, StepSpec):
        return str(step.action or "")
    if isinstance(step, dict):
        return str(step.get("action") or "")
    return str(getattr(step, "action", "") or "")
