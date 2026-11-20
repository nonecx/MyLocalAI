# -*- coding: utf-8 -*-
"""平台热度评估（platform/heat.py）：一批发现结果 → 确定性关注度 E 分。

复用既有两层，不新造公式：
  - `attention.estimate.estimate_from_search(query, results)` 从**已取回的**结果
    估 x/y/z（本模块不再发起任何检索——巡检的检索结果就是估计的输入）；
  - `attention.attention.attention_score(x, y, z)` 算 E（纯确定性公式）。

口径诚实（与 attention 包一致）：y/z 是代理/估计值，`estimated` 恒为 True，
调用方（runner/前端）必须原样展示「代理/估计值」徽章；检索无结果时
quality="missing"、E=0，由上层按"未取到"处理，不当成"不热"。

**永不抛异常**：估计失败退 {"e": 0.0, "quality": "missing", ...} + error 说明。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["estimate_heat", "relative_change"]


def estimate_heat(query: str, results: List[Any], *,
                  per_article_reads: float = 1e5,
                  params: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """一批结果 → 热度评估 dict。

    返回：{"x", "y", "z", "e", "quality", "estimated", "notes", "sources", "error"}。
    "error" 非空表示估计过程降级（此时 e=0.0、quality="missing"）。
    """
    try:
        from mylocalai.attention import attention as _attn
        from mylocalai.attention.estimate import estimate_from_search
        est = estimate_from_search(query, results or [], per_article_reads=per_article_reads)
        e = _attn.attention_score(est.x, est.y, est.z, params)
        out = est.to_dict()
        out.update({"e": float(e), "error": ""})
        return out
    except Exception as exc:  # noqa: BLE001  热度是增益信息，估不出不挡巡检
        return {"x": 0, "y": 0.0, "z": 0.0, "e": 0.0, "quality": "missing",
                "estimated": True, "notes": [], "sources": [], "query": str(query or ""),
                "error": f"heat_estimate_failed: {type(exc).__name__}"}


def relative_change(new: Any, old: Any) -> float:
    """环比相对变化：|new − old| / max(|new|, |old|)；两值同为 0 或无旧值 → 0.0。

    定义与 assistant/reminders.py 的快照变化幅度逐字一致（同一口径两处使用：
    提醒行与传播风险分级）；任一值不是有限实数 → 0.0（无环比，不猜）。
    返回值裁剪到 [0, 1]——relative change 数学上不超过 1。
    """
    try:
        a = float(new)
        b = float(old)
    except (TypeError, ValueError):
        return 0.0
    if a != a or b != b:            # NaN
        return 0.0
    if a in (float("inf"), float("-inf")) or b in (float("inf"), float("-inf")):
        return 0.0
    denom = max(abs(a), abs(b))
    if denom <= 0:
        return 0.0
    return min(1.0, abs(a - b) / denom)
