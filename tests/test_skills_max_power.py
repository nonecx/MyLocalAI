# -*- coding: utf-8 -*-
"""最大功率模式测试：规则包络、服务工厂覆盖与回合时限。"""
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

from mylocalai.assistant import loop as loop_mod  # noqa: E402
from mylocalai.assistant.loop import AssistantLoop  # noqa: E402
from mylocalai.assistant.skills import preferences as pref  # noqa: E402
from mylocalai.assistant.skills import pipeline as evidence_pipe  # noqa: E402
from mylocalai.assistant.skills.authority import pipeline as authority_pipe  # noqa: E402
from mylocalai.assistant.skills.guide import guide as guide_pipe  # noqa: E402
from mylocalai.assistant.skills.research import pipeline as research_pipe  # noqa: E402
from mylocalai.assistant.skills.source import pipeline as source_pipe  # noqa: E402
from mylocalai.assistant.skills.statistics import pipeline as statistics_pipe  # noqa: E402
from mylocalai.assistant.skills.timeline import pipeline as timeline_pipe  # noqa: E402
from mylocalai.assistant.skills.comparison import compare as compare_pipe  # noqa: E402


class TestMaxPowerRuleBundles(unittest.TestCase):
    def test_every_bundle_uses_existing_safe_upper_bounds(self):
        modules = (evidence_pipe, research_pipe, authority_pipe, source_pipe,
                   statistics_pipe, timeline_pipe, guide_pipe, compare_pipe)
        for module in modules:
            limits = getattr(module, "_RULE_LIMITS")
            bundle = module.MAX_POWER_RULES
            for key, value in bundle.items():
                self.assertIn(key, limits, f"{module.__name__}: unknown {key}")
                self.assertEqual(value, limits[key][1], f"{module.__name__}: {key}")
            for key in ("io_timeout_s", "max_page_bytes", "max_text_chars", "max_redirect_hops", "max_quote_chars"):
                self.assertNotIn(key, bundle, f"{module.__name__}: unsafe key raised")

    def test_expected_examples(self):
        self.assertEqual(evidence_pipe.MAX_POWER_RULES["max_pages"], 200)
        self.assertEqual(research_pipe.MAX_POWER_RULES["total_deadline_s"], 3600)
        self.assertEqual(source_pipe.MAX_POWER_RULES["max_pages"], 4)
        self.assertEqual(compare_pipe.MAX_POWER_RULES["max_versions"], 50)


class TestLoopPowerMode(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "skills.json"
        self.old = os.environ.get(pref.PATH_ENV)
        os.environ[pref.PATH_ENV] = str(self.path)

    def tearDown(self):
        if self.old is None:
            os.environ.pop(pref.PATH_ENV, None)
        else:
            os.environ[pref.PATH_ENV] = self.old
        self.tmp.cleanup()

    def _save(self, mode):
        pref.save_preferences({
            "power_mode": mode,
            "research": {"workflow_mode": "pipeline", "auto_verify_findings": True},
            "authority": {"workflow_mode": "pipeline"},
            "composite": {"enabled": True},
        }, self.path)

    def test_power_mode_switches_all_skill_factory_rules(self):
        loop = AssistantLoop(rules={})
        names = ("evidence_skills", "research_skills", "authority_skills",
                 "source_skills", "guide_skills", "statistics_skills", "timeline_skills")
        self._save("normal")
        normal = {name: loop.services[name]().rules for name in names}
        self._save("max")
        maximum = {name: loop.services[name]().rules for name in names}
        for name in names:
            self.assertEqual(maximum[name]["total_deadline_s"], 3600, name)
        self.assertEqual(normal["research_skills"]["max_pages"], 10)
        self.assertEqual(maximum["research_skills"]["max_pages"], 200)
        self.assertEqual(normal["evidence_skills"]["total_deadline_s"], 90.0)

    def test_turn_deadline_power_mode_and_env_override(self):
        loop = AssistantLoop(rules={"turn_deadline_s": 123})
        self._save("max")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CHAT_TURN_DEADLINE_S", None)
            self.assertEqual(loop._turn_budget_s(), 3600.0)
        with mock.patch.dict(os.environ, {"CHAT_TURN_DEADLINE_S": "45"}):
            self.assertEqual(loop._turn_budget_s(), 45.0)
        self._save("normal")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CHAT_TURN_DEADLINE_S", None)
            self.assertEqual(loop._turn_budget_s(), 123.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
