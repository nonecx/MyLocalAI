# -*- coding: utf-8 -*-
"""材料批次编排（专业用户 A · 材料分析，阶段 2）。

定位：不是对话工具（无 action 名、不进路由 allowlist）——批量是"面板按钮 →
POST /api/batch → 后台线程 + SSE 进度"的独立通道，与对话回合的解耦点同哲学
（web 层持有、loop 不感知）。

编排契约：
  1. 条目校验（条数/单条字数/总字数）由 web 层在 400 前拦；本模块按 rules 的
     batch 段取上限，双预算（条目级 item_deadline_s + 整批 deadline_s）；
  2. 同源预分组（fingerprint Jaccard，dossier/batch_summary.group_similar）：
     同组各自判定，汇总层合并呈现，**不擅自合并结论**；
  3. 逐条：services["verify"]（RumorAgent 全链路）→ start_dossier（归会话/批次
     所属工作区）→ record_analysis；单条失败记 error 不阻断后续；
  4. 取消（cancel_check）与总预算用尽 → 剩余条目标 not_run/cancelled，
     **绝不把"没跑"显示成"没发现"**；
  5. 收尾：summary 纯函数汇总（含规格 §5 三要素，原料为条目档案证据）+
     batch_summary 卡片（card 事件 + 落库）；done 事件 cost 取 llm_client
     进程级计数器的段前/段后快照差值（缓存命中如实为 0，不估算）。

依赖方向：assistant → dossier（allowed）；引擎经 services 注入（测试可打桩）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from mylocalai.assistant.fingerprint import DEFAULT_SHINGLE_SIZE, jaccard, \
    normalize_claim, shingles
from mylocalai.llm import llm_cache

__all__ = ["group_similar", "run_batch"]

#: rules["batch"] 缺段/写坏时的内置兜底（与 assistant_rules.json 的 batch 段同值）
_BATCH_DEFAULTS = {
    "max_items": 8,
    "max_item_chars": 8000,
    "max_total_chars": 64000,
    "item_deadline_s": 90.0,
    "deadline_s": 900.0,
    "similarity_threshold": 0.72,
}


def _cfg(rules: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = dict(_BATCH_DEFAULTS)
    raw = (rules or {}).get("batch")
    if isinstance(raw, dict):
        for key in cfg:
            if key in raw:
                try:
                    cfg[key] = type(cfg[key])(raw[key])
                except (TypeError, ValueError):
                    pass   # 配置写坏按默认值，不放大成故障
    return cfg


def group_similar(texts: List[str], threshold: float = 0.72,
                  k: int = DEFAULT_SHINGLE_SIZE) -> List[int]:
    """把文本列表按相似度分组，返回与输入等长的 group_id 列表（0=无组，组号从 1 起）。

    单链分组（并查集）：A~B 且 B~C ⇒ 同组。阈值是 Jaccard（与指纹闸
    fingerprint_threshold 同口径，保守：松散近义不并组）。导入失败/坏输入按
    "无组"降级。放在 assistant 层是因为它用 assistant.fingerprint——依赖方向
    禁 dossier → assistant（tests/test_import_rules 锁定）。
    """
    n = len(texts or [])
    if n == 0:
        return []
    try:
        norms = [normalize_claim(t) for t in texts]
        shingle_sets = [shingles(t, k) if t else frozenset() for t in norms]
    except Exception:  # noqa: BLE001  指纹不可用 = 不分组（汇总仍可用）
        return [0] * n

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i in range(n):
        if not shingle_sets[i]:
            continue
        for j in range(i + 1, n):
            if not shingle_sets[j]:
                continue
            if jaccard(shingle_sets[i], shingle_sets[j]) >= float(threshold):
                union(i, j)

    # 只给"成员 ≥2"的组编号（单成员组等于无组，不出现在汇总里）
    counts: Dict[int, int] = {}
    for i in range(n):
        counts[find(i)] = counts.get(find(i), 0) + 1
    group_of_root: Dict[int, int] = {}
    next_id = 1
    for i in range(n):
        root = find(i)
        if counts.get(root, 0) >= 2 and root not in group_of_root:
            group_of_root[root] = next_id
            next_id += 1
    return [group_of_root.get(find(i), 0) for i in range(n)]


def _progress_emitter(events: Any) -> Optional[Callable[[str], None]]:
    """组件完成 → SSE progress（与 tools_verify 同款；events 为 None 时禁用）。"""
    if events is None:
        return None
    state = {"done": 0}

    def _on_component_done(name: str) -> None:
        state["done"] += 1
        try:
            events.emit("progress", component=str(name), done=state["done"], total=7)
        except Exception:  # noqa: BLE001  进度事件失败不影响主结论
            pass
    return _on_component_done


def _fetch_url_text(url: str) -> tuple[Optional[str], str]:
    """URL 条目取正文（fail-closed：未授权外部抓取 → (None, 说明)）。

    授权口径与检索层一致：external_allowed("public-web")；抓取用
    retrieval.search.fetch_article（自带 SSRF 防护/大小/超时约束）。
    """
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None, "URL 必须是 http(s) 地址"
    try:
        from mylocalai.core.privacy import load_preferences
        prefs = load_preferences()
        if not prefs.external_allowed("public-web"):
            return None, "外部网页抓取未授权（设置 → 隐私与授权 → 外部处理）"
        from mylocalai.retrieval.search import fetch_article
        got = fetch_article(url)
    except Exception as exc:  # noqa: BLE001  抓取失败按该条降级，不阻断整批
        return None, f"抓取失败：{type(exc).__name__}: {exc}"
    if not isinstance(got, dict):
        return None, "页面未取到正文（反爬或内容过短）"
    text = str(got.get("text") or "").strip()
    if not text:
        return None, "页面未取到正文（反爬或内容过短）"
    return text, ""


def run_batch(batch_id: int, *, store: Any, dossiers: Any, services: Dict[str, Any],
              events: Any = None, cancel_check: Optional[Callable[[], bool]] = None,
              rules: Optional[Dict[str, Any]] = None,
              profile_id: Optional[int] = None) -> Dict[str, Any]:
    """跑完一个批次：逐条核查 + 汇总 + 卡片。永不抛；返回批次结果 dict。"""
    from mylocalai.dossier import batch_summary

    cfg = _cfg(rules)
    items = store.items(int(batch_id))
    batch = store.get_batch(int(batch_id)) or {}
    # services 可能是 dict（测试 stub）或 loop 的 _ServiceBox（只有 __getitem__）——
    # 统一按 __getitem__ 取，取不到按"未装配"降级，绝不猜一个服务。
    try:
        verify = services["verify"]
    except (KeyError, TypeError):
        verify = None
    out: Dict[str, Any] = {"batch_id": int(batch_id), "items": [], "summary": {}}

    def emit(name: str, **payload: Any) -> None:
        if events is None:
            return
        try:
            events.emit(name, **payload)
        except Exception:  # noqa: BLE001  事件失败不挡编排
            pass

    def cancelled() -> bool:
        try:
            return bool(cancel_check()) if cancel_check is not None else False
        except Exception:  # noqa: BLE001
            return False

    # R7：进程级真实用量计数器的段前快照（llm_client.usage_snapshot，与 loop.py
    # 成本汇总同款读法）。计数面不可用（导入/读取失败）退回 0 起点——段后差值
    # 自然为 0，与既有"如实降级"口径一致。
    cost_before = {"requests": 0, "tokens": 0}
    try:
        from mylocalai.llm.llm_client import usage_snapshot
        cost_before = dict(usage_snapshot())
    except Exception:  # noqa: BLE001  计数面不可用不影响编排
        cost_before = {"requests": 0, "tokens": 0}

    # 同源预分组（raw_text 可能还是空——URL 条目此时未抓取；用 url 兜底参与分组）
    group_ids = group_similar(
        [str(it.get("raw_text") or it.get("url") or "") for it in items],
        threshold=float(cfg["similarity_threshold"]))
    for it, gid in zip(items, group_ids):
        if gid:
            store.update_item(int(it["id"]), group_id=int(gid))

    emit("meta", batch_id=int(batch_id))
    emit("status", stage="running")

    ok = failed = blocked = not_run = 0
    total_deadline = time.monotonic() + float(cfg["deadline_s"])
    item_deadline_s = float(cfg["item_deadline_s"])

    for index, it in enumerate(items):
        if cancelled():
            store.update_item(int(it["id"]), status="not_run", error="批次已取消")
            not_run += 1
            continue
        if time.monotonic() >= total_deadline:
            store.update_item(int(it["id"]), status="not_run",
                              error="整批时间预算用尽，本条未运行")
            not_run += 1
            continue

        store.update_item(int(it["id"]), status="running")
        emit("progress", done=index, total=len(items), position=index, status="running")

        text = str(it.get("raw_text") or "").strip()
        if not text and str(it.get("url") or "").strip():
            text, fetch_error = _fetch_url_text(str(it.get("url")))
            if text:
                store.update_item(int(it["id"]), raw_text=text[:cfg["max_item_chars"]])
            else:
                store.update_item(int(it["id"]), status="blocked", error=fetch_error)
                blocked += 1
                emit("progress", done=index + 1, total=len(items), position=index,
                     status="blocked")
                continue
        if not text:
            store.update_item(int(it["id"]), status="error", error="材料内容为空")
            failed += 1
            emit("progress", done=index + 1, total=len(items), position=index,
                 status="error")
            continue
        if len(text) > cfg["max_item_chars"]:
            text = text[:cfg["max_item_chars"]]

        if verify is None:
            store.update_item(int(it["id"]), status="error", error="核查服务未装配")
            failed += 1
            emit("progress", done=index + 1, total=len(items), position=index,
                 status="error")
            continue

        try:
            # 条目级预算：嵌套进本线程的 deadline scope（取更紧者）
            with llm_cache.deadline_scope_at(time.monotonic() + item_deadline_s):
                result = verify(text, False,
                                on_component_done=_progress_emitter(events),
                                cancel_check=cancel_check) or {}
        except Exception as exc:  # noqa: BLE001  单条炸不炸整批
            store.update_item(int(it["id"]), status="error",
                              error=f"{type(exc).__name__}: {exc}")
            failed += 1
            emit("progress", done=index + 1, total=len(items), position=index,
                 status="error")
            continue
        if not isinstance(result, dict):
            result = {}

        dossier_id = None
        try:
            dossier_id = dossiers.start_dossier(text, profile_id=profile_id)
        except Exception:  # noqa: BLE001  建档失败不影响该条结论呈现
            dossier_id = None
        label = str(result.get("label") or "")
        risk = result.get("risk_score")
        version = None
        if dossier_id is not None:
            try:
                rec = dossiers.record_analysis(dossier_id, result, kind="batch",
                                               duration_s=0.0,
                                               requests=int(result.get("llm_turns") or 0)) or {}
                version = rec.get("version")
            except Exception:  # noqa: BLE001  落库失败照常出该条结论
                version = None
        store.update_item(int(it["id"]), status="ok", label=label, risk_score=risk,
                          dossier_id=dossier_id)
        ok += 1
        emit("progress", done=index + 1, total=len(items), position=index,
             status="ok", label=label, dossier_id=dossier_id, version=version)

    items = store.items(int(batch_id))
    # 规格 §5 三要素原料：批次各条目档案的库内证据（BatchStore 只读口）。store
    # 没有该口（注入替身）或读取失败时按无证据降级——三要素空结构，如实省略。
    evidence_by_dossier = None
    dossier_ids = []
    for it in items:
        try:
            did = int(it.get("dossier_id") or 0)
        except (TypeError, ValueError):
            continue
        if did:
            dossier_ids.append(did)
    if dossier_ids:
        getter = getattr(store, "evidence_for_dossiers", None)
        if callable(getter):
            try:
                evidence_by_dossier = getter(dossier_ids)
            except Exception:  # noqa: BLE001  证据读不到不影响主汇总
                evidence_by_dossier = None
    summary = batch_summary.summarize(items, evidence_by_dossier=evidence_by_dossier)
    # R7：段后快照与段前的差值 = 本批次真实用量（缓存命中不计数、提供方不回
    # usage 时如实为 0——与 record_usage 的口径一致，绝不按字数估算）。这里先
    # 算好并入 summary 落库：完结批次刷新后的 SSE 重放 done 事件也如实带成本。
    cost = {"requests": 0, "tokens": 0}
    try:
        from mylocalai.llm.llm_client import usage_snapshot
        after = usage_snapshot()
        cost = {"requests": max(0, int(after.get("requests", 0))
                                - int(cost_before.get("requests", 0))),
                "tokens": max(0, int(after.get("tokens", 0))
                              - int(cost_before.get("tokens", 0)))}
    except Exception:  # noqa: BLE001  计数面不可用：如实退 0（既有降级口径）
        cost = {"requests": 0, "tokens": 0}
    summary["cost"] = dict(cost)
    # R5：blocked 与 failed 分列落库（failed_count 只记条目 error），blocked 走
    # summary JSON（BatchStore 读侧派生 blocked_count，表结构不动）。
    counts = {"ok": summary["ok"], "failed": summary["failed"],
              "blocked": summary["blocked"], "not_run": summary["not_run"]}
    # status 判定（R5 最小改动方案）：
    #   cancelled —— 用户取消且确实停下了后续条目（not_run>0；R1 接线后"停止"
    #                按钮断开 SSE 是主取消源，哪怕已有部分条目成功也如实记已取消）；
    #   done     —— 无 failed/blocked/not_run（全部成功）；
    #   partial  —— 有产出（ok>0）或有被拦截条目（blocked>0，全 blocked 不再落 failed）；
    #   failed   —— 剩余情形（无 ok 无 blocked：全是失败或未跑）。
    status = ("cancelled" if cancelled() and counts["not_run"]
              else "done" if not (counts["failed"] or counts["blocked"]
                                  or counts["not_run"])
              else "partial" if counts["ok"] or counts["blocked"] else "failed")
    store.set_batch_status(int(batch_id), status, ok_count=counts["ok"],
                           failed_count=counts["failed"],
                           not_run_count=counts["not_run"],
                           summary=summary)
    batch = store.get_batch(int(batch_id)) or batch
    card = batch_summary.summary_card(batch, items, summary=summary)
    out["items"] = items
    out["summary"] = summary
    emit("card", **card)
    emit("narrative", delta=_narrative(summary))
    emit("done", cost=cost)
    return out


def _narrative(summary: Dict[str, Any]) -> str:
    """批次收尾的一句话叙述（模板拼接，零 LLM； counts 全部来自纯函数汇总）。"""
    parts = [f"批次分析完成：共 {summary.get('total', 0)} 条，成功 {summary.get('ok', 0)} 条"]
    if summary.get("failed"):
        parts.append(f"失败 {summary['failed']} 条")
    if summary.get("blocked"):
        parts.append(f"未授权抓取 {summary['blocked']} 条")
    if summary.get("not_run"):
        parts.append(f"未运行 {summary['not_run']} 条（预算用尽或取消）")
    by_label = summary.get("by_label") or {}
    if by_label:
        parts.append("结论分布：" + "、".join(f"{k} {v}" for k, v in by_label.items()))
    if summary.get("groups"):
        parts.append(f"检出 {len(summary['groups'])} 组疑似同源说法")
    return "；".join(parts) + "。结论以上方汇总卡为准，逐条档案已归档。"
