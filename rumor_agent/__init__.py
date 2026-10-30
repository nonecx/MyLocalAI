# -*- coding: utf-8 -*-
"""基于 LangChain 的网络谣言识别 Agent。

    from rumor_agent import RumorAgent
    result = RumorAgent().analyze("听说自来水被下毒了，速转！")
"""

__all__ = ["RumorAgent", "format_report"]


def __getattr__(name):  # 延迟导入：只用 verdict/patterns 时不必加载 langchain
    if name == "RumorAgent":
        from .agent import RumorAgent
        return RumorAgent
    if name == "format_report":
        from .report import format_report
        return format_report
    raise AttributeError(name)
