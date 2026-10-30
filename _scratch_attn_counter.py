# -*- coding: utf-8 -*-
"""第三轮：反方论证的结构性证据 + 阈值两种失效模式的仿真。"""
from __future__ import annotations

import math
import sys

sys.path.insert(0, ".")
from attn_agent.attention import (  # noqa: E402
    DEFAULT_PARAMS, attention_score, contributions, deep_read_triggered,
)

TOPICS = [
    ("老干妈", 30, 48316000, 149),
    ("阿雅", 5, 12506705, 302),
    ("长鑫", 11, 13822047, 42),
    ("花生地", 3, 2067344, 129),
    ("男子", 6, 3766947, 1),
]

print("=== N. 锚点隐含的互动率 vs 真实互动率 ===")
print(f"锚点隐含：y0/z0 = {DEFAULT_PARAMS['y0']/DEFAULT_PARAMS['z0']:.0f} 阅读/留言"
      f"（= {DEFAULT_PARAMS['z0']/DEFAULT_PARAMS['y0']*100:.1f}% 留言率）")
for name, x, y, z in TOPICS:
    ratio = (y / z) if z else float("inf")
    print(f"  {name}: y/z = {ratio:,.0f} 阅读/留言 -> 留言率 {z/y*100:.6f}%"
          f"  （锚点的 {ratio/100:.0f} 倍稀释）")

print("\n=== O. 若留言率回到锚点水平（z = y*1%），这些话题会是多少分 ===")
for name, x, y, z in TOPICS:
    z_fixed = y * 0.01
    e = attention_score(x, y, z_fixed, DEFAULT_PARAMS)
    c = contributions(x, y, z_fixed, DEFAULT_PARAMS)
    print(f"  {name}: z 从 {z} -> {z_fixed:,.0f}（y*1%）: S={c['S']:.4f} E={e:.2f} "
          f"触发={deep_read_triggered(e)}")

print("\n=== P. '全触发'与'全不触发'两种失效模式的仿真 ===")
print("--- P1 全不触发（现状，阈值 66）---")
for name, x, y, z in TOPICS:
    e = attention_score(x, y, z, DEFAULT_PARAMS)
    print(f"  {name}: E={e:.2f} 深读={deep_read_triggered(e)}")
print("  => 5/5 不触发。深读链路在本批数据上零执行。")

print("\n--- P2 全触发（把阈值降到 40）---")
for name, x, y, z in TOPICS:
    e = attention_score(x, y, z, DEFAULT_PARAMS)
    print(f"  {name}: E={e:.2f} 深读(>40)={e > 40.0}")
print("  => 5/5 触发，且连 E=41.75 的'男子出狱砍死妻子'这种 1 条留言的话题也触发。")

print("\n--- P3 阈值 60 的效果（不动参数）---")
n = 0
for name, x, y, z in TOPICS:
    e = attention_score(x, y, z, DEFAULT_PARAMS)
    n += e > 60.0
    print(f"  {name}: E={e:.2f} 深读(>60)={e > 60.0}")
print(f"  => {n}/5 触发。")

print("\n--- P4 阈值 70 的效果（不动参数）---")
n = 0
for name, x, y, z in TOPICS:
    e = attention_score(x, y, z, DEFAULT_PARAMS)
    n += e > 70.0
    print(f"  {name}: E={e:.2f} 深读(>70)={e > 70.0}")
print(f"  => {n}/5 触发。")

print("\n=== Q. 66 阈值把 S 空间的哪一段划走（lambda=0.7） ===")
for s in (0.5, 0.77, 0.9, 1.0, 1.14, 1.35, 1.54, 2.0, 3.0, 5.0):
    e = 100 * (1 - math.exp(-0.7 * s))
    print(f"  S={s:5.2f} -> E={e:6.2f}  {'高关注' if e > 66 else ('中关注' if e > 50 else ('低关注' if e > 33 else '冷启动'))}")

print("\n=== R. 一个'数量级'能买多少分：反方用来论证阈值过高的表 ===")
base = attention_score(1000, 1e7, 1e4, DEFAULT_PARAMS)
print(f"基线 x=1e3,y=1e7,z=1e4: E={base:.2f}")
for mult, lab in ((10, "10x"), (100, "100x"), (1000, "1000x")):
    e = attention_score(1000 * mult, 1e7 * mult, 1e4 * mult, DEFAULT_PARAMS)
    print(f"  全项 x{mult:<5g} ({lab}): E={e:.2f}（+{e-base:.2f} 分）")

print("\n=== S. 幂律检查：如果 x 是长尾，头部有多稀有 ===")
for x in (3, 11, 30, 100, 1000, 10000, 1e5, 1e6):
    e = attention_score(x, 1e7, 1e4, DEFAULT_PARAMS)
    print(f"  x={x:<8g} (y=1e7,z=1e4): E={e:.2f}")

print("\n=== T. 关键阈值点：y 单独要多高才能过 66（x,z 给到'很努力'的水平）===")
for (xx, zz, tag) in ((30, 149, "老干妈原样"), (100, 1000, "锚点"),
                      (1000, 10000, "中腰部"), (5000, 1000000, "努力")):
    c = contributions(xx, 1.0, zz, DEFAULT_PARAMS)
    ty_need = -math.log(1 - 0.66) / 0.7 - c["term_x"] - c["term_z"]
    y_need = math.expm1(ty_need * math.log1p(100000) / 0.5)
    print(f"  x={xx},z={zz} ({tag}): y 需 >= {y_need:.4g}（= 现有 y 的 "
          f"{y_need/48316000:.2f} 倍于老干妈量级）")
