# -*- coding: utf-8 -*-
"""言论分层（四档）的**唯一**实现。

`core_judge.judge` 与 `verdict.speech_tier` 都从这里取规则——此前同一套四档规则在
两个文件里各写一遍（2026-10-22 检验报告发现 G5）。阈值放在
`src/mylocalai/config/verdict_rules.json` 的 `speech_tier` 段，可用 `RUMOR_RULES_PATH` /
`RUMOR_RULES_EXTRA_PATH` 覆盖（见 `src/mylocalai/rumor/rules.py`）。

刻意**不做分桶**：把情绪分向下取整到 2 的倍数会把有效档位线从 5/7 左移到
4/6 与 6/8（7 分不再算「高度疑似」），属于产品口径变更；而「言论分层与
core_judge 逐条等价」是既有契约（`tests/test_stability_config.py` 锁定）。
消抖由 `core_judge` 的「边界复核」负责。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from mylocalai.config.rules import load_rules

TIER_HIGH = "高度疑似水军/煽动性言论"
TIER_SUSPECT = "疑似情绪化谣言"
TIER_RATIONAL = "理性探讨"
TIER_OTHER = "情绪化宣泄/普通网络争论"

_ALL_TIERS = (TIER_HIGH, TIER_SUSPECT, TIER_RATIONAL, TIER_OTHER)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cfg_number(section: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(section[key])
    except (KeyError, TypeError, ValueError):
        return default


def thresholds() -> Tuple[float, float]:
    """当前档位线（high, suspect）。每次调用都读配置，换配置不必重启进程。"""
    speech = load_rules().get("speech_tier") or {}
    return (_cfg_number(speech, "high_score", 7.0),
            _cfg_number(speech, "suspect_score", 5.0))


def speech_tier(emotion: Dict[str, Any], attack: Dict[str, Any],
                logic: Dict[str, Any], thresholds_: Optional[Tuple[float, float]] = None) -> str:
    """四档言论分层：与 `core_judge.judge` 的结论规则逐条一致。

    `thresholds_` 省略时读 `verdict_rules.json` 的 `speech_tier` 段（默认 7 / 5）。
    """
    high, suspect = thresholds() if thresholds_ is None else thresholds_
    score = _to_float((emotion or {}).get("emotion_score", 0.0))
    aggression = str((attack or {}).get("aggression_level", "")).strip().lower()
    rel = str((logic or {}).get("logic_relevance", "")).strip().lower()
    if score >= high and aggression == "高" and rel == "弱":
        return TIER_HIGH
    if score >= suspect and rel == "弱":
        return TIER_SUSPECT
    if rel == "强" and aggression == "低":
        return TIER_RATIONAL
    return TIER_OTHER


__all__ = ["speech_tier", "thresholds", "TIER_HIGH", "TIER_SUSPECT", "TIER_RATIONAL",
           "TIER_OTHER"]
