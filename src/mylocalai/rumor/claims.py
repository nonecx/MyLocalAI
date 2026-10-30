# -*- coding: utf-8 -*-
"""事实声明提取（谣言识别的前提判断）。

谣言 = 以事实口吻陈述、但无法核实或已被证伪的信息。纯辱骂、纯观点、纯情绪
不构成谣言，所以先要回答："这段话里有没有可被核实的事实性声明？"

沿用项目既有约定：System Prompt 锁死枚举 + 代码侧白名单回退 + 失败返回默认值并带 error，
不抛异常。
"""

from __future__ import annotations

from typing import Any, Dict, List

from mylocalai.llm.llm_client import chat_json

CLAIMS_SYSTEM_PROMPT = (
    "你是一名事实核查助理。请找出用户文本中“可被核实的事实性声明”，并输出JSON。\n"
    "事实性声明＝关于具体人物/事件/时间/地点/数据/因果关系的断言，原则上能通过查证判断真假。"
    "观点、情绪宣泄、辱骂、个人感受、主观评价、对未来的预测都不是事实性声明。\n"
    "字段要求：\n"
    "1. has_factual_claim：文本是否包含至少一条事实性声明，true 或 false。\n"
    "2. claims：事实性声明列表，最多3条，没有则为空数组。每条包含：\n"
    "   - claim：声明概括，不超过40字；\n"
    "   - has_source：文本是否给出可追溯的来源（具体机构、媒体、文献、链接）。"
    "“听说/网传/朋友说/内部人士”不算可追溯来源；\n"
    "   - verifiability：只能取“可核实/无法核实”。能找到公开权威信息佐证或证伪的取“可核实”；"
    "依赖匿名消息或无法查证的取“无法核实”。\n"
    '请严格按照以下JSON格式输出，不要输出任何其他内容：\n'
    '{"has_factual_claim": true, "claims": [{"claim": "...", "has_source": false, "verifiability": "无法核实"}]}'
)

_VERIFIABILITY_ENUM = {"可核实", "无法核实"}
_MAX_CLAIMS = 3


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "是", "有", "yes")


def extract_claims(text: str) -> Dict[str, Any]:
    """
    提取事实性声明。

    返回 {"has_factual_claim": bool|None, "claims": [...], "unsourced_unverifiable": int}。
    has_factual_claim 为 None 表示"没能判断"（空输入以外的失败情形，附 error），
    由 verdict 退回到正则特征做判断。
    """
    if not str(text or "").strip():
        return {"has_factual_claim": False, "claims": [], "unsourced_unverifiable": 0,
                "error": "输入文本为空，未调用分析"}

    obj, err = chat_json(CLAIMS_SYSTEM_PROMPT, text, temperature=0.0, max_tokens=400)
    if obj is None:
        return {"has_factual_claim": None, "claims": [], "unsourced_unverifiable": 0, "error": err}

    claims: List[Dict[str, Any]] = []
    raw_claims = obj.get("claims")
    if isinstance(raw_claims, list):
        for item in raw_claims[:_MAX_CLAIMS]:
            if not isinstance(item, dict):
                continue
            claim_text = str(item.get("claim", "")).strip()[:60]
            if not claim_text:
                continue
            verifiability = str(item.get("verifiability", "")).strip()
            if verifiability not in _VERIFIABILITY_ENUM:
                verifiability = "无法核实"  # 无法判定时按更保守（风险更高）的一侧处理
            claims.append({
                "claim": claim_text,
                "has_source": _to_bool(item.get("has_source", False)),
                "verifiability": verifiability,
            })

    declared = _to_bool(obj.get("has_factual_claim", bool(claims)))
    has_claim = bool(claims) or declared
    unsourced = sum(1 for c in claims if not c["has_source"] and c["verifiability"] == "无法核实")
    return {"has_factual_claim": has_claim, "claims": claims, "unsourced_unverifiable": unsourced}
