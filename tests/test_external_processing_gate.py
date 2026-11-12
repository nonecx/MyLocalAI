# -*- coding: utf-8 -*-
"""External-processing consent is checked before outbound provider work."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills import propagation  # noqa: E402
from mylocalai.core.privacy import (  # noqa: E402
    DEFAULT_PREFERENCES,
    PrivacyAuthorizationError,
    PrivacyPreferences,
    _validated,
)
from mylocalai.llm import llm_client  # noqa: E402
from mylocalai.web.privacy_api import get_preferences  # noqa: E402


def prefs(providers=()):
    raw = copy.deepcopy(DEFAULT_PREFERENCES)
    if providers:
        raw.update(external_processing_enabled=True,
                   external_processing_providers=list(providers))
    return PrivacyPreferences(_validated(raw))


class ExternalProcessingGateTests(unittest.TestCase):
    def test_llm_gate_rejects_before_client_creation(self):
        with mock.patch("mylocalai.core.privacy.load_preferences", return_value=prefs()), \
                mock.patch.object(llm_client, "_provider_names", return_value=["public"]), \
                mock.patch.object(llm_client, "providers", return_value={
                    "public": {"base_url_env": "", "base_url": "https://api.example.com/v1"}}), \
                mock.patch.object(llm_client, "create_client",
                                  side_effect=AssertionError("must not construct client")):
            out = llm_client._chat_raw_providers("system", "sensitive input", 0.0, 100)
        self.assertIn("PrivacyAuthorizationError", out[1])
        self.assertIn("public", out[1])

    def test_local_llm_endpoint_is_permitted_without_external_consent(self):
        local = prefs()
        self.assertTrue(local.allows_provider_url("local", "http://127.0.0.1:11434/v1"))
        self.assertFalse(local.allows_provider_url("public", "https://provider.example/v1"))

    def test_propagation_search_rejects_before_search_function(self):
        calls = []
        def search(*args, **kwargs):
            calls.append("search")
            return {"status": "ok", "results": []}
        with self.assertRaises(PrivacyAuthorizationError):
            propagation.discover_candidates("C1", "a claim", search_fn=search,
                                            safe_url_fn=propagation.safe_public_url,
                                            privacy=prefs())
        self.assertEqual(calls, [])

    def test_propagation_fetch_rejects_before_fetch_function(self):
        candidate = {"candidate_id": "P1", "claim_text": "a specific claim",
                     "url": "https://example.org/post", "status": "candidate"}
        calls = []
        def fetch(*args, **kwargs):
            calls.append("fetch")
            raise AssertionError("must not fetch")
        with self.assertRaises(PrivacyAuthorizationError):
            propagation.verify_candidate("C1", candidate, fetch_fn=fetch,
                                          safe_url_fn=propagation.safe_public_url,
                                          match_fn=lambda *_: {}, allowed_evidence_ids=["E1"],
                                          privacy=prefs())
        self.assertEqual(calls, [])

    def test_settings_default_external_processing_off(self):
        with mock.patch("mylocalai.web.privacy_api.load_preferences", return_value=prefs()):
            response = get_preferences()
        self.assertFalse(response["external_processing_enabled"])
        self.assertEqual(response["external_processing_providers"], [])


if __name__ == "__main__":
    unittest.main()
