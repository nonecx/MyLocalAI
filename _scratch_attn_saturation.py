# -*- coding: utf-8 -*-
"""第五轮（修正版）：y 项的凹性刻画 + 预算分析 + CLI 等价校验。"""
from __future__ import annotations

import math
import sys

sys.path.insert(0, ".")
from attn_agent.attention import DEFAULT_PARAMS, attention_score, contributions  # noqa: E402

WX, WY, WZ = DEFAULT_PARAMS["w_x"], DEFAULT_PARAMS["w_y"], DEFAULT_PARAMS["w_z"]
X0, Y0, Z0 = DEFAULT_PARAMS["x0"], DEFAULT_PARAMS["y0"], DEFAULT_PARAMS["z0"]
S66 = -math.log(1 - 0.66) / DEFAULT_PARAMS["lambda"]

print("=== Z1. 三个维度的'每数量级售价'完全一致（这是锚点的设计结果） ===")
for w, v0, lab in ((WX, X0, "x"), (WY, Y0, "y"), (WZ, Z0, "z")):
    print(f"  {lab}: w={w} v0={v0:g}  ln(1+v0)/w={math.log1p(v0)/w:.4f}"
          f"  每10倍 S +{w*math.log(10)/math.log1p(v0):.6f}")
print("  => ln(1+v0)/w 三者都是 ≈23.0：一个数量级在任何维度上都恰好值 0.1 的 S。")

print("\n=== Z2. 因此 S66 = 1.5412 就是一个'数量级预算' ===")
print(f"  S66 = {S66:.4f} = {S66/0.1:.2f} 个数量级当量（decade-equivalents）")
print("  每个话题必须在 x+y+z 上凑够 15.4 个数量级当量。")

print("\n=== Z3. 五个话题各出了多少'数量级当量' ===")
for name, x, y, z in (("老干妈", 30, 48316000, 149), ("阿雅", 5, 12506705, 302),
                      ("长鑫", 11, 13822047, 42), ("花生地", 3, 2067344, 129),
                      ("男子", 6, 3766947, 1)):
    c = contributions(x, y, z, DEFAULT_PARAMS)
    print(f"  {name}: S={c['S']:.4f} = {c['S']/0.1:5.2f} 个数量级当量"
          f"（x={c['term_x']/0.1:.2f} y={c['term_y']/0.1:.2f} z={c['term_z']/0.1:.2f}）"
          f"  缺口 {S66/0.1 - c['S']/0.1:.2f} 个")

print("\n=== Z4. 凹性的真正含义：y 从 10^7 再涨，每个数量级只值 0.1 分（恒定） ===")
print("  但'要达到 S66'需要的是绝对量。看 term_y 与 S66 的差距：")
for y in (1e5, 1e6, 1e7, 1e8, 1e9, 1e10):
    t = WY * math.log1p(y) / math.log1p(Y0)
    print(f"  y={y:8.0e}: term_y={t:.4f}（{t/0.1:5.2f} 个数量级当量）"
          f"  距 S66 还差 {S66-t:.4f}"
          f"（= y 还要涨 10^{(S66-t)/0.1:.1f} 倍，即到 {math.expm1((S66-t)*math.log1p(Y0)/WY):.3g}）")

print("\n=== Z5. 预算分析：给定 y，x/z 最多能凑出多少 E ===")
print("  （x、z 取'现实上限'量级，看 y 要到多少才能过 66）")
CASES = [
    ("x<=1e3, z<=1e4（中腰部）", 1000, 10000),
    ("x<=3e3, z<=1e5（大热门）", 3000, 100000),
    ("x<=5e3, z<=1e6（爆款）", 5000, 1000000),
]
for tag, xx, zz in CASES:
    cx = WX * math.log1p(xx) / math.log1p(X0)
    cz = WZ * math.log1p(zz) / math.log1p(Z0)
    ty_need = S66 - cx - cz
    y_need = math.expm1(ty_need * math.log1p(Y0) / WY) if ty_need > 0 else 0
    print(f"  {tag}: term_x={cx:.3f} term_z={cz:.3f} -> term_y 需 {ty_need:.3f}"
          f" -> y >= {y_need:.4g}（log10={math.log10(y_need) if y_need>0 else 0:.2f}）")

print("\n=== Z6. 反过来说：y 的现实上限内，66 到底可不可达 ===")
for y in (1e7, 1e8, 1e9, 1e10, 1e11):
    best = 0.0
    bestcfg = None
    for xx in (100, 300, 1000, 3000, 10000, 1e5):
        for zz in (100, 1000, 1e4, 1e5, 1e6, 1e7):
            e = attention_score(xx, y, zz, DEFAULT_PARAMS)
            if e > best:
                best, bestcfg = e, (xx, zz)
    print(f"  y={y:8.0e} 且 x,z 取遍 {100}~{1e7}: 最高 E={best:.2f}"
          f"（x={bestcfg[0]:.0e}, z={bestcfg[1]:.0e}）{'  <- 过线' if best > 66 else ''}")

print("\n=== Z7. 候选 P2 / P3 的关键 CLI 点复算（与 --rules 文件等价） ===")
CAND = {
    "P2": {"x0": 100, "y0": 1e6, "z0": 1e4, "w_x": 0.25, "w_y": 0.45, "w_z": 0.3,
           "lambda": 0.9, "epsilon": 1e-6},
    "P3": {"x0": 100, "y0": 3e5, "z0": 3000, "w_x": 0.3, "w_y": 0.4, "w_z": 0.3,
           "lambda": 1.0, "epsilon": 1e-6},
}
PTS = [("老干妈", 30, 48316000, 149), ("阿雅", 5, 12506705, 302),
       ("花生地", 3, 2067344, 129), ("中腰部M1", 100, 1e6, 1000),
       ("热门M2", 1000, 1e7, 1e4), ("国民N1", 2000, 1e9, 1e5),
       ("国民N3", 1000, 1e8, 1e5)]
for cn, p in CAND.items():
    print(f"--- {cn}: x0={p['x0']:g} y0={p['y0']:g} z0={p['z0']:g} "
          f"w=({p['w_x']},{p['w_y']},{p['w_z']}) lambda={p['lambda']:g} ---")
    s66p = -math.log(1 - 0.66) / p["lambda"]
    print(f"    S66({p['lambda']:g})={s66p:.4f} = {s66p/0.1:.2f} 个数量级当量")
    for label, x, y, z in PTS:
        e = attention_score(x, y, z, p)
        print(f"    {label:8s}: E={e:6.2f} {'触发' if e > 66 else '未触发'}")

print("\n=== Z8. 建议方案对'真实话题'与'国民级'的分离度 ===")
for cn, p in [("默认", DEFAULT_PARAMS)] + list(CAND.items()):
    real = [attention_score(x, y, z, p) for _, x, y, z in PTS[:3]]
    nat = [attention_score(x, y, z, p) for _, x, y, z in PTS[5:7]]
    mid = [attention_score(x, y, z, p) for _, x, y, z in PTS[3:5]]
    print(f"  {cn:4s}: 真实最高={max(real):5.2f}  中腰最高={max(mid):5.2f}  "
          f"国民最低={min(nat):5.2f}  分离度(国民最低-真实最高)={min(nat)-max(real):5.2f}")
