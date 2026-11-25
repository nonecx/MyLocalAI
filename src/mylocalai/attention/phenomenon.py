# -*- coding: utf-8 -*-
"""关注度「现象判读」（Attention Phenomenon）：在 E 分之外回答「是哪一种热」。

`attention.py` 的 E 是**标量**，只回答"多热"；同一组 x/y/z 派生出的三个比值回答
"是哪一种热"：

    spread            = x        （传播广度：有多少篇文章在说）
    engagement_ratio  = z / y    （参与深度：读的人里多大比例留言）
    resonance         = z / x    （共鸣强度：单篇平均留言）

三个比值目前只喂给深读的 LLM 提示词，界面上看不到形态判读。本模块把比值翻成
**五档封闭形态 + 无法判读**，纯函数、零 LLM、零网络：

| archetype      | 语义             | 判据（阈值外置） |
| -------------- | ---------------- | ---------------- |
| `broad_deep`    | 全网热议         | 广度达标 且 参与深度达标（单篇共鸣达标为加强项） |
| `broad_shallow` | 广而浅·媒体议题  | 广度达标 且 参与深度未达标 |
| `narrow_deep`   | 窄而深·圈层激辩  | 广度未达标 且（深度达标 或 单篇共鸣达标） |
| `single_point`  | 单点引爆         | 广度未达标 且（单篇触达极高 或 单篇留言极高） |
| `low_attention` | 低度关注         | 广度、深度、单篇共鸣均未达标 |
| `unknown`       | 无法判读         | 输入不足（x<=0）或输入非法（非有限实数/负数），不猜 |

**同一个 E 分、不同形态必须判出不同结论**：判据只看比值形态，不看 E；
`tests/test_attention_phenomenon.py` 用五组 E≈48.53 的输入锁定这一点
（E 是标量，形态是结构，两者正交）。

口径诚实（与全仓库「代理/估计值」约定一致）：
  * 判读句固定带「同口径相对 · 代理估计」限定，禁止"全社会广泛关注"这类绝对断言；
  * y<=0（缺阅读量）时参与深度不可判，按广度与单篇共鸣**降级判读**，置信度降为 low；
  * z<=0 时留言量可能确实为 0、也可能是取不到，置信度降为 medium；
  * x<=0（无文章数）时不猜，直接 `unknown`。

退化输入一律给 `unknown` 或安全降级，**绝不抛异常、绝不返回 inf/nan**：
非有限实数（inf/nan）、负数、bool、非数值、超出 float 范围的大整数都在入口拦下。
阈值、档位名外置在 `attn_rules.json` 的 `phenomenon` 段；文件缺失、JSON 写坏、
结构不合法时退回内置默认，并把中文告警写进返回的 `basis`（卡片上可展开看到），
与 `attention.load_params` 的 fail-soft 约定一致。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Sequence

from mylocalai.attention.attention import derive_signals

# 形态是**封闭枚举**：下游只能拿到这六个值之一（unknown 也是合法出口，不猜）。
ARCHETYPES: tuple[str, ...] = (
    "broad_deep",
    "broad_shallow",
    "narrow_deep",
    "single_point",
    "low_attention",
    "unknown",
)

# 内置默认阈值（与 attn_rules.json 的 phenomenon.thresholds 一致；写坏时退回这里）。
# 选值依据：
#   * spread_min=20：20 篇以上算"多篇在说"（检索 k=8 的话题模式通常拿不到 20 篇，
#     这本身就是"广度未达标"的诚实信号，不是 bug）；
#   * engagement_min=0.002：读的人里 ≥0.2% 会留言，才算"参与深"；
#   * resonance_min=5：单篇平均 ≥5 条留言，才算"单篇共鸣高"；
#   * single_point_reach=1e6：单篇平均触达 ≥100 万，算"一篇文章引爆"；
#   * single_point_resonance=500：单篇平均 ≥500 条留言，同样算单点引爆。
DEFAULT_THRESHOLDS: dict[str, float] = {
    "spread_min": 20.0,
    "engagement_min": 0.002,
    "resonance_min": 5.0,
    "single_point_reach": 1_000_000.0,
    "single_point_resonance": 500.0,
}

THRESHOLD_KEYS: tuple[str, ...] = tuple(DEFAULT_THRESHOLDS)

# 档位名（中文短语）：外置在 phenomenon.labels，改文案不改代码。
DEFAULT_LABELS: dict[str, str] = {
    "broad_deep": "全网热议",
    "broad_shallow": "广而浅·媒体议题",
    "narrow_deep": "窄而深·圈层激辩",
    "single_point": "单点引爆",
    "low_attention": "低度关注",
    "unknown": "无法判读",
}

DEFAULT_RULES: dict[str, dict] = {
    "thresholds": dict(DEFAULT_THRESHOLDS),
    "labels": dict(DEFAULT_LABELS),
}

PHENOMENON_RULES_PATH: Path = Path(__file__).resolve().parent / "attn_rules.json"

# 各形态的判读核心句（前缀统一加「同口径相对 · 代理估计」，禁止绝对断言）。
_CORE_SENTENCE: dict[str, str] = {
    "broad_deep": "多篇文章在说、阅读者参与留言的比例也高，是「多篇 + 深参与」的广泛热议形态。",
    "broad_shallow": "文章数量不少，但阅读者基本不留言，更像媒体议题铺开、公众参与有限。",
    "narrow_deep": "说的人不多，但参与留言的比例或单篇留言量偏高，像小圈层内部的激烈讨论。",
    "single_point": "文章数量少，但单篇触达或单篇留言极高，是一篇文章引爆，而非全网铺开。",
    "low_attention": "文章数量、参与深度、单篇共鸣都偏低，目前仍属低度关注。",
    "unknown": "文章数或阅读量不足，无法判读形态；不猜。",
}

_SENTENCE_PREFIX = "同口径相对 · 代理估计："

# 降级说明（附在判读句末尾，让读者知道这条判读是基于不完整输入的）。
_DEGRADED_SUFFIX: dict[str, str] = {
    "y_missing": "（阅读量缺失，参与深度不可判，本条为降级判读）",
    "z_missing": "（留言量为 0 或缺失，参与深度与共鸣按 0 处理）",
}


def _is_real(value: object) -> bool:
    """判断是否是实数（int/float），并显式排除 bool（与 attention._is_real 同口径）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt(value: float) -> str:
    """把数值格式化成卡片上可读的紧凑文本（30.0→"30"、0.002→"0.002"、1e6→"1e+06"）。"""
    return f"{float(value):g}"


def _default_rules() -> dict:
    """返回内置默认口径的深拷贝（调用方可以随意改返回值，不污染模块常量）。"""
    return {
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "labels": dict(DEFAULT_LABELS),
    }


def validate_thresholds(thresholds: object) -> list[str]:
    """校验阈值段，返回中文问题描述列表；空列表表示合法。

    检查项：必须是 dict；五个键齐全且为有限正实数（bool 不算）；
    单点阈值必须严格高于普通达标阈值（否则 single_point / narrow_deep 的判据会自相矛盾）。
    """
    issues: list[str] = []
    if not isinstance(thresholds, dict):
        return [f"phenomenon.thresholds 必须是对象，实际是 {type(thresholds).__name__}"]

    values: dict[str, float] = {}
    for key in THRESHOLD_KEYS:
        if key not in thresholds:
            issues.append(f"缺少阈值 {key}")
            continue
        raw = thresholds[key]
        if not _is_real(raw):
            issues.append(f"阈值 {key} 必须是实数（int/float，bool 不算），"
                          f"实际是 {type(raw).__name__}: {_safe_repr(raw)}")
            continue
        try:
            number = float(raw)
        except (OverflowError, ValueError):
            issues.append(f"阈值 {key} 超出实数范围，实际是 {_safe_repr(raw)}")
            continue
        if not math.isfinite(number):
            issues.append(f"阈值 {key} 必须是有限实数，实际是 {_safe_repr(raw)}")
            continue
        if number <= 0:
            issues.append(f"阈值 {key} 必须 > 0，实际是 {_safe_repr(raw)}")
            continue
        values[key] = number

    if all(key in values for key in ("resonance_min", "single_point_resonance")):
        if values["single_point_resonance"] <= values["resonance_min"]:
            issues.append(
                f"阈值 single_point_resonance（{values['single_point_resonance']}）"
                f"必须大于 resonance_min（{values['resonance_min']}）")
    return issues


def _resolve_labels(labels: object) -> tuple[dict[str, str], list[str]]:
    """把配置里的档位名合并到内置默认上；单条写坏只退该条并留告警。"""
    resolved = dict(DEFAULT_LABELS)
    warnings: list[str] = []
    if labels is None:
        return resolved, warnings
    if not isinstance(labels, dict):
        return resolved, [
            f"phenomenon.labels 必须是对象，实际是 {type(labels).__name__}，已用内置默认档位名"]
    for archetype in ARCHETYPES:
        if archetype not in labels:
            continue
        text = labels[archetype]
        if isinstance(text, str) and text.strip():
            resolved[archetype] = text.strip()
        else:
            warnings.append(
                f"phenomenon.labels.{archetype} 必须是非空字符串，实际是 {text!r}，已用内置默认档位名")
    return resolved, warnings


def load_phenomenon_rules(path: str | os.PathLike | None = None) -> tuple[dict, list[str]]:
    """读取外置判读口径，返回 (rules, warnings)。

    `path` 为 None 时用包内 `attn_rules.json`。文件不存在、JSON 语法错、结构不合法、
    阈值/档位名写坏时，**永不抛异常**：能退的退到内置默认，并返回中文告警
    （与 attention.load_params 的既有约定一致；配置写坏不能拖垮卡片渲染）。
    """
    target = Path(path) if path is not None else PHENOMENON_RULES_PATH
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _default_rules(), [f"关注度现象判读口径文件未找到（{target}），已用内置默认阈值"]
    except json.JSONDecodeError as exc:
        return _default_rules(), [
            f"关注度现象判读口径文件 JSON 语法错误（{target}）：{exc}，已用内置默认阈值"]
    except OSError as exc:
        return _default_rules(), [
            f"关注度现象判读口径文件读取失败（{target}）：{type(exc).__name__}: {exc}，已用内置默认阈值"]
    except Exception as exc:  # noqa: BLE001  坏配置不能拖垮卡片渲染
        return _default_rules(), [
            f"关注度现象判读口径文件无法解析（{target}）：{type(exc).__name__}: {exc}，已用内置默认阈值"]

    if not isinstance(data, dict):
        return _default_rules(), [
            f"关注度现象判读口径文件根节点必须是对象，实际是 {type(data).__name__}，已用内置默认阈值"]

    raw = data.get("phenomenon")
    if not isinstance(raw, dict):
        return _default_rules(), [
            "关注度口径文件缺少 phenomenon 段（或 phenomenon 不是对象），已用内置默认阈值与档位名"]

    warnings: list[str] = []
    thresholds = raw.get("thresholds")
    issues = validate_thresholds(thresholds)
    if issues:
        return _default_rules(), [
            "关注度现象判读 phenomenon.thresholds 不合法：" + "；".join(issues)
            + "，已用内置默认阈值"]

    labels, label_warnings = _resolve_labels(raw.get("labels"))
    warnings.extend(label_warnings)
    return {
        "thresholds": {key: float(thresholds[key]) for key in THRESHOLD_KEYS},
        "labels": labels,
    }, warnings


def _safe_repr(value: object, limit: int = 48) -> str:
    """安全的短 repr：坏对象的 __repr__ 抛错、超大整数都只影响这一条依据，不拖垮判读。"""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001  坏对象的 __repr__ 不该让判读抛
        text = f"<{type(value).__name__} 对象>"
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _coerce_point(name: str, value: object) -> tuple[float | None, str]:
    """把 x/y/z 规整成有限非负 float；非法时返回 (None, 中文原因)，**不抛异常**。"""
    if not _is_real(value):
        return None, f"{name}={_safe_repr(value)}（不是实数，按无法判读处理）"
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None, f"{name}={_safe_repr(value)}（超出实数范围，按无法判读处理）"
    if not math.isfinite(number):
        return None, f"{name}={_safe_repr(value)}（非有限实数，按无法判读处理）"
    if number < 0:
        return None, f"{name}={_safe_repr(value)}（负数，按无法判读处理）"
    return number, ""


def _comparison(expr: str, value: float, threshold: float,
                met: bool, met_word: str, unmet_word: str) -> str:
    """生成一条可复算的判读依据，如 `x=30 ≥ 20（广度达标）`。"""
    sign = "≥" if met else "<"
    return f"{expr}={_fmt(value)} {sign} {_fmt(threshold)}（{met_word if met else unmet_word}）"


def _derive_ratios(x: float, y: float, z: float,
                   params: dict | None, profile: str | None,
                   proxy_flags: Sequence[str] | None) -> tuple[dict[str, float], list[str]]:
    """复用 `derive_signals` 派生三个比值（绝不另写一套口径）。

    返回值只保留 phenomenon 需要的三项；params 非法等极端情况下退回默认参数口径，
    仍走同一个 `derive_signals`，并留一条中文告警。
    """
    warnings: list[str] = []
    try:
        signals = derive_signals(x, y, z, params, profile, proxy_flags)
    except Exception:  # noqa: BLE001  判读是附加信息，不能因 params 写坏而抛
        warnings.append("传入的 params 不合法，比值已按默认参数口径派生")
        try:
            signals = derive_signals(x, y, z, None, profile, proxy_flags)
        except Exception:  # noqa: BLE001  理论上不会到这里；兜底给 0，绝不抛
            return {"spread": 0.0, "engagement_ratio": 0.0, "resonance": 0.0}, warnings
    return {
        "spread": float(signals.get("spread", 0.0)),
        "engagement_ratio": float(signals.get("engagement_ratio", 0.0)),
        "resonance": float(signals.get("resonance", 0.0)),
    }, warnings


def classify_phenomenon(x: float, y: float, z: float, *,
                        params: dict | None = None,
                        profile: str | None = None,
                        proxy_flags: Sequence[str] | None = None) -> dict:
    """把 x/y/z 判读成一种关注度形态（纯函数，零 LLM、零网络、绝不抛异常）。

    参数:
        x: 文章数量（传播广度）
        y: 文章阅读量（参与深度的分母）
        z: 文章留言量
        params: 可选的 E 公式参数（只影响 derive_signals 的份额，不影响三个比值）；
                非法时退回默认参数口径并留告警
        profile: 参数档位名（原样回显，供下游标注口径）
        proxy_flags: 代理/估计值标记（原样回显，如 ["y_proxy", "z_proxy"]）

    返回 dict（键固定，便于单测逐项断言）:
        archetype   封闭枚举之一（见 `ARCHETYPES`）
        label       中文短语（外置可改，默认见 `DEFAULT_LABELS`）
        sentence    一句人话判读，必带「同口径相对 · 代理估计」限定
        basis       判读依据字符串列表（卡片可折叠展示，供答辩复算）
        ratios      {"spread", "engagement_ratio", "resonance"}，与 derive_signals 同口径
        confidence  "high" / "medium" / "low"
        profile     原样回显
        proxy_flags 原样回显

    退化输入（全 0、x<=0、y<=0、z<=0、负数、inf/nan、bool、非数值）不抛异常：
    给 `unknown` 或按广度/共鸣降级判读，置信度相应降级。配置写坏的告警以
    「口径告警：」开头写进 `basis`，不新增返回键。
    """
    rules, config_warnings = load_phenomenon_rules()
    thresholds = rules["thresholds"]
    labels = rules["labels"]

    basis: list[str] = [f"口径告警：{text}" for text in config_warnings]
    invalid: list[str] = []
    points: dict[str, float | None] = {}
    for name, value in (("x", x), ("y", y), ("z", z)):
        number, issue = _coerce_point(name, value)
        points[name] = number
        if number is None:
            invalid.append(issue)

    echo_flags = [str(flag) for flag in (proxy_flags or [])]

    if invalid:
        # 非法输入：不猜形态，比值一律给 0（绝不返回 inf/nan）
        basis.extend(invalid)
        return {
            "archetype": "unknown",
            "label": labels["unknown"],
            "sentence": _SENTENCE_PREFIX + _CORE_SENTENCE["unknown"],
            "basis": basis,
            "ratios": {"spread": 0.0, "engagement_ratio": 0.0, "resonance": 0.0},
            "confidence": "low",
            "profile": profile,
            "proxy_flags": echo_flags,
        }

    px, py, pz = float(points["x"]), float(points["y"]), float(points["z"])
    ratios, ratio_warnings = _derive_ratios(px, py, pz, params, profile, proxy_flags)
    basis.extend(f"口径告警：{text}" for text in ratio_warnings)

    if px <= 0:
        # 没有文章数就没有"传播广度"，形态无从谈起：不猜。
        basis.append(f"x={_fmt(px)}（文章数缺失，无法判读形态）")
        return {
            "archetype": "unknown",
            "label": labels["unknown"],
            "sentence": _SENTENCE_PREFIX + _CORE_SENTENCE["unknown"],
            "basis": basis,
            "ratios": ratios,
            "confidence": "low",
            "profile": profile,
            "proxy_flags": echo_flags,
        }

    spread_ok = px >= thresholds["spread_min"]
    depth_known = py > 0
    engagement = ratios["engagement_ratio"] if depth_known else 0.0
    depth_ok = depth_known and engagement >= thresholds["engagement_min"]
    resonance = ratios["resonance"]
    resonance_ok = resonance >= thresholds["resonance_min"]
    reach = py / px if depth_known else 0.0
    reach_ok = depth_known and reach >= thresholds["single_point_reach"]
    peak_ok = resonance >= thresholds["single_point_resonance"]
    single_hit = (not spread_ok) and (reach_ok or peak_ok)

    basis.append(_comparison("x", px, thresholds["spread_min"],
                             spread_ok, "广度达标", "广度不足"))
    if depth_known:
        basis.append(_comparison("z/y", engagement, thresholds["engagement_min"],
                                 depth_ok, "参与深度达标", "参与深度低"))
    else:
        basis.append(f"y={_fmt(py)}（阅读量缺失：参与深度不可判，按广度与单篇共鸣降级判读）")
    basis.append(_comparison("z/x", resonance, thresholds["resonance_min"],
                             resonance_ok, "单篇共鸣高", "单篇共鸣低"))
    if pz <= 0 and depth_known:
        basis.append(f"z={_fmt(pz)}（留言量为 0 或缺失，参与深度与共鸣按 0 处理）")
    if not spread_ok:
        # 只有广度未达标时，单点信号才是区分 single_point / narrow_deep 的关键，列出来供复算
        basis.append(_comparison("y/x", reach, thresholds["single_point_reach"],
                                 reach_ok, "单篇触达极高", "单篇触达未达单点阈值"))
        basis.append(_comparison("z/x", resonance, thresholds["single_point_resonance"],
                                 peak_ok, "单篇留言极高", "单篇留言未达单点阈值"))

    if spread_ok and depth_ok:
        archetype = "broad_deep"
    elif spread_ok:
        # 阅读量缺失但单篇共鸣高：无法区分"广而深"与"广而浅"，按共鸣降级判为广深
        archetype = "broad_deep" if (not depth_known and resonance_ok) else "broad_shallow"
    elif single_hit:
        archetype = "single_point"
    elif depth_ok or resonance_ok:
        archetype = "narrow_deep"
    else:
        archetype = "low_attention"

    if not depth_known:
        confidence = "low"
    elif pz <= 0:
        confidence = "medium"
    else:
        confidence = "high"

    sentence = _SENTENCE_PREFIX + _CORE_SENTENCE[archetype]
    if not depth_known:
        sentence += _DEGRADED_SUFFIX["y_missing"]
    elif pz <= 0:
        sentence += _DEGRADED_SUFFIX["z_missing"]

    return {
        "archetype": archetype,
        "label": labels[archetype],
        "sentence": sentence,
        "basis": basis,
        "ratios": ratios,
        "confidence": confidence,
        "profile": profile,
        "proxy_flags": echo_flags,
    }