# -*- coding: utf-8 -*-
"""用户能力 Skill 包（总览 §6）：面向用户的能力层，与 skills/ 内部执行层分层。

改了什么、为什么、锁定什么：
  - 新建 capabilities 包，容纳四个用户能力 Skill 的文档：claim_checking（事实核查）、
    evidence_research（证据研究）、topic_understanding（话题理解）、
    speech_understanding（言论理解），各目录一份 SKILL.md。
  - 本包只放"人读"的能力策略文档，不放代码：SKILL.md 是文档不是 Python 模块，
    因此本 __init__ 不 import 任何子目录、不导出符号（__all__ = []）。
  - 机器可执行契约（能力 id/版本/输入 schema/计划步骤/预算/权限）的唯一规范来源是
    assistant.registry 注册表 + assistant.skills.pipeline（《规格》§2 防双维护），
    本包不复制、不派生任何可执行细节，避免两处维护漂移。
  - 依赖方向（tests/test_import_rules.py 锁定）：assistant 及其子包只准 import
    core/llm/retrieval/classic/rumor/attention/dossier/config/util，禁 import web；
    本文件零 import，天然满足。
"""

from __future__ import annotations

__all__ = []
