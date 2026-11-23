<!--
技能名：check-statistics（统计说法核查）
分类：专用
服务能力：statistical-claim-checking
职责：把用户原文里的数字主张（百分比/百分点/倍数/约·超过·近 等约数表述/带单位绝对值/年份）逐条与已读来源正文做口径比对，给出 consistent/inconsistent/caliber_mismatch/unverifiable 四态；不判内容真假。
输入：一段含数字说法的原文（≤4000 字，一次只查一段）。
输出（JSON，键名锁定，与 pipeline 实际返回键完全一致）：
  {"text", "state", "claims", "limits", "counts"}
  claims[] = {claim_id, excerpt, number, unit, metric, qualifiers{time,place,entity,base},
              verdict, source_number, source_url, source_title, quote, as_of, basis}
  verdict ∈ consistent|inconsistent|caliber_mismatch|unverifiable
  state = ok|unclear|unreachable
  counts = {claims, searches, pages, llm_calls}
  原文没有数字表达 → task_status=no_factual_claim（合法空出口，不是失败）
边界：不判内容真假（digest/report 不出现真假判定词，「没核实到」绝不说成「数字为假」）；
      数值抽取、归一（千分位/精度/单位进率）与比对全部由代码完成，LLM 只做摘录结构化与
      段落定位，quote 不逐字锚定即丢弃；不建档、无分数、不写库。
与代码校验的关系：判定口径全部代码实现于 skills/statistics/pipeline.py（正则抽取 + 数值归一 + 比对矩阵），
      模型对 SKILL.md 的任何"解读"都不改变判定结果。
-->

# 统计说法核查（check-statistics）

## 1. 这个技能在回答什么

只回答一个问题：**这段说法里的数字，和已读来源正文对得上吗**。用户贴一段带数字的
说法，逐条给出四态判语。它**不判断说法内容的真假**：数字对得上不代表内容为真，
对不上也不代表内容为假——真假裁决归用户，权威核验走 verify_claim /
verify_authority。

两条硬约束贯穿全流程：数值表达只由**代码正则**抽取（百分比/百分点/倍数/带单位
绝对值/年份），模型报的数字必须等于代码从同一摘录里抽到的某一个；段落只许在
**实际读取过的正文**里逐字选取，引文定位失败即整条丢弃。

## 2. 四态判定口径（判语只有四种，全部代码判定）

| 判语 | 条件（数值归一后由代码比对） |
|---|---|
| ① 一致（consistent） | 来源段落含**同单位**数字，且值与主张相等（约数表述按容差：约/近 ±10%，超过=来源不低于主张，不足=来源不高于主张） |
| ② 不一致（inconsistent） | 来源段落含同单位数字，但值与主张不等 |
| ③ 口径不一致（caliber_mismatch） | 来源段落只有**同口径族但不同单位**的数字（如 百分比 vs 百分点），不作数值比对 |
| ④ 未核实到（unverifiable） | 没有定位到含可比数字的段落，或段落数字与主张跨口径族（如 金额 vs 比率）；**只表示这次没核到，不代表任何真假结论** |

数值归一口径（代码实现于 `pipeline._extract_numbers` / `_normalize_unit` /
`_values_match`）：千分位逗号剥离（"1,234.5"="1234.5"）、小数精度按浮点比较、
单位进率折算（万亿 1e12 / 亿 1e8 / 万 1e4，万元与亿元可换）、口径族分组
（percent+percentage_point 同族、money、people、multiple、year、generic）。

## 3. 核查流程与预算

1. **预检**：空/超长（`max_input_chars=4000`）→ needs_clarification，请用户补一段
   含数字的说法，不猜。
2. **抽数字主张**：代码正则抽数值表达（最多 `max_claims=5` 条）；注入了模型时才
   允许一次批量结构化（excerpt 必须逐字来自原文、number/unit 必须与代码抽取一致、
   封闭字段坏形状即丢弃）；模型不可用或全部未过校验 → 退回代码抽取结果。
3. **逐主张检索**（每主张 `per_claim_max_queries=2`）：检索词 = 主张摘录 + 指标/
   实体补充，先剥 PII 再剥 `site:`/`inurl:` 等范围操作符；总量 `max_searches=8`。
   外发授权 fail-closed：检索降级/未授权 = 零外发 + 如实记 error，主张落
   unverifiable，绝不把"没搜"说成"搜过了没找到"。
4. **逐主张读页**（每主张 `per_claim_max_pages=2`，总量 `max_pages=8`）：候选按
   信源目录锚点/档位排序（`collect_core.classify_source`）；域名允许集锁定为候选
   自己的域。
5. **口径比对**：模型只许在已读正文里逐字定位段落（quote 不逐字锚定即丢弃）；
   未注入模型时按代码确定性定位（第一个含同口径族数字的分句）；数字只从**已锚定
   段落**里抽，与主张做归一比对，产出四态判语。
6. **汇总**：默认纯代码模板拼接（零模型，最稳）；注入了模型时才允许一段 ≤100 字
   受约束补充说明，不得新增主张/数字/URL、不得出现真假判定词或判定性表述，越界
   即丢弃、退回纯模板。

## 4. 降级行为（每个出口都有）

| 场景 | 行为 |
|---|---|
| 输入为空/超长 | needs_clarification，请用户补一段含数字的说法，不猜 |
| 原文没有数字表达 | task_status=no_factual_claim、status=completed（合法空出口，不检索不读页） |
| 检索未授权/后端故障/取页全败 | 零外发或如实记 error，主张全部 unverifiable，`state=unreachable` |
| 预算/死线到顶 | budget_exhausted，保留已取得的比对结果，limits/gaps 写明 |
| 用户取消 | cancelled，保留已取得的主张与比对结果 |
| 模型结构化/定位/补充说明越界或不可用 | 丢弃该次模型产物（结构化退回代码抽取、定位该页不比对、补充说明退回纯模板），主结论不受影响 |

注意：`verdict=unverifiable` 与 `state=unreachable` 都不是"查过没问题"，是"这次
没核到"；报告与摘要都会把这一点写在明处。

## 5. 边界案例

| # | 场景 | 这样做 | 不这样做 |
|---|---|---|---|
| 1 | 主张"失业率 5.2%"，来源段落为 4.9% | 同单位异值 → inconsistent | 因为接近就判一致 |
| 2 | 主张"失业率 5.2%"，来源段落为 5.20% | 精度归一后相等 → consistent | 因小数位数不同判不一致 |
| 3 | 主张"增长 3.5 万亿元"，来源为"35000 亿元" | 进率折算后相等 → consistent | 因单位写法不同判不一致 |
| 4 | 主张"上升 5%"，来源段落只有"上升 5 个百分点" | 同族异单位 → caliber_mismatch | 把百分点当百分比直接比 |
| 5 | 主张"失业率 5.2%"，来源段落只有金额数字 | 跨口径族 → unverifiable | 硬凑一个数说一致/不一致 |
| 6 | 主张"约 5.2%"，来源为 5.5% | 约数容差内 → consistent | 精确相等才算一致 |
| 7 | 模型给的摘录原文里找不到 | claim_not_anchored 整条丢弃，退回代码抽取 | 把模型编的摘录当主张 |
| 8 | 模型给的数字不是代码抽取结果 | number_not_from_code 丢弃 | 采用模型口述的数字 |
| 9 | 模型给的段落定位不到 | quote_not_anchored 丢弃，该页不比对 | 拿没锚定的引文当证据 |
| 10 | 检索未授权 | 零外发、主张 unverifiable、error 如实记 | 把"没搜"记成"搜过了没找到" |

## 6. 与代码校验的关系（重要）

- `verdict` / `source_number` / `basis` 全部由代码产出（`_extract_numbers` +
  `_normalize_unit` + `_values_match` + `_compare_claim`），模型不参与判定；
- 模型只做两件事：把代码抽到的摘录结构化成 claim 对象（封闭字段，坏形状丢弃）、
  在已读正文里逐字定位段落（不锚定即丢弃）；它给出的数字/单位必须与代码抽取
  口径一致，否则整条丢弃；
- 四态是封闭枚举，未知判语一律落 unverifiable，绝不猜；
- 可选的模型补充说明只许转述代码判定：新增数字/URL、真假判定词、判定性表述、
  超 100 字，一律整段丢弃并记 limits；
- 本核查只比对数字与单位口径，不对任何说法给真假结论，也不给数值分、不建档。

> 记忆点：只比数字不判真假；同单位比值、同族比口径、跨族不硬比；「未核实到」
> 只说这次没核到，绝不说成「数字为假」。
