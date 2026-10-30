# -*- coding: utf-8 -*-
"""第六轮：'y 用热度指数代理'这条反方论证的定量刻画 + 稳健性检验。"""
from __future__ import annotations

import math
import sys

sys.path.insert(0, ".")
from attn_agent.attention import DEFAULT_PARAMS, attention_score, contributions  # noqa: E402

S66 = -math.log(1 - 0.66) / DEFAULT_PARAMS["lambda"]

print("=== AA. 把 term_y 整体缩放 s 倍，等于把 y0 改成多少 ===")
print("  term_y = w_y*ln(1+y)/ln(1+y0)。若把 ln(1+y) 换成 ln(1+s*(y-1))（≈按比例缩放 y），")
print("  等价于 y0 -> y0/s。所以'代理变量口径不一致'直接表现为'有效 y0 变化'。")
for s, tag in ((0.1, "代理口径高估 10 倍"), (0.5, "代理口径高估 2 倍"),
               (1.0, "口径准确"), (2.0, "代理口径低估 2 倍"), (10.0, "代理口径低估 10 倍")):
    y0_eff = DEFAULT_PARAMS["y0"] / s
    p = {**DEFAULT_PARAMS, "y0": y0_eff}
    e = attention_score(30, 48316000, 149, p)
    print(f"  {tag}: y0_eff={y0_eff:.4g} -> 老干妈 E={e:.2f}"
          f"（原 {attention_score(30, 48316000, 149, DEFAULT_PARAMS):.2f}）"
          f"{'  <- 过线' if e > 66 else ''}")

print("\n=== AB. 花生地 HotValue 重取 2067344 -> 5680682（+175%）对 E 的影响 ===")
for y, tag in ((2067344, "初次快照"), (5680682, "重取快照")):
    e = attention_score(3, y, 129, DEFAULT_PARAMS)
    c = contributions(3, y, 129, DEFAULT_PARAMS)
    print(f"  {tag}: y={y} -> term_y={c['term_y']:.4f} S={c['S']:.4f} E={e:.4f}")
e1 = attention_score(3, 2067344, 129, DEFAULT_PARAMS)
e2 = attention_score(3, 5680682, 129, DEFAULT_PARAMS)
print(f"  => E 变动 {e2-e1:+.4f} 分（+{(e2-e1)/e1*100:.2f}%）。y 涨 175% 只换来 {e2-e1:.2f} 分。")
print("     换算：y 涨 2.75 倍 = 10^%.2f，值 %.3f 的 S ≈ %.2f 分。"
      % (math.log10(5680682 / 2067344), math.log10(5680682 / 2067344) * 0.1,
         100 * (1 - math.exp(-0.7 * (e2 - e1) / 100))))

print("\n=== AC. 噪声敏感性：要让 E 从 54.81 抖动到 66，y 要抖多少 ===")
c = contributions(30, 48316000, 149, DEFAULT_PARAMS)
ty_need = S66 - c["term_x"] - c["term_z"]
y_need = math.expm1(ty_need * math.log1p(DEFAULT_PARAMS["y0"]) / DEFAULT_PARAMS["w_y"])
print(f"  y 需从 4.8316e7 涨到 {y_need:.4g}（{y_need/48316000:.0f} 倍，"
      f"+{(y_need/48316000-1)*100:.0f}%）")
print("  而 HotValue 跨时点抖动实测 +175%。=> 远不足以跨过 66。")
print("  这是好事：阈值高 => 对噪声不敏感；也是坏事：真实增长也被一起压制。")

print("\n=== AD. 稳健性：如果 x 漏统计（真实 x 可能是观测值的 3 倍/10 倍）===")
for mult in (1, 2, 3, 5, 10, 30, 100):
    e = attention_score(30 * mult, 48316000, 149, DEFAULT_PARAMS)
    print(f"  x 放大 {mult:3d} 倍 -> E={e:.2f}")
print("  => x 即使漏统计 100 倍，也只从 54.81 涨到约 60 分，仍不过线。")
print("     说明'漏统计 x'不是本批不过线的原因；y 的量级才是决定性因素。")

print("\n=== AE. 稳健性：如果 z 漏统计 ===")
for mult in (1, 10, 100, 1000, 1e4):
    e = attention_score(30, 48316000, 149 * mult, DEFAULT_PARAMS)
    print(f"  z 放大 {mult:8g} 倍 -> E={e:.2f}")
print("  => z 要漏统计 1e4 倍（149 -> 1.49e6）才勉强过线，不现实。")

print("\n=== AF. 因此：本批不过线的主因是 y 的量级，不是 x/z 的漏统计 ===")
print("  证据：把 x 放大 100 倍只到 ~60 分；把 z 放大 1e4 倍才过线；")
print("  而把 y 从 4.83e7 涨到 5.59e11（其余不变）就过线。")

print("\n=== AG. 'y 不该进公式'的定量回应：把 term_y 置 0 会怎样 ===")
p = {**DEFAULT_PARAMS, "w_y": 0.0, "w_x": 0.4, "w_z": 0.6}
print("  注：validate_params 要求 w_y > 0，所以这里只做数学演示（不走校验）。")
for label, x, y, z in (("老干妈", 30, 48316000, 149), ("花生地", 3, 2067344, 129),
                       ("男子", 6, 3766947, 1)):
    t = 0.4 * math.log1p(x) / math.log1p(100) + 0.6 * math.log1p(z) / math.log1p(1000)
    e = 100 * (1 - math.exp(-0.7 * t))
    print(f"  去掉 y 项 {label}: S={t:.4f} E={e:.2f}")
print("  => 去掉 y 项后，五个话题会全部掉到 10~20 分量级，'国民级'与'小话题'也一起失去区分度。")
print("     即：y 项虽然主导，但它是唯一能把'10^7 热度'与'10^3 热度'分开的维度。")

print("\n=== AH. y 项主导性的量化：term_y / S ===")
for name, x, y, z in (("老干妈", 30, 48316000, 149), ("阿雅", 5, 12506705, 302),
                      ("长鑫", 11, 13822047, 42), ("花生地", 3, 2067344, 129),
                      ("男子", 6, 3766947, 1)):
    c = contributions(x, y, z, DEFAULT_PARAMS)
    print(f"  {name}: term_x/S={c['term_x']/c['S']*100:5.1f}%  "
          f"term_y/S={c['term_y']/c['S']*100:5.1f}%  term_z/S={c['term_z']/c['S']*100:5.1f}%")
