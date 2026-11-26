# docs/paper —— 中文 LaTeX 论文与可复现实验

论文：**《判定与生成分离：面向网络言论分析与谣言识别的确定性判定系统设计与实证评估》**

## 文件地图

| 路径 | 作用 |
| --- | --- |
| `main.tex` | 论文正文（XeLaTeX + ctex，15 页；中英文摘要、7 节正文、声明与参考文献） |
| `main.pdf` | 由 `main.tex` 编译的成品 |
| `generated/numbers.tex` | 正文所有数字宏，**由脚本从原始 JSON 生成，禁止手改** |
| `generated/table_api.tex` | 端到端结果表，同样自动生成 |
| `experiments/dataset.json` | 40 条中文实验文本（四组各 10 条） |
| `experiments/bench_deterministic.py` | 确定性层基准（关注度/指纹/分层/风险分/护栏/韧性/空输入） |
| `experiments/bench_api.py` | 真实 API 端到端实验（消耗配额，产出 `data/api_summary.json`） |
| `experiments/make_tables.py` | 从 `data/*.json` 生成 `generated/*.tex` |
| `experiments/make_docx.py` | 把 `main.tex` 转成 DOCX（可选产物） |
| `experiments/_check_claims.py` | 论文硬编码数字 ↔ 原始数据的对账脚本 |
| `experiments/_verify_pdf.py` | PDF 文本层核验（乱码/缺字/引用与标签完整性） |
| `data/` | 原始实验结果（JSON），论文数字的唯一来源 |
| `figures/arch-1.png` | 架构图位图（`make_docx.py` 使用；PDF 里是 TikZ 矢量图） |

## 编译

```powershell
# 两次编译以正确生成交叉引用
xelatex -interaction=nonstopmode main.tex
xelatex -interaction=nonstopmode main.tex
```

需要 MiKTeX/TeX Live 的 `xelatex` 与 `ctex`（`fontset=windows` 用系统宋体/黑体）。
编译中间产物（`*.aux`、`*.log`、`*.out`、页面渲染 PNG）不入库。

## 复现实验

```powershell
# 1) 确定性层（不消耗 API 配额，约 1 分钟）
.venv\Scripts\python.exe -X utf8 docs\paper\experiments\bench_deterministic.py

# 2) 端到端真实 API（消耗配额；需 .env 中配好密钥，12 条约 15 分钟）
.venv\Scripts\python.exe -X utf8 docs\paper\experiments\bench_api.py --limit 12

# 3) 从原始数据重算全部数字宏与表格（幂等：无新实验结果时输出逐字节一致）
.venv\Scripts\python.exe -X utf8 docs\paper\experiments\make_tables.py

# 4) 对账与核验
.venv\Scripts\python.exe -X utf8 docs\paper\experiments\_check_claims.py
pdftotext -enc UTF-8 main.pdf main.txt
.venv\Scripts\python.exe -X utf8 docs\paper\experiments\_verify_pdf.py
```

## 参考文献核验

15 条参考文献中有 DOI 的条目均在 Crossref 逐条核对过元数据（作者、年份、刊名、卷期、页码）：

| 引用 | DOI | 核验结果 |
| --- | --- | --- |
| Vosoughi et al., Science 2018 | `10.1126/science.aap9559` | 359(6380):1146–1151，一致 |
| Ji et al., ACM CSUR 2023 | `10.1145/3571730` | 55(12):1–38，一致 |
| Shu et al., SIGKDD Explor. 2017 | `10.1145/3137597.3137600` | 19(1):22–36，一致 |
| Breck et al., IEEE BigData 2017 | `10.1109/BigData.2017.8258038` | 1123–1132，一致 |

无 DOI 的条目为会议论文、arXiv 预印本与政府规章，按可公开访问的出处著录。

## 数据口径与边界

- 端到端数据采集于 **2026-11-25**，模型 `deepseek-v4.1-flash`，温度为 0、推理档位关闭；
  网关为共享服务，实验期间出现拥塞，延迟方差大，论文如实保留未剔除。
- 12 条样本的组别标注由作者单人按 rubric 完成，**无双人独立标注**，因此论文只报告
  描述性一致性，不报告准确率推断统计。
- 关注度评分里的 `y`（阅读量）是平台热度指数或转引口径，`E` 只适合同口径相对排序。
- `reachable` 档位在两批真实话题上均未让任何话题越过 66（0/5），论文按实测数据报告，
  不宣称换档位可以制造高关注。

## DOCX 说明

DOCX 是可选导出，**不入库**（`docs/paper/*.docx` 已在 `.gitignore` 中），由脚本按需生成：

```powershell
python docs\paper\experiments\make_docx.py
```

`make_docx.py` 默认写 `MyLocalAI-论文.docx`。若该文件正被 Word/WPS 打开，脚本会改写到
`MyLocalAI-论文.updated.docx` 并打印告警（不会留下半成品）。关闭阅读器后覆盖重命名即可：

```powershell
Move-Item 'MyLocalAI-论文.updated.docx' 'MyLocalAI-论文.docx' -Force
```
