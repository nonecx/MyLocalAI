# -*- coding: utf-8 -*-
"""平台规则加载（platform/rules.py）：platform_rules.json → 规整后的规则字典。

与全项目"口径外置"文化一致（参照 attention/deep_read.py 的 load_rules）：
  - 文件缺失 / JSON 写坏 / 段结构不合法 → 对应段退回内置默认并留中文告警，
    **既不崩、也不静默生效**；
  - `PLATFORM_RULES_PATH` 整套替换、`PLATFORM_RULES_EXTRA_PATH` 增量覆盖
    （经 config/config_loader.py 统一合并）；
  - `load_rules()` 永不抛异常，返回 (rules, warnings)。

规则段：
  enabled / scan_interval_s / max_queries_per_scan / max_candidates_per_query
  screen.heat_screen_e / deep_verify.{max_per_scan,deadline_s}
  dedup.fingerprint_threshold / risk.{weights,breadth_full_sources,levels,floors}
  report.{enabled,max_llm_calls,max_entries,max_chars_per_entry,deadline_s,max_tokens}
  watch_queries / rss_sources

report 段是**可选段**（不在必填清单里）：缺失 → 内置默认且不告警——巡检报告
技能（platform/report）按默认开启；段内逐键校验（见 _normalize_report）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["RULES_PATH", "load_rules", "DEFAULT_RULES"]

RULES_PATH: Path = Path(__file__).resolve().parent / "platform_rules.json"

#: 内置默认（与 platform_rules.json 逐字同值；文件整体缺失时用它）。
DEFAULT_RULES: Dict[str, Any] = {
    "enabled": False,
    "scan_interval_s": 900,
    "max_queries_per_scan": 5,
    "max_candidates_per_query": 8,
    "screen": {"heat_screen_e": 40},
    "deep_verify": {"max_per_scan": 3, "deadline_s": 120},
    "dedup": {"fingerprint_threshold": 0.72},
    "risk": {
        "weights": {"heat": 0.4, "growth": 0.15, "breadth": 0.2, "rumor": 0.25},
        "breadth_full_sources": 10,
        "levels": {"suspect": 35, "high": 60, "urgent": 80},
        "floors": [
            {"min_rumor_score": 60, "min_breadth": 50, "level": "high"},
            {"min_growth": 60, "min_heat": 50, "level": "suspect"},
        ],
    },
    "report": {
        "enabled": True,
        "max_llm_calls": 1,
        "max_entries": 20,
        "max_chars_per_entry": 300,
        "deadline_s": 60,
        "max_tokens": 1200,
    },
    "watch_queries": [],
    "rss_sources": [],
}

#: 必填段（缺段留告警，与深读规则的 _missing_section_warnings 同口径）。
_REQUIRED_SECTIONS = ("screen", "deep_verify", "risk")

_RISK_WEIGHT_KEYS = ("heat", "growth", "breadth", "rumor")
_RISK_LEVEL_KEYS = ("suspect", "high", "urgent")


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _missing_section_warnings(target: Path) -> List[str]:
    """原文件缺了哪个必填段就告警哪个（读不到文件交给 load_config 报）。"""
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:  # noqa: BLE001  读不到/解析失败由 load_config 统一报告
        return []
    if not isinstance(raw, dict):
        return []
    return [f"平台规则文件（{target.name}）缺少 {name} 段，该段已用内置默认"
            for name in _REQUIRED_SECTIONS if name not in raw]


def _pos_int(value: Any, default: int, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return default
    return value


def _num(value: Any, default: float, minimum: float = 0.0) -> float:
    if not _is_num(value) or float(value) < minimum:
        return default
    return float(value)


def _normalize_risk(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    """risk 段规整：权重必须四个键齐全且总和为 1（否则整段退默认+告警）；
    阈值/广度满值/ floors 逐键校验，坏键退默认值。"""
    issues: List[str] = []
    default = DEFAULT_RULES["risk"]
    if not isinstance(raw, dict):
        return json.loads(json.dumps(default)), ["risk 段不是对象，已用内置默认"]
    out: Dict[str, Any] = {}

    weights_raw = raw.get("weights")
    weights = dict(default["weights"])
    if isinstance(weights_raw, dict) and all(k in weights_raw for k in _RISK_WEIGHT_KEYS) \
            and all(_is_num(weights_raw[k]) and float(weights_raw[k]) >= 0
                    for k in _RISK_WEIGHT_KEYS):
        total = sum(float(weights_raw[k]) for k in _RISK_WEIGHT_KEYS)
        if abs(total - 1.0) < 1e-6:
            weights = {k: float(weights_raw[k]) for k in _RISK_WEIGHT_KEYS}
        else:
            issues.append(f"risk.weights 总和为 {total:g} 而非 1，已用内置默认权重")
    else:
        issues.append("risk.weights 缺少键或含负值，已用内置默认权重")
    out["weights"] = weights

    out["breadth_full_sources"] = _pos_int(
        raw.get("breadth_full_sources"), default["breadth_full_sources"], minimum=1)

    levels = dict(default["levels"])
    levels_raw = raw.get("levels")
    if isinstance(levels_raw, dict):
        for key in _RISK_LEVEL_KEYS:
            if key in levels_raw and _is_num(levels_raw[key]) \
                    and 0 <= float(levels_raw[key]) <= 100:
                levels[key] = float(levels_raw[key])
    if not (levels["suspect"] <= levels["high"] <= levels["urgent"]):
        levels = dict(default["levels"])
        issues.append("risk.levels 阈值不单调（suspect<=high<=urgent），已用内置默认")
    out["levels"] = levels

    floors: List[Dict[str, Any]] = []
    floors_raw = raw.get("floors")
    if isinstance(floors_raw, list):
        for item in floors_raw:
            if not isinstance(item, dict):
                continue
            level = item.get("level")
            if level not in _RISK_LEVEL_KEYS:
                continue
            floor: Dict[str, Any] = {"level": level}
            for key in ("min_rumor_score", "min_breadth", "min_growth", "min_heat"):
                if key in item and _is_num(item[key]):
                    floor[key] = float(item[key])
            floors.append(floor)
    out["floors"] = floors or json.loads(json.dumps(default["floors"]))
    return out, issues


def _normalize_queries(raw: Any) -> List[Dict[str, Any]]:
    """watch_queries 段：元素必须是含 query 的对象/字符串；坏元素跳过。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            q = item.strip()
            if q:
                out.append({"query": q, "enabled": True, "note": ""})
        elif isinstance(item, dict):
            q = str(item.get("query") or "").strip()
            if q:
                out.append({"query": q,
                            "enabled": bool(item.get("enabled", True)),
                            "note": str(item.get("note") or "")})
    return out


def _normalize_rss(raw: Any) -> List[Dict[str, Any]]:
    """rss_sources 段：元素必须含 http(s) URL；坏元素跳过。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        out.append({"name": str(item.get("name") or url)[:80], "url": url,
                    "enabled": bool(item.get("enabled", True))})
    return out


def _report_int(raw: Dict[str, Any], key: str, default: Dict[str, Any],
                lo: int, hi: int, issues: List[str]) -> int:
    """report 段整数键：坏类型退默认 + issue；越界夹取 + issue。"""
    value = raw.get(key, default[key])
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(f"report.{key} 必须是 [{lo},{hi}] 内的整数，已用内置默认")
        return default[key]
    if value < lo or value > hi:
        issues.append(f"report.{key}={value} 超出 [{lo},{hi}]，已夹取")
        return min(max(value, lo), hi)
    return value


def _report_num(raw: Dict[str, Any], key: str, default: Dict[str, Any],
                lo: float, hi: float, issues: List[str]) -> Any:
    """report 段数值键（int/float 均可）：坏类型退默认 + issue；越界夹取 + issue。"""
    value = raw.get(key, default[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        issues.append(f"report.{key} 必须是 [{lo:g},{hi:g}] 内的数值，已用内置默认")
        return default[key]
    if value < lo or value > hi:
        issues.append(f"report.{key}={value} 超出 [{lo:g},{hi:g}]，已夹取")
        return lo if value < lo else hi
    return value


def _normalize_report(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    """report 段规整（巡检报告技能口径，platform/report）。

    - 段缺失（None）→ 内置默认，**不告警**（报告是可选能力，默认开启由段内
      enabled 自己控制，缺段不等于配置错误）；
    - 段不是对象 → 默认 + issue；
    - 逐键校验：enabled 必须 bool；max_llm_calls int 夹取 [1,5]；max_entries int
      [1,50]；max_chars_per_entry int [50,2000]；deadline_s num [10,600]；
      max_tokens int [200,4000]。坏类型退默认 + issue，越界夹取 + issue。
    """
    default = DEFAULT_RULES["report"]
    if raw is None:
        return json.loads(json.dumps(default)), []
    if not isinstance(raw, dict):
        return json.loads(json.dumps(default)), ["report 段不是对象，已用内置默认"]
    out: Dict[str, Any] = {}
    issues: List[str] = []

    enabled = raw.get("enabled", default["enabled"])
    if not isinstance(enabled, bool):
        issues.append("report.enabled 必须为布尔值，已用内置默认")
        enabled = default["enabled"]
    out["enabled"] = enabled
    out["max_llm_calls"] = _report_int(raw, "max_llm_calls", default, 1, 5, issues)
    out["max_entries"] = _report_int(raw, "max_entries", default, 1, 50, issues)
    out["max_chars_per_entry"] = _report_int(raw, "max_chars_per_entry", default,
                                             50, 2000, issues)
    out["deadline_s"] = _report_num(raw, "deadline_s", default, 10, 600, issues)
    out["max_tokens"] = _report_int(raw, "max_tokens", default, 200, 4000, issues)
    return out, issues


def _normalize(data: Any) -> Tuple[Dict[str, Any], List[str]]:
    """把加载到的配置规整成合法结构；段级降级（某段坏只退那一段）。"""
    rules: Dict[str, Any] = {}
    issues: List[str] = []
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULT_RULES)), ["平台规则根节点必须是对象，已全部退回内置默认"]

    rules["enabled"] = bool(data.get("enabled", DEFAULT_RULES["enabled"]))
    rules["scan_interval_s"] = _pos_int(
        data.get("scan_interval_s"), DEFAULT_RULES["scan_interval_s"], minimum=10)
    rules["max_queries_per_scan"] = _pos_int(
        data.get("max_queries_per_scan"), DEFAULT_RULES["max_queries_per_scan"], minimum=1)
    rules["max_candidates_per_query"] = _pos_int(
        data.get("max_candidates_per_query"), DEFAULT_RULES["max_candidates_per_query"],
        minimum=1)

    screen = data.get("screen")
    screen_default = DEFAULT_RULES["screen"]
    if isinstance(screen, dict):
        rules["screen"] = {"heat_screen_e": _num(
            screen.get("heat_screen_e"), screen_default["heat_screen_e"], minimum=0.0)}
    else:
        rules["screen"] = dict(screen_default)
        issues.append("screen 段不是对象，已用内置默认")

    verify = data.get("deep_verify")
    verify_default = DEFAULT_RULES["deep_verify"]
    if isinstance(verify, dict):
        rules["deep_verify"] = {
            "max_per_scan": _pos_int(verify.get("max_per_scan"),
                                     verify_default["max_per_scan"], minimum=0),
            "deadline_s": _num(verify.get("deadline_s"), verify_default["deadline_s"],
                               minimum=1.0),
        }
    else:
        rules["deep_verify"] = dict(verify_default)
        issues.append("deep_verify 段不是对象，已用内置默认")

    dedup = data.get("dedup")
    dedup_default = DEFAULT_RULES["dedup"]
    if isinstance(dedup, dict):
        rules["dedup"] = {"fingerprint_threshold": _num(
            dedup.get("fingerprint_threshold"), dedup_default["fingerprint_threshold"])}
    else:
        rules["dedup"] = dict(dedup_default)
        issues.append("dedup 段不是对象，已用内置默认")

    risk, risk_issues = _normalize_risk(data.get("risk"))
    rules["risk"] = risk
    issues.extend(risk_issues)

    # report 段：可选段，缺失不告警（见 _normalize_report）
    report, report_issues = _normalize_report(data.get("report"))
    rules["report"] = report
    issues.extend(report_issues)

    rules["watch_queries"] = _normalize_queries(data.get("watch_queries"))
    rules["rss_sources"] = _normalize_rss(data.get("rss_sources"))
    return rules, issues


def load_rules(path: Optional[str] = None) -> Tuple[Dict[str, Any], List[str]]:
    """加载平台规则，返回 (rules, warnings)。**永不抛异常。**"""
    from mylocalai.config import config_loader

    target = Path(path) if path is not None else RULES_PATH
    warnings = _missing_section_warnings(target)
    try:
        data, loader_warnings = config_loader.load_config(
            str(target), builtin=json.loads(json.dumps(DEFAULT_RULES)),
            required_sections=_REQUIRED_SECTIONS,
            env_full="PLATFORM_RULES_PATH",
            env_extra="PLATFORM_RULES_EXTRA_PATH",
        )
        warnings.extend(loader_warnings.values())
        rules, issues = _normalize(data)
        warnings.extend(issues)
        return rules, warnings
    except Exception as exc:  # noqa: BLE001  加载器自身异常也不能炸调用方
        return json.loads(json.dumps(DEFAULT_RULES)), \
            [f"平台规则加载异常（{type(exc).__name__}: {exc}），已用内置默认"]
