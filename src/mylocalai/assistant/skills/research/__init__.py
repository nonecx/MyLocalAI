# -*- coding: utf-8 -*-
"""资料收集技能（research）：主题 → 范围 → 检索 → 读页 → 摘录 → 综述 → 报告。

改了什么、为什么、锁定什么：
  - 新增 `assistant.skills.research` 包，对外只暴露 `ResearchSkills` / `collect_research` /
    `render_report` 与规则加载；两套工作流模式（pipeline / agent）共用同一套收口，
    调用方不需要知道模式差异。
  - 包内 `rules.json` 是额度唯一来源；`scope/`、`digest/`、`agent/` 三个 SKILL.md 是
    阶段提示词素材（口径只在那里维护，JSON 键名由 pipeline 追加）。
  - 不 import web：依赖方向由 tests/test_import_rules.py 锁定。
"""

from __future__ import annotations

from .pipeline import (
    DEFAULT_RESEARCH_RULES,
    RESEARCH_RULES_PATH,
    ROLES,
    SUPPORT_LEVELS,
    ResearchSkills,
    collect_research,
    load_research_rules,
    render_report,
)

__all__ = [
    "DEFAULT_RESEARCH_RULES",
    "RESEARCH_RULES_PATH",
    "ROLES",
    "SUPPORT_LEVELS",
    "ResearchSkills",
    "collect_research",
    "load_research_rules",
    "render_report",
]
