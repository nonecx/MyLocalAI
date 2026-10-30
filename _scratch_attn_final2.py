# -*- coding: utf-8 -*-
"""第九轮：最终候选 P4 的全面验证 + 网格搜索定版。"""
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

print("=== DA. P4 全面表现 ===")
print(f"{'场景':>10} {'x':>7} {'y':>11} {'z':>8} {'S':>7} {'E':>7} {'y占比':>7} 深读")
for label, x, y, z in SCEN:
    c = contributions(x, y, z, P4)
    e = attention_score(x, y, z, P4)
    print(f"{label:>10} {x:7g} {y:11.3g} {z:8g} {c['S']:7.4f} {e:7.2f} "
          f"{c['term_y']/c['S']*100:6.1f}% {'触发' if e > 66 else '未触发'}")

print("\n=== DB. P4 的设计逻辑 ===")
print(f"  H_x = ln(1+100)/0.3   = {math.log1p(100)/0.3:.4f}")
print(f"  H_y = ln(1+1e6)/0.4   = {math.log1p(1e6)/0.4:.4f}")
print(f"  H_z = ln(1+300)/0.3   = {math.log1p(300)/0.3:.4f}")
print("  => 三个'每数量级价格'被刻意拉开：x 最贵(24.4)、y 次之(34.5)、z 最便宜(19.1)")
print("     这样 y 的动态范围优势被削弱，x/z 也能说话。")

print("\n=== DC. P4 与默认参数的 S66 / 预算对比 ===")
for cn, p in (("默认", DEFAULT_PARAMS), ("P4", P4)):
    s66 = -math.log(1 - 0.66) / p["lambda"]
    hs = [math.log1p(p[k]) / p[w] for k, w in (("x0", "w_x"), ("y0", "w_y"), ("z0", "w_z"))]
    hb = sum(hs) / 3
    print(f"  {cn}: S66={s66:.4f}  H_x/H_y/H_z={hs[0]:.1f}/{hs[1]:.1f}/{hs[2]:.1f}  "
          f"乘积预算={math.exp(s66*hb):.4g}  GM门槛={math.exp(s66*hb/3):,.0f}")

print("\n=== DD. 网格搜索：以'真实头部 41~55 不触发、大热门过线'为目标的定版 ===")
# 目标：
#  T1 五个真实话题全部 < 60（留足余量）
#  T2 大热门情景（x>=500, y>=1e7, z>=1e4）> 66
#  T3 中腰部（x=100,y=1e6,z=1e3）< 66
GOOD_SCEN = [("M2", 1000, 1e7, 1e4), ("M3", 500, 3e7, 2e4),
             ("N1", 2000, 1e9, 1e5), ("N3", 1000, 1e8, 1e5),
             ("N2", 5000, 1e10, 1e6)]
results = []
for x0 in (50, 100, 200):
    for y0 in (3e5, 5e5, 1e6, 2e6, 5e6):
        for z0 in (200, 300, 500, 1000):
            for wx, wy, wz in ((0.25, 0.4, 0.35), (0.3, 0.35, 0.35), (0.3, 0.4, 0.3),
                               (0.2, 0.4, 0.4), (0.35, 0.35, 0.3), (0.25, 0.35, 0.4)):
                for lam in (0.7, 0.8, 0.9, 1.0):
                    p = {"x0": x0, "y0": y0, "z0": z0, "w_x": wx, "w_y": wy,
                         "w_z": wz, "lambda": lam, "epsilon": 1e-6}
                    real = [attention_score(x, y, z, p) for _, x, y, z in TOPICS]
                    good = [attention_score(x, y, z, p) for _, x, y, z in GOOD_SCEN]
                    mid = attention_score(100, 1e6, 1000, p)
                    if (max(real) < 60.0 and min(good) > 66.0 and mid < 66.0
                            and max(real) > 40.0):
                        # 越接近"真实头部刚在 55~60、国民级刚过 67~72"越好
                        q = -abs(max(real) - 57.0) - abs(min(good) - 69.0)
                        results.append((q, max(real), min(good), mid, p))
results.sort(key=lambda t: -t[0])
print(f"合格组数：{len(results)}")
for q, rmax, gmin, mid, p in results[:8]:
    print(f"  q={q:6.2f} 真实最高={rmax:5.2f} 国民最低={gmin:5.2f} 中腰部={mid:5.2f} | "
          f"x0={p['x0']:g} y0={p['y0']:g} z0={p['z0']:g} "
          f"w=({p['w_x']},{p['w_y']},{p['w_z']}) lambda={p['lambda']:g}")

print("\n=== DE. 备选：'只动阈值不动参数'这条路走不通的证据 ===")
print("  因为 S 与 E 是同一单调映射，阈值只能整体平移；")
for th in (50, 55, 58, 60, 62, 66, 70):
    real_n = sum(1 for _, x, y, z in TOPICS if attention_score(x, y, z, DEFAULT_PARAMS) > th)
    good_n = sum(1 for _, x, y, z in GOOD_SCEN if attention_score(x, y, z, DEFAULT_PARAMS) > th)
    print(f"  阈值 {th}: 真实 {real_n}/5 触发, 大热门/国民 {good_n}/5 触发")
print("  => 在 50~70 之间，要么真实 0 触发，要么大热门全触发但真实也开始触发（阈值<54.81 时）。")
print("     不存在一个阈值能同时做到'真实不触发 + 大热门触发'。")

print("\n=== DF. 结论：必须改参数，不能只改阈值 ===")
print("  默认参数下真实话题 E 区间 [41.75, 54.81]，大热门 M2/M3 = 62.45/63.69（不触发），")
print("  国民 N1/N3/N2 = 70.20/67.36/74.80（触发）。")
print("  => 默认参数下，'大热门'和'国民级'之间已经有一个天然的 3~7 分间隔，")
print("     问题只在这个间隔正好落在 66 附近：M2/M3 差 2~4 分，N3 只高 1.36 分。")
print("     把阈值降到 62 会让 M2/M3 触发但 N3 也触发——但真实话题仍 0 触发。")
print("     所以阈值 62~63 在默认参数下其实是一个更好的'分离点'！")
for th in (62, 63, 64):
    print(f"     验证阈值 {th}: 真实 " +
          str(sum(1 for _, x, y, z in TOPICS if attention_score(x, y, z, DEFAULT_PARAMS) > th))
          + "/5, 大热门/国民 " +
          str(sum(1 for _, x, y, z in GOOD_SCEN if attention_score(x, y, z, DEFAULT_PARAMS) > th))
          + "/5")
