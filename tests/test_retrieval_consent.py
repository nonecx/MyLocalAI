# -*- coding: utf-8 -*-
"""Search aggregation consent boundary tests."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.core.privacy import DEFAULT_PREFERENCES, PrivacyPreferences, _validated  # noqa: E402
from mylocalai.retrieval import search as search_mod  # noqa: E402


class RetrievalConsentTests(unittest.TestCase):
    def test_default_providers_are_blocked_without_external_consent(self):
        raw = copy.deepcopy(DEFAULT_PREFERENCES)
        prefs = PrivacyPreferences(_validated(raw))
        with mock.patch("mylocalai.core.privacy.load_preferences", return_value=prefs), \
                mock.patch.object(search_mod, "default_providers",
                                  side_effect=AssertionError("must not build providers")):
            out = search_mod.search_all("query")
        self.assertTrue(out["blocked"])
        self.assertEqual(out["results"], [])
        self.assertEqual(out["error"], "external_processing_not_authorized")

    def test_explicit_provider_injection_is_available_for_local_adapters(self):
        class Stub:
            name = "stub"
            def search(self, query, k):
                return [], None
        out = search_mod.search_all("query", providers=[Stub()])
        self.assertFalse(out.get("blocked", False))
        self.assertEqual(out["engines"]["stub"]["ok"], True)


if __name__ == "__main__":
    unittest.main()
