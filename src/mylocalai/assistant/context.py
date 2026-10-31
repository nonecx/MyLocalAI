# -*- coding: utf-8 -*-
"""上下文窗口构建器（§5.4）。

每轮组装顺序（预算 ``context_char_budget`` 默认 12000 字符，超出从最旧可丢弃项裁起）：
  ① system prompt（人设 + 边界 + 工具摘要）
  ② 活跃档案卡（本会话最近涉及档案的紧凑摘要，"刚才那条"靠它解析）
  ③ 最近 12 轮完整对话（超过 30 轮触发一次廉价摘要并缓存）
  ④ 本轮工具结果 digest（卡片全量、证据本前 5 条 + 计数）

缓存分区：assistant_router / assistant_narrate / assistant_sum（沿用 deep_read 分区先例）。
摘要触发阈值与轮数外置在 assistant_rules.json；本模块只做确定性的裁剪与拼接。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

__all__ = ["build_context", "DEFAULT_CONTEXT_BUDGET"]

DEFAULT_CONTEXT_BUDGET = 12000
DEFAULT_RECENT_TURNS = 24          # 12 轮对话 ≈ 24 条消息（user+assistant 各算一条）
DEFAULT_SUMMARY_TRIGGER = 60       # 超过 30 轮（60 条消息）触发一次摘要


def _load_rules() -> Dict[str, Any]:
    try:
        path = __file__.replace("context.py", "assistant_rules.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001  坏配置退默认
        return {}


def build_context(system_prompt: str,
                  history: List[Dict[str, Any]],
                  tool_digest: str = "",
                  dossier_cards: str = "",
                  cached_summary: str = "",
                  budget: int = DEFAULT_CONTEXT_BUDGET) -> List[Dict[str, str]]:
    """组装 chat 消息数组，返回 [{"role": ..., "content": ...}]。

    history 是 messages 表的行（role=user/assistant/system_note）；system_note
    一律并入 user 侧以方括号标注（部分上游不认三角色之外还挑 role 名）。
    裁剪从最旧开始丢弃，保证总量不超预算；本轮 tool_digest 永远保留。
    """
    rules = _load_rules()
    recent_n = int(rules.get("context_recent_messages", DEFAULT_RECENT_TURNS))
    budget = int(budget or rules.get("context_char_budget", DEFAULT_CONTEXT_BUDGET))

    convo: List[Dict[str, str]] = []
    for m in history:
        role = "assistant" if m.get("role") == "assistant" else "user"
        text = str(m.get("content") or "")
        if m.get("role") == "system_note":
            text = f"[系统备注] {text}"
        convo.append({"role": role, "content": text})

    tail = convo[-recent_n:]
    dropped = convo[:-recent_n] if len(convo) > recent_n else []
    summary = cached_summary or ""
    if dropped and not summary:
        # 廉价摘要：把被裁掉的早期消息压成一行一条（确定性拼接，不调 LLM；
        # LLM 摘要在 loop 层触发后经 cached_summary 传进来）
        summary = "（更早的对话已省略）" + "".join(
            f"\n- {d['role']}: {d['content'][:80]}" for d in dropped[:12])

    parts: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    middle = ""
    if summary:
        middle += f"[早期对话摘要]\n{summary}\n\n"
    if dossier_cards:
        middle += f"[活跃档案]\n{dossier_cards}\n\n"
    if tool_digest:
        middle += f"[本轮工具结果]\n{tool_digest}\n\n"

    # 预算：system 不裁；middle 超预算则从摘要端开始截；tail 从最旧裁起。
    fixed = len(system_prompt)
    if len(middle) > budget // 3:
        middle = middle[: max(0, budget // 3)]
    remaining = budget - fixed - len(middle)
    while tail and remaining - sum(len(t["content"]) for t in tail) < 0:
        tail.pop(0)

    if middle:
        parts.append({"role": "user", "content": middle})
        parts.append({"role": "assistant",
                      "content": "已读取以上背景。请回答我接下来的消息。"})
    parts.extend(tail)
    return parts
