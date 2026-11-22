# -*- coding: utf-8 -*-
"""助手工具注册表：schema 校验、权限分级、每工具成本/延迟元数据（§5.2）。

注册的每个工具声明：
  - name / handler / params schema（手工校验器列表，与全项目"白名单 + 越界回退"文化一致）
  - permission: "read"（只读，不落档案）| "write"（读+写档案/快照/导出）
  - cost: "zero" | "light" | "medium" | "heavy"（对外可展示的成本徽章口径）
  - est_s: 预估耗时秒数（tool_start 事件的 est_s 用）

防护链顺序（§5.1）：枚举白名单（router 层）→ 参数 schema 校验（本模块 validate）
→ 工具 allowlist（本模块 check_allowed）→ 权限检查（本模块 check_allowed）。
任何一步不过，上层降级为 clarify，绝不猜。
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Dict, List, Optional

__all__ = ["ToolSpec", "ToolRegistry", "ValidationError", "ToolNotAllowed"]

MAX_TEXT_LENGTH = 8000   # 与网站输入框上限一致
MAX_TOPIC_LENGTH = 200   # 与 /api/attention/search 的话题上限一致


class ValidationError(Exception):
    """参数未通过 schema 校验（路由层据此降级 clarify）。"""


class ToolNotAllowed(Exception):
    """工具不在 allowlist / 权限不足。"""


ToolSpec = Dict[str, Any]


def _v_str(value: Any, name: str, max_len: int, required: bool = True) -> str:
    if value is None:
        if required:
            raise ValidationError(f"缺少参数 {name}")
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"参数 {name} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise ValidationError(f"参数 {name} 不能为空")
    if len(value) > max_len:
        raise ValidationError(f"参数 {name} 过长（最多 {max_len} 字）")
    return value


def _v_id(value: Any, name: str = "id") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"参数 {name} 必须是正整数")
    return value


def _v_num(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or \
       not math.isfinite(float(value)) or value < 0:
        raise ValidationError(f"参数 {name} 必须是非负数")
    return float(value)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, name: str, handler: Callable, params: List[str],
                 permission: str = "read", cost: str = "zero", est_s: float = 0.0,
                 description: str = "", validators: Optional[Dict[str, Callable]] = None) -> None:
        assert permission in ("read", "write"), permission
        assert cost in ("zero", "light", "medium", "heavy"), cost
        self._tools[name] = {
            "name": name, "handler": handler, "params": list(params),
            "permission": permission, "cost": cost, "est_s": float(est_s),
            "description": description, "validators": dict(validators or {}),
        }

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def validate(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """按注册的参数清单校验并返回规整后的参数（只保留声明过的键）。

        声明了的参数必填（除非该参数的校验器显式接受 None）；未声明的键一律丢弃
        （用户消息/路由 JSON 里的多余字段不进工具）。
        """
        spec = self._tools.get(name)
        if spec is None:
            raise ToolNotAllowed(f"未注册的工具: {name}")
        if not isinstance(args, dict):
            raise ValidationError("参数必须是对象")
        out: Dict[str, Any] = {}
        for param in spec["params"]:
            validator = spec["validators"].get(param)
            if validator is None:
                raise ValidationError(f"工具 {name} 参数 {param} 缺少校验器")
            out[param] = validator(args.get(param))
        return out

    def check_allowed(self, name: str, allowlist: Optional[List[str]] = None,
                      write_enabled: bool = True) -> None:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolNotAllowed(f"未注册的工具: {name}")
        if allowlist is not None and name not in allowlist:
            raise ToolNotAllowed(f"工具不在允许清单: {name}")
        if spec["permission"] == "write" and not write_enabled:
            raise ToolNotAllowed(f"工具需要写权限: {name}")

    def catalog(self, allowlist: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """给路由 system prompt 的工具摘要（名字 + 一句话 + 成本/延迟档）。

        allowlist（角色权限层，可选）：给定时只返回名字在清单内的条目——路由模型
        只看得到该角色可用的工具（assistant_rules.role_tool_allowlists 的角色白名单）；
        None（缺省）→ 全量目录，与加角色前的行为逐位一致。
        """
        names = set(allowlist) if allowlist is not None else None
        return [{"name": t["name"], "description": t["description"],
                 "cost": t["cost"], "est_s": t["est_s"], "permission": t["permission"]}
                for t in self._tools.values()
                if names is None or t["name"] in names]


def text_param(max_len: int = MAX_TEXT_LENGTH):
    def _check(value: Any) -> str:
        return _v_str(value, "text", max_len)
    return _check


def topic_param(value: Any) -> str:
    return _v_str(value, "topic", MAX_TOPIC_LENGTH)


def id_param(value: Any) -> int:
    return _v_id(value)


def xyz_param(name: str):
    def _check(value: Any) -> float:
        return _v_num(value, name)
    return _check


def optional_query(value: Any) -> str:
    return _v_str(value, "query", 200, required=False)


def limit_param(value: Any) -> int:
    if value is None:
        return 20
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 100):
        raise ValidationError("参数 limit 必须是 1-100 的整数")
    return value


def deep_param(value: Any) -> bool:
    if value is None or value is False:
        return False
    if not isinstance(value, bool):
        raise ValidationError("参数 deep 必须是布尔值")
    return value


_ID_RE = re.compile(r"^\d{1,12}$")


def export_id_param(value: Any) -> int:
    return _v_id(value)
