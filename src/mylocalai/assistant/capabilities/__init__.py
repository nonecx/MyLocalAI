# -*- coding: utf-8 -*-
"""用户能力 Skill 包（docs/Agent工作流规格-v0.1.md §2 能力表）：面向用户的能力层，与 skills/ 内部执行层分层。

改了什么、为什么、锁定什么：
  - 新建 capabilities 包，容纳用户能力 Skill 的文档：claim_checking（事实核查）、
    evidence_research（证据研究）、topic_understanding（话题理解）、
    speech_understanding（言论理解），各目录一份 SKILL.md。
  - 2026-11-20 角色扩展追加两个复合形态能力（规格 §3.2：已登记复合能力按合同
    自动执行，不新开第五种“执行阶段”能力）：material_analysis（材料分析，
    claim-checking 的批量复合，analyst 主能力）与 domain_monitoring（领域监测，
    topic-understanding 的持续复合，monitor 主能力）。两者都不占对话路由枚举，
    通道是面板 + REST（web/batch_api.py、web/monitor_api.py）。
  - 2026-11-20 第三波再追加两个只读对话能力：information_collection（资料收集，
    collect_research，主动调研主题出带逐字引文的研究简报）与 authority_verification
    （权威核验，verify_authority，逐主张找权威资料给证据倾向与受约束分析）。
    两者均只读、不建档、不出真假结论、不给数值评分，共占对话路由枚举两项
    （router.TOOL_ACTIONS）；至此用户能力共八项（四类基础 + 两类复合形态 +
    两类只读对话）。
  - 2026-11-21 分类与第四波：能力分"通用/专用"两档，落到各 SKILL.md 头部新增的
    "分类/适用角色"两个字段——通用（全部角色可用）与专用（绑定档案工作区主角色：
    claim-checking→layperson、material-analysis→analyst、domain-monitoring→monitor，
    判定语义与角色无关）；skills/ 执行层各 SKILL.md 的 HTML 注释头同步加
    "分类/服务能力"挂接字段（注释会被 pipeline 剥离、不注入模型）。同日再追加两个
    通用只读对话能力：source_credibility（信源可信度速查，check_source，单源画像
    速查）与 fact_check_guide（辟谣方法指南，guide_fact_check，零检索纯知识），
    同样只读、不建档、无分数，共占对话路由枚举两项，机器契约来源为
    skills/source/pipeline.py 与 skills/guide/guide.py（共享口径 skills/collect_core.py）；
    至此用户能力共十项，本目录共 10 份能力文档（四类基础 + 两类复合形态 +
    四类只读对话）。
  - 本包只放“人读”的能力策略文档，不放代码：SKILL.md 是文档不是 Python 模块，
    因此本 __init__ 不 import 任何子目录、不导出符号（__all__ = []）。
  - 机器可执行契约（能力 id/版本/输入 schema/计划步骤/预算/权限）的唯一规范来源是
    assistant.registry 注册表 + assistant.skills.pipeline（《规格》§2 防双维护）；
    本次两项只读对话能力的契约来源另加 skills/research/pipeline.py 与
    skills/authority/pipeline.py（共享口径 skills/collect_core.py）；复合形态能力的
    契约来源另见各自 SKILL.md 头部的“文档性质”段；
    本包不复制、不派生任何可执行细节，避免两处维护漂移。
  - 依赖方向（tests/test_import_rules.py 锁定）：assistant 及其子包只准 import
    core/llm/retrieval/classic/rumor/attention/dossier/config/util，禁 import web；
    本文件零 import，天然满足。
"""

from __future__ import annotations

__all__ = []
