# -*- coding: utf-8 -*-
"""能力注册表（《Agent工作流规格-v0.1》§2/§3.1）：机器可读的唯一规范来源。

机器可执行的字段（capability_id/version/category/roles/channel/entry_actions/composite）
只在这里维护；人可读的能力策略仍在 capabilities/*/SKILL.md（规格 §2 的分层，两者不互相
复制）。路由/波次 2 的 plan 展开与角色裁剪都按本表编程，不再各自硬编码清单。

横向工具（list_dossiers/get_dossier/export_card/search_web/insights/read_opinions）是
"对话跟进"机制的一部分，**明确不注册**为任何能力的入口——capability_for_action 对它们
返回 None，路由层按跟进语义处理（规格 §1.1"对话跟进是横向机制"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

__all__ = ["Capability", "CAPABILITIES", "HORIZONTAL_TOOLS",
           "capability_for_action", "capabilities", "entry_action_set"]

#: 全部四种档案角色（dossier/profiles.py 的封闭枚举，复值此处按字面写死并注释来源）。
_ALL_ROLES: Tuple[str, ...] = ("layperson", "analyst", "monitor", "full")


@dataclass(frozen=True)
class Capability:
    """一条已登记能力。category："通用"=四角色可用；"专用"=主角色+full。"""

    capability_id: str
    version: str
    category: str                    # "通用" / "专用"
    roles: Tuple[str, ...]           # 适用角色（通用=_ALL_ROLES；专用=主角色+full）
    channel: str                     # "对话" / "面板"（面板复合能力走 REST，不占对话路由枚举）
    entry_actions: Tuple[str, ...]   # 对话入口 action（面板复合能力为空元组）
    composite: bool = False          # True=已登记复合形态（材料分析/领域监测）


#: 十三条能力（规格 §2 表 + 2026-11-20 修订的两个只读对话能力 + 两个面板复合形态
#: + 第五波三个只读对话能力：统计说法核查 / 档案对比 / 事件时间线）。
#: claim-checking 版本 "2" 与 core.ClaimCheckingPlan.to_dict 的 capability_version 同源。
CAPABILITIES: Dict[str, Capability] = {
    # 事实核查：专用能力（主角色 layperson + full）；六步固定图见 core/claim_workflow.py。
    "claim-checking": Capability(
        capability_id="claim-checking", version="2", category="专用",
        roles=("layperson", "full"), channel="对话",
        entry_actions=("verify_claim", "recheck_dossier")),
    # 证据研究：只要证据、不下真假结论。
    "evidence-research": Capability(
        capability_id="evidence-research", version="1", category="通用",
        roles=_ALL_ROLES, channel="对话", entry_actions=("collect_evidence",)),
    # 资料收集（2026-11-20 新增只读对话能力）。
    "information-collection": Capability(
        capability_id="information-collection", version="1", category="通用",
        roles=_ALL_ROLES, channel="对话", entry_actions=("collect_research",)),
    # 权威核验（2026-11-20 新增只读对话能力）。
    "authority-verification": Capability(
        capability_id="authority-verification", version="1", category="通用",
        roles=_ALL_ROLES, channel="对话", entry_actions=("verify_authority",)),
    # 话题理解：热度评估/计算/受控深读。
    "topic-understanding": Capability(
        capability_id="topic-understanding", version="1", category="通用",
        roles=_ALL_ROLES, channel="对话",
        entry_actions=("check_heat", "compute_heat", "deep_read")),
    # 言论理解：表达方式分析（classic 四档）。
    "speech-understanding": Capability(
        capability_id="speech-understanding", version="1", category="通用",
        roles=_ALL_ROLES, channel="对话", entry_actions=("analyze_speech",)),
    # 信源速查。
    "source-credibility": Capability(
        capability_id="source-credibility", version="1", category="通用",
        roles=_ALL_ROLES, channel="对话", entry_actions=("check_source",)),
    # 辟谣指南。
    "fact-check-guide": Capability(
        capability_id="fact-check-guide", version="1", category="通用",
        roles=_ALL_ROLES, channel="对话", entry_actions=("guide_fact_check",)),
    # 材料分析：专业用户 A 的复合形态，面板 + REST 通道，不占对话路由枚举。
    "material-analysis": Capability(
        capability_id="material-analysis", version="1", category="专用",
        roles=("analyst", "full"), channel="面板", entry_actions=(), composite=True),
    # 领域监测：专业用户 B 的复合形态，面板 + REST 通道。
    "domain-monitoring": Capability(
        capability_id="domain-monitoring", version="1", category="专用",
        roles=("monitor", "full"), channel="面板", entry_actions=(), composite=True),
    # 统计说法核查（第五波）：专用能力（主角色 layperson + full）——"这个数对不对、
    # 口径有没有问题"是普通用户工作区的主责（与 claim-checking 核"说法真假"分工：
    # 本技能核数字与统计口径，只读、不建档、不出真假分）。
    "statistical-claim-checking": Capability(
        capability_id="statistical-claim-checking", version="1", category="专用",
        roles=("layperson", "full"), channel="对话",
        entry_actions=("check_statistics",)),
    # 档案对比（第五波）：专用能力（主角色 analyst + full）——两份档案的并排比对是
    # 专业用户·材料分析的主责；确定性纯函数比对（无检索、无模型，故不设模型段）。
    "dossier-comparison": Capability(
        capability_id="dossier-comparison", version="1", category="专用",
        roles=("analyst", "full"), channel="对话",
        entry_actions=("compare_dossiers",)),
    # 事件时间线（第五波）：专用能力（主角色 monitor + full）——把零散事件按时间
    # 排成线是专业用户·领域监测的主责。
    "event-timeline": Capability(
        capability_id="event-timeline", version="1", category="专用",
        roles=("monitor", "full"), channel="对话",
        entry_actions=("event_timeline",)),
}

#: 横向工具（对话跟进机制，不入能力枚举——见模块 docstring）。
HORIZONTAL_TOOLS: Tuple[str, ...] = ("list_dossiers", "get_dossier", "export_card",
                                     "search_web", "insights", "read_opinions")

#: 入口 action → capability_id 的反查表（由 CAPABILITIES 派生，不单独维护）。
_ACTION_MAP: Dict[str, str] = {
    action: capability.capability_id
    for capability in CAPABILITIES.values()
    for action in capability.entry_actions
}


def capability_for_action(action: object) -> Optional[str]:
    """入口 action → capability_id；横向工具/未注册名一律 None（调用方按跟进/未知处理）。"""
    return _ACTION_MAP.get(str(action or ""))


def capabilities() -> Dict[str, dict]:
    """机器可读视图：capability_id → 字段 dict（副本；防调用方改动注册表内部）。"""
    return {
        capability.capability_id: {
            "capability_id": capability.capability_id,
            "version": capability.version,
            "category": capability.category,
            "roles": tuple(capability.roles),
            "channel": capability.channel,
            "entry_actions": tuple(capability.entry_actions),
            "composite": bool(capability.composite),
        }
        for capability in CAPABILITIES.values()
    }


def entry_action_set() -> frozenset:
    """全部入口 action 的封闭集合（路由枚举的合法域，横向工具不在其中）。"""
    return frozenset(_ACTION_MAP)
