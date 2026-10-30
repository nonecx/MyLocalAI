# -*- coding: utf-8 -*-
"""JSON 配置加载：整套替换 + 增量覆盖 + 失败退回内置默认并留告警。

供 `mylocalai/config/prompt_config.py`（提示词）与 `mylocalai/llm/llm_client.py`（提供方表）
共用，避免两处各写一份加载逻辑。语义与 `src/mylocalai/rumor/rules.py` 一致：

    <ENV_FULL>     整套替换（不再以内置默认为底稿；漏段要留告警）
    默认文件        正常底稿
    <ENV_EXTRA>    增量覆盖（字典递归合并、列表取并集）

任何一步失败都退回 `builtin` 并留下告警——配置写坏不能拖垮分析链路。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, List, Optional, Tuple


def _read(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"配置根节点必须是对象，实际是 {type(data).__name__}")
    return data


def _strip(data: Dict[str, Any]) -> Dict[str, Any]:
    """去掉 `_xxx_comment` 说明键。"""
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """深合并：字典递归；列表取并集（保留 base 顺序）。"""
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        elif isinstance(value, list) and isinstance(out.get(key), list):
            out[key] = list(out[key]) + [x for x in value if x not in out[key]]
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(default_path: str, builtin: Optional[Dict[str, Any]] = None,
                required_sections: Tuple[str, ...] = (),
                env_full: Optional[str] = None,
                env_extra: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """返回 (配置, 告警)。

    `builtin` 是紧急兜底（默认文件缺失/损坏时用）。告警按条形 key 存，
    同名只留最新一条，因此反复读取不会把旧告警累积成噪音。
    """
    warnings: Dict[str, str] = {}
    name = os.path.basename(default_path)
    data: Dict[str, Any] = {}
    override = (os.environ.get(env_full, "") if env_full else "").strip()
    base_path = override or default_path
    try:
        loaded = _strip(_read(base_path))
        data = {} if override else copy.deepcopy(builtin or {})
        data = _merge(data, loaded)
        warnings.pop(name, None)
        if override:
            missing = [k for k in required_sections if k not in loaded]
            if missing:
                warnings[f"{name}#replaced"] = (
                    f"{env_full} 指向的替换文件未包含这些段，没有内置默认兜底："
                    f"{'、'.join(missing)}（请确认是有意省略）")
    except FileNotFoundError:
        warnings[name] = f"{name} 未找到（{base_path}），已用内置默认值"
        data = copy.deepcopy(builtin or {})
    except Exception as exc:  # noqa: BLE001  坏配置不能拖垮分析链路
        warnings[name] = (f"{name} 读取失败（{base_path}）：{type(exc).__name__}: {exc}，"
                          f"已用内置默认值")
        data = copy.deepcopy(builtin or {})

    extra = (os.environ.get(env_extra, "") if env_extra else "").strip()
    if extra:
        try:
            data = _merge(data, _strip(_read(extra)))
            warnings.pop(f"{name}#extra", None)
        except Exception as exc:  # noqa: BLE001
            warnings[f"{name}#extra"] = f"{name} 增量配置读取失败（{extra}）：{type(exc).__name__}: {exc}"
    else:
        warnings.pop(f"{name}#extra", None)

    if not override:
        # 不再整套替换时，上一次替换留下的"漏段"告警已过期
        warnings.pop(f"{name}#replaced", None)
    return data, warnings


__all__ = ["load_config", "merge"]
