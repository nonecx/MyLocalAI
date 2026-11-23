# -*- coding: utf-8 -*-
"""统计说法核查技能（第五波，专用，服务 layperson 角色）：只比对数字口径，不判内容真假。

只导出 StatisticsSkills / check_statistics / render_report / load_rules
（工具层经 services["statistics_skills"] 取用）。判定全部由代码完成
（数值抽取/归一/比对都在 pipeline 内实现），LLM 只做摘录结构化与段落定位；
不建档、不给数值分、不写库。
"""
from .pipeline import StatisticsSkills, check_statistics, load_rules, render_report

__all__ = ["StatisticsSkills", "check_statistics", "load_rules", "render_report"]
