# -*- coding: utf-8 -*-
"""辟谣方法指南技能（通用，零检索纯知识）：教用户查证五步法，不判具体说法真假。

只导出 GuideSkills / guide_fact_check（工具层经 services["guide_skills"] 取用）。
五步法口径代码锁定；模型只写受约束讲解，越界即丢弃并用内置模板降级。
模块内不 import 检索/取页函数，保证零外发检索请求。
"""
from .guide import GuideSkills, guide_fact_check

__all__ = ["GuideSkills", "guide_fact_check"]
