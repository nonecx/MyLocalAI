# -*- coding: utf-8 -*-
"""一次性核对脚本：把论文正文里的硬编码数字逐条对回原始实验数据。

只读，不修改任何数据；输出一份“声称 vs 数据”对照表，便于人工复核。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "docs" / "paper" / "data"


def main() -> int:
    det = json.loads((DATA / "deterministic.json").read_text(encoding="utf-8"))
    api = json.loads((DATA / "api_summary.json").read_text(encoding="utf-8"))
    tests = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))

    attn = det["attention"]
    rows = attn["profile_rows"]

    print("== 关注度定标点 ==")
    print(json.dumps(attn["anchors"], ensure_ascii=False, indent=1))
    print("S66 =", attn["s66"])

    print("\n== 5 个真实话题两档对比 ==")
    for r in rows:
        delta = r["reachable"] - r["spec"]
        print(f"{r['topic']:<16} spec={r['spec']:>6.2f} reachable={r['reachable']:>6.2f} delta={delta:+.2f}")
    over_spec = sum(1 for r in rows if r["spec"] > 66)
    over_reach = sum(1 for r in rows if r["reachable"] > 66)
    deltas = [r["reachable"] - r["spec"] for r in rows]
    print(f"越 66：spec {over_spec}/{len(rows)}，reachable {over_reach}/{len(rows)}")
    print(f"delta 范围：{min(deltas):+.2f} ~ {max(deltas):+.2f}，上升 {sum(1 for d in deltas if d > 0)} 个，下降 {sum(1 for d in deltas if d < 0)} 个")

    print("\n== 护栏 17 例 ==")
    for r in det["guard"]["rows"]:
        print(f"{r['name']:<22} outcome={r['outcome']:<9} expect={r.get('expect')!s:<9} ok={r['ok']}")

    print("\n== 规模 ==")
    print(json.dumps({k: tests[k] for k in ("code_files", "code_loc", "json_files", "test_files",
                                            "test_loc", "cases", "elapsed_s", "skipped", "failed")},
                     ensure_ascii=False))

    print("\n== API ==")
    print(json.dumps({"cold": api["cold_latency_s"], "warm": api["warm_latency_s"],
                      "identical": api["warm_cache_hit_identical"], "degraded": api["degraded_items"],
                      "tokens": api["total_tokens"], "drift": api["no_cache_drift"],
                      "conc": api["concurrency"]["upstream"]["requests"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
