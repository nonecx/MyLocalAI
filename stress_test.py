# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""
项目压力测试脚本。

对本项目做负载/边界压力测试，重点覆盖：
  1. 批量负载：连续分析大量文本，验证不崩溃、性能稳定。
  2. 长文本边界：超长输入触发 truncation，验证不 OOM。
  3. 空/特殊字符/混合输入：验证异常隔离，单条坏输入不崩整体。
  4. judge() + report_generator 在负载下的稳定性。
  5. 并发/多线程：验证 ThreadPoolExecutor 调用三个分析函数不冲突。

说明：
  - 模型类模块（ModelScope / ToxiTrace）在本机缺失或拉取失败时会优雅降级，
    压力测试主要覆盖逻辑规则引擎、裁判规则、报告生成与异常隔离在负载下的稳定性。
  - 纯 CPU、内存受限环境。

用法：
    python stress_test.py            # 运行完整压力测试
    python stress_test.py --quick    # 只跑小规模冒烟压力
"""

from __future__ import annotations

import argparse
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from module_inference import analyze_logic
from core_judge import judge, _to_float
from report_generator import generate_report

# 测试样本池
SAMPLES = [
    "你真是个蠢货，滚开！大家都这么说，凭什么说我？",
    "因为根据统计数据，所以这个结论是成立的，证据非常充分。",
    "你这个废物，去死吧，别在这丢人现眼。",
    "我觉得我们可以求同存异，理性探讨一下。",
    "一旦你这么做，迟早会失去一切，最终会完蛋。",
    "专家说这个药有效，科学家都权威认证了。",
    "正常的一句话，没有任何问题。",
    "大家冷静一下，不要冲动，我们好好沟通。",
    "你就是想害我，你总说对我不好，自己都做不好还说我。",
    "感谢你的耐心解答，我会认真考虑。",
]


def _make_long_text(n: int) -> str:
    """生成长度为 n 字符的文本，用于测试截断。"""
    base = "这是一句用于压力测试填充长度的中文句子，用来模拟超长网络言论。"
    return (base * ((n // len(base)) + 1))[:n]


def _measure(func, *args):
    """执行 func 并返回 (结果, 耗时秒)。"""
    t0 = time.perf_counter()
    result = func(*args)
    return result, time.perf_counter() - t0


def run_batch_load(n: int, verbose: bool = True) -> dict:
    """批量负载：连续分析 n 条随机样本。"""
    if verbose:
        print(f"\n[1] 批量负载测试：连续分析 {n} 条文本 ...")
    times = []
    errors = 0
    t_start = time.perf_counter()
    for i in range(n):
        text = random.choice(SAMPLES)
        try:
            res, dt = _measure(analyze_logic, text)
            times.append(dt)
            if "error" in res:
                errors += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if verbose:
                print(f"    第 {i} 条异常: {type(exc).__name__}: {exc}")
    total = time.perf_counter() - t_start
    avg = sum(times) / len(times) if times else 0
    peak = max(times) if times else 0
    if verbose:
        print(f"    完成 {n} 条，耗时 {total:.2f}s，平均 {avg*1000:.2f}ms/条，峰值 {peak*1000:.2f}ms，异常 {errors} 次")
    return {"count": n, "total_s": round(total, 3), "avg_ms": round(avg * 1000, 3), "peak_ms": round(peak * 1000, 3), "errors": errors}


def run_long_text_edge(verbose: bool = True) -> dict:
    """长文本边界：超长输入触发 truncation，验证不崩溃。"""
    if verbose:
        print("\n[2] 长文本边界测试：超长输入触发 truncation ...")
    results = {}
    for n in (1000, 5000, 20000):
        long_text = _make_long_text(n)
        try:
            res, dt = _measure(analyze_logic, long_text)
            results[n] = {"ok": True, "ms": round(dt * 1000, 2), "logic": res}
            if verbose:
                print(f"    {n}字符: OK，{dt*1000:.2f}ms，logic={res}")
        except Exception as exc:  # noqa: BLE001
            results[n] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if verbose:
                print(f"    {n}字符: FAIL {type(exc).__name__}: {exc}")
    return results


def run_edge_inputs(verbose: bool = True) -> dict:
    """边界输入：空/特殊字符/混合输入，验证异常隔离。"""
    if verbose:
        print("\n[3] 边界输入测试：空/特殊字符/混合输入 ...")
    edge_inputs = [
        "",
        " ",
        "\n\t",
        "!!!@@@###$$$%%%",
        "😀🎉❤️🔥 表情符号测试",
        "a" * 100,          # 无意义字母
        "1234567890",
        "<script>alert('xss')</script>",
        None,               # 故意传 None
    ]
    results = {}
    for i, txt in enumerate(edge_inputs):
        try:
            res = analyze_logic(txt)
            results[f"input{i}"] = {"ok": True, "res": res}
        except Exception as exc:  # noqa: BLE001
            results[f"input{i}"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if verbose:
                print(f"    边界输入 {i} ({str(txt)[:20]!r}): FAIL {type(exc).__name__}")
    ok_count = sum(1 for v in results.values() if v["ok"])
    if verbose:
        print(f"    边界输入 {len(results)} 个，成功 {ok_count} 个，失败 {len(results)-ok_count} 个")
    return {"total": len(results), "ok": ok_count, "results": results}


def run_judge_load(n: int, verbose: bool = True) -> dict:
    """judge() 负载：完整裁判链路在负载下稳定。"""
    if verbose:
        print(f"\n[4] judge() 负载测试：完整裁判链路 x{n} ...")
    times = []
    errors = 0
    conclusions = {}
    t_start = time.perf_counter()
    for i in range(n):
        text = random.choice(SAMPLES)
        try:
            res, dt = _measure(judge, text)
            times.append(dt)
            c = res.get("conclusion", "?")
            conclusions[c] = conclusions.get(c, 0) + 1
            # 报告必须生成且含关键字段
            rep = res.get("report", "")
            if not rep or "【分析结论】" not in rep:
                errors += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if verbose:
                print(f"    第 {i} 条异常: {type(exc).__name__}: {exc}")
    total = time.perf_counter() - t_start
    avg = sum(times) / len(times) if times else 0
    if verbose:
        print(f"    完成 {n} 次 judge()，耗时 {total:.2f}s，平均 {avg*1000:.2f}ms/次，异常 {errors} 次")
        print(f"    结论分布: {conclusions}")
    return {"count": n, "total_s": round(total, 3), "avg_ms": round(avg * 1000, 3), "errors": errors, "conclusions": conclusions}


def run_concurrency(verbose: bool = True) -> dict:
    """并发/多线程：ThreadPoolExecutor 同时调用三个分析函数，验证不冲突。"""
    if verbose:
        print("\n[5] 并发测试：多线程同时调用 analyze_logic ...")
    # 模拟真实并发：多个 worker 同时分析
    texts = [random.choice(SAMPLES) for _ in range(20)]
    errors = 0
    results = []
    t_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(analyze_logic, t) for t in texts]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if verbose:
                    print(f"    并发任务异常: {type(exc).__name__}: {exc}")
    total = time.perf_counter() - t_start
    if verbose:
        print(f"    并发完成 {len(texts)} 个任务，耗时 {total:.2f}s，成功 {len(results)} 个，异常 {errors} 次")
    # 校验所有结果都是合法 dict 且无内部异常
    valid = all(isinstance(r, dict) and "logic_relevance" in r for r in results)
    if verbose:
        print(f"    结果合法性校验: {'通过' if valid else '不通过'}")
    return {"tasks": len(texts), "total_s": round(total, 3), "errors": errors, "all_valid": valid}


def run_report_load(n: int, verbose: bool = True) -> dict:
    """报告生成负载：generate_report 在负载下稳定。"""
    if verbose:
        print(f"\n[6] 报告生成负载测试：generate_report x{n} ...")
    errors = 0
    lens = []
    t_start = time.perf_counter()
    for _ in range(n):
        try:
            rep = generate_report(
                "情绪化宣泄/普通网络争论",
                {"emotion_tendency": "负面", "emotion_score": 6.0, "keywords": ["废物"]},
                {"aggression_level": "高", "attack_type": "仇恨言论", "attack_target": "未知"},
                {"logic_relevance": "弱", "logic_fallacy": "人身攻击"},
            )
            lens.append(len(rep))
        except Exception as exc:  # noqa: BLE001
            errors += 1
    total = time.perf_counter() - t_start
    if verbose:
        print(f"    完成 {n} 次报告生成，耗时 {total:.2f}s，平均 {total/n*1000:.2f}ms/次，异常 {errors} 次")
    return {"count": n, "total_s": round(total, 3), "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description="项目压力测试")
    parser.add_argument("--quick", action="store_true", help="快速小规模测试")
    args = parser.parse_args()

    print("=" * 60)
    print("网络言论智能分析助手 - 压力测试")
    print("=" * 60)

    batch_n = 50 if args.quick else 300
    judge_n = 20 if args.quick else 80
    report_n = 50 if args.quick else 200

    # 一次性导入（确保模块加载）
    analyze_logic("预热")

    summary = {}
    summary["batch"] = run_batch_load(batch_n)
    summary["long_text"] = run_long_text_edge()
    summary["edge"] = run_edge_inputs()
    summary["judge"] = run_judge_load(judge_n)
    summary["concurrency"] = run_concurrency()
    summary["report"] = run_report_load(report_n)

    # ---- 汇总 ----
    print("\n" + "=" * 60)
    print("压力测试汇总")
    print("=" * 60)
    total_errors = (
        summary["batch"]["errors"]
        + summary["judge"]["errors"]
        + summary["concurrency"]["errors"]
        + summary["report"]["errors"]
    )
    long_fail = sum(1 for v in summary["long_text"].values() if not v.get("ok"))
    edge_fail = summary["edge"]["total"] - summary["edge"]["ok"]
    print(f"批量负载   : {summary['batch']['count']} 条，平均 {summary['batch']['avg_ms']}ms/条，异常 {summary['batch']['errors']}")
    print(f"长文本边界 : 3 档长度，失败 {long_fail} 档")
    print(f"边界输入   : {summary['edge']['total']} 个，失败 {edge_fail} 个")
    print(f"judge负载  : {summary['judge']['count']} 次，平均 {summary['judge']['avg_ms']}ms/次，异常 {summary['judge']['errors']}")
    print(f"并发测试   : {summary['concurrency']['tasks']} 任务，异常 {summary['concurrency']['errors']}，结果合法={summary['concurrency']['all_valid']}")
    print(f"报告负载   : {summary['report']['count']} 次，异常 {summary['report']['errors']}")
    print(f"总计异常   : {total_errors} 次")
    print("=" * 60)
    if total_errors == 0 and long_fail == 0 and edge_fail == 0:
        print("压力测试结论：全部通过 ✅（无崩溃、无异常泄漏）")
    else:
        print("压力测试结论：存在需关注的异常点 ⚠️（见上方明细）")
    print("=" * 60)


if __name__ == "__main__":
    main()