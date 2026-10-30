# -*- coding: utf-8 -*-
"""确定性的谣言判定：四档言论分层 + 谣言风险分。

最终结论完全由本模块的纯函数得出，不经过大模型——Agent 里的大模型只负责调度
工具和撰写解释。这样即使用户文本里夹带提示词注入，也改变不了标签和分数。

两层判定：
  1. speech_tier   与 core_judge.judge 完全一致的四档规则（tests 里有等价性校验）
  2. rumor verdict 在 1 的基础上加入"是否含可核实事实声明"这个谣言的必要条件，
                   再叠加谣言话术特征、情绪强度、逻辑缺陷、无来源声明，得出 0-100 风险分

为什么不能直接用 speech_tier 当谣言结论：它的"疑似情绪化谣言"只要求
"情绪≥5 且逻辑关联弱"，纯情绪发泄、甚至热情的普通好评都会命中。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mylocalai.config.rules import load_rules
from mylocalai.core.tiering import speech_tier  # noqa: F401  四档言论分层的唯一实现（原处已删除）

LABEL_HIGH = "高风险谣言"
LABEL_SUSPECT = "疑似谣言"
LABEL_DOUBT = "存疑待核实"
LABEL_LOW = "低风险"
LABEL_NO_CLAIM = "无可核实陈述"

ALL_LABELS = (LABEL_HIGH, LABEL_SUSPECT, LABEL_DOUBT, LABEL_LOW, LABEL_NO_CLAIM)

# 判定口径来自 src/mylocalai/config/verdict_rules.json（可用 RUMOR_RULES_PATH 换文件）。
# 这里仍以模块常量的形式暴露，便于调用方读取当前口径；改口径请改 JSON 而不是这里。
_RULES = load_rules()
_THRESHOLDS = _RULES.get("thresholds") or {}
_EXTRA = _RULES.get("evidence_lift") or {}


def _cfg_int(section: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(section[key])
    except (KeyError, TypeError, ValueError):
        return default


NO_CLAIM_RISK_CAP = _cfg_int(_RULES, "no_claim_risk_cap", 24)   # 没有事实声明时风险分上限
THRESHOLD_HIGH = _cfg_int(_THRESHOLDS, "high", 70)
THRESHOLD_SUSPECT = _cfg_int(_THRESHOLDS, "suspect", 45)
THRESHOLD_DOUBT = _cfg_int(_THRESHOLDS, "doubt", 25)

# 证据抬分锚点：≥2 个独立可靠反驳（含官方权重）→ 高风险档下限；1 个 → 疑似档下限
EVIDENCE_RISK_MULTI = _cfg_int(_EXTRA, "multi_source_risk", 78)
EVIDENCE_RISK_SINGLE = _cfg_int(_EXTRA, "single_source_risk", 58)
EVIDENCE_MIN_WEIGHT_HIGH = _cfg_int(_EXTRA, "min_weight_for_high", 2)

# ---- 稳定性护栏 ----
# 上游大模型在 temperature=0 下仍有采样漂移（检验报告发现 4/10：同一文本重复分析，
# aggression 在「中/高」间跳、emotion_score 1 分抖动经 round(score*2) 放大成 2 分，
# 正好跨过 45 分阈值）。LongCat 还不认 seed（实测同 seed 6 次调用 5 种输出），
# 所以抖动只能在下游挡：把决定分数的连续量按粒度归并，让"1 分的模型抖动"不改变得分。
_STABILITY = _RULES.get("stability") or {}
EMOTION_SCORE_BUCKET = _cfg_int(_STABILITY, "emotion_score_bucket", 2)
EMOTION_MAX_POINTS = _cfg_int(_STABILITY, "emotion_max_points", 20)


ADVICE = {
    LABEL_HIGH: "不要转发。请通过政府官网、权威媒体或辟谣平台核实后再判断，必要时向平台举报。",
    LABEL_SUSPECT: "转发前先核实：查找原始信源，对照权威媒体或官方通报，注意发布时间与出处。",
    LABEL_DOUBT: "信息存疑，暂不建议传播。可检索权威来源交叉验证。",
    LABEL_LOW: "未发现明显的谣言特征，但这不等于内容属实，重要信息仍建议核对来源。",
    LABEL_NO_CLAIM: "文本没有可被核实的事实性陈述，不属于谣言类内容。",
}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def bucket_score(score: Any, bucket: Optional[int] = None) -> float:
    """把情绪分归并到"所在桶的下沿"（bucket=2：7.0/8.0/9.0 都归到 6.0）。

    归并的是"连续量的微小抖动"，不改变评级口径：语言模块提示词里的档位
    （0-3 / 4-6 / 7-10）仍按原始分判断，只有**计分**用归并后的值。
    bucket<=1 表示不归并（保持原样）。

    为什么向下取整而不是四舍五入到最近倍数：最近倍数归并会把 6.0 与 7.0
    （实测抖动，正好跨过 45 分阈值：41 存疑 ↔ 45 疑似）分到两个桶；
    下取整后 6/7/8/9 同桶，抖动不再跨档（2026-10-15 检验报告发现 4）。
    代价是桶边界左移到 5↔6、9↔10；进一步消除靠调用侧缓存兜底。
    """
    step = EMOTION_SCORE_BUCKET if bucket is None else int(bucket)
    value = _to_float(score)
    if step <= 1:
        return value
    value = max(0.0, min(10.0, value))
    return min(10.0, (int(value // step)) * step)


def _label_for(score: int) -> str:
    if score >= THRESHOLD_HIGH:
        return LABEL_HIGH
    if score >= THRESHOLD_SUSPECT:
        return LABEL_SUSPECT
    if score >= THRESHOLD_DOUBT:
        return LABEL_DOUBT
    return LABEL_LOW


def assess(
    markers: Optional[Dict[str, Any]],
    emotion: Optional[Dict[str, Any]],
    attack: Optional[Dict[str, Any]],
    logic: Optional[Dict[str, Any]],
    claims: Optional[Dict[str, Any]],
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    汇总各组件结果，得出谣言判定。入参允许为 None（组件未完成，如超时），按"缺失"处理。

    证据本（evidence）的取向是"只上调、不下调"：找到可靠来源辟谣 → 该内容已是
    被辟谣的谣言，风险分抬到对应档位下限；没找到辟谣 ≠ 不是谣言（新谣言、冷门
    事件都可能搜不到），不降分。补搜与立场判定失败只降置信度。
    """
    markers = markers or {}
    emotion = emotion or {}
    attack = attack or {}
    logic = logic or {}
    claims = claims or {}
    evidence = evidence or {}

    # ---- 组件健康度：缺失或带 error 的 LLM 组件记为降级 ----
    degraded: List[str] = []
    for name, comp in (("emotion", emotion), ("aggression", attack), ("logic", logic), ("claims", claims)):
        if not comp or comp.get("error"):
            degraded.append(name)
    # evidence：搜索/判定失败且一条证据都没有才算降级；主动跳过（无可核实声明）不算，
    # "具体事件按范围过滤后没有该事件的资料"也不算失败——那是有意的范围收紧。
    # 但"搜到了、却全被判为无关而剔除"要算（empty_after_filter，2026-10-22 发现 F6）。
    ev_entries = evidence.get("entries") or []
    ev_filtered_by_scope = bool(evidence.get("excluded_not_event")) and evidence.get("scope") == "event"
    ev_empty_after_filter = bool(evidence.get("empty_after_filter"))
    if evidence and (evidence.get("error") or ev_empty_after_filter) \
            and not ev_entries and not ev_filtered_by_scope:
        degraded.append("evidence")
    confidence = "高" if not degraded else ("中" if len(degraded) == 1 else "低")

    # ---- 是否含事实声明（谣言的必要条件） ----
    declared = claims.get("has_factual_claim")
    if declared is None:
        has_claim = bool(markers.get("assertion_signal"))
        claim_basis = "话术特征推断（事实声明提取不可用）"
    else:
        has_claim = bool(declared)
        claim_basis = "大模型提取"

    # ---- 各部分得分 ----
    marker_pts = int(markers.get("marker_points", 0))
    cred_pts = int(markers.get("credibility_points", 0))
    # 情绪强度按粒度归并后计分：上游 1 分抖动（7/8/9 之间跳）不再改变分数。
    # 注意"言论分层"（speech_tier）仍用原始分，与 core_judge 保持逐条等价。
    if emotion.get("error") or not emotion:
        emotion_pts = 0
        emotion_effective = 0.0
    else:
        emotion_effective = bucket_score(emotion.get("emotion_score"))
        emotion_pts = int(round(min(float(EMOTION_MAX_POINTS), emotion_effective * 2)))
    rel = str(logic.get("logic_relevance", "")).strip()
    fallacy = str(logic.get("logic_fallacy", "")).strip()
    logic_pts = 0
    if logic and not logic.get("error"):
        logic_pts = {"弱": 15, "中": 5}.get(rel, 0)
        if fallacy not in ("", "无", "未知"):
            logic_pts += 5
    claims_pts = min(15, 5 * int(claims.get("unsourced_unverifiable", 0)))

    raw = marker_pts + cred_pts + emotion_pts + logic_pts + claims_pts
    risk = max(0, min(100, raw))
    capped = False
    if not has_claim and risk > NO_CLAIM_RISK_CAP:
        risk, capped = NO_CLAIM_RISK_CAP, True

    # ---- 证据核实抬升（只上调不下调，且有事实声明才生效） ----
    stance_sum = evidence.get("stance_summary") or {}
    n_refute = int(stance_sum.get("reliable_refute", 0) or 0)
    n_official = int(stance_sum.get("official_refute", 0) or 0)
    n_support = int(stance_sum.get("reliable_support", 0) or 0)
    refute_domains = stance_sum.get("reliable_refute_domains") or []
    refute_weight = n_refute + n_official    # 官方来源（央视/新华/央广/人民）权重加倍
    evidence_pts = 0
    if has_claim and refute_weight >= EVIDENCE_MIN_WEIGHT_HIGH:
        evidence_pts = max(0, EVIDENCE_RISK_MULTI - risk)    # 已辟谣（含官方来源）→ 高风险档
    elif has_claim and refute_weight == 1:
        evidence_pts = max(0, EVIDENCE_RISK_SINGLE - risk)   # 单一来源反驳 → 保守抬到疑似档
    risk = max(0, min(100, risk + evidence_pts))

    label = LABEL_NO_CLAIM if not has_claim else _label_for(risk)
    tier = speech_tier(emotion, attack, logic)

    # ---- 可读信号 ----
    signals: List[str] = []
    for v in markers.get("risk_hits", {}).values():
        signals.append(f"{v['label']}：{'、'.join(v['hits'])}（+{v['points']}）")
    for v in markers.get("credibility_hits", {}).values():
        signals.append(f"可信度线索·{v['label']}：{'、'.join(v['hits'])}（{v['points']}）")
    if emotion_pts:
        signals.append(f"情绪化强度 {_to_float(emotion.get('emotion_score')):g}/10（+{emotion_pts}）")
    if logic_pts:
        extra = f"，存在{fallacy}" if fallacy not in ("", "无", "未知") else ""
        signals.append(f"逻辑关联性{rel}{extra}（+{logic_pts}）")
    if claims_pts:
        signals.append(f"{claims.get('unsourced_unverifiable', 0)} 条事实声明既无来源又无法核实（+{claims_pts}）")
    if not has_claim:
        signals.append("文本中没有可被核实的事实性声明" + ("，风险分已封顶" if capped else ""))
    if evidence_pts:
        dom = "、".join(refute_domains[:3])
        official = f"，含官方来源 {n_official} 个" if n_official else ""
        signals.append(f"证据核实：{n_refute} 个独立可靠来源已辟谣{official}（{dom}）（+{evidence_pts}）")
    elif n_support and evidence.get("entries"):
        signals.append("证据核实：有可靠来源支持该说法（主张可能属实）")
    elif evidence.get("entries"):
        signals.append("证据核实：未找到可靠来源的直接定论")

    return {
        "label": label,
        "risk_score": risk,
        "confidence": confidence,
        "speech_tier": tier,
        "has_factual_claim": has_claim,
        "claim_basis": claim_basis,
        "claims": claims.get("claims", []),
        "evidence_hint": evidence.get("verdict_hint") or "",
        "score_breakdown": {
            "话术特征": marker_pts, "可信度线索": cred_pts, "情绪强度": emotion_pts,
            "逻辑缺陷": logic_pts, "无来源声明": claims_pts,
            "证据核实": evidence_pts, "封顶前合计": raw,
        },
        "signals": signals,
        "degraded_components": degraded,
        "advice": ADVICE[label],
    }
