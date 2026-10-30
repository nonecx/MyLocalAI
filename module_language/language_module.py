# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""
语言模块（纯提示词工程方案，调用 LongCat 大语言模型 API）。

负责分析文本的"情绪底色"与"攻击性"，包含两个相互独立的函数：

  analyze_emotion(text) —— 情感分析
      输出 {"emotion_tendency", "emotion_score", "keywords"}
      - emotion_tendency 枚举: 冷静客观/积极宏观/情绪化宣泄/极度负面/恶意辱骂
      - emotion_score 为"情绪化强度"0-10（0=完全冷静，10=极度激烈；与情感正负无关，
        正面亢奋同样给高分，供核心裁判规则引擎统一使用）
      - keywords 为情绪关键词列表（最多 5 个）

  analyze_aggression(text) —— 攻击性分析
      输出 {"attack_target", "aggression_level", "attack_type"}
      - attack_target 枚举: 针对个人/针对群体/针对事件/无
      - aggression_level 枚举: 高/中/低
      - attack_type 枚举: 人身攻击/群体仇恨/煽动对立/无

工程约定：
  - System Prompt 中锁死枚举取值，避免模型自由发挥导致输出格式混乱。
  - JSON 解析失败或 API 失败时返回默认值 + error 键，绝不抛异常，
    保证核心裁判调度器不因单模块失效而崩溃。
"""

from __future__ import annotations

from typing import Any, Dict

from shared_core.llm_client import chat_json

# ---- 情感分析 System Prompt（枚举锁死）----
EMOTION_SYSTEM_PROMPT_BUILTIN = (
    "你是一名网络言论情感分析专家。请分析用户提供的文本并输出JSON。\n"
    "字段要求：\n"
    "1. emotion_tendency：情感倾向，只能取以下之一："
    "冷静客观/积极宏观/情绪化宣泄/极度负面/恶意辱骂\n"
    "2. emotion_score：情绪化强度，0到10的数字（0=完全冷静客观，10=极度激烈煽动）。"
    "注意：强度与情感正负无关，正面亢奋、盲目狂热同样要给高分。评级锚点："
    "日常陈述、普通好评/推荐、一般吐槽→0-3分；"
    "明显激动、激烈争论或带煽动性表述→4-6分；"
    "狂热煽动、辱骂、极端渲染→7-10分。\n"
    "3. keywords：体现情绪的关键词列表，1到5个，字符串数组。\n"
    '请严格按照以下JSON格式输出，不要输出任何其他内容：\n'
    '{"emotion_tendency": "...", "emotion_score": 0, "keywords": ["..."]}'
)

# ---- 攻击性分析 System Prompt（枚举锁死）----
AGGRESSION_SYSTEM_PROMPT_BUILTIN = (
    "你是一名网络言论攻击性分析专家。请分析用户提供的文本并输出JSON。\n"
    "字段要求（取值必须严格遵守）：\n"
    "1. attack_target：攻击对象，只能取：针对个人/针对群体/针对事件/无\n"
    "2. aggression_level：攻击性等级，只能取：高/中/低\n"
    "3. attack_type：攻击类型，只能取：人身攻击/群体仇恨/煽动对立/无\n"
    '请严格按照以下JSON格式输出，不要输出任何其他内容：\n'
    '{"attack_target": "...", "aggression_level": "...", "attack_type": "..."}'
)

# ---- 解析/API 失败时的默认返回（默认值保证规则引擎不出格） ----
# 注意：这里用 "未知" 作哨兵值，而它**刻意不在**白名单 _EMOTION_TENDENCY_ENUM 里——
# 白名单是"模型可以说的取值"，哨兵是"代码自己给的兜底"。两者混在一起会让
# "回退是否生效"变得没法检查（检验报告发现 5 记录过这个口径分歧），所以显式命名。
_EMOTION_TENDENCY_DEFAULT = "未知"
_EMOTION_DEFAULT: Dict[str, Any] = {
    "emotion_tendency": _EMOTION_TENDENCY_DEFAULT,
    "emotion_score": 0.0,
    "keywords": [],
}
_AGGRESSION_DEFAULT: Dict[str, Any] = {
    "attack_target": "未知",
    "aggression_level": "低",
    "attack_type": "未知",
}

# ---- 枚举白名单（内置默认；模型输出不在白名单内时回退，保证契约稳定） ----
# 白名单只装"模型被允许输出的取值"；"未知"是代码侧的哨兵（见上），故意不含在内。
_EMOTION_TENDENCY_ENUM_BUILTIN = {"冷静客观", "积极宏观", "情绪化宣泄", "极度负面", "恶意辱骂"}
_AGGRESSION_LEVEL_BUILTIN = {"高", "中", "低"}
_ATTACK_TARGET_BUILTIN = {"针对个人", "针对群体", "针对事件", "无"}
_ATTACK_TYPE_BUILTIN = {"人身攻击", "群体仇恨", "煽动对立", "无"}

# ---- 生效值：配置里有就用配置的，否则用上面的内置默认（rumor_agent/prompts.json） ----
from shared_core import prompt_config  # noqa: E402

EMOTION_SYSTEM_PROMPT = prompt_config.get_prompt("classic_emotion", EMOTION_SYSTEM_PROMPT_BUILTIN)
AGGRESSION_SYSTEM_PROMPT = prompt_config.get_prompt("classic_aggression",
                                                    AGGRESSION_SYSTEM_PROMPT_BUILTIN)
_EMOTION_TENDENCY_ENUM = prompt_config.get_whitelist("classic_emotion", "emotion_tendency",
                                                     _EMOTION_TENDENCY_ENUM_BUILTIN)
_AGGRESSION_LEVEL_ENUM = prompt_config.get_whitelist("classic_aggression", "aggression_level",
                                                     _AGGRESSION_LEVEL_BUILTIN)
_ATTACK_TARGET_ENUM = prompt_config.get_whitelist("classic_aggression", "attack_target",
                                                  _ATTACK_TARGET_BUILTIN)
_ATTACK_TYPE_ENUM = prompt_config.get_whitelist("classic_aggression", "attack_type",
                                                _ATTACK_TYPE_BUILTIN)


def _safe_score(value: Any) -> float:
    """把模型给的分数安全转成 0-10 的 float，失败返回 0.0。"""
    try:
        return max(0.0, min(10.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _safe_keywords(value: Any) -> list:
    """把 keywords 字段安全转成字符串列表（最多 5 个），失败返回空列表。"""
    if isinstance(value, list):
        return [str(k).strip() for k in value if str(k).strip()][:5]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def analyze_emotion(text: str, use_cache: bool = True) -> Dict[str, Any]:
    """
    情感分析。

    输入: text —— 待分析文本
    输出: {"emotion_tendency", "emotion_score", "keywords"}
    解析失败或 API 失败 -> 返回默认值 + error 键，不崩溃。

    use_cache=False：不读不写进程内缓存（仍走 single-flight），供 core_judge 在
    分数贴着档位线时取一个独立样本做边界复核。
    """
    if not str(text or "").strip():
        return {**_EMOTION_DEFAULT, "error": "输入文本为空，未调用分析"}

    obj, err = chat_json(EMOTION_SYSTEM_PROMPT, text, temperature=0.0, max_tokens=256,
                         use_cache=use_cache)
    if obj is None:
        return {**_EMOTION_DEFAULT, "error": err}

    tendency = str(obj.get("emotion_tendency", "")).strip()
    if tendency not in _EMOTION_TENDENCY_ENUM:
        tendency = _EMOTION_TENDENCY_DEFAULT
    return {
        "emotion_tendency": tendency,
        "emotion_score": _safe_score(obj.get("emotion_score")),
        "keywords": _safe_keywords(obj.get("keywords")),
    }


def analyze_aggression(text: str) -> Dict[str, Any]:
    """
    攻击性分析。

    输入: text —— 待分析文本
    输出: {"attack_target", "aggression_level", "attack_type"}
    解析失败或 API 失败 -> 返回默认值 + error 键，不崩溃。
    """
    if not str(text or "").strip():
        return {**_AGGRESSION_DEFAULT, "error": "输入文本为空，未调用分析"}

    obj, err = chat_json(AGGRESSION_SYSTEM_PROMPT, text, temperature=0.0, max_tokens=256)
    if obj is None:
        return {**_AGGRESSION_DEFAULT, "error": err}

    level = str(obj.get("aggression_level", "")).strip()
    if level not in _AGGRESSION_LEVEL_ENUM:
        level = "低"  # 攻击性判定从低，避免误伤
    target = str(obj.get("attack_target", "")).strip()
    if target not in _ATTACK_TARGET_ENUM:
        target = "未知"
    attack_type = str(obj.get("attack_type", "")).strip()
    if attack_type not in _ATTACK_TYPE_ENUM:
        attack_type = "未知"
    return {
        "attack_target": target,
        "aggression_level": level,
        "attack_type": attack_type,
    }


# ===========================================================================
# 自测入口
# ===========================================================================
def main() -> None:
    """命令行自测：python language_module.py "某段文本" """
    from shared_core.console_io import force_utf8_stdio
    force_utf8_stdio()
    import argparse
    import pprint

    parser = argparse.ArgumentParser(description="语言模块自测（情感 + 攻击性，LongCat API）")
    parser.add_argument("text", nargs="*", default=["你真是个蠢货，滚开！"])
    args = parser.parse_args()
    text = " ".join(args.text)

    print(f"输入文本: {text}\n")
    print("===== 情感分析 =====")
    pprint.pprint(analyze_emotion(text), width=60)
    print("===== 攻击性分析 =====")
    pprint.pprint(analyze_aggression(text), width=60)


if __name__ == "__main__":
    main()
