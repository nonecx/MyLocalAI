# -*- coding: utf-8 -*-
"""LangChain 聊天模型构造：复用项目 mylocalai.llm.llm_client 的提供方表与备用链路（.env）。

与 classic 三条 LLM 路径（chat_json/chat_text 的备用链路）保持一致：
  - 提供方不再固定 LongCat：主提供方没密钥时按 LLM_FALLBACKS 顺序取第一个可用的
    （2026-10-15 检验报告发现 3：Agent 侧原先只连主提供方，备用链路对它完全无效）；
  - "关闭思维链"参数只发给 LongCat（别家不认这个 extra_body），不再无条件附带。
"""

from __future__ import annotations

from typing import Optional

from mylocalai.llm import llm_cache
from mylocalai.llm import llm_client


def build_llm(timeout: Optional[float] = None, max_retries: Optional[int] = None,
              max_tokens: int = 700) -> Optional[object]:
    """
    构造聊天模型实例（ChatOpenAI，OpenAI 兼容接口）。没有可用密钥或
    langchain-openai 未安装时返回 None，Agent 会据此降级为"直接流水线"模式。

    说明：
      - temperature=0：分类/解释任务要稳定。
      - extra_body 关闭思维链只发给声明了 supports_thinking_disable 的提供方
        （LongCat-2.5-Preview 默认会把 token 花在 reasoning_content 上，在工具调用
        场景下既慢又会让正文为空；其他提供方不认这个参数）。
      - timeout / max_retries 默认与 classic 链路同源（LLM_TIMEOUT_S / LLM_MAX_RETRIES，
        见 mylocalai/llm/llm_client.py）：此前这里写死 45s / 2，与 classic 的 30s / 1
        两套口径，且不受请求级预算约束（2026-10-22 修复 S7）。
    """
    if timeout is None:
        timeout = llm_client.get_timeout()
    if max_retries is None:
        max_retries = llm_client.get_max_retries()
    provider = llm_client.first_available_provider()
    if provider is None:
        return None
    spec = llm_client.provider_spec(provider)
    if not spec or not spec["api_key"]:
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        return None
    kwargs = dict(
        model=spec["model"],
        api_key=spec["api_key"],
        base_url=spec["base_url"],
        temperature=0,
        max_tokens=max_tokens,
        timeout=llm_cache.attempt_timeout(timeout),
        max_retries=max_retries,
    )
    if llm_client.supports_thinking_disable(provider):
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    return ChatOpenAI(**kwargs)
