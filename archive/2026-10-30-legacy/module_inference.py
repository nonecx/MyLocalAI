# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""
网络言论智能分析助手 —— 本地开源模型推理模块。

本模块整合三个分析子模块，全部强制在 CPU 上推理，适合无 NVIDIA 独显（仅 Intel 核显）的电脑：

  1. 情感分析  analyze_sentiment(text)
      模型：ModelScope  damo/nlp_structbert_sentiment-classification_chinese-base
      输出：{"emotion_tendency", "emotion_score", "keywords"}

  2. 攻击性    analyze_aggression(text)
      模型：HuggingFace ArdLi/ToxiTrace
      输出：{"aggression_level", "attack_type", "attack_target"}

  3. 逻辑谬误  analyze_logic(text)
      模型：无，纯规则引擎（关键词匹配词典）
      输出：{"logic_relevance", "logic_fallacy"}

通用约定：
  - 每个函数都有 try-except，单模块失败只返回 {"error": "模块失效"}，绝不影响其它模块。
  - 模型加载后缓存在模块级变量，仅首次加载，后续直接复用（避免重复加载拖慢 CPU）。
  - 文本统一 truncation=True, max_length=512，防止超长文本撑爆内存。
  - 顶层设置 torch.set_num_threads(4) 限制 CPU 线程数，防止系统卡死。

用法：
    from module_inference import analyze_sentiment, analyze_aggression, analyze_logic
"""

from __future__ import annotations

import os
from typing import Any, Dict

import torch

# ===========================================================================
# 0. CPU 推理优化：限制线程数，避免与操作系统其它程序抢 CPU 导致卡死
# ===========================================================================
torch.set_num_threads(4)

# 首次运行 HuggingFace 模型时，若官网 huggingface.co 无法访问，
# 可用国内镜像加速下载（提示词要求）：
#   os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
if os.environ.get("HF_ENDPOINT") is None:
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# ===========================================================================
# 全局常量：模型 ID / 文本截断上限 / 各模块输出默认值
# ===========================================================================
SENTIMENT_MODEL = "damo/nlp_structbert_sentiment-classification_chinese-base"
AGGRESSION_MODEL = "ArdLi/ToxiTrace"
MAX_LENGTH = 512  # 文本截断上限（防止 CPU 内存爆炸）
DEVICE_CPU = "cpu"          # ModelScope 用 'cpu' 字符串
DEVICE_HF = -1              # HuggingFace 用 -1 表示 CPU

# 模块级缓存：模型第一次加载后常驻内存；
# 若加载失败，也缓存"已尝试过"标志，避免每次调用都重复联网下载/重试。
_CACHE = {
    "sentiment_pipeline": None,
    "sentiment_attempted": False,
    "aggression_pipeline": None,
    "aggression_attempted": False,
}


# ===========================================================================
# 1. 情感分析模块（ModelScope）
# ===========================================================================
def _get_sentiment_pipeline():
    """
    获取（并缓存）ModelScope 情感分类 pipeline。

    - 仅首次调用时加载模型，之后复用缓存的实例。
    - 若 modelscope 未安装，返回 None 并给出安装提示。
    """
    # 已尝试过（无论成败）则直接返回缓存结果，避免重复联网/重试
    if _CACHE["sentiment_attempted"]:
        return _CACHE["sentiment_pipeline"]
    _CACHE["sentiment_attempted"] = True
    try:
        from modelscope.pipelines import pipeline
        # 强制 device='cpu'：ModelScope 用字符串 'cpu' 指定 CPU
        pipe = pipeline(
            "text-classification",
            model=SENTIMENT_MODEL,
            device=DEVICE_CPU,
        )
        _CACHE["sentiment_pipeline"] = pipe
        return pipe
    except ImportError as exc:
        print(f"[module_inference] modelscope 未安装，请运行: pip install modelscope （错误: {exc}）")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[module_inference] 加载情感模型失败: {exc}")
        return None


def analyze_sentiment(text: str) -> Dict[str, Any]:
    """
    情感分析。

    输入: text —— 待分析文本
    输出: 统一格式字典：
        {"emotion_tendency": "极度负面", "emotion_score": 8.5, "keywords": []}

    说明：
      - ModelScope 返回 label（如 正面/负面/positive/negative 等）与 score(概率 0~1)。
      - emotion_tendency 由 label 映射为中文倾向描述。
      - emotion_score = 概率 × 10（0~10 分制）。
    """
    try:
        pipe = _get_sentiment_pipeline()
        if pipe is None:
            return {"emotion_tendency": "未知", "emotion_score": 0.0, "keywords": [], "error": "模型不可用"}

        # 推理（truncation=True, max_length=512 防止长文本撑爆内存）
        # torch.inference_mode() 进一步减少内存/显存开销，加速 CPU 推理
        with torch.inference_mode():
            result = pipe(text, truncation=True, max_length=MAX_LENGTH)
        # result 形如 [{"label": "负面", "score": 0.97}] 或 {"label":..,"score":..}
        first = result[0] if isinstance(result, list) else result
        label = str(first.get("label", "未知")).strip().lower()
        prob = float(first.get("score", 0.0))

        # ---- 标签 -> 中文倾向 ----
        tendency_map = {
            "positive": "积极",
            "neg": "负面",
            "negative": "极度负面",
            "good": "积极",
            "bad": "极度负面",
            "好评": "积极",
            "差评": "极度负面",
            "正面": "积极",
            "负面": "极度负面",
            "中性": "中性",
            "neutral": "中性",
        }
        # 从 label 里找关键词（兼容包含式，如 "negative" 里含 "neg"）
        tendency = tendency_map.get(label, "未知")
        if tendency == "未知":
            for key, val in tendency_map.items():
                if key in label:
                    tendency = val
                    break

        # 概率 × 10 -> 0~10 评分（并做截断，保留 1 位小数）
        score = max(0.0, min(10.0, round(prob * 10.0, 1)))

        return {
            "emotion_tendency": tendency,
            "emotion_score": score,
            "keywords": [],
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[module_inference] 情感分析失败: {exc}")
        return {"emotion_tendency": "未知", "emotion_score": 0.0, "keywords": [], "error": "模块失效"}


# ===========================================================================
# 2. 攻击性分析模块（HuggingFace）
# ===========================================================================
def _get_aggression_pipeline():
    """
    获取（并缓存）HuggingFace 攻击性/毒性检测 pipeline。

    - 仅首次加载，之后复用。
    - 若 transformers 异常则返回 None。
    """
    # 已尝试过（无论成败）则直接返回缓存结果，避免重复联网/重试
    if _CACHE["aggression_attempted"]:
        return _CACHE["aggression_pipeline"]
    _CACHE["aggression_attempted"] = True
    try:
        from transformers import pipeline as hf_pipeline
        # device=-1 强制 CPU（HuggingFace 约定 -1 表示 CPU）
        pipe = hf_pipeline(
            "text-classification",
            model=AGGRESSION_MODEL,
            device=DEVICE_HF,
        )
        _CACHE["aggression_pipeline"] = pipe
        return pipe
    except Exception as exc:  # noqa: BLE001
        print(f"[module_inference] 加载攻击性模型失败: {exc}")
        return None


def analyze_aggression(text: str) -> Dict[str, Any]:
    """
    攻击性检测。

    输入: text —— 待分析文本
    输出: 统一格式字典：
        {"aggression_level": "高", "attack_type": "仇恨言论", "attack_target": "未知"}

    说明：
      - ArdLi/ToxiTrace 通常是二分类：LABEL_0=无毒, LABEL_1=有毒。
      - 有毒性时按置信度分档：高(>0.8) / 中(>0.6) / 低(其他)。
    """
    try:
        pipe = _get_aggression_pipeline()
        if pipe is None:
            return {"aggression_level": "低", "attack_type": "未知", "attack_target": "未知", "error": "模型不可用"}

        # torch.inference_mode() 减少内存开销，加速 CPU 推理
        with torch.inference_mode():
            result = pipe(text, truncation=True, max_length=MAX_LENGTH)
        first = result[0] if isinstance(result, list) else result
        label = str(first.get("label", "LABEL_0")).strip().lower()
        score = float(first.get("score", 0.0))

        # ---- 标签映射：判断是否有毒 ----
        is_toxic = False
        if label == "label_1" or label in ("toxic", "offensive", "hate", "hateful", "abusive", "insult", "threat"):
            is_toxic = True
        elif label == "label_0" or label in ("nontoxic", "non-toxic", "normal", "benign"):
            is_toxic = False
        else:
            # 保守：score 高才判为有毒
            is_toxic = score >= 0.5

        # ---- 攻击等级分档 ----
        if not is_toxic:
            level = "低"
        elif score > 0.8:
            level = "高"
        elif score > 0.6:
            level = "中"
        else:
            level = "低"

        # 攻击类型：有项目输出"仇恨言论"；无独立分类时给通用类型
        attack_type = "仇恨言论" if is_toxic else "无"
        # 攻击对象：当前模型不输出对象，统一"未知"
        attack_target = "未知"

        return {
            "aggression_level": level,
            "attack_type": attack_type,
            "attack_target": attack_target,
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[module_inference] 攻击性分析失败: {exc}")
        return {"aggression_level": "低", "attack_type": "未知", "attack_target": "未知", "error": "模块失效"}


# ===========================================================================
# 3. 逻辑谬误分析模块（规则引擎 / 关键词匹配）
# ===========================================================================
# 常见逻辑谬误词典（关键词 -> 谬误类型）。命中越多，关联性越弱。
LOGIC_FALLACY_DICT: Dict[str, list] = {
    "人身攻击": [
        "蠢", "废物", "垃圾", "傻", "脑子进水", "白痴", "智障",
        "恶心", "不要脸", "畜生", "滚", "滚蛋", "去死", "闭嘴",
    ],
    "诉诸情绪": [
        "凭什么", "太过分", "气死", "愤怒", "受不了", "无语",
        "失望", "凭什么说我", "你有什么资格",
    ],
    "诉诸公众": [
        "大家都", "所有人都", "人人", "每个都", "全世界都",
        "别人都", "都这么说", "都这样做", "都买了",
    ],
    "滑坡谬误": [
        "一旦", "如果…就会", "迟早", "迟早会", "最终会",
        "越…越", "只要…就", "就会导致",
    ],
    "稻草人谬误": [
        "你就是", "你总说", "你只会", "你自己都", "你就是想",
        "你根本就是",
    ],
    "诉诸权威": [
        "专家说", "科学家说", "权威", "领导说", "书上说",
    ],
}

# 逻辑关联性强的提示词（客观、讲证据、理性），命中越多关联性越强
LOGIC_RELEVANCE_STRONG = [
    "因为", "所以", "根据", "数据", "统计", "事实", "证据",
    "原因", "结论", "论证", "逻辑", "证明", "研究表明",
]


def analyze_logic(text: str) -> Dict[str, Any]:
    """
    逻辑谬误检测（纯规则引擎，无需模型）。

    输入: text —— 待分析文本
    输出: 统一格式字典：
        {"logic_relevance": "弱", "logic_fallacy": "诉诸情绪"}

    说明：
      - 统计文本命中各类谬误关键词的次数，取命中最多者作为 logic_fallacy。
      - 统计"强关联"提示词命中数；谬误多而强关联词少 => 关联性弱，反之为强。
    """
    try:
        text = str(text)

        # ---- 统计各类谬误命中数 ----
        fallacy_hits: Dict[str, int] = {}
        for fallacy, words in LOGIC_FALLACY_DICT.items():
            count = sum(1 for w in words if w in text)
            if count > 0:
                fallacy_hits[fallacy] = count

        # ---- 统计强关联提示词命中数 ----
        strong_hits = sum(1 for w in LOGIC_RELEVANCE_STRONG if w in text)

        # ---- 判定关联性强弱 ----
        if fallacy_hits:
            # 有谬误：谬误数多且强关联词少 => 弱；否则中等
            total_fallacies = sum(fallacy_hits.values())
            if total_fallacies >= 2 or strong_hits <= 1:
                relevance = "弱"
            else:
                relevance = "中"
            # 取命中最多的一种谬误
            main_fallacy = max(fallacy_hits, key=fallacy_hits.get)
        else:
            # 无谬误：强关联词越多 => 关联性越强
            if strong_hits >= 2:
                relevance = "强"
            elif strong_hits == 1:
                relevance = "中"
            else:
                relevance = "弱"
            main_fallacy = "无"

        return {
            "logic_relevance": relevance,
            "logic_fallacy": main_fallacy,
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[module_inference] 逻辑分析失败: {exc}")
        return {"logic_relevance": "弱", "logic_fallacy": "无", "error": "模块失效"}


# ===========================================================================
# 4. 便捷入口：一次性分析三个模块
# ===========================================================================
def analyze_all(text: str) -> Dict[str, Any]:
    """
    依次调用三个分析函数（纯串行，单线程）。

    返回: {"emotion":..., "attack":..., "logic":...}
    """
    emotion = analyze_sentiment(text)
    attack = analyze_aggression(text)
    logic = analyze_logic(text)
    return {"emotion": emotion, "attack": attack, "logic": logic}


# ===========================================================================
# 5. 自测入口
# ===========================================================================
def main() -> None:
    """命令行自测：python module_inference.py "某段文本" """
    import argparse
    import json

    parser = argparse.ArgumentParser(description="本地推理模块自测")
    parser.add_argument("text", nargs="*", default=["你真是个蠢货，滚开！"])
    args = parser.parse_args()
    text = " ".join(args.text)

    print(f"输入文本: {text}\n")
    print("===== 情感分析 =====")
    print(json.dumps(analyze_sentiment(text), ensure_ascii=False, indent=2))
    print("===== 攻击性分析 =====")
    print(json.dumps(analyze_aggression(text), ensure_ascii=False, indent=2))
    print("===== 逻辑分析 =====")
    print(json.dumps(analyze_logic(text), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()