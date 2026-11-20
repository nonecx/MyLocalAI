# -*- coding: utf-8 -*-
"""取证三技能（内部执行层，总览 §5）：只读不写，回答"谈什么、去哪查、查到什么"。
只导出 collect_evidence / EvidenceSkills（总览 §5.2）。"""
from .pipeline import EvidenceSkills, collect_evidence
__all__ = ["collect_evidence", "EvidenceSkills"]
