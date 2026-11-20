# -*- coding: utf-8 -*-
"""`mylocalai.llm.model_registry` 离线单测：契约、校验、fail-soft、密钥不落敏、外发授权。

为什么这样测：
  - 一切读写都重定向到 `tempfile.TemporaryDirectory()`（`MYLOCALAI_MODELS_PATH` /
    `MYLOCALAI_MODEL_SECRETS_PATH` / `MYLOCALAI_PRIVACY_PREFERENCES_PATH`），
    **绝不碰真实 data/**，跑完即清理；
  - 环境变量是按提供方成组读密钥的，真实 .env 可能已经带进来，所以每个用例先摘干净，
    由 `mock.patch.dict(os.environ)` 负责还原，避免"本机碰巧配了密钥"让断言时红时绿；
  - 唯一会外发的入口 `available_models()` 一律 monkeypatch `urllib.request.urlopen`：
    未授权的用例顺带断言"一个字节都没发出去"。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_model_registry -v
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.core.privacy import (  # noqa: E402
    DEFAULT_PREFERENCES,
    PrivacyAuthorizationError,
    save_preferences,
)
from mylocalai.llm import model_registry as registry  # noqa: E402

# 真实 .env 里的提供方相关变量：逐个摘掉，保证"来源 = env/stored/none"的断言只由本用例决定
_ENV_KEYS = (
    "LLM_PROVIDER", "LLM_FALLBACKS",
    "LONGCAT_API_KEY", "LONGCAT_MODEL", "LONGCAT_BASE_URL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL",
)

_SECRET = "sk-example-4be5ra91"          # 掩码应为 sk-…ra91
_BAD_JSON = "{ 这不是 JSON"


class _FakeResponse:
    """urlopen() 的假响应：只要 read() 与上下文管理器协议。"""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, _size: int | None = None) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RegistryTestCase(unittest.TestCase):
    """公共夹具：临时目录 + 三个路径环境变量 + 清缓存。"""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="model_registry_test_")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.models_path = self.dir / "models.json"
        self.secrets_path = self.dir / "model_secrets.json"
        self.prefs_path = self.dir / "privacy_preferences.json"
        env_patch = mock.patch.dict(os.environ, {
            "MYLOCALAI_MODELS_PATH": str(self.models_path),
            "MYLOCALAI_MODEL_SECRETS_PATH": str(self.secrets_path),
            "MYLOCALAI_PRIVACY_PREFERENCES_PATH": str(self.prefs_path),
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)          # stop 时整份环境快照还原（含被摘掉的键）
        for name in _ENV_KEYS:
            os.environ.pop(name, None)
        registry.reset_cache()
        self.addCleanup(registry.reset_cache)

    # ---- 夹具工具 ----
    def write_models(self, data) -> None:
        self.models_path.write_text(
            data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
            encoding="utf-8")

    def write_secrets(self, data) -> None:
        self.secrets_path.write_text(
            data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
            encoding="utf-8")

    def authorize(self, *pids: str) -> None:
        """写入"允许这些提供方外发"的隐私偏好（默认是关的 → 403 的成因）。"""
        raw = copy.deepcopy(DEFAULT_PREFERENCES)
        raw["external_processing_enabled"] = True
        raw["external_processing_providers"] = list(pids)
        save_preferences(raw, self.prefs_path)

    def create_provider(self, pid: str = "acme", **overrides) -> dict:
        payload = {
            "display_name": "示例网关",
            "base_url": "https://api.example.com/v1",
            "protocol": "openai",
            "key_env": "EXAMPLE_API_KEY",
            "enabled": True,
        }
        payload.update(overrides)
        return registry.upsert_provider(pid, payload)

    def assert_raises_field(self, field: str, func, *args, **kwargs):
        """断言抛 ModelRegistryError，且错误信息里点名了出问题的字段。"""
        with self.assertRaises(registry.ModelRegistryError) as ctx:
            func(*args, **kwargs)
        self.assertIn(field, str(ctx.exception))
        return str(ctx.exception)


class BuiltinAndFailSoftTests(RegistryTestCase):
    """没有/坏掉 data/models.json 时的行为：退回内置提供方表 + 告警 + 不覆盖坏文件。"""

    def test_missing_file_falls_back_to_builtin_providers(self):
        view = registry.view()
        self.assertEqual([p["id"] for p in view["providers"]], ["longcat", "deepseek", "openai"])
        for provider in view["providers"]:
            self.assertTrue(provider["builtin"])
        self.assertEqual(view["active"]["provider"], "longcat")
        self.assertEqual(view["active"]["model"], "LongCat-2.5-Preview")
        self.assertFalse(self.models_path.exists(), "读配置不该顺手创建文件")
        self.assertTrue(view["warnings"], "缺文件要留一条告警供页面展示")

    def test_builtin_default_catalog_is_providers_json_model(self):
        catalog = registry.default_model_views("deepseek")
        self.assertEqual([m["id"] for m in catalog], ["deepseek-chat"])
        self.assertIsNone(catalog[0]["context_window"])
        self.assertIsNone(catalog[0]["max_output_tokens"])
        self.assertEqual(catalog[0]["input_types"], ["text"])
        self.assertEqual(registry.default_model_views("acme"), [], "自定义提供方没有出厂目录")

    def test_bad_json_fails_soft_and_never_overwrites(self):
        self.write_models(_BAD_JSON)
        view = registry.view()
        self.assertEqual([p["id"] for p in view["providers"]], ["longcat", "deepseek", "openai"])
        self.assertTrue(any("读取失败" in w for w in view["warnings"]))
        self.assertEqual(self.models_path.read_text(encoding="utf-8"), _BAD_JSON)

    def test_empty_file_fails_soft_and_never_overwrites(self):
        self.models_path.write_text("", encoding="utf-8")
        view = registry.view()
        self.assertEqual(len(view["providers"]), 3)
        self.assertTrue(view["warnings"])
        self.assertEqual(self.models_path.read_text(encoding="utf-8"), "")

    def test_schema_version_mismatch_fails_soft_and_never_overwrites(self):
        payload = {
            "schema_version": 2,
            "active": {"provider": "acme", "model": "acme-x"},
            "providers": {"acme": {"display_name": "示例", "base_url": "https://api.example.com/v1",
                                   "protocol": "openai", "key_env": "", "enabled": True,
                                   "models": []}},
        }
        self.write_models(payload)
        view = registry.view()
        self.assertEqual([p["id"] for p in view["providers"]], ["longcat", "deepseek", "openai"])
        self.assertTrue(any("schema_version" in w for w in view["warnings"]))
        self.assertEqual(json.loads(self.models_path.read_text(encoding="utf-8")), payload)

    def test_one_broken_provider_is_ignored_but_the_rest_survive(self):
        """单条写坏不该把整份配置丢掉：点名告警 + 其余提供方照常可用。"""
        payload = {
            "schema_version": 1,
            "active": {"provider": "good", "model": "good-x"},
            "providers": {
                "good": {"display_name": "好的", "base_url": "https://api.good.test/v1",
                         "protocol": "openai", "key_env": "", "enabled": True,
                         "models": [{"id": "good-x", "input_types": ["text"]}]},
                "broken": {"display_name": "坏的", "base_url": "ftp://no-host",
                           "protocol": "openai", "key_env": "", "enabled": True, "models": []},
            },
        }
        self.write_models(payload)
        view = registry.view()
        ids = [p["id"] for p in view["providers"]]
        self.assertIn("good", ids)
        self.assertNotIn("broken", ids)
        self.assertTrue(any("broken" in w for w in view["warnings"]))
        self.assertEqual(view["active"]["provider"], "good")

    def test_active_provider_unknown_in_file_falls_back_with_warning(self):
        self.write_models({
            "schema_version": 1,
            "active": {"provider": "ghost", "model": None},
            "providers": {},
        })
        view = registry.view()
        self.assertEqual(view["active"]["provider"], "longcat")
        self.assertTrue(any("active.provider" in w for w in view["warnings"]))


class ViewContractTests(RegistryTestCase):
    """GET 载荷的形状（规格 §3.1）：前端与接口层都按它取字段。"""

    def test_view_exposes_frozen_contract(self):
        view = registry.view()
        for key in ("schema_version", "active", "runtime", "protocols", "input_types",
                    "reasoning_tiers", "limits", "providers", "warnings"):
            self.assertIn(key, view)
        self.assertEqual(view["schema_version"], registry.SCHEMA_VERSION)
        self.assertEqual(view["limits"]["max_providers"], 50)
        self.assertEqual(view["limits"]["max_models_per_provider"], 200)
        self.assertEqual([p["id"] for p in view["protocols"]],
                         list(registry.PROTOCOLS))
        self.assertEqual([p["runtime_supported"] for p in view["protocols"]],
                         [p in registry.RUNTIME_PROTOCOLS for p in registry.PROTOCOLS])
        self.assertEqual([t["id"] for t in view["reasoning_tiers"]],
                         list(registry.REASONING_TIERS))
        self.assertEqual([t["id"] for t in view["input_types"]], list(registry.INPUT_TYPES))
        self.assertNotIn("api_key", view["runtime"], "runtime 子集不得带明文密钥")

    def test_provider_view_fields_and_unknown_pid(self):
        self.create_provider()
        provider = registry.provider_view("acme")
        for key in ("id", "display_name", "base_url", "protocol", "protocol_runtime_supported",
                    "key_env", "api_key_set", "api_key_source", "api_key_masked", "enabled",
                    "builtin", "is_active", "models", "default_models"):
            self.assertIn(key, provider)
        self.assertFalse(provider["builtin"])
        self.assertTrue(provider["protocol_runtime_supported"])
        self.assertIsNone(registry.provider_view("nope"))

    def test_model_view_marks_active_model(self):
        self.create_provider()
        registry.upsert_model("acme", {"id": "acme-x"})
        registry.upsert_model("acme", {"id": "acme-y"})
        registry.set_active("acme", "acme-y")
        models = registry.provider_view("acme")["models"]
        self.assertEqual([(m["id"], m["is_active"]) for m in models],
                         [("acme-x", False), ("acme-y", True)])


class SecretTests(RegistryTestCase):
    """密钥：只进 model_secrets.json，GET 视图只有掩码。"""

    def test_secret_round_trip_masks_and_never_leaks_plaintext(self):
        self.create_provider()
        registry.set_secret("acme", _SECRET)
        status = registry.key_status("acme")
        self.assertEqual(status, {"set": True, "source": "stored", "masked": "sk-…ra91"})
        self.assertTrue(self.secrets_path.exists())
        self.assertEqual(json.loads(self.secrets_path.read_text(encoding="utf-8"))["secrets"],
                         {"acme": _SECRET})
        # 明文不得出现在任何视图或 models.json 里
        self.assertNotIn(_SECRET, json.dumps(registry.view(), ensure_ascii=False))
        self.assertNotIn(_SECRET, self.models_path.read_text(encoding="utf-8"))

    def test_env_var_wins_over_stored_secret(self):
        self.create_provider()
        registry.set_secret("acme", "stored-key-0000")
        with mock.patch.dict(os.environ, {"EXAMPLE_API_KEY": "env-key-1111"}):
            status = registry.key_status("acme")
        self.assertEqual(status["source"], "env")
        self.assertEqual(status["masked"], "env…1111")
        self.assertNotIn("stored", json.dumps(status))

    def test_short_key_uses_tail_only_mask(self):
        self.create_provider()
        registry.set_secret("acme", "abc123")     # 不足 8 字符 → "…+尾部"
        self.assertEqual(registry.key_status("acme")["masked"], "…c123")

    def test_empty_key_clears_stored_secret(self):
        self.create_provider()
        registry.set_secret("acme", _SECRET)
        registry.set_secret("acme", "")
        self.assertEqual(registry.key_status("acme"), {"set": False, "source": "none", "masked": ""})
        self.assertNotIn("acme", json.loads(self.secrets_path.read_text(encoding="utf-8"))["secrets"])

    def test_upsert_provider_empty_api_key_does_not_clear_existing_secret(self):
        """只改显示名不该把已有密钥抹掉（清除请走 PUT /secret 传空串）。"""
        self.create_provider()
        registry.set_secret("acme", _SECRET)
        self.create_provider(display_name="改名后", api_key="")
        self.assertEqual(registry.key_status("acme")["source"], "stored")

    def test_upsert_provider_with_api_key_writes_secret(self):
        provider = self.create_provider(api_key="inline-key-2222")
        self.assertTrue(provider["api_key_set"])
        self.assertEqual(provider["api_key_source"], "stored")
        self.assertEqual(provider["api_key_masked"], "inl…2222")

    def test_set_secret_unknown_provider_rejected(self):
        self.assert_raises_field("未知提供方", registry.set_secret, "nope", _SECRET)

    def test_corrupt_secrets_file_fails_soft_without_key_in_warning(self):
        self.create_provider()
        self.write_secrets(_BAD_JSON)
        status = registry.key_status("acme")
        self.assertEqual(status["source"], "none")
        warns = registry.warnings()
        self.assertTrue(any("密钥文件" in w for w in warns))
        self.assertNotIn(_BAD_JSON, "".join(warns))
        self.assertEqual(self.secrets_path.read_text(encoding="utf-8"), _BAD_JSON)

    def test_writes_are_atomic_and_leave_no_temp_files(self):
        self.create_provider()
        registry.set_secret("acme", _SECRET)
        registry.upsert_model("acme", {"id": "acme-x"})
        leftovers = [p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "原子写不应留下临时文件")

    def test_key_status_of_unknown_provider_is_none_source(self):
        self.assertEqual(registry.key_status("nope"), {"set": False, "source": "none", "masked": ""})


class ProviderValidationTests(RegistryTestCase):
    """规格 §2.2 的提供方校验，逐条对应用例。"""

    def test_provider_id_must_match_pattern(self):
        for bad in ("DeepSeek", "1abc", "ab-cd!", "a" * 33, ""):
            self.assert_raises_field("提供方 ID", registry.upsert_provider, bad,
                                     {"base_url": "https://api.example.com/v1"})

    def test_provider_id_in_body_must_match_path(self):
        self.assert_raises_field("provider.id", registry.upsert_provider, "acme",
                                 {"id": "other", "base_url": "https://api.example.com/v1"})

    def test_base_url_rules(self):
        self.assert_raises_field("provider.base_url", registry.upsert_provider, "acme",
                                 {"base_url": "ftp://api.example.com"})
        self.assert_raises_field("provider.base_url", registry.upsert_provider, "acme",
                                 {"base_url": "https://"})
        self.assert_raises_field("provider.base_url", registry.upsert_provider, "acme",
                                 {"base_url": "https://api.example.com/" + "a" * 600})
        self.assert_raises_field("provider.base_url", registry.upsert_provider, "acme",
                                 {"base_url": ""})
        self.create_provider(base_url="http://127.0.0.1:11434/v1")   # 合法：本地地址
        self.assertEqual(registry.provider_view("acme")["base_url"], "http://127.0.0.1:11434/v1")

    def test_protocol_must_be_known(self):
        self.assert_raises_field("provider.protocol", registry.upsert_provider, "acme",
                                 {"base_url": "https://api.example.com/v1", "protocol": "grpc"})
        provider = self.create_provider(protocol="anthropic")
        self.assertFalse(provider["protocol_runtime_supported"])

    def test_key_env_rules(self):
        self.assert_raises_field("provider.key_env", registry.upsert_provider, "acme",
                                 {"base_url": "https://api.example.com/v1", "key_env": "example_key"})
        self.create_provider(key_env="")     # 允许留空（表示不读环境变量）
        self.assertEqual(registry.provider_view("acme")["key_env"], "")

    def test_enabled_must_be_boolean(self):
        self.assert_raises_field("provider.enabled", registry.upsert_provider, "acme",
                                 {"base_url": "https://api.example.com/v1", "enabled": "yes"})

    def test_unknown_and_read_only_fields_are_rejected_by_name(self):
        self.assert_raises_field("provider.display_nam", registry.upsert_provider, "acme",
                                 {"base_url": "https://api.example.com/v1",
                                  "display_nam": "拼错的字段名"})
        self.assert_raises_field("api_key_set", registry.upsert_provider, "acme",
                                 {"base_url": "https://api.example.com/v1", "api_key_set": True})

    def test_provider_limit_enforced(self):
        # 内置的三家也占名额：先按"上限 - 已有数量"补满，再加第 51 个就该被拒
        remaining = registry.LIMITS["max_providers"] - len(registry.view()["providers"])
        for index in range(remaining):
            self.create_provider(f"p{index:02d}")
        message = self.assert_raises_field("上限", registry.upsert_provider, "overflow",
                                           {"base_url": "https://api.example.com/v1"})
        self.assertIn("50", message)


class ModelValidationTests(RegistryTestCase):
    """规格 §2.2 的模型校验。"""

    def setUp(self) -> None:
        super().setUp()
        self.create_provider()

    def test_model_id_rules(self):
        self.assert_raises_field("model.id", registry.upsert_model, "acme", {"id": ""})
        self.assert_raises_field("model.id", registry.upsert_model, "acme", {"id": " deepseek-chat "})
        self.assert_raises_field("model.id", registry.upsert_model, "acme", {"id": "m" * 201})
        self.assert_raises_field("model.id", registry.upsert_model, "acme", {"id": 123})

    def test_context_window_rules(self):
        self.assert_raises_field("model.context_window", registry.upsert_model, "acme",
                                 {"id": "m", "context_window": 0})
        self.assert_raises_field("model.context_window", registry.upsert_model, "acme",
                                 {"id": "m", "context_window": 10_000_001})
        self.assert_raises_field("model.context_window", registry.upsert_model, "acme",
                                 {"id": "m", "context_window": "65536"})
        self.assert_raises_field("model.context_window", registry.upsert_model, "acme",
                                 {"id": "m", "context_window": True})

    def test_max_output_tokens_must_not_exceed_context_window(self):
        message = self.assert_raises_field("model.max_output_tokens", registry.upsert_model, "acme",
                                           {"id": "m", "context_window": 8192,
                                            "max_output_tokens": 8193})
        self.assertIn("context_window", message)
        model = registry.upsert_model("acme", {"id": "m", "context_window": 8192,
                                               "max_output_tokens": 8192})["models"][0]
        self.assertEqual(model["max_output_tokens"], 8192)   # 相等是允许的
        self.assert_raises_field("model.max_output_tokens", registry.upsert_model, "acme",
                                 {"id": "m2", "max_output_tokens": 1_000_001})

    def test_input_types_rules(self):
        self.assert_raises_field("model.input_types", registry.upsert_model, "acme",
                                 {"id": "m", "input_types": ["audio"]})
        self.assert_raises_field("model.input_types", registry.upsert_model, "acme",
                                 {"id": "m", "input_types": []})
        self.assert_raises_field("model.input_types", registry.upsert_model, "acme",
                                 {"id": "m", "input_types": "text"})
        model = registry.upsert_model("acme", {"id": "m",
                                               "input_types": ["text", "text", "image"]})["models"][0]
        self.assertEqual(model["input_types"], ["text", "image"], "重复勾选要去重")

    def test_reasoning_tiers_rules(self):
        self.assert_raises_field("reasoning.tiers[0].tier", registry.upsert_model, "acme",
                                 {"id": "m", "reasoning": {"tiers": [{"tier": "turbo"}]}})
        self.assert_raises_field("tier 重复", registry.upsert_model, "acme",
                                 {"id": "m", "reasoning": {"tiers": [{"tier": "low"},
                                                                     {"tier": "low"}]}})
        self.assert_raises_field("reasoning.active_tier", registry.upsert_model, "acme",
                                 {"id": "m", "reasoning": {"enabled": True, "active_tier": "high",
                                                           "tiers": [{"tier": "low",
                                                                      "api_value": "low"}]}})
        self.assert_raises_field("reasoning.param", registry.upsert_model, "acme",
                                 {"id": "m", "reasoning": {"param": "2bad param"}})
        self.assert_raises_field("reasoning.tiers[0].api_value", registry.upsert_model, "acme",
                                 {"id": "m", "reasoning": {"tiers": [{"tier": "low",
                                                                      "api_value": "x" * 65}]}})

    def test_empty_tiers_force_reasoning_disabled(self):
        model = registry.upsert_model("acme", {
            "id": "m", "reasoning": {"enabled": True, "tiers": []}})["models"][0]
        self.assertFalse(model["reasoning"]["enabled"])
        self.assertEqual(model["reasoning"]["active_tier"], "off")

    def test_reasoning_round_trip_structure(self):
        model = registry.upsert_model("acme", {
            "id": "m",
            "reasoning": {"enabled": True, "param": "reasoning_effort", "active_tier": "low",
                          "tiers": [{"tier": "off", "api_value": None},
                                    {"tier": "low", "api_value": "low"}]}})["models"][0]
        self.assertEqual(model["reasoning"], {
            "enabled": True, "param": "reasoning_effort", "active_tier": "low",
            "tiers": [{"tier": "off", "api_value": None}, {"tier": "low", "api_value": "low"}]})

    def test_unknown_and_read_only_model_fields_are_rejected_by_name(self):
        self.assert_raises_field("model.contex_window", registry.upsert_model, "acme",
                                 {"id": "m", "contex_window": 8192})
        self.assert_raises_field("model.is_active", registry.upsert_model, "acme",
                                 {"id": "m", "is_active": True})

    def test_duplicate_model_id_in_payload_list_rejected(self):
        self.assert_raises_field("重复", registry.upsert_provider, "acme",
                                 {"base_url": "https://api.example.com/v1",
                                  "models": [{"id": "dup"}, {"id": "dup"}]})

    def test_upsert_model_unknown_provider_rejected(self):
        self.assert_raises_field("未知提供方", registry.upsert_model, "nope", {"id": "m"})


class MutationTests(RegistryTestCase):
    """写盘往返、内置不可删、恢复默认、目录变更后的 active 自洽。"""

    def test_provider_and_model_round_trip_after_cache_reset(self):
        self.create_provider()
        registry.upsert_model("acme", {"id": "acme-x", "context_window": 65536,
                                       "max_output_tokens": 8192, "input_types": ["text", "image"]})
        registry.set_secret("acme", _SECRET)
        registry.set_active("acme", "acme-x")

        registry.reset_cache()
        state = json.loads(self.models_path.read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], registry.SCHEMA_VERSION)
        self.assertEqual(state["active"], {"provider": "acme", "model": "acme-x"})
        provider = registry.provider_view("acme")
        self.assertEqual(provider["display_name"], "示例网关")
        self.assertEqual(provider["key_env"], "EXAMPLE_API_KEY")
        self.assertEqual(provider["models"][0]["context_window"], 65536)
        self.assertEqual(provider["models"][0]["input_types"], ["text", "image"])
        self.assertEqual(registry.active(), {"provider": "acme", "model": "acme-x"})
        self.assertEqual(registry.key_status("acme")["source"], "stored")
        self.assertEqual(registry.warnings(), [], "保存成功后不该继续报配置告警")

    def test_builtin_provider_cannot_be_deleted(self):
        self.assert_raises_field("不能删除", registry.delete_provider, "deepseek")
        self.assertIsNotNone(registry.provider_view("deepseek"))

    def test_builtin_provider_can_be_edited(self):
        provider = registry.upsert_provider("deepseek", {"display_name": "DeepSeek 官方",
                                                         "base_url": "https://api.deepseek.com/v1"})
        self.assertEqual(provider["display_name"], "DeepSeek 官方")
        self.assertTrue(provider["builtin"])

    def test_delete_unknown_provider_returns_false(self):
        self.assertFalse(registry.delete_provider("nope"))

    def test_delete_provider_removes_secret_and_resets_active(self):
        self.create_provider()
        registry.upsert_model("acme", {"id": "acme-x"})
        registry.set_secret("acme", _SECRET)
        registry.set_active("acme", "acme-x")
        self.assertTrue(registry.delete_provider("acme"))
        self.assertIsNone(registry.provider_view("acme"))
        self.assertEqual(registry.active(), {"provider": "longcat", "model": "LongCat-2.5-Preview"})
        self.assertNotIn("acme", json.loads(self.secrets_path.read_text(encoding="utf-8"))["secrets"])

    def test_restore_default_models_restores_builtin_catalog(self):
        self.create_provider()
        registry.upsert_model("acme", {"id": "acme-x"})
        self.assertTrue(registry.delete_model("deepseek", "deepseek-chat"))
        self.assertEqual(registry.provider_view("deepseek")["models"], [])
        restored = registry.restore_default_models("deepseek")
        self.assertEqual([m["id"] for m in restored], ["deepseek-chat"])
        self.assertEqual([m["id"] for m in registry.provider_view("deepseek")["models"]],
                         ["deepseek-chat"])
        self.assertEqual(registry.restore_default_models("acme"), [],
                         "自定义提供方没有出厂目录，恢复即清空")
        self.assertEqual(registry.provider_view("acme")["models"], [])

    def test_restore_default_models_unknown_provider_rejected(self):
        self.assert_raises_field("未知提供方", registry.restore_default_models, "nope")

    def test_delete_active_model_reconciles_active_model(self):
        self.create_provider()
        registry.upsert_model("acme", {"id": "acme-x"})
        registry.set_active("acme", "acme-x")
        self.assertTrue(registry.delete_model("acme", "acme-x"))
        self.assertEqual(registry.active(), {"provider": "acme", "model": None})
        self.assertFalse(registry.delete_model("acme", "acme-x"))

    def test_delete_model_unknown_provider_returns_false(self):
        self.assertFalse(registry.delete_model("nope", "m"))


class ActiveAndRuntimeTests(RegistryTestCase):
    """当前选择与运行期解析：环境变量优先，模型参数取自模型目录。"""

    def setUp(self) -> None:
        super().setUp()
        self.create_provider()
        registry.upsert_model("acme", {
            "id": "acme-x", "context_window": 128000, "max_output_tokens": 4096,
            "reasoning": {"enabled": True, "active_tier": "low",
                          "tiers": [{"tier": "off", "api_value": None},
                                    {"tier": "low", "api_value": "low"}]}})
        registry.set_secret("acme", _SECRET)
        registry.set_active("acme", "acme-x")

    def test_runtime_resolution_without_env(self):
        resolved = registry.runtime_resolution()
        self.assertEqual(resolved["provider"], "acme")
        self.assertEqual(resolved["model"], "acme-x")
        self.assertEqual(resolved["base_url"], "https://api.example.com/v1")
        self.assertEqual(resolved["api_key"], _SECRET)
        self.assertEqual(resolved["key_source"], "stored")
        self.assertEqual(resolved["protocol"], "openai")
        self.assertEqual(resolved["params"], {
            "context_window": 128000, "max_output_tokens": 4096,
            "reasoning": {"param": "reasoning_effort", "tier": "low", "api_value": "low"}})

    def test_env_var_wins_over_registry_active(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "deepseek",
                                          "DEEPSEEK_MODEL": "deepseek-reasoner"}):
            resolved = registry.runtime_resolution()
        self.assertEqual(resolved["provider"], "deepseek")
        self.assertEqual(resolved["model"], "deepseek-reasoner")
        self.assertEqual(resolved["base_url"], "https://api.deepseek.com/v1")
        self.assertIsNone(resolved["params"]["reasoning"], "内置模型未声明数组 → 不带推理参数")
        # 环境变量只影响运行期解析，不该悄悄改写文件里的当前选择
        self.assertEqual(registry.active(), {"provider": "acme", "model": "acme-x"})

    def test_runtime_env_overrides_base_url_and_key(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "deepseek",
                                          "DEEPSEEK_BASE_URL": "https://proxy.example.com/v1",
                                          "DEEPSEEK_API_KEY": "env-deepseek-3333"}):
            resolved = registry.runtime_resolution()
        self.assertEqual(resolved["base_url"], "https://proxy.example.com/v1")
        self.assertEqual(resolved["api_key"], "env-deepseek-3333")
        self.assertEqual(resolved["key_source"], "env")

    def test_runtime_resolution_has_no_reasoning_when_disabled(self):
        registry.upsert_model("acme", {"id": "plain", "context_window": 8192})
        registry.set_active("acme", "plain")
        self.assertIsNone(registry.runtime_resolution()["params"]["reasoning"])

    def test_set_active_rejects_unknown_provider(self):
        self.assert_raises_field("未知提供方", registry.set_active, "nope")

    def test_set_active_rejects_non_openai_protocol(self):
        self.create_provider("anthropic-gw", protocol="anthropic")
        message = self.assert_raises_field("运行期", registry.set_active, "anthropic-gw")
        self.assertIn("anthropic", message)

    def test_set_active_without_model_leaves_model_unset_then_resolves(self):
        registry.set_active("deepseek")
        self.assertEqual(registry.active(), {"provider": "deepseek", "model": "deepseek-chat"})
        self.assertEqual(registry.provider_view("deepseek")["is_active"], True)


class DiscoverTests(RegistryTestCase):
    """`available_models()`：先授权后外发，失败一律 error 非空不抛。"""

    def test_unauthorized_provider_raises_and_sends_nothing(self):
        self.create_provider()
        registry.set_secret("acme", _SECRET)
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(PrivacyAuthorizationError):
                registry.available_models("acme")
        urlopen.assert_not_called()

    def test_missing_key_reports_error_without_any_request(self):
        self.create_provider()
        self.authorize("acme")
        with mock.patch("urllib.request.urlopen") as urlopen:
            result = registry.available_models("acme")
        urlopen.assert_not_called()
        self.assertEqual(result["models"], [])
        self.assertEqual(result["count"], 0)
        self.assertIn("密钥", result["error"])

    def test_discover_parses_catalog_and_sends_bearer_header(self):
        self.create_provider()
        registry.set_secret("acme", _SECRET)
        self.authorize("acme")
        captured: dict = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return _FakeResponse(json.dumps({
                "object": "list",
                "data": [{"id": "acme-x", "owned_by": "acme"},
                         {"id": "acme-y", "owned_by": "acme"},
                         {"id": "acme-x", "owned_by": "acme"}],   # 重复 ID 只留一条
            }).encode("utf-8"))

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = registry.available_models("acme", timeout_s=3)
        self.assertEqual(captured["url"], "https://api.example.com/v1/models")
        self.assertEqual(captured["auth"], f"Bearer {_SECRET}")
        self.assertEqual(captured["timeout"], 3.0)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["models"],
                         [{"id": "acme-x", "owned_by": "acme"}, {"id": "acme-y", "owned_by": "acme"}])
        self.assertEqual(result["count"], 2)
        self.assertNotIn(_SECRET, json.dumps(result, ensure_ascii=False),
                         "发现结果里不得出现密钥")

    def test_discover_reports_upstream_500_without_raising(self):
        self.create_provider()
        registry.set_secret("acme", _SECRET)
        self.authorize("acme")
        error = registry.urllib.error.HTTPError("https://api.example.com/v1/models", 500,
                                                "Internal Server Error", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            result = registry.available_models("acme")
        self.assertEqual(result["models"], [])
        self.assertEqual(result["count"], 0)
        self.assertIn("HTTP 500", result["error"])

    def test_discover_reports_network_failure_without_raising(self):
        self.create_provider()
        registry.set_secret("acme", _SECRET)
        self.authorize("acme")
        error = registry.urllib.error.URLError("connection refused")
        with mock.patch("urllib.request.urlopen", side_effect=error):
            result = registry.available_models("acme")
        self.assertTrue(result["error"])
        self.assertEqual(result["count"], 0)

    def test_discover_reports_non_json_body_without_raising(self):
        self.create_provider()
        registry.set_secret("acme", _SECRET)
        self.authorize("acme")
        with mock.patch("urllib.request.urlopen",
                        lambda request, timeout=None: _FakeResponse(b"<html>404</html>")):
            result = registry.available_models("acme")
        self.assertIn("JSON", result["error"])

    def test_discover_empty_catalog_is_reported_honestly(self):
        self.create_provider()
        registry.set_secret("acme", _SECRET)
        self.authorize("acme")
        with mock.patch("urllib.request.urlopen",
                        lambda request, timeout=None: _FakeResponse(b'{"data": []}')):
            result = registry.available_models("acme")
        self.assertEqual(result["error"], "")
        self.assertEqual(result["models"], [])
        self.assertEqual(result["count"], 0)

    def test_loopback_provider_needs_no_consent_and_no_key(self):
        """本地服务（ollama 之类）既不要授权也不要密钥：沿用 core/privacy 的本地口径。"""
        self.create_provider(base_url="http://127.0.0.1:11434/v1")
        captured: dict = {}

        def fake_urlopen(request, timeout=None):
            captured["auth"] = request.get_header("Authorization")
            return _FakeResponse(b'{"data": [{"id": "llama3"}]}')

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = registry.available_models("acme")
        self.assertIsNone(captured["auth"])
        self.assertEqual([m["id"] for m in result["models"]], ["llama3"])

    def test_discover_unknown_provider_returns_error(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            result = registry.available_models("nope")
        urlopen.assert_not_called()
        self.assertIn("未知提供方", result["error"])
        self.assertEqual(result["count"], 0)


if __name__ == "__main__":
    unittest.main()
