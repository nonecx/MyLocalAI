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
  - module_inference.py（本地模型推理）保留为备选方案。本机两个本地模型当前
    不可用（modelscope 依赖缺失 / ToxiTrace config 不兼容 transformers 5.x），
    故主链路走 API。
  - 三个分析函数全部内置容错，单模块失败只返回带 error 键的默认值，
    不会让整个裁判崩溃。

用法：
    python core_judge.py "这是一段待判定的文本。"
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from module_language.language_module import analyze_aggression, analyze_emotion
from module_logic.logic_module import analyze_logic
from report_generator import generate_report
from rumor_agent import tiering
from shared_core import llm_cache
from shared_core.console_io import force_utf8_stdio
from attn_agent.deep_read import deep_read

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
# 档位线本身来自 rumor_agent/verdict_rules.json 的 speech_tier 段（rumor_agent/tiering.py）。
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


def _attention_score_for_analysis(query: str = "",
                                  allow_network: bool = False) -> Dict[str, Any]:
    """给命令行链路算关注度分数。

    两种模式：
      * `allow_network=False`（默认）：不联网，用参考基准值占位，E 恒为 50.34，
        仅表示"关注度评估这条链路已接通"，**不代表真实热度**。
      * `allow_network=True`：走一次真实检索，用 `attn_agent.estimate` 从搜索结果
        估出 x/y/z 再算分。检索失败时自动退回占位结果，**绝不抛异常**，
        也不能影响主分析流程。

    联网是"尽力而为"：拿到多少算多少，并如实标注 estimated / quality，
    让调用方（CLI、网页）能把"真实数据"和"占位/估算"区分开。
    """
    try:
        from attn_agent import attention as _attn

        if allow_network and query.strip():
            from attn_agent.estimate import estimate_from_search
            try:
                from rumor_agent.search import search_all
                payload = search_all(query, k=8)
                est = estimate_from_search(query, payload.get("results") or [])
                detail = _attn.score_detail(float(est.x), est.y, est.z)
                detail.update(est.to_dict())
                detail["estimated"] = True
                detail["quality"] = est.quality
                return detail
            except Exception as exc:  # noqa: BLE001  检索失败要能退回占位结果
                detail = _baseline_attention(_attn)
                detail["note"] = (
                    f"联网取数失败（{type(exc).__name__}: {exc}），"
                    "已退回基准值；这不代表真实热度")
                detail["estimated"] = True
                detail["quality"] = "fallback"
                return detail

        return _baseline_attention(_attn)
    except Exception:  # noqa: BLE001  关注度是附加信息，算不出也不能影响主流程
        return {}


def _baseline_attention(attn_module) -> Dict[str, Any]:
    """构造"占位"关注度：x/y/z 均取参考基准值，E 恒为定标点 50.34。"""
    detail = attn_module.score_detail(
        attn_module.DEFAULT_PARAMS["x0"],
        attn_module.DEFAULT_PARAMS["y0"],
        attn_module.DEFAULT_PARAMS["z0"],
    )
    detail["estimated"] = True
    detail["quality"] = "baseline"
    detail["note"] = ("未联网取证，x/y/z 取参考基准值，E 仅表示“已进入关注度评估”；"
                      "真实量级请用 --attention-online 或网页端「话题联网估参」")
    return detail


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
    attention_online: bool = False,
) -> Dict[str, Any]:
    """
    核心裁判流程：
      1. 依次调用三个 API 分析函数（串行、单线程，各自内置容错）。
         若调用方已传入预计算的 emotion/attack/logic，则直接复用：
         大模型在 temperature=0 下仍有输出漂移，重复调用会让报告引用值
         与模块 JSON 不一致，复用可同时省去约 3 次重复 API 调用。
      2. 用 rumor_agent/tiering.py 的四档规则得出结论（与 verdict.speech_tier 共用同一份规则与阈值）。
      3. 将 conclusion 与三个 JSON 交给 generate_report 生成报告。
      4. 附带一个确定性关注度（attention）字段：见 `_attention_score_for_analysis`，
         命令行下取基准值，仅表示链路已接通，不代表真实传播规模。

    整个流程共用一份时长预算（deadline_s，默认取 JUDGE_DEADLINE_S）：
    上游慢/挂时不会一轮轮等满超时，预算用尽即让剩余模块走降级默认值。

    返回: {"emotion":..., "attack":..., "logic":..., "conclusion":..., "report":...}
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

        # ---- 2/3. 四档言论分层（唯一实现：rumor_agent/tiering.py） ----
        # 与 verdict.speech_tier 共用同一份规则与阈值（verdict_rules.json 的 speech_tier 段），
        # 不再各写一遍 if-else。边界复核后的分数只用于判档，不回写组件结果
        # （报告里引用的仍是模块返回的原始分）。
        conclusion = tiering.speech_tier({**emotion, "emotion_score": emotion_score}, attack, logic)

        # ---- 4. 生成最终报告 ----
        report = generate_report(conclusion, emotion, attack, logic)

        # ---- 5. 关注度（Attention）评估 ----
        # 与三维分析不同，这里**不调用大模型**；默认只用基准值占位。
        # 传 attention_online=True 时额外做一次真实检索来估 x/y/z，
        # 失败则自动退回占位结果，绝不影响上面的分析结果。
        attention = _attention_score_for_analysis(
            query=text, allow_network=attention_online)

    return {
        "emotion": emotion,
        "attack": attack,
        "logic": logic,
        "conclusion": conclusion,
        "report": report,
        "attention": attention,
        "degraded_components": _degraded_components(emotion, attack, logic, report),
    }


def main() -> None:
    """命令行测试入口。"""
    force_utf8_stdio()
    import argparse
    import pprint

    parser = argparse.ArgumentParser(description="核心裁判调度器（自测）")
    parser.add_argument("text", nargs="*", default=["这是一个测试文本。"])
    parser.add_argument("--no-attention", action="store_true",
                        help="不在输出中附带关注度（Attention）字段")
    parser.add_argument("--attention-online", action="store_true",
                        help="联网检索话题热度以估算关注度参数（默认用基准值占位，不联网）")
    parser.add_argument("--attention-profile", default=None,
                        help="关注度参数档位：spec（默认，用户指定原值）/ reachable（拉高可达性）")
    args = parser.parse_args()

    text = " ".join(args.text)
    output = judge(text, attention_online=args.attention_online)
    if args.no_attention:
        output.pop("attention", None)
    elif args.attention_profile:
        # 用户显式指定档位：用该档参数重算，让分数与档位一致
        try:
            from attn_agent import attention as _attn
            params, warns = _attn.load_profile(args.attention_profile)
            for w in warns:
                print(f"告警：{w}", file=sys.stderr)
            # load_profile 在档位名非法时会回退默认档，这里以“实际生效的档位”为准，
            # 而不是回显用户传入的无效名字，避免输出误导。
            effective = args.attention_profile
            if any("不存在" in w or "不合法" in w for w in warns):
                effective = _attn.DEFAULT_PROFILE
            attn = output.get("attention") or {}
            if "x" in attn:
                output["attention"] = _attn.score_detail(
                    attn.get("x", 0), attn.get("y", 0), attn.get("z", 0), params)
                output["attention"]["estimated"] = attn.get("estimated", True)
                output["attention"]["quality"] = attn.get("quality", "baseline")
                output["attention"]["note"] = attn.get("note", "")
                output["attention"]["profile"] = effective
        except Exception as exc:  # noqa: BLE001  档位切换失败不影响主流程
            print(f"警告：切换关注度参数档位失败（{exc}），沿用默认档结果",
                  file=sys.stderr)
    pprint.pprint(output)


if __name__ == "__main__":
    main()