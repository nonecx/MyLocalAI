# -*- coding: utf-8 -*-
"""关注度快照趋势线（M3-4）测试：纯 SVG 渲染 + change_rate 触发实现。离线，不调大模型。

覆盖（对齐任务书 M3-4 验收）：
   1. `render_trend_svg`：0 / 1 / 2 / 多点形态（元素与数值标签断言）、坏行跳过、
      输入是垃圾也不抛异常
   2. `trend_summary`：方向矩阵（up / down / flat / 单点 / 空序列）与 min/max/first/last
   3. `should_deep_read` / `deep_read` 的 change_rate：无历史/单快照恒 False、
      双快照未达/达阈值（含 ≥ 边界）、触发详情如实记录所用两快照与比率、
      阈值未配置恒 False、与 absolute 的 any 组合
   4. 逐位不变守卫：不传 snapshot_history 的 deep_read 返回值与既有回归逐键相同
   5. 规则文件：默认档加载干净；change_rate_threshold 写坏 → 告警 + 停用，其他段不破坏

运行：.venv/Scripts/python.exe -m unittest tests.test_attention_trend -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.attention import attention as A  # noqa: E402
from mylocalai.attention import deep_read as D  # noqa: E402
from mylocalai.attention import trend as T  # noqa: E402


def detail_of(x, y, z, params=None):
    d = A.score_detail(x, y, z, params)
    d["threshold"] = A.DEEP_READ_THRESHOLD
    return d


def snap(taken_at, e_score):
    return {"taken_at": taken_at, "e_score": e_score}


def rules_for(trigger_overrides=None, mode="absolute"):
    """手搓一份合法规则（形状与 tests.test_deep_read_stage1 的既有约定一致）。"""
    trigger = {"mode": mode}
    trigger.update(trigger_overrides or {})
    return {"trigger": trigger,
            "deep_read": dict(D.DEFAULT_DEEP_READ_PARAMS),
            "banned_patterns": [dict(p) for p in D.DEFAULT_BANNED_PATTERNS]}


# 2026-10-26 实测的五个真实头部话题（x, y, z）——与 test_deep_read_stage1 同源
TOPICS = [
    ("老干妈回应AI裁员传闻", 30, 48316000, 149),
    ("台湾艺人阿雅公开表态", 5, 12506705, 302),
    ("长鑫存储硬气拒绝苹果", 11, 13822047, 42),
    ("承包户花生地遭村民捡拾", 3, 2067344, 129),
    ("男子出狱砍死妻子二审", 6, 3766947, 1),
]


# ===========================================================================
# 1. render_trend_svg：形态与元素
# ===========================================================================

class TestRenderTrendSvg(unittest.TestCase):
    """0 / 1 / 2 / 多点形态，坏行跳过，永不抛异常。"""

    def test_zero_points_placeholder(self):
        """0 点 → "暂无快照"占位 svg：有根元素、无折线无圆点。"""
        for empty in ([], None, "not-a-list", [snap("t1", float("nan"))]):
            with self.subTest(empty=empty):
                svg = T.render_trend_svg(empty)
                self.assertIsInstance(svg, str)
                self.assertTrue(svg.startswith("<svg"))
                self.assertTrue(svg.endswith("</svg>"))
                self.assertIn("暂无快照", svg)
                self.assertNotIn("<polyline", svg)
                self.assertNotIn("<circle", svg)

    def test_one_point_single_marker(self):
        """1 点 → 单点圆标 + 数值标签 + 刻度线，无折线（单点不成趋势）。"""
        svg = T.render_trend_svg([snap("2026-10-31T10:00:00", 40)])
        self.assertIn("<circle", svg)
        self.assertNotIn("<polyline", svg)
        self.assertIn(">40</text>", svg)          # 单点数值标签（:g 格式）
        for tick in ("0", "50", "100"):
            self.assertIn(f">{tick}</text>", svg)  # 0-100 刻度线
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', svg)  # 可独立内嵌

    def test_two_points_polyline_with_first_last_labels(self):
        """2 点 → polyline + 两个圆标 + 首末数值标签。"""
        svg = T.render_trend_svg([snap("t1", 40), snap("t2", 70)])
        self.assertIn("<polyline", svg)
        self.assertEqual(svg.count("<circle"), 2)
        self.assertIn(">40</text>", svg)
        self.assertIn(">70</text>", svg)

    def test_multi_points_shape_and_decimal_labels(self):
        """多点 → 折线点数与快照数一致，首末小数标签如实（:g 六位有效数字）。"""
        snaps = [snap("t1", 33.5), snap("t2", 48.25), snap("t3", 51.0),
                 snap("t4", 69.5634)]
        svg = T.render_trend_svg(snaps)
        self.assertIn("<polyline", svg)
        self.assertEqual(svg.count("<circle"), 4)
        self.assertIn(">33.5</text>", svg)     # 首点
        self.assertIn(">69.5634</text>", svg)  # 末点（未被四舍五入吞掉）
        self.assertNotIn(">48.25</text>", svg)  # 中间点只有圆标、无数值标签
        # 折线坐标与刻度线存在
        self.assertIn('points="', svg)
        self.assertIn("<line", svg)

    def test_bad_rows_skipped(self):
        """缺 taken_at / 缺 e_score / 非 dict / 非数值 / bool / nan / inf 的行全跳过。"""
        snaps = [
            "junk",                       # 非 dict
            42,                           # 非 dict
            {"e_score": 10},              # 缺 taken_at
            {"taken_at": "t2"},           # 缺 e_score
            {"taken_at": "t3", "e_score": "x"},    # 非数值
            {"taken_at": "t4", "e_score": True},   # bool 不算数
            {"taken_at": "t5", "e_score": float("nan")},
            {"taken_at": "t6", "e_score": float("inf")},
            snap("t7", 10),               # 有效
            snap("t8", 20),               # 有效
        ]
        svg = T.render_trend_svg(snaps)
        self.assertIn("<polyline", svg)
        self.assertEqual(svg.count("<circle"), 2)  # 只剩两个有效点
        self.assertIn(">10</text>", svg)
        self.assertIn(">20</text>", svg)
        summary = T.trend_summary(snaps)
        self.assertEqual(summary["count"], 2)
        self.assertEqual((summary["first"], summary["last"]), (10.0, 20.0))

    def test_never_raises_on_garbage(self):
        """任何输入都不抛异常：坏结构退占位 / 空 dict。"""
        garbage = [object(), {"taken_at": {}, "e_score": [1]}, [snap("t", 1)], None]
        svg = T.render_trend_svg(garbage)
        self.assertIsInstance(svg, str)
        self.assertIn("暂无快照", svg)  # 唯一有效点在第 3 项（嵌套 list）之外全坏
        self.assertIsInstance(T.trend_summary(garbage), dict)


# ===========================================================================
# 2. trend_summary：方向矩阵
# ===========================================================================

class TestTrendSummary(unittest.TestCase):
    """count/first/last/min/max/direction 全字段矩阵。"""

    def test_empty_summary(self):
        self.assertEqual(T.trend_summary([]),
                         {"count": 0, "first": None, "last": None,
                          "min": None, "max": None, "direction": "flat"})

    def test_direction_matrix(self):
        cases = [
            ([10.0, 20.0], "up"),
            ([20.0, 10.0], "down"),
            ([15.0, 15.0], "flat"),                       # 恰相等
            ([15.0, 15.0 + 1e-12], "flat"),               # 差在阈值内判平
            ([15.0, 15.0 + 1e-3], "up"),                  # 差超出阈值判升
            ([15.0], "flat"),                             # 单点无趋势
            ([40.0, 10.0, 70.0, 55.0], "up"),             # 只看首末：40→55
            ([70.0, 90.0, 20.0, 30.0], "down"),           # 中间冲高不改首末方向
        ]
        for values, expect in cases:
            with self.subTest(values=values, expect=expect):
                self.assertEqual(T.trend_summary([snap("t", v) for v in values])["direction"],
                                 expect)

    def test_min_max_first_last_and_count(self):
        s = T.trend_summary([snap("t1", 40), snap("t2", 10), snap("t3", 70),
                             snap("t4", 55)])
        self.assertEqual(s["count"], 4)
        self.assertEqual(s["first"], 40.0)
        self.assertEqual(s["last"], 55.0)
        self.assertEqual(s["min"], 10.0)
        self.assertEqual(s["max"], 70.0)


# ===========================================================================
# 3. change_rate 触发矩阵（should_deep_read / _change_rate_detail）
# ===========================================================================

class TestChangeRateTrigger(unittest.TestCase):
    """环比增幅：无历史/单快照恒 False，双快照按 (最新-前一)/max(前一, ε) ≥ 阈值。"""

    def test_no_history_or_single_snapshot_always_false(self):
        """无历史 / 空历史 / 单快照 → change_rate 恒 False（score 再高也不触发）。"""
        rules = rules_for({"change_rate_threshold": 0.5}, mode="change_rate")
        for history in (None, [], [50.0], (40.0,)):
            with self.subTest(history=history):
                self.assertFalse(D.should_deep_read({"score": 99.0},
                                                    rules=rules,
                                                    snapshot_history=history))

    def test_two_snapshots_threshold_matrix(self):
        """双快照：rate=(最新-前一)/max(前一, ε)，≥ 阈值触发（含边界），未达不触发。"""
        rules = lambda th: rules_for({"change_rate_threshold": th},
                                     mode="change_rate")  # noqa: E731
        # [40, 50] → rate = 10/40 = 0.25
        self.assertFalse(D.should_deep_read({"score": 0.0}, rules=rules(0.3),
                                            snapshot_history=[40.0, 50.0]))
        self.assertTrue(D.should_deep_read({"score": 0.0}, rules=rules(0.25),
                                          snapshot_history=[40.0, 50.0]))  # ≥ 边界
        self.assertTrue(D.should_deep_read({"score": 99.0}, rules=rules(0.2),
                                           snapshot_history=[40.0, 50.0]))
        # 下降序列 rate < 0，任何非负阈值都不触发
        self.assertFalse(D.should_deep_read({"score": 99.0}, rules=rules(0.0),
                                            snapshot_history=[50.0, 40.0]))
        # 前一为 0 时分母取 ε（CHANGE_RATE_EPSILON）：比率巨大，如实触发
        self.assertTrue(D.should_deep_read({"score": 0.0}, rules=rules(0.5),
                                           snapshot_history=[0.0, 40.0]))
        # tuple 输入同 list
        self.assertTrue(D.should_deep_read({"score": 0.0}, rules=rules(0.25),
                                           snapshot_history=(40.0, 50.0)))

    def test_threshold_not_configured_always_false(self):
        """阈值未配置（None / 缺键 / 坏类型）→ change_rate 恒 False。"""
        for overrides in ({"change_rate_threshold": None}, {}, {"change_rate_threshold": "x"}):
            with self.subTest(overrides=overrides):
                rules = rules_for(overrides, mode="change_rate")
                self.assertFalse(D.should_deep_read({"score": 99.0}, rules=rules,
                                                    snapshot_history=[40.0, 99.0]))

    def test_change_rate_mode_ignores_absolute_threshold(self):
        """change_rate 模式下 absolute 阈值不参与：E 再低也能靠环比触发，E 高也不因此触发。"""
        rules = rules_for({"change_rate_threshold": 0.5,
                           "absolute_threshold": 10.0}, mode="change_rate")
        # E=54.81 < 10 不成立也不影响：模式是 change_rate，只看环比
        self.assertTrue(D.should_deep_read(detail_of(30, 48316000, 149), rules=rules,
                                           snapshot_history=[40.0, 61.0]))
        self.assertFalse(D.should_deep_read(detail_of(1000, 1e8, 1e6), rules=rules,
                                            snapshot_history=[90.0, 50.0]))

    def test_any_mode_combination(self):
        """any 模式 = absolute 或 quantile 或 change_rate 任一满足。"""
        rules = rules_for({"change_rate_threshold": 0.5,
                           "absolute_threshold": 66.0}, mode="any")
        # 当前 54 分：absolute 不中；同批有 100 分压顶，quantile 不中；
        # 环比 [40, 61] = 0.525 ≥ 0.5 → change_rate 中 → any 触发
        batch = [100.0, 54.0]
        self.assertTrue(D.should_deep_read(batch, rules=rules,
                                           snapshot_history=[40.0, 61.0]))
        # 同一批、不传历史：三项全不中 → 不触发（环比停用回落既有规则）
        self.assertFalse(D.should_deep_read(batch, rules=rules, snapshot_history=None))
        # 环比未达阈值且 absolute 不中 → 不触发
        self.assertFalse(D.should_deep_read(batch, rules=rules,
                                            snapshot_history=[40.0, 50.0]))
        # absolute 中时 any 照旧触发（环比结果无关）
        self.assertTrue(D.should_deep_read([100.0, 80.0], rules=rules,
                                           snapshot_history=[90.0, 50.0]))


# ===========================================================================
# 4. deep_read 端到端：触发详情与返回形状
# ===========================================================================

class TestDeepReadChangeRateEndToEnd(unittest.TestCase):
    """deep_read(..., snapshot_history=...)：详情如实、形状守卫。"""

    def test_trigger_detail_records_ratio_and_snapshots(self):
        """触发时详情如实记录所用两快照与比率，模板口径写明环比。"""
        rules = rules_for({"change_rate_threshold": 0.5}, mode="change_rate")
        r = D.deep_read("某话题", detail_of(30, 48316000, 149), use_llm=False,
                        rules=rules, snapshot_history=[40.0, 61.0])
        self.assertTrue(r["triggered"])
        self.assertEqual(r["trigger_mode"], "change_rate")
        detail = r["trigger_detail"]["change_rate"]
        self.assertTrue(detail["evaluated"])
        self.assertTrue(detail["triggered"])
        self.assertEqual(detail["prev"], 40.0)
        self.assertEqual(detail["latest"], 61.0)
        self.assertAlmostEqual(detail["rate"], 0.525, places=12)
        self.assertEqual(detail["threshold"], 0.5)
        # 模板阈值表述随模式变化，不谎称绝对阈值
        self.assertIn("环比增幅 ≥ 0.5", r["text"])
        self.assertNotIn("E > 66", r["text"])

    def test_not_triggered_records_evaluated_reason(self):
        """双快照未达阈值：不触发，但详情如实记录比率与两快照。"""
        rules = rules_for({"change_rate_threshold": 0.6}, mode="change_rate")
        r = D.deep_read("某话题", detail_of(30, 48316000, 149), use_llm=False,
                        rules=rules, snapshot_history=[40.0, 50.0])
        self.assertFalse(r["triggered"])
        detail = r["trigger_detail"]["change_rate"]
        self.assertTrue(detail["evaluated"])
        self.assertFalse(detail["triggered"])
        self.assertEqual((detail["prev"], detail["latest"]), (40.0, 50.0))
        self.assertAlmostEqual(detail["rate"], 0.25, places=12)
        self.assertIn("0.25", r["reason"])  # 未触发原因里如实写比率

    def test_insufficient_history_records_reason(self):
        """传了历史但不足 2 条：不触发，详情写明单快照停用原因。"""
        rules = rules_for({"change_rate_threshold": 0.5}, mode="change_rate")
        r = D.deep_read("某话题", detail_of(30, 48316000, 149), use_llm=False,
                        rules=rules, snapshot_history=[50.0])
        self.assertFalse(r["triggered"])
        detail = r["trigger_detail"]["change_rate"]
        self.assertFalse(detail["evaluated"])
        self.assertIn("单快照", detail["reason"])
        self.assertIn("快照历史不足", r["reason"])


# ===========================================================================
# 5. 逐位不变守卫：不传 snapshot_history 的既有行为
# ===========================================================================

class TestBackwardCompatGuards(unittest.TestCase):
    """不传 snapshot_history → 触发判定与返回形状与改造前逐位一致。"""

    #: deep_read 未触发出口的既有键集（改造前形状，逐键对拍）
    LEGACY_NOT_TRIGGERED_KEYS = {
        "triggered", "text", "source", "degraded", "warnings", "reason",
        "signals", "applicable", "result", "guard_dropped"}
    #: 模板回退出口的既有键集
    LEGACY_TEMPLATE_KEYS = {
        "triggered", "text", "source", "degraded", "degraded_reason", "warnings",
        "signals", "profile", "trigger_mode", "trigger_threshold",
        "applicable", "result", "guard_dropped"}

    def test_return_shape_without_history_unchanged(self):
        """不传 snapshot_history：未触发/模板回退两出口均无 trigger_detail、键集不变。"""
        not_triggered = D.deep_read("t", detail_of(30, 48316000, 149), use_llm=False)
        self.assertFalse(not_triggered["triggered"])
        self.assertNotIn("trigger_detail", not_triggered)
        self.assertEqual(set(not_triggered), self.LEGACY_NOT_TRIGGERED_KEYS)

        template = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=False)
        self.assertTrue(template["triggered"])
        self.assertNotIn("trigger_detail", template)
        self.assertEqual(set(template), self.LEGACY_TEMPLATE_KEYS)

    def test_trigger_decision_without_history_matches_legacy(self):
        """不传 snapshot_history：should_deep_read 与改造前 deep_read_triggered 逐位一致。"""
        rules = rules_for()
        for _, x, y, z in TOPICS:
            d = detail_of(x, y, z)
            self.assertEqual(
                D.should_deep_read(d, profile="spec", rules=rules),
                A.deep_read_triggered(d["score"], 66.0),
                f"话题 ({x},{y},{z}) 触发判定与改造前不一致")
            # 显式传 None 也必须等价于不传
            self.assertEqual(
                D.should_deep_read(d, profile="spec", rules=rules,
                                   snapshot_history=None),
                A.deep_read_triggered(d["score"], 66.0))

    def test_providing_history_does_not_change_absolute_quantile(self):
        """传了 history 也不影响 absolute/quantile 判定（只给 change_rate 加通道）。"""
        rules = rules_for()
        d = detail_of(30, 48316000, 149)
        for history in ([40.0, 99.0], [1.0, 2.0], None):
            self.assertEqual(
                D.should_deep_read(d, profile="spec", rules=rules,
                                   snapshot_history=history),
                A.deep_read_triggered(d["score"], 66.0))


# ===========================================================================
# 6. 规则文件：默认档干净加载 + change_rate_threshold 写坏告警路径
# ===========================================================================

class TestRulesFile(unittest.TestCase):
    """deep_read_rules.json 改动后：告警路径不破坏、坏阈值退回停用而非崩溃。"""

    def test_default_rules_still_load_cleanly(self):
        """默认规则文件加载零告警，change_rate_threshold 保持未配置。"""
        rules, warnings = D.load_rules()
        self.assertEqual(warnings, [])
        self.assertIsNone(rules["trigger"]["change_rate_threshold"])
        self.assertEqual(rules["trigger"]["mode"], "absolute")
        self.assertEqual(rules["deep_read"]["max_words"], 300)
        self.assertTrue(rules["banned_patterns"])

    def test_rules_json_values_unchanged(self):
        """规则文件只改注释不改值：触发口径与模块默认仍逐键一致。"""
        data = json.loads(D.DEEP_READ_RULES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["trigger"]["mode"], D.DEFAULT_TRIGGER_RULES["mode"])
        self.assertEqual(data["trigger"]["absolute_threshold"],
                         D.DEFAULT_TRIGGER_RULES["absolute_threshold"])
        self.assertEqual(data["trigger"]["change_rate_threshold"],
                         D.DEFAULT_TRIGGER_RULES["change_rate_threshold"])
        self.assertIsNone(data["trigger"]["change_rate_threshold"])  # 默认不启用环比

    def test_bad_change_rate_threshold_warns_and_disables_only_it(self):
        """change_rate_threshold 写坏：告警 + 该项停用，其他段不受影响。"""
        for bad in (-1, "abc", True, [0.5]):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "r.json"
                p.write_text(json.dumps({"trigger": {"mode": "absolute",
                                                     "change_rate_threshold": bad},
                                         "deep_read": {"max_words": 500}}),
                             encoding="utf-8")
                rules, warnings = D.load_rules(str(p))
                self.assertIsNone(rules["trigger"]["change_rate_threshold"])
                self.assertTrue(any("change_rate_threshold" in w for w in warnings))
                # 其他段不破坏
                self.assertEqual(rules["trigger"]["mode"], "absolute")
                self.assertEqual(rules["trigger"]["absolute_threshold"], 66.0)
                self.assertEqual(rules["deep_read"]["max_words"], 500)
                self.assertTrue(rules["banned_patterns"])

    def test_valid_change_rate_threshold_loads_as_float(self):
        """合法阈值（含 by_profile 覆盖）照常加载，且坏值不影响触发判定（手搓规则同防线）。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({
                "trigger": {"mode": "change_rate", "change_rate_threshold": 0.5,
                            "by_profile": {"reachable": {"change_rate_threshold": 0.8}}},
                "deep_read": {}}), encoding="utf-8")
            rules, warnings = D.load_rules(str(p))
        self.assertEqual(warnings, [])
        self.assertEqual(rules["trigger"]["change_rate_threshold"], 0.5)
        resolved = D.resolve_trigger_rules(rules, "reachable")
        self.assertEqual(resolved["change_rate_threshold"], 0.8)
        # 手搓规则（未经 _normalize_rules）里写坏阈值：评估层同防线兜住，不抛不触发
        hand_made = rules_for({"change_rate_threshold": "oops"}, mode="change_rate")
        self.assertFalse(D.should_deep_read({"score": 99.0}, rules=hand_made,
                                            snapshot_history=[40.0, 99.0]))


if __name__ == "__main__":
    unittest.main()
