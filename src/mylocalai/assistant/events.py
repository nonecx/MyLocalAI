# -*- coding: utf-8 -*-
"""SSE 事件协议（§5.3）：事件名封闭枚举 + 组装器。

事件顺序契约：meta → status(routing) → tool_start → scan（仅 verify 类，零延迟先发）
→ progress* → card → narrative* → tool_end → done；异常出口 error（可带 recoverable）。
卡片永远在叙述之前；叙述失败也先有卡片（loop 层负责）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

__all__ = ["EVENT_NAMES", "make_event", "format_sse", "EventStream"]

EVENT_NAMES = ("meta", "status", "tool_start", "scan", "progress", "card",
               "narrative", "tool_end", "done", "error")


def make_event(name: str, **payload: Any) -> Dict[str, Any]:
    """构造一个事件 dict（name 必须在封闭枚举内）。"""
    if name not in EVENT_NAMES:
        raise ValueError(f"未知事件名: {name}")
    return {"event": name, **payload}


def format_sse(event: Dict[str, Any]) -> str:
    """序列化成 SSE 帧文本：`event: <name>\\ndata: <json>\\n\\n`。"""
    name = event.get("event", "message")
    body = {k: v for k, v in event.items() if k != "event"}
    data = json.dumps(body, ensure_ascii=False)
    return f"event: {name}\ndata: {data}\n\n"


class EventStream:
    """线程安全的事件收集器：loop 在工作线程里发事件，SSE 端点负责吐给前端。

    断开取消：web 端在 SSE 断开时置 cancelled（调用 request_cancel），loop 的
    cancel_check 轮询它（复用 agent 的协作取消语义）。
    """

    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []
        self._cancelled = False

    def emit(self, name: str, **payload: Any) -> None:
        evt = make_event(name, **payload)
        self._events.append(evt)

    def snapshot(self) -> List[Dict[str, Any]]:
        return list(self._events)

    def request_cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled
