# -*- coding: utf-8 -*-
"""平台规则加载测试（P1）：默认值、坏配置降级、整套替换/增量、权重校验。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.platform import rules as pr  # noqa: E402


class TestDefaults(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_builtin_rules_shape(self):
        """内置默认结构完整：必填键齐全、权重总和为 1、阈值单调。"""
        d = pr.DEFAULT_RULES
        for key in ("enabled", "scan_interval_s", "max_queries_per_scan",
                    "max_candidates_per_query", "screen", "deep_verify", "dedup",
                    "risk", "watch_queries", "rss_sources"):
            self.assertIn(key, d)
        total = sum(d["risk"]["weights"].values())
        self.assertAlmostEqual(total, 1.0, places=6)
        levels = d["risk"]["levels"]
        self.assertLessEqual(levels["suspect"], levels["high"])
        self.assertLessEqual(levels["high"], levels["urgent"])

    def test_load_default_file_matches_builtin(self):
        """包内 platform_rules.json 与内置默认同口径（防文件与代码漂移）。"""
        loaded, warnings = pr.load_rules()
        self.assertEqual(warnings, [])
        self.assertFalse(loaded["enabled"])
        self.assertEqual(loaded["scan_interval_s"], 900)
        self.assertEqual(loaded["deep_verify"]["max_per_scan"], 3)
        self.assertEqual(loaded["risk"]["levels"]["urgent"], 80)
        self.assertEqual(loaded["watch_queries"], [])
        self.assertEqual(loaded["rss_sources"], [])

    def test_load_never_raises_on_missing_file(self):
        """指向不存在的路径：退内置默认 + 告警，不抛。"""
        loaded, warnings = pr.load_rules(str(Path(self.tmp.name) / "nope.json"))
        self.assertEqual(loaded, json.loads(json.dumps(pr.DEFAULT_RULES)))
        self.assertTrue(warnings)


class TestBadConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, data) -> Path:
        p = Path(self.tmp.name) / "rules.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    def test_root_not_object_falls_back(self):
        p = self._write([1, 2, 3])
        loaded, warnings = pr.load_rules(str(p))
        self.assertEqual(loaded, json.loads(json.dumps(pr.DEFAULT_RULES)))
        self.assertTrue(any("根节点" in w for w in warnings))

    def test_broken_json_falls_back(self):
        p = Path(self.tmp.name) / "rules.json"
        p.write_text("{ not json", encoding="utf-8")
        loaded, warnings = pr.load_rules(str(p))
        self.assertEqual(loaded, json.loads(json.dumps(pr.DEFAULT_RULES)))
        self.assertTrue(warnings)

    def test_missing_required_section_warns(self):
        p = self._write({"enabled": True})
        loaded, warnings = pr.load_rules(str(p))
        self.assertTrue(loaded["enabled"])             # 该段仍生效
        self.assertTrue(any("screen" in w for w in warnings))

    def test_weights_not_summing_to_one_fall_back(self):
        p = self._write({"risk": {"weights": {"heat": 0.5, "growth": 0.5,
                                              "breadth": 0.5, "rumor": 0.5}}})
        loaded, warnings = pr.load_rules(str(p))
        self.assertEqual(loaded["risk"]["weights"], pr.DEFAULT_RULES["risk"]["weights"])
        self.assertTrue(any("weights" in w for w in warnings))

    def test_weights_missing_key_fall_back(self):
        p = self._write({"risk": {"weights": {"heat": 1.0}}})
        loaded, warnings = pr.load_rules(str(p))
        self.assertEqual(loaded["risk"]["weights"], pr.DEFAULT_RULES["risk"]["weights"])
        self.assertTrue(any("weights" in w for w in warnings))

    def test_levels_not_monotonic_fall_back(self):
        p = self._write({"risk": {"levels": {"suspect": 90, "high": 50, "urgent": 80}}})
        loaded, warnings = pr.load_rules(str(p))
        self.assertEqual(loaded["risk"]["levels"], pr.DEFAULT_RULES["risk"]["levels"])
        self.assertTrue(any("levels" in w for w in warnings))

    def test_bad_scalars_fall_back_per_key(self):
        p = self._write({"scan_interval_s": -5, "max_queries_per_scan": "abc",
                         "screen": {"heat_screen_e": "x"}})
        loaded, _ = pr.load_rules(str(p))
        self.assertEqual(loaded["scan_interval_s"], pr.DEFAULT_RULES["scan_interval_s"])
        self.assertEqual(loaded["max_queries_per_scan"],
                         pr.DEFAULT_RULES["max_queries_per_scan"])
        self.assertEqual(loaded["screen"]["heat_screen_e"],
                         pr.DEFAULT_RULES["screen"]["heat_screen_e"])

    def test_queries_and_rss_normalized(self):
        p = self._write({
            "watch_queries": [" 话题A ", {"query": "话题B", "enabled": False, "note": "n"},
                              "", {"no_query": 1}, 42],
            "rss_sources": [{"name": "s", "url": "https://example.com/feed.xml"},
                            {"name": "bad", "url": "ftp://x"}, "junk"],
        })
        loaded, _ = pr.load_rules(str(p))
        self.assertEqual(loaded["watch_queries"],
                         [{"query": "话题A", "enabled": True, "note": ""},
                          {"query": "话题B", "enabled": False, "note": "n"}])
        self.assertEqual(loaded["rss_sources"],
                         [{"name": "s", "url": "https://example.com/feed.xml",
                           "enabled": True}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
