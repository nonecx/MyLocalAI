# -*- coding: utf-8 -*-
"""平台 REST 接口（web/platform_api.py）：状态 / 信息流 / 预警 / 监控词 / 信息源 / 巡检。

与 privacy_api/models_api 同一 router 模式（前缀 /api/platform，中文 400 detail）。
调度器与本模块各持一个 PlatformStore（同库不同连接，WAL 单写者由 SQLite 串行化，
既有 dossier/assistant 双 store 先例）。

隐私口径：本模块只读偏好状态（external_authorized）供前端出引导态；
授权真正的执行门在 runner/sources（未授权时巡检 skipped、不外发）。
信息源与领域两组端点同样不设隐私门——它们只改本机配置与手设领域，
真正的外发发生在巡检（发现源）与试运行（单源抓取）里，由 provider 自己
按授权/SSRF 口径把关。

领域契约（v7）：`GET /domains` 是前端领域下拉与徽章中文名的唯一数据源；
`feed_entries.domain` 是 URL 主机域，领域列叫 `topic_domain`（信息流徽章
读 `topic_domain` / `topic_domain_source`，不读 domain）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mylocalai.core.privacy import load_preferences
from mylocalai.platform.domains import DEFAULT_DOMAINS, domain_ids, load_domains
from mylocalai.platform.sources import SEARCH_PROVIDER_ID
from mylocalai.platform.store import PATROL_SOURCE_KINDS, PlatformStore

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


class WatchDomainIn(BaseModel):
    """PUT /api/platform/watch/{id} 的请求体（用户手设领域）。

    domain 空串 = 恢复自动推断；写入即清 inferred_domain/inferred_at 缓存
    （store.set_watch_query_domain 的口径：用户手改即废陈旧推断）。
    """

    domain: str = ""


class SourceIn(BaseModel):
    """POST /api/platform/sources 的请求体（用户自建信息源）。

    kind 只收 rss/page（store.PATROL_SOURCE_KINDS）；query_ids 为空列表/None
    都表示"暂不绑定"（= 全局源，对所有监控词生效）。
    """

    name: str = ""
    url: str = ""
    kind: str = "rss"
    note: str = ""
    query_ids: List[int] = Field(default_factory=list)


class SourceUpdateIn(BaseModel):
    """PUT /api/platform/sources/{id} 的请求体。

    与 store.update_patrol_source 同口径：None = 该字段不动；
    query_ids 为 [] = 清空全部绑定，为 [id...] = 重建绑定。
    """

    enabled: Optional[bool] = None
    note: Optional[str] = None
    query_ids: Optional[List[int]] = None


class SourceTestIn(BaseModel):
    """POST /api/platform/sources/{id}/test 的请求体（试运行查询词，可空）。"""

    query: str = ""


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


@router.put("/watch/{query_id}")
def platform_watch_set_domain(query_id: int, body: WatchDomainIn) -> Dict[str, Any]:
    """监控词领域手设：domain 空串 = 恢复自动推断（写入即清推断缓存）。

    领域 id 必须在领域表内（GET /domains 的 12 项）——不猜、不自造；
    监控词不存在 → 404。
    """
    if query_id <= 0:
        raise HTTPException(status_code=400, detail="id 必须是正整数")
    domain = (body.domain or "").strip()
    if domain and domain not in domain_ids():
        raise HTTPException(status_code=400, detail=f"领域不存在: {domain}")
    if not get_store().set_watch_query_domain(query_id, domain):
        raise HTTPException(status_code=404, detail=f"监控词不存在: {query_id}")
    return {"updated": True}


def _domain_items() -> List[Dict[str, Any]]:
    """领域表 → 对外形状 [{id, name, examples}]；加载异常/空表退内置默认。

    load_domains 自身承诺永不抛（坏配置已退内置默认并留告警），这里再兜一层
    空清单：前端下拉与徽章中文名不能拿到空数据源。
    """
    table, _warnings = load_domains()
    items: List[Dict[str, Any]] = []
    for raw in (table.get("domains") or []):
        if not isinstance(raw, dict):
            continue
        did = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not did or not name:
            continue
        examples = [str(x) for x in (raw.get("examples") or []) if str(x or "").strip()]
        items.append({"id": did, "name": name, "examples": examples})
    if items:
        return items
    return [{"id": str(d.get("id") or ""), "name": str(d.get("name") or ""),
             "examples": [str(x) for x in (d.get("examples") or [])]}
            for d in DEFAULT_DOMAINS if str(d.get("id") or "").strip()]


@router.get("/domains")
def platform_domains() -> Dict[str, Any]:
    """领域表（12 项）：监控词领域下拉与徽章中文名的唯一数据源。"""
    return {"domains": _domain_items()}


@router.get("/sources")
def platform_sources_list() -> Dict[str, Any]:
    """全部用户自建信息源（每项带 query_ids 绑定清单；含已停用的）。"""
    return {"sources": get_store().list_patrol_sources()}


@router.post("/sources", status_code=201)
def platform_source_add(body: SourceIn) -> Dict[str, Any]:
    """新增信息源：name/url 空 → 400；kind 非 rss/page → 400；
    URL 已存在（store 去重闸）→ 409；绑定不存在的监控词 → 400。"""
    name = (body.name or "").strip()
    url = (body.url or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="来源名称不能为空")
    if not url:
        raise HTTPException(status_code=400, detail="来源 URL 不能为空")
    kind = (body.kind or "rss").strip()
    if kind not in PATROL_SOURCE_KINDS:
        raise HTTPException(status_code=400,
                            detail="来源类型非法: "
                                   f"{kind}（可选 {' / '.join(PATROL_SOURCE_KINDS)}）")
    try:
        source_id = get_store().add_patrol_source(
            name, url, kind=kind, note=body.note or "",
            query_ids=list(body.query_ids or []))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400,
                            detail="绑定的监控词不存在（query_ids 含未知 id）") from exc
    if source_id is None:
        raise HTTPException(status_code=409, detail="该来源 URL 已存在")
    return {"id": source_id}


@router.put("/sources/{source_id}")
def platform_source_update(source_id: int, body: SourceUpdateIn) -> Dict[str, Any]:
    """改信息源：备注 / 绑定 / 启停（字段缺省不动）。源不存在 → 404。"""
    if source_id <= 0:
        raise HTTPException(status_code=400, detail="id 必须是正整数")
    store = get_store()
    try:
        updated = store.update_patrol_source(source_id, note=body.note,
                                            query_ids=body.query_ids)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400,
                            detail="绑定的监控词不存在（query_ids 含未知 id）") from exc
    if not updated:
        raise HTTPException(status_code=404, detail=f"信息源不存在: {source_id}")
    if body.enabled is not None:
        if not store.set_patrol_source_enabled(source_id, bool(body.enabled)):
            raise HTTPException(status_code=404, detail=f"信息源不存在: {source_id}")
    return {"updated": True}


@router.delete("/sources/{source_id}")
def platform_source_delete(source_id: int) -> Dict[str, Any]:
    """删除信息源（绑定经外键 CASCADE 一并消失）。不存在 → 404。"""
    if source_id <= 0:
        raise HTTPException(status_code=400, detail="id 必须是正整数")
    if not get_store().delete_patrol_source(source_id):
        raise HTTPException(status_code=404, detail=f"信息源不存在: {source_id}")
    return {"deleted": True}


def _run_source_test(source_id: int, query: str) -> Dict[str, Any]:
    """单源试运行的公共实现：经 PatrolSourceProvider.test_source 对该源单独跑
    一次发现逻辑（不走 store 清单，保存前也能先试）。同步抓取（RSS/网页），
    超时由 provider 内部控制；绝不抛，失败如实进 error。"""
    store = get_store()
    source = store.get_patrol_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"信息源不存在: {source_id}")
    # query 缺省时用源名，再退备注——两者都空则由 provider 报 empty_query
    text = ((query or "").strip() or str(source.get("name") or "").strip()
            or str(source.get("note") or "").strip())
    from mylocalai.platform.sources import PatrolSourceProvider
    result = PatrolSourceProvider(store).test_source(source, text, k=5)
    return {
        "ok": not result.degraded,
        "item_count": len(result.candidates),
        "sample_titles": [str(c.title) for c in result.candidates[:3]],
        "error": str(result.error or ""),
    }


@router.post("/sources/{source_id}/test")
def platform_source_test(source_id: int,
                         body: Optional[SourceTestIn] = None) -> Dict[str, Any]:
    """试运行信息源：返回 {ok, item_count, sample_titles(前 3 条标题), error}。

    ok = 源可达且解析成功（不保证有匹配项，item_count 如实计数）；
    失败/零匹配不抛，error 带源名与原因。body 可整个省略（query 缺省用源名/备注）。
    """
    return _run_source_test(source_id, body.query if body else "")


@router.post("/sources/{source_id}")
def platform_source_test_alias(source_id: int,
                               body: Optional[SourceTestIn] = None) -> Dict[str, Any]:
    """试运行的等价写法（契约里也写作 POST /sources/{id}）：与 /test 同一实现，
    不复制逻辑——源试运行只有一种口径，多一种路径只是少记一个后缀。"""
    return _run_source_test(source_id, body.query if body else "")


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
