# -*- coding: utf-8 -*-
"""attn_agent 关注度（Attention）评分离线测试：不联网、不调用大模型、结果可复现。

被测公式（attn_agent/attention.py 必须逐字实现）：
    E = 100 * (1 - exp(-lambda * S))
    S = w_x*ln(1+x)/ln(1+x0) + w_y*ln(1+y)/ln(1+y0) + w_z*ln(1+z)/ln(1+z0) + epsilon
    默认 x0=100, y0=100000, z0=1000, w_x=0.2, w_y=0.5, w_z=0.3, lambda=0.7, epsilon=1e-6

独立性说明：本文件自带一份用 ``math.log`` / ``math.exp`` 直写的参考实现
（``ref_terms`` / ``ref_score`` / ``ref_additivity_ok``），**不** import 被测模块的
``contributions`` 等分项函数。只要实现把分母写成 ln(x0)、漏掉 epsilon、或把指数
写成别的形状，第 1~5、10 组数值对比就会失败。

运行（项目根目录下）：  ./.venv/Scripts/python.exe -m unittest tests.test_attention -v
"""

from __future__ import annotations

import io
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from attn_agent import attention as A  # noqa: E402

# ---------------------------------------------------------------- 规格常量（照抄规格，不读实现）
X0, Y0, Z0 = 100.0, 100000.0, 1000.0
W_X, W_Y, W_Z = 0.2, 0.5, 0.3
LAMBDA, EPSILON = 0.7, 1e-06
SPEC_PARAMS = {
    "x0": X0, "y0": Y0, "z0": Z0,
    "w_x": W_X, "w_y": W_Y, "w_z": W_Z,
    "lambda": LAMBDA, "epsilon": EPSILON,
}
SPEC_KEYS = ("x0", "y0", "z0", "w_x", "w_y", "w_z", "lambda", "epsilon")
SPEC_BANDS = [
    {"min": 66.0, "label": "高关注"},
    {"min": 50.0, "label": "中关注"},
    {"min": 33.0, "label": "低关注"},
    {"min": 0.0, "label": "冷启动"},
]
DEEP_READ = 66.0

# 数值对比样点：含定标点、全零点、小数、单变量点、1e6/1e9、1e12 大数点
SAMPLES = [
    (0.0, 0.0, 0.0),
    (1.0, 1.0, 1.0),
    (100.0, 100000.0, 1000.0),
    (1000.0, 10000000.0, 50000.0),
    (12345.0, 6.7e8, 43210.0),
    (0.5, 3.0, 7.25),
    (0.0, 100000.0, 0.0),
    (100.0, 0.0, 0.0),
    (0.0, 0.0, 1000.0),
    (1e6, 1e9, 1e6),
    (1e12, 1e12, 1e12),
]


# ---------------------------------------------------------------- 独立参考实现（math.log / math.exp 直写）
def ref_terms(x: float, y: float, z: float, params: dict | None = None) -> dict:
    """参考分项：term_x = w_x*ln(1+x)/ln(1+x0)，S 再加 epsilon。"""
    p = dict(SPEC_PARAMS if params is None else params)
    term_x = p["w_x"] * math.log(1.0 + x) / math.log(1.0 + p["x0"])
    term_y = p["w_y"] * math.log(1.0 + y) / math.log(1.0 + p["y0"])
    term_z = p["w_z"] * math.log(1.0 + z) / math.log(1.0 + p["z0"])
    s = term_x + term_y + term_z + p["epsilon"]
    return {"term_x": term_x, "term_y": term_y, "term_z": term_z,
            "epsilon": p["epsilon"], "S": s}


def ref_score(x: float, y: float, z: float, params: dict | None = None) -> float:
    """参考总分：E = 100*(1-exp(-lambda*S))。"""
    p = dict(SPEC_PARAMS if params is None else params)
    s = ref_terms(x, y, z, p)["S"]
    return 100.0 * (1.0 - math.exp(-p["lambda"] * s))


def ref_additivity_ok(terms: dict) -> float:
    """参考可加性残差：|term_x+term_y+term_z+epsilon-S|。"""
    return abs(terms["term_x"] + terms["term_y"] + terms["term_z"]
               + terms["epsilon"] - terms["S"])


def params_with(**kw) -> dict:
    """在规格默认参数上打补丁，得到一份显式参数（用于非法参数用例）。"""
    p = dict(SPEC_PARAMS)
    p.update(kw)
    return p


def run_main(argv: list[str]) -> tuple[int, str, str]:
    """在 StringIO 上跑 main，返回 (返回码, stdout, stderr)；绝不向真实控制台写。"""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
        rc = A.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def has_cjk(text: str) -> bool:
    """文本里是否含中日韩汉字（用于校验错误提示是中文）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def pick_scratch_base() -> Path:
    """挑一个可写的临时文件落点，返回目录路径。

    优先系统临时目录；受限沙箱下「新建子目录」不可写，故退回仓库 ``.tmp/``
    与 tests/ 目录，直接在其中建临时文件（不再新建子目录），保证只读环境下
    测试也能跑完。
    """
    for base in (Path(tempfile.gettempdir()), ROOT / ".tmp", Path(__file__).resolve().parent):
        try:
            fd, name = tempfile.mkstemp(prefix="_attn_probe_", dir=str(base))
        except OSError:
            continue
        os.close(fd)
        try:
            os.unlink(name)
        except OSError:
            pass
        return base
    return Path(tempfile.gettempdir())


SCRATCH_BASE = pick_scratch_base()


class TempRulesMixin:
    """在可写目录里落临时规则文件，测试结束逐个删除（不新建子目录）。"""

    def setUp(self) -> None:
        self._scratch: list[Path] = []

    def tearDown(self) -> None:
        for path in self._scratch:
            try:
                path.unlink()
            except OSError:
                pass

    def write_rules(self, name: str, text: str) -> Path:
        """把一个临时规则文件写进可写目录，返回路径。"""
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
        fd, raw = tempfile.mkstemp(prefix=f"_attn_{safe}_", suffix=".json",
                                   dir=str(SCRATCH_BASE))
        path = Path(raw)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        self._scratch.append(path)
        return path

    def scratch_dir(self) -> str:
        """返回一个「已存在的目录」路径（用于目录被当成规则文件的用例）。"""
        return str(SCRATCH_BASE)


# ================================================================ 1. 常量与配置文件
class TestConstants(unittest.TestCase):
    """常量口径：必须与规格逐字一致。"""

    def test_default_params_values(self):
        """DEFAULT_PARAMS 八个键取值与规格完全一致。"""
        self.assertEqual(A.DEFAULT_PARAMS, SPEC_PARAMS)

    def test_param_keys_order(self):
        """PARAM_KEYS 顺序与规格一致（元组）。"""
        self.assertEqual(tuple(A.PARAM_KEYS), SPEC_KEYS)

    def test_deep_read_threshold(self):
        """DEEP_READ_THRESHOLD 为 66.0。"""
        self.assertEqual(float(A.DEEP_READ_THRESHOLD), DEEP_READ)

    def test_rules_path_points_to_module_dir(self):
        """RULES_PATH 指向同目录下的 attn_rules.json。"""
        self.assertIsInstance(A.RULES_PATH, Path)
        self.assertEqual(A.RULES_PATH.name, "attn_rules.json")
        self.assertEqual(A.RULES_PATH.parent, Path(A.__file__).resolve().parent)

    def test_default_bands_match_spec(self):
        """DEFAULT_BANDS 与规格档位一致（按 min 降序）。"""
        self.assertEqual(len(A.DEFAULT_BANDS), len(SPEC_BANDS))
        for got, want in zip(A.DEFAULT_BANDS, SPEC_BANDS):
            self.assertEqual(float(got["min"]), float(want["min"]))
            self.assertEqual(got["label"], want["label"])
        mins = [float(b["min"]) for b in A.DEFAULT_BANDS]
        self.assertEqual(mins, sorted(mins, reverse=True))

    def test_exception_hierarchy(self):
        """三个异常都是 ValueError 子类：AttentionError > {Param,Input}Error。"""
        self.assertTrue(issubclass(A.AttentionError, ValueError))
        self.assertTrue(issubclass(A.AttentionParamError, A.AttentionError))
        self.assertTrue(issubclass(A.AttentionInputError, A.AttentionError))


class TestRulesFile(unittest.TestCase):
    """attn_agent/attn_rules.json 的内容必须与规格一致、可被 UTF-8 解析。"""

    def test_rules_file_exists_and_parses(self):
        """规则文件存在、是合法 JSON、结构为 params/bands/deep_read_threshold。"""
        self.assertTrue(A.RULES_PATH.is_file(), f"缺少规则文件：{A.RULES_PATH}")
        data = json.loads(A.RULES_PATH.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertIsInstance(data["params"], dict)
        self.assertIsInstance(data["bands"], list)
        self.assertEqual(float(data["deep_read_threshold"]), DEEP_READ)

    def test_rules_file_params_are_defaults(self):
        """规则文件里的参数等于内置默认值。"""
        data = json.loads(A.RULES_PATH.read_text(encoding="utf-8"))
        for k in SPEC_KEYS:
            self.assertIn(k, data["params"])
            self.assertAlmostEqual(float(data["params"][k]), SPEC_PARAMS[k], places=12)

    def test_rules_file_bands_match_default_bands(self):
        """规则文件档位与 DEFAULT_BANDS 一致。"""
        data = json.loads(A.RULES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(data["bands"]), len(A.DEFAULT_BANDS))
        for got, want in zip(data["bands"], A.DEFAULT_BANDS):
            self.assertEqual(float(got["min"]), float(want["min"]))
            self.assertEqual(got["label"], want["label"])


# ================================================================ 2. 定标点（Lead 已独立核过）
class TestCalibrationPoints(unittest.TestCase):
    """定标点必须复现：中间点 ~50.3415、全零 ~7e-5、1e12 点 ~91.9385。"""

    def test_midpoint_scaling(self):
        """x=x0,y=y0,z=z0 时 E≈50.3415（S=1+epsilon -> E=100*(1-exp(-0.7))）。"""
        score = A.attention_score(X0, Y0, Z0)
        self.assertAlmostEqual(score, 50.3415, places=4)
        self.assertAlmostEqual(score, ref_score(X0, Y0, Z0), places=9)

    def test_midpoint_s_is_one_plus_epsilon(self):
        """中间点三项各为 w，S = 1 + epsilon。"""
        terms = A.contributions(X0, Y0, Z0)
        self.assertAlmostEqual(terms["S"], 1.0 + EPSILON, places=12)

    def test_zero_point_strictly_positive(self):
        """x=y=z=0 时 E≈7.0e-05，必须严格 >0（epsilon 的意义）。"""
        score = A.attention_score(0, 0, 0)
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1e-3)
        self.assertAlmostEqual(score, 7.0e-05, places=9)
        self.assertAlmostEqual(score, ref_score(0.0, 0.0, 0.0), places=12)

    def test_zero_point_without_epsilon_would_be_zero(self):
        """反证：若实现漏掉 epsilon，E 会恰好是 0.0，本断言会失败。"""
        score = A.attention_score(0, 0, 0)
        self.assertNotEqual(score, 0.0)
        self.assertGreater(score, 1e-9)

    def test_huge_point(self):
        """x=y=z=1e12 时 E≈91.9385，有限、<100、单调不封顶。"""
        score = A.attention_score(1e12, 1e12, 1e12)
        self.assertTrue(math.isfinite(score))
        self.assertAlmostEqual(score, 91.9385, places=4)
        self.assertAlmostEqual(score, ref_score(1e12, 1e12, 1e12), places=9)
        self.assertLess(score, 100.0)
        self.assertNotEqual(score, 100.0)

    def test_never_reaches_100(self):
        """大数区间（1e12~1e300，含超出 float 分辨率的极端区）都不达到 100。

        关键点：u = lambda*S 超过 53*ln2 ≈ 36.7368 后，``1 - exp(-u)`` 在 double 里
        会被舍入成恰好 1.0，若实现直接写 ``100*(1-exp(-u))`` 就会返回 100.0，
        违反硬约束 0<E<100。本用例要求这些点都必须返回严格 <100 的值。
        """
        for v in (1e12, 1e50, 1e100, 1e150, 1e200, 1e300):
            with self.subTest(v=v):
                score = A.attention_score(v, v, v)
                self.assertTrue(math.isfinite(score))
                self.assertGreater(score, 0.0)
                self.assertLess(score, 100.0)
                self.assertNotEqual(score, 100.0)

    def test_extreme_upper_bound_is_saturating_not_clamped(self):
        """超出 float 分辨率时取 nextafter(100, 0)，而不是把结果夹到 100。

        直接写 ``100*(1-exp(-u))`` 在 x=y=z=1e300 时会舍入成恰好 100.0。
        实现必须给出 double 里严格小于 100 的最大可表示值。
        """
        score = A.attention_score(1e300, 1e300, 1e300)
        self.assertEqual(score, math.nextafter(100.0, 0.0))
        self.assertLess(score, 100.0)
        self.assertGreater(score, 99.0)

    def test_lower_bound_tiny_epsilon_stays_positive(self):
        """极小 epsilon（外置配置给出）时 E 仍必须严格 > 0。

        ``100*(1-exp(-u))`` 在 u < 2^-54 时会舍入成恰好 0.0，违反 E>0。
        外置 attn_rules.json 是用户可改的，因此这条不是理论洁癖。
        """
        for eps in (1e-10, 1e-16, 1e-20, 1e-300):
            with self.subTest(eps=eps):
                p = params_with(epsilon=eps)
                score = A.attention_score(0.0, 0.0, 0.0, p)
                self.assertGreater(score, 0.0)
                self.assertLess(score, 1e-3)

    def test_boundary_guard_is_not_an_assert(self):
        """边界守卫不能用 assert：python -O 下 assert 会被剔除，从而静默返回 100.0。

        在子进程里以 -O 运行同一输入，确认极端点仍返回严格 <100。
        """
        import subprocess
        code = (
            "import sys; sys.path.insert(0, %r);"
            "from attn_agent.attention import attention_score;"
            "s = attention_score(1e300, 1e300, 1e300);"
            "print(s)"
        ) % str(ROOT)
        proc = subprocess.run([sys.executable, "-O", "-c", code],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertLess(float(proc.stdout.strip()), 100.0)

    def test_normal_range_matches_textbook_formula(self):
        """正常量级下与教科书式 100*(1-exp(-lambda*S)) 的偏差必须远小于 places=9。

        本次为实现为守开区间改用等价写法 ``100 - 100*exp(exponent)``，
        本用例锁死它没有偷偷改变业务量级上的结果（容差 5e-10）。
        """
        worst = 0.0
        for x, y, z in SAMPLES:
            with self.subTest(x=x, y=y, z=z):
                got = A.attention_score(x, y, z)
                want = ref_score(x, y, z)
                worst = max(worst, abs(got - want))
        self.assertLess(worst, 5e-10, msg=f"最大偏差 {worst} 超过 places=9 容差")


# ================================================================ 3. 与独立参考实现逐点对比
class TestAgainstReferenceFormula(unittest.TestCase):
    """核心独立性检查：与 math.log/math.exp 直写的参考实现在样点上 places=9 一致。"""

    def test_samples_match_reference(self):
        """SAMPLES 每个样点：attention_score 与参考实现一致（places=9）。"""
        for x, y, z in SAMPLES:
            with self.subTest(x=x, y=y, z=z):
                got = A.attention_score(x, y, z)
                self.assertAlmostEqual(got, ref_score(x, y, z), places=9)

    def test_samples_match_reference_with_explicit_params(self):
        """换一组权重（和为 1）后仍与参考实现一致：分母/权重不能写死。"""
        custom = params_with(w_x=0.25, w_y=0.45, w_z=0.30)
        custom["lambda"] = 1.3
        for x, y, z in SAMPLES:
            with self.subTest(x=x, y=y, z=z):
                got = A.attention_score(x, y, z, params=custom)
                self.assertAlmostEqual(got, ref_score(x, y, z, custom), places=9)

    def test_custom_x0_y0_z0_used(self):
        """改基准值 x0/y0/z0 后与参考实现一致：分母必须走参数。"""
        custom = params_with(x0=10.0, y0=1000.0, z0=10.0)
        for x, y, z in [(0, 0, 0), (10, 1000, 10), (100, 100000, 1000), (1e9, 1e9, 1e9)]:
            with self.subTest(x=x, y=y, z=z):
                self.assertAlmostEqual(
                    A.attention_score(x, y, z, params=custom),
                    ref_score(x, y, z, custom), places=9)

    def test_int_and_float_inputs_agree(self):
        """整数与等价浮点输入给出同一个分数，且返回类型是 float。"""
        score_int = A.attention_score(100, 100000, 1000)
        score_float = A.attention_score(100.0, 100000.0, 1000.0)
        self.assertIsInstance(score_int, float)
        self.assertIsInstance(score_float, float)
        self.assertEqual(score_int, score_float)


# ================================================================ 4. 开区间 (0,100) 与单调性
class TestRangeAndMonotonicity(unittest.TestCase):
    """严格落在开区间 (0,100)，且对 x/y/z 严格单调递增。"""

    def test_strict_open_interval(self):
        """样点上 0 < E < 100，且不得用夹边界的方式得到 100。"""
        for x, y, z in SAMPLES + [(0.0, 1e12, 0.0), (1e12, 0.0, 1e12), (1.0, 0.0, 0.0)]:
            with self.subTest(x=x, y=y, z=z):
                score = A.attention_score(x, y, z)
                self.assertGreater(score, 0.0)
                self.assertLess(score, 100.0)
                self.assertNotEqual(score, 0.0)
                self.assertNotEqual(score, 100.0)

    def test_monotonic_in_x(self):
        """固定 y,z，x 增大时 E 严格增大。"""
        xs = [0.0, 0.5, 1.0, 10.0, 100.0, 1000.0, 1e4, 1e5, 1e6, 1e9, 1e12]
        scores = [A.attention_score(x, Y0, Z0) for x in xs]
        for i in range(len(scores) - 1):
            with self.subTest(x=xs[i], next_x=xs[i + 1]):
                self.assertGreaterEqual(scores[i + 1], scores[i])
                self.assertGreater(scores[i + 1], scores[i])

    def test_monotonic_in_y(self):
        """固定 x,z，y 增大时 E 严格增大。"""
        ys = [0.0, 0.5, 1.0, 100.0, 1e4, Y0, 1e6, 1e8, 1e10, 1e12]
        scores = [A.attention_score(X0, y, Z0) for y in ys]
        for i in range(len(scores) - 1):
            with self.subTest(y=ys[i], next_y=ys[i + 1]):
                self.assertGreaterEqual(scores[i + 1], scores[i])
                self.assertGreater(scores[i + 1], scores[i])

    def test_monotonic_in_z(self):
        """固定 x,y，z 增大时 E 严格增大。"""
        zs = [0.0, 0.5, 1.0, 100.0, Z0, 1e4, 1e6, 1e9, 1e12]
        scores = [A.attention_score(X0, Y0, z) for z in zs]
        for i in range(len(scores) - 1):
            with self.subTest(z=zs[i], next_z=zs[i + 1]):
                self.assertGreaterEqual(scores[i + 1], scores[i])
                self.assertGreater(scores[i + 1], scores[i])

    def test_monotonic_matches_reference_ordering(self):
        """单调性同时被参考实现确认（防止测试自身假设错误）。"""
        xs = [0.0, 1.0, 100.0, 1e6, 1e12]
        refs = [ref_score(x, Y0, Z0) for x in xs]
        self.assertEqual(refs, sorted(refs))


# ================================================================ 5. 输入合法性
class TestInputValidation(unittest.TestCase):
    """x/y/z 必须是 int/float（bool 不算）、有限、>=0，否则 AttentionInputError。"""

    BAD_VALUES = [None, "100", "abc", [], {}, True, False,
                  -1, -0.5, -1e-9, float("nan"), float("inf"), float("-inf")]

    def test_bad_values_rejected(self):
        """三个位置分别喂非法值都必须抛 AttentionInputError。"""
        for bad in self.BAD_VALUES:
            for pos in range(3):
                args = [X0, Y0, Z0]
                args[pos] = bad
                with self.subTest(value=bad, pos=pos):
                    with self.assertRaises(A.AttentionInputError):
                        A.attention_score(*args)

    def test_bool_is_not_a_number(self):
        """bool 是 int 子类但不算数值：True/False 都要报错。"""
        for bad in (True, False):
            with self.subTest(value=bad):
                with self.assertRaises(A.AttentionInputError):
                    A.attention_score(bad, 1.0, 1.0)
                with self.assertRaises(A.AttentionInputError):
                    A.attention_score(1.0, bad, 1.0)
                with self.assertRaises(A.AttentionInputError):
                    A.attention_score(1.0, 1.0, bad)

    def test_zero_is_allowed(self):
        """0 是合法下界。"""
        self.assertGreater(A.attention_score(0, 0, 0), 0.0)
        self.assertGreater(A.attention_score(0.0, 0.0, 0.0), 0.0)

    def test_negative_zero_and_tiny_ok(self):
        """-0.0 与正的小数视为合法（-0.0 >= 0）。"""
        self.assertGreater(A.attention_score(-0.0, -0.0, -0.0), 0.0)
        self.assertGreater(A.attention_score(1e-12, 1e-12, 1e-12), 0.0)

    def test_large_finite_values_ok(self):
        """极大但有限、仍可分辨的值合法，不得溢出成 inf/nan。"""
        score = A.attention_score(1e150, 1e150, 1e150)
        self.assertTrue(math.isfinite(score))
        self.assertLess(score, 100.0)


# ================================================================ 6. 参数合法性
class TestParamValidation(unittest.TestCase):
    """validate_params 报问题；显式传非法 params 时 attention_score 抛 AttentionParamError。"""

    BAD_PARAMS = [
        ("权重和不为1", params_with(w_x=0.2, w_y=0.5, w_z=0.4)),
        ("权重和明显偏离", params_with(w_x=0.5, w_y=0.5, w_z=0.5)),
        ("w_x为0", params_with(w_x=0.0, w_y=0.5, w_z=0.5)),
        ("w_y为负", params_with(w_x=0.6, w_y=-0.3, w_z=0.7)),
        ("w_z为负", params_with(w_x=0.6, w_y=0.6, w_z=-0.2)),
        ("x0为0", params_with(x0=0.0)),
        ("x0为负", params_with(x0=-100.0)),
        ("y0为0", params_with(y0=0.0)),
        ("z0为负", params_with(z0=-1.0)),
        ("lambda为0", params_with(**{"lambda": 0.0})),
        ("lambda为负", params_with(**{"lambda": -0.7})),
        ("epsilon为0", params_with(epsilon=0.0)),
        ("epsilon为负", params_with(epsilon=-1e-9)),
        ("x0为NaN", params_with(x0=float("nan"))),
        ("lambda为inf", params_with(**{"lambda": float("inf")})),
        ("x0为bool", params_with(x0=True)),
        ("x0为字符串", params_with(x0="100")),
        ("缺lambda键", {k: v for k, v in SPEC_PARAMS.items() if k != "lambda"}),
        ("缺w_z键", {k: v for k, v in SPEC_PARAMS.items() if k != "w_z"}),
        ("空字典", {}),
        ("不是字典_列表", [1, 2, 3]),
        ("不是字典_字符串", "params"),
    ]

    # 规格明确「params 为 None 时用 DEFAULT_PARAMS」，故 None 只能由 validate_params 报问题，
    # 不能在 attention_score 里抛 AttentionParamError（否则等号两侧语义自相矛盾）。
    NON_DICT_PARAMS_CASES = [
        ("空字典", {}),
        ("不是字典_列表", [1, 2, 3]),
        ("不是字典_字符串", "params"),
        ("不是字典_None", None),
    ]

    def test_validate_params_accepts_defaults(self):
        """默认参数合法：返回空列表。"""
        self.assertEqual(A.validate_params(dict(SPEC_PARAMS)), [])
        self.assertEqual(A.validate_params(dict(A.DEFAULT_PARAMS)), [])

    def test_validate_params_boundary_weight_sum(self):
        """权重和容差 1e-9：偏 1e-10 合法，偏 1e-6 非法。"""
        ok = params_with(w_x=0.2, w_y=0.5, w_z=0.3 + 1e-10)
        self.assertEqual(A.validate_params(ok), [])
        bad = params_with(w_x=0.2, w_y=0.5, w_z=0.3 + 1e-6)
        self.assertTrue(A.validate_params(bad))

    def test_validate_params_reports_each_bad_case(self):
        """每个非法参数都返回非空的中文问题描述列表。"""
        for name, bad in self.BAD_PARAMS + self.NON_DICT_PARAMS_CASES:
            with self.subTest(case=name):
                problems = A.validate_params(bad)
                self.assertIsInstance(problems, list)
                self.assertTrue(problems, f"{name} 未被报出")
                for item in problems:
                    self.assertIsInstance(item, str)
                    self.assertTrue(item.strip())

    def test_validate_params_problem_messages_are_chinese(self):
        """问题描述是可读中文（含汉字）。"""
        problems = A.validate_params(params_with(w_x=0.2, w_y=0.5, w_z=0.4))
        self.assertTrue(any(has_cjk(p) for p in problems))

    def test_score_rejects_bad_params(self):
        """显式传入非法 dict params 时 attention_score 抛 AttentionParamError。"""
        for name, bad in self.BAD_PARAMS:
            with self.subTest(case=name):
                with self.assertRaises(A.AttentionParamError):
                    A.attention_score(X0, Y0, Z0, params=bad)

    def test_params_none_means_default(self):
        """params=None 等价于默认参数（规格：None 时用 DEFAULT_PARAMS）。"""
        self.assertEqual(A.attention_score(X0, Y0, Z0, params=None),
                         A.attention_score(X0, Y0, Z0))

    def test_score_rejects_unvalidated_param_patch(self):
        """只改一个键成 0 也必须被拦住（不能静默用默认值兜底）。"""
        with self.assertRaises(A.AttentionParamError):
            A.attention_score(X0, Y0, Z0, params=params_with(epsilon=0.0))

    def test_contributions_also_rejects_bad_params(self):
        """contributions 显式收非法参数同样抛 AttentionParamError。"""
        with self.assertRaises(A.AttentionParamError):
            A.contributions(X0, Y0, Z0, params=params_with(**{"lambda": 0.0}))

    def test_params_not_mutated_by_calls(self):
        """调用不得就地修改调用方传入的参数字典。"""
        custom = params_with(w_x=0.25, w_y=0.45, w_z=0.30)
        snapshot = dict(custom)
        A.attention_score(X0, Y0, Z0, params=custom)
        A.contributions(X0, Y0, Z0, params=custom)
        A.score_detail(X0, Y0, Z0, params=custom)
        self.assertEqual(custom, snapshot)


# ================================================================ 7. load_params 退回默认
class TestLoadParams(TempRulesMixin, unittest.TestCase):
    """配置类问题一律退回内置默认 + 中文告警，绝不抛异常。"""

    def _assert_defaults_and_warnings(self, params, warnings, case: str):
        """公共断言：返回默认参数副本 + 非空告警。"""
        self.assertIsInstance(params, dict)
        self.assertIsInstance(warnings, list)
        self.assertTrue(warnings, f"{case} 应有告警")
        for w in warnings:
            self.assertIsInstance(w, str)
            self.assertTrue(w.strip())
        self.assertEqual(tuple(sorted(params)), tuple(sorted(SPEC_KEYS)))
        for k in SPEC_KEYS:
            self.assertAlmostEqual(float(params[k]), SPEC_PARAMS[k], places=12)

    def test_load_rules_path_explicit(self):
        """显式传入 attn_rules.json：参数等于默认值且无告警。"""
        params, warnings = A.load_params(str(A.RULES_PATH))
        self.assertEqual(warnings, [])
        self.assertEqual(tuple(sorted(params)), tuple(sorted(SPEC_KEYS)))
        for k in SPEC_KEYS:
            self.assertAlmostEqual(float(params[k]), SPEC_PARAMS[k], places=12)

    def test_load_rules_path_as_pathlike(self):
        """Path 对象也能当 path 传入。"""
        params, warnings = A.load_params(A.RULES_PATH)
        self.assertEqual(warnings, [])
        self.assertAlmostEqual(float(params["x0"]), X0, places=12)

    def test_load_params_default_path(self):
        """path=None 时必须能用上项目自带规则（不得报错）；参数为默认口径。"""
        params, warnings = A.load_params()
        self.assertIsInstance(params, dict)
        self.assertIsInstance(warnings, list)
        self.assertEqual(tuple(sorted(params)), tuple(sorted(SPEC_KEYS)))
        for k in SPEC_KEYS:
            self.assertAlmostEqual(float(params[k]), SPEC_PARAMS[k], places=12)

    def test_load_params_values_are_numbers(self):
        """返回的参数值是数值（int/float，不是字符串或 bool）。"""
        params, _ = A.load_params(str(A.RULES_PATH))
        for k in SPEC_KEYS:
            v = params[k]
            self.assertIsInstance(v, (int, float))
            self.assertNotIsInstance(v, bool)

    def test_missing_file_falls_back(self):
        """文件不存在：默认值 + 告警，不抛异常。"""
        missing = Path(self.scratch_dir()) / "_attn_definitely_missing" / "attn_rules.json"
        params, warnings = A.load_params(str(missing))
        self._assert_defaults_and_warnings(params, warnings, "文件不存在")

    def test_bad_json_falls_back(self):
        """JSON 语法错：默认值 + 告警，不抛异常。"""
        path = self.write_rules("broken.json", "{ \"params\": { 不是 JSON")
        params, warnings = A.load_params(str(path))
        self._assert_defaults_and_warnings(params, warnings, "JSON 语法错")

    def test_empty_file_falls_back(self):
        """空文件：默认值 + 告警。"""
        path = self.write_rules("empty.json", "")
        params, warnings = A.load_params(str(path))
        self._assert_defaults_and_warnings(params, warnings, "空文件")

    def test_json_array_falls_back(self):
        """顶层是数组（结构不合法）：默认值 + 告警。"""
        path = self.write_rules("array.json", "[1, 2, 3]")
        params, warnings = A.load_params(str(path))
        self._assert_defaults_and_warnings(params, warnings, "顶层非对象")

    def test_missing_params_section_falls_back(self):
        """缺 params 段：默认值 + 告警。"""
        path = self.write_rules("noparams.json", json.dumps({"bands": SPEC_BANDS}))
        params, warnings = A.load_params(str(path))
        self._assert_defaults_and_warnings(params, warnings, "缺 params")

    def test_illegal_params_values_fall_back(self):
        """params 里数值非法（x0=0 / 权重和!=1）：默认值 + 告警。"""
        for name, bad in [("io_x0", params_with(x0=0.0)),
                          ("io_w", params_with(w_x=0.2, w_y=0.5, w_z=0.4)),
                          ("io_lambda", params_with(**{"lambda": 0.0}))]:
            payload = {"params": bad, "bands": SPEC_BANDS, "deep_read_threshold": 66}
            path = self.write_rules(f"{name}.json", json.dumps(payload, ensure_ascii=False))
            with self.subTest(case=name):
                params, warnings = A.load_params(str(path))
                self._assert_defaults_and_warnings(params, warnings, name)

    def test_non_numeric_params_fall_back(self):
        """params 里出现字符串（类型错）：默认值 + 告警。"""
        payload = {"params": params_with(x0="100"), "bands": SPEC_BANDS}
        path = self.write_rules("strparam.json", json.dumps(payload, ensure_ascii=False))
        params, warnings = A.load_params(str(path))
        self._assert_defaults_and_warnings(params, warnings, "参数类型错")

    def test_directory_path_does_not_raise(self):
        """路径是目录（OSError）：按配置问题退回默认，不抛异常。"""
        params, warnings = A.load_params(self.scratch_dir())
        self._assert_defaults_and_warnings(params, warnings, "路径是目录")

    def test_fallback_returns_copy_not_singleton(self):
        """退回默认时返回的是副本：改它不影响 DEFAULT_PARAMS。"""
        missing = Path(self.scratch_dir()) / "_attn_missing_default.json"
        params, warnings = A.load_params(str(missing))
        self.assertTrue(warnings)
        self.assertIsNot(params, A.DEFAULT_PARAMS)
        snapshot = dict(A.DEFAULT_PARAMS)
        params["x0"] = 999.0
        params["w_x"] = 0.9
        self.assertEqual(A.DEFAULT_PARAMS, snapshot)

    def test_valid_custom_file_is_used(self):
        """合法自定义文件必须真的被采纳（不能永远返回默认）。"""
        custom = params_with(w_x=0.25, w_y=0.45, w_z=0.30)
        payload = {"params": custom, "bands": SPEC_BANDS, "deep_read_threshold": 66}
        path = self.write_rules("custom.json", json.dumps(payload, ensure_ascii=False))
        params, warnings = A.load_params(str(path))
        self.assertEqual(warnings, [])
        for k in SPEC_KEYS:
            self.assertAlmostEqual(float(params[k]), custom[k], places=12)


# ================================================================ 8. classify / deep_read
class TestClassifyAndDeepRead(unittest.TestCase):
    """档位边界与深读阈值：66.0 归高关注但不触发深读（严格大于才触发）。"""

    def test_classify_default_bands(self):
        """默认档位下各区间取值正确。"""
        cases = [(100.0, "高关注"), (66.0, "高关注"), (65.999, "中关注"),
                 (50.0, "中关注"), (49.999, "低关注"), (33.0, "低关注"),
                 (32.999, "冷启动"), (0.0, "冷启动"), (7e-05, "冷启动")]
        for score, label in cases:
            with self.subTest(score=score):
                self.assertEqual(A.classify(score), label)

    def test_classify_boundary_66_and_65_999(self):
        """66.0 归高关注、65.999 归中关注（>= min 取下界闭合）。"""
        self.assertEqual(A.classify(66.0), "高关注")
        self.assertEqual(A.classify(65.999), "中关注")

    def test_classify_custom_bands_fallback_last(self):
        """自定义档位都不满足时返回最后一条的 label。"""
        bands = [{"min": 66.0, "label": "高"}, {"min": 50.0, "label": "中"}]
        self.assertEqual(A.classify(10.0, bands=bands), "中")
        self.assertEqual(A.classify(50.0, bands=bands), "中")
        self.assertEqual(A.classify(66.0, bands=bands), "高")

    def test_classify_with_default_bands_list(self):
        """显式传 DEFAULT_BANDS 与不传结果一致。"""
        for score in (0.0, 33.0, 50.0, 66.0, 90.0):
            with self.subTest(score=score):
                self.assertEqual(A.classify(score, bands=A.DEFAULT_BANDS),
                                 A.classify(score))

    def test_classify_bad_score_raises(self):
        """非数值 score 抛 AttentionInputError。"""
        for bad in (None, "66", "", [], {}, float("nan"), float("inf")):
            with self.subTest(value=bad):
                with self.assertRaises(A.AttentionInputError):
                    A.classify(bad)

    def test_deep_read_strictly_greater(self):
        """deep_read_triggered 严格大于才为 True。"""
        self.assertFalse(A.deep_read_triggered(66.0))
        self.assertTrue(A.deep_read_triggered(66.0001))
        self.assertFalse(A.deep_read_triggered(0.0))
        self.assertTrue(A.deep_read_triggered(100.0))
        self.assertFalse(A.deep_read_triggered(DEEP_READ, threshold=DEEP_READ))

    def test_deep_read_custom_threshold(self):
        """自定义阈值同样严格大于才触发。"""
        self.assertFalse(A.deep_read_triggered(50.0, threshold=50.0))
        self.assertTrue(A.deep_read_triggered(50.0001, threshold=50.0))

    def test_deep_read_default_threshold_equals_constant(self):
        """不传阈值时用 DEEP_READ_THRESHOLD。"""
        self.assertFalse(A.deep_read_triggered(float(A.DEEP_READ_THRESHOLD)))
        self.assertTrue(A.deep_read_triggered(float(A.DEEP_READ_THRESHOLD) + 1e-6))


# ================================================================ 9. contributions / score_detail
class TestContributionsAndDetail(unittest.TestCase):
    """分项可加性、分项数值、score_detail 字段与内部一致性。"""

    def test_contribution_keys(self):
        """contributions 返回且仅返回五个键。"""
        terms = A.contributions(X0, Y0, Z0)
        self.assertEqual(set(terms), {"term_x", "term_y", "term_z", "epsilon", "S"})

    def test_additivity(self):
        """term_x+term_y+term_z+epsilon == S（places=12）。"""
        for x, y, z in SAMPLES:
            with self.subTest(x=x, y=y, z=z):
                t = A.contributions(x, y, z)
                self.assertAlmostEqual(
                    t["term_x"] + t["term_y"] + t["term_z"] + t["epsilon"],
                    t["S"], places=12)
                self.assertLess(ref_additivity_ok(t), 1e-12)

    def test_additivity_with_custom_params(self):
        """自定义参数下可加性同样成立。"""
        custom = params_with(w_x=0.25, w_y=0.45, w_z=0.30, epsilon=1e-05)
        for x, y, z in [(0, 0, 0), (100, 100000, 1000), (1e9, 1e9, 1e9)]:
            with self.subTest(x=x, y=y, z=z):
                t = A.contributions(x, y, z, params=custom)
                self.assertAlmostEqual(
                    t["term_x"] + t["term_y"] + t["term_z"] + t["epsilon"],
                    t["S"], places=12)

    def test_terms_match_reference(self):
        """每个分项都与参考实现一致（places=9）。"""
        for x, y, z in SAMPLES:
            with self.subTest(x=x, y=y, z=z):
                got = A.contributions(x, y, z)
                want = ref_terms(x, y, z)
                for key in ("term_x", "term_y", "term_z", "epsilon", "S"):
                    self.assertAlmostEqual(got[key], want[key], places=9)

    def test_midpoint_terms_are_weights(self):
        """中间点三项分别等于 w_x/w_y/w_z。"""
        t = A.contributions(X0, Y0, Z0)
        self.assertAlmostEqual(t["term_x"], W_X, places=12)
        self.assertAlmostEqual(t["term_y"], W_Y, places=12)
        self.assertAlmostEqual(t["term_z"], W_Z, places=12)

    def test_zero_terms(self):
        """全零点三项为 0、S 恰好等于 epsilon。"""
        t = A.contributions(0, 0, 0)
        self.assertEqual(t["term_x"], 0.0)
        self.assertEqual(t["term_y"], 0.0)
        self.assertEqual(t["term_z"], 0.0)
        self.assertAlmostEqual(t["S"], EPSILON, places=15)

    def test_score_detail_keys(self):
        """score_detail 字段齐全。"""
        detail = A.score_detail(1000, 10000000, 50000)
        for key in ("x", "y", "z", "params", "term_x", "term_y", "term_z", "S",
                    "exponent", "score", "band", "deep_read"):
            self.assertIn(key, detail)

    def test_score_detail_consistency(self):
        """detail 内部自洽：score/exponent/band/deep_read 与各函数一致。"""
        for x, y, z in SAMPLES:
            with self.subTest(x=x, y=y, z=z):
                d = A.score_detail(x, y, z)
                score = A.attention_score(x, y, z)
                self.assertAlmostEqual(d["score"], score, places=12)
                self.assertAlmostEqual(d["exponent"], -LAMBDA * d["S"], places=12)
                self.assertAlmostEqual(d["S"], A.contributions(x, y, z)["S"], places=12)
                self.assertAlmostEqual(
                    d["term_x"] + d["term_y"] + d["term_z"] + d["params"]["epsilon"],
                    d["S"], places=12)
                self.assertEqual(d["band"], A.classify(score))
                self.assertEqual(bool(d["deep_read"]), score > DEEP_READ)
                self.assertEqual(d["x"], x)
                self.assertEqual(d["y"], y)
                self.assertEqual(d["z"], z)

    def test_score_detail_params_recorded(self):
        """detail['params'] 记录实际使用的参数（默认或自定义）。"""
        d = A.score_detail(X0, Y0, Z0)
        for k in SPEC_KEYS:
            self.assertAlmostEqual(float(d["params"][k]), SPEC_PARAMS[k], places=12)
        custom = params_with(w_x=0.25, w_y=0.45, w_z=0.30)
        d2 = A.score_detail(X0, Y0, Z0, params=custom)
        for k in SPEC_KEYS:
            self.assertAlmostEqual(float(d2["params"][k]), custom[k], places=12)

    def test_score_detail_band_high_and_low(self):
        """高关注点触发深读、冷启动点不触发。"""
        high = A.score_detail(12345, 6.7e8, 43210)
        self.assertEqual(high["band"], "高关注")
        self.assertTrue(high["deep_read"])
        low = A.score_detail(0, 0, 0)
        self.assertEqual(low["band"], "冷启动")
        self.assertFalse(low["deep_read"])

    def test_score_detail_rejects_bad_input(self):
        """score_detail 继承输入校验。"""
        with self.assertRaises(A.AttentionInputError):
            A.score_detail(-1, 1, 1)
        with self.assertRaises(A.AttentionParamError):
            A.score_detail(1, 1, 1, params=params_with(w_x=0.5, w_y=0.5, w_z=0.5))


# ================================================================ 10. 命令行入口
class TestMain(TempRulesMixin, unittest.TestCase):
    """main(): --json 输出可解析且分数一致；参数问题返回非 0 且不抛未捕获异常。"""

    def test_main_json_output(self):
        """--json 输出是单个 JSON 对象，score 与 attention_score 一致。"""
        rc, out, err = run_main(["--x", "1000", "--y", "10000000", "--z", "50000", "--json"])
        self.assertEqual(rc, 0, f"stderr={err}")
        data = json.loads(out)
        self.assertIsInstance(data, dict)
        self.assertIn("score", data)
        expected = A.attention_score(1000.0, 10000000.0, 50000.0)
        self.assertAlmostEqual(float(data["score"]), expected, places=9)
        self.assertIn(A.classify(expected), out)

    def test_main_json_output_zero_point(self):
        """全零点的 JSON 输出同样可解析、分数严格为正。"""
        rc, out, err = run_main(["--x", "0", "--y", "0", "--z", "0", "--json"])
        self.assertEqual(rc, 0, f"stderr={err}")
        data = json.loads(out)
        self.assertGreater(float(data["score"]), 0.0)
        self.assertAlmostEqual(float(data["score"]),
                               A.attention_score(0.0, 0.0, 0.0), places=12)

    def test_main_json_accepts_floats(self):
        """--x/--y/--z 接受小数与科学计数法。"""
        rc, out, err = run_main(["--x", "1e6", "--y", "2.5e7", "--z", "1.5e4", "--json"])
        self.assertEqual(rc, 0, f"stderr={err}")
        data = json.loads(out)
        self.assertAlmostEqual(float(data["score"]),
                               A.attention_score(1e6, 2.5e7, 1.5e4), places=9)

    def test_main_human_readable_output(self):
        """非 --json 输出中文可读结果，含档位与分项信息。"""
        rc, out, err = run_main(["--x", "1000", "--y", "10000000", "--z", "50000"])
        self.assertEqual(rc, 0, f"stderr={err}")
        expected = A.attention_score(1000.0, 10000000.0, 50000.0)
        self.assertTrue(has_cjk(out), "人类可读输出应为中文")
        self.assertIn(A.classify(expected), out)
        self.assertIn("S", out, "非 --json 输出应含 S（综合信号量）")

    def test_main_missing_x_returns_nonzero(self):
        """缺 --x 返回非 0，打印中文错误，不抛异常。"""
        rc, out, err = run_main(["--y", "100", "--z", "100"])
        self.assertNotEqual(rc, 0)
        self.assertTrue(has_cjk(out + err), "错误提示应为中文")

    def test_main_all_args_missing_returns_nonzero(self):
        """完全不给参数也返回非 0。"""
        rc, out, err = run_main([])
        self.assertNotEqual(rc, 0)
        self.assertTrue(has_cjk(out + err))

    def test_main_missing_y_and_z_return_nonzero(self):
        """只给 --x、或只给 --x/--y 都返回非 0。"""
        for argv in (["--x", "100"], ["--x", "100", "--y", "100"]):
            with self.subTest(argv=argv):
                rc, out, err = run_main(argv)
                self.assertNotEqual(rc, 0)

    def test_main_non_numeric_returns_nonzero(self):
        """--x 无法转 float 时返回非 0，不抛未捕获异常。"""
        for bad in ("abc", "", "--", "1,000"):
            with self.subTest(value=bad):
                rc, out, err = run_main(["--x", bad, "--y", "100", "--z", "100"])
                self.assertNotEqual(rc, 0)
                self.assertTrue(has_cjk(out + err))

    def test_main_nan_inf_rejected_gracefully(self):
        """nan/inf 能转 float 但不是合法输入：必须被捕获并返回非 0。"""
        for bad in ("nan", "inf", "-inf"):
            with self.subTest(value=bad):
                rc, out, err = run_main(["--x", bad, "--y", "100", "--z", "100"])
                self.assertNotEqual(rc, 0)

    def test_main_negative_returns_nonzero(self):
        """负数输入返回非 0。"""
        rc, out, err = run_main(["--x", "-1", "--y", "100", "--z", "100"])
        self.assertNotEqual(rc, 0)

    def test_main_with_custom_rules(self):
        """--rules 指向合法自定义文件时，参数真的生效。"""
        custom = params_with(w_x=0.25, w_y=0.45, w_z=0.30)
        payload = {"params": custom,
                   "bands": [{"min": 66, "label": "高关注"}, {"min": 50, "label": "中关注"},
                             {"min": 33, "label": "低关注"}, {"min": 0, "label": "冷启动"}],
                   "deep_read_threshold": 66}
        path = self.write_rules("custom.json", json.dumps(payload, ensure_ascii=False))
        rc, out, err = run_main(["--x", "1000", "--y", "10000000", "--z", "50000",
                                 "--rules", str(path), "--json"])
        self.assertEqual(rc, 0, f"stderr={err}")
        data = json.loads(out)
        self.assertAlmostEqual(float(data["score"]),
                               A.attention_score(1000.0, 10000000.0, 50000.0, params=custom),
                               places=9)

    def test_main_broken_rules_falls_back_to_default(self):
        """--rules 指向坏 JSON 时退回默认口径并照常出分（不崩、返回 0）。"""
        path = self.write_rules("broken.json", "{ 不是 JSON")
        rc, out, err = run_main(["--x", "1000", "--y", "10000000", "--z", "50000",
                                 "--rules", str(path), "--json"])
        self.assertEqual(rc, 0, f"stderr={err}")
        data = json.loads(out)
        self.assertAlmostEqual(float(data["score"]),
                               A.attention_score(1000.0, 10000000.0, 50000.0), places=9)

    def test_main_missing_rules_falls_back_to_default(self):
        """--rules 指向不存在的文件时退回默认口径。"""
        missing = Path(self.scratch_dir()) / "_attn_no_such_rules.json"
        rc, out, err = run_main(["--x", "1000", "--y", "10000000", "--z", "50000",
                                 "--rules", str(missing), "--json"])
        self.assertEqual(rc, 0, f"stderr={err}")
        data = json.loads(out)
        self.assertAlmostEqual(float(data["score"]),
                               A.attention_score(1000.0, 10000000.0, 50000.0), places=9)

    def test_main_argv_none_is_supported(self):
        """argv=None 时读 sys.argv；此处只验证不抛未捕获异常且返回 int。"""
        with mock.patch.object(sys, "argv", ["attention", "--x", "100", "--y", "100",
                                             "--z", "100", "--json"]):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                rc = A.main()
        self.assertIsInstance(rc, int)
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertAlmostEqual(float(data["score"]),
                               A.attention_score(100.0, 100.0, 100.0), places=9)


if __name__ == "__main__":
    unittest.main()
