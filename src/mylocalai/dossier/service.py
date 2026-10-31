# -*- coding: utf-8 -*-
"""档案库服务编排（M2）：自动建档、结论/证据落库、复检版本化。

职责（工作指导 §6 M2-7 / M3-2）：
  - verify/recheck 类工具跑完 agent 全链路后，把结论卡片、证据本、分析运行记录
    写进档案库（结论按版本追加，证据按批次追加）；
  - recheck 时在原档案上追加 v2/v3… 与新批次证据，"版本对比"由调用方取
    conclusions() 自行渲染；
  - 章节懒加载：热度快照 / 观点光谱 / 深读由对应工具触发时写入（本模块只存）；
  - 超时/降级时只写已完成的部分，档案状态如实（degraded 标记），不编造。

依赖方向：dossier → rumor/classic/attention（allowed），上游不知道档案库存在。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from mylocalai.core.card import derive_card
from mylocalai.dossier.store import DossierStore

__all__ = ["DossierService"]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class DossierService:
    """档案域编排门面（store 之上的业务语义层）。"""

    def __init__(self, store: Optional[DossierStore] = None) -> None:
        self.store = store or DossierStore()

    def close(self) -> None:
        self.store.close()

    # ---- 建档 ----

    def start_dossier(self, text: str, title: Optional[str] = None,
                      input_kind: str = "text") -> int:
        title = title or (str(text or "").strip()[:24] or "未命名核查")
        return self.store.create_dossier(title, str(text or ""), input_kind=input_kind)

    # ---- 结论落库（verify / recheck 共用） ----

    def record_analysis(self, dossier_id: int, result: Dict[str, Any],
                        kind: str = "verify", duration_s: float = 0.0,
                        requests: int = 0, tokens: int = 0) -> Dict[str, Any]:
        """把一次 agent 全链路结果写入档案：卡片化结论 + 证据批次 + 运行记录。

        result 是 RumorAgent.analyze() 的返回（label/risk_score/evidence_book/...）。
        返回 {"version", "card", "batch"}。失败不抛：写不进的部分返回 degraded 标记。
        """
        out: Dict[str, Any] = {"version": None, "card": None, "batch": None}
        try:
            book = result.get("evidence_book") or {}
            entries = [e for e in (book.get("entries") or []) if isinstance(e, dict)]
            batch = 1
            prev = self.store.evidence(dossier_id)
            if prev:
                batch = max(int(r.get("batch", 1)) for r in prev) + 1
            self.store.add_evidence(dossier_id, batch, entries)
            out["batch"] = batch

            card = derive_card(result, entries, as_of=_now_iso())
            version = self.store.add_conclusion(
                dossier_id,
                label=str(result.get("label", "")),
                risk_score=result.get("risk_score"),
                confidence=str(card["confidence"]),
                evidence_sufficiency=card["evidence_sufficiency"],
                as_of=card["as_of"],
                recheck_conditions=card["recheck_conditions"],
                raw_verdict={k: result.get(k) for k in
                             ("label", "risk_score", "signals", "speech_tier", "confidence")
                             if k in result},
            )
            card["label"] = str(result.get("label", ""))
            out["version"] = version
            out["card"] = card

            degraded = bool(result.get("degraded_components") or result.get("agent_error"))
            self.store.add_run(dossier_id, kind=kind, status="ok" if not degraded else "degraded",
                               degraded=degraded, duration_s=duration_s,
                               requests=requests, tokens=tokens)
            self.store.touch_dossier(dossier_id)
        except Exception as exc:  # noqa: BLE001  档案写失败不影响主结论
            out["degraded"] = True
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    def record_speech_analysis(self, dossier_id: int, result: Dict[str, Any],
                               duration_s: float = 0.0) -> Dict[str, Any]:
        """classic（言论土壤）结果建档：四档结论为标签，无风险分，无证据本。"""
        out: Dict[str, Any] = {"version": None, "card": None}
        try:
            conclusion = str(result.get("conclusion", ""))
            card = derive_card({"label": conclusion, "risk_score": None}, [], as_of=_now_iso())
            version = self.store.add_conclusion(
                dossier_id, label=conclusion, risk_score=None,
                confidence=card["confidence"],
                evidence_sufficiency=card["evidence_sufficiency"],
                as_of=card["as_of"], recheck_conditions=card["recheck_conditions"],
                raw_verdict={k: result.get(k) for k in
                             ("emotion", "attack", "logic", "conclusion") if k in result})
            out["version"] = version
            out["card"] = card
            degraded = bool(result.get("degraded_components"))
            self.store.add_run(dossier_id, kind="speech",
                               status="ok" if not degraded else "degraded",
                               degraded=degraded, duration_s=duration_s)
            self.store.touch_dossier(dossier_id)
        except Exception as exc:  # noqa: BLE001
            out["degraded"] = True
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    # ---- 章节落库 ----

    def record_heat(self, dossier_id: int, detail: Dict[str, Any],
                    profile: str = "") -> int:
        return self.store.add_snapshot(
            dossier_id, float(detail.get("x", 0)), float(detail.get("y", 0)),
            float(detail.get("z", 0)), float(detail.get("score", 0)),
            profile=profile or str(detail.get("profile", "")),
            proxy_flags=list(detail.get("proxy_flags") or []))

    def record_opinions(self, dossier_id: int, payload: Dict[str, Any]) -> int:
        book = payload.get("evidence_book") or {}
        prev = self.store.evidence(dossier_id)
        batch = (max(int(r.get("batch", 1)) for r in prev) if prev else 1)
        return self.store.add_opinions(
            dossier_id, batch, payload.get("distribution") or {},
            summary=str(payload.get("summary", "")), degraded=bool(payload.get("degraded")))

    def record_deep_read(self, dossier_id: int, payload: Dict[str, Any]) -> int:
        return self.store.add_deep_read(
            dossier_id, payload, degraded=bool(payload.get("degraded")),
            guard_dropped=list(payload.get("guard_dropped") or []))

    # ---- 版本对比（M3-2 的数据面） ----

    def compare_versions(self, dossier_id: int) -> Dict[str, Any]:
        """v1→vN 的标签/分数变化 + 证据批次增减（纯读取，供 get_dossier 渲染）。"""
        cs = self.store.conclusions(dossier_id)
        ev = self.store.evidence(dossier_id)
        by_batch: Dict[int, int] = {}
        for e in ev:
            by_batch[int(e.get("batch", 1))] = by_batch.get(int(e.get("batch", 1)), 0) + 1
        return {
            "versions": [{"version": c["version"], "label": c["label"],
                          "risk_score": c["risk_score"], "as_of": c["as_of"]} for c in cs],
            "evidence_batches": by_batch,
            "changed": len(cs) > 1 and cs[0]["label"] != cs[-1]["label"],
        }
