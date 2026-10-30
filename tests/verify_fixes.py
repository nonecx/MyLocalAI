# -*- coding: utf-8 -*-
"""针对第二轮三项建议修复的验证：fallacy 归一化、emotion 强度锚点、logic few-shot。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module_logic.logic_module import _normalize_fallacy

print("== 1. fallacy 归一化单元验证 ==")
cases = [
    ("无", "无"),
    ("无明显谬误", "无"),
    ("没有发现谬误", "无"),
    ("不存在逻辑谬误", "无"),
    ("诉诸情绪谬误", "诉诸情绪"),
    ("偷换概念", "未知"),
    ("", "未知"),
]
ok = 0
for raw, want in cases:
    got = _normalize_fallacy(raw)
    mark = "OK" if got == want else f"FAIL(期望{want})"
    ok += got == want
    print(f"  normalize({raw!r}) = {got!r} {mark}")
print(f"  {ok}/{len(cases)} 通过")

print("== 2. emotion 强度锚点：普通正面推荐应 ≤ 4 分 ==")
from module_language.language_module import analyze_emotion

r = analyze_emotion("这家店的服务真的太好了，强烈推荐大家都去试试！")
print("  ", json.dumps(r, ensure_ascii=False))

print("== 3. logic few-shot：理性论证 relevance 期望'强'、fallacy 期望'无' ==")
from module_logic.logic_module import analyze_logic

r2 = analyze_logic("根据统计数据，因为样本量超过一万份，所以这个结论具有较高的可信度，证据比较充分。")
print("  ", json.dumps(r2, ensure_ascii=False))

print("== 4. 全链路：用例6 结论不应再是'疑似情绪化谣言' ==")
from core_judge import judge

out = judge("这家店的服务真的太好了，强烈推荐大家都去试试！")
print("  conclusion:", out["conclusion"])
print("  judge.emotion:", json.dumps(out["emotion"], ensure_ascii=False))
print("  judge.logic:", json.dumps(out["logic"], ensure_ascii=False))
