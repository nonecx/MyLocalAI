# -*- coding: utf-8 -*-
"""真实 API 端到端实验：classic 分析链路（情绪/攻击性/逻辑三模块 + 报告）。

用法（在仓库根目录、已安装本包或设置 PYTHONPATH=src）：
  python docs/paper/experiments/bench_api.py --limit 12 --model gpt-6.1-sol

环境变量（由脚本设置默认值，可覆盖）：
  LLM_PROVIDER=new  MYLOCALAI_NEW_MODEL=<model>  JUDGE_DEADLINE_S=240  LLM_TIMEOUT_S=60
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "docs" / "paper" / "data"
DATA.mkdir(parents=True, exist_ok=True)

GROUPS = ("high_risk_agitation", "emotional_rumor", "rational_discussion", "emotional_vent")


def load_items(limit: int):
    payload = json.loads((ROOT / "docs" / "paper" / "experiments" / "dataset.json").read_text(encoding="utf-8"))
    items = payload["items"]
    if limit <= 0:
        return items
    per_group = max(1, limit // len(GROUPS))
    picked = []
    for group in GROUPS:
        picked.extend([it for it in items if it["group"] == group][:per_group])
    return picked[:limit]


def snapshot():
    from mylocalai.llm import llm_client as L
    return L.usage_snapshot()


def delta(before, after):
    return {"requests": after["requests"] - before["requests"],
            "tokens": after["tokens"] - before["tokens"]}


def run_judge(text, deadline):
    from mylocalai.classic import judge as J
    before = snapshot()
    t0 = time.perf_counter()
    result = J.judge(text, deadline_s=deadline)
    elapsed = time.perf_counter() - t0
    after = snapshot()
    return {
        "latency_s": round(elapsed, 2),
        "usage": delta(before, after),
        "conclusion": result.get("conclusion"),
        "degraded_components": result.get("degraded_components") or [],
        "emotion_score": (result.get("emotion") or {}).get("emotion_score"),
        "emotion_error": (result.get("emotion") or {}).get("error"),
        "aggression": (result.get("attack") or {}).get("aggression_level"),
        "attack_error": (result.get("attack") or {}).get("error"),
        "logic_relevance": (result.get("logic") or {}).get("logic_relevance"),
        "logic_error": (result.get("logic") or {}).get("error"),
        "report_len": len(result.get("report") or ""),
    }


def prepare_models_config(model: str) -> Path:
    """把选定模型的 reasoning 关掉，写一份实验专用 models.json（不改仓库 data/）。"""
    src = ROOT / "data" / "models.json"
    payload = json.loads(src.read_text(encoding="utf-8"))
    entry = payload["providers"]["new"]["models"]
    found = False
    for item in entry:
        if item["id"] == model:
            item["reasoning"] = {"enabled": False, "param": "reasoning_effort", "active_tier": "off",
                                 "tiers": [{"tier": "off", "api_value": None}]}
            found = True
    if not found:
        entry.append({"id": model, "display_name": model, "context_window": None,
                      "max_output_tokens": None, "input_types": ["text"], "enabled": True,
                      "reasoning": {"enabled": False, "param": "reasoning_effort", "active_tier": "off",
                                    "tiers": [{"tier": "off", "api_value": None}]}})
    payload["active"] = {"provider": "new", "model": model}
    path = DATA / "models_experiment.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--model", default=os.environ.get("MYLOCALAI_NEW_MODEL", "deepseek-v4.1-flash"))
    parser.add_argument("--deadline", type=float, default=240.0)
    parser.add_argument("--no-cache-repeat", type=int, default=6)
    parser.add_argument("--out", default=str(DATA / "api_runs.jsonl"))
    parser.add_argument("--skip-baseline", action="store_true",
                        help="复用已有 api_summary.json 的 baseline 段，只跑并发探测")
    args = parser.parse_args()

    os.environ.setdefault("LLM_PROVIDER", "new")
    os.environ["MYLOCALAI_NEW_MODEL"] = args.model
    os.environ.setdefault("JUDGE_DEADLINE_S", str(int(args.deadline)))
    os.environ.setdefault("LLM_TIMEOUT_S", "60")
    os.environ.setdefault("LLM_MAX_RETRIES", "1")
    os.environ["MYLOCALAI_MODELS_PATH"] = str(prepare_models_config(args.model))

    from mylocalai.llm import llm_client as L

    L.reset_usage()
    L.reset_cache()

    items = load_items(args.limit)
    out_path = Path(args.out)
    records = []
    no_cache = []
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    meta = {"started": started, "model": L.get_model(), "provider": L.get_provider(),
            "base_url": L.get_base_url(), "limit": len(items), "deadline_s": args.deadline}

    if args.skip_baseline:
        previous = json.loads((DATA / "api_summary.json").read_text(encoding="utf-8"))
        records = previous.get("records") or []
        no_cache = previous.get("no_cache") or []
        meta = previous.get("meta") or meta
        print(f"[baseline] reused {len(records)} records", flush=True)
    else:
        for idx, item in enumerate(items, 1):
            print(f"[{idx}/{len(items)}] {item['id']} cold", flush=True)
            try:
                cold = run_judge(item["text"], args.deadline)
            except Exception as exc:  # noqa: BLE001
                cold = {"latency_s": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
            print(f"  -> {cold.get('conclusion')} {cold.get('latency_s')}s {cold.get('usage')}", flush=True)

            print(f"[{idx}/{len(items)}] {item['id']} warm", flush=True)
            try:
                warm = run_judge(item["text"], args.deadline)
            except Exception as exc:  # noqa: BLE001
                warm = {"latency_s": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
            print(f"  -> {warm.get('conclusion')} {warm.get('latency_s')}s {warm.get('usage')}", flush=True)

            records.append({"id": item["id"], "group": item["group"], "text": item["text"],
                            "cold": cold, "warm": warm})
            out_path.write_text(json.dumps({"meta": meta, "records": records}, ensure_ascii=False, indent=2),
                                encoding="utf-8")

        for item in items[:args.no_cache_repeat]:
            print(f"[nocache] {item['id']}", flush=True)
            os.environ["LLM_CACHE_MAX"] = "0"
            L.reset_cache()
            try:
                fresh = run_judge(item["text"], args.deadline)
            except Exception as exc:  # noqa: BLE001
                fresh = {"latency_s": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
            os.environ.pop("LLM_CACHE_MAX", None)
            L.reset_cache()
            no_cache.append({"id": item["id"], "fresh": fresh})
            print(f"  -> {fresh.get('conclusion')} {fresh.get('latency_s')}s {fresh.get('usage')}", flush=True)

    print("[concurrency] 4 threads same text, cold cache", flush=True)
    from mylocalai.llm import llm_cache
    llm_cache.clear()
    L.reset_usage()
    before = snapshot()
    text = items[0]["text"]

    def one():
        return run_judge(text, args.deadline)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: one(), range(4)))
    wall = time.perf_counter() - t0
    after = snapshot()
    concurrency = {"threads": 4, "wall_s": round(wall, 2), "upstream": delta(before, after),
                   "conclusions": [r.get("conclusion") for r in results],
                   "latencies": [r.get("latency_s") for r in results]}

    print("[empty] judge('')", flush=True)
    L.reset_cache()
    before = snapshot()
    t0 = time.perf_counter()
    from mylocalai.classic import judge as J
    empty_res = J.judge("")
    empty = {"latency_s": round(time.perf_counter() - t0, 4), "usage": delta(before, snapshot()),
             "conclusion": empty_res.get("conclusion")}

    cold_lat = [r["cold"]["latency_s"] for r in records if r["cold"].get("latency_s")]
    warm_lat = [r["warm"]["latency_s"] for r in records if r["warm"].get("latency_s")]
    same = sum(1 for r in records if r["cold"].get("conclusion") == r["warm"].get("conclusion"))
    degraded = sum(1 for r in records if r["cold"].get("degraded_components"))
    tokens = sum((r["cold"].get("usage") or {}).get("tokens", 0) for r in records)
    requests = sum((r["cold"].get("usage") or {}).get("requests", 0) for r in records)
    drift = sum(1 for r in no_cache
                if r["fresh"].get("conclusion") != next(
                    (x["cold"].get("conclusion") for x in records if x["id"] == r["id"]), None))

    summary = {
        "meta": meta,
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "n_items": len(records),
        "cold_latency_s": {"mean": round(statistics.fmean(cold_lat), 2) if cold_lat else None,
                           "p50": round(statistics.median(cold_lat), 2) if cold_lat else None,
                           "max": round(max(cold_lat), 2) if cold_lat else None},
        "warm_latency_s": {"mean": round(statistics.fmean(warm_lat), 3) if warm_lat else None,
                           "max": round(max(warm_lat), 3) if warm_lat else None},
        "warm_cache_hit_identical": f"{same}/{len(records)}",
        "degraded_items": f"{degraded}/{len(records)}",
        "total_upstream_requests": requests,
        "total_tokens": tokens,
        "tokens_per_item": round(tokens / len(records), 1) if records else None,
        "no_cache_drift": f"{drift}/{len(no_cache)}",
        "no_cache": no_cache,
        "concurrency": concurrency,
        "empty_input": empty,
        "records": records,
    }
    summary_path = DATA / "api_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "records"}, ensure_ascii=False, indent=2))
    print(f"wrote {summary_path}")


if __name__ == "__main__":
    main()
