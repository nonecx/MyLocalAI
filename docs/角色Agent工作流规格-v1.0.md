# 角色 Agent 工作流 · 实施规格 v1.0（2026-11-22）

> 状态：已实施（分支 `feature/role-agent-workflows`）。本文是"角色 Agent 工作流"这轮
> 改动的**人读实施规格**：记录决策、四角色工作流、编排引擎架构、角色权限层、
> 去数值分层口径、测试矩阵与已知局限。体例沿《信息收集与权威核验技能-2026-11-20.md》。
> 范围：新包 `src/mylocalai/assistant/workflow/`（编排引擎）+ 路由/loop 角色接线 +
> 持久化 v9 + 两个面板 REST 角色闸 + 对话界面全面去数值；**不动判定语义**
> （`verdict.assess` 纯函数、RumorAgent 引擎 SYSTEM_PROMPT）与 `/api/analyze` 契约。
> 上游依据：《Agent工作流规格-v0.1.md》（§3 Router/Plan/Run）、《Skill分类与提示词规范
> -v1.0.md》（通用/专用分类）、《用户角色与Profile规格-v1.0.md》（四角色工作区）。
> 原则延续：**模型不拼工具**；**判定语义全角色一致**；**每个出口都有降级**；
> 白名单 fail-open 回落既有行为；全离线可测（测试一律 stub）。

## 1. 决策记录

| # | 决策 | 理由 |
|---|---|---|
| D1 | 编排为"双层"：路由 LLM 枚举仍是 action 名（tool_start/前端/测试零破坏），capability_id 由注册表从 entry_action **纯代码映射** | `registry.capability_for_action` 反查表由 CAPABILITIES 派生（不单独维护）；"Router 产出 capability 封闭结构"由代码达成，不靠模型输出 capability_id |
| D2 | claim-checking 保守接线：`collect_fact_evidence` 内部调 `RumorAgent.analyze` **完整链**（保留 langchain 检索/读原文/解释），编排器只管 plan 生命周期与三轴，物理执行合并波次 | 核查质量零回归；六步固定图复用 `core.build_claim_checking_plan`；旧引擎无 `source_span`/`quote_span` → 走 `compile_answer` partial + legacy 诚实出口，不冒充五态结论 |
| D3 | 传播取证首次接入（v0.1 规格 Phase 2 落地）：授权闸 **fail-closed**（读不到偏好=不授权，零外发），失败/超预算/取消折 partial 级联不阻断 | 传播是补充面：deadline 份额 25% 且封顶 30s（`_PROPAGATION_SHARE`/`_PROPAGATION_CAP_S`），不挤占事实取证主预算；未授权步骤落 `not_triggered`/`external_processing_disabled`，摘要轴记 `not_applicable` |
| D4 | 横向工具裁定（讨论汇总开放问题 #5）：`list_dossiers/get_dossier/export_card/search_web/insights/read_opinions` = **横向对话跟进机制**，不注册能力、不按角色裁剪；`deep_read` 归 topic-understanding 能力入口 | 跟进机制（v0.1 §1 第 1 条）；`registry.HORIZONTAL_TOOLS` 封闭列举，`capability_for_action` 对它们返回 None，计划层按单步直执处理 |
| D5 | 角色白名单 **fail-open**（缺段/缺角色/非列表/空 → 回落全局 `tool_allowlist`）+ 专用工具裁剪语义 = skill 分类：通用 7 能力全角色可用，专用 claim-checking→layperson+full；面板闸 analyst/monitor | 缺配置时行为与加角色前逐位一致；`role="full"` 恒全量（不读配置）；单条说法核查是普通用户工作区的主责 |
| D6 | 数值口径**分层**：界面去、数据层留档；置信度档位徽章/E 分/证据计数保留 | 对话卡是呈现层（MCP 同核对照/web 事件流/result 三处同一份）；档案库结论行、REST、导出仍留档 `risk_score` 兼容字段；E 分是确定性公式的代理估计（非判定分），estimated 标注已有 |
| D7 | 白名单外 action 走**既有"枚举外 → clarify"降级**；防 clarify 风暴靠路由模型目录按角色过滤（模型看不到被裁剪的工具） | 不新开降级通道；`_router_system` 的工具目录按 `registry.catalog(allowlist=...)` 过滤，从源头让模型不产出越权 action |

## 2. 四角色工作流

四角色沿用《用户角色与Profile规格-v1.0.md》的档案工作区模型；本轮把"角色"从呈现层
扩展到**路由枚举 + 编排计划 + 面板服务端闸**三层。

### 2.1 layperson 普通用户（核查主线 · 简化语气）

```
消息 → router.route(role="layperson")：白名单 17 工具 + 3 元动作（含专用 verify_claim/recheck_dossier）
    → expand_plan：verify_claim/recheck_dossier → claim-checking 六步计划
    → loop 折叠为一次承载工具调用 → workflow.claim 六步执行器
    → 卡片去数值 + presentation="simple"（简化标题/语气，narrate 禁数值）
```

| 能力 | 编排 | 出口姿态 |
|---|---|---|
| claim-checking〔专用〕 | verify_claim / recheck_dossier → 六步计划（提取→事实取证→传播→评估→校验→提交） | 人话结论先行；无主张短出口不调取证；卡片无 risk_score |
| 通用 7 能力 | 各自单步计划 | 只述证据/档位，不下真假结论 |
| 面板（材料批次/领域监测） | 无权限（403） | addon 一句话导流："那是专业用户工作区的功能，顶栏可切换" |

### 2.2 analyst 专业用户·材料分析（批量面板 + 通用研究）

```
批量材料："我收集了一批材料/帮我批量查" → addon 导流左栏「材料批次」面板
    → POST /api/batch（角色闸 role∈{analyst,full}，403 detail 见 §4.2）
    → 逐条 RumorAgent.analyze + 建档 → 汇总卡（计数与分布）
对话内：通用 7 能力 + 横向工具（白名单 15 工具，无 verify_claim/recheck_dossier）
```

| 能力 | 编排 | 出口姿态 |
|---|---|---|
| material-analysis〔专用·面板·复合〕 | REST 通道，不占对话枚举 | 汇总转述只复述汇总卡计数与分布，不新增/改写条目结论 |
| 通用 7 能力 | 单步计划 | 可带档案号与 as_of，展开关键证据/缺口/降级原因 |
| 专用核查工具 | 白名单裁剪 | 用户明确只查一条 → addon 引导去材料批次面板或普通用户工作区 |

### 2.3 monitor 专业用户·领域监测（订阅调度 + 话题理解）

```
盯领域："帮我盯着某领域/有新消息提醒我" → addon 导流左栏「领域监测」面板
    → 订阅 CRUD + run-now（角色闸 role∈{monitor,full}）→ 调度线程到点跑检
    → 变化卡（URL 差集 + E 分首末差，只在面板呈现）
对话内：check_heat/compute_heat/deep_read 回答话题热度（白名单 15 工具）
```

| 能力 | 编排 | 出口姿态 |
|---|---|---|
| domain-monitoring〔专用·面板·复合〕 | REST + 调度通道，不占对话枚举 | 只述检索结果与变化，不做真假断言；E 分注明代理估计 |
| topic-understanding〔通用〕 | check_heat/compute_heat/deep_read | 热度不代表真实性 |
| 专用核查工具 | 白名单裁剪 | addon："单条说法的直接核查不在本工作区对话内，可切普通用户工作区" |

### 2.4 full 全量

白名单 17 工具 + 3 元动作（与 layperson 同集）；`roles.full = {}` → 提示词基座原样注入；
两个面板服务端闸都放行（full 同时在 {analyst,full} 与 {monitor,full} 内）；
路由行为与加角色前逐位一致（`role="full"` 恒走全局 `tool_allowlist`）。

### 2.5 导流矩阵（router addon 的封闭口径；"无专属指令"= 按封闭枚举路由）

| 用户表达 | layperson | analyst | monitor | full |
|---|---|---|---|---|
| 单条说法核查 | verify_claim 就地核查（addon 默认优先） | addon 导流：材料批次面板或普通用户工作区 | addon 导流：切普通用户工作区 | 就地核查（白名单含专用工具，无 addon） |
| 批量材料 | addon 一句话说明是专业用户·材料分析工作区功能 | addon 导流左栏「材料批次」面板 | 无专属 addon 指令 | 无 addon；服务端面板闸放行（analyst,full） |
| 盯领域/订阅 | addon 一句话说明是专业用户·领域监测工作区功能 | 无专属 addon 指令 | addon 导流左栏「领域监测」面板 | 无 addon；服务端面板闸放行（monitor,full） |
| 通用 7 能力 | 就地执行 | 就地执行 | 就地执行 | 就地执行 |

## 3. 编排引擎架构（新包 `src/mylocalai/assistant/workflow/`）

```
用户消息 → router.route(..., role)            # 枚举=角色白名单∪META；目录按角色过滤
        → loop._route_plan(..., role)          # 透传角色；expand_plan 代码展开
        → workflow/plan.py  expand_plan        # 模型不拼工具：action → 固定步骤图
        → workflow/runner.py ordered_steps     # Kahn 拓扑；StepRunRecorder 落库
        → workflow/claim.py execute_claim_checking   # 仅 claim-checking
        → loop per-step 持久化（plan_id/step_id/execution_status/task_status，v9）
```

- **registry.py**：`CAPABILITIES` 10 条（frozen dataclass：capability_id/version/
  category（通用|专用）/roles/channel（对话|面板）/entry_actions/composite）。映射：
  claim-checking v2 专用 (layperson,full) entry=`("verify_claim","recheck_dossier")`；
  evidence-research→`("collect_evidence",)`；information-collection→`("collect_research",)`；
  authority-verification→`("verify_authority",)`；topic-understanding→
  `("check_heat","compute_heat","deep_read")`；speech-understanding→`("analyze_speech",)`；
  source-credibility→`("check_source",)`；fact-check-guide→`("guide_fact_check",)`；
  material-analysis 专用 (analyst,full) 面板 composite；domain-monitoring 专用
  (monitor,full) 面板 composite；`HORIZONTAL_TOOLS` 6 个横向工具明确不注册（D4）。
- **plan.py**：`expand_plan(action, args, *, role="full")` 代码展开——claim-checking
  六步 phase 计划复用 `core/claim_workflow.py` 的 `build_claim_checking_plan`；其余
  单步（横向工具/未注册名 → capability_id=None 单步直执）。`CLAIM_STEPS` 与
  `core.PLAN_STEPS`/`_DEPS` 对拍锁定（test_workflow_engine）。`new_plan_id` =
  `'<capability>:<短哈希>'`（纳秒时间戳 + 进程内序号，重跑不撞号）。
  `DEFAULT_BUDGET = {queries: 8, pages: 8, deadline_s: 180}`。
- **runner.py**：`ordered_steps`（Kahn 拓扑，未知依赖/重复 step_id/循环 → ValueError
  当场暴露）；`aggregate_execution_status`（cancelled > budget_exhausted > failed→partial
  > partial > 全部终态 completed > running，与 `core.claim_workflow._aggregate` 同口径）；
  `derive_task_status`（显式 task_status > no_factual_claim > 步骤显式 > 全 not_* >
  "none"）；`shell_to_three_axis`（skills 六态外壳 → {execution_status, task_status,
  assessment}，needs_clarification 是任务出口、执行轴 completed）；`StepRunRecorder`
  （start/finish 带 plan_id/step_id/两轴的持久化薄包装）。
- **claim.py**：`execute_claim_checking(text, *, deep, verify_fn, propagate_fn, dossiers,
  profile_id, cancel_check, on_step, on_component_done, state, budget)` 六步执行：
  ①`extract_claims`——claims/markers 经共享 `AnalysisState` compute-once 预取；claims
  空 → `no_factual_claim` 诚实短出口（不调取证）；**提取失败不冒充无主张**（按单主张
  继续，verdict 兜底）；主张数上限 3（与 rumor.claims 同口径）。②`collect_fact_evidence`
  ——verify_fn = `RumorAgent.analyze` 完整链，state 共享（D2）；异常原样上抛（loop
  既有 error 兜底）。③`collect_propagation`——**首次接入传播取证**（skills/propagation
  包装）；授权闸 fail-closed → 步骤 `not_triggered`/`external_processing_disabled`、
  摘要轴记 `not_applicable`；失败/超预算折 partial 不阻断；记录过
  `validate_propagation_record` 校验（搜索摘要不能升级成传播事实）。④`assess_claims`
  ——verdict 已由 verify_fn 纯函数给出，不重算、不改判。⑤`validate_answer`——
  `core.compile_answer` 优先；旧引擎无 source_span/quote_span → partial + legacy 诚实
  过渡对象（`has_incomplete_claims` 恒 True，不冒充五态结论）；claim_answer 新增
  `has_incomplete_claims` 与行内 `propagation_records` 两键。⑥`commit_result`——落库
  仍由调用方（tools_verify 既有建档路径）完成，本步只登记计划状态。plan_id 可经
  `budget` 参数注入（loop 透传，tool_runs.plan_id 与计划文档同源）。
- **loop 接线**：`_route_plan(..., role)` 透传；tools 计划带 plan_id/capability_id；
  `_collapse_plan_steps` 把 claim 六步折叠为一次承载工具调用（后续 step_id 入
  `extra_step_ids`，执行后按 plan 文档补 per-phase 审计行）；`resolve_action(action,
  role)` 让 action 通道走同一份角色白名单；ctx 注入 plan_id 透传执行器；per-step
  finish_run 带 execution_status/task_status（执行器结果自带两轴，其余
  shell_to_three_axis；取消/超时路径记 cancelled/budget_exhausted）。
- **持久化 v9**：`dossier/store.py` `SCHEMA_VERSION = 9`，MIGRATIONS 追加 v8→v9：
  `tool_runs` 只追加 `plan_id TEXT / step_id TEXT / execution_status TEXT /
  task_status TEXT`；`session_store.start_run(..., plan_id=None, step_id=None)`、
  `finish_run(..., execution_status=None, task_status=None)`（COALESCE 语义，老调用点
  逐位不变）；迁移测试在 tests/test_workflow_engine.py。**合并注意**：platform 线
  （feature/platform-monitor）的 schema v7 与主线 v7 同名不同物（既有已知问题）；主线
  新增 v9 段在两线合并时按既有"表存在性探测 + 版本重登记"方案处理。
- **配套改动**：`rumor/agent.py analyze(..., state=None)` 共享状态（compute-once 不重
  算，None 时自建、逐位不变）；`tools_verify.py` 的 verify_claim/recheck_dossier 改走
  执行器，handler 返回键/卡片形状/建档路径不变，digest 去数值（§5）。

## 4. 角色权限层

### 4.1 对话白名单（assistant_rules.json `role_tool_allowlists`）

- **layperson / full**：17 工具 + 3 元动作（chitchat/clarify/refuse）——含专用
  verify_claim/recheck_dossier；
- **analyst / monitor**：15 工具 + 3 元动作——无 verify_claim/recheck_dossier；
- 解析在 `router._role_allowlist`：role 缺省 / `"full"` → 全局 `tool_allowlist`
  （full 恒全量，不读配置）；段缺失/角色缺项/非列表/空列表 → 回落全局（fail-open，
  D5）；路由枚举 = 角色白名单 ∪ META。
- 路由模型目录按 `assistant/registry.py catalog(allowlist=...)` 过滤（D7）；loop 的
  action 通道 `resolve_action(action, role)` 用同一份白名单。

### 4.2 面板服务端角色闸（REST 写端点）

| 端点 | 允许角色 | 403 detail |
|---|---|---|
| `POST /api/batch`（建+自动开跑，web/batch_api.py） | analyst, full | 「材料批次面板仅对专业用户·材料分析工作区开放（当前工作区角色无此权限），请切换档案工作区后再试」 |
| `POST/PUT/DELETE /api/subscriptions`（+run-now，web/monitor_api.py） | monitor, full | 「领域监测面板仅对专业用户·领域监测工作区开放（当前工作区角色无此权限），请切换档案工作区后再试」 |

读端点不动（按 profile_id 归属校验，跨工作区 404 不泄露存在性）；角色来源 = profiles
store 单例 `role_of(active_profile_id)`，fail-open 读不到 → `"full"`；**权限闸先于
条目校验与存在性泄露**（越权请求得不到"条目是否存在"的信息）。

### 4.3 提示词 addon 导流（prompts.json `roles` 段）

analyst router addon 172 字、monitor 167 字（各补一句"单条说法的直接核查不在本工作区
对话内"导流；≤200 字硬约束内，test_role_prompts 锁定）；layperson 140 字。narrate
addon 维持各角色 posture（§2 各表"出口姿态"列）。

## 5. 去数值分层口径（界面去、数据层留档）

- **loop `_present_for_role`**：全角色剔除 `risk_score`（`_LAYERSON_HIDDEN_CARD_FIELDS =
  ("risk_score",)`，不再限 layperson）；presentation=simple 仍 layperson 专属；
  调整只发生在对话卡片（card 事件 + kind="card" 落库同一份），历史回放渲染一致。
  `result["card"]` = 呈现卡（MCP 同核对照 / web 事件流 / result 三处同一份）；
  confidence 档位徽章（高/中/低，CONF_LABEL）保留。
- **前端**：cards.js `renderConclusion` 删风险分行与进度条；`renderSpeech` "强度 X/10"
  → 情绪强度档位（`EMOTION_BAND_HIGH=7` / `EMOTION_BAND_SUSPECT=5`，与
  verdict_rules.json speech_tier 的 high_score/suspect_score 同线）；批次条目"风险分"
  徽章 → 结论标签（`batchItemLabel`：有 label 用 label，ok 无 label 出"已核查"，失败
  出"—"）；chat.js 档案库版本对比表删风险分数值列（只留 版本/结论标签/as_of，
  行高亮只看"看得见的变化"）。
- **tools_verify digest**：删"风险分 X/100"；recheck 对比行档位化
  （"v1（误导）→ v2（谣言）"式标签演进，不带分数）。
- **E 分保留**：关注度代理估计（确定性公式、非判定分），estimated 标注已有。
- **数据层不动**：`core/card.py derive_card` 仍产 risk_score、conclusions 表、
  REST `/api/dossiers`、`dossier/service.py export_markdown` 留档；`/api/analyze`
  REST 契约不动（web/app.py）。
- **文档矛盾裁定**：v0.1 Phase 4 原文"全面不出"与本轮口径以本节为准——**界面去数值、
  数据层留档**（兼容字段），AI 叙述层同样禁数值（narrate addon）。

## 6. 测试矩阵（全 stub，零网络零 LLM）

| 测试模块 | 用例数 | 覆盖要点 |
|---|---|---|
| `tests/test_workflow_engine.py` | 20 | 17 action 映射全覆盖/entry_action_set 封闭；单步与 claim 计划形状（plan_id 前缀、to_dict）；拓扑序（依赖顺序/未知依赖/循环 ValueError）；归并口径与 core 对拍；三轴映射；v9 迁移幂等 + start/finish_run 扩参 + 老调用点不变；StepRunRecorder 落 plan/step |
| `tests/test_workflow_claim.py` | 13 | 无主张短出口；六步推进与状态；state 共享 compute-once；授权闸 fail-closed（零外发）；取消/传播失败折 partial；编译路径与 legacy 诚实出口 |
| `tests/test_role_allowlists.py` | 38 | 配置对拍（rules 段 ↔ 注册表语义）；fail-open 回落矩阵；catalog 过滤；route 角色行为（被裁 action → clarify）；REST 闸（403/detail/读端点不动/fail-open） |
| `tests/test_workflow_integration.py` | 5 | 注册表 ↔ SKILL.md 头注释逐字对拍；role_tool_allowlists ↔ 注册表语义对拍；full 恒全量 |

适配既有测试：test_chat_action_channel / test_research_tool / test_authority_tool /
test_research_assistant（plan 形状 + capability_id）、test_cost_accounting（verify 段
(1,321)→(2,642)：编排主张提取为真实新增调用，stub 未收 state 故翻倍，生产路径 state
共享）、test_profiles / test_assistant_e2e / test_mcp_assistant / test_assistant_injection
（去数值口径反转 + assertNotIn）。**全量 1758 例**（本轮实测 unittest discover 计数）。

## 7. 已知局限与后续

1. **span 锚定未接入**：旧核查引擎不产 `source_span`/`quote_span`，validate_answer 走
   legacy 诚实出口（`has_incomplete_claims=True`）；引擎补锚点后自动切换 compile_answer
   路径（执行器已就绪，无需改编排层）。
2. **传播记录"宁缺毋假"**：assistant.skills.propagation 现行实现核验后在返回值里丢弃
   page_text，verified 记录不可二次锚定 → 不计入已核验传播，由 `dropped_unvalidated`
   计数如实暴露；transport 返回 page_text 后自动接入。确定性引文匹配 → 语义匹配属
   v0.1 规格 Phase 2 范围。
3. **check_source 无 history_fn**：信源速查暂不带历史战绩口径（insights 沉淀的战绩
   另走横向工具）。
4. **/api/analyze 契约未动**：REST 直连分析不带角色闸/计划持久化（对话编排层先行，
   REST 通道后续波次评估）。
5. **schema v9 与 platform 合并**：platform 线（feature/platform-monitor）的 v7 与主线
   v7 同名不同物（既有已知问题）；合并时按"表存在性探测 + 版本重登记"方案处理
   （§3 持久化段）。
6. **路由枚举仍以 action 名为界**（D1）：capability_id 直出路由枚举（v0.1 §3.1 的
   封闭结构）是后续波次；本层由代码映射达成同等的封闭性。

## 8. 机器契约唯一来源

- 能力注册（capability_id/version/category/roles/channel/entry_actions/composite）：
  `src/mylocalai/assistant/workflow/registry.py` 的 `CAPABILITIES` + `HORIZONTAL_TOOLS`；
- 角色白名单：`src/mylocalai/assistant/assistant_rules.json` 的 `role_tool_allowlists`
  （代码解析在 `router._role_allowlist`）；
- 注册表 ↔ SKILL.md ↔ 白名单的一致性：`tests/test_workflow_integration.py`、
  `tests/test_role_allowlists.py` 对拍锁定；
- 计划展开与六步执行：`workflow/plan.py`（CLAIM_STEPS 对拍 core）、
  `workflow/claim.py`（注入契约见模块 docstring）；
- 状态归并/三轴/落库：`workflow/runner.py`、`assistant/session_store.py`
  （start_run/finish_run 扩参）、`dossier/store.py` MIGRATIONS v8→v9；
- 面板闸：`web/batch_api.py` / `web/monitor_api.py` 的 `_require_panel_role`；
- 提示词 posture：`prompts.json` roles 段（test_role_prompts 锁定 ≤200 字）。

本文与各角色 SKILL.md 均不复制上述可执行细节；发现不一致时以上述来源为准，并回头
修正文档。
