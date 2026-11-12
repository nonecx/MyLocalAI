# -*- coding: utf-8 -*-
"""Claim-level evidence and rumor-state contracts.

This module validates structured references and applies the confirmed five-state
precedence. It does not decide whether natural-language evidence reasoning is
semantically correct; that remains a model-quality/evaluation responsibility.
"""

from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set
from urllib.parse import urlsplit

__all__ = [
    "EVIDENCE_RELATIONS",
    "PROPAGATION_STATUSES",
    "EXECUTION_STATUSES",
    "RUMOR_STATE_LABELS",
    "validate_timestamp",
    "validate_claim",
    "validate_evidence",
    "validate_propagation_record",
    "validate_truth_assessment",
    "derive_rumor_state",
]

EVIDENCE_RELATIONS = frozenset({"supports", "refutes", "partial", "background", "unclear"})
PROPAGATION_STATUSES = frozenset({
    "not_started", "completed", "partial", "failed", "cancelled", "budget_exhausted",
})
EXECUTION_STATUSES = PROPAGATION_STATUSES - {"not_started"}
RUMOR_STATE_LABELS = {
    "propagation_insufficient": "传播证据不足",
    "awaiting_review": "尚待核查",
    "suspected_rumor": "疑似谣言",
    "refuted_rumor": "已证伪的谣言",
    "no_specific_issue_found": "未发现具体问题",
}


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field} must not contain surrounding whitespace")
    return value


def validate_timestamp(value: Any, field: str = "timestamp", *, optional: bool = False,
                        allow_date_only: bool = False) -> Optional[str]:
    if optional and value is None:
        return None
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        if allow_date_only:
            try:
                datetime.strptime(text, "%Y-%m-%d")
                return text
            except ValueError:
                pass
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if allow_date_only:
            try:
                datetime.strptime(text, "%Y-%m-%d")
                return text
            except ValueError:
                pass
        raise ValueError(f"{field} must include a timezone offset")
    return text


def _string_list(value: Any, field: str) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    if any(item != item.strip() for item in value):
        raise ValueError(f"{field} entries must not contain surrounding whitespace")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} must not contain duplicates")
    return list(value)


def _http_url(value: Any, field: str = "url") -> str:
    """Validate URL form and literal host; transport still enforces DNS/SSRF policy."""
    url = _required_text(value, field)
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{field} must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"{field} must not contain URL credentials")
        port = parsed.port  # Access validates malformed and out-of-range ports.
        if port is not None and not 1 <= port <= 65535:
            raise ValueError(f"{field} has an invalid port")
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError(f"{field} must use a public hostname")
        try:
            address = ip_address(host)
        except ValueError:
            address = None
        if address is not None and (not address.is_global or address.is_multicast or address.is_reserved):
            raise ValueError(f"{field} must use a public IP address")
    except ValueError as exc:
        if str(exc).startswith(field):
            raise
        raise ValueError(f"{field} is invalid: {exc}") from exc
    return url


def _span(value: Any, field: str, upper_bound: Optional[int] = None) -> Dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    start, end = value.get("start"), value.get("end")
    if (isinstance(start, bool) or not isinstance(start, int) or
            isinstance(end, bool) or not isinstance(end, int) or start < 0 or end <= start):
        raise ValueError(f"{field} must be a non-empty half-open integer span")
    if upper_bound is not None and end > upper_bound:
        raise ValueError(f"{field} exceeds source text")
    return {"start": start, "end": end}


def _claim_ids(value: Any, known_claim_ids: Optional[Set[str]] = None,
               field: str = "claim_ids") -> List[str]:
    ids = _string_list(value, field)
    if not ids:
        raise ValueError(f"{field} must not be empty")
    if known_claim_ids is not None and not set(ids).issubset(known_claim_ids):
        raise ValueError(f"{field} contains an unknown claim_id")
    return ids


def validate_claim(claim: Any, original_text: Optional[str] = None) -> Dict[str, Any]:
    """Validate one extracted claim while preserving an exact source quote/span.

    Offsets are Python Unicode code-point indexes in the exact decoded string, not
    UTF-8 bytes or JavaScript UTF-16 code units.
    """
    if not isinstance(claim, Mapping):
        raise ValueError("claim must be an object")
    claim_id = _required_text(claim.get("claim_id"), "claim_id")
    text = _required_text(claim.get("text"), "text")
    quote = _required_text(claim.get("original_quote"), "original_quote")
    span = _span(claim.get("source_span"), "source_span",
                 len(original_text) if isinstance(original_text, str) else None)
    if span["end"] - span["start"] != len(quote):
        raise ValueError("source_span length must match original_quote")
    if isinstance(original_text, str) and original_text[span["start"]:span["end"]] != quote:
        raise ValueError("original_quote does not match the supplied source span")
    return {"claim_id": claim_id, "text": text, "original_quote": quote,
            "source_span": span, "offset_unit": "unicode_codepoint"}


def validate_evidence(evidence: Any, known_claim_ids: Iterable[str],
                      page_text: str) -> Dict[str, Any]:
    """Validate evidence identity, claim links, relation, URL, and fetched-page quote anchor."""
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence must be an object")
    if not isinstance(page_text, str) or not page_text:
        raise ValueError("evidence requires fetched page_text")
    evidence_id = _required_text(evidence.get("evidence_id"), "evidence_id")
    claim_ids = _claim_ids(evidence.get("claim_ids"), set(known_claim_ids))
    relation = _required_text(evidence.get("relation"), "relation")
    if relation not in EVIDENCE_RELATIONS:
        raise ValueError(f"unsupported evidence relation: {relation}")
    url = _http_url(evidence.get("url"))
    source_id = _required_text(evidence.get("source_id"), "source_id")
    fetched_at = validate_timestamp(evidence.get("fetched_at"), "fetched_at")
    quote = _required_text(evidence.get("quote"), "quote")
    quote_span = _span(evidence.get("quote_span"), "quote_span", len(page_text))
    if quote_span["end"] - quote_span["start"] != len(quote):
        raise ValueError("quote_span length must match quote")
    if page_text[quote_span["start"]:quote_span["end"]] != quote:
        raise ValueError("quote does not match the supplied fetched page span")
    return {"evidence_id": evidence_id, "claim_ids": claim_ids,
            "relation": relation, "source_id": source_id, "url": url,
            "fetched_at": fetched_at, "quote": quote, "quote_span": quote_span,
            "offset_unit": "unicode_codepoint"}


def validate_propagation_record(record: Any, known_claim_ids: Iterable[str],
                                page_text: Optional[str] = None) -> Dict[str, Any]:
    """Validate a public propagation instance; a search result alone is never enough."""
    if not isinstance(record, Mapping):
        raise ValueError("propagation record must be an object")
    record_id = _required_text(record.get("propagation_record_id"), "propagation_record_id")
    claim_ids = _claim_ids(record.get("claim_ids"), set(known_claim_ids))
    url = _http_url(record.get("url"))
    source_id = _required_text(record.get("source_id"), "source_id")
    status = _required_text(record.get("status"), "status")
    if status not in {"verified", "not_verified", "unavailable"}:
        raise ValueError(f"unsupported propagation record status: {status}")
    out = {"propagation_record_id": record_id, "claim_ids": claim_ids,
           "url": url, "source_id": source_id, "status": status}
    if status == "verified":
        if not isinstance(page_text, str) or not page_text:
            raise ValueError("verified propagation record requires fetched page_text")
        quote = _required_text(record.get("quote"), "quote")
        verified_at = validate_timestamp(record.get("verified_at"), "verified_at")
        quote_span = _span(record.get("quote_span"), "quote_span", len(page_text))
        if quote_span["end"] - quote_span["start"] != len(quote):
            raise ValueError("propagation quote span length must match quote")
        if page_text[quote_span["start"]:quote_span["end"]] != quote:
            raise ValueError("propagation quote is not anchored in fetched page text")
        published_at = validate_timestamp(record.get("published_at"), "published_at",
                                          optional=True, allow_date_only=True)
        out.update(quote=quote, quote_span=quote_span, verified_at=verified_at,
                   published_at=published_at, offset_unit="unicode_codepoint")
    return out


def _assessment_refs(assessment: Mapping[str, Any], key: str,
                     evidence_by_id: Mapping[str, Mapping[str, Any]], claim_id: str) -> List[str]:
    values = _string_list(assessment.get(key), key)
    for evidence_id in values:
        item = evidence_by_id.get(evidence_id)
        if item is None:
            raise ValueError(f"{key} references unknown evidence_id: {evidence_id}")
        if claim_id not in item.get("claim_ids", []):
            raise ValueError(f"{evidence_id} is not linked to claim {claim_id}")
    return values


def _validate_independent_review(review: Any, claim_id: str, assessor_id: str,
                                 evidence_by_id: Mapping[str, Mapping[str, Any]],
                                 reviewer_id: str) -> Dict[str, Any]:
    if not isinstance(review, Mapping):
        raise ValueError("independent_review must be an object")
    reviewer_id = _required_text(reviewer_id, "reviewer_id")
    if reviewer_id == assessor_id:
        raise ValueError("independent reviewer must differ from the initial assessor")
    evidence_ids = _assessment_refs(
        review, "evidence_ids", evidence_by_id, claim_id)
    if not evidence_ids:
        raise ValueError("independent review must cite evidence")
    resolved = review.get("conflict_resolved")
    if not isinstance(resolved, bool):
        raise ValueError("independent_review.conflict_resolved must be boolean")
    out = {"evidence_ids": evidence_ids, "conflict_resolved": resolved}
    out["reviewer_id"] = reviewer_id
    if resolved:
        for key in ("refutation_sufficient", "concrete_issue_signal"):
            if not isinstance(review.get(key), bool):
                raise ValueError(f"independent_review.{key} must be boolean")
        refutation_ids = _assessment_refs(
            review, "refutation_basis_evidence_ids", evidence_by_id, claim_id)
        issue_ids = _assessment_refs(
            review, "issue_basis_evidence_ids", evidence_by_id, claim_id)
        if review["refutation_sufficient"] and not refutation_ids:
            raise ValueError("reviewed refutation requires cited evidence")
        if review["concrete_issue_signal"] and not issue_ids:
            raise ValueError("reviewed issue signal requires cited evidence")
        if any(evidence_by_id[eid].get("relation") != "refutes" for eid in refutation_ids):
            raise ValueError("reviewed refutation basis must reference refuting evidence")
        if any(evidence_by_id[eid].get("relation") not in {"refutes", "partial"}
               for eid in issue_ids):
            raise ValueError("reviewed issue basis must reference refuting or partial evidence")
        out.update(refutation_sufficient=review["refutation_sufficient"],
                   refutation_basis_evidence_ids=refutation_ids,
                   concrete_issue_signal=review["concrete_issue_signal"],
                   issue_basis_evidence_ids=issue_ids)
    else:
        out["limitations"] = _string_list(review.get("limitations"),
                                          "independent_review.limitations")
    return out


def validate_truth_assessment(assessment: Any, claim_id: str,
                              evidence: Sequence[Mapping[str, Any]],
                              independent_review: Optional[Mapping[str, Any]] = None,
                              *, assessor_id: str,
                              force_review: bool = False,
                              reviewer_id: Optional[str] = None) -> Dict[str, Any]:
    """Validate model references and derive a fail-closed independent-review gate.

    The first-pass model does not control whether review is needed. Conflicting
    support/refute evidence always requires a separate review; the orchestrator can
    also force review for other important counterevidence or uncertain citations.
    """
    if not isinstance(assessment, Mapping):
        raise ValueError("truth assessment must be an object")
    claim_id = _required_text(claim_id, "claim_id")
    evidence_by_id = {item.get("evidence_id"): item for item in evidence
                      if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)}
    if len(evidence_by_id) != len([item for item in evidence if isinstance(item, Mapping)]):
        raise ValueError("evidence_id values must be unique")

    assessor_id = _required_text(assessor_id, "assessor_id")
    statement = _required_text(assessment.get("statement"), "statement")
    supporting = _assessment_refs(assessment, "supporting_evidence_ids", evidence_by_id, claim_id)
    contrary = _assessment_refs(assessment, "contrary_evidence_ids", evidence_by_id, claim_id)
    unresolved = _assessment_refs(assessment, "unresolved_evidence_ids", evidence_by_id, claim_id)
    refutation_basis = _assessment_refs(
        assessment, "refutation_basis_evidence_ids", evidence_by_id, claim_id)
    issue_basis = _assessment_refs(assessment, "issue_basis_evidence_ids", evidence_by_id, claim_id)
    if any(evidence_by_id[eid].get("relation") != "supports" for eid in supporting):
        raise ValueError("supporting_evidence_ids must reference supporting evidence")
    if any(evidence_by_id[eid].get("relation") != "refutes" for eid in contrary):
        raise ValueError("contrary_evidence_ids must reference refuting evidence")
    if any(evidence_by_id[eid].get("relation") != "refutes" for eid in refutation_basis):
        raise ValueError("refutation basis must reference refuting evidence")
    if any(evidence_by_id[eid].get("relation") not in {"refutes", "partial"}
           for eid in issue_basis):
        raise ValueError("concrete issue basis must reference refuting or partial evidence")

    for key in ("refutation_sufficient", "concrete_issue_signal", "proportionate_review_complete"):
        if not isinstance(assessment.get(key), bool):
            raise ValueError(f"{key} must be boolean")
    if assessment["refutation_sufficient"] and not refutation_basis:
        raise ValueError("sufficient refutation requires cited refutation evidence")
    if assessment["concrete_issue_signal"] and not issue_basis:
        raise ValueError("a concrete issue signal requires cited evidence")
    scope = _required_text(assessment.get("review_scope"), "review_scope")
    limitations = _string_list(assessment.get("limitations"), "limitations")

    conflict = bool(supporting and contrary)
    review_required = bool(force_review or conflict)
    review = None
    if independent_review is not None:
        if not review_required:
            raise ValueError("independent review supplied when no review trigger exists")
        if not reviewer_id:
            raise ValueError("independent reviewer identity must be supplied by the orchestrator")
        review = _validate_independent_review(independent_review, claim_id, assessor_id,
                                              evidence_by_id, reviewer_id)
        review_required = not review["conflict_resolved"]
        if not review_required and review["conflict_resolved"]:
            refutation_basis = review.get("refutation_basis_evidence_ids", [])
            issue_basis = review.get("issue_basis_evidence_ids", [])
            assessment_refutation_sufficient = review.get("refutation_sufficient", False)
            assessment_issue_signal = review.get("concrete_issue_signal", False)
        else:
            assessment_refutation_sufficient = assessment["refutation_sufficient"]
            assessment_issue_signal = assessment["concrete_issue_signal"]
    else:
        assessment_refutation_sufficient = assessment["refutation_sufficient"]
        assessment_issue_signal = assessment["concrete_issue_signal"]

    return {
        "claim_id": claim_id,
        "assessor_id": assessor_id,
        "statement": statement,
        "supporting_evidence_ids": supporting,
        "contrary_evidence_ids": contrary,
        "unresolved_evidence_ids": unresolved,
        "refutation_sufficient": assessment_refutation_sufficient,
        "refutation_basis_evidence_ids": refutation_basis,
        "concrete_issue_signal": assessment_issue_signal,
        "issue_basis_evidence_ids": issue_basis,
        "proportionate_review_complete": assessment["proportionate_review_complete"],
        "review_required": review_required,
        "independent_review": review,
        "review_scope": scope,
        "limitations": limitations,
    }


def derive_rumor_state(*, claim_id: str,
                       has_factual_claim: bool,
                       propagation_status: str,
                       propagation_records: Sequence[Mapping[str, Any]],
                       truth_assessment: Mapping[str, Any],
                       fact_search_status: str) -> Dict[str, Any]:
    """Apply state precedence after all claim/evidence/review validators pass.

    A verified publication is a durable fact even if the broader propagation search
    is partial. No verified publication plus an incomplete search is still labeled
    propagation_insufficient, accompanied by the incomplete execution status.
    """
    if not isinstance(has_factual_claim, bool):
        raise ValueError("has_factual_claim must be boolean")
    if propagation_status not in EXECUTION_STATUSES:
        raise ValueError(f"unsupported propagation status: {propagation_status}")
    if fact_search_status not in EXECUTION_STATUSES:
        raise ValueError(f"unsupported fact search status: {fact_search_status}")
    if not isinstance(truth_assessment, Mapping):
        raise ValueError("truth_assessment must be an object")
    for key in ("refutation_sufficient", "concrete_issue_signal",
                "proportionate_review_complete", "review_required"):
        if not isinstance(truth_assessment.get(key), bool):
            raise ValueError(f"truth_assessment.{key} must be boolean")
    if not isinstance(truth_assessment.get("limitations"), list):
        raise ValueError("truth_assessment.limitations must be a list")
    claim_id = _required_text(claim_id, "claim_id")
    records = list(propagation_records)
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("propagation_records must contain objects")
        if claim_id not in record.get("claim_ids", []):
            raise ValueError("propagation record is not linked to the assessed claim")
        if record.get("status") not in {"verified", "not_verified", "unavailable"}:
            raise ValueError("propagation record has an invalid status")
        _http_url(record.get("url"))
        _required_text(record.get("propagation_record_id"), "propagation_record_id")
        if record.get("status") == "verified":
            _required_text(record.get("quote"), "quote")
            validate_timestamp(record.get("verified_at"), "verified_at")
    if not has_factual_claim:
        state = None
        task_status = "no_factual_claim"
        reason = "no_factual_claim"
        verified = []
    else:
        task_status = None
        verified = [row for row in records if row.get("status") == "verified"]
        if not verified:
            state = "propagation_insufficient"
            reason = ("no_verified_publication" if propagation_status == "completed"
                      else "propagation_search_incomplete")
        elif truth_assessment.get("review_required") is True:
            if truth_assessment.get("concrete_issue_signal") is True:
                state = "suspected_rumor"
                reason = "concrete_issue_signal_with_verified_propagation_review_pending"
            else:
                state = "awaiting_review"
                reason = "independent_review_required"
        elif truth_assessment.get("refutation_sufficient") is True:
            state = "refuted_rumor"
            reason = "sufficient_refutation_with_verified_propagation"
        elif truth_assessment.get("concrete_issue_signal") is True:
            state = "suspected_rumor"
            reason = "concrete_issue_signal_with_verified_propagation"
        elif (truth_assessment.get("proportionate_review_complete") is True
              and fact_search_status == "completed"
              and propagation_status == "completed"):
            state = "no_specific_issue_found"
            reason = "proportionate_review_found_no_specific_issue"
        else:
            state = "awaiting_review"
            reason = "review_incomplete_or_unresolved"
    limitations = _string_list(truth_assessment.get("limitations"), "limitations")
    if truth_assessment.get("review_required") is True:
        limitations.append("独立复核尚未完成；当前判断为初步判断。")
    has_verified_record = any(
        isinstance(row, Mapping) and row.get("status") == "verified" for row in records)
    if propagation_status != "completed" and has_verified_record:
        limitations.append("已核验至少一条公开发布记录，但传播检索未完整完成。")
    if fact_search_status != "completed":
        limitations.append("事实证据检索未完整完成，真实性判断仍有缺口。")
    return {
        "state": state,
        "label": RUMOR_STATE_LABELS.get(state) if state is not None else None,
        "task_status": task_status,
        "reason_code": reason,
        "propagation_status": propagation_status,
        "fact_search_status": fact_search_status,
        "has_verified_propagation": bool(verified) if has_factual_claim else False,
        "limitations": list(dict.fromkeys(limitations)),
    }
