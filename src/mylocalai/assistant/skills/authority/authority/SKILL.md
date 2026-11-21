<!--
技能名：authority-discovery（权威源发现与身份核验，权威核验阶段2）
分类：通用
服务能力：authority-verification
职责：从检索候选里判定哪些域名有资格作为权威证据来源、以哪种核验方式记录；只回答"这个来源能不能作证"，不判真假。
输入：候选域名/URL 清单 + 已读正文（动态信号只能来自实际读过的页面）+ skills/sources.json 目录与 authorities.json 配置。
输出（由代码产出，模型不参与判定）：每个来源的 authority_class ∈ official/international_org/academic/standards/factcheck/media/unverified，
     verification ∈ catalog_anchor/verified_site_link/session_verified/unverified。
边界：不判真假、不给数值分、不把页面自称当成身份依据、不把媒体升格为权威、不给未核验域名作证明/反驳的资格。
与代码校验的关系：本文件是 collect_core.classify_source / dynamic_authority_signals / is_decisive_source 的口径说明；
     代码是唯一判定者，模型对本文件的任何"解读"都不改变判定结果。
-->

# 权威源发现与身份核验（authority-discovery）

## 1. 这个技能在回答什么

只回答一个问题：**这个来源有没有资格作证**。它是权威核验的第二阶段，产出每个候选
来源的**权威类别**与**核验方式**。它不判断主张的真假、不读页面内容下结论、不给任何
数值分，也绝不因为"域名看起来像官方"或"页面自称权威"就承认其身份。

两条硬约束贯穿本阶段：身份依据必须来自**实际读取过的正文**（不是搜索摘要、不是
模型转述）；未通过核验的候选只能作线索/背景，不能进证明或反驳的证据链。

## 2. 权威类别口径（authority_class）

| 类别 | 含义 | 能否作决定性证据 |
|---|---|---|
| `official` | 政府及政府权威机构站点 | 可以 |
| `international_org` | 国际组织（WHO、欧盟机构等） | 可以 |
| `academic` | 学术机构、期刊、科研发布 | 可以 |
| `standards` | 标准制定/发布机构 | 可以 |
| `factcheck` | 专业事实核查平台 | 可以 |
| `media` | 权威媒体/门户/UGC | **不可以，只作倾向** |
| `unverified` | 目录外且未通过动态核验 | **不可以** |

类别由代码按域名判定（信源分级目录档位 + `authorities.json.class_overrides`），
模型不能指定、不能改写。媒体（含高利害领域的公共卫生/财经/政策法规报道）无论多有
分量，都只提供**倾向**，不提供证明或反驳——这是契约写死的口径，不因话题重要性放宽。

## 3. 核验方式口径（verification）

| 方式 | 含义 | 决定性资格 |
|---|---|---|
| `catalog_anchor` | 域名在 `skills/sources.json` 里登记为锚点 | 有（类别属决定性集合时） |
| `verified_site_link` | 已核验页面的正文里**由代码抽出**的直链 | 有 |
| `session_verified` | 本次运行内代码信号核验通过 | 需交叉佐证才算 |
| `unverified` | 目录外域名，或信号不足 | 无 |

"已核验页面"指本次运行里**真正读取过正文**且核验方式不是 unverified 的页面。
没读过就没有入站直链、没有交叉提及——搜索摘要里出现的域名不算。

## 4. 动态核验（目录外候选）

1. **只有**官方/国际组织/学术/标准/事实核查类候选尝试动态核验；媒体类不动态升格，
   未核验类连尝试的资格都没有。
2. 代码信号至少 `dynamic_min_signals`（默认 2）个，缺一即 `unverified`：
   - `official_suffix`：域名后缀命中 `authorities.json.official_suffixes`；
   - `official_marker`：**已读正文**里命中官方标记（政府网站标识码/主办单位/公网安备等）；
   - `inbound_link`：已核验页面正文里由代码抽出的、指向该域名的直链；
   - `cross_mention`：≥2 个独立已核验来源的正文同时提到该域名/机构。
3. **单点不决定性**：`dynamic_requires_corroboration=true` 时，动态核验来源要作
   决定性证据，还需 ≥1 个独立已核验来源交叉佐证，或存在 ≥2 个独立动态来源互为佐证。
   只有信号没有佐证 → 只作倾向。
4. 单次运行最多 `max_dynamic_domains` 个动态候选：身份核验不该烧掉取证预算。
5. `session_verified` **仅本次运行有效**，不写回信源目录；下次运行重新核验。

## 5. 边界案例

| # | 场景 | 这样做 | 不这样做 |
|---|---|---|---|
| 1 | 候选域名是 `fake.gov.cn`，已读正文没有任何官方标记 | 只有 `official_suffix` 一个信号 → `unverified`，只作线索 | 因为后缀像官方就采信 |
| 2 | 候选域名是 `gov.example`（非官方后缀），正文有"主办单位"字样 | 只有 `official_marker` 一个信号 → `unverified` | 看见标记就认官方 |
| 3 | 候选页面通篇自称"权威发布""官方指定" | 自称不是信号；若无其他代码信号 → `unverified` | 把自述当身份依据 |
| 4 | 伪装域名 `xinhuanet.com.evil.com` | 域名边界后缀匹配失败 → `unverified` | 当新华网采信 |
| 5 | 两个动态候选互为独立来源、都通过信号核验 | 记为互为佐证，可作决定性证据 | 仍按单点降级 |
| 6 | 媒体站点被官方页面正文链接 | `verified_site_link`，但媒体类别不变，仍只作倾向 | 借官方链接把媒体升格 |
| 7 | 用户提供的链接指向某官方页面 | 该链接是**待核验原文**，记 unverified，不作证据 | 拿用户的链接给它自己作证 |

## 6. 与代码校验的关系（重要）

- 类别、核验方式、佐证判定全部由代码执行（`classify_source` /
  `dynamic_authority_signals` / `is_decisive_source`），模型不参与；
- 身份判断引用的"原句"只能是**已读正文**里的官方标记文本，代码逐字在正文里找；
  找不到就不算信号，也不进 `verification_basis`；
- 未知类别/未知核验方式一律落 `unverified`（unknown → unverified），绝不猜；
- 目录缺失/配置写坏 → 退化为"没有目录锚点"，候选全部按目录外走动态核验，不抛异常。

> 记忆点：身份不能自封，信号必须来自读过的正文，单点动态只配作倾向。
