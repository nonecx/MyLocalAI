# -*- coding: utf-8 -*-
"""
MyLocalAI 全模块测试执行器（只读测试，不修改项目源码）。

对 12 条内置测试用例逐条调用并记录：
  a) module_language.language_module.analyze_emotion(text)   -> "emotion"
  b) module_language.language_module.analyze_aggression(text)-> "aggression"
  c) module_logic.logic_module.analyze_logic(text)           -> "logic_api"
  d) module_inference.analyze_sentiment/analyze_aggression/analyze_logic
     -> "local_sentiment" / "local_aggression" / "local_logic"（本地备选，仅记录现状）
  - 用例 1-8、12 另调用 core_judge.judge(text, emotion=..., attack=..., logic=...)
    -> "judge"（完整返回 dict；传入步骤 a/b/c 的预计算结果，judge 内部复用不再重复调 API）
  - report_generator 单独测：正常调用（report_standalone）+ 强制回退（report_fallback，放最后）
  - subprocess 运行一次 main.py --text ...，stdout 存为 "main_cli"

每次调用记录耗时秒数，存入该用例的 "elapsed" 子 dict。
每完成一条用例即增量写盘到 <项目根>/tests/test_results.json，防止中途超时丢数据。

运行（在项目根目录下，用项目 venv 的 python）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/run_module_tests.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 项目根目录按本文件位置推导，不写死盘符（换机/换目录/换操作系统都能跑）
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
RESULTS_PATH = os.path.join(PROJECT_ROOT, "tests", "test_results.json")

# ===========================================================================
# 12 条内置测试用例（用例 11 的超长文本在代码里循环生成，约 5000 字符）
# ===========================================================================
_BASE_SENTENCE = "这是一句用于测试超长输入的中文句子。"
_LONG_TEXT = ""
while len(_LONG_TEXT) < 5000:
    _LONG_TEXT += _BASE_SENTENCE

CASES = [
    {"id": 1, "category": "人身攻击辱骂", "text": "你真是个蠢货，滚开！别在这丢人现眼。"},
    {"id": 2, "category": "群体仇恨煽动", "text": "这些人就是社会的垃圾，全都该滚出去！大家都这么说！"},
    {"id": 3, "category": "情绪化谣言", "text": "气死了！听说自来水被人下毒了，所有人都被骗了，官方根本不敢说！"},
    {"id": 4, "category": "理性论证", "text": "根据统计数据，因为样本量超过一万份，所以这个结论具有较高的可信度，证据比较充分。"},
    {"id": 5, "category": "中性陈述", "text": "今天下午三点在图书馆三楼有一场讲座，感兴趣的同学可以参加。"},
    {"id": 6, "category": "正面推荐", "text": "这家店的服务真的太好了，强烈推荐大家都去试试！"},
    {"id": 7, "category": "滑坡谬误", "text": "一旦允许这种行为，迟早会失去控制，最终整个社会都会完蛋。"},
    {"id": 8, "category": "诉诸权威", "text": "专家说了这个方法有效，科学家都认证过了，你们反对就是反科学。"},
    {"id": 9, "category": "空字符串", "text": ""},
    {"id": 10, "category": "纯特殊字符", "text": "！！！@@@###￥￥￥???"},
    {"id": 11, "category": "超长文本", "text": _LONG_TEXT},
    {"id": 12, "category": "混合情绪攻击", "text": "我就是气不过，凭什么他可以我不可以？大家都这么说我也不怕，你就是想害我。"},
]

# 非"纯边界"用例（跑 core_judge.judge）: 1-8 和 12；9/10/11 跳过 judge
JUDGE_IDS = {1, 2, 3, 4, 5, 6, 7, 8, 12}

# ===========================================================================
# 全局结果容器 + 增量写盘
# ===========================================================================
results = {
    "generated_at": datetime.now().isoformat(),
    "cases": [],
    "report_standalone": "",
    "report_fallback": "",
    "main_cli": "",
}


def save_results() -> None:
    """把当前全部结果增量写入 test_results.json（ensure_ascii=False, utf-8）。"""
    try:
        with open(RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 写盘失败: {type(exc).__name__}: {exc}", flush=True)


# ===========================================================================
# 通用计时调用助手
# ===========================================================================
def timed_call(fn, *args, **kwargs):
    """调用 fn(*args, **kwargs)，返回 (结果或{"error":...}, 耗时秒)。异常不外抛。"""
    t0 = time.perf_counter()
    try:
        out = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 调用异常: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        out = {"error": f"{type(exc).__name__}: {exc}"}
    return out, round(time.perf_counter() - t0, 3)


def timed_call_with_timeout(fn, *args, timeout=90.0):
    """带看门狗的调用：超时则记录 error（线程留在后台，进程结束时随之消亡）。"""
    box = {}

    def _target():
        try:
            box["result"] = fn(*args)
        except Exception as exc:  # noqa: BLE001
            box["result"] = {"error": f"{type(exc).__name__}: {exc}"}

    t0 = time.perf_counter()
    th = threading.Thread(target=_target, daemon=True)
    th.start()
    th.join(timeout)
    elapsed = round(time.perf_counter() - t0, 3)
    if th.is_alive():
        return {"error": f"本地调用超时（>{int(timeout)}s，后台线程仍在运行，仅记录现状）"}, elapsed
    return box.get("result", {"error": "本地调用无返回值"}), elapsed


# ===========================================================================
# 模块导入（失败不中断，逐条调用时记 error）
# ===========================================================================
_IMPORT_ERRORS = {}

try:
    from module_language import language_module
except Exception as exc:  # noqa: BLE001
    language_module = None
    _IMPORT_ERRORS["module_language"] = f"{type(exc).__name__}: {exc}"

try:
    from module_logic import logic_module
except Exception as exc:  # noqa: BLE001
    logic_module = None
    _IMPORT_ERRORS["module_logic"] = f"{type(exc).__name__}: {exc}"

try:
    import module_inference
except Exception as exc:  # noqa: BLE001
    module_inference = None
    _IMPORT_ERRORS["module_inference"] = f"{type(exc).__name__}: {exc}"

try:
    import core_judge
except Exception as exc:  # noqa: BLE001
    core_judge = None
    _IMPORT_ERRORS["core_judge"] = f"{type(exc).__name__}: {exc}"

try:
    import report_generator
except Exception as exc:  # noqa: BLE001
    report_generator = None
    _IMPORT_ERRORS["report_generator"] = f"{type(exc).__name__}: {exc}"

try:
    import shared_core.llm_client as llm_client_mod
except Exception as exc:  # noqa: BLE001
    llm_client_mod = None
    _IMPORT_ERRORS["shared_core.llm_client"] = f"{type(exc).__name__}: {exc}"


def _import_error_result(module_name: str) -> dict:
    return {"error": f"模块导入失败: {_IMPORT_ERRORS.get(module_name, '未知')}"}


# ===========================================================================
# 逐用例执行
# ===========================================================================
print(f"=== MyLocalAI 全模块测试开始 {results['generated_at']} ===", flush=True)
print(f"超长文本（用例11）生成字符数: {len(_LONG_TEXT)}", flush=True)
if _IMPORT_ERRORS:
    print(f"[WARN] 存在导入失败的模块: {_IMPORT_ERRORS}", flush=True)

for idx, case in enumerate(CASES, start=1):
    cid = case["id"]
    category = case["category"]
    text = case["text"]
    print(f"---- 用例 {cid}（{category}，{len(text)} 字符）----", flush=True)

    record = {
        "id": cid,
        "category": category,
        "text": text,
        "emotion": {},
        "aggression": {},
        "logic_api": {},
        "local_sentiment": {},
        "local_aggression": {},
        "local_logic": {},
        "elapsed": {},
    }

    # a) 情感分析（LongCat API）
    if language_module is not None:
        record["emotion"], record["elapsed"]["emotion"] = timed_call(
            language_module.analyze_emotion, text
        )
    else:
        record["emotion"] = _import_error_result("module_language")

    # b) 攻击性分析（LongCat API）
    if language_module is not None:
        record["aggression"], record["elapsed"]["aggression"] = timed_call(
            language_module.analyze_aggression, text
        )
    else:
        record["aggression"] = _import_error_result("module_language")

    # c) 逻辑分析（LongCat API）
    if logic_module is not None:
        record["logic_api"], record["elapsed"]["logic_api"] = timed_call(
            logic_module.analyze_logic, text
        )
    else:
        record["logic_api"] = _import_error_result("module_logic")

    # d) 本地备选模块（仅记录现状，本机基本不可用）
    if module_inference is not None:
        record["local_sentiment"], record["elapsed"]["local_sentiment"] = timed_call_with_timeout(
            module_inference.analyze_sentiment, text, timeout=90.0
        )
        record["local_aggression"], record["elapsed"]["local_aggression"] = timed_call_with_timeout(
            module_inference.analyze_aggression, text, timeout=90.0
        )
        record["local_logic"], record["elapsed"]["local_logic"] = timed_call_with_timeout(
            module_inference.analyze_logic, text, timeout=30.0
        )
    else:
        record["local_sentiment"] = _import_error_result("module_inference")
        record["local_aggression"] = _import_error_result("module_inference")
        record["local_logic"] = _import_error_result("module_inference")

    # judge（仅用例 1-8、12）：复用上面 a/b/c 三份预计算结果，
    # 不再让 judge 内部重复调用 API（aggression 的结果传给 judge 的 attack 参数）
    if cid in JUDGE_IDS:
        if core_judge is not None:
            record["judge"], record["elapsed"]["judge"] = timed_call(
                core_judge.judge,
                text,
                emotion=record["emotion"],
                attack=record["aggression"],
                logic=record["logic_api"],
            )
        else:
            record["judge"] = _import_error_result("core_judge")

    results["cases"].append(record)
    save_results()  # 增量写盘：每完成一条用例立即保存

    # 每条用例一行简短进度
    if cid not in JUDGE_IDS:
        judge_status = "跳过（纯边界用例）"
    elif "judge" not in record or not isinstance(record.get("judge"), dict):
        judge_status = "无结果"
    elif "error" in record["judge"]:
        judge_status = f"出错: {record['judge']['error']}"
    else:
        conclusion = record["judge"].get("conclusion", "")
        judge_status = f"有结论: {conclusion}" if conclusion else "无结论"
    print(
        f"[{idx}/12] id={cid} category={category} judge={judge_status}",
        flush=True,
    )

# ===========================================================================
# e) report_generator 正常调用（独立样例数据）
# ===========================================================================
print("---- report_generator 独立测试（正常路径）----", flush=True)
SAMPLE_CONCLUSION = "恶意攻击言论"
SAMPLE_EMOTION = {"emotion_score": 8, "emotion_tendency": "极度负面", "keywords": ["废物", "垃圾"]}
SAMPLE_ATTACK = {"aggression_level": "高", "attack_target": "针对个人", "attack_type": "人身攻击"}
SAMPLE_LOGIC = {"logic_relevance": "弱", "logic_fallacy": "诉诸情绪"}

if report_generator is not None:
    t0 = time.perf_counter()
    try:
        results["report_standalone"] = report_generator.generate_report(
            SAMPLE_CONCLUSION, SAMPLE_EMOTION, SAMPLE_ATTACK, SAMPLE_LOGIC
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        results["report_standalone"] = f"<generate_report 异常: {type(exc).__name__}: {exc}>"
    print(f"report_standalone 耗时 {round(time.perf_counter() - t0, 3)}s", flush=True)
else:
    results["report_standalone"] = f"<report_generator 导入失败: {_IMPORT_ERRORS.get('report_generator')}>"
save_results()

# ===========================================================================
# main.py CLI 子进程测试（在 report_fallback 之前跑，避免任何污染；独立进程互不影响）
# ===========================================================================
print("---- main.py CLI 子进程测试 ----", flush=True)
_env = dict(os.environ)
_env["PYTHONIOENCODING"] = "utf-8"
_t0 = time.perf_counter()
try:
    proc = subprocess.run(
        [sys.executable, "main.py", "--text", "你真是个蠢货，滚开！"],
        cwd=PROJECT_ROOT,
        env=_env,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )
    results["main_cli"] = proc.stdout or ""
    if proc.returncode != 0:
        print(f"[WARN] main.py 退出码 {proc.returncode}，stderr:\n{proc.stderr}", flush=True)
    elif proc.stderr.strip():
        print(f"[INFO] main.py stderr:\n{proc.stderr}", flush=True)
except subprocess.TimeoutExpired as exc:
    partial = exc.stdout or ""
    if isinstance(partial, bytes):
        partial = partial.decode("utf-8", errors="replace")
    results["main_cli"] = partial + "\n<ERROR: main.py 子进程超时（120s）>"
    print("[WARN] main.py 子进程超时（120s）", flush=True)
except Exception as exc:  # noqa: BLE001
    traceback.print_exc()
    results["main_cli"] = f"<ERROR: 子进程调用异常 {type(exc).__name__}: {exc}>"
print(f"main_cli 耗时 {round(time.perf_counter() - _t0, 3)}s，输出 {len(results['main_cli'])} 字符", flush=True)
save_results()

# ===========================================================================
# f) report_generator 强制回退测试（放最后执行；用完即恢复 monkeypatch）
#    先清空进程内 LLM 结果缓存：缓存键不含"客户端是否可用"，不清的话第二次调用会直接
#    命中前面正常路径缓存的成功正文，真实回退根本不会发生——验收就成了假通过
#    （2026-10-15 检验报告发现 11）。
# ===========================================================================
print("---- report_generator 强制回退测试（monkeypatch create_client -> None）----", flush=True)
if report_generator is not None and llm_client_mod is not None:
    _orig_create_client = llm_client_mod.create_client
    llm_client_mod.create_client = lambda: None
    try:
        import shared_core.llm_cache as _cache_mod
        _cache_mod.clear()
        _t0 = time.perf_counter()
        results["report_fallback"] = report_generator.generate_report(
            SAMPLE_CONCLUSION, SAMPLE_EMOTION, SAMPLE_ATTACK, SAMPLE_LOGIC
        )
        print(f"report_fallback 耗时 {round(time.perf_counter() - _t0, 3)}s", flush=True)
        # 这次必须真的"发起过失败的调用"，而不是复用缓存：cache 里不该新增条目
        _cache_stats = _cache_mod.stats()
        results["report_fallback_cache"] = _cache_stats
        print(f"回退后缓存统计: {_cache_stats}", flush=True)
    except Exception as exc:
        traceback.print_exc()
        results["report_fallback"] = f"<generate_report(回退) 异常: {type(exc).__name__}: {exc}>"
    finally:
        llm_client_mod.create_client = _orig_create_client  # 还原 monkeypatch
    has_marker = "本地模板回退" in results["report_fallback"]
    print(f"report_fallback 是否包含'本地模板回退': {has_marker}", flush=True)
else:
    results["report_fallback"] = "<跳过: report_generator 或 shared_core.llm_client 导入失败>"
    print(results["report_fallback"], flush=True)
save_results()

# ===========================================================================
# 结束摘要
# ===========================================================================
print("=== 测试完成 ===", flush=True)
print(f"结果文件: {RESULTS_PATH}", flush=True)
print(f"用例数: {len(results['cases'])}/12", flush=True)
print(
    f"report_fallback 含'本地模板回退': {'本地模板回退' in results['report_fallback']}, "
    f"main_cli 输出字符数: {len(results['main_cli'])}",
    flush=True,
)
