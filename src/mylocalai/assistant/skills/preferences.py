# -*- coding: utf-8 -*-
"""两个新技能的本地工作流偏好（设置页“技能”页的持久化层）。

改了什么、为什么、锁定什么：
  - 新增 `assistant.skills.preferences`：资料收集 / 权威核验**分别**选择
    `pipeline`（固定阶段）或 `agent`（受限循环），并保存自动核验/复合开关。
   - 2026-11-25 增 `power_mode`（normal|max，最大功率）：max 时所有技能调用不考虑
     配额（检索/读页/模型调用/条数/总时限抬到各自安全区间上限，安全参数与阈值不动），
     只确保功能完成；回合总时限同步抬到 3600s（loop._turn_budget_s 消费）。
  - 只存本机配置，不存用户内容；坏 JSON/坏类型一律回退默认 + warning，
    绝不让设置文件写坏拖垮对话。
  - 写入走同目录临时文件 + os.replace 原子替换；接口/测试可经
    `MYLOCALAI_SKILLS_PREFERENCES_PATH` 指向临时路径。

依赖方向：本模块只 import 标准库（assistant 子包），不 import web。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

__all__ = ["DEFAULT_PREFERENCES", "PATH_ENV", "default_path", "load_preferences",
           "save_preferences", "normalize_preferences", "max_power_enabled"]

PATH_ENV = "MYLOCALAI_SKILLS_PREFERENCES_PATH"

#: 功率模式闭集：normal=按各技能 rules.json 配额执行；max=最大功率（不考虑配额）。
POWER_MODES = ("normal", "max")
#: 最大功率模式下回合总时限的抬升值（秒）：与各技能 total_deadline_s 的安全上限
#: （3600s）一致——只抬技能内部配额不抬回合时限，技能仍会在 180s 被截断。
MAX_POWER_TURN_BUDGET_S = 3600.0

DEFAULT_PREFERENCES: Dict[str, Any] = {
    "schema_version": 1,
    "power_mode": "normal",
    "research": {"workflow_mode": "pipeline", "auto_verify_findings": True},
    "authority": {"workflow_mode": "pipeline"},
    "composite": {"enabled": True},
}

_WORKFLOW_MODES = ("pipeline", "agent")


def default_path() -> Path:
    """默认写在仓库 data/skills_preferences.json（与隐私/模型配置同层）。"""
    configured = str(os.environ.get(PATH_ENV) or "").strip()
    if configured:
        return Path(configured)
    # src/mylocalai/assistant/skills/preferences.py -> 仓库根
    return Path(__file__).resolve().parents[4] / "data" / "skills_preferences.json"


def _defaults() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_PREFERENCES)


def _warn(warnings: list, text: str) -> None:
    if text and text not in warnings:
        warnings.append(text)


def _normalize(raw: Any, *, strict: bool) -> Tuple[Dict[str, Any], list]:
    """把外部 JSON 规整成闭集配置。

    strict=True（保存接口）：字段类型/枚举不合法直接 ValueError，避免把坏配置
    写进磁盘；strict=False（加载）：逐字段回退默认并收集 warning。
    """
    out = _defaults()
    warnings: list = []
    if not isinstance(raw, Mapping):
        if strict:
            raise ValueError("技能偏好必须是对象")
        return out, ["技能偏好不是对象，已回退默认"]

    version = raw.get("schema_version", 1)
    if version != 1:
        if strict:
            raise ValueError("schema_version 只支持 1")
        _warn(warnings, "schema_version 不受支持，已回退默认")

    power = raw.get("power_mode", out["power_mode"])
    if power not in POWER_MODES:
        if strict:
            raise ValueError("power_mode 必须是 normal|max")
        _warn(warnings, "power_mode 非法，已回退 normal")
        power = "normal"
    out["power_mode"] = power

    research = raw.get("research")
    if not isinstance(research, Mapping):
        if strict:
            raise ValueError("research 必须是对象")
        _warn(warnings, "research 段缺失或类型错误，已回退默认")
        research = {}
    mode = research.get("workflow_mode", out["research"]["workflow_mode"])
    if mode not in _WORKFLOW_MODES:
        if strict:
            raise ValueError("research.workflow_mode 必须是 pipeline|agent")
        _warn(warnings, "research.workflow_mode 非法，已回退 pipeline")
        mode = "pipeline"
    out["research"]["workflow_mode"] = mode
    auto = research.get("auto_verify_findings", out["research"]["auto_verify_findings"])
    if not isinstance(auto, bool):
        if strict:
            raise ValueError("research.auto_verify_findings 必须是布尔值")
        _warn(warnings, "research.auto_verify_findings 非法，已回退 true")
        auto = True
    out["research"]["auto_verify_findings"] = auto

    authority = raw.get("authority")
    if not isinstance(authority, Mapping):
        if strict:
            raise ValueError("authority 必须是对象")
        _warn(warnings, "authority 段缺失或类型错误，已回退默认")
        authority = {}
    amode = authority.get("workflow_mode", out["authority"]["workflow_mode"])
    if amode not in _WORKFLOW_MODES:
        if strict:
            raise ValueError("authority.workflow_mode 必须是 pipeline|agent")
        _warn(warnings, "authority.workflow_mode 非法，已回退 pipeline")
        amode = "pipeline"
    out["authority"]["workflow_mode"] = amode

    composite = raw.get("composite")
    if not isinstance(composite, Mapping):
        if strict:
            raise ValueError("composite 必须是对象")
        _warn(warnings, "composite 段缺失或类型错误，已回退默认")
        composite = {}
    enabled = composite.get("enabled", out["composite"]["enabled"])
    if not isinstance(enabled, bool):
        if strict:
            raise ValueError("composite.enabled 必须是布尔值")
        _warn(warnings, "composite.enabled 非法，已回退 true")
        enabled = True
    out["composite"]["enabled"] = enabled

    if not enabled and out["research"]["auto_verify_findings"]:
        out["research"]["auto_verify_findings"] = False
        _warn(warnings, "复合能力关闭时自动核验强制关闭")
    return out, warnings


def _view(data: Dict[str, Any], warnings: list) -> Dict[str, Any]:
    return {**copy.deepcopy(data), "warning": "；".join(warnings)}


def load_preferences(path: Optional[Any] = None) -> Dict[str, Any]:
    """读配置；文件缺失/坏 JSON/坏字段 → 默认值 + warning，永不抛。"""
    target = Path(path) if path is not None else default_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _view(_defaults(), [])
    except Exception as exc:  # noqa: BLE001  坏配置只影响本次设置，不挡对话
        return _view(_defaults(), [f"技能偏好读取失败，已回退默认：{type(exc).__name__}"])
    data, warnings = _normalize(raw, strict=False)
    return _view(data, warnings)


def save_preferences(raw: Any, path: Optional[Any] = None) -> Dict[str, Any]:
    """校验并原子写；非法输入抛 ValueError（由 API 转 400）。"""
    data, warnings = _normalize(raw, strict=True)
    target = Path(path) if path is not None else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".skills-prefs-", suffix=".json",
                                    dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return _view(data, warnings)


def normalize_preferences(raw: Any) -> Dict[str, Any]:
    """供测试/调用方直接校验（非法抛 ValueError）。"""
    data, _warnings = _normalize(raw, strict=True)
    return data


def max_power_enabled(prefs: Optional[Mapping[str, Any]] = None) -> bool:
    """是否处于最大功率模式（不考虑配额）。

    传 None 时自行读盘（读盘本身永不抛：坏文件回退默认 normal）；传 dict 时按已
    加载的偏好判断，避免热路径重复读文件。判断只认闭集内的 "max"——坏值在
    `_normalize` 里已被回退成 "normal"，这里再兜一层类型检查。
    """
    data = prefs if prefs is not None else load_preferences()
    return str(data.get("power_mode") or "") == "max"
