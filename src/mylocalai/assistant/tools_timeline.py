# -*- coding: utf-8 -*-
"""事件时间线的只读工具封装（第五波，服务 monitor 角色）：event_timeline。

改了什么、为什么、锁定什么：
   - 新增 `assistant.tools_timeline`：把 `TimelineSkills.run`（预检 → 检索 → 读页 →
      事件抽取 → 同源转载去重 → 排序汇总）接进助手工具层，注册为
      `permission="read"` 的 event_timeline（不建档、无分数——本工具没有任何
      分数字段，也没有任何"真/假/属实"式判定；每条事件的引文锚定、日期归一、
      同源去重全部由流水线内的代码完成，大模型只负责从已读正文里抽事件描述）；
   - 死线/取消/进度透传：handler 把 `ctx.cancel_check`、`llm_cache.deadline_at()`
      （回合预算，嵌套取更紧者）与 `_progress_emitter(ctx.events)` 原样交给流水线；
      死线取不到就传 None，由流水线按 rules.total_deadline_s 自建；
   - 卡片即用户可见的时间线面：events/undated 的日期、事件描述与引文全部来自
      `result.data`（逐字透传，不改写）；digest 只做结构化要点转述，叙述层不得
      新增任何事件、日期或来源；
   - 永不抛异常（与 tools_research/tools_source 的降级文化一致）：服务工厂取不到 /
      装配炸 / run 炸 / 返回坏形状，一律折成 ok=False + 诚实 digest，会话不崩。

三条铁律：只述有来源记录的事件（digest/report 不出现真假判定词，不外推趋势）；
大模型不参与判定（引文锚定/日期归一/去重/上限全由代码计算，模型只许抽事件描述与
一段受约束补充说明，越界即被流水线丢弃）；每个出口都有降级（六种 status 都是合法
出口，零事件给空列表，绝不为有输出而编）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from mylocalai.assistant.registry import ToolRegistry, topic_param

__all__ = ["register_all"]

#: 合法出口：六种 status 里只有这三个算 ok=False；needs_clarification（输入空/超长）
#: 与"检索完成零事件"的 completed 都是合法空出口，不算失败。
_FAILED_STATUSES = ("cancelled", "budget_exhausted", "failed")
#: degraded 口径：partial 及以上都如实标注降级；completed/needs_clarification 不算。
_DEGRADED_STATUSES = ("partial", "failed", "cancelled", "budget_exhausted")

#: 运行状态（data.state）封闭枚举的中文标签（与 pipeline._STATE_LABELS 同口径；
#: 工具层自带一份而不 import pipeline 的映射：渲染层改文案不该静默改掉 digest 口径）
_STATE_LABELS = {"ok": "检索与抽取正常完成",
                 "unclear": "输入为空/超长，或运行在检索前被取消",
                 "search_unavailable": "检索不可用（未授权或后端故障）",
                 "extract_unavailable": "未注入事件描述模型，只完成检索",
                 "page_unavailable": "有候选来源但一页都没读到"}
#: 日期精度的中文标签（day/month/year 三值封闭枚举）
_PRECISION_LABELS = {"day": "精确到日", "month": "精确到月", "year": "精确到年"}

#: digest 里逐字带出的事件条数（叙述层不得自行新增事件）
_EVENT_PREVIEW = 3


def _query_param(value: Any) -> str:
    """event_timeline 的 query 参数：1-200 字非空字符串（首尾空白剥掉）。

    字数上限直接复用 `registry.topic_param`（MAX_TOPIC_LENGTH=200，与 timeline
    规则 max_input_chars 同值）：不另抄一份常量，避免"registry 一份、工具层一份"
    两处漂移（tools_source/tools_research 的自带校验器是历史写法，本工具按合同
    口径走 registry.topic_param）。
    """
    return topic_param(value)


def _deadline() -> Optional[float]:
    """当前线程的绝对死线（回合预算，time.monotonic 基准）；未设预算返回 None。"""
    try:
        from mylocalai.llm import llm_cache
        return llm_cache.deadline_at()
    except Exception:  # noqa: BLE001  死线是增益约束：取不到就不设，流水线自建
        return None


def _progress_emitter(events: Any) -> Optional[Callable[[str, int, int], None]]:
    """把流水线的 on_progress(stage, done, total) 转成 SSE progress 事件源。

    component 用中文阶段名（pipeline 契约，共 4 步）：确定主题 / 检索来源 /
    阅读抽取 / 整理时间线——工具层只透传，不改写阶段名；events 为 None（MCP/同步
    调用没有事件流）时返回 None，流水线对 None 回调逐位不变。
    """
    if events is None:
        return None

    def _on_progress(stage: Any, done: Any, total: Any) -> None:
        try:
            events.emit("progress", component=str(stage), done=int(done),
                        total=int(total))
        except Exception:  # noqa: BLE001  进度事件失败不影响主结论
            pass
    return _on_progress


def _skills(ctx: Any) -> Any:
    """经 ctx.services 取事件时间线流水线工厂并实例化（服务是工厂，同 research/source）。"""
    services = getattr(ctx, "services", None)
    if services is None:
        raise KeyError("ctx.services 缺失")
    factory = services["timeline_skills"]
    skills = factory() if callable(factory) else factory
    if skills is None or not hasattr(skills, "run"):
        raise TypeError("timeline_skills 服务未返回可运行的流水线实例")
    return skills


def _unavailable(reason: str) -> Dict[str, Any]:
    """降级出口：装配/执行失败 → ok=False + 诚实 digest，不崩不编。"""
    detail = str(reason or "").strip()[:200] or "原因未知"
    return {"ok": False, "card": None,
            "digest": f"[事件时间线] 不可用：{detail}。本次未取得任何事件，"
                      "我不会替它编一条时间线。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True,
            "status": "failed", "task_status": "", "not_saved": True,
            "save_reason": "read_only_capability_no_dossier", "report": ""}


def _event_view(item: Dict[str, Any]) -> Dict[str, Any]:
    """有日期事件紧凑视图：日期/精度/描述/来源/引文逐字透传（截断改写会让锚定失效）。"""
    return {"date": str(item.get("date") or ""),
            "precision": str(item.get("precision") or ""),
            "event": str(item.get("event") or ""),
            "source_url": str(item.get("source_url") or ""),
            "source_title": str(item.get("source_title") or ""),
            "quote": str(item.get("quote") or ""),
            "evidence_id": str(item.get("evidence_id") or "")}


def _undated_view(item: Dict[str, Any]) -> Dict[str, Any]:
    """无日期事件紧凑视图（键名与 pipeline._UNDATED_KEYS 一致：不带 evidence_id）。"""
    return {"event": str(item.get("event") or ""),
            "source_url": str(item.get("source_url") or ""),
            "source_title": str(item.get("source_title") or ""),
            "quote": str(item.get("quote") or "")}


def _range_view(value: Any) -> Dict[str, str]:
    """时间范围视图（首末两个日期串；零事件时为空串，不编区间）。"""
    rng = value if isinstance(value, dict) else {}
    return {"earliest": str(rng.get("earliest") or ""),
            "latest": str(rng.get("latest") or "")}


def _timeline_card(result: Dict[str, Any]) -> Dict[str, Any]:
    """timeline 卡：事件时间线的结构化承载，字段全部来自运行结果（逐字透传）。"""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    events = [e for e in (data.get("events") or []) if isinstance(e, dict)]
    undated = [u for u in (data.get("undated") or []) if isinstance(u, dict)]
    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    status = str(result.get("status") or "")
    state = str(data.get("state") or "")
    return {
        "card_type": "timeline",
        "status": status,
        "task_status": str(result.get("task_status") or ""),
        "as_of": str(result.get("finished_at") or ""),
        "degraded": status in _DEGRADED_STATUSES,
        "query": str(data.get("query") or ""),
        "state": state,
        "state_label": _STATE_LABELS.get(state, ""),
        "events": [_event_view(e) for e in events],
        "undated": [_undated_view(u) for u in undated],
        "range": _range_view(data.get("range")),
        "limits": [str(x) for x in (data.get("limits") or [])],
        "gaps": [str(g) for g in (result.get("gaps") or [])],
        "errors": [{"stage": str(e.get("stage") or ""),
                    "category": str(e.get("category") or ""),
                    "detail": str(e.get("detail") or "")} for e in errors],
        # counts 用 data 里的五键实耗（events/dropped 只存在于 data，audit 没有）
        "counts": {"searches": int(counts.get("searches") or audit.get("searches") or 0),
                   "pages": int(counts.get("pages") or audit.get("pages") or 0),
                   "llm_calls": int(counts.get("llm_calls") or audit.get("llm_calls") or 0),
                   "events": int(counts.get("events") or len(events)),
                   "dropped": int(counts.get("dropped") or 0)},
    }


def _timeline_digest(result: Dict[str, Any], card: Dict[str, Any]) -> str:
    """叙述层要点：只转述流水线产物（事件/引文/来源/计数），不新增任何事件。

    引文逐字带出——叙述模型唯一的证据来源就是这里 + 卡片，它不得自行补充事件、
    日期或来源，也不得给真假判断。
    """
    counts = card["counts"]
    rng = card["range"]
    lines: List[str] = ["[事件时间线] 只述有来源记录的事件，不判真假、不外推"
                        "（不建档、无分数）"]
    lines.append(f"- 执行状态：{card['status'] or '（未知）'}；"
                 f"任务状态：{card['task_status'] or '（未知）'}"
                 f"｜运行状态：{card['state_label'] or card['state'] or '（未知）'}")
    lines.append(f"- 主题：{card['query'] or '（未记录）'}")
    lines.append(f"- 时间范围：{rng['earliest'] or '（无）'} 至 {rng['latest'] or '（无）'}"
                 f"（有日期 {len(card['events'])} 条 / 无日期 {len(card['undated'])} 条）")
    lines.append(f"- 事件前 {_EVENT_PREVIEW} 条（引文逐字；叙述层不得新增事件）：")
    if card["events"]:
        for item in card["events"][:_EVENT_PREVIEW]:
            precision = _PRECISION_LABELS.get(item["precision"], item["precision"])
            lines.append(f"  · {item['date'] or '日期未知'}（{precision}）"
                         f"{item['event']}（{item['evidence_id']}）")
            lines.append(f"    「{item['quote']}」——《{item['source_title'] or '（无标题）'}》"
                         f"{item['source_url']}")
    else:
        lines.append("  （本次没有带日期的事件）")
    if card["undated"]:
        lines.append(f"- 无日期事件前 {_EVENT_PREVIEW} 条（不混入主线）：")
        for item in card["undated"][:_EVENT_PREVIEW]:
            lines.append(f"  · {item['event']}")
            lines.append(f"    「{item['quote']}」——《{item['source_title'] or '（无标题）'}》"
                         f"{item['source_url']}")
    limits = card["limits"]
    lines.append("- 限制前 3 条：" + ("；".join(limits[:3]) if limits else "（无）"))
    gaps = card["gaps"]
    lines.append("- 缺口前 3 条：" + ("；".join(gaps[:3]) if gaps else "（无）"))
    errors = card["errors"]
    lines.append("- 错误前 3 条：" + ("；".join(
        f"[{e['stage']}] {e['category']}：{e['detail']}" for e in errors[:3])
        if errors else "（无）"))
    lines.append(f"- 已执行 {counts['searches']} 次搜索、{counts['pages']} 次读页、"
                 f"{counts['llm_calls']} 次模型调用；收录 {counts['events']} 条事件、"
                 f"丢弃 {counts['dropped']} 条")
    lines.append("- 边界：只整理有来源记录的事件，不判真假、不外推趋势；"
                 "要核查具体说法请用 verify_claim / verify_authority")
    return "\n".join(lines)


def _result(result: Dict[str, Any]) -> Dict[str, Any]:
    """运行结果 → 统一工具返回（ok/card/digest/cost/不建档/只读元数据）。"""
    card = _timeline_card(result)
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    status = card["status"]
    # 粗口径成本：模型调用 + 检索 + 读页次数（M4 真实计数由 loop 的 delta 覆盖）
    requests = (int(audit.get("llm_calls") or 0) + int(audit.get("searches") or 0)
                + int(audit.get("pages") or 0))
    return {"ok": status not in _FAILED_STATUSES,
            "card": card,
            "digest": _timeline_digest(result, card),
            "cost": {"requests": requests, "tokens": 0},
            "dossier_id": None,   # 只读整理：不建档（铁律：不扩权）
            "degraded": status in _DEGRADED_STATUSES,
            "status": status,
            "task_status": card["task_status"],
            "not_saved": True,
            "save_reason": "read_only_capability_no_dossier",
            "report": str(result.get("report") or "")}


def _event_timeline(ctx: Any, query: str) -> Dict[str, Any]:
    """event_timeline：事件时间线整理（检索 → 读页 → 抽取 → 去重排序），只读。"""
    try:
        skills = _skills(ctx)
    except Exception as exc:  # noqa: BLE001  工厂缺失/装配失败 = 诚实降级，会话不崩
        return _unavailable(f"事件时间线流水线装配失败（{type(exc).__name__}）")
    try:
        result = skills.run(str(query),
                            cancel_check=getattr(ctx, "cancel_check", None),
                            deadline=_deadline(),
                            on_progress=_progress_emitter(getattr(ctx, "events", None)))
    except Exception as exc:  # noqa: BLE001  流水线自带"永不抛"外壳，这里是二道闸
        return _unavailable(f"事件时间线流水线执行异常（{type(exc).__name__}）")
    if not isinstance(result, dict):
        return _unavailable("事件时间线流水线返回形状不符")
    return _result(result)


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册事件时间线的只读工具（services 形参约定同 tools_research.register_all）。"""
    registry.register("event_timeline", _event_timeline, params=["query"],
                      permission="read", cost="medium", est_s=30.0,
                      description="事件时间线整理：按主题检索公开来源并逐页抽取带逐字"
                                  "引文的事件，按时间排序输出（只述有来源记录的事件，"
                                  "不判真假、不外推；只读、不建档、无分数）",
                      validators={"query": _query_param})
