# -*- coding: utf-8 -*-
"""第二轮：档位边界、结构诊断、候选参数网格搜索（全部用真实代码算）。"""
from __future__ import annotations

import itertools
import math
import sys

sys.path.insert(0, ".")
from attn_agent.attention import (  # noqa: E402
    DEFAULT_PARAMS, attention_score, contributions, deep_read_triggered,
)

LAM = DEFAULT_PARAMS["lambda"]

print("=== H. 档位边界反解成 S（默认 lambda=0.7） ===")
for e in (33.0, 50.0, 66.0, 75.0, 90.0, 99.0):
    s = -math.log(1 - e / 100) / LAM
    print(f"  E={e:5.1f} -> S={s:.4f}（= {s/0.1:.2f} 个\"数量级当量\"）")

print("\n=== I. E 的凹性：高关注带 66~100 在 S 上有多宽 ===")
s66 = -math.log(1 - 0.66) / LAM
s99 = -math.log(1 - 0.99) / LAM
print(f"  S: 66->{s66:.3f}, 99->{s99:.3f}，宽 {s99-s66:.3f}；"
      f"而 33->50->66 三段总宽只有 {s66 - (-math.log(1-0.33)/LAM):.3f}")
print("  => 高关注带吞掉 S>1.54 的全部空间，E 越高越不可分辨")

print("\n=== J. 若 x、z 给到'国民级合理上限'，y 还要多少 ===")
for (xx, zz, tag) in ((1000, 10000, "千篇/万评"), (2000, 100000, "2千篇/10万评"),
                      (5000, 1000000, "5千篇/百万评")):
    c = contributions(xx, 1.0, zz, DEFAULT_PARAMS)
    need_ty = s66 - c["term_x"] - c["term_z"]
    y_need = math.expm1(need_ty * math.log1p(100000) / 0.5)
    print(f"  {tag}: term_x={c['term_x']:.4f} term_z={c['term_z']:.4f} -> term_y 需 {need_ty:.4f}"
          f" -> y >= {y_need:.4g}")
    for yv in (1e7, 1e8, 1e9, 1e10):
        e = attention_score(xx, yv, zz, DEFAULT_PARAMS)
        print(f"      y={yv:.0e}: E={e:.2f} 触发={deep_read_triggered(e)}")

print("\n=== K. y 项每十分位的实际售价 vs 权重 w 的表面大小 ===")
for w, v0, lab in ((0.2, 100, "x"), (0.5, 100000, "y"), (0.3, 1000, "z")):
    print(f"  {lab}: w={w} v0={v0:g}  ln(1+v0)/w={math.log1p(v0)/w:.4f}"
          f"  -> 每 10 倍 S +{w*math.log(10)/math.log1p(v0):.6f}"
          f"  -> 每 100 倍 S +{w*math.log(100)/math.log1p(v0):.6f}")
print("  注：三者的 ln(1+v0)/w 都≈23，即三个维度的'每数量级售价'几乎相同（≈0.1/decade）")

print("\n=== L. 候选参数网格搜索 ===")
SCEN = [
    ("真实:老干妈", 30, 48316000, 149),
    ("真实:阿雅", 5, 12506705, 302),
    ("真实:长鑫", 11, 13822047, 42),
    ("真实:花生地", 3, 2067344, 129),
    ("真实:男子", 6, 3766947, 1),
    ("中腰部M1", 100, 1e6, 1000),
    ("热门M2", 1000, 1e7, 1e4),
    ("大热门M3", 500, 3e7, 2e4),
    ("国民N1", 2000, 1e9, 1e5),
    ("国民N3", 1000, 1e8, 1e5),
    ("超大N2", 5000, 1e10, 1e6),
    ("国民+评论N4", 3000, 2e9, 5e5),
]
REAL = SCEN[:5]
GOOD = [s for s in SCEN if s[0].startswith(("国民", "超大"))]


def evaluate(p):
    """返回 (是否合格, 真实话题最高E, 国民情景最低E, 中腰部最高E, 明细)。"""
    real_e = [attention_score(x, y, z, p) for _, x, y, z in REAL]
    good_e = [attention_score(x, y, z, p) for _, x, y, z in GOOD]
    mid_e = [attention_score(x, y, z, p) for _, x, y, z in SCEN[5:8]]
    ok = (max(real_e) < 66.0 and min(good_e) > 66.0
          and max(mid_e) < 66.0 and min(good_e) - max(real_e) >= 4.0)
    return ok, max(real_e), min(good_e), max(mid_e), real_e, good_e


grid = []
for x0 in (50, 100, 200, 500):
    for y0 in (1e5, 3e5, 5e5, 1e6):
        for z0 in (300, 1000, 3000, 1e4):
            for wx, wy, wz in ((0.2, 0.5, 0.3), (0.25, 0.45, 0.3), (0.3, 0.4, 0.3),
                               (0.25, 0.5, 0.25), (0.2, 0.45, 0.35), (0.3, 0.45, 0.25)):
                for lam in (0.6, 0.7, 0.8, 0.9, 1.0, 1.1):
                    p = {"x0": x0, "y0": y0, "z0": z0, "w_x": wx, "w_y": wy, "w_z": wz,
                         "lambda": lam, "epsilon": 1e-6}
                    ok, rmax, gmin, mmax, real_e, good_e = evaluate(p)
                    if ok:
                        # 排序分：国民情景触发要稳（高于66越多越好，但不>80），
                        # 真实话题要低，间隔要大
                        spread = gmin - rmax
                        score = spread - abs(gmin - 71.0) * 0.5
                        grid.append((score, spread, rmax, gmin, mmax, p, real_e, good_e))

grid.sort(key=lambda t: -t[0])
print(f"合格参数组数量：{len(grid)} / {4*4*4*6*6} = {4*4*4*6*6}")
for score, spread, rmax, gmin, mmax, p, real_e, good_e in grid[:12]:
    print(f"\n  score={score:.2f} spread={spread:.2f} 真实最高={rmax:.2f} "
          f"国民最低={gmin:.2f} 中腰最高={mmax:.2f}")
    print(f"    params: x0={p['x0']:g} y0={p['y0']:g} z0={p['z0']:g} "
          f"w=({p['w_x']},{p['w_y']},{p['w_z']}) lambda={p['lambda']:g}")
    print(f"    实测E: " + " ".join(f"{e:.1f}" for e in real_e))
    print(f"    国民E: " + " ".join(f"{e:.1f}" for e in good_e))

print("\n=== M. 三组人工精选候选的完整明细 ===")
CANDIDATES = {
    "默认(现状)": dict(DEFAULT_PARAMS),
    "候选P1 仅提 lambda=1.0": {**DEFAULT_PARAMS, "lambda": 1.0},
    "候选P2 y0=1e6,z0=1e4,w=(0.25,0.45,0.30),lambda=0.9": {
        "x0": 100, "y0": 1e6, "z0": 1e4, "w_x": 0.25, "w_y": 0.45, "w_z": 0.30,
        "lambda": 0.9, "epsilon": 1e-6},
    "候选P3 y0=3e5,z0=3000,w=(0.3,0.4,0.3),lambda=1.0": {
        "x0": 100, "y0": 3e5, "z0": 3000, "w_x": 0.3, "w_y": 0.4, "w_z": 0.3,
        "lambda": 1.0, "epsilon": 1e-6},
    "候选P4 只动阈值不动参数(阈值改60)": dict(DEFAULT_PARAMS),
}
for cname, p in CANDIDATES.items():
    print(f"\n--- {cname} ---")
    for label, x, y, z in SCEN:
        e = attention_score(x, y, z, p)
        print(f"   {label:14s} x={x:<8g} y={y:<12g} z={z:<8g} E={e:6.2f} "
              f"{'触发' if deep_read_triggered(e) else '未触发'}")
