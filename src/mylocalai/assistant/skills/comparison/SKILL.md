<!--
技能名：compare-dossiers（档案对比）
分类：专用
服务能力：dossier-comparison
职责：对比两份档案已记录的内容——结论标签演进（首末版本）+ as_of + 置信度档位、证据条目与信源域名的增减、声明指纹相似度；只读 diff，不重算、不改写、不判断真假。
输入：两个正整数档案 id（left_id / right_id，不能相同；跨工作区与不存在同形拒绝）。
输出（JSON，键名锁定，与 compare.py 实际返回键完全一致）：
  data = {"left", "right", "similarity", "conclusion_delta", "evidence_diff",
          "source_diff", "limits", "gaps"}
  left/right = {"id","title","label","confidence","as_of","versions","evidence_count","domains"}
              （label/confidence/as_of 为最新结论字段，缺结论为 null；versions 为最近
               N 版 [{version,label,confidence,as_of}]，不带 risk_score）
  similarity = {"score","threshold","suspected_same_source","basis"}
  conclusion_delta = {"changed","from_label","to_label","from_as_of","to_as_of"}
              （取「左档案首版 → 右档案末版」为演进两端，只陈述，不做升级/降级判断）
  evidence_diff = {"added_count","removed_count","added":[{url,title,relation}],"removed":[...]}
              （按 url 去重，以左档案为基准：右有左无为增、左有右无为减；
                relation 即库里的 stance 立场字段）
  source_diff = {"added_domains","removed_domains"}
  返回外壳：{"status": "ok"|"partial"|"not_found"|"invalid", "missing_id", "data"}
边界：纯读取 + diff，零 LLM、零网络、零写库；档案不存在或属于别的工作区同形返回
      「档案 #N 不存在」（tools_guard 契约）；相同 id / 非正整数 id → 诚实降级
      （ok=False + digest 说明要怎么改，invalid_params=True；路由期 registry.validate
      对非正整数的拒绝折 clarify 不变）；单边读失败折 partial + gaps 明示；缺章按
      absent 处理不编造；相似度只标「疑似同源」不断言同一说法；差异只述
      「标签从 X 变为 Y（as_of …）」，不做升级/降级判断；不建档、无分数、对话卡去数值
      （不出 risk_score）。
与代码校验的关系：全部输出由代码从档案库直读派生（tools_compare → 本目录 compare.py
      纯函数），大模型不参与；相似度复用 assistant/fingerprint.py 的 Jaccard
      （输入文本归一后），阈值外置在 rules.json（与 assistant_rules.json 的
      fingerprint_threshold 同口径）。
-->

# 档案对比（compare-dossiers）

## 1. 这个技能在回答什么

只回答一个问题：**两份档案已经记录下来的内容，差在哪里**。输入两个档案号，对比给出
两侧最新结论的标签/as_of/置信度档位、标签从哪一版演进到哪一版、证据条目与信源域名
各自多了什么少了什么、两条声明原文的指纹相似度。它**不重新核查任何东西**：不读网页、
不调模型、不重算分数、不改写档案，更不判断哪份档案"更正确"——只呈现 diff。

## 2. 输出口径（只陈述，不判断）

| 块 | 内容 | 明确不做的事 |
|---|---|---|
| `left` / `right` | 每侧：标题、最新结论标签/置信度档位/as_of、最近 N 版版本历史、证据条数、信源域名 | 不带 `risk_score`（对话卡去数值，《角色Agent工作流规格》§5 同口径） |
| `conclusion_delta` | 标签演进：左档案**首版** → 右档案**末版**的标签与 as_of，`changed` 仅表示两端标签不同 | 不说"升级/降级/恶化/缓和"——演进方向的价值判断归用户 |
| `evidence_diff` | 按 url 去重后，以左档案为基准的增/删条目（url/title/relation） | 不判断哪条证据更可信、不做证据权重 |
| `source_diff` | 信源域名增/删（域名集合差） | 不给域名打分、不评可信度 |
| `similarity` | 声明原文归一化后的指纹 Jaccard；达阈值只标「疑似同源」 | **不断言**两侧是同一说法；即便疑似同源，结论各自独立 |
| `limits` / `gaps` | 裁剪量与缺口/失败的如实说明 | 不把静默裁剪伪装成完整输出 |

`changed` 的口径：只有两端都**有结论**且标签不同才为 true；任一侧缺结论（缺章）时
为 false 并在 gaps 写明——缺章不编造。

## 3. 流程与预算

1. **同 id / 坏 id 劝退**：`left_id == right_id` 或任一侧非正整数 → 诚实降级
   （ok=False + digest 说明要怎么改 + `invalid_params=True`；相同 id 时指路
   get_dossier 看该档案的版本历史）。用户可修正的输入不摔成系统错误；路由期
   registry.validate 对非正整数的拒绝（折 clarify）不变，两层互不依赖。
2. **跨区/不存在防护**（`tools_guard.dossier_visible`，与 get_dossier 同一契约）：
   档案不存在或属于别的工作区 → 同一种回答「档案 #N 不存在」+ `not_found=True`，
   不泄露别的工作区有哪些档案号。
3. **直读两侧**（`get_dossier` / `conclusions` / `evidence`）：每个子读独立兜底，
   单边读失败只折 partial + gaps 明示，不影响另一侧。
4. **缺章按 absent 处理**：没有结论 → 标签字段为 null 且 gaps 写明"尚无结论"；
   没有证据 → 证据差集按空集算；没有声明原文 → 相似度不可比（score=null）。
   懒加载是常态，**不编造**。
5. **相似度**：两侧声明原文（`input_text`，空则退 `claim_excerpt`）经
   `fingerprint.normalize_claim` 归一后算 Jaccard，阈值取
   `rules.json` 的 `similarity_threshold`（与 `assistant_rules.json` 的
   `fingerprint_threshold` 同值）；达阈值只出「疑似同源」徽章。
6. **裁剪**：版本历史/证据条目/域名清单按 `rules.json` 的 `max_versions` /
   `max_evidence_items` / `max_domain_items` 取上限，裁剪量写进 `limits`。

预算：本技能零检索、零取页、零模型调用——`rules.json` 里没有任何页数/时限键，
只有呈现裁剪量与相似度阈值。

## 4. 降级行为（每个出口都有）

| 场景 | 行为 |
|---|---|
| 相同 id / 非正整数 id | 诚实降级：ok=False + digest 说明要怎么改 + `invalid_params=True`（不抛、不落 error 事件；相同 id 时指路 get_dossier） |
| 档案不存在 / 属于别的工作区 | 同形「档案 #N 不存在」+ not_found=True，不建卡 |
| 档案库整体读失败 | ok=False + 诚实 digest「档案库读取失败：…」，degraded |
| 单边子章读失败（结论/证据炸了） | status=partial，该侧缺字段按 absent，gaps 写明读失败原因 |
| rules.json 缺失/写坏 | 逐键回退内置默认并夹取，diff 照常产出 |
| 声明原文缺失（保留策略清理） | similarity.score=null，gaps 写明"跳过指纹相似度比对" |

## 5. 边界案例

| # | 场景 | 这样做 | 不这样做 |
|---|---|---|---|
| 1 | 两个 id 相同 | 诚实降级（invalid_params=True），digest 请用户给两个不同档案、指路 get_dossier 看版本历史 | 拿同一档案自比产出"无差异" |
| 2 | 右档案在别的工作区 | 「档案 #N 不存在」同形返回 | 提示"该档案属于别的工作区" |
| 3 | 左档案有结论、右档案没跑过核查 | 右侧重结论字段为 null，gaps 写明，`changed=false` | 把"尚无结论"当成一种标签去比较 |
| 4 | 两侧原文一模一样（标点不同） | 归一后 Jaccard 高 →「疑似同源」徽章 | 断言"这就是同一条说法" |
| 5 | 标签从「疑似谣言」变「谣言」 | 陈述"标签从 X 变为 Y（as_of …）" | 说"事态升级/恶化" |
| 6 | 一侧证据读失败 | partial + gaps 明示，另一侧照常呈现 | 整块对比失败或拿空集冒充差集 |
| 7 | 证据 url 在两侧都有但 stance 不同 | url 去重后不算增/删（本技能不比对条目内字段） | 声称"证据立场发生变化" |

## 6. 与代码校验的关系（重要）

- `left/right/similarity/conclusion_delta/evidence_diff/source_diff` 全部由代码从
  档案库直读派生（`skills/comparison/compare.py` 纯函数），大模型不参与任何一步；
- 相似度口径的唯一权威是 `assistant/fingerprint.py`（normalize_claim/shingles/
  jaccard），本技能不另写一套相似度算法；阈值外置在 `rules.json`；
- 本技能不读 `risk_score`、不产出任何评分字段——对话卡去数值（§5 同口径），
  档案库结论行的兼容字段由数据层保留，不归本技能呈现；
- 工具层（`tools_compare`）只做跨区防护、同 id/坏 id 劝退与卡片/digest 组装，不做第二个
  事实源（test_comparison 有与 store 直读的对拍用例）。

> 记忆点：只呈现 diff 不判断；缺章如实 absent 不编造；相似度只标「疑似同源」；
> 差异只述"标签从 X 变为 Y（as_of …）"，不替用户做升级/降级的价值判断。
