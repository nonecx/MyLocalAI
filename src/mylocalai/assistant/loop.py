# -*- coding: utf-8 -*-
"""助手外层循环（工作指导 §4.2 loop.py）：收消息 → 拒绝预检 → 路由 → 工具执行 → 叙述 → 落库。

事件顺序契约（§5.3，与 web 层 chat.js 对齐，不得偏离）：
    meta{session_id, run_id} → status{stage:"routing"} → tool_start → scan（仅 verify 类）
    → progress*（verify 的 on_component_done）→ card → narrative* → tool_end → done
    异常出口 error{code, recoverable, message}，已知 code：cancelled（用户断开）、
    deadline（轮预算耗尽）、tool_failed（工具异常）、internal（未预期异常）。
    **卡片永远在叙述之前**；叙述失败也不丢卡片。

单轮工具上限：rules["max_tool_calls_per_turn"]（默认 2）。M2 的路由一轮恒只产出 1 个
工具 action，本模块在结构上就是"计划列表 + 上限封顶"的执行循环——上限是给 M3 指纹
"先问再查"的第二槽位预留的（先秒回旧结论、后台再重查，见 §1 决策），测试锁定超限不执行。

轮预算（§8）：进入 handle 即以 llm_cache.deadline_scope_at 设定本回合总时限
（环境变量 CHAT_TURN_DEADLINE_S 优先，缺省 rules["turn_deadline_s"]=180s；<=0 不限），
下游 verify（RumorAgent）/speech（classic.judge）/各 LLM 调用经 deadline_at() 自动继承
（与 JUDGE_DEADLINE_S 同一继承机制，嵌套取更紧者）。每次工具执行前查剩余时间：不足则
不再开新工具，emit error{code:"deadline", recoverable}，**已有 card 照发（永不因超时丢
结论）**，tool_run 记 status="timeout"，回复用 prompts["deadline_reply"] 诚实降级文案。

降级文化（§5.7）：路由失败 → clarify；工具异常 → error 事件 + 诚实降级回复 + run 状态
error，会话不崩；叙述失败 → 卡片 + 固定模板收尾，**永不因叙述失败丢结论**。
"""

from __future__ import annotations

import json
import os
import re
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from mylocalai.llm import llm_cache
from mylocalai.assistant import router as router_mod
from mylocalai.assistant import tools_heat, tools_memory, tools_verify
from mylocalai.assistant.context import build_context
from mylocalai.assistant.events import EventStream
from mylocalai.assistant.registry import MAX_TEXT_LENGTH, ToolRegistry

__all__ = ["AssistantLoop"]

#: 分句假流式（§5.5）：按中英文句读切 delta，llm_client 零改动。
_SENTENCE_RE = re.compile(r"[^。！？!?…\n]*[。！？!?…\n]+|[^。！？!?…\n]+$")

#: 会话摘要落库的前缀（system_note）；下次 build_context 从最近一条取 cached_summary。
_SUMMARY_PREFIX = "[早期对话摘要]"

#: 工具 → 卡片类型（card 事件的 card_type；web cards.js 按它分发渲染器）。
_CARD_TYPE = {
    "verify_claim": "conclusion", "recheck_dossier": "conclusion", "get_dossier": "conclusion",
    "analyze_speech": "speech",
    "check_heat": "heat", "compute_heat": "heat",
}

_ARGS_PREVIEW_WIDTH = 60


def _preview(args: Dict[str, Any], width: int = _ARGS_PREVIEW_WIDTH) -> str:
    """tool_start 的 args 预览（截断，不泄全文）。"""
    try:
        return json.dumps(args, ensure_ascii=False)[:width]
    except Exception:  # noqa: BLE001
        return str(args)[:width]


def _split_sentences(text: str) -> List[str]:
    parts = [p for p in _SENTENCE_RE.findall(str(text or "")) if p and p.strip()]
    return parts or ([str(text)] if str(text or "").strip() else [])


class _ServiceBox:
    """服务注入盒（编排者契约）：注入优先，缺省键惰性 import 真实引擎。

    测试只注入 stub（全离线）；生产构造 AssistantLoop() 时不 import 任何重引擎，
    首次真正用到某服务才装配。handler 统一经 ctx.services[name] 取服务。
    """

    def __init__(self, overrides: Optional[Dict[str, Any]], rules: Dict[str, Any]) -> None:
        self._over: Dict[str, Any] = dict(overrides or {})
        self._rules = rules or {}
        self._real: Dict[str, Any] = {}

    def __getitem__(self, name: str) -> Callable:
        if name in self._over:
            return self._over[name]
        if name not in self._real:
            self._real[name] = _real_service(name, self._rules)
        return self._real[name]

    def __contains__(self, name: str) -> bool:
        return True  # 所有契约内服务都可取（缺省走真实引擎）


def _real_service(name: str, rules: Dict[str, Any]) -> Callable:
    """真实引擎的惰性装配（只在生产路径被触发；测试注入 stub 后永不到这里）。"""
    if name == "verify":
        def _verify(text: str, deep: bool = False, on_component_done: Optional[Callable] = None,
                    cancel_check: Optional[Callable] = None) -> Dict[str, Any]:
            from mylocalai.rumor.agent import RumorAgent
            # classic 三维组件由入口层装配注入（依赖方向：assistant → {classic, rumor}），
            # 与 web 层 get_rumor_agent 的构造同参——不注入 funcs 会让 emotion/aggression/
            # logic 三个组件在生产路径永远缺席。
            from mylocalai.classic.language import analyze_aggression, analyze_emotion
            from mylocalai.classic.logic import analyze_logic
            agent = RumorAgent(
                max_chars=MAX_TEXT_LENGTH,
                funcs={
                    "emotion": analyze_emotion,
                    "aggression": analyze_aggression,
                    "logic": analyze_logic,
                },
                on_component_done=on_component_done,
            )
            # 时限继承（§8）：analyze() 内部 deadline_scope(self.deadline_s) 与本线程已设的
            # 回合预算（handle 的 deadline_scope_at）取 min——轮预算自动收紧引擎预算；
            # cancel_check：回合级取消在 loop 层收口；引擎内协作取消（AnalysisState.cancel）
            # 的接线留 M3——analyze() 当前不接受取消回调，此参数按服务契约占位。
            return agent.analyze(text)
        return _verify

    if name == "speech":
        def _speech(text: str) -> Dict[str, Any]:
            from mylocalai.classic.judge import judge
            return judge(text)
        return _speech

    if name == "heat":
        def _heat(topic: str) -> Dict[str, Any]:
            from mylocalai.attention.attach import attention_for_text
            detail = dict(attention_for_text(query=str(topic), allow_network=True) or {})
            detail.setdefault("profile", "")
            quality = str(detail.get("quality", ""))
            # 占位（baseline）/检索失败回退（fallback）/无结果（missing）都如实算降级；
            # "estimated"（代理值兜底但检索成功）不算降级，靠 proxy_flags 标注口径。
            detail["degraded"] = quality in ("baseline", "fallback", "missing")
            if not detail.get("proxy_flags") and detail.get("estimated"):
                detail["proxy_flags"] = ["y_proxy", "z_proxy"]  # 阅读量/留言量是代理值
            return detail
        return _heat

    if name == "heat_formula":
        def _heat_formula(x: float, y: float, z: float) -> Dict[str, Any]:
            from mylocalai.attention import attention as attn
            detail = attn.score_detail(float(x), float(y), float(z))
            detail.setdefault("profile", attn.DEFAULT_PROFILE)
            return detail
        return _heat_formula

    if name == "search":
        def _search(query: str) -> Dict[str, Any]:
            from mylocalai.retrieval.search import search_all
            raw = search_all(str(query), k=8)
            engines = raw.get("engines") or {}
            degraded = not any((e or {}).get("ok") for e in engines.values())
            return {"results": raw.get("results") or [], "engines": engines,
                    "degraded": degraded}
        return _search

    if name == "route_llm":
        cfg = (rules or {}).get("route_model") or {}

        def _route_llm(system: str, user: str) -> Tuple[Optional[Dict[str, Any]], str]:
            from mylocalai.llm import llm_client
            return llm_client.chat_json(system, user,
                                        temperature=float(cfg.get("temperature", 0)),
                                        max_tokens=int(cfg.get("max_tokens", 300)),
                                        namespace=str(cfg.get("cache_namespace", "assistant_router")))
        return _route_llm

    if name == "narrate_llm":
        cfg = (rules or {}).get("narrate_model") or {}

        def _narrate_llm(system: str, user: str) -> Tuple[Optional[str], str]:
            from mylocalai.llm import llm_client
            return llm_client.chat_text(system, user,
                                        temperature=float(cfg.get("temperature", 0)),
                                        max_tokens=int(cfg.get("max_tokens", 700)),
                                        namespace=str(cfg.get("cache_namespace", "assistant_narrate")))
        return _narrate_llm

    if name == "summarize_llm":
        cfg = (rules or {}).get("summary_model") or {}

        def _summarize_llm(system: str, user: str) -> Tuple[Optional[str], str]:
            from mylocalai.llm import llm_client
            return llm_client.chat_text(system, user,
                                        temperature=float(cfg.get("temperature", 0)),
                                        max_tokens=int(cfg.get("max_tokens", 400)),
                                        namespace=str(cfg.get("cache_namespace", "assistant_sum")))
        return _summarize_llm

    raise KeyError(f"未知服务: {name}")


def _pkg_json(name: str) -> Dict[str, Any]:
    import os
    path = os.path.join(os.path.dirname(__file__), name)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001  配置写坏退空 dict（各键取代码内默认）
        return {}


class AssistantLoop:
    """一回合对话的编排器：路由 → 工具 → 叙述 → 落库，全部依赖可注入。"""

    def __init__(self, registry: Optional[ToolRegistry] = None,
                 sessions: Any = None, dossiers: Any = None,
                 services: Optional[Dict[str, Any]] = None,
                 rules: Optional[Dict[str, Any]] = None,
                 prompts: Optional[Dict[str, Any]] = None) -> None:
        self._registry = registry
        self._sessions = sessions
        self._dossiers = dossiers
        self._services = services
        self._service_box: Optional[_ServiceBox] = None
        self._rules = rules
        self._prompts = prompts

    # ---- 惰性缺省（构造零开销，web 首请求才装配） ----

    @property
    def rules(self) -> Dict[str, Any]:
        if self._rules is None:
            self._rules = _pkg_json("assistant_rules.json")
        return self._rules

    @property
    def prompts(self) -> Dict[str, Any]:
        if self._prompts is None:
            self._prompts = _pkg_json("prompts.json")
        return self._prompts

    @property
    def registry(self) -> ToolRegistry:
        if self._registry is None:
            reg = ToolRegistry()
            tools_verify.register_all(reg, None)
            tools_heat.register_all(reg, None)
            tools_memory.register_all(reg, None)
            self._registry = reg
        return self._registry

    @property
    def sessions(self) -> Any:
        if self._sessions is None:
            from mylocalai.assistant.session_store import SessionStore
            self._sessions = SessionStore()
        return self._sessions

    @property
    def dossiers(self) -> Any:
        if self._dossiers is None:
            from mylocalai.dossier.service import DossierService
            self._dossiers = DossierService()
        return self._dossiers

    @property
    def services(self) -> _ServiceBox:
        if self._service_box is None:
            self._service_box = _ServiceBox(self._services, self.rules)
        return self._service_box

    # ---- 对外主入口 ----

    def handle(self, session_id: int, user_text: str,
               events: Optional[EventStream] = None,
               cancel_check: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        """处理一回合对话。永不抛异常：任何未捕获异常都折成 error 事件收尾。

        轮预算（§8）：进入即以 deadline_scope_at 设定本回合总时限（环境变量
        CHAT_TURN_DEADLINE_S 优先，缺省 rules["turn_deadline_s"]=180；<=0 不限），
        下游 RumorAgent/classic/LLM 链路经 llm_cache.deadline_at() 自动继承
        （与 JUDGE_DEADLINE_S 同一机制：嵌套 scope 取更紧者）。
        """
        if events is None:
            events = EventStream()
        result: Dict[str, Any] = {"session_id": session_id, "reply": "", "action": "",
                                  "card": None, "tool_run_id": None, "dossier_id": None,
                                  "degraded": False, "cost": {"requests": 0, "tokens": 0}}
        budget = self._turn_budget_s()
        at = time.monotonic() + budget if budget is not None else None
        try:
            with llm_cache.deadline_scope_at(at):
                self._turn(int(session_id), str(user_text or ""), events, cancel_check, result)
        except Exception as exc:  # noqa: BLE001  最外层防线：会话不崩
            try:
                events.emit("error", code="internal", recoverable=False,
                            message=f"助手处理异常: {type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass
            result["degraded"] = True
        return result

    # ---- 轮预算（§8） ----

    def _turn_budget_s(self) -> Optional[float]:
        """本回合预算秒数：环境变量 CHAT_TURN_DEADLINE_S 优先，缺省 rules 值；<=0 不限。

        环境变量写坏（非 float）按未设处理，退回 rules 值——配置错误不放大成故障。
        """
        raw = str(os.environ.get("CHAT_TURN_DEADLINE_S") or "").strip()
        if raw:
            try:
                env_value = float(raw)
            except ValueError:
                env_value = None
            if env_value is not None:
                return env_value if env_value > 0 else None
        try:
            value = float(self.rules.get("turn_deadline_s", 180.0))
        except (TypeError, ValueError):
            value = 180.0
        return value if value > 0 else None

    @staticmethod
    def _budget_left() -> Optional[float]:
        """本回合剩余预算秒数（未设预算/不限 → None，调用方据此跳过检查）。"""
        return llm_cache.remaining_seconds()

    # ---- 回合实现 ----

    def _cancelled(self, cancel_check: Optional[Callable[[], bool]]) -> bool:
        try:
            return bool(cancel_check()) if cancel_check is not None else False
        except Exception:  # noqa: BLE001  取消探测自身失败不视为取消
            return False

    def _route_plan(self, user_text: str) -> Dict[str, Any]:
        """路由 → 执行计划（refusal / meta / tools 三种）。

        M2 路由恒产出单个工具 action；这里返回"计划列表"的形状是为 M3 指纹
        "先问再查"的第二槽位预留（先秒回旧结论、后台再重查），执行侧已按
        max_tool_calls_per_turn 封顶。
        """
        res = router_mod.route(user_text, self.registry, self.services["route_llm"],
                               self.rules, self.prompts)
        if res["action"] in router_mod.META_ACTIONS:
            return {"kind": "meta", "action": res["action"],
                    "reply": res.get("reply", ""), "error": res.get("error", "")}
        return {"kind": "tools", "steps": [{"action": res["action"], "args": res.get("args", {})}]}

    def _turn(self, session_id: int, user_text: str, events: EventStream,
              cancel_check: Optional[Callable[[], bool]], result: Dict[str, Any]) -> None:
        rules, prompts = self.rules, self.prompts
        sessions, dossiers, services = self.sessions, self.dossiers, self.services
        cost = result["cost"]

        # ① 用户消息落库；历史快照定在"本回合产物之前"（叙述吃 digest，不吃卡片 JSON）
        sessions.add_message(session_id, "user", user_text)
        history = sessions.messages(session_id)

        # ② 拒绝词表预检：注入指令在进路由模型之前就被拦下（§5.6 对话层第一道闸）
        refusal = router_mod.pre_refuse(user_text, rules, prompts)
        if refusal is not None:
            # 预检是纯词表子串匹配（零 LLM 成本）；拒绝不进路由模型
            plan: Dict[str, Any] = {"kind": "refusal", "reply": refusal["reply"], "error": ""}
        else:
            cost["requests"] += 1  # 路由模型被咨询一次（失败也算一次尝试，M4 接真实计数）
            plan = self._route_plan(user_text)

        if plan["kind"] == "refusal":
            events.emit("meta", session_id=session_id, run_id=None)
            events.emit("status", stage="routing")
            reply = str(plan["reply"])
            self._say(events, reply)
            sessions.add_message(session_id, "assistant", reply, kind="refusal")
            result.update(action="refuse", reply=reply)
            events.emit("done", cost=cost)
            return

        if plan["kind"] == "meta":
            events.emit("meta", session_id=session_id, run_id=None)
            events.emit("status", stage="routing")
            action = str(plan["action"])
            reply = str(plan.get("reply") or "")
            narrated = False
            if action == "chitchat" and not reply.strip():
                # chitchat 路由没给 reply → narrate_llm 补一句（§5.1）；再失败用固定话术
                text, _err = self._narrate(history, "")
                narrated = True
                cost["requests"] += 1
                reply = (text or "").strip() or str(prompts.get("chitchat_fallback", ""))
            self._say(events, reply)
            sessions.add_message(session_id, "assistant", reply, kind="text")
            result.update(action=action, reply=reply)
            self._maybe_summarize(session_id, cost)
            events.emit("done", cost=cost)
            return

        # ③ 工具回合：先 start_run 拿 run_id，meta 永远是第一个事件（run_id 回传给前端绑定）
        max_calls = max(1, int(rules.get("max_tool_calls_per_turn", 2) or 2))
        steps = list(plan["steps"])[:max_calls]   # 上限封顶：超限的计划项不执行（M3 槽位预留）
        run_id = sessions.start_run(session_id, steps[0]["action"], steps[0]["args"])
        events.emit("meta", session_id=session_id, run_id=run_id)
        events.emit("status", stage="routing")

        executed: List[Dict[str, Any]] = []
        deadline_hit = False   # 轮预算已耗尽：不再开新工具；已有 card 照发（永不因超时丢结论）
        for idx, step in enumerate(steps):
            if idx > 0:
                run_id = sessions.start_run(session_id, step["action"], step["args"])
            if self._cancelled(cancel_check):
                # 协作取消（§5.3 取消链）：本轮 run 记 cancelled，error 后收尾——
                # web 端 drain 对 error 收尾已兼容；不再叙述（断开后也送不到前端）。
                sessions.finish_run(run_id, status="cancelled")
                events.emit("error", code="cancelled", recoverable=True,
                            message="本轮已取消（用户断开或请求取消）。")
                result.update(action=str(step["action"]), tool_run_id=run_id, degraded=True)
                return
            # 轮预算闸（§8）：剩余时间不足则不再开新工具。拦在 tool_start 之前——
            # 不开工就不预告；本 run 如实记 timeout，走 error + 诚实降级文案收尾。
            left = self._budget_left()
            if left is not None and left <= 0:
                deadline_hit = True
                sessions.finish_run(run_id, status="timeout", degraded=True)
                events.emit("error", code="deadline", recoverable=True,
                            message="本轮核查时间预算已用尽，不再开始新的取证。")
                result.update(action=str(step["action"]), tool_run_id=run_id, degraded=True)
                break
            spec = self.registry.get(step["action"])
            est_s = float((spec or {}).get("est_s", 0.0))
            events.emit("tool_start", tool=str(step["action"]),
                        args_preview=_preview(step["args"]), est_s=est_s)

            # ④ 慢查先发本地初扫（零成本 markers，§5.3：仅 verify 类）
            scan_text = self._scan_text(step, dossiers)
            if scan_text is not None:
                chips = self._scan_chips(scan_text)
                events.emit("scan", markers=chips, text=self._scan_intro(chips, prompts))

            t0 = time.perf_counter()
            tool_error = ""
            out: Dict[str, Any] = {}
            try:
                ctx = SimpleNamespace(sessions=sessions, dossiers=dossiers, events=events,
                                      cancel_check=cancel_check, services=services)
                if spec is None:
                    tool_error = f"工具未注册: {step['action']}"
                else:
                    out = spec["handler"](ctx, **step["args"])
                    if not isinstance(out, dict):
                        out, tool_error = {}, f"工具返回了非对象: {type(out).__name__}"
            except Exception as exc:  # noqa: BLE001  工具炸了会话不炸（§6 M2-3 验收）
                out, tool_error = {}, f"{type(exc).__name__}: {exc}"
            duration = time.perf_counter() - t0

            # 工具后取消轮询（§5.3 取消链）：慢工具执行期间用户断开 → 协作收工。
            # 不再发卡片/叙述（断开后也送不到前端）；run 记 cancelled（已建档则带上
            # dossier_id，结论已在档案库不丢）。
            if self._cancelled(cancel_check):
                sessions.finish_run(run_id, status="cancelled", degraded=True,
                                    duration_s=round(duration, 3),
                                    dossier_id=out.get("dossier_id"))
                events.emit("error", code="cancelled", recoverable=True,
                            message="本轮已取消（用户断开或请求取消）。")
                result.update(action=str(step["action"]), tool_run_id=run_id, degraded=True)
                return

            if tool_error:
                out = {"ok": False, "card": None, "digest": "",
                       "cost": {"requests": 0, "tokens": 0}, "dossier_id": None,
                       "degraded": True}
                events.emit("error", code="tool_failed", recoverable=True,
                            message=f"工具 {step['action']} 执行失败：{tool_error}")
            left = self._budget_left()
            timed_out = left is not None and left <= 0
            if timed_out:
                deadline_hit = True   # 预算已尽：后续不再开新工具/叙述调用（§8）
            status = ("error" if tool_error
                      else "timeout" if timed_out
                      else ("ok" if out.get("ok", True) else "degraded"))
            executed.append({"step": step, "run_id": run_id, "out": out,
                             "duration": duration, "status": status, "error": tool_error})

            # ⑤ 卡片永远在叙述之前；卡片同时以 kind="card" 落库
            card = out.get("card")
            if isinstance(card, dict) and card:
                self._emit_card(events, sessions, session_id,
                                _CARD_TYPE.get(str(step["action"]), "conclusion"), card, run_id)
                result["card"] = card
            cost["requests"] += int((out.get("cost") or {}).get("requests", 0))
            cost["tokens"] += int((out.get("cost") or {}).get("tokens", 0))
            if out.get("dossier_id") is not None:
                result["dossier_id"] = out.get("dossier_id")
            result["degraded"] = bool(result["degraded"] or out.get("degraded")
                                      or status in ("error", "timeout", "degraded"))
            result["tool_run_id"] = run_id

            if timed_out and not tool_error:
                # 轮预算在工具执行期间耗尽：已有 card 照发（上方），error 后不再开新工具，
                # 叙述改用诚实降级文案（预算已尽，不再发起叙述调用）。工具自身已报错的
                # 回合只发 tool_failed 一条 error（不重复报 budget 账）。
                events.emit("error", code="deadline", recoverable=True,
                            message="本轮核查时间预算已用尽，取证未能在预算内完成。")
                break

        # ⑤' 叙述前取消轮询：工具全部返回后用户才断开 → 不再叙述（送不到前端）。
        # 已完成的 run 如实记 cancelled（结论已落档案库，不丢）；带 dossier_id 收尾。
        if self._cancelled(cancel_check):
            for e in executed:
                sessions.finish_run(e["run_id"], status="cancelled", degraded=True,
                                    duration_s=round(e["duration"], 3),
                                    dossier_id=e["out"].get("dossier_id"))
            events.emit("error", code="cancelled", recoverable=True,
                        message="本轮已取消（用户断开或请求取消）。")
            result["degraded"] = True
            return

        # ⑥ 叙述（narrate_llm；失败退固定模板——永不因叙述失败丢结论）
        digest = "\n\n".join(str(e["out"].get("digest") or "") for e in executed
                             if e["out"].get("digest"))
        failed = [e for e in executed if e["error"]]
        if deadline_hit:
            # 轮预算耗尽：不再发起叙述调用（预算同尽），用外置诚实降级文案收尾——
            # 已有卡片照发；没有卡片就如实说没有，绝不编一个结论补位（§5.7）。
            reply = str(prompts.get(
                "deadline_reply",
                "本轮核查时间预算已用尽，取证未能在预算内完成。已有结论以上方卡片为准。"))
            result["degraded"] = True
        elif failed and not digest:
            reply = str(prompts.get("degraded_reply", "深查暂时不可用（{reason}）。")).format(
                reason=failed[0]["error"])
        else:
            cost["requests"] += 1  # 叙述尝试一次
            text, _err = self._narrate(history, digest)
            if text and str(text).strip():
                reply = str(text).strip()
            else:
                reply = str(prompts.get("narrative_fallback", "核查完成，结论见上方卡片。要点：{digest}")
                            ).format(digest=digest or "（无工具输出要点）")
        self._say(events, reply)
        sessions.add_message(session_id, "assistant", reply, kind="text")
        result["reply"] = reply
        if executed:
            result["action"] = str(executed[-1]["step"]["action"])

        # ⑦ tool_end（叙述之后，§5.3 顺序）+ finish_run 只调一次（建档后补 dossier_id）
        for e in executed:
            out = e["out"]
            run_cost = out.get("cost") or {}
            degraded = bool(out.get("degraded")) or e["status"] in ("error", "timeout")
            sessions.finish_run(e["run_id"], status=e["status"], degraded=degraded,
                                duration_s=round(e["duration"], 3),
                                requests=int(run_cost.get("requests", 0)),
                                tokens=int(run_cost.get("tokens", 0)),
                                dossier_id=out.get("dossier_id"))
            events.emit("tool_end", tool=str(e["step"]["action"]),
                        duration_s=round(e["duration"], 3), degraded=degraded,
                        dossier_id=out.get("dossier_id"))

        if not deadline_hit:
            # 预算已尽的回合不再发起摘要 LLM 调用（§8：预算耗尽后不开新的上游调用）
            self._maybe_summarize(session_id, cost)
        events.emit("done", cost=cost)

    # ---- 组装件 ----

    def _emit_card(self, events: EventStream, sessions: Any, session_id: int,
                   card_type: str, card: Dict[str, Any], run_id: Optional[int]) -> None:
        """card 事件 + kind="card" 落库。

        载荷形状（编排者契约，web 端按此实现）：{"card_type": ..., "card": {...}}；
        额外把卡片字段并置在顶层——chat.js 的历史回放（renderHistoryMessage）把整包
        payload 交给 renderCard 后按 card_type 分发、直接读顶层字段；实时路径
        onCard 会先解 d.card，两种形状都成立（与 web 层实现逐一对过）。
        """
        payload: Dict[str, Any] = {"card_type": card_type, "card": card}
        payload.update({k: v for k, v in card.items() if k not in payload})
        events.emit("card", **payload)
        sessions.add_message(session_id, "assistant", json.dumps(payload, ensure_ascii=False),
                             kind="card", tool_run_id=run_id)

    def _say(self, events: EventStream, text: str) -> None:
        """分句假流式（§5.5 M2 方案）：整段文本按句切 delta 逐个 emit。"""
        for part in _split_sentences(text):
            try:
                events.emit("narrative", delta=part)
            except Exception:  # noqa: BLE001  事件流失败不挡落库与返回
                pass

    def _narrate(self, history: List[Dict[str, Any]], tool_digest: str) -> Tuple[Optional[str], str]:
        """叙述：消息数组 = build_context(narrate_system, 最近历史, tool_digest)。

        build_context 产出多角色数组，而 narrate_llm 契约是 (system, user) 双串——
        这里把 system 之外的部分并成一段 user 文本传入（预算裁剪仍由 build_context 负责）。
        """
        system = str(self.prompts.get("narrate_system", ""))
        try:
            budget = int(self.rules.get("context_char_budget") or 12000)
        except (TypeError, ValueError):
            budget = 12000
        msgs = build_context(system, history, tool_digest=tool_digest,
                             cached_summary=self._last_summary(history), budget=budget)
        user = "\n\n".join(str(m.get("content") or "") for m in msgs[1:])
        try:
            return self.services["narrate_llm"](system, user)
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    def _last_summary(self, history: List[Dict[str, Any]]) -> str:
        """最近一条摘要 system_note 的正文（无则空串 → build_context 走确定性拼接）。"""
        for m in reversed(history):
            if m.get("role") == "system_note":
                content = str(m.get("content") or "")
                if content.startswith(_SUMMARY_PREFIX):
                    return content[len(_SUMMARY_PREFIX):].strip()
                return ""
        return ""

    def _maybe_summarize(self, session_id: int, cost: Dict[str, int]) -> None:
        """超长会话摘要（§5.4）：距上一次摘要累计 trigger_messages 条消息才触发一次。

        只统计最近一次摘要之后的消息：摘要落库为 system_note 后即"覆盖"此前历史，
        避免每个回合都重复摘要；摘要失败静默退回 context.py 的确定性拼接（不落备注）。
        """
        cfg = self.rules.get("summary_model") or {}
        try:
            trigger = int(cfg.get("trigger_messages", 60))
        except (TypeError, ValueError):
            trigger = 60
        if trigger <= 0:
            return
        msgs = self.sessions.messages(session_id)
        last_summary_idx = -1
        for i, m in enumerate(msgs):
            if m.get("role") == "system_note" and str(m.get("content") or "").startswith(_SUMMARY_PREFIX):
                last_summary_idx = i
        if len(msgs) - 1 - last_summary_idx < trigger:
            return
        convo = "\n".join(f"{m.get('role')}: {str(m.get('content') or '')[:200]}"
                          for m in msgs if m.get("role") != "system_note")
        system = str(self.prompts.get("summary_system", ""))
        try:
            text, _err = self.services["summarize_llm"](system, convo)
        except Exception:  # noqa: BLE001
            return
        cost["requests"] += 1
        if text and str(text).strip():
            self.sessions.add_message(session_id, "system_note",
                                      f"{_SUMMARY_PREFIX} {str(text).strip()}", kind="text")

    def _scan_text(self, step: Dict[str, Any], dossiers: Any) -> Optional[str]:
        """初扫文本：verify_claim 用参数原文；recheck_dossier 用档案原文。

        返回 None 表示不发 scan（§5.3：仅 verify 类发；档案不存在时 handler 会
        如实报"不存在"，没有可扫的文本）。
        """
        action = str(step["action"])
        if action == "verify_claim":
            return str(step["args"].get("text") or "")
        if action == "recheck_dossier":
            try:
                row = dossiers.store.get_dossier(int(step["args"].get("id", 0)))
            except Exception:  # noqa: BLE001
                return None
            if not row:
                return None
            return str(row.get("input_text") or "")
        return None

    def _scan_chips(self, text: str) -> List[str]:
        """本地初扫特征 chips（patterns.scan_markers 纯正则，零 API）：如"来源模糊×2"。"""
        try:
            from mylocalai.rumor.patterns import scan_markers
            scan = scan_markers(text)
            return [f"{v.get('label', k)}×{len(v.get('hits') or [])}"
                    for k, v in (scan.get("risk_hits") or {}).items()]
        except Exception:  # noqa: BLE001  初扫是垫场体验，失败就空列表（前端有兜底文案）
            return []

    def _scan_intro(self, chips: List[str], prompts: Dict[str, Any]) -> str:
        tpl = str(prompts.get("scan_intro", "") or "")
        try:
            return tpl.format(count=len(chips), markers="、".join(chips))
        except Exception:  # noqa: BLE001  文案模板坏了退原样
            return tpl
