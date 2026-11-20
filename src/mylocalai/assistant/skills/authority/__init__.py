# -*- coding: utf-8 -*-
"""权威核验技能包（authority-verification，工具 verify_authority）。

改了什么、为什么：新增 `assistant.skills.authority` 子包，对外只暴露三个稳定入口——
`AuthoritySkills`（流水线类，pipeline / agent 两种模式）、`verify_authority`（一行调用）、
`render_report`（确定性纯文本报告）；`decide_verdict` 也导出，方便调用方与测试直接对
balance/lean 决策表做单元校验（它只做纯计算，不碰 IO）。

锁定：包内不 import web / platform；结论模板与枚举只在这一处实现，避免上层再写一套。
"""

from __future__ import annotations

from .pipeline import (
    AuthoritySkills,
    decide_verdict,
    render_report,
    verify_authority,
)

__all__ = ["AuthoritySkills", "verify_authority", "render_report", "decide_verdict"]
