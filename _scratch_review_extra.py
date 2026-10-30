# -*- coding: utf-8 -*-
"""补充复算：分析1 anxiety/caveats 里的具体数字。"""
import math

P = dict(x0=100.0, y0=100000.0, z0=1000.0, w_x=0.2, w_y=0.5, w_z=0.3,
         **{"lambda": 0.7}, epsilon=1e-6)


def parts(x, y, z, p=P):
    tx = p["w_x"] * math.log1p(x) / math.log1p(p["x0"])
    ty = p["w_y"] * math.log1p(y) / math.log1p(p["y0"])
    tz = p["w_z"] * math.log1p(z) / math.log1p(p["z0"])
    S = tx + ty + tz + p["epsilon"]
    return tx, ty, tz, S, 100.0 * (1 - math.exp(-p["lambda"] * S))


print("=== 分析1 anxiety 里的可达性数字 ===")
for lbl, x, y, z in (("x=1000,y=1e8,z=1e5", 1000, 1e8, 1e5),
                     ("x=2000,y=1e9,z=1e5", 2000, 1e9, 1e5)):
    _, _, _, S, e = parts(x, y, z)
    print("  %-22s E=%.4f" % (lbl, e))

print()
print("=== 阈值降到 40 是否 5/5 触发 ===")
REAL = [(30, 48316000, 149), (5, 12506705, 302), (11, 13822047, 42),
        (3, 2067344, 129), (6, 3766947, 1)]
for t in (40, 41, 41.5, 41.75, 46.85):
    print("  t=%-6g 触发=%d/5" % (t, sum(1 for x, y, z in REAL if parts(x, y, z)[4] > t)))

print()
print("=== 候选参数 term_y/S 区间（真实5点 vs 真实+假想11点） ===")
P2 = dict(P, y0=1e6, z0=300.0, w_x=0.3, w_y=0.4, w_z=0.3, **{"lambda": 0.8})
P3 = dict(P, y0=1e6, w_x=0.25, w_y=0.40, w_z=0.35, **{"lambda": 1.0})
HOT = [(100, 1e6, 1000), (1000, 1e7, 1e4), (500, 3e7, 2e4),
       (2000, 1e9, 1e5), (5000, 1e9, 1e7), (1000, 1e8, 1e5)]
for lbl, p, claim in (("候选二", P2, "33.3%~72.9%"), ("候选三", P3, "35.7%~75.7%")):
    for scope, pts in (("真实5点", REAL), ("真实+假想11点", REAL + HOT)):
        vals = [100 * parts(x, y, z, p)[1] / parts(x, y, z, p)[3] for x, y, z in pts]
        print("  %s %-14s term_y/S = %.1f%% ~ %.1f%%   (文中称 %s)"
              % (lbl, scope, min(vals), max(vals), claim))
    print("     各点: " + " ".join("%.1f" % (100 * parts(x, y, z, p)[1] / parts(x, y, z, p)[3])
                              for x, y, z in REAL + HOT))

print()
print("=== 默认参数 term_y/S（真实5点，核对 67.7~85.2） ===")
vals = [100 * parts(x, y, z)[1] / parts(x, y, z)[3] for x, y, z in REAL]
print("  %.1f%% ~ %.1f%%" % (min(vals), max(vals)))

print()
print("=== 候选二的 H 值（每数量级价格） ===")
for k, v0, w in (("x", 100, 0.3), ("y", 1e6, 0.4), ("z", 300, 0.3)):
    print("  H_%s = ln(1+%g)/%g = %.2f" % (k, v0, w, math.log1p(v0) / w))

print()
print("=== 分析1 反方论证二(e) 的检验：y=1e12 时 term_y=1.2 ===")
print("  term_y(1e12) = %.4f ; w_y = %.2f" % (0.5 * math.log1p(1e12) / math.log1p(1e5), 0.5))
print("  注: term_y 无上界为真，但分析称『y 的绝对量级可以直接把 x、z 的贡献压成零』")
print("  检验: 老干妈 term_x+term_z = %.4f, term_y = %.4f" %
      (parts(30, 48316000, 149)[0] + parts(30, 48316000, 149)[2], parts(30, 48316000, 149)[1]))
