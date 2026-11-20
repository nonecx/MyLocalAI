# -*- coding: utf-8 -*-
"""核查类工具封装（§5.2）：verify_claim / analyze_speech / recheck_dossier / search_web。

统一约定（编排者契约）：
  - handler 签名 handler(ctx, **args)，ctx 为 SimpleNamespace(sessions, dossiers,
    events, cancel_check, services)；服务一律经 ctx.services 取（注入优先、缺省惰性
    import 真实引擎），handler 自身不做任何真实网络/LLM 调用；
  - 返回统一 {"ok", "card", "digest", "cost": {"requests","tokens"}, "dossier_id",
    "degraded", + 业务字段}；digest 是给叙述层的结构化要点（进 build_context 的
    [本轮工具结果]），卡片字段全部来自确定性派生（大模型不参与打分）；
  - verify_claim / analyze_speech / recheck_dossier 经 DossierService 自动建档；
  - 永不抛异常：服务异常在 loop 层兜成 error 事件 + 诚实降级回复，本模块对
    "服务返回了坏形状" 也按降级处理而不是崩。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

from mylocalai.assistant.registry import ToolRegistry, ValidationError, text_param, deep_param, id_param
from mylocalai.assistant.tools_guard import dossier_visible

__all__ = ["register_all", "query_param"]

#: verify 类组件总数（SSE progress 的 total）：与 rumor.state.COMPONENTS 对齐；
#: import 失败时退 6（当前组件数），只影响进度条分母，不影响结论。
_FALLBACK_COMPONENT_TOTAL = 6

_EVIDENCE_PREVIEW = 5   # digest/卡片展开里证据本最多带前 5 条（§2.2 三层信息架构）

#: 言论分析卡里 classic 完整报告的展示上限：超过即截断并标 report_truncated=True。
#: 与 registry.MAX_TEXT_LENGTH 数值相同但语义不同（那是输入上限、这是输出展示上限），
#: 故单独定义——将来任一侧调整时不至于互相牵动。
_REPORT_MAX_CHARS = 8000


def query_param(value: Any) -> str:
    """search_web 的必填检索词（与 topic 同一长度上限口径：≤200 字）。"""
    from mylocalai.assistant.registry import MAX_TOPIC_LENGTH
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("参数 query 必须是非空字符串")
    value = value.strip()
    if len(value) > MAX_TOPIC_LENGTH:
        raise ValidationError(f"参数 query 过长（最多 {MAX_TOPIC_LENGTH} 字）")
    return value


def _component_total() -> int:
    try:
        from mylocalai.rumor.state import COMPONENTS
        return max(1, len(COMPONENTS))
    except Exception:  # noqa: BLE001  引擎结构变化时进度条退化为固定分母
        return _FALLBACK_COMPONENT_TOTAL


def _progress_emitter(events: Any) -> Optional[Callable[[str], None]]:
    """把 on_component_done 包装成 SSE progress 事件源（§5.3：组件完成顺序即事件顺序）。

    events 为 None（MCP/同步调用没有事件流）时返回 None，服务侧不启用回调——
    与 AnalysisState"不传回调行为逐位不变"的守卫契约一致。
    """
    if events is None:
        return None
    total = _component_total()
    state = {"done": 0}

    def _on_component_done(name: str) -> None:
        state["done"] += 1
        try:
            events.emit("progress", component=str(name), done=state["done"], total=total)
        except Exception:  # noqa: BLE001  进度事件失败不影响主结论
            pass
    return _on_component_done


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _evidence_preview(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """证据本前 N 条的紧凑视图（卡片展开层与 digest 共用）。"""
    book = result.get("evidence_book") or {}
    entries = book.get("entries") if isinstance(book, dict) else None
    out = []
    for e in (entries or [])[:_EVIDENCE_PREVIEW]:
        if isinstance(e, dict):
            out.append({"title": str(e.get("title") or "")[:60],
                        "domain": str(e.get("domain") or ""),
                        "source_kind": str(e.get("source_kind") or ""),
                        "stance": str(e.get("stance") or ""),
                        "url": str(e.get("url") or "")})
    return out


def _evidence_count(result: Dict[str, Any]) -> int:
    book = result.get("evidence_book") or {}
    entries = book.get("entries") if isinstance(book, dict) else None
    return len([e for e in (entries or []) if isinstance(e, dict)])


def _verify_digest(result: Dict[str, Any], card: Optional[Dict[str, Any]]) -> str:
    """verify 结果的叙述要点：标签/分数/置信度/as_of/证据分布/复审条件/证据前5条。"""
    lines: List[str] = ["[核查结论]"]
    label = str(result.get("label", ""))
    score = _as_float(result.get("risk_score"))
    lines.append(f"- 标签：{label or '（无）'}；风险分：{'暂无' if score is None else score:g}/100"
                 f"（verdict.assess() 纯函数输出，大模型不参与打分）")
    if card:
        lines.append(f"- 置信度：{card.get('confidence', '')}；证据截至：{card.get('as_of', '') or '未知'}")
        suff = card.get("evidence_sufficiency") or {}
        if suff:
            lines.append(f"- 证据充分度：官方 {suff.get('official', 0)} / 非官方 "
                         f"{suff.get('unofficial', 0)} / 未知 {suff.get('unknown', 0)}"
                         f"，共 {suff.get('total', 0)} 条")
        rc = card.get("recheck_conditions") or []
        if rc:
            lines.append("- 复审条件：" + "；".join(str(x) for x in rc))
    preview = _evidence_preview(result)
    if preview:
        lines.append(f"- 证据本前 {len(preview)} 条（共 {_evidence_count(result)} 条）：")
        for e in preview:
            lines.append(f"  · {e['title'] or e['domain']}（{e['source_kind'] or '来源未知'}，"
                         f"立场 {e['stance'] or '未知'}）")
    if result.get("agent_error"):
        lines.append(f"- 引擎降级说明：{result.get('agent_error')}")
    return "\n".join(lines)


def _legacy_claim_answer(original_text: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Expose a truthful transitional answer contract without fabricating v0.1 fields.

    The legacy RumorAgent does not provide verified public-propagation records,
    exact claim spans, or fetched-page quote locators. Its output therefore cannot
    satisfy the validated claim-answer contract and must be labeled incomplete.
    """
    claims = result.get("claims") if isinstance(result.get("claims"), list) else []
    claim_rows = []
    for index, item in enumerate(claims[:3], 1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("claim") or item.get("text") or "").strip()
        if not text:
            continue
        claim_rows.append({
            "claim_id": f"LEGACY-C{index}",
            "claim_excerpt": text[:120],
            "truth_assessment": None,
            "rumor_state": None,
            "evidence_status": "unvalidated_legacy_evidence",
            "propagation_status": "not_started",
            "limitations": [
                "旧核查引擎尚未提供可验证的传播记录、主张原文定位和页面引文锚点；"
                "本对象不是可展示的五态核查结论。"
            ],
        })
    return {
        "schema_version": 1,
        "answer_status": "insufficient_data",
        "checked_at": "",
        "claims": claim_rows,
        "limitations": ["legacy_verdict_not_mapped_to_truth_or_rumor_state"],
        "source_text_available_in_memory": bool(original_text),
    }


def _start_dossier(ctx: Any, text: str, input_kind: str = "text",
                   claim_excerpt: str = "") -> Optional[int]:
    """Build a dossier only when result saving is enabled; never turn refusal into failure.

    profile_id：落到会话所属的档案工作区（v6 多 Profile 分家）。ctx 未注入
    （stub/旧装配）时传 None，由 DossierService 取当前激活档案兜底。
    """
    try:
        return ctx.dossiers.start_dossier(text, input_kind=input_kind,
                                          claim_excerpt=claim_excerpt,
                                          profile_id=getattr(ctx, "profile_id", None))
    except Exception:  # noqa: BLE001
        return None


def _record(ctx: Any, dossier_id: Optional[int], result: Dict[str, Any], kind: str,
            duration_s: float, requests: int) -> Dict[str, Any]:
    """结论落库（record_analysis）；失败返回空记录，不抛。"""
    if dossier_id is None:
        return {}
    try:
        return ctx.dossiers.record_analysis(dossier_id, result, kind=kind,
                                            duration_s=duration_s,
                                            requests=requests, tokens=0) or {}
    except Exception:  # noqa: BLE001
        return {}


def _result(result: Any, card: Any, digest: str, cost: Dict[str, int],
            dossier_id: Optional[int], degraded: bool, **extra: Any) -> Dict[str, Any]:
    out = {"ok": True, "card": card, "digest": digest,
           "cost": {"requests": int(cost.get("requests", 0)), "tokens": int(cost.get("tokens", 0))},
           "dossier_id": dossier_id, "degraded": bool(degraded)}
    out.update(extra)
    return out


# ---------------------------------------------------------------- 工具实现

def _verify_claim(ctx: Any, text: str, deep: bool = False) -> Dict[str, Any]:
    """verify_claim：RumorAgent.analyze 全链路（30-120s 慢查）→ 自动建档 v1。"""
    services = ctx.services
    t0 = time.perf_counter()
    result = services["verify"](str(text), bool(deep),
                                on_component_done=_progress_emitter(ctx.events),
                                cancel_check=getattr(ctx, "cancel_check", None)) or {}
    duration = time.perf_counter() - t0
    if not isinstance(result, dict):
        result = {}
    degraded = bool(result.get("degraded_components") or result.get("agent_error"))
    # requests 口径：agent 返回的 llm_turns（M4 接 llm_client 计数包装后换真实值）
    requests = int(result.get("llm_turns") or 0)

    claims = result.get("claims") if isinstance(result.get("claims"), list) else []
    claim_excerpt = "；".join(
        str(item.get("claim") or item.get("text") or "").strip()[:120]
        for item in claims if isinstance(item, dict)
        and str(item.get("claim") or item.get("text") or "").strip())[:500]
    dossier_id = _start_dossier(ctx, str(text), claim_excerpt=claim_excerpt)
    rec = _record(ctx, dossier_id, result, kind="verify", duration_s=duration, requests=requests)
    card = rec.get("card")
    if isinstance(card, dict):
        # 卡片展开层（前端折叠展示用）：证据本前 5 条紧凑视图 + 总条数。只增展示字段，
        # 不改 core/card.py 纯函数的打分语义（卡片打分字段仍全是确定性派生）。
        card = dict(card)
        card["evidence_preview"] = _evidence_preview(result)
        card["evidence_total"] = _evidence_count(result)
    store_failed = bool(rec.get("degraded"))
    not_saved = dossier_id is None
    claim_answer = _legacy_claim_answer(str(text), result)
    digest = _verify_digest(result, card)
    # The new answer object is the only source the narrator may use for truth/rumor
    # wording. Legacy risk scores remain compatibility card data, not a verdict.
    answer_view = {
        "answer_status": claim_answer["answer_status"],
        "checked_at": claim_answer["checked_at"],
        "claims": [{
            "claim_id": item["claim_id"],
            "claim_excerpt": item["claim_excerpt"],
            "truth_assessment": item.get("truth_assessment"),
            "rumor_assessment": item.get("rumor_assessment"),
            "propagation_records": item.get("propagation_records", []),
            "key_evidence": item.get("key_evidence", []),
            "limitations": ((item.get("rumor_assessment") or {}).get("limitations", [])
                            if isinstance(item.get("rumor_assessment"), dict)
                            else item.get("limitations", [])),
        } for item in claim_answer.get("claims", [])],
        "limitations": claim_answer.get("limitations", []),
    }
    digest += "\n[claim_answer]\n" + json.dumps(answer_view, ensure_ascii=False)
    if not_saved:
        digest += "\n- 本次结果未保存到本机（保存偏好关闭或档案服务不可用）。"
    return _result(result, card, digest,
                   {"requests": requests, "tokens": 0}, dossier_id,
                   degraded or store_failed,
                   label=str(result.get("label", "")), risk_score=result.get("risk_score"),
                   version=rec.get("version"), batch=rec.get("batch"),
                   not_saved=not_saved,
                   save_reason="results_disabled_or_store_unavailable" if not_saved else "",
                    claim_answer=claim_answer)


def _analyze_speech(ctx: Any, text: str) -> Dict[str, Any]:
    """analyze_speech：classic judge（言论土壤四档）→ 自动建档。"""
    services = ctx.services
    t0 = time.perf_counter()
    result = services["speech"](str(text),
                                cancel_check=getattr(ctx, "cancel_check", None)) or {}
    duration = time.perf_counter() - t0
    if not isinstance(result, dict):
        result = {}
    degraded = bool(result.get("degraded_components"))

    speech_excerpt = str(result.get("conclusion") or "").strip()[:240]
    dossier_id = _start_dossier(ctx, str(text), claim_excerpt=speech_excerpt)
    rec: Dict[str, Any] = {}
    if dossier_id is not None:
        try:
            rec = ctx.dossiers.record_speech_analysis(dossier_id, result, duration_s=duration) or {}
        except Exception:  # noqa: BLE001
            rec = {}

    # 言论土壤卡：四档结论 + 三维摘要（cards.js renderSpeech 的字段口径）
    dims: Dict[str, Any] = {}
    for src, keys in (("emotion", ("emotion_tendency", "emotion_score")),
                      ("attack", ("attack_target", "aggression_level", "attack_type")),
                      ("logic", ("logic_relevance", "logic_fallacy"))):
        comp = result.get(src)
        if isinstance(comp, dict):
            dims[src] = {k: comp.get(k) for k in keys if k in comp}
    card = {"conclusion": str(result.get("conclusion", "")),
            "as_of": str((rec.get("card") or {}).get("as_of", "")),
            "degraded": degraded}
    card.update(dims)
    # classic 完整报告（前端折叠展开层）：报告是整段文本，超过展示上限就截断并如实
    # 标注 report_truncated=True——截断要看得见，不能让前端以为这就是全文。
    report = str(result.get("report") or "")
    if len(report) > _REPORT_MAX_CHARS:
        report = report[:_REPORT_MAX_CHARS]
        card["report_truncated"] = True
    card["report"] = report

    lines = ["[言论土壤]", f"- 四档结论：{result.get('conclusion', '')}（core/tiering 确定性分层）"]
    e = dims.get("emotion") or {}
    if e:
        lines.append(f"- 情绪：{e.get('emotion_tendency', '未知')}（强度 {_as_float(e.get('emotion_score')) or 0:g}/10）")
    a = dims.get("attack") or {}
    if a:
        lines.append(f"- 攻击性：{a.get('aggression_level', '未知')}，对象 {a.get('attack_target', '未知')}")
    lg = dims.get("logic") or {}
    if lg:
        lines.append(f"- 逻辑：关联度 {lg.get('logic_relevance', '未知')}，谬误 {lg.get('logic_fallacy', '未知')}")
    if degraded:
        lines.append("- 部分维度降级（组件结果缺失，如实标注）")
    digest = "\n".join(lines)
    not_saved = dossier_id is None
    if not_saved:
        digest += "\n- 本次言论分析结果未保存到本机（保存偏好关闭或档案服务不可用）。"
    return _result(result, card, digest, {"requests": 0, "tokens": 0}, dossier_id, degraded,
                   conclusion=str(result.get("conclusion", "")), version=rec.get("version"),
                   not_saved=not_saved,
                   save_reason="results_disabled_or_store_unavailable" if not_saved else "")


def _recheck_dossier(ctx: Any, id: int) -> Dict[str, Any]:  # noqa: A002  与路由参数名一致
    """recheck_dossier：读旧档案原文 → 重跑 verify → 同档案追加 v2/新证据批次。"""
    services = ctx.services
    visible, store_error = dossier_visible(ctx, id)
    if store_error:
        return _result({}, None, f"档案库读取失败：{store_error}",
                       {"requests": 0, "tokens": 0}, None, False, ok=False, degraded=True)
    if not visible:
        # 档案不存在**或不属于当前工作区**：同一种诚实回答，不泄露别的工作区
        return _result({}, None, f"档案 #{id} 不存在，无法复检。请先用 list_dossiers 确认档案号。",
                       {"requests": 0, "tokens": 0}, None, False, ok=False, not_found=True)

    try:
        row = ctx.dossiers.store.get_dossier(int(id))
    except Exception:  # noqa: BLE001
        row = None
    if not row:
        # 档案不存在是合法输入错误：诚实说没有，不猜、不新建
        return _result({}, None, f"档案 #{id} 不存在，无法复检。请先用 list_dossiers 确认档案号。",
                       {"requests": 0, "tokens": 0}, None, False, ok=False, not_found=True)

    full_input_saved = bool(str(row.get("input_text") or "").strip())
    text = str(row.get("input_text") or row.get("claim_excerpt") or "")
    if not text.strip():
        return _result({}, None,
                       f"档案 #{id} 未保存可用于复检的原始输入或主张摘录。"
                       "请重新提交原始说法发起一次新核查。",
                       {"requests": 0, "tokens": 0}, int(id), False,
                       ok=False, not_saved=True, reason="raw_input_unavailable")
    t0 = time.perf_counter()
    result = services["verify"](text, False, on_component_done=_progress_emitter(ctx.events),
                                cancel_check=getattr(ctx, "cancel_check", None)) or {}
    duration = time.perf_counter() - t0
    if not isinstance(result, dict):
        result = {}
    degraded = bool(result.get("degraded_components") or result.get("agent_error"))
    requests = int(result.get("llm_turns") or 0)

    rec = _record(ctx, int(id), result, kind="recheck", duration_s=duration, requests=requests)
    card = rec.get("card")

    # 版本对比摘要（§6 M3-2 的数据面，M2 先把事实带上：标签/分数变化 + 证据批次增减）
    compare_lines: List[str] = []
    try:
        cmp = ctx.dossiers.compare_versions(int(id)) or {}
        versions = cmp.get("versions") or []
        if len(versions) > 1:
            first, last = versions[0], versions[-1]
            compare_lines.append(
                f"- 版本：v{first.get('version')}（{first.get('label')}，"
                f"风险分 {first.get('risk_score')}）→ v{last.get('version')}"
                f"（{last.get('label')}，风险分 {last.get('risk_score')}）")
        batches = cmp.get("evidence_batches") or {}
        if batches:
            compare_lines.append("- 证据批次：" + "，".join(
                f"第{k}批 {v} 条" for k, v in sorted(batches.items(), key=lambda kv: int(kv[0]))))
    except Exception:  # noqa: BLE001
        pass

    lines = ["[复检结论]", _verify_digest(result, card)]
    if not full_input_saved:
        lines.append("- 本次仅基于已保存的最小主张摘录复检，未包含原始输入全文。")
    lines.extend(compare_lines)
    return _result(result, card, "\n".join(lines), {"requests": requests, "tokens": 0},
                   int(id), degraded,
                   label=str(result.get("label", "")), version=rec.get("version"),
                   batch=rec.get("batch"))


def _search_web(ctx: Any, query: str) -> Dict[str, Any]:
    """search_web：受控检索的只读薄封装（不建档、不写任何状态）。"""
    services = ctx.services
    raw = services["search"](str(query)) or {}
    if not isinstance(raw, dict):
        raw = {}
    items = [r for r in (raw.get("results") or []) if isinstance(r, (dict, object))
             and (isinstance(r, dict) or hasattr(r, "url"))]
    degraded = bool(raw.get("degraded"))

    def _field(r: Any, key: str) -> str:
        if isinstance(r, dict):
            return str(r.get(key) or "")
        return str(getattr(r, key, "") or "")

    lines = [f"[检索结果] 『{query}』 共 {len(items)} 条" + ("（检索降级）" if degraded else "")]
    for r in items[:_EVIDENCE_PREVIEW]:
        title = _field(r, "title") or "（无标题）"
        lines.append(f"· {title[:60]} — {_field(r, 'url')}")
    if not items:
        lines.append("· 未返回任何结果" + ("（后端不可用）" if degraded else ""))
    # 一次受控检索 = 1 次网络请求（多后端合并在检索服务内部）
    return _result(raw, None, "\n".join(lines), {"requests": 0 if degraded else 1, "tokens": 0},
                   None, degraded, results=len(items))


# ---------------------------------------------------------------- 注册

def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """把核查类工具注册进 registry。

    services 参数与 ctx.services 同源（loop 的注入盒）；M2 的 handler 统一运行期经
    ctx.services 取服务，这里保留形参是为了接口稳定与将来按服务可用性裁剪注册。
    """
    registry.register("verify_claim", _verify_claim, params=["text", "deep"],
                      permission="write", cost="heavy", est_s=60.0,
                      description="联网取证核实一条消息/说法的真实性（30-120s 慢查，自动建档）",
                      validators={"text": text_param(), "deep": deep_param})
    registry.register("analyze_speech", _analyze_speech, params=["text"],
                      permission="write", cost="medium", est_s=20.0,
                      description="分析一段言论的煽动性/情绪土壤（classic 四档，10-30s，自动建档）",
                      validators={"text": text_param()})
    registry.register("recheck_dossier", _recheck_dossier, params=["id"],
                      permission="write", cost="heavy", est_s=60.0,
                      description="把之前查过的档案再查一遍，追加新版本结论与新证据批次",
                      validators={"id": id_param})
    registry.register("search_web", _search_web, params=["query"],
                      permission="read", cost="light", est_s=5.0,
                      description="受控检索一段资料（只读，不建档）",
                      validators={"query": query_param})
