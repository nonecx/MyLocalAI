# -*- coding: utf-8 -*-
"""资料收集技能（契约 §4）：用户给主题，助手主动拆子问题、多轮检索、读页摘录出简报。

只导出 ResearchSkills / collect_research（工具层经 services["research_skills"] 取用，
pipeline.load_rules 从子模块导入）。只述资料、不判真假、不建档、不给数值分：
关键发现必须引用已存在的 evidence_id，只有 unverified 证据的一律降级为线索。
"""
from .pipeline import ResearchSkills, collect_research

__all__ = ["ResearchSkills", "collect_research"]
