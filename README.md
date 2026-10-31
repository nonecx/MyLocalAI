# MyLocalAI —— 网络言论智能分析与谣言识别

对一段网络文本（评论、帖子、聊天记录）做**多维度分析与风险判定**，并提供可交互的展示网站
和可被 MCP 客户端接入的谣言识别 Agent。全部靠**提示词工程 + 大模型 API** 实现，无需 GPU。

## 目录结构

```text
.
├── src/mylocalai/              唯一的包（pip install -e . 后 import mylocalai.*）
│   ├── core/
│   │   └── tiering.py          四档言论分层的唯一实现（classic.judge 与 verdict 共用）
│   ├── llm/                    LLM 网关（唯一构造路径）
│   │   ├── llm_client.py       提供方成组绑定 + 配置自检 + 备用链路 + JSON 稳健解析
│   │   ├── llm_cache.py        进程内结果缓存（同请求只调一次）+ 请求级时长预算
│   │   ├── adapter.py          LangChain 聊天模型构造（Agent 侧，同源同一网关）
│   │   └── providers.json      提供方表（密钥/地址/模型/能否关思维链）——改这里而非改代码
│   ├── config/                 统一配置加载（叶子层）
│   │   ├── config_loader.py    JSON 配置加载器（失败退回默认 + 告警）
│   │   ├── prompt_config.py    提示词与枚举白名单的加载（整套替换 + 增量覆盖）
│   │   ├── rules.py            判定口径/信源名单加载（阈值等全部外置）
│   │   ├── verdict_rules.json  分层阈值/风险分阈值/封顶/抬分锚点/稳定性/话术权重
│   │   └── sources_registry.json  信源分级名单与官方/非官方站名表——改这里而非改代码
│   ├── retrieval/              联网取数基础设施
│   │   ├── search.py           搜索聚合（可配置化）
│   │   ├── browser.py          受控浏览（原文抓取约束）
│   │   ├── dns_fallback.py     DNS 兜底
│   │   └── sources.py          域名分级/站名归一（名单读 config/sources_registry.json）
│   ├── classic/                classic 引擎
│   │   ├── language.py         情感 + 攻击性分析（提示词与白名单可外置，越界回退）
│   │   ├── logic.py            逻辑关联性与谬误分析
│   │   ├── judge.py            核心裁判：并行三模块 → 四档结论 → 报告
│   │   └── report.py           报告生成（API 失败回退本地模板，永不抛异常）
│   ├── rumor/                  谣言识别 Agent（联网取证、受控浏览、确定性打分）
│   │   ├── agent.py / verdict.py / evidence.py / state.py / tools.py / ...
│   │   ├── mcp.py              MCP 服务器封装（python -m mylocalai.rumor.mcp）
│   │   ├── prompts.json        Agent 提示词与枚举白名单——改这里而非改代码
│   │   └── README.md           Agent 的详细说明（证据本、受控浏览、MCP、降级行为）
│   ├── attention/              关注度评分与深读（确定性公式 + 联网估参 + 受控生成）
│   │   ├── attention.py        E/S 确定性公式、参数档位、derive_signals 结构化信号
│   │   ├── estimate.py         话题联网估参（产出 x/y/z，带 estimated/quality 口径）
│   │   ├── attach.py           关注度挂载器（入口层接到 classic 结果上；classic 不 import attention）
│   │   ├── deep_read.py        深读主流程（触发判定、受控生成、回退、物理隔离）
│   │   ├── deep_read_guard.py  后处理护栏（议题封闭/证据/L3 挂靠/禁词，代码实现）
│   │   ├── attn_rules.json / deep_read_rules.json / society_issues.json / prompts.json
│   │   └── README.md           关注度与深读的完整说明（公式、触发、护栏、隔离）
│   ├── web/                    FastAPI 网站 + static/（src/mylocalai/web/app.py）
│   ├── util/                   text_safety（代理项规整）/ console_io（stdio 规整）
│   └── cli.py                  命令行入口（python -m mylocalai.cli）
├── my_website/                 启动器（start.bat / start.sh）与说明，实际代码在 src/mylocalai/web/
├── tests/                      离线测试套件与真实 API 端到端脚本（import mylocalai.*）
├── docs/                       检验报告、修复说明、项目/研究报告与关注度标定报告
├── pyproject.toml              src 布局包定义（pip install -e .）
└── archive/                    遗留资产归档区（2026-10-30 清场建立，不入库）
```

依赖方向只有一条：`core ← llm ← retrieval ← {classic, rumor, attention} ← web/cli/mcp`；
classic / rumor / attention 互不 import（`tests/test_import_rules.py` 用 AST 锁定）。

## 快速开始

```bash
# 1. 建虚拟环境并安装依赖（可选依赖见 requirements.txt 注释），再可编辑安装本包
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -e .            # src 布局：import mylocalai.* 全仓库可用

# 2. 配置密钥：复制样例文件后填入自己的 Key
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env

# 3. 自检 + 跑一次分析
.venv/Scripts/python.exe tests/smoke_api.py            # 配置自检 + 连通性
.venv/Scripts/python.exe -m mylocalai.cli --text "气死了！听说自来水被人下毒了，官方根本不敢说！"

# 4. 谣言识别 Agent（联网取证）
.venv/Scripts/python.exe -m mylocalai.rumor "网传喝隔夜茶会致癌，转给家人看看！"

# 5. 展示网站
双击 my_website/start.bat，或看 my_website/README.md 的命令行方式
#    Linux / macOS：cd my_website && ./start.sh（端口用 PORT= 覆盖，解释器用 PYTHON= 覆盖）
#    实际启动命令：python -m uvicorn mylocalai.web.app:app --host 127.0.0.1 --port 8000
```

`.env` 里最关键的只有两条：`LLM_PROVIDER`（默认 longcat）与对应提供方的密钥
（如 `LONGCAT_API_KEY`）。**密钥、接口地址、模型名按提供方成组绑定**，换供应商只改
`LLM_PROVIDER`；密钥与提供方不匹配时启动自检会直接报错，不会静默把请求发到别家。
完整变量说明见 `.env.example` 与 `src/mylocalai/rumor/README.md`。

## 两条分析路径

| | classic（网站默认 / `main.py`） | agent（谣言识别） |
| --- | --- | --- |
| 做什么 | 情感 + 攻击性 + 逻辑三路分析 → 规则引擎四档结论 → 报告 | 七路分析 + **联网搜索证据本** + 受控浏览原文 → 标签 + 0-100 风险分 |
| 结论 | 高度疑似水军/煽动性言论、疑似情绪化谣言、理性探讨、情绪化宣泄/普通网络争论 | 高风险谣言、疑似谣言、存疑待核实、低风险、无可核实陈述 |
| 判定来源 | 代码里的 if-else（**大模型不参与打分**） | `verdict.assess` 纯函数（**大模型不参与打分**，注入改不动） |
| 耗时 | 约 10-30 秒 | 约 30-120 秒（含联网） |

两条路径都有降级：单模块失败不影响整体，报告回退本地模板，Agent 超时只用已完成组件。

**关于"同输入同结论"的适用范围**：可复现靠的是进程内结果缓存（同请求只真正调用一次），
所以范围是**同一进程、缓存 TTL（默认 900 秒）内、且检索层拿回同一批证据条目**。进程重启、
多进程/多 worker 部署、TTL 过期、条数淘汰之后，上游模型的采样漂移会重新显现——上游不认 seed，
这一点工程侧只能收敛、无法根除。agent 模式还会受检索结果本身变化的影响。

## 关注度（Attention）评估

`src/mylocalai/attention/` 把"一件事有多少人关注"变成一个**确定性分数**，**不调用大模型、不联网取数**
（同一组输入离线必然复现同一分数）：

```
E = 100 * (1 - exp(-λ·S))
S = w_x·ln(1+x)/ln(1+x0) + w_y·ln(1+y)/ln(1+y0) + w_z·ln(1+z)/ln(1+z0) + ε
```

`x`=文章数量、`y`=文章阅读量、`z`=文章留言量；默认 `x0=100, y0=100000, z0=1000,
w=(0.2,0.5,0.3), λ=0.7, ε=1e-6`；约束 `w_x+w_y+w_z=1`、各权重 `>0`、`E∈(0,100)`。
`E>66` 进入「高关注」档并触发深读（严格大于才算）。

命令行：

```bash
.venv/Scripts/python.exe -X utf8 -m attn_agent.attention --x 1000 --y 100000000 --z 1000000 --json
```

**网页端已实现**（双击 `my_website/start.bat`，首页下方「📈 关注度（Attention）评估」面板）：

| 能力 | 接口 | 说明 |
| --- | --- | --- |
| 手动算分 | `POST /api/attention` | 页面填 x/y/z，纯确定性返回 E、S、三个分项、档位、是否触发深读 |
| 话题联网估参 | `POST /api/attention/search` | 输入话题，`src/mylocalai/attention/estimate.py` 走真实检索估出 x/y/z 后算分 |
| 深读解读 | 上述两个接口的 `deep_read` 字段 | 触发时做**受控生成**：议题标签 + 张力 + 现象 + 反方 + 边界；`applicable=false` 是正常出口；LLM 不可用或被护栏全丢时回退本地模板并标 `degraded` |
| 分析记录内嵌 | `POST /api/analyze` 返回的 `attention` 字段 | classic / agent 两种模式都带一个确定性关注度分 |

命令行：

```bash
# 直接算分（默认档位 spec）
.venv/Scripts/python.exe -X utf8 -m attn_agent.attention --x 1000 --y 100000000 --z 1000000 --json

# 切换到「reachable」档位（拉高可达性，让真实头部话题能越过 66）
.venv/Scripts/python.exe -X utf8 -m attn_agent.attention --x 1000 --y 100000000 --z 1000000 --profile reachable

# 对高关注度话题生成深读解读（--no-llm 跳过模型调用，仅用本地模板）
.venv/Scripts/python.exe -X utf8 -m attn_agent.deep_read --query "某话题" \
    --x 1000 --y 100000000 --z 1000000
```

**参数档位**：`spec`（默认，即最初指定的参数）与 `reachable`（拉高可达性）。
切换档位会改变所有分数，属于口径变更，需显式选择；未指定时行为与引入档位机制前完全一致。

### 深读（Deep Read）：受控生成

深读回答"为什么关注"。它**不参与任何打分**：是否触发由确定性代码决定，
解读文本不改 E、不改档位、不进 verdict、不写主分析缓存（物理隔离）。

**触发规则**外置在 `src/mylocalai/attention/deep_read_rules.json`（`DEEP_READ_RULES_PATH` 整套替换、
`DEEP_READ_RULES_EXTRA_PATH` 增量覆盖），四种模式：

| 模式 | 含义 | 默认档位 |
| --- | --- | --- |
| `absolute` | E **严格大于**阈值（等于不算） | `spec` 档（保留 E>66 原始口径） |
| `quantile` | 同批 Top-K%（当前话题在本批 E 里排进前 K%） | `reachable` 档 |
| `change_rate` | 环比增幅，**预留未实现**（恒不触发） | — |
| `any` | absolute 或 quantile 任一满足 | — |

`spec` 档实测 5 个真实头部话题 0/5 越过 66（问题 A），所以 `reachable` 档默认改
`quantile`；切档位是显式行为，未指定时与改造前逐位一致。判定是纯函数
`should_deep_read(scores, profile, rules)`，不碰 LLM、可单测。

**输入信号**：`attention.derive_signals(x, y, z)` 派生 `engagement_ratio`（z/y，参与深度）、
`spread`（x，传播广度）、`resonance`（z/x，单篇平均留言）、`share_x/y/z`（三项占 S 比例）、
`profile`、`proxy_flags`，全部随 prompt 给模型——只给一个 E 值时模型只能复述"关注度高"。

**封闭议题清单**：`src/mylocalai/attention/society_issues.json`（40 个中性命名的中文议题，
每项含 `tension_a_hint`/`tension_b_hint`/`include`/`exclude`）。LLM 只能从清单里选
`issue_tags`，不得自创 id；清单外一律归 `other`。写坏时退回内置最小默认
（`info_transparency_trust` + `other`）并留告警。

**输出 schema**（`applicable=false` 是**必须支持的出口**：纯情绪/纯事实/证据不足时
输出 `{"applicable": false, "reason": "..."}`，不为"有输出"而编）：

```json
{
  "applicable": true, "degraded": false,
  "issue_tags": [{"id": "info_transparency_trust", "confidence": "medium", "evidence_ids": ["e3","e5","e7"]}],
  "tensions": [{"a": "信息发布节奏", "b": "公众知情需求", "evidence_ids": ["e3","e7","e9"], "level": "L3"}],
  "phenomena": [{"text": "多条发言指向通报时间晚于网络传闻", "level": "L1", "evidence_ids": ["e3","e5","e7"], "count": "7/12"}],
  "counter_reading": {"text": "3 条发言认为质疑源于误解", "evidence_ids": ["e11"]},
  "boundary": {"scope": "sample", "sample_size": 12, "proxy_flags": ["y_proxy"]},
  "uncertainty": ["代理阅读量口径为估计值，E 仅同口径可比"]
}
```

**后处理护栏**（`src/mylocalai/attention/deep_read_guard.py`，不依赖模型自觉，全部代码实现）：
议题封闭性（清单外 → other/low 或丢弃）、证据有效性（无效 `evidence_id` 剔除，
剔除后为空 → 丢条目）、L3 挂靠（全篇无 L1/L2 时 L3 降级为显式不确定性）、
禁词禁归因（`banned_patterns` 正则：具体主体归因 / 样本推普遍 / 价值判断 / 预测，
命中整段丢弃并记 `guard_dropped`）、反方必填、字数上限（超限截断 + uncertainty 追加）。
任何条目被丢弃 → `degraded=true`。

**降级行为**：LLM 不可用或输出被护栏全丢 → 本地模板回退（按结构化字段拼接，
不做自由生成），`degraded=true`，`degraded_reason` 记录是 LLM 失败还是护栏丢弃。
前端据此显示「降级：本地模板」「未触发深读解读」「代理/估计值」三种徽章。

```bash
# 命令行分析时联网估算热度（需联网；失败自动退回占位值，不影响主流程）
python -m mylocalai.cli --attention-online "某段待分析的文本"
```

**读数边界（页面会原样标注）**：话题模式下 `x` 是检索命中的独立文章数（下限，不是全网总数），
`y`/`z` 是代理值——**没有任何站点公开文章级精确阅读量**（搜狐文末「阅读 ()」为空、
人民网文章页的 222 是模板占位），所以页面一律显示「代理 / 估计值」徽章，
`E` 只适合同口径下的相对排序。manual 模式则是用户自己给的数，算的是确定性公式本身。

实测结论（5 个真实头部话题，2026-10-26）：默认参数下 `E>66` 需要 `y` 达 1e8 量级，
本批 5 个话题 0/5 触发（最高 54.81）。详见 `docs/关注度评估报告-2026-10-26.md`。

## 判定口径可配置

风险分阈值、无事实声明封顶、证据抬分锚点、信源分级名单，以及**"哪几档算可靠/可抓/可扩链"
与各档渠道权重**都在 JSON 里，**换行业/换判定标准（含重命名档位）不需要改 Python**：

- `src/mylocalai/config/verdict_rules.json` —— 言论分层阈值（`speech_tier`）、风险分阈值、封顶、
  抬分锚点、稳定性护栏、话术特征权重（`markers`）
- `src/mylocalai/config/sources_registry.json` —— 信源名单、站名表、`reliable_tiers` / `fetch_tiers` /
  `link_tiers` / `channel_score`（按档名声明，改档名不再静默失效）
- `src/mylocalai/rumor/prompts.json` —— 三模块 System Prompt、报告模板、枚举白名单（2026-10-22 修复 G8）
- `src/mylocalai/llm/providers.json` —— 提供方表（密钥/地址/模型名成组绑定 + 能否关思维链，修复 G4）
- `src/mylocalai/attention/attn_rules.json` —— 关注度公式参数、档位表、深读阈值（2026-10-26）
- `src/mylocalai/attention/deep_read_rules.json` —— 深读触发规则（absolute/quantile/change_rate/any ×
  按档位覆盖）、受控生成参数、护栏禁词正则（2026-10-29）
- `src/mylocalai/attention/society_issues.json` —— 封闭议题清单（40 项，LLM 只能从中选 `issue_tags`）
- `src/mylocalai/attention/prompts.json` —— 深读受控生成提示词（system 硬约束 + few-shot 正反对比）
用 `RUMOR_RULES_PATH` / `RUMOR_SOURCES_PATH` / `RUMOR_PROMPTS_PATH` / `LLM_PROVIDERS_PATH` /
`DEEP_READ_RULES_PATH` / `DEEP_READ_ISSUES_PATH` / `DEEP_READ_PROMPTS_PATH`
可整套替换，用对应的 `_EXTRA_PATH` 可只做增量。配置写坏只退回默认值并留告警
（`rules.load_warnings()` / `prompt_config.warnings()` / `llm_client.provider_warnings()` /
`deep_read.load_rules()` / `deep_read.load_issues()` / `deep_read.load_prompts()`），
**既不崩、也不会把官方来源静默降级**：已知结构（`tiers`/`official_sites`/`commercial_sites`/
档位角色、阈值/封顶/抬分锚点/稳定性/话术权重、提示词各段）在合并后会做类型校验，
不合规的段退回内置默认并留下告警。

**言论分层只有一处实现**（`src/mylocalai/core/tiering.py`）：`core_judge.judge` 与
`verdict.speech_tier` 共用同一份规则与阈值，不再各写一遍 if-else；阈值改
`verdict_rules.json` 的 `speech_tier` 段即可，两条链路一起变。

## 稳定性与抗压保护

| 问题 | 措施 |
| --- | --- |
| 同一文本重复分析结论会跳 | 进程内结果缓存（同请求只真正调用一次）+ 情绪分向下归并（`LLM_CACHE_TTL_S` / `verdict_rules.json` 的 stability） |
| 上游不可达时单次请求挂几分钟 | 请求级时长预算：`JUDGE_DEADLINE_S`（默认 120 秒），用尽即降级；预算会继承进 Agent 的工作线程 |
| 上游抖动被重试放大 | `LLM_TIMEOUT_S`（30）、`LLM_MAX_RETRIES`（1）；"关闭思维链"参数被拒才回落一次（用不再内部重试的客户端，不叠乘）；**只有 4xx 参数类错误**才回落——5xx/超时/DNS 不会把该提供方永久标记成"不支持该参数" |
| 上游失败时并发相同请求被放大 | single-flight 在失败路径也共享结果（4 个并发同请求 → 上游只被调用 1 次）；**关缓存不影响 single-flight**，只失去跨请求复用 |
| 预算被超出 | 预算在**每次 HTTP 尝试之前**与**每个提供方切换之前**都查一次；单次尝试超时不超过剩余预算（下限可配 `LLM_MIN_ATTEMPT_S`，默认 0.25s）；single-flight 的等待者最多等到预算耗尽，不再空等 30 秒 |
| 网站被慢请求拖死 | `/api/analyze` 应用层并发上限（超出返 503），`/api/health` 不碰大模型链路 |
| Agent 超时后后台线程继续跑 | 超时即取消：后台线程 daemon 化 + 协作取消，残留工具调用不再开算（`read_source` 也会先检查 `state.cancelled`，不再发真实抓取） |
| 前端无法中断长请求 | 分析中显示"取消分析"（AbortController） |
| 混入无法编码的字符（孤立代理项）导致 500/崩溃 | 文本进入分析前统一规整为可编码形式；网站入口对这类输入回 400 而不是 500 |
| 空输入仍打大模型 | `judge()` 对空/纯空白输入直接短路返回本地结论，**一次 API 都不调**（改造前还会走一次报告生成） |
| 配置写坏静默生效 | 阈值/封顶/抬分锚点/信源名单写坏时退回默认并留告警（`rules.load_warnings()`）；"整套替换漏段"与"增量文件读取失败"两类告警不会再被形状校验抹掉 |
| 不同链路复用同一份 LLM 缓存互相串结果 | `llm_client.chat_text/chat_json` 支持 `namespace` 缓存分区名；深读用 `attn_deep_read`，与主分析算出不同缓存键（分区名为空时键与加参数前逐字节相同，不悄悄换掉全部缓存） |
| 降级了但看不出来 | `/api/health` 增加 `llm{usable, provider, model, base_url, error}`；classic 的分析记录带 `degraded_components`（哪些组件不可用、报告是否走了本地模板回退） |
| 英文/非 UTF-8 控制台打印中文崩溃 | 各命令行入口统一 `force_utf8_stdio()`（`errors="replace"`） |

## 测试

```bash
# 离线测试套件（不消耗 API 配额）
.venv/Scripts/python.exe -m unittest tests.test_rumor_agent_offline tests.test_browser_mcp \
    tests.test_mutations tests.test_opinions tests.test_stability_config \
    tests.test_p0_fixes_2026_10_22 tests.test_prompt_config tests.test_p1p2_fixes_2026_10_23 \
    tests.test_fixes_2026_10_24 tests.test_attention tests.test_attention_profiles \
    tests.test_core_judge_attention tests.test_deep_read tests.test_deep_read_stage1 \
    tests.test_deep_read_controlled tests.test_website_attention \
    tests.test_import_rules tests.test_llm_gateway

# 真实 API 端到端（会消耗配额）
.venv/Scripts/python.exe tests/rumor_agent_e2e.py
# 结果默认写 tests/；在"不许写仓库"的检验环境里改到临时目录：
.venv/Scripts/python.exe tests/rumor_agent_e2e.py --out .tmp/verify/out
```

离线套件共 638 个用例：Agent 流程与注入免疫、浏览与 MCP 约束、变异测试（确认约束真被验到）、
民间观点聚合、两轮修复的验收（可配置口径与档位角色、稳定性护栏、时限与预算继承、
提供方绑定与备用链路、single-flight 失败共享、并发上限、不可编码输入、无写死路径），
以及 2026-10-22 检验报告 P0 的回归（配置告警不再被抹掉、关缓存不再连带关掉 single-flight、
预算下限可配、瞬时故障不再被当成"参数不支持"、read_source 取消后不再抓取、
空输入 0 次 API 调用、非 UTF-8 控制台规整、/api/health 暴露上游可用性），
外加 P1/P2 的回归（提供方表/提示词/分层阈值/话术权重外置、三维模块并行、证据本空置信度、
解释一致性、网站 agent 三维、多进程可观测、Agent 超时对齐、缓存清扫、依赖版本上限、启动脚本），
以及 10-24 复核补修的回归（报告与结论一致性 + 不一致回退模板 + REPORT_MODE、
证据本"全被过滤"标记的真实代码路径），
以及 10-26 关注度模块的回归（公式与独立参考实现逐点对比、开区间边界、
`python -O` 下边界守卫仍生效、配置写坏退回默认、档位与深读阈值语义、
话题估参口径与时间短语假阳性、**前后端自洽性**：前端引用的接口路径与元素 id
必须在后端路由和 HTML 里真实存在），
以及 10-29 深读受控生成的回归（触发矩阵 absolute/quantile × spec/reachable、
信号派生与公式逐点一致、议题封闭性、证据覆盖、L3 挂靠、禁词红队、
`applicable=false` 出口、反方必填、字数上限、**物理隔离**（AST 检查 deep_read
不 import verdict、深读异常不影响主结论、**LLM 缓存分区端到端验证**）、降级、
配置告警、向后兼容）。
另有 `tests/run_module_tests.py`（12 条内置用例全模块跑一遍并落盘）、
`tests/smoke_api.py`（配置自检 + 连通性）。

## 已知局限

- **单一提供方为默认**：默认只配 LongCat；备用链路需要自己填别家的密钥才生效。
- **本地备选推理已归档**：`module_inference.py` 及两个本地模型的旧训练链路
  （modelscope 依赖缺失 / HF config 不兼容）已于 2026-10-30 移入 `archive/`，主链路只走 API。
- **本地模型不认 seed**：LongCat 在 temperature=0 下仍有采样漂移，且忽略 `seed`，
  所以复现性靠调用侧缓存与分桶，不是靠采样参数。
- **蒸馏实验已归档**：`distill/`（含教师标签测试集）移入 `archive/`，作为 L5 阶段的教师标注源按需重启。
- **信源名单是人工整理的**：不完整，名单外的好来源会被归为"未知来源"（有意的保守取向）；
  可用增量配置文件补。
- **演示级部署**：网站监听 127.0.0.1、单进程、无鉴权，历史记录在内存里（重启清空）。
  多 worker 部署时缓存与历史各自独立（见 `my_website/README.md`）。

## 文档

- `docs/研究报告-2026-10-30.md` —— 重塑前的质量基线与实测证据（智能助手重塑的依据）。
- `docs/工作指导-智能助手重塑-2026-10-30.md` —— 重塑工作的唯一执行依据（里程碑与验收）。
- `docs/项目报告-2026-10-26.md` —— 项目现状报告（关注度接入网页端与深读之后）。
- `docs/关注度评估报告-2026-10-26.md` —— 关注度公式与阈值的评估报告。
- `docs/关注度公式与深读阈值标定报告.md` —— 公式标定过程与局限清单。
- `docs/检验报告-2026-10-22.md` —— 稳定性 / 抗压性 / 功能落地 + 泛用性优化方案（含 P0 修复后的复验结果）。
- `docs/修复说明-2026-10-24.md` —— 10-24 复核补修：F2 报告一致性、F6 标记死代码等遗留项。
- `docs/修复说明-2026-10-23.md` —— 针对 10-22 报告 P1/P2 的修复落地说明（含实测证据）。
- `docs/修复说明-2026-10-22.md` —— 针对 10-22 报告 10 项 P0 的修复落地说明（含前后数字对比）。
- `docs/检验报告-2026-10-15.md` —— 上一轮完整检验报告与 13 项优化方案。
- `docs/检验报告-2026-10-14.md` —— 更早一轮检验报告（14 项优化方案的来源）。
- `docs/修复说明-2026-10-15.md` —— 针对 10-14 报告的 14 项修复的落地说明。
- `docs/修复说明-2026-10-15-第二轮.md` —— 针对 10-15 报告的修复落地说明（含实测验证）。
- `docs/修复说明-2026-10-15-第三轮.md` —— 档位角色随配置走的修复说明。
- `docs/opinions_module_spec.md` —— 民间观点聚合模块的规格说明。
- `src/mylocalai/rumor/README.md` —— 谣言 Agent 的架构、证据本、受控浏览、MCP 与降级行为。
- `my_website/README.md` —— 展示网站的启动方式与页面功能。

## 智能助手 MCP 接入（2026-10-31，M2-9）

把助手核心（AssistantLoop）暴露成 MCP 工具，与网页端对话共用同一 loop 核心（同核：同一输入得到同一卡片）：

    .venv/Scripts/python.exe -m mylocalai.assistant.mcp_server    # stdio 传输

需要 mcp 包：`pip install mylocalai[mcp]`。暴露三个工具：

- `assistant_chat(message, session_id?)` —— 与助手对话一回合，同步返回结构化 JSON：
  reply（叙述）/ card（结论卡片载荷，与 web 端卡片事件同形状）/ action / dossier_id /
  degraded / cost / events（事件名序列）。传回返回值里的 session_id 可延续会话上下文。
- `assistant_sessions(limit)` —— 列出会话（id 可回传 assistant_chat）。
- `assistant_dossiers(query?, limit?)` —— 列出核查档案。

口径：助手**不判真假、只述证据**——汇总可核实的证据与来源立场并给出确定性结论卡片，
真伪裁决留给用户；降级时如实说明，不编造。库路径与网页端同库（data/dossier.db，
DOSSIER_DB_PATH 可覆盖）。测试：`tests.test_mcp_assistant`（同核对照、工具清单、守护路径）。
