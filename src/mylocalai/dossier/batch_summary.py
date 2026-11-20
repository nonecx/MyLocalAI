# -*- coding: utf-8 -*-
"""材料批次汇总纯函数（专业用户 A · 阶段 2）：汇总 + 卡片派生。

三条铁律在本模块的落法：
  - 零 LLM：只做计数与排序，不做任何真假断言；
  - 不擅自合并判定：同源组内**每条材料各自判定**，组只影响"汇总层怎么呈现"
    （代表文本 + 组内标签分布），绝不用一条的结论覆盖另一条；
  - 永不抛：坏输入按空数据降级，调用方（编排/API）拿到合法空结构。

同源分组（group_similar）不在这里：它用 assistant.fingerprint 的归一化/
shingle/Jaccard，而依赖方向禁 dossier → assistant（tests/test_import_rules
锁定，store.py 的 _fp_* 同因此才存在），分组由编排层
（assistant/tools_batch.group_similar）算好后随 group_id 落入库，本模块只消费。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

__all__ = ["summarize", "summary_card", "now_iso"]


def _label_of(item: Dict[str, Any]) -> str:
    return str(item.get("label") or "")


def summarize(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """批次条目 → 汇总 dict（零 LLM、永不抛）。

    items 每项至少含 position/status；ok 项带 label/risk_score/dossier_id；
    error/blocked 项带 error；group_id 来自 group_similar。
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
        groups.append({
            "group_id": gid,
            "size": len(members),
            "positions": [int(m.get("position", 0)) for m in members],
            "representative": representative[:120],
            "labels": {_label_of(m): 1 for m in members if _label_of(m)},
        })

    degraded = [f"第{int(it.get('position', 0)) + 1}条：{str(it.get('error') or '')}"
                for it in rows
                if str(it.get("status") or "") in ("error", "blocked")
                and str(it.get("error") or "").strip()]
    return {
        "total": len(rows),
        "ok": by_status.get("ok", 0),
        "failed": by_status.get("error", 0),
        "blocked": by_status.get("blocked", 0),
        "not_run": by_status.get("not_run", 0),
        "by_label": by_label,
        "groups": groups,
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
                 as_of: str = "") -> Dict[str, Any]:
    """批次 + 条目 → batch_summary 卡片载荷（cards.js renderBatchSummary 消费）。"""
    summary = summarize(items)
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
