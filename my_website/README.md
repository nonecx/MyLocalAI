# 网络言论智能分析系统 —— 展示网站

给 MyLocalAI 分析项目做的前后端一体展示站点：网页输入一段网络言论，
后端复用根目录的分析链路（三个大模型 API 分析模块 + if-else 规则引擎 + 报告生成），
在页面上展示四档结论、三维分析卡片和完整报告。

## 目录结构

```text
my_website/
├── app.py              # FastAPI 后端：分析/历史/健康检查接口 + 静态页托管
├── requirements.txt    # 网站额外依赖（fastapi、uvicorn）
├── start.bat           # Windows 一键启动（端口/解释器可配）
├── start.sh            # Linux / macOS 一键启动（同上）
├── static/
│   ├── index.html      # 页面结构
│   ├── style.css       # 样式
│   └── app.js          # 交互逻辑（原生 JS，无框架、无构建步骤）
└── README.md
```

## 快速开始

前提：项目根目录的 venv 已建好，且根目录 `.env` 里配置了 `LLM_PROVIDER`（默认 longcat）
与对应提供方的密钥（如 `LONGCAT_API_KEY`）。可用
`.venv/Scripts/python.exe tests/smoke_api.py` 先做配置自检。

**方式一（最简单）**：
- Windows：直接双击 `my_website/start.bat`
- Linux / macOS：`cd my_website && ./start.sh`（或 `bash start.sh`）

两个脚本都会自动启动服务、2 秒后打开浏览器，并在**找不到 venv 时回落到 PATH 上的
python**（找不到任何解释器时给出可操作的报错，而不是直接崩）。
脚本为纯 ASCII + CRLF 编码以规避 cmd 中文代码页解析问题，窗口内文字为英文：
`Close this window to STOP the server` = 关窗口即停止服务。

端口与解释器可用环境变量覆盖（2026-10-22 修复 G7）：

```bash
PORT=8010 ./start.sh                  # 换端口（8000 被占用时用）
PYTHON=/path/to/python ./start.sh     # 指定解释器
```

> 若端口已被占用会报绑定错误，说明服务已在运行，直接访问对应端口即可。

**方式二（命令行，在项目根目录执行；路径按你的实际位置替换）**：

```bash
# 1. 安装网站依赖（只需一次，装进项目 venv）
.venv/Scripts/python.exe -m pip install -r my_website/requirements.txt

# 2. 启动服务
cd my_website
../.venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000

# 3. 浏览器打开 http://127.0.0.1:8000
```

> 注意：不要直接双击 `static/index.html` 打开（file:// 协议下接口全部失效）。
> 页面对这种情况做了检测，会自动显示启动引导横幅。

> **多进程部署注意**：`--workers N` 会让每个 worker 各有一份进程内结果缓存与内存历史，
> 同一段文本在不同 worker 上可能给出不同结论（上游采样漂移 + 缓存互不共享）。
> 需要跨进程一致时请用单 worker，或设置 `APP_WORKERS=N` 让服务在启动时给出告警，
> 并用 `/api/health` 的 `llm_cache.instance` 观察当前实例。

## 页面功能

- **示例一键填充**：内置辱骂攻击、群体煽动、情绪化谣言、理性论证四类示例
- **分析进度指示**：情感 → 攻击性 → 逻辑 → 报告 四步动画（全链路约 10-30 秒）
- **可取消**：分析中会出现"取消分析"按钮；上游变慢时不必干等（服务端仍会把这一条跑完并计入历史，稍后可回看）
- **结论横幅**：按"水军/谣言/理性/普通"四档结论配色展示
- **三维卡片**：情感（倾向 + 0-10 强度条 + 关键词）、攻击性（对象/等级/类型）、逻辑（论据→结论/关联性/谬误）
- **完整报告**：展示 report_generator 生成的四节报告原文
- **证据本**（agent 模式）：每条来源同时显示 `source_kind`（官方/非官方）与 5 级 `domain_tier`
- **关注度评估**（2026-10-27 新增）：首页下方「📈 关注度（Attention）评估」面板，
  手动填 x/y/z 或输入话题联网估参，展示 E、S、三个加权分项、档位与是否触发深读；
  分析记录里也内嵌一个确定性关注度分
- **分析记录**：服务内存保留最近 50 条，点击可回看（重启清空）

### 关注度（Attention）评估

公式 `E = 100*(1-exp(-λ·S))`，`x`=文章数量、`y`=文章阅读量、`z`=文章留言量，
**分数由后端确定性计算，不调用大模型**。面板提供两种取数方式：

| 页签 | 接口 | 行为 |
|---|---|---|
| 🔢 手动输入 x/y/z | `POST /api/attention` | 纯确定性算分；返回 E、S、term_x/y/z、档位、`deep_read`、阈值反解值 `threshold_s` |
| 🛰️ 话题联网估参 | `POST /api/attention/search` | 走真实检索估出 x/y/z 再算分，回带证据来源 URL 与取数说明 |

当 `E > 66`（严格大于）时，两个接口都会附带 `deep_read` 解读，包含三节：
「为什么人们会关注这件事」「反映了什么社会问题」「反映了什么样的担忧和思想」。
解读由大模型生成；若 LLM 不可用则回退本地模板，并标记 `degraded: true`，
此时内容只是提问框架而非实证分析，页面会明确提示。

读数边界：话题模式下 `x` 是检索命中的独立文章数（下限，非全网总数），`y`/`z` 是**代理值**
——没有任何站点公开文章级精确阅读量，页面会显示「代理 / 估计值」徽章，
`E` 只适合同口径下的相对排序。`E>66` 时提示深读解读尚未接入网页端。

## 接口语义与保护

| 状态码 | 含义 |
| --- | --- |
| 200 | 分析完成（单模块失败也在 200 里体现为降级字段） |
| 400 | 入参问题（空文本 / 纯空白 / 超过 8000 字 / 未知模式） |
| 422 | 请求体不符合模型（例如 `text` 字段缺失，由 Pydantic 返回） |
| 503 | 同时在跑的分析已达上限（默认 4 个），请稍后重试；响应带 `Retry-After` |

`GET /api/health` **不触碰大模型链路**，因此上游挂掉、分析名额占满时它仍能立刻回话，
并顺带报告 `active_analyses`、`analyze_budget_s`（请求级时限预算）与缓存统计。
注意 `status: ok` 只表示**网站进程**活着；上游是否可用看 `llm` 字段
（`usable / provider / model / base_url / error`，2026-10-22 修复 P0-9）。
一次分析有总时长预算 `JUDGE_DEADLINE_S`（默认 120 秒），预算用尽即降级返回，
不会像以前那样挂满几分钟。

classic 的分析记录里带 `degraded_components`（哪些组件没给出可用结果、报告是否走了
本地模板回退）与 `cache_hits` / `cache_instance`；agent 模式的记录除标签/风险分/证据本外，
同样带三维分析的 `emotion` / `attack` / `logic`（2026-10-22 修复 F8）。

## API 一览（交互式文档见 /docs）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/analyze` | `{"text": "...", "mode": "classic\|agent"}` → 三维 JSON + conclusion + report（agent 模式另有标签/风险分/证据本；两种模式都带 `attention` 关注度字段） |
| POST | `/api/attention` | `{"x": 30, "y": 48316000, "z": 149}` → E、S、三个分项、档位、是否触发深读（纯确定性，不调大模型） |
| POST | `/api/attention/search` | `{"query": "老干妈回应AI裁员传闻"}` → 联网估出 x/y/z 后算分，回带来源与取数说明 |
| GET | `/api/history` | 最近分析记录（最新在前） |
| GET | `/api/health` | 健康检查（不碰大模型链路，含并发数与时长预算） |
| GET | `/docs` | FastAPI 自动生成的交互式接口文档 |

## 技术说明

- 后端 FastAPI 只做"薄封装"：分析逻辑全部复用根目录 `core_judge.py`，
  网站侧不重复实现任何分析规则（高内聚、低耦合）。
- 前端原生 HTML/CSS/JS，无需 Node/npm，双击无法运行（有跨域与接口依赖），
  请通过上面的 uvicorn 命令访问。
- 文本上限 8000 字；分析历史保存在服务内存中，重启即清空。
