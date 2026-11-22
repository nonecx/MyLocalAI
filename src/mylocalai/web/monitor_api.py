# -*- coding: utf-8 -*-
"""领域监测订阅 API（专业用户 B · 阶段 3）：CRUD + 立即运行 + 调度线程。

端点（本地、无鉴权，与既有 API 一致）：
  GET    /api/subscriptions              列表（当前档案工作区）
  POST   /api/subscriptions              新建 {topic, keywords, domains, frequency_s}
  PUT    /api/subscriptions/{id}         改主题/关键词/域名/频率/启停
  DELETE /api/subscriptions/{id}         删除
  POST   /api/subscriptions/{id}/run-now   立即跑一轮（202 + 后台线程）
  GET    /api/subscriptions/{id}         详情 + 运行历史 + 最新变化卡
  GET    /api/subscriptions/{id}/runs    运行历史

调度：web 应用 lifespan 起一个 daemon 线程（tick 60s，串行，单 worker 假设），
到期的订阅逐轮跑检；多 worker 部署时只有主进程调度（启动时告警，与
app._warn_if_multiworker 同口径）。频率下限 300s（防把搜索引擎打爆）。

角色闸（v6 角色权限层）：写/运行端点（POST/PUT/DELETE 订阅与 run-now）要求
当前激活档案的 role ∈ {monitor, full}——领域监测是"专业用户·领域监测"工作区
的面板能力；其余角色 403（诚实中文 detail）。读端点（GET）不做角色闸，只按
profile_id 归属分家。

分家：订阅归当前激活档案工作区；跨工作区访问 → 404（不泄露存在性）。
授权：每轮运行前查 public-search 外发授权，未过 → blocked + 订阅暂停，零外发。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mylocalai.assistant import tools_monitor
from mylocalai.dossier.subscriptions import (MAX_DOMAINS, MAX_KEYWORDS, MAX_TOPIC,
                                             MIN_FREQUENCY_S, SubscriptionStore)

router = APIRouter()

_store: Optional[SubscriptionStore] = None
_dossiers_factory: Optional[Any] = None       # 测试注入点：返回带 store/DossierService
_running: set = set()                          # 正在跑的 subscription_id（防重入）
_running_lock = threading.Lock()

#: 调度线程 tick 间隔（秒）；先睡再扫，短生命进程（测试）不会触发首轮
TICK_S = 60.0
#: 每 tick 最多跑的订阅数（串行，防一轮拖死整个 tick）
DUE_LIMIT = 3


def get_subscription_store() -> SubscriptionStore:
    """订阅存储单例（测试注入点）。"""
    global _store
    if _store is None:
        _store = SubscriptionStore()
    return _store


def _dossiers() -> Any:
    if _dossiers_factory is not None:
        return _dossiers_factory()
    from mylocalai.dossier.service import DossierService
    return DossierService()


def _active_profile_id() -> Optional[int]:
    try:
        pid = get_subscription_store().active_profile_id()
        return int(pid) if pid else None
    except Exception:  # noqa: BLE001
        return None


#: 领域监测面板的角色闸（语义基线）：写/运行端点仅 monitor（专业用户·领域监测）
#: 与 full。layperson/analyst 403。
_MONITOR_PANEL_ROLES = ("monitor", "full")


def _active_role() -> str:
    """当前激活档案工作区的角色（沿 web 层 profiles store 单例读，同一数据源）。

    读不到（store 不可用/旧库/档案缺失）→ "full" 不裁剪——与
    ProfileStore.role_of 的缺省口径一致（fail-open，与既有降级文化同向）。
    """
    try:
        from mylocalai.web.profiles_api import get_profile_store
        return get_profile_store().role_of(_active_profile_id())
    except Exception:  # noqa: BLE001  角色读不到不裁剪（安全退化）
        return "full"


def _require_panel_role(roles: tuple, message: str) -> None:
    """面板服务端角色闸：当前激活档案的角色不在 roles → 403（诚实中文 detail）。

    只用于写/运行端点；读端点按 profile_id 归属分家，不做角色闸。
    """
    if _active_role() not in roles:
        raise HTTPException(status_code=403, detail=message)


class SubscriptionIn(BaseModel):
    """POST /api/subscriptions 请求体。"""
    topic: str
    keywords: list = []
    domains: list = []
    frequency_s: int = 3600


class SubscriptionUpdate(BaseModel):
    """PUT /api/subscriptions/{id} 请求体（字段均可选）。"""
    topic: Optional[str] = None
    keywords: Optional[list] = None
    domains: Optional[list] = None
    frequency_s: Optional[int] = None
    enabled: Optional[bool] = None


def _validate(topic: str, keywords: list, domains: list, frequency_s: int) -> None:
    topic = str(topic or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="监测主题不能为空")
    if len(topic) > MAX_TOPIC:
        raise HTTPException(status_code=400, detail=f"监测主题过长（最多 {MAX_TOPIC} 字）")
    if len(keywords or []) > MAX_KEYWORDS:
        raise HTTPException(status_code=400,
                            detail=f"关键词过多（最多 {MAX_KEYWORDS} 个）")
    if len(domains or []) > MAX_DOMAINS:
        raise HTTPException(status_code=400,
                            detail=f"域名过多（每轮最多 {MAX_DOMAINS} 个 site: 限定）")
    if int(frequency_s) < MIN_FREQUENCY_S:
        raise HTTPException(status_code=400,
                            detail=f"频率过低（最少 {MIN_FREQUENCY_S} 秒，保护搜索引擎）")


def _sub_or_404(sub_id: int) -> Dict[str, Any]:
    store = get_subscription_store()
    sub = store.get_subscription(int(sub_id))
    if sub is None:
        raise HTTPException(status_code=404, detail=f"订阅不存在: {sub_id}")
    profile_id = _active_profile_id()
    owner = sub.get("profile_id")
    if profile_id is not None and owner is not None and int(owner) != int(profile_id):
        raise HTTPException(status_code=404, detail=f"订阅不存在: {sub_id}")
    return sub


@router.get("/api/subscriptions")
def subscriptions_list() -> Dict[str, Any]:
    """订阅列表（当前档案工作区）。"""
    store = get_subscription_store()
    return {"subscriptions": store.list_subscriptions(profile_id=_active_profile_id()),
            "active_profile_id": _active_profile_id(),
            "min_frequency_s": MIN_FREQUENCY_S, "max_domains": MAX_DOMAINS}


@router.post("/api/subscriptions")
def subscriptions_create(req: SubscriptionIn) -> Dict[str, Any]:
    """新建订阅（角色不符 → 403；频率下限/域名上限 → 400）。"""
    _require_panel_role(
        _MONITOR_PANEL_ROLES,
        "领域监测面板仅对专业用户·领域监测工作区开放（当前工作区角色无此权限），"
        "请切换档案工作区后再试")
    _validate(req.topic, req.keywords, req.domains, req.frequency_s)
    store = get_subscription_store()
    try:
        sub_id = store.create_subscription(req.topic, req.keywords, req.domains,
                                           int(req.frequency_s),
                                           profile_id=_active_profile_id())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"subscription": store.get_subscription(sub_id)}


@router.put("/api/subscriptions/{sub_id}")
def subscriptions_update(sub_id: int, req: SubscriptionUpdate) -> Dict[str, Any]:
    """改主题/关键词/域名/频率/启停（角色不符 → 403；跨工作区 404）。"""
    _require_panel_role(
        _MONITOR_PANEL_ROLES,
        "领域监测面板仅对专业用户·领域监测工作区开放（当前工作区角色无此权限），"
        "请切换档案工作区后再试")
    _sub_or_404(int(sub_id))
    fields: Dict[str, Any] = {}
    if req.topic is not None:
        fields["topic"] = str(req.topic).strip()
    if req.keywords is not None:
        fields["keywords"] = [str(k).strip() for k in req.keywords if str(k).strip()]
    if req.domains is not None:
        fields["domains"] = [str(d).strip().lower() for d in req.domains if str(d).strip()]
    if req.frequency_s is not None:
        fields["frequency_s"] = int(req.frequency_s)
    if req.enabled is not None:
        fields["enabled"] = bool(req.enabled)
    if not fields:
        raise HTTPException(status_code=400, detail="没有要更新的字段")
    if "topic" in fields or "keywords" in fields or "domains" in fields \
            or "frequency_s" in fields:
        current = get_subscription_store().get_subscription(int(sub_id)) or {}
        topic = fields.get("topic", current.get("topic") or "")
        keywords = fields.get("keywords", current.get("keywords") or [])
        domains = fields.get("domains", current.get("domains") or [])
        frequency = fields.get("frequency_s", current.get("frequency_s") or 3600)
        _validate(topic, keywords, domains, frequency)
    try:
        row = get_subscription_store().update_subscription(int(sub_id), **fields)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"subscription": row}


@router.delete("/api/subscriptions/{sub_id}")
def subscriptions_delete(sub_id: int) -> Dict[str, Any]:
    """删除订阅（角色不符 → 403；跨工作区 404）。"""
    _require_panel_role(
        _MONITOR_PANEL_ROLES,
        "领域监测面板仅对专业用户·领域监测工作区开放（当前工作区角色无此权限），"
        "请切换档案工作区后再试")
    _sub_or_404(int(sub_id))
    get_subscription_store().delete_subscription(int(sub_id))
    return {"deleted": True, "id": int(sub_id)}


@router.post("/api/subscriptions/{sub_id}/run-now", status_code=202)
def subscriptions_run_now(sub_id: int) -> Dict[str, Any]:
    """立即跑一轮（角色不符 → 403；202 + 后台线程；已在跑 → 409）。"""
    _require_panel_role(
        _MONITOR_PANEL_ROLES,
        "领域监测面板仅对专业用户·领域监测工作区开放（当前工作区角色无此权限），"
        "请切换档案工作区后再试")
    sub = _sub_or_404(int(sub_id))
    with _running_lock:
        if int(sub_id) in _running:
            raise HTTPException(status_code=409, detail="该订阅正在运行中，请稍候")
        _running.add(int(sub_id))
    threading.Thread(target=_run_worker, args=(int(sub_id), sub),
                     name=f"monitor-{int(sub_id)}", daemon=True).start()
    return {"started": True, "id": int(sub_id)}


def _run_worker(sub_id: int, sub: Dict[str, Any]) -> None:
    try:
        tools_monitor.run_subscription(get_subscription_store(), sub, _dossiers())
    except Exception:  # noqa: BLE001  run_subscription 永不抛，这里是双保险
        try:
            get_subscription_store().finish_run(
                get_subscription_store().start_run(sub_id), "error",
                error="worker_exception")
        except Exception:  # noqa: BLE001
            pass
    finally:
        with _running_lock:
            _running.discard(int(sub_id))


@router.get("/api/subscriptions/{sub_id}")
def subscriptions_detail(sub_id: int) -> Dict[str, Any]:
    """详情 + 运行历史 + 最新一轮的变化卡。"""
    sub = _sub_or_404(int(sub_id))
    store = get_subscription_store()
    runs = store.runs(int(sub_id), limit=20)
    card: Dict[str, Any] = {}
    latest = next((r for r in runs if str(r.get("status")) != "running"), None)
    if latest is not None:
        card = tools_monitor.change_card(sub, latest)
    return {"subscription": sub, "runs": runs, "card": card}


@router.get("/api/subscriptions/{sub_id}/runs")
def subscriptions_runs(sub_id: int) -> Dict[str, Any]:
    """运行历史（最新在前）。"""
    _sub_or_404(int(sub_id))
    return {"runs": get_subscription_store().runs(int(sub_id), limit=50)}


# ---------------------------------------------------------------------------
# 调度线程（web lifespan 驱动）
# ---------------------------------------------------------------------------

_scheduler_stop = threading.Event()


def scheduler_tick(now: Optional[str] = None) -> int:
    """跑一轮到期的订阅；返回实际开跑数（测试可直接调用）。"""
    from datetime import datetime
    stamp = now or datetime.now().astimezone().isoformat(timespec="seconds")
    store = get_subscription_store()
    due = store.due_subscriptions(stamp, limit=DUE_LIMIT)
    started = 0
    for sub in due:
        sub_id = int(sub.get("id", 0))
        with _running_lock:
            if sub_id in _running:
                continue
            _running.add(sub_id)
        started += 1
        try:
            tools_monitor.run_subscription(store, sub, _dossiers())
        except Exception:  # noqa: BLE001  单订阅失败不影响其他订阅与 tick
            pass
        finally:
            with _running_lock:
                _running.discard(sub_id)
    return started


def start_scheduler() -> threading.Thread:
    """起调度 daemon 线程（幂等保护由调用方——lifespan 只起一次）。"""
    _scheduler_stop.clear()

    def _loop() -> None:
        while not _scheduler_stop.wait(TICK_S):
            try:
                scheduler_tick()
            except Exception:  # noqa: BLE001  调度循环永不退出
                pass

    thread = threading.Thread(target=_loop, name="monitor-scheduler", daemon=True)
    thread.start()
    return thread


def stop_scheduler() -> None:
    _scheduler_stop.set()
