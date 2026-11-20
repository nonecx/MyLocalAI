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
  - store.PlatformStore     平台域存储读写（监控词/信息源/信息流/巡检记录）
  - domains.load_domains / domain_ids / domain_hints / classify_domain /
    resolve_domain          领域表加载与链式推断（LLM 不猜领域，可注入 llm_fn）
  - risk.assess_spread_risk 传播风险分级纯函数（大模型不参与打分）
  - runner.PlatformRunner   巡检编排（发现→初筛→深查→风险→落库）
  - scheduler.PlatformScheduler  后台调度（启动即跑一次 + 间隔循环 + 单飞 + 取消）
"""

from __future__ import annotations

__all__ = ["load_rules", "PlatformStore", "assess_spread_risk",
           "PlatformRunner", "PlatformScheduler",
           "load_domains", "domain_ids", "domain_hints",
           "classify_domain", "resolve_domain"]

_LAZY = {
    "load_rules": ("mylocalai.platform.rules", "load_rules"),
    "PlatformStore": ("mylocalai.platform.store", "PlatformStore"),
    "assess_spread_risk": ("mylocalai.platform.risk", "assess_spread_risk"),
    "PlatformRunner": ("mylocalai.platform.runner", "PlatformRunner"),
    "PlatformScheduler": ("mylocalai.platform.scheduler", "PlatformScheduler"),
    "load_domains": ("mylocalai.platform.domains", "load_domains"),
    "domain_ids": ("mylocalai.platform.domains", "domain_ids"),
    "domain_hints": ("mylocalai.platform.domains", "domain_hints"),
    "classify_domain": ("mylocalai.platform.domains", "classify_domain"),
    "resolve_domain": ("mylocalai.platform.domains", "resolve_domain"),
}


def __getattr__(name: str):
    # 延迟公开面：import platform 不拖拽重依赖（各模块按需加载）。
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    module = importlib.import_module(target[0])
    return getattr(module, target[1])
