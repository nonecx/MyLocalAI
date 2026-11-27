# -*- coding: utf-8 -*-
"""HTTP path-id error semantics across web resource routers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestWebPathIdSemantics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
            import mylocalai.web.app as webapp
        except Exception as exc:  # pragma: no cover - dependency guard
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.client = TestClient(webapp.app)

    def assert_missing_resource(self, method: str, path: str, **kwargs):
        response = self.client.request(method, path, **kwargs)
        self.assertEqual(response.status_code, 404, path)
        self.assertIn("不存在", response.json().get("detail", ""), path)

    def test_profile_and_subscription_ids_use_404_for_non_integer(self):
        self.assert_missing_resource("PUT", "/api/profiles/abc",
                                     json={"display_name": "x"})
        self.assert_missing_resource("POST", "/api/profiles/abc/activate")
        self.assert_missing_resource("DELETE", "/api/profiles/abc")
        self.assert_missing_resource("PUT", "/api/subscriptions/abc",
                                     json={"enabled": True})
        self.assert_missing_resource("DELETE", "/api/subscriptions/abc")
        self.assert_missing_resource("POST", "/api/subscriptions/abc/run-now")
        self.assert_missing_resource("GET", "/api/subscriptions/abc")
        self.assert_missing_resource("GET", "/api/subscriptions/abc/runs")

    def test_platform_ids_use_404_for_non_integer(self):
        self.assert_missing_resource("DELETE", "/api/platform/watch/abc")
        self.assert_missing_resource("PUT", "/api/platform/watch/abc",
                                     json={"domain": ""})
        self.assert_missing_resource("PUT", "/api/platform/sources/abc",
                                     json={"note": "x"})
        self.assert_missing_resource("DELETE", "/api/platform/sources/abc")
        self.assert_missing_resource("POST", "/api/platform/sources/abc/test")
        self.assert_missing_resource("POST", "/api/platform/sources/abc")
        self.assert_missing_resource("GET", "/api/platform/scans/abc")
        self.assert_missing_resource("GET", "/api/platform/reports/abc")


if __name__ == "__main__":
    unittest.main()
