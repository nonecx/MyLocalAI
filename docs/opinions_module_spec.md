# 民间观点聚合分析模块规格（rumor_agent/opinions.py）

## 背景与目标

联网证据本里常常**搜不到官方或权威媒体的定论**，只有一堆私人看法、评论和自媒体文章。
这种情况下"有没有辟谣"判断不了，但"民间怎么说"本身可以分析。本模块就是干这件事：

1. 把文章按对主张的立场分成**支持 / 反对**两类（只有一类时不做两类划分）
2. 每类**随机抽取最多 4 篇**（不足 4 篇全选）深读，提取其中的论据
3. 对论据做三项分析：
   - **证据重合度**：该论据在同类文章中出现的**篇数** ÷ 该类**实际抽取的篇数**
   - **矛盾**：论据自身是否自相矛盾，不同论据之间是否互相矛盾
   - **可信度**：从来源、获取渠道等方面评估

## 约束

- 只**新建** `rumor_agent/opinions.py` 与 `tests/test_opinions.py` 两个文件。
- **不要改动** `rumor_agent/evidence.py`、`verdict.py`、`agent.py`、`tools.py`、`search.py`、`sources.py`。
  现有 90 个离线测试（`tests.test_rumor_agent_offline`、`tests.test_browser_mcp`、`tests.test_mutations`）
  必须继续全绿。
- 代码风格与项目既有模块一致：中文 docstring、类型注解、**任何失败都记错误不抛异常**。

## 复用既有设施

```python
from .sources import RELIABLE_TIERS, classify_domain, source_kind, normalize_host
from .search import search_all, fetch_article
from .evidence import collect_evidence, build_query, judge_stances_llm
from shared_core.llm_client import chat_json   # 返回 (obj, err)，绝不抛异常
```

注意 `collect_evidence` 的 `search_fn`/`fetch_fn` 默认参数是 `None`，在**调用期**解析为模块级函数，
这样测试才能 `mock.patch` 模块属性。本模块也照此办理。

## 常量

```python
SAMPLE_PER_SIDE = 4
CHANNEL_SCORE = {"官方权威": 3.0, "权威媒体/辟谣平台": 2.0, "一般门户媒体": 1.5,
                 "自媒体/UGC": 1.0, "未知来源": 0.5}
OVERLAP_THRESHOLD = 0.5      # 2 字滑窗重合率，达到即视为同一论据（规则回退用）
MAX_ARGUMENTS_PER_ARTICLE = 6
```

## 一、触发条件

```python
should_trigger(book) -> (bool, reason)
```

- `book` 为空 / 有 `skipped` / 没有 `entries` → `(False, 原因说明)`
- `entries` 里存在任何 `domain_tier` 属于 `RELIABLE_TIERS` 的条目
  → `(False, "已有官方或权威媒体来源，不走民间观点分析")`
- 剩下的私人看法条目中，`stance` 属于 `支持`/`反驳` 的合计 ≥ 1 → `(True, "")`
- 否则 → `(False, "没有可分析的私人看法")`

## 二、分组与抽样

```python
split_by_stance(entries) -> dict
```

返回：

```python
{"支持": [...], "反驳": [...],
 "neutral": int,          # stance 为"无关"或空的条数
 "single_sided": bool,    # 只有一类非空
 "group_mode": "two-sided" | "single-sided"}
```

只有一类非空时 `single_sided=True`、`group_mode="single-sided"`：**不做两组对比**，但仍对该单组做三项分析。

```python
sample_articles(entries, n=SAMPLE_PER_SIDE, rng=None) -> list
```

- 用 `random.Random(seed)` 随机抽 n 篇；不足 n 篇就全选
- 抽完**按原列表索引排序**返回，保证同 seed 结果稳定可复现
- 传入的 `entries` 是 dict 列表（含 `url`/`title`/`stance`/`domain_tier`/`snippet`）

## 三、论据提取

```python
extract_arguments(text) -> dict
```

一次 `chat_json`，system prompt 要求输出：

```json
{"arguments": [{"argument": "论据的一句话概括",
                "basis": "该论据依据什么（数据/个人经历/传言/专家说法）",
                "source": "论据里提到的具体来源，没有则空串",
                "channel": "获取渠道（亲历/亲友转述/网络传言/媒体报道/官方通报）",
                "verifiable": "可核实|无法核实"}]}
```

上限 `MAX_ARGUMENTS_PER_ARTICLE` 条。解析失败返回 `{"arguments": [], "error": err}`。

```python
fallback_cluster(claim_texts) -> clusters
```

纯规则聚类：用 **2 字滑窗重合率 ≥ OVERLAP_THRESHOLD** 视为同一论据。这是 LLM 聚类失败时的回退。
输入是论据文本列表，输出是编号分组的列表（如 `[[0,2],[1]]`）。
重合率定义：`公共 2 字片段数 ÷ min(片段数A, 片段数B)`。

```python
merge_arguments(per_article, stance) -> dict
```

把**该类所有文章的论据**做一次 `chat_json` 聚类，要求输出 `{"clusters": [[该 cluster 的论据编号...], ...]}`，
编号是**全局编号**（跨文章连续编号，从 0 开始）。任一编号越界 / 重复 / 遗漏都判为无效 → 回退 `fallback_cluster`。

返回：

```python
{"items": [{"argument": 代表论据文本,
            "article_count": k,          # 出现该论据的篇数
            "article_ratio": round(k / 篇数, 2),
            "articles": [文章下标...],
            "sources": [...], "channels": [...]}],
 "method": "llm" | "rule",
 "error": None 或错误说明}
```

**同一篇文章里重复出现同一论据只算一次**（`article_count` 按去重后的文章数算）。

## 四、矛盾检测

```python
find_contradictions(per_article, merged) -> dict
```

一次 `chat_json`，要求输出：

```json
{"contradictions": [{"kind": "自相矛盾|互相矛盾",
                     "a": "论据A原文概括",
                     "b": "论据B概括（自相矛盾时填同一条）",
                     "why": "矛盾在哪，一句话"}]}
```

失败返回 `{"contradictions": [], "error": err}`，不抛异常。

## 五、可信度

```python
score_credibility(per_article, merged) -> list
```

**分数与等级由代码算，大模型不参与定级。** 对每条合并后的论据：

```
渠道分 = 该论据来源文章里最高的 CHANNEL_SCORE（按 domain_tier 取最大）
来源分 = 论据提到具体来源（source 非空，或 basis 含 数据/研究/论文/通报/实验）→ 1.0，否则 0.0
可验分 = verifiable == "可核实" → 1.0，否则 0.0

score = 渠道分/3.0 * 0.6 + 来源分 * 0.25 + 可验分 * 0.15      # 归一化到 0-1
```

等级：`score >= 0.66` → 高；`>= 0.4` → 中；其余 → 低。

返回按 `score` 降序：

```python
[{"argument": .., "level": "高|中|低", "score": float,
  "channel": .., "source_kind": ..,
  "reasons": ["渠道 自媒体/UGC 记 1.0/3.0", "论据未提具体来源", "标注为无法核实", ...]}]
```

`reasons` 要**逐项列出加分/减分原因**，用人话写。

## 六、主入口

```python
collect_opinion_analysis(text, evidence_book=None, sample_n=4, seed=None,
                         search_fn=None, fetch_fn=None, llm=True) -> dict
```

1. `evidence_book` 为 `None` 时自己调 `collect_evidence(text, search_fn=..., fetch_fn=...)`。
   `search_fn`/`fetch_fn` 为 `None` 时在**调用期**解析为模块级 `search_all`/`fetch_article`
   （不要写成默认参数值，否则 mock 打不进去）。
2. `should_trigger` 判定；不触发时返回
   `{"triggered": False, "skip_reason": reason, 其余字段为空}`。
3. `split_by_stance` → 每类 `sample_articles` → 对每篇抽样文章调 `fetch_fn` 取正文
   （失败则该篇用证据本的 `title` + `snippet` 兜底，并在 `read_from` 记 `"摘要"`、
   同时把该篇 URL 加进 `degraded`），调 `extract_arguments` 得论据。
4. 每类分别做 `merge_arguments`（重合度）、`find_contradictions`、`score_credibility`。
5. 返回：

```python
{"triggered": True,
 "group_mode": "two-sided"|"single-sided",
 "groups": {"支持": [url...], "反驳": [url...]},
 "sampled": {"支持": [url...], "反驳": [url...]},
 "per_article": [{"url","title","stance","domain_tier","source_kind",
                  "arguments": [...], "read_from": "全文"|"摘要"}],
 "per_group": {"支持": {"overlap": {...}, "contradictions": {...}, "credibility": [...]},
               "反驳": {...}},
 "degraded": [...],
 "error": None}
```

任何一步失败都记入 `degraded` 并继续，**绝不抛异常**。

## 七、命令行入口

```python
main(argv=None) -> int
```

支持：

```
python -m rumor_agent.opinions --json "文本"
python -m rumor_agent.opinions --file 路径          # 按 utf-8 读
python -m rumor_agent.opinions --seed N
python -m rumor_agent.opinions --sample N
```

默认输出人类可读的中文摘要（分组、抽样、重合度表、矛盾、可信度）；`--json` 输出完整结构化结果。
文件底部加 `if __name__ == "__main__": sys.exit(main())`。

## 八、测试要求（tests/test_opinions.py，离线，不联网、不调真实 API）

用 `unittest`；对 LLM 与抓取一律用 `unittest.mock.patch` 打桩；
文件顶部 `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`。必须覆盖：

1. **触发条件三种情形**：有可靠来源条目不触发；无证据不触发；只有私人看法且含支持/反驳才触发。
2. **分组**：两类都有 → `group_mode="two-sided"`；只有一类 → `single_sided=True`、`group_mode="single-sided"`。
3. **抽样**：n=4 且 6 篇 → 抽 4 篇；只有 2 篇 → 全选 2 篇；同一 seed 两次结果完全一致；抽出的 URL 属于该组。
4. **重合度**：构造 4 篇文章，同一论据出现在其中 3 篇 → `article_count=3`、`article_ratio=0.75`；
   同一篇里该论据出现两次仍只算 1 篇。
5. **可信度**：UGC 渠道 + 无来源 + 无法核实 → 低；官方权威渠道 + 有具体来源 + 可核实 → 高；
   中间情形 → 中。
6. **LLM 失败降级**：`mock.patch` 让 `chat_json` 返回 `(None, "超时")` → `extract_arguments` 返回空论据并带 `error`，
   `collect_opinion_analysis` 不抛异常且把这些记入 `degraded`，同时用 `fallback_cluster` 规则聚类**仍然给出 overlap**。
7. **聚类编号非法回退**：`merge_arguments` 收到越界/重复编号的 clusters → 回退 `rule` 方法且 `method == "rule"`。
8. **抓取失败兜底**：`fetch_fn` 返回 `None` 的文章用 `title`+`snippet` 兜底、`read_from == "摘要"`、并记录降级。

## 完成标准

```
./.venv/Scripts/python.exe -X utf8 -m unittest tests.test_opinions                    # 必须全绿
./.venv/Scripts/python.exe -X utf8 -m unittest tests.test_rumor_agent_offline \
    tests.test_browser_mcp tests.test_mutations                                       # 必须仍然全绿
```
