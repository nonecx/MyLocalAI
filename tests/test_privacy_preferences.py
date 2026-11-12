# -*- coding: utf-8 -*-
"""Offline tests for privacy preference validation and authorization gates."""
from __future__ import annotations

import copy
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.core.privacy import (  # noqa: E402
    DATA_CATEGORIES,
    DEFAULT_PREFERENCES,
    PrivacyAuthorizationError,
    PrivacyPreferences,
    _validated,
    load_preferences,
    save_preferences,
)


class _MemoryStream:
    def __init__(self):
        self.buffer = io.StringIO()

    def write(self, value):
        return self.buffer.write(value)

    def flush(self):
        return None

    def fileno(self):
        return 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class PrivacyPreferencesTests(unittest.TestCase):
    def test_missing_file_defaults_all_persistence_and_external_processing_off(self):
        with mock.patch.object(Path, "exists", return_value=False):
            prefs = load_preferences(Path("missing-prefs.json"))
        self.assertEqual([prefs.save_enabled(category) for category in DATA_CATEGORIES],
                         [False, False, False])
        self.assertFalse(prefs.external_allowed("provider-a"))
        self.assertEqual(prefs.retention_days("external_cache"), 7)

    def test_corrupt_file_fails_closed(self):
        with mock.patch.object(Path, "exists", return_value=True), \
                mock.patch.object(Path, "open", return_value=io.StringIO("{broken")):
            prefs = load_preferences(Path("corrupt-prefs.json"))
        self.assertTrue(prefs.warning)
        self.assertFalse(prefs.save_enabled("results"))
        self.assertFalse(prefs.external_allowed("provider-a"))

    def test_external_provider_requires_separate_allowlist(self):
        raw = copy.deepcopy(DEFAULT_PREFERENCES)
        raw.update(external_processing_enabled=True,
                   external_processing_providers=["provider-a"])
        prefs = PrivacyPreferences(_validated(raw))
        prefs.require_external("provider-a")
        with self.assertRaises(PrivacyAuthorizationError):
            prefs.require_external("provider-b")

    def test_local_model_url_is_local_but_public_model_needs_consent(self):
        prefs = PrivacyPreferences(_validated(copy.deepcopy(DEFAULT_PREFERENCES)))
        self.assertTrue(prefs.allows_provider_url("local", "http://127.0.0.1:11434/v1"))
        self.assertFalse(prefs.allows_provider_url("public", "https://api.example.com/v1"))

    def test_provider_allowlist_rejected_when_global_external_switch_off(self):
        raw = copy.deepcopy(DEFAULT_PREFERENCES)
        raw["external_processing_providers"] = ["provider-a"]
        with self.assertRaises(ValueError):
            _validated(raw)

    def test_external_cache_retention_has_hard_seven_day_maximum(self):
        raw = copy.deepcopy(DEFAULT_PREFERENCES)
        raw["retention_days"]["external_cache"] = 8
        with self.assertRaises(ValueError):
            _validated(raw)

    def test_atomic_writer_uses_temp_file_fsync_and_replace_without_payload_data(self):
        raw = copy.deepcopy(DEFAULT_PREFERENCES)
        raw["save_enabled"]["results"] = True
        stream = _MemoryStream()
        target = Path("prefs.json")
        with mock.patch.object(Path, "mkdir") as mkdir, \
                mock.patch("mylocalai.core.privacy.tempfile.mkstemp", return_value=(1, "prefs.tmp")) as mkstemp, \
                mock.patch("mylocalai.core.privacy.os.fdopen", return_value=stream), \
                mock.patch("mylocalai.core.privacy.os.fsync") as fsync, \
                mock.patch("mylocalai.core.privacy.os.replace") as replace:
            prefs = save_preferences(raw, target)
        self.assertTrue(prefs.save_enabled("results"))
        mkstemp.assert_called_once()
        fsync.assert_called_once_with(1)
        replace.assert_called_once_with("prefs.tmp", target)
        data = json.loads(stream.buffer.getvalue())
        self.assertNotIn("text", data)
        self.assertNotIn("claim", data)

    def test_unknown_category_rejected(self):
        prefs = PrivacyPreferences(_validated(copy.deepcopy(DEFAULT_PREFERENCES)))
        with self.assertRaises(ValueError):
            prefs.save_enabled("conversation")


if __name__ == "__main__":
    unittest.main()
