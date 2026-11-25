# -*- coding: utf-8 -*-
"""热度类工具封装（§5.2）：check_heat（联网估参）/ compute_heat（纯公式零 API）。

两条车道（§5.3 快慢车道）：
  - compute_heat：纯确定性公式（attention.attention），x/y/z 由路由层校验非负有限数，
    零 API、零建档、<10ms，秒答车道；
  - check_heat：检索估参（estimate）→ 公式算分 → 写热度快照章节（读+写快照），
    建档 input_kind="topic"（档案域把"话题核查"与"消息核查"区分开）。

热度口径铁律：分数由 attention 纯函数算出，大模型不参与；y/z 抽不到公开数值时用
代理值并如实标注 proxy_flags（口径诚实标注沿 §2.3）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from mylocalai.assistant.registry import ToolRegistry, id_param, topic_param, xyz_param

__all__ = ["register_all"]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _num(value: Any, default: float = 0.0) -> float:
    """服务返回值的容错浮点化（坏形状退默认，不抛）。"""
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _heat_card(detail: Dict[str, Any], as_of: str) -> Dict[str, Any]:
    """热度 E 分卡（cards.js renderHeat 的字段口径：score/x/y/z/band/profile/proxy_flags）。

    另带 `phenomenon`（现象判读）：卡片多渲染一行形态判读；字段缺失时前端整行不渲染。
    """
    phenomenon = detail.get("phenomenon")
    return {
        "x": _num(detail.get("x")),
        "y": _num(detail.get("y")),
        "z": _num(detail.get("z")),
        "score": _num(detail.get("score")),
        "band": str(detail.get("band", "")),
        "profile": str(detail.get("profile", "")),
        "proxy_flags": [str(f) for f in (detail.get("proxy_flags") or [])],
        "phenomenon": phenomenon if isinstance(phenomenon, dict) else {},
        "degraded": bool(detail.get("degraded")),
        "as_of": as_of,
    }


def _heat_digest(topic: str, detail: Dict[str, Any], card: Dict[str, Any]) -> str:
    lines = [f"[热度评估] 话题：{topic}"]
    lines.append(f"- 关注度 E = {card['score']:.2f}/100（档位：{card['band'] or '未知'}；"
                 f"参数档 {card['profile'] or '默认'}；attention 纯函数，大模型不参与）")
    lines.append(f"- 输入：文章数 x={card['x']:g}，阅读量 y={card['y']:g}，留言量 z={card['z']:g}")
    if card["proxy_flags"]:
        lines.append("- 口径标注（代理/估计值）：" + "、".join(card["proxy_flags"]))
    if card["degraded"]:
        lines.append("- 降级说明：" + str(detail.get("error") or detail.get("note")
                                        or "取数不完整，分数仅供参考"))
    if detail.get("quality"):
        lines.append(f"- 取数质量：{detail.get('quality')}")
    return "\n".join(lines)


def _check_heat(ctx: Any, topic: str) -> Dict[str, Any]:
    """check_heat：联网估参 → 公式 → 写快照章节（读+写快照）。"""
    services = ctx.services
    detail = services["heat"](str(topic))
    if not isinstance(detail, dict) or not detail:
        detail = {"x": 0.0, "y": 0.0, "z": 0.0, "score": 0.0, "profile": "",
                  "proxy_flags": [], "degraded": True,
                  "error": "热度服务无输出"}
    degraded = bool(detail.get("degraded"))

    card = _heat_card(detail, _now_iso())
    # 话题核查建档（input_kind="topic"）+ 快照落库；档案写失败不挡结论
    dossier_id: Optional[int] = None
    try:
        dossier_id = ctx.dossiers.start_dossier(str(topic), input_kind="topic")
        ctx.dossiers.record_heat(dossier_id, detail, profile=card["profile"])
    except Exception:  # noqa: BLE001
        dossier_id = None
    # 一次联网估参 = 1 轮检索（§5.2 成本口径）；占位/降级输出如实计 0
    requests = 0 if degraded else 1
    not_saved = dossier_id is None
    digest = _heat_digest(str(topic), detail, card)
    if not_saved:
        digest += "\n- 本次热度结果未保存到本机（保存偏好关闭或档案服务不可用）。"
    return {"ok": True, "card": card, "digest": digest,
            "cost": {"requests": requests, "tokens": 0},
            "dossier_id": dossier_id, "degraded": degraded,
            "not_saved": not_saved,
            "save_reason": "results_disabled_or_store_unavailable" if not_saved else "",
            "score": card["score"], "band": card["band"], "quality": detail.get("quality", ""),
            "phenomenon": card["phenomenon"]}


def _compute_heat(ctx: Any, x: float, y: float, z: float) -> Dict[str, Any]:
    """compute_heat：纯公式零 API、只读、不建档（秒答车道）。

    x/y/z 已经过 registry.xyz_param 校验（非负有限数），heat_formula 服务内部
    （attention.score_detail）还有第二层类型防御——两层一致才放行。
    """
    detail = ctx.services["heat_formula"](float(x), float(y), float(z))
    if not isinstance(detail, dict) or not detail:
        detail = {"x": float(x), "y": float(y), "z": float(z), "score": 0.0,
                  "profile": "", "proxy_flags": [], "degraded": True,
                  "error": "公式服务无输出"}
    card = _heat_card(detail, _now_iso())
    return {"ok": True, "card": card, "digest": _heat_digest("给定 x/y/z 的直算", detail, card),
            "cost": {"requests": 0, "tokens": 0},
            "dossier_id": None, "degraded": bool(detail.get("degraded")),
            "score": card["score"], "band": card["band"],
            "phenomenon": card["phenomenon"]}


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册热度类工具（services 形参约定同 tools_verify.register_all）。"""
    registry.register("check_heat", _check_heat, params=["topic"],
                      permission="write", cost="medium", est_s=10.0,
                      description="查一个话题现在有多热（联网估参 + 确定性公式，写热度快照）",
                      validators={"topic": topic_param})
    registry.register("compute_heat", _compute_heat, params=["x", "y", "z"],
                      permission="read", cost="zero", est_s=0.0,
                      description="用 x/y/z 三个数直接算关注度分数（纯公式，零 API，秒回）",
                      validators={"x": xyz_param("x"), "y": xyz_param("y"), "z": xyz_param("z")})
