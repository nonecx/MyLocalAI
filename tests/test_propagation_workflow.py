# -*- coding: utf-8 -*-
"""Offline tests for controlled public-propagation discovery and verification."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills.propagation import (  # noqa: E402
    collect_propagation_records,
    discover_candidates,
    safe_public_url,
    verify_candidate,
)
from mylocalai.core.privacy import (  # noqa: E402
    DEFAULT_PREFERENCES,
    PrivacyAuthorizationError,
    PrivacyPreferences,
    _validated,
)


class PropagationWorkflowTests(unittest.TestCase):
    def privacy(self, providers=()):
        raw = copy.deepcopy(DEFAULT_PREFERENCES)
        if providers:
            raw.update(external_processing_enabled=True,
                       external_processing_providers=list(providers))
        return PrivacyPreferences(_validated(raw))

    def test_search_candidates_are_not_verified_records(self):
        def search(query, k, timeout):
            return {"status": "ok", "results": [{"url": "https://example.org/post",
                                                    "title": "Public post", "snippet": "snippet"}]}
        out = discover_candidates("C1", "A specific claim", search_fn=search,
                                  safe_url_fn=safe_public_url,
                                  privacy=self.privacy(["public-search"]))
        self.assertEqual(out["status"], "completed")
        self.assertEqual(len(out["candidates"]), 1)
        self.assertEqual(out["candidates"][0]["status"], "candidate")

    def test_fetched_page_and_located_quote_are_required(self):
        candidate = {"candidate_id": "P1", "claim_text": "A specific public claim",
                     "url": "https://example.org/post", "status": "candidate"}
        with self.assertRaises(PrivacyAuthorizationError):
            verify_candidate("C1", candidate, fetch_fn=lambda *a, **k: self.fail("unauthorized fetch"),
                             safe_url_fn=safe_public_url, match_fn=lambda *_: {},
                             allowed_evidence_ids=["E1"], privacy=self.privacy())
        result = verify_candidate(
            "C1", candidate, safe_url_fn=safe_public_url,
            fetch_fn=lambda url, **kw: {"status": "ok", "final_url": url,
                                       "title": "Public post", "published_at": None,
                                       "text": "A specific public claim was posted."},
            match_fn=lambda claim, body: {"claim_id": "C1", "relation": "repeats",
                                         "evidence_ids": ["E1"],
                                         "quote": "A specific public claim"},
            allowed_evidence_ids=["E1"], privacy=self.privacy(["public-web"]))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["quote"], "A specific public claim")
        self.assertEqual(result["quote_span"], {"start": 0, "end": 23})

    def test_paraphrase_requires_evidence_linked_matcher_quote(self):
        candidate = {"candidate_id": "P1", "claim_text": "The claim in neutral form",
                     "url": "https://example.org/post", "status": "candidate"}
        fetch = lambda url, **kw: {"status": "ok", "final_url": url,
                                   "title": "Public post", "published_at": None,
                                   "text": "Original wording differs from the claim."}
        result = verify_candidate("C1", candidate, fetch_fn=fetch,
                                  safe_url_fn=safe_public_url,
                                  match_fn=lambda *_: {"claim_id": "C1", "relation": "mentions",
                                                       "evidence_ids": ["E1"],
                                                       "quote": "Original wording differs"},
                                  allowed_evidence_ids=["E1"], privacy=self.privacy(["public-web"]))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["quote_span"], {"start": 0, "end": 24})
        with self.assertRaisesRegex(ValueError, "evidence IDs"):
            verify_candidate("C1", candidate, fetch_fn=fetch, safe_url_fn=safe_public_url,
                             match_fn=lambda *_: {"claim_id": "C1", "relation": "mentions",
                                                  "evidence_ids": ["FAKE"], "quote": "Original wording"},
                             allowed_evidence_ids=["E1"], privacy=self.privacy(["public-web"]))

    def test_search_empty_is_completed_but_total_error_is_failed(self):
        empty = discover_candidates("C1", "A claim", safe_url_fn=safe_public_url,
                                    privacy=self.privacy(["public-search"]),
                                    search_fn=lambda *a, **kw: {"status": "ok", "results": []})
        self.assertEqual(empty["status"], "completed")
        failed = discover_candidates("C1", "A claim", safe_url_fn=safe_public_url,
                                     privacy=self.privacy(["public-search"]),
                                     search_fn=lambda *a, **kw: {"status": "error", "error": "offline"})
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["candidates"], [])

    def test_collect_preserves_verified_page_when_other_page_fails(self):
        def search(query, k, timeout):
            return {"status": "ok", "results": [
                {"url": "https://example.org/good", "title": "Good"},
                {"url": "https://example.org/down", "title": "Down"},
            ]}
        def fetch(url, **kw):
            if url.endswith("down"):
                return {"status": "error", "error": "unavailable"}
            return {"status": "ok", "final_url": url, "title": "Good", "published_at": None,
                    "text": "A specific public claim appeared here."}
        matcher = lambda claim, body: {"claim_id": "C1", "relation": "repeats",
                                       "evidence_ids": ["E1"],
                                       "quote": "A specific public claim"}
        out = collect_propagation_records("C1", "A specific public claim", search_fn=search,
                                          fetch_fn=fetch, safe_url_fn=safe_public_url,
                                          match_fn=matcher, allowed_evidence_ids=["E1"],
                                          privacy=self.privacy(["public-search", "public-web"]),
                                          page_limit=2)
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["verified_count"], 1)
        self.assertEqual(out["records"][0]["status"], "verified")

    def test_page_limit_marks_partial_without_dropping_candidates_count(self):
        def search(query, k, timeout):
            return {"status": "ok", "results": [
                {"url": "https://example.org/one"}, {"url": "https://example.org/two"}]}
        fetch = lambda url, **kw: {"status": "ok", "final_url": url,
                                   "title": "Public", "published_at": None,
                                   "text": "A specific public claim appeared here."}
        matcher = lambda claim, body: {"claim_id": "C1", "relation": "repeats",
                                       "evidence_ids": ["E1"],
                                       "quote": "A specific public claim"}
        out = collect_propagation_records("C1", "A specific public claim", search_fn=search,
                                          fetch_fn=fetch, safe_url_fn=safe_public_url,
                                          match_fn=matcher, allowed_evidence_ids=["E1"],
                                          privacy=self.privacy(["public-search", "public-web"]),
                                          page_limit=1)
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["candidate_count"], 2)
        self.assertEqual(out["verified_count"], 1)

    def test_cancel_stops_before_page_fetch(self):
        calls = []
        def search(query, k, timeout):
            return {"status": "ok", "results": [{"url": "https://example.org/post"}]}
        def fetch(url, **kw):
            calls.append(url)
            return {"status": "ok", "final_url": url, "title": "Public", "published_at": None,
                    "text": "A specific public claim appeared here."}
        out = collect_propagation_records("C1", "A specific public claim", search_fn=search,
                                          fetch_fn=fetch, safe_url_fn=safe_public_url,
                                          match_fn=lambda *_: {},
                                          privacy=self.privacy(["public-search", "public-web"]),
                                          cancel_check=lambda: True)
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(out["verified_count"], 0)
        self.assertEqual(calls, [])

    def test_invalid_search_url_is_discarded(self):
        out = discover_candidates("C1", "A claim", safe_url_fn=safe_public_url,
                                  privacy=self.privacy(["public-search"]),
                                  search_fn=lambda *a, **kw: {
                                      "status": "ok", "results": [{"url": "http://127.0.0.1/private"}]})
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["candidates"], [])

    def test_no_external_authorization_means_no_search_call(self):
        calls = []
        def search(*args, **kwargs):
            calls.append(True)
            return {"status": "ok", "results": []}
        with self.assertRaises(PrivacyAuthorizationError):
            discover_candidates("C1", "A claim", search_fn=search,
                                safe_url_fn=safe_public_url, privacy=self.privacy())
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
