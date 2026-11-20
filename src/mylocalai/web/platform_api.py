# -*- coding: utf-8 -*-
"""平台 REST 接口（web/platform_api.py）：状态 / 信息流 / 预警 / 监控词 / 巡检。

与 privacy_api/models_api 同一 router 模式（前缀 /api/platform，中文 400 detail）。
调度器与本模块各持一个 PlatformStore（同库不同连接，WAL 单写者由 SQLite 串行化，
既有 dossier/assistant 双 store 先例）。

隐私口径：本模块只读偏好状态（external_authorized）供前端出引导态；
授权真正的执行门在 runner/sources（未授权时巡检 skipped、不外发）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mylocalai.core.privacy import load_preferences
from mylocalai.platform.sources import SEARCH_PROVIDER_ID
from mylocalai.platform.store import PlatformStore

router = APIRouter(prefix="/api/platform", tags=["platform"])

logger = logging.getLogger("my_website")

#: 监控词长度上限（与 registry.MAX_TOPIC_LENGTH 同口径）。
_MAX_QUERY_LEN = 200

_store: Optional[PlatformStore] = None
_scheduler = None
_lock = threading.Lock()


def get_store() -> PlatformStore:
    """进程级平台 store 单例（惰性创建；测试可经 set_store 注入）。"""
    global _store
    with _lock:
        if _store is None:
            _store = PlatformStore()
        return _store


def set_store(store: Optional[PlatformStore]) -> None:
    global _store
    with _lock:
        _store = store


def get_scheduler():
    """进程级调度器单例（web 启动时 start；手动巡检走 trigger_async）。"""
    global _scheduler
    with _lock:
        if _scheduler is None:
            from mylocalai.platform.scheduler import PlatformScheduler
            _scheduler = PlatformScheduler()
        return _scheduler


def set_scheduler(scheduler) -> None:
    """测试/装配注入点（与 set_store 对称）。"""
    global _scheduler
    with _lock:
        _scheduler = scheduler


def start_scheduler_if_enabled() -> bool:
    """web 启动钩子：规则 enabled=false 时**不构造**调度器（零副作用：
    不建 store、不开线程），直接返回 False。任何失败只记日志不挡服务。"""
    try:
        from mylocalai.platform.rules import load_rules
        rules, warnings = load_rules()
        if not rules.get("enabled"):
            return False
        for warning in warnings:
            logger.warning("平台规则告警：%s", warning)
        return bool(get_scheduler().start())
    except Exception as exc:  # noqa: BLE001  调度器起不来不影响网站主功能
        logger.warning("平台巡检调度器启动失败：%s: %s", type(exc).__name__, exc)
        return False


def _external_authorized() -> bool:
    try:
        return bool(load_preferences().external_allowed(SEARCH_PROVIDER_ID))
    except Exception:  # noqa: BLE001  偏好读取失败按未授权（fail-closed）
        return False


class WatchIn(BaseModel):
    """POST /api/platform/watch 的请求体。"""

    query: str = ""
    note: str = ""


@router.get("/status")
def platform_status() -> Dict[str, Any]:
    """调度器状态 + 最近巡检摘要 + 外部授权态（前端据此出引导卡）。"""
    status = get_scheduler().status()
    status["external_authorized"] = _external_authorized()
    return status


@router.get("/feed")
def platform_feed(risk: str = "", limit: int = 50) -> Dict[str, Any]:
    """信息流（created_at 倒序）。risk 可传单档或逗号分隔多档过滤。"""
    entries = get_store().list_feed_entries(risk=risk or None, limit=limit)
    return {"entries": entries, "external_authorized": _external_authorized()}


@router.get("/warnings")
def platform_warnings(limit: int = 50) -> Dict[str, Any]:
    """预警列表：risk_level ∈ {high, urgent}（大规模传播风险档）。"""
    return {"entries": get_store().warnings(limit=limit),
            "external_authorized": _external_authorized()}


@router.get("/watch")
def platform_watch_list() -> Dict[str, Any]:
    return {"queries": get_store().list_watch_queries()}


@router.post("/watch", status_code=201)
def platform_watch_add(body: WatchIn) -> Dict[str, Any]:
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="监控词不能为空")
    if len(query) > _MAX_QUERY_LEN:
        raise HTTPException(status_code=400, detail=f"监控词过长（最多 {_MAX_QUERY_LEN} 字）")
    qid = get_store().add_watch_query(query, note=body.note or "")
    if qid is None:
        raise HTTPException(status_code=409, detail="监控词已存在")
    return {"id": qid, "query": query}


@router.delete("/watch/{query_id}")
def platform_watch_delete(query_id: int) -> Dict[str, Any]:
    if query_id <= 0:
        raise HTTPException(status_code=400, detail="id 必须是正整数")
    deleted = get_store().delete_watch_query(query_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"监控词不存在: {query_id}")
    return {"deleted": True}


@router.post("/scan")
def platform_scan_trigger() -> Dict[str, Any]:
    """触发一次巡检（后台线程执行）。已有巡检在跑 → started=false（单飞）。"""
    started = get_scheduler().trigger_async()
    return {"started": started}


@router.get("/scans")
def platform_scans(limit: int = 20) -> Dict[str, Any]:
    return {"scans": get_store().list_scan_runs(limit=limit)}


@router.get("/scans/{scan_id}")
def platform_scan_detail(scan_id: int) -> Dict[str, Any]:
    run = get_store().get_scan_run(scan_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"巡检记录不存在: {scan_id}")
    return {"scan": run}
