# -*- coding: utf-8 -*-
"""权威核验技能（契约 §5）：用户给说法/链接，找权威资料证明/反驳并做受约束分析。

只导出 AuthoritySkills / verify_authority（工具层经 services["authority_skills"] 取用，
pipeline.load_rules 从子模块导入）。不判真假、不建档、不给数值分：
balance/lean/conclusion 全部由代码按契约 §5.4 的决策表与模板生成，LLM 只做拆主张、
抽引文与受约束分析。
"""
from .pipeline import AuthoritySkills, verify_authority

__all__ = ["AuthoritySkills", "verify_authority"]
