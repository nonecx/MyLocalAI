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
│   │   ├── llm_client.py       提供方成组绑定 + 配置自检 + 备用链路 + JSON 稳健解析 + 进程级真实用量计数（M4）
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
│   ├── dossier/                档案库（SQLite 单文件 + WAL）
│   │   ├── store.py            连接工厂、schema_version 顺序迁移（v4 指纹表+话术标记表、v6 Profile 工作区、v7 材料批次、v8 监测订阅）、档案域七表 + 会话域三表
│   │   ├── profiles.py         Profile 工作区（多档案数据分家 + 角色，v6）
│   │   ├── batches.py          材料批次存储（v7）
│   │   ├── batch_summary.py    批次汇总纯函数（零 LLM，永不抛）
│   │   ├── subscriptions.py    监测订阅存储（v8）
│   │   ├── service.py          建档编排：结论卡片化落库、证据批次、recheck 版本化、六章节聚合与导出、初扫 markers 沉淀
│   │   └── insights.py         参谋三聚合纯函数：话术模式库 / 信源画像 / 周报（零 LLM，永不抛）
│   ├── assistant/              智能助手核心（对话 → 路由 → 工具 → 叙述 → 落库）
│   │   ├── loop.py             外层工具循环（事件流、单轮上限、轮预算 CHAT_TURN_DEADLINE_S、取消链）
│   │   ├── router.py           意图路由（chat_json + 封闭枚举 + 四层防护链，绝不猜）
│   │   ├── registry.py         工具注册表（schema 校验、权限分级、成本/延迟元数据）
│   │   ├── tools_verify.py / tools_heat.py / tools_memory.py / tools_chapters.py
│   │   ├── tools_batch.py      材料批次编排（阶段 2：同源分组 + 逐条核查 + 双预算）
│   │   └── tools_monitor.py    领域监测编排（阶段 3：授权闸 + 检索 + 变化摘要）
│   │   ├── fingerprint.py      话术指纹纯函数（归一化/shingle/Jaccard——谣言库秒回的地基）
│   │   ├── reminders.py        被动提醒纯函数（档案年龄/热度变化 → 一行提示）
│   │   ├── context.py          上下文窗口构建器（12k 字符预算、裁剪与摘要）
│   │   ├── events.py           SSE 事件协议（封闭枚举：meta…done/error）
│   │   ├── session_store.py    会话域三表读写（sessions/messages/tool_runs）
│   │   ├── mcp_server.py       助手 MCP（python -m mylocalai.assistant.mcp_server）
│   │   ├── prompts.json        人设与固定话术——改这里而非改代码
│   │   └── assistant_rules.json  路由模型/上限/预算/指纹阈值/信源画像阈值/拒绝词表——改这里而非改代码
│   ├── web/                    FastAPI 网站 + static/（src/mylocalai/web/app.py）
│   │   ├── app.py              REST API 全貌（分析/关注度/对话/会话/档案/画像/Profile/批次/监测）+ lifespan 调度
│   │   ├── profiles_api.py     Profile 工作区 CRUD + 激活（v6）
│   │   ├── batch_api.py        材料批次 API + SSE 进度（v7）
│   │   ├── materials_api.py    上传文件内存解析（txt/md/csv/json，不落盘）
│   │   ├── monitor_api.py      监测订阅 API + 调度线程（v8）
│   │   └── static/             单一对话页的全部界面：index.html + shell.js（file:// 引导/服务状态）
│   │                           + chat.js/cards.js（会话、档案库、事件流与全部卡片渲染）+ chat.css
│   │                           + profiles.js（档案切换器）+ batch.js（材料批次面板）+ monitor.js（领域监测面板）
│   ├── util/                   text_safety（代理项规整）/ console_io（stdio 规整）
│   └── cli.py                  命令行入口（python -m mylocalai.cli）
├── my_website/                 启动器（start.bat / start.sh）与说明，实际代码在 src/mylocalai/web/
├── tests/                      离线测试套件与真实 API 端到端脚本（import mylocalai.*）
├── docs/                       检验报告、修复说明、项目/研究报告与关注度标定报告
├── pyproject.toml              src 布局包定义（pip install -e .）
└── archive/                    遗留资产归档区（2026-10-30 清场建立，不入库）
```

依赖方向只有一条：`core ← llm ← retrieval ← {classic, rumor, attention} ← {dossier, assistant} ← web/cli/mcp`；
classic / rumor / attention 互不 import，dossier/assistant 不得被上游反向 import
（`tests/test_import_rules.py` 用 AST 锁定）。

## 快速开始

```bash
# 1. 建虚拟环境并安装依赖（可选依赖见 requirements.txt 注释），再可编辑安装本包
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -e .            # src 布局：import mylocalai.* 全仓库可用

# 2. 配置密钥：复制样例文件后填入自己的 Key
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env

# 2b. 首次运行授权（默认 fail-closed，不授权就不落盘、不外发）
#     保存三类数据 + 授权外部提供方，均在 data/privacy_preferences.json（不入库，见下）
#     GET  /api/privacy/preferences   查看当前授权
#     PUT  /api/privacy/preferences   设置（网页端调用或 curl 均可）

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

**网页端已实现**（双击 `my_website/start.bat`：单页对话 composer 上方的「算热度」快捷面板，
填 x/y/z 后经结构化 `action={"name":"compute_heat","args":{x,y,z}}` 发一轮对话，纯公式、零 LLM）。
接口契约全部不变，只是页面入口由原「工具模式」页合并进对话页：

| 能力 | 接口 | 说明 |
| --- | --- | --- |
| 手动算分 | `POST /api/attention` | 对话页「算热度」面板填 x/y/z（等价于 `compute_heat` action），纯确定性返回 E、S、三个分项、档位、是否触发深读 |
| 话题联网估参 | `POST /api/attention/search` | 输入话题，`src/mylocalai/attention/estimate.py` 走真实检索估出 x/y/z 后算分；单页化后只保留接口，页面暂无联网估参入口 |
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
# 离线测试套件（不消耗 API 配额；全量 discover 一键跑）
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p "test_*.py"

# 一键 CI（M4）：compileall src → 离线套件 → mock 闸门，任一步失败即整体失败
# （mock 闸门脚本在 .tmp/ 本地检验资产里，缺失时该步打印 SKIP，前两步照常判定）
scripts\ci.cmd
# （POSIX shell 环境参考 scripts/ci.sh；本机沙箱子进程跑 bash 会崩，Windows 一律用 ci.cmd）

# 模拟上游闸门（不耗配额，28 条：26 条既有 + 对话轮预算/SSE 取消两条 M2 探针）
# 注意：脚本在 .tmp/verify-2026-10-22/（.tmp/ 不入库），属本地检验资产；
# 缺脚本时 ci.cmd 第 3 步自动跳过并打印 SKIP，离线套件不受影响
.venv/Scripts/python.exe .tmp/verify-2026-10-22/mock_run.py

# 真实 API 端到端（会消耗配额）
.venv/Scripts/python.exe tests/rumor_agent_e2e.py
# 结果默认写 tests/；在"不许写仓库"的检验环境里改到临时目录：
.venv/Scripts/python.exe tests/rumor_agent_e2e.py --out .tmp/verify/out
```

离线套件共 1354 个用例（unittest discover 全量，2026-11-20 角色扩展 + 取证三技能合并后实测；
**M4 起由 `tests.test_readme_counts` 自动校验此数字与 mock 闸门条数，改动后须同步回写**）：
M1-M3 的全部回归（详见 git 历史），加上 M4 新增模块：
**成本真实计数**（llm_client 进程级计数器：真实上游调用数 + usage tokens，缓存命中/
single-flight 等待不计、取不到 usage 如实 0；loop 按"段前段后快照 delta"接线进
done.cost / tool_runs / 档案 analysis_runs，计数面不可用退回粗口径）、
**README 数字守护**（用例数 / mock 闸门条数 / 引用的测试与文档文件存在性，治文档漂移）、
web 会话与档案成本汇总端点及前端会话累计徽章的自洽；
加上 L4 新增模块：**参谋三聚合**（dossier/insights.py 纯函数 + dossier_markers v4
沉淀 + 三端点与对话内 insights 卡前端自洽）、**信源画像反哺抬分**（collect_evidence
source_profile 注入：空画像逐位不变守卫、抬分端到端、装配透传）；
加上单页化新增模块：**insights 只读工具**（kind 封闭枚举 patterns/sources/weekly、
零 LLM、空库合法空卡）、**结构化 action 通道**（未注册/不在 allowlist/写工具/坏参数
一律 400，合法时跳过路由直达只读工具，事件协议不新增）、**卡片折叠字段**
（结论卡 evidence_preview 前 5 条、言论土壤卡 report 完整报告）；
加上角色扩展阶段 1 新增模块：**Profile 工作区**（v6 迁移与存量回填、CRUD/激活/删除
保护、会话/档案/指纹/画像按 profile_id 分家、跨工作区 404 防护、layperson 结论卡
简化、/api/profiles 与前端切换器前后端自洽）；
加上角色扩展阶段 2 新增模块：**材料批次**（v7 batches/batch_items 存储、同源分组
（assistant 层指纹，保守不并组）、逐条 RumorAgent 核查 + 自动建档、双预算与取消的
诚实降级、汇总纯函数与 batch_summary 卡、/api/batch CRUD + SSE 进度 + 并发信号量、
上传内存解析四格式、前端材料批次面板与前后端自洽）；
加上角色扩展阶段 3 新增模块：**领域监测**（v8 subscriptions/subscription_runs、
search_all site: 领域变体（不传逐位不变）、授权闸 blocked 零外发、监测档案证据
批次 + 关注度快照现算、变化摘要纯函数（URL 差集 + E 首末差）、/api/subscriptions
CRUD + run-now 防重入 + lifespan 调度线程、前端监测面板）；
加上取证三技能新增模块：**安全取页**（`tests.test_skills_transport` 31 例：DNS 钉扎/
混合解析/逐跳重定向/超量/压缩/PDF/死线/并发槽位）、**三技能确定性编排**
（`tests.test_skills_pipeline` 26 例：分类标签白名单与原文定位、信息不足先追问、
信源身份不可自封、同域名不产生第二个权威、跨站转载去重、三阶段只接受已核验信源、
引文定位与关系枚举、PII/范围操作符剥离、预算耗尽保留部分结果、报告转义与不出现
真假判定）、**collect_evidence 工具与研究助手接线**（`tests.test_research_assistant`
22 例：工具注册与校验、取消与绝对死线透传、六种状态保真、路由与 allowlist、
上下文不伪造档案号、报告引用逐字保留）。
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
- **演示级部署**：网站监听 127.0.0.1、单进程、无鉴权。对话会话/消息/工具运行与核查档案
  已持久化到 `data/dossier.db`（SQLite + WAL，重启保留）；`/api/analyze` 的历史记录仍是
  内存轻模式（重启清空，契约不变）。多 worker 部署时缓存与历史各自独立（见 `my_website/README.md`）。
- **真实上游冒烟依赖提供方状态**：LongCat 有返回空 content 的前科（2026-10-14 检验报告、
  2026-10-31 M2 末冒烟复现两次），冒烟失败不代表链路回退——离线套件与 mock 闸门全绿为准。

## 文档

- `docs/Agent工作流规格-v0.1.md` —— **Agent 工作流实施基线**（用户能力、主张/传播证据双维判定、外发授权、分类保存与验收矩阵；规格不等于所有阶段已实现）。
- `docs/取证技能-2026-11-05.md` —— 取证三技能（领域识别/信源发现/证据检索）的口径文档：分工、执行流程、安全模型、状态取值、预算与局限。
- `docs/智能助手Skill工作流设计讨论-2026-11-07.md` —— Skill 融入工作流的问题与方向讨论汇总（两套证据链现状、能力分层、混合表达、内部机制不暴露、待决问题清单）。
- `docs/使用说明书.md` —— **使用者操作手册**（安装/配置/启动/单页对话与左栏/命令行/MCP/配置调整/数据备份/常见问题）。
- `docs/研究报告-2026-10-30.md` —— 重塑前的质量基线与实测证据（智能助手重塑的依据）。
- `docs/评估-真流式-2026-10-31.md` —— M4 真流式评估（现状/要动的面/收益/风险；结论：建议 M5 前不做，拍板留给作者）。
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

## 智能助手（2026-10-31，M2；单页化后入口全部合并进对话页）

网站首页是**单一对话页 + 左栏**，不再有"工具模式""参谋"两个 Tab：把"这条消息是真的吗"
丢进对话框，拿回一张带证据、带置信度、带"什么时候该再看一眼"的结论卡片。
设计口径见 `docs/工作指导-智能助手重塑-2026-10-30.md`。

- **单页 + 左栏（单页化）**：页面只有一个对话流——顶栏（品牌 + 服务状态 chip + 「关于」弹层）、
  左栏三块（会话 / 档案库 / 我的画像）、消息流、composer 上方四个快捷 chips
  （核查真伪 / 言论土壤 / 算热度 / 我的画像）。原「工具模式」与「参谋」Tab 已下线、
  `static/app.js` 与 `static/insights.js` 已删除（改为 `shell.js` 只做 file:// 引导与服务状态）；
  能力全部并入对话，`/api/analyze`、`/api/attention*`、`/api/insights/*` 端点契约不变，只是页面入口变了。
- **结构化 action 通道**：`POST /api/chat` 支持可选 `action={"name","args"}`，供精确输入类入口
  （「算热度」的 x/y/z、「我的画像」的三种 kind）使用——只放行只读白名单工具，未注册/越界参数/
  写工具一律在 SSE 开始前 400 拒绝；合法时跳过路由模型直达工具，事件协议一个都不新增。
- **我的画像（原参谋三块）**：话术模式库 / 信源画像 / 本周简报由 `insights` 只读工具
  （kind 封闭枚举、零 LLM、成本恒 0）在**消息流里出一张 insights 卡**呈现，数据只来自本机档案库。
- **三档车道**：闲聊/追问（轻）；analyze_speech 言论土壤（快）；verify_claim/recheck 联网核查（慢查 30-120s，
  先发本地初扫 markers + 组件级进度，绝不允许白屏干等）。
- **路由四层防护链**：封闭枚举白名单 → 参数 schema 校验 → 工具 allowlist → 权限；任一步失败降级
  clarify（追问一次），绝不猜；用户消息里的"改分数/删档案/越权"指令一律 refuse，对话层注入由
  15 个变异用例锁定无效。
- **卡片三层信息架构**：标签+置信度 → 证据计数/as_of/degraded 徽章 → 证据本前 5 条；
  卡片永远在叙述之前；叙述失败时以卡片+固定模板收尾，永不丢结论。
  折叠细节：结论卡把证据本前 5 条收进 `<details>`，言论土壤卡把完整分析报告收进 `<details>`
  （超长由后端截断并标 `report_truncated`）。
- **结论卡片口径**（全纯函数派生，大模型不参与打分）：label/risk_score（兼容层）+ confidence +
  evidence_sufficiency（official/unofficial/unknown/total）+ as_of + recheck_conditions +
  verdict_basis 标记；映射外置在 `config/verdict_rules.json` 的 `card` 段。
- **持久化**：核查自动建档（结论版本化 v1/v2…、证据按批次、分析运行记录），会话/消息/工具运行
  落 SQLite；重启后档案与会话都在。删除档案不是助手工具（只有程序内部可达）。
- **回合预算与取消**：`CHAT_TURN_DEADLINE_S`（默认 180s）经 llm_cache 预算继承进引擎链；
  前端 AbortController → SSE 断开 → 协作取消（agent 轮询 cancel_check、judge 四取消位），
  超时/取消都诚实降级、已有卡片照发。
- **取证三技能与用户能力分层**（2026-11-20）：面向用户的能力收敛为四个**用户能力 Skill**
   （事实核查 / 证据研究 / 话题理解 / 言论理解，`src/mylocalai/assistant/capabilities/`，
   各一份人读的 SKILL.md；机器可执行契约的唯一规范来源是 registry 注册表 +
   skills/pipeline.py，不搞两处维护），内部由**取证三技能**（领域识别 → 信源发现 →
   证据检索，`src/mylocalai/assistant/skills/`）作为执行层支撑。
   新增只读工具 **`collect_evidence`**（text；"不要判断真假，只查官方资料/只找证据"）：
   三阶段确定性编排——分类标签白名单 + basis/主张/引文全部逐字定位校验，信源身份不可
   自我认证（陌生域名最多记为未核验候选、不进证据检索），`site:` 由代码拼接、模型写的
   范围操作符一律剥掉，检索词生成前先剥 PII，正文哈希 + 引文归一化去重（转载不产生
   新证据），预算/取消都保留已取得的证据，**不建档、不出真假分、无任何分数**（权限 read）。
   安全取页在 `skills/transport.py`：DNS 钉扎（公网私网混合解析直接失败）、逐跳重定向
   复核、体积/时限/内容类型上限、并发槽位。一句话同时要求"核查真假"和"解释热度"时
   不拼接工具，回一句选择话术让用户定先后（总览 §6.3）。evidence 卡把领域、已核验/
   未核验信源、主张、证据（关系 + 逐字引文 + 链接 + 时点）、缺口与错误、"已执行 N 次
   搜索/N 次读页/N 次模型调用"结构化呈现。
   注意：读网页需要"外部取页"授权（`data/privacy_preferences.json` 的
   `external_processing_providers`）；只开检索授权时选站与检索仍可用，读页会如实标注
   未授权而不是假装搜过。
- **MCP 同期交付**：见下节。

## 记忆与完整性（2026-10-31，M3）

- **谣言库指纹秒回**：每次核查把原文归一化指纹（NFKC+去标点+blake2b shingle）沉淀进
  `claim_fingerprints` 表；相似说法再次出现时**先问再查**（"查过相似说法，秒回旧结论还是重查？"），
  绝不擅自断言同一谣言。阈值/ shingle 外置 `assistant_rules.json`。待决问句只对紧随的下一轮生效；
  "重查"按旧档案原文新建档案（不自动版本化到旧档案——避免擅自断言同一谣言），用户当轮的新措辞
  可在问句后直接发消息正常核查。
- **档案库页**：左栏档案入口 → 六章节视图（核查结论版本对比、证据批次、言论土壤、热度趋势
  SVG、观点光谱、背景解读），缺章如实标 absent。
- **Markdown 导出**：`GET /api/dossiers/{id}/export?format=md` 六章节+徽章+免责语+as_of 页脚。
- **被动提醒**：档案年龄/热度变化超阈值时，打开会话以一行浅色提示呈现（规则外置），无推送无打扰。
- **change_rate 触发**：同一档案的快照序列环比增幅达阈值即触发深读（单快照不触发）。

## 成本真实计数（2026-10-31，M4）

M4 起 `requests/tokens` 是**真实上游用量**，不再是粗口径估算（§8 成本模型的落地）：

- **计数点**：`src/mylocalai/llm/llm_client.py` 进程级计数器（`threading.Lock`，跨线程安全）。
  requests 只在**真实发起并返回的上游调用**时 +1——缓存命中、single-flight 等待、预算耗尽
  未发起都不计；tokens 从响应 `usage.total_tokens` 取，取不到**如实记 0，不估算**。备用链的
  每次成功调用各计一次。
- **接线**：助手回合内按"段前段后快照 delta"分别记入——路由/叙述/摘要段进 `done.cost`
  （前端"本轮成本"徽章）；每个工具段进 `tool_runs`（会话域）与档案 `analysis_runs`
  （经 record_analysis 调用点注入）。计数面不可用时逐段退回 M4 前粗口径，绝不挡回合。
- **可见处**：每轮 done.cost 徽章；左栏会话列表的会话累计成本（`/api/sessions` 每项附
  `cost`）；`/api/sessions/{sid}/messages` 与 `/api/dossiers/{id}` 附 `cost` 汇总
  （sum 真实落库值）。
- **口径边界（如实声明）**：Agent 的 LangChain 工具轮不经 llm_client，不计数；轮内路由/
  叙述成本只在当轮徽章呈现、不落库（会话/档案汇总只累落库部分）；并发回合同时进行时，
  进程级计数器的段归属是近似。守护测试：`tests.test_cost_accounting`。
- **CI**：`scripts\ci.cmd` 一键（compileall src → 离线套件 → mock 闸门 28 条），用法见"测试"一节。
- **真流式评估（M4 结论）**：收益集中在闲聊车道首字（省约 2-4s），慢查车道几乎不动且
  LongCat SSE 支持未实测——**建议 M5 前不做**，拍板留给作者（详见 `docs/评估-真流式-2026-10-31.md`）。

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

## 用户角色与档案工作区（2026-11-20，schema v6）

把"普通用户 / 专业用户"落成**同一产品内的多个档案工作区（Profile）**，不做账号体系
（本项目明确不做多用户与鉴权，部署口径是单用户本地工具）。顶栏切换器一键换边，
每个工作区有**独立的会话、档案与画像数据**（`profile_id` 分家，跨工作区访问一律按
"不存在"处理，不泄露别的工作区有哪些会话/档案号）。

| 角色 | 面向 | 现状（2026-11-20 阶段 1） |
| --- | --- | --- |
| `layperson` 普通用户 | 只做言论核查：输入一条消息，拿回结论 | 结论卡**简化呈现**（隐去 risk_score 数值、标注 presentation=simple，完整数值仍在档案库）；能力不裁剪 |
| `analyst` 专业用户 · 材料分析 | 分析辨别自行搜集的多条材料 | 批量核查入口建设中（阶段 2） |
| `monitor` 专业用户 · 领域监测 | 按领域自动搜集信息 | 订阅 + 服务内定时线程建设中（阶段 3） |
| `full` 全部能力 | 不按角色裁剪 | v6 迁移给存量数据的默认档，保证旧行为逐位不变 |

- **数据模型**：`profiles` 表（key/display_name/role）+ `app_state` 单行激活态；
  `sessions`/`dossiers` 加 `profile_id`（v6 迁移把存量数据回填到"默认档案"，
  role=full，旧档案可读、行为不变）。
- **接口**：`GET/POST /api/profiles`、`PUT/DELETE /api/profiles/{id}`、
  `POST /api/profiles/{id}/activate`；列表/详情/画像端点一律按激活档案过滤。
- **删除保护**：最后一个档案、当前激活档案、仍有会话/档案数据的档案一律拒删（409）。
- **口径**：角色只改**呈现与数据范围**，不改判定语义；`verdict.assess` 打分、
  隐私 fail-closed 授权、路由四层防护链均不随角色放宽。
- 测试：`tests.test_profiles`（迁移回填 / CRUD / 分家 / 跨区防护 / layperson 卡片 /
  Web API 全链）。

### 专业用户 A · 材料批量分析（阶段 2）

`analyst` 档案工作区左栏「材料批次」面板：多行文本（每行一条）/ 文件上传
（txt/md/csv/json，内存解析不落盘）/ URL 列表（受控抓取，未授权记 blocked）三种输入，
一次最多 8 条、双预算（条目 90s + 整批 900s）。逐条走 RumorAgent 全链路并自动建档；
同源材料（指纹 Jaccard ≥ 0.72）分组呈现但**各自判定**；单条失败不阻断，预算用尽或
取消的条目标 `not_run`（不把"没跑"显示成"没发现"）。

- **接口**：`POST /api/batch`（202 + 后台跑）、`GET /api/batch`、`GET /api/batch/{id}`、
  `GET /api/batch/{id}/stream`（SSE 进度）、`POST /api/materials/upload`；
- **汇总卡**（`batch_summary`，零 LLM 纯函数）：状态计数 / 结论分布 / 疑似同源组 /
  逐条结论与档案链接 / 降级清单；
- **schema v7**：`batches` + `batch_items`（group_id 同源分组、status 六态）；
- 测试：`tests.test_batch`（存储 / 分组 / 汇总 / 编排八场景 / Web API / 上传解析）。

### 专业用户 B · 领域监测订阅（阶段 3）

`monitor` 档案工作区左栏「领域监测」面板：订阅话题（可选关键词与域名限定，
频率下限 300 秒），到点由**服务内调度线程**自动跑检，也可手动"立即运行"。每轮
受控检索（`site:域名` 变体 ≤3 个，引擎不支持时如实标注）、证据落订阅专属监测
档案、关注度快照用本次检索结果现算（零额外联网）；变化摘要只述事实（URL 差集 +
E 分首末差，不外推）。

- **接口**：`GET/POST /api/subscriptions`、`PUT/DELETE /api/subscriptions/{id}`、
  `POST /api/subscriptions/{id}/run-now`（202 + 后台，409 防重入）、
  `GET /api/subscriptions/{id}`（详情 + 运行历史 + 最新变化卡）、`GET .../runs`；
- **授权闸**：每轮运行前查 public-search 外发授权，未过 → `blocked`、订阅暂停并
  写明原因，**零外发**（fail-closed）；
- **调度**：web lifespan 起 daemon 线程（tick 60s、串行、单 worker 假设 + 启动
  告警）；关服即停，产品口径如实告知；
- **schema v8**：`subscriptions` + `subscription_runs`；
- 测试：`tests.test_monitor`（存储 / due / 变化摘要 / 编排含 blocked 零外发 /
  失败不卡死 / API / 调度 tick）。

## 参谋与洞察（2026-10-31，L4）

"越用越聪明"（§2.5）的兑现点：三种积累全部 **SQLite 聚合 + 纯函数离线推导，零 LLM 调用**，
数据只来自你自己的档案库。单页化后不再单独开「参谋」Tab：左栏「**我的画像**」三个按钮
（话术模式库 / 信源画像 / 本周简报）点一下就在对话流里出一张 `insights` 卡，
也可以直接对助手说"看看我的画像"。三个只读端点契约不变，仍可直连：
`GET /api/insights/patterns`、`/api/insights/sources`、`/api/insights/weekly?days=7`
（页面走 `insights` 工具的 `action` 通道，不直连端点——直连会绕开会话史、事件流与取消链）。

- **话术模式库**：核查落库时把初扫 markers（patterns 六类风险特征 + 三类可信度线索）结构化
  沉淀进 `dossier_markers` 表（schema v4，一档案一条幂等）；跨档案聚合出"你查过的档案里
  各话术模式命中多少、最新结论标签分布、常见组合是什么"（`tests.test_insights`）。
- **信源画像反哺抬分**：按域名聚合证据史（域名从 url 现算，与引擎同口径 normalize）——
  某域名在你的档案里反复为**已证伪结论**（高风险谣言/疑似谣言）提供反驳证据，默认 ≥2 次
  （`assistant_rules.json` 的 `source_profile.min_refutes` 可调）即记入个人画像；此后
  verify/recheck 装配引擎时把画像注入 `collect_evidence(source_profile=…)`，该域名当前的
  "反驳"条目并入可靠反驳独立域名集合（官方口径不变），stance_summary 增加
  `profile_refute_domains`/`profile_refute` 标注。**空画像/不传画像时输出与从前逐位不变**（守卫测试锁定）；
  画像只来自档案库 SQL，LLM 输出没有写画像的通道（`tests.test_source_profile_lift`）。
- **个人信息环境周报**：按时间窗（默认 7 天，1-90 可调）聚合新档案 / 结论分布 / 分析运行与
  真实成本（requests/tokens）/ 高频话术 / 辟谣主力域名 / 热度快照 / 指纹沉淀量；无推送无打扰，
  点「本周简报」才算（§2.2 不制造新压力）。
- 三端点全部只读、聚合函数永不抛；库不可用时降级为 503 或合法空结构（均为诚实降级）；
  画像卡数据为空显示"暂无数据"。`insights` 工具的 digest 只把**计数**交给叙述层，
  域名明细只进卡片、不进模型上下文。
  测试：`tests.test_insights_api`（端点 + 前后端自洽）、`tests.test_insights`、
  `tests.test_source_profile_lift`。
