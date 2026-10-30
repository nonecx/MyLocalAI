# -*- coding: utf-8 -*-
"""控制台输出规整：非 UTF-8 的控制台（例如英文 Windows 的 cp1252）不再崩。

背景（2026-10-22 检验报告发现 8）：`main.py` 打印中文报告时，在
`PYTHONIOENCODING=cp1252` 下抛 `UnicodeEncodeError` 并以退出码 1 结束；
而 `rumor_agent/cli.py` 早就做了 `reconfigure`，两条命令行入口口径不一致。
把这段逻辑收成一处，所有入口统一调用。
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """把 stdout / stderr 规整为 UTF-8（编码失败时用 U+FFFD 替换，绝不抛异常）。

    幂等，可重复调用；没有 `reconfigure` 的流（如被重定向成对象）会静默跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            pass


__all__ = ["force_utf8_stdio"]
