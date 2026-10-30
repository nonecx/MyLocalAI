# -*- coding: utf-8 -*-
"""深读受控生成的**后处理护栏**：不依赖模型自觉，全部由代码实现。

设计前提（项目哲学第 2、4 条）：LLM 只做受限生成，降级不崩。因此本模块把
"模型应该做到的事"全部在代码里再验一遍——模型漏挂证据、自创议题 id、写出
L3 却没有任何 L1/L2 支撑、滑向宏大叙事/政治化归因/群体贴标签，都在这里被
拦下，而不是指望提示词起作用。

入口只有一个纯函数 `validate_and_sanitize()`，不碰 LLM、不联网、可单测：

    1. schema 校验      字段齐全、类型正确，不合规直接判不合格
    2. 议题封闭性       issue_tags[].id 不在清单 → 改 other 且降 confidence 为 low；
                        other 也不允许 → 丢弃该 tag
    3. 证据有效性       evidence_ids 必须在有效证据集内，无效 id 剔除；
                        剔除后为空 → 丢弃该条目
    4. L3 挂靠          全篇无任何 L1/L2 条目时，L3 条目丢弃或降级为 uncertainty
    5. 禁词/禁归因      命中 banned_patterns（正则，可配置）→ 整段丢弃 + 记 guard_dropped
    6. 反方必填         require_counter_reading=true 时缺 counter_reading → 不合格
    7. 字数上限         超 max_words → 截断并在 uncertainty 追加说明
    8. 降级标记         任何条目被丢弃 → degraded=true，guard_dropped 记录原因

与 `deep_read.py` 的分工：`deep_read.py` 负责"要不要生成、怎么生成"，
本模块负责"生成出来的东西能不能用"。两者都**不参与任何打分**——深读产出
与 verdict 打分、缓存主结论物理隔离（见 deep_read.py 的隔离说明）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 合法置信度与抽象层级（封闭集，来自 2.2 的输出 schema）
CONFIDENCE_LEVELS: Tuple[str, ...] = ("high", "medium", "low")
EVIDENCE_LEVELS: Tuple[str, ...] = ("L1", "L2", "L3")

# 兜底议题 id：清单外但模型给了 id 时改成它
FALLBACK_TAG = "other"

# 默认禁词表（可被 deep_read_rules.json 的 banned_patterns 覆盖）。
# 覆盖三类：具体主体归因、样本推普遍、价值判断/预测——与任务书 2.4 一致。
DEFAULT_BANNED_PATTERNS: List[Dict[str, str]] = [
    {"id": "subject_blame",
     "pattern": r"(政府|官方|有关部门|部门|机关|公司|企业|某群体|某地人|当地人|相关部门)"
                r"[^。！？；;\n]{0,12}"
                r"(在瞒|隐瞒|欺骗|撒谎|造假|篡改|压制|操控|漠视|放任|失职|渎职|打压|封锁|甩锅|遮丑|捂嘴)",
     "note": "具体主体归因：主语是机构/公司/群体/地域 + 负面谓词"},
    {"id": "universal_claim",
     "pattern": r"(全社会|普遍|必然|所有人|无一例外|人人都|谁都|无一幸免|都在|全在|统统|清一色)",
     "note": "从样本推普遍性（含群体/地域整体化表述）"},
    {"id": "value_judgment",
     "pattern": r"(可悲|落后|愚昧|无知|奴性|麻木|可笑|荒唐)",
     "note": "价值判断词"},
    {"id": "prediction",
     "pattern": r"(将引发|将会引发|必将引发|必然导致|迟早|终将|不可避免|长此以往)",
     "note": "对未发生事件的预测"},
]


class GuardRejected(ValueError):
    """深读输出整体不合格（schema 不合法或缺必填项）。

    继承 ValueError 便于调用方统一兜住；`reason` 是给用户/日志看的中文说明。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _is_real(value: Any) -> bool:
    """是否是实数（bool 不算）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text_of(value: Any) -> str:
    """任意取值转成去空白字符串（模型可能给数字/null）。"""
    return str(value).strip() if value is not None else ""


def _evidence_ids_of(raw: Any) -> List[str]:
    """把 evidence_ids 规整成字符串列表（不校验有效性，交给调用方）。"""
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        name = _text_of(item)
        if name and name not in out:
            out.append(name)
    return out


def _compile_banned(patterns: Any) -> List[Dict[str, str]]:
    """把禁词表规整成 [{id, pattern, note, regex}]；坏项跳过（不抛异常）。

    坏正则（写坏了圆括号之类）不能让整个深读链路崩：跳过该项并继续，
    与项目"配置写坏退回默认"的既有约定一致。
    """
    items = patterns if isinstance(patterns, list) else list(DEFAULT_BANNED_PATTERNS)
    compiled: List[Dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pattern = item.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        try:
            regex = re.compile(pattern)
        except re.error:
            continue
        compiled.append({
            "id": str(item.get("id") or "").strip(),
            "pattern": pattern,
            "note": str(item.get("note") or "").strip(),
            "regex": regex,
        })
    return compiled


def _banned_hits(text: str, banned: Sequence[Dict[str, Any]]) -> List[str]:
    """返回 text 命中的禁词 id 列表（未命中时空列表）。"""
    if not text:
        return []
    hits: List[str] = []
    for item in banned:
        if item["regex"].search(text):
            name = item["id"] or "banned"
            if name not in hits:
                hits.append(name)
    return hits


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def validate_and_sanitize(raw_output: Any,
                          issues: Any = None,
                          evidence_ids: Optional[Sequence[str]] = None,
                          rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """校验并清洗深读的模型输出，返回**可直接给前端的结构**。

    参数:
        raw_output:  模型输出（`llm_client.extract_json_obj` 之后的 dict）。
                     非 dict 或 None 时抛 `GuardRejected`。
        issues:      议题清单，两种形态都接受：
                       * list[dict] —— `society_issues.json` 的 issues 数组
                       * dict —— {"issues": [...], "fallback": "other"} 整个配置
                     None / 空时退化为"只认 other"（清单加载失败的安全侧）。
        evidence_ids: 本次可用的有效证据编号（如 ["e1","e2",...]）。
                     None 表示**不校验证据有效性**（调用方没有证据集时）。
        rules:       规则字典（`deep_read.load_rules()` 的输出）。读取
                     `rules.deep_read` 的 max_words / require_counter_reading /
                     max_issue_tags / allow_other_tag，以及 `rules.banned_patterns`。
                     None 时用内置默认。

    返回（永不返回 None）:
        {"applicable": bool, "reason": str, "degraded": bool, "data": {...},
         "guard_dropped": [...], "warnings": [...]}

        * `applicable=False` 是**必须支持的出口**：纯情绪宣泄、纯事实陈述、
          纯商业推广、证据不足时输出 {"applicable": False, "reason": "..."}，
          不得为"有输出"而编（任务书 2.2）。
        * 条目被护栏丢弃时 `degraded=True`，`guard_dropped` 记下丢了多少、为什么。

    抛 `GuardRejected`：模型输出根本不是合规 JSON 对象、或缺 `counter_reading`
    这类必填项——此时调用方应走本地模板回退（见 `deep_read.py` 的回退路径）。
    """
    params = (rules or {}).get("deep_read") if isinstance(rules, dict) else None
    params = params if isinstance(params, dict) else {}
    max_words = params.get("max_words", 300)
    if not (isinstance(max_words, int) and not isinstance(max_words, bool) and max_words > 0):
        max_words = 300
    require_counter = params.get("require_counter_reading", True)
    if not isinstance(require_counter, bool):
        require_counter = True
    max_tags = params.get("max_issue_tags", 3)
    if not (isinstance(max_tags, int) and not isinstance(max_tags, bool) and max_tags > 0):
        max_tags = 3
    allow_other = params.get("allow_other_tag", True)
    if not isinstance(allow_other, bool):
        allow_other = True
    banned = _compile_banned((rules or {}).get("banned_patterns"))

    # ---- 1) schema 校验 ----
    if not isinstance(raw_output, dict):
        raise GuardRejected(f"模型输出不是 JSON 对象（实际是 {type(raw_output).__name__}），无法做深读")

    applicable = raw_output.get("applicable", True)
    if not isinstance(applicable, bool):
        raise GuardRejected(f"applicable 必须是布尔值（实际是 {type(applicable).__name__}）")
    if not applicable:
        # 出口：证据不足/话题不适合时不许为"有输出"而编
        reason = _text_of(raw_output.get("reason")) or "模型判断该话题不适合做社会议题分析"
        return {"applicable": False, "reason": reason, "degraded": False,
                "data": {}, "guard_dropped": [], "warnings": []}

    tag_ids = _known_tag_ids(issues)
    valid_evidence = None if evidence_ids is None else {str(e) for e in evidence_ids}
    dropped: List[str] = []
    warnings: List[str] = []
    # L3 无支撑时降级成的显式不确定性（最后并进 uncertainty 字段）
    uncertainty_extra: List[str] = []

    # ---- 2) 议题封闭性 ----
    raw_tags = raw_output.get("issue_tags")
    if not isinstance(raw_tags, list) or not raw_tags:
        raise GuardRejected("issue_tags 缺失或不是非空数组")
    tags: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for item in raw_tags:
        if not isinstance(item, dict):
            warnings.append("issue_tags 含非对象项，已忽略")
            continue
        tag_id = _text_of(item.get("id"))
        confidence = _text_of(item.get("confidence")) or "low"
        if confidence not in CONFIDENCE_LEVELS:
            warnings.append(f"议题 {tag_id or '(空)'} 的 confidence={confidence!r} 不合法，已降为 low")
            confidence = "low"
        if tag_id not in tag_ids:
            if not allow_other:
                dropped.append(f"issue_tag {tag_id or '(空)'}：不在议题清单且不允许 other，已丢弃")
                continue
            warnings.append(f"议题 id {tag_id or '(空)'} 不在封闭清单内，已改为 {FALLBACK_TAG} 并降置信度")
            tag_id = FALLBACK_TAG
            confidence = "low"
        if tag_id in seen_ids:
            continue
        seen_ids.add(tag_id)
        ids = _filter_evidence(_evidence_ids_of(item.get("evidence_ids")), valid_evidence, dropped,
                               f"issue_tag {tag_id}")
        if not ids:
            continue
        tags.append({"id": tag_id, "confidence": confidence, "evidence_ids": ids})
        if len(tags) >= max_tags:
            if len(tags) < len(raw_tags):
                warnings.append(f"议题标签超过上限 {max_tags}，多出的已忽略")
            break
    if not tags:
        raise GuardRejected("全部议题标签都未通过校验（无有效证据或不在清单内）")

    # ---- 3) 证据有效性 + 5) 禁词：tensions ----
    raw_tensions = raw_output.get("tensions")
    if not isinstance(raw_tensions, list) or not raw_tensions:
        raise GuardRejected("tensions 缺失或不是非空数组")
    tensions: List[Dict[str, Any]] = []
    for item in raw_tensions:
        if not isinstance(item, dict):
            warnings.append("tensions 含非对象项，已忽略")
            continue
        a = _text_of(item.get("a"))
        b = _text_of(item.get("b"))
        text = f"{a} 与 {b}"
        if not a or not b:
            dropped.append("tension 缺少 a 或 b 端，已丢弃")
            continue
        hits = _banned_hits(text, banned)
        if hits:
            dropped.append(f"tension（{text}）命中禁词 {'、'.join(hits)}，已整段丢弃")
            continue
        ids = _filter_evidence(_evidence_ids_of(item.get("evidence_ids")), valid_evidence, dropped,
                               "tension")
        if not ids:
            continue
        level = _text_of(item.get("level")) or "L1"
        if level not in EVIDENCE_LEVELS:
            warnings.append(f"tension（{text}）的 level={level!r} 不合法，已按 L1 计")
            level = "L1"
        tensions.append({"a": a, "b": b, "evidence_ids": ids, "level": level})

    # ---- 3) 证据有效性 + 5) 禁词：phenomena ----
    raw_phenomena = raw_output.get("phenomena")
    phenomena: List[Dict[str, Any]] = []
    if isinstance(raw_phenomena, list):
        for item in raw_phenomena:
            if not isinstance(item, dict):
                continue
            text = _text_of(item.get("text"))
            if not text:
                continue
            hits = _banned_hits(text, banned)
            if hits:
                dropped.append(f"phenomenon（{text[:24]}…）命中禁词 {'、'.join(hits)}，已整段丢弃")
                continue
            ids = _filter_evidence(_evidence_ids_of(item.get("evidence_ids")), valid_evidence,
                                   dropped, "phenomenon")
            if not ids:
                continue
            level = _text_of(item.get("level")) or "L1"
            if level not in EVIDENCE_LEVELS:
                warnings.append(f"phenomenon（{text[:16]}…）的 level={level!r} 不合法，已按 L1 计")
                level = "L1"
            entry = {"text": text, "level": level, "evidence_ids": ids}
            count = _text_of(item.get("count"))
            if count:
                entry["count"] = count
            phenomena.append(entry)
    else:
        warnings.append("phenomena 缺失或不是数组，已按空处理")

    # ---- 4) L3 挂靠 ----
    # 全篇无任何 L1/L2 条目时，L3 不得单独存在：降级为 uncertainty（保留判断但不当作结论）
    has_lower = any(p.get("level") in ("L1", "L2") for p in phenomena) or \
        any(t.get("level") in ("L1", "L2") for t in tensions)
    if not has_lower:
        for coll, kind in ((tensions, "tension"), (phenomena, "phenomenon")):
            survivors = []
            for entry in coll:
                if entry.get("level") != "L3":
                    survivors.append(entry)
                    continue
                summary = (f"{entry.get('a', '')} 与 {entry.get('b', '')}"
                           if kind == "tension" else entry.get("text", ""))
                dropped.append(f"{kind}（{summary[:24]}）L3 无 L1/L2 支撑，已降级为显式不确定性")
                uncertainty_extra.append(_l3_uncertainty_text(entry, kind))
            coll[:] = survivors

    # ---- 6) 反方必填 ----
    raw_counter = raw_output.get("counter_reading")
    counter: Optional[Dict[str, Any]] = None
    if isinstance(raw_counter, dict) and _text_of(raw_counter.get("text")):
        counter_text = _text_of(raw_counter.get("text"))
        hits = _banned_hits(counter_text, banned)
        if hits:
            dropped.append(f"counter_reading 命中禁词 {'、'.join(hits)}，已整段丢弃")
            counter = None
        else:
            ids = _filter_evidence(_evidence_ids_of(raw_counter.get("evidence_ids")),
                                   valid_evidence, dropped, "counter_reading")
            counter = {"text": counter_text, "evidence_ids": ids}
    elif raw_counter is not None:
        warnings.append("counter_reading 结构不完整（缺 text），已按缺失处理")
    if counter is None and require_counter:
        raise GuardRejected("缺少 counter_reading（反方解读），而规则要求必填")

    # ---- 7) 字数上限 ----
    body = _render_body(tags, tensions, phenomena, counter, raw_output)
    truncated = False
    if len(body) > max_words:
        body = body[:max_words]
        truncated = True

    # ---- 组装 ----
    # uncertainty 只保留字符串；L3 降级来的显式不确定性并进来
    uncertainty = [str(u) for u in (raw_output.get("uncertainty") or [])
                   if isinstance(u, str) and _text_of(u)]
    uncertainty.extend(uncertainty_extra)
    if truncated:
        uncertainty.append("输出因长度上限截断")
    # guard_dropped 已经逐条记了原因，不再往 warnings 里重复一份
    warnings = [w for w in warnings if "护栏丢弃：" not in w]

    boundary_raw = raw_output.get("boundary")
    boundary = boundary_raw if isinstance(boundary_raw, dict) else {}

    data = {
        "issue_tags": tags,
        "tensions": tensions,
        "phenomena": phenomena,
        "counter_reading": counter,
        "boundary": boundary,
        "uncertainty": uncertainty,
        "degraded": bool(dropped),
        "body": body,
        "truncated": truncated,
    }
    return {"applicable": True, "reason": "", "degraded": bool(dropped),
            "data": data, "guard_dropped": dropped, "warnings": warnings}


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _known_tag_ids(issues: Any) -> set:
    """从议题清单取出全部合法 id。清单不可用时返回 {other}（安全侧：只认兜底标签）。"""
    items: Any = None
    if isinstance(issues, dict):
        items = issues.get("issues")
    elif isinstance(issues, list):
        items = issues
    ids = {FALLBACK_TAG}
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                name = _text_of(item.get("id"))
                if name:
                    ids.add(name)
    return ids


def _filter_evidence(ids: List[str], valid: Optional[set], dropped: List[str],
                     label: str) -> List[str]:
    """剔除无效 evidence_id；剔除后为空则记一条 guard_dropped 并返回空列表。

    `valid is None` 表示调用方没有证据集，此时不校验（保持向后兼容的宽松路径）。
    """
    if valid is None:
        return ids
    kept = [i for i in ids if i in valid]
    if len(kept) != len(ids):
        invalid = [i for i in ids if i not in valid]
        dropped.append(f"{label} 的 evidence_ids {invalid} 不在有效证据集内，已剔除")
    if not kept:
        dropped.append(f"{label} 剔除无效证据后无任何 evidence_id，该条目已丢弃")
    return kept


def _l3_uncertainty_text(entry: Dict[str, Any], kind: str) -> str:
    """把降级掉的 L3 条目转成一条显式不确定性（判断保留，但不再当结论）。"""
    if kind == "tension":
        return (f"「{entry.get('a', '')}」与「{entry.get('b', '')}」之间的结构性判断"
                f"缺少可直接引用的具体证据支撑，仅作假设列出")
    return f"「{entry.get('text', '')}」为结构性推断，缺少具体证据支撑，仅作假设列出"


def _render_body(tags: List[Dict[str, Any]], tensions: List[Dict[str, Any]],
                 phenomena: List[Dict[str, Any]], counter: Optional[Dict[str, Any]],
                 raw_output: Dict[str, Any]) -> str:
    """把清洗后的结构渲染成一段人类可读文本（用于 text 字段与字数统计）。"""
    lines: List[str] = []
    labels = [t["id"] for t in tags]
    if labels:
        lines.append("议题：" + "、".join(labels))
    for t in tensions:
        lines.append(f"张力：{t['a']} 与 {t['b']}（{t['level']}，证据 {'、'.join(t['evidence_ids'])}）")
    for p in phenomena:
        count = f"（{p['count']}）" if p.get("count") else ""
        lines.append(f"现象：{p['text']}{count}（{p['level']}，证据 {'、'.join(p['evidence_ids'])}）")
    if counter:
        lines.append(f"反方：{counter['text']}")
    boundary = raw_output.get("boundary")
    if isinstance(boundary, dict):
        scope = _text_of(boundary.get("scope")) or "sample"
        size = boundary.get("sample_size")
        flags = boundary.get("proxy_flags")
        parts = [f"边界：{scope}"]
        if size is not None:
            parts.append(f"样本量 {size}")
        if isinstance(flags, list) and flags:
            parts.append("代理值 " + "、".join(str(f) for f in flags))
        lines.append("；".join(parts))
    return "\n".join(lines)


__all__ = ["validate_and_sanitize", "GuardRejected", "DEFAULT_BANNED_PATTERNS"]
