<!--
能力 id：material-analysis（材料分析）
分类：专用
适用角色：analyst
版本：1（首个发布版；升版只改本文件头，不制造第二份机器契约）
一句话目标：用户把自行搜集的多条材料一次丢进来，助手逐条核查、自动建档，最后给出跨条汇总（同源分组、结论分布、逐条档案链接），真假裁决留给用户。
面向用户的典型表达："这是我收集的几篇材料，帮我一个个查 / 批量核查这些说法 / 我找了点资料你帮我分析"
文档性质：面向用户的能力层策略（人读）。机器可执行契约（条目上限/预算/同源阈值/并发）
  的唯一规范来源 = assistant_rules.json 的 batch 段 + web/batch_api.py +
  assistant/tools_batch.py；本文件不复制任何可执行细节。
-->

# 材料分析（material-analysis）

## 1. 能力定义

对**用户自行搜集的多条材料**做批量核查的用户能力，是事实核查（claim-checking）的
**批量复合形态**（`docs/Agent工作流规格-v0.1.md` §3.2：已登记复合能力按合同自动执行，
不得自由拼工具）——不为它新开第五种用户能力，也不新开对话 action。

用户旅程：切到 analyst 档案工作区 → 左栏「材料批次」面板选输入模式（多行文本每行
一条 / 上传 txt/md/csv/json / 每行一个 URL）→ 开始分析 → 进度实时刷新（第 N 条
进行中）→ 汇总卡（计数、结论分布、疑似同源组、逐条结论与档案链接）→ 点档案号看
单条完整六章节。

与相邻能力的分工：

- 只查**一条**说法 → 事实核查（claim-checking，对话里直接发）；
- 只要资料、不要结论 → 证据研究（evidence-research，只读取证不建档）；
- 想持续盯着某个领域 → 领域监测（domain-monitoring）。

## 2. 覆盖工具与通道

本能力**不走对话路由**（不占 `router.TOOL_ACTIONS` 枚举），通道是面板 + REST：

| 通道 | 背后模块 | 权限 | 成本 | 预期延迟 | 降级路径 |
|---|---|---|---|---|---|
| POST /api/batch | assistant/tools_batch.run_batch（逐条 RumorAgent + 自动建档） | write（逐条建档） | 重 | 单条 ≤90s，整批 ≤900s | 单条失败记 error 不阻断；预算用尽/取消记 not_run |
| GET /api/batch/{id}/stream | web/batch_api（SSE 进度） | read | 零 | — | 已完结批次用库内汇总一次性重放 |
| POST /api/materials/upload | web/materials_api（内存解析，不落盘） | read | 零 | <1s | 扩展名/大小/编码/解析失败 → 400 |

- 条目上限与预算外置在 `assistant_rules.json` 的 `batch` 段：`max_items`（默认 8）、
  `max_item_chars`（8000）、`max_total_chars`（64000）、`item_deadline_s`（90）、
  `deadline_s`（900）、`similarity_threshold`（0.72）、`max_concurrent`（2）。
- 并发名额：同时在跑的批次达 `max_concurrent` 时新请求 503 + Retry-After，
  不堆在框架线程池里（与 /api/analyze 的 _Slot 同模式）。
- URL 条目：编排期经 `retrieval.search.fetch_article` 抓正文（自带 SSRF 防护），
  抓取失败该条记 blocked/error，不阻断整批。

## 3. 硬门禁要点

1. **同源组不合并判定**。指纹相似（Jaccard ≥ `similarity_threshold`，与谣言库
   先问再查同口径，保守）的材料只影响**汇总层怎么呈现**（代表文本 + 组内标签
   分布），每条材料**各自判定、各自建档**；绝不用一条的结论覆盖另一条，也不把
   “疑似同源"说成"是同一条谣言”。
2. **预算与取消诚实**。单条超预算、整批预算用尽、用户断开取消 → 未跑的条目标
   `not_run` 并在汇总卡如实计数；**不把"没跑"显示成"没发现"**。
3. **未授权零外发**。URL 条目抓取前查 `external_allowed("public-web")`
   （`core/privacy.py`，fail-closed），未过记 `blocked`；检索链路同样受
   `public-search` 授权控制。
4. **判定语义与角色无关**。逐条核查复用 RumorAgent 全链路与 `verdict.assess`
   纯函数——同一材料在任何档案工作区同结论；批量不改变任何单条判定口径。
5. **保存开关生效**。结果保存关闭时逐条不建档（`DossierService.start_dossier`
   返回 None），汇总卡照常出，条目 `dossier_id` 为空——不假装存过。

## 4. 输出契约

- **汇总卡（card_type=batch_summary）先于叙述**；SSE 事件序：
  meta{batch_id} → status → progress{done,total,position,status}* → card → narrative → done。
- 卡片字段：`total/ok/failed/blocked/not_run`（状态计数）、`by_label`（结论分布）、
  `groups`（疑似同源组：group_id/size/positions/representative/labels）、
  `items`（逐条 position/status/label/risk_score/dossier_id/url/group_id/error）、
  `as_of`、`degraded`（失败条目的一行原因清单）。缺字段的块整块不显示，不编造。
- **layperson 简化不适用于本能力**：analyst 是专业用户，卡片保留 risk_score 等
  完整数值（`_present_for_role` 只对 layperson 生效）。
- 叙述层只转述汇总卡的计数与分布，不得新增或改写任何条目结论。
- 已完结批次的 stream 是一次性重放（meta/status/card/done，无 progress），
  卡片 `as_of` 为空时前端如实显示"未知"。

## 5. 降级与合法空结果

- 零结果合法：上传文件解析出 0 条 → 400（用户错误）；检索/抓取全失败 → 条目
  error/blocked 并在汇总卡计数，不是"材料没问题"。
- 单条失败（引擎异常/超时）→ 该条 `error` + 原因，其余条目照常跑；整批状态
  `partial`（有成功有失败）或 `failed`（全失败）。
- 保存开关关闭 → 不建档但汇总照常；批次记录本身也受结果保存控制。
- 并发超限 → 503 + Retry-After，明确说"服务正忙"，不排队闷等。
- 用户中途停止（AbortController 断开）→ 已跑完的条目保留，剩余 `not_run`。

## 6. 边界案例

| # | 场景 | 这样做 | 不这样做 |
|---|---|---|---|
| 1 | 用户贴 20 条材料 | 说明一次最多 8 条（rules.batch.max_items），请分批或先筛 | 静默默认只跑前 8 条 |
| 2 | 两条材料是同一条谣言的不同转述 | 汇总里归入同源组、各自判定各自建档 | 合并成一条结论或断言"是同一条" |
| 3 | 某条 URL 抓取被反爬挡住 | 该条记 blocked/error 并说明原因，其余继续 | 整批失败或用搜索摘要冒充正文 |
| 4 | 第 5 条时整批预算用尽 | 前 4 条照常出，后 4 条 not_run 并如实计数 | 把没跑的条目标成"未发现问题" |
| 5 | 结果保存开关关着 | 汇总照常出、条目 dossier_id 为空、明示未保存 | 假装存了或静默丢弃 |
| 6 | 用户在对话里说"帮我批量查这批" | 一句话导流到材料批次面板（角色 addon 口径），不拼工具 | 在对话里逐条手动拼 verify_claim |
| 7 | 两个批次同时提交 | 第二个在名额内照跑；超限 503 + Retry-After | 无提示堆在线程池里 |

## 7. 话术口径示例

- 导流时："批量核查在左栏「材料批次」面板里：一次最多 8 条，逐条查完会出一张汇总卡。把材料贴进去点开始就行。"
- 汇总转述："这批 8 条里 6 条跑完：疑似谣言 2、存疑待核实 3、低风险 1；1 条抓取被拦、1 条没用完预算没跑。结论以上方汇总卡为准，逐条档案已归档。"
- 同源组时："第 2、5 条说法很接近，已归为疑似同源，但它们是各自判定的——结论可能不同，这正常。"

## 8. 本能力不输出什么

- 不输出批量材料的"总体真假"——只给逐条结论与汇总计数；
- 不把同源组合并成单一判定，不把"疑似同源"说成"是同一条谣言"；
- 不修改历史结论、标签、档位，不删除档案；这类请求一律拒绝；
- 不伪造来源、档案号、引文、URL 与核验时点；
- 不把失败、未跑、未授权伪装成完整成功。

## 9. 角色与工作区（v6 档案工作区）

- **analyst（专业用户 · 材料分析）的主能力**，入口只在 analyst 工作区可见
  （面板显隐由 `active.role === "analyst"` 决定，快照为空时隐藏）。
- 批次与条目归**当前激活档案工作区**（batches.profile_id）；跨工作区访问按
  "不存在"处理（`web/batch_api._batch_or_404`），不泄露别区批次。
- 对话里的导流话术由 `assistant/prompts.json` 的 `roles.analyst.router_system_addon`
  提供；本能力不占对话路由枚举。
