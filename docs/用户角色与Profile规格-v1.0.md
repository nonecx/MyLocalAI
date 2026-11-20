# 用户角色与档案工作区规格 v1.0

> 状态：阶段 1 已实现（Profile 骨架 + 普通用户呈现）；阶段 2（材料批量分析）、
> 阶段 3（领域监测订阅）按本文档的接口契约分阶段落地。
> 日期：2026-11-20
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
| `analyst` | 分析辨别自行搜集的多条信息 | 角色已定义，入口未建 | 阶段 2：批量核查 + 跨条汇总 |
| `monitor` | 自动搜集特定领域信息 | 角色已定义，入口未建 | 阶段 3：订阅 + 服务内调度 |
| `full` | 不按角色裁剪 | v6 迁移默认档 | — |

## 2. 数据模型（schema v6）

```sql
profiles(id, key UNIQUE, display_name, role CHECK IN ('layperson','analyst','monitor','full'),
         created_at, updated_at)
app_state(key PK, value)            -- 单行 active_profile_id
sessions.profile_id  INTEGER        -- v6 新增列 + 索引
dossiers.profile_id INTEGER         -- v6 新增列 + 索引
```

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

## 5. 阶段 2 契约（专业用户 A · 材料分析）——未实现

- 输入：多条文本粘贴 / 文件上传（txt/md/csv/json）/ URL 列表；
- 编排：逐条 `RumorAgent.analyze` + 自动建档；条目上限与双预算
  （`MAX_BATCH_ITEMS`、`BATCH_ITEM_DEADLINE_S`、`BATCH_DEADLINE_S`）；
- 预去重：`claim_fingerprints` 相似度分组，汇总层标注"疑似同源"（不擅自合并判定）；
- 汇总：纯函数（label 分布、立场矩阵、证据重合 Top、信源战绩、degraded 清单）；
- 失败语义：单条失败记 `status=error` 不阻断；预算用尽未跑条目标 `not_run`，
  汇总卡如实呈现，**不把"没跑"显示成"没发现"**。

## 6. 阶段 3 契约（专业用户 B · 领域监测）——未实现

- `subscriptions`（profile_id/topic/keywords/domains/frequency_s/enabled/next_run_at/…）
  + `subscription_runs`（status ∈ ok/partial/blocked/error、results_count/new_count/summary）；
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

## 8. 已知取舍

- **信源画像抬分（evidence lift）暂按全库聚合**：verify 服务在 `_ServiceBox`
  装配、无会话上下文，且测试 stub 的 services 签名已锁定。单用户前提下可接受；
  画像的**可见面**（insights 卡/端点）已按工作区过滤。
- **隐私偏好暂全局**（不按 Profile 拆分）：单机单用户姿态下一份授权即可覆盖。
- **多 worker 部署**：分家查询与激活态都走同一 SQLite 文件，单 writer 假设不变
  （既有告警机制覆盖）；阶段 3 调度同样只在单 worker 下可靠。
