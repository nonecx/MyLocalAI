# -*- coding: utf-8 -*-
"""Offline contracts for claim-level evidence and rumor-state derivation."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.core.claim_checking import (  # noqa: E402
    derive_rumor_state,
    validate_claim,
    validate_evidence,
    validate_propagation_record,
    validate_timestamp,
    validate_truth_assessment,
)


class ClaimAndEvidenceValidationTests(unittest.TestCase):
    def test_claim_span_is_exact_and_unicode_codepoint_based(self):
        text = "😀 A claim here"
        claim = validate_claim({
            "claim_id": "C1", "text": "A claim", "original_quote": "A claim",
            "source_span": {"start": 2, "end": 9},
        }, text)
        self.assertEqual(claim["offset_unit"], "unicode_codepoint")

    def test_claim_rejects_wrong_span_and_whitespace_ids(self):
        with self.assertRaises(ValueError):
            validate_claim({"claim_id": " C1", "text": "x", "original_quote": "x",
                            "source_span": {"start": 0, "end": 1}}, "x")
        with self.assertRaises(ValueError):
            validate_claim({"claim_id": "C1", "text": "x", "original_quote": "x",
                            "source_span": {"start": 1, "end": 2}}, "x")

    def test_evidence_must_be_anchored_to_fetched_page(self):
        item = {"evidence_id": "E1", "claim_ids": ["C1"], "relation": "supports",
                "source_id": "S1", "url": "https://example.org/a",
                "fetched_at": "2026-11-12T10:00:00+08:00", "quote": "claim is true",
                "quote_span": {"start": 2, "end": 15}}
        self.assertEqual(validate_evidence(item, ["C1"], "A claim is true") ["evidence_id"], "E1")
        with self.assertRaises(ValueError):
            validate_evidence(item, ["C1"], "different body text")

    def test_evidence_rejects_bad_time_url_and_private_literal(self):
        base = {"evidence_id": "E1", "claim_ids": ["C1"], "relation": "supports",
                "source_id": "S1", "url": "https://example.org/a",
                "fetched_at": "2026-11-12T10:00:00+08:00", "quote": "abc",
                "quote_span": {"start": 0, "end": 3}}
        for url in ("http://localhost/x", "http://127.0.0.1/x", "http://10.1.2.3/x",
                    "http://example.org:bad/x", "http://u:p@example.org/x"):
            item = {**base, "url": url}
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_evidence(item, ["C1"], "abc")
        with self.assertRaises(ValueError):
            validate_evidence({**base, "fetched_at": "yesterday"}, ["C1"], "abc")


class PropagationRecordTests(unittest.TestCase):
    def record(self, **updates):
        value = {"propagation_record_id": "P1", "claim_ids": ["C1"], "url": "https://example.org/post",
                 "source_id": "S1", "status": "verified", "quote": "claim posted",
                 "quote_span": {"start": 4, "end": 16},
                 "verified_at": "2026-11-12T10:00:00+08:00", "published_at": None}
        value.update(updates)
        return value

    def test_verified_record_requires_read_page_and_anchored_quote(self):
        with self.assertRaises(ValueError):
            validate_propagation_record(self.record(), ["C1"])
        self.assertEqual(validate_propagation_record(
            self.record(quote_span={"start": 2, "end": 14}),
            ["C1"], "A claim posted")["status"], "verified")
        with self.assertRaises(ValueError):
            validate_propagation_record(
                self.record(quote_span={"start": 2, "end": 14}), ["C1"], "other text")

    def test_timestamp_optional_only_for_publication_date(self):
        self.assertIsNone(validate_timestamp(None, "published_at", optional=True))
        for value in ("2026-11-12T10:00:00", "bad-date"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_timestamp(value)


class RumorStateTests(unittest.TestCase):
    def assessment(self, **updates):
        base = {"claim_id": "C1", "statement": "当前材料显示……",
                "supporting_evidence_ids": [], "contrary_evidence_ids": [],
                "unresolved_evidence_ids": [], "refutation_sufficient": False,
                "refutation_basis_evidence_ids": [], "concrete_issue_signal": False,
                "issue_basis_evidence_ids": [], "proportionate_review_complete": False,
                "review_scope": "本次检索范围", "limitations": ["范围有限"], "review_required": False}
        base.update(updates)
        return base

    def verified(self):
        return {"propagation_record_id": "P1", "claim_ids": ["C1"],
                "url": "https://example.org/post", "source_id": "S1", "status": "verified",
                "quote": "claim posted", "quote_span": {"start": 4, "end": 16},
                "verified_at": "2026-11-12T10:00:00+08:00"}

    def derive(self, *, propagation_status="completed", fact_status="completed",
               records=None, has_claim=True, assessment=None):
        return derive_rumor_state(
            claim_id="C1", has_factual_claim=has_claim,
            propagation_status=propagation_status,
            propagation_records=[] if records is None else records,
            truth_assessment=assessment or self.assessment(),
            fact_search_status=fact_status,
        )

    def test_no_claim_is_not_applicable(self):
        result = self.derive(has_claim=False)
        self.assertIsNone(result["state"])
        self.assertEqual(result["task_status"], "no_factual_claim")

    def test_no_verified_record_after_completed_search_is_insufficient(self):
        out = self.derive(records=[])
        self.assertEqual(out["state"], "propagation_insufficient")
        self.assertEqual(out["reason_code"], "no_verified_publication")

    def test_incomplete_propagation_with_no_record_is_not_a_completed_negative(self):
        out = self.derive(propagation_status="budget_exhausted", records=[])
        self.assertEqual(out["state"], "propagation_insufficient")
        self.assertEqual(out["reason_code"], "propagation_search_incomplete")

    def test_verified_record_survives_partial_additional_search(self):
        out = self.derive(propagation_status="partial", records=[self.verified()])
        self.assertEqual(out["state"], "awaiting_review")
        self.assertTrue(out["has_verified_propagation"])
        self.assertTrue(out["limitations"])

    def test_unreviewed_refutation_does_not_become_refuted_rumor(self):
        assessment = self.assessment(
            refutation_sufficient=True,
            refutation_basis_evidence_ids=["E1"],
            review_required=True,
        )
        out = self.derive(records=[self.verified()], assessment=assessment)
        self.assertEqual(out["state"], "awaiting_review")

    def test_reviewed_refutation_becomes_refuted_only_after_conflict_resolution(self):
        assessment = self.assessment(
            supporting_evidence_ids=["E2"], contrary_evidence_ids=["E1"],
            refutation_sufficient=True,
            refutation_basis_evidence_ids=["E1"],
        )
        independent_review = {"evidence_ids": ["E1", "E2"],
                              "conflict_resolved": True, "refutation_sufficient": True,
                              "refutation_basis_evidence_ids": ["E1"],
                              "concrete_issue_signal": True, "issue_basis_evidence_ids": ["E1"]}
        checked = validate_truth_assessment(assessment, "C1", [
            {"evidence_id": "E1", "claim_ids": ["C1"], "relation": "refutes"},
            {"evidence_id": "E2", "claim_ids": ["C1"], "relation": "supports"},
        ], independent_review=independent_review,
           assessor_id="provider-a/model-a", reviewer_id="provider-b/model-b")
        out = self.derive(records=[self.verified()], assessment=checked)
        self.assertEqual(out["state"], "refuted_rumor")

    def test_conflicting_relations_force_different_reviewer(self):
        evidence = [
            {"evidence_id": "E1", "claim_ids": ["C1"], "relation": "supports"},
            {"evidence_id": "E2", "claim_ids": ["C1"], "relation": "refutes"},
        ]
        assessment = self.assessment(
            supporting_evidence_ids=["E1"], contrary_evidence_ids=["E2"],
        )
        independent_review = {"evidence_ids": ["E1", "E2"],
                              "conflict_resolved": True, "refutation_sufficient": False,
                              "refutation_basis_evidence_ids": [], "concrete_issue_signal": False,
                              "issue_basis_evidence_ids": []}
        with self.assertRaisesRegex(ValueError, "differ"):
            validate_truth_assessment(assessment, "C1", evidence,
                                      independent_review=independent_review,
                                      assessor_id="provider-a/model-a",
                                      reviewer_id="provider-a/model-a")

    def test_mismatched_relation_category_is_rejected(self):
        evidence = [{"evidence_id": "E1", "claim_ids": ["C1"], "relation": "refutes"}]
        assessment = self.assessment(supporting_evidence_ids=["E1"])
        with self.assertRaisesRegex(ValueError, "supporting_evidence_ids"):
            validate_truth_assessment(assessment, "C1", evidence, assessor_id="model-a")

    def test_unknown_record_claim_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not linked"):
            self.derive(records=[{**self.verified(), "claim_ids": ["C2"]}])

    def test_verified_propagation_and_complete_review_no_issue(self):
        assessment = self.assessment(proportionate_review_complete=True)
        out = self.derive(records=[self.verified()], assessment=assessment)
        self.assertEqual(out["state"], "no_specific_issue_found")

    def test_specific_issue_with_verified_propagation_is_suspected(self):
        assessment = self.assessment(concrete_issue_signal=True, issue_basis_evidence_ids=["E1"])
        out = self.derive(records=[self.verified()], assessment=assessment)
        self.assertEqual(out["state"], "suspected_rumor")
        self.assertEqual(out["label"], "疑似谣言")


if __name__ == "__main__":
    unittest.main()
