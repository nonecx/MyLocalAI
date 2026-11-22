# -*- coding: utf-8 -*-
"""claim-checking 六步执行器测试（workflow/claim.py，全 stub 离线）。

锁定：
  - 空主张 → no_factual_claim 诚实短出口（不调 verify_fn，零取证）；
  - 正常路径六步状态推进 + 返回含 plan/claim_answer/propagation 且既有键齐全；
  - 传播授权闸 fail-closed（未授权 → 该步不发生 + 计划继续 + 零调用）；
  - 传播失败/取消 → partial 级联不阻断（核查结论照常产出）；
  - state 共享（verify_fn 收到同一 AnalysisState 对象，claims/markers 不重算）；
  - 新引擎契约（claims 带 source_span + evidence 锚点）→ compile_answer 编译路径。
"""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.workflow.claim import execute_claim_checking  # noqa: E402
from mylocalai.rumor.state import AnalysisState  # noqa: E402

CLAIM_TEXT = "自来水被人下毒了"
ORIGINAL_TEXT = f"网传{CLAIM_TEXT}，官方根本不敢说！速转！"


def _state(claims: dict, markers: dict | None = None) -> AnalysisState:
    """注入共享状态的测试替身：claims/markers 用 stub（零 LLM），其余组件不装配
    （ensure 如实返回"组件未装配"，等价离线降级）。"""
    funcs = {"claims": lambda text: claims}
    if markers is not None:
        funcs["markers"] = lambda text: markers
    return AnalysisState(ORIGINAL_TEXT, funcs)


def _markers() -> dict:
    """scan_markers 的最小形状（verdict.assess 只读这些键）。"""
    return {"risk_hits": {}, "credibility_hits": {}, "marker_points": 0,
            "credibility_points": 0, "assertion_signal": True}


def _verified_record(page_text: str = "官方通报：自来水被人下毒了属于不实信息，请勿传播。",
                     claim_id: str = "C1") -> dict:
    """过 validate_propagation_record 校验的已核验传播记录（verified 需携带临时 page_text
    供执行器锚定复核，校验后剥除）。"""
    quote = "自来水被人下毒了属于不实信息"
    start = page_text.index(quote)
    return {"propagation_record_id": "P-1", "claim_ids": [claim_id],
            "url": "https://gov.example/post", "source_id": "gov.example",
            "status": "verified", "quote": quote,
            "quote_span": {"start": start, "end": start + len(quote)},
            "published_at": "2026-11-01", "verified_at": "2026-11-22T08:00:00+08:00",
            "page_text": page_text}


def _privacy(external: bool):
    """可控隐私偏好：external=False 即缺省 fail-closed（外发全关）。"""
    from mylocalai.core.privacy import DEFAULT_PREFERENCES, PrivacyPreferences
    data = json.loads(json.dumps(DEFAULT_PREFERENCES))
    data["external_processing_enabled"] = external
    data["external_processing_providers"] = ["public-search", "public-web"] if external else []
    return PrivacyPreferences(data)


@contextmanager
def _unauthorized():
    with mock.patch("mylocalai.core.privacy.load_preferences",
                    return_value=_privacy(False)):
        yield


@contextmanager
def _authorized():
    with mock.patch("mylocalai.core.privacy.load_preferences",
                    return_value=_privacy(True)):
        yield


def _run(**kwargs) -> dict:
    defaults = dict(
        text=ORIGINAL_TEXT, deep=False,
        verify_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 verify_fn")),
        propagate_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 propagate_fn")),
        dossiers=None, profile_id=None)
    defaults.update(kwargs)
    return execute_claim_checking(**defaults)


class NoClaimShortExitTests(unittest.TestCase):
    """诚实短出口：claims 明确为空 → no_factual_claim，不调取证。"""

    def test_no_factual_claim_skips_verification(self):
        """has_factual_claim=False → plan 自动 no_factual_claim，verify_fn/propagate_fn
        零调用，返回形状与现有 not_factual 出口一致（label/claim_answer/propagation）。"""
        seen = []
        state = _state({"has_factual_claim": False, "claims": [], "unsourced_unverifiable": 0},
                       markers=_markers())
        out = _run(state=state,
                   verify_fn=lambda *a, **k: seen.append(1) or {},
                   propagate_fn=lambda *a, **k: seen.append(2) or {})
        self.assertEqual(seen, [])   # 铁律：无主张不取证
        self.assertEqual(out["label"], "无可核实陈述")
        self.assertFalse(out["has_factual_claim"])
        self.assertEqual(out["plan"]["status"], "no_factual_claim")
        self.assertTrue(out["plan"]["plan_id"].startswith("claim-checking:"))
        self.assertEqual(out["plan"]["steps"]["collect_fact_evidence"]["status"],
                         "not_applicable")
        self.assertEqual(out["claim_answer"]["task_status"], "no_factual_claim")
        self.assertEqual(out["claim_answer"]["claims"], [])
        self.assertEqual(out["propagation"]["status"], "not_applicable")
        self.assertEqual(out["task_status"], "no_factual_claim")
        self.assertEqual(out["execution_status"], "completed")

    def test_failed_extraction_is_not_claimed_as_no_claim(self):
        """提取失败（has_factual_claim=None）≠ 无主张：按单主张继续取证（verdict 兜底），
        绝不把"提取失败"说成"没有谣言"。"""
        calls = []
        state = _state({"has_factual_claim": None, "claims": [], "error": "上游抽风"})
        out = _run(state=state,
                   verify_fn=lambda text, *, deep, state=None, on_component_done=None,
                   cancel_check=None: calls.append(text) or
                   {"label": "存疑待核实", "risk_score": 30, "claims": [],
                    "degraded_components": [], "agent_error": "", "llm_turns": 0,
                    "evidence_book": {"entries": []}},
                   propagate_fn=lambda *a, **k: {"status": "completed", "records": []})
        self.assertEqual(calls, [ORIGINAL_TEXT])
        self.assertNotEqual(out["plan"]["status"], "no_factual_claim")
        self.assertEqual(out["plan"]["claim_ids"], ["C0"])
        self.assertEqual(out["task_status"], "")


def _verify_stub(calls: list, label: str = "高风险谣言"):
    """模块级 verify_fn 替身：记录调用并返回 verdict 形状的完整结果（旧引擎形状）。"""
    def _verify(text, *, deep=False, state=None, on_component_done=None,
                cancel_check=None):
        calls.append({"text": text, "deep": deep, "state": state})
        return {"label": label, "risk_score": 88, "claims": [{"claim": CLAIM_TEXT}],
                "signals": ["来源模糊"], "degraded_components": [], "agent_error": "",
                "llm_turns": 3, "evidence_book": {"entries": []}}
    return _verify


class NormalPathTests(unittest.TestCase):
    """正常路径：六步推进、既有键齐全、传播记录并入。"""

    def test_six_steps_advance_and_legacy_keys_survive(self):
        """六步全 completed；返回 = verify 结果原样 + plan/claim_answer/propagation 追加键。"""
        calls = []
        state = _state({"has_factual_claim": True,
                        "claims": [{"claim": CLAIM_TEXT, "has_source": False,
                                    "verifiability": "无法核实"}],
                        "unsourced_unverifiable": 1},
                       markers=_markers())
        with _authorized():
            out = _run(state=state, deep=True,
                       verify_fn=_verify_stub(calls),
                       propagate_fn=lambda claims, *, deadline_s, cancel_check=None:
                           {"status": "completed", "records": []})
        # 六步状态推进（固定图）
        steps = out["plan"]["steps"]
        self.assertEqual([step["status"] for step in steps.values()],
                         ["completed"] * 6)
        self.assertEqual(steps["commit_result"]["reason_code"], "committed_by_caller")
        self.assertEqual(out["plan"]["status"], "completed")
        # state 共享：verify_fn 收到同一对象（claims/markers compute-once 不重算）
        self.assertIs(calls[0]["state"], state)
        self.assertTrue(calls[0]["deep"])
        # 既有键一个不删（数据层留档）
        for key in ("label", "risk_score", "claims", "signals", "llm_turns", "evidence_book"):
            self.assertIn(key, out)
        # 追加键：plan/claim_answer/propagation + 两轴
        self.assertEqual(out["plan"]["claim_ids"], ["C1"])
        self.assertEqual(out["execution_status"], "completed")
        self.assertEqual(out["task_status"], "")
        # 旧引擎无 span → claim_answer 是既有诚实过渡形状（契约不变）
        answer = out["claim_answer"]
        self.assertEqual(answer["answer_status"], "insufficient_data")
        self.assertEqual(len(answer["claims"]), 1)
        self.assertIsNone(answer["claims"][0]["rumor_state"])
        self.assertIn("risk_score", out)            # 数据层留档（digest/卡片层裁剪）
        self.assertNotIn("risk_score", json.dumps(answer, ensure_ascii=False))

    def test_verified_propagation_record_merges_into_legacy_rows(self):
        """过校验的 verified 传播记录并入对应 claim 行的 propagation_records；
        未携带 page_text 的 verified 记录按"不可二次锚定"如实丢弃并计数。"""
        anchored = _verified_record()                       # 带临时 page_text → 收
        unanchored = dict(_verified_record(), page_text=None)  # 无 page_text → 丢
        state = _state({"has_factual_claim": True,
                        "claims": [{"claim": CLAIM_TEXT}], "unsourced_unverifiable": 0},
                       markers=_markers())
        with _authorized():
            out = _run(state=state,
                       verify_fn=_verify_stub([]),
                       propagate_fn=lambda claims, *, deadline_s, cancel_check=None:
                           (self.assertEqual([c["claim_id"] for c in claims], ["C1"]),
                            {"status": "completed",
                             "records": [anchored, unanchored]})[-1])
        propagation = out["propagation"]
        self.assertEqual(propagation["status"], "completed")
        self.assertEqual(propagation["verified_count"], 1)
        self.assertEqual(propagation["dropped_unvalidated"], 1)
        self.assertNotIn("page_text", json.dumps(propagation["records"]))   # 瞬态字段已剥
        answer_rows = out["claim_answer"]["claims"]
        self.assertEqual(len(answer_rows[0]["propagation_records"]), 1)
        self.assertEqual(answer_rows[0]["propagation_records"][0]["url"],
                         "https://gov.example/post")

    def test_propagation_failure_degrades_to_partial_without_blocking(self):
        """传播失败 → 步骤 partial 级联不阻断：计划继续、结论照常、计划级 partial。"""
        state = _state({"has_factual_claim": True,
                        "claims": [{"claim": CLAIM_TEXT}], "unsourced_unverifiable": 0},
                       markers=_markers())
        with _authorized():
            out = _run(state=state,
                       verify_fn=_verify_stub([]),
                       propagate_fn=lambda claims, *, deadline_s, cancel_check=None:
                           {"status": "failed", "records": [],
                            "reason_code": "propagation_error:RuntimeError"})
        steps = out["plan"]["steps"]
        self.assertEqual(steps["collect_propagation"]["status"], "partial")
        self.assertEqual(steps["assess_claims"]["status"], "completed")   # 不被传播失败挡住
        self.assertEqual(out["plan"]["status"], "partial")
        self.assertEqual(out["execution_status"], "partial")
        self.assertEqual(out["claim_answer"]["answer_status"], "insufficient_data")
        self.assertEqual(out["propagation"]["reason_code"],
                         "propagation_error:RuntimeError")


class AuthorizationGateTests(unittest.TestCase):
    """传播授权闸：fail-closed，未授权零外发。"""

    def test_unauthorized_propagation_never_called_and_plan_continues(self):
        """core.privacy 未授权 → collect_propagation 不发生（not_triggered）、计划继续、
        传播摘要如实记 not_applicable；事实取证照常。"""
        calls = []
        state = _state({"has_factual_claim": True,
                        "claims": [{"claim": CLAIM_TEXT}], "unsourced_unverifiable": 0},
                       markers=_markers())
        with _unauthorized():
            out = _run(state=state,
                       verify_fn=_verify_stub(calls),
                       propagate_fn=lambda *a, **k: calls.append("propagation") or {})
        self.assertEqual(len(calls), 1)   # 只有 verify_fn；propagate_fn 零调用（零外发）
        steps = out["plan"]["steps"]
        self.assertEqual(steps["collect_propagation"]["status"], "not_triggered")
        self.assertEqual(steps["collect_propagation"]["reason_code"],
                         "external_processing_disabled")
        self.assertEqual(steps["assess_claims"]["status"], "completed")
        self.assertEqual(out["propagation"]["status"], "not_applicable")
        self.assertEqual(out["plan"]["status"], "completed")

    def test_authorized_propagation_runs(self):
        """授权开启 → propagate_fn 被调用（预算为计划 deadline 的封顶份额）。"""
        seen = []
        state = _state({"has_factual_claim": True,
                        "claims": [{"claim": CLAIM_TEXT}], "unsourced_unverifiable": 0},
                       markers=_markers())
        with _authorized():
            out = _run(state=state, budget={"deadline_s": 200.0},
                       verify_fn=_verify_stub([]),
                       propagate_fn=lambda claims, *, deadline_s, cancel_check=None:
                           seen.append(deadline_s) or {"status": "completed", "records": []})
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], 30.0)   # 传播份额封顶：不挤占事实取证主预算
        self.assertEqual(out["plan"]["steps"]["collect_propagation"]["status"], "completed")


class CancelTests(unittest.TestCase):
    """协作取消：取消后不再开新工作，已完成结果保留。"""

    def test_cancel_before_verification(self):
        """verify 前取消 → 该步 cancelled、verify_fn 不调、执行轴 cancelled。"""
        state = _state({"has_factual_claim": True,
                        "claims": [{"claim": CLAIM_TEXT}], "unsourced_unverifiable": 0},
                       markers=_markers())
        out = _run(state=state, cancel_check=lambda: True,
                   verify_fn=_verify_stub([]),
                   propagate_fn=lambda *a, **k: {})
        self.assertEqual(out["plan"]["status"], "cancelled")
        self.assertEqual(out["plan"]["steps"]["collect_fact_evidence"]["status"], "cancelled")
        self.assertEqual(out["execution_status"], "cancelled")

    def test_cancel_after_verification_keeps_partial_conclusion(self):
        """verify 返回后（工具内）取消 → collect_fact_evidence 已产出结论：步骤记
        cancelled 但 verify 结果整包保留，传播步不再发生。"""
        calls = []
        flag = {"cancelled": False}

        def _verify(text, *, deep=False, state=None, on_component_done=None,
                    cancel_check=None):
            calls.append(1)
            flag["cancelled"] = True    # 模拟取证过程中用户断开
            return {"label": "存疑待核实", "risk_score": 40, "claims": [],
                    "degraded_components": ["evidence"],
                    "agent_error": "分析已取消（用户中断）", "llm_turns": 1,
                    "evidence_book": {"entries": []}}

        state = _state({"has_factual_claim": True,
                        "claims": [{"claim": CLAIM_TEXT}], "unsourced_unverifiable": 0},
                       markers=_markers())
        out = _run(state=state, verify_fn=_verify,
                   propagate_fn=lambda *a, **k: {"status": "completed", "records": []},
                   cancel_check=lambda: flag["cancelled"])
        self.assertEqual(calls, [1])
        self.assertEqual(out["plan"]["steps"]["collect_fact_evidence"]["status"], "cancelled")
        self.assertEqual(out["plan"]["steps"]["collect_propagation"]["status"], "pending")
        self.assertEqual(out["plan"]["status"], "cancelled")
        self.assertEqual(out["label"], "存疑待核实")   # 已完成结论不丢
        self.assertEqual(out["execution_status"], "cancelled")


class SharedStateTests(unittest.TestCase):
    """state 共享：compute-once 的接线保证。"""

    def test_executor_reuses_injected_state_without_recompute(self):
        """注入 state → ensure 计数不重复：claims/markers 各算一次，verify 收到同一对象。"""
        counter = {"claims": 0, "markers": 0}
        received = {}

        def claims_fn(text):
            counter["claims"] += 1
            return {"has_factual_claim": True, "claims": [{"claim": CLAIM_TEXT}],
                    "unsourced_unverifiable": 0}

        def markers_fn(text):
            counter["markers"] += 1
            return _markers()

        def _verify(text, *, deep=False, state=None, on_component_done=None,
                    cancel_check=None):
            received["state"] = state   # 记录执行器传入的共享状态对象
            return {"label": "高风险谣言", "risk_score": 70, "claims": [],
                    "degraded_components": [], "agent_error": "",
                    "evidence_book": {"entries": []}}

        state = AnalysisState(ORIGINAL_TEXT, {"claims": claims_fn, "markers": markers_fn})
        with _authorized():
            out = _run(state=state, verify_fn=_verify,
                       propagate_fn=lambda *a, **k: {"status": "completed", "records": []})
        self.assertEqual(counter, {"claims": 1, "markers": 1})   # compute-once
        self.assertIs(received["state"], state)                  # 同一对象，不重算
        self.assertEqual(out["plan"]["status"], "completed")

    def test_executor_created_state_carries_classic_funcs(self):
        """执行器自建 state 时装配 classic 三维（与 loop._real_service 同源）——
        共享给 analyze 后组件不缺度。装配失败按"未装配"诚实降级。"""
        captured = {}
        with mock.patch("mylocalai.assistant.workflow.claim._state_funcs",
                        return_value={"emotion": lambda text: {"emotion_score": 3}}), \
                _unauthorized():
            out = _run(state=None,
                       verify_fn=lambda text, *, deep=False, state=None,
                       on_component_done=None, cancel_check=None:
                           (captured.update(state=state),
                            {"label": "低风险", "risk_score": 10, "claims": [],
                             "degraded_components": [], "agent_error": "",
                             "evidence_book": {"entries": []}})[-1],
                       propagate_fn=lambda *a, **k: {"status": "completed", "records": []})
        self.assertIsNotNone(captured["state"])
        # classic funcs 经执行器装配进了共享状态（这里注入了 emotion 替身）
        self.assertEqual(captured["state"]._funcs.get("emotion")(ORIGINAL_TEXT),
                         {"emotion_score": 3})


class CompiledAnswerTests(unittest.TestCase):
    """新引擎契约路径：带 span/锚点的结果走 compile_answer。"""

    def _verify_result(self) -> dict:
        page_text = "官方通报：自来水被人下毒了属于不实信息，请勿传播。"
        quote = "自来水被人下毒了属于不实信息"
        start = page_text.index(quote)
        claim_start = ORIGINAL_TEXT.index(CLAIM_TEXT)
        return {"label": "高风险谣言", "risk_score": 80,
                "claims": [{"claim_id": "C1", "text": CLAIM_TEXT,
                            "original_quote": CLAIM_TEXT,
                            "source_span": {"start": claim_start,
                                            "end": claim_start + len(CLAIM_TEXT)}}],
                "evidence": [{"evidence_id": "E1", "claim_ids": ["C1"], "relation": "refutes",
                              "source_id": "gov.example", "url": "https://gov.example/1",
                              "fetched_at": "2026-11-22T08:00:00+08:00",
                              "quote": quote, "quote_span": {"start": start,
                                                             "end": start + len(quote)},
                              "page_text": page_text, "title": "官方辟谣",
                              "domain": "gov.example", "source_kind": "gov",
                              "independence_group": "gov.example"}],
                "truth_assessments": {"C1": {
                    "statement": "官方来源已明确辟谣该主张。",
                    "supporting_evidence_ids": [], "contrary_evidence_ids": ["E1"],
                    "unresolved_evidence_ids": [], "refutation_sufficient": True,
                    "refutation_basis_evidence_ids": ["E1"],
                    "concrete_issue_signal": False, "issue_basis_evidence_ids": [],
                    "proportionate_review_complete": True,
                    "review_scope": "已检查官方通报原文。", "limitations": []}},
                "assessor_ids": {"C1": "stub/model-a"},
                "propagation_status": {"C1": "completed"},
                "fact_search_status": {"C1": "completed"}}

    def test_new_engine_payload_compiles_validated_answer(self):
        """带 span/锚点 + verified 传播记录 → compile_answer 产物（五态），瞬态 page_text
        不出现在任何输出里。"""
        state = _state({"has_factual_claim": True,
                        "claims": [{"claim": CLAIM_TEXT}], "unsourced_unverifiable": 0},
                       markers=_markers())
        with _authorized():
            out = _run(state=state,
                       verify_fn=lambda text, *, deep=False, state=None,
                       on_component_done=None, cancel_check=None: self._verify_result(),
                       propagate_fn=lambda claims, *, deadline_s, cancel_check=None:
                           {"status": "completed", "records": [_verified_record()]})
        self.assertEqual(out["plan"]["steps"]["validate_answer"]["reason_code"],
                         "compiled_answer")
        answer = out["claim_answer"]
        self.assertEqual(answer["answer_status"], "validated")
        self.assertEqual(answer["claims"][0]["rumor_assessment"]["state"], "refuted_rumor")
        self.assertEqual(answer["claims"][0]["propagation_records"][0]["url"],
                         "https://gov.example/post")
        self.assertNotIn("page_text", json.dumps(answer, ensure_ascii=False))
        self.assertNotIn("page_text", json.dumps(out["propagation"]["records"],
                                                 ensure_ascii=False))

    def test_compiler_rejection_falls_back_to_legacy_shape(self):
        """编译器拒收（引用不锚定等）→ 退回诚实过渡对象，绝不硬凑五态结论。"""
        payload = self._verify_result()
        payload["evidence"][0]["quote_span"] = {"start": 0, "end": 3}   # 引文对不上正文
        state = _state({"has_factual_claim": True,
                        "claims": [{"claim": CLAIM_TEXT}], "unsourced_unverifiable": 0},
                       markers=_markers())
        with _authorized():
            out = _run(state=state,
                       verify_fn=lambda text, *, deep=False, state=None,
                       on_component_done=None, cancel_check=None: payload,
                       propagate_fn=lambda claims, *, deadline_s, cancel_check=None:
                           {"status": "completed", "records": [_verified_record()]})
        self.assertEqual(out["plan"]["steps"]["validate_answer"]["reason_code"],
                         "legacy_transitional_answer")
        self.assertEqual(out["claim_answer"]["answer_status"], "insufficient_data")


if __name__ == "__main__":
    unittest.main(verbosity=2)
