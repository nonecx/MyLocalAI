# -*- coding: utf-8 -*-
"""平台只读工具（P7）：platform_feed —— 在对话里看监测平台的预警/信息流/巡检记录/巡检报告。

与 insights 同族（§5.2 L4 参谋的同一模式）：
  - 只读：permission="read"、cost="zero"、est_s=0.0，不建档（dossier_id 恒 None）；
  - 零 LLM：全部读 platform.store 三表 + scan_reports，本模块不碰任何模型调用；
  - kind 封闭枚举（warnings/feed/scans/report），越界一律 ValidationError（绝不猜）；
  - 空库是合法输出（"暂无预警"照常出卡），库不可用才 ok=False + degraded=True；
  - 呈现口径：传播风险分级只描述传播风险、不判定真假；热度是代理/估计值；
    巡检报告是代码汇总 + LLM 语言整理（platform.report），同样不做真假判断。

依赖方向：assistant → platform（test_import_rules 允许：助手消费平台数据）。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from mylocalai.assistant.registry import ToolRegistry, ValidationError

__all__ = ["KINDS", "kind_param", "register_all", "get_store", "set_store"]

#: 封闭枚举：四种只读视图。
KINDS = ("warnings", "feed", "scans", "report")

#: 信息流/预警的展示条数上限（卡片不背全量数据）。
_FEED_LIMIT = 20
_SCANS_LIMIT = 10

#: 风险档位中文名（与 platform/risk.py 的 LEVEL_LABELS 同口径；展示层唯一来源）。
_LEVEL_LABELS = {"notice": "关注", "suspect": "疑似", "high": "高危", "urgent": "紧急"}

#: 报告 LLM 组织状态 → 中文口径（与 platform/store.py REPORT_LLM_STATUSES、
#: web/static/platform.js LLM_STATUS_TEXT 同封闭集；越界值原样透传不猜）。
_LLM_STATUS_TEXT = {
    "ok": "已生成", "disabled": "已关闭", "unavailable": "模型不可用",
    "budget_exhausted": "预算耗尽", "cancelled": "已取消",
    "skipped_empty": "本轮无新发现", "error": "生成异常",
}

#: 报告卡的口径行（铁律：不判真假、E 是代理估计值、报告只做语言组织）。
_REPORT_NOTE = ("传播风险分级不判定真假；E 为代理估计值；巡检报告不做真假判断")

_store = None
_lock = threading.Lock()


def kind_param(value: Any) -> str:
    """platform_feed 的 kind 参数：封闭枚举校验（None/非字符串/越界都抛）。"""
    if value is None:
        raise ValidationError("缺少参数 kind")
    if not isinstance(value, str):
        raise ValidationError("参数 kind 必须是字符串")
    kind = value.strip().lower()
    if kind not in KINDS:
        raise ValidationError("参数 kind 必须是 " + "|".join(KINDS))
    return kind


def get_store():
    """进程级平台 store 单例（惰性创建；测试经 set_store 注入）。"""
    global _store
    with _lock:
        if _store is None:
            from mylocalai.platform.store import PlatformStore
            _store = PlatformStore()
        return _store


def set_store(store) -> None:
    global _store
    with _lock:
        _store = store


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _entry_view(entry: Dict[str, Any]) -> Dict[str, Any]:
    """信息流条目 → 卡片视图（档位中文名 + 风险理由 +  verdict 摘要 + 领域）。

    topic_domain/topic_domain_source 是平台 v7 的领域列（feed_entries.domain 是
    主机域，勿混）；对话卡按 topic_domain 有值才渲染「领域」，无值不加。
    """
    level = str(entry.get("risk_level") or "notice")
    verdict = str(entry.get("verdict_label") or "")
    score = entry.get("risk_score")
    return {
        "id": entry.get("id"),
        "title": str(entry.get("title") or "")[:80],
        "url": str(entry.get("url") or ""),
        "domain": str(entry.get("domain") or ""),
        "risk_level": level,
        "risk_label": _LEVEL_LABELS.get(level, level),
        "risk_reasons": entry.get("risk_reasons") or [],
        "e_score": entry.get("e_score"),
        "verdict_label": verdict,
        "risk_score": score,
        "status": str(entry.get("status") or ""),
        "dossier_id": entry.get("dossier_id"),
        "discovered_at": str(entry.get("discovered_at") or ""),
        "topic_domain": str(entry.get("topic_domain") or ""),
        "topic_domain_source": str(entry.get("topic_domain_source") or ""),
    }


def _as_list(value: Any) -> List[Any]:
    """数组字段兜底：非列表 → 空（坏形状不炸对话，按"没有"处理）。"""
    return list(value) if isinstance(value, list) else []


def _report_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """报告行（store.latest_report 项）→ 卡片视图。

    payload 是 platform.report.PatrolReport 的产物（代码汇总 + LLM 语言整理）；
    本工具只转述、不改写、不补造。缺键一律兜底（坏形状不炸对话），llm_status
    越界原样透传（前端按封闭集映射，越界如实显示原值，不猜正常状态）。
    as_of 用行的 created_at（报告落库/首建时刻），与其余视图同口径。
    """
    payload = row.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    counts = payload.get("counts")
    return {
        "scan_id": row.get("scan_id"),
        "title": str(row.get("title") or ""),
        "summary": str(payload.get("summary") or ""),
        "llm_status": str(row.get("llm_status") or ""),
        "status": str(row.get("status") or ""),
        "as_of": str(row.get("created_at") or ""),
        "counts": counts if isinstance(counts, dict) else {},
        "topic_notes": _as_list(payload.get("topic_notes")),
        "analysis": _as_list(payload.get("analysis")),
        "risks": str(payload.get("risks") or ""),
        "followups": _as_list(payload.get("followups")),
        "gaps": _as_list(payload.get("gaps")),
    }


def _card(kind: str, as_of: str, data: Any) -> Dict[str, Any]:
    """卡片形状：{card_type:"platform", kind, as_of, warnings|feed|scans}。

    card_type 是前端分发键（loop._CARD_TYPE 同值，cards.js renderPlatform 消费）。
    """
    return {"card_type": "platform", "kind": kind, "as_of": as_of, kind: data}


def _degraded(reason: str) -> Dict[str, Any]:
    """降级出口：ok=False + degraded=True，digest 如实说明（不编空卡冒充成功）。"""
    return {"ok": False, "card": None,
            "digest": f"监测平台数据暂时不可用：{reason}。"
                      "这不是空结果，只是本次没能读到平台库；请稍后重试。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None,
            "degraded": True, "not_saved": False}


def _platform_feed(ctx: Any, kind: str) -> Dict[str, Any]:
    """platform_feed：按 kind 产出预警/信息流/巡检记录/巡检报告（零 LLM、零落库、永不抛）。"""
    as_of = _now_iso()
    try:
        store = get_store()
        if kind == "warnings":
            rows = store.warnings(limit=_FEED_LIMIT)
            data = [_entry_view(r) for r in rows]
            digest = (f"[监测平台·预警] {len(data)} 条信息存在大规模传播风险"
                      f"（高危/紧急档；传播风险分级，不判定真假）"
                      if data else "[监测平台·预警] 暂无预警（没有高危/紧急档信息）")
        elif kind == "feed":
            rows = store.list_feed_entries(limit=_FEED_LIMIT)
            data = [_entry_view(r) for r in rows]
            digest = (f"[监测平台·信息流] 最近 {len(data)} 条发现"
                      if data else "[监测平台·信息流] 暂无信息（还没有巡检记录）")
        elif kind == "report":
            row = store.latest_report()
            if row is None:
                # 空库是合法输出：空卡 + 如实摘要（不是降级——报告确实还没生成过）
                data = []
                digest = ("[监测平台·巡检报告] 暂无巡检报告"
                          "（还没有生成过巡检简报数据）｜" + _REPORT_NOTE)
            else:
                view = _report_view(row)
                data = view
                llm_text = _LLM_STATUS_TEXT.get(view["llm_status"],
                                                view["llm_status"] or "状态未知")
                counts = view["counts"]
                digest = (f"[监测平台·巡检报告] {view['title']}"
                          f"（LLM 整理：{llm_text}；话题 {counts.get('queries', 0)}"
                          f" · 候选 {counts.get('candidates', 0)}"
                          f" · 深查 {counts.get('verified', 0)}）｜{_REPORT_NOTE}")
        else:   # kind_param 已限定封闭枚举，走到这里只可能是 scans
            rows = store.list_scan_runs(limit=_SCANS_LIMIT)
            data = [{"id": r.get("id"), "status": str(r.get("status") or ""),
                     "started_at": str(r.get("started_at") or ""),
                     "finished_at": str(r.get("finished_at") or ""),
                     "candidates": r.get("candidates", 0),
                     "verified": r.get("verified", 0),
                     "degraded": r.get("degraded", 0),
                     "error": str(r.get("error") or "")} for r in rows]
            digest = (f"[监测平台·巡检] 最近 {len(data)} 次巡检记录"
                      if data else "[监测平台·巡检] 暂无巡检记录")
    except Exception as exc:  # noqa: BLE001  平台是增益面：任何故障降级，绝不挡对话
        return _degraded(f"{type(exc).__name__}: {exc}")
    return {"ok": True, "card": _card(kind, as_of, data), "digest": digest,
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None,
            "degraded": False, "not_saved": False, "kind": kind}


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册 platform_feed 工具（services 形参约定同 tools_verify.register_all）。

    只读零 LLM：结构化 action 通道放行，成本徽章与 tool_start 预估如实
    （本地 SQLite 读，毫秒级）。
    """
    registry.register("platform_feed", _platform_feed, params=["kind"],
                      permission="read", cost="zero", est_s=0.0,
                      description="看监测平台的预警/信息流/巡检记录/巡检报告"
                                  "（warnings=大规模传播风险预警 / feed=信息流 /"
                                  " scans=巡检历史 / report=最近一份巡检报告；"
                                  "零 LLM 只读）",
                      validators={"kind": kind_param})
