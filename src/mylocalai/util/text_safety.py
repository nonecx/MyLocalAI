# -*- coding: utf-8 -*-
"""文本安全：把不能按 UTF-8 编码的字符（孤立代理项）规整为可编码形式。

背景（2026-10-15 检验报告发现 12）：文本里混入孤立代理项（形如 "\\ud800"）时，
`mylocalai/llm/llm_client.py` 的缓存键 `blob.encode("utf-8")` 会抛 UnicodeEncodeError。
该行位于 chat_json「调用方据此返回默认值 + error 键，保证永不抛异常」承诺的**上方**，
所以异常不会被转成 error 键：网站 /api/analyze 回 500、main.py 以退出码 1 崩掉。

处理策略（两层）：
  sanitize_text()  —— 把代理项替换为 U+FFFD；进入模型/缓存/检索/回显前统一调用，
                      让"分析照常进行"，而不是崩掉。
  has_surrogates() —— 供外部入口（网站）先判断并返回 400（明确的用户输入错误），
                      而不是 500。

只处理代理项：它是 Python str 里唯一无法按 UTF-8 编码的字符类别，其余字符都能编码。
"""

from __future__ import annotations

_REPLACEMENT = "\ufffd"      # U+FFFD REPLACEMENT CHARACTER


def has_surrogates(text: object) -> bool:
    """文本里是否含孤立代理项（U+D800–U+DFFF）。"""
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in str(text or ""))


def sanitize_text(text: object) -> str:
    """把孤立代理项替换为 U+FFFD，返回保证可 utf-8 编码的字符串。

    不含代理项时原样返回（不复制、不改动），所以对正常文本零开销。
    """
    s = str(text or "")
    if not has_surrogates(s):
        return s
    return "".join(_REPLACEMENT if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in s)


__all__ = ["has_surrogates", "sanitize_text"]
