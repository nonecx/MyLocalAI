# -*- coding: utf-8 -*-
"""第四轮：定量刻画'66 距离本批头部有多远'，以及 lambda 的可行区间。"""
from __future__ import annotations

import math
import sys

sys.path.insert(0, ".")
from attn_agent.attention import DEFAULT_PARAMS, attention_score, contributions  # noqa: E402

TOPICS = [
    ("老干妈", 30, 48316000, 149),
    ("阿雅", 5, 12506705, 302),
    ("长鑫", 11, 13822047, 42),
    ("花生地", 3, 2067344, 129),
    ("男子", 6, 3766947, 1),
]
S66 = -math.log(1 - 0.66) / DEFAULT_PARAMS["lambda"]

print("=== U. 要把每个话题'等比例放大'多少倍才刚好到 66 ===")
for name, x, y, z in TOPICS:
    c = contributions(x, y, z, DEFAULT_PARAMS)
    # S(kx, ky, kz) = S + ln(k)*[ (w_x/ln(1+x0)) + (w_y/ln(1+y0)) + (w_z/ln(1+z0)) ]
    # (因 log1p(k*v) ≈ ln k + ln(1+v)，v 远大于 1 时近似成立；精确用数值解)
    coef = (DEFAULT_PARAMS["w_x"] / math.log1p(DEFAULT_PARAMS["x0"])
            + DEFAULT_PARAMS["w_y"] / math.log1p(DEFAULT_PARAMS["y0"])
            + DEFAULT_PARAMS["w_z"] / math.log1p(DEFAULT_PARAMS["z0"]))
    k_approx = math.exp((S66 - c["S"]) / coef)
    # 精确：二分
    lo, hi = 1.0, 1e30
    for _ in range(300):
        mid = math.sqrt(lo * hi)
        if attention_score(x * mid, y * mid, z * mid, DEFAULT_PARAMS) < 66.0:
            lo = mid
        else:
            hi = mid
    k_exact = math.sqrt(lo * hi)
    # 验证
    e_check = attention_score(x * k_exact, y * k_exact, z * k_exact, DEFAULT_PARAMS)
    print(f"  {name}: 需全项 x{k_exact:,.0f}（≈10^{math.log10(k_exact):.2f}）"
          f" 校验E={e_check:.4f}")

print("\n=== V. 老干妈的 E=54.81 与 66 之间差多少，换算成'数量级' ===")
c = contributions(30, 48316000, 149, DEFAULT_PARAMS)
coef = (DEFAULT_PARAMS["w_x"] / math.log1p(DEFAULT_PARAMS["x0"])
        + DEFAULT_PARAMS["w_y"] / math.log1p(DEFAULT_PARAMS["y0"])
        + DEFAULT_PARAMS["w_z"] / math.log1p(DEFAULT_PARAMS["z0"]))
decs = (S66 - c["S"]) / coef
print(f"  S 缺口 = {S66 - c['S']:.4f}；每'全项同乘 10 倍' S +{coef*math.log(10):.6f}")
print(f"  => 全项同乘 10^{decs:.2f}（≈{math.exp(decs*math.log(10)):.0f} 倍）才到 66")

print("\n=== W. 只调 lambda 能否'把 5 个话题压到 66 以下、同时让国民级上去' ===")
# lambda 越大，同一 S 的 E 越高（越早触发）。所以调 lambda 无法同时压低真实话题和抬高国民级：
# 二者在 S 上是同一单调映射，lambda 只改刻度不改排序。
for lam in (0.3, 0.5, 0.7, 0.9, 1.2, 2.0, 4.0):
    p = {**DEFAULT_PARAMS, "lambda": lam}
    es = [attention_score(x, y, z, p) for _, x, y, z in TOPICS]
    nat = attention_score(2000, 1e9, 1e5, p)
    print(f"  lambda={lam:4.1f}: 真实话题 E 范围 [{min(es):5.2f}, {max(es):5.2f}]  "
          f"国民N1 E={nat:6.2f}  (真实最高 - 国民) = {max(es)-nat:+6.2f}")
print("  => lambda 只平移刻度，真实话题与国民级的相对差恒为负且几乎不变；"
      "调 lambda 不可能在不把国民级也推过线的情况下单独压低真实话题。")

print("\n=== X. 反过来：要触发必须的 y（在真实话题的 x,z 下）与'十亿'的关系 ===")
for name, x, y, z in TOPICS:
    c = contributions(x, 1.0, z, DEFAULT_PARAMS)
    ty_need = S66 - c["term_x"] - c["term_z"]
    y_need = math.expm1(ty_need * math.log1p(DEFAULT_PARAMS["y0"]) / DEFAULT_PARAMS["w_y"])
    print(f"  {name}: x={x},z={z} -> y >= {y_need:.4g}"
          f"（log10 = {math.log10(y_need):.2f}）  当前 y={y:.4g}"
          f"（log10 = {math.log10(y):.2f}）  缺口 {math.log10(y_need)-math.log10(y):.2f} 个数量级")

print("\n=== Y. '深读'在全触发/全不触发下的成本推演（仅结构，不引入外部数字） ===")
print("  现状：5/5 不触发 -> 深读执行次数 0。系统对该批话题只输出 E/档位，无深读内容。")
print("  阈值降到 40：5/5 触发 -> 深读执行 5 次，其中最低分话题 E=41.75（x=6,y=3.77e6,z=1），")
print("    其 z=1（留言量 1 条），x=6（6 篇文章）——即'几乎没有讨论'的话题也被深读。")
print("  阈值 60 / 70：0/5 触发，与 66 无差别。=> 在当前样本上，60~70 是同一个'空档'。")
