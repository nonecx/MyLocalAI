# -*- coding: utf-8 -*-
"""信源可信度速查的只读工具封装（通用技能，第四波）：check_source。

改了什么、为什么、锁定什么：
   - 新增 `assistant.tools_source`：把 `SourceSkills.run`（解析输入 → 读 1 页 →
     代码信号判定 → 历史战绩 → 摘要）接进助手工具层，注册为 `permission="read"`
     的 check_source（不建档、无分数——本工具没有任何分数字段，也没有任何
     "真/假/属实"式判定；权威类别/已核实判语全部由流水线内的代码信号算出，
     大模型不参与判定）；
   - 死线/取消/进度透传：handler 把 `ctx.cancel_check`、`llm_cache.deadline_at()`
     （回合预算，嵌套取更紧者）与 `_progress_emitter(ctx.events)` 原样交给流水线；
     死线取不到就传 None，由流水线按 rules.total_deadline_s 自建；
   - 卡片即用户可见的判定面：verified/category/signals/summary 全部来自
     `result.data`（逐字透传，不改写）；digest 只做结构化要点转述，叙述层不得
     新增任何信号或升级判语；
   - 永不抛异常（与 tools_research/tools_authority 的降级文化一致）：服务工厂
     取不到 / 装配炸 / run 炸 / 返回坏形状，一律折成 ok=False + 诚实 digest，
     会话不崩。

三条铁律：只评信源身份不判内容真假（digest/report 不出现真假判定词，"已核实"
只指信源身份、永远不指内容）；大模型不参与判定（目录锚点/动态核验信号/类别/
verified 全由代码计算，模型只许在注入时写一段受约束补充说明，越界即被流水线
丢弃）；每个出口都有降级（unreachable/unclear 是如实标注的合法出口，绝不把
"没读到页面"说成"核验过没问题"）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from mylocalai.assistant.registry import ToolRegistry, ValidationError

__all__ = ["register_all"]

#: 合法出口：六种 status 里只有这三个算 ok=False；needs_clarification（输入无法
#: 识别）是合法空出口，不算失败；unreachable/unclear 落在 data.state 里如实标注。
_FAILED_STATUSES = ("cancelled", "budget_exhausted", "failed")
#: degraded 口径：partial 及以上都如实标注降级；completed/needs_clarification 不算。
_DEGRADED_STATUSES = ("partial", "failed", "cancelled", "budget_exhausted")

#: 输入长度上限（与 source 规则 max_input_chars=500 同口径；registry 的
#: MAX_TEXT_LENGTH=8000 是通用文本上限，语义不同，不共用）。
_MAX_QUERY_CHARS = 500

#: state 封闭枚举的中文标签（与 source/SKILL.md 的"1+3"口径对应）
_STATE_LABELS = {"ok": "已取得页面并完成信号判定",
                 "unreachable": "页面不可达（未取得可用正文）",
                 "unclear": "输入无法识别为可核查信源"}
#: 代码信号的中文标签（与流水线 SIGNAL_LABELS 同口径；工具层自带一份而不
#: import pipeline 的映射：渲染层改文案不该静默改掉 digest 口径）
_SIGNAL_LABELS = {"official_suffix": "官方域名后缀", "official_marker": "正文官方标记",
                  "inbound_link": "已核验站点直链", "cross_mention": "多来源交叉提及"}


def _query_param(value: Any) -> str:
    """check_source 的 query 参数：1-500 字非空字符串（首尾空白剥掉）。"""
    if not isinstance(value, str):
        raise ValidationError("参数 query 必须是字符串")
    value = value.strip()
    if not value:
        raise ValidationError("参数 query 不能为空")
    if len(value) > _MAX_QUERY_CHARS:
        raise ValidationError(f"参数 query 过长（最多 {_MAX_QUERY_CHARS} 字）")
    return value


def _deadline() -> Optional[float]:
    """当前线程的绝对死线（回合预算，time.monotonic 基准）；未设预算返回 None。"""
    try:
        from mylocalai.llm import llm_cache
        return llm_cache.deadline_at()
    except Exception:  # noqa: BLE001  死线是增益约束：取不到就不设，流水线自建
        return None


def _progress_emitter(events: Any) -> Optional[Callable[[str, int, int], None]]:
    """把流水线的 on_progress(stage, done, total) 转成 SSE progress 事件源。

    component 用中文阶段名（pipeline 契约，共 4 步）：解析输入 / 读取页面 /
    信号判定 / 汇总输出——工具层只透传，不改写阶段名；events 为 None（MCP/同步
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
    """经 ctx.services 取信源速查流水线工厂并实例化（服务是工厂，同 research/authority）。"""
    services = getattr(ctx, "services", None)
    if services is None:
        raise KeyError("ctx.services 缺失")
    factory = services["source_skills"]
    skills = factory() if callable(factory) else factory
    if skills is None or not hasattr(skills, "run"):
        raise TypeError("source_skills 服务未返回可运行的流水线实例")
    return skills


def _unavailable(reason: str) -> Dict[str, Any]:
    """降级出口：装配/执行失败 → ok=False + 诚实 digest，不崩不编。"""
    detail = str(reason or "").strip()[:200] or "原因未知"
    return {"ok": False, "card": None,
            "digest": f"[信源速查] 不可用：{detail}。本次未取得任何判定，"
                      "我不会替它补一个结论。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True,
            "status": "failed", "task_status": "", "not_saved": True,
            "save_reason": "read_only_capability_no_dossier", "report": ""}


def _ids(value: Any) -> List[str]:
    return [str(x) for x in (value or []) if str(x or "").strip()]


def _resolved_view(resolved: Any) -> Dict[str, Any]:
    """resolved 紧凑视图（键名与 pipeline 的 _RESOLVED_KEYS 一致）。"""
    resolved = resolved if isinstance(resolved, dict) else {}
    return {"kind": str(resolved.get("kind") or ""),
            "url": str(resolved.get("url") or ""),
            "host": str(resolved.get("host") or ""),
            "name": str(resolved.get("name") or ""),
            "catalog": bool(resolved.get("catalog")),
            "catalog_anchor": bool(resolved.get("catalog_anchor")),
            "source_id": str(resolved.get("source_id") or ""),
            "tier": str(resolved.get("tier") or "")}


def _source_card(result: Dict[str, Any]) -> Dict[str, Any]:
    """source 卡：确定性判定的结构化承载，字段全部来自运行结果（逐字透传）。"""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    status = str(result.get("status") or "")
    return {
        "card_type": "source",
        "status": status,
        "task_status": str(result.get("task_status") or ""),
        "as_of": str(result.get("finished_at") or ""),
        "degraded": status in _DEGRADED_STATUSES,
        "query": str(data.get("query") or ""),
        "resolved": _resolved_view(data.get("resolved")),
        "state": str(data.get("state") or ""),
        "state_label": _STATE_LABELS.get(str(data.get("state") or ""), ""),
        "authority_category": str(data.get("authority_category") or ""),
        "category_label": str(data.get("category_label") or ""),
        "verified": bool(data.get("verified")),
        "basis": str(data.get("basis") or ""),
        "signals": [str(s) for s in (data.get("signals") or [])],
        "history": data.get("history") if isinstance(data.get("history"), dict) else None,
        "summary": str(data.get("summary") or ""),
        "limits": [str(x) for x in (data.get("limits") or [])],
        "gaps": [str(g) for g in (result.get("gaps") or [])],
        "errors": [{"stage": str(e.get("stage") or ""),
                    "category": str(e.get("category") or ""),
                    "detail": str(e.get("detail") or "")} for e in errors],
        "counts": {"searches": int(audit.get("searches") or 0),
                   "pages": int(audit.get("pages") or 0),
                   "llm_calls": int(audit.get("llm_calls") or 0)},
    }


def _source_digest(result: Dict[str, Any], card: Dict[str, Any]) -> str:
    """叙述层要点：只转述流水线判定（summary/依据/信号/战绩），不新增任何信号或升级判语。

    "已核实"只指信源身份、不指内容——digest 逐字转述流水线的 summary，叙述模型
    不得自行补充"内容可信/说法为真"之类的延伸。
    """
    counts = card["counts"]
    resolved = card["resolved"]
    lines: List[str] = ["[信源速查] 只评信源身份，不判内容真假（不建档、无分数）"]
    lines.append(f"- 执行状态：{card['status'] or '（未知）'}；"
                 f"任务状态：{card['task_status'] or '（未知）'}")
    lines.append(f"- 输入：{card['query'] or '（未记录）'}")
    kind_label = {"url": "网址", "domain": "域名", "name": "已登记机构名"}.get(
        resolved["kind"], "未识别")
    lines.append(f"- 解析：{kind_label} {resolved['host'] or resolved['name'] or '（未识别）'}"
                 f"｜目录锚点：{'是' if resolved['catalog_anchor'] else '否'}")
    state_label = card["state_label"] or _STATE_LABELS.get(card["state"], card["state"])
    lines.append(f"- 判定：{'已核实' if card['verified'] else '未核验/倾向'}（{state_label}）"
                 f"｜权威类别：{card['category_label'] or card['authority_category'] or '（未知）'}")
    signals = card["signals"]
    lines.append("- 代码信号：" + ("、".join(
        _SIGNAL_LABELS.get(s, s) for s in signals) if signals else "无"))
    lines.append(f"- 依据：{card['basis'] or '（无）'}")
    history = card["history"]
    if history:
        shown = list(history.items())[:4]
        lines.append("- 历史战绩：有记录；" + "；".join(f"{k}={v}" for k, v in shown))
    else:
        lines.append("- 历史战绩：无记录")
    if card["summary"]:
        lines.append("- 摘要（流水线判定原文，逐字）：")
        for line in card["summary"].splitlines():
            lines.append(f"  {line}")
    limits = card["limits"]
    lines.append("- 限制前 3 条：" + ("；".join(limits[:3]) if limits else "（无）"))
    gaps = card["gaps"]
    lines.append("- 缺口前 3 条：" + ("；".join(gaps[:3]) if gaps else "（无）"))
    errors = card["errors"]
    lines.append("- 错误前 3 条：" + ("；".join(
        f"[{e['stage']}] {e['category']}：{e['detail']}" for e in errors[:3])
        if errors else "（无）"))
    lines.append(f"- 已执行 {counts['pages']} 次读页、{counts['llm_calls']} 次模型调用；"
                 "检索 0 次（本技能不检索）")
    lines.append("- 边界：本速查不判页面内容真假；要核查具体说法请用 "
                 "verify_claim / verify_authority")
    return "\n".join(lines)


def _result(result: Dict[str, Any]) -> Dict[str, Any]:
    """运行结果 → 统一工具返回（ok/card/digest/cost/不建档/只读元数据）。"""
    card = _source_card(result)
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    status = card["status"]
    # 粗口径成本：模型调用 + 读页次数（M4 真实计数由 loop 的 delta 覆盖）
    requests = int(audit.get("llm_calls") or 0) + int(audit.get("pages") or 0)
    return {"ok": status not in _FAILED_STATUSES,
            "card": card,
            "digest": _source_digest(result, card),
            "cost": {"requests": requests, "tokens": 0},
            "dossier_id": None,   # 只读速查：不建档（铁律：不扩权）
            "degraded": status in _DEGRADED_STATUSES,
            "status": status,
            "task_status": card["task_status"],
            "not_saved": True,
            "save_reason": "read_only_capability_no_dossier",
            "report": str(result.get("report") or "")}


def _check_source(ctx: Any, query: str) -> Dict[str, Any]:
    """check_source：信源可信度速查（解析 → 读 1 页 → 代码信号判定 → 摘要），只读。"""
    try:
        skills = _skills(ctx)
    except Exception as exc:  # noqa: BLE001  工厂缺失/装配失败 = 诚实降级，会话不崩
        return _unavailable(f"信源速查流水线装配失败（{type(exc).__name__}）")
    try:
        result = skills.run(str(query),
                            cancel_check=getattr(ctx, "cancel_check", None),
                            deadline=_deadline(),
                            on_progress=_progress_emitter(getattr(ctx, "events", None)))
    except Exception as exc:  # noqa: BLE001  流水线自带"永不抛"外壳，这里是二道闸
        return _unavailable(f"信源速查流水线执行异常（{type(exc).__name__}）")
    if not isinstance(result, dict):
        return _unavailable("信源速查流水线返回形状不符")
    return _result(result)


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册信源速查的只读工具（services 形参约定同 tools_research.register_all）。"""
    registry.register("check_source", _check_source, params=["query"],
                      permission="read", cost="medium", est_s=15.0,
                      description="信源可信度速查：给一个网址/域名/机构名，只评信源身份"
                                  "（是不是可识别可追责的官方/权威信源），不判内容真假；"
                                  "只读 1 页、不建档、无分数",
                      validators={"query": _query_param})
