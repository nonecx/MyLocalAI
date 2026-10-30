# -*- coding: utf-8 -*-
"""第七轮：核对背景中的情景表数字 + 精确求 y 的临界曲线。"""
from __future__ import annotations

import math
import sys

sys.path.insert(0, ".")
from attn_agent.attention import DEFAULT_PARAMS, attention_score, contributions  # noqa: E402

print("=== BA. 背景情景表逐行复算（默认参数） ===")
ROWS = [
    ("全项 10 倍基准", 1000, 1e6, 1e4, 59.73, False),
    ("千万阅读/千篇/十万留言", 1000, 1e7, 1e5, 64.99, False),
    ("亿级阅读/千篇/百万留言", 1000, 1e8, 1e6, 71.62, True),
    ("十亿级/五千篇/千万留言", 5000, 1e9, 1e7, 76.50, True),
]
for label, x, y, z, claimed, claimed_trig in ROWS:
    e = attention_score(x, y, z, DEFAULT_PARAMS)
    print(f"  {label}: x={x:g} y={y:g} z={z:g}")
    print(f"     复算 E={e:.4f}  背景称 {claimed}  差 {e-claimed:+.4f}  "
          f"触发={e > 66.0}（背景称 {claimed_trig}）")
    # 试 10 倍 y
    e10 = attention_score(x, y * 10, z, DEFAULT_PARAMS)
    print(f"     若 y 改 {y*10:g}: E={e10:.4f}（背景称 {claimed}）"
          f"{'  <-- 与背景吻合' if abs(e10 - claimed) < 0.01 else ''}")

print("\n=== BB. 因此：背景表中'亿级'/'十亿级'两行的数字，与 y 标注值相差一个数量级 ===")
print("  复算结论：x=1e3,y=1e8,z=1e6 -> E=69.56；x=5e3,y=1e9,z=1e7 -> E=74.80。")
print("  背景的 71.62 / 76.50 对应 y=1e9 / y=1e10（即标注值再大 10 倍）。")
print("  => '十亿级阅读才稳定越过 66' 这个结论本身成立，且比我复算的更宽松：")
print("     在本行的 x,z 水平上，y=1e8 就已经过线（69.56）。")

print("\n=== BC. 关键：y 的临界值（使 E 恰好 66）随 (x, z) 怎么变 ===")
S66 = -math.log(1 - 0.66) / DEFAULT_PARAMS["lambda"]
print(f"  {'x':>8} {'z':>10} {'term_x':>8} {'term_z':>8} {'term_y需':>10} {'y临界':>12} {'log10':>7}")
for xx in (30, 100, 300, 1000, 3000, 10000):
    for zz in (149, 1000, 1e4, 1e5, 1e6):
        cx = 0.2 * math.log1p(xx) / math.log1p(100)
        cz = 0.3 * math.log1p(zz) / math.log1p(1000)
        ty = S66 - cx - cz
        if ty <= 0:
            print(f"  {xx:8g} {zz:10g} {cx:8.4f} {cz:8.4f} {'<=0':>10} {'(x,z已足够)':>12}")
            continue
        yc = math.expm1(ty * math.log1p(1e5) / 0.5)
        print(f"  {xx:8g} {zz:10g} {cx:8.4f} {cz:8.4f} {ty:10.4f} {yc:12.4g} "
              f"{math.log10(yc):7.2f}")

print("\n=== BD. 本批五个话题的 (x,z) 实际落点 => y 临界值 ===")
for name, x, y, z in (("老干妈", 30, 48316000, 149), ("阿雅", 5, 12506705, 302),
                      ("长鑫", 11, 13822047, 42), ("花生地", 3, 2067344, 129),
                      ("男子", 6, 3766947, 1)):
    c = contributions(x, 1.0, z, DEFAULT_PARAMS)
    ty = S66 - c["term_x"] - c["term_z"]
    yc = math.expm1(ty * math.log1p(1e5) / 0.5)
    print(f"  {name}: x={x}, z={z} -> y 临界 = {yc:.4g}（log10={math.log10(yc):.2f}）"
          f"  实测 y={y:.4g}（log10={math.log10(y):.2f}）  "
          f"缺口 {math.log10(yc)-math.log10(y):.2f} 个数量级")

print("\n=== BE. 反过来问：如果 y 就是 10^7~5e7（本批量级），x,z 要多少才过 66 ===")
for y in (1e7, 2e7, 5e7):
    # 求最小的 (x+z 数量级当量) 组合：令 term_x + term_z = S66 - term_y
    need = S66 - 0.5 * math.log1p(y) / math.log1p(1e5)
    if need <= 0:
        print(f"  y={y:g}: y 项已足够（term_y={0.5*math.log1p(y)/math.log1p(1e5):.4f} >= S66）")
        continue
    print(f"  y={y:g}: term_x+term_z 需 >= {need:.4f}（= {need/0.1:.2f} 个数量级当量）")
    for xx in (30, 100, 300, 1000, 3000, 10000, 1e5):
        tz_need = need - 0.2 * math.log1p(xx) / math.log1p(100)
        if tz_need <= 0:
            print(f"      x={xx:g} 时 z 项无需贡献（x 已够）")
            break
        zc = math.expm1(tz_need * math.log1p(1000) / 0.3)
        print(f"      x={xx:8g} -> z 需 >= {zc:.4g}（log10={math.log10(zc):.2f}）")
