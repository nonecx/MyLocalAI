# -*- coding: utf-8 -*-
"""巡检调度器（platform/scheduler.py）：后台线程按间隔自动巡检。

两种通道（计划决策 3）：
  - **web 内置**：服务启动时 start()（规则 enabled=false 时是空操作，不巡检）；
  - **CLI 一次性**：`python -m mylocalai.platform --once`（可挂 Windows 计划任务），
    不经本模块，直接跑 PlatformRunner.scan()。

设计约束：
  - daemon 线程：进程退出不被拖住；停机经 stop() 协作取消（在跑的扫描以
    partial/cancelled 收尾，已完成条目保留）；
  - **单飞**：调度循环与手动触发（web POST /api/platform/scan）互斥，同一时刻
    只有一个扫描在跑（_scan_lock）；
  - 规则**每轮重读**：间隔、开关改了下次循环即生效，无需重启服务；
  - 启动即跑一次（服务重启后补看"停机期间"的变化），之后按间隔；
  - 巡检异常永不逃出线程：全部收进 _last_scan（status=failed）。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from mylocalai.platform.runner import PlatformRunner
from mylocalai.platform.rules import load_rules

__all__ = ["PlatformScheduler"]

#: stop() 等待循环线程退出的最长时间（秒）。扫描本身可能还在跑（协作取消中），
#: 不等它——线程是 daemon，且取消后它自己会收尾。
_JOIN_TIMEOUT_S = 10.0

#: 间隔下限（秒）：防配置写个 0 把 CPU 打满。
_MIN_INTERVAL_S = 10


def _interval_of(rules: Dict[str, Any]) -> int:
    """规则间隔 → 实际间隔（秒）。None/坏值退 900；0 是显式值（由下限兜底）——
    不用 `x or 900`：那会把显式的 0 偷偷换成默认值。"""
    try:
        raw = rules.get("scan_interval_s")
        value = int(raw) if raw is not None else 900
    except (TypeError, ValueError):
        value = 900
    return max(_MIN_INTERVAL_S, value)


class PlatformScheduler:
    """后台巡检调度门面。runner 可注入（测试换 stub）。"""

    def __init__(self, runner: Optional[PlatformRunner] = None,
                 rules_loader: Optional[Callable[[], tuple]] = None) -> None:
        self.runner = runner or PlatformRunner()
        self._rules_loader = rules_loader or load_rules
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._scan_lock = threading.Lock()
        self._last_scan: Optional[Dict[str, Any]] = None
        self._last_error = ""

    # ---- 生命周期 ----

    def start(self) -> bool:
        """启动调度循环。规则 enabled=false → 空操作返回 False（默认不打扰）。"""
        rules, _ = self._rules_loader()
        if not rules.get("enabled"):
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop_event.clear()
        thread = threading.Thread(target=self._loop, name="platform-scheduler",
                                  daemon=True)
        self._thread = thread
        thread.start()
        return True

    def stop(self, timeout: float = _JOIN_TIMEOUT_S) -> bool:
        """请求停止并等待循环线程退出。返回 True 表示已干净停止。"""
        self._stop_event.set()          # 同时是在跑扫描的取消信号
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        return not (thread is not None and thread.is_alive())

    # ---- 手动触发（web POST /api/platform/scan 用）----

    def trigger_async(self, queries: Optional[List[str]] = None) -> bool:
        """后台线程跑一次扫描。已有扫描在跑 → False（单飞，不排队不叠加）。

        极小的竞态窗口（locked() 判断到线程启动之间）只可能造成一次"误报
        忙"，最坏多等一个间隔，不会重复跑。
        """
        if self._scan_lock.locked():
            return False
        threading.Thread(target=self._run_scan, args=(queries,),
                         name="platform-scan-manual", daemon=True).start()
        return True

    # ---- 状态 ----

    def status(self) -> Dict[str, Any]:
        """调度器状态快照（web /api/platform/status 用）。last_scan 只带摘要，
        条目清单走 /api/platform/feed——状态响应不背全量数据。"""
        rules, warnings = self._rules_loader()
        last = self._last_scan or {}
        last_summary: Dict[str, Any] = {}
        if last:
            last_summary = {
                "scan_id": last.get("scan_id"),
                "status": last.get("status"),
                "counters": last.get("counters") or {},
                "error": last.get("error", ""),
                "warnings": (last.get("warnings") or [])[:5],
                "queries": [{"query": q.get("query"), "e": q.get("e"),
                             "candidates": q.get("candidates")}
                            for q in (last.get("queries") or [])[:5]],
            }
        return {
            "enabled": bool(rules.get("enabled")),
            "interval_s": _interval_of(rules),
            "running": self._thread is not None and self._thread.is_alive(),
            "scan_in_progress": self._scan_lock.locked(),
            "last_scan": last_summary,
            "last_error": self._last_error,
            "rule_warnings": warnings,
        }

    # ---- 内部 ----

    def _loop(self) -> None:
        """循环体：启动即跑一次，之后每间隔一次；停止/关开关即时响应。"""
        while not self._stop_event.is_set():
            rules, _ = self._rules_loader()
            self._run_scan(None)                     # 启动即跑（补看停机期间）
            if self._stop_event.is_set():
                return
            interval = _interval_of(rules)
            if self._stop_event.wait(interval):
                return                               # 等待中收到停止
            rules, _ = self._rules_loader()
            if not rules.get("enabled"):
                continue                             # 运行中被关开关：空转等待

    def _run_scan(self, queries: Optional[List[str]]) -> Dict[str, Any]:
        """带单飞锁跑一次扫描；任何异常收成 failed 摘要，绝不逃出线程。"""
        with self._scan_lock:
            try:
                summary = self.runner.scan(
                    queries=queries, cancel_check=self._stop_event.is_set)
            except Exception as exc:  # noqa: BLE001  调度器永不允许因巡检崩掉
                summary = {"scan_id": None, "status": "failed",
                           "counters": {"queries": 0, "candidates": 0, "verified": 0,
                                        "deduped": 0, "degraded": 0},
                           "entries": [], "queries": [],
                           "error": f"scheduler: {type(exc).__name__}: {exc}",
                           "warnings": []}
            self._last_scan = summary
            self._last_error = (summary.get("error") or ""
                                if summary.get("status") == "failed" else "")
            return summary
