# -*- coding: utf-8 -*-
"""2026-10-22 检验报告 P1/P2 修复（配置外置）的回归测试。

覆盖：提示词/枚举白名单外置（G8）、提供方表外置（G4）、言论分层单点化（G5）、
话术权重外置（G5b），以及"配置文件与代码内置默认不漂移"。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mylocalai.classic import language as LM# noqa: E402
from mylocalai.classic import logic as LG# noqa: E402
from mylocalai.rumor import patterns
from mylocalai.core import tiering
from mylocalai.config import config_loader
from mylocalai.llm import llm_client
from mylocalai.config import prompt_config
import mylocalai.classic.report as report_generator# noqa: E402


def _write(data, name):
    path = ROOT / ".tmp" / "verify" / "p1p2_tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class TestPromptConfig(unittest.TestCase):
    """G8(b)：提示词与枚举白名单外置。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("RUMOR_PROMPTS_PATH", "RUMOR_PROMPTS_EXTRA_PATH")}
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_file_matches_builtin_constants(self):
        """防漂移：prompts.json 与代码里的 *_BUILTIN 必须逐字一致。"""
        data = prompt_config.load_prompts()
        self.assertEqual(data["classic_emotion"]["system"], LM.EMOTION_SYSTEM_PROMPT_BUILTIN)
        self.assertEqual(data["classic_aggression"]["system"], LM.AGGRESSION_SYSTEM_PROMPT_BUILTIN)
        self.assertEqual(data["classic_logic"]["system"], LG.SYSTEM_PROMPT_BUILTIN)
        self.assertEqual(data["report"]["system"], report_generator.SYSTEM_PROMPT_BUILTIN)
        self.assertEqual(set(data["classic_emotion"]["whitelists"]["emotion_tendency"]),
                         set(LM._EMOTION_TENDENCY_ENUM_BUILTIN))
        self.assertEqual(set(data["classic_logic"]["whitelists"]["logic_relevance"]),
                         set(LG._RELEVANCE_ENUM_BUILTIN))
        self.assertEqual(set(data["classic_logic"]["whitelists"]["logic_fallacy"]),
                         set(LG._FALLACY_ENUM_BUILTIN))

    def test_extra_path_overrides_the_prompt(self):
        extra = _write({"classic_emotion": {"system": "SENTINEL-EMOTION"}}, "extra_prompts.json")
        self.addCleanup(os.remove, extra)
        os.environ["RUMOR_PROMPTS_EXTRA_PATH"] = str(extra)
        self.assertEqual(prompt_config.get_prompt("classic_emotion", "builtin"), "SENTINEL-EMOTION")
        # 没被覆盖的段不受影响
        self.assertEqual(prompt_config.get_prompt("classic_logic", "builtin"),
                         LG.SYSTEM_PROMPT_BUILTIN)

    def test_whitelist_can_be_extended_by_extra_path(self):
        extra = _write({"classic_emotion": {"whitelists": {"emotion_tendency": ["新增档"]}}},
                       "extra_enum.json")
        self.addCleanup(os.remove, extra)
        os.environ["RUMOR_PROMPTS_EXTRA_PATH"] = str(extra)
        merged = prompt_config.get_whitelist("classic_emotion", "emotion_tendency", frozenset())
        self.assertIn("新增档", merged)
        self.assertIn("冷静客观", merged, "增量覆盖是取并集，不该丢掉原有取值")

    def test_broken_extra_file_falls_back_with_warning(self):
        bad = ROOT / ".tmp" / "verify" / "p1p2_tests" / "bad_prompts.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{ not json", encoding="utf-8")
        self.addCleanup(os.remove, bad)
        os.environ["RUMOR_PROMPTS_EXTRA_PATH"] = str(bad)
        # 增量文件坏掉不该连累底稿：默认文件里的提示词照常生效
        self.assertEqual(prompt_config.get_prompt("classic_emotion", "builtin"),
                         LM.EMOTION_SYSTEM_PROMPT_BUILTIN)
        self.assertTrue(any("增量配置" in w for w in prompt_config.warnings()),
                        f"坏增量文件必须留告警：{prompt_config.warnings()}")

    def test_full_replacement_missing_sections_warns(self):
        partial = _write({"classic_emotion": {"system": "only"}}, "partial_prompts.json")
        self.addCleanup(os.remove, partial)
        os.environ["RUMOR_PROMPTS_PATH"] = str(partial)
        prompt_config.load_prompts()
        self.assertTrue(any("#replaced" in w or "未包含这些段" in w
                            for w in prompt_config.warnings()),
                        f"整套替换漏段必须留告警：{prompt_config.warnings()}")


class TestProviderTable(unittest.TestCase):
    """G4：提供方表外置。"""

    #: 连同注册表/密钥/隐私偏好路径一起隔离：providers() 会读 data/models.json，
    #: 用户在设置面板自建的提供方会让"默认文件 == 内置表"的断言失效。
    #: LLM_PROVIDER / ZHIPU_API_KEY 也要纳入：test_extra_path_adds_a_new_provider
    #: 的 finally 会 pop 掉 LLM_PROVIDER（.env 加载的原值），不保存还原就会
    #: 泄漏给后续测试（target：LLM_PROVIDER=None 时 claims 适配走空路径）。
    KEYS = ("LLM_PROVIDERS_PATH", "LLM_PROVIDERS_EXTRA_PATH",
            "MYLOCALAI_MODELS_PATH", "MYLOCALAI_MODEL_SECRETS_PATH",
            "MYLOCALAI_PRIVACY_PREFERENCES_PATH",
            "LLM_PROVIDER", "ZHIPU_API_KEY")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for key in self.KEYS:
            os.environ.pop(key, None)
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["MYLOCALAI_MODELS_PATH"] = str(Path(self._tmp.name) / "models.json")
        os.environ["MYLOCALAI_MODEL_SECRETS_PATH"] = str(Path(self._tmp.name) / "secrets.json")
        os.environ["MYLOCALAI_PRIVACY_PREFERENCES_PATH"] = str(Path(self._tmp.name) / "privacy.json")
        llm_client.reset_cache()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        llm_client.reset_cache()

    def test_default_file_matches_builtin_providers(self):
        data = llm_client.providers()
        self.assertEqual(data, llm_client._BUILTIN_PROVIDERS)
        self.assertTrue(llm_client.supports_thinking_disable("longcat"))
        self.assertFalse(llm_client.supports_thinking_disable("openai"))

    def test_extra_path_adds_a_new_provider(self):
        extra = _write({"zhipu": {"key_env": "ZHIPU_API_KEY", "base_url_env": "ZHIPU_BASE_URL",
                                  "model_env": "ZHIPU_MODEL",
                                  "base_url": "https://example.invalid/v1",
                                  "model": "glm-zero", "supports_thinking_disable": False}},
                       "extra_providers.json")
        self.addCleanup(os.remove, extra)
        os.environ["LLM_PROVIDERS_EXTRA_PATH"] = str(extra)
        os.environ["ZHIPU_API_KEY"] = "zh-test"
        os.environ["LLM_PROVIDER"] = "zhipu"
        try:
            llm_client.reset_cache()
            self.assertIn("zhipu", llm_client.providers())
            self.assertEqual(llm_client.first_available_provider(), "zhipu")
            self.assertFalse(llm_client.supports_thinking_disable("zhipu"))
            self.assertEqual(llm_client.get_base_url(), "https://example.invalid/v1")
            self.assertEqual(llm_client.get_model(), "glm-zero")
        finally:
            os.environ.pop("ZHIPU_API_KEY", None)
            os.environ.pop("LLM_PROVIDER", None)
            llm_client.reset_cache()

    def test_broken_extra_file_falls_back_with_warning(self):
        bad = ROOT / ".tmp" / "verify" / "p1p2_tests" / "bad_providers.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("not json", encoding="utf-8")
        self.addCleanup(os.remove, bad)
        os.environ["LLM_PROVIDERS_EXTRA_PATH"] = str(bad)
        llm_client.reset_cache()
        self.assertIn("longcat", llm_client.providers())
        self.assertTrue(any("增量配置" in w for w in llm_client.provider_warnings()),
                        f"坏增量文件必须留告警：{llm_client.provider_warnings()}")


class TestTieringAndWeights(unittest.TestCase):
    """G5 / G5b：言论分层单点化 + 话术权重外置。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("RUMOR_RULES_PATH", "RUMOR_RULES_EXTRA_PATH")}
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_core_judge_and_speech_tier_share_one_implementation(self):
        from mylocalai.rumor import verdict
        cases = [
            ({"emotion_score": 9.0}, {"aggression_level": "高"}, {"logic_relevance": "弱"}),
            ({"emotion_score": 6.0}, {"aggression_level": "低"}, {"logic_relevance": "弱"}),
            ({"emotion_score": 1.0}, {"aggression_level": "低"}, {"logic_relevance": "强"}),
            ({"emotion_score": 3.0}, {"aggression_level": "中"}, {"logic_relevance": "中"}),
        ]
        for emo, atk, lg in cases:
            self.assertEqual(tiering.speech_tier(emo, atk, lg), verdict.speech_tier(emo, atk, lg))

    def test_tier_thresholds_are_configurable(self):
        # high_score 抬到 10：9 分且攻击性高、逻辑弱 → 从"高度疑似"降为"疑似"
        extra = _write({"speech_tier": {"high_score": 10, "suspect_score": 5}}, "tier_extra.json")
        self.addCleanup(os.remove, extra)
        os.environ["RUMOR_RULES_EXTRA_PATH"] = str(extra)
        import importlib
        from mylocalai.rumor import verdict
        importlib.reload(verdict)
        try:
            emo = {"emotion_score": 9.0}
            atk = {"aggression_level": "高"}
            lg = {"logic_relevance": "弱"}
            self.assertEqual(verdict.speech_tier(emo, atk, lg), tiering.TIER_SUSPECT)
            self.assertEqual(tiering.speech_tier(emo, atk, lg), tiering.TIER_SUSPECT)
        finally:
            os.environ.pop("RUMOR_RULES_EXTRA_PATH", None)
            importlib.reload(verdict)

    def test_marker_weights_are_configurable(self):
        before = patterns.scan_markers("网传这个消息，速转给家人！")
        extra = _write({"markers": {"risk_weights": {"virality": 1}}}, "weights_extra.json")
        self.addCleanup(os.remove, extra)
        os.environ["RUMOR_RULES_EXTRA_PATH"] = str(extra)
        import importlib
        importlib.reload(patterns)
        try:
            after = patterns.scan_markers("网传这个消息，速转给家人！")
            self.assertEqual(after["risk_hits"]["virality"]["points"], 1,
                             "权重改成 1 之后，诱导传播的分值应跟着变")
            self.assertLess(after["marker_points"], before["marker_points"])
        finally:
            os.environ.pop("RUMOR_RULES_EXTRA_PATH", None)
            importlib.reload(patterns)


class TestConfigLoader(unittest.TestCase):
    """共用加载器本身的行为：合并语义与漏段告警。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("RUMOR_PROMPTS_PATH", "RUMOR_PROMPTS_EXTRA_PATH")}
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_merge_recurses_and_unions_lists(self):
        default = _write({"classic_emotion": {"system": "S", "whitelists": {"a": ["1", "2"]}},
                          "report": {"system": "R"}}, "merge_default.json")
        extra = _write({"classic_emotion": {"whitelists": {"a": ["2", "3"], "b": ["9"]}}},
                       "merge_extra.json")
        self.addCleanup(os.remove, default)
        self.addCleanup(os.remove, extra)
        os.environ["RUMOR_PROMPTS_PATH"] = str(default)
        os.environ["RUMOR_PROMPTS_EXTRA_PATH"] = str(extra)
        data = prompt_config.load_prompts()
        self.assertEqual(data["classic_emotion"]["system"], "S", "未覆盖的段保持原样")
        self.assertEqual(data["classic_emotion"]["whitelists"]["a"], ["1", "2", "3"],
                         "列表取并集且保留 base 顺序")
        self.assertEqual(data["classic_emotion"]["whitelists"]["b"], ["9"])
        self.assertEqual(data["report"]["system"], "R")

    def test_missing_default_file_falls_back_with_warning(self):
        os.environ["RUMOR_PROMPTS_PATH"] = str(ROOT / ".tmp" / "verify" / "p1p2_tests" / "nope.json")
        data = prompt_config.load_prompts()
        self.assertEqual(data, {}, "默认文件缺失时返回空配置（各模块用内置默认兜底）")
        self.assertTrue(any("未找到" in w for w in prompt_config.warnings()),
                        f"默认文件缺失必须留告警：{prompt_config.warnings()}")


if __name__ == "__main__":
    unittest.main()
