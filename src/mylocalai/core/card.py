# -*- coding: utf-8 -*-
"""结论卡片派生（§4.5）——全纯函数，**大模型不参与打分**。

卡片口径：标签 + 置信度 + 证据充分度 + as_of + 复审条件。标签与风险分沿用
verdict.assess() 的兼容层输出；新字段只从两样东西推导：
  1. assess() 的确定性结果（label/risk_score，纯函数）；
  2. 证据本的信源分布（domain_tier / source_kind 计数）。

映射规则外置在 ``config/verdict_rules.json`` 的 ``card`` 段（confidence_rules 按
顺序取第一条满足的；recheck 按证据缺口生成），代码只读不写死。配套变异测试
（tests/test_conclusion_card）锁定：篡改卡片字段必失败、LLM 输出进不了打分链。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["derive_card", "sufficiency_counts", "CARD_VERSION"]

CARD_VERSION = 2

#: verdict_rules.json 加载失败时的内置兜底（与文件默认值逐字一致）。
_BUILTIN_CARD = {
    "_comment": "结论卡片派生：confidence 只由证据分布决定；recheck_conditions 由证据缺口决定。",
    "confidence_rules": [
        {"min_official": 2, "min_total": 5, "confidence": "high"},
        {"min_official": 1, "min_total": 3, "confidence": "medium"},
        {"min_total": 3, "confidence": "medium"},
        {"min_total": 0, "confidence": "low"},
    ],
    "recheck": {
        "no_official": "出现官方/权威媒体级信源的通报或报道",
        "few_evidence": "证据本独立条目≥{threshold}",
        "few_evidence_threshold": 5,
        "official_lift": "证据本新增权威条目≥2",
    },
}

_CACHE: Optional[Dict[str, Any]] = None


def _card_shape_ok(data: Dict[str, Any]) -> bool:
    """card 段的形状校验（2026-10-31 复核修复）：合并前把关。

    - confidence_rules 必须是非空 list，且元素均为 dict、都含 "confidence" 键；
    - recheck 若存在必须是 dict。
    形状不合法 → 整段弃用退内置默认（与坏 JSON 同一路径）。此前只检查
    "confidence_rules 非空"就合并：confidence_rules 写成 dict/元素非 dict 时，
    _confidence 迭代到字符串键后 rule.get 抛 AttributeError（try 只捕
    TypeError/ValueError），derive_card 会当着调用方的面崩——配置笔误不能炸判定链。
    """
    crules = data.get("confidence_rules")
    if not isinstance(crules, list) or not crules:
        return False
    for rule in crules:
        if not isinstance(rule, dict) or "confidence" not in rule:
            return False
    if "recheck" in data and not isinstance(data.get("recheck"), dict):
        return False
    return True


def _rules() -> Dict[str, Any]:
    """读 verdict_rules.json 的 card 段（坏配置/形状不合法退回内置默认，不抛异常）。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    rules = dict(_BUILTIN_CARD)
    try:
        from mylocalai.config import rules as _rules_mod
        data = _rules_mod.load_rules().get("card")
        if isinstance(data, dict) and data.get("confidence_rules") and _card_shape_ok(data):
            merged = {k: v for k, v in _BUILTIN_CARD.items()}
            merged.update(data)
            rules = merged
    except Exception:  # noqa: BLE001  配置缺失/写坏 → 内置默认
        pass
    _CACHE = rules
    return rules


def reset_card_rules_cache() -> None:
    global _CACHE
    _CACHE = None


_TIERS_CACHE: Optional[Dict[str, Any]] = None


def _tier_info() -> Dict[str, Any]:
    """官方档名与已声明档名（与 retrieval/sources.py 同一份 sources_registry.json）。

    依赖方向 core ← retrieval，core 不能反向 import；这里直接读同一份配置文件（含同名
    环境变量语义）。解析语义与 sources._resolve_roles 一致：official_tiers 显式列出且
    在 tiers 声明里 → 用它；否则按 tiers 声明顺序取前 1 档兜底。改 sources_registry.json
    两边同时生效；若两端口径分叉，test_conclusion_card 的对拍用例会红。
    """
    global _TIERS_CACHE
    if _TIERS_CACHE is not None:
        return _TIERS_CACHE
    info: Dict[str, Any] = {"official": set(), "declared": set()}
    try:
        import json
        import os
        from pathlib import Path

        path = os.environ.get("RUMOR_SOURCES_PATH") or str(
            Path(__file__).resolve().parents[1] / "config" / "sources_registry.json")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        extra = os.environ.get("RUMOR_SOURCES_EXTRA_PATH")
        if extra and os.path.exists(extra):
            with open(extra, encoding="utf-8") as fh:
                extra_data = json.load(fh)
            if isinstance(extra_data.get("tiers"), dict) and isinstance(data.get("tiers"), dict):
                merged = dict(data["tiers"])
                merged.update(extra_data["tiers"])
                data["tiers"] = merged
            for key in ("official_tiers",):
                if key in extra_data:
                    data[key] = extra_data[key]
        tiers = data.get("tiers")
        if isinstance(tiers, dict) and tiers:
            declared = [str(t) for t in tiers.keys()]
            info["declared"] = set(declared)
            roles = data.get("official_tiers")
            if isinstance(roles, list) and roles and all(isinstance(x, str) for x in roles):
                picked = [t for t in roles if t in info["declared"]]
                info["official"] = set(picked) if picked else {declared[0]}
            else:
                info["official"] = {declared[0]}
    except Exception:  # noqa: BLE001  配置缺失/写坏 → 空集，靠 source_kind 字符串兜底
        pass
    _TIERS_CACHE = info
    return info


def reset_tier_info_cache() -> None:
    global _TIERS_CACHE
    _TIERS_CACHE = None


def sufficiency_counts(entries: List[Dict[str, Any]]) -> Dict[str, int]:
    """证据本条目按信源充分度计数：official / unofficial / unknown / total。

    口径与 retrieval.sources.source_kind 的词表一致（它返回"官方（…）/非官方（…）/其他"
    三类值，见 evidence.py:526 既有惯例）：source_kind 以"官方"开头或 domain_tier 属
    官方档 → official；以"非官方"开头或 tier 在声明档里 → unofficial；其余（"其他"/空）→ unknown。
    """
    info = _tier_info()
    counts = {"official": 0, "unofficial": 0, "unknown": 0}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("source_kind") or "")
        tier = str(e.get("domain_tier") or "")
        if kind.startswith("官方") or (tier and tier in info["official"]):
            counts["official"] += 1
        elif kind.startswith("非官方") or (tier and tier in info["declared"]):
            counts["unofficial"] += 1
        else:
            counts["unknown"] += 1
    counts["total"] = sum(counts.values())
    return counts


def _confidence(counts: Dict[str, int]) -> str:
    for rule in _rules().get("confidence_rules", []):
        try:
            if counts.get("official", 0) >= int(rule.get("min_official", 0)) and \
               counts.get("total", 0) >= int(rule.get("min_total", 0)):
                return str(rule.get("confidence", "low"))
        except (TypeError, ValueError):
            continue
    return "low"


def _recheck_conditions(counts: Dict[str, int]) -> List[str]:
    rc = _rules().get("recheck", {})
    out: List[str] = []
    if counts.get("official", 0) == 0:
        out.append(str(rc.get("no_official", "出现官方/权威媒体级信源的通报或报道")))
    try:
        threshold = int(rc.get("few_evidence_threshold", 5))
    except (TypeError, ValueError):
        threshold = 5
    if counts.get("total", 0) < threshold:
        out.append(str(rc.get("few_evidence", "证据本独立条目≥{threshold}")).replace(
            "{threshold}", str(threshold)))
    elif counts.get("official", 0) > 0:
        out.append(str(rc.get("official_lift", "证据本新增权威条目≥2")))
    return out


def derive_card(verdict: Optional[Dict[str, Any]],
                evidence_entries: Optional[List[Dict[str, Any]]],
                as_of: str = "") -> Dict[str, Any]:
    """从 assess() 输出 + 证据本条目派生结论卡片。

    只接受纯函数输出；叙述/LLM 的任何字段都进不了卡片（防打分链污染的边界在此）。
    """
    verdict = verdict or {}
    entries = [e for e in (evidence_entries or []) if isinstance(e, dict)]
    counts = sufficiency_counts(entries)
    return {
        "version": CARD_VERSION,
        "label": str(verdict.get("label", "")),
        "risk_score": verdict.get("risk_score"),
        "confidence": _confidence(counts),
        "evidence_sufficiency": counts,
        "as_of": str(as_of or ""),
        "recheck_conditions": _recheck_conditions(counts),
        "verdict_basis": "verdict.assess() 纯函数输出，大模型不参与打分",
    }
