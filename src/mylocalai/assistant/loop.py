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

成本真实计数（M4，§6 M4/§8）：路由/叙述/摘要与每个工具段的真实 LLM 成本取自
llm_client 进程计数器的快照 delta（真实上游调用数 + usage tokens），分别记入
done.cost、tool_runs.finish_run 与档案 analysis_runs（经 _CostDossiers 在
record_analysis 调用点注入）；计数面不可用时逐段退回 M4 前的粗口径，绝不挡回合。
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
from mylocalai.assistant import (tools_authority, tools_chapters, tools_composite,
                                 tools_evidence, tools_heat, tools_insights, tools_memory,
                                 tools_research, tools_verify)
from mylocalai.assistant.context import build_context
from mylocalai.assistant.events import EventStream
from mylocalai.assistant.fingerprint import DEFAULT_SHINGLE_SIZE
from mylocalai.assistant.registry import (MAX_TEXT_LENGTH, ToolNotAllowed, ToolRegistry,
                                          ValidationError)

__all__ = ["AssistantLoop", "action_payload"]

#: 分句假流式（§5.5）：按中英文句读切 delta，llm_client 零改动。
_SENTENCE_RE = re.compile(r"[^。！？!?…\n]*[。！？!?…\n]+|[^。！？!?…\n]+$")

#: 会话摘要落库的前缀（system_note）；下次 build_context 从最近一条取 cached_summary。
_SUMMARY_PREFIX = "[早期对话摘要]"

#: 指纹待决标记（M3-1"先问再查"，system_note 机器可读前缀）与消费标记。
#: 待决：上一回合以指纹问句收尾，等用户"秒回/重查"表态；已决：表态已消费，
#: 防止几轮之后用户随口说"重查"误触发陈年待决（见 _pending_fingerprint）。
_FP_PENDING_PREFIX = "[指纹待决]"
_FP_RESOLVED_PREFIX = "[指纹已决]"
#: 待决标记的解析式：dossier_id=<正整数> score=<小数>（_fingerprint_ask_turn 落库）。
_FP_PENDING_RE = re.compile(r"dossier_id=(\d+)(?:.*?score=([0-9.]+))?", re.S)

#: 工具 → 卡片类型（card 事件的 card_type；web cards.js 按它分发渲染器）。
_CARD_TYPE = {
    "verify_claim": "conclusion", "recheck_dossier": "conclusion", "get_dossier": "conclusion",
    "analyze_speech": "speech",
    "check_heat": "heat", "compute_heat": "heat",
    # L4 参谋：一张卡三种子视图（kind=patterns/sources/weekly 由卡片字段自带）
    "insights": "insights",
    # 取证三技能流水线（collect_evidence）：evidence 卡，前端 cards.js renderEvidence
    "collect_evidence": "evidence",
    # 信息收集 / 权威核验 / 复合：三张新卡（cards.js renderResearch/Authority/Composite）
    "collect_research": "research",
    "verify_authority": "authority",
    "collect_and_verify": "composite",
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


def _safe_format(tpl: Any, fallback: str, **kwargs: Any) -> str:
    """外置文案模板的安全 format：模板写坏/缺键退 fallback，不让文案挡业务。"""
    try:
        return str(tpl or "").format(**kwargs)
    except Exception:  # noqa: BLE001  KeyError/IndexError/ValueError 统一兜底
        return fallback


def _opinions_payload(res: Any) -> Dict[str, Any]:
    """opinions 引擎结果 → 章节服务契约 payload（tools_chapters 消费形状）。

    - distribution：按引擎分组的立场计数（{"支持": n, "反驳": m}，来自证据条目的
      既有立场判定，不新打分）；未触发时为 {}；
    - summary：各侧可信度 Top1 论据的一行要点（无论据如实空串，不编）；
    - degraded：引擎的降级记录**列表**原样透传（handler 对 bool/list 两种形状都认）；
    - triggered/skip_reason/group_mode 原样带出（未触发的"为什么"要能落库）。
    永不抛：引擎结果形状坏按"未触发+降级"处理。
    """
    if not isinstance(res, dict):
        return {"triggered": False, "skip_reason": "观点分析引擎无输出",
                "distribution": {}, "summary": "", "degraded": True}
    distribution = {str(side): len(urls or [])
                    for side, urls in (res.get("groups") or {}).items()
                    if isinstance(urls, list)}
    parts: List[str] = []
    for side, grp in (res.get("per_group") or {}).items():
        cred = (grp or {}).get("credibility") if isinstance(grp, dict) else None
        for item in (cred or [])[:1]:
            arg = str((item or {}).get("argument") or "").strip()
            if arg:
                level = str((item or {}).get("level") or "").strip()
                parts.append(f"{side}方主要说法：{arg[:60]}"
                             + (f"（可信度{level}）" if level else ""))
    return {"triggered": bool(res.get("triggered")),
            "skip_reason": str(res.get("skip_reason") or ""),
            "group_mode": str(res.get("group_mode") or ""),
            "distribution": distribution,
            "summary": "；".join(parts),
            "degraded": list(res.get("degraded") or []),
            "error": res.get("error")}


def _opinions_signals_from_rows(rows: Any) -> Dict[str, List[str]]:
    """已存观点章节行 → 深读的 opinions_signals（规格③："形状按 opinions payload 有什么取什么"）。

    三键与 deep_read prompt 的消费键对齐（appeals/attribution_targets/action_tendencies）。
    当前章节存储只保留 distribution/summary/degraded 三列（record_opinions 契约），
    诉求/归因/行动倾向没有专列——按"有什么取什么"：行里出现同名字段（list[str]）就
    透传（前向兼容未来加列），没有就空列表。三键恒在；全空的 signals 不传（调用方
    用 any(signals.values()) 判断），避免把空信号当原料喂给模型（空列表在 deep_read
    prompt 侧也会被跳过）。
    """
    signals: Dict[str, List[str]] = {key: [] for key in
                                     ("appeals", "attribution_targets", "action_tendencies")}
    latest = rows[-1] if rows else None
    for key in signals:
        value = (latest or {}).get(key) if isinstance(latest, dict) else None
        if isinstance(value, list):
            # 只收非空字符串（prompt 原料；数字/None 等脏值按"没有"处理，不 str 硬转）
            signals[key] = [v for v in value if isinstance(v, str) and v.strip()]
    return signals


class _ServiceBox:
    """服务注入盒（编排者契约）：注入优先，缺省键惰性 import 真实引擎。

    测试只注入 stub（全离线）；生产构造 AssistantLoop() 时不 import 任何重引擎，
    首次真正用到某服务才装配。handler 统一经 ctx.services[name] 取服务。
    owner 是装配本盒的 AssistantLoop：章节类真实服务（opinions/deep_read）要从
    档案库取 input_text/证据行/快照，经 owner.dossiers 惰性解析（注入优先的
    同一来源），构造期不触发任何装配。
    """

    def __init__(self, overrides: Optional[Dict[str, Any]], rules: Dict[str, Any],
                 owner: Optional["AssistantLoop"] = None) -> None:
        self._over: Dict[str, Any] = dict(overrides or {})
        self._rules = rules or {}
        self._owner = owner
        self._real: Dict[str, Any] = {}

    def __getitem__(self, name: str) -> Callable:
        if name in self._over:
            return self._over[name]
        if name not in self._real:
            self._real[name] = _real_service(name, self._rules, self._owner)
        return self._real[name]

    def __contains__(self, name: str) -> bool:
        return True  # 所有契约内服务都可取（缺省走真实引擎）


# ---------------------------------------------------------------------------
# M4 成本真实计数接线（工作指导 §6 M4 / §8）
# ---------------------------------------------------------------------------
# 口径：路由/叙述/摘要与每个工具的真实 LLM 成本 = llm_client 进程计数器（真实上游
# 调用数 / usage tokens）在**本回合本段前后**的快照 delta。快照取不到（计数面不可用）
# 时退回 M4 前的粗口径（路由/叙述/摘要各记 1 次尝试、工具用其 out.cost 粗记），
# 保证成本是增益面：计数器故障绝不挡回合。已知边界如实声明：Agent 的 LangChain
# 工具轮不经 llm_client，不计入（见 llm_client.record_usage 处注释）。

def _usage_now() -> Optional[Dict[str, int]]:
    """进程级真实用量快照；计数面不可用时返回 None（调用方退粗口径）。"""
    try:
        from mylocalai.llm.llm_client import usage_snapshot
        return usage_snapshot()
    except Exception:  # noqa: BLE001  计数是增益面：取不到就退回粗口径，绝不挡回合
        return None


def _usage_delta(base: Optional[Dict[str, int]]) -> Optional[Dict[str, int]]:
    """相对 base 的真实用量 delta；base/当前快照任一不可用 → None。"""
    if base is None:
        return None
    now = _usage_now()
    if now is None:
        return None
    return {"requests": max(0, int(now["requests"]) - int(base["requests"])),
            "tokens": max(0, int(now["tokens"]) - int(base["tokens"]))}


def _add_usage_delta(cost: Dict[str, int], base: Optional[Dict[str, int]],
                     fallback_requests: int = 0) -> None:
    """把一段真实用量 delta 记入成本账；快照不可用退粗口径（fallback_requests 次尝试）。"""
    delta = _usage_delta(base)
    if delta is not None:
        cost["requests"] += delta["requests"]
        cost["tokens"] += delta["tokens"]
    else:
        cost["requests"] += fallback_requests


#: 卡片 → 角色呈现（v6 多 Profile 分家）：普通用户版隐去的**展示字段**。
#: 只动呈现、不动判定语义：label/置信度/证据充分度/as_of/复审条件原样保留
#: （口径诚实铁律：该标的都标）；risk_score 是 verdict.assess 的兼容层数值，
#: 按 Agent工作流规格 §1.6“前端不显示风险分/置信度等数值评分”的方向，普通用户
#: 视图不展示——档案详情页与 full/analyst/monitor 角色仍可看到完整数值。
_LAYERSON_HIDDEN_CARD_FIELDS = ("risk_score",)


def _present_for_role(card: Dict[str, Any], role: str) -> Dict[str, Any]:
    """按档案角色调整对话卡片的呈现（layperson = 普通用户简化版）。

    简化只发生在**对话卡片**（card 事件 + kind="card" 落库的这一份），不改
    store 里的结论行——历史回放渲染同一份简化卡片，行为自洽。
    """
    if role != "layperson" or not isinstance(card, dict):
        return card
    out = {k: v for k, v in card.items() if k not in _LAYERSON_HIDDEN_CARD_FIELDS}
    out["presentation"] = "simple"
    return out


def _prompts_for_role(prompts: Dict[str, Any], role: str) -> Dict[str, Any]:
    """按档案角色合并提示词（v6 角色扩展，2026-11-20）。

    合并规则（改口径只改 prompts.json 的 roles 段，不改本函数）：
      - `*_addon` 结尾的键 → **追加**到基座同名键之后（空 addon 不追加）；
      - 其余键 → 覆盖基座同名键；
      - `label` / `mission` 等基座没有的键 → 直接可用（消费方 .get 取值）；
      - `full` 与未知角色（含 roles 段缺失/写坏）→ 返回基座**原对象**，
        逐位不变（守卫测试锁定）。

    设计约束：这里只合并"路由偏向 + 叙述 posture"两类提示词，**不碰判定语义**——
    同一说法在任何工作区必须同结论（项目铁律），因此 RumorAgent 的引擎
    SYSTEM_PROMPT 保持共享，角色差异不进打分链。
    """
    base = prompts if isinstance(prompts, dict) else {}
    roles = base.get("roles")
    if not isinstance(roles, dict):
        return base
    cfg = roles.get(str(role or ""))
    if not isinstance(cfg, dict) or not cfg:
        return base
    merged = dict(base)
    for key, value in cfg.items():
        if key.endswith("_addon"):
            root = key[: -len("_addon")]
            addon = str(value or "").strip()
            if addon:
                merged[root] = (str(merged.get(root) or "") + "\n" + addon).strip()
            continue
        merged[key] = value
    return merged


class _CostDossiers:
    """ctx.dossiers 的成本接线代理（M4）：把真实 delta 注入 record_analysis。

    tools_verify 的 handler 在执行期内调 ctx.dossiers.record_analysis(
    requests=llm_turns, tokens=0)（M4 前的粗口径占位）。verify/recheck 的全部上游
    调用都发生在 record_analysis 之前，故"调用点即该工具真实成本的收敛点"：本代理
    把 requests/tokens 参数替换为进程计数器的真实 delta（快照不可用时原样透传、
    退回粗口径），让档案 analysis_runs 拿到真实成本——tools_verify/service 均为
    只读文件，接线收敛在 loop 层。其余属性一律透传。
    """

    def __init__(self, inner: Any, base: Optional[Dict[str, int]]) -> None:
        self._inner = inner
        self._base = base

    def record_analysis(self, dossier_id: int, result: Dict[str, Any], **kwargs: Any):
        delta = _usage_delta(self._base)
        if delta is not None:
            kwargs["requests"] = delta["requests"]
            kwargs["tokens"] = delta["tokens"]
        return self._inner.record_analysis(dossier_id, result, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _qualified_profile(store: Any, rules: Dict[str, Any]) -> Optional[Dict[str, Dict[str, int]]]:
    """档案库信源画像 → "达标信源" dict（L4 参谋装配，纯读取、零 LLM、永不抛）。

    返回形状即引擎注入的 source_profile 参数（RumorAgent → evidence.collect_evidence）：
    {domain: {"refutes": n}}。空库返回 {}（引擎对空 dict 逐位不变）；min_refutes 取
    rules["source_profile"]["min_refutes"]，缺段/写坏按 2 兜底、不抛；任何读取故障
    返回 None（等价"不启用"）——画像是增益面，绝不挡核查。

    口径说明（v6 多 Profile 分家）：这里的画像**按全库聚合、不按工作区过滤**。
    原因：verify 服务在 _ServiceBox 里装配（无会话上下文），且测试 stub 的
    services["verify"] 签名已被锁定（加参数会打穿约 40 处调用点）。单用户前提下
    这是可接受的保守取向：抬分只影响证据权重，不向任何界面泄露别的工作区数据；
    画像的**可见面**（insights 卡 / /api/insights/*）已按工作区过滤。
    """
    try:
        # 惰性 import（先例：_usage_now / _verify 内的引擎 import），测试可 mock 模块属性
        from mylocalai.dossier.insights import qualified_source_profile, source_profiles
        cfg = (rules or {}).get("source_profile")
        try:
            min_refutes = int(cfg.get("min_refutes", 2)) if isinstance(cfg, dict) else 2
        except (TypeError, ValueError):
            min_refutes = 2
        return qualified_source_profile(source_profiles(store), min_refutes=min_refutes)
    except Exception:  # noqa: BLE001  画像装配失败 = 不启用，行为与改动前逐位一致
        return None


def _real_service(name: str, rules: Dict[str, Any],
                  owner: Optional["AssistantLoop"] = None) -> Callable:
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
            # 信源画像装配（L4 参谋）：owner 带档案库时从档案史挑"达标信源"（历史证伪
            # ≥阈值 min_refutes 的域名）注入引擎参与抬分；装配失败/owner 缺位一律
            # None——引擎对 None/空 dict 逐位不变（tests/test_source_profile_lift.py
            # 锁定）；{}（空库）也照传，语义即"画像启用但暂无达标信源"。
            source_profile: Optional[Dict[str, Dict[str, int]]] = None
            if owner is not None:
                try:
                    source_profile = _qualified_profile(owner.dossiers.store, rules)
                except Exception:  # noqa: BLE001  画像是增益面：取不到就当不启用
                    source_profile = None
            agent = RumorAgent(
                max_chars=MAX_TEXT_LENGTH,
                funcs={
                    "emotion": analyze_emotion,
                    "aggression": analyze_aggression,
                    "logic": analyze_logic,
                },
                on_component_done=on_component_done,
                source_profile=source_profile,
            )
            # 时限继承（§8）：analyze() 内部 deadline_scope(self.deadline_s) 与本线程已设的
            # 回合预算（handle 的 deadline_scope_at）取 min——轮预算自动收紧引擎预算。
            # cancel_check（§5.3 协作取消）：回合级取消在 loop 层收口之外，引擎内协作取消
            # 经 analyze(cancel_check=...) 接到 AnalysisState.cancel——轮询命中后不再开算
            # 新组件，analyze 提前返回已完成组件的降级结论（mode="cancelled"）。
            return agent.analyze(text, cancel_check=cancel_check)
        return _verify

    if name == "speech":
        def _speech(text: str, cancel_check: Optional[Callable] = None) -> Dict[str, Any]:
            from mylocalai.classic.judge import judge
            # cancel_check（§5.3 协作取消）：judge 在阶段间与边界复核前轮询，命中后
            # 不再开新的上游调用，已完成阶段如实降级收尾（不抛异常）。
            return judge(text, cancel_check=cancel_check)
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

    if name == "opinions":
        def _opinions(dossier_id: int, llm: bool = True) -> Dict[str, Any]:
            """真实观点光谱服务（§5.2 M3-3，tools_chapters 顶部规格）。

            档案证据行直接组证据本（章节化：证据本复用，不重新采集）→
            collect_opinion_analysis（检索/取文函数用引擎模块默认，调用期解析，
            测试可 mock 模块属性）→ 换算成章节服务契约 payload。
            llm 形参供离线冒烟走 llm=False 变体（handler 走缺省 True 的生产路径）。
            """
            from mylocalai.rumor.opinions import collect_opinion_analysis
            store = owner.dossiers.store
            row = store.get_dossier(int(dossier_id)) or {}
            if not row:
                return {"triggered": False, "skip_reason": f"档案 #{dossier_id} 不存在",
                        "distribution": {}, "summary": "", "degraded": False}
            text = str(row.get("input_text") or "")
            entries = [{"url": r.get("url"), "title": r.get("title"),
                        "snippet": r.get("snippet"), "stance": r.get("stance"),
                        "domain_tier": r.get("domain_tier"),
                        "source_kind": r.get("source_kind")}
                       for r in store.evidence(int(dossier_id))]
            book = {"claim": text, "entries": entries}
            res = collect_opinion_analysis(text, evidence_book=book, llm=bool(llm))
            return _opinions_payload(res)
        return _opinions

    if name == "deep_read":
        def _deep_read(dossier_id: int, use_llm: bool = True) -> Dict[str, Any]:
            """真实深读服务（§5.2 M3-5；装配顺序 = tools_chapters 顶部《章节化调用方规格》①→④）。

            use_llm 形参供离线冒烟走 use_llm=False 变体（模板回退，handler 走缺省 True）。
            """
            from mylocalai.attention import attention as attn
            from mylocalai.attention import deep_read as dr
            store = owner.dossiers.store
            row = store.get_dossier(int(dossier_id)) or {}
            if not row:
                return {"triggered": False, "applicable": False, "text": "", "source": "",
                        "degraded": False, "warnings": [],
                        "reason": f"档案 #{dossier_id} 不存在，无法做背景解读",
                        "result": {}, "guard_dropped": []}
            snaps = store.snapshots(int(dossier_id))
            latest = snaps[-1] if snaps else None
            score = float(latest.get("e_score", 0.0) or 0.0) if latest else 0.0
            try:
                band = str(attn.classify(score) or "")
            except Exception:  # noqa: BLE001  档位算不出退空串（attention 语义下最小可用）
                band = ""
            # detail：最新快照的 E 分 + x/y/z/profile（有快照如实带全；无快照 score=0.0
            # 最小可用——未做过热度检查的档案按 E=0 评估，通常如实不触发深读）。
            detail: Dict[str, Any] = {"score": score, "band": band}
            profile: Optional[str] = None
            if latest is not None:
                detail.update({"x": latest.get("x", 0), "y": latest.get("y", 0),
                               "z": latest.get("z", 0),
                               "proxy_flags": latest.get("proxy_flags") or []})
                profile = str(latest.get("profile") or "") or None
            # ① 证据本复用：档案证据行按行序编号成受控生成的编号证据；空 snippet 行
            #    不进编号（空行占号会让 e 引用与正文错位）。
            evidence = [{"text": str(r.get("snippet") or ""),
                         "stance": str(r.get("stance") or ""),
                         "source": str(r.get("domain") or ""),
                         "title": str(r.get("title") or ""),
                         "url": str(r.get("url") or "")}
                        for r in store.evidence(int(dossier_id))
                        if str(r.get("snippet") or "").strip()]
            ev_arg: Dict[str, Any] = {"evidence": evidence} if evidence else {}
            # ④ 空证据本回退：不传 evidence——引擎侧按"未提供编号证据"处理（模型如实
            #    判 applicable=false，不硬编）；引擎版本内建独立搜索路径时自然走该路径。
            # ③ opinions_signals：已有观点章节才组装（"有什么取什么"，见辅助函数注释）；
            #    三键全空不传（空信号不喂给模型当原料）。
            signals = _opinions_signals_from_rows(store.opinions(int(dossier_id)))
            sig_arg: Dict[str, Any] = {"opinions_signals": signals} if any(signals.values()) else {}
            # ② snapshot_history：e_score 升序序列（change_rate 触发判定的原料）。
            #    引擎参数已落地（M3-4），恒传——不再用 inspect 探测（探测在测试打桩
            #    场景会看到 mock 签名而漏传，M3 复核 P2-1）。
            hist_arg: Dict[str, Any] = {
                "snapshot_history": [float(s.get("e_score", 0.0) or 0.0) for s in snaps]}
            return dr.deep_read(query=str(row.get("title") or ""), detail=detail,
                                use_llm=bool(use_llm), profile=profile,
                                **ev_arg, **sig_arg, **hist_arg)
        return _deep_read

    if name == "evidence_skills":
        def _evidence_skills():
            from mylocalai.assistant.skills import EvidenceSkills
            from mylocalai.assistant.skills.pipeline import load_rules
            from mylocalai.assistant.skills.transport import default_transport
            from mylocalai.core.privacy import load_preferences
            from mylocalai.llm import llm_client

            cfg = (rules or {}).get("skills_model") or {}

            def _llm(system, user):
                return llm_client.chat_json(system, user,
                    temperature=float(cfg.get("temperature", 0)),
                    max_tokens=int(cfg.get("max_tokens", 800)),
                    namespace=str(cfg.get("cache_namespace", "assistant_skills")))

            def _search(query, k=6):
                # 外发授权门（总览 §5.4）：consent 未开 = 如实降级记 search_unavailable，
                # 不当"搜过了没找到"；engines 全败同理（网络故障不是空结果）。
                if not load_preferences().external_allowed("public-search"):
                    return {"results": [], "degraded": True,
                            "error": "consent_denied: 外部检索未授权"}
                from mylocalai.retrieval.search import search_all
                raw = search_all(str(query), k=k)
                engines = raw.get("engines") or {}
                error = str(raw.get("error") or "")
                degraded = (bool(raw.get("blocked")) or bool(error)
                            or not any((e or {}).get("ok") for e in engines.values()))
                if degraded and not error:
                    error = "检索后端均不可用"
                # search_all 给的是 SearchResult 对象，pipeline 的 search_fn 契约要 dict
                # （url/title/snippet）——不过这一层，全部结果会被当成坏形状静默丢弃。
                rows = [r.to_dict() if hasattr(r, "to_dict") else r
                        for r in (raw.get("results") or [])]
                return {"results": rows, "degraded": degraded,
                        "error": error, "engines": engines}
            transport = default_transport()

            def _fetch(url, *, allowed_domains=None, cancel_check=None, deadline=None):
                if not load_preferences().external_allowed("public-web"):
                    return {"status": "error", "category": "consent_denied",
                            "detail": "外部取页未授权", "url": url, "final_url": ""}
                return transport.fetch(url, allowed_domains=allowed_domains,
                                       cancel_check=cancel_check, deadline=deadline)
            return EvidenceSkills(rules=load_rules(), search_fn=_search,
                                  fetch_fn=_fetch, llm_fn=_llm)
        return _evidence_skills

    if name in ("research_skills", "authority_skills"):
        def _skill_factory():
            from mylocalai.assistant.skills.preferences import load_preferences
            from mylocalai.assistant.skills.transport import default_transport
            from mylocalai.core.privacy import load_preferences as load_privacy
            from mylocalai.llm import llm_client

            cfg = (rules or {}).get(
                "research_model" if name == "research_skills" else "authority_model") or {}
            prefs = load_preferences()

            def _llm(system, user):
                return llm_client.chat_json(
                    system, user, temperature=float(cfg.get("temperature", 0)),
                    max_tokens=int(cfg.get("max_tokens", 900)),
                    namespace=str(cfg.get("cache_namespace", "assistant_" + name)))

            def _search(query, k=6):
                if not load_privacy().external_allowed("public-search"):
                    return {"results": [], "degraded": True,
                            "error": "consent_denied: 外部检索未授权"}
                from mylocalai.retrieval.search import search_all
                raw = search_all(str(query), k=k)
                engines = raw.get("engines") or {}
                error = str(raw.get("error") or "")
                degraded = (bool(raw.get("blocked")) or bool(error)
                            or not any((e or {}).get("ok") for e in engines.values()))
                if degraded and not error:
                    error = "检索后端均不可用"
                rows = [r.to_dict() if hasattr(r, "to_dict") else r
                        for r in (raw.get("results") or [])]
                return {"results": rows, "degraded": degraded,
                        "error": error, "engines": engines}

            transport = default_transport()

            def _fetch(url, *, allowed_domains=None, cancel_check=None, deadline=None):
                if not load_privacy().external_allowed("public-web"):
                    return {"status": "error", "category": "consent_denied",
                            "detail": "外部取页未授权", "url": url, "final_url": ""}
                return transport.fetch(url, allowed_domains=allowed_domains,
                                       cancel_check=cancel_check, deadline=deadline)

            if name == "research_skills":
                from mylocalai.assistant.skills.research.pipeline import ResearchSkills
                return ResearchSkills(search_fn=_search, fetch_fn=_fetch, llm_fn=_llm,
                                      preferences=prefs)
            from mylocalai.assistant.skills.authority.pipeline import AuthoritySkills
            return AuthoritySkills(search_fn=_search, fetch_fn=_fetch, llm_fn=_llm,
                                   preferences=prefs)
        return _skill_factory

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


def action_payload(action: Any) -> Tuple[str, Dict[str, Any]]:
    """结构化 action 的载荷形状校验：返回 (name, args)；形状非法抛 ValidationError。

    只校验形状（name 非空字符串 + args 是对象，缺省 {}），不查注册表——web 层在
    拿到 loop 之前先用它把明显坏掉的请求挡成 400（形状检查不需要任何装配成本）。
    多余的键一律忽略（与 registry.validate"只保留声明参数"同一口径）。
    """
    if not isinstance(action, dict):
        raise ValidationError("action 必须是 {name, args} 对象")
    name = action.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("action.name 必须是非空字符串")
    args = action.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValidationError("action.args 必须是对象")
    return name.strip(), dict(args)


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
            # M3 章节类工具（read_opinions/deep_read，§5.2 M3-3/M3-5）进缺省注册表——
            # 路由枚举（router.TOOL_ACTIONS）与 allowlist 已同步补两项。
            tools_chapters.register_all(reg, None)
            # L4 参谋工具（insights）：只读零 LLM 的画像/周报视图，路由枚举与
            # allowlist 同步补 "insights"。
            tools_insights.register_all(reg, None)
            # 取证三技能流水线（collect_evidence）：只读取证工具——把原文拆成主张、
            # 逐个去已核验信源找可定位引文，不建档、不出真假分（铁律：不扩权）。
            # 路由枚举与 allowlist 同步补 "collect_evidence"。
            tools_evidence.register_all(reg, None)
            # 信息收集 / 权威核验 / 复合：只读重成本工具（不建档、不出数值分）。
            tools_research.register_all(reg, None)
            tools_authority.register_all(reg, None)
            tools_composite.register_all(reg, None)
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
            self._service_box = _ServiceBox(self._services, self.rules, owner=self)
        return self._service_box

    # ---- 对外主入口 ----

    def handle(self, session_id: int, user_text: str,
               events: Optional[EventStream] = None,
               cancel_check: Optional[Callable[[], bool]] = None,
               action: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """处理一回合对话。永不抛异常：任何未捕获异常都折成 error 事件收尾。

        轮预算（§8）：进入即以 deadline_scope_at 设定本回合总时限（环境变量
        CHAT_TURN_DEADLINE_S 优先，缺省 rules["turn_deadline_s"]=180；<=0 不限），
        下游 RumorAgent/classic/LLM 链路经 llm_cache.deadline_at() 自动继承
        （与 JUDGE_DEADLINE_S 同一机制：嵌套 scope 取更紧者）。

        action（可选，x/y/z 精确输入通道）：{"name": str, "args": dict}。合法时跳过
        路由 LLM，直接走既有"工具→卡片→叙述→落库"流水线。**非法立即抛给调用方**
        （ValidationError/ToolNotAllowed；web 层转 400）——不走"永不抛"的降级路径：
        本通道的调用方是可信前端，把非法输入折成 error 事件会让前端不知道请求没生效。
        """
        if events is None:
            events = EventStream()
        # 校验放在 try 之前：非法 action 必须抛给调用方，不得被外层兜底折成 error 事件
        action_plan = self.resolve_action(action) if action is not None else None
        result: Dict[str, Any] = {"session_id": session_id, "reply": "", "action": "",
                                  "card": None, "tool_run_id": None, "dossier_id": None,
                                  "degraded": False, "cost": {"requests": 0, "tokens": 0}}
        budget = self._turn_budget_s()
        at = time.monotonic() + budget if budget is not None else None
        try:
            with llm_cache.deadline_scope_at(at):
                self._turn(int(session_id), str(user_text or ""), events, cancel_check, result,
                           action_plan=action_plan)
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

    def _route_plan(self, user_text: str, history_hint: str = "",
                    id_fallback: Optional[int] = None,
                    prompts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """路由 → 执行计划（refusal / meta / tools 三种）。

        history_hint/id_fallback（§5.4 ②）：会话背景（活跃档案卡+最近对话摘要）与
        "本会话最近档案 id"的确定性兜底，透传给 router.route；两者缺省（空/None）
        时路由行为与旧版逐位一致。

        prompts：按档案角色合并后的提示词（v6）。None 时用 self.prompts——
        与加角色分家前的行为逐位一致（测试 stub/旧调用路径不受影响）。

        M2 路由恒产出单个工具 action；这里返回"计划列表"的形状是为 M3 指纹
        "先问再查"的第二槽位预留（先秒回旧结论、后台再重查），执行侧已按
        max_tool_calls_per_turn 封顶。
        """
        res = router_mod.route(user_text, self.registry, self.services["route_llm"],
                               self.rules, prompts if prompts is not None else self.prompts,
                               history_hint=history_hint, id_fallback=id_fallback)
        if res["action"] in router_mod.META_ACTIONS:
            return {"kind": "meta", "action": res["action"],
                    "reply": res.get("reply", ""), "error": res.get("error", "")}
        return {"kind": "tools", "steps": [{"action": res["action"], "args": res.get("args", {})}]}

    def resolve_action(self, action: Any) -> Dict[str, Any]:
        """把结构化 action 校验成执行计划（合法即"跳过路由 LLM"的精确输入通道）。

        与 router.route 的降级哲学刻意相反：调用方是本地可信前端（web 的 POST
        /api/chat 会在 SSE 开始前调用本方法），非法必须**抛给调用方**（web 转 400），
        不能折成 clarify、更不能猜一个工具——精确输入通道的价值就是"要么照办、
        要么明确报错"。

        放行条件（三道闸全过，顺序与路由防护链一致）：
          ① 已注册（ToolNotAllowed）；
          ② 在 rules["tool_allowlist"]（ToolNotAllowed）；
          ③ permission == "read"（ToolNotAllowed）——写工具（verify_claim /
             analyze_speech / check_heat / recheck_dossier / read_opinions / deep_read）
             一律拒绝：写档案必须经"自然语言 → 路由 → 拒绝词表"的正常对话路径，
             action 通道不提供绕过（测试锁定这一条）。
        参数最后过 registry.validate 规整（只保留声明过的键，越界抛 ValidationError）。
        """
        name, args = action_payload(action)
        allowlist = [str(a) for a in (self.rules.get("tool_allowlist") or [])]
        spec = self.registry.get(name)
        if spec is None:
            raise ToolNotAllowed(f"未注册的工具: {name}")
        if name not in allowlist:
            raise ToolNotAllowed(f"工具不在允许清单: {name}")
        if str(spec.get("permission")) != "read":
            raise ToolNotAllowed(f"结构化 action 只允许只读工具，拒绝写工具: {name}")
        clean = self.registry.validate(name, args)
        return {"kind": "tools", "steps": [{"action": name, "args": clean}]}

    # ---- 路由上下文（§5.4 ②：活跃档案卡 + 最近对话摘要） ----

    def _route_context(self, session_id: int,
                       history: List[Dict[str, Any]]) -> Tuple[str, Optional[int]]:
        """组装路由用的会话背景，返回 (history_hint, 本会话最近档案 id)。

        - 活跃档案卡：最近 tool_runs 里出现过的 dossier_id（去重、取最近 3 个），
          经档案库取 title + 最新结论 label/as_of，拼成紧凑文本
          （如 "#3《某消息》(疑似谣言, 2026-10-31T…)"）——"刚才那条"靠它解析；
        - 最近对话摘要：本回合之前最近 6 条 user/assistant 消息（各截 80 字；
          kind="card" 的 JSON 载荷不入卡，结论已在档案卡里）。
        两者皆空 → history_hint 为空串，路由 user 文本与旧版逐位一致。
        全程只读，任何读取失败都降级为空——背景构建失败绝不挡路由。
        """
        parts: List[str] = []
        last_dossier_id: Optional[int] = None
        recent_ids: List[int] = []
        try:
            runs = self.sessions.runs_for_session(session_id)
        except Exception:  # noqa: BLE001  会话库读失败 → 无背景可给
            runs = []
        for run in reversed(runs):   # 最近优先
            try:
                did = int(run.get("dossier_id")) if run.get("dossier_id") is not None else None
            except (TypeError, ValueError):
                continue
            if did is None:
                continue
            if last_dossier_id is None:
                last_dossier_id = did
            if did not in recent_ids and len(recent_ids) < 3:
                recent_ids.append(did)
        cards = [c for c in (self._dossier_card(did) for did in recent_ids) if c]
        if cards:
            parts.append("活跃档案：" + "；".join(cards))
        prior = [m for m in history[:-1] if m.get("role") in ("user", "assistant")
                 and m.get("kind") != "card"][-6:]
        if prior:
            lines = [f"{m.get('role')}: {str(m.get('content') or '')[:80]}" for m in prior]
            parts.append("最近对话：\n" + "\n".join(lines))
        return ("\n".join(parts), last_dossier_id)

    def _dossier_card(self, dossier_id: int) -> str:
        """活跃档案卡的单条紧凑摘要：#3《某消息》(疑似谣言, 2026-10-31T…)。"""
        try:
            row = self.dossiers.store.get_dossier(int(dossier_id))
        except Exception:  # noqa: BLE001  档案读失败 → 这条不进卡
            return ""
        if not row:
            return ""
        title = str(row.get("title") or "")
        try:
            latest = self.dossiers.store.latest_conclusion(int(dossier_id))
        except Exception:  # noqa: BLE001
            latest = None
        if latest:
            label = str(latest.get("label") or "")
            as_of = str(latest.get("as_of") or "")
            meta = f"{label}, {as_of}" if as_of else label
        else:
            meta = "尚无结论"
        return f"#{int(dossier_id)}《{title}》({meta})"

    # ---- 指纹先问再查（M3-1 首要验收项：谣言库沉淀 + 相似说法秒回） ----

    def _session_profile(self, sessions: Any, session_id: int) -> Dict[str, Any]:
        """会话归属的档案工作区：{"profile_id", "role"}（v6 多 Profile 分家）。

        role 决定卡片呈现（layperson → 简化结论卡）；profile_id 供工具层做
        跨工作区防护与建档归属。任何失败（stub 会话库/旧装配/库故障）一律按
        {"profile_id": None, "role": "full"} 降级——不裁剪、不拦截，与加角色
        分家前的行为逐位一致。
        """
        getter = getattr(sessions, "session_profile", None)
        if not callable(getter):
            return {"profile_id": None, "role": "full"}
        try:
            row = getter(int(session_id)) or {}
        except Exception:  # noqa: BLE001  工作区解析失败 = 不裁剪
            return {"profile_id": None, "role": "full"}
        pid = row.get("profile_id") if isinstance(row, dict) else None
        try:
            pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid = None
        role = str(row.get("role") or "").strip() if isinstance(row, dict) else ""
        return {"profile_id": pid, "role": role or "full"}

    def _fingerprint_hit(self, plan: Dict[str, Any], rules: Dict[str, Any],
                         profile_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """verify_claim 执行前查指纹库：命中返回最高分行（含 score），未命中/故障返回 None。

        只看首个计划步骤（M2 路由恒单步；多槽位是 M3 预留）：verify_claim 才查——
        recheck_dossier 是对已有档案的显式重查，用户点名旧档案，无"先问"可言。
        指纹是增益层：库读失败/规则写坏一律按"不命中"降级，绝不挡正常核查。
        profile_id 非 None 时只召回本工作区沉淀的指纹（别的工作区查过的说法
        不该在这里被"秒回"）。
        """
        steps = plan.get("steps") or []
        if not steps:
            return None
        first = steps[0]
        if str(first.get("action")) != "verify_claim":
            return None
        text = str((first.get("args") or {}).get("text") or "")
        if not text.strip():
            return None
        try:
            threshold = float(rules.get("fingerprint_threshold", 0.72))
            k = int((rules.get("fingerprint") or {}).get("shingle_size",
                                                         DEFAULT_SHINGLE_SIZE))
            hits = self.dossiers.store.find_fingerprints(text, threshold, k=k,
                                                         profile_id=profile_id)
        except Exception:  # noqa: BLE001  指纹闸故障 ≠ 核查故障：放行
            return None
        return hits[0] if hits else None

    def _fingerprint_ask_turn(self, session_id: int, hit: Dict[str, Any],
                              events: EventStream, result: Dict[str, Any],
                              cost: Dict[str, int]) -> None:
        """指纹命中先问再查的整回合收尾：不执行工具、不建档、零叙述调用。

        回复 = prompts.fingerprint_hit 固定话术（narrative 事件 + 同文落库）；机器可读
        的待决标记落 system_note（"[指纹待决] dossier_id=<X> score=<s>"）——下一轮
        _pending_fingerprint 靠它旁过路由。刻意不调 _maybe_summarize：待决标记必须
        保持为会话最后一条 system_note。事件形状与 refusal/meta 回合同族：
        meta{run_id=None} → status → narrative* → done。
        """
        did = int(hit.get("dossier_id") or 0)
        score = float(hit.get("score") or 0.0)
        reply = _safe_format(
            self.prompts.get("fingerprint_hit"),
            f"查过相似的说法（档案 #{did}）。秒回旧结论，还是重新查一遍？（回复'秒回'或'重查'）",
            dossier_id=did, title=self._dossier_title(did), score=score)
        events.emit("meta", session_id=session_id, run_id=None)
        events.emit("status", stage="routing")
        self._say(events, reply)
        self.sessions.add_message(session_id, "assistant", reply, kind="text")
        self.sessions.add_message(session_id, "system_note",
                                  f"{_FP_PENDING_PREFIX} dossier_id={did} score={score:.4f}",
                                  kind="text")
        result.update(action="verify_claim", reply=reply)
        result["fingerprint_hit"] = {"dossier_id": did, "score": score}
        events.emit("done", cost=cost)

    def _pending_fingerprint(self, session_id: int) -> Optional[Dict[str, Any]]:
        """下一轮解析（路由前）：会话最后一条 system_note 是指纹待决标记 → 解析出档案。

        只认**最后一条** system_note：其后落了别的 system_note（摘要/已决标记）即视为
        待决已消费或覆盖，不再旁过。解析失败（标记写坏/无 score）按无待决处理——
        标记坏了绝不挡路由。返回 {"dossier_id": int, "score": float} 或 None。
        """
        try:
            msgs = self.sessions.messages(session_id)
        except Exception:  # noqa: BLE001  会话库读失败 → 无待决
            return None
        for m in reversed(msgs):
            if m.get("role") != "system_note":
                continue
            content = str(m.get("content") or "")
            if not content.startswith(_FP_PENDING_PREFIX):
                return None   # 最后一条 system_note 不是待决（摘要/已决/别的备注）
            match = _FP_PENDING_RE.search(content)
            if not match:
                return None
            try:
                score = float(match.group(2)) if match.group(2) else 0.0
            except ValueError:
                score = 0.0
            return {"dossier_id": int(match.group(1)), "score": score}
        return None

    @staticmethod
    def _fingerprint_intent(user_text: str) -> str:
        """用户对指纹问句的表态：含"秒回"→reuse；含"重查"→recheck；否则 ""（普通路由）。

        刻意只认问句文案里明示的两个词（"回复'秒回'或'重查'"），不做同义词猜测——
        猜错的代价是查错档案/漏查，多问一句比猜一个强（§5.1 防护链同一哲学）。
        """
        t = str(user_text or "")
        if "秒回" in t:
            return "reuse"
        if "重查" in t:
            return "recheck"
        return ""

    def _dossier_text(self, dossier_id: int) -> str:
        """档案原文（"重查"构造 verify_claim 参数用）；读失败/档案没了返回空串。"""
        try:
            row = self.dossiers.store.get_dossier(int(dossier_id))
        except Exception:  # noqa: BLE001
            return ""
        return str((row or {}).get("input_text") or "")

    def _dossier_title(self, dossier_id: int) -> str:
        """档案标题（指纹问句文案用）；读失败/空标题退"档案 #X"占位，不让文案空一块。"""
        try:
            row = self.dossiers.store.get_dossier(int(dossier_id))
        except Exception:  # noqa: BLE001
            row = None
        title = str((row or {}).get("title") or "").strip()
        return title or f"档案 #{int(dossier_id)}"

    def _deposit_fingerprint(self, dossier_id: int, step: Dict[str, Any],
                             out: Dict[str, Any], rules: Dict[str, Any]) -> None:
        """核查结论落库成功后沉淀声明指纹（record_analysis 成功后的谣言库积累，§2.5）。

        只认 verify_claim / recheck_dossier 且 out 里 dossier_id+version 都在
        （version 在 = record_analysis 确实写进了结论，档案可被"秒回"）。verify 的
        原文取参数 text；recheck 参数只有 id，原文回档案库取。沉淀是纯增益：
        任何失败静默放过，绝不影响已完成的核查回合。
        """
        try:
            if out.get("dossier_id") is None or out.get("version") is None:
                return
            text = str((step.get("args") or {}).get("text") or "")
            if not text.strip():
                text = self._dossier_text(int(out["dossier_id"]))
            if not text.strip():
                return
            k = int((rules.get("fingerprint") or {}).get("shingle_size",
                                                         DEFAULT_SHINGLE_SIZE))
            self.dossiers.store.add_claim_fingerprint(
                int(out["dossier_id"]), text, label=str(out.get("label") or ""), k=k)
        except Exception:  # noqa: BLE001  沉淀失败不影响主流程
            pass

    def _turn(self, session_id: int, user_text: str, events: EventStream,
              cancel_check: Optional[Callable[[], bool]], result: Dict[str, Any],
              action_plan: Optional[Dict[str, Any]] = None) -> None:
        """单回合实现。action_plan 非 None 时跳过路由，直接执行调用方指定的工具计划。"""
        rules = self.rules
        sessions, dossiers, services = self.sessions, self.dossiers, self.services
        cost = result["cost"]

        # ② 会话所属档案工作区（v6 多 Profile 分家）：role 决定卡片呈现与
        #    提示词 posture，profile_id 供工具层做跨工作区防护与建档归属。
        #    解析失败按 full/None 降级（提示词逐位不变）。
        profile = self._session_profile(sessions, session_id)
        profile_id: Optional[int] = profile.get("profile_id")
        profile_role: str = str(profile.get("role") or "full")
        # 角色提示词合并（full/未知角色返回基座原对象，逐位不变）
        prompts = _prompts_for_role(self.prompts, profile_role)

        # ① 用户消息落库；历史快照定在"本回合产物之前"（叙述吃 digest，不吃卡片 JSON）
        sessions.add_message(session_id, "user", user_text)
        # Persistence policy controls disk storage, not the in-flight conversation.
        # When raw-input saving is disabled, SessionStore intentionally stores an
        # empty marker; reconstruct this turn in memory so routing/narration still work.
        history = sessions.messages(session_id)
        if not history or history[-1].get("role") != "user" or not history[-1].get("content"):
            history = list(history) + [{"role": "user", "kind": "text", "content": user_text}]

        # ② 拒绝词表预检：注入指令在进路由模型之前就被拦下（§5.6 对话层第一道闸）。
        #    优先级最高：待决旁过也不豁免拒绝预检。
        refusal = router_mod.pre_refuse(user_text, rules, prompts)

        # ②' 指纹待决解析（M3-1"先问再查"的下一轮，路由前）：上一回合以指纹问句收尾、
        #     本回合用户表了态——
        #     含"秒回" → 直接构造 get_dossier(旧档案) 旁过路由 LLM（旧结论秒回，零路由成本）；
        #     含"重查" → 直接构造 verify_claim(档案原文)，且本回合跳过指纹闸——同文会
        #                再次命中，不跳过就是"问了又问"死循环；档案已被删 → 放弃旁过，
        #                落回普通路由；
        #     没表态   → 普通路由（待决标记保留，用户之后随时可表态）。
        #     表态被消费时落 "[指纹已决]" system_note：待决标记不再是最末 system_note，
        #     防止几轮之后用户随口说"重查"误触发陈年待决（_pending_fingerprint 只认
        #     最后一条 system_note）。拒绝预检命中时不旁过（安全闸最优先）。
        plan: Optional[Dict[str, Any]] = None
        skip_fp = False
        pending: Optional[Dict[str, Any]] = None
        if action_plan is not None:
            # ②''' 结构化 action（x/y/z 精确输入：调用方已给定工具与参数）→ 跳过拒绝
            #      预检、指纹待决与路由 LLM 三步，直接进既有"工具→卡片→叙述→落库"
            #      流水线。可以跳过拒绝预检的理由：那道闸拦的是经"自然语言 → 路由模型"
            #      通道混进来的越权/篡改指令，而本通道由 resolve_action 结构性地只放行
            #      只读工具（写工具/未注册工具直接抛错）；user_text 只是前端附上的人类
            #      可读说明，不该再被当成用户意图解析一遍。
            plan = action_plan
        else:
            pending = self._pending_fingerprint(session_id)   # 只读探测，拒绝预检仍最优先
            if refusal is None:
                if pending is not None:
                    intent = self._fingerprint_intent(user_text)
                    did = int(pending.get("dossier_id") or 0)
                    if intent == "reuse" and did > 0:
                        plan = {"kind": "tools",
                                "steps": [{"action": "get_dossier", "args": {"id": did}}]}
                    elif intent == "recheck" and did > 0:
                        text = self._dossier_text(did)
                        if text:
                            plan = {"kind": "tools",
                                    "steps": [{"action": "verify_claim",
                                               "args": {"text": text}}]}
                            skip_fp = True   # 用户已表态重查：本回合跳过指纹检查（防死循环）
                    if plan is not None:
                        sessions.add_message(session_id, "system_note",
                                             f"{_FP_RESOLVED_PREFIX} dossier_id={did}"
                                             f" action={intent}", kind="text")

            if refusal is not None:
                # 预检是纯词表子串匹配（零 LLM 成本）；拒绝不进路由模型
                plan = {"kind": "refusal", "reply": refusal["reply"], "error": ""}
            elif plan is None:
                # M4 真实计数：路由段成本 = 计数器 delta；快照不可用退粗口径（+1 次咨询尝试）
                route_base = _usage_now()
                history_hint, last_dossier_id = self._route_context(session_id, history)
                plan = self._route_plan(user_text, history_hint=history_hint,
                                        id_fallback=last_dossier_id, prompts=prompts)
                _add_usage_delta(cost, route_base, fallback_requests=1)

            if pending is not None and plan.get("kind") != "tools":
                # 复核修复（M3 P2-2）：待决只对**紧随的下一轮**生效。本回合没有被消费
                # （没表态/表态但档案已删/拒绝预检命中）→ 立即落 [指纹已决] 关闭劫持窗口——
                # 否则此后任意含"重查/秒回"字样的消息都会被陈年待决旁过、当前请求被静默
                # 丢弃。问句与档案卡仍在对话上下文里，用户之后换种说法总能经正常路由
                # 拿到同样结果。
                sessions.add_message(session_id, "system_note",
                                     f"{_FP_RESOLVED_PREFIX} dossier_id="
                                     f"{int(pending.get('dossier_id') or 0)}"
                                     f" action=expired", kind="text")

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
                # （roles 段的 chitchat_fallback 覆盖随局部 prompts 生效）
                narrate_base = _usage_now()
                text, _err = self._narrate(history, "", prompts=prompts)
                narrated = True
                _add_usage_delta(cost, narrate_base, fallback_requests=1)
                reply = (text or "").strip() or str(prompts.get("chitchat_fallback", ""))
            self._say(events, reply)
            sessions.add_message(session_id, "assistant", reply, kind="text")
            result.update(action=action, reply=reply)
            self._maybe_summarize(session_id, cost)
            events.emit("done", cost=cost)
            return

        # ②'' 指纹先问再查（M3-1 首要验收项）：verify_claim 执行前查谣言库相似声明。
        #    命中且 ask_before_reuse（缺省开）→ 本回合不执行工具不建档：以指纹问句收尾
        #    （零工具、零叙述调用），待用户下一轮"秒回/重查"表态（见 _pending_fingerprint）。
        #    ask_before_reuse=False → 不问，直接旁过为 get_dossier 秒回旧结论（自动复用）。
        #    "重查"构造的回合（skip_fp=True）不进本闸——否则同文再次命中、问了又问。
        if not skip_fp:
            hit = self._fingerprint_hit(plan, rules, profile_id=profile_id)
            if hit is not None:
                if bool((rules.get("fingerprint") or {}).get("ask_before_reuse", True)):
                    self._fingerprint_ask_turn(session_id, hit, events, result, cost)
                    return
                result["fingerprint_hit"] = {
                    "dossier_id": int(hit.get("dossier_id") or 0),
                    "score": float(hit.get("score") or 0.0)}
                plan = {"kind": "tools", "steps": [
                    {"action": "get_dossier", "args": {"id": int(hit.get("dossier_id") or 0)}}]}

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
            tool_base = _usage_now()   # 工具段真实成本基线（M4 计数接线）
            tool_error = ""
            out: Dict[str, Any] = {}
            try:
                ctx = SimpleNamespace(sessions=sessions,
                                      dossiers=_CostDossiers(dossiers, tool_base),
                                      events=events, cancel_check=cancel_check,
                                      services=services, profile_id=profile_id,
                                      session_id=session_id, user_text=user_text)
                if spec is None:
                    tool_error = f"工具未注册: {step['action']}"
                else:
                    out = spec["handler"](ctx, **step["args"])
                    if not isinstance(out, dict):
                        out, tool_error = {}, f"工具返回了非对象: {type(out).__name__}"
            except Exception as exc:  # noqa: BLE001  工具炸了会话不炸（§6 M2-3 验收）
                out, tool_error = {}, f"{type(exc).__name__}: {exc}"
            duration = time.perf_counter() - t0
            # M4 真实计数：该工具执行期间进程计数器的 delta = 它真实花掉的上游调用/token
            tool_delta = _usage_delta(tool_base)

            # 工具后取消轮询（§5.3 取消链）：慢工具执行期间用户断开 → 协作收工。
            # 不再发卡片/叙述（断开后也送不到前端）；run 记 cancelled（已建档则带上
            # dossier_id，结论已在档案库不丢）。
            if self._cancelled(cancel_check):
                sessions.finish_run(run_id, status="cancelled", degraded=True,
                                    duration_s=round(duration, 3),
                                    requests=int((tool_delta or {}).get("requests", 0)),
                                    tokens=int((tool_delta or {}).get("tokens", 0)),
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
            # M4 真实计数：tool_run/done.cost 记工具段真实 delta；快照不可用退回
            # 工具自报的粗口径 out.cost（M4 前口径）
            if tool_delta is not None:
                run_cost = dict(tool_delta)
            else:
                coarse = out.get("cost") or {}
                run_cost = {"requests": int(coarse.get("requests", 0)),
                            "tokens": int(coarse.get("tokens", 0))}
            executed.append({"step": step, "run_id": run_id, "out": out,
                             "duration": duration, "status": status, "error": tool_error,
                             "run_cost": run_cost})

            # ⑤ 卡片永远在叙述之前；卡片同时以 kind="card" 落库
            claim_answer = out.get("claim_answer")
            if isinstance(claim_answer, dict):
                result["claim_answer"] = claim_answer
            card = out.get("card")
            if isinstance(card, dict) and card:
                # 工具可用 out.card_type 覆盖映射：collect_research 自动接力后产 composite 卡。
                card_type = str(out.get("card_type") or _CARD_TYPE.get(str(step["action"]),
                                                                      "conclusion"))
                self._emit_card(events, sessions, session_id,
                                card_type, card, run_id, role=profile_role,
                                claim_answer=claim_answer if isinstance(claim_answer, dict) else None)
                result["card"] = card
            cost["requests"] += int(run_cost.get("requests", 0))
            cost["tokens"] += int(run_cost.get("tokens", 0))
            if out.get("dossier_id") is not None:
                result["dossier_id"] = out.get("dossier_id")
            result["degraded"] = bool(result["degraded"] or out.get("degraded")
                                      or status in ("error", "timeout", "degraded"))
            result["tool_run_id"] = run_id

            # ⑤'' 指纹沉淀（M3-1 谣言库，§2.5）：核查结论落库成功后把声明指纹入档——
            # 下次相似说法命中即可"先问再查"秒回。放在每步收尾处（早于叙述/取消路径），
            # 结论既已落库，后续取消也不丢这条积累。
            if str(step["action"]) in ("verify_claim", "recheck_dossier"):
                self._deposit_fingerprint(int(out.get("dossier_id") or 0), step, out, rules)

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
            # M4 真实计数：叙述段成本 = 计数器 delta；快照不可用退粗口径（+1 次尝试）
            narrate_base = _usage_now()
            text, _err = self._narrate(history, digest, prompts=prompts)
            _add_usage_delta(cost, narrate_base, fallback_requests=1)
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
            run_cost = e["run_cost"]   # M4：工具段真实 delta（粗口径为 fallback）
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
                   card_type: str, card: Dict[str, Any], run_id: Optional[int],
                   role: str = "full",
                   claim_answer: Optional[Dict[str, Any]] = None) -> None:
        """card 事件 + kind="card" 落库。

        载荷形状（编排者契约，web 端按此实现）：{"card_type": ..., "card": {...}}；
        额外把卡片字段并置在顶层——chat.js 的历史回放（renderHistoryMessage）把整包
        payload 交给 renderCard 后按 card_type 分发、直接读顶层字段；实时路径
        onCard 会先解 d.card，两种形状都成立（与 web 层实现逐一对过）。

        role（v6 多 Profile 分家）：layperson（普通用户）走 _present_for_role 简化
        （隐去 risk_score 数值、标 presentation=simple）后再 emit/落库——实时与
        历史回放看到同一份简化卡片；其余角色原样。
        """
        card = _present_for_role(card, role)
        payload: Dict[str, Any] = {"card_type": card_type, "card": card}
        if isinstance(claim_answer, dict):
            payload["claim_answer"] = claim_answer
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

    def _narrate(self, history: List[Dict[str, Any]], tool_digest: str,
                 prompts: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], str]:
        """叙述：消息数组 = build_context(narrate_system, 最近历史, tool_digest)。

        build_context 产出多角色数组，而 narrate_llm 契约是 (system, user) 双串——
        这里把 system 之外的部分并成一段 user 文本传入（预算裁剪仍由 build_context 负责）。

        prompts：按档案角色合并后的提示词（v6）。None 时用 self.prompts，
        与加角色分家前逐位一致。
        """
        source = prompts if prompts is not None else self.prompts
        system = str(source.get("narrate_system", ""))
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
        sum_base = _usage_now()   # M4 真实计数：摘要段成本基线
        try:
            text, _err = self.services["summarize_llm"](system, convo)
        except Exception:  # noqa: BLE001
            return
        _add_usage_delta(cost, sum_base, fallback_requests=1)
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
