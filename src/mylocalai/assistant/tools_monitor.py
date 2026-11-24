# -*- coding: utf-8 -*-
"""领域监测编排（专业用户 B · 阶段 3）：订阅到期跑检 + 变化摘要。

定位：与 batch 同哲学——不是对话工具，是"订阅 → 调度线程/立即运行 → 落库 +
面板呈现"的独立通道。web 层（monitor_api 的路由与调度线程）持有，loop 不感知。

编排契约（run_subscription）：
  1. 授权闸（fail-closed）：外部检索未授权 → 本轮记 blocked、订阅暂停并写明原因，
     **零外发**（与隐私模块既有口径一致）；
  2. 检索：search_all(topic+keywords, k=8, domains=订阅域名)——site: 变体由
     检索层构造（≤3 个），不支持时按实到结果呈现；domain_scope_applied 与
     scope_degraded（变体全空兜底）两个标注都如实写进 run.summary，不谎报"已限定"；
  3. 落库：订阅专属监测档案（input_kind='topic'）证据按批次追加；关注度快照用
     **本次检索结果**现算（estimate_from_search，零额外联网，best-effort）；
  4. 变化摘要（纯函数）：与上一轮的 URL 差集 + E 分首末差，只述事实不外推；
  5. 任何一步失败都记 error 并继续收尾，绝不抛；订阅的 next_run_at 照常推进
     （失败不该让订阅卡死）。

依赖方向：assistant → dossier/retrieval/attention（allowed）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

__all__ = ["change_summary", "change_card", "run_subscription", "now_iso"]

#: 每轮检索条数（与 attention 估参的口径一致）
SEARCH_K = 8
#: query 里最多带几个关键词（其余忽略，避免 query 过长被搜索引擎截断）
MAX_QUERY_KEYWORDS = 3


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def change_summary(prev_urls: Optional[List[str]], new_urls: Optional[List[str]],
                   prev_e: Optional[float] = None,
                   new_e: Optional[float] = None) -> Dict[str, Any]:
    """URL 差集 + E 分变化（纯函数、永不抛）。

    只描述事实：新增/消失的 URL 集合与 E 分首末两点之差；不预测、不外推
    （与 attention/trend.py 的"方向只述首末差"同一口径）。
    """
    prev = {str(u).strip().rstrip("/").lower() for u in (prev_urls or []) if str(u).strip()}
    new = {str(u).strip().rstrip("/").lower() for u in (new_urls or []) if str(u).strip()}

    def _num(value: Any) -> Optional[float]:
        try:
            if value is None or isinstance(value, bool):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    prev_score, new_score = _num(prev_e), _num(new_e)
    delta = (round(new_score - prev_score, 2)
             if prev_score is not None and new_score is not None else None)
    return {"new_urls": sorted(new - prev), "removed_urls": sorted(prev - new),
            "e_score": new_score, "prev_e_score": prev_score, "e_delta": delta}


def _entry_rows(results: List[Any]) -> List[Dict[str, Any]]:
    """检索结果 → 证据行（与 evidence 收集同一字段口径）。"""
    rows: List[Dict[str, Any]] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        url = str(r.get("url") or "").strip()
        if not url:
            continue
        rows.append({
            "url": url,
            "domain": str(r.get("domain") or ""),
            "domain_tier": str(r.get("domain_tier") or ""),
            "source_kind": str(r.get("source_kind") or ""),
            "stance": str(r.get("stance") or ""),
            "title": str(r.get("title") or "")[:120],
            "snippet": str(r.get("snippet") or "")[:500],
            "fetched_at": now_iso(),
        })
    return rows


def _attention_snapshot(query: str, results: List[Any]) -> Dict[str, Any]:
    """用本次检索结果现算关注度快照（零额外联网，best-effort；算不出返 {}）。"""
    try:
        from mylocalai.attention import attention as attn
        from mylocalai.attention.estimate import estimate_from_search
        est = estimate_from_search(query, results)
        params, _warnings = attn.load_profile(None)
        detail = attn.score_detail(float(est.x), float(est.y), float(est.z), params)
        return {"x": float(est.x), "y": float(est.y), "z": float(est.z),
                "score": float(detail.get("score", 0.0)),
                "band": str(detail.get("band") or ""),
                "profile": str(detail.get("profile") or attn.DEFAULT_PROFILE),
                "proxy_flags": list(detail.get("proxy_flags") or []),
                "quality": str(est.to_dict().get("quality") or "")}
    except Exception:  # noqa: BLE001  快照是增益面：算不出不挡监测
        return {}


def _query_of(sub: Dict[str, Any]) -> str:
    topic = str(sub.get("topic") or "").strip()
    keywords = [str(k).strip() for k in (sub.get("keywords") or []) if str(k).strip()]
    return " ".join([topic] + keywords[:MAX_QUERY_KEYWORDS]).strip()


def _advance(store: Any, sub_id: int, sub: Dict[str, Any], status: str) -> None:
    """推进 last_run_at / next_run_at 与终态标注（失败也不卡死订阅；best-effort）。

    不隐式恢复启用（R8）：enabled=0（授权闸暂停等）的订阅保持暂停与暂停原因，
    由用户显式"恢复"操作解，避免"跑一次→恢复→下一轮又暂停"的抖动；
    enabled=1 时照旧清空 paused_reason。
    """
    try:
        frequency = int(sub.get("frequency_s") or 3600)
        next_at = (datetime.now().astimezone() + timedelta(seconds=max(300, frequency)))
        fields: Dict[str, Any] = dict(
            last_run_at=now_iso(),
            next_run_at=next_at.isoformat(timespec="seconds"),
            last_status=status)
        if sub.get("enabled"):
            fields["enabled"] = 1
            fields["paused_reason"] = ""
        # enabled=0：不写 enabled/paused_reason，保持暂停现场
        store.update_subscription(int(sub_id), **fields)
    except Exception:  # noqa: BLE001  推进失败不挡收尾
        pass


def change_card(sub: Dict[str, Any], run: Dict[str, Any]) -> Dict[str, Any]:
    """一次运行 → monitor_change 卡片载荷（前端面板渲染；字段口径见 batch 卡）。"""
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    return {
        "card_type": "monitor_change",
        "subscription_id": int(sub.get("id", 0)),
        "topic": str(sub.get("topic") or ""),
        "run_id": int(run.get("id", 0)),
        "status": str(run.get("status") or ""),
        "results_count": int(run.get("results_count") or 0),
        "new_count": int(run.get("new_count") or 0),
        "new_urls": list(summary.get("new_urls") or []),
        "removed_urls": list(summary.get("removed_urls") or []),
        "e_score": summary.get("e_score"),
        "prev_e_score": summary.get("prev_e_score"),
        "e_delta": summary.get("e_delta"),
        "as_of": str(run.get("finished_at") or ""),
        "error": str(run.get("error") or ""),
    }


def run_subscription(store: Any, sub: Dict[str, Any], dossiers: Any,
                     events: Any = None,
                     cancel_check: Optional[Callable[[], bool]] = None,
                     run_id: Optional[int] = None) -> Dict[str, Any]:
    """跑一轮监测：授权闸 → 检索 → 落库 → 变化摘要 → 运行记录。永不抛。

    run_id（可选）：调用方预建的 subscription_runs 行 id——run-now 需要响应体
    立即带 run_id，先建行再交给本函数执行；缺省自建一行（调度线程路径）。
    """
    if run_id is None:
        run_id = store.start_run(int(sub["id"]))
    else:
        run_id = int(run_id)
    try:
        store.mark_running(int(sub["id"]))   # R2：起跑即置 running（调度路径同口径）
    except Exception:  # noqa: BLE001  标注失败不挡运行；终态仍由收尾写
        pass
    out: Dict[str, Any] = {"run_id": run_id, "status": "error", "results_count": 0,
                           "new_count": 0, "summary": {}}

    def emit(name: str, **payload: Any) -> None:
        if events is None:
            return
        try:
            events.emit(name, **payload)
        except Exception:  # noqa: BLE001  事件失败不挡监测
            pass

    def cancelled() -> bool:
        try:
            return bool(cancel_check()) if cancel_check is not None else False
        except Exception:  # noqa: BLE001
            return False

    # ① 授权闸（fail-closed）：未授权零外发
    try:
        from mylocalai.core.privacy import load_preferences
        prefs = load_preferences()
    except Exception:  # noqa: BLE001  偏好读失败按未授权处理（保守）
        prefs = None
    if prefs is None or not prefs.external_allowed("public-search"):
        reason = "外部检索未授权（设置 → 隐私与授权 → 外部处理），订阅已暂停"
        store.finish_run(run_id, "blocked", error=reason)
        store.update_subscription(int(sub["id"]), last_status="blocked",
                                  paused_reason=reason, enabled=0)
        out["status"] = "blocked"
        out["summary"] = {"reason": reason}
        emit("error", code="blocked", recoverable=True, message=reason)
        return out

    query = _query_of(sub)
    if not query:
        store.finish_run(run_id, "error", error="订阅主题为空")
        store.update_subscription(int(sub["id"]), last_status="error",
                                  paused_reason="订阅主题为空")
        out["status"] = "error"
        return out

    # ② 检索（site: 变体由检索层构造；失败按 error 收尾但推进 next_run_at）
    try:
        from mylocalai.retrieval.search import search_all
        payload = search_all(query, k=SEARCH_K,
                             domains=list(sub.get("domains") or []))
    except Exception as exc:  # noqa: BLE001  检索炸了记 error，不抛
        error = f"{type(exc).__name__}: {exc}"
        store.finish_run(run_id, "error", error=error)
        _advance(store, int(sub["id"]), sub, "error")
        out["status"] = "error"
        out["summary"] = {"reason": error}
        return out
    results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
    if payload.get("blocked"):
        reason = "检索层未授权（external_processing_not_authorized）"
        store.finish_run(run_id, "blocked", error=reason)
        store.update_subscription(int(sub["id"]), last_status="blocked",
                                  paused_reason=reason, enabled=0)
        out["status"] = "blocked"
        return out

    # ③ 落库：监测档案 + 证据批次 + 关注度快照（best-effort）
    profile_id = store.subscription_profile_id(int(sub["id"]))
    dossier_id = sub.get("dossier_id")
    if dossier_id is None:
        try:
            dossier_id = dossiers.start_dossier(query, title=str(sub.get("topic") or "")[:24],
                                                input_kind="topic",
                                                claim_excerpt=str(sub.get("topic") or "")[:120],
                                                profile_id=profile_id)
        except Exception:  # noqa: BLE001  建档失败不挡检索结果呈现
            dossier_id = None
        if dossier_id is not None:
            try:
                store.update_subscription(int(sub["id"]), dossier_id=int(dossier_id))
            except Exception:  # noqa: BLE001
                pass
    rows = _entry_rows(results)
    batch = 1
    if dossier_id is not None and rows:
        try:
            prev = dossiers.store.evidence(int(dossier_id))
            batch = (max(int(r.get("batch", 1)) for r in prev) + 1) if prev else 1
            dossiers.store.add_evidence(int(dossier_id), batch, rows)
        except Exception:  # noqa: BLE001  证据落库失败不挡运行记录
            pass
    snapshot = _attention_snapshot(query, results)
    if dossier_id is not None and snapshot:
        try:
            dossiers.record_heat(int(dossier_id), snapshot)
        except Exception:  # noqa: BLE001  快照是增益面（含保存开关未开的 PermissionError）
            pass

    # ④ 变化摘要（与上一轮比）
    prev_runs = store.runs(int(sub["id"]), limit=2)
    prev_run = next((r for r in prev_runs if int(r.get("id", 0)) != run_id), None)
    prev_summary = (prev_run or {}).get("summary") if isinstance(prev_run, dict) else {}
    prev_urls = list((prev_summary or {}).get("urls") or [])
    prev_e = (prev_summary or {}).get("e_score") if isinstance(prev_summary, dict) else None
    urls = [str(r.get("url") or "") for r in rows]
    summary = change_summary(prev_urls, urls, prev_e=prev_e,
                             new_e=snapshot.get("score") if snapshot else None)
    summary["urls"] = urls
    summary["batch"] = batch
    summary["dossier_id"] = int(dossier_id) if dossier_id is not None else None
    summary["domain_scope_applied"] = bool(payload.get("domain_scope_applied"))
    # R9：site: 变体全空兜底时检索层标 scope_degraded=True，两个标注都进
    # run.summary，如实呈现"这一轮没限定住"，不谎报已限定。
    summary["scope_degraded"] = bool(payload.get("scope_degraded"))
    summary["quality"] = str(snapshot.get("quality") or "") if snapshot else ""

    new_count = len(summary["new_urls"])
    status = "ok" if not cancelled() else "partial"
    store.finish_run(run_id, status, results_count=len(rows), new_count=new_count,
                     summary=summary)
    # next_run_at 照常推进（失败也不卡死订阅）；恢复启用与清暂停原因
    _advance(store, int(sub["id"]), sub, status)
    out.update({"status": status, "results_count": len(rows), "new_count": new_count,
                "summary": summary})
    card = change_card({**sub, "id": int(sub["id"])},
                       {"id": run_id, "status": status, "results_count": len(rows),
                        "new_count": new_count, "summary": summary,
                        "finished_at": now_iso()})
    emit("card", **card)
    emit("narrative", delta=_narrative(card))
    emit("done", run_id=run_id)
    return out


def _narrative(card: Dict[str, Any]) -> str:
    """一轮监测的一句话收尾（模板拼接，零 LLM）。"""
    parts = [f"「{card.get('topic') or ''}」本轮检到 {card.get('results_count', 0)} 条，"
             f"新增 {card.get('new_count', 0)} 条"]
    if card.get("e_score") is not None:
        parts.append(f"关注度 E={card['e_score']}")
    if card.get("new_urls"):
        parts.append("最新一条：" + str(card["new_urls"][0])[:80])
    return "；".join(parts) + "。只述检索结果，不判定真假。"
