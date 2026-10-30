# -*- coding: utf-8 -*-
"""独立审视用的复算脚本：只验证四份分析里可被公式/算术检验的数字。"""
import math

P = dict(x0=100.0, y0=100000.0, z0=1000.0, w_x=0.2, w_y=0.5, w_z=0.3,
         **{"lambda": 0.7}, epsilon=1e-6)


def terms(x, y, z, p=P):
    tx = p["w_x"] * math.log1p(x) / math.log1p(p["x0"])
    ty = p["w_y"] * math.log1p(y) / math.log1p(p["y0"])
    tz = p["w_z"] * math.log1p(z) / math.log1p(p["z0"])
    return tx, ty, tz


def E(x, y, z, p=P):
    tx, ty, tz = terms(x, y, z, p)
    S = tx + ty + tz + p["epsilon"]
    return 100.0 * (1 - math.exp(-p["lambda"] * S)), S, tx, ty, tz

print("=== 1. 五个话题复算 ===")
TOPICS = [
    ("老干妈", 30, 48316000, 149),
    ("阿雅", 5, 12506705, 302),
    ("长鑫", 11, 13822047, 42),
    ("花生地", 3, 2067344, 129),
    ("男子", 6, 3766947, 1),
]
S66 = -math.log(1 - 0.66) / 0.7
print("S66 = %.6f ; E(S66) = %.6f" % (S66, 100 * (1 - math.exp(-0.7 * S66))))
for n, x, y, z in TOPICS:
    e, S, tx, ty, tz = E(x, y, z)
    print("%-6s x=%-4d y=%-10d z=%-4d S=%.6f E=%.4f  tx/S=%.1f%% ty/S=%.1f%% tz/S=%.1f%%"
          % (n, x, y, z, S, e, 100 * tx / S, 100 * ty / S, 100 * tz / S))

print()
print("=== 2. 每数量级价格 H_v = ln(1+v0)/w ===")
Hs = {}
for k, w in (("x", 0.2), ("y", 0.5), ("z", 0.3)):
    v0 = {"x": 100, "y": 100000, "z": 1000}[k]
    Hs[k] = math.log1p(v0) / w
    print("H_%s = %.4f" % (k, Hs[k]))
mean = sum(Hs.values()) / 3
print("mean = %.4f ; 相对均值最大偏差 = %.4f%%" % (mean, 100 * max(abs(v - mean) for v in Hs.values()) / mean))
print("两两最大相对偏差 = %.4f%%" % (100 * (max(Hs.values()) - min(Hs.values())) / min(Hs.values())))
print("乘积预算 exp(S66*mean) = %.4e ; GM门槛 = %.1f" % (math.exp(S66 * mean), math.exp(S66 * mean) ** (1 / 3)))
for n, x, y, z in TOPICS:
    gm = ((1 + x) * (1 + y) * (1 + z)) ** (1 / 3)
    approx = (math.log1p(x) + math.log1p(y) + math.log1p(z)) / mean
    e, S, *_ = E(x, y, z)
    print("%-6s GM=%-8d 门槛占比=%.2f%%  近似S=%.6f 精确S=%.6f 误差=%.6f"
          % (n, gm, 100 * gm / math.exp(S66 * mean) ** (1 / 3), approx, S, abs(approx - S)))

print()
print("=== 3. 背景情景表复算 ===")
SC = [
    ("全项10倍基准", 1000, 1e7, 1e4, 59.73),
    ("千万阅读/千篇/十万留言", 1000, 1e7, 1e5, 64.99),
    ("亿级阅读/千篇/百万留言", 1000, 1e8, 1e6, 71.62),
    ("十亿级/五千篇/千万留言", 5000, 1e9, 1e7, 76.50),
]
for n, x, y, z, claim in SC:
    e, S, *_ = E(x, y, z)
    print("%-24s x=%-6g y=%-8g z=%-8g 复算E=%.4f  背景称=%.2f  %s"
          % (n, x, y, z, e, claim, "一致" if abs(e - claim) < 0.02 else "不一致"))
print("E(1000,1e9,1e6)=%.4f  E(5000,1e10,1e7)=%.4f" % (E(1000, 1e9, 1e6)[0], E(5000, 1e10, 1e7)[0]))
print("E(1000,1e8,1e6)=%.4f  <- 这就是『亿级阅读/千篇/百万留言』" % E(1000, 1e8, 1e6)[0])

print()
print("=== 4. 单变量敏感性（老干妈 30/4.8316e7/149）===")
for k, need in (("x", None), ("y", None), ("z", None)):
    lo, hi = 0.0, 1e18
    for _ in range(300):
        mid = math.sqrt(lo * hi) if lo > 0 else 1.0
        if E(30, 48316000, 149)[0] >= 66 and k == "x":
            pass
        lo, hi = lo, hi
    break


def solve_x(y, z, target=66.0):
    lo, hi = 1e-9, 1e18
    for _ in range(400):
        mid = math.sqrt(lo * hi)
        if E(mid, y, z)[0] < target:
            lo = mid
        else:
            hi = mid
    return hi


def solve_y(x, z, target=66.0):
    lo, hi = 1e-9, 1e20
    for _ in range(400):
        mid = math.sqrt(lo * hi)
        if E(x, mid, z)[0] < target:
            lo = mid
        else:
            hi = mid
    return hi


def solve_z(x, y, target=66.0):
    lo, hi = 1e-9, 1e18
    for _ in range(400):
        mid = math.sqrt(lo * hi)
        if E(x, y, mid)[0] < target:
            lo = mid
        else:
            hi = mid
    return hi


for n, x, y, z in TOPICS:
    print("%-6s 仅x需%.4e  仅y需%.4e  仅z需%.4e" % (n, solve_x(y, z), solve_y(x, z), solve_z(x, y)))

print()
print("=== 5. 等比例放大到 66 的倍数 ===")
for n, x, y, z in TOPICS:
    lo, hi = 1.0, 1e6
    for _ in range(300):
        mid = math.sqrt(lo * hi)
        if E(x * mid, y * mid, z * mid)[0] < 66.0:
            lo = mid
        else:
            hi = mid
    print("%-6s 需 x%.2f  (x=%g y=%g z=%g) -> E=%.4f"
          % (n, hi, x * hi, y * hi, z * hi, E(x * hi, y * hi, z * hi)[0]))

print()
print("=== 6. y 临界值表 ===")
for x, z, claim in ((30, 149, 5.593e11), (100, 1000, 2.58e10), (1000, 10000, 2.619e8),
                    (3000, 100000, 8.76e6), (5000, 1e6, 5.264e5)):
    got = solve_y(x, z)
    print("(x=%g,z=%g) 复算y*=%.4e  文中称%.4e  %s" % (x, z, got, claim, "一致" if abs(got - claim) / claim < 0.01 else "不一致"))
print("y=1e7 时过线所需 x/z：")
print("  x=1e5 -> z* = %.0f (文中称>=2647)" % solve_z(1e5, 1e7))
print("  z=1e5 -> x* = %.0f (文中称>=100000)" % solve_x(1e7, 1e5))
print("  x=30  -> z* = %.4e (文中称8.4e6)" % solve_z(30, 1e7))

print()
print("=== 7. 稳健性 / 口径敏感性 ===")
e0 = E(30, 48316000, 149)[0]
print("老干妈基线 E=%.4f" % e0)
print("  x*100  -> E=%.4f (文中 60.67)" % E(3000, 48316000, 149)[0])
print("  z*1e4  -> E=%.4f (文中 65.84)" % E(30, 48316000, 1490000)[0])
print("  y*2.75 -> E=%.4f  (Delta=%.2f, 文中称约+1.6)" % (E(30, 48316000 * 2.75, 149)[0], E(30, 48316000 * 2.75, 149)[0] - e0))
print("  花生地 HotValue 2067344 -> 5680682: E %.4f -> %.4f (Delta=%.2f)"
      % (E(3, 2067344, 129)[0], E(3, 5680682, 129)[0], E(3, 5680682, 129)[0] - E(3, 2067344, 129)[0]))
print("  去掉 y 项 (w_y=0,w_x=0.4,w_z=0.6)：老干妈 %.2f 花生地 %.2f 男子 %.2f"
      % (E(30, 48316000, 149, dict(P, w_y=0.0, w_x=0.4, w_z=0.6))[0],
         E(3, 2067344, 129, dict(P, w_y=0.0, w_x=0.4, w_z=0.6))[0],
         E(6, 3766947, 1, dict(P, w_y=0.0, w_x=0.4, w_z=0.6))[0]))
print("  让老干妈到 66 需 y*%.0f 倍" % (solve_y(30, 149) / 48316000))

print()
print("=== 8. term_y 无上界 / 凹性 ===")
for y in (1e3, 1e5, 1e7, 1e9, 1e11, 1e12):
    print("  y=%-8g term_y=%.4f" % (y, 0.5 * math.log1p(y) / math.log1p(1e5)))
print("  dE/dS @ S=1.1348 = %.2f" % (100 * 0.7 * math.exp(-0.7 * 1.134802)))
print("  E(1000,1e7,1e4)=%.4f  x10=%.4f  x100=%.4f  x1000=%.4f"
      % (E(1000, 1e7, 1e4)[0], E(1e4, 1e8, 1e5)[0], E(1e5, 1e9, 1e6)[0], E(1e6, 1e10, 1e7)[0]))
print("  E=99 需 S=%.4f ; S66=%.4f ; 高关注带宽度=%.2f" % (-math.log(0.01) / 0.7, S66, -math.log(0.01) / 0.7 - S66))
print("  E(100,1e5,1e3)=%.4f (模块定标点 50.3415)" % E(100, 100000, 1000)[0])

print()
print("=== 9. 档位 vs 深读口径 ===")
import sys
sys.path.insert(0, r"D:\MyLocalAI")
from attn_agent.attention import classify, deep_read_triggered, attention_score
print("classify(66.0) =", classify(66.0), "| deep_read_triggered(66.0) =", deep_read_triggered(66.0))

print()
print("=== 10. 候选参数复算 ===")
P1 = dict(P, **{"lambda": 0.8})
P2 = dict(P, y0=1e6, z0=300.0, w_x=0.3, w_y=0.4, w_z=0.3, **{"lambda": 0.8})
P3 = dict(P, y0=1e6, w_x=0.25, w_y=0.40, w_z=0.35, **{"lambda": 1.0})
for label, p in (("候选一 lambda=0.8", P1), ("候选二 y0=1e6,z0=300,w=.3/.4/.3,l=.8", P2),
                 ("候选三 y0=1e6,w=.25/.40/.35,l=1.0", P3)):
    print("-- %s" % label)
    for n, x, y, z in TOPICS:
        e, S, tx, ty, tz = E(x, y, z, p)
        print("   %-6s E=%.4f  ty/S=%.1f%%" % (n, e, 100 * ty / S))
    for n, x, y, z in (("M1", 100, 1e6, 1000), ("M2", 1000, 1e7, 1e4), ("M3", 500, 3e7, 2e4),
                       ("N1", 2000, 1e9, 1e5), ("N2", 5000, 1e9, 1e7), ("N3", 1000, 1e8, 1e6)):
        e, S, tx, ty, tz = E(x, y, z, p)
        print("   %-3s E=%.4f" % (n, e))
    print("   term_y/S 区间: %.1f%% ~ %.1f%%" % (
        100 * min(E(x, y, z, p)[3] / E(x, y, z, p)[1] for _, x, y, z in TOPICS),
        100 * max(E(x, y, z, p)[3] / E(x, y, z, p)[1] for _, x, y, z in TOPICS)))

print()
print("=== 11. 老干妈分项与 decade 计数（分析2证据8/9） ===")
tx, ty, tz = terms(30, 48316000, 149)
print("tx=%.5f ty=%.5f tz=%.5f S=%.6f" % (tx, ty, tz, tx + ty + tz + 1e-6))
print("decades: log10(31)=%.4f log10(48316001)=%.4f log10(150)=%.4f 合计=%.4f"
      % (math.log10(31), math.log10(48316001), math.log10(150),
         math.log10(31) + math.log10(48316001) + math.log10(150)))
print("相对 x0/z0 的数量级缺口: x %.4f  z %.4f" % (math.log10(100 / 30), math.log10(1000 / 149)))
print("相对『亿/千/百万』情景缺口: y %.4f x %.4f z %.4f"
      % (math.log10(1e9 / 48316000), math.log10(1000 / 30), math.log10(1e6 / 149)))
print("x*23 等比例: E=%.4f" % E(690, 48316000 * 23, 149 * 23)[0])
print("阿雅 E 复算: %.4f" % E(5, 12506705, 302)[0])
