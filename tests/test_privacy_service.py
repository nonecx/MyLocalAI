# -*- coding: utf-8 -*-
"""DossierService persistence gates using in-memory SQLite."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.core.privacy import DEFAULT_PREFERENCES, PrivacyPreferences, _validated  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402


class PrivacyServiceTests(unittest.TestCase):
    def prefs(self, *, results=False, raw=False, cache=False):
        raw_cfg = copy.deepcopy(DEFAULT_PREFERENCES)
        raw_cfg["save_enabled"].update(results=results, raw_input=raw, external_cache=cache)
        return PrivacyPreferences(_validated(raw_cfg))

    def result(self):
        return {"label": "x", "risk_score": None, "evidence_book": {
            "entries": [{"url": "https://example.org/a", "domain": "example.org",
                          "snippet": "external text", "fetched_at": "2026-11-10T00:00:00+00:00"}]}}

    def test_results_off_does_not_create_dossier(self):
        store = DossierStore(":memory:")
        try:
            svc = DossierService(store=store, privacy=self.prefs(results=False, raw=True))
            self.assertIsNone(svc.start_dossier("full original", claim_excerpt="minimal claim"))
            with self.assertRaises(PermissionError):
                svc.record_heat(1, {"x": 1})
            with self.assertRaises(PermissionError):
                svc.record_opinions(1, {})
            with self.assertRaises(PermissionError):
                svc.record_deep_read(1, {})
            with self.assertRaises(PermissionError):
                svc.record_speech_analysis(1, {})
        finally:
            store.close()

    def test_results_on_raw_off_keeps_only_claim_excerpt(self):
        store = DossierStore(":memory:")
        try:
            svc = DossierService(store=store, privacy=self.prefs(results=True, raw=False, cache=False))
            dossier_id = svc.start_dossier("full original secret", claim_excerpt="minimal claim")
            row = store.get_dossier(dossier_id)
            self.assertEqual(row["input_text"], "")
            self.assertEqual(row["claim_excerpt"], "minimal claim")
            out = svc.record_analysis(dossier_id, self.result())
            self.assertEqual(out["version"], 1)
            self.assertEqual(store.evidence(dossier_id)[0]["snippet"], "")
        finally:
            store.close()

    def test_results_and_cache_on_retains_snippet_with_expiry(self):
        store = DossierStore(":memory:")
        try:
            svc = DossierService(store=store, privacy=self.prefs(results=True, raw=True, cache=True))
            dossier_id = svc.start_dossier("full original")
            svc.record_analysis(dossier_id, self.result())
            evidence = store.evidence(dossier_id)[0]
            self.assertEqual(evidence["snippet"], "external text")
            self.assertTrue(evidence["cache_expires_at"])
            self.assertTrue(store.get_dossier(dossier_id)["result_expires_at"])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
