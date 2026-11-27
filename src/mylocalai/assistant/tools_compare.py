# -*- coding: utf-8 -*-
"""档案对比的只读工具封装（专用技能 dossier-comparison，第五波）：compare_dossiers。

改了什么、为什么、锁定什么：
   - 新增 `assistant.tools_compare`：把 `skills.comparison.compare_dossiers`
     （两份档案的只读 diff：标签演进/as_of/置信度档位、证据与信源域名增减、
     声明指纹相似度）接进助手工具层，注册为 `permission="read"` 的
     compare_dossiers（不建档、无分数——本工具没有任何分数字段，也没有任何
     "升级/降级"式判断；呈现只陈述两侧档案已记录的内容）。
   - 跨区防护与不存在同形（`tools_guard.dossier_visible`，与 get_dossier /
     export_card 同一契约）：档案不存在或属于别的工作区 → 同一种回答
     「档案 #N 不存在」+ `not_found=True`，不泄露别的工作区有哪些档案号。
   - 相同 id / 非正整数 id → **诚实降级**（ok=False + digest 说明要怎么改，
     `invalid_params=True`）：用户可修正的输入不摔成系统错误——仓库文化同
     tools_memory 的 not_found（loop 照常走叙述层，不落 error 事件）；
     registry.validate 层对非正整数的拒绝（路由期 clarify）保持不变，本模块
     不碰 registry。
   - 单边读失败折 partial + gaps 明示（纯函数口径，工具层只透传，不补结论）。
   - 对话卡去数值（《角色Agent工作流规格》§5 同口径）：卡片只出标签演进、
     as_of、置信度档位徽章与「疑似同源」徽章；相似度数值只在 data 层留档，
     不出 risk_score。
   - 永不抛（二道闸）：ctx.dossiers 取不到 / store 读炸 / 纯函数返回坏形状，
     一律折成 ok=False + 诚实 digest，会话不崩。

三条铁律：只呈现 diff 不判断真假（digest/card 不出现真假判定词，也不做升级/
降级判断）；大模型不参与任何一步（读数/差集/相似度全由代码计算）；每个出口都有
降级（partial 是如实标注的合法出口，绝不把"没读到"说成"对比过没问题"）。
"""

from __future__ import annotations

from typing import Any, Dict, List

from mylocalai.assistant.registry import ToolRegistry, id_param
from mylocalai.assistant.skills.comparison import MAX_POWER_RULES, compare_dossiers
from mylocalai.assistant.tools_guard import dossier_visible

__all__ = ["register_all"]

#: 置信度档位中文标签（与 web cards.js 的 CONF_LABEL 同口径；渲染层自带一份，
#: 不 import web——依赖方向禁 assistant → web）
_CONF_LABELS = {"high": "高", "medium": "中", "low": "低"}

#: 卡内相似度徽章的固定口径（与 compare.py 的 _SIMILARITY_BASIS 同义，卡内不出数值）
_SIMILARITY_BASIS = ("声明指纹相似度（输入文本归一化后的字符 k-shingle Jaccard）"
                     "只用于标注「疑似同源」，不作同一性断言；即便疑似同源，"
                     "两侧结论也各自独立、分别审阅。")


def _store_of(ctx: Any) -> Any:
    """经 ctx.dossiers.store 取档案库（与 tools_memory 的 dossier 访问方式一致）。"""
    return ctx.dossiers.store


def _store_failure(detail: str) -> Dict[str, Any]:
    """档案库整体读失败：ok=False + 诚实 digest，degraded（与 tools_memory 同口径）。"""
    return {"ok": False, "card": None,
            "digest": f"档案库读取失败：{str(detail or '').strip()[:200] or '原因未知'}。"
                      "本次未做任何对比，我不会替它补一份结果。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True,
            "status": "failed", "not_saved": True,
            "save_reason": "read_only_capability_no_dossier"}


def _not_found(dossier_id: int) -> Dict[str, Any]:
    """不存在或跨区：同形「档案 #N 不存在」（tools_guard 契约，逐字沿用 get_dossier）。"""
    return {"ok": False, "card": None, "digest": f"档案 #{int(dossier_id)} 不存在。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": False,
            "not_found": True, "not_saved": True,
            "save_reason": "read_only_capability_no_dossier"}


def _side_line(side: Any) -> str:
    """单侧档案的一句话呈现（无数值分数；缺结论如实说"尚无结论"）。"""
    if not isinstance(side, dict):
        return "读取失败或不可用（见缺口）"
    head = f"#{side.get('id')}《{side.get('title') or '未命名'}》"
    if side.get("label"):
        conf = _CONF_LABELS.get(str(side.get("confidence") or ""),
                                str(side.get("confidence") or "未知"))
        return (f"{head} 最新结论：{side.get('label')}"
                f"（置信度 {conf}，as_of {side.get('as_of') or '未知'}）")
    return f"{head} 尚无结论（该档案还没有跑过核查）"


def _entry_line(entry: Dict[str, Any], sign: str) -> str:
    title = str(entry.get("title") or "") or str(entry.get("url") or "（无标题）")
    relation = str(entry.get("relation") or "") or "立场未标注"
    return f"  {sign} {title}（{relation}）"


def _card(data: Dict[str, Any], status: str) -> Dict[str, Any]:
    """comparison 卡：确定性 diff 的结构化承载，字段全部来自纯函数输出（逐字透传）。

    卡内不出相似度数值（score/threshold 只在 data 层留档）——对话卡去数值口径；
    left/right 的版本历史本来就不带 risk_score。
    """
    similarity = data.get("similarity") if isinstance(data.get("similarity"), dict) else {}
    return {
        "card_type": "comparison",
        "status": status,
        "degraded": status == "partial",
        "left": data.get("left"),
        "right": data.get("right"),
        "similarity": {"suspected_same_source": bool(similarity.get("suspected_same_source")),
                       "basis": str(similarity.get("basis") or _SIMILARITY_BASIS)},
        "conclusion_delta": data.get("conclusion_delta"),
        "evidence_diff": data.get("evidence_diff"),
        "source_diff": data.get("source_diff"),
        "limits": [str(x) for x in (data.get("limits") or [])],
        "gaps": [str(g) for g in (data.get("gaps") or [])],
    }


def _digest(data: Dict[str, Any], status: str) -> str:
    """叙述层要点：只转述纯函数的 diff（标签演进/as_of/档位徽章/差集计数/缺口），
    不新增任何判断——没有真假判定词，也没有升级/降级表述，没有数值分数。"""
    lines: List[str] = [
        "[档案对比] 纯读取 diff：只陈述两侧档案已记录的内容，不重算、不判断真假、"
        "不做升级/降级判断，不建档、无分数"]
    if status == "partial":
        lines.append("- 执行状态：partial（有一侧读取不完整，见缺口；未编造任何内容）")
    lines.append(f"- 左档案：{_side_line(data.get('left'))}")
    lines.append(f"- 右档案：{_side_line(data.get('right'))}")

    delta = data.get("conclusion_delta") if isinstance(data.get("conclusion_delta"),
                                                       dict) else {}
    if delta.get("changed"):
        lines.append(f"- 标签演进：从「{delta.get('from_label')}」"
                     f"（as_of {delta.get('from_as_of') or '未知'}）变为"
                     f"「{delta.get('to_label')}」"
                     f"（as_of {delta.get('to_as_of') or '未知'}）")
    else:
        lines.append("- 标签演进：两端标签未变（或任一侧缺结论，见缺口）")

    similarity = data.get("similarity") if isinstance(data.get("similarity"),
                                                      dict) else {}
    if similarity.get("suspected_same_source"):
        lines.append("- 指纹相似度：疑似同源（仅标注，不作同一性断言）")
    elif similarity.get("score") is None:
        lines.append("- 指纹相似度：无法比对（至少一侧没有可用的声明原文，见缺口）")
    else:
        lines.append("- 指纹相似度：未见明显同源（仅标注，不作同一性断言）")

    evidence = data.get("evidence_diff") if isinstance(data.get("evidence_diff"),
                                                       dict) else {}
    lines.append(f"- 证据差集（按 url 去重，以左档案为基准）："
                 f"新增 {evidence.get('added_count', 0)} 条、"
                 f"减少 {evidence.get('removed_count', 0)} 条")
    for entry in (evidence.get("added") or [])[:3]:
        if isinstance(entry, dict):
            lines.append(_entry_line(entry, "+"))
    for entry in (evidence.get("removed") or [])[:3]:
        if isinstance(entry, dict):
            lines.append(_entry_line(entry, "-"))

    source = data.get("source_diff") if isinstance(data.get("source_diff"),
                                                    dict) else {}
    added_domains = [str(d) for d in (source.get("added_domains") or [])]
    removed_domains = [str(d) for d in (source.get("removed_domains") or [])]
    lines.append("- 信源域名：新增 " + ("、".join(added_domains) or "无")
                 + "；减少 " + ("、".join(removed_domains) or "无"))

    limits = [str(x) for x in (data.get("limits") or [])]
    gaps = [str(g) for g in (data.get("gaps") or [])]
    lines.append("- 限制前 3 条：" + ("；".join(limits[:3]) if limits else "（无）"))
    lines.append("- 缺口前 3 条：" + ("；".join(gaps[:3]) if gaps else "（无）"))
    lines.append("- 边界：本对比不判断真假、不做升级/降级判断；要核查具体说法请用 "
                 "verify_claim / verify_authority")
    return "\n".join(lines)


def _invalid_params(reason: str, hint: str = "") -> Dict[str, Any]:
    """参数层优雅降级（相同 id / 非正整数 id）：ok=False + 诚实 digest，**不抛**。

    用户可修正的输入走诚实降级而不是系统错误（仓库文化，同 tools_memory 的
    not_found 返回）：digest 把"要怎么改"说清楚（相同 id 时指路 get_dossier 看
    版本历史），loop 照常走叙述层，不落 error 事件、不摔成 degraded。
    `invalid_params=True` 是比 not_found 只多一个的标记键，供上层区分
    "参数劝退"与"查无此档"两种合法空出口。
    """
    text = f"档案对比未执行：{str(reason or '').strip()[:200] or '参数不合法'}"
    if hint:
        text += f"；{hint}"
    return {"ok": False, "card": None, "digest": text,
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None,
            "degraded": False, "status": "invalid", "not_saved": True,
            "save_reason": "read_only_capability_no_dossier", "report": "",
            "invalid_params": True}


def _result(result: Dict[str, Any]) -> Dict[str, Any]:
    """纯函数结果 → 统一工具返回（ok/card/digest/cost/不建档/只读元数据）。"""
    status = str(result.get("status") or "")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if status == "not_found":
        return _not_found(int(result.get("missing_id") or 0))
    if status == "invalid":
        gaps = [str(g) for g in (data.get("gaps") or [])]
        return _invalid_params(gaps[0] if gaps else "对比参数不合法")
    tool_status = "partial" if status == "partial" else "completed"
    return {"ok": True,
            "card": _card(data, tool_status),
            "digest": _digest(data, tool_status),
            "cost": {"requests": 0, "tokens": 0},
            "dossier_id": None,   # 对比不指向单一档案：不建档（铁律：不扩权）
            "degraded": tool_status == "partial",
            "status": tool_status,
            "not_saved": True,
            "save_reason": "read_only_capability_no_dossier",
            "data": data}


def _compare_dossiers(ctx: Any, left_id: int, right_id: int) -> Dict[str, Any]:  # noqa: A002
    """compare_dossiers：两份档案的只读对比（标签演进/证据域名差集/指纹相似度）。"""
    # 相同 id：诚实降级（用户可修正的输入不摔成系统错误；指路 get_dossier 看版本历史）
    if int(left_id) == int(right_id):
        return _invalid_params(
            f"左右档案不能相同（#{left_id}）：对比需要两个不同的档案",
            "如需查看该档案的历史版本变化，可用 get_dossier")
    # 直调路径的坏 id 兜底（registry.id_param 已在 validate 拦过一层，路由期折
    # clarify）：非正整数按参数劝退处理，不拿去查库、也不折成"不存在"
    if int(left_id) <= 0 or int(right_id) <= 0:
        return _invalid_params("档案 id 必须是两个不同的正整数档案号")
    try:
        store = _store_of(ctx)
    except Exception as exc:  # noqa: BLE001  ctx.dossiers 缺失/坏形状 = 诚实降级
        return _store_failure(f"档案库不可用（{type(exc).__name__}）")
    # 跨区/不存在防护：与 get_dossier 同一契约，两侧都查（先左后右）
    for did in (left_id, right_id):
        try:
            visible, store_error = dossier_visible(ctx, int(did))
        except Exception as exc:  # noqa: BLE001  防护自身不炸（二道闸）
            return _store_failure(f"档案库读取失败（{type(exc).__name__}）")
        if store_error:
            return _store_failure(store_error)
        if not visible:
            return _not_found(int(did))
    try:
        from mylocalai.assistant.skills.preferences import max_power_enabled
        compare_rules = MAX_POWER_RULES if max_power_enabled() else None
        result = compare_dossiers(store, int(left_id), int(right_id), rules=compare_rules)
    except Exception as exc:  # noqa: BLE001  纯函数自带"永不抛"外壳，这里是二道闸
        return _store_failure(f"对比执行异常（{type(exc).__name__}）")
    if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
        return _store_failure("对比返回形状不符")
    return _result(result)


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册档案对比的只读工具（本技能无模型无网络，不进 _ServiceBox，services 不用）。"""
    registry.register("compare_dossiers", _compare_dossiers,
                      params=["left_id", "right_id"],
                      permission="read", cost="zero", est_s=2.0,
                      description="对比两份档案：结论标签演进（首末版本）+ as_of + "
                                  "置信度档位、证据与信源域名增减（按 url 去重）、"
                                  "声明指纹相似度（只标疑似同源）；纯读取 diff，"
                                  "不重算、不判断真假、不建档、无分数",
                      validators={"left_id": id_param, "right_id": id_param})
