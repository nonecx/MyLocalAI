# -*- coding: utf-8 -*-
"""`llm_client` 与模型注册表集成后的离线单测（设计规格 §5，T4 集成任务）。

为什么这样测：
  - 一切读写都重定向到 `tempfile.TemporaryDirectory()`（`MYLOCALAI_MODELS_PATH` /
    `MYLOCALAI_MODEL_SECRETS_PATH` / `MYLOCALAI_PRIVACY_PREFERENCES_PATH`），
    **绝不碰真实 data/**，跑完即清理；
  - 提供方相关的环境变量逐个摘掉（真实 .env 可能已经带进来），由
    `mock.patch.dict(os.environ)` 负责还原，避免"本机碰巧配了密钥"让断言时红时绿；
  - 不联网：唯一会外发的 `_chat_raw_providers` 一律 monkeypatch 掉，只验证
    "参数怎么算、缓存键怎么算、隐私门怎么过"这些纯逻辑；
  - **不复刻金样本哈希**：缓存键的字节级稳定性由 tests/test_llm_gateway.py 的金样本
    负责，这里只断言"默认路径一个字节都不加、带了参数才加"这类不变量——两边都锁，
    才不会出现"新测试抄了旧哈希、旧测试改了没人知道"的分工漏洞。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_llm_client_model_settings -v
"""

from __future__ import annotations

import copy
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.llm import llm_cache          # noqa: E402
from mylocalai.llm import llm_client         # noqa: E402
from mylocalai.llm import model_registry as registry   # noqa: E402

# 真实 .env 里的提供方相关变量：逐个摘掉，保证"当前提供方 / 密钥来源"只由本用例决定
_ENV_KEYS = (
    "LLM_PROVIDER", "LLM_FALLBACKS",
    "LONGCAT_API_KEY", "LONGCAT_MODEL", "LONGCAT_BASE_URL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL",
)

_SECRET = "sk-test-4be5ra91"


class LlmClientModelSettingsTestCase(unittest.TestCase):
    """公共夹具：临时注册表 + 三个路径环境变量 + 摘干净提供方变量 + 清两侧缓存。"""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="llm_client_models_test_")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.models_path = self.dir / "models.json"
        self.secrets_path = self.dir / "model_secrets.json"
        self.prefs_path = self.dir / "privacy_preferences.json"
        llm_cache.clear()
        self.addCleanup(llm_cache.clear)
        llm_client.reset_cache()
        self.addCleanup(llm_client.reset_cache)
        env_patch = mock.patch.dict(os.environ, {
            "MYLOCALAI_MODELS_PATH": str(self.models_path),
            "MYLOCALAI_MODEL_SECRETS_PATH": str(self.secrets_path),
            "MYLOCALAI_PRIVACY_PREFERENCES_PATH": str(self.prefs_path),
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)      # stop 时整份环境快照还原（含被摘掉的键）
        for name in _ENV_KEYS:
            os.environ.pop(name, None)
        registry.reset_cache()
        self.addCleanup(registry.reset_cache)

    # ---- 夹具工具 ----
    def activate_acme(self, **model_overrides) -> None:
        """建一个自定义提供方 + 一个带参数的模型，设为当前并存一份本地密钥。"""
        registry.upsert_provider("acme", {
            "display_name": "示例网关",
            "base_url": "https://api.example.com/v1",
            "protocol": "openai",
            "key_env": "EXAMPLE_API_KEY",
            "enabled": True,
        })
        model = {
            "id": "acme-large",
            "display_name": "acme-large",
            "context_window": 128000,
            "max_output_tokens": 4096,
            "input_types": ["text"],
            "enabled": True,
            "reasoning": {
                "enabled": True,
                "param": "reasoning_effort",
                "active_tier": "low",
                "tiers": [{"tier": "off", "api_value": None},
                          {"tier": "low", "api_value": "low"}],
            },
        }
        model.update(model_overrides)
        registry.upsert_model("acme", model)
        registry.set_active("acme", "acme-large")
        registry.set_secret("acme", _SECRET)
        # 注册表写完了：llm_client 侧的提供方表/参数缓存必须跟着失效，
        # 否则这一用例读到的还是上一轮的旧快照
        llm_client.reset_cache()

    def authorize(self, *pids: str) -> None:
        """写入"允许这些提供方外发"的隐私偏好（默认是关的 → 403 的成因）。"""
        from mylocalai.core.privacy import DEFAULT_PREFERENCES, save_preferences
        raw = copy.deepcopy(DEFAULT_PREFERENCES)
        raw["external_processing_enabled"] = True
        raw["external_processing_providers"] = list(pids)
        save_preferences(raw, self.prefs_path)


class TestNoRegistryFile(LlmClientModelSettingsTestCase):
    """规格 §0.4 / §5.1：没有 data/models.json 时，表与解析结果与改动前一致。"""

    def test_providers_table_is_byte_identical_to_builtin(self):
        self.assertFalse(self.models_path.exists(), "夹具不应自带 models.json")
        self.assertEqual(llm_client.providers(), llm_client._BUILTIN_PROVIDERS)

    def test_resolve_uses_builtin_longcat(self):
        resolved = llm_client._resolve()
        self.assertEqual(resolved["provider"], "longcat")
        self.assertEqual(resolved["model"], "LongCat-2.5-Preview")
        self.assertEqual(resolved["base_url"], "https://api.longcat.chat/openai/v1")
        self.assertIn("api.longcat.chat", resolved["base_url"])
        self.assertEqual(llm_client._provider_names(), ["longcat"])
        self.assertIsNone(llm_client.first_available_provider(), "没有密钥时给不出提供方")
        self.assertEqual(llm_client.provider_spec("longcat")["api_key"], "")

    def test_builtin_model_declares_no_params(self):
        """内置默认模型目录故意不声明上限与档位（注册表 _default_model_entry 的约定）。"""
        self.assertEqual(llm_client._model_params("longcat", "LongCat-2.5-Preview"),
                         {"max_output_tokens": None, "reasoning": None})


class TestRegistryActiveSelection(LlmClientModelSettingsTestCase):
    """规格 §5.1 / §5.2 / §5.3：有 models.json 时 active 生效，密钥走两级口径。"""

    def test_active_provider_model_and_base_url_take_effect(self):
        self.activate_acme()
        resolved = llm_client._resolve()
        self.assertEqual(resolved["provider"], "acme")
        self.assertEqual(resolved["model"], "acme-large")
        self.assertEqual(resolved["base_url"], "https://api.example.com/v1")
        self.assertEqual(resolved["api_key"], _SECRET, "没有环境变量时应读注册表密钥文件")

    def test_key_source_is_stored_then_env(self):
        self.activate_acme()
        spec = llm_client.providers()["acme"]
        self.assertEqual(spec["key_env"], "EXAMPLE_API_KEY", "key_env 用注册表声明的名字")
        self.assertEqual(llm_client._provider_key("acme", spec), (_SECRET, "stored"))
        os.environ["EXAMPLE_API_KEY"] = "sk-env-wins"
        try:
            # 环境变量优先：既是 llm_client 的口径，也是注册表 provider_secret 的口径
            self.assertEqual(llm_client._provider_key("acme", spec), ("sk-env-wins", "env"))
            self.assertEqual(llm_client._resolve()["api_key"], "sk-env-wins")
            self.assertEqual(llm_client.provider_spec("acme")["api_key"], "sk-env-wins")
            self.assertEqual(llm_client.first_available_provider(), "acme")
        finally:
            os.environ.pop("EXAMPLE_API_KEY", None)
            llm_client.reset_cache()

    def test_llm_provider_env_beats_registry_active(self):
        self.activate_acme()
        os.environ["LLM_PROVIDER"] = "deepseek"
        os.environ["DEEPSEEK_API_KEY"] = "sk-deepseek"
        try:
            llm_client.reset_cache()
            resolved = llm_client._resolve()
            self.assertEqual(resolved["provider"], "deepseek")
            self.assertEqual(resolved["model"], "deepseek-chat")
            self.assertEqual(llm_client._provider_names()[0], "deepseek")
        finally:
            os.environ.pop("LLM_PROVIDER", None)
            os.environ.pop("DEEPSEEK_API_KEY", None)
            llm_client.reset_cache()

    def test_llm_provider_env_can_point_back_to_builtin(self):
        self.activate_acme()
        os.environ["LLM_PROVIDER"] = "longcat"
        try:
            llm_client.reset_cache()
            self.assertEqual(llm_client._resolve()["provider"], "longcat")
            self.assertEqual(llm_client._resolve()["model"], "LongCat-2.5-Preview")
        finally:
            os.environ.pop("LLM_PROVIDER", None)
            llm_client.reset_cache()

    def test_registry_overrides_builtin_provider_entry(self):
        """内置提供方也能在面板里改：base_url / key_env 以注册表为准，其余键不丢。"""
        registry.upsert_provider("deepseek", {
            "base_url": "https://proxy.internal/v1",
            "key_env": "DEEPSEEK_API_KEY",
        })
        registry.set_active("deepseek", "deepseek-chat")
        llm_client.reset_cache()
        table = llm_client.providers()
        self.assertEqual(table["deepseek"]["base_url"], "https://proxy.internal/v1")
        self.assertEqual(table["deepseek"]["key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(table["deepseek"]["model_env"], "DEEPSEEK_MODEL")
        self.assertFalse(table["deepseek"]["supports_thinking_disable"])
        self.assertEqual(table["longcat"], llm_client._BUILTIN_PROVIDERS["longcat"],
                         "没被注册表提到的提供方一个字节都不该变")
        self.assertEqual(llm_client._resolve()["base_url"], "https://proxy.internal/v1")
        os.environ["DEEPSEEK_BASE_URL"] = "https://env-wins.internal/v1"
        try:
            llm_client.reset_cache()
            self.assertEqual(llm_client._resolve()["base_url"], "https://env-wins.internal/v1",
                             "提供方自己的 *_BASE_URL 环境变量仍然压过注册表")
        finally:
            os.environ.pop("DEEPSEEK_BASE_URL", None)
            llm_client.reset_cache()

    def test_custom_provider_without_key_env_gets_synthesized_name(self):
        """没声明 key_env 时合成 MYLOCALAI_<PID大写>_API_KEY——且注册表算出的同一个名字。"""
        registry.upsert_provider("acme", {
            "display_name": "没声明密钥变量",
            "base_url": "https://api.example.com/v1",
        })
        llm_client.reset_cache()
        spec = llm_client.providers()["acme"]
        self.assertEqual(spec["key_env"], "MYLOCALAI_ACME_API_KEY")
        self.assertEqual(spec["base_url_env"], "MYLOCALAI_ACME_BASE_URL")
        self.assertEqual(spec["model_env"], "MYLOCALAI_ACME_MODEL")
        os.environ["MYLOCALAI_ACME_API_KEY"] = "sk-synth"
        try:
            # 两边必须算出同一个名字：注册表的私有实现与本模块各写一份同口径规则
            self.assertEqual(registry.provider_secret("acme"), ("sk-synth", "env"))
            self.assertEqual(llm_client._provider_key("acme", spec), ("sk-synth", "env"))
        finally:
            os.environ.pop("MYLOCALAI_ACME_API_KEY", None)
            llm_client.reset_cache()


class TestModelParams(LlmClientModelSettingsTestCase):
    """规格 §5.4：max_tokens 下调与推理档位参数（在真正发起调用处生效）。"""

    def test_max_output_tokens_clamps_down_and_never_up(self):
        params = {"max_output_tokens": 4096, "reasoning": None}
        self.assertEqual(llm_client._clamp_max_tokens(10000, params), 4096)
        self.assertEqual(llm_client._clamp_max_tokens(1000, params), 1000, "要少了不该放大")
        self.assertEqual(llm_client._clamp_max_tokens(4096, params), 4096, "相等时原样")
        self.assertEqual(llm_client._clamp_max_tokens(10000, {"max_output_tokens": None}), 10000,
                         "未声明上限就不动")
        self.assertEqual(llm_client._clamp_max_tokens(10000, {}), 10000)

    def test_reasoning_enabled_with_api_value_sends_param(self):
        self.activate_acme()
        params = llm_client._model_params("acme", "acme-large")
        self.assertEqual(params["max_output_tokens"], 4096)
        self.assertEqual(params["reasoning"], {"param": "reasoning_effort", "api_value": "low"})
        self.assertEqual(llm_client._reasoning_extra_params(params), {"reasoning_effort": "low"})

    def test_reasoning_off_or_disabled_sends_nothing(self):
        for reasoning in (
            {"enabled": True, "param": "reasoning_effort", "active_tier": "off",
             "tiers": [{"tier": "off", "api_value": None},
                       {"tier": "low", "api_value": "low"}]},
            {"enabled": False, "param": "reasoning_effort", "active_tier": "low",
             "tiers": [{"tier": "off", "api_value": None},
                       {"tier": "low", "api_value": "low"}]},
            {"enabled": True, "param": "reasoning_effort", "active_tier": "low",
             "tiers": [{"tier": "low", "api_value": None}]},
        ):
            with self.subTest(reasoning=reasoning):
                self.activate_acme(reasoning=reasoning)
                params = llm_client._model_params("acme", "acme-large")
                self.assertIsNone(params["reasoning"])
                self.assertEqual(llm_client._reasoning_extra_params(params), {})

    def test_unknown_model_has_no_params(self):
        self.activate_acme()
        self.assertEqual(llm_client._model_params("acme", "no-such-model"),
                         {"max_output_tokens": None, "reasoning": None})
        self.assertEqual(llm_client._model_params("not-a-provider", "whatever"),
                         {"max_output_tokens": None, "reasoning": None})


class TestChatRawPlumbing(LlmClientModelSettingsTestCase):
    """规格 §5.4：推导出的参数同时进缓存键与真实请求（同一个键 ↔ 同一批请求字节）。"""

    def _capture(self):
        seen = {}

        def fake_providers(system_prompt, user_text, temperature, max_tokens,
                           enable_thinking=False, extra_params=None):
            seen[user_text] = {"max_tokens": max_tokens, "extra_params": extra_params}
            return "正文", ""

        return seen, fake_providers

    def test_chat_raw_clamps_and_merges_params(self):
        self.activate_acme()
        seen, fake = self._capture()
        with mock.patch.object(llm_client, "_chat_raw_providers", fake):
            content, err = llm_client._chat_raw("系统提示", "甲", 0.0, 100000)
            self.assertEqual((content, err), ("正文", ""))
            llm_client._chat_raw("系统提示", "乙", 0.0, 1000)
            llm_client._chat_raw("系统提示", "丙", 0.0, 100000, extra_params={})
        self.assertEqual(seen["甲"], {"max_tokens": 4096, "extra_params": {"reasoning_effort": "low"}},
                         "超过上限要下调，档位参数要带上")
        self.assertEqual(seen["乙"]["max_tokens"], 1000, "低于上限不该动")
        self.assertEqual(seen["丙"], {"max_tokens": 4096, "extra_params": {}},
                         "显式传空字典 = 调用方已自行处理，不再推导")

    def test_chat_raw_once_merges_reasoning_into_extra_body(self):
        calls = []

        class _Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return type("Resp", (), {"choices": [type("Ch", (), {
                    "message": type("Msg", (), {"content": "ok"})()})()]})()

        client = type("C", (), {"chat": type("H", (), {"completions": _Completions()})()})()
        content, err = llm_client._chat_raw_once(
            client, "sys", "text", 0.0, 64, False,
            "https://api.example.com/v1", "acme-large",
            extra_params={"reasoning_effort": "low"})
        self.assertEqual((content, err), ("ok", ""))
        self.assertEqual(calls[0]["extra_body"],
                         {"chat_template_kwargs": {"enable_thinking": False},
                          "reasoning_effort": "low"},
                         "档位参数与关闭思维链参数并存")


class TestCacheKeyInvariants(LlmClientModelSettingsTestCase):
    """规格 §5.4 缓存键：默认路径不加字节；这里只断言不变量，不抄金样本哈希。"""

    def test_no_extra_params_adds_no_bytes(self):
        base = llm_client._cache_key("系统提示", "用户文本", 0.0, 700, False)
        self.assertEqual(llm_client._cache_key("系统提示", "用户文本", 0.0, 700, False,
                                               extra_params=None), base)
        self.assertEqual(llm_client._cache_key("系统提示", "用户文本", 0.0, 700, False,
                                               extra_params={}), base,
                         "空字典与 None 同义：都不往 blob 里加字节")

    def test_namespace_still_partitions(self):
        base = llm_client._cache_key("系统提示", "用户文本", 0.0, 700, False)
        self.assertNotEqual(llm_client._cache_key("系统提示", "用户文本", 0.0, 700, False,
                                                  namespace="attn_deep_read"), base)

    def test_extra_params_change_the_key(self):
        base = llm_client._cache_key("s", "u", 0.0, 700, False)
        with_low = llm_client._cache_key("s", "u", 0.0, 700, False,
                                         extra_params={"reasoning_effort": "low"})
        with_high = llm_client._cache_key("s", "u", 0.0, 700, False,
                                          extra_params={"reasoning_effort": "high"})
        self.assertNotEqual(with_low, base, "真的会多带参数时键必须分开")
        self.assertNotEqual(with_low, with_high, "参数值不同也是两种请求")
        self.assertEqual(llm_client._cache_key("s", "u", 0.0, 700, False,
                                               extra_params={"reasoning_effort": "low"}), with_low,
                         "同一组参数必须稳定复现同一个键")


class TestResetCacheAndPrivacyGate(LlmClientModelSettingsTestCase):
    """规格 §5.5 / §5.6：reset_cache 清两侧缓存；自定义提供方同样要过隐私门。"""

    def test_reset_cache_clears_registry_and_params_caches(self):
        self.activate_acme()
        self.assertIn("acme", llm_client.providers())        # 填充 llm_client 侧提供方表缓存
        # 参数缓存只由 _model_params() 填充（它在真正发起调用时才被调到），
        # 所以这里先真的解析一次，否则断言的是"还没用到过"而不是"没被清掉"
        self.assertEqual(llm_client._model_params("acme", "acme-large")["max_output_tokens"], 4096)
        self.assertIsNotNone(llm_client._PARAMS_CACHE.get(("acme", "acme-large")))
        self.assertIsNotNone(registry._MODELS_CACHE)
        llm_client.reset_cache()
        self.assertIsNone(llm_client._PROVIDERS_CACHE)
        self.assertEqual(llm_client._PARAMS_CACHE, {})
        self.assertIsNone(registry._MODELS_CACHE, "注册表缓存必须跟着一起清")
        self.assertIsNone(registry._SECRETS_CACHE)
        # 行为面：文件没了再 reset，表回到内置三家
        self.models_path.unlink()
        llm_client.reset_cache()
        self.assertEqual(llm_client.providers(), llm_client._BUILTIN_PROVIDERS)
        self.assertEqual(llm_client._resolve()["provider"], "longcat")

    def test_custom_provider_still_gated_by_privacy(self):
        """隐私门在建客户端之前：未授权时不建客户端、不外发（规格 §0.2 / §5.6）。"""
        self.activate_acme()
        with mock.patch.object(llm_client, "create_client") as create, \
             mock.patch.object(llm_client, "_client_for") as client_for:
            content, err = llm_client._chat_raw_providers("系统提示", "用户文本", 0.0, 512)
        self.assertEqual(content, "")
        self.assertIn("acme", err)
        self.assertIn("PrivacyAuthorizationError", err)
        create.assert_not_called()
        client_for.assert_not_called()

    def test_authorized_provider_passes_the_gate(self):
        """授权后门会放行（用假 _chat_raw_once 接住，仍然不联网）。"""
        self.activate_acme()
        self.authorize("acme")
        seen = {}

        def fake_once(client, system_prompt, user_text, temperature, max_tokens,
                      enable_thinking=True, base_url="", model="",
                      make_fallback_client=None, extra_params=None):
            seen["base_url"] = base_url
            seen["model"] = model
            seen["extra_params"] = extra_params
            return "正文", ""

        with mock.patch.object(llm_client, "create_client", return_value=object()), \
             mock.patch.object(llm_client, "_chat_raw_once", fake_once):
            content, err = llm_client._chat_raw_providers(
                "系统提示", "用户文本", 0.0, 512, extra_params={"reasoning_effort": "low"})
        self.assertEqual((content, err), ("正文", ""))
        self.assertEqual(seen["base_url"], "https://api.example.com/v1",
                         "过门用的必须是合并后的 base_url")
        self.assertEqual(seen["model"], "acme-large")
        self.assertEqual(seen["extra_params"], {"reasoning_effort": "low"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
