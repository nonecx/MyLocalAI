# -*- coding: utf-8 -*-
"""一次性核查脚本：把背景里的事实全部用真实代码复算一遍，并扫描候选参数。"""
from __future__ import annotations

import json
import math
import sys

sys.path.insert(0, ".")
from attn_agent.attention import (  # noqa: E402
    DEFAULT_PARAMS, attention_score, contributions, deep_read_triggered,
)

TOPICS = [
    ("老干妈回应AI裁员传闻", 30, 48316000, 149),
    ("阿雅表态一个中国", 5, 12506705, 302),
    ("长鑫存储拒绝苹果", 11, 13822047, 42),
    ("花生地被村民捡拾", 3, 2067344, 129),
    ("男子出狱砍死妻子二审", 6, 3766947, 1),
]

# 反解 S66
S66 = -math.log(1 - 0.66) / DEFAULT_PARAMS["lambda"]
print("=== A. 阈值反解 ===")
print(f"S66 = -ln(1-0.66)/lambda = {S66:.6f}")
print(f"E(S66) 校验 = {attention_score(0, 0, 0, {**DEFAULT_PARAMS}) and 0 or 0}", end="")
# 用 S66 直接算 E 校验（构造一个 x 使 term_x = S66 不可行，改用公式）
print(f"  -> 100*(1-exp(-0.7*{S66:.6f})) = {100 * (1 - math.exp(-0.7 * S66)):.6f}")

print("\n=== B. 五个真实话题复算（默认参数） ===")
rows = []
for name, x, y, z in TOPICS:
    c = contributions(x, y, z, DEFAULT_PARAMS)
    e = attention_score(x, y, z, DEFAULT_PARAMS)
    prod = (1 + x) * (1 + y) * (1 + z)
    rows.append((name, x, y, z, c["S"], e, c["term_x"], c["term_y"], c["term_z"], prod))
    print(f"{name}: x={x} y={y} z={z}")
    print(f"   term_x={c['term_x']:.6f} term_y={c['term_y']:.6f} term_z={c['term_z']:.6f} "
          f"S={c['S']:.6f} E={e:.4f} deep_read={deep_read_triggered(e)}")
    print(f"   占比: x={c['term_x']/c['S']*100:.1f}% y={c['term_y']/c['S']*100:.1f}% "
          f"z={c['term_z']/c['S']*100:.1f}%")
    print(f"   乘积 (1+x)(1+y)(1+z) = {prod:.4g}  log10 = {math.log10(prod):.3f}")
    print(f"   距 S66 缺口 = {S66 - c['S']:.4f}（= {(S66 - c['S'])/0.1:.2f} 个\"十分位\"）")

print("\n=== C. 单变量充分性（其余两维保持该话题实测值不变） ===")
for name, x, y, z in TOPICS:
    c = contributions(x, y, z, DEFAULT_PARAMS)
    need_x = math.expm1(S66 * math.log1p(100) / 0.2 - c["term_y"] - c["term_z"]) if False else None
    # term_x 需要达到的值
    tx_need = S66 - c["term_y"] - c["term_z"]
    x_need = math.expm1(tx_need * math.log1p(100) / 0.2)
    ty_need = S66 - c["term_x"] - c["term_z"]
    y_need = math.expm1(ty_need * math.log1p(100000) / 0.5)
    tz_need = S66 - c["term_x"] - c["term_y"]
    z_need = math.expm1(tz_need * math.log1p(1000) / 0.3)
    print(f"{name}: 需 x={x_need:.4g} / y={y_need:.4g} / z={z_need:.4g}"
          f"（当前 x={x}, y={y}, z={z}）")
    # 纯单变量（其余为 0）
    x_solo = math.expm1(S66 * math.log1p(100) / 0.2)
    print(f"   纯单变量（其余=0）: x={x_solo:.4g} y={math.expm1(S66*math.log1p(100000)/0.5):.4g} "
          f"z={math.expm1(S66*math.log1p(1000)/0.3):.4g}")

print("\n=== D. 每十倍（一个数量级）的边际收益 ===")
for w, v0, label in ((0.2, 100, "x"), (0.5, 100000, "y"), (0.3, 1000, "z")):
    slope = w * math.log(10) / math.log1p(v0)
    print(f"  {label}: w={w} v0={v0} -> 每 10 倍 S +{slope:.6f}（≈{slope/0.1*100:.1f}% of 0.1）")
print("  => S66=1.5412 需要 1.5412/0.1 = %.2f 个\"数量级当量\"" % (S66 / 0.1))

print("\n=== E. 边际 dE/d(数量级)（在各话题当前点上） ===")
for name, x, y, z, s, e, tx, ty, tz, prod in rows:
    k = 0.7 * math.exp(-0.7 * s)
    print(f"{name}: E={e:.2f} -> x 每10倍 +{k*0.0998:.2f} 分, y 每10倍 +{k*0.1:.2f} 分, "
          f"z 每10倍 +{k*0.1:.2f} 分")

print("\n=== F. 情景复算（默认参数） ===")
scen = [
    ("全项 10 倍基准 x=1e3,y=1e6,z=1e4", 1000, 1e6, 1e4),
    ("千万阅读/千篇/十万留言 x=1e3,y=1e7,z=1e5", 1000, 1e7, 1e5),
    ("亿级阅读/千篇/百万留言 x=1e3,y=1e8,z=1e6", 1000, 1e8, 1e6),
    ("十亿级/五千篇/千万留言 x=5e3,y=1e9,z=1e7", 5000, 1e9, 1e7),
    ("国民级参考 x=2e3,y=1e9,z=1e5", 2000, 1e9, 1e5),
    ("头部热搜参考 x=30,y=5e7,z=150", 30, 5e7, 150),
    ("三项都在锚点 x=100,y=1e5,z=1e3", 100, 1e5, 1000),
]
for label, x, y, z in scen:
    c = contributions(x, y, z, DEFAULT_PARAMS)
    e = attention_score(x, y, z, DEFAULT_PARAMS)
    print(f"{label}: S={c['S']:.4f} E={e:.4f} 触发={deep_read_triggered(e)}")

print("\n=== G. 候选参数扫描 ===")
CANDS = {
    "默认(现状)": dict(DEFAULT_PARAMS),
    "方案A 仅调 lambda=0.85": {**DEFAULT_PARAMS, "lambda": 0.85},
    "方案B 重锚点 y0=1e6,z0=100,w=(0.25,0.45,0.30),lambda=0.8": {
        "x0": 100, "y0": 1e6, "z0": 100, "w_x": 0.25, "w_y": 0.45, "w_z": 0.30,
        "lambda": 0.8, "epsilon": 1e-6},
    "方案C 温和: lambda=0.8, y0=5e5, z0=300, w=(0.25,0.45,0.30)": {
        "x0": 100, "y0": 5e5, "z0": 300, "w_x": 0.25, "w_y": 0.45, "w_z": 0.30,
        "lambda": 0.8, "epsilon": 1e-6},
}
for cname, p in CANDS.items():
    s66 = -math.log(1 - 0.66) / p["lambda"]
    coef = p["w_y"] / math.log1p(p["y0"])  # 每 log 单位
    budget = math.exp(s66 / (p["w_y"] / math.log1p(p["y0"])))
    print(f"\n--- {cname} ---")
    print(f"  S66={s66:.4f}  \"乘积预算\"≈{budget:.4g}  E(锚点三连)="
          f"{attention_score(p['x0'], p['y0'], p['z0'], p):.2f}")
    for label, x, y, z in scen:
        e = attention_score(x, y, z, p)
        print(f"    {label}: E={e:.2f} 触发={deep_read_triggered(e)}")
    for name, x, y, z in TOPICS:
        e = attention_score(x, y, z, p)
        print(f"    实测[{name}]: E={e:.2f} 触发={deep_read_triggered(e)}")
