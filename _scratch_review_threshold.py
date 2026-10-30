# -*- coding: utf-8 -*-
"""复算『只改阈值不改参数』的扫描，检验分析1内部的『空档』论断。"""
import math

P = dict(x0=100.0, y0=100000.0, z0=1000.0, w_x=0.2, w_y=0.5, w_z=0.3,
         **{"lambda": 0.7}, epsilon=1e-6)


def E(x, y, z, p=P):
    S = (p["w_x"] * math.log1p(x) / math.log1p(p["x0"])
         + p["w_y"] * math.log1p(y) / math.log1p(p["y0"])
         + p["w_z"] * math.log1p(z) / math.log1p(p["z0"]) + p["epsilon"])
    return 100.0 * (1 - math.exp(-p["lambda"] * S))


REAL = [("老干妈", 30, 48316000, 149), ("阿雅", 5, 12506705, 302),
        ("长鑫", 11, 13822047, 42), ("花生地", 3, 2067344, 129),
        ("男子", 6, 3766947, 1)]
HOT = [("M1", 100, 1e6, 1000), ("M2", 1000, 1e7, 1e4), ("M3", 500, 3e7, 2e4),
       ("N1", 2000, 1e9, 1e5), ("N2", 5000, 1e9, 1e7), ("N3", 1000, 1e8, 1e5)]

print("=== 情景点 E 值（默认参数） ===")
for grp, pts in (("真实", REAL), ("假想", HOT)):
    for n, x, y, z in pts:
        print("  %s %-4s E=%.4f  %s" % (grp, n, E(x, y, z), "触发@66" if E(x, y, z) > 66 else ""))

print()
print("=== 阈值扫描：触发数 ===")
print("%-6s %-12s %-14s" % ("阈值", "真实(0/5?)", "假想6点"))
for t in (50, 55, 58, 60, 62, 63, 64, 66, 70):
    r = sum(1 for _, x, y, z in REAL if E(x, y, z) > t)
    h = sum(1 for _, x, y, z in HOT if E(x, y, z) > t)
    names = [n for n, x, y, z in HOT if E(x, y, z) > t]
    print("%-6g %-12s %-14s %s" % (t, "%d/5" % r, "%d/6" % h, ",".join(names)))

print()
print("=== 分析1原文断言的核对 ===")
print("断言A: '阈值取 55/58/60/62/64/66/70，本批触发数分别是 0/0/0/0/0/0/0'")
for t in (55, 58, 60, 62, 64, 66, 70):
    r = sum(1 for _, x, y, z in REAL if E(x, y, z) > t)
    print("   t=%-3g 真实触发=%d" % (t, r))
print()
print("断言B: '大热门+国民触发数为 5/5/5/5/5/3/2'（7个数对8个阈值，本身缺一项）")
for t in (50, 55, 58, 60, 62, 64, 66, 70):
    h = sum(1 for _, x, y, z in HOT if E(x, y, z) > t)
    print("   t=%-3g 假想触发=%d/6" % (t, h))
print()
print("断言C: '把 66 改成 60 或 70 完全没有区别'（就本批真实话题而言）")
for t in (60, 66, 70):
    r = sum(1 for _, x, y, z in REAL if E(x, y, z) > t)
    h = sum(1 for _, x, y, z in HOT if E(x, y, z) > t)
    print("   t=%-3g 真实=%d/5  假想=%d/6  -> %s" % (t, r, h, "确有区别" if h != 3 or r != 0 else "无区别"))
print()
print("断言D: '66 恰好卡在「大热门 M2/M3=62.45/63.69 差一点、国民 N3=67.36 只高 1.36」的尴尬位置'")
print("   M2=%.4f M3=%.4f N3=%.4f" % (E(1000, 1e7, 1e4), E(500, 3e7, 2e4), E(1000, 1e8, 1e5)))
print("   N3 - 66 = %.4f" % (E(1000, 1e8, 1e5) - 66))
print()
print("断言E: '阈值 62~63 才是默认参数下真正的分离点（真实 0/5，大热门+国民 5/5）'")
for t in (62, 62.45, 62.5, 63, 63.69, 64):
    r = sum(1 for _, x, y, z in REAL if E(x, y, z) > t)
    h = sum(1 for _, x, y, z in HOT if E(x, y, z) > t)
    print("   t=%-6g 真实=%d/5 假想=%d/6 %s" % (t, r, h, "<- 完全分离" if r == 0 and h == 6 else ""))
print()
print("=== E=66 的分离效果（默认参数） ===")
print("真实 0/5 =", sum(1 for _, x, y, z in REAL if E(x, y, z) > 66) == 0)
print("假想触发 =", [n for n, x, y, z in HOT if E(x, y, z) > 66])
