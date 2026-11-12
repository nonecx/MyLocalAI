# -*- coding: utf-8 -*-
"""Tests for fixed claim-checking plan and validated answer compiler."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.core.claim_workflow import (  # noqa: E402
    PLAN_STEPS,
    build_claim_checking_plan,
    compile_answer,
)


class PlanTests(unittest.TestCase):
    def test_plan_has_fixed_steps_and_rejects_unknown_step(self):
        plan = build_claim_checking_plan("T1", ["C1"])
        self.assertEqual(tuple(plan.to_dict()["steps"]), PLAN_STEPS)
        with self.assertRaisesRegex(ValueError, "not registered"):
            plan.start_step("free_tool_from_model")

    def test_dependency_blocks_assessment_until_both_collections_finish(self):
        plan = build_claim_checking_plan("T1", ["C1"])
        plan.start_step("extract_claims")
        plan.finish_step("extract_claims", "completed", result_ref="claims:C1")
        plan.start_step("collect_fact_evidence")
        plan.finish_step("collect_fact_evidence", "completed", result_ref="evidence:C1")
        with self.assertRaisesRegex(ValueError, "dependency collect_propagation"):
            plan.start_step("assess_claims")
        plan.start_step("collect_propagation")
        plan.finish_step("collect_propagation", "completed", result_ref="propagation:C1")
        plan.start_step("assess_claims")
        plan.finish_step("assess_claims", "completed", result_ref="assessment:C1")
        plan.skip_review()
        plan.start_step("validate_answer")

    def test_required_stage_failure_blocks_downstream_nodes(self):
        plan = build_claim_checking_plan("T1", ["C1"])
        plan.start_step("extract_claims")
        plan.finish_step("extract_claims", "completed")
        plan.start_step("collect_fact_evidence")
        plan.finish_step("collect_fact_evidence", "failed", reason_code="transport_error")
        self.assertEqual(plan.steps["assess_claims"]["status"], "not_run")
        self.assertEqual(plan.status, "partial")
        self.assertEqual(plan.reason_code, "required_step_failed")
        with self.assertRaises(ValueError):
            plan.start_step("assess_claims")

    def test_no_claim_plan_is_separate_task_state_not_rumor_state(self):
        plan = build_claim_checking_plan("T0", [])
        self.assertEqual(plan.status, "no_factual_claim")
        self.assertEqual(plan.steps["collect_propagation"]["status"], "not_applicable")
        self.assertIsNone(plan.to_dict().get("rumor_state"))
        answer = compile_answer(original_text="仅表达态度。", claims=[], evidence=[],
                                propagation_records=[], truth_assessments={},
                                propagation_status={}, fact_search_status={},
                                checked_at="2026-11-12T12:00:00+00:00")
        self.assertEqual(answer["task_status"], "no_factual_claim")
        self.assertEqual(answer["claims"], [])


class AnswerCompilerTests(unittest.TestCase):
    def fixture(self, *, conflicting=False, propagation_status="completed"):
        original = "Claim A happened on 2026-11-01."
        quote = "Claim A happened"
        start = original.index(quote)
        claim = {"claim_id": "C1", "text": "Claim A happened", "original_quote": quote,
                 "source_span": {"start": start, "end": start + len(quote)}}
        evidence = [{"evidence_id": "E1", "claim_ids": ["C1"], "relation": "supports",
                     "source_id": "S1", "url": "https://example.org/a",
                     "title": "Source A", "domain": "example.org", "source_kind": "news",
                     "fetched_at": "2026-11-12T10:00:00+00:00", "published_at": None,
                     "quote": "Claim A happened", "quote_span": {"start": 0, "end": len("Claim A happened")},
                     "page_text": "Claim A happened according to the source."}]
        support_ids = ["E1"]
        contrary_ids = []
        if conflicting:
            evidence.append({"evidence_id": "E2", "claim_ids": ["C1"], "relation": "refutes",
                             "source_id": "S2", "url": "https://example.net/b",
                             "title": "Source B", "domain": "example.net", "source_kind": "gov",
                             "fetched_at": "2026-11-12T10:00:00+00:00", "published_at": None,
                             "quote": "Claim A did not happen", "quote_span": {"start": 0, "end": len("Claim A did not happen")},
                             "page_text": "Claim A did not happen, according to records."})
            contrary_ids = ["E2"]
        record = {"propagation_record_id": "P1", "claim_ids": ["C1"],
                  "url": "https://social.example.org/post", "source_id": "Psrc",
                  "status": "verified", "quote": "Claim A happened",
                  "quote_span": {"start": 0, "end": len("Claim A happened")},
                  "published_at": "2026-11-01", "verified_at": "2026-11-12T10:00:00+00:00",
                  "page_text": "Claim A happened publicly."}
        assessment = {"statement": "现有材料对该日期主张存在分歧，需按来源原文判断。",
                      "supporting_evidence_ids": support_ids,
                      "contrary_evidence_ids": contrary_ids,
                      "unresolved_evidence_ids": [],
                      "refutation_sufficient": False,
                      "refutation_basis_evidence_ids": [],
                      "concrete_issue_signal": bool(conflicting),
                      "issue_basis_evidence_ids": contrary_ids,
                      "proportionate_review_complete": not conflicting,
                      "review_scope": "已检查两份来源原文及一条公开发布记录。",
                      "limitations": []}
        return original, claim, evidence, record, assessment

    def compile(self, *, conflict=False, prop_status="completed"):
        original, claim, evidence, record, assessment = self.fixture(conflicting=conflict,
                                                                      propagation_status=prop_status)
        reviews = None
        assessor_ids = {"C1": "provider-a/model-a"}
        reviewer_ids = None
        if conflict:
            reviews = {"C1": {"evidence_ids": ["E1", "E2"], "conflict_resolved": False,
                               "limitations": ["两份来源对关键日期仍有分歧。"]}}
            reviewer_ids = {"C1": "provider-b/model-b"}
        return compile_answer(
            original_text=original, claims=[claim], evidence=evidence,
            propagation_records=[record], truth_assessments={"C1": assessment},
            propagation_status={"C1": prop_status}, fact_search_status={"C1": "completed"},
            checked_at="2026-11-12T12:00:00+00:00", execution_status="completed",
            independent_reviews=reviews, assessor_ids=assessor_ids,
            reviewer_ids=reviewer_ids)

    def test_compiles_dual_assessment_and_strips_transient_page_body(self):
        answer = self.compile()
        self.assertEqual(answer["schema_version"], 1)
        self.assertEqual(answer["answer_status"], "validated")
        self.assertEqual(len(answer["claims"]), 1)
        claim = answer["claims"][0]
        self.assertEqual(claim["rumor_assessment"]["state"], "no_specific_issue_found")
        self.assertEqual(claim["truth_assessment"]["supporting_evidence_ids"], ["E1"])
        self.assertEqual(claim["propagation_records"][0]["url"], "https://social.example.org/post")
        self.assertEqual(claim["propagation_records"][0]["verified_at"],
                         "2026-11-12T10:00:00+00:00")
        self.assertNotIn("page_text", str(answer))
        self.assertFalse(answer["has_incomplete_claims"])

    def test_conflict_is_triggered_for_independent_review_and_all_evidence_is_prominent(self):
        answer = self.compile(conflict=True)
        claim = answer["claims"][0]
        self.assertEqual(answer["answer_status"], "partial")
        self.assertEqual(claim["assessment_status"], "partial")
        self.assertEqual(claim["rumor_assessment"]["state"], "suspected_rumor")
        self.assertEqual(claim["rumor_assessment"]["label"], "疑似谣言")
        self.assertIn("尚未证实", claim["rumor_assessment"]["limitations"][0])
        self.assertEqual({e["evidence_id"] for e in claim["key_evidence"]}, {"E1", "E2"})

    def test_rejects_unanchored_or_unknown_evidence(self):
        original, claim, evidence, record, assessment = self.fixture()
        evidence[0]["quote_span"] = {"start": 2, "end": 18}
        with self.assertRaisesRegex(ValueError, "quote"):
            compile_answer(original_text=original, claims=[claim], evidence=evidence,
                           propagation_records=[record], truth_assessments={"C1": assessment},
                           propagation_status={"C1": "completed"},
                           fact_search_status={"C1": "completed"},
                           checked_at="2026-11-12T12:00:00+00:00",
                           assessor_ids={"C1": "model-a"})

    def test_issue_signal_survives_budget_exhaustion_as_suspected_not_false(self):
        original, claim, evidence, record, assessment = self.fixture(conflicting=True)
        evidence = [evidence[1]]  # keep a cited concrete issue signal, without support/refute conflict
        assessment["supporting_evidence_ids"] = []
        assessment["contrary_evidence_ids"] = ["E2"]
        assessment["issue_basis_evidence_ids"] = ["E2"]
        assessment["proportionate_review_complete"] = False
        answer = compile_answer(original_text=original, claims=[claim], evidence=evidence,
                                propagation_records=[record], truth_assessments={"C1": assessment},
                                propagation_status={"C1": "completed"},
                                fact_search_status={"C1": "budget_exhausted"},
                                checked_at="2026-11-12T12:00:00+00:00",
                                execution_status="budget_exhausted",
                                assessor_ids={"C1": "model-a"})
        self.assertEqual(answer["claims"][0]["rumor_assessment"]["state"], "suspected_rumor")
        self.assertTrue(answer["has_incomplete_claims"])

    def test_incomplete_execution_does_not_become_no_issue(self):
        original, claim, evidence, record, assessment = self.fixture()
        answer = compile_answer(original_text=original, claims=[claim], evidence=evidence,
                                propagation_records=[record], truth_assessments={"C1": assessment},
                                propagation_status={"C1": "partial"},
                                fact_search_status={"C1": "completed"},
                                checked_at="2026-11-12T12:00:00+00:00",
                                assessor_ids={"C1": "model-a"})
        self.assertEqual(answer["claims"][0]["rumor_assessment"]["state"], "awaiting_review")
        self.assertEqual(answer["answer_status"], "partial")
        self.assertEqual(answer["claims"][0]["assessment_status"], "partial")
        self.assertTrue(answer["has_incomplete_claims"])
        self.assertTrue(answer["claims"][0]["rumor_assessment"]["limitations"])


if __name__ == "__main__":
    unittest.main()
