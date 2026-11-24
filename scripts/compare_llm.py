# -*- coding: utf-8 -*-
"""对比实验脚本：本项目（证据核查流水线） vs 通用大模型原生对话（如 DeepSeek）。

用法（在本项目根目录，服务已启动时）：
    set DEEPSEEK_API_KEY=sk-xxxx
    .venv\\Scripts\\python.exe scripts\\compare_llm.py --claims-file .tmp\\claims.txt

- 本项目侧：POST http://127.0.0.1:8000/api/analyze（mode=agent，联网取证 + 确定性打分）。
- 大模型侧：OpenAI 兼容 Chat Completions（默认 DeepSeek base_url，可用 --base-url/--model
  换成任何兼容提供方）。每组输入跑两遍，用于测同输入稳定性。
- 指标：延迟、两次输出一致性（编辑距离近似）、JSON 结构合法率（结构化提示时）、
  证据引用数（是否给了可核验 URL）、免责/不确定表述、token 用量与估算成本。
- 输出：.tmp/compare-llm/report.json + 控制台摘要。本脚本只读本地服务与外部模型 API，
  不写仓库数据。

设计口径（对应 docs/评估-对比通用大模型-2026-11-22.md §附录）：
同一组说法、同一台机器、同一时段；对比的是"直接问大模型"与"走本项目流水线"的
产物质量与运行特征，不评判模型本身智力。
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 控制台兜底

DEFAULT_CLAIMS = [
    "网传喝隔夜茶会致癌，转给家人看看！",
    "听说某地自来水被下了毒，官方根本不敢说！",
    "研究表明每天喝一杯红酒能延长寿命十年。",
    "今晚八点某平台系统升级，大家赶紧把余额提出来！",
    "某知名品牌奶粉被检出有毒物质，家里有孩子的注意了。",
]

PROJECT_BASE = "http://127.0.0.1:8000"
DEEPSEEK_BASE = "https://api.deepseek.com/v1"

PLAIN_PROMPT = (
    "请判断下面这条网络说法是不是谣言，直接给出你的判断和理由：\n\n{claim}"
)
STRUCT_PROMPT = (
    "你是谣言核查助手。对下面的说法输出严格 JSON（不要多余文字），字段：\n"
    '{"verdict": "高风险谣言|疑似谣言|存疑待核实|低风险|无可核实陈述",\n'
    ' "risk_score": 0-100 整数, "reasons": ["…"], "evidence": [{"title":"…","url":"…"}],\n'
    ' "uncertainty": ["…"]}\n'
    "要求：只依据你确定的信息；没有把握的证据不要编造 URL。\n\n说法：{claim}"
)


def load_claims(path: str | None) -> list[str]:
    if not path:
        return list(DEFAULT_CLAIMS)
    text = Path(path).read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip()]


def stability(a: str, b: str) -> float:
    """两次输出的相似度（SequenceMatcher 比率，1=逐字一致）。"""
    return round(difflib.SequenceMatcher(None, a or "", b or "").ratio(), 3)


def evidence_count(text: str) -> int:
    """输出里出现的可核验 URL 数（本项目口径：只数 http/https 链接）。"""
    return len(re.findall(r"https?://[^\s\"'）)\]，。]+", text or ""))


def run_project(claim: str, timeout: int) -> dict:
    t0 = time.time()
    r = requests.post(f"{PROJECT_BASE}/api/analyze",
                      json={"text": claim, "mode": "agent"}, timeout=timeout)
    dt = round(time.time() - t0, 2)
    d = r.json()
    ev = d.get("evidence") or []
    return {
        "side": "project",
        "latency_s": dt,
        "verdict": d.get("conclusion"),
        "risk_score": d.get("risk_score"),
        "confidence": d.get("confidence"),
        "degraded": bool(d.get("degraded_components") or (d.get("degraded") if isinstance(d.get("degraded"), bool) else False)),
        "evidence_count": len(ev) or evidence_count(json.dumps(d, ensure_ascii=False)),
        "raw_len": len(json.dumps(d, ensure_ascii=False)),
        "_raw": d,
    }


def run_llm(claim: str, base: str, key: str, model: str, timeout: int) -> dict:
    prompt = STRUCT_PROMPT.format(claim=claim)
    out = {}
    latencies = []
    texts = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    for i in range(2):  # 两遍：测同输入稳定性
        t0 = time.time()
        r = requests.post(f"{base}/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": model, "temperature": 0,
                                "messages": [{"role": "user", "content": prompt}]},
                          timeout=timeout)
        latencies.append(round(time.time() - t0, 2))
        d = r.json()
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}: {str(d)[:200]}"
            break
        msg = d["choices"][0]["message"]["content"]
        texts.append(msg)
        u = d.get("usage") or {}
        usage["prompt_tokens"] += u.get("prompt_tokens", 0)
        usage["completion_tokens"] += u.get("completion_tokens", 0)
    if not texts:
        return {"side": "llm", "latency_s": latencies, "error": out.get("error")}
    first = texts[0]
    try:
        parsed = json.loads(re.search(r"\{.*\}", first, re.S).group(0))
        json_ok = isinstance(parsed, dict) and "verdict" in parsed
        verdict = parsed.get("verdict")
        score = parsed.get("risk_score")
    except Exception:
        json_ok, verdict, score = False, None, None
    return {
        "side": "llm",
        "latency_s": latencies,
        "verdict": verdict,
        "risk_score": score,
        "json_valid": json_ok,
        "stability": stability(texts[0], texts[1]) if len(texts) > 1 else None,
        "evidence_count": evidence_count(first),
        "hedges": sum(first.count(w) for w in ("可能", "不确定", "待核实", "没有把握", "无法确认")),
        "raw_len": len(first),
        "usage": usage,
        "_text": first,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claims-file", default=None, help="每行一条说法的 UTF-8 文本文件")
    ap.add_argument("--base-url", default=DEEPSEEK_BASE, help="OpenAI 兼容 API base")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""),
                    help="缺省读 DEEPSEEK_API_KEY 环境变量")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    claims = load_claims(args.claims_file)
    print(f"说法 {len(claims)} 条 | LLM 侧: {args.base_url} {args.model}")
    rows = []
    for c in claims:
        print(f"\n== {c[:40]}…")
        try:
            p = run_project(c, args.timeout)
        except Exception as e:
            p = {"side": "project", "error": f"{type(e).__name__}: {e}"}
        print("  项目 :", {k: p.get(k) for k in ("latency_s", "verdict", "risk_score",
                                                "confidence", "evidence_count", "degraded", "error")})
        if not args.api_key:
            print("  LLM  : 跳过（未提供 --api-key / DEEPSEEK_API_KEY）")
            l = {"side": "llm", "error": "no api key"}
        else:
            l = run_llm(c, args.base_url, args.api_key, args.model, args.timeout)
            print("  LLM  :", {k: l.get(k) for k in ("latency_s", "verdict", "risk_score",
                                                     "json_valid", "stability", "evidence_count", "hedges", "error")})
        rows.append({"claim": c, "project": p, "llm": l})

    out_dir = Path(".tmp/compare-llm")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "report.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写出 {out} —— 汇总见 docs/评估-对比通用大模型-2026-11-22.md 的指标口径")


if __name__ == "__main__":
    main()
