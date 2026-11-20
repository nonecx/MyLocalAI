# -*- coding: utf-8 -*-
"""材料批次 API（专业用户 A · 材料分析，阶段 2）。

端点（本地、无鉴权，与既有 API 一致）：
  POST /api/batch                建批次并后台开跑（202 + batch）
  GET  /api/batch                批次列表（当前档案工作区）
  GET  /api/batch/{id}           批次详情 + 条目 + 汇总
  GET  /api/batch/{id}/stream    SSE 进度（meta/status/progress/card/narrative/done/error）

并发：MAX_CONCURRENT_BATCHES（rules.batch.max_concurrent，缺省 2）个信号量，
超限 503 + Retry-After——与 /api/analyze 的 _Slot 同模式，批量最坏可跑 15 分钟，
不能把线程池和档案库写堵死。

分家：批次归当前激活档案工作区；跨工作区访问 → 404（与不存在同形）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from mylocalai.assistant.events import EventStream, format_sse
from mylocalai.dossier import batch_summary
from mylocalai.dossier.batches import BatchStore

router = APIRouter()

_batch_store: Optional[BatchStore] = None
_slots: Optional[threading.BoundedSemaphore] = None
_slots_lock = threading.Lock()
_active_batches = 0

#: 循环工厂（测试注入点）：默认惰性构造真实 AssistantLoop（services + dossiers）。
_loop_factory: Optional[Any] = None


def _build_loop() -> Any:
    if _loop_factory is not None:
        return _loop_factory()
    from mylocalai.assistant.loop import AssistantLoop
    return AssistantLoop()


def _rules() -> Dict[str, Any]:
    """assistant_rules.json（batch 段宿主）；读坏退空 dict，各取值处有兜底。"""
    try:
        from pathlib import Path
        import mylocalai.assistant as _pkg
        import json as _json
        path = Path(_pkg.__file__).resolve().parent / "assistant_rules.json"
        data = _json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001  规则读不到按内置默认
        return {}

#: 每个在跑批次的事件流登记表（batch_id → EventStream）。 finished 后由下一次
#: GET /api/batch/{id}/stream 用库内汇总重建卡片（见 _stream），不依赖该表。
_streams: Dict[int, EventStream] = {}
_streams_lock = threading.Lock()

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def get_batch_store() -> BatchStore:
    """批次存储单例（测试注入点）。"""
    global _batch_store
    if _batch_store is None:
        _batch_store = BatchStore()
    return _batch_store


def _active_profile_id() -> Optional[int]:
    """当前激活档案工作区 id（与 batches 同库的 app_state 单行）。"""
    try:
        pid = get_batch_store().active_profile_id()
        return int(pid) if pid else None
    except Exception:  # noqa: BLE001  激活态读失败不过滤（安全退化）
        return None


def _concurrency() -> int:
    try:
        return max(1, int((_rules().get("batch") or {}).get("max_concurrent", 2)))
    except (TypeError, ValueError):
        return 2


def _acquire_slot(timeout: float = 2.0) -> bool:
    """应用层并发名额：拿不到就 503，不把请求堆在框架线程池里。"""
    global _slots, _active_batches
    with _slots_lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(_concurrency())
    if not _slots.acquire(timeout=timeout):
        return False
    with _slots_lock:
        _active_batches += 1
    return True


def _release_slot() -> None:
    global _active_batches
    with _slots_lock:
        _active_batches = max(0, _active_batches - 1)
    if _slots is not None:
        _slots.release()


class BatchItemIn(BaseModel):
    """批次条目：text 直接给原文；url 给链接（编排期抓取）。"""
    text: str = ""
    url: str = ""


class BatchCreateRequest(BaseModel):
    """POST /api/batch 请求体。"""
    items: List[Any]
    title: str = ""
    source: str = "text"


def _normalize_items(raw_items: List[Any]) -> List[Dict[str, str]]:
    """请求 items → [{"text","url"}]；形状非法/超限抛 ValueError（web 转 400）。"""
    cfg = _rules().get("batch") or {}
    try:
        max_items = int(cfg.get("max_items", 8))
        max_item = int(cfg.get("max_item_chars", 8000))
        max_total = int(cfg.get("max_total_chars", 64000))
    except (TypeError, ValueError):
        max_items, max_item, max_total = 8, 8000, 64000

    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items 不能为空")
    if len(raw_items) > max_items:
        raise ValueError(f"条目数超限（最多 {max_items} 条，收到 {len(raw_items)}）")
    out: List[Dict[str, str]] = []
    total = 0
    for index, item in enumerate(raw_items, 1):
        text, url = "", ""
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = str(item.get("text") or "")
            url = str(item.get("url") or "").strip()
        else:
            raise ValueError(f"第 {index} 条材料形状非法（应为字符串或 {{text, url}} 对象）")
        text = text.strip()
        if not text and not url:
            raise ValueError(f"第 {index} 条材料为空（text 与 url 至少给一个）")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError(f"第 {index} 条 URL 必须是 http(s) 地址")
        if len(text) > max_item:
            raise ValueError(f"第 {index} 条材料过长（最多 {max_item} 字）")
        total += len(text)
        if total > max_total:
            raise ValueError(f"材料总字数超限（最多 {max_total} 字）")
        out.append({"text": text, "url": url})
    return out


@router.post("/api/batch")
def batch_create(req: BatchCreateRequest) -> Dict[str, Any]:
    """建批次并后台开跑。条目形状/超限 → 400；并发名额不足 → 503。"""
    try:
        items = _normalize_items(req.items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if req.source not in ("text", "file", "urls"):
        raise HTTPException(status_code=400,
                            detail=f"source 非法: {req.source}（可选 text/file/urls）")
    if not _acquire_slot():
        raise HTTPException(status_code=503,
                            detail="服务正忙（同时在跑的批次已达上限），请稍后重试",
                            headers={"Retry-After": "10"})
    store = get_batch_store()
    profile_id = _active_profile_id()
    title = (req.title or "").strip() or f"材料批次（{len(items)} 条）"
    batch_id = store.create_batch(title, req.source, items, profile_id=profile_id)
    events = EventStream()
    with _streams_lock:
        _streams[batch_id] = events
    threading.Thread(target=_run_worker,
                     args=(batch_id, events, profile_id),
                     name=f"batch-{batch_id}", daemon=True).start()
    batch = store.get_batch(batch_id) or {}
    return JSONResponse({"batch": batch}, status_code=202)


def _run_worker(batch_id: int, events: EventStream, profile_id: Optional[int]) -> None:
    """后台线程：装配引擎服务跑批次；收尾释放并发名额。"""
    try:
        loop = _build_loop()
        from mylocalai.assistant.tools_batch import run_batch
        run_batch(batch_id, store=get_batch_store(), dossiers=loop.dossiers,
                  services=loop.services, events=events, rules=_rules(),
                  profile_id=profile_id)
    except Exception as exc:  # noqa: BLE001  编排炸了也要给前端一个诚实收尾
        try:
            get_batch_store().set_batch_status(batch_id, "failed",
                                               error=f"{type(exc).__name__}: {exc}")
            events.emit("error", code="internal", recoverable=False,
                        message=f"批次编排异常：{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        _release_slot()


@router.get("/api/batch")
def batch_list() -> Dict[str, Any]:
    """批次列表（当前档案工作区，最新在前）。"""
    return {"batches": get_batch_store().list_batches(profile_id=_active_profile_id()),
            "active_profile_id": _active_profile_id()}


def _batch_or_404(batch_id: int) -> Dict[str, Any]:
    store = get_batch_store()
    batch = store.get_batch(int(batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail=f"批次不存在: {batch_id}")
    profile_id = _active_profile_id()
    owner = batch.get("profile_id")
    if profile_id is not None and owner is not None and int(owner) != int(profile_id):
        raise HTTPException(status_code=404, detail=f"批次不存在: {batch_id}")
    return batch


@router.get("/api/batch/{batch_id}")
def batch_detail(batch_id: str) -> Dict[str, Any]:
    """批次详情 + 条目 + 汇总（跨工作区 404）。"""
    try:
        bid = int(str(batch_id))
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail=f"批次不存在: {batch_id}") from None
    store = get_batch_store()
    batch = _batch_or_404(bid)
    items = store.items(bid)
    summary = batch.get("summary") if isinstance(batch.get("summary"), dict) else {}
    if not summary:
        summary = batch_summary.summarize(items)
    return {"batch": batch, "items": items, "summary": summary}


async def _drain(events: EventStream, poll_interval: float = 0.05):
    """把 EventStream 新增事件逐帧吐成 SSE（终止契约同 web.chat：done/error 且
    工作已收尾；批次 worker 是 daemon 线程，这里以事件流终止为准）。"""
    seen = 0
    while True:
        snapshot = events.snapshot()
        total = len(snapshot)
        while seen < total:
            yield format_sse(snapshot[seen])
            seen += 1
        last_name = snapshot[-1]["event"] if snapshot else ""
        if last_name in ("done", "error"):
            return
        await asyncio.sleep(poll_interval)


@router.get("/api/batch/{batch_id}/stream")
async def batch_stream(batch_id: str) -> StreamingResponse:
    """SSE 进度流。在跑的批次直播事件；已完结的批次用库内汇总重建卡片后一次性吐出
    （刷新页面不丢结论，与"卡片永远在叙述之前"同一取向）。"""
    try:
        bid = int(str(batch_id))
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail=f"批次不存在: {batch_id}") from None
    store = get_batch_store()
    batch = _batch_or_404(bid)
    with _streams_lock:
        events = _streams.get(bid)
    if events is not None and str(batch.get("status")) == "running":
        return StreamingResponse(_drain(events), media_type="text/event-stream",
                                 headers=_SSE_HEADERS)

    # 已完结：重建卡片（meta → card → narrative → done）
    items = store.items(bid)
    summary = batch.get("summary") if isinstance(batch.get("summary"), dict) \
        else batch_summary.summarize(items)
    card = batch_summary.summary_card(batch, items)

    def _replay():
        from mylocalai.assistant.events import make_event
        yield format_sse(make_event("meta", batch_id=bid))
        yield format_sse(make_event("status", stage=str(batch.get("status") or "done")))
        yield format_sse(make_event("card", **card))
        yield format_sse(make_event("done", cost={"requests": 0, "tokens": 0}))
    return StreamingResponse(_replay(), media_type="text/event-stream",
                             headers=_SSE_HEADERS)
