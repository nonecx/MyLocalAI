# -*- coding: utf-8 -*-
"""确定性层基准：关注度公式、话术指纹、四档分层、谣言判定、深读护栏、缓存/单飞/预算。

不调用外部 API，结果可复现（固定随机种子）。输出 docs/paper/data/deterministic.json。
"""

from __future__ import annotations

import json
import math
import os
import platform
import random
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs" / "paper" / "data"
OUT.mkdir(parents=True, exist_ok=True)

SEED = 20261125
random.seed(SEED)

results = {"meta": {}, "attention": {}, "fingerprint": {}, "tiering": {}, "verdict": {},
           "guard": {}, "resilience": {}}


def timed(fn, *args, **kwargs):
    t0 = time.perf_counter()
    value = fn(*args, **kwargs)
    return value, time.perf_counter() - t0


def pct(values, q):
    values = sorted(values)
    if not values:
        return 0.0
    idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
    return values[idx]


def bench_attention():
    from mylocalai.attention import attention as A

    n = 20000
    triples = [(10 ** random.uniform(0, 12), 10 ** random.uniform(0, 12), 10 ** random.uniform(0, 12))
               for _ in range(n)]
    t0 = time.perf_counter()
    scores = [A.attention_score(x, y, z) for x, y, z in triples]
    elapsed = time.perf_counter() - t0
    bounds_ok = all(0.0 < s < 100.0 for s in scores)

    monotonic_violations = 0
    for _ in range(5000):
        x = 10 ** random.uniform(0, 10)
        y = 10 ** random.uniform(0, 10)
        z = 10 ** random.uniform(0, 10)
        base = A.attention_score(x, y, z)
        for dx, dy, dz in ((x * 1.5, y, z), (x, y * 1.5, z), (x, y, z * 1.5)):
            if not A.attention_score(dx, dy, dz) > base:
                monotonic_violations += 1

    anchors = {
        "E(100,1e5,1e3)": A.attention_score(100, 100000, 1000),
        "E(0,0,0)": A.attention_score(0, 0, 0),
        "E(1e12,1e12,1e12)": A.attention_score(1e12, 1e12, 1e12),
        "E(1e300,1e300,1e300)": A.attention_score(1e300, 1e300, 1e300),
    }
    s66 = -math.log(0.34) / 0.7

    topics = [
        {"name": "老干妈回应AI裁员传闻", "x": 30, "y": 48316000, "z": 149},
        {"name": "台湾艺人阿雅公开表态", "x": 5, "y": 12506705, "z": 302},
        {"name": "长鑫存储拒绝苹果", "x": 11, "y": 13822047, "z": 42},
        {"name": "承包户花生地遭捡拾", "x": 3, "y": 2067344, "z": 129},
        {"name": "男子出狱案二审", "x": 6, "y": 3766947, "z": 1},
    ]
    spec_params, spec_warn = A.load_profile("spec")
    reach_params, reach_warn = A.load_profile("reachable")
    profile_rows = []
    for t in topics:
        spec = A.attention_score(t["x"], t["y"], t["z"], spec_params)
        reach = A.attention_score(t["x"], t["y"], t["z"], reach_params)
        profile_rows.append({"topic": t["name"], "spec": round(spec, 2), "reachable": round(reach, 2)})

    results["attention"] = {
        "n": n,
        "elapsed_s": round(elapsed, 3),
        "throughput_per_s": round(n / elapsed, 1),
        "bounds_ok": bounds_ok,
        "min": min(scores),
        "max": max(scores),
        "mean": round(statistics.fmean(scores), 3),
        "monotonic_checks": 15000,
        "monotonic_violations": monotonic_violations,
        "anchors": {k: v for k, v in anchors.items()},
        "s66": round(s66, 6),
        "profile_rows": profile_rows,
        "profile_warnings": [list(spec_warn), list(reach_warn)],
    }


def _corpus(n=600):
    heads = ["网传", "据说", "刚刚", "紧急", "有人爆料", "官方通报", "记者调查", "专家表示",
             "网友反映", "最新消息", "视频显示", "文件称"]
    subs = ["某地自来水", "某品牌奶粉", "某医院", "某小区", "某学校", "某工厂", "某景区",
            "某食品厂", "某网红", "某公司"]
    verbs = ["被曝", "被查", "发生", "出现", "检出", "宣布", "否认", "回应", "辟谣", "删除"]
    objs = ["重金属超标", "集体感染", "食物中毒", "数据造假", "违规排放", "价格暴涨",
            "人员伤亡", "信息泄露", "质量问题", "安全事故"]
    tails = ["，引发关注。", "，正在核实。", "，官方尚未回应。", "，请以通报为准。", "，切勿传播。"]
    out = []
    for _ in range(n):
        out.append(random.choice(heads) + random.choice(subs) + random.choice(verbs)
                   + random.choice(objs) + random.choice(tails))
    return out


def bench_fingerprint():
    from mylocalai.assistant import fingerprint as F

    base = _corpus(500)
    near, far = [], []
    for text in base:
        chars = list(text)
        for _ in range(max(1, len(chars) // 12)):
            i = random.randrange(len(chars))
            chars[i] = random.choice("的一是在不了有和人这中大为上个国我以要他")
        near.append((text, "".join(chars)))
        far.append((text, random.choice(base)))
    t0 = time.perf_counter()
    near_scores = [F.jaccard(a, b) for a, b in near]
    far_scores = [F.jaccard(a, b) for a, b in far]
    elapsed = time.perf_counter() - t0

    threshold = 0.6
    tp = sum(1 for s in near_scores if s >= threshold)
    fp = sum(1 for s in far_scores if s >= threshold)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / len(near_scores)

    t0 = time.perf_counter()
    for _ in range(2000):
        F.normalize_claim(random.choice(base))
    norm_elapsed = time.perf_counter() - t0

    results["fingerprint"] = {
        "pairs": len(near_scores) * 2,
        "elapsed_s": round(elapsed, 3),
        "pairs_per_s": round(len(near_scores) * 2 / elapsed, 1),
        "near_mean": round(statistics.fmean(near_scores), 4),
        "far_mean": round(statistics.fmean(far_scores), 4),
        "near_p10": round(pct(near_scores, 0.10), 4),
        "far_p90": round(pct(far_scores, 0.90), 4),
        "threshold": threshold,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "normalize_per_s": round(2000 / norm_elapsed, 1),
    }


def bench_tiering():
    from mylocalai.core import tiering as T

    combos = []
    for score in [i / 2 for i in range(21)]:
        for aggression in ("低", "中", "高"):
            for relevance in ("强", "中", "弱"):
                combos.append((score, aggression, relevance))

    def expected(score, aggression, relevance):
        if score >= 7 and aggression == "高" and relevance == "弱":
            return T.TIER_HIGH
        if score >= 5 and relevance == "弱":
            return T.TIER_SUSPECT
        if relevance == "强" and aggression == "低":
            return T.TIER_RATIONAL
        return T.TIER_OTHER

    mismatches = 0
    dist = {}
    t0 = time.perf_counter()
    for score, aggression, relevance in combos:
        tier = T.speech_tier({"emotion_score": score}, {"aggression_level": aggression},
                             {"logic_relevance": relevance})
        dist[tier] = dist.get(tier, 0) + 1
        if tier != expected(score, aggression, relevance):
            mismatches += 1
    elapsed = time.perf_counter() - t0

    boundary = {
        "6.99/弱": T.speech_tier({"emotion_score": 6.99}, {"aggression_level": "高"}, {"logic_relevance": "弱"}),
        "7.00/弱/高": T.speech_tier({"emotion_score": 7.0}, {"aggression_level": "高"}, {"logic_relevance": "弱"}),
        "7.00/弱/中": T.speech_tier({"emotion_score": 7.0}, {"aggression_level": "中"}, {"logic_relevance": "弱"}),
        "5.00/弱": T.speech_tier({"emotion_score": 5.0}, {"aggression_level": "低"}, {"logic_relevance": "弱"}),
        "4.99/弱": T.speech_tier({"emotion_score": 4.99}, {"aggression_level": "低"}, {"logic_relevance": "弱"}),
        "强/低": T.speech_tier({"emotion_score": 0}, {"aggression_level": "低"}, {"logic_relevance": "强"}),
    }
    results["tiering"] = {
        "combos": len(combos),
        "elapsed_s": round(elapsed, 6),
        "mismatches": mismatches,
        "distribution": dist,
        "thresholds": list(T.thresholds()),
        "boundary": boundary,
    }


def bench_verdict():
    from mylocalai.rumor import verdict as V

    n = 20000
    rows = []
    for _ in range(n):
        markers = {"marker_points": random.randint(0, 45), "credibility_points": random.randint(-15, 0)}
        emotion = {"emotion_score": random.choice([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
                   "error": random.random() < 0.08}
        attack = {"aggression_level": random.choice(["低", "中", "高"])}
        logic = {"logic_relevance": random.choice(["强", "中", "弱"]),
                 "logic_fallacy": random.choice(["无", "未知", "诉诸情绪", "滑坡谬误"])}
        claims = {"has_factual_claim": random.random() < 0.7,
                  "unsourced_unverifiable": random.randint(0, 3)}
        if random.random() < 0.5:
            claims = {"error": "timeout"}
        evidence = {"stance_summary": {"reliable_refute": random.randint(0, 3),
                                       "official_refute": random.randint(0, 2),
                                       "reliable_support": random.randint(0, 2),
                                       "reliable_refute_domains": ["a.gov.cn", "b.com"]}}
        rows.append((markers, emotion, attack, logic, claims, evidence))

    t0 = time.perf_counter()
    out = [V.assess(*r) for r in rows]
    elapsed = time.perf_counter() - t0

    violations = 0
    cap_ok = 0
    cap_checked = 0
    anchor_ok = 0
    anchor_checked = 0
    for (markers, emotion, attack, logic, claims, evidence), res in zip(rows, out):
        risk = res["risk_score"]
        if not (0 <= risk <= 100):
            violations += 1
        if not claims.get("error") and claims.get("has_factual_claim") is False:
            cap_checked += 1
            if risk <= V.NO_CLAIM_RISK_CAP:
                cap_ok += 1
        if not claims.get("error") and claims.get("has_factual_claim") is True:
            ss = evidence["stance_summary"]
            weight = ss["reliable_refute"] + ss["official_refute"]
            if weight >= 2:
                anchor_checked += 1
                if risk >= V.EVIDENCE_RISK_MULTI:
                    anchor_ok += 1
        if res["label"] not in V.ALL_LABELS:
            violations += 1

    dist = {}
    for res in out:
        dist[res["label"]] = dist.get(res["label"], 0) + 1

    from mylocalai.config import rules as R

    t0 = time.perf_counter()
    for _ in range(2000):
        R.load_rules()
    rules_load_ms = (time.perf_counter() - t0) / 2000 * 1000

    results["verdict"] = {
        "n": n,
        "elapsed_s": round(elapsed, 3),
        "throughput_per_s": round(n / elapsed, 1),
        "rules_load_ms_per_call": round(rules_load_ms, 4),
        "violations": violations,
        "no_claim_cap_checked": cap_checked,
        "no_claim_cap_ok": cap_ok,
        "evidence_anchor_checked": anchor_checked,
        "evidence_anchor_ok": anchor_ok,
        "label_distribution": dist,
        "thresholds": [V.THRESHOLD_HIGH, V.THRESHOLD_SUSPECT, V.THRESHOLD_DOUBT],
        "bucket_examples": {str(s): V.bucket_score(s) for s in (5, 6, 7, 8, 9, 10)},
    }


def bench_guard():
    from mylocalai.attention import deep_read as D
    from mylocalai.attention import deep_read_guard as G

    issues, _ = D.load_issues()
    rules, _ = D.load_rules()
    ev = ["e1", "e2", "e3"]
    valid_tag = issues["issues"][0]["id"]

    def base():
        return {
            "applicable": True,
            "issue_tags": [{"id": valid_tag, "confidence": "medium", "evidence_ids": ["e1"]}],
            "tensions": [{"a": "信息发布节奏", "b": "公众知情需求", "evidence_ids": ["e1"], "level": "L1"}],
            "phenomena": [{"text": "多条发言指向通报晚于传闻", "level": "L2", "evidence_ids": ["e2"], "count": "7/12"}],
            "counter_reading": {"text": "部分发言认为质疑源于误解", "evidence_ids": ["e3"]},
            "boundary": {"scope": "sample", "sample_size": 12, "proxy_flags": ["y_proxy"]},
            "uncertainty": ["代理阅读量口径为估计值"],
        }

    cases = []

    def add(name, raw, verify, expect):
        cases.append({"name": name, "raw": raw, "verify": verify, "expect": expect})

    def is_accepted(res):
        return True

    def is_rejected(res):
        return False

    def no_degraded(res):
        return res["degraded"] is False and not res["guard_dropped"]

    def is_degraded(res):
        return res["degraded"] is True and len(res["guard_dropped"]) >= 1

    def is_exit(res):
        return res["applicable"] is False and res["data"] == {}

    def remapped(res):
        tag = res["data"]["issue_tags"][0]
        return tag["id"] == "other" and tag["confidence"] == "low" and bool(res["warnings"])

    def truncated(res):
        return res["data"]["truncated"] is True and any("截断" in u for u in res["data"]["uncertainty"])

    def tags_capped(res):
        return len(res["data"]["issue_tags"]) == 3

    def confidence_low(res):
        return res["data"]["issue_tags"][0]["confidence"] == "low"

    def level_l1(res):
        return res["data"]["tensions"][0]["level"] == "L1"

    add("合法输出", base(), no_degraded, "accepted")
    add("非对象", ["not", "a", "dict"], is_rejected, "rejected")
    add("applicable 非布尔", {"applicable": "yes"}, is_rejected, "rejected")
    add("applicable=false 出口", {"applicable": False, "reason": "证据不足"}, is_exit, "exit")
    bad_tag = base(); bad_tag["issue_tags"] = [{"id": "made_up_tag", "confidence": "high", "evidence_ids": ["e1"]}]
    add("清单外议题改判 other", bad_tag, remapped, "remapped")
    bad_ev = base(); bad_ev["issue_tags"] = [{"id": valid_tag, "confidence": "high", "evidence_ids": ["e99"]}]
    add("证据编号全部无效", bad_ev, is_rejected, "rejected")
    l3 = base(); l3["tensions"][0]["level"] = "L3"; l3["phenomena"][0]["level"] = "L3"
    add("仅 L3 无支撑", l3, is_degraded, "degraded")
    blame = base(); blame["tensions"][0]["a"] = "当地部门在隐瞒实情"
    add("具体主体归因", blame, is_degraded, "degraded")
    univ = base(); univ["phenomena"][0]["text"] = "所有人都知道这件事"
    add("以偏概全", univ, is_degraded, "degraded")
    pred = base(); pred["phenomena"][0]["text"] = "这必将引发更大的争议"
    add("预测性表述", pred, is_degraded, "degraded")
    value = base(); value["counter_reading"]["text"] = "这种做法实在可悲"
    add("反方命中禁词", value, is_rejected, "rejected")
    no_counter = base(); no_counter.pop("counter_reading")
    add("缺反方解读", no_counter, is_rejected, "rejected")
    long_raw = base(); long_raw["phenomena"] = [{"text": "很长的现象描述" * 60, "level": "L1", "evidence_ids": ["e2"]}]
    add("超字数上限", long_raw, truncated, "truncated")
    many = base(); many["issue_tags"] = [
        {"id": issues["issues"][i]["id"], "confidence": "low", "evidence_ids": ["e1"]} for i in range(5)]
    add("标签超上限", many, tags_capped, "capped")
    bad_conf = base(); bad_conf["issue_tags"][0]["confidence"] = "very_high"
    add("置信度非法", bad_conf, confidence_low, "low")
    bad_level = base(); bad_level["tensions"][0]["level"] = "L9"
    add("证据等级非法", bad_level, level_l1, "L1")
    empty_tags = base(); empty_tags["issue_tags"] = []
    add("议题为空", empty_tags, is_rejected, "rejected")

    rows = []
    t0 = time.perf_counter()
    for case in cases:
        try:
            res = G.validate_and_sanitize(case["raw"], issues, ev, rules)
            ok = case["verify"](res)
            rows.append({"name": case["name"], "outcome": "accepted", "ok": bool(ok),
                         "degraded": res["degraded"], "dropped": len(res["guard_dropped"]),
                         "truncated": res["data"].get("truncated", False),
                         "expect": case["expect"]})
        except G.GuardRejected as exc:
            ok = case["verify"] is is_rejected
            rows.append({"name": case["name"], "outcome": "rejected", "ok": bool(ok),
                         "reason": str(exc)[:80], "expect": case["expect"]})
    elapsed = time.perf_counter() - t0

    results["guard"] = {
        "cases": len(rows),
        "elapsed_s": round(elapsed, 6),
        "matched": sum(1 for r in rows if r["ok"]),
        "rows": rows,
        "banned_patterns": len(rules.get("banned_patterns") or []),
        "issues": len(issues.get("issues") or []),
    }


def bench_resilience():
    from mylocalai.llm import llm_cache

    llm_cache.clear()
    os.environ.pop("LLM_CACHE_MAX", None)
    os.environ.pop("LLM_CACHE_TTL_S", None)
    os.environ.pop("LLM_CACHE_TTL_S", None)

    calls = {"n": 0}
    lock = threading.Lock()

    def produce():
        with lock:
            calls["n"] += 1
        time.sleep(0.2)
        return "value"

    threads = [threading.Thread(target=lambda: llm_cache.call("k1", produce, lambda v: True)) for _ in range(8)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    single_flight_s = time.perf_counter() - t0
    single_flight_calls = calls["n"]

    llm_cache.clear()
    calls["n"] = 0
    errors = []

    def boom():
        with lock:
            calls["n"] += 1
        time.sleep(0.1)
        raise RuntimeError("upstream down")

    def run():
        try:
            llm_cache.call("k2", boom, lambda v: True)
        except RuntimeError:
            errors.append(1)

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    failure_calls = calls["n"]
    failure_shared = len(errors)

    llm_cache.clear()
    calls["n"] = 0
    os.environ["LLM_CACHE_MAX"] = "0"
    threads = [threading.Thread(target=lambda: llm_cache.call("k3", produce, lambda v: True)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cache_off_calls = calls["n"]
    llm_cache.clear()
    calls["n"] = 0
    llm_cache.call("k3b", produce, lambda v: True)
    llm_cache.call("k3b", produce, lambda v: True)
    cache_off_sequential = calls["n"]
    os.environ.pop("LLM_CACHE_MAX", None)

    llm_cache.clear()
    calls["n"] = 0
    llm_cache.call("k4", produce, lambda v: True)
    llm_cache.call("k4", produce, lambda v: True)
    cache_on_calls = calls["n"]
    stats = llm_cache.stats()

    os.environ["LLM_CACHE_TTL_S"] = "0.05"
    llm_cache.clear()
    calls["n"] = 0
    llm_cache.call("k5", produce, lambda v: True)
    time.sleep(0.12)
    llm_cache.call("k5", produce, lambda v: True)
    ttl_calls = calls["n"]
    os.environ.pop("LLM_CACHE_TTL_S", None)

    llm_cache.clear()
    with llm_cache.deadline_scope(0.5):
        timeout = llm_cache.attempt_timeout(30.0)
        remaining = llm_cache.remaining_seconds()
    with llm_cache.deadline_scope(0.05):
        time.sleep(0.08)
        exhausted = llm_cache.budget_exhausted()

    results["resilience"] = {
        "single_flight_threads": 8,
        "single_flight_upstream_calls": single_flight_calls,
        "single_flight_wall_s": round(single_flight_s, 3),
        "failure_threads": 6,
        "failure_upstream_calls": failure_calls,
        "failure_shared_errors": failure_shared,
        "cache_off_threads": 4,
        "cache_off_upstream_calls": cache_off_calls,
        "cache_off_sequential_calls": cache_off_sequential,
        "cache_on_sequential_calls": cache_on_calls,
        "cache_stats": stats,
        "ttl_expired_calls": ttl_calls,
        "attempt_timeout_under_0_5s": round(timeout, 3),
        "remaining_under_0_5s": round(remaining, 3),
        "budget_exhausted_after_0_05s": exhausted,
    }


def bench_empty_judge():
    from mylocalai.classic import judge as J
    from mylocalai.llm import llm_client as L

    L.reset_usage()
    L.reset_cache()
    t0 = time.perf_counter()
    res = J.judge("   ")
    elapsed = time.perf_counter() - t0
    results["empty_input"] = {
        "latency_s": round(elapsed, 4),
        "usage": L.usage_snapshot(),
        "conclusion": res.get("conclusion"),
        "degraded": res.get("degraded_components"),
    }


def main():
    results["meta"] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "seed": SEED,
        "cwd": str(ROOT),
    }
    bench_attention()
    bench_fingerprint()
    bench_tiering()
    bench_verdict()
    bench_guard()
    bench_resilience()
    bench_empty_judge()
    path = OUT / "deterministic.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
