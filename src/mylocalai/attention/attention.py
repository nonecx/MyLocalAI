# -*- coding: utf-8 -*-
"""关注度（Attention）评分：纯确定性口径，**分数由代码算，大模型不参与**。

公式（逐字实现，不得改写）：

    E = 100 * (1 - exp(-lambda * S))
    S = w_x*ln(1+x)/ln(1+x0) + w_y*ln(1+y)/ln(1+y0) + w_z*ln(1+z)/ln(1+z0) + epsilon

变量含义：
    x = 文章数量，y = 文章阅读量，z = 文章留言量；x0/y0/z0 为参考基准值（分母锚点）。

默认参数（见 `DEFAULT_PARAMS`）：
    x0=100, y0=100000, z0=1000, w_x=0.2, w_y=0.5, w_z=0.3, lambda=0.7, epsilon=1e-6

硬约束与性质：
    * w_x,w_y,w_z 均 > 0 且 w_x+w_y+w_z = 1；x,y,z >= 0；
    * 返回值严格落在开区间 (0, 100)：`epsilon` 保证 x=y=z=0 时分数仍严格 > 0，
      指数衰减保证分数随规模单调递增但永远取不到 100（不存在"恰好 100"的写法）；
    * ln(1+v) 用 `math.log1p(v)`（大数下比 log(1+v) 更精确），指数用 `math.exp`；
    * 为守住开区间 (0, 100)，最终分数按等价的 ``100 - 100*exp(exponent)`` 计算：
      直接写 ``100*(1-exp(-u))`` 在 double 下当 u > 53*ln2 ≈ 36.74 会舍入成恰好
      100.0（x=y=z=1e300 即触发），极小 u 下又会舍入成 0.0，均违反硬约束；
      越界时取 `math.nextafter` 给出的最接近合法值，并用显式 if 校验
      （不用 assert：`python -O` 下 assert 会被整段剔除）。

本模块只用标准库 `math` / `json`，**不联网、不调用任何大模型**：同一组输入在离线环境
下必然复现同一分数，可被单元测试精确断言。口径（参数、档位、深读阈值）外置在
`attn_rules.json`；文件缺失、JSON 写坏或结构不合法时 `load_params` 退回内置
`DEFAULT_PARAMS` 并留中文告警，绝不抛异常（配置写坏不能拖垮报告生成链路）；
而调用方传错 x/y/z 或显式传入非法 params 时，则显式抛 `AttentionInputError` /
`AttentionParamError`。

定标点（可用于回归核对）：
    E(100, 100000, 1000)  ≈ 50.3415
    E(0, 0, 0)            ≈ 7.0e-05（严格 > 0）
    E(1e12, 1e12, 1e12)   ≈ 91.9385（严格 < 100）
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Sequence

# ---- 常量（口径） ----

DEFAULT_PARAMS: dict[str, float] = {
    "x0": 100.0,
    "y0": 100000.0,
    "z0": 1000.0,
    "w_x": 0.2,
    "w_y": 0.5,
    "w_z": 0.3,
    "lambda": 0.7,
    "epsilon": 1e-06,
}

PARAM_KEYS: tuple[str, ...] = ("x0", "y0", "z0", "w_x", "w_y", "w_z", "lambda", "epsilon")

# 与 attn_rules.json 的 bands 一致（按 min 降序）
DEFAULT_BANDS: list[dict] = [
    {"min": 66.0, "label": "高关注"},
    {"min": 50.0, "label": "中关注"},
    {"min": 33.0, "label": "低关注"},
    {"min": 0.0, "label": "冷启动"},
]

DEEP_READ_THRESHOLD: float = 66.0

RULES_PATH: Path = Path(__file__).resolve().parent / "attn_rules.json"

# ---- 参数档位（profile）----
# 阈值 66 在默认参数下偏高：本批 5 个真实头部话题 0/5 触发。根因是
# `ln(1+v0)/w` 三维几乎相等（偏差 0.216%），公式等价于纯乘积预算
# `GM(x,y,z) >= 138401`，任何单项暴涨都被另外两项的稀疏抵消。
# 因此"只调阈值不改参数"无效（阈值 55~70 对本批话题触发数全为 0），
# 必须改变三个维度的**每数量级价格** H_v = ln(1+v0)/w。
#
# 这里预置两组候选，但**默认仍是用户指定的原始参数**（profile "spec"），
# 因为任何调参都会改变全部分数，属于需要论证的产品决策而非技术修复。
# 想切换时用 load_profile(name) 或在配置里设 "active_profile"。
PROFILES: dict[str, dict] = {
    "spec": {
        "_comment": "用户指定的原始默认参数（本项目的基准口径）。",
        "params": dict(DEFAULT_PARAMS),
        "bands": [dict(b) for b in DEFAULT_BANDS],
        "deep_read_threshold": DEEP_READ_THRESHOLD,
    },
    "reachable": {
        "_comment": "候选档：把三维每数量级价格拉开，让真实头部话题可达到 66 而不让中腰部轻易触发。"
                    "y0 提高到 1e6 抑制 y 的绝对量级优势；z0 降到 300 鼓励参与深度；"
                    "w 改为 (0.3,0.4,0.3) 提升 x 权重；lambda 0.7→0.8 收紧饱和。"
                    "实测 term_y/S 占比从 67.7%~85.2% 降到约 51%~73%。",
        "params": {"x0": 100.0, "y0": 1000000.0, "z0": 300.0,
                   "w_x": 0.3, "w_y": 0.4, "w_z": 0.3,
                   "lambda": 0.8, "epsilon": 1e-06},
        "bands": [{"min": 66.0, "label": "高关注"}, {"min": 50.0, "label": "中关注"},
                  {"min": 33.0, "label": "低关注"}, {"min": 0.0, "label": "冷启动"}],
        "deep_read_threshold": 66.0,
    },
}

DEFAULT_PROFILE = "spec"

_USAGE = ("用法：python -m mylocalai.attention.attention --x 1000 --y 10000000 --z 50000 "
          "[--json] [--rules PATH] [--profile spec|reachable]")


class AttentionError(ValueError):
    """关注度模块的错误基类（继承 ValueError，便于调用方统一兜住）。"""


class AttentionParamError(AttentionError):
    """参数不合法：权重和不为 1、基准值/衰减系数非正、缺键、非有限实数等。"""


class AttentionInputError(AttentionError):
    """x / y / z（或分数）取值不合法：bool、非数值、非有限、负数等。"""


def _is_real(value: object) -> bool:
    """判断是否是实数（int/float），并显式排除 bool。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_params(params: object) -> list[str]:
    """校验参数字典，返回中文问题描述列表；空列表表示合法。

    检查项：必须是 dict；八个键齐全且为有限实数（int/float，bool 不算）；
    x0,y0,z0 > 0；w_x,w_y,w_z > 0 且 |w_x+w_y+w_z-1| <= 1e-9；lambda > 0；epsilon > 0。
    """
    issues: list[str] = []
    if not isinstance(params, dict):
        return [f"参数必须是 dict（映射），实际是 {type(params).__name__}"]

    values: dict[str, float] = {}
    for key in PARAM_KEYS:
        if key not in params:
            issues.append(f"缺少参数 {key}")
            continue
        value = params[key]
        if not _is_real(value):
            issues.append(f"参数 {key} 必须是实数（int/float，bool 不算），"
                          f"实际是 {type(value).__name__}: {value!r}")
            continue
        number = float(value)
        if not math.isfinite(number):
            issues.append(f"参数 {key} 必须是有限实数，实际是 {value!r}")
            continue
        values[key] = number

    for key in ("x0", "y0", "z0"):
        if key in values and values[key] <= 0:
            issues.append(f"基准值 {key} 必须 > 0，实际是 {values[key]}")

    for key in ("w_x", "w_y", "w_z"):
        if key in values and values[key] <= 0:
            issues.append(f"权重 {key} 必须 > 0，实际是 {values[key]}")

    if all(key in values for key in ("w_x", "w_y", "w_z")):
        total = values["w_x"] + values["w_y"] + values["w_z"]
        if abs(total - 1.0) > 1e-9:
            issues.append(f"权重之和必须等于 1，实际是 {total}（w_x+w_y+w_z）")

    if "lambda" in values and values["lambda"] <= 0:
        issues.append(f"衰减系数 lambda 必须 > 0，实际是 {values['lambda']}")

    if "epsilon" in values and values["epsilon"] <= 0:
        issues.append(f"epsilon 必须 > 0，实际是 {values['epsilon']}")

    return issues


def load_params(path: str | os.PathLike | None = None) -> tuple[dict[str, float], list[str]]:
    """读取外置口径文件，返回 (params, warnings)。

    `path` 为 None 时用包内 `attn_rules.json`。文件不存在、JSON 语法错、结构不合法时，
    返回 `DEFAULT_PARAMS` 的副本 + 中文告警，**永不抛异常**（项目既有约定：配置写坏
    退回默认并留告警）。成功且合法时 warnings 为空列表。
    """
    target = Path(path) if path is not None else RULES_PATH
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return dict(DEFAULT_PARAMS), [f"关注度口径文件未找到（{target}），已用内置默认参数"]
    except json.JSONDecodeError as exc:
        return dict(DEFAULT_PARAMS), [
            f"关注度口径文件 JSON 语法错误（{target}）：{exc}，已用内置默认参数"]
    except OSError as exc:
        return dict(DEFAULT_PARAMS), [
            f"关注度口径文件读取失败（{target}）：{type(exc).__name__}: {exc}，已用内置默认参数"]
    except Exception as exc:  # noqa: BLE001  坏配置不能拖垮报告生成链路
        return dict(DEFAULT_PARAMS), [
            f"关注度口径文件无法解析（{target}）：{type(exc).__name__}: {exc}，已用内置默认参数"]

    if not isinstance(data, dict):
        return dict(DEFAULT_PARAMS), [
            f"关注度口径文件根节点必须是对象，实际是 {type(data).__name__}，已用内置默认参数"]

    raw = data.get("params")
    if not isinstance(raw, dict):
        return dict(DEFAULT_PARAMS), [
            "关注度口径文件缺少 params 段（或 params 不是对象），已用内置默认参数"]

    params: dict[str, object] = {key: raw[key] for key in PARAM_KEYS if key in raw}
    issues = validate_params(params)
    if issues:
        return dict(DEFAULT_PARAMS), [
            "关注度口径文件 params 不合法：" + "；".join(issues) + "，已用内置默认参数"]

    return {key: float(params[key]) for key in PARAM_KEYS}, []


def load_profile(name: str | None = None,
                 path: str | os.PathLike | None = None) -> tuple[dict[str, float], list[str]]:
    """按档位名取参数，返回 (params, warnings)。

    优先级（从高到低）：
      1. 显式 `name` 指定的档位
      2. 配置文件 `active_profile` 指定的档位
      3. 配置文件里的 `params` 段（用户显式写下的参数）
      4. `DEFAULT_PROFILE`（"spec"）

    即：调用方显式选档 > 配置里指定的档位 > 配置里裸写的参数 > 默认档。
    任何一层不合法时继续往下回退，并留中文告警，**永不抛异常**
    （与 load_params 的约定一致：配置写坏不能拖垮调用方）。

    默认（无显式档位、无 active_profile、配置只含 params）行为与
    改动前完全一致——引入档位机制不改变任何既有分数。
    """
    warnings: list[str] = []
    target = Path(path) if path is not None else RULES_PATH

    data: object = None
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None   # 读不到/解析失败交给上层（load_params）报告，这里静默

    raw_params: dict = {}
    config_profile: str | None = None
    if isinstance(data, dict):
        if isinstance(data.get("params"), dict):
            raw_params = {k: v for k, v in data["params"].items() if k in PARAM_KEYS}
        candidate = data.get("active_profile")
        if isinstance(candidate, str) and candidate.strip():
            config_profile = candidate.strip()

    # 1) 显式档位优先
    chosen: str | None = name
    if chosen is None:
        chosen = config_profile

    if chosen is not None:
        if chosen not in PROFILES:
            warnings.append(f"关注度参数档位 {chosen!r} 不存在（可用：{'、'.join(PROFILES)}），"
                            f"已退回默认档 {DEFAULT_PROFILE!r}")
            chosen = DEFAULT_PROFILE
        params = {key: float(PROFILES[chosen]["params"][key]) for key in PARAM_KEYS}
        if validate_params(params):
            warnings.append(f"档位 {chosen!r} 参数不合法，已退回默认档 {DEFAULT_PROFILE!r}")
            chosen = DEFAULT_PROFILE
            params = {key: float(PROFILES[chosen]["params"][key]) for key in PARAM_KEYS}
        return params, warnings

    # 2) 无显式档位：沿用配置文件里的 params 段（保持既有行为）
    if raw_params:
        issues = validate_params(raw_params)
        if not issues and len(raw_params) == len(PARAM_KEYS):
            return {k: float(raw_params[k]) for k in PARAM_KEYS}, warnings
        warnings.append("关注度口径文件 params 段不合法（"
                        + "；".join(issues or ["字段不完整"]) + "），已改用默认档参数")

    # 3) 兜底：默认档
    params = {key: float(PROFILES[DEFAULT_PROFILE]["params"][key]) for key in PARAM_KEYS}
    return params, warnings


def active_profile_name(path: str | os.PathLike | None = None) -> str:
    """返回当前生效的档位名（配置 `active_profile` 合法时用它，否则 DEFAULT_PROFILE）。

    只负责"名字"，参数仍以 `load_profile()` 为准。用途：结论卡 / `/api/attention`
    要把实际采用的档位如实回显，不能让前端猜（猜错就是把两档分数混在一个标签下）。
    配置缺失、JSON 写坏、档位名不存在时一律退回 `DEFAULT_PROFILE`，**永不抛异常**。
    """
    target = Path(path) if path is not None else RULES_PATH
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return DEFAULT_PROFILE
    if isinstance(data, dict):
        candidate = data.get("active_profile")
        if isinstance(candidate, str) and candidate.strip() in PROFILES:
            return candidate.strip()
    return DEFAULT_PROFILE


def profile_bands(name: str | None = None) -> list[dict]:
    """取档位对应的档位表（bands）。档位不存在时退回默认档。"""
    chosen = name if name in PROFILES else DEFAULT_PROFILE
    return [dict(b) for b in PROFILES[chosen]["bands"]]


def profile_threshold(name: str | None = None) -> float:
    """取档位对应的深读阈值。档位不存在时退回默认档。"""
    chosen = name if name in PROFILES else DEFAULT_PROFILE
    return float(PROFILES[chosen]["deep_read_threshold"])


def list_profiles() -> list[str]:
    """返回可用档位名（顺序即 PROFILES 定义顺序，默认档在前）。"""
    return list(PROFILES.keys())


def _check_point(name: str, value: object) -> float:
    """校验单个观测值的类型与取值范围，返回 float；非法时抛 AttentionInputError。"""
    if not _is_real(value):
        raise AttentionInputError(
            f"{name} 必须是 int 或 float（bool 不算），实际是 {type(value).__name__}: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise AttentionInputError(f"{name} 必须是有限实数，实际是 {value!r}")
    if number < 0:
        raise AttentionInputError(f"{name} 必须 >= 0，实际是 {value!r}")
    return number


def _resolve_params(params: dict | None) -> dict[str, float]:
    """把 params 规整成完整参数表；显式传入非法参数时抛 AttentionParamError。"""
    if params is None:
        return dict(DEFAULT_PARAMS)
    issues = validate_params(params)
    if issues:
        raise AttentionParamError("关注度参数不合法：" + "；".join(issues))
    return {key: float(params[key]) for key in PARAM_KEYS}


def _compute(x: float, y: float, z: float,
             params: dict | None) -> tuple[dict[str, float], dict[str, float], float, float]:
    """核心计算：返回 (params, 分项, exponent, score)。score 显式校验落在 (0, 100)。

    公式按 ``100 * (1 - exp(-lambda*S))`` 实现，但为守住开区间 (0, 100)，
    实际按等价的 ``100 - 100*exp(exponent)`` 计算，细节见下方注释。
    """
    point_x = _check_point("x", x)
    point_y = _check_point("y", y)
    point_z = _check_point("z", z)
    resolved = _resolve_params(params)

    term_x = resolved["w_x"] * math.log1p(point_x) / math.log1p(resolved["x0"])
    term_y = resolved["w_y"] * math.log1p(point_y) / math.log1p(resolved["y0"])
    term_z = resolved["w_z"] * math.log1p(point_z) / math.log1p(resolved["z0"])
    total = term_x + term_y + term_z + resolved["epsilon"]
    exponent = -resolved["lambda"] * total

    # 先算"距 100 有多远"再相减：gap 天然不会让分数越过 100 这条线。
    # 数学上 100*(1-exp(-u)) 等价于 100 - 100*exp(-u)；直接写前者在双精度下有两个
    # 真实盲区，都会违反硬约束 0 < E < 100：
    #   * 上界：-exponent > 53*ln(2) ≈ 36.7368 时，1 - exp(-u) 舍入成恰好 1.0，
    #     于是 E == 100.0（例：x=y=z=1e300，exp(-u)=4.6e-28，1.0-4.6e-28 仍等于 1.0）；
    #   * 下界：-exponent < 2^-54 时（外置配置给出极小 epsilon，如 1e-20），
    #     100*(1-exp(-u)) 舍入成恰好 0.0，于是 E == 0。
    # 改为先算 gap 再相减，越界时取 double 里最接近的合法值 nextafter，
    # 既不谎报次大值，也不用 min/max 把结果夹到边界上。
    gap = 100.0 * math.exp(exponent)
    score = 100.0 - gap
    if not (0.0 < score < 100.0):
        score = math.nextafter(100.0, 0.0) if score >= 100.0 else math.nextafter(0.0, 1.0)

    # 显式校验而不是 assert：python -O 下 assert 会被整段剔除，那样会静默返回 100.0/0.0
    if not (0.0 < score < 100.0):
        raise AttentionParamError(
            f"关注度分数越界（{score!r}），请检查参数是否已退化"
            f"（x0/y0/z0 过小、lambda 过大、epsilon 过小等）")

    terms = {
        "term_x": term_x,
        "term_y": term_y,
        "term_z": term_z,
        "epsilon": resolved["epsilon"],
        "S": total,
    }
    return resolved, terms, exponent, score


def contributions(x: float, y: float, z: float,
                  params: dict | None = None) -> dict[str, float]:
    """返回三个加权分项与总和：term_x/term_y/term_z/epsilon/S。

    term_x = w_x*ln(1+x)/ln(1+x0)，其余同理；S = 三项之和 + epsilon。
    """
    _, terms, _, _ = _compute(x, y, z, params)
    return terms


def attention_score(x: float, y: float, z: float, params: dict | None = None) -> float:
    """计算关注度分数 E，严格落在开区间 (0, 100)。

    x/y/z 必须是 int 或 float（bool 不算）、有限且 >= 0，否则抛 `AttentionInputError`；
    params 为 None 时用 `DEFAULT_PARAMS`，显式传入但非法时抛 `AttentionParamError`。
    """
    _, _, _, score = _compute(x, y, z, params)
    return float(score)


def classify(score: float, bands: list[dict] | None = None) -> str:
    """按档位表给分数归档：返回第一个 score >= min 的 label。

    `bands` 默认 `DEFAULT_BANDS`，约定按 min 降序排列；都不满足（分数低于所有 min）
    时返回最后一条的 label。score 非数值（或档位表结构不合法）抛 `AttentionInputError`。
    """
    if not _is_real(score) or not math.isfinite(float(score)):
        raise AttentionInputError(
            f"score 必须是有限实数（int/float，bool 不算），实际是 {type(score).__name__}: {score!r}")

    table = DEFAULT_BANDS if bands is None else bands
    if not isinstance(table, list) or not table:
        raise AttentionInputError("档位表必须是非空列表（每项含 min 与 label）")

    number = float(score)
    last_label: str | None = None
    for band in table:
        if not isinstance(band, dict):
            raise AttentionInputError(f"档位表每一项必须是 dict，实际是 {type(band).__name__}")
        if "min" not in band or "label" not in band:
            raise AttentionInputError("档位表每一项必须同时含 min 与 label 键")
        if not _is_real(band["min"]) or not isinstance(band["label"], str):
            raise AttentionInputError(
                f"档位的 min 必须是实数、label 必须是字符串，实际是 {band!r}")
        last_label = band["label"]
        if number >= float(band["min"]):
            return last_label

    return str(last_label)


def deep_read_triggered(score: float, threshold: float = DEEP_READ_THRESHOLD) -> bool:
    """是否触发深读：严格大于阈值才为 True（等于阈值不算）。"""
    if not _is_real(score) or not math.isfinite(float(score)):
        raise AttentionInputError(
            f"score 必须是有限实数（int/float，bool 不算），实际是 {type(score).__name__}: {score!r}")
    if not _is_real(threshold) or not math.isfinite(float(threshold)):
        raise AttentionInputError(
            f"threshold 必须是有限实数（int/float，bool 不算），实际是 {type(threshold).__name__}: {threshold!r}")
    return float(score) > float(threshold)


def derive_signals(x: float, y: float, z: float,
                   params: dict | None = None,
                   profile: str | None = None,
                   proxy_flags: Sequence[str] | None = None) -> dict:
    """从 x/y/z 派生出**可解释的结构化信号**（纯函数，不碰 LLM、不联网）。

    为什么要它：只给解读环节一个 E 值（"关注度 69.56"）时，模型只能复述"关注度高"，
    说不出"高在哪"。本函数把三个原始输入翻译成几个人人能懂的比值与占比，让下游
    （深读的 prompt）拿到解释力的原料。

    返回 dict（键固定，便于单测逐项断言）：
        x, y, z          原始输入（float 化）
        engagement_ratio z / y —— 阅读里有多大比例转化成留言（参与深度）；y <= 0 时为 0.0
        spread            x —— 文章数量（传播广度，直接回显）
        resonance         z / x —— 单篇平均留言量（共鸣强度）；x <= 0 时为 0.0
        share_x/y/z       三个加权分项占 S 的比例（与 `contributions()` 逐点一致，
                          含 epsilon，故三者之和略小于 1）
        profile           参数档位名（原样回显，供下游按档位选择口径）
        proxy_flags       哪些输入是代理/估计值，如 ["y_proxy", "z_proxy"]

    除零保护：y <= 0 或 x <= 0 时对应比值给 0.0（**不抛异常、不给 inf/nan**）——
    话题模式下 y/z 抽不到数是常态（见 estimate.py），深读链路必须能继续跑。

    输入合法性（x/y/z 非有限实数、负数、params 非法）与 `attention_score` 同样处理：
    抛 `AttentionInputError` / `AttentionParamError`，由调用方兜住。
    """
    point_x = _check_point("x", x)
    point_y = _check_point("y", y)
    point_z = _check_point("z", z)
    resolved = _resolve_params(params)

    terms = contributions(point_x, point_y, point_z, resolved)
    total = float(terms["S"])

    def _ratio(numerator: float, denominator: float) -> float:
        """比值：分母 <= 0 时给 0.0（除零保护，绝不返回 inf/nan）。"""
        if denominator <= 0:
            return 0.0
        return float(numerator) / float(denominator)

    flags: list[str] = []
    for raw in proxy_flags or []:
        name = str(raw).strip()
        if name and name not in flags:
            flags.append(name)

    return {
        "x": point_x,
        "y": point_y,
        "z": point_z,
        "engagement_ratio": _ratio(point_z, point_y),
        "spread": point_x,
        "resonance": _ratio(point_z, point_x),
        "share_x": _ratio(terms["term_x"], total),
        "share_y": _ratio(terms["term_y"], total),
        "share_z": _ratio(terms["term_z"], total),
        "profile": str(profile) if profile else DEFAULT_PROFILE,
        "proxy_flags": flags,
    }


def score_detail(x: float, y: float, z: float, params: dict | None = None) -> dict:
    """一次算出分数与全部中间量，便于报告生成与排查。

    返回 {"x","y","z","params","term_x","term_y","term_z","S","exponent",
    "score","band","deep_read"}；band 用 `classify()`，
    deep_read = score > `DEEP_READ_THRESHOLD`。
    """
    resolved, terms, exponent, score = _compute(x, y, z, params)
    return {
        "x": float(x),
        "y": float(y),
        "z": float(z),
        "params": resolved,
        "term_x": terms["term_x"],
        "term_y": terms["term_y"],
        "term_z": terms["term_z"],
        "S": terms["S"],
        "exponent": exponent,
        "score": float(score),
        "band": classify(score),
        "deep_read": deep_read_triggered(score),
    }


def main(argv: list[str] | None = None) -> int:
    """命令行入口：`python -m mylocalai.attention.attention --x .. --y .. --z .. [--json] [--rules PATH]`。

    非 --json 打印中文可读结果（E、S、三个分项、档位、是否触发深读）；--json 打印
    UTF-8 JSON。参数缺失或无法转成 float 时打印中文错误并返回非 0，不抛未捕获异常。
    """
    from mylocalai.util.console_io import force_utf8_stdio# 项目既有约定：入口规整 UTF-8

    force_utf8_stdio()

    args = list(sys.argv[1:]) if argv is None else [str(item) for item in argv]
    raw: dict[str, str] = {}
    as_json = False
    rules_path: str | None = None
    profile_name: str | None = None

    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-h", "--help"):
            print(_USAGE)
            print("可用参数档位：" + "、".join(list_profiles())
                  + f"（默认 {DEFAULT_PROFILE}）")
            return 0
        if token == "--json":
            as_json = True
            index += 1
            continue
        if token == "--profile":
            if index + 1 >= len(args):
                print(f"错误：{token} 后面缺少取值。\n{_USAGE}", file=sys.stderr)
                return 2
            profile_name = args[index + 1]
            index += 2
            continue
        if token in ("--x", "--y", "--z", "--rules"):
            if index + 1 >= len(args):
                print(f"错误：{token} 后面缺少取值。\n{_USAGE}", file=sys.stderr)
                return 2
            value = args[index + 1]
            if token == "--rules":
                rules_path = value
            else:
                raw[token[2:]] = value
            index += 2
            continue
        print(f"错误：无法识别的参数 {token!r}。\n{_USAGE}", file=sys.stderr)
        return 2

    missing = [name for name in ("x", "y", "z") if name not in raw]
    if missing:
        print("错误：缺少必填参数 " + " ".join(f"--{name}" for name in missing)
              + f"。\n{_USAGE}", file=sys.stderr)
        return 2

    values: dict[str, float] = {}
    for name in ("x", "y", "z"):
        try:
            values[name] = float(raw[name])
        except (TypeError, ValueError):
            print(f"错误：--{name} 的取值 {raw[name]!r} 无法转成数字（float）。", file=sys.stderr)
            return 2

    params, warnings = load_profile(profile_name, rules_path)
    for warning in warnings:
        print(f"告警：{warning}", file=sys.stderr)

    # 档位名非法时 load_profile 会回退默认档，这里以“实际生效的档位”为准，
    # 避免把无效档位名回显成好像生效了一样。
    effective_profile = DEFAULT_PROFILE if warnings else (profile_name or DEFAULT_PROFILE)

    try:
        detail = score_detail(values["x"], values["y"], values["z"], params)
    except AttentionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if as_json:
        detail["profile"] = effective_profile
        detail["threshold"] = profile_threshold(effective_profile)
        print(json.dumps(detail, ensure_ascii=False, indent=2))
        return 0

    print("关注度（Attention）评分")
    print(f"  参数档位：{effective_profile}")
    print(f"  输入：x={detail['x']:g}（文章数） y={detail['y']:g}（阅读量） z={detail['z']:g}（留言量）")
    print(f"  分项：term_x={detail['term_x']:.6f} term_y={detail['term_y']:.6f} "
          f"term_z={detail['term_z']:.6f} epsilon={detail['params']['epsilon']:g}")
    print(f"  合成：S={detail['S']:.6f} exponent={detail['exponent']:.6f}")
    print(f"  关注度：E={detail['score']:.4f}")
    print(f"  档位：{detail['band']}")
    print(f"  深读：{'触发' if detail['deep_read'] else '未触发'}"
          f"（阈值 {profile_threshold(profile_name):g}，严格大于才算触发）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
