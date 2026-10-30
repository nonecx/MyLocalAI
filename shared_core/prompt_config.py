# -*- coding: utf-8 -*-
"""提示词与枚举白名单的外部配置加载。

背景（2026-10-22 检验报告发现 G8(b)）：三模块的 System Prompt、报告模板、
枚举白名单此前都写死在各自的 .py 里，换行业/换口径要改 Python。
现在它们外置到 `rumor_agent/prompts.json`，并支持覆盖：

    RUMOR_PROMPTS_PATH        整套替换（默认 rumor_agent/prompts.json）
    RUMOR_PROMPTS_EXTRA_PATH  增量覆盖（深合并：字典递归、列表取并集）

任何一步失败都退回内置默认（代码里的 `*_BUILTIN` 常量），并留下 `warnings()`
——与 `rumor_agent/rules.py` 的"退回默认 + 告警"同一取向。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from shared_core import config_loader

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PROMPTS_PATH = os.path.join(_REPO_ROOT, "rumor_agent", "prompts.json")

#: 默认文件里必须存在的段（整套替换时漏段要留告警）。
_REQUIRED_SECTIONS = ("classic_emotion", "classic_aggression", "classic_logic", "report")

_WARNINGS: Dict[str, str] = {}


def warnings() -> List[str]:
    """本次加载配置时出现的问题（为空表示一切正常）。"""
    return list(_WARNINGS.values())


def load_prompts() -> Dict[str, Any]:
    """读提示词配置。失败退回内置默认（各模块的 `*_BUILTIN` 常量）并留告警。

    注意：这里**不**内嵌提示词文本——内置默认值留在各模块的 `*_BUILTIN` 常量里，
    默认文件 `rumor_agent/prompts.json` 与它们逐字一致（由
    `tests/test_prompt_config.py` 保证不漂移）。
    """
    data, warnings = config_loader.load_config(
        DEFAULT_PROMPTS_PATH, builtin={},
        required_sections=_REQUIRED_SECTIONS,
        env_full="RUMOR_PROMPTS_PATH", env_extra="RUMOR_PROMPTS_EXTRA_PATH")
    _WARNINGS.clear()
    _WARNINGS.update(warnings)
    return data


def get_prompt(section: str, builtin: str) -> str:
    """取某段提示词：配置里有就用配置的，否则用内置默认。"""
    value = (load_prompts().get(section) or {}).get("system")
    return str(value) if value else builtin


def get_whitelist(section: str, key: str, builtin) -> frozenset:
    """取某段枚举白名单：配置里有就用配置的，否则用内置默认。"""
    value = ((load_prompts().get(section) or {}).get("whitelists") or {}).get(key)
    if isinstance(value, (list, tuple)) and value:
        return frozenset(str(x) for x in value)
    return frozenset(builtin)


__all__ = ["load_prompts", "get_prompt", "get_whitelist", "warnings", "DEFAULT_PROMPTS_PATH"]
