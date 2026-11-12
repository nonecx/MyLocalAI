# -*- coding: utf-8 -*-
"""Controlled discovery and verification of public propagation records.

Search results are candidates only. A record becomes verified only after the
existing safe transport fetches the page and a quote is anchored in fetched text.
This adapter is not registered on the user-facing Agent until external-processing
consent is implemented and checked by the orchestrator.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import math
import time
from typing import Any, Callable, Dict, List, Optional

from mylocalai.core.claim_checking import validate_propagation_record
from mylocalai.core.privacy import PrivacyPreferences, load_preferences


def safe_public_url(value: Any) -> str:
    """Syntax/local-address guard. Network callers must additionally pin public DNS."""
    from ipaddress import ip_address
    from urllib.parse import urlsplit
    if not isinstance(value, str) or not value or any(ord(ch) <= 0x20 for ch in value):
        raise ValueError("invalid URL")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only absolute HTTP(S) URLs are permitted")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are forbidden")
    _ = parsed.port
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("local hostname is forbidden")
    try:
        addr = ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and (not addr.is_global or addr.is_multicast or addr.is_reserved):
        raise ValueError("non-public IP address is forbidden")
    return value

__all__ = ["discover_candidates", "verify_candidate", "collect_propagation_records"]


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _record_id(claim_id: str, url: str) -> str:
    token = hashlib.sha256((claim_id + "\0" + url).encode("utf-8")).hexdigest()[:16]
    return f"P-{token}"


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("propagation workflow deadline exceeded")
    return value


def _cancelled(cancel_check: Optional[Callable[[], bool]]) -> bool:
    if cancel_check is None:
        return False
    try:
        return bool(cancel_check())
    except Exception:
        return False


def _queries(claim_text: str, limit: int) -> List[str]:
    variants = [claim_text, f'"{claim_text}"', f"{claim_text} 原文"]
    out = []
    for value in variants:
        value = _clean(value, 240)
        if value and value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def discover_candidates(claim_id: str, claim_text: str, *, search_fn: Callable,
                        safe_url_fn: Callable,
                        privacy: Optional[PrivacyPreferences] = None,
                        query_limit: int = 3, results_per_query: int = 5,
                        timeout_s: float = 20.0,
                        cancel_check: Optional[Callable[[], bool]] = None,
                        deadline: Optional[float] = None) -> Dict[str, Any]:
    """Run deterministic query variants under a shared deadline and result budget."""
    claim_id = _clean(claim_id, 80)
    claim_text = _clean(claim_text, 500)
    if not claim_id or not claim_text:
        raise ValueError("claim_id and claim_text are required")
    if isinstance(query_limit, bool) or not isinstance(query_limit, int) or not 1 <= query_limit <= 3:
        raise ValueError("query_limit must be between 1 and 3")
    if isinstance(results_per_query, bool) or not isinstance(results_per_query, int) or not 1 <= results_per_query <= 10:
        raise ValueError("results_per_query must be between 1 and 10")
    timeout_s = float(timeout_s)
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be finite and positive")
    policy = privacy or load_preferences()
    policy.require_external("public-search")
    until = min(deadline, time.monotonic() + timeout_s) if deadline is not None else time.monotonic() + timeout_s
    queries = _queries(claim_text, query_limit)
    candidates: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    calls = 0
    status = "completed"
    for query in queries:
        if _cancelled(cancel_check):
            status = "cancelled"
            break
        try:
            remaining = _remaining(until)
        except TimeoutError:
            status = "budget_exhausted"
            break
        calls += 1
        try:
            result = search_fn(query, k=results_per_query, timeout=remaining)
        except TimeoutError as exc:
            errors.append({"query": query, "error": f"TimeoutError: {exc}"})
            status = "budget_exhausted" if time.monotonic() >= until else "partial"
            if status == "budget_exhausted":
                break
            continue
        except Exception as exc:  # network adapters fail closed
            errors.append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
            status = "partial"
            continue
        if not isinstance(result, dict) or result.get("status") != "ok":
            errors.append({"query": query,
                           "error": _clean((result or {}).get("error", "search failed"), 300)
                           if isinstance(result, dict) else "invalid search result"})
            status = "partial"
            continue
        for row in result.get("results", []):
            if not isinstance(row, dict):
                continue
            try:
                url = safe_url_fn(row.get("url", ""))
            except (TypeError, ValueError):
                continue
            candidate_id = _record_id(claim_id, url)
            candidates.append({
                "candidate_id": candidate_id,
                "claim_id": claim_id,
                "claim_text": claim_text,
                "url": url,
                "title": _clean(row.get("title"), 300),
                "snippet": _clean(row.get("snippet") or row.get("description"), 500),
                "query": query,
                "search_engine": _clean(row.get("engine") or row.get("source"), 80),
                "status": "candidate",
            })
    unique = {row["candidate_id"]: row for row in candidates}
    if status == "completed" and errors:
        status = "partial" if unique else "failed"
    elif status == "partial" and not unique and calls and len(errors) == calls:
        status = "failed"
    return {"claim_id": claim_id, "status": status,
            "queries": queries, "candidates": list(unique.values()),
            "errors": errors, "usage": {"searches": calls}}


def _quote_span(body: str, quote: str) -> Optional[Dict[str, int]]:
    index = body.find(quote)
    if index < 0:
        return None
    return {"start": index, "end": index + len(quote)}


def verify_candidate(claim_id: str, candidate: Dict[str, Any], *,
                     fetch_fn: Callable,
                     safe_url_fn: Callable,
                     match_fn: Callable[[str, str], Dict[str, Any]],
                     allowed_evidence_ids: Optional[List[str]] = None,
                     privacy: Optional[PrivacyPreferences] = None,
                     timeout_s: float = 10.0,
                     max_bytes: int = 1_500_000) -> Dict[str, Any]:
    """Fetch safely; a verified result requires an exact quote locator in the body."""
    if not isinstance(candidate, dict) or candidate.get("status") != "candidate":
        raise ValueError("candidate must be a search candidate")
    claim_id = _clean(claim_id, 80)
    claim_text = _clean(candidate.get("claim_text"), 500)
    if not claim_id or not claim_text:
        raise ValueError("claim_id and candidate claim_text are required")
    url = safe_url_fn(candidate.get("url", ""))
    policy = privacy or load_preferences()
    policy.require_external("public-web")
    record_id = candidate.get("candidate_id") or _record_id(claim_id, url)
    try:
        page = fetch_fn(url, timeout=timeout_s, max_bytes=max_bytes)
    except Exception as exc:
        return {"propagation_record_id": record_id, "claim_ids": [claim_id],
                "url": url, "source_id": url, "status": "unavailable",
                "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(page, dict) or page.get("status") != "ok" or not page.get("text"):
        return {"propagation_record_id": record_id, "claim_ids": [claim_id],
                "url": url, "source_id": url, "status": "unavailable",
                "error": _clean(page.get("error") if isinstance(page, dict) else "invalid fetch result", 300)}
    body = str(page["text"])
    try:
        match = match_fn(claim_text, body)
    except Exception as exc:
        return {"propagation_record_id": record_id, "claim_ids": [claim_id],
                "url": url, "source_id": url, "status": "not_verified",
                "reason": f"matcher_error:{type(exc).__name__}"}
    if not isinstance(match, dict):
        raise ValueError("match_fn must return an object")
    if match.get("claim_id") != claim_id:
        raise ValueError("match_fn must return the requested claim_id")
    evidence_ids = match.get("evidence_ids")
    allowed_ids = set(allowed_evidence_ids or [])
    if (not isinstance(evidence_ids, list) or not evidence_ids or not allowed_ids
            or any(not isinstance(eid, str) or eid not in allowed_ids for eid in evidence_ids)):
        raise ValueError("semantic match must cite evidence IDs supplied by the orchestrator")
    relation = match.get("relation")
    if relation not in {"asserts", "repeats", "reports", "debunks", "mentions", "unclear"}:
        raise ValueError("unsupported publication relation")
    quote = _clean(match.get("quote"), 1200)
    span = _quote_span(body, quote) if quote else None
    if not quote or span is None:
        return {"propagation_record_id": record_id, "claim_ids": [claim_id],
                "url": url, "source_id": url, "status": "not_verified",
                "reason": "matcher_quote_not_anchored"}
    match_basis = relation
    final_url = safe_url_fn(page.get("final_url") or url)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    record = {"propagation_record_id": record_id, "claim_ids": [claim_id],
              "url": final_url, "source_id": final_url, "status": "verified",
              "quote": quote, "quote_span": span,
              "published_at": page.get("published_at"), "verified_at": now,
              "title": _clean(page.get("title") or candidate.get("title"), 300)}
    validated = validate_propagation_record(record, [claim_id], page_text=body)
    validated["title"] = record["title"]
    validated["match_basis"] = match_basis
    validated["independence_group"] = final_url
    return validated


def collect_propagation_records(claim_id: str, claim_text: str, *,
                                search_fn: Callable,
                                fetch_fn: Callable,
                                safe_url_fn: Callable,
                                match_fn: Callable[[str, str], Dict[str, Any]],
                                allowed_evidence_ids: Optional[List[str]] = None,
                                privacy: Optional[PrivacyPreferences] = None,
                                query_limit: int = 3,
                                results_per_query: int = 5,
                                page_limit: int = 5,
                                timeout_s: float = 30.0,
                                cancel_check: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Bounded end-to-end collection under one deadline, retaining completed records."""
    if isinstance(page_limit, bool) or not isinstance(page_limit, int) or not 1 <= page_limit <= 10:
        raise ValueError("page_limit must be between 1 and 10")
    timeout_s = float(timeout_s)
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be finite and positive")
    deadline = time.monotonic() + timeout_s
    policy = privacy or load_preferences()
    found = discover_candidates(claim_id, claim_text, search_fn=search_fn,
                                safe_url_fn=safe_url_fn, privacy=policy,
                                query_limit=query_limit, results_per_query=results_per_query,
                                timeout_s=timeout_s, cancel_check=cancel_check,
                                deadline=deadline)
    all_candidates = found["candidates"]
    candidates = all_candidates[:page_limit]
    records = []
    errors = list(found["errors"])
    status = found["status"]
    if len(all_candidates) > page_limit and status == "completed":
        status = "partial"
    if status not in {"failed", "cancelled", "budget_exhausted"}:
        for candidate in candidates:
            if _cancelled(cancel_check):
                status = "cancelled"
                break
            try:
                remaining = _remaining(deadline)
            except TimeoutError:
                status = "budget_exhausted"
                break
            result = verify_candidate(claim_id, candidate, fetch_fn=fetch_fn,
                                      safe_url_fn=safe_url_fn, match_fn=match_fn,
                                      allowed_evidence_ids=allowed_evidence_ids, privacy=policy,
                                      timeout_s=min(remaining, 15.0))
            if result.get("status") == "verified":
                records.append(result)
            elif result.get("status") == "unavailable":
                errors.append({"candidate_id": candidate["candidate_id"],
                               "error": result.get("error", "fetch unavailable")})
                if status == "completed":
                    status = "partial"
    if status == "completed" and errors:
        status = "partial"
    return {"claim_id": _clean(claim_id, 80), "status": status,
            "records": records, "candidate_count": len(found["candidates"]),
            "verified_count": len(records), "errors": errors,
            "usage": {"searches": found["usage"]["searches"],
                      "pages": len(candidates)}}
