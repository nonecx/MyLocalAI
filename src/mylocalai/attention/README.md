# attn_agent —— 关注度（Attention）评分与深读

把"一件事有多少人关注"变成一个**确定性分数**，并解释"为什么关注"。
分数由代码算，大模型不参与；解读允许大模型参与，但只做**受控生成**。

```
src/mylocalai/attention/
├── attention.py           确定性公式 E/S + 参数档位 + derive_signals（结构化信号）
├── estimate.py            话题联网估参（产出 x/y/z，带 estimated/quality 口径）
├── deep_read.py           深读主流程：触发判定、受控生成、回退、隔离
├── deep_read_guard.py     后处理护栏（不依赖模型自觉，全部代码实现）
├── attn_rules.json        公式参数/档位表/深读阈值
├── deep_read_rules.json   深读触发规则 + 受控生成参数 + 护栏禁词
├── society_issues.json    封闭议题清单（40 项）
├── prompts.json           受控生成提示词（system + few-shot）
└── attn_deep_read.json    旧版文本口径（三段式 system + 本地模板，向后兼容）
```

## 1. 确定性公式

```
E = 100 * (1 - exp(-λ·S))
S = w_x·ln(1+x)/ln(1+x0) + w_y·ln(1+y)/ln(1+y0) + w_z·ln(1+z)/ln(1+z0) + ε
```

`x`=文章数量、`y`=文章阅读量、`z`=文章留言量。默认 `x0=100, y0=100000, z0=1000,
w=(0.2,0.5,0.3), λ=0.7, ε=1e-6`。硬约束：`w_x+w_y+w_z=1`、各权重 `>0`、`E∈(0,100)`。
**不联网、不调用大模型**，同一组输入离线必然复现同一分数。

参数档位（`attn_rules.json` 的 `active_profile`，或 CLI `--profile`）：

| 档位 | 说明 |
| --- | --- |
| `spec` | 默认，用户指定的原始参数。深读阈值 66，absolute 口径 |
| `reachable` | 候选档：拉开三维每数量级价格，让真实头部话题可越过 66。深读改 quantile 口径 |

切换档位会改变所有分数，属口径变更，需显式选择；未指定时行为与引入档位前完全一致。

## 2. 深读触发规则

外置在 `deep_read_rules.json`，经 `shared_core.config_loader` 加载
（`DEEP_READ_RULES_PATH` 整套替换、`DEEP_READ_RULES_EXTRA_PATH` 增量覆盖）。
写坏时对应段退回内置默认并留中文告警，**不崩**。

| 模式 | 含义 |
| --- | --- |
| `absolute` | E 严格大于阈值（等于不算） |
| `quantile` | 同批 Top-K%：当前话题在本批 E 里排进前 K%（K 至少取 1） |
| `change_rate` | 环比增幅，**预留未实现**，恒不触发 |
| `any` | absolute 或 quantile 任一满足 |

`by_profile` 可按档位覆盖顶层配置。默认口径：`spec` 档 `absolute`/66（与改造前
逐位一致），`reachable` 档 `quantile`/0.1（实测 5 个真实头部话题 0/5 越过 66，
绝对阈值在此档失效）。

判定是纯函数 `should_deep_read(scores, profile, rules) -> bool`，不碰 LLM、可单测。

## 3. 输入信号

`attention.derive_signals(x, y, z, params, profile, proxy_flags)` 派生解释力原料，
随 prompt 一起给模型（只给 E 值时模型只能复述"关注度高"）：

| 字段 | 含义 |
| --- | --- |
| `engagement_ratio` | z/y，阅读里多大比例转化成留言（参与深度） |
| `spread` | x，文章数量（传播广度） |
| `resonance` | z/x，单篇平均留言量（共鸣强度） |
| `share_x/y/z` | 三个加权分项占 S 的比例（与 `contributions()` 逐点一致） |
| `profile` | 参数档位名（决定触发口径） |
| `proxy_flags` | 哪些输入是代理/估计值，如 `["y_proxy","z_proxy"]` |

除零保护：`y<=0` 或 `x<=0` 时对应比值给 `0.0`，**不抛异常、不给 inf/nan**
（话题模式下 y/z 抽不到数是常态）。

## 4. 封闭议题清单

`society_issues.json`：40 个中性命名的中文议题，覆盖公共安全、医疗健康、教育、
就业、住房、养老、性别、地域、消费维权、环境、科技伦理、隐私、收入分配、家庭、
心理、法治、媒体信任、代际、城乡、网络暴力等领域。

每项含 `id` / `label` / `definition` / `tension_a_hint` / `tension_b_hint` /
`include`（≥2 条典型表现）/ `exclude`（≥2 条区分边界）。

**为什么封闭**：放任模型自造议题名，同一件事两次跑会得到两个标签，标签就无法
聚合、统计、回归。封闭集让标签成为可比对的枚举值。代价是清单一定不完整，
所以清单外一律落 `other`——为了归类而贴错标签，比承认"未归类"更糟。

加载失败/结构不合规 → 退回内置最小默认（`info_transparency_trust` + `other`）并留告警。

## 5. 受控生成与输出 schema

提示词外置在 `prompts.json`（`deep_read_system` + `deep_read_fewshot`，
`DEEP_READ_PROMPTS_PATH` / `DEEP_READ_PROMPTS_EXTRA_PATH` 可替换/增量）。
System 含 9 条硬约束，核心是：只能从清单选 `issue_tags`；社会问题只能表述为
「A 与 B 之间的张力」；每个判断必须挂 `evidence_id`；抽象层级逐级
（L1 文本证据 → L2 检索证据 → L3 结构推断，L3 不得无 L1/L2 支撑）；
禁止具体主体归因/群体贴标签/样本推普遍/价值判断/预测。Few-shot 含正反对比。

输出 schema 见 `README.md`「深读（Deep Read）：受控生成」一节。
`applicable: false` 是**必须支持的出口**：纯情绪宣泄、纯事实陈述、纯商业推广、
证据不足时输出 `{"applicable": false, "reason": "..."}`，不为"有输出"而编。

## 6. 后处理护栏

`deep_read_guard.validate_and_sanitize(raw_output, issues, evidence_ids, rules)`，
纯函数、不碰 LLM。模型漏挂证据、自创议题 id、写 L3 却无 L1/L2 支撑、滑向宏大叙事，
都在这里被拦下——**不靠提示词起作用**：

1. schema 校验：字段齐全、类型正确，不合规判不合格
2. 议题封闭性：清单外 id → `other` 且降 `confidence` 为 `low`；`other` 也不允许 → 丢弃
3. 证据有效性：`evidence_ids` 必须在有效证据集内，无效剔除；剔除后为空 → 丢条目
4. L3 挂靠：全篇无 L1/L2 时，L3 条目降级为显式不确定性（判断保留，不当结论）
5. 禁词禁归因：`banned_patterns` 正则命中 → 整段丢弃 + 记 `guard_dropped`
6. 反方必填：`require_counter_reading=true` 时缺 `counter_reading` → 不合格
7. 字数上限：超 `max_words` → 截断 + `uncertainty` 追加"输出因长度上限截断"
8. 降级标记：任何条目被丢弃 → `degraded=true`

禁词表覆盖四类（可在 `deep_read_rules.json` 增删）：

| id | 覆盖 |
| --- | --- |
| `subject_blame` | 具体主体归因：机构/公司/群体/地域作主语 + 负面谓词 |
| `universal_claim` | 样本推普遍性（全社会/普遍/必然/所有人/都在…） |
| `value_judgment` | 价值判断词（可悲/落后/愚昧/无知/麻木…） |
| `prediction` | 对未发生事件的预测（将引发/必然导致/迟早/长此以往…） |

## 7. 降级与隔离

**降级**：LLM 不可用或输出被护栏全丢 → 本地模板回退。模板按结构化字段拼接
（议题标签、张力、现象、反方、边界），不做自由生成；拼不出给旧三段式框架提示。
`degraded=true`，`degraded_reason` 记录是 LLM 失败还是护栏丢弃。

**物理隔离**（写死了测试）：

- `deep_read.py` 不得 import `rumor_agent.verdict` 或任何 `mylocalai.rumor` 模块
  （`tests/test_deep_read_controlled.py` 用 AST 检查，注释/文档串不算）
- **缓存分区**：深读与主分析复用同一个进程内缓存 `shared_core.llm_cache`，但深读用
  独立分区名 `CACHE_NAMESPACE = "attn_deep_read"`（`chat_text(..., namespace=...)`）。
  同一段 (system, user) 在两种分区名下算出**不同的缓存键**，所以一边读不到另一边的结果。
  测试端到端验证：mock 掉更底层的 `_chat_raw_providers` 让缓存真实写入，再用主分析口径
  （无分区名）查同一段文本必须 miss，而深读自己的分区必须 hit。
  分区名为空时不往键里加任何东西，既有调用方的缓存键与加参数之前逐字节相同。
- 深读抛任何异常 → 捕获，主分析流程不受影响，深读字段标 `degraded`

**复用 opinions**：若调用方传入 `opinions_signals`（核心诉求/归因对象/行动倾向），
这些字段会进 prompt，让 LLM 在"诉求→议题标签→张力表述"这一层工作，而不是从零
抽取。不硬依赖：没有就跳过。

## 8. 命令行

```bash
# 算分
python -X utf8 -m attn_agent.attention --x 1000 --y 100000000 --z 1000000 --json

# 深读（默认走 LLM；--no-llm 只用本地模板）
python -X utf8 -m attn_agent.deep_read --query "某话题" --x 1000 --y 100000000 --z 1000000
python -X utf8 -m attn_agent.deep_read --query "某话题" --x 1000 --y 100000000 --z 1000000 --json
python -X utf8 -m attn_agent.deep_read --query "某话题" --x 1000 --y 100000000 --z 1000000 --no-llm

# 可选参数：--profile spec|reachable、--rules-path PATH
```

`--query/--x/--y/--z/--no-llm/--json` 全部向后兼容；新增 `--profile`（切档位，
随之切换触发规则）与 `--rules-path`（替换规则文件）。

## 9. 测试

```bash
.venv/Scripts/python.exe -m unittest tests.test_deep_read_controlled -v   # 受控生成全量（96 例）
.venv/Scripts/python.exe -m unittest tests.test_deep_read tests.test_deep_read_stage1 -v
.venv/Scripts/python.exe -m unittest tests.test_attention tests.test_website_attention -v
```

全部离线，不调真实 API。覆盖：触发矩阵、信号派生、议题封闭性、证据覆盖、
L3 挂靠、禁词红队、applicable 出口、反方必填、字数上限、物理隔离（AST 检查
不 import verdict、深读异常不影响主结论、**缓存分区端到端验证**）、降级、
配置告警、向后兼容。
