# -*- coding: utf-8 -*-
"""关注度（Attention）挂载器：入口层（web/cli）把它接到分析结果上。

2026-10-30 M1-2 装配倒转：classic/judge 不再 import attention（依赖方向
classic/rumor/attention 互不 import），"judge 输出附带 attention 字段"改为由
入口层在拿到主结论后调用本模块挂载。函数体自 classic/judge.py 原样迁入。

两种模式：
  * allow_network=False（默认）：不联网，用参考基准值占位，E 恒为 50.34，
    仅表示"关注度评估这条链路已接通"，**不代表真实热度**。
  * allow_network=True：走一次真实检索，用 mylocalai.attention.estimate 从搜索结果
    估出 x/y/z 再算分。检索失败时自动退回占位结果，**绝不抛异常**，
    也不能影响主分析流程。

联网是"尽力而为"：拿到多少算多少，并如实标注 estimated / quality，
让调用方（CLI、网页）能把"真实数据"和"占位/估算"区分开。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _attach_phenomenon(detail: Dict[str, Any], profile: Optional[str] = None) -> Dict[str, Any]:
    """给一份关注度 detail 补上「现象判读」字段（失败不影响主结果）。

    判读口径与网页端 /api/attention 完全一致：都走 `attention.phenomenon.classify_phenomenon`，
    x/y/z 直接取 detail 里的值，proxy_flags 原样透传，保证 CLI 与网页同口径。
    """
    if not isinstance(detail, dict):
        return detail
    try:
        from mylocalai.attention.phenomenon import classify_phenomenon

        detail["phenomenon"] = classify_phenomenon(
            detail.get("x", 0.0),
            detail.get("y", 0.0),
            detail.get("z", 0.0),
            profile=detail.get("profile") or profile,
            proxy_flags=detail.get("proxy_flags"),
        )
    except Exception:  # noqa: BLE001  判读是附加信息，算不出也不能影响主流程
        pass
    return detail


def attention_for_text(query: str = "",
                       allow_network: bool = False,
                       profile: Optional[str] = None) -> Dict[str, Any]:
    """给一段文本/话题算关注度分数（原 classic.judge._attention_score_for_analysis）。

    返回的 detail 里始终带 `phenomenon`（五档形态判读，纯函数、零 LLM）；
    判读本身失败时字段缺失，主关注度结果照常返回。
    """
    try:
        from mylocalai.attention import attention as _attn

        if allow_network and query.strip():
            from mylocalai.attention.estimate import estimate_from_search
            try:
                from mylocalai.retrieval.search import search_all
                payload = search_all(query, k=8)
                est = estimate_from_search(query, payload.get("results") or [])
                detail = _attn.score_detail(float(est.x), est.y, est.z)
                detail.update(est.to_dict())
                detail["estimated"] = True
                detail["quality"] = est.quality
                return _attach_phenomenon(detail, profile)
            except Exception as exc:  # noqa: BLE001  检索失败要能退回占位结果
                detail = _baseline_attention(_attn)
                detail["note"] = (
                    f"联网取数失败（{type(exc).__name__}: {exc}），"
                    "已退回基准值；这不代表真实热度")
                detail["estimated"] = True
                detail["quality"] = "fallback"
                return _attach_phenomenon(detail, profile)

        detail = _baseline_attention(_attn)
        if profile:
            # 用户显式指定档位：用该档参数重算，让分数与档位一致
            try:
                params, _warns = _attn.load_profile(profile)
                if "x" in detail:
                    effective = profile
                    detail = _attn.score_detail(
                        detail.get("x", 0), detail.get("y", 0), detail.get("z", 0), params)
                    detail["estimated"] = True
                    detail["quality"] = "baseline"
                    detail["profile"] = effective
            except Exception:  # noqa: BLE001  档位切换失败不影响主流程
                pass
        return _attach_phenomenon(detail, profile)
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
