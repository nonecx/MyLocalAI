# -*- coding: utf-8 -*-
"""In-memory SQLite tests for retention classification and expiry cleanup."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.session_store import SessionStore  # noqa: E402
from mylocalai.core.privacy import PrivacyPreferences, _validated, DEFAULT_PREFERENCES  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402

NOW = "2026-11-12T12:00:00+00:00"
PAST = "2026-11-10T12:00:00+00:00"


class PrivacyLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.store = DossierStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_purge_is_category_isolated_and_idempotent(self):
        did_raw = self.store.create_dossier("raw", "raw original", save_result=False,
                                            save_raw_input=True, raw_input_retention_days=1)
        did_result = self.store.create_dossier("result", "", save_result=True,
                                               result_retention_days=1, save_raw_input=False)
        did_cache = self.store.create_dossier("cache", "", save_result=False,
                                              save_raw_input=False)
        with self.store._lock, self.store._conn:
            self.store._conn.execute(
                "UPDATE dossiers SET raw_input_expires_at=? WHERE id=?", (PAST, did_raw))
            self.store._conn.execute(
                "UPDATE dossiers SET result_expires_at=? WHERE id=?", (PAST, did_result))
        self.store.add_evidence(did_cache, 1, [{"url": "https://example.org/a",
                                                "snippet": "external excerpt",
                                                "fetched_at": PAST}],
                                save_external_cache=True, cache_retention_days=1)
        self.assertEqual(self.store.evidence(did_cache)[0]["snippet"], "external excerpt")
        stats = self.store.purge_expired(now=NOW)
        self.assertEqual(stats, {"results_deleted": 1, "input_rows_cleared": 1,
                                 "cache_rows_cleared": 1})
        self.assertIsNone(self.store.get_dossier(did_result))
        self.assertEqual(self.store.get_dossier(did_raw)["input_text"], "")
        cache_row = self.store.evidence(did_cache)[0]
        self.assertEqual(cache_row["snippet"], "")
        self.assertEqual(cache_row["url"], "https://example.org/a")
        self.assertEqual(self.store.purge_expired(now=NOW),
                         {"results_deleted": 0, "input_rows_cleared": 0,
                          "cache_rows_cleared": 0})

    def test_result_retention_is_not_extended_by_conclusion_versions(self):
        did = self.store.create_dossier("result", "", save_result=True,
                                        result_retention_days=1, save_raw_input=False)
        expiry = self.store.get_dossier(did)["result_expires_at"]
        self.store.add_conclusion(did, "x", None, result_retention_days=30)
        self.assertEqual(self.store.get_dossier(did)["result_expires_at"], expiry)

    def test_session_raw_input_off_persists_no_message_or_tool_arguments(self):
        privacy = PrivacyPreferences(_validated(dict(DEFAULT_PREFERENCES)))
        sessions = SessionStore(":memory:", privacy=privacy)
        try:
            sid = sessions.create_session("session")
            sessions.add_message(sid, "user", "sensitive user text")
            rows = sessions.messages(sid)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["content"], "")
            run = sessions.start_run(sid, "verify_claim", {"text": "sensitive tool arg"})
            self.assertEqual(sessions.get_run(run)["args"], {"text": ""})
        finally:
            sessions.close()

    def test_session_raw_and_result_message_expiry_are_independent(self):
        raw = copy.deepcopy(DEFAULT_PREFERENCES)
        raw["save_enabled"].update(raw_input=True, results=True)
        prefs = PrivacyPreferences(_validated(raw))
        sessions = SessionStore(":memory:", privacy=prefs)
        try:
            sid = sessions.create_session("session")
            raw_id = sessions.add_message(sid, "user", "claim text")
            result_id = sessions.add_message(sid, "assistant", "result text")
            rows = {row["id"]: row for row in sessions.messages(sid)}
            self.assertTrue(rows[raw_id]["raw_input_expires_at"])
            self.assertIsNone(rows[raw_id]["result_expires_at"])
            self.assertTrue(rows[result_id]["result_expires_at"])
            self.assertIsNone(rows[result_id]["raw_input_expires_at"])
        finally:
            sessions.close()


if __name__ == "__main__":
    unittest.main()
