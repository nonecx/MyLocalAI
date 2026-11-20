# -*- coding: utf-8 -*-
"""热门监测平台（platform 包）：自动发现 → 初筛 → 深查 → 风险分级 → 展示预警。

定位（相对既有「智能助手」）：助手等用户发消息，平台主动巡检。两者共用同一批
引擎与档案库：发现走 retrieval、热度走 attention、查验走 rumor、沉淀走 dossier。

铁律与助手完全一致：
  - 传播风险分级是**纯函数**（platform/risk.py），大模型不参与打分；
  - 每个出口都有降级，隐私未授权时不外发不落库（fail-closed）；
  - 只述证据与传播风险，不判定真假。

公开面（随里程碑扩充）：
  - rules.load_rules        平台规则加载（永不抛）
  - store.PlatformStore     平台域三表读写
"""

from __future__ import annotations

__all__ = ["load_rules", "PlatformStore"]


def __getattr__(name: str):
    # 延迟公开面： import platform 不拖拽重依赖（scheduler/runner 后续里程碑落地）。
    if name == "load_rules":
        from mylocalai.platform.rules import load_rules
        return load_rules
    if name == "PlatformStore":
        from mylocalai.platform.store import PlatformStore
        return PlatformStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
