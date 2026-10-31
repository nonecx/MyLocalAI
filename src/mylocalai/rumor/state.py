# -*- coding: utf-8 -*-
"""单次分析的共享状态：每个分析组件最多计算一次，线程安全。

为什么需要它：
  1. 工具不让大模型转抄原文——文本由本对象持有，工具无参数，避免原文被改写/截断。
  2. LangGraph 的 ToolNode 会并行执行同一轮里的多个工具调用；结果放在这里，
     风险评估工具不必依赖大模型把 JSON 来回传递（那样容易出错）。
  3. 每个组件用独立的锁 + 缓存实现 compute-once：即使大模型重复调用同一工具，
     或者评估工具与分析工具同时运行，也不会重复消耗 API。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

COMPONENTS = ("markers", "emotion", "aggression", "logic", "claims", "evidence")

def default_funcs(state: "AnalysisState | None" = None) -> Dict[str, Callable[[str], Dict[str, Any]]]:
    """rumor 内部组件的默认实现（延迟导入，避免仅导入本模块就加载全部依赖）。

    evidence 组件依赖 claims（构造搜索词）与 markers（补搜依据），通过闭包引用
    state 在计算时按需 ensure——组件锁各自独立且 claims 不反向依赖 evidence，
    不存在死锁。

    emotion/aggression/logic 三个 classic 组件**不在**这里导入：依赖方向只允许
    上层组装下层，由入口层（web/cli/mcp）构造 RumorAgent 时经 funcs 注入
    （2026-10-30 M1-2 装配倒转）。未注入时 ensure() 返回带 error 的结果，不崩。
    """
    from .claims import extract_claims
    from .evidence import collect_evidence
    from .patterns import scan_markers

    def _collect_evidence(text: str) -> Dict[str, Any]:
        return collect_evidence(
            text,
            claims=state.ensure("claims") if state is not None else None,
            markers=state.peek("markers") if state is not None else None,
            source_profile=getattr(state, "source_profile", None) if state is not None else None,
        )

    return {
        "markers": scan_markers,
        "claims": extract_claims,
        "evidence": _collect_evidence,
    }


class AnalysisState:
    def __init__(self, text: str, funcs: Optional[Dict[str, Callable]] = None,
                 on_component_done: Optional[Callable[[str], None]] = None,
                 source_profile: Optional[Dict[str, Dict[str, int]]] = None) -> None:
        self.text = text
        merged = default_funcs(self)
        if funcs:
            merged.update(funcs)
        self._funcs = merged
        # 进度回调（M2 对引擎的唯一侵入）：组件算完时回调一次组件名；默认 None，
        # 不传时行为逐位不变（守卫测试 tests/test_progress_hook.py 锁定）。
        self.on_component_done = on_component_done
        # 信源画像（L4 参谋）：档案库沉淀的 {域名: {"refutes": n}}。默认 None=不启用，
        # 默认 evidence 闭包只在非空时把它透传给 collect_evidence 做反哺抬分，
        # 不传时输出逐位不变（守卫测试 tests/test_source_profile_lift.py 锁定）。
        self.source_profile = source_profile
        self._results: Dict[str, Dict[str, Any]] = {}
        self._locks = {name: threading.Lock() for name in COMPONENTS}
        self.timings: Dict[str, float] = {}
        # 各组件相对本次分析开始的 [起始, 结束] 偏移（秒）。有了它才能断言"组件是并行的"，
        # 而不是拿端到端墙钟去猜（那样测的是机器闲不闲，见检验报告发现 3/6/9/11）。
        self._t0 = time.perf_counter()
        self.spans: Dict[str, List[float]] = {}
        self._browser: Any = None
        self._browser_lock = threading.Lock()
        # 协作取消：Agent 超时后调用 cancel()，后续组件不再开算（尤其是不再发起联网/LLM）。
        # 这只是"不再开始新的工作"，不打断已经在跑的那一次调用——Python 线程没有抢占式中断。
        self._cancelled = threading.Event()

    def cancel(self, reason: str = "分析已取消") -> None:
        """标记取消（幂等）。用于 Agent 超时后阻止后台线程继续消耗 API/网络。"""
        self._cancelled.set()
        self.cancel_reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def get_browser(self) -> Any:
        """浏览会话：证据本完成后创建，种子为证据本全部条目 URL。"""
        from mylocalai.retrieval.browser import BrowserSession
        with self._browser_lock:
            if self._browser is None:
                ev = self._results.get("evidence") or {}
                urls = [e.get("url") for e in (ev.get("entries") or [])
                        if isinstance(e, dict) and e.get("url")]
                self._browser = BrowserSession(urls)
            return self._browser

    def ensure(self, name: str) -> Dict[str, Any]:
        """取组件结果；没算过就算一次。组件函数抛异常时记录为带 error 的空结果。

        已取消时不再开算，直接返回带 error 的结果——这样 Agent 超时返回后，
        残留在后台的工具调用不会继续发起 LLM/联网请求。
        """
        with self._locks[name]:
            if name not in self._results:
                if self.cancelled:
                    self._results[name] = {"error": getattr(self, "cancel_reason", "分析已取消")}
                    return self._results[name]
                if name not in self._funcs:
                    self._results[name] = {"error": "组件未装配（classic 组件需由入口层注入）"}
                    return self._results[name]
                t0 = time.perf_counter()
                try:
                    result = self._funcs[name](self.text)
                    if not isinstance(result, dict):
                        raise TypeError(f"组件返回了非 dict: {type(result).__name__}")
                except Exception as exc:  # noqa: BLE001  组件本应自带容错，这里是最后防线
                    result = {"error": f"组件异常: {type(exc).__name__}: {exc}"}
                self._results[name] = result
                elapsed = time.perf_counter() - t0
                self.timings[name] = round(elapsed, 2)
                self.spans[name] = [round(t0 - self._t0, 3), round(time.perf_counter() - self._t0, 3)]
                if self.on_component_done is not None:
                    try:
                        self.on_component_done(name)
                    except Exception:  # noqa: BLE001  进度回调绝不影响分析本身
                        pass
            return self._results[name]

    def peek(self, name: str) -> Optional[Dict[str, Any]]:
        """只读已有结果，不触发计算（超时场景下使用）。"""
        return self._results.get(name)

    def completed(self) -> list:
        return [n for n in COMPONENTS if n in self._results]
