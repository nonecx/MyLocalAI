# 用户角色与档案工作区规格 v1.0

> 状态：**阶段 1/2/3 全部已实现**（阶段 1 于 2026-11-20 落地，阶段 2/3 于
> 2026-11-20 落地、2026-11-21 完成验证收口与监测平台两线合并）。本文档的接口
> 契约即实现契约；实现细节与验收以 §7 验收矩阵和 README 对应章节为准。
> 日期：2026-11-20（2026-11-21 更新状态与数据模型）
> 上游依据：`docs/Agent工作流规格-v0.1.md`（四类用户能力）、
> `docs/工作指导-智能助手重塑-2026-10-30.md`（§2.4 非目标：不做多用户与鉴权）。

## 1. 定位与边界

把"普通用户 / 专业用户"落成**同一产品内的多个档案工作区（Profile）**，不做账号体系。

- **为什么不是账号**：本项目明确不做多用户与鉴权（单进程、127.0.0.1、单用户本地工具）。
  角色表达的是"同一个人在不同场景下的使用方式"，不是"不同的人"。
- **Profile 是什么**：一个工作区 = 独立的会话集 + 档案集 + 画像数据 + 角色标签。
  顶栏切换器一键换边；数据按 `profile_id` 分家。
- **角色只改三件事**：入口与呈现（layperson 简化结论卡）、数据范围（列表/画像过滤）、
  后续阶段的专属面板。**不改判定语义**：`verdict.assess` 打分、隐私 fail-closed、
  路由四层防护链均不随角色放宽。

| 角色 | 用户诉求 | 阶段 1 现状 | 后续 |
| --- | --- | --- | --- |
| `layperson` | 只判定单条言论是不是谣言 | 简化结论卡（无 risk_score 数值、presentation=simple） | — |
| `analyst` | 分析辨别自行搜集的多条信息 | 已落地：左栏「材料批次」面板（阶段 2） | — |
| `monitor` | 自动搜集特定领域信息 | 已落地：左栏「领域监测」面板（阶段 3） | — |
| `full` | 不按角色裁剪 | v6 迁移默认档 | — |

## 2. 数据模型（schema v6 → v10，2026-11-21 两线合并后）

```sql
-- v6：Profile 工作区
profiles(id, key UNIQUE, display_name, role CHECK IN ('layperson','analyst','monitor','full'),
         created_at, updated_at)
app_state(key PK, value)            -- 单行 active_profile_id
sessions.profile_id  INTEGER        -- v6 新增列 + 索引
dossiers.profile_id INTEGER         -- v6 新增列 + 索引
-- v7：材料批次（阶段 2）
batches(id, profile_id, source CHECK IN ('text','file','urls'), status CHECK IN
        ('running','done','partial','failed','cancelled'), ok/failed/not_run 计数, summary, …)
batch_items(batch_id, position, raw_text/url, dossier_id, status CHECK IN
        ('pending','running','ok','error','blocked','not_run'), label, risk_score, group_id, …)
-- v8：领域监测订阅（阶段 3）
subscriptions(id, profile_id, topic, keywords, domains, frequency_s, enabled, dossier_id,
        last_run_at, next_run_at, last_status, paused_reason, …)
subscription_runs(subscription_id, status CHECK IN ('running','ok','partial','blocked','error'),
        results_count, new_count, summary, …)
-- v9/v10：平台域三表 + 信息源/领域列（原 platform-monitor 线 v6/v7，2026-11-21 合并平移）
scan_runs / watch_queries / feed_entries（v9）
patrol_sources / patrol_query_sources + watch_queries.domain 等列 + feed_entries.topic_domain 等（v10）
```

**两线合并的迁移语义（`dossier/store.py` 的 `_rebase_platform_line`）**：合并前
platform-monitor 线发布过自己的 v6/v7（平台三表/领域列），`schema_migrations` 只记
版本号无法区分链别——按**表存在性探测**（有平台三表且无 profiles 表）识别 A 线存量库，
把登记号 6→9、7→10 **重登记**（绝不重放：v10 含无 IF NOT EXISTS 的 ALTER），再照常
补跑统一链缺口段。

迁移语义（`dossier/store.py` 的 `_ensure_default_profile`，每次 migrate 幂等执行）：

1. 一个档案都没有 → 建 `key='default'`、`display_name='默认档案'`、`role='full'`；
2. `profile_id IS NULL` 的存量会话/档案 → 归属最老档案（v6 之前的全部数据）；
3. `active_profile_id` 缺失或指向已删档案 → 重置为最老档案。

**存量保护**：默认档 role=full 不裁剪能力，旧会话/档案可读、行为逐位不变。

## 3. 接口契约（web/profiles_api.py，全部本地无鉴权）

| 方法 | 路径 | 成功 | 失败 |
| --- | --- | --- | --- |
| GET | `/api/profiles` | 200 `{profiles[], active, roles[]}` | — |
| POST | `/api/profiles` | 200 `{profile}` | 400（key 非法/重复、角色越界、名称为空/超长） |
| PUT | `/api/profiles/{id}` | 200 `{profile}` | 404（不存在）/ 400（参数非法） |
| POST | `/api/profiles/{id}/activate` | 200 `{profile}` | 404 |
| DELETE | `/api/profiles/{id}` | 200 `{deleted, id}` | 404 / 409（最后一个/激活态/有数据） |

**分家规则（全端点一致）**：

- `GET /api/sessions`、`GET /api/dossiers`、`/api/insights/*` 只返回激活档案的数据；
- `GET /api/sessions/{sid}/messages|reminders`、`/api/dossiers/{id}|/export`：
  会话/档案属于别的工作区 → **404（与不存在同形，不泄露存在性）**；
- `POST /api/chat`：`session_id` 属于别的工作区 → 404；新建会话归激活档案；
- 助手工具层：`list_dossiers` 按会话工作区过滤；`get_dossier`/`export_card`/
  `recheck_dossier`/`read_opinions`/`deep_read` 经 `tools_guard.dossier_visible`
  做同一防护；指纹召回（`find_fingerprints`）按工作区过滤。

## 4. 呈现契约（layperson）

- 后端：`loop._present_for_role` 对 layperson 会话的结论卡移除 `risk_score`、
  加 `presentation="simple"`，**简化后的卡片才 emit 与落库**（历史回放一致）；
  档案库里的结论行不动（full/analyst/monitor 与档案详情页仍可见完整数值）。
- 前端：`cards.js renderConclusion` 按 `presentation==="simple"` 换标题
  （"核查结果"）、隐去"结论卡"徽章、加一行简化说明；其余徽章
  （置信度/证据充分度/as_of/降级）保留——口径诚实铁律不 relax。

## 5. 阶段 2 契约（专业用户 A · 材料分析）——已实现（2026-11-20 落地，2026-11-21 收口）

- 输入：多条文本粘贴 / 文件上传（txt/md/csv/json）/ URL 列表；
- 编排：逐条 `RumorAgent.analyze` + 自动建档；条目上限与双预算
  （`MAX_BATCH_ITEMS`、`BATCH_ITEM_DEADLINE_S`、`BATCH_DEADLINE_S`）；
- 预去重：`claim_fingerprints` 相似度分组，汇总层标注"疑似同源"（不擅自合并判定）；
- 汇总：纯函数（label 分布、立场矩阵、证据重合 Top、信源战绩、degraded 清单）；
- 失败语义：单条失败记 `status=error` 不阻断；预算用尽未跑条目标 `not_run`，
  汇总卡如实呈现，**不把"没跑"显示成"没发现"**。

## 6. 阶段 3 契约（专业用户 B · 领域监测）——已实现（2026-11-20 落地，2026-11-21 收口）

- `subscriptions`（profile_id/topic/keywords/domains/frequency_s/enabled/next_run_at/…）
  + `subscription_runs`（status ∈ running/ok/partial/blocked/error——running 为
  起跑初态，2026-11-21 收口补充；results_count/new_count/summary）；
- 调度：web lifespan 起驻线程（tick 60s、串行、单 worker 假设 + 启动告警）；
- 授权：每轮运行前查 `external_processing_enabled` + `public-search`，未过 →
  `blocked`、订阅暂停并写明原因，**零外发**；
- 检索：`search_all` 增加可选 `domains` 参数（`site:` 查询变体，≤3 个/轮），
  不传时逐位不变；
- 变化摘要：纯函数（新增/消失 URL、E 分变化；只述首末差，不外推）。

## 7. 验收矩阵

| # | 验收项 | 测试 |
| --- | --- | --- |
| 1 | v6 迁移幂等；存量数据回填默认档案且可读 | test_profiles 迁移组、test_dossier_store、test_fingerprint |
| 2 | Profile CRUD/激活/删除保护（409 语义） | test_profiles CRUD 组 |
| 3 | 会话/档案/指纹/画像按工作区分家 | test_profiles 分家组 |
| 4 | 跨工作区访问 404（不泄露存在性） | test_profiles guard 组 + Web API 组 |
| 5 | layperson 结论卡无 risk_score + presentation=simple；full 逐位不变 | test_profiles loop 组 |
| 6 | /api/profiles 与前端切换器前后端自洽 | test_website_chat TestProfilesFrontendConsistency |
| 7 | 全量离线套件 + CI 绿（除既知存量失败外无新增） | scripts\ci.cmd |
| 8 | 批次三输入/上限 8 条/双预算/同源 Jaccard≥0.72 分组各自判定/失败不阻断/not_run 语义 | test_batch 存储与编排组 |
| 9 | 批次接口（202/SSE 进度/重放/并发闸）与上传解析（txt/md/csv/json、2MB、413） | test_batch Web API 与上传组 |
| 10 | 批次取消链路（SSE 断开 → cancelled + not_run）与规格 §5 汇总三要素 | test_batch 取消与三要素用例（2026-11-21 收口补齐） |
| 11 | 订阅 CRUD/频率下限 300s/授权闸 fail-closed 零外发/失败推进不卡死 | test_monitor 存储与编排组 |
| 12 | run-now 起跑即 running + run_id、409 防重入与暂停拒绝、调度生命周期 | test_monitor API 与调度组（2026-11-21 收口补齐） |
| 13 | search_all domains 直测（变体≤3/合并过滤/全空兜底 scope_degraded/不传逐位不变） | test_monitor 检索直测组（2026-11-21 补齐） |
| 14 | 四区前端自洽与可访问性（脚本顺序/id 接线/类名↔CSS/aria/键盘） | test_models_ui、test_website_chat、test_platform_api |

## 8. Skill 与提示词分层（2026-11-20 角色扩展第二轮）

### 8.1 合并记录

2026-11-20 将并行分支的取证三技能（`collect_evidence` 只读取证流水线）合并进本分支
（合并提交 56f80f8）：冲突仅 README.md 与 cards.js 两处（均为“两边各加各的”，取并集）；
loop.py 自动合并且经 review——角色扩展的 `_session_profile`/`_present_for_role` 与
取证三技能的 `mixed_choice` 闸/collect_evidence 接线互不冲突。合并后既知存量失败
（test_insights_api 的卡片字段契约）因对方补齐 API 字段而自然修复。

2026-11-21 将并行分支的**监测平台**（热门信息自动巡检 + 热榜式 UI + 信息源/领域）合并进
本分支：schema 统一为 v6 profiles → v7 batches → v8 subscriptions → v9/v10 平台域
（`_rebase_platform_line` 按表存在性重登记收编 platform 线存量库，见 §2）；冲突 15 文件
逐个解决（union 口径，两线功能并存）；app.py 两个调度线程统一收进 `_lifespan`
（monitor 调度 + `_WORKERS<=1` 守卫下的 platform 调度），模块导入期不再有启动副作用。
平台三表**不做 profile 分家**（产品决策，单用户本地工具口径下暂不阻塞）。

### 8.2 角色 ↔ 能力 ↔ Skill ↔ 通道 映射总表

本表是"角色体系"的事实总览（替代此前不在库内的《项目工作流与 Skill 总览》引用；
四个旧能力 SKILL 的悬空引用已改写为库内文档路径，由 test_role_prompts 守护）。

| 角色 | 主能力（capability id） | 能力 SKILL（capabilities/） | 提示词 posture（prompts.json roles 段） | 通道 |
| --- | --- | --- | --- | --- |
| layperson 普通用户 | claim-checking（**呈现档位**：简化卡、无数值） | claim_checking/SKILL.md | roles.layperson（router 偏向 verify_claim；narrate 人话结论禁数值） | 对话 |
| analyst 专业·材料分析 | material-analysis（claim-checking 批量复合）+ evidence-research | material_analysis/SKILL.md、evidence_research/SKILL.md | roles.analyst（识别"批量/我收集的材料"→ 导流面板，不拼工具） | 材料批次面板 + REST |
| monitor 专业·领域监测 | domain-monitoring（topic-understanding 持续复合） | domain_monitoring/SKILL.md、topic_understanding/SKILL.md | roles.monitor（识别"盯着某领域"→ 导流面板；只述变化） | 领域监测面板 + REST + 调度 |
| full 全部能力 | 四能力全量 | 四份既有 SKILL | 不合并（roles.full = {}） | 对话 |

横向能力（speech-understanding 言论理解）与对话跟进机制不按角色裁剪，全部工作区可用。

### 8.3 提示词分层注入规则（loop._prompts_for_role）

- `roles.<role>` 段中 `*_addon` 键 → **追加**到基座同名键之后；其余键 → 覆盖；
  `label`/`mission` 等新键直接可读；`full` 与未知角色 → 返回基座**原对象**（逐位不变）。
- 注入面只有两处：**router system**（路由偏向 + 面板导流）与 **narrate system**
  （叙述 posture）。`RumorAgent` 引擎 SYSTEM_PROMPT 保持共享——**判定语义与角色
  无关**（同一说法在任何工作区同结论，项目铁律）。
- addon 长度 ≤200 字（test_role_prompts 锁定上限，防提示词膨胀）。

### 8.4 防漂移测试（tests/test_role_prompts.py）

1. 合并规则：addon 追加/覆盖/full 逐位不变/未知角色回落/roles 段缺失；
2. loop 集成：三角色 router/narrate system 各含自家导流与 posture；full 与未知
   角色逐位一致且 narrate 与基座相等；
3. 导流口径：analyst/monitor 附加段含"材料批次/领域监测"面板导流与"不拼工具/
   不新开 action"指令；
4. SKILL.md lint：六份能力文档头字段（能力 id/版本/一句话目标/文档性质）与必备
   章节齐全、能力 id 与目录名一致、**无悬空《总览》引用**；
5. 对拍：prompts.json 的 roles 键 ⊆ `dossier/profiles.py` 的 ROLES（`_` 开头为
   注释键）；capabilities 目录集合 == 六个预期能力。

## 9. 已知取舍

- **信源画像抬分（evidence lift）暂按全库聚合**：verify 服务在 `_ServiceBox`
  装配、无会话上下文，且测试 stub 的 services 签名已锁定。单用户前提下可接受；
  画像的**可见面**（insights 卡/端点）已按工作区过滤。
- **隐私偏好暂全局**（不按 Profile 拆分）：单机单用户姿态下一份授权即可覆盖。
- **多 worker 部署**：分家查询与激活态都走同一 SQLite 文件，单 writer 假设不变
  （既有告警机制覆盖）；阶段 3 调度同样只在单 worker 下可靠。
