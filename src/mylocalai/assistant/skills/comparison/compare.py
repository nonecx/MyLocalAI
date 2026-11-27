# -*- coding: utf-8 -*-
"""档案对比纯函数（专用技能 dossier-comparison，第五波）：两份档案的只读 diff。

改了什么、为什么、锁定什么：
   - 新增 `assistant.skills.comparison.compare`：把"读两份档案 → 结论标签演进 →
     证据/信源域名增减 → 声明指纹相似度"编排成一组纯函数（零 LLM、零网络、
     零写库、**永不抛**——任何读取失败折 partial + gaps 明示，缺章按 absent
     处理不编造）。
   - 数据一律经注入的 store 直读（get_dossier/conclusions/evidence），与档案库
     单一事实源保持一致；工具层（tools_compare）只做跨区防护与呈现，不做第二个
     事实源（test_comparison 有与 store 直读的对拍用例）。
   - 相似度复用 `assistant.fingerprint` 的 normalize_claim/shingles/jaccard
     （输入文本归一后），阈值与 assistant_rules.json 的 fingerprint_threshold
     同值（0.72，外置到 rules.json 可调）；只标「疑似同源」，**不断言**两侧是
     同一说法。
   - 口径：纯读取 + diff，不重算不改写；差异只述「标签从 X 变为 Y（as_of …）」，
     不做升级/降级判断；不建档、无分数（本模块不读 risk_score，也不产出任何
     评分字段）。

依赖方向：本模块**不 import dossier**（store 由调用方注入，duck-typed：只要
有 get_dossier/conclusions/evidence 三个方法即可，测试可用替身），只 import
标准库与 assistant.fingerprint（assistant 包内，test_import_rules 允许）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from mylocalai.assistant.fingerprint import jaccard, normalize_claim, shingles

__all__ = ["DEFAULT_COMPARISON_RULES", "MAX_POWER_RULES", "load_rules", "compare_dossiers"]

_HERE = Path(__file__).resolve().parent
_RULES_PATH = _HERE / "rules.json"

#: 内置默认值（与 rules.json 同值；rules.json 缺失/写坏时逐键兜底）。
#: 三个裁剪键只影响**呈现量**（版本历史/证据条目/域名清单各显示多少），不动判定；
#: similarity_threshold 与 assistant_rules.json 的 fingerprint_threshold 同口径。最大功率
#: 模式会把呈现条数抬到各自安全区间上限，不修改相似度阈值。
DEFAULT_COMPARISON_RULES: Dict[str, Any] = {
    "max_versions": 10,
    "max_evidence_items": 20,
    "max_domain_items": 20,
    "similarity_threshold": 0.72,
}

#: 数值安全区间（未列出的键按 defaults 类型校验，不夹取）。
_RULE_LIMITS: Dict[str, Tuple[float, float]] = {
    "max_versions": (1, 50),
    "max_evidence_items": (1, 100),
    "max_domain_items": (1, 100),
    "similarity_threshold": (0.30, 0.95),
}

#: 最大功率模式只抬呈现条数；相似度阈值属于判定口径，保持原配置。
MAX_POWER_RULES: Dict[str, Any] = {
    key: bounds[1] for key, bounds in _RULE_LIMITS.items()
    if key.startswith("max_")
}

#: data 键名锁定（SKILL.md 头部契约同款；测试按此锁定，增删键先改文档）
_DATA_KEYS = ("left", "right", "similarity", "conclusion_delta",
              "evidence_diff", "source_diff", "limits", "gaps")
_SIDE_KEYS = ("id", "title", "label", "confidence", "as_of", "versions",
              "evidence_count", "domains")
_SIMILARITY_KEYS = ("score", "threshold", "suspected_same_source", "basis")
_DELTA_KEYS = ("changed", "from_label", "to_label", "from_as_of", "to_as_of")
_EVIDENCE_DIFF_KEYS = ("added_count", "removed_count", "added", "removed")
_SOURCE_DIFF_KEYS = ("added_domains", "removed_domains")

#: 相似度徽章的固定口径文案（只标疑似同源，不作同一性断言）
_SIMILARITY_BASIS = ("声明指纹相似度（输入文本归一化后的字符 k-shingle Jaccard）"
                     "只用于标注「疑似同源」，不作同一性断言；即便疑似同源，"
                     "两侧结论也各自独立、分别审阅。")

#: 缺章的固定说明（懒加载是常态：没生成过的章节如实 absent，不编造）
_ABSENT_CONCLUSION = "尚无结论（该档案还没有跑过核查）"
_ABSENT_EVIDENCE = "证据本为空（该档案还没有收集过证据）"
_ABSENT_TEXT = "没有可用的声明原文（未保存或已按保留策略清理），跳过指纹相似度比对"


# ---------------------------------------------------------------------------
# 规则加载
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001  配置缺失/写坏 = 回退默认，不让运行崩
        return {}


def _clamp_rules(rules: Any) -> Dict[str, Any]:
    """逐键类型校验 + 数值夹取 + 未知键丢弃（坏配置不放大成故障）。

    与 skills/source 的 load_rules 同款降级文化：缺键回退默认、坏类型回退默认、
    数值越界夹到 _RULE_LIMITS 的安全区间、未知键丢弃。
    """
    source = rules if isinstance(rules, Mapping) else {}
    out: Dict[str, Any] = {}
    for key, default in DEFAULT_COMPARISON_RULES.items():
        value = source.get(key, default)
        if isinstance(default, bool):
            out[key] = value if isinstance(value, bool) else default
            continue
        if isinstance(default, (int, float)):
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = float(default)
            if number != number or number in (float("inf"), float("-inf")):
                number = float(default)   # NaN/inf 按默认值，不放大成故障
            bounds = _RULE_LIMITS.get(key)
            if bounds is not None:
                number = min(max(number, float(bounds[0])), float(bounds[1]))
            out[key] = int(round(number)) if isinstance(default, int) else number
            continue
        out[key] = value
    return out


def load_rules(overrides: Any = None) -> Dict[str, Any]:
    """加载 comparison/rules.json：缺键回退默认、数值夹取、坏类型回退、未知键丢弃。

    传 dict 按"覆盖项"处理（便于测试按需收紧裁剪量，缺的键仍取默认）；不传读包内
    rules.json；传路径读该文件。文件缺失/不可解析 → 内置默认值（不抛）：取不到
    配置时"按默认裁剪量跑"比"整个技能不可用"更符合降级文化。
    """
    if isinstance(overrides, Mapping):
        return _clamp_rules(overrides)
    target = _RULES_PATH if overrides is None else Path(overrides)
    raw = _read_json(target)
    return _clamp_rules({**DEFAULT_COMPARISON_RULES,
                         **{k: v for k, v in raw.items()
                            if k in DEFAULT_COMPARISON_RULES}})


# ---------------------------------------------------------------------------
# 内部小工具（纯函数，永不抛）
# ---------------------------------------------------------------------------

def _text_of(value: Any) -> str:
    """库字段 → 去首尾空白字符串（None/坏形状按空串，不抛）。"""
    try:
        return str(value if value is not None else "").strip()
    except Exception:  # noqa: BLE001  __str__ 炸掉的怪对象按空串
        return ""


def _as_id(value: Any) -> Optional[int]:
    """宽松解析档案 id：int（拒 bool）/ 十进制字符串 → int；其余 None（拒绝）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.lstrip("-").isdigit():
            return int(s)
    return None


def _domains(evidence: List[Dict[str, Any]]) -> List[str]:
    """证据本 → 去重排序的非空域名清单（无证据 → 空表，不编造）。"""
    found = {_text_of(e.get("domain")) for e in evidence}
    found.discard("")
    return sorted(found)


def _by_url(evidence: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """证据本按 url 去重（首次出现为准，store 顺序为 batch,id 升序）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for entry in evidence:
        url = _text_of(entry.get("url"))
        if url and url not in out:
            out[url] = entry
    return out


# ---------------------------------------------------------------------------
# 单侧读取（每个子读独立兜底：单边读失败折 partial + gaps 明示）
# ---------------------------------------------------------------------------

def _read_side(store: Any, dossier_id: int, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """读一侧档案：本体 + 结论版本 + 证据本 + 声明原文。

    返回内部记录（**不进 data**，data 只收 side 公开 dict）：
      {"side": 公开侧 dict 或 None, "text": 声明原文, "conclusions": [...],
       "evidence": [...], "gaps": [...], "fatal": bool, "missing": bool}
    降级口径：
      - 本体读失败/读不到 → side=None、fatal=True（该侧整体不可用）；
      - 结论/证据读失败 → side 照常返回，缺字段按 absent 处理并记 gaps；
      - 每一步都 try/except：单边读失败只折 partial，绝不外抛。
    """
    rec: Dict[str, Any] = {"side": None, "text": "", "conclusions": [],
                           "evidence": [], "gaps": [], "fatal": False,
                           "missing": False}
    try:
        row = store.get_dossier(int(dossier_id))
    except Exception as exc:  # noqa: BLE001  库故障折成 gap，由上层折 partial
        rec["gaps"].append(f"档案 #{dossier_id} 本体读取失败："
                           f"{type(exc).__name__}: {exc}")
        rec["fatal"] = True
        return rec
    if not isinstance(row, dict) or not row:
        rec["gaps"].append(f"档案 #{dossier_id} 不存在")
        rec["missing"] = True
        return rec

    # 声明原文：优先 input_text，空则退 claim_excerpt（保留策略可能清掉前者）
    rec["text"] = _text_of(row.get("input_text")) or _text_of(row.get("claim_excerpt"))

    try:
        conclusions = store.conclusions(int(dossier_id)) or []
    except Exception as exc:  # noqa: BLE001  结论读失败按缺章处理，不编造
        conclusions = []
        rec["gaps"].append(f"档案 #{dossier_id} 结论版本读取失败："
                           f"{type(exc).__name__}: {exc}（按缺章处理，不编造）")
    rec["conclusions"] = [c for c in (conclusions if isinstance(conclusions, list)
                                      else []) if isinstance(c, dict)]

    try:
        evidence = store.evidence(int(dossier_id)) or []
    except Exception as exc:  # noqa: BLE001  证据读失败按缺章处理，不编造
        evidence = []
        rec["gaps"].append(f"档案 #{dossier_id} 证据本读取失败："
                           f"{type(exc).__name__}: {exc}（按缺章处理，不编造）")
    rec["evidence"] = [e for e in (evidence if isinstance(evidence, list)
                                   else []) if isinstance(e, dict)]

    latest = rec["conclusions"][-1] if rec["conclusions"] else None
    version_cap = max(1, int(cfg["max_versions"]))
    domain_cap = max(1, int(cfg["max_domain_items"]))
    rec["side"] = {
        "id": int(dossier_id),
        "title": _text_of(row.get("title")),
        # 缺结论 → None（缺章如实 absent，不填空串冒充"无标签结论"）
        "label": _text_of(latest.get("label")) if latest else None,
        "confidence": _text_of(latest.get("confidence")) if latest else None,
        "as_of": _text_of(latest.get("as_of")) if latest else None,
        # 版本历史：最近 version_cap 版（升序），只带标签/as_of/置信度档位，
        # **不带 risk_score**（对话卡去数值，《角色Agent工作流规格》§5 同口径）
        "versions": [{"version": c.get("version"),
                      "label": _text_of(c.get("label")),
                      "confidence": _text_of(c.get("confidence")),
                      "as_of": _text_of(c.get("as_of"))}
                     for c in rec["conclusions"][-version_cap:]],
        "evidence_count": len(rec["evidence"]),
        "domains": _domains(rec["evidence"])[:domain_cap],
    }
    return rec


# ---------------------------------------------------------------------------
# 各 diff 块（纯函数）
# ---------------------------------------------------------------------------

def _conclusion_delta(left_c: List[Dict[str, Any]],
                      right_c: List[Dict[str, Any]]) -> Dict[str, Any]:
    """标签演进（首末版本口径）：取「左档案首版 → 右档案末版」为演进两端。

    只陈述两端标签与 as_of，**不做升级/降级判断**（"从谣言变为存疑"是升级还是
    降级归用户/后续能力判断，本技能不表态）；任一端缺结论 → changed=False
    （缺章不编造，gap 由调用方记录）。
    """
    first = left_c[0] if left_c else None
    last = right_c[-1] if right_c else None
    from_label = _text_of(first.get("label")) if first else None
    to_label = _text_of(last.get("label")) if last else None
    return {
        "changed": bool(from_label and to_label and from_label != to_label),
        "from_label": from_label,
        "to_label": to_label,
        "from_as_of": _text_of(first.get("as_of")) if first else None,
        "to_as_of": _text_of(last.get("as_of")) if last else None,
    }


def _evidence_entry_view(entry: Dict[str, Any]) -> Dict[str, str]:
    """证据条目紧凑视图：url/title/relation（relation = 库里的 stance 立场字段）。"""
    return {"url": _text_of(entry.get("url")),
            "title": _text_of(entry.get("title")),
            "relation": _text_of(entry.get("stance"))}


def _evidence_diff(left_ev: List[Dict[str, Any]], right_ev: List[Dict[str, Any]],
                   cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """证据差集：按 url 去重，以左档案为基准——右有左无为「新增」、左有右无为「减少」。

    added_count/removed_count 是去重后的**总数**；added/removed 是按
    max_evidence_items 裁剪后的展示清单（裁剪在 limits 明示，不静默）。
    """
    left_map, right_map = _by_url(left_ev), _by_url(right_ev)
    added = [_evidence_entry_view(right_map[u])
             for u in sorted(right_map) if u not in left_map]
    removed = [_evidence_entry_view(left_map[u])
               for u in sorted(left_map) if u not in right_map]
    total_added, total_removed = len(added), len(removed)
    limits: List[str] = []
    cap = max(1, int(cfg["max_evidence_items"]))
    if total_added > cap:
        limits.append(f"证据「新增」条目仅显示前 {cap} 条（共 {total_added} 条，按 url 排序）")
        added = added[:cap]
    if total_removed > cap:
        limits.append(f"证据「减少」条目仅显示前 {cap} 条（共 {total_removed} 条，按 url 排序）")
        removed = removed[:cap]
    diff = {"added_count": total_added, "removed_count": total_removed,
            "added": added, "removed": removed}
    return diff, limits


def _source_diff(left_ev: List[Dict[str, Any]], right_ev: List[Dict[str, Any]],
                 cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """信源域名差集：右有左无为「新增」、左有右无为「减少」（同样按 max_domain_items 裁剪）。"""
    left_domains = _domains(left_ev)
    right_domains = _domains(right_ev)
    left_set, right_set = set(left_domains), set(right_domains)
    added = [d for d in right_domains if d not in left_set]
    removed = [d for d in left_domains if d not in right_set]
    limits: List[str] = []
    cap = max(1, int(cfg["max_domain_items"]))
    if len(added) > cap:
        limits.append(f"「新增」信源域名仅显示前 {cap} 个（共 {len(added)} 个）")
        added = added[:cap]
    if len(removed) > cap:
        limits.append(f"「减少」信源域名仅显示前 {cap} 个（共 {len(removed)} 个）")
        removed = removed[:cap]
    return {"added_domains": added, "removed_domains": removed}, limits


def _similarity(left_text: str, right_text: str,
                cfg: Dict[str, Any]) -> Dict[str, Any]:
    """声明指纹相似度（复用 assistant.fingerprint 的 Jaccard，输入文本归一后）。

    任一侧没有可用原文 → score=None（不可比，疑似同源=False，缺口由调用方记）；
    只标「疑似同源」，不作同一性断言（口径文案见 _SIMILARITY_BASIS）。
    """
    threshold = float(cfg["similarity_threshold"])
    basis = _SIMILARITY_BASIS
    if not left_text or not right_text:
        return {"score": None, "threshold": threshold,
                "suspected_same_source": False, "basis": basis}
    try:
        score = jaccard(shingles(normalize_claim(left_text)),
                        shingles(normalize_claim(right_text)))
        score = float(score)
    except Exception:  # noqa: BLE001  指纹不可用 = 不比对（diff 其余部分照常）
        return {"score": None, "threshold": threshold,
                "suspected_same_source": False, "basis": basis}
    return {"score": round(score, 4), "threshold": threshold,
            "suspected_same_source": bool(score >= threshold), "basis": basis}


# ---------------------------------------------------------------------------
# 拒绝/未找到的空形状（键名与完整形状一致，调用方按 status 分流）
# ---------------------------------------------------------------------------

def _empty_data(cfg: Dict[str, Any], gaps: List[str]) -> Dict[str, Any]:
    return {
        "left": None,
        "right": None,
        "similarity": {"score": None,
                       "threshold": float(cfg["similarity_threshold"]),
                       "suspected_same_source": False,
                       "basis": _SIMILARITY_BASIS},
        "conclusion_delta": {"changed": False, "from_label": None,
                             "to_label": None, "from_as_of": None,
                             "to_as_of": None},
        "evidence_diff": {"added_count": 0, "removed_count": 0,
                          "added": [], "removed": []},
        "source_diff": {"added_domains": [], "removed_domains": []},
        "limits": [],
        "gaps": list(gaps),
    }


def _refused(reason: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """参数层拒绝（相同 id / 非正整数 id）：不抛，返回拒绝形状。"""
    return {"status": "invalid", "missing_id": None,
            "data": _empty_data(cfg, [reason])}


def _not_found(missing_id: int, cfg: Dict[str, Any],
               gaps: List[str]) -> Dict[str, Any]:
    """一侧不存在：与跨区同形（由工具层折成「档案 #N 不存在」）。"""
    return {"status": "not_found", "missing_id": int(missing_id),
            "data": _empty_data(cfg, gaps or [f"档案 #{missing_id} 不存在"])}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def compare_dossiers(store: Any, left_id: Any, right_id: Any,
                     rules: Any = None) -> Dict[str, Any]:
    """两份档案的只读对比（纯函数，**永不抛**）。

    参数:
        store:       DossierStore（或同方法替身）：只用 get_dossier/conclusions/
                     evidence 三个读方法；
        left_id/right_id: 两个正整数档案 id。相同 id → status="invalid" 的拒绝
                     形状（工具层另抛 ValidationError，上层按 clarify 降级）；
        rules:       已加载的规则 dict / 覆盖 dict / 路径；None 读包内
                     rules.json（缺失或写坏逐键回退默认并夹取）。

    返回:
        {"status": "ok" | "partial" | "not_found" | "invalid",
         "missing_id": None | int,          # not_found 时指向不存在的一侧
         "data": {left, right, similarity, conclusion_delta, evidence_diff,
                  source_diff, limits, gaps}}   # 键名锁定，见 _DATA_KEYS

    口径（与 SKILL.md 同款）：纯读取 + diff，不重算不改写；单边读失败折 partial
    且 gaps 明示；缺章按 absent 处理不编造；相似度只标「疑似同源」不断言同一
    说法；差异只述「标签从 X 变为 Y（as_of …）」，不做升级/降级判断；不建档、
    无分数（不读 risk_score，也不产出任何评分字段）。
    """
    cfg = load_rules(rules)
    left = _as_id(left_id)
    right = _as_id(right_id)
    if left is None or right is None or left <= 0 or right <= 0:
        return _refused("档案 id 必须是正整数", cfg)
    if left == right:
        return _refused(f"左右档案 id 相同（#{left}）：对比需要两个不同的档案", cfg)

    left_rec = _read_side(store, left, cfg)
    if left_rec["missing"]:
        return _not_found(left, cfg, left_rec["gaps"])
    right_rec = _read_side(store, right, cfg)
    if right_rec["missing"]:
        return _not_found(right, cfg, right_rec["gaps"])

    gaps: List[str] = list(left_rec["gaps"]) + list(right_rec["gaps"])
    limits: List[str] = []

    # 缺章如实标注（懒加载是常态，不编造）
    for side_id, rec in ((left, left_rec), (right, right_rec)):
        if not rec["conclusions"]:
            gaps.append(f"档案 #{side_id} {_ABSENT_CONCLUSION}")
        if not rec["evidence"]:
            gaps.append(f"档案 #{side_id} {_ABSENT_EVIDENCE}")
        if not rec["text"]:
            gaps.append(f"档案 #{side_id} {_ABSENT_TEXT}")

    # 裁剪说明（呈现量夹取，静默裁剪不如写在明处）
    version_cap = max(1, int(cfg["max_versions"]))
    domain_cap = max(1, int(cfg["max_domain_items"]))
    for side_id, rec in ((left, left_rec), (right, right_rec)):
        total_versions = len(rec["conclusions"])
        if total_versions > version_cap:
            limits.append(f"档案 #{side_id} 的版本历史仅显示最近 {version_cap} 版"
                          f"（共 {total_versions} 版）")
        total_domains = len(_domains(rec["evidence"]))
        if total_domains > domain_cap:
            limits.append(f"档案 #{side_id} 的信源域名仅显示前 {domain_cap} 个"
                          f"（共 {total_domains} 个）")

    similarity = _similarity(left_rec["text"], right_rec["text"], cfg)
    delta = _conclusion_delta(left_rec["conclusions"], right_rec["conclusions"])
    ev_diff, ev_limits = _evidence_diff(left_rec["evidence"], right_rec["evidence"], cfg)
    src_diff, src_limits = _source_diff(left_rec["evidence"], right_rec["evidence"], cfg)
    limits += ev_limits + src_limits

    data: Dict[str, Any] = {
        "left": left_rec["side"],
        "right": right_rec["side"],
        "similarity": similarity,
        "conclusion_delta": delta,
        "evidence_diff": ev_diff,
        "source_diff": src_diff,
        "limits": limits,
        "gaps": gaps,
    }
    status = "partial" if (gaps or left_rec["fatal"] or right_rec["fatal"]) else "ok"
    return {"status": status, "missing_id": None, "data": data}
