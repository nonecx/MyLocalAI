# -*- coding: utf-8 -*-
"""用户能力 Skill 包（总览 §6）：面向用户的能力层，与 skills/ 内部执行层分层。

改了什么、为什么、锁定什么：
  - 新建 capabilities 包，容纳用户能力 Skill 的文档：claim_checking（事实核查）、
    evidence_research（证据研究）、topic_understanding（话题理解）、
    speech_understanding（言论理解），各目录一份 SKILL.md。
  - 2026-11-20 角色扩展追加两个复合形态能力（规格 §3.2：已登记复合能力按合同
    自动执行，不新开第五种“执行阶段”能力）：material_analysis（材料分析，
    claim-checking 的批量复合，analyst 主能力）与 domain_monitoring（领域监测，
    topic-understanding 的持续复合，monitor 主能力）。两者都不占对话路由枚举，
    通道是面板 + REST（web/batch_api.py、web/monitor_api.py）。
  - 2026-11-21 再追加两个只读用户能力：information_collection（资料收集，工具
    collect_research）与 authority_verification（权威核验，工具 verify_authority），
    并由 collect_and_verify 提供“一句话同时收集+核验”的复合形态；两者都占对话路由枚举。
  - 本包只放"人读"的能力策略文档，不放代码：SKILL.md 是文档不是 Python 模块（资料收集/
    权威核验的可执行契约分别在 skills/research/pipeline.py 与 skills/authority/pipeline.py），
    因此本 __init__ 不 import 任何子目录、不导出符号（__all__ = []）。
  - 机器可执行契约（能力 id/版本/输入 schema/计划步骤/预算/权限）的唯一规范来源是
    assistant.registry 注册表 + assistant.skills.pipeline（《规格》§2 防双维护），
     复合形态能力的契约来源另见各自 SKILL.md 头部的“文档性质”段；
    本包不复制、不派生任何可执行细节，避免两处维护漂移。
  - 依赖方向（tests/test_import_rules.py 锁定）：assistant 及其子包只准 import
    core/llm/retrieval/classic/rumor/attention/dossier/config/util，禁 import web；
    本文件零 import，天然满足。
"""

from __future__ import annotations

__all__ = []
