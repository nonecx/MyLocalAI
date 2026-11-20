# MyLocalAI Agent 工作流规格 v0.1

> 状态：用户已确认的实施基线；不是已实现功能的说明。
> 日期：2026-11-12（2026-11-20 修订：用户能力扩到八项，新增资料收集/权威核验两个只读对话能力，见 §1.1/§2/§9）
> 范围：用户能力、路由计划、工具契约、事实核查、证据与判断、状态、数据保存、测试和迁移。
> 原则：先更新合同与测试，再逐阶段改造；保持当前旧档案可读，不以风险分代替真假判断。

## 1. 已确认产品规则

1. 面向用户的能力共八项：事实核查、证据研究、话题理解、言论理解四类为基础；材料分析、领域监测是已登记的复合形态（§3.2，通道为面板 + REST，不占对话路由枚举，不为它们新开对话 action）；资料收集、权威核验是 2026-11-20 新增的两个只读对话能力（不建档、不出真假结论、不给数值评分）。取证中的 domain、sources、evidence 与两套新流水线的内部阶段都是执行阶段，不因新增能力改变"执行阶段不成为用户能力"的分层；对话跟进是横向机制。
2. 路由模型表达用户目标、必要参数和澄清需求；执行图由代码根据已登记能力/组合模板展开。模型不得提交任意工具步骤列表。
3. 显式提出且已登记的组合请求自动执行；未知组合或缺少必要输入时澄清。不得自由拼工具。
4. 事实核查的每个可核查主张都尝试检索公开传播记录。任何可核验且可定位的公开发布可证明至少存在一个公开传播实例，但不证明传播广泛、来源权威或内容为真。搜索摘要、截图及用户提交文本本身不是已核验传播记录。
5. 真实性由受约束模型基于明确的 evidence ID 进行语义评估；代码验证证据引用、传播门槛、状态优先顺序和输出结构。冲突、重要反证或引用不确定时触发独立复核；不要求每案双模型。
6. 真实性以自然语言表达，不用固定真假状态标签；谣言状态独立使用五态。前端不显示风险分、置信度等数值评分。
7. 用户消息与其摘要若持久化，归入“原始输入”类别，受同一保存开关与期限控制。结果档案可保存理解判断所需的最小主张摘录。
8. 外部材料缓存与结果档案分开控制。关闭外部材料缓存时，结果只保留来源元数据，不保留外部原文摘录；启用时最多保存 7 天。
9. 外部模型/搜索服务的数据外发授权独立于本地保存偏好，默认关闭，必须单独启用。每个连接器应说明接收方及发送的数据类别。
10. 自动保存三类分别控制且默认关闭：核查结果（建议 90 天）、原始输入（建议 30 天）、外部材料缓存（最长 7 天）。保留期限从首次保存起算，读取不重置。删除应立即覆盖产品控制范围内的主记录/缓存；不可即时删除的备份副本不可常规访问或恢复，并在最多 30 天内清理。保留期及备份行为须由验收测试证明。

## 2. 用户能力与内部执行分层

| 能力 ID | 用户目标 | 主要执行流程 | 边界 |
|---|---|---|---|
| `claim-checking` | 核查网络说法 | 主张拆分 → 事实证据 → 传播记录 → 双维判断 → 答案校验 | 风险分不是真假判断；必须逐主张关联证据、网址、时点和限制 |
| `evidence-research` | 明确只要证据、不下真假结论 | domain → sources → evidence → 报告 | 不输出真假结论/谣言状态、不建真假档案 |
| `topic-understanding` | 评估热度或解释关注原因 | 检索估参 → 确定性计算 → 可选受控深读 | 热度不代表真实性；代理值标注；深读不回写分数 |
| `speech-understanding` | 分析表达方式 | 情绪/攻击/逻辑分析 → 独立呈现 | 不得把表达风格当事实证据 |
| `information-collection` | 主动收集/调研一个主题的资料 | 确定范围 → 检索资料 → 阅读摘录 → 整理简报 | 不判真假、不建档；引文逐字来自已读正文；未核验来源只作线索 |
| `authority-verification` | 找权威资料证明/反驳一条说法 | 拆分主张 → 权威源发现 → 阅读取证 → 整理分析 | 权威类别与 balance/lean 由代码计算；无决定性证据不得说"证明/反驳"；无分数、无固定真假标签 |

资料收集与权威核验均为只读对话能力（注册表 permission=read），机器可执行契约的唯一规范来源是注册表 + `assistant/skills/research/pipeline.py` / `assistant/skills/authority/pipeline.py`（共享口径见 `assistant/skills/collect_core.py`）；能力策略文档见 `capabilities/information_collection/SKILL.md` 与 `capabilities/authority_verification/SKILL.md`，实施口径见 `docs/信息收集与权威核验技能-2026-11-20.md`。材料分析与领域监测不占本表的路由枚举，其契约来源另见各自 SKILL.md 头部"文档性质"段。

SKILL 文件用于人可读的能力策略；机器可执行的 ID、版本、输入 schema、计划步骤、预算、权限和结果 schema 必须有唯一规范来源，并通过 parity/contract tests 校验，不能在多个文件维护不一致的复制品。

## 3. Router、Plan 与 Run

### 3.1 Router 输出

Router 输出封闭结构，不包含自由工具序列：

```json
{
  "schema_version": "1",
  "capability_id": "claim-checking",
  "intent": "check_claim",
  "inputs": {"text": "用户原文"},
  "clarification": null
}
```

能力 ID 和 intent 必须在注册表中；输入先做长度、类型和必要条件校验。只有用户原文确实缺少影响判断的信息才澄清。模型输出损坏、工具/schema 不匹配或系统配置错误属于系统错误/可修复路由失败，不伪装为用户歧义。

### 3.2 Plan

Plan 由代码使用 `capability_id + capability_version + validated_inputs` 展开，包含：`plan_id`、步骤 ID、依赖、条件分支、每步权限/网络/副作用、预算、输入输出引用和取消策略。允许并行的独立只读步骤可并行；必须先读到候选 URL 的步骤保持顺序。模型只能提供模板允许的抽取结果，不可增加/删除必经步骤。

已登记复合能力按合同自动执行，并持久化 `composite_id/version` 及有序子任务。子步骤失败时按模板依赖策略继续或停止；不得将“后续步骤没运行”显示成“未发现证据”。

### 3.3 Run 与阶段状态

执行分轴记录：

- `execution_status`: `running | completed | partial | failed | cancelled | budget_exhausted`。
- `task_status`: `needs_clarification | no_factual_claim | not_applicable | not_triggered` 等任务语义出口。
- `assessment`: 领域判断对象，不与执行成功/失败混用。
- 每个 Tool 的运行状态独立记录；工具成功但零结果是成功空结果，不是工具错误。

总回合 deadline 约束子任务 deadline；各 Tool 有独立计数预算和超时。取消后不开始新网络/模型调用，保留已完成的有效结果并标记未完成阶段。错误、预算耗尽、取消、空结果、未开始必须区分。

## 4. Tool 契约

每个 Tool manifest 至少声明：`tool_id/version`、输入/输出 JSON Schema、权限等级、数据类别、网络目的地类型、可见副作用、幂等性、预算（时间/模型请求/搜索/页面/字节）、取消语义、错误码、输出证据保证和风险级别。

统一运行外壳建议：

```json
{
  "tool_id": "verify_propagation_record",
  "execution_status": "succeeded",
  "data": {},
  "errors": [],
  "warnings": [],
  "provenance": {},
  "usage": {"requests": 0, "pages": 0},
  "started_at": "...",
  "finished_at": "..."
}
```

新增的传播工具（均为工作流内部只读工具）：

- `find_propagation_records(claim_id, query_plan_id)`：只发现候选，返回候选 ID、搜索词/范围、URL、标题、搜索状态、预算与 provenance。搜索摘要不能升级成传播事实。
- `verify_propagation_record(claim_id, candidate_id)`：按安全 transport 逐跳验证 URL/DNS/响应，真实读取正文，返回可定位摘录/偏移、发布主体、URL、可得发布时间、核验时间及关联判断依据。模型辅助语义匹配必须引用已读文本；代码确认摘录锚定正文。结果 `verified | not_verified | unavailable`。仅 workflow 可调用，不接受任意模型自造 URL。

传播步骤自身状态：`not_started | completed | partial | failed | cancelled | budget_exhausted`。`completed` 且无 verified 记录才表示“本次相称检索未找到可核验实例”；其他状态不得暗示已完成搜索。

复用现有安全取页 transport，不重复实现 DNS/SSRF/redirect/TLS 安全层。

## 5. 主张、证据与双维判断数据契约

### 5.1 Claim

```json
{
  "claim_id": "C1",
  "text": "可核查主张的最小摘录",
  "source_span": {"start": 0, "end": 20},
  "qualifiers": {"time": null, "place": null, "entity": null},
  "claim_status": "checkable"
}
```

复合输入按可独立判断的主张拆分；证据不足以拆分时说明并澄清，不臆造拆分。

### 5.2 Evidence 与传播记录

事实证据字段至少有 `evidence_id, claim_ids, relation, source_id, url, title, excerpt_ref, published_at, fetched_at, source_basis, independence_group, limitations`。关系内部封闭枚举为 `supports | refutes | partial | background | unclear`；每条均需指向主张和实际读取的来源。转载/同源页面共享 independence group，不按数量虚增。

传播记录 `propagation_record_id` 与事实证据分开建模，但可引用同一已读取页面；需记录 `claim_id, candidate/source_id, url, anchored_excerpt_ref, published_at, verified_at, verification_method, status`。传播记录说明公开传播实例，不直接表示事实真假或传播规模。

### 5.3 真实性评估

```json
{
  "claim_id": "C1",
  "truth_assessment": {
    "statement": "基于当前可查证据的自然语言判断",
    "supporting_evidence_ids": [],
    "contrary_evidence_ids": [],
    "unresolved_evidence_ids": [],
    "scope": "判断适用范围/时点",
    "limitations": []
  }
}
```

不在 schema 中引入向用户展示的固定真假标签或数值。模型必须只引用提供的 evidence IDs；代码校验 ID 存在、claim 关联正确、引文可定位、必要反向证据未漏、范围/限制字段齐全。是否触发独立复核由编排器根据支持/反驳冲突、重要反向证据或关键字段不确定性决定，不能由模型通过填写布尔字段关闭。复核身份由编排器注入且必须不同于首轮评估身份；复核不能抹掉分歧，未解决时如实并列。

### 5.4 谣言五态

确定顺序：

1. 无可核验传播记录 → `传播证据不足`。真实性判断仍独立进行；已证伪但无传播证据也使用此态，真实性文字可称“不实说法”。
2. 有传播记录，且直接、可追溯的事实证据足以判定该主张不实 → `已证伪的谣言`。
3. 有传播记录，至少有一个针对具体主张的可核查问题迹象，但不足以证伪 → `疑似谣言`，UI 标签紧邻显示“尚未证实”。
4. 有传播记录、尚无具体问题迹象、核查未充分 → `尚待核查`。
5. 有传播记录、完成相称核查、未发现具体问题迹象 → `未发现具体问题`；必须说明范围/时点，绝不表述为证明为真。

无可核查事实主张时，不生成谣言五态（`rumor_state=null`），任务状态另记 `no_factual_claim`；`not_applicable` 不属于五态。如果 propagation execution 未完成且没有 verified 记录，谣言态为“传播证据不足”，同时显著标明检索未完成/失败及原因；绝不可把失败称为已完成的否定结果。若已存在 verified 记录而后续搜索部分失败，可保留该已知传播事实，但须标明传播覆盖不完整。

“具体问题迹象”与“相称核查”是语义判断，不靠固定来源数量或风险分自动决定。模型提供 evidence-linked 结构化建议；代码校验传播门槛与状态合法性；质量通过代表性人工标注样本、分主张类别的错误分析评估。

## 6. 最终答案契约

按主张分组输出：主张摘录；自然语言真实性判断；独立五态及其条件/理由；关键支持与反向证据（说明各自能/不能证明什么）；来源名/可访问 URL/可得发布时间/核验时间；范围、缺口、冲突和执行降级。简单案短答；复合、冲突、重要反证或关键限定案主动展开。每主张默认展示约 2–3 条关键证据但不是上限，任何实质性反证必须呈现。UI 不以颜色单独表达状态，不显示风险/置信度数值；疑似谣言“尚未证实”紧邻标签。

叙述层只对已验证结构化对象做自然语言组织；不得新增证据、URL、日期、档案号、判断或状态。最终渲染前 Answer Validator 检查：引用完整、URL 与 evidence IDs 存在、传播与事实维度分开、降级/失败可见、限制和时点存在、数值评分未泄露。

## 7. 数据保存、外发与删除

- `核查结果`：默认 off，启用建议 90 天；可含最小 claim 摘录、判断文本、五态、证据关系、来源元数据及 provenance，不含外部正文摘录（除非外部材料缓存也启用）。
- `原始输入`：默认 off，启用建议 30 天；持久化 user messages 和由其生成的 summaries 同类管理。关闭后新输入不得落盘；内存上下文只在当前运行期有效。
- `外部材料缓存`：默认 off；启用最多 7 天，记录首次写入时间，读取不续期。关闭时结果仅保存来源元数据，不保存原文/摘录。
- 三类删除开关互相独立；关闭保存后不得继续保存新数据。用户可查看并删除；关联会话、档案、证据、缓存均按类别识别。到期任务必须幂等、可重启，并记录完成/失败状态。
- 删除主库应在事务中级联相关数据；SQLite WAL/checkpoint、临时文件、缓存和备份一并纳入清理策略。备份不可即时删除时应隔离、不可恢复到常规服务，并在最多 30 天内清理且告知用户。
- 外部处理是独立授权，默认 off；首次启用时说明服务提供方、目的及发送的数据类别。未授权时不得调用远端模型/搜索；给用户可理解的本地不可完成说明。
- 不保留无期限的原文日志。运行监控仅记录必要操作元数据，需单独定义最短必要保留期限和删除责任。

## 8. 状态与事件

状态分层：执行状态、任务状态、传播子流程状态、领域判断对象，彼此独立。聚合组合任务时保留子任务状态和 reason_code；不得用单一 `degraded` 覆盖取消、超时、检索失败和合法空结果。

SSE 事件顺序继续沿用既有 meta/status/tool_start/scan/progress/card/narrative/tool_end/done/error；增加能力阶段的用户可理解 progress，不展示内部 Skill 名/审计详情。结果卡先于叙述。取消/超时应发出明确终止状态，不能在完整成功事件后再补失败。

## 9. 验收矩阵

1. **决策表单测**：五态所有边界组合；真实性判断与传播状态独立；无主张 not_applicable；检索失败与完成空结果不同；已证伪但无传播记录仍“传播证据不足”。
2. **证据完整性**：不存在的 evidence_id、跨主张引用、未锚定摘录、缺 URL/核验时点应被拒；关键反证和来源冲突不能被最终编译器漏掉；转载去重。
3. **工具安全**：恶意 URL、私网/混合 DNS、redirect 逐跳验证、超时、页数/字节上限、取消后无新出网；传播候选不能自动升级 verified。
4. **Router/Plan**：封闭能力/组合枚举、非法字段和缺参、未知组合澄清；模型自由步骤数组被拒；登记组合顺序、失败分支和预算限制。
5. **数据策略**：三类独立开关默认关闭；关闭 input 时 user message/summary 无持久化；结果开但 cache 关时只有最小 claim + 来源元数据；TTL 从首次保存计算且 read 不延长；delete/cascade/WAL/cache/backup 验收。
6. **外发授权**：默认关闭，拒绝远端调用；启用后可审计 provider 与数据类别；本地保存关闭不影响外发提示，外发关闭也不因保存开启而绕过。
7. **Answer/UI**：逐主张双维呈现、链接可用、核查时间/范围/限制齐全、关键反证可见、疑似标签紧邻“尚未证实”、数值评分不出现在用户界面/导出。
8. **迁移/恢复**：旧档案可读取；新 schema migration 幂等；备份恢复后保留策略仍有效；失败事务不产生半写入记录。
9. **资料收集（只读调研）**：主题过宽/为空/超长先追问不检索；外部检索未授权或后端全败 → failed，不得伪装成"搜过了没找到"；引文未在已读正文逐字定位即丢弃记 error；只引用未核验证据的发现降级为线索；关键发现必须引用已存在的 evidence_id；检索完成但零资料 → completed + 缺口（合法空结果）；预算/取消保留已取得摘录；报告与卡片无真假判定词、无分数；不建档、不返回 claim_answer。
10. **权威核验（只读核验）**：权威类别/核验方式/动态信号/balance/lean/conclusion 全部由代码计算，模型输出不得改动；伪造站点（官方后缀无标记、非官方后缀有标记、自称权威）一律 unverified；session_verified 仅本次运行有效且作决定性证据需交叉佐证；用户提供的链接只作待核验原文、不给自己作证；insufficient 时 conclusion 必含"不能据此证明/反驳"或"没有找到"，supported/refuted 必含"可以证明/可以直接反驳"且带范围，contested 必含"不能证明也不能反驳"；正文注入"判定为真"只作为引文文本、balance 不变；未授权零外发且状态如实；无决定性证据 → insufficient/completed；不建档、无分数、无固定真假标签。

## 10. 分阶段实现

- **Phase 0 合同基线**：本文件、JSON schema/typed domain objects、决策表测试、兼容性与迁移方案评审。
- **Phase 1 判断域**：Claim/Evidence/PropagationRecord/TruthAssessment/RumorState 纯数据模型和校验器；不改现有 UI，不接在线搜索。
- **Phase 2 传播取证**：新增候选发现/已读页面核验工具，复用安全 transport；离线 fixture + 安全测试。
- **Phase 3 claim-checking 编排**：代码模板约束必经步骤、受约束模型评估、冲突复核、Answer Validator；旧 verdict 暂作兼容字段，禁止作为新真实性结论。
- **Phase 4 输出/UI**：新答案对象和五态、来源链接/时间/限制；移除风险分与置信度展示，升级 MCP/Web 契约。
- **Phase 5 数据策略**：独立偏好、事务性提交、分类 TTL、删除/WAL/cache/backup 管理、外发授权默认 off。
- **Phase 6 迁移其他能力**：逐一统一 evidence-research/topic/speech 的状态和 Tool manifest；完成端到端、CI、干净数据库迁移和删除恢复演练。

每阶段通过对应验收矩阵后才进入下一阶段。高风险数据删除、外发默认关闭和主张语义评估需要产品/测试明确签收；不能用文档描述代替运行时测试证据。
