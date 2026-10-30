# -*- coding: utf-8 -*-
"""第八轮：验证'乘积法则'恒等式 + 两个候选参数组的完整表现 + 边界不一致。"""
from __future__ import annotations

import math
import sys

sys.path.insert(0, ".")
from attn_agent.attention import (  # noqa: E402
    DEFAULT_BANDS, DEFAULT_PARAMS, attention_score, classify, contributions,
    deep_read_triggered,
)

TOPICS = [
    ("老干妈", 30, 48316000, 149),
    ("阿雅", 5, 12506705, 302),
    ("长鑫", 11, 13822047, 42),
    ("花生地", 3, 2067344, 129),
    ("男子", 6, 3766947, 1),
]
WX, WY, WZ = DEFAULT_PARAMS["w_x"], DEFAULT_PARAMS["w_y"], DEFAULT_PARAMS["w_z"]
X0, Y0, Z0 = DEFAULT_PARAMS["x0"], DEFAULT_PARAMS["y0"], DEFAULT_PARAMS["z0"]
S66 = -math.log(1 - 0.66) / DEFAULT_PARAMS["lambda"]

print("=== CA. 三个维度的'每数量级价格' H = ln(1+v0)/w ===")
for w, v0, lab in ((WX, X0, "x"), (WY, Y0, "y"), (WZ, Z0, "z")):
    print(f"  {lab}: H = ln(1+{v0:g})/{w} = {math.log1p(v0)/w:.4f}")
Hs = [math.log1p(v0) / w for w, v0 in ((WX, X0), (WY, Y0), (WZ, Z0))]
Hbar = sum(Hs) / 3
print(f"  三者均值 = {Hbar:.4f}，最大相对偏差 = "
      f"{max(abs(h - Hbar) / Hbar for h in Hs) * 100:.3f}%")
print("  => 三个维度的'每数量级售价'几乎相同，权重 w 与锚点 v0 几乎互相抵消。")

print("\n=== CB. 因此 S 近似是一个纯'乘积法则' ===")
print("  S ≈ (1/Hbar) * ln((1+x)(1+y)(1+z))")
print(f"  S66 = {S66:.4f} -> 乘积预算 = exp({S66:.4f} * {Hbar:.4f}) = "
      f"{math.exp(S66 * Hbar):.4g}")
gm = math.exp(S66 * Hbar / 3)
print(f"  => 几何平均门槛 GM(x,y,z) >= {gm:.4g}")
print("\n  验证（近似 vs 精确）：")
for name, x, y, z in TOPICS:
    prod = (1 + x) * (1 + y) * (1 + z)
    s_approx = math.log(prod) / Hbar
    c = contributions(x, y, z, DEFAULT_PARAMS)
    gm_real = prod ** (1 / 3)
    print(f"    {name}: 乘积={prod:.4g} GM={gm_real:,.0f}  "
          f"S近似={s_approx:.4f} S精确={c['S']:.4f} 差={s_approx - c['S']:+.4f}  "
          f"GM/门槛={gm_real / gm:.4f}")

print("\n=== CC. 每个话题差多少倍（乘积口径）===")
for name, x, y, z in TOPICS:
    prod = (1 + x) * (1 + y) * (1 + z)
    print(f"  {name}: 乘积={prod:.4g}，需 {math.exp(S66*Hbar)/prod:,.0f} 倍"
          f"（10^{math.log10(math.exp(S66*Hbar)/prod):.2f}）")

print("\n=== CD. 真实数据的三维动态范围（这是 y 主导的真正原因）===")
for lab, vals in (("x", [v[1] for v in TOPICS]), ("y", [v[2] for v in TOPICS]),
                  ("z", [v[3] for v in TOPICS])):
    print(f"  {lab}: 实测范围 {min(vals):g} ~ {max(vals):g}"
          f" = {math.log10(max(vals) / min(vals)):.2f} 个数量级")
print("  => y 跨 7.37 个数量级，x 只跨 1.30，z 只跨 2.48。")
print("     y 项占比高不是因为 w_y=0.5，而是因为 y 的数据动态范围最大。")

print("\n=== CE. term_y 并非以 w_y 为上限（修正背景表述）===")
for y in (1e5, 1e6, 1e7, 1e8, 1e9, 1e10, 1e12):
    t = WY * math.log1p(y) / math.log1p(Y0)
    print(f"  y={y:8.0e} -> term_y={t:.4f}"
          f"{'  (> w_y=0.5，说明 w_y 不是上限)' if t > WY else ''}")

print("\n=== CF. 两个候选参数组 ===")
CAND = {
    "方案一 lambda=0.8（其余不动）": {**DEFAULT_PARAMS, "lambda": 0.8},
    "方案二 y0=1e6,w=(0.25,0.40,0.35),lambda=1.0": {
        "x0": 100, "y0": 1e6, "z0": 1000, "w_x": 0.25, "w_y": 0.40, "w_z": 0.35,
        "lambda": 1.0, "epsilon": 1e-6},
}
SCEN = [
    ("真实:老干妈", 30, 48316000, 149),
    ("真实:阿雅", 5, 12506705, 302),
    ("真实:长鑫", 11, 13822047, 42),
    ("真实:花生地", 3, 2067344, 129),
    ("真实:男子", 6, 3766947, 1),
    ("中腰部M1", 100, 1e6, 1000),
    ("大热门M2", 1000, 1e7, 1e4),
    ("大热门M3", 500, 3e7, 2e4),
    ("国民N1", 2000, 1e9, 1e5),
    ("国民N3", 1000, 1e8, 1e5),
    ("超大N2", 5000, 1e10, 1e6),
]
for cn, p in [("默认(现状)", DEFAULT_PARAMS)] + list(CAND.items()):
    print(f"\n--- {cn} ---")
    print(f"    params: x0={p['x0']:g} y0={p['y0']:g} z0={p['z0']:g} "
          f"w=({p['w_x']},{p['w_y']},{p['w_z']}) lambda={p['lambda']:g}")
    s66p = -math.log(1 - 0.66) / p["lambda"]
    hs = [math.log1p(p[k]) / p[w] for k, w in (("x0", "w_x"), ("y0", "w_y"), ("z0", "w_z"))]
    hb = sum(hs) / 3
    print(f"    S66={s66p:.4f}  乘积预算={math.exp(s66p*hb):.4g}  "
          f"GM门槛={math.exp(s66p*hb/3):,.0f}")
    for label, x, y, z in SCEN:
        c = contributions(x, y, z, p)
        e = attention_score(x, y, z, p)
        print(f"    {label:12s} S={c['S']:.4f} E={e:6.2f} "
              f"y占比={c['term_y']/c['S']*100:5.1f}% "
              f"{'触发' if deep_read_triggered(e) else '未触发'}")

print("\n=== CG. 边界不一致：band 用 >=，deep_read 用 > ===")
print(f"  classify(66.0) = {classify(66.0)!r}（bands min=66 -> 高关注）")
print(f"  deep_read_triggered(66.0) = {deep_read_triggered(66.0)}")
print("  => E 恰好等于 66 时：档位是'高关注'，但不触发深读。两处口径不一致。")

print("\n=== CH. 60~70 阈值区间在本批上的触发数（说明 66 不是关键旋钮）===")
for th in (40, 50, 55, 60, 62, 64, 66, 70):
    n = sum(1 for _, x, y, z in TOPICS
            if attention_score(x, y, z, DEFAULT_PARAMS) > th)
    print(f"  阈值 {th}: {n}/5 触发")
