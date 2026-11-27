# -*- coding: utf-8 -*-
"""技能偏好 REST API 测试（临时路径隔离，无副作用）。"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))


class SkillsPreferencesApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.client = TestClient(webapp.app)

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "skills.json"
        patch = mock.patch.dict(os.environ, {"MYLOCALAI_SKILLS_PREFERENCES_PATH": str(self.path)})
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def _body(mode="max"):
        return {"schema_version": 1, "power_mode": mode,
                "research": {"workflow_mode": "pipeline", "auto_verify_findings": True},
                "authority": {"workflow_mode": "pipeline"},
                "composite": {"enabled": True}}

    def test_get_returns_default_mode(self):
        response = self.client.get("/api/skills/preferences")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["power_mode"], "normal")

    def test_put_roundtrips_max_mode(self):
        response = self.client.put("/api/skills/preferences", json=self._body())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["power_mode"], "max")
        loaded = self.client.get("/api/skills/preferences")
        self.assertEqual(loaded.json()["power_mode"], "max")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["power_mode"], "max")

    def test_put_invalid_mode_returns_400_without_write(self):
        response = self.client.put("/api/skills/preferences", json=self._body("unlimited"))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
