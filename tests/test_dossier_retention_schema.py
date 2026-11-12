# -*- coding: utf-8 -*-
"""Retention schema migration checks without filesystem temp directories."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.dossier.store import SCHEMA_VERSION, connect, migrate  # noqa: E402


class RetentionSchemaTests(unittest.TestCase):
    def test_memory_migration_adds_retention_columns_and_is_idempotent(self):
        conn = connect(":memory:")
        try:
            self.assertEqual(migrate(conn), SCHEMA_VERSION)
            self.assertEqual(migrate(conn), 0)
            expected = {
                "dossiers": {"result_expires_at", "raw_input_expires_at", "claim_excerpt"},
                "sessions": {"raw_input_expires_at"},
                "messages": {"raw_input_expires_at", "result_expires_at"},
                "tool_runs": {"input_expires_at", "result_expires_at"},
                "evidence_entries": {"cache_expires_at"},
            }
            for table, columns in expected.items():
                got = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                self.assertTrue(columns.issubset(got), table)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
