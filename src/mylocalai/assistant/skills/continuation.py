# -*- coding: utf-8 -*-
"""追问承接：从本会话已落库的卡片里找上一张同类型结果，做增量而不是重跑。

改了什么、为什么、锁定什么：
  - 新增 `assistant.skills.continuation`：只读会话消息（kind="card" 的 JSON 载荷），
    找最近一张 research / authority / composite 卡；composite 按需取子卡。
  - 增量口径由调用方决定：本模块只负责“找 prior、判断是否追问、合并证据”，
    不发起网络/模型调用。
  - 结果保存关闭时卡片内容为空串（SessionStore 的隐私策略），此时返回 None，
    调用方必须如实说明“上一张卡不可用”，不能假装记得。
  - 任何解析失败都返回 None/空，绝不抛。

依赖方向：只 import 标准库；不 import web。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

__all__ = ["CARD_TYPES", "find_prior_card", "looks_like_followup",
           "looks_like_rerun", "merge_evidence", "prior_topic", "prior_text",
           "prior_as_of"]

CARD_TYPES = ("research", "authority", "composite")

_FOLLOWUP_RE = re.compile(
    r"补充|再查|继续|深入|细化|展开|换个角度|只看|刚才|上一条|上一条|第二条|"
    r"再核|再找|进一步|再多|追加|补一点|延伸")
_RERUN_RE = re.compile(r"重新查|重跑|重新来|换主题|换个主题|换一个主题|新话题|重新收集|重新核验|从头")


def _payload_card(payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return None
    card = payload.get("card")
    if isinstance(card, Mapping):
        return dict(card)
    # 历史/实时两种载荷都可能把卡片字段并置在顶层；card_type 是分发键。
    if payload.get("card_type"):
        return dict(payload)
    return None


def find_prior_card(sessions: Any, session_id: Any,
                    types: Sequence[str] = ("research", "authority")) -> Optional[Dict[str, Any]]:
    """找本会话最近一张指定类型的卡；composite 自动取对应子卡。

    types 可含 "composite"；当 types 含 "research"/"authority" 且遇到 composite 时，
    返回 composite 里对应的子卡（保留 card_type）。找不到/读不到返回 None。
    """
    if sessions is None or session_id is None:
        return None
    wanted = tuple(str(t) for t in (types or ()) if str(t))
    if not wanted:
        return None
    try:
        messages = sessions.messages(session_id)
    except Exception:  # noqa: BLE001  会话库读失败 = 没有可复用结果
        return None
    for message in reversed(list(messages or [])):
        if not isinstance(message, Mapping) or message.get("kind") != "card":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            continue
        card = _payload_card(payload)
        if not card:
            continue
        card_type = str(card.get("card_type") or "")
        if card_type == "composite":
            for key in ("research", "authority"):
                if key in wanted:
                    sub = card.get(key)
                    if isinstance(sub, Mapping):
                        return dict(sub)
            if "composite" in wanted:
                return card
            continue
        if card_type in wanted:
            return card
    return None


def looks_like_followup(text: Any) -> bool:
    """追问标记：补充/再查/换个角度/第二条…；明确“重跑/换主题”不算追问。"""
    value = str(text or "").strip()
    if not value or looks_like_rerun(value):
        return False
    return bool(_FOLLOWUP_RE.search(value))


def looks_like_rerun(text: Any) -> bool:
    return bool(_RERUN_RE.search(str(text or "")))


def merge_evidence(old: Any, new: Any, max_items: int = 40) -> List[Dict[str, Any]]:
    """合并旧/新证据：先按 evidence_id、再按 url+quote 去重；旧结果优先，保留原 ID。"""
    try:
        cap = max(1, int(max_items))
    except (TypeError, ValueError):
        cap = 40
    out: List[Dict[str, Any]] = []
    seen_ids = set()
    seen_keys = set()
    for group in (old, new):
        if not isinstance(group, (list, tuple)):
            continue
        for item in group:
            if not isinstance(item, Mapping):
                continue
            row = dict(item)
            eid = str(row.get("evidence_id") or "")
            key = (str(row.get("url") or ""), str(row.get("quote") or ""))
            if eid and eid in seen_ids:
                continue
            if key != ("", "") and key in seen_keys:
                continue
            if eid:
                seen_ids.add(eid)
            if key != ("", ""):
                seen_keys.add(key)
            out.append(row)
            if len(out) >= cap:
                return out
    return out


def prior_topic(prior: Optional[Mapping[str, Any]]) -> str:
    """从上一张 research/authority/composite 卡里取主题。"""
    if not isinstance(prior, Mapping):
        return ""
    if prior.get("topic"):
        return str(prior.get("topic") or "").strip()
    research = prior.get("research")
    if isinstance(research, Mapping) and research.get("topic"):
        return str(research.get("topic") or "").strip()
    return ""


def prior_text(prior: Optional[Mapping[str, Any]]) -> str:
    """从上一张 authority 卡里取主张原文（多条用换行连接）。"""
    if not isinstance(prior, Mapping):
        return ""
    claims = prior.get("claims")
    if not isinstance(claims, list):
        authority = prior.get("authority")
        claims = authority.get("claims") if isinstance(authority, Mapping) else None
    if not isinstance(claims, list):
        return ""
    rows = [str(item.get("text") or "").strip() for item in claims
            if isinstance(item, Mapping) and str(item.get("text") or "").strip()]
    return "\n".join(rows)


def prior_as_of(prior: Optional[Mapping[str, Any]]) -> str:
    if not isinstance(prior, Mapping):
        return ""
    return str(prior.get("as_of") or prior.get("base_as_of") or "")
