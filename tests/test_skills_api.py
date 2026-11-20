# -*- coding: utf-8 -*-
"""技能偏好 API 测试：GET/PUT、默认值、坏文件降级、非法 400、路由挂载。"""

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

from mylocalai.assistant.skills import preferences as P  # noqa: E402


def _valid() -> dict:
    return {
        "schema_version": 1,
        "research": {"workflow_mode": "agent", "auto_verify_findings": False},
        "authority": {"workflow_mode": "pipeline"},
        "composite": {"enabled": True},
    }


class SkillsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.client = TestClient(webapp.app)

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="skills_api_test_")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "skills_preferences.json"
        patch = mock.patch.dict(os.environ, {P.PATH_ENV: str(self.path)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_get_defaults(self):
        resp = self.client.get("/api/skills/preferences")
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(data["research"]["workflow_mode"], "pipeline")
        self.assertEqual(data["authority"]["workflow_mode"], "pipeline")
        self.assertTrue(data["research"]["auto_verify_findings"])
        self.assertTrue(data["composite"]["enabled"])
        self.assertEqual(data["warning"], "")

    def test_put_roundtrip(self):
        resp = self.client.put("/api/skills/preferences", json=_valid())
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertTrue(self.path.is_file())
        got = self.client.get("/api/skills/preferences").json()
        self.assertEqual(got["research"]["workflow_mode"], "agent")
        self.assertFalse(got["research"]["auto_verify_findings"])
        self.assertEqual(got["authority"]["workflow_mode"], "pipeline")

    def test_put_invalid_mode_400(self):
        body = _valid()
        body["research"]["workflow_mode"] = "turbo"
        resp = self.client.put("/api/skills/preferences", json=body)
        self.assertEqual(400, resp.status_code, resp.text)
        self.assertIn("workflow_mode", resp.json()["detail"])

    def test_put_missing_section_400(self):
        resp = self.client.put("/api/skills/preferences", json={"schema_version": 1})
        self.assertEqual(400, resp.status_code, resp.text)

    def test_put_composite_disabled_forces_auto_off(self):
        body = _valid()
        body["research"]["auto_verify_findings"] = True
        body["composite"]["enabled"] = False
        resp = self.client.put("/api/skills/preferences", json=body)
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertFalse(data["research"]["auto_verify_findings"])
        self.assertIn("强制关闭", data["warning"])

    def test_bad_file_falls_back_with_warning(self):
        self.path.write_text("{bad json", encoding="utf-8")
        resp = self.client.get("/api/skills/preferences")
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(data["research"]["workflow_mode"], "pipeline")
        self.assertIn("读取失败", data["warning"])

    def test_route_registered_in_app(self):
        paths = self.client.get("/openapi.json").json().get("paths", {})
        self.assertIn("/api/skills/preferences", paths)

    def test_no_secret_fields_in_response(self):
        resp = self.client.get("/api/skills/preferences")
        self.assertNotIn("sk-", resp.text)
        self.assertNotIn("api_key", resp.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
