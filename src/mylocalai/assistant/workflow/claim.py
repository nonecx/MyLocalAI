# -*- coding: utf-8 -*-
"""claim-checking 六步执行器（规格 §3.2/§3.3 + core.ClaimCheckingPlan 的运行期接线）。

分工与铁律：
  - 判定语义不变：标签/风险分全部来自 verify_fn 内部的 verdict.assess 纯函数，本执行器
    不重算、不改判，只做计划状态推进与答案编译；
  - "不拼工具"：六步固定图来自 core.build_claim_checking_plan，模型无法增删必经步骤；
  - 每个出口都有降级：取证异常向上抛（loop 层既有 error 兜底成诚实降级回复）、传播
    失败/超预算/取消折 partial 级联不阻断、主张提取失败不冒充"无主张"（按单主张继续，
    verdict 兜底）；
  - 全离线可测：verify_fn / propagate_fn / state / 预算全部可注入 stub。

注入契约：
  verify_fn(text, *, deep, state=None, on_component_done=None, cancel_check=None) -> dict
      取证执行体（RumorAgent.analyze 结果形状）。波次 2 由 loop._real_service 的 _verify
      补 state 透传；tests 用 stub。
  propagate_fn(claims, *, deadline_s, cancel_check=None) -> dict
      传播取证包装（tools_verify 内包装 assistant.skills.propagation），返回
      {"status": completed|partial|failed|cancelled|budget_exhausted, "records": [...]}；
      records 只收过 validate_propagation_record 校验的传播记录（verified 记录须携带
      临时 page_text 供锚定复核，校验后剥除——与 compile_answer 的瞬态约定一致）。

commit_result 不做在本执行器内：建档（start_dossier/record_analysis）仍由 tools_verify
现有路径完成，本执行器只把计划状态与 step result_ref 填好——保持 tools_verify 契约与
loop 落库模式不变。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from mylocalai.core.claim_workflow import build_claim_checking_plan, compile_answer
from mylocalai.core.claim_checking import validate_propagation_record
from mylocalai.rumor.state import AnalysisState
from mylocalai.util.text_safety import sanitize_text

from mylocalai.assistant.workflow.plan import new_plan_id
from mylocalai.assistant.workflow.runner import aggregate_execution_status

__all__ = ["execute_claim_checking"]

#: 执行器内不上抛的可调用形状（仅供类型提示；测试注入 stub）。
VerifyFn = Callable[..., Dict[str, Any]]
PropagateFn = Callable[..., Dict[str, Any]]

#: 传播检索的预算份额：占计划 deadline 的固定比例，且封顶 30s——传播检索是补充面，
#: 不得挤占事实取证的主预算（规格 §3.3：总回合 deadline 约束子任务 deadline）。
_PROPAGATION_SHARE = 0.25
_PROPAGATION_CAP_S = 30.0

#: 主张提取的行数上限（与 rumor.claims._MAX_CLAIMS 同口径，防计划主张数失控）。
_MAX_PLAN_CLAIMS = 3

#: 计划级状态 → 执行轴（task_status 分轴独立：no_factual_claim 是任务出口，执行本身
#: 是"正常完成"——规格 §3.3 明确两轴不混用）。
_EXEC_ALIASES = {"no_factual_claim": "completed", "running": "partial"}


def _cancelled(cancel_check: Optional[Callable[[], bool]]) -> bool:
    """取消探测：与 loop/rumor 同一口径（探测自身抛异常不视为取消）。"""
    try:
        return bool(cancel_check()) if cancel_check is not None else False
    except Exception:  # noqa: BLE001
        return False


def _checked_at() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _budget_view(budget: Any) -> Dict[str, Any]:
    """budget 参数规整：dict（queries/pages/deadline_s/plan_id）或 None → 内置缺省。

    缺省与 core.build_claim_checking_plan 缺省及 assistant_rules["turn_deadline_s"]=180
    同口径（8 次检索 / 8 页 / 180 秒；见 workflow.plan.DEFAULT_BUDGET 的注释）。坏值一律
    退缺省，不让预算参数把核查打崩。
    """
    budget = budget if isinstance(budget, dict) else {}

    def _int_default(key: str, default: int) -> int:
        value = budget.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 50:
            return default
        return int(value)

    deadline = budget.get("deadline_s", 180.0)
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) \
            or not 0.0 < float(deadline) < 1e6:
        deadline = 180.0
    plan_id = budget.get("plan_id")
    return {"queries": _int_default("queries", 8), "pages": _int_default("pages", 8),
            "deadline_s": float(deadline),
            "plan_id": str(plan_id) if isinstance(plan_id, str) and plan_id.strip() else None}


def _state_funcs() -> Dict[str, Callable]:
    """classic 三维组件的入口层装配（与 loop._real_service 的 _verify 同参同源）。

    执行器自建共享状态时必须带上 classic 组件：否则共享给 RumorAgent.analyze 后，
    emotion/aggression/logic 三个组件会因"未装配"缺度（生产回归）。装配失败如实降级
    （AnalysisState.ensure 返回带 error 的结果，不崩）。测试可 patch 本函数注入替身。
    """
    try:
        from mylocalai.classic.language import analyze_aggression, analyze_emotion
        from mylocalai.classic.logic import analyze_logic
        return {"emotion": analyze_emotion,
                "aggression": analyze_aggression,
                "logic": analyze_logic}
    except Exception:  # noqa: BLE001  装配是增益面：取不到就按"组件未装配"诚实降级
        return {}


def _prepare_text(raw: str) -> Tuple[str, bool]:
    """与 RumorAgent.analyze 同一规整口径（sanitize + 上限截断），返回 (text, truncated)。

    共享 state 时文本一致性由调用方保证：这里用与生产装配（RumorAgent(max_chars=
    assistant.registry.MAX_TEXT_LENGTH)）相同的上限截断，保证执行器自建状态与 analyze
    内部看到同一份文本。
    """
    text = sanitize_text(str(raw or "")).strip()
    try:
        from mylocalai.assistant.registry import MAX_TEXT_LENGTH
        limit = int(MAX_TEXT_LENGTH)
    except Exception:  # noqa: BLE001  取不到上限就不截断（与 analyze 的装配缺省一致）
        return text, False
    return text[:limit], len(text) > limit


def _claim_texts(claims_result: Any) -> List[str]:
    """主张提取结果 → 计划用主张文本列表（最多 3 条，与 rumor.claims 同口径）。"""
    if not isinstance(claims_result, dict):
        return []
    rows = claims_result.get("claims")
    if not isinstance(rows, list):
        return []
    out: List[str] = []
    for row in rows[:_MAX_PLAN_CLAIMS]:
        if isinstance(row, dict):
            text = str(row.get("claim") or row.get("text") or "").strip()
            if text:
                out.append(text[:120])
    return out


def _span_anchored(verify_result: Dict[str, Any]) -> bool:
    """verify 结果的主张是否带原文定位（source_span）——旧引擎没有，assess 步如实标注。"""
    claims = verify_result.get("claims")
    rows = [row for row in claims if isinstance(row, dict)] if isinstance(claims, list) else []
    return bool(rows) and all(isinstance(row.get("source_span"), dict) for row in rows)


def _validated_records(raw_records: Any, claim_ids: List[str]) -> Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    """只收过 validate_propagation_record 校验的传播记录（规格：搜索摘要不能升级成传播
    事实）。返回 (剥离 page_text 的干净记录, 丢弃数, 保留原样的瞬态记录)——瞬态记录供
    compile_answer 复核锚点用（它自己再校验并剥 page_text）。

    verified 记录须携带临时 page_text；assistant.skills.propagation 现行实现核验后在
    返回值里丢弃 page_text，这类记录按"不可二次锚定"如实不计入已核验传播（宁缺毋假），
    由 dropped_unvalidated 计数暴露——transport 返回 page_text 后自动接入。
    """
    clean: List[Dict[str, Any]] = []
    transient: List[Dict[str, Any]] = []
    dropped = 0
    for record in (raw_records or []):
        if not isinstance(record, dict):
            dropped += 1
            continue
        page_text = record.get("page_text")
        try:
            validated = validate_propagation_record(
                record, claim_ids, page_text if isinstance(page_text, str) else None)
        except ValueError:
            dropped += 1
            continue
        transient.append(dict(record))
        validated.pop("page_text", None)
        clean.append(validated)
    return clean, dropped, transient


def _prop_summary(status: str, records: List[Dict[str, Any]], dropped: int,
                  reason_code: str) -> Dict[str, Any]:
    return {"status": str(status or "not_started"), "records": list(records),
            "verified_count": sum(1 for row in records if row.get("status") == "verified"),
            "dropped_unvalidated": int(dropped), "reason_code": str(reason_code or "")}


def _legacy_answer(original_text: str, verify_result: Dict[str, Any],
                   claim_ids: List[str],
                   propagation_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """旧引擎过渡答案对象（与 tools_verify._legacy_claim_answer 同形状：无 span/引文锚点
    就不冒充五态结论）。过校验的传播记录按计划 claim 序并入对应行的 propagation_records
    字段；has_incomplete_claims 恒 True（旧引擎答案永远不完整，如实标注）。"""
    claims = verify_result.get("claims")
    claims = claims if isinstance(claims, list) else []
    by_claim: Dict[str, List[Dict[str, Any]]] = {}
    for record in propagation_records:
        if isinstance(record, dict):
            for cid in (record.get("claim_ids") or []):
                by_claim.setdefault(str(cid), []).append(record)
    rows: List[Dict[str, Any]] = []
    for index, item in enumerate(claims[:_MAX_PLAN_CLAIMS], 1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("claim") or item.get("text") or "").strip()
        if not text:
            continue
        plan_cid = claim_ids[index - 1] if index - 1 < len(claim_ids) else ""
        rows.append({
            "claim_id": f"LEGACY-C{index}",
            "claim_excerpt": text[:120],
            "truth_assessment": None,
            "rumor_state": None,
            "evidence_status": "unvalidated_legacy_evidence",
            "propagation_status": "not_started",
            "propagation_records": [
                {key: record.get(key) for key in
                 ("propagation_record_id", "url", "source_id", "status", "quote",
                  "published_at", "verified_at")}
                for record in by_claim.get(plan_cid, [])],
            "limitations": [
                "旧核查引擎尚未提供可验证的传播记录、主张原文定位和页面引文锚点；"
                "本对象不是可展示的五态核查结论。"],
        })
    return {"schema_version": 1, "answer_status": "insufficient_data", "checked_at": "",
            "claims": rows, "has_incomplete_claims": True,
            "limitations": ["legacy_verdict_not_mapped_to_truth_or_rumor_state"],
            "source_text_available_in_memory": bool(original_text)}


def _execution_so_far(plan: Any) -> str:
    """validate_answer 时点的执行轴：取已完结步骤状态的归并（compile_answer 的
    execution_status 决定 assessment_status 能否到 validated——此刻 assess/validate/
    commit 还没跑完，plan.status 仍是 running，不能拿它当执行轴，否则什么都没缺也
    会被打成 partial）。not_run（required 失败级联）视为 partial。"""
    statuses = [str(step.get("status") or "") for step in plan.steps.values()]
    if any(status == "not_run" for status in statuses):
        return "partial"
    finished = [status for status in statuses if status not in {"pending", "running"}]
    return aggregate_execution_status(finished)


def _try_compile(original_text: str, verify_result: Dict[str, Any],
                 claim_ids: List[str], transient_records: List[Dict[str, Any]],
                 prop_status: str, plan: Any) -> Optional[Dict[str, Any]]:
    """新引擎契约路径：verify 结果带齐 source_span/evidence 锚点/truth_assessments/
    assessor_ids 时走 core.compile_answer；缺料或编译器拒收（引用不锚定/字段缺失）→
    None（调用方退回诚实过渡对象），绝不硬凑五态结论。"""
    claims = verify_result.get("claims")
    evidence = verify_result.get("evidence")
    assessments = verify_result.get("truth_assessments")
    assessor_ids = verify_result.get("assessor_ids")
    if not (isinstance(claims, list) and claims and isinstance(evidence, list) and evidence
            and isinstance(assessments, dict) and assessments
            and isinstance(assessor_ids, dict) and assessor_ids):
        return None
    execution = _execution_so_far(plan)
    if execution not in {"completed", "partial", "failed", "cancelled", "budget_exhausted"}:
        execution = "partial"
    # 传播/事实检索状态：新引擎显式给映射就用；缺省按编排事实推导（传播未跑/未授权
    # 不冒充 completed——derive_rumor_state 只认 EXECUTION_STATUSES）。
    prop_default = {"completed": "completed", "partial": "partial", "failed": "failed",
                    "cancelled": "cancelled", "budget_exhausted": "budget_exhausted"}.get(
                        prop_status, "")
    if not prop_default:
        return None
    fact_default = "completed" if verify_result.get("label") else "partial"
    propagation_status = verify_result.get("propagation_status")
    fact_search_status = verify_result.get("fact_search_status")
    per_prop = {cid: ((propagation_status or {}).get(cid, prop_default)
                      if isinstance(propagation_status, dict) else prop_default)
                for cid in claim_ids}
    per_fact = {cid: ((fact_search_status or {}).get(cid, fact_default)
                      if isinstance(fact_search_status, dict) else fact_default)
                for cid in claim_ids}
    try:
        return compile_answer(
            original_text=original_text, claims=claims, evidence=evidence,
            propagation_records=transient_records,
            truth_assessments=assessments,
            propagation_status=per_prop, fact_search_status=per_fact,
            checked_at=_checked_at(), execution_status=execution,
            independent_reviews=(verify_result.get("independent_reviews") or None),
            assessor_ids=assessor_ids,
            reviewer_ids=(verify_result.get("reviewer_ids") or None),
            force_review_claim_ids=(verify_result.get("force_review_claim_ids") or ()))
    except (ValueError, TypeError, KeyError):
        return None


def _no_claim_verdict(state: AnalysisState) -> Dict[str, Any]:
    """无主张短出口的判定：组件已按 compute-once 预取，verdict.assess 纯函数汇总
    （与 analyze 的 not_factual 出口同源，打分语义零改动）。langchain 不可用时退
    verdict 常量的最小诚实形状。"""
    try:
        from mylocalai.rumor.tools import compute_verdict
        return compute_verdict(state, allow_compute=True)
    except Exception:  # noqa: BLE001  引擎结构变化的兜底形状（标签仍取 verdict 常量）
        from mylocalai.rumor.verdict import ADVICE, LABEL_NO_CLAIM
        return {"label": LABEL_NO_CLAIM, "risk_score": 0, "confidence": "高",
                "speech_tier": "", "has_factual_claim": False,
                "claim_basis": "大模型提取", "claims": [], "evidence_hint": "",
                "score_breakdown": {}, "signals": [], "degraded_components": [],
                "advice": ADVICE[LABEL_NO_CLAIM]}


def execute_claim_checking(text: str, *, deep: bool, verify_fn: VerifyFn,
                           propagate_fn: PropagateFn, dossiers: Any = None,
                           profile_id: Optional[int] = None, cancel_check: Optional[Callable[[], bool]] = None,
                           on_step: Optional[Callable[[str, str], None]] = None,
                           on_component_done: Optional[Callable[[str], None]] = None,
                           state: Optional[AnalysisState] = None,
                           budget: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """claim-checking 六步执行。返回值 = verify_fn 结果 dict 原样（既有键一个不删）
    + 追加 plan / claim_answer / propagation / execution_status / task_status / elapsed_s。

    dossiers/profile_id 为保留参数：commit_result 步的落库仍由调用方（tools_verify 现有
    建档路径）完成；后续波次把 step result_ref 落库时使用。
    """
    t0 = time.perf_counter()
    raw_text = str(text or "")
    cfg = _budget_view(budget)
    plan_id = cfg["plan_id"] or new_plan_id("claim-checking")

    # ---- 共享状态：注入复用（compute-once，调用方保证文本一致）；否则按生产装配自建 ----
    truncated = False
    if state is None:
        prepared, truncated = _prepare_text(raw_text)
        state = AnalysisState(prepared, funcs=_state_funcs())
    prepared_text = str(getattr(state, "text", "") or "")

    def _emit(step_id: str, status: str) -> None:
        if on_step is not None:
            try:
                on_step(step_id, status)
            except Exception:  # noqa: BLE001  事件回调绝不影响执行本身
                pass

    # ① extract_claims：claims/markers 经共享状态 compute-once 预取（analyze 复用不重算）。
    claims_result = state.ensure("claims")
    state.ensure("markers")
    claim_texts = _claim_texts(claims_result)
    has_claim = claims_result.get("has_factual_claim") if isinstance(claims_result, dict) else None

    # 主张提取明确判定"无事实主张"→ 诚实短出口（与现有 not_factual 出口一致），不调取证。
    # 提取失败/未能判断（has_factual_claim=None）≠ 无主张：按单主张继续，verdict 兜底——
    # 绝不把"提取失败"说成"没有谣言"。
    if has_claim is False:
        return _no_claim_exit(state, raw_text, prepared_text, truncated, plan_id,
                              t0, _emit)

    claim_ids = [f"C{index}" for index, _ in enumerate(claim_texts, 1)] or ["C0"]
    plan = build_claim_checking_plan(plan_id, claim_ids, query_budget=cfg["queries"],
                                     page_budget=cfg["pages"], deadline_s=cfg["deadline_s"])
    verify_result: Dict[str, Any] = {}
    prop_records: List[Dict[str, Any]] = []
    transient_records: List[Dict[str, Any]] = []
    prop_status, prop_dropped, prop_reason = "not_started", 0, ""
    claim_answer: Dict[str, Any] = _legacy_answer(raw_text, verify_result, claim_ids, [])

    def _finalize() -> Dict[str, Any]:
        out = dict(verify_result)
        out["plan"] = {**plan.to_dict(), "plan_id": plan_id}
        out["claim_answer"] = claim_answer
        out["propagation"] = _prop_summary(prop_status, prop_records, prop_dropped, prop_reason)
        out["execution_status"] = _EXEC_ALIASES.get(plan.status, plan.status)
        out["task_status"] = "no_factual_claim" if plan.status == "no_factual_claim" else ""
        out["elapsed_s"] = round(time.perf_counter() - t0, 1)
        return out

    # ---- ① extract_claims ----
    plan.start_step("extract_claims")
    plan.finish_step("extract_claims", "completed", result_ref="state:claims",
                     reason_code="claims_extracted" if claim_texts else "claims_undetermined")
    _emit("extract_claims", "completed")

    # ---- ② collect_fact_evidence：整包保留 verify_fn 结果（卡片/verdict/解释全来自它）----
    if _cancelled(cancel_check):
        plan.start_step("collect_fact_evidence")
        plan.finish_step("collect_fact_evidence", "cancelled", reason_code="user_cancelled")
        _emit("collect_fact_evidence", "cancelled")
        return _finalize()
    plan.start_step("collect_fact_evidence")
    try:
        verify_result = verify_fn(raw_text, deep=bool(deep), state=state,
                                  on_component_done=on_component_done,
                                  cancel_check=cancel_check)
    except Exception:
        # 取证异常原样上抛：loop 层既有 error 事件 + 诚实降级回复兜底（契约不因执行器改变）。
        plan.finish_step("collect_fact_evidence", "failed", reason_code="verify_fn_error")
        raise
    if not isinstance(verify_result, dict):
        verify_result = {}
    if _cancelled(cancel_check) or str(verify_result.get("mode") or "") == "cancelled":
        plan.finish_step("collect_fact_evidence", "cancelled", result_ref="verify:partial",
                         reason_code="user_cancelled")
        _emit("collect_fact_evidence", "cancelled")
        claim_answer = _legacy_answer(raw_text, verify_result, claim_ids, [])
        return _finalize()
    if verify_result.get("label"):
        plan.finish_step("collect_fact_evidence", "completed", result_ref="verify:result",
                         reason_code="verdict_ready")
    else:
        plan.finish_step("collect_fact_evidence", "partial", result_ref="verify:result",
                         reason_code="verdict_missing")
    _emit("collect_fact_evidence", plan.steps["collect_fact_evidence"]["status"])

    # ---- ③ collect_propagation：授权闸 fail-closed；失败/超预算/取消折 partial 不阻断 ----
    if _cancelled(cancel_check):
        plan.start_step("collect_propagation")
        plan.finish_step("collect_propagation", "cancelled", reason_code="user_cancelled")
        _emit("collect_propagation", "cancelled")
        claim_answer = _legacy_answer(raw_text, verify_result, claim_ids, [])
        return _finalize()
    policy = _external_policy()
    authorized = policy is not None and (policy.external_allowed("public-search")
                                         and policy.external_allowed("public-web"))
    if not authorized:
        # 未授权零外发（规格 §1.9/§7）：该步不发生 + 计划继续，绝不发起检索。
        # 步骤状态用 not_triggered（"条件未触发"）：核心计划机的依赖闸（start_step 只认
        # completed/partial/not_triggered）不允许 not_applicable 后还继续推进 assess——
        # not_triggered 是机器合法且语义一致的落点；传播摘要里仍如实记 not_applicable。
        # 不走 finish_step：not_applicable/not_triggered 属"未发生"态，与 plan.__post_init__
        # 的 no_factual_claim 收口同法直接落步骤状态并重算聚合。
        plan.steps["collect_propagation"].update(
            status="not_triggered", reason_code="external_processing_disabled")
        plan._aggregate()
        _emit("collect_propagation", "not_triggered")
        prop_status, prop_reason = "not_applicable", "external_processing_disabled"
    else:
        plan.start_step("collect_propagation")
        share = max(1.0, min(_PROPAGATION_CAP_S, cfg["deadline_s"] * _PROPAGATION_SHARE))
        search_claims = [{"claim_id": cid, "text": claim_text} for cid, claim_text
                         in zip(claim_ids, claim_texts or [prepared_text[:120]])]
        try:
            prop = propagate_fn(search_claims, deadline_s=share, cancel_check=cancel_check)
        except Exception as exc:  # noqa: BLE001  传播层任何异常都折成失败，不挡核查主线
            prop = {"status": "failed", "records": [],
                    "reason_code": f"propagation_error:{type(exc).__name__}"}
        if not isinstance(prop, dict):
            prop = {"status": "failed", "records": [], "reason_code": "propagation_bad_shape"}
        prop_status = str(prop.get("status") or "failed")
        prop_reason = str(prop.get("reason_code") or f"propagation_{prop_status}")
        prop_records, prop_dropped, transient_records = _validated_records(
            prop.get("records"), claim_ids)
        step_status = {"completed": "completed", "partial": "partial",
                       "budget_exhausted": "partial", "failed": "partial",
                       "cancelled": "cancelled"}.get(prop_status, "partial")
        plan.finish_step("collect_propagation", step_status, result_ref="propagation:records",
                         reason_code=prop_reason)
        _emit("collect_propagation", step_status)
        if step_status == "cancelled":
            claim_answer = _legacy_answer(raw_text, verify_result, claim_ids, prop_records)
            return _finalize()

    # ---- ④ assess_claims：verdict 已由 verify_fn（纯函数）给出，不重算、不改判 ----
    plan.start_step("assess_claims")
    plan.finish_step("assess_claims", "completed", result_ref="verify:verdict",
                     reason_code="verdict_reused" if _span_anchored(verify_result)
                     else "verdict_reused_no_span_anchor")
    _emit("assess_claims", "completed")

    # ---- ⑤ validate_answer：compile_answer 优先，旧引擎退诚实过渡对象（同形状） ----
    plan.start_step("validate_answer")
    compiled = _try_compile(raw_text, verify_result, claim_ids, transient_records,
                            prop_status, plan)
    if compiled is not None:
        claim_answer = compiled
        validate_reason = "compiled_answer"
    else:
        claim_answer = _legacy_answer(raw_text, verify_result, claim_ids, prop_records)
        validate_reason = "legacy_transitional_answer"
    plan.finish_step("validate_answer", "completed", result_ref="answer:compiled",
                     reason_code=validate_reason)
    _emit("validate_answer", "completed")

    # ---- ⑥ commit_result：落库由调用方完成，本步只登记计划状态（见模块 docstring） ----
    plan.start_step("commit_result")
    plan.finish_step("commit_result", "completed", reason_code="committed_by_caller")
    _emit("commit_result", "completed")

    return _finalize()


def _no_claim_exit(state: AnalysisState, raw_text: str, prepared_text: str,
                   truncated: bool, plan_id: str, t0: float,
                   _emit: Callable[[str, str], None]) -> Dict[str, Any]:
    """诚实短出口（claims 明确为空 → plan 自动 no_factual_claim，不调取证）。

    classic 三维 + evidence 组件按 compute-once 补齐（与 analyze 的 _fill_missing 同一口径；
    evidence 对无主张文本主动跳过、零外发），verdict.assess 纯函数兜底出与现有
    not_factual 出口一致的结论形状——tools_verify 的卡片/digest/建档路径照常工作。
    """
    plan = build_claim_checking_plan(plan_id, [])
    for name in ("emotion", "aggression", "logic", "evidence"):
        state.ensure(name)
    verdict = _no_claim_verdict(state)
    ev = state.peek("evidence")
    evidence_book = None
    if ev:
        evidence_book = {key: ev.get(key) for key in
                         ("claim", "queries", "entries", "skipped", "error",
                          "scope", "scope_event", "scope_filter", "scope_reason",
                          "scope_anchors", "excluded_not_event",
                          "stance_summary", "filtered_out", "verdict_hint", "llm_error")}
    out: Dict[str, Any] = {
        **verdict,
        "emotion": state.peek("emotion") or {},
        "attack": state.peek("aggression") or {},
        "logic": state.peek("logic") or {},
        "evidence_book": evidence_book,
        "explanation": str(verdict.get("advice") or ""),
        "explanation_source": "template",
        "mode": "pipeline",
        "agent_error": "",
        "tools_called": [],
        "llm_turns": 0,
        "component_timings": dict(state.timings),
        "component_spans": dict(state.spans),
        "truncated": truncated,
        "text_preview": prepared_text[:60],
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }
    out["plan"] = {**plan.to_dict(), "plan_id": plan_id}
    out["claim_answer"] = {
        "schema_version": 1, "answer_status": "insufficient_data",
        "checked_at": _checked_at(), "execution_status": "completed",
        "task_status": "no_factual_claim", "claims": [], "has_incomplete_claims": False,
        "limitations": ["文本没有可被核实的事实性声明；未发起联网取证。"],
        "source_text_available_in_memory": bool(raw_text),
    }
    out["propagation"] = _prop_summary("not_applicable", [], 0, "no_factual_claim")
    out["task_status"] = "no_factual_claim"
    out["execution_status"] = "completed"
    # 六步状态在 plan.__post_init__ 已按 no_factual_claim 收口（completed/not_applicable），
    # 这里只补事件回调，供上层按需呈现。
    for step_id, step in plan.steps.items():
        _emit(step_id, str(step.get("status") or ""))
    return out


def _external_policy():
    """外发授权快照（fail-closed：读不到偏好按未授权处理，绝不放行外发）。"""
    try:
        from mylocalai.core.privacy import load_preferences
        return load_preferences()
    except Exception:  # noqa: BLE001  偏好读取故障 = 不授权（规格 §1.9 默认关闭）
        return None
