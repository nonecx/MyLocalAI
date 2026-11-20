# -*- coding: utf-8 -*-
"""传播风险分级（platform/risk.py）——**纯函数，大模型不参与打分**（项目铁律）。

回答一个问题：这条信息**如果继续传播，造成大规模误信/骚动的风险**有多大？
注意它与"真假"是两个维度：本函数只度量传播风险（热度 × 增速 × 覆盖面 ×
谣言特征），不判定内容真伪；真伪由 rumor 引擎的证据链负责。

模型（全部输入都是确定性/已落库值，无一来自大模型）：
    score = w_heat·E + w_growth·(环比×100) + w_breadth·覆盖面 + w_rumor·核查风险分
    level = 阈值分档（notice/suspect/high/urgent），再应用 floors 保底规则

输入契约（变异测试锁定）：
  - heat:     E 分，有限实数或 None（无热度信息按 0）；
  - growth:   环比相对变化，有限实数 ∈ [0, 1]（heat.relative_change 的输出）；
  - breadth:  {"total": int, "official": int, "reliable": int} 或 None；
  - rumor:    {"label": str, "risk_score": 实数} 或 None；label 必须在
              rumor.verdict.ALL_LABELS 内（确定性结论的封闭集，堵死"塞一段
              大模型文本冒充结论"的通道）；
  - rules:    platform_rules.json 的 risk 段（weights/levels/floors/
              breadth_full_sources），缺键退 rules.DEFAULT_RULES 的对应值。

任何类型/范围不符 → 抛 ValueError/TypeError（调用方 bug 或注入尝试，不静默纠正）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mylocalai.platform import rules as _rules_mod

__all__ = ["LEVELS", "LEVEL_LABELS", "risk_rank", "assess_spread_risk"]

#: 传播风险档位（升序）。前端徽章与筛选口径同此 closed set。
LEVELS = ("notice", "suspect", "high", "urgent")

#: 档位中文名（展示层唯一来源；存储仍存英文档位，换语言不改数据）。
LEVEL_LABELS = {"notice": "关注", "suspect": "疑似", "high": "高危", "urgent": "紧急"}

#: 预警档位（UI 横幅/预警列表口径）：high 起算"大规模传播风险"。
WARNING_LEVELS = ("high", "urgent")


def risk_rank(level: Any) -> int:
    """档位 → 序数（越界按 -1，调用方据此拒绝坏档位）。"""
    try:
        return LEVELS.index(str(level))
    except ValueError:
        return -1


def _finite(value: Any, name: str, lo: Optional[float] = None,
            hi: Optional[float] = None) -> float:
    """有限实数校验；越界/坏类型直接抛（绝不把坏输入纠正成 0 蒙混过关）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} 必须是有限实数，得到 {type(value).__name__}")
    out = float(value)
    if out != out or out in (float("inf"), float("-inf")):
        raise ValueError(f"{name} 必须是有限实数")
    if lo is not None and out < lo:
        raise ValueError(f"{name} 不能小于 {lo}")
    if hi is not None and out > hi:
        raise ValueError(f"{name} 不能大于 {hi}")
    return out


def _opt_num(value: Any, name: str, lo: float, hi: float) -> float:
    return 0.0 if value is None else _finite(value, name, lo, hi)


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


def _rumor_factor(rumor: Any) -> tuple:
    """rumor → (风险分, 展示名)。None → (0, '未深查')；坏形状/越界 label 抛。"""
    if rumor is None:
        return 0.0, "未深查（无核查结论）"
    if not isinstance(rumor, dict):
        raise TypeError("rumor 必须是对象或 None")
    label = rumor.get("label")
    if label not in _ALL_LABELS():
        raise ValueError(f"rumor.label 不在确定性结论封闭集内: {label!r}")
    score = _finite(rumor.get("risk_score"), "rumor.risk_score", 0.0, 100.0)
    return score, str(label)


def _ALL_LABELS() -> tuple:
    """rumor.verdict.ALL_LABELS（延迟 import；import 失败时退内置五标签）。"""
    try:
        from mylocalai.rumor.verdict import ALL_LABELS
        return tuple(ALL_LABELS)
    except Exception:  # noqa: BLE001  rumor 包结构变化时保底（值逐字一致）
        return ("高风险谣言", "疑似谣言", "存疑待核实", "低风险", "无可核实陈述")


def assess_spread_risk(heat: Any, growth: Any, breadth: Any, rumor: Any,
                       rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """传播风险分级主函数。返回 {"level", "score", "reasons", "factors"}。

    level ∈ LEVELS；score ∈ [0, 100]；reasons 是纯推导的一句句中文理由
    （进卡片与预警列表，不进大模型上下文也不算分——只是分数的可解释呈现）。
    """
    risk_rules = (rules or {}).get("risk") if isinstance(rules, dict) else None
    if not isinstance(risk_rules, dict):
        risk_rules = _rules_mod.DEFAULT_RULES["risk"]
    weights = risk_rules.get("weights") or _rules_mod.DEFAULT_RULES["risk"]["weights"]
    levels = risk_rules.get("levels") or _rules_mod.DEFAULT_RULES["risk"]["levels"]
    floors = risk_rules.get("floors") or _rules_mod.DEFAULT_RULES["risk"]["floors"]
    breadth_full = risk_rules.get("breadth_full_sources") \
        or _rules_mod.DEFAULT_RULES["risk"]["breadth_full_sources"]

    # ---- 输入校验（坏输入即抛，不做"猜一个合理值"）----
    heat_v = _opt_num(heat, "heat", 0.0, 100.0)
    growth_v = _opt_num(growth, "growth", 0.0, 1.0)
    if breadth is None:
        total = official = reliable = 0
    else:
        if not isinstance(breadth, dict):
            raise TypeError("breadth 必须是对象或 None")
        total = _count(breadth.get("total", 0), "breadth.total")
        official = _count(breadth.get("official", 0), "breadth.official")
        reliable = _count(breadth.get("reliable", 0), "breadth.reliable")
    rumor_score, rumor_name = _rumor_factor(rumor)

    # ---- 四因素（各自 0-100）----
    breadth_factor = min(100.0, total / max(1, int(breadth_full)) * 100.0)
    factors = {
        "heat": heat_v,
        "growth": growth_v * 100.0,
        "breadth": breadth_factor,
        "rumor": rumor_score,
    }
    score = sum(float(weights.get(k, 0.0)) * v for k, v in factors.items())
    score = max(0.0, min(100.0, score))

    # ---- 阈值分档：score >= 阈值即进该档（从高到低取第一个满足的）----
    level = "notice"
    for name in ("urgent", "high", "suspect"):
        if score >= float(levels.get(name, 1e9)):
            level = name
            break

    # ---- floors 保底：满足全部指定条件 → 档位不低于该级 ----
    reasons: List[str] = []
    for floor in floors if isinstance(floors, list) else []:
        if not isinstance(floor, dict):
            continue
        ok = True
        for key, value in floor.items():
            if key == "level":
                continue
            if key == "min_rumor_score" and rumor_score < float(value):
                ok = False
            elif key == "min_breadth" and breadth_factor < float(value):
                ok = False
            elif key == "min_growth" and growth_v * 100.0 < float(value):
                ok = False
            elif key == "min_heat" and heat_v < float(value):
                ok = False
        if ok and risk_rank(floor.get("level")) > risk_rank(level):
            level = str(floor.get("level"))
            reasons.append(f"触发保底规则（{LEVEL_LABELS.get(level, level)}）")

    # ---- 可解释理由（呈现层，不参与打分）----
    reasons.append(f"热度 E {heat_v:g}/100")
    if growth_v > 0:
        reasons.append(f"热度环比增幅 {growth_v * 100:g}%")
    if total:
        authority = f"，其中权威信源 {official} 个" if official else ""
        reasons.append(f"独立信源 {total} 个{authority}")
    reasons.append(f"核查结论：{rumor_name}"
                   + (f"（风险分 {rumor_score:g}）" if rumor is not None else ""))

    return {"level": level, "score": round(score, 2), "reasons": reasons,
            "factors": {k: round(v, 2) for k, v in factors.items()}}
