# -*- coding: utf-8 -*-
"""巡检报告技能（platform/report 包）：一轮巡检 → 结构化报告 payload。

公开面只有 PatrolReport（契约冻结）：
  - `_aggregate` 纯代码汇总（零 LLM：计数/分级/排序全部代码算，大模型不参与）；
  - `_narrate` 一次 LLM 调用（SKILL.md 指令 + 机器契约，只做语言组织与逻辑判断）；
  - 代码校验器（安全闸 + 截断 + 白名单）内嵌实现，平台包不 import assistant。

延迟公开面：import 本包不拖拽 pipeline 与其依赖（按需加载，与 platform/__init__.py
同口径）；规则口径在 platform/rules.py 的 report 段与 platform_rules.json。
"""

from __future__ import annotations

__all__ = ["PatrolReport"]

_LAZY = {
    "PatrolReport": ("mylocalai.platform.report.pipeline", "PatrolReport"),
}


def __getattr__(name: str):
    # 延迟公开面：import platform.report 不拖重依赖（pipeline 按需加载）。
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    module = importlib.import_module(target[0])
    return getattr(module, target[1])
