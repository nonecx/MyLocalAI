# -*- coding: utf-8 -*-
"""谣言特征扫描（纯正则，离线，不耗 API）。

谣言的传播学特征比"情绪强不强"更能说明问题。本模块把常见的中文谣言话术
归成 6 类风险特征 + 3 类可信度线索，逐类命中后给出可解释的加/减分：

  风险特征（加分）
    source_vague  来源模糊    听说/据说/网传/内部人士/朋友的朋友……
    virality      诱导传播    速转/扩散/转给家人/不转不是中国人……
    fear_claim    恐吓性断言  致癌/下毒/爆炸/千万别……
    conspiracy    阴谋叙事    官方不敢说/被封杀/删帖/真相是……
    absolute      绝对化措辞  所有人/百分之百/肯定是……
    urgency       制造紧迫感  震惊/紧急/重磅/细思极恐……
  可信度线索（减分，注意：线索只是"文本里出现了这类说法"，并未核验真伪）
    source_cited  引用了来源  据XX报道/来源：/链接/研究表明……
    official      官方渠道    XX部门发布/通报/辟谣……
    numeric_detail 具体细节   日期/数字+单位

权重是人工经验值，未经标注数据校准，仅作启发式信号。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# (key, 中文名, 权重, 正则列表)
RISK_CATEGORIES: List[Tuple[str, str, int, List[str]]] = [
    ("source_vague", "来源模糊", 12, [
        r"听说", r"据说", r"听人说", r"有人说", r"据传", r"网传", r"传言",
        r"(朋友圈|群里|微博上?)(都)?在(传|说|转)", r"内部(人士|消息|人员)", r"知情人",
        r"有消息(称|说|指出)", r"小道消息", r"朋友的朋友",
        r"我(一个|有个)?(朋友|同学|亲戚|同事)(说|告诉|在.{0,8}(工作|上班))",
    ]),
    ("virality", "诱导传播", 15, [
        r"速转", r"求扩散", r"紧急扩散", r"扩散(出去|一下|开来)", r"转起来", r"帮忙转", r"转给",
        r"转发(给|出去|扩散|让|一下)", r"不转不是", r"赶紧(转|告诉|通知|分享)",
        r"务必(转|告诉|通知|分享)", r"告诉.{0,6}(家人|亲人|身边的?人|朋友)",
        r"让更多(的)?人知道", r"看到的(人)?(请|赶紧|务必)",
    ]),
    ("fear_claim", "恐吓性断言", 10, [
        r"致癌", r"致命", r"剧毒", r"有毒", r"下毒", r"投毒", r"中毒", r"爆炸", r"大规模(感染|爆发|死亡)",
        r"疫情(爆发|扩散)", r"传染", r"癌症", r"丧命", r"危及生命", r"千万(别|不要|不能)", r"绝对不能",
    ]),
    ("conspiracy", "阴谋叙事", 15, [
        r"官方(根本|都|就)?不敢", r"不敢(说|公布|报道|曝光|承认)", r"被(封|删|屏蔽|禁|封杀|和谐)",
        r"封杀", r"删帖", r"压(下去|着不让)", r"瞒着", r"隐瞒", r"掩盖", r"不让(你|我们|大家)知道",
        r"媒体(都)?(不|不敢)报", r"真相是", r"你不知道的真相", r"阴谋", r"幕后黑手", r"洗脑",
    ]),
    ("absolute", "绝对化措辞", 6, [
        r"所有人(都)?", r"全都", r"百分之百", r"100%", r"绝对", r"肯定是", r"一定是",
        r"毫无疑问", r"无一例外", r"铁定",
    ]),
    ("urgency", "制造紧迫感", 6, [
        r"震惊", r"紧急(通知|情况)?", r"重磅", r"速看", r"惊天", r"细思极恐", r"不看后悔",
        r"删前速看", r"突发",
    ]),
]

CREDIBILITY_CATEGORIES: List[Tuple[str, str, int, List[str]]] = [
    ("source_cited", "引用了来源", 6, [
        r"(据|根据)[^，。,.\s]{1,12}(报道|通报|公告|数据|统计|发布)", r"来源[:：]", r"https?://", r"www\.",
        r"参考文献", r"论文", r"研究(表明|显示|发现|指出)",
    ]),
    ("official", "官方渠道", 6, [
        r"(国家|省|市|县|区|卫健委|气象局|地震局|公安|法院|检察院|教育部|统计局|官方)"
        r"[^，。,.\s]{0,6}(发布|通报|公告|回应|证实|辟谣)",
    ]),
    ("numeric_detail", "具体细节", 4, [
        r"\d{4}年", r"\d{1,2}月\d{1,2}[日号]", r"\d+(\.\d+)?\s?(%|％|万|亿|人|例|吨|公里|元|份)",
    ]),
]

MARKER_CAP = 45          # 风险特征总加分上限
CREDIBILITY_CAP = 15     # 可信度线索总减分上限（绝对值）
_MAX_SHOWN_HITS = 5


def _config_weights() -> Dict[str, Any]:
    """从 src/mylocalai/config/verdict_rules.json 的 markers 段读权重（正则表仍在本文件里）。

    2026-10-22 修复 G5b：数值权重外置，换行业/换判定倾向不必改 Python。
    读不到就用下面的内置默认值（与 JSON 里的默认值一致）。
    """
    try:
        from mylocalai.config.rules import load_rules
        markers = load_rules().get("markers") or {}
    except Exception:  # noqa: BLE001  配置读不到不该让特征扫描崩掉
        markers = {}
    risk = markers.get("risk_weights") or {}
    cred = markers.get("credibility_weights") or {}
    return {
        "marker_cap": markers.get("marker_cap", MARKER_CAP),
        "credibility_cap": markers.get("credibility_cap", CREDIBILITY_CAP),
        "risk": {key: risk.get(key, weight) for key, _n, weight, _p in RISK_CATEGORIES},
        "cred": {key: cred.get(key, weight) for key, _n, weight, _p in CREDIBILITY_CATEGORIES},
    }


_WEIGHTS = _config_weights()
MARKER_CAP_EFFECTIVE = _WEIGHTS["marker_cap"]
CREDIBILITY_CAP_EFFECTIVE = _WEIGHTS["credibility_cap"]


def _compile(table):
    weights = _WEIGHTS["risk"] if table is RISK_CATEGORIES else _WEIGHTS["cred"]
    return [(k, name, weights.get(k, w), [re.compile(p) for p in pats]) for k, name, w, pats in table]


_RISK = _compile(RISK_CATEGORIES)
_CRED = _compile(CREDIBILITY_CATEGORIES)


def _match_category(text: str, patterns) -> List[str]:
    """返回该类命中的去重原文片段（保序）。"""
    seen: List[str] = []
    for pat in patterns:
        for m in pat.finditer(text):
            frag = m.group(0).strip()
            if frag and frag not in seen:
                seen.append(frag)
    return seen


def scan_markers(text: str) -> Dict[str, Any]:
    """
    扫描谣言特征。永不抛异常。

    返回:
      risk_hits         {key: {"label", "hits": [...], "points": int}}  仅含命中的类
      credibility_hits  同上，points 为负数
      marker_points     风险特征总加分（≤ MARKER_CAP）
      credibility_points 可信度线索总减分（≤ 0，≥ -CREDIBILITY_CAP）
      assertion_signal  是否出现"在陈述事实"的迹象（来源模糊/恐吓/阴谋/传播诱导任一命中）
    """
    text = str(text or "")
    risk_hits: Dict[str, Any] = {}
    cred_hits: Dict[str, Any] = {}
    try:
        for key, name, weight, pats in _RISK:
            hits = _match_category(text, pats)
            if hits:
                # 首个命中记满分，每多一个不同命中 +25%，最多 1.5 倍
                points = round(weight * min(1 + 0.25 * (len(hits) - 1), 1.5))
                risk_hits[key] = {"label": name, "hits": hits[:_MAX_SHOWN_HITS], "points": points}
        for key, name, weight, pats in _CRED:
            hits = _match_category(text, pats)
            if hits:
                cred_hits[key] = {"label": name, "hits": hits[:_MAX_SHOWN_HITS], "points": -weight}
    except Exception as exc:  # noqa: BLE001  正则层面理论上不会失败，保险起见
        return {
            "risk_hits": {}, "credibility_hits": {}, "marker_points": 0,
            "credibility_points": 0, "assertion_signal": False,
            "error": f"特征扫描失败: {type(exc).__name__}: {exc}",
        }

    marker_points = min(MARKER_CAP_EFFECTIVE, sum(v["points"] for v in risk_hits.values()))
    cred_points = max(-CREDIBILITY_CAP_EFFECTIVE, sum(v["points"] for v in cred_hits.values()))
    assertion_signal = any(k in risk_hits for k in ("source_vague", "fear_claim", "conspiracy", "virality"))
    return {
        "risk_hits": risk_hits,
        "credibility_hits": cred_hits,
        "marker_points": marker_points,
        "credibility_points": cred_points,
        "assertion_signal": assertion_signal,
    }
