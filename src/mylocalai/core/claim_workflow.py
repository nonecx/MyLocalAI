# -*- coding: utf-8 -*-
"""Deterministic claim-checking plan and validated answer compiler.

The model may produce claim/evidence-linked assessments, but it cannot invent the
execution graph or directly emit an unchecked user answer. This module supplies a
small stable contract for assistant/Web/MCP adapters to share.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from mylocalai.core.claim_checking import (
    derive_rumor_state,
    validate_claim,
    validate_evidence,
    validate_propagation_record,
    validate_timestamp,
    validate_truth_assessment,
)

__all__ = [
    "PLAN_STEPS",
    "PLAN_STATUSES",
    "STEP_STATUSES",
    "ClaimCheckingPlan",
    "build_claim_checking_plan",
    "compile_answer",
]

PLAN_STEPS = ("extract_claims", "collect_fact_evidence", "collect_propagation",
              "assess_claims", "validate_answer", "commit_result")
STEP_STATUSES = frozenset({"pending", "running", "completed", "partial", "failed",
                           "cancelled", "budget_exhausted", "not_triggered", "not_applicable",
                           "not_run"})
PLAN_STATUSES = frozenset({"running", "completed", "partial", "failed", "cancelled",
                           "budget_exhausted", "no_factual_claim"})
_TERMINAL = {"completed", "partial", "failed", "cancelled", "budget_exhausted",
             "not_triggered", "not_applicable", "not_run"}
_DEPS = {
    "extract_claims": (),
    "collect_fact_evidence": ("extract_claims",),
    "collect_propagation": ("extract_claims",),
    "assess_claims": ("collect_fact_evidence", "collect_propagation"),
    "validate_answer": ("assess_claims",),
    "commit_result": ("validate_answer",),
}


def _positive_int(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _budget_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("deadline_s must be positive")
    seconds = float(value)
    if seconds != seconds or seconds == float("inf"):
        raise ValueError("deadline_s must be finite")
    return seconds


@dataclass
class ClaimCheckingPlan:
    """A fixed graph with runtime step states; arbitrary step lists are impossible."""
    task_id: str
    claim_ids: List[str]
    query_budget: int
    page_budget: int
    deadline_s: float
    steps: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    status: str = "running"
    reason_code: str = "plan_started"

    def __post_init__(self) -> None:
        if not self.task_id or not isinstance(self.task_id, str):
            raise ValueError("task_id must be non-empty")
        if len(self.claim_ids) != len(set(self.claim_ids)):
            raise ValueError("claim_ids must be unique")
        self.steps = {
            key: {"status": "pending", "depends_on": list(_DEPS[key]), "result_ref": None,
                  "reason_code": "not_started"}
            for key in PLAN_STEPS
        }
        if not self.claim_ids:
            self.steps["extract_claims"].update(status="completed", reason_code="no_claims")
            for key in PLAN_STEPS[1:]:
                self.steps[key].update(status="not_applicable", reason_code="no_factual_claim")
            self.status = "no_factual_claim"
            self.reason_code = "no_factual_claim"

    def start_step(self, step_id: str) -> None:
        step = self._step(step_id)
        if step["status"] != "pending":
            raise ValueError("only pending steps may start")
        for dep in step["depends_on"]:
            if self.steps[dep]["status"] not in {"completed", "partial", "not_triggered"}:
                raise ValueError(f"dependency {dep} is not complete")
        if step_id == "validate_answer" and "independent_review" in self.steps:
            if self.steps["independent_review"]["status"] not in {
                    "completed", "partial", "not_triggered"}:
                raise ValueError("independent review decision is not terminal")
        step.update(status="running", reason_code="step_started")

    def finish_step(self, step_id: str, status: str, *, result_ref: Optional[str] = None,
                    reason_code: str = "") -> None:
        step = self._step(step_id)
        if step["status"] != "running":
            raise ValueError("only running steps may finish")
        if status not in _TERMINAL - {"not_triggered", "not_applicable", "not_run"}:
            raise ValueError(f"invalid terminal step status: {status}")
        step.update(status=status, result_ref=result_ref,
                    reason_code=reason_code or status)
        if status not in {"completed", "partial"}:
            # Required-stage failures stop dependent work; mark it not_run rather
            # than letting a later validator infer a negative/no-result conclusion.
            queue = [step_id]
            while queue:
                completed = queue.pop(0)
                for candidate_id, candidate in self.steps.items():
                    if completed in candidate["depends_on"] and candidate["status"] == "pending":
                        candidate.update(status="not_run", reason_code=f"dependency_{completed}_{status}")
                        queue.append(candidate_id)
        self._aggregate()

    def skip_review(self, reason_code: str = "no_review_trigger") -> None:
        """Independent review is a conditional substep and defaults not-triggered."""
        if self.steps["assess_claims"]["status"] not in {"completed", "partial"}:
            raise ValueError("assessment must finish before deciding review")
        self.steps["independent_review"] = {
            "status": "not_triggered", "depends_on": ["assess_claims"],
            "result_ref": None, "reason_code": reason_code,
        }

    def schedule_review(self) -> None:
        if self.steps["assess_claims"]["status"] not in {"completed", "partial"}:
            raise ValueError("assessment must finish before review")
        self.steps["independent_review"] = {
            "status": "pending", "depends_on": ["assess_claims"],
            "result_ref": None, "reason_code": "review_triggered",
        }

    def start_review(self) -> None:
        step = self.steps.get("independent_review")
        if not step or step["status"] != "pending":
            raise ValueError("no pending independent review")
        step["status"] = "running"
        step["reason_code"] = "step_started"

    def finish_review(self, status: str, *, result_ref: Optional[str] = None,
                      reason_code: str = "") -> None:
        step = self.steps.get("independent_review")
        if not step or step["status"] != "running":
            raise ValueError("no running independent review")
        if status not in _TERMINAL - {"not_triggered", "not_applicable"}:
            raise ValueError("invalid review terminal status")
        step.update(status=status, result_ref=result_ref,
                    reason_code=reason_code or status)
        # Answer validation must depend on the review decision if it was scheduled.
        self.steps["validate_answer"]["depends_on"] = ["assess_claims", "independent_review"]
        self._aggregate()

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": 1, "task_id": self.task_id,
                "capability_id": "claim-checking", "capability_version": "2",
                "claim_ids": list(self.claim_ids),
                "budget": {"queries": self.query_budget, "pages": self.page_budget,
                           "deadline_s": self.deadline_s},
                "steps": {key: dict(value) for key, value in self.steps.items()},
                "status": self.status, "reason_code": self.reason_code}

    def _step(self, step_id: str) -> Dict[str, Any]:
        if step_id not in self.steps:
            raise ValueError(f"step is not registered: {step_id}")
        return self.steps[step_id]

    def _aggregate(self) -> None:
        values = [step["status"] for step in self.steps.values()]
        if any(value == "cancelled" for value in values):
            self.status, self.reason_code = "cancelled", "step_cancelled"
        elif any(value == "budget_exhausted" for value in values):
            self.status, self.reason_code = "budget_exhausted", "budget_exhausted"
        elif any(value == "failed" for value in values):
            self.status, self.reason_code = "partial", "required_step_failed"
        elif any(value == "partial" for value in values):
            self.status, self.reason_code = "partial", "one_or_more_steps_partial"
        elif all(value in _TERMINAL for value in values):
            self.status, self.reason_code = "completed", "all_required_steps_terminal"
        else:
            self.status, self.reason_code = "running", "steps_remaining"


def build_claim_checking_plan(task_id: str, claim_ids: Sequence[str], *,
                              query_budget: int = 8, page_budget: int = 8,
                              deadline_s: float = 180.0) -> ClaimCheckingPlan:
    ids = list(claim_ids)
    if any(not isinstance(item, str) or not item.strip() for item in ids):
        raise ValueError("claim_ids must contain non-empty strings")
    return ClaimCheckingPlan(task_id=str(task_id), claim_ids=ids,
                             query_budget=_positive_int(query_budget, "query_budget", 50),
                             page_budget=_positive_int(page_budget, "page_budget", 50),
                             deadline_s=_budget_seconds(deadline_s))


def compile_answer(*, original_text: str,
                   claims: Sequence[Mapping[str, Any]],
                   evidence: Sequence[Mapping[str, Any]],
                   propagation_records: Sequence[Mapping[str, Any]],
                   truth_assessments: Mapping[str, Mapping[str, Any]],
                   propagation_status: Mapping[str, str],
                   fact_search_status: Mapping[str, str],
                   checked_at: str,
                   execution_status: str = "completed",
                   independent_reviews: Optional[Mapping[str, Mapping[str, Any]]] = None,
                   assessor_ids: Optional[Mapping[str, str]] = None,
                   reviewer_ids: Optional[Mapping[str, str]] = None,
                   force_review_claim_ids: Iterable[str] = ()) -> Dict[str, Any]:
    """Compile and validate the user answer object; never emit an unchecked verdict.

    Evidence records must include the fetched page text transiently for anchor checks;
    page_text is stripped from the compiled answer and can be discarded immediately.
    """
    if execution_status not in {"completed", "partial", "failed", "cancelled", "budget_exhausted"}:
        raise ValueError("invalid execution_status")
    checked_at = validate_timestamp(checked_at, "checked_at") or ""
    validated_claims = [validate_claim(item, original_text) for item in claims]
    claim_ids = [item["claim_id"] for item in validated_claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("claim_id values must be unique")
    evidence_by_id: Dict[str, Dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ValueError("evidence entries must be objects")
        normalized = validate_evidence(item, claim_ids, item.get("page_text"))
        evidence_id = normalized["evidence_id"]
        if evidence_id in evidence_by_id:
            raise ValueError("evidence_id values must be unique")
        normalized.update({key: str(item.get(key) or "") for key in
                           ("title", "domain", "source_kind", "independence_group")})
        normalized["published_at"] = item.get("published_at")
        evidence_by_id[evidence_id] = normalized

    records_by_claim: Dict[str, List[Dict[str, Any]]] = {cid: [] for cid in claim_ids}
    for record in propagation_records:
        if not isinstance(record, Mapping):
            raise ValueError("propagation records must be objects")
        normalized = validate_propagation_record(record, claim_ids, record.get("page_text"))
        for cid in normalized["claim_ids"]:
            records_by_claim[cid].append(normalized)

    independent_reviews = independent_reviews or {}
    assessor_ids = assessor_ids or {}
    reviewer_ids = reviewer_ids or {}
    forced = set(force_review_claim_ids)
    compiled: List[Dict[str, Any]] = []
    for claim in validated_claims:
        cid = claim["claim_id"]
        assessment = truth_assessments.get(cid)
        if not isinstance(assessment, Mapping):
            raise ValueError(f"missing truth assessment for {cid}")
        checked_assessment = validate_truth_assessment(
            assessment, cid, list(evidence_by_id.values()),
            independent_review=independent_reviews.get(cid),
            assessor_id=str(assessor_ids.get(cid) or ""),
            reviewer_id=reviewer_ids.get(cid),
            force_review=cid in forced)
        if cid not in propagation_status or cid not in fact_search_status:
            raise ValueError(f"missing collection status for {cid}")
        state = derive_rumor_state(
            claim_id=cid, has_factual_claim=True,
            propagation_status=propagation_status[cid],
            propagation_records=records_by_claim[cid],
            truth_assessment=checked_assessment,
            fact_search_status=fact_search_status[cid])
        related = [item for item in evidence_by_id.values() if cid in item["claim_ids"]]
        key_evidence = [
            {key: item.get(key) for key in
             ("evidence_id", "relation", "source_id", "url", "title", "domain",
              "source_kind", "quote", "quote_span", "published_at", "fetched_at",
              "independence_group")}
            for item in related if item["relation"] in {"supports", "refutes", "partial"}
        ]
        # A support/refute conflict is always prominent, even if the UI shows only a
        # compact initial evidence list. The full list remains available to expand.
        has_conflict = bool(checked_assessment["supporting_evidence_ids"] and
                            checked_assessment["contrary_evidence_ids"])
        if state["state"] == "suspected_rumor":
            state["limitations"] = list(dict.fromkeys(
                ["尚未证实；当前仅有针对该主张的具体问题迹象。"] +
                list(state.get("limitations") or [])))
        compiled.append({
            "claim_id": cid,
            "claim_excerpt": claim["text"],
            "original_quote": claim["original_quote"],
            "source_span": claim["source_span"],
            "truth_assessment": {
                "statement": checked_assessment["statement"],
                "supporting_evidence_ids": checked_assessment["supporting_evidence_ids"],
                "contrary_evidence_ids": checked_assessment["contrary_evidence_ids"],
                "unresolved_evidence_ids": checked_assessment["unresolved_evidence_ids"],
                "scope": checked_assessment["review_scope"],
                "limitations": checked_assessment["limitations"],
            },
            "rumor_assessment": state,
            "assessment_status": "validated" if (
                execution_status == "completed" and
                propagation_status[cid] == "completed" and
                fact_search_status[cid] == "completed" and
                not checked_assessment["review_required"]
            ) else "partial",
            "propagation_records": [
                {key: record.get(key) for key in
                 ("propagation_record_id", "url", "source_id", "status", "quote",
                  "published_at", "verified_at")}
                for record in records_by_claim[cid]
            ],
            "key_evidence": key_evidence if has_conflict else key_evidence[:3],
            "evidence_count": len(related),
        })
    answer_status = ("insufficient_data" if not compiled else
                     "validated" if execution_status == "completed" and
                     all(item["assessment_status"] == "validated" for item in compiled)
                     else "partial")
    return {
        "schema_version": 1,
        "answer_status": answer_status,
        "checked_at": checked_at,
        "execution_status": execution_status,
        "task_status": "no_factual_claim" if not compiled else None,
        "claims": compiled,
        "has_incomplete_claims": execution_status != "completed" or any(
            item["rumor_assessment"]["propagation_status"] != "completed" or
            item["rumor_assessment"]["fact_search_status"] != "completed"
            for item in compiled),
    }
