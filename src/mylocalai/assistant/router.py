# -*- coding: utf-8 -*-
"""意图路由（工作指导 §5.1）：chat_json + 封闭枚举，不赌原生 function calling。

路由器只做一件事：把用户消息映射成"封闭枚举里的一个 action + 合法参数"。
防护链顺序（§5.1，任何一步不过 → 降级 clarify 追问一次，**绝不猜**）：
  ① 枚举白名单：action ∈ 角色工具白名单（缺省=assistant_rules.tool_allowlist）
     + chitchat/clarify/refuse；
  ② 参数 schema 手工校验（registry.validate：长度上限/正整数/非负数/布尔）；
  ③ 工具 allowlist + 权限检查（registry.check_allowed）。
路由 LLM 失败（返回 None / 坏 JSON / 抛异常）与"枚举外 action"同样降级 clarify——
误判的代价被刻意设计成"多问一句"，而不是"猜一个工具乱查"。

角色裁剪（role_tool_allowlists）：route(role=...) 给定档案角色时，枚举与路由模型
看到的工具目录都按该角色的白名单裁剪（专用工具 verify_claim/recheck_dossier 仅
layperson/full）；被裁剪的 action 走既有"枚举外 → clarify"降级路径。role 缺省
（None）或不配白名单段时行为与加角色前逐位一致（fail-open，不裁剪）。

拒绝预检（pre_refuse）在路由之前独立执行：用户消息命中 refusal_triggers 词表
（子串匹配）就直接拒绝，不进路由模型——"把风险分改成0"这类注入指令永远到不了
工具层（§5.6 对话层注入防护的第一道闸）。

 混合表达闸（mixed_choice，总览 §6.3）同样在路由 LLM 之前：一句话同时要求“核查
 真假”与“解释热度”时不拼接工具，固定选择话术让用户定先后——确定性子串匹配、
 零 LLM 成本，与 pre_refuse 同族。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from mylocalai.assistant.registry import ToolNotAllowed, ToolRegistry, ValidationError

__all__ = ["META_ACTIONS", "TOOL_ACTIONS", "pre_refuse", "mixed_choice", "route"]

#: 非工具类 action：不调引擎、不建档，只产出一段直接回复。
META_ACTIONS = ("chitchat", "clarify", "refuse")

#: M2 工具类 action（封闭枚举的路由面；allowlist 仍以 assistant_rules 为准）。
#: M3 追加章节类两项（与 tools_chapters.register_all 的注册名一致，§5.2 M3-3/M3-5）。
#: L4 追加参谋类 insights（tools_insights：三类只读画像视图，kind 为封闭枚举）。
#: 第二波追加 collect_evidence（取证三技能流水线，只读：不建档、不出真假分）。
#: 第三波追加 collect_research / verify_authority（资料收集 / 权威核验，均只读：
#: 不建档、无分数、不出真假结论；与既有能力的分流见 prompts.json router_system）。
#: 第四波追加 check_source / guide_fact_check（信源可信度速查 / 辟谣方法指南，均只读）：
#: 前者评信源本身（与 verify_claim 核说法内容区分），后者教查证方法（与 chitchat 闲聊区分）。
TOOL_ACTIONS = ("verify_claim", "analyze_speech", "check_heat", "compute_heat",
                "list_dossiers", "get_dossier", "recheck_dossier", "export_card",
                "search_web", "read_opinions", "deep_read", "insights", "collect_evidence",
                "collect_research", "verify_authority", "check_source", "guide_fact_check")

#: 带 id 参数的工具：缺 id 时允许"本会话最近档案"的确定性兜底（§5.4 ②）。
#: 章节类两项同规：用户说"看看刚才那条的观点"没带档案号，用会话最近档案补。
_ID_ARG_ACTIONS = ("get_dossier", "recheck_dossier", "export_card",
                   "read_opinions", "deep_read")

#: 拒绝原因分类：命中"改/删/越权"类词 → tamper；"批量"类 → bulk；其余 → fabricate。
_TAMPER_WORDS = ("改", "删", "越权", "提权", "分数", "档案")
_BULK_WORDS = ("批量",)


def _categorize(trigger: str) -> str:
    """给命中的拒绝触发词归类（选段 refusal_reasons 用）。

    分类依据是**触发词本身**而不是用户整句：词表是人配的（"改一下分数"配 tamper），
    分类必须与词表同源，避免"帮我改一下措辞"这类误伤——它不含任何触发词，根本不命中。
    """
    if any(w in trigger for w in _TAMPER_WORDS):
        return "tamper"
    if any(w in trigger for w in _BULK_WORDS):
        return "bulk"
    return "fabricate"


def pre_refuse(user_text: Any, rules: Optional[Dict[str, Any]],
               prompts: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """拒绝词表预检：refusal_triggers 子串命中 → 固定话术拒绝。

    返回 {"action": "refuse", "reply": ...} 或 None（未命中）。永不抛异常：
    词表/话术配置写坏时退化为"不预检"，交给路由层的枚举防护兜底。
    """
    try:
        text = str(user_text or "")
        if not text:
            return None
        triggers = (rules or {}).get("refusal_triggers") or []
        reasons = (prompts or {}).get("refusal_reasons") or {}
        prefix = str((prompts or {}).get("refusal_prefix", ""))
        for trig in triggers:
            trig = str(trig or "")
            if trig and trig in text:
                key = _categorize(trig)
                reply = prefix + str(reasons.get(key, reasons.get("fabricate", "")))
                return {"action": "refuse", "reply": reply, "trigger": trig}
    except Exception:  # noqa: BLE001  预检失败不能挡住正常对话
        return None
    return None


#: 混合表达（总览 §6.3）的两组确定性模式：真假疑问短语 × 热度疑问短语，同句双命中才触发。
_TRUTH_RE = re.compile(r"真的[吗呢]|是真的|真的假|真假|核实|核查|辟谣|是不是真")
_HEAT_RE = re.compile(r"多热|热度|受关注|这么火|为什么火|关注度|有多少人关注|热度如何")

#: prompts.json 缺 mixed_choose_reply 时的内置兜底（与 JSON 同文，改文案只改一处生效优先）。
_MIXED_CHOICE_FALLBACK = ("我可以先帮你核查这条说法，也可以再补充它为什么受到关注。"
                          "你希望我先核查真实性，还是两部分一起看？")


def mixed_choice(user_text: Any, prompts: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """一句话同时要求“核查真假”与“解释热度” → 不拼接工具，回选择话术让用户定先后。

    与 pre_refuse 同族：确定性子串匹配、零 LLM 成本、永不抛异常。
    只认“热度疑问短语”，不认“热搜”这类话题名词——“帮我核实这个热搜是不是真的”
    只有一个意图（核查），不能误触发。
    """
    try:
        text = str(user_text or "")
        if not text:
            return None
        if not (_TRUTH_RE.search(text) and _HEAT_RE.search(text)):
            return None
        reply = str((prompts or {}).get("mixed_choose_reply") or "").strip()
        return {"action": "clarify", "args": {}, "reply": reply or _MIXED_CHOICE_FALLBACK,
                "error": ""}
    except Exception:  # noqa: BLE001  预检失败不能挡住正常对话（交给路由层兜底）
        return None


def _router_system(registry: Optional[ToolRegistry], prompts: Dict[str, Any],
                   allowlist: Optional[List[str]] = None) -> str:
    """人设路由 prompt + 工具目录摘要（registry.catalog()，含成本/延迟/权限档）。

    allowlist（角色权限层）：给定时目录只列该角色可用的工具（模型看不到被裁剪的
    工具，从源头减少"选了枚举外工具"的浪费）；None → 全量目录，行为与加角色前
    逐位一致。
    """
    base = str(prompts.get("router_system", ""))
    try:
        catalog = registry.catalog(allowlist=allowlist) if registry is not None else []
    except Exception:  # noqa: BLE001  目录摘要失败只降级信息量，不挡路由
        catalog = []
    return (base + "\n\n可用工具目录（JSON 数组，name/description/cost/est_s/permission）：\n"
            + json.dumps(catalog, ensure_ascii=False))


def _role_allowlist(rules: Dict[str, Any], role: Optional[str]) -> List[str]:
    """角色工具白名单解析（fail-open）。

    - role 缺省 / role="full" → 全局 tool_allowlist（full 恒全量，不读配置）；
    - role_tool_allowlists 段缺失 / 角色缺项 / 值非列表 / 空列表 → 回落全局
      tool_allowlist（不裁剪，与加角色前行为逐位一致）；
    - 否则返回该角色的白名单（专用工具 verify_claim/recheck_dossier 仅
      layperson/full，见 assistant_rules.json 的段注释）。
    """
    global_list: List[str] = [str(a) for a in (rules.get("tool_allowlist") or [])]
    if not role or str(role) == "full":
        return global_list
    section = rules.get("role_tool_allowlists")
    if not isinstance(section, dict):
        return global_list
    raw = section.get(str(role))
    if not isinstance(raw, list) or not raw:
        return global_list
    return [str(a) for a in raw]


def _fixed_reply(action: str, prompts: Dict[str, Any]) -> str:
    """路由 reply 字段为空时的话术兜底：clarify/refuse 有固定文案，chitchat 交给叙述层。"""
    if action == "clarify":
        return str(prompts.get("clarify_reply", ""))
    if action == "refuse":
        return (str(prompts.get("refusal_prefix", ""))
                + str(prompts.get("refusal_generic", "该请求超出了本助手的工作范围。")))
    return ""


def _clarify(prompts: Dict[str, Any], error: str) -> Dict[str, Any]:
    return {"action": "clarify", "args": {}, "reply": str(prompts.get("clarify_reply", "")),
            "error": error}


def route(user_text: Any, registry: ToolRegistry,
          route_llm: Callable[[str, str], Any],
          rules: Optional[Dict[str, Any]],
          prompts: Optional[Dict[str, Any]],
          history_hint: str = "",
          id_fallback: Optional[int] = None,
          role: Optional[str] = None) -> Dict[str, Any]:
    """路由一个用户消息，返回 {"action", "args", "reply", "error"}。

    - 工具 action：args 是 registry.validate 规整后的参数（多余键已丢弃）；
    - chitchat/clarify/refuse：args 恒为空 dict，reply 取路由 JSON 的 reply 字段
      （空时 clarify/refuse 用 prompts 固定话术；chitchat 返回空串，由 loop 层走
      narrate_llm 补一句——见 §5.1）；
    - 任何防护链失败 → {"action": "clarify", ...}，绝不猜。

    history_hint（可选，§5.4 ②）：会话背景文本（活跃档案卡 + 最近对话摘要，由
    loop 层组装）。非空时拼进 route_llm 的 user 文本（"【会话背景】…\\n【用户消息】…"），
    供路由解析"刚才那条"；为空（缺省）时 user 文本与旧版逐位一致。

    id_fallback（可选，§5.4 ② 确定性兜底）：本会话最近一次 tool_run 的 dossier_id
    （正整数，由 loop 层给出）。仅当路由产出 get_dossier/recheck_dossier/export_card
    且**id 参数缺失**时填入它再走正常校验——有则继续，无（None）则照旧 clarify；
    用户显式给了 id（哪怕非法）绝不替换，除会话最近档案外绝不瞎猜其它数字。

    role（可选，角色权限层）：会话所属档案工作区的角色（v6）。给定时枚举与路由
    模型目录都按 assistant_rules.role_tool_allowlists 的该角色白名单裁剪——路由
    LLM 输出被裁剪的 action 走既有"枚举外 → clarify"降级路径；缺省（None，或
    白名单段缺失/角色缺项时按 fail-open 回落）行为与加角色前逐位一致；role="full"
    恒全量。

    route_llm 的契约是 (system, user) -> (dict|None, err)（chat_json 包装）；本函数
    对它抛异常、返回 None、返回非 dict 一视同仁地降级，保证永不向外抛异常。
    """
    rules = rules or {}
    prompts = prompts or {}
    allowlist: List[str] = _role_allowlist(rules, role)
    # ① 枚举白名单 = allowlist ∪ 元动作（rules 写漏了元动作也不至于把闲聊当工具查）
    enum = set(allowlist) | set(META_ACTIONS)

    user_text_s = str(user_text or "")
    if history_hint:
        user_text_s = f"【会话背景】{history_hint}\n【用户消息】{user_text_s}"

    # 混合表达（总览 §6.3）：一句里同时要"核查真假"和"解释热度"时不拼接工具、不猜
    # 主次——固定选择话术让用户定先后。顺序：pre_refuse（loop 层）> 本闸 > 路由 LLM，
    # 命中即返回，零 LLM 成本。
    mixed = mixed_choice(user_text, prompts)
    if mixed is not None:
        return mixed

    obj: Any = None
    err = ""
    try:
        # role=None 时目录不传 allowlist（registry.catalog() 全量，逐位同旧）；
        # 给定角色时目录按角色白名单过滤，模型看不到被裁剪的工具。
        catalog_allowlist: Optional[List[str]] = None if role is None else allowlist
        obj, err = route_llm(_router_system(registry, prompts, catalog_allowlist),
                             user_text_s)
    except Exception as exc:  # noqa: BLE001  路由模型不可用 → 降级，不让对话崩
        obj, err = None, f"{type(exc).__name__}: {exc}"

    if not isinstance(obj, dict):
        # None / 坏 JSON / 非 dict 都到不了防护链：统一 clarify（绝不猜）
        return _clarify(prompts, err or "路由模型返回为空或无法解析")

    action = obj.get("action")
    if not isinstance(action, str) or action not in enum:
        return _clarify(prompts, f"action 非法: {action!r}")

    if action in META_ACTIONS:
        raw = str(obj.get("reply") or "").strip()
        return {"action": action, "args": {}, "reply": raw or _fixed_reply(action, prompts),
                "error": ""}

    # ② 参数 schema 校验（只保留声明过的键）→ ③ allowlist + 权限检查
    raw_args = {k: v for k, v in obj.items() if k not in ("action", "reply")}
    if id_fallback is not None and action in _ID_ARG_ACTIONS and "id" not in raw_args:
        # 确定性兜底（§5.4 ②）："刚才那条再查一遍"没带 id → 填本会话最近档案 id。
        # 只信 loop 层从会话 tool_runs 里查到的那个正整数，绝不瞎猜其它数字；
        # 用户显式给了 id（哪怕非法）也绝不替换。
        fb = id_fallback if isinstance(id_fallback, int) and not isinstance(id_fallback, bool) else 0
        if fb > 0:
            raw_args = {**raw_args, "id": fb}
    try:
        clean = registry.validate(action, raw_args)
        registry.check_allowed(action, allowlist=allowlist, write_enabled=True)
    except (ValidationError, ToolNotAllowed) as exc:
        return _clarify(prompts, f"{type(exc).__name__}: {exc}")

    return {"action": action, "args": clean, "reply": "", "error": ""}
