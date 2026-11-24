# -*- coding: utf-8 -*-
"""材料批次汇总纯函数（专业用户 A · 阶段 2）：汇总 + 卡片派生。

三条铁律在本模块的落法：
  - 零 LLM：只做计数与排序，不做任何真假断言；
  - 不擅自合并判定：同源组内**每条材料各自判定**，组只影响"汇总层怎么呈现"
    （代表文本 + 组内标签分布），绝不用一条的结论覆盖另一条；
  - 永不抛：坏输入按空数据降级，调用方（编排/API）拿到合法空结构。

规格 §5 汇总三要素（stance_matrix 立场矩阵 / evidence_overlap_top 证据重合
Top / sources_top 信源战绩）也在这里：全部零 LLM 纯函数，原料是调用方传入的
{dossier_id: [证据行]}（批次条目档案的库内证据，经 BatchStore 只读取）；
取不到立场/信源的数据行如实省略、三项可为空结构，不编造。

同源分组（group_similar）不在这里：它用 assistant.fingerprint 的归一化/
shingle/Jaccard，而依赖方向禁 dossier → assistant（tests/test_import_rules
锁定，store.py 的 _fp_* 同因此才存在），分组由编排层
（assistant/tools_batch.group_similar）算好后随 group_id 落入库，本模块只消费。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

__all__ = ["summarize", "summary_card", "now_iso"]

#: 三要素的 Top-N 上限（sources_top / evidence_overlap_top 同一档位，≤5）
_TOP_N = 5


def _label_of(item: Dict[str, Any]) -> str:
    return str(item.get("label") or "")


def _int_id(value: Any) -> int:
    """宽松转正整数（档案 id）；坏值按 0（=无档案）。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _url_host(url: str) -> str:
    """从 URL 提取主机域名（用于"URL 重合已隐含域名重合"的去重）；取不到为空。"""
    try:
        from urllib.parse import urlparse
        return (urlparse(url).netloc or "").strip().lower()
    except Exception:  # noqa: BLE001  解析不了就不去重（如实多计一条）
        return ""


def _stance_matrix(rows: List[Dict[str, Any]],
                   evidence_by_dossier: Optional[Dict[int, List[Any]]]
                   ) -> Dict[str, Dict[str, int]]:
    """规格 §5 要素一：结论标签 × 证据立场的计数矩阵。

    口径：只统计 status=ok 且带 label 的条目、其档案证据里 stance 非空的条目；
    证据行缺 stance（取不到立场）如实省略，不编造、不归入"无关"。零 LLM 纯计数。
    """
    matrix: Dict[str, Dict[str, int]] = {}
    if not isinstance(evidence_by_dossier, dict):
        return matrix
    for it in rows:
        if str(it.get("status") or "") != "ok":
            continue
        label = _label_of(it)
        if not label:
            continue
        for ev in (evidence_by_dossier.get(_int_id(it.get("dossier_id"))) or []):
            if not isinstance(ev, dict):
                continue
            stance = str(ev.get("stance") or "").strip()
            if not stance:
                continue
            bucket = matrix.setdefault(label, {})
            bucket[stance] = bucket.get(stance, 0) + 1
    return matrix


def _sources_top(rows: List[Dict[str, Any]],
                 evidence_by_dossier: Optional[Dict[int, List[Any]]]
                 ) -> List[Dict[str, Any]]:
    """规格 §5 要素三：出现最多的信源域名 + 反驳条目计数（cards.js 消费形状）。

    契约（cards.js renderBatchSummary → sources_top 渲染位）：数组，每元素
    {"domain": str, "refute_entries": int}；排序为出现次数降序、同数按域名升序；
    无证据/无域名数据时给空列表（前端判空整块不渲染）。反驳条目只按 stance
    原值计数，不给任何站点判真假。
    """
    if not isinstance(evidence_by_dossier, dict):
        return []
    totals: Dict[str, int] = {}
    refutes: Dict[str, int] = {}
    for it in rows:
        if str(it.get("status") or "") != "ok":
            continue
        for ev in (evidence_by_dossier.get(_int_id(it.get("dossier_id"))) or []):
            if not isinstance(ev, dict):
                continue
            domain = str(ev.get("domain") or "").strip()
            if not domain:
                continue
            totals[domain] = totals.get(domain, 0) + 1
            if str(ev.get("stance") or "").strip() == "反驳":
                refutes[domain] = refutes.get(domain, 0) + 1
    ordered = sorted(totals, key=lambda d: (-totals[d], d))[:_TOP_N]
    return [{"domain": d, "refute_entries": int(refutes.get(d, 0))} for d in ordered]


def _evidence_overlap_top(rows: List[Dict[str, Any]],
                          evidence_by_dossier: Optional[Dict[int, List[Any]]]
                          ) -> List[Dict[str, Any]]:
    """规格 §5 要素二：条目两两间证据 URL/域名重合的 Top-N（N≤5，去重）。

    口径：每条 ok 条目的证据键集 = {非空 url} ∪ {非空 domain}（各自去重）；
    两两交集非空才算重合，shared 里若某条 URL 与其域名同时重合，只记 URL
    （URL 重合已隐含域名重合，不重复计数）。0 重合给空列表——前端判空不渲染。
    形状：[{"a": posA, "b": posB, "shared": [键...], "count": n}]，
    按 count 降序、同数按 (a, b) 升序取前 5。
    """
    if not isinstance(evidence_by_dossier, dict):
        return []
    keyed: List[Dict[str, Any]] = []
    for it in rows:
        if str(it.get("status") or "") != "ok":
            continue
        urls: set = set()
        domains: set = set()
        for ev in (evidence_by_dossier.get(_int_id(it.get("dossier_id"))) or []):
            if not isinstance(ev, dict):
                continue
            url = str(ev.get("url") or "").strip()
            domain = str(ev.get("domain") or "").strip()
            if url:
                urls.add(url)
            if domain:
                domains.add(domain)
        if urls or domains:
            keyed.append({"position": _int_id(it.get("position")),
                          "urls": urls, "domains": domains})

    pairs: List[Dict[str, Any]] = []
    for i in range(len(keyed)):
        for j in range(i + 1, len(keyed)):
            a, b = keyed[i], keyed[j]
            shared_urls = a["urls"] & b["urls"]
            shared_domains = a["domains"] & b["domains"]
            # URL 重合已隐含域名重合：从域名交集里剔除已重合 URL 的主机
            hosts = {_url_host(u) for u in shared_urls} - {""}
            shared_domains -= hosts
            shared = sorted(shared_domains) + sorted(shared_urls)
            count = len(shared)
            if count > 0:
                pairs.append({"a": a["position"], "b": b["position"],
                              "shared": shared, "count": count})
    pairs.sort(key=lambda p: (-int(p["count"]), int(p["a"]), int(p["b"])))
    return pairs[:_TOP_N]


def summarize(items: List[Dict[str, Any]],
              evidence_by_dossier: Optional[Dict[int, List[Any]]] = None
              ) -> Dict[str, Any]:
    """批次条目 → 汇总 dict（零 LLM、永不抛）。

    items 每项至少含 position/status；ok 项带 label/risk_score/dossier_id；
    error/blocked 项带 error；group_id 来自 group_similar。
    evidence_by_dossier（可选）是 {dossier_id: [证据行 dict]}——规格 §5 三要素
    （stance_matrix / evidence_overlap_top / sources_top）的原料，由调用方经
    BatchStore 只读取；不给就三项空结构，绝不编造。
    """
    rows = [it for it in (items or []) if isinstance(it, dict)]
    by_status: Dict[str, int] = {}
    by_label: Dict[str, int] = {}
    for it in rows:
        status = str(it.get("status") or "")
        by_status[status] = by_status.get(status, 0) + 1
        if status == "ok" and _label_of(it):
            by_label[_label_of(it)] = by_label.get(_label_of(it), 0) + 1

    groups: List[Dict[str, Any]] = []
    by_group: Dict[int, List[Dict[str, Any]]] = {}
    for it in rows:
        gid = it.get("group_id") or 0
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            gid = 0
        if gid > 0:
            by_group.setdefault(gid, []).append(it)
    for gid in sorted(by_group):
        members = sorted(by_group[gid], key=lambda m: int(m.get("position", 0)))
        representative = ""
        for m in members:
            candidate = str(m.get("raw_text") or "").strip()
            if candidate:
                representative = candidate
                break
        # R3：组内标签分布按成员累加（同组 3 条同标签就是 ×3）——此前 dict 推导
        # 同键互相覆盖恒显示 ×1，与卡片渲染不符。
        labels: Dict[str, int] = {}
        for m in members:
            lab = _label_of(m)
            if lab:
                labels[lab] = labels.get(lab, 0) + 1
        groups.append({
            "group_id": gid,
            "size": len(members),
            "positions": [int(m.get("position", 0)) for m in members],
            "representative": representative[:120],
            "labels": labels,
        })

    degraded = [f"第{int(it.get('position', 0)) + 1}条：{str(it.get('error') or '')}"
                for it in rows
                if str(it.get("status") or "") in ("error", "blocked")
                and str(it.get("error") or "").strip()]
    # 规格 §5 三要素（零 LLM 纯函数；证据原料缺省时三项如实为空结构）
    stance = _stance_matrix(rows, evidence_by_dossier)
    overlap = _evidence_overlap_top(rows, evidence_by_dossier)
    sources = _sources_top(rows, evidence_by_dossier)
    return {
        "total": len(rows),
        "ok": by_status.get("ok", 0),
        "failed": by_status.get("error", 0),
        "blocked": by_status.get("blocked", 0),
        "not_run": by_status.get("not_run", 0),
        "by_label": by_label,
        "groups": groups,
        "stance_matrix": stance,
        "evidence_overlap_top": overlap,
        "sources_top": sources,
        "degraded": degraded,
        "items": [{
            "position": int(it.get("position", 0)),
            "status": str(it.get("status") or ""),
            "label": _label_of(it),
            "risk_score": it.get("risk_score"),
            "dossier_id": it.get("dossier_id"),
            "url": str(it.get("url") or ""),
            "group_id": (int(it.get("group_id") or 0)),
            "error": str(it.get("error") or ""),
        } for it in rows],
    }


def summary_card(batch: Dict[str, Any], items: List[Dict[str, Any]],
                 as_of: str = "",
                 evidence_by_dossier: Optional[Dict[int, List[Any]]] = None,
                 summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """批次 + 条目 → batch_summary 卡片载荷（cards.js renderBatchSummary 消费）。

    summary（可选）直接给现成汇总（编排/重放路径已算过或已落库的），避免重算；
    没给就现算（可带 evidence_by_dossier 出三要素）。传入的 summary 不被改动。
    """
    if summary is None:
        summary = summarize(items, evidence_by_dossier)
    summary = dict(summary)
    summary.pop("items", None)
    return {
        "card_type": "batch_summary",
        "batch_id": int(batch.get("id", 0)),
        "title": str(batch.get("title") or ""),
        "source": str(batch.get("source") or ""),
        "as_of": str(as_of or ""),
        **summary,
        "items": [{
            "position": int(it.get("position", 0)),
            "status": str(it.get("status") or ""),
            "label": _label_of(it),
            "risk_score": it.get("risk_score"),
            "dossier_id": it.get("dossier_id"),
            "url": str(it.get("url") or ""),
            "group_id": int(it.get("group_id") or 0),
            "error": str(it.get("error") or ""),
        } for it in items if isinstance(it, dict)],
    }


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
