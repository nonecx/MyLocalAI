# -*- coding: utf-8 -*-
"""章节类工具封装（§5.2 M3-3/M3-5）：read_opinions（观点光谱）/ deep_read（背景解读）。

==============================================================================
《deep_read 章节化调用方规格》（给编排者的接线说明，20 行内）
真实 deep_read 服务（services["deep_read"](dossier_id) 的实现方）按以下顺序装配：
  ① 证据本复用：从 dossier.store.evidence(id) 取档案证据行，按行序编号成
    [{"text": <snippet>, "stance": ..., "source": <domain>, ...}, ...] 传给
    attention.deep_read.deep_read(evidence=...)。引擎按传入条数生成编号 e1..eN，
    护栏有效证据集=档案证据集（模型引用 e(N+1) 会被剔除，guard_dropped 可见）。
  ② 快照历史：从 dossier.store.snapshots(id) 取 e_score 序列作为 snapshot_history
    供 change_rate 触发判定（环比基于同档案快照序列；单快照不触发，回落既有规则）。
  ③ opinions_signals：档案已有观点光谱章节（store.opinions(id) 非空）时，把其中的
    诉求/归因对象/行动倾向组装成 {"appeals": [...], "attribution_targets": [...],
    "action_tendencies": [...]} 传给 deep_read(opinions_signals=...)；无章节则不传。
  ④ 空证据本回退：档案证据本为空时不进受控生成，回退独立搜索——collect_evidence
    自取证据（现有行为）后再进深读；仍无证据则如实在 digest 标"不适用"，不硬编。
deep_read 引擎本身不变（attention/deep_read.py 只读）；本文件只消费 services 返回。
==============================================================================

约定同 tools_verify/tools_heat（编排者契约）：handler(ctx, **args)，服务一律经
ctx.services 取（注入优先、缺省键由 loop 的 _ServiceBox 惰性装配真实引擎），返回
统一 {"ok", "card", "digest", "cost": {"requests","tokens"}, "dossier_id",
"degraded", + 业务字段}；永不抛异常（服务缺失/抛错都折成诚实 error 返回）。

铁律口径（§0）：
  - 观点光谱只描述"证据本里的人怎么说"（立场分布 + 论据可信度），**不判真假**；
  - "证据不足 / 未触发 / 不适用（applicable=false / triggered=false）"是合法输出，
    如实写进 digest 并落库，不为有输出而编；
  - 两工具都是"读+写章节"：分析结果经 DossierService.record_opinions /
    record_deep_read 写为档案章节（ctx.dossiers）；落库失败不挡 digest（如实标注）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from mylocalai.assistant.registry import ToolRegistry, id_param
from mylocalai.assistant.tools_guard import dossier_visible

__all__ = ["register_all"]

#: 深读文本进 digest 的截断上限（叙述层另有 12k 字符预算，digest 是要点不是全文转载）
_DIGEST_TEXT_CAP = 500
#: 成本口径：一次完整分析粗记 1 轮请求（取文+LLM 合并计数；M4 接 llm_client 真实计数后换真实值）
_REQUESTS_WHEN_ACTIVE = 1


def _result(ok: bool, digest: str, dossier_id: Optional[int], degraded: bool,
            requests: int = 0, **extra: Any) -> Dict[str, Any]:
    out = {"ok": bool(ok), "card": None, "digest": digest,
           "cost": {"requests": int(requests), "tokens": 0},
           "dossier_id": dossier_id, "degraded": bool(degraded)}
    out.update(extra)
    return out


def _dossier_exists(ctx: Any, dossier_id: int) -> Tuple[bool, str]:
    """档案是否存在**且属于当前工作区**（章节工具的落点；v6 多 Profile 分家）。

    跨工作区的档案号对外按"不存在"处理（tools_guard.dossier_visible 契约：
    不泄露别的工作区有哪些档案）。库读失败时把错误如实带回（err 非空）。
    """
    return dossier_visible(ctx, dossier_id)


def _degraded_of(payload: Dict[str, Any]) -> bool:
    """服务契约里 degraded 是 bool；引擎原始形状是降级记录列表——两种都认。"""
    d = payload.get("degraded")
    if isinstance(d, list):
        return bool(d)
    return bool(d)


def _degraded_notes(payload: Dict[str, Any]) -> List[str]:
    """把降级原因整理成可读列表（bool 形状取 degraded_reason/error，list 形状逐条取 reason）。"""
    d = payload.get("degraded")
    if isinstance(d, list):
        return [str((x.get("reason") if isinstance(x, dict) else x) or "").strip()
                for x in d if x]
    note = str(payload.get("degraded_reason") or payload.get("error") or "").strip()
    return [note] if note else []


def _count_of(value: Any) -> Optional[int]:
    """立场计数的容错读取：int/float 直接用；{"count": n} 形状取 count；其余不认（不编）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        n = value.get("count", value.get("total"))
        if isinstance(n, (int, float)) and not isinstance(n, bool):
            return int(n)
    return None


def _distribution_line(distribution: Any) -> str:
    """立场分布一行文本：'支持 3 / 反驳 2（共 5 条）'；空/坏形状返回空串（如实留空）。"""
    if not isinstance(distribution, dict) or not distribution:
        return ""
    parts: List[str] = []
    total = 0
    for key, value in distribution.items():
        n = _count_of(value)
        if n is None:
            continue
        total += n
        parts.append(f"{key} {n}")
    if not parts:
        return ""
    return " / ".join(parts) + f"（共 {total} 条）"


def _record_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """观点章节的落库形状。record_opinions 只存 distribution/summary/degraded 三列：
    未触发时引擎的 skip_reason 不在任何列里——把它补进 summary，章节才如实说明
    "为什么没有分布"，否则空行会被读成"分析过且什么都没有"（不诚实）。"""
    record = dict(payload)
    record["degraded"] = _degraded_of(payload)
    if not str(record.get("summary") or "").strip():
        reason = str(record.get("skip_reason") or record.get("reason") or "").strip()
        if reason:
            record["summary"] = f"未触发：{reason}"
    return record


def _opinions_digest(dossier_id: int, payload: Dict[str, Any], stored: bool) -> str:
    lines = [f"[观点光谱] 档案 #{dossier_id}"]
    dist = _distribution_line(payload.get("distribution"))
    if dist:
        lines.append(f"- 立场分布：{dist}")
    if payload.get("triggered") is False:
        reason = str(payload.get("skip_reason") or payload.get("reason") or "").strip()
        if reason:
            lines.append(f"- 未触发民间观点分析：{reason}（证据不足或已有权威来源是合法出口，不硬析）")
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.append(f"- 要点：{summary}")
    if not dist and not summary:
        lines.append("- 服务未给出立场分布与要点（如实留空，不编造）")
    notes = [n for n in _degraded_notes(payload) if n]
    if notes:
        lines.append("- 降级说明：" + "；".join(notes))
    lines.append("- 口径：只描述证据本里的立场分布与论据可信度（大模型不参与定级），不构成真假判定。")
    if not stored:
        lines.append("- 章节落库失败：本结果未写入档案（其余信息如实如上）。")
    return "\n".join(lines)


def _read_opinions(ctx: Any, id: int) -> Dict[str, Any]:  # noqa: A002  与路由参数名一致
    """read_opinions：观点光谱分析 → 写档案观点章节（读+写章节，§5.2 M3-3）。"""
    exists, err = _dossier_exists(ctx, id)
    if err:
        return _result(False, f"档案库读取失败：{err}", None, True)
    if not exists:
        # 档案不存在是合法输入错误：诚实说没有，不猜、不新建
        return _result(False, f"档案 #{id} 不存在，无法生成观点光谱。请先用 list_dossiers 确认档案号。",
                       None, False, not_found=True)
    try:
        payload = ctx.services["opinions"](int(id))
    except KeyError:
        return _result(False, "观点光谱服务未接入（services 缺少 opinions 服务键）。",
                       int(id), True)
    except Exception as exc:  # noqa: BLE001  服务抛错折成诚实 error 返回，不抛
        return _result(False, f"观点光谱分析失败：{type(exc).__name__}: {exc}", int(id), True)
    if not isinstance(payload, dict) or not payload:
        return _result(False, f"观点光谱服务无输出（档案 #{id}）。", int(id), True)

    degraded = _degraded_of(payload)
    try:
        ctx.dossiers.record_opinions(int(id), _record_payload(payload))
        stored = True
    except Exception as exc:  # noqa: BLE001  落库失败不挡 digest，如实标注
        stored = False
        record_error = f"{type(exc).__name__}: {exc}"
    else:
        record_error = ""

    digest = _opinions_digest(int(id), payload, stored=stored)
    if record_error:
        digest += f"\n- 落库错误：{record_error}"
    # 未触发 = 只读了档案证据做触发判定，零网络；触发 = 取文+LLM 一轮完整分析
    active = bool(payload.get("triggered"))
    return _result(True, digest, int(id), degraded or not stored,
                   requests=_REQUESTS_WHEN_ACTIVE if active else 0,
                   triggered=bool(payload.get("triggered")))


def _deep_read_digest(dossier_id: int, payload: Dict[str, Any], stored: bool) -> str:
    lines = [f"[背景解读（深读）] 档案 #{dossier_id}"]
    if not payload.get("triggered"):
        reason = str(payload.get("reason") or "").strip() or "未达深读触发条件"
        lines.append(f"- 未触发深读：{reason}（不适用是合法出口，不是失败，不硬写解读）")
    elif not payload.get("applicable", True):
        reason = str(payload.get("reason") or "").strip() or "该话题不适合做社会议题解读"
        lines.append(f"- 深读不适用：{reason}（证据不足或话题不合适是合法出口，不为有输出而编）")
    else:
        text = str(payload.get("text") or "").strip()
        if text:
            shown = text if len(text) <= _DIGEST_TEXT_CAP else text[:_DIGEST_TEXT_CAP] + "…"
            lines.append("- 解读要点：")
            lines.append(shown)
        else:
            lines.append("- 深读已触发但没有产出文本（如实说明，不补写）")
        dropped = payload.get("guard_dropped") or []
        if dropped:
            lines.append(f"- 护栏丢弃 {len(dropped)} 条不合规条目（证据引用/禁词/归因校验）：")
            lines.extend(f"  · {str(d)}" for d in dropped[:3])
    notes = [n for n in _degraded_notes(payload) if n]
    if notes:
        lines.append("- 降级说明：" + "；".join(notes))
    lines.append("- 口径：解读为叙事内容（允许大模型参与），不改变关注度分数与任何结论，不构成真假判定。")
    if not stored:
        lines.append("- 章节落库失败：本结果未写入档案（其余信息如实如上）。")
    return "\n".join(lines)


def _deep_read(ctx: Any, id: int) -> Dict[str, Any]:  # noqa: A002  与路由参数名一致
    """deep_read：背景解读 → 写档案深读章节（读+写章节，§5.2 M3-5）。

    证据本复用/快照历史/opinions_signals 的装配在真实 deep_read 服务内部完成
    （见本文件顶部《deep_read 章节化调用方规格》）；本 handler 只消费其结果并落库。
    """
    exists, err = _dossier_exists(ctx, id)
    if err:
        return _result(False, f"档案库读取失败：{err}", None, True)
    if not exists:
        return _result(False, f"档案 #{id} 不存在，无法做背景解读。请先用 list_dossiers 确认档案号。",
                       None, False, not_found=True)
    try:
        payload = ctx.services["deep_read"](int(id))
    except KeyError:
        return _result(False, "深读服务未接入（services 缺少 deep_read 服务键）。",
                       int(id), True)
    except Exception as exc:  # noqa: BLE001
        return _result(False, f"深读分析失败：{type(exc).__name__}: {exc}", int(id), True)
    if not isinstance(payload, dict) or not payload:
        return _result(False, f"深读服务无输出（档案 #{id}）。", int(id), True)

    degraded = _degraded_of(payload)
    try:
        # 整包 payload 存 JSON：triggered/applicable/reason/guard_dropped 全持久化，
        # 未触发的记录也是章节的诚实状态（"评估过、不适用"）
        ctx.dossiers.record_deep_read(int(id), payload)
        stored = True
    except Exception as exc:  # noqa: BLE001
        stored = False
        record_error = f"{type(exc).__name__}: {exc}"
    else:
        record_error = ""

    digest = _deep_read_digest(int(id), payload, stored=stored)
    if record_error:
        digest += f"\n- 落库错误：{record_error}"
    active = bool(payload.get("triggered"))   # 触发了才调过大模型（模板回退也算一次尝试）
    return _result(True, digest, int(id), degraded or not stored,
                   requests=_REQUESTS_WHEN_ACTIVE if active else 0,
                   triggered=bool(payload.get("triggered")),
                   applicable=bool(payload.get("applicable", True)))


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册章节类工具（services 形参约定同 tools_verify.register_all）。"""
    registry.register("read_opinions", _read_opinions, params=["id"],
                      permission="write", cost="medium", est_s=25.0,
                      description="分析某档案证据本里的民间观点光谱（立场分布+论据可信度，写观点章节）",
                      validators={"id": id_param})
    registry.register("deep_read", _deep_read, params=["id"],
                      permission="write", cost="medium", est_s=30.0,
                      description="对某档案做背景解读（受控生成+护栏，复用档案证据本，写深读章节）",
                      validators={"id": id_param})
