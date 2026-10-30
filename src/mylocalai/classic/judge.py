# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""
核心裁判调度器（纯提示词工程 + 大模型 API 版）。

本版本已彻底移除旧版的 LoRA 挂载/卸载、并发调度等微调逻辑，改为：
  - 从 module_language 导入语言模块（API 方案）：analyze_emotion / analyze_aggression
  - 从 module_logic 导入逻辑模块（API 方案）：analyze_logic
  - 三个分析模块并行调用（ThreadPoolExecutor(3)，共享同一次 judge 的时限预算）。
  - 用精确 if-else 规则给出最终结论。
  - 将 conclusion 与三个模块 JSON 交给 report_generator.generate_report 生成报告。

说明：
  - module_inference.py（本地模型备选推理）已于 2026-10-30 归档至 archive/，
    备选链路退出主树；主链路只走 API。
  - 三个分析函数全部内置容错，单模块失败只返回带 error 键的默认值，
    不会让整个裁判崩溃。

用法：
    python src/mylocalai/classic/judge.py "这是一段待判定的文本。"
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from mylocalai.classic.language import analyze_aggression, analyze_emotion
from mylocalai.classic.logic import analyze_logic
from mylocalai.classic.report import generate_report
from mylocalai.core import tiering
from mylocalai.llm import llm_cache
from mylocalai.util.console_io import force_utf8_stdio

# 一次 judge() 的总时长预算（秒）。上游不可达时原先要串行等 4 个模块 × 多次重试，
# 最差 515.8s；设了预算后每次 LLM 调用的等待不超过剩余预算，用完即快速失败转降级。
DEFAULT_JUDGE_DEADLINE_S = 120.0


def judge_deadline_s() -> float:
    """读取 judge() 的总预算（环境变量 JUDGE_DEADLINE_S，默认 120s；<=0 表示不限）。"""
    try:
        value = float(os.environ.get("JUDGE_DEADLINE_S") or DEFAULT_JUDGE_DEADLINE_S)
    except (TypeError, ValueError):
        return DEFAULT_JUDGE_DEADLINE_S
    return value


def _to_float(value: Any, default: float = 0.0) -> float:
    """安全地将值转为 float，失败返回 default。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# 分数贴着档位线（±0.5）时，上游 1 分的采样抖动就足以翻档，所以这些样本要再取一个
# 独立样本、用中位数定档——**不改档位阈值本身**：分桶（向下取整到 2 的倍数）虽然也能
# 消抖，但会把有效档位线整体左移（7 变成实际要 8 才算），属于产品口径变更；而言论
# 分层这条链路上"speech_tier 用原始分、与 core_judge 逐条等价"是既有契约
# （tests/test_stability_config.py:205 锁定），不能破坏。
# 档位线本身来自 src/mylocalai/config/verdict_rules.json 的 speech_tier 段（src/mylocalai/core/tiering.py）。
TIER_MARGIN = 0.5


def _is_empty_text(text: Any) -> bool:
    """空 / 纯空白（含只有空白字符）视为空输入。"""
    return not str(text or "").strip()


def _needs_boundary_recheck(score: float) -> bool:
    """分数是否贴着某个档位线（±TIER_MARGIN）。"""
    high, suspect = tiering.thresholds()
    return any(abs(score - threshold) <= TIER_MARGIN for threshold in (high, suspect))


def _recheck_emotion_score(text: str, score: float) -> float:
    """边界复核：再取一个**独立样本**（绕过进程内缓存），用两者中位数定档。

    为什么必须绕过缓存：同一段文本的第二次调用只会命中缓存、拿到同一个值，
    起不到复核作用（2026-10-22 检验报告发现 7 的实现要点）。
    """
    try:
        again = analyze_emotion(text, use_cache=False)
    except Exception:  # noqa: BLE001  复核失败就退回原值，不影响主链路
        return score
    second = _to_float(again.get("emotion_score"), score)
    return (score + second) / 2.0


def _degraded_components(emotion: Dict[str, Any], attack: Dict[str, Any],
                         logic: Dict[str, Any], report: str) -> List[str]:
    """对外可见的降级清单：哪个组件没给出可用结果、报告是否走了本地模板回退。

    改造前 judge() 的返回里没有任何降级信号，调用方（网站 / 前端 / 脚本）
    无法区分"正常结论"与"上游挂了但硬撑出一个结论"（2026-10-22 检验报告发现 9）。
    """
    degraded = [name for name, comp in (("emotion", emotion), ("attack", attack), ("logic", logic))
                if isinstance(comp, dict) and comp.get("error")]
    if "本地模板回退" in (report or ""):
        degraded.append("report")
    return degraded


def _empty_input_result(conclusion: str) -> Dict[str, Any]:
    """空输入结果：不调任何模块、也不调报告接口，直接给本地结论。

    改造前 judge("") 会照常走三个模块（模块内部短路，0 次调用）后，
    再走 report_generator 的**一次大模型调用**——真实上游下实测 4.27s。
    契约里"空输入 0 次 API 调用"只覆盖三个模块，报告那一次是漏网的。
    """
    skipped = "输入文本为空，未调用分析"
    return {
        "emotion": {"emotion_tendency": "未知", "emotion_score": 0.0, "keywords": [],
                    "error": skipped},
        "attack": {"attack_target": "未知", "aggression_level": "低", "attack_type": "未知",
                   "error": skipped},
        "logic": {"argument": "", "conclusion": "", "logic_relevance": "中",
                  "logic_fallacy": "未知", "error": skipped},
        "conclusion": conclusion,
        "report": (f"📊 【分析结论】\n该言论有较高可能性为【{conclusion}】。\n\n"
                   f"⚠️ 【免责声明】\n输入文本为空，未做分析（未调用任何大模型接口）。\n"),
        "degraded_components": ["emotion", "attack", "logic", "report"],
        "empty_input": True,
    }


def judge(
    text: str,
    emotion: Optional[Dict[str, Any]] = None,
    attack: Optional[Dict[str, Any]] = None,
    logic: Optional[Dict[str, Any]] = None,
    deadline_s: Optional[float] = None,
) -> Dict[str, Any]:
    """
    核心裁判流程：
      1. 依次调用三个 API 分析函数（串行、单线程，各自内置容错）。
         若调用方已传入预计算的 emotion/attack/logic，则直接复用：
         大模型在 temperature=0 下仍有输出漂移，重复调用会让报告引用值
         与模块 JSON 不一致，复用可同时省去约 3 次重复 API 调用。
      2. 用 src/mylocalai/core/tiering.py 的四档规则得出结论（与 verdict.speech_tier 共用同一份规则与阈值）。
      3. 将 conclusion 与三个 JSON 交给 generate_report 生成报告。
    整个流程共用一份时长预算（deadline_s，默认取 JUDGE_DEADLINE_S）：
    上游慢/挂时不会一轮轮等满超时，预算用尽即让剩余模块走降级默认值。

    返回: {"emotion":..., "attack":..., "logic":..., "conclusion":..., "report":...}

    注意：返回体不再带 attention 字段——关注度挂载上移到入口层
    （web / mylocalai.cli，经 mylocalai.attention.attach），classic 与 attention
    互不 import（2026-10-30 M1-2 装配倒转）。
    """
    budget = judge_deadline_s() if deadline_s is None else deadline_s
    if _is_empty_text(text):
        # 空输入短路：连报告接口都不调（见 _empty_input_result）
        return _empty_input_result("情绪化宣泄/普通网络争论")
    with llm_cache.deadline_scope(budget):
        # ---- 1. 三个分析函数并行（各自内置容错；已预计算则复用）----
        # 三者互不依赖，串行会把墙钟叠成 3 倍（实测单次全链路 17.4s，其中三次调用是大头）。
        # 子线程看不到主线程的预算（threading.local），所以用 deadline_scope_at 继承同一个
        # 绝对截止时刻，并行不会把预算撑破（2026-10-22 修复 F3）。
        budget_at = llm_cache.deadline_at()

        def _run(fn, *args):
            with llm_cache.deadline_scope_at(budget_at):
                return fn(*args)

        emotion_was_computed = emotion is None
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_emo = pool.submit(_run, analyze_emotion, text) if emotion is None else None
            f_atk = pool.submit(_run, analyze_aggression, text) if attack is None else None
            f_log = pool.submit(_run, analyze_logic, text) if logic is None else None
            emotion = f_emo.result() if f_emo is not None else emotion
            attack = f_atk.result() if f_atk is not None else attack
            logic = f_log.result() if f_log is not None else logic

        emotion_score = _to_float(emotion.get("emotion_score", 0.0))
        # 只在"本次真的调用了模块"时做边界复核：调用方预计算好的值由调用方负责。
        if emotion_was_computed and _needs_boundary_recheck(emotion_score):
            emotion_score = _recheck_emotion_score(text, emotion_score)

        # ---- 2/3. 四档言论分层（唯一实现：src/mylocalai/core/tiering.py） ----
        # 与 verdict.speech_tier 共用同一份规则与阈值（verdict_rules.json 的 speech_tier 段），
        # 不再各写一遍 if-else。边界复核后的分数只用于判档，不回写组件结果
        # （报告里引用的仍是模块返回的原始分）。
        conclusion = tiering.speech_tier({**emotion, "emotion_score": emotion_score}, attack, logic)

        # ---- 4. 生成最终报告 ----
        report = generate_report(conclusion, emotion, attack, logic)

    return {
        "emotion": emotion,
        "attack": attack,
        "logic": logic,
        "conclusion": conclusion,
        "report": report,
        "degraded_components": _degraded_components(emotion, attack, logic, report),
    }


def main() -> None:
    """命令行测试入口。"""
    force_utf8_stdio()
    import argparse
    import pprint

    parser = argparse.ArgumentParser(description="核心裁判调度器（自测）")
    parser.add_argument("text", nargs="*", default=["这是一个测试文本。"])
    args = parser.parse_args()

    text = " ".join(args.text)
    output = judge(text)
    pprint.pprint(output)


if __name__ == "__main__":
    main()