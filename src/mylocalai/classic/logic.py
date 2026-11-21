# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""
逻辑谬误分析模块（纯提示词工程方案，调用 LongCat 大语言模型 API）。

本模块通过 OpenAI 兼容 API 调用大语言模型，对文本做逻辑分析：
  - 提取论据（argument）与结论（conclusion）
  - 判断逻辑关联性（logic_relevance：强/中/弱）
  - 识别逻辑谬误类型（logic_fallacy）

接口：
    def analyze_logic(text: str) -> dict:
        返回 {"argument", "conclusion", "logic_relevance", "logic_fallacy"}
        解析失败时返回默认值；API 失败时返回错误信息，不崩溃。

工程约定：
  - System Prompt 锁死枚举取值，避免模型自由发挥导致输出格式混乱。
  - 客户端与 JSON 解析统一走 mylocalai.llm.llm_client（密钥放 .env，不硬编码）。

用法：
    from mylocalai.classic.logic import analyze_logic
    print(analyze_logic("这段言论..."))
"""

from __future__ import annotations

from typing import Any, Dict

from mylocalai.llm.llm_client import chat_json

# ---- 逻辑分析 System Prompt（枚举锁死）----
# 三段结构（角色身份/任务目标/边界）加在开头；字段要求与 JSON 契约原样保留在后。
SYSTEM_PROMPT_BUILTIN = (
    "【角色身份】你是事实核查链的逻辑谬误分析专家，负责提取论据与结论并评估论证质量。\n"
    "【任务目标】你为谣言判定链的核心裁判规则引擎达成可计算的四字段JSON："
    "argument、conclusion、logic_relevance 与 logic_fallacy。\n"
    "【边界】你只分析论证结构与谬误类型，不判断言论本身的真假；关联性拿不准时取\"中\"，"
    "没有明确谬误时不臆造谬误名。\n"
    "请分析用户提供的文本并输出JSON。\n"
    "字段要求：\n"
    "1. argument：文本中的论据（支持结论的理由/证据），用简洁原文概括；"
    "无明确论证时填\"无明确论证\"。\n"
    "2. conclusion：文本试图得出的结论，用简洁原文概括；无明确论证时填\"无明确论证\"。\n"
    "3. logic_relevance：论据对结论的逻辑关联性，只能取：强/中/弱。评级锚点："
    "论据直接、量化或因果明确地支撑结论（如统计数据、实验证据直接支持判断）→强；"
    "论据与结论相关但支撑不充分、或需要额外假设才能成立→中；"
    "论据与结论基本无关、仅靠情绪或立场关联→弱。\n"
    "4. logic_fallacy：逻辑谬误类型，无谬误时取\"无\"；有谬误时只能取："
    "诉诸情绪/稻草人谬误/滑坡谬误/人身攻击/诉诸公众/诉诸权威/虚假两难/以偏概全\n"
    "参考示例（仅作评级对照，不要照抄到输出）："
    "\"研究表明长期熬夜会导致记忆力下降，实验数据见论文\"→logic_relevance取强；"
    "\"大家都这么说，肯定是真的\"→logic_relevance取弱。\n"
    '请严格按照以下JSON格式输出，不要输出任何其他内容：\n'
    '{"argument": "...", "conclusion": "...", "logic_relevance": "强/中/弱", "logic_fallacy": "..."}'
)

# 与 _FALLACY_ENUM 并列的哨兵值：模型没给出可识别谬误时的兜底，刻意不在白名单里。
# 分开命名是为了让"回退是否生效"可检查（检验报告发现 5 记录过这个口径分歧）。
_FALLACY_SENTINEL = "未知"

# 解析/API 失败时的默认返回（relevance 取"中"保证规则引擎不出格）
_DEFAULT_RESULT: Dict[str, str] = {
    "argument": "未知",
    "conclusion": "未知",
    "logic_relevance": "中",
    "logic_fallacy": _FALLACY_SENTINEL,
}

_RELEVANCE_ENUM_BUILTIN = {"强", "中", "弱"}
_FALLACY_ENUM_BUILTIN = {
    "无", "诉诸情绪", "稻草人谬误", "滑坡谬误", "人身攻击",
    "诉诸公众", "诉诸权威", "虚假两难", "以偏概全",
}

# ---- 生效值：配置里有就用配置的，否则用上面的内置默认（src/mylocalai/rumor/prompts.json） ----
from mylocalai.config import prompt_config

SYSTEM_PROMPT = prompt_config.get_prompt("classic_logic", SYSTEM_PROMPT_BUILTIN)
_RELEVANCE_ENUM = prompt_config.get_whitelist("classic_logic", "logic_relevance",
                                              _RELEVANCE_ENUM_BUILTIN)
_FALLACY_ENUM = prompt_config.get_whitelist("classic_logic", "logic_fallacy",
                                            _FALLACY_ENUM_BUILTIN)


def _normalize_fallacy(raw: str) -> str:
    """
    把模型输出的谬误名归一到枚举：
      ① 含"无/没有/不存在/未发现"等字样 → 映射"无"
        （安全：枚举其余八值均不含"无"字）；
      ② 再用子串匹配归一（如"诉诸情绪谬误"→"诉诸情绪"）；
      ③ 仍不中才回退"未知"。
    """
    text = str(raw or "").strip()
    if not text:
        return _FALLACY_SENTINEL
    if any(k in text for k in ("无", "没有", "不存在", "未发现", "未见")):
        return "无"
    for name in _FALLACY_ENUM:
        if name != "无" and (name in text or text in name):
            return name
    return _FALLACY_SENTINEL


def analyze_logic(text: str) -> Dict[str, Any]:
    """
    调用 LongCat API 对文本做逻辑谬误分析。

    输入: text —— 待分析文本
    输出: dict —— {"argument","conclusion","logic_relevance","logic_fallacy"}
    解析失败 -> 返回默认值；API 失败 -> 捕获异常并返回错误信息，不崩溃。
    """
    if not str(text or "").strip():
        return {**_DEFAULT_RESULT, "error": "输入文本为空，未调用分析"}

    obj, err = chat_json(SYSTEM_PROMPT, text, temperature=0.0, max_tokens=512)
    if obj is None:
        return {**_DEFAULT_RESULT, "error": err}

    relevance = str(obj.get("logic_relevance", "")).strip()
    if relevance not in _RELEVANCE_ENUM:
        relevance = "中"
    fallacy = _normalize_fallacy(str(obj.get("logic_fallacy", "")))
    return {
        "argument": str(obj.get("argument", "未知")),
        "conclusion": str(obj.get("conclusion", "未知")),
        "logic_relevance": relevance,
        "logic_fallacy": fallacy,
    }


# ===========================================================================
# 自测入口
# ===========================================================================
def main() -> None:
    """命令行自测：python logic_module.py "某段文本" """
    from mylocalai.util.console_io import force_utf8_stdio
    force_utf8_stdio()
    import argparse
    import pprint

    parser = argparse.ArgumentParser(description="逻辑谬误分析（LongCat API）")
    parser.add_argument("text", nargs="*", default=["大家都这么做，所以你也应该做。"])
    args = parser.parse_args()
    text = " ".join(args.text)

    print(f"输入文本: {text}\n")
    result = analyze_logic(text)
    pprint.pprint(result, width=60)


if __name__ == "__main__":
    main()
