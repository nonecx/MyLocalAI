# -*- coding: utf-8 -*-
"""技能偏好测试：默认值、坏配置降级、闭集校验、复合关闭联动、原子写。"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills import preferences as P  # noqa: E402


class TestPreferencesLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "skills_preferences.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_defaults(self):
        out = P.load_preferences(self.path)
        self.assertEqual(out["power_mode"], "normal")
        self.assertFalse(P.max_power_enabled(out))
        self.assertEqual(out["research"]["workflow_mode"], "pipeline")
        self.assertEqual(out["authority"]["workflow_mode"], "pipeline")
        self.assertTrue(out["research"]["auto_verify_findings"])
        self.assertTrue(out["composite"]["enabled"])
        self.assertEqual(out["warning"], "")

    def test_bad_json_falls_back_with_warning(self):
        self.path.write_text("{not json", encoding="utf-8")
        out = P.load_preferences(self.path)
        self.assertEqual(out["research"]["workflow_mode"], "pipeline")
        self.assertIn("读取失败", out["warning"])

    def test_invalid_mode_falls_back(self):
        self.path.write_text(json.dumps({
            "schema_version": 1,
            "research": {"workflow_mode": "turbo", "auto_verify_findings": False},
            "authority": {"workflow_mode": "agent"},
            "composite": {"enabled": True},
        }), encoding="utf-8")
        out = P.load_preferences(self.path)
        self.assertEqual(out["research"]["workflow_mode"], "pipeline")
        self.assertEqual(out["authority"]["workflow_mode"], "agent")
        self.assertFalse(out["research"]["auto_verify_findings"])
        self.assertIn("research.workflow_mode", out["warning"])

    def test_invalid_power_mode_falls_back(self):
        self.path.write_text(json.dumps({"power_mode": "unlimited"}), encoding="utf-8")
        out = P.load_preferences(self.path)
        self.assertEqual(out["power_mode"], "normal")
        self.assertIn("power_mode", out["warning"])

    def test_max_power_mode_roundtrips(self):
        self.path.write_text(json.dumps({"power_mode": "max"}), encoding="utf-8")
        out = P.load_preferences(self.path)
        self.assertEqual(out["power_mode"], "max")
        self.assertTrue(P.max_power_enabled(out))

    def test_unknown_keys_ignored(self):
        self.path.write_text(json.dumps({
            "schema_version": 1,
            "research": {"workflow_mode": "agent", "mystery": 1},
            "authority": {}, "composite": {}, "extra": {"x": 1},
        }), encoding="utf-8")
        out = P.load_preferences(self.path)
        self.assertEqual(out["research"]["workflow_mode"], "agent")
        self.assertNotIn("mystery", out["research"])
        self.assertNotIn("extra", out)

    def test_composite_disabled_forces_auto_off(self):
        self.path.write_text(json.dumps({
            "schema_version": 1,
            "research": {"workflow_mode": "pipeline", "auto_verify_findings": True},
            "authority": {}, "composite": {"enabled": False},
        }), encoding="utf-8")
        out = P.load_preferences(self.path)
        self.assertFalse(out["composite"]["enabled"])
        self.assertFalse(out["research"]["auto_verify_findings"])
        self.assertIn("强制关闭", out["warning"])


class TestPreferencesSave(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "skills_preferences.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_creates_parent_and_roundtrips(self):
        out = P.save_preferences({
            "schema_version": 1,
            "research": {"workflow_mode": "agent", "auto_verify_findings": False},
            "authority": {"workflow_mode": "agent"},
            "composite": {"enabled": True},
        }, self.path)
        self.assertEqual(out["research"]["workflow_mode"], "agent")
        self.assertTrue(self.path.is_file())
        loaded = P.load_preferences(self.path)
        self.assertEqual(loaded["authority"]["workflow_mode"], "agent")
        self.assertEqual(loaded["warning"], "")

    def test_invalid_power_mode_raises(self):
        with self.assertRaises(ValueError):
            P.save_preferences({"power_mode": "unlimited"}, self.path)
        self.assertFalse(self.path.exists())

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            P.save_preferences({
                "research": {"workflow_mode": "fast", "auto_verify_findings": True},
                "authority": {"workflow_mode": "pipeline"},
                "composite": {"enabled": True},
            }, self.path)
        self.assertFalse(self.path.exists())

    def test_invalid_bool_raises(self):
        with self.assertRaises(ValueError):
            P.save_preferences({
                "research": {"workflow_mode": "pipeline", "auto_verify_findings": "yes"},
                "authority": {"workflow_mode": "pipeline"},
                "composite": {"enabled": True},
            }, self.path)

    def test_composite_disabled_saves_auto_off(self):
        out = P.save_preferences({
            "research": {"workflow_mode": "pipeline", "auto_verify_findings": True},
            "authority": {"workflow_mode": "pipeline"},
            "composite": {"enabled": False},
        }, self.path)
        self.assertFalse(out["research"]["auto_verify_findings"])
        self.assertIn("强制关闭", out["warning"])

    def test_atomic_write_leaves_no_temp_file(self):
        P.save_preferences({
            "research": {"workflow_mode": "pipeline", "auto_verify_findings": True},
            "authority": {"workflow_mode": "pipeline"},
            "composite": {"enabled": True},
        }, self.path)
        names = [p.name for p in self.path.parent.iterdir()]
        self.assertIn("skills_preferences.json", names)
        self.assertFalse([n for n in names if n.startswith(".skills-prefs-")])


class TestDefaultPath(unittest.TestCase):
    def test_env_override(self):
        old = os.environ.get(P.PATH_ENV)
        os.environ[P.PATH_ENV] = "C:/tmp/custom-skills.json"
        try:
            self.assertEqual(str(P.default_path()).replace("\\", "/"), "C:/tmp/custom-skills.json")
        finally:
            if old is None:
                os.environ.pop(P.PATH_ENV, None)
            else:
                os.environ[P.PATH_ENV] = old

    def test_default_path_is_data_json(self):
        old = os.environ.pop(P.PATH_ENV, None)
        try:
            self.assertTrue(str(P.default_path()).endswith("data\\skills_preferences.json")
                            or str(P.default_path()).endswith("data/skills_preferences.json"))
        finally:
            if old is not None:
                os.environ[P.PATH_ENV] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
