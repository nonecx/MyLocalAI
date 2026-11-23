# -*- coding: utf-8 -*-
"""档案对比技能（专用，dossier-comparison，第五波）：两份档案的只读 diff。

只导出 compare_dossiers / load_rules（工具层 tools_compare 直接调纯函数——
本技能无模型、无网络、不进 _ServiceBox）。全部读取经调用方注入的 store 完成
（get_dossier/conclusions/evidence），与档案库单一事实源保持一致；不重算、
不改写、不建档、无分数；单边读失败折 partial + gaps 明示，缺章按 absent
处理不编造；相似度只标「疑似同源」不断言同一说法。
"""
from .compare import DEFAULT_COMPARISON_RULES, compare_dossiers, load_rules

__all__ = ["compare_dossiers", "load_rules", "DEFAULT_COMPARISON_RULES"]
