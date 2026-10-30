# -*- coding: utf-8 -*-
"""报告合成前的一次性复算：所有将写入报告的 E 值都出自本脚本。"""
from __future__ import annotations

import math
import sys

sys.path.insert(0, ".")
from attn_agent.attention import (  # noqa: E402
    DEFAULT_BANDS, DEFAULT_PARAMS, attention_score, classify, contributions,
    deep_read_triggered,
)

TOPICS = [
    ("老干妈回应AI裁员传闻", 30, 48316000, 149),
    ("阿雅表态一个中国", 5, 12506705, 302),
    ("长鑫存储拒绝苹果", 11, 13822047, 42),
    ("花生地被村民捡拾", 3, 2067344, 129),
    ("男子出狱砍死妻子二审", 6, 3766947, 1),
]
SCEN = TOPICS + [
    ("中腰部M1", 100, 1e6, 1000),
    ("大热门M2", 1000, 1e7, 1e4),
    ("大热门M3", 500, 3e7, 2e4),
    ("国民N1", 2000, 1e9, 1e5),
    ("国民N3", 1000, 1e8, 1e5),
    ("超大N2", 5000, 1e10, 1e6),
]
P4 = {"x0": 100, "y0": 1e6, "z0": 300, "w_x": 0.3, "w_y": 0.4, "w_z": 0.3,
      "lambda": 0.8, "epsilon": 1e-6}

print("=== A. 五个真实话题（默认参数） ===")
print(f"{'话题':<22}{'x':>6}{'y':>12}{'z':>6}{'S':>9}{'E':>9}{'档位':>7}  深读  y占比")
for name, x, y, z in TOPICS:
    c = contributions(x, y, z, DEFAULT_PARAMS)
    e = attention_score(x, y, z, DEFAULT_PARAMS)
    print(f"{name:<22}{x:>6}{y:>12}{z:>6}{c['S']:>9.4f}{e:>9.4f}{classify(e):>7}"
          f"  {deep_read_triggered(e)}  {c['term_y']/c['S']*100:.1f}%")

print("\n=== B. S66 反解与闭合 ===")
s66 = -math.log(1 - 0.66) / DEFAULT_PARAMS["lambda"]
print(f"  S66 = -ln(1-0.66)/0.7 = {s66:.6f}")
print(f"  100*(1-exp(-0.7*S66)) = {100*(1-math.exp(-DEFAULT_PARAMS['lambda']*s66)):.6f}")
print(f"  classify(66.0)={classify(66.0)!r}  deep_read_triggered(66.0)={deep_read_triggered(66.0)}")

print("\n=== C. 达标情景表（默认参数） ===")
rows = [
    ("全项10倍基准", 1000, 1e7, 1e4),
    ("千万阅读/千篇/十万留言", 1000, 1e7, 1e5),
    ("亿级阅读/千篇/百万留言", 1000, 1e8, 1e6),
    ("十亿级/五千篇/千万留言", 5000, 1e9, 1e7),
]
for label, x, y, z in rows:
    e = attention_score(x, y, z, DEFAULT_PARAMS)
    print(f"  {label:<26} x={x:<6g} y={y:<8g} z={z:<8g} E={e:7.2f} 触发={deep_read_triggered(e)}")

print("\n=== D. 阈值扫描（默认参数，8 个阈值） ===")
th_list = (50, 55, 58, 60, 62, 64, 66, 70)
real_n, good_n = [], []
for th in th_list:
    r = sum(1 for _, x, y, z in TOPICS if attention_score(x, y, z, DEFAULT_PARAMS) > th)
    g = sum(1 for _, x, y, z in SCEN[5:] if attention_score(x, y, z, DEFAULT_PARAMS) > th)
    real_n.append(r)
    good_n.append(g)
print("  阈值:        " + " ".join(f"{t:>4}" for t in th_list))
print("  真实5个话题: " + " " * 1 + " ".join(f"{n:>4}" for n in real_n))
print("  假想6个情景: " + " " * 1 + " ".join(f"{n:>4}" for n in good_n))
print("  假想情景逐点 E: " + "  ".join(
    f"{lbl}={attention_score(x, y, z, DEFAULT_PARAMS):.2f}" for lbl, x, y, z in SCEN[5:]))

print("\n=== E. 候选参数 P4（y0=1e6, z0=300, w=(0.3,0.4,0.3), lambda=0.8） ===")
print(f"{'场景':<22}{'E':>8}{'触发':>6}{'y占比':>8}")
for label, x, y, z in SCEN:
    c = contributions(x, y, z, P4)
    e = attention_score(x, y, z, P4)
    print(f"{label:<22}{e:>8.2f}{str(deep_read_triggered(e)):>6}{c['term_y']/c['S']*100:>7.1f}%")
real_shares = [contributions(x, y, z, P4)["term_y"] / contributions(x, y, z, P4)["S"]
               for _, x, y, z in TOPICS]
print(f"  P4 在真实5点上的 y 占比区间: {min(real_shares)*100:.1f}%~{max(real_shares)*100:.1f}%")
def_shares = [contributions(x, y, z, DEFAULT_PARAMS)["term_y"] / contributions(x, y, z, DEFAULT_PARAMS)["S"]
              for _, x, y, z in TOPICS]
print(f"  默认在真实5点上的 y 占比区间: {min(def_shares)*100:.1f}%~{max(def_shares)*100:.1f}%")

print("\n=== F. 每数量级价格 H_v 与乘积预算 ===")
for cn, p in (("默认", DEFAULT_PARAMS), ("P4", P4)):
    hs = {k: math.log1p(p[k]) / p[w] for k, w in (("x0", "w_x"), ("y0", "w_y"), ("z0", "w_z"))}
    s66p = -math.log(1 - 0.66) / p["lambda"]
    hb = sum(hs.values()) / 3
    print(f"  {cn}: H_x={hs['x0']:.4f} H_y={hs['y0']:.4f} H_z={hs['z0']:.4f} "
          f"S66={s66p:.4f} 乘积预算={math.exp(s66p*hb):.4g} GM门槛={math.exp(s66p*hb/3):,.0f}")
hs = {k: math.log1p(DEFAULT_PARAMS[k]) / DEFAULT_PARAMS[w]
      for k, w in (("x0", "w_x"), ("y0", "w_y"), ("z0", "w_z"))}
print(f"  默认三项最大相对偏差: {(max(hs.values())-min(hs.values()))/min(hs.values())*100:.3f}%")
for name, x, y, z in TOPICS:
    gm = (x * y * z) ** (1 / 3)
    print(f"  GM({name}) = {gm:,.0f}")

print("\n=== G. y 临界值与稳健性 ===")
print(f"  x放大100倍: E={attention_score(30*100, 48316000, 149, DEFAULT_PARAMS):.2f}")
print(f"  z放大1e4倍: E={attention_score(30, 48316000, 149*1e4, DEFAULT_PARAMS):.2f}")
c0 = contributions(30, 1.0, 149, DEFAULT_PARAMS)
need = s66 - c0["term_x"] - c0["term_z"]
y_need = math.expm1(need * math.log1p(1e5) / 0.5)
print(f"  仅靠y过线（x=30,z=149）: y >= {y_need:.4g}（实测 {attention_score(30, y_need, 149, DEFAULT_PARAMS):.2f}）")
print(f"  HotValue 重取: y 2067344 -> 5680682, E {attention_score(3, 2067344, 129, DEFAULT_PARAMS):.4f}"
      f" -> {attention_score(3, 5680682, 129, DEFAULT_PARAMS):.4f}")
W_NOY = {"x0": 100.0, "y0": 100000.0, "z0": 1000.0, "w_x": 0.4, "w_y": 1e-9,
         "w_z": 0.6, "lambda": 0.7, "epsilon": 1e-6}
W_NOY = dict(W_NOY, w_z=1.0 - W_NOY["w_x"] - W_NOY["w_y"])
print("  去掉 y 项 (w=(0.4,~0,0.6)): "
      f"老干妈={attention_score(30, 48316000, 149, W_NOY):.2f} "
      f"花生地={attention_score(3, 2067344, 129, W_NOY):.2f} "
      f"男子={attention_score(6, 3766947, 1, W_NOY):.2f}")
print(f"  等比例放大到66所需倍数（二分）:")
for name, x, y, z in TOPICS:
    lo, hi = 1.0, 1e6
    for _ in range(200):
        mid = (lo * hi) ** 0.5
        if attention_score(x * mid, y * mid, z * mid, DEFAULT_PARAMS) < 66.0:
            lo = mid
        else:
            hi = mid
    print(f"    {name}: x{hi:.2f}")

print("\n=== H. 单变量敏感性（仅靠单项过线所需量级） ===")
for name, x, y, z in TOPICS:
    row = []
    for idx, val in ((0, x), (1, y), (2, z)):
        lo, hi = 1.0, 1e30
        for _ in range(300):
            mid = (lo * hi) ** 0.5
            pt = [x, y, z]
            pt[idx] = mid
            if attention_score(*pt, DEFAULT_PARAMS) < 66.0:
                lo = mid
            else:
                hi = mid
        row.append(f"{hi:.3g}")
    print(f"  {name}: 仅x {row[0]}  仅y {row[1]}  仅z {row[2]}")

print("\n=== I. 档位表 ===")
for b in DEFAULT_BANDS:
    print(f"  {b}")
