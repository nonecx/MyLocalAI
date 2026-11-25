# -*- coding: utf-8 -*-
"""关注度现象判读（Attention Phenomenon）离线测试：纯函数、零 LLM、零网络。

覆盖（「新增确定性关注度现象判读层」工作单）：
  1. 五档形态各一例（阈值内），label / confidence 与 archetype 一一对应；
  2. 退化输入：全 0 / x=0 / y=0 / z=0 / 负数 / inf / nan / bool / 非数值 /
     超出 float 范围的大整数 → unknown 或安全降级，不抛、不返回 inf/nan；
  3. 灵魂用例：五组 E≈48.53 的输入必须判出五种不同形态（判据只看比值形态，不看 E）；
  4. 判读句必带「同口径相对」「代理估计」，且不含绝对断言；
  5. basis 非空、每条能对上输入/阈值，可直接复算；
  6. 配置写坏（缺段 / 类型错 / 阈值非法 / JSON 坏 / 文件缺失）→ 退回内置默认 +
     中文告警，不崩；labels 可单独覆盖；
  7. payload 接线：attention_for_text / /api/attention / /api/attention/search /
     heat_formula 服务都带 phenomenon（检索用 mock，不联网）；
  8. 前端契约：cards.js 的 renderHeat 含判读行与可折叠依据，缺字段时整块不渲染。

运行（项目根目录下）：
    .venv/Scripts/python.exe -m unittest tests.test_attention_phenomenon -v
"""

from __future__ import annotations

import json
import math
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到包化后的 web/app.py

from mylocalai.attention import attention as A  # noqa: E402
from mylocalai.attention import phenomenon as P  # noqa: E402
from mylocalai.attention.attach import attention_for_text  # noqa: E402
from mylocalai.assistant.loop import _real_service  # noqa: E402

STATIC = ROOT / "src" / "mylocalai" / "web" / "static"

# 五档形态的代表点（全部在阈值内；E 见第 3 组灵魂用例）
CASES: dict[str, tuple[float, float, float]] = {
    "broad_deep": (30, 100000, 1000),
    "broad_shallow": (30, 10000000, 9),
    "narrow_deep": (3, 1000000, 771),
    "single_point": (1, 1000000, 1542),
    "low_attention": (19, 9682000, 15),
}

EXPECTED_LABELS = {
    "broad_deep": "全网热议",
    "broad_shallow": "广而浅·媒体议题",
    "narrow_deep": "窄而深·圈层激辩",
    "single_point": "单点引爆",
    "low_attention": "低度关注",
    "unknown": "无法判读",
}

REQUIRED_KEYS = {"archetype", "label", "sentence", "basis", "ratios",
                 "confidence", "profile", "proxy_flags"}


def _block(src: str, start: str, end: str) -> str:
    """切出 start 标记到 end 标记之间的代码段（取不到就返回空串）。"""
    i = src.find(start)
    if i < 0:
        return ""
    j = src.find(end, i + len(start))
    return src[i:j if j >= 0 else len(src)]


class TestArchetypes(unittest.TestCase):
    """五档形态各一例：判据、档位名、置信度、返回键。"""

    def test_broad_deep(self):
        out = P.classify_phenomenon(*CASES["broad_deep"])
        self.assertEqual(out["archetype"], "broad_deep")
        self.assertEqual(out["label"], "全网热议")
        self.assertEqual(out["confidence"], "high")
        self.assertIn("z/y=0.01 ≥ 0.002", " ".join(out["basis"]))

    def test_broad_shallow(self):
        out = P.classify_phenomenon(*CASES["broad_shallow"])
        self.assertEqual(out["archetype"], "broad_shallow")
        self.assertEqual(out["label"], "广而浅·媒体议题")
        self.assertEqual(out["confidence"], "high")

    def test_narrow_deep(self):
        out = P.classify_phenomenon(*CASES["narrow_deep"])
        self.assertEqual(out["archetype"], "narrow_deep")
        self.assertEqual(out["label"], "窄而深·圈层激辩")
        self.assertEqual(out["confidence"], "high")

    def test_single_point(self):
        out = P.classify_phenomenon(*CASES["single_point"])
        self.assertEqual(out["archetype"], "single_point")
        self.assertEqual(out["label"], "单点引爆")
        self.assertEqual(out["confidence"], "high")
        joined = " ".join(out["basis"])
        self.assertIn("y/x=1e+06 ≥ 1e+06", joined)
        self.assertIn("z/x=1542 ≥ 500", joined)

    def test_low_attention(self):
        out = P.classify_phenomenon(*CASES["low_attention"])
        self.assertEqual(out["archetype"], "low_attention")
        self.assertEqual(out["label"], "低度关注")
        self.assertEqual(out["confidence"], "high")

    def test_unknown_all_zero(self):
        out = P.classify_phenomenon(0, 0, 0)
        self.assertEqual(out["archetype"], "unknown")
        self.assertEqual(out["label"], "无法判读")
        self.assertEqual(out["confidence"], "low")

    def test_fixed_keys_and_echo(self):
        out = P.classify_phenomenon(30, 100000, 1000, profile="reachable",
                                    proxy_flags=["y_proxy", "z_proxy"])
        self.assertTrue(REQUIRED_KEYS <= set(out), f"缺少固定键：{REQUIRED_KEYS - set(out)}")
        self.assertEqual(out["profile"], "reachable")
        self.assertEqual(out["proxy_flags"], ["y_proxy", "z_proxy"])
        self.assertEqual(set(out["ratios"]), {"spread", "engagement_ratio", "resonance"})

    def test_ratios_reuse_derive_signals(self):
        """三个比值必须与 derive_signals 逐位一致（不另写一套口径）。"""
        signals = A.derive_signals(*CASES["broad_deep"])
        out = P.classify_phenomenon(*CASES["broad_deep"])
        for key in ("spread", "engagement_ratio", "resonance"):
            self.assertEqual(out["ratios"][key], signals[key], key)


class TestDegenerateInputs(unittest.TestCase):
    """退化输入：unknown 或安全降级，绝不抛、绝不返回 inf/nan。"""

    def _assert_finite(self, out):
        for key, value in out["ratios"].items():
            self.assertTrue(math.isfinite(value), f"{key} 不是有限数：{value!r}")
            self.assertGreaterEqual(value, 0.0, f"{key} 为负：{value!r}")

    def test_x_zero_is_unknown(self):
        out = P.classify_phenomenon(0, 1000, 10)
        self.assertEqual(out["archetype"], "unknown")
        self._assert_finite(out)
        self.assertTrue(any("文章数缺失" in item for item in out["basis"]))

    def test_y_zero_degrades_to_low_confidence(self):
        """y=0 缺阅读量：参与深度不可判，按广度/共鸣降级判读，置信度 low。"""
        out = P.classify_phenomenon(30, 0, 100)
        self.assertEqual(out["archetype"], "broad_shallow")
        self.assertEqual(out["confidence"], "low")
        self.assertIn("降级判读", out["sentence"])
        self._assert_finite(out)

    def test_z_zero_is_medium_confidence(self):
        out = P.classify_phenomenon(100, 10000000, 0)
        self.assertEqual(out["archetype"], "broad_shallow")
        self.assertEqual(out["confidence"], "medium")
        self._assert_finite(out)

    def test_all_zero_unknown(self):
        out = P.classify_phenomenon(0, 0, 0)
        self.assertEqual(out["archetype"], "unknown")
        self.assertEqual(out["ratios"]["spread"], 0.0)
        self.assertEqual(out["ratios"]["engagement_ratio"], 0.0)
        self.assertEqual(out["ratios"]["resonance"], 0.0)

    def test_negative_inputs_do_not_raise(self):
        for point in ((-1, 1, 1), (1, -1, 1), (1, 1, -1), (-5, -5, -5)):
            with self.subTest(point=point):
                out = P.classify_phenomenon(*point)
                self.assertEqual(out["archetype"], "unknown")
                self._assert_finite(out)

    def test_non_finite_inputs_do_not_raise(self):
        bad = (float("inf"), float("-inf"), float("nan"))
        for value in bad:
            for index in range(3):
                point = [30.0, 100000.0, 1000.0]
                point[index] = value
                with self.subTest(point=tuple(point)):
                    out = P.classify_phenomenon(*point)
                    self.assertEqual(out["archetype"], "unknown")
                    self._assert_finite(out)

    def test_non_numeric_inputs_do_not_raise(self):
        for value in (True, False, "30", None, [1], {"x": 1}):
            with self.subTest(value=value):
                out = P.classify_phenomenon(value, 1000, 10)
                self.assertEqual(out["archetype"], "unknown")
                self._assert_finite(out)

    def test_huge_integer_does_not_raise(self):
        """超出 float 范围的大整数（float() 会 OverflowError）也必须兜住。"""
        out = P.classify_phenomenon(10 ** 400, 1, 1)
        self.assertEqual(out["archetype"], "unknown")
        self._assert_finite(out)

    def test_weird_repr_object_does_not_raise(self):
        class Boom:
            def __repr__(self):
                raise RuntimeError("boom")

        out = P.classify_phenomenon(Boom(), 1, 1)
        self.assertEqual(out["archetype"], "unknown")
        self._assert_finite(out)

    def test_invalid_params_degrade_not_raise(self):
        out = P.classify_phenomenon(30, 100000, 1000, params={"x0": -1})
        self.assertIn(out["archetype"], set(P.ARCHETYPES) - {"unknown"})
        self.assertTrue(any("params 不合法" in item for item in out["basis"]))


class TestSameScoreDifferentShape(unittest.TestCase):
    """灵魂用例：E 几乎相同，形态必须不同——只按 E 分档的话这条功能就白做了。"""

    def test_five_shapes_share_one_score(self):
        scores = [A.attention_score(*point) for point in CASES.values()]
        spread = max(scores) - min(scores)
        self.assertLess(spread, 0.01, f"五组输入的 E 不够接近：{scores}")
        self.assertTrue(all(48.0 < score < 49.0 for score in scores), scores)

        archetypes = [P.classify_phenomenon(*point)["archetype"]
                      for point in CASES.values()]
        self.assertEqual(archetypes, list(CASES.keys()))
        self.assertEqual(len(set(archetypes)), 5)

    def test_score_field_is_not_used_for_shape(self):
        """同一形态在不同 E 档上必须保持同一判读（形态判据与 E 正交）。"""
        low = P.classify_phenomenon(3, 1000, 1)          # E 很低
        high = P.classify_phenomenon(3, 1e6, 1000)       # E 明显更高
        self.assertEqual(low["archetype"], "low_attention")
        self.assertEqual(high["archetype"], "narrow_deep")
        self.assertNotEqual(A.attention_score(3, 1000, 1), A.attention_score(3, 1e6, 1000))


class TestSentenceAndBasis(unittest.TestCase):
    """判读句口径诚实；basis 可复算。"""

    def test_sentence_carries_proxy_qualifier(self):
        for archetype, point in CASES.items():
            with self.subTest(archetype=archetype):
                sentence = P.classify_phenomenon(*point)["sentence"]
                self.assertIn("同口径相对", sentence)
                self.assertIn("代理估计", sentence)

    def test_sentence_has_no_absolute_claim(self):
        banned = ("全社会广泛关注", "所有人都", "必然", "一定", "普遍", "确凿")
        for archetype, point in CASES.items():
            sentence = P.classify_phenomenon(*point)["sentence"]
            for word in banned:
                self.assertNotIn(word, sentence, f"{archetype} 判读句出现绝对断言：{word}")

    def test_basis_non_empty_and_traceable(self):
        for archetype, point in CASES.items():
            with self.subTest(archetype=archetype):
                basis = P.classify_phenomenon(*point)["basis"]
                self.assertTrue(basis, f"{archetype} 的 basis 为空")
                for item in basis:
                    self.assertIsInstance(item, str)
                    self.assertTrue(item.strip())
                    self.assertTrue(any(marker in item for marker in ("x=", "y=", "z=")),
                                    f"依据对不上输入：{item}")

    def test_basis_contains_threshold_and_value(self):
        out = P.classify_phenomenon(30, 100000, 1000)
        joined = " ".join(out["basis"])
        self.assertIn("x=30 ≥ 20（广度达标）", joined)
        self.assertIn("z/y=0.01 ≥ 0.002（参与深度达标）", joined)
        self.assertIn("z/x=33.3333 ≥ 5（单篇共鸣高）", joined)

    def test_basis_for_low_attention_shows_three_low(self):
        joined = " ".join(P.classify_phenomenon(*CASES["low_attention"])["basis"])
        self.assertIn("x=19 < 20（广度不足）", joined)
        self.assertIn("参与深度低", joined)
        self.assertIn("单篇共鸣低", joined)

    def test_sentence_mentions_degraded_when_y_missing(self):
        out = P.classify_phenomenon(30, 0, 100)
        self.assertIn("阅读量缺失", out["sentence"])


class TestRulesConfig(unittest.TestCase):
    """阈值外置：默认口径、写坏回退、告警、labels 可覆盖。"""

    def test_default_rules_load_clean(self):
        rules, warnings = P.load_phenomenon_rules()
        self.assertEqual(warnings, [])
        self.assertEqual(rules["thresholds"], P.DEFAULT_THRESHOLDS)
        self.assertEqual(rules["labels"], P.DEFAULT_LABELS)

    def test_missing_phenomenon_section_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps({"params": {"x0": 100}}), encoding="utf-8")
            rules, warnings = P.load_phenomenon_rules(path)
        self.assertTrue(warnings)
        self.assertIn("phenomenon", warnings[0])
        self.assertEqual(rules["thresholds"], P.DEFAULT_THRESHOLDS)

    def test_wrong_type_thresholds_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            payload = {"phenomenon": {"thresholds": {"spread_min": "20"}}}
            path.write_text(json.dumps(payload), encoding="utf-8")
            rules, warnings = P.load_phenomenon_rules(path)
        self.assertTrue(any("不合法" in item for item in warnings))
        self.assertEqual(rules["thresholds"], P.DEFAULT_THRESHOLDS)

    def test_negative_threshold_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            thresholds = dict(P.DEFAULT_THRESHOLDS, engagement_min=-0.1)
            path.write_text(json.dumps({"phenomenon": {"thresholds": thresholds}}),
                            encoding="utf-8")
            rules, warnings = P.load_phenomenon_rules(path)
        self.assertTrue(any("engagement_min" in item for item in warnings))
        self.assertEqual(rules["thresholds"], P.DEFAULT_THRESHOLDS)

    def test_single_point_not_stricter_than_resonance_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            thresholds = dict(P.DEFAULT_THRESHOLDS, single_point_resonance=5)
            path.write_text(json.dumps({"phenomenon": {"thresholds": thresholds}}),
                            encoding="utf-8")
            rules, warnings = P.load_phenomenon_rules(path)
        self.assertTrue(any("single_point_resonance" in item for item in warnings))
        self.assertEqual(rules["thresholds"], P.DEFAULT_THRESHOLDS)

    def test_broken_json_and_missing_file_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.json"
            broken.write_text("{ not json", encoding="utf-8")
            rules, warnings = P.load_phenomenon_rules(broken)
            self.assertTrue(warnings)
            self.assertEqual(rules["thresholds"], P.DEFAULT_THRESHOLDS)

            rules2, warnings2 = P.load_phenomenon_rules(Path(tmp) / "absent.json")
            self.assertTrue(warnings2)
            self.assertEqual(rules2["thresholds"], P.DEFAULT_THRESHOLDS)

    def test_labels_can_override_individually(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            payload = {"phenomenon": {
                "thresholds": dict(P.DEFAULT_THRESHOLDS),
                "labels": {"broad_deep": "超级热议", "unknown": ""},
            }}
            path.write_text(json.dumps(payload), encoding="utf-8")
            rules, warnings = P.load_phenomenon_rules(path)
        self.assertEqual(rules["labels"]["broad_deep"], "超级热议")
        self.assertEqual(rules["labels"]["unknown"], P.DEFAULT_LABELS["unknown"])
        self.assertEqual(rules["labels"]["low_attention"], P.DEFAULT_LABELS["low_attention"])
        self.assertTrue(any("unknown" in item for item in warnings))

    def test_classify_survives_broken_config_with_warning_in_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text("{}", encoding="utf-8")
            with mock.patch.object(P, "PHENOMENON_RULES_PATH", path):
                out = P.classify_phenomenon(30, 100000, 1000)
        self.assertEqual(out["archetype"], "broad_deep")   # 退回内置默认阈值
        self.assertTrue(any(item.startswith("口径告警：") for item in out["basis"]))

    def test_custom_threshold_changes_shape(self):
        """阈值外置真的生效：把广度门槛拉到 100，30 篇就从"广"变"窄"。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            thresholds = dict(P.DEFAULT_THRESHOLDS, spread_min=100)
            path.write_text(json.dumps({"phenomenon": {"thresholds": thresholds}}),
                            encoding="utf-8")
            with mock.patch.object(P, "PHENOMENON_RULES_PATH", path):
                out = P.classify_phenomenon(30, 100000, 1000)
        self.assertEqual(out["archetype"], "narrow_deep")


class TestPayloadWiring(unittest.TestCase):
    """接线：CLI/网页/热度服务都带 phenomenon，且不改原有判定字段。"""

    def test_attention_for_text_baseline_carries_phenomenon(self):
        detail = attention_for_text("某话题", allow_network=False)
        self.assertIn("phenomenon", detail)
        self.assertEqual(detail["phenomenon"]["archetype"], "broad_deep")
        self.assertEqual(detail["phenomenon"]["label"], "全网热议")
        self.assertEqual(detail["score"], A.attention_score(
            A.DEFAULT_PARAMS["x0"], A.DEFAULT_PARAMS["y0"], A.DEFAULT_PARAMS["z0"]))

    def test_attention_for_text_echoes_profile(self):
        detail = attention_for_text("某话题", allow_network=False, profile="reachable")
        self.assertEqual(detail["phenomenon"]["profile"], "reachable")
        self.assertAlmostEqual(detail["score"], A.attention_score(
            A.DEFAULT_PARAMS["x0"], A.DEFAULT_PARAMS["y0"], A.DEFAULT_PARAMS["z0"],
            A.PROFILES["reachable"]["params"]), places=12)

    def test_attention_for_text_network_failure_degrades_with_phenomenon(self):
        with mock.patch("mylocalai.retrieval.search.search_all",
                        side_effect=RuntimeError("offline")):
            detail = attention_for_text("某话题", allow_network=True)
        self.assertEqual(detail["quality"], "fallback")
        self.assertIn("phenomenon", detail)

    def test_heat_formula_service_carries_phenomenon(self):
        service = _real_service("heat_formula", {})
        detail = service(30, 100000, 1000)
        self.assertIn("phenomenon", detail)
        self.assertEqual(detail["phenomenon"]["archetype"], "broad_deep")
        self.assertAlmostEqual(detail["score"], A.attention_score(30, 100000, 1000), places=12)

    def test_compute_heat_tool_card_carries_phenomenon(self):
        from mylocalai.assistant import tools_heat

        card = tools_heat._heat_card(
            {"x": 30, "y": 100000, "z": 1000, "score": 48.53, "band": "低关注",
             "profile": "spec", "proxy_flags": [], "phenomenon": {"label": "全网热议"}},
            "2026-01-01T00:00:00")
        self.assertEqual(card["phenomenon"]["label"], "全网热议")
        # 缺 phenomenon 的老 payload：卡片字段给空对象，前端整行不渲染
        self.assertEqual(tools_heat._heat_card({"x": 1}, "t")["phenomenon"], {})

    def test_api_attention_carries_phenomenon(self):
        try:
            from fastapi.testclient import TestClient
            from app import app
        except Exception as exc:  # noqa: BLE001  fastapi 缺失时整组跳过而非报错
            raise unittest.SkipTest(f"无法导入 mylocalai.web.app: {type(exc).__name__}: {exc}")
        client = TestClient(app)
        resp = client.post("/api/attention", json={"x": 30, "y": 100000, "z": 1000})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("phenomenon", data)
        self.assertEqual(data["phenomenon"]["archetype"], "broad_deep")
        self.assertEqual(data["phenomenon"]["profile"], "spec")
        # 不改判定：E / 档位 / 深读字段与模块口径一致
        self.assertAlmostEqual(data["score"], A.attention_score(30, 100000, 1000), places=12)
        self.assertEqual(data["band"], "低关注")
        self.assertFalse(data["deep_read"]["triggered"])

    def test_api_attention_search_carries_phenomenon(self):
        try:
            from fastapi.testclient import TestClient
            from app import app
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"无法导入 mylocalai.web.app: {type(exc).__name__}: {exc}")
        fake = {
            "query": "测试话题",
            "results": [
                {"engine": "fake", "rank": 1, "title": "测试话题引发关注",
                 "url": "https://a.example.com/1", "snippet": "阅读量 4831.6万，评论 287"},
                {"engine": "fake", "rank": 2, "title": "测试话题二",
                 "url": "https://b.example.com/2", "snippet": "评论 99"},
            ],
            "engines": {"fake": {"ok": True}},
        }
        with mock.patch("mylocalai.retrieval.search.search_all", return_value=fake):
            client = TestClient(app)
            resp = client.post("/api/attention/search", json={"query": "测试话题"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("phenomenon", data)
        self.assertEqual(data["phenomenon"]["archetype"], "single_point")
        self.assertAlmostEqual(data["score"], A.attention_score(data["x"], data["y"], data["z"]),
                               places=12)


class TestFrontendContract(unittest.TestCase):
    """前端契约：renderHeat 增加判读行，缺字段整行不渲染。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cards = (STATIC / "cards.js").read_text(encoding="utf-8")
        cls.css = (STATIC / "chat.css").read_text(encoding="utf-8")
        cls.block = _block(cls.cards, "function renderHeat(p)", "/* ---------- 4.")

    def test_render_heat_block_exists(self):
        self.assertTrue(self.block, "cards.js 缺少 renderHeat 渲染区")
        self.assertIn("function renderHeat(p)", self.block)

    def test_consumes_phenomenon_fields(self):
        for field in ("p.phenomenon", "phenomenon.label", "phenomenon.sentence",
                      "phenomenon.basis"):
            self.assertIn(field, self.block, f"renderHeat 未消费字段：{field}")

    def test_renders_label_sentence_and_foldable_basis(self):
        self.assertIn("现象判读", self.block)
        self.assertIn("判读依据", self.block)
        self.assertIn("chat-evidence-fold", self.block)
        self.assertIn("chat-ev-list", self.block)
        self.assertIn("escapeHtml(phenomenonSentence)", self.block)
        self.assertIn("escapeHtml(b)", self.block)

    def test_reuses_existing_layout_classes(self):
        self.assertIn('class="dim-row"', self.block)
        self.assertIn('class="badge gray"', self.block)

    def test_missing_field_skips_row(self):
        """字段缺失/形状不对 → 整行不渲染（老后端、老快照照常显示 E 卡）。"""
        self.assertIn('const phenomenon = (p.phenomenon && typeof p.phenomenon === "object")',
                      self.block)
        self.assertIn("(phenomenonLabel || phenomenonSentence) ?", self.block)
        self.assertIn(": \"\";", self.block)   # 空块兜底，不往 HTML 里塞 undefined

    def test_does_not_touch_conclusion_card(self):
        """不碰结论卡分发：另一条分支正在 renderConclusion 加 E 行。"""
        conclusion = _block(self.cards, "function renderConclusion(", "function renderSpeech(")
        self.assertNotIn("phenomenon", conclusion)

    def test_css_classes_defined(self):
        used = set()
        for chunk in re.findall(r'class="([^"]+)"', self.block):
            used.update(re.findall(r"[A-Za-z][\w-]*", chunk))
        missing = [c for c in sorted(used) if c.startswith("chat-")
                   and f".{c}" not in self.css]
        self.assertEqual(missing, [], f"chat.css 缺少定义：{missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
