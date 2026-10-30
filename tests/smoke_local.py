# -*- coding: utf-8 -*-
"""本地推理链路冒烟测试：验证 module_inference 的三个函数能否正常工作（含模型下载）。"""
import json
import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module_inference import analyze_aggression, analyze_logic, analyze_sentiment

text = "你真是个蠢货，滚开！大家都这么说，凭什么说我？"

t0 = time.time()
try:
    r = analyze_sentiment(text)
    print(f"[sentiment {time.time() - t0:.1f}s] {json.dumps(r, ensure_ascii=False)}")
except Exception as e:  # noqa: BLE001
    print(f"[sentiment] 崩溃: {type(e).__name__}: {e}")

t0 = time.time()
try:
    r = analyze_aggression(text)
    print(f"[aggression {time.time() - t0:.1f}s] {json.dumps(r, ensure_ascii=False)}")
except Exception as e:  # noqa: BLE001
    print(f"[aggression] 崩溃: {type(e).__name__}: {e}")

t0 = time.time()
r = analyze_logic(text)
print(f"[logic-rules {time.time() - t0:.1f}s] {json.dumps(r, ensure_ascii=False)}")
