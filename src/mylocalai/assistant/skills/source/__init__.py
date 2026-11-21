# -*- coding: utf-8 -*-
"""信源可信度速查技能（通用）：只评信源身份，不评内容真假。

只导出 SourceSkills / check_source（工具层经 services["source_skills"] 取用，
pipeline.load_rules 从子模块导入）。判定全部由代码完成（目录锚点/动态核验信号
复用 collect_core），模型不参与判定；不建档、不给数值分、不写库。
"""
from .pipeline import SourceSkills, check_source

__all__ = ["SourceSkills", "check_source"]
