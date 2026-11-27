# -*- coding: utf-8 -*-
"""从实验结果生成 LaTeX 宏与表格，保证论文数字与原始 JSON 一致。"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "docs" / "paper" / "data"
GEN = ROOT / "docs" / "paper" / "generated"
GEN.mkdir(parents=True, exist_ok=True)

GROUP_LABEL = {
    "high_risk_agitation": "高风险煽动",
    "emotional_rumor": "情绪化谣言",
    "rational_discussion": "理性探讨",
    "emotional_vent": "情绪化宣泄",
}

EXPECTED = {
    "high_risk_agitation": "高度疑似水军/煽动性言论",
    "emotional_rumor": "疑似情绪化谣言",
    "rational_discussion": "理性探讨",
    "emotional_vent": "情绪化宣泄/普通网络争论",
}

SHORT_CONCLUSION = {
    "高度疑似水军/煽动性言论": "高度疑似",
    "疑似情绪化谣言": "疑似谣言",
    "理性探讨": "理性探讨",
    "情绪化宣泄/普通网络争论": "情绪化宣泄",
}

SHORT_COMPONENT = {
    "emotion": "情绪",
    "attack": "攻击",
    "logic": "逻辑",
    "report": "报告",
}


def num(value, digits=0):
    if value is None:
        return "---"
    if digits == 0:
        return f"{int(round(value)):,}".replace(",", "{,}")
    return f"{value:.{digits}f}"


def main():
    det = json.loads((DATA / "deterministic.json").read_text(encoding="utf-8"))
    api = json.loads((DATA / "api_summary.json").read_text(encoding="utf-8"))
    tests = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))

    attn = det["attention"]
    fp = det["fingerprint"]
    tier = det["tiering"]
    verdict = det["verdict"]
    guard = det["guard"]
    res = det["resilience"]
    empty = det["empty_input"]

    cold = api["cold_latency_s"]
    warm = api["warm_latency_s"]
    slowdown = (cold["mean"] / warm["mean"]) if cold["mean"] and warm["mean"] else None

    warm_values = [r["warm"]["latency_s"] for r in api["records"] if r["warm"].get("latency_s") is not None]
    fast_warm = sum(1 for v in warm_values if v <= 0.05)
    comp_fail = sum(len(r["cold"].get("degraded_components") or []) for r in api["records"])
    comp_total = len(api["records"]) * 4
    agree = sum(1 for r in api["records"] if r["cold"].get("conclusion") == EXPECTED.get(r["group"]))
    clean = [r for r in api["records"] if not (r["cold"].get("degraded_components") or [])]
    agree_clean = sum(1 for r in clean if r["cold"].get("conclusion") == EXPECTED.get(r["group"]))

    macros = {
        "PaperDate": "2026年11月25日",
        "Seed": str(det["meta"]["seed"]),
        "CodeFiles": num(tests["code_files"]),
        "CodeLOC": num(tests["code_loc"]),
        "JsonFiles": num(tests["json_files"]),
        "TestFiles": num(tests["test_files"]),
        "TestLOC": num(tests["test_loc"]),
        "TestCases": num(tests["cases"]),
        "TestTime": f"{tests['elapsed_s']:.1f}",
        "TestSkip": num(tests["skipped"]),
        "TestFail": num(tests["failed"]),
        "TestFailRate": f"{tests['failed'] / tests['cases'] * 100:.2f}\\%",
        "AttnN": num(attn["n"]),
        "AttnThroughput": num(attn["throughput_per_s"]),
        "AttnPerCall": f"{1e6 / attn['throughput_per_s']:.1f}",
        "AttnMonoChecks": num(attn["monotonic_checks"]),
        "AttnMonoViol": num(attn["monotonic_violations"]),
        "FpPairs": num(fp["pairs"]),
        "FpThroughput": num(fp["pairs_per_s"]),
        "FpPrecision": f"{fp['precision']:.3f}",
        "FpRecall": f"{fp['recall']:.3f}",
        "FpNearMean": f"{fp['near_mean']:.3f}",
        "FpFarMean": f"{fp['far_mean']:.3f}",
        "TierCombos": num(tier["combos"]),
        "TierMismatch": num(tier["mismatches"]),
        "VerdictN": num(verdict["n"]),
        "VerdictThroughput": num(verdict["throughput_per_s"]),
        "VerdictViol": num(verdict["violations"]),
        "CapChecked": num(verdict["no_claim_cap_checked"]),
        "CapOk": num(verdict["no_claim_cap_ok"]),
        "AnchorChecked": num(verdict["evidence_anchor_checked"]),
        "AnchorOk": num(verdict["evidence_anchor_ok"]),
        "RulesLoadMs": f"{verdict['rules_load_ms_per_call']:.2f}",
        "GuardCases": num(guard["cases"]),
        "GuardMatched": num(guard["matched"]),
        "SfThreads": num(res["single_flight_threads"]),
        "SfCalls": num(res["single_flight_upstream_calls"]),
        "SfWall": f"{res['single_flight_wall_s']:.2f}",
        "FailThreads": num(res["failure_threads"]),
        "FailCalls": num(res["failure_upstream_calls"]),
        "CacheOffThreads": num(res["cache_off_threads"]),
        "CacheOffCalls": num(res["cache_off_upstream_calls"]),
        "CacheOffSeq": num(res["cache_off_sequential_calls"]),
        "TtlCalls": num(res["ttl_expired_calls"]),
        "EmptyLatency": f"{empty['latency_s']:.2f}",
        "EmptyCalls": num(empty["usage"]["requests"]),
        "ApiN": num(api["n_items"]),
        "ApiColdMean": f"{cold['mean']:.1f}",
        "ApiColdMedian": f"{cold['p50']:.1f}",
        "ApiColdMax": f"{cold['max']:.1f}",
        "ApiWarmMean": f"{warm['mean']:.1f}",
        "ApiWarmMax": f"{warm['max']:.1f}",
        "ApiWarmMedian": f"{statistics.median(warm_values):.2f}" if warm_values else "---",
        "ApiFastWarm": f"{fast_warm}/{len(warm_values)}",
        "ApiIdentical": api["warm_cache_hit_identical"],
        "ApiDegraded": api["degraded_items"],
        "ApiCompFail": f"{comp_fail}/{comp_total}",
        "ApiTokens": num(api["total_tokens"]),
        "ApiTokPerItem": f"{api['tokens_per_item']:.0f}",
        "ApiDrift": api["no_cache_drift"],
        "ApiConcRequests": num(api["concurrency"]["upstream"]["requests"]),
        "ApiConcWall": f"{api['concurrency']['wall_s']:.1f}",
        "ApiConcConclusion": api["concurrency"]["conclusions"][0],
        "ApiSlowdown": f"{slowdown:.1f}" if slowdown else "---",
        "ApiAgree": f"{agree}/{len(api['records'])}",
        "ApiAgreeClean": f"{agree_clean}/{len(clean)}",
    }

    lines = ["% 由 docs/paper/experiments/make_tables.py 自动生成，请勿手改。"]
    for key, value in macros.items():
        lines.append(f"\\newcommand{{\\{key}}}{{{value}}}")
    (GEN / "numbers.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = []
    for rec in api["records"]:
        cold_r = rec["cold"]
        warm_r = rec["warm"]
        degraded = "、".join(SHORT_COMPONENT.get(x, x) for x in (cold_r.get("degraded_components") or [])) or "无"
        conclusion = SHORT_CONCLUSION.get(cold_r.get("conclusion"), cold_r.get("conclusion") or "失败")
        cold_s = f"{cold_r['latency_s']:.1f}" if cold_r.get("latency_s") is not None else "---"
        warm_s = f"{warm_r['latency_s']:.2f}" if warm_r.get("latency_s") is not None else "---"
        tokens = (cold_r.get("usage") or {}).get("tokens", 0)
        rows.append(f"{rec['id']} & {GROUP_LABEL.get(rec['group'], rec['group'])} & {cold_s} & "
                    f"{warm_s} & {num(tokens)} & {degraded} & {conclusion} \\\\")

    table = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\footnotesize",
        r"\caption{端到端真实 API 结果（12 条，冷启动与缓存命中）}",
        r"\label{tab:api}",
        r"\begin{tabular}{@{}llrrrrl@{}}",
        r"\toprule",
        r"编号 & 组别 & 冷启动(s) & 命中(s) & 令牌 & 降级组件 & 结论\\",
        r"\midrule",
        *rows,
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
    ]
    (GEN / "table_api.tex").write_text("\n".join(table) + "\n", encoding="utf-8")

    print(f"wrote {GEN / 'numbers.tex'} and {GEN / 'table_api.tex'}")
    print(json.dumps({k: macros[k] for k in ("TestCases", "ApiColdMean", "ApiWarmMedian", "ApiDrift",
                                             "ApiConcRequests", "ApiAgree", "ApiAgreeClean",
                                             "ApiCompFail", "ApiFastWarm")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
