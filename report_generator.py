# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""
报告生成模块（纯提示词工程方案，调用 LongCat 大语言模型 API）。

本模块通过 OpenAI 兼容 API 调用大语言模型，根据四个输入
（核心裁判结论、情感字典、攻击性字典、逻辑字典）生成规范的中文分析报告。

特性：
  - 将四个参数拼接成 User Prompt 发送给大模型。
  - System Prompt 固定为报告撰写专家模板。
  - 报告与核心裁判结论不一致（含否定措辞的"一致"）时，回退到本地模板，
    不让第 4 次 LLM 调用推翻前三步的判定（2026-10-22 检验报告发现 F2）。
  - temperature 固定为 0（报告要可复现地与结论一致，不做自由采样）。
  - 环境变量 REPORT_MODE=template 可完全跳过大模型，直接出本地模板
    （一次 judge 从 4 次 LLM 调用降到 3 次）。
  - 若 API 请求失败（无密钥 / 网络错误 / 解析失败），自动回退到本地模板生成
    一份含错误提示的默认报告，保证函数永不崩溃、始终返回字符串。

依赖：openai 库（OpenAI 兼容格式）
    pip install openai

配置：项目根目录 .env 中的 LONGCAT_API_KEY（兼容 DEEPSEEK_API_KEY / OPENAI_API_KEY），
客户端统一由 shared_core.llm_client 提供。

接口：
    def generate_report(conclusion, emotion_json, attack_json, logic_json) -> str

用法：
    from report_generator import generate_report
    print(generate_report("高度疑似水军/煽动性言论", emo, atk, logic))
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict

from shared_core.llm_client import chat_text

# ---- 系统提示词（报告撰写专家）----
# 内置默认；可被 rumor_agent/prompts.json 的 report 段覆盖（2026-10-22 修复 G8(b)）
SYSTEM_PROMPT_BUILTIN = """\
你是一名专业的网络言论分析报告撰写专家。请根据用户提供的核心裁判结论和三个维度的JSON数据，严格按照以下模板生成最终报告：
📊 【分析结论】
该言论有较高可能性为【结论】。

🔍 【判定依据】
- 🎭 情感特征：【emotion_tendency】，带有【keywords】情绪（评分：【emotion_score】/10）。
- 🎯 攻击对象：【attack_target】，攻击性【aggression_level】，属于【attack_type】。
- ⛓️ 逻辑链路：因果关联性【logic_relevance】，存在【logic_fallacy】。

💬 【综合解释】
（将上述依据串联成一段通顺、客观的解释文字，说明为什么得出此结论）

⚠️ 【免责声明】
注：本结果由AI模型基于文本特征初步分析生成，最终判断权与事实核查请由用户自行把握。

填写要求：
1. attack_target 取值已自带"针对"前缀（如"针对个人"）或为"无/未知"，填入【attack_target】时直接使用原值，不要再额外添加"针对"二字。
2. 整份报告输出为纯文本，禁止使用任何 Markdown 标记（包括 **、*、#）。
"""

# ---- 生效值：配置里有就用配置的，否则用上面的内置默认（rumor_agent/prompts.json） ----
from shared_core import prompt_config  # noqa: E402

SYSTEM_PROMPT = prompt_config.get_prompt("report", SYSTEM_PROMPT_BUILTIN)


def _fmt_keywords(keywords: Any) -> str:
    """将关键词字段（可能是列表）转为可读字符串。"""
    if isinstance(keywords, (list, tuple, set)):
        items = [str(x) for x in keywords if str(x).strip()]
        return "、".join(items) if items else "未知"
    return str(keywords) if keywords not in (None, "") else "未知"


def _build_user_prompt(
    conclusion: str,
    emotion_json: dict,
    attack_json: dict,
    logic_json: dict,
) -> str:
    """
    将四个参数拼接成 User Prompt 发送给大模型。
    """
    return (
        f"核心裁判结论：{conclusion or '未知'}\n"
        f"情感分析JSON：{json.dumps(emotion_json, ensure_ascii=False)}\n"
        f"攻击性分析JSON：{json.dumps(attack_json, ensure_ascii=False)}\n"
        f"逻辑分析JSON：{json.dumps(logic_json, ensure_ascii=False)}"
    )


def _local_fallback_report(
    conclusion: str,
    emotion_json: dict,
    attack_json: dict,
    logic_json: dict,
    error: str,
) -> str:
    """
    本地回退模板：当 API 失败时生成含错误提示的默认报告，保证永不崩溃。
    复用与 System Prompt 一致的模板结构，保证输出格式稳定。
    """
    conclusion_safe = conclusion or "未知"
    emotion_tendency = str(emotion_json.get("emotion_tendency", "未知"))
    emotion_keywords = _fmt_keywords(emotion_json.get("keywords"))
    emotion_score = str(emotion_json.get("emotion_score", "未知"))
    attack_target = str(attack_json.get("attack_target", "未知"))
    aggression_level = str(attack_json.get("aggression_level", "未知"))
    attack_type = str(attack_json.get("attack_type", "未知"))
    logic_relevance = str(logic_json.get("logic_relevance", "未知"))
    logic_fallacy = str(logic_json.get("logic_fallacy", "未知"))

    return (
        f"📊 【分析结论】\n"
        f"该言论有较高可能性为【{conclusion_safe}】。\n"
        f"\n"
        f"🔍 【判定依据】\n"
        f"- 🎭 情感特征：{emotion_tendency}，带有{emotion_keywords}情绪（评分：{emotion_score}/10）。\n"
        f"- 🎯 攻击对象：{attack_target}，攻击性{aggression_level}，属于{attack_type}。\n"
        f"- ⛓️ 逻辑链路：因果关联性{logic_relevance}，存在{logic_fallacy}。\n"
        f"\n"
        f"💬 【综合解释】\n"
        f"本段言论表现出明显的{emotion_tendency}倾向，{attack_target}进行了{attack_type}。"
        f"其逻辑关联性为{logic_relevance}，存在{logic_fallacy}的谬误，符合{conclusion_safe}的特征。\n"
        f"\n"
        f"⚠️ 【免责声明】\n"
        f"注：本结果由AI模型基于文本特征初步分析生成，最终判断权与事实核查请由用户自行把握。\n"
        f"\n"
        f"（提示：本报告由本地模板回退生成，未调用大模型 API。错误信息：{error}）"
    )


def _report_matches_conclusion(content: str, conclusion: str) -> bool:
    """报告正文是否与核心裁判结论一致（2026-10-22 检验报告发现 F2）。

    一致 = 存在某个句子既包含结论字样、又不含否定措辞（"该言论不是疑似情绪化
    谣言"这类句式不算一致）。结论为空时无从校验，按不一致处理、交给本地模板
    兜底，避免模型在无结论约束时自由发挥。
    """
    if not content or not conclusion:
        return False
    negations = ("不是", "并非", "不属于", "不算", "而非", "没有")
    for sentence in re.split(r"[。！？!?\n；;]", content):
        if conclusion in sentence and not any(neg in sentence for neg in negations):
            return True
    return False


def generate_report(
    conclusion: str,
    emotion_json: dict,
    attack_json: dict,
    logic_json: dict,
) -> str:
    """
    生成最终分析报告（LongCat API 方案）。

    参数:
        conclusion:    核心裁判结论字符串。
        emotion_json:  情感分析字典（emotion_tendency/keywords/emotion_score 等）。
        attack_json:   攻击性分析字典（attack_target/aggression_level/attack_type 等）。
        logic_json:    逻辑分析字典（logic_relevance/logic_fallacy 等）。

    返回:
        大模型生成的报告字符串。API 失败、或报告与结论不一致时，
        返回含错误提示的本地回退报告。
    """
    if (os.environ.get("REPORT_MODE") or "").strip().lower() == "template":
        return _local_fallback_report(
            conclusion, emotion_json, attack_json, logic_json,
            "REPORT_MODE=template，已按配置跳过大模型调用")
    content, err = chat_text(
        SYSTEM_PROMPT,
        _build_user_prompt(conclusion, emotion_json, attack_json, logic_json),
        temperature=0.0,   # F2：报告必须可复现地与结论一致，撤掉原先的自由采样
        max_tokens=800,
    )
    if content and _report_matches_conclusion(content, conclusion):
        return content
    if content and not err:
        err = "报告与核心裁判结论不一致，已回退本地模板"
    return _local_fallback_report(conclusion, emotion_json, attack_json, logic_json, err)


# ===========================================================================
# 兼容旧接口（可选保留）：模板化 ReportGenerator 类
# ===========================================================================
class ReportGenerator:
    """模板化报告生成器（保留旧接口兼容，非 API 方案）。"""

    TEMPLATE = """\
================ 人工智能分析报告 ================
生成时间：{timestamp}

【输入文本】
{text}

【综合裁定】
- 严重程度：{severity}
- 是否含攻击性：{is_attack}
- 情感倾向：{emotion}
- 逻辑谬误：{fallacies}

【模块明细】
{module_details}

================================================
"""

    def __init__(self, template: str | None = None) -> None:
        self.template = template or self.TEMPLATE

    def render(self, text: str, judgment: dict[str, Any]) -> str:
        """将裁定结果填充到模板中。"""
        verdict = judgment.get("verdict", {})
        results = judgment.get("results", {})

        lines = []
        for name, out in results.items():
            lines.append(f"- {name}: {json.dumps(out, ensure_ascii=False)}")

        return self.template.format(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            text=text,
            severity=verdict.get("severity", "unknown"),
            is_attack=verdict.get("is_attack", False),
            emotion=verdict.get("emotion", "unknown"),
            fallacies="、".join(verdict.get("fallacies", [])) or "无",
            module_details="\n".join(lines) if lines else "（无模块结果）",
        )

    def render_json(self, text: str, judgment: dict[str, Any]) -> str:
        """输出结构化 JSON 报告。"""
        return json.dumps(
            {"text": text, "timestamp": datetime.now().isoformat(), "judgment": judgment},
            ensure_ascii=False,
            indent=2,
        )


# ===========================================================================
# 自测入口
# ===========================================================================
def main() -> None:
    """命令行自测：演示 generate_report 的完整用法。"""
    from shared_core.console_io import force_utf8_stdio
    force_utf8_stdio()
    sample_conclusion = "恶意攻击言论"
    sample_emotion = {
        "emotion_score": 8,
        "emotion_tendency": "极度负面",
        "keywords": ["废物", "垃圾"],
    }
    sample_attack = {
        "aggression_level": "高",
        "attack_target": "特定个人",
        "attack_type": "人身攻击",
    }
    sample_logic = {
        "logic_relevance": "弱",
        "logic_fallacy": "诉诸情绪",
    }

    print("===== 完整数据报告 =====")
    print(generate_report(sample_conclusion, sample_emotion, sample_attack, sample_logic))
    print()
    print("===== 缺失键回退测试 =====")
    print(generate_report("", {"keywords": []}, {}, {}))


if __name__ == "__main__":
    main()