# Skill 分类与提示词规范 v1.0（2026-11-21）

> 状态：已实施（分支 `feature/skill-taxonomy-prompt-upgrade`，基线远程主线
> agent-claim-checking-v02）。本文记录本次升级的四件事：**skill 通用/专用分类体系**、
> **提示词三段结构（角色身份/任务目标/边界）**、**提示词外置与覆盖机制**、
> **两条新只读对话能力**（信源可信度速查 / 辟谣方法指南）。
> 读者：项目作者、后续执行会话、评审者。
> 原则延续（不变铁律）：**判定语义与角色/提示词措辞无关**（白名单、封闭枚举、
> 状态机、JSON 键名契约逐字保留）；**大模型不参与判定**；**每个出口都有降级**；
> **full 与未知角色的提示词合并逐位不变**；全离线可测。

---

## 1. 决策记录

| # | 决策 | 理由 |
|---|---|---|
| D1 | 分类维度采用**按角色绑定**：通用 = 横向能力（任何工作区可用）；专用 = 角色主能力 | 与《用户角色与Profile规格-v1.0》§8.2 的映射总表和既有"横向能力"表述同构，不引入第三套概念 |
| D2 | 分类以**元数据字段**落地，不移动目录 | capabilities/ 与 skills/ 的目录名被测试对拍、代码装配点引用（pipeline 按目录名读 SKILL.md），移动目录的破坏面远大于两行头注释 |
| D3 | 能力层与执行层**都**登记分类 | 能力层回答"给谁用"，执行层补 `服务能力` 字段建立"执行层→能力层"映射，两层各自机器可校验 |
| D4 | 新增能力只做两条：**source-credibility**（单源速查）与 **fact-check-guide**（零检索教方法） | 从既有缺口出发：前者补"单源画像速查"（与权威核验的"多源对主张"互补），后者补"教方法"（此前只有闲聊兜底）；其余候选（统计说法核查/档案对比/事件时间线）留扩展位（§7） |
| D5 | 提示词升级采用**三段结构前缀**，不重写行为规则 | 【角色身份】【任务目标】【边界】各 1-2 句加在开头，原有行为规则/字段要求/JSON 契约逐字保留——降低改变判定语义的风险面 |
| D6 | 谣言引擎链 8 段提示词**外置**到 `rumor/prompts.json`，机制照抄 classic | classic 的 `get_prompt + *_BUILTIN 兜底 + RUMOR_PROMPTS_PATH/_EXTRA_PATH` 已被 test_prompt_config 锁定多年，复制成熟模式而非发明新机制 |
| D7 | `assistant/prompts.json` 补 `ASSISTANT_PROMPTS_PATH/_EXTRA_PATH` 环境变量覆盖 | 对齐"判定口径可配置"章节的整套替换/增量覆盖约定；坏配置降级留告警，不挡对话回合 |
| D8 | attention 旧版文本口径 **保留 + 标注 deprecated**，不删 | `attn_deep_read.json` 有运行时读取方（`deep_read._load_rules` 被降级模板路径消费），删除会破坏降级出口；受控版 `attention/prompts.json` 为唯一编辑源 |

## 2. 通用/专用分类体系

**定义**（按角色绑定）：

- **通用 skill**：横向能力，不绑定特定角色，任何工作区（含 full）都可用。
- **专用 skill**：绑定特定角色的主能力，是该角色工作区的核心产物形态。

**能力层映射（10 项）**：

| 分类 | 能力（capability id） | 工具/通道 | 适用角色 |
| --- | --- | --- | --- |
| 通用 | evidence-research 证据研究 | collect_evidence | 全部 |
| 通用 | information-collection 资料收集 | collect_research | 全部 |
| 通用 | authority-verification 权威核验 | verify_authority | 全部 |
| 通用 | topic-understanding 话题理解 | heat/深读 | 全部 |
| 通用 | speech-understanding 言论理解 | classic 链 | 全部 |
| 通用 | source-credibility 信源可信度速查（新） | check_source | 全部 |
| 通用 | fact-check-guide 辟谣方法指南（新） | guide_fact_check | 全部 |
| 专用 | claim-checking 事实核查 | verify_claim | layperson |
| 专用 | material-analysis 材料分析 | 批次面板 + REST | analyst |
| 专用 | domain-monitoring 领域监测 | 监测面板 + REST + 调度 | monitor |

**执行层映射（9 份 SKILL.md，均通用）**：domain / sources / evidence →
服务 evidence-research；research/scope、research/digest → 服务 information-collection；
authority/authority、authority/adjudicate → 服务 authority-verification；
source → 服务 source-credibility；guide → 服务 fact-check-guide。
RumorAgent 引擎 SYSTEM_PROMPT 服务 claim-checking（专用；判定语义共享铁律不变，
角色与分类都不改变判定）。

**元数据字段约定**（不动目录、不动既有字段）：

- 能力层 SKILL.md 头注释（`capabilities/<dir>/SKILL.md`）新增两字段：
  `分类：通用|专用`、`适用角色：全部|layperson|analyst|monitor`。
- 执行层 SKILL.md 头注释（`skills/<dir>/SKILL.md`，HTML 注释会被 `pipeline._skill_prompt`
  剥离、不进模型）新增两字段：`分类：通用|专用`、`服务能力：<capability id>`。
  注：collect_core 的 `load_skill_prompt`（research/authority/source/guide 用）不剥注释，
  头注释会进入模型提示词——字段即契约声明，属预期行为。

**扩展规则**：新增能力先定分类与适用角色，再登记进 `tests/test_role_prompts.py` 的
`EXPECTED_CAPS` / `EXPECTED_CLASSIFICATION`（执行层另加 `EXPECTED_SKILL_SERVICE`），
漏登记直接测试失败——目录集合即全量清单。

## 3. 新增技能（第四波通用技能）

### 3.1 source-credibility 信源可信度速查（check_source）

- **定位**：给一个网站/域名/机构名，产出**确定性**可信度画像。与权威核验的分工：
  verify_authority 是"多权威来源对主张"，本能力是"单源画像速查"，互不替代。
- **通道**：对话工具 `check_source`（params=["query"]，permission=read，不建档、
  无分数；`dossier_id=None` + `not_saved=True`）。
- **流水线**：`skills/source/pipeline.py` 的 `SourceSkills`——规范化输入 → transport
  安全取页（1 页，授权闸 `public-web` fail-closed）→ 权威信号判定（复用
  `collect_core.classify_source` / `dynamic_authority_signals`，官方后缀 / 正文官方
  标记 / 已核验站点直链 / 多来源交叉提及 ≥2）→ 可选历史战绩（注入 history_fn 且查到
  才输出，否则如实"无记录"）→ 摘要（默认代码模板拼接；注入 llm_fn 才允许一段受约束
  补充说明，越界丢弃退模板）。
- **输出口径**：沿用"1+3"——只有代码判定的决定性信号才说"官方/权威"（verified=true），
  否则只给倾向或"未核验"；state 六态外壳（ok/unreachable/unclear 为合法出口）。
- **data 键锁定**：`query, resolved{kind,url,host,name,catalog,catalog_anchor,source_id,tier},
  state, authority_category, category_label, verified, basis, signals[], history,
  summary, limits[]`；SKILL.md 头注释与 `_DATA_KEYS` 对拍锁定。
- **预算外置**：`skills/source/rules.json`（max_pages=1、total_deadline_s=20、
  io_timeout_s=8 等键，load_rules 类型校验 + 缺键回退内置默认）。
- **边界**：不判内容真假只评信源；不做超预算深挖；页面不可达如实降级，绝不把
  "没读到页面"说成"核验过没问题"。

### 3.2 fact-check-guide 辟谣方法指南（guide_fact_check）

- **定位**：教用户自行识别与查证谣言的方法，**零检索零取页纯知识**。
- **通道**：对话工具 `guide_fact_check`（params=["question"]，permission=read）。
- **执行层**：`skills/guide/SKILL.md` 固定"查证五步法"口径（代码锁定，模型只能引用
  不能改写）：①看出处（作者/机构/域名是否可识别可追责）②交叉验证（多方独立信源
  是否一致）③查原始引用（转述是否忠实原始出处）④识别情绪化标题与断言强度
  （绝对化措辞、恐吓式提醒）⑤理解证据等级（官方文件>权威媒体报道>自媒体转述）。
- **实现**：`GuideSkills(llm_fn=...)` 一次 LLM 调用生成讲解；模块**禁止 import 检索/
  取页函数**（零外发请求靠模块结构保证，测试断言注入位不存在）。讲解含 URL 或
  真假判定词 → 整条丢弃，退内置五步法模板（state=degraded 如实标注）。
- **data 键锁定**：`question, state(ok|degraded), answer, steps[], follow_up_hint`；
  follow_up_hint 指路 verify_claim（教方法不查事实，具体核查引导到专用能力）。
- **边界**：不联网、不给具体事件说法的真假结论、示例只做方法论演示。

### 3.3 接线（与 research/authority 完全同构）

router `TOOL_ACTIONS` + allowlist 追加 `check_source` / `guide_fact_check`（17 个
工具 action）；`loop._real_service` 工厂新增 `source_skills` / `guide_skills`
（死线透传、授权门、`source_model` / `guide_model` 模型段，缓存分区
`assistant_source` / `assistant_guide`）；registry 注册 `tools_source.register_all` /
`tools_guide.register_all`；前端 `cards.js` 新增 renderSource / renderGuide
（信源卡：绿=信源身份已核实 / 琥珀=未核验仅倾向，"已核实"只指信源身份永远不指
内容；指南卡：五步法有序列表 + 讲解原文）。

## 4. 提示词三段结构规范

**模板**（每段 1-2 句，加在提示词开头；原有内容逐字保留在其后）：

```
【角色身份】你是……（一句定位）
【任务目标】你为<服务对象/上游环节>达成<可观察产物>
【边界】你不做……；不确定时<降级行为>
```

**覆盖清单**：

| 层 | 提示词 | 位置 |
| --- | --- | --- |
| 对话层 | router_system / narrate_system / summary_system | `assistant/prompts.json` |
| 角色层 | roles 段（label/mission 即身份+目标样板；addon ≤200 字不变） | `assistant/prompts.json` |
| 引擎链 | agent_system / claims_system / stance_system / scope_system / event_relevance_system / opinions_arguments_system / opinions_cluster_system / opinions_contradiction_system | `rumor/prompts.json`（新键）+ 各模块 `*_BUILTIN` |
| classic 四段 | classic_emotion / classic_aggression / classic_logic / report | `rumor/prompts.json`（既有键）与 `*_BUILTIN` 逐字同文同步升级 |
| 深读 | deep_read_system | `attention/prompts.json` |
| 执行层 | 9 份 SKILL.md 正文（头注释职责/边界字段之外，正文口径同步） | `assistant/skills/` |
| MCP | rumor/mcp.py 4 个工具 description（轻改：各补一句目标与边界） | `rumor/mcp.py` |

固定话术 12 条（refuse/clarify/chitchat 等）不是 system 提示词，不加三段结构。

**身份措辞统一**：引擎链统一为"事实核查"体系下的分工表述（主张拆解助理 /
立场判定助理 / 事件界定助理 / 论据抽取·聚类·矛盾检测助理……），消灭"事实核查
助理"同身份不同措辞；边界段把既有约束显式化，**不新增不删除任何既有约束**。

## 5. 提示词外置与覆盖机制

1. **谣言引擎链外置**（D6）：`rumor/prompts.json` 新增 8 键，各模块
   `XXX_BUILTIN` 常量 + `get_prompt(key, builtin=...)`（import 时取用，classic 同构）；
   `SYSTEM_PROMPT` 名字保留（`_run_agent` 与 `__all__` 按原名引用）；
   `RUMOR_PROMPTS_PATH`（整套替换）/ `RUMOR_PROMPTS_EXTRA_PATH`（增量覆盖）对 8 个
   新键继续生效；整套替换漏新键时自动退回 `*_BUILTIN`。
2. **对话层覆盖**（D7）：`loop._load_prompts()` 支持
   `ASSISTANT_PROMPTS_PATH`（整套替换，成功时不再以默认文件为底稿）与
   `ASSISTANT_PROMPTS_EXTRA_PATH`（增量覆盖：字典递归深合并、列表并集去重、其余
   覆盖）；坏路径/坏 JSON → 替换失败退默认文件内容、增量失败忽略增量，全部写进
   `loop.prompts_warnings()`（按条形 key 只留最新）；无环境变量时行为与旧版逐位一致。
3. **attention 收敛**（D8）：`attn_deep_read.json` 顶部加 `_deprecated` 字段，
   `deep_read.py` 注释标注"运行时仅降级模板段被消费、system_prompt 段无运行时消费方、
   受控版见 prompts.json"；`_LEGACY` 兜底保留（有读取方），受控版为唯一编辑源。

## 6. 测试矩阵（全 stub，零网络零 LLM）

| 测试 | 守护点 |
| --- | --- |
| `tests/test_role_prompts.py` | 能力目录集合 == 10；头字段含 分类/适用角色；分类与适用角色按 `EXPECTED_CLASSIFICATION` 逐字对拍 + 语义对拍（通用=全部、专用=三角色之一）；执行层 9 份 `分类：通用` / `服务能力` 映射对拍；roles 键 ⊆ ROLES；addon ≤200 字；full/未知角色逐位不变 |
| `tests/test_source_skills.py`（30 例） | 官方后缀/正文官方标记/交叉提及≥2/单点信号/页面不可达/无战绩各判定路径；data 键名锁定；不建档；digest 不出现真假判定词 |
| `tests/test_guide_skills.py`（23 例） | 五步法代码锁定；mock llm 正常/降级路径；讲解含 URL/真假判定词整条丢弃；零外发请求（无检索/取页注入位） |
| `tests/test_prompt_config.py` | classic 四段与 `*_BUILTIN` 逐字同文（外置不漂移）；RUMOR_PROMPTS_PATH/_EXTRA 对新键生效 |
| `tests/test_assistant_router.py` 等 | 路由防护链行为不变（无 action 数量断言，新 action 走封闭枚举 + allowlist + registry 三方一致：17 个工具 action） |
| `tests/test_readme_counts.py` | README 用例数与 discover 实际数一致（1682） |

全量：`.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p "test_*.py"`
（1682 例 OK）；CI：`scripts\ci.cmd`。

## 7. 已知局限与后续

- **候选池（留扩展位，本期不做）**：统计说法核查（数字/百分比/口径类主张的专项
  处理）、档案对比（两次核查结果的结论与证据差异）、事件时间线梳理。新增时按 §2
  扩展规则先定分类再登记。
- **平台线提示词未覆盖**：`feature/platform-monitor` 分支的领域分类助理提示词
  （platform/domains.py）不在本分支；等两线合并后按本规范 §4 补三段结构。
- **对话层注入面的拼接失真未动**：`context.build_context` 的双串契约与假 assistant
  应答机制保持现状（动它属于对话层机制改造，另行立项）。
- **check_source 历史战绩**：当前 loop 工厂未注入 history_fn（输出"无记录"）；
  dossier 信源战绩查询的接线属增量优化，接口已预留。

## 8. 机器契约唯一来源

与《信息收集与权威核验技能》§9 同口径，防双维护：

- 能力注册：`assistant.registry`（loop.registry property 装配）；
- 路由枚举与放行：`router.TOOL_ACTIONS` + `assistant_rules.json` 的 `tool_allowlist`；
- 流水线：`skills/source/pipeline.py`（SourceSkills）、`skills/guide/guide.py`
  （GuideSkills）+ 各自 rules.json；
- 提示词装配：`skills/source/SKILL.md`、`skills/guide/SKILL.md`（经
  collect_core.load_skill_prompt）；
- 分类元数据：各能力/执行层 SKILL.md 头注释 + `tests/test_role_prompts.py` 三张
  映射表（EXPECTED_CAPS / EXPECTED_CLASSIFICATION / EXPECTED_SKILL_SERVICE）；
- 提示词外置：`rumor/prompts.json`（引擎链与 classic）+ 各模块 `*_BUILTIN` 兜底 +
  `assistant/prompts.json`（对话层）+ `attention/prompts.json`（深读）。

本文与各能力 SKILL.md 只写人读策略与决策记录，不复制上述可执行细节；
发现不一致时以上述来源为准。
