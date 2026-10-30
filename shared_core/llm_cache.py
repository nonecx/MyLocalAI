# -*- coding: utf-8 -*-
"""大模型调用的进程内结果缓存 + 请求级时限（对齐检验报告发现 4/7/10）。

**为什么要缓存**：LongCat-2.5-Preview 在 temperature=0 下仍有采样漂移，而且**不认 seed**
（2026-10-15 实测：同 seed 连调 6 次得到 5 种输出）。所以"同一段文本重复分析给出不同结论"
只能在调用侧用"同样的请求只算一次"来消除——这既让用户可见结论可复现，也省掉重复 API 花销。
缓存只保存**成功**结果；失败结果不缓存（下次仍会真正重试）。

**为什么要时限**：单次 HTTP 最长等 60s、每次逻辑调用还要因 extra_body 回落翻倍、再加重试，
不可达地址下单次 /api/analyze 曾达 515.8s 且不可中断。请求级 deadline 让所有下游 LLM 调用
共享一个总预算：预算用尽即快速失败（转降级），不再一轮轮等满超时。

缓存与时限都是进程内的，不落盘；TTL/条数上限可在 rumor_agent/verdict_rules.json 的
stability 段配置，也可用环境变量 LLM_CACHE_TTL_S / LLM_CACHE_MAX 覆盖（0 表示关闭缓存）。
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

_LOCK = threading.Lock()
_ENTRIES: Dict[str, Tuple[float, Any]] = {}
# 在飞（single-flight）记录：key -> {"event": Event, "value": 结果, "error": 异常, "accepted": bool}
# 等待者等的是"结果或失败"，不是只等信号：leader 失败时若只 set() 信号，等待者醒来仍
# 缓存未命中、各自重跑 produce——并发相同请求从"1 次"退回"N 次"，恰在上游最脆弱时放大
# （2026-10-15 检验报告发现 5：失败路径实测 4 线程 → produce 调用 4 次）。
_INFLIGHT: Dict[str, Dict[str, Any]] = {}
_HITS = 0
_MISSES = 0
# 进程实例标识（pid + 启动时刻）：多进程部署时每个 worker 各有一份缓存，
# 用它可以判断两次统计是不是同一个实例（2026-10-22 修复 S5）。
_INSTANCE = f"{os.getpid()}:{time.monotonic():.6f}"


def _ttl() -> float:
    """缓存有效期秒数（LLM_CACHE_TTL_S，默认 900）。"""
    try:
        value = float(os.environ.get("LLM_CACHE_TTL_S") or 900)
    except (TypeError, ValueError):
        return 900.0
    return max(0.0, value)


def _max_entries() -> int:
    """缓存条数上限（LLM_CACHE_MAX，默认 256）；设为 0 即关闭缓存。"""
    try:
        value = int(os.environ.get("LLM_CACHE_MAX") or 256)
    except (TypeError, ValueError):
        return 256
    return max(0, value)


def _sweep_locked(now: float) -> None:
    """清掉过期条目；仍超限时按"最早过期优先"淘汰。"""
    for key in [k for k, (exp, _) in _ENTRIES.items() if exp <= now]:
        _ENTRIES.pop(key, None)
    limit = _max_entries()
    while len(_ENTRIES) > limit:
        oldest = min(_ENTRIES.items(), key=lambda kv: kv[1][0])[0]
        _ENTRIES.pop(oldest, None)


def get(key: str) -> Tuple[bool, Any]:
    """查缓存，返回 (是否命中, 值)。"""
    global _HITS
    if _max_entries() == 0 or _ttl() <= 0:
        return False, None
    now = time.monotonic()
    with _LOCK:
        # 清扫放在读路径上（而不只是写入时）：过期条目与超容量不必等下一次写入才回收
        # （2026-10-22 修复 S8）。
        _sweep_locked(now)
        hit = _ENTRIES.get(key)
        if hit and hit[0] > now:
            _HITS += 1
            return True, hit[1]
        if hit:
            _ENTRIES.pop(key, None)
    return False, None


def put(key: str, value: Any) -> None:
    """写入缓存（只放成功结果）。"""
    if _max_entries() == 0 or _ttl() <= 0:
        return
    now = time.monotonic()
    with _LOCK:
        _ENTRIES[key] = (now + _ttl(), value)
        _sweep_locked(now)


def _cache_enabled() -> bool:
    """缓存（读 + 写）是否开启。注意：single-flight 不受它影响。"""
    return _max_entries() > 0 and _ttl() > 0


def call(key: str, produce: Callable[[], Any], accept: Callable[[Any], bool],
         use_cache: bool = True) -> Any:
    """带单飞（single-flight）的取用：同 key 并发时只算一次，其余等它算完复用。

    **single-flight 与缓存是两件独立的事**，这里刻意分开：
      - single-flight：同 key 并发时只算一次（**永远生效**，不受缓存开关影响）；
      - 缓存：跨请求复用成功结果，受 `LLM_CACHE_MAX` / `TTL` 控制；
        `use_cache=False` 时既不读也不写（用于"同一文本再取一个独立样本"的边界复核）。

    此前实现把两者绑在一起（缓存关闭时直接 `produce()`），于是**关缓存会连带关掉
    并发去重**，恰好在上游最脆弱时把单次调用放大成 N 次（2026-10-22 检验报告发现 3）。

    produce() 的结果只在 accept() 认可（例如无错误）时才写缓存；失败结果不写缓存，
    但**会在这批等待者之间共享**——leader 失败时等待者拿到同一份失败结果，
    而不是各自再打一次上游（原实现在失败路径会把单次调用放大成 N 次）。
    抛异常时把异常共享给等待者后原样抛出，不写缓存。
    """
    global _MISSES
    enabled = bool(use_cache) and _cache_enabled()
    if enabled:
        ok, value = get(key)
        if ok:
            return value
    with _LOCK:
        _MISSES += 1
        record = _INFLIGHT.get(key)
        if record is None:
            record = {"event": threading.Event(), "value": None, "error": None, "accepted": False}
            _INFLIGHT[key] = record
            leader = True
        else:
            leader = False

    if not leader:
        # 等首位算完直接复用它的结果（超时兜底：跟着请求预算走）
        signaled = record["event"].wait(timeout=_wait_budget())
        if signaled:
            if record["error"] is not None:
                raise record["error"]
            # leader 失败（未 accept）时也把同一份失败结果还给等待者，不重跑 produce
            return record["value"]
        ok, value = get(key)
        if ok:
            return value
        return produce()      # 首位仍未返回（等超时）：自己算一次

    try:
        try:
            value = produce()
        except BaseException as exc:  # noqa: BLE001  失败共享给等待者后再原样抛出
            record["error"] = exc
            raise
        if enabled and accept(value):
            put(key, value)
            record["accepted"] = True
        record["value"] = value
        return value
    finally:
        with _LOCK:
            _INFLIGHT.pop(key, None)
        record["event"].set()


def _wait_budget() -> float:
    """等待首位结果的最长时间：跟着请求预算走，避免自己也吊死。

    未设预算时给一个宽松兜底；**设了预算时最多等到预算耗尽**——原来的
    `max(30.0, remaining)` 会在预算已用尽后仍让等待者空等 30 秒，
    使整条链路超出预算约 30 秒（2026-10-22 检验报告发现 4）。
    """
    remaining = remaining_seconds()
    if remaining is None:
        return 120.0
    return max(0.0, remaining)


def stats() -> Dict[str, Any]:
    """缓存统计（供 /api/health 与检验脚本观察）。

    `instance` 是"进程实例标识"（pid + 启动时刻）：多进程部署时每个 worker 各有一份
    缓存，用它可以判断两次统计是不是同一个实例（2026-10-22 修复 S5）。
    """
    with _LOCK:
        return {"entries": len(_ENTRIES), "hits": _HITS, "misses": _MISSES,
                "inflight": len(_INFLIGHT), "instance": _INSTANCE}


def clear() -> None:
    """清空缓存与统计（测试与长驻进程用）。"""
    global _HITS, _MISSES
    with _LOCK:
        _ENTRIES.clear()
        _INFLIGHT.clear()
        _HITS = _MISSES = 0


# ===========================================================================
# 请求级时限：调用方用 deadline_scope() 划一个总预算，下游每次 LLM 调用都遵守它
# ===========================================================================
_DEADLINE = threading.local()


@contextlib.contextmanager
def deadline_scope(seconds: Optional[float]):
    """在一段代码内设定总时限（秒）。嵌套时取"更紧"的那个预算。

    退出后恢复外层预算。seconds=None 表示不限制。
    """
    previous = getattr(_DEADLINE, "at", None)
    if seconds is None or seconds <= 0:
        _DEADLINE.at = previous
        try:
            yield None
        finally:
            _DEADLINE.at = previous
        return
    candidate = time.monotonic() + float(seconds)
    _DEADLINE.at = candidate if previous is None else min(previous, candidate)
    try:
        yield _DEADLINE.at
    finally:
        _DEADLINE.at = previous


@contextlib.contextmanager
def deadline_scope_at(absolute_at: Optional[float]):
    """按"绝对截止时刻"设预算（与 deadline_scope 同样的单调时钟基准）。

    用途：工作线程继承调用线程的预算。`_DEADLINE` 是 threading.local，Agent 的工具链
    跑在子线程里，看不到主线程设的预算（2026-10-15 检验报告发现 6：主线程预算耗尽时
    子线程仍计划等满 30s）。在子线程里用本上下文套上主线程传来的 at 即可对齐。
    """
    previous = getattr(_DEADLINE, "at", None)
    if absolute_at is None:
        _DEADLINE.at = previous
        try:
            yield previous
        finally:
            _DEADLINE.at = previous
        return
    _DEADLINE.at = absolute_at if previous is None else min(previous, absolute_at)
    try:
        yield _DEADLINE.at
    finally:
        _DEADLINE.at = previous


def deadline_at() -> Optional[float]:
    """当前线程的绝对截止时刻（time.monotonic 基准）；未设预算时返回 None。"""
    return getattr(_DEADLINE, "at", None)


def remaining_seconds() -> Optional[float]:
    """当前请求还剩多少秒；未设预算时返回 None。"""
    at = getattr(_DEADLINE, "at", None)
    if at is None:
        return None
    return at - time.monotonic()


def budget_exhausted() -> bool:
    """预算是否已用尽。"""
    left = remaining_seconds()
    return left is not None and left <= 0


def attempt_timeout(default: float) -> float:
    """单次 HTTP 尝试该等多久：不超过剩余预算，也不小于 LLM_MIN_ATTEMPT_S。

    2026-10-22 检验报告发现 4：原来的下限写死 `max(1.0, …)`，预算只剩 0.2s 时
    仍会发一次完整尝试，单条链路因此超出预算。下限改为可配（默认 0.25s），
    并在每次尝试之前先查一次预算（见 `llm_client._chat_raw_once`）。
    """
    left = remaining_seconds()
    if left is None:
        return default
    try:
        floor = float(os.environ.get("LLM_MIN_ATTEMPT_S") or 0.25)
    except (TypeError, ValueError):
        floor = 0.25
    return max(max(0.0, floor), min(default, left))
