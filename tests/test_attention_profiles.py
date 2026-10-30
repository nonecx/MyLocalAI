# -*- coding: utf-8 -*-
"""关注度参数档位（profile）机制测试。

核心约束：
  1. 默认档 "spec" 必须与用户指定的原始参数完全一致 —— 引入档位机制
      不能改变任何既有行为。
  2. 非法档位名必须退回默认档并给出中文告警，不能抛异常。
  3. 候选档 "reachable" 必须真的改变分数分布（否则没有存在意义）。
  4. CLI / HTTP 层要能透传档位参数。
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from attn_agent import attention as A  # noqa: E402

# 2026-10-26 实测的五个真实话题（x, y, z）
TOPICS = [
    ("老干妈回应AI裁员传闻", 30, 48316000, 149),
    ("台湾艺人阿雅公开表态", 5, 12506705, 302),
    ("长鑫存储硬气拒绝苹果", 11, 13822047, 42),
    ("承包户花生地遭村民捡拾", 3, 2067344, 129),
    ("男子出狱砍死妻子二审", 6, 3766947, 1),
]


class TestProfileDefaults(unittest.TestCase):
    """默认档必须与改动前完全一致。"""

    def test_default_profile_is_spec(self):
        self.assertEqual(A.DEFAULT_PROFILE, "spec")

    def test_spec_profile_equals_original_defaults(self):
        spec = A.PROFILES["spec"]
        self.assertEqual(spec["params"], A.DEFAULT_PARAMS)
        self.assertEqual(spec["bands"], A.DEFAULT_BANDS)
        self.assertEqual(spec["deep_read_threshold"], A.DEEP_READ_THRESHOLD)

    def test_load_profile_default_matches_default_params(self):
        params, warnings = A.load_profile()
        self.assertEqual(warnings, [])
        self.assertEqual(params, A.DEFAULT_PARAMS)

    def test_load_profile_none_matches_default(self):
        p1, _ = A.load_profile(None)
        p2, _ = A.load_profile("")
        self.assertEqual(p1, A.DEFAULT_PARAMS)
        self.assertEqual(p2, A.DEFAULT_PARAMS)

    def test_explicit_spec_matches_default(self):
        params, warnings = A.load_profile("spec")
        self.assertEqual(warnings, [])
        self.assertEqual(params, A.DEFAULT_PARAMS)


class TestProfileValidation(unittest.TestCase):
    """非法档位必须安全回退。"""

    def test_unknown_profile_falls_back_with_warning(self):
        params, warnings = A.load_profile("no-such-profile")
        self.assertEqual(params, A.DEFAULT_PARAMS)
        self.assertTrue(warnings)
        self.assertTrue(any("不存在" in w for w in warnings))

    def test_profile_bands_fallback(self):
        self.assertEqual(A.profile_bands("no-such"), A.DEFAULT_BANDS)
        self.assertEqual(A.profile_threshold("no-such"), A.DEEP_READ_THRESHOLD)

    def test_all_profiles_are_valid(self):
        for name in A.list_profiles():
            params, warnings = A.load_profile(name)
            self.assertEqual(warnings, [], f"{name}: {warnings}")
            self.assertEqual(A.validate_params(params), [])


class TestReachableProfile(unittest.TestCase):
    """候选档必须真的改变分数分布，否则没有存在意义。"""

    def test_scores_differ_from_default(self):
        """同一组输入在两个档位下分数必须不同。"""
        for _, x, y, z in TOPICS:
            spec = A.attention_score(x, y, z, A.PROFILES["spec"]["params"])
            reach = A.attention_score(x, y, z, A.PROFILES["reachable"]["params"])
            self.assertNotAlmostEqual(
                spec, reach, places=6,
                msg=f"x={x},y={y},z={z} 两档分数相同，档位没有意义")

    def test_reachable_raises_midrange_cases(self):
        """候选档应让"千万阅读/千篇/十万留言"这类中高热度案例跨过阈值。"""
        params = A.PROFILES["reachable"]["params"]
        score = A.attention_score(1000, 1e7, 1e5, params)
        self.assertGreater(score, 66.0)

    def test_low_traffic_stays_below(self):
        """候选档不应让低热度话题（极少文章、无互动）也触发深读。"""
        params = A.PROFILES["reachable"]["params"]
        score = A.attention_score(3, 2067344, 129, params)
        self.assertLess(score, 66.0)

    def test_reachable_weakens_y_dominance(self):
        """候选档应降低 y 项的占比（这是调参的核心目标）。"""
        def y_share(params):
            d = A.score_detail(1000, 1e7, 1e5, params)
            return d["term_y"] / d["S"]

        spec_share = y_share(A.PROFILES["spec"]["params"])
        reach_share = y_share(A.PROFILES["reachable"]["params"])
        self.assertLess(reach_share, spec_share,
                        f"y 项占比未下降: spec={spec_share:.3f} reach={reach_share:.3f}")


class TestCliProfileFlag(unittest.TestCase):
    """命令行 --profile 开关。"""

    def _run(self, argv):
        import subprocess
        import sys as _sys
        proc = subprocess.run(
            [_sys.executable, "-X", "utf8", "-m", "attn_agent.attention"] + argv,
            capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        return proc.returncode, proc.stdout, proc.stderr

    def test_profile_flag_changes_score(self):
        args = ["--x", "1000", "--y", "100000000", "--z", "1000000", "--json"]
        rc1, out1, _ = self._run(args)
        rc2, out2, _ = self._run(args + ["--profile", "reachable"])
        self.assertEqual((rc1, rc2), (0, 0))
        s1 = json.loads(out1)["score"]
        s2 = json.loads(out2)["score"]
        self.assertNotEqual(s1, s2)

    def test_bad_profile_falls_back(self):
        """非法档位名：告警 + 回退默认档，且结果与不传 profile 一致。"""
        rc, out, err = self._run(
            ["--x", "1000", "--y", "100000", "--z", "1000", "--json", "--profile", "nope"])
        self.assertEqual(rc, 0)
        self.assertIn("不存在", err)
        # 回退到默认档：分数应与不传 profile 时逐位相同
        rc2, out2, _ = self._run(
            ["--x", "1000", "--y", "100000", "--z", "1000", "--json"])
        self.assertEqual(json.loads(out)["score"], json.loads(out2)["score"])
        self.assertEqual(json.loads(out)["profile"], A.DEFAULT_PROFILE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
