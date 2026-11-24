# -*- coding: utf-8 -*-
"""模型管理 HTTP 接口层（web/models_api.py）离线测试：契约、状态码、密钥不落敏。

为什么这样测：
   - 一切读写都重定向到 `tempfile.TemporaryDirectory()`（MYLOCALAI_MODELS_PATH /
     MYLOCALAI_MODEL_SECRETS_PATH / MYLOCALAI_PRIVACY_PREFERENCES_PATH），
     **绝不碰真实 data/**，跑完即清理（夹具写法同 tests/test_model_registry.py）；
   - 环境变量是按提供方成组读密钥的，真实 .env 可能已经带进来，所以每个用例先摘干净，
     由 `mock.patch.dict(os.environ)` 负责还原；
   - **全程不联网**：/discover 的成功/失败两条路 monkeypatch
     `mylocalai.llm.model_registry.available_models`；未授权那条路**不 patch 它**，
     而是让真的注册表逻辑去撞隐私授权，同时把 `urllib.request.urlopen` mock 成
     "调用即失败"，从而断言"403 且一个字节都没外发"（fail-closed 的直接证据）；
     增补 §8.3 的 `/api/models/discover`（未保存提供方）沿用同一套手法；
   - `/api/models/config/open` 绝不在测试里真的弹文件管理器：monkeypatch 掉
     `models_api.os.startfile`（Windows 分支）与 `models_api.subprocess.run`（其他平台分支），
     并断言传给它们的**只有**服务端用 `registry_paths()` 解析出来的路径；
   - 不起真 uvicorn：TestClient(app) 即可（import 方式同 tests/test_website_chat.py）。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_models_api -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到 web/app.py

from mylocalai.core.privacy import (  # noqa: E402
    DEFAULT_PREFERENCES,
    save_preferences,
)
from mylocalai.llm import model_registry as registry  # noqa: E402
from mylocalai.web import models_api  # noqa: E402

# 真实 .env 里的提供方相关变量：逐个摘掉，保证"来源 = env/stored/none"的断言只由本用例决定
_ENV_KEYS = (
    "LLM_PROVIDER", "LLM_FALLBACKS",
    "LONGCAT_API_KEY", "LONGCAT_MODEL", "LONGCAT_BASE_URL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL",
    "EXAMPLE_API_KEY",
    "MYLOCALAI_ACME_API_KEY", "MYLOCALAI_ACME_MODEL", "MYLOCALAI_ACME_BASE_URL",
)

_SECRET = "sk-example-4be5ra91"          # 掩码应为 sk-…ra91
_BAD_JSON = "{ 这不是 JSON"


def _model(model_id: str, **overrides) -> dict:
    """一份合法的完整模型对象（规格 §1.1 / §3.2 的 POST .../models 请求体）。"""
    payload = {
        "id": model_id,
        "display_name": model_id,
        "context_window": 65536,
        "max_output_tokens": 8192,
        "input_types": ["text"],
        "enabled": True,
        "reasoning": {
            "enabled": False,
            "param": "reasoning_effort",
            "active_tier": "off",
            "tiers": [{"tier": "off", "api_value": None}],
        },
    }
    payload.update(overrides)
    return payload


def _provider_payload(pid: str, **overrides) -> dict:
    """一份合法的提供方请求体（POST /api/models/providers 用，含 id）。"""
    payload = {
        "id": pid,
        "display_name": "示例网关",
        "base_url": "https://api.example.com/v1",
        "protocol": "openai",
        "key_env": "EXAMPLE_API_KEY",
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def _app_route_table(app) -> set:
    """收集 (HTTP 方法, 路径) 路由表，兼容两种 FastAPI 形态。

    新版 FastAPI（0.163+）include_router 后 app.routes 里只是 _IncludedRouter，
    真正的 APIRoute 挂在它的 original_router 上；旧版是直接摊平的 APIRoute。
    路径里的转换器后缀（{model_id:path}）归一成 {model_id}，只比对路径形状。
    """
    table = set()
    stack = [app]
    seen = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        for route in getattr(node, "routes", None) or []:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path and methods:
                table.update((method, re.sub(r"\{[^/}]+(?::\w+)?\}", "{}", path))
                             for method in methods)
            nested = getattr(route, "original_router", None)
            if nested is not None:
                stack.append(nested)
    return table


class ModelsApiTestBase(unittest.TestCase):
    """公共夹具：临时三路径 + TestClient(app) + 清注册表缓存。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001  fastapi 缺失/应用导入失败时整组跳过而非报错
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.client = TestClient(webapp.app)

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="models_api_test_")
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

    def authorize(self, *pids: str) -> None:
        """写入"允许这些提供方外发"的隐私偏好（默认是关的 → 403 的成因）。"""
        raw = json.loads(json.dumps(DEFAULT_PREFERENCES))
        raw["external_processing_enabled"] = True
        raw["external_processing_providers"] = list(pids)
        save_preferences(raw, self.prefs_path)

    def create_provider(self, pid: str = "acme", **overrides) -> dict:
        """走 HTTP 建一个提供方，返回更新后的 provider view（失败即测试失败）。"""
        resp = self.client.post("/api/models/providers",
                                json=_provider_payload(pid, **overrides))
        self.assertEqual(200, resp.status_code, resp.text)
        return resp.json()

    def provider_of(self, payload: dict, pid: str) -> dict:
        """从 GET /api/models 的全量视图里挑出一个提供方。"""
        for view in payload["providers"]:
            if view["id"] == pid:
                return view
        self.fail(f"GET /api/models 里没有提供方 {pid!r}")


class GetModelsApiTest(ModelsApiTestBase):
    """GET /api/models：全量视图、密钥掩码、坏配置 fail-soft。"""

    def test_get_models_returns_full_contract_view(self):
        """顶层键与规格 §3.1 一致；内置三家在场且标 builtin。"""
        resp = self.client.get("/api/models")
        self.assertEqual(200, resp.status_code, resp.text)
        payload = resp.json()
        for key in ("schema_version", "active", "runtime", "protocols", "input_types",
                    "reasoning_tiers", "limits", "providers", "warnings"):
            self.assertIn(key, payload, f"GET /api/models 缺少顶层键 {key}")
        self.assertEqual(1, payload["schema_version"])
        # runtime 是给页面显示"现在用哪家"的安全子集：绝不带明文密钥
        self.assertNotIn("api_key", payload["runtime"])
        self.assertEqual("longcat", payload["active"]["provider"])

        providers = {view["id"]: view for view in payload["providers"]}
        for pid in ("longcat", "deepseek", "openai"):
            self.assertIn(pid, providers, f"内置提供方 {pid} 不在视图里")
            self.assertTrue(providers[pid]["builtin"], f"{pid} 应标 builtin")
            for field in ("api_key_set", "api_key_source", "api_key_masked",
                          "models", "default_models"):
                self.assertIn(field, providers[pid], f"provider view 缺少 {field}")
        self.assertEqual([{"id": "openai", "label": "OpenAI Chat Completions",
                           "runtime_supported": True},
                          {"id": "anthropic", "label": "Anthropic Messages",
                           "runtime_supported": False},
                          {"id": "gemini", "label": "Gemini generateContent",
                           "runtime_supported": False}],
                         payload["protocols"])
        self.assertEqual({"max_providers": 50, "max_models_per_provider": 200,
                          "max_context_window": 10000000,
                          "max_output_tokens": 1000000},
                         payload["limits"])

    def test_get_models_never_leaks_plaintext_key(self):
        """环境变量里有密钥时，GET 只回掩码 + 来源，绝不明文。"""
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": _SECRET}):
            resp = self.client.get("/api/models")
        self.assertEqual(200, resp.status_code, resp.text)
        deepseek = self.provider_of(resp.json(), "deepseek")
        self.assertTrue(deepseek["api_key_set"])
        self.assertEqual("env", deepseek["api_key_source"])
        self.assertIn("…", deepseek["api_key_masked"])
        self.assertTrue(deepseek["api_key_masked"].endswith(_SECRET[-4:]))
        self.assertNotIn(_SECRET, resp.text, "响应里出现了明文密钥")
        self.assertNotIn("api_key\"", resp.text, "响应里出现了名为 api_key 的字段")

    def test_broken_models_file_fails_soft(self):
        """models.json 是坏 JSON：200 + 告警 + 退回内置表（绝不 500）。"""
        self.models_path.write_text(_BAD_JSON, encoding="utf-8")
        resp = self.client.get("/api/models")
        self.assertEqual(200, resp.status_code, resp.text)
        payload = resp.json()
        self.assertTrue(payload["warnings"], "坏配置必须留告警")
        ids = {view["id"] for view in payload["providers"]}
        self.assertIn("longcat", ids, "坏配置应退回内置提供方表")

    def test_runtime_snapshot_reports_source_and_env_provider(self):
        """runtime 段带 source/env_provider（WS2）：无 env 覆盖时来源是 registry。"""
        resp = self.client.get("/api/models")
        self.assertEqual(200, resp.status_code, resp.text)
        runtime = resp.json()["runtime"]
        self.assertEqual("registry", runtime["source"], "没有 LLM_PROVIDER 时生效提供方来自注册表")
        self.assertEqual("", runtime["env_provider"], "没有 env 覆盖时该字段为空串")
        # 加法不破坏既有键（前端 buildKeyHealthCard 等按旧键取值）
        for key in ("provider", "model", "base_url", "key_source", "protocol"):
            self.assertIn(key, runtime, f"runtime 段缺少既有键 {key}")
        self.assertNotIn("api_key", runtime)

    def test_runtime_snapshot_env_provider_overrides_active(self):
        """.env 的 LLM_PROVIDER 压过面板选择：runtime.provider != active.provider 且 source=env。

        这是设置面板「模型」Tab 错配警示卡的触发条件（前端按这两个字段的差异弹卡），
        所以错配必须在载荷里显性可见，而不是让用户以为面板的选择生效了。
        """
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": " DeepSeek "}):
            resp = self.client.get("/api/models")
        self.assertEqual(200, resp.status_code, resp.text)
        payload = resp.json()
        runtime = payload["runtime"]
        self.assertEqual("env", runtime["source"])
        self.assertEqual("deepseek", runtime["env_provider"],
                         "env_provider 与 runtime_resolution 同口径：strip + lower")
        self.assertEqual("deepseek", runtime["provider"])
        self.assertEqual("longcat", payload["active"]["provider"],
                         "环境变量只影响运行期解析，不改写面板的当前选择")
        self.assertNotEqual(runtime["provider"], payload["active"]["provider"])


class ProviderWriteApiTest(ModelsApiTestBase):
    """提供方增删改 + 密钥：状态码、掩码、内置保护。"""

    def test_create_provider_with_api_key_stores_masked_only(self):
        """POST 建提供方：密钥进 secrets 文件（明文只在本地文件），响应只有掩码。"""
        resp = self.client.post("/api/models/providers",
                                json=_provider_payload("acme", api_key=_SECRET))
        self.assertEqual(200, resp.status_code, resp.text)
        view = resp.json()
        self.assertEqual("acme", view["id"])
        self.assertFalse(view["builtin"], "新建的应是自定义提供方")
        self.assertTrue(view["api_key_set"])
        self.assertEqual("stored", view["api_key_source"])
        self.assertNotIn(_SECRET, resp.text, "响应里出现了明文密钥")

        stored = json.loads(self.secrets_path.read_text(encoding="utf-8"))
        self.assertEqual(_SECRET, stored["secrets"]["acme"],
                         "密钥应落在 model_secrets.json（本地文件）里")

        full = self.client.get("/api/models").json()
        acme = self.provider_of(full, "acme")
        self.assertTrue(acme["api_key_set"])
        self.assertEqual("stored", acme["api_key_source"])
        self.assertNotIn(_SECRET, json.dumps(full, ensure_ascii=False),
                         "全量视图里出现了明文密钥")

    def test_empty_api_key_on_create_keeps_existing_secret(self):
        """POST 时 api_key 传空串 = 不改（清除必须显式走 PUT .../secret）。"""
        self.create_provider("acme", api_key=_SECRET)
        resp = self.client.post("/api/models/providers",
                                json=_provider_payload("acme", api_key=""))
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertTrue(resp.json()["api_key_set"], "空串不该把已存密钥抹掉")

    def test_put_provider_updates_without_touching_secret(self):
        """PUT 改提供方：字段生效，且不带 api_key 不该抹掉已存密钥。"""
        self.create_provider("acme", api_key=_SECRET)
        resp = self.client.put("/api/models/providers/acme", json={
            "display_name": "新名字",
            "base_url": "https://api.example.com/v2",
            "protocol": "openai",
            "key_env": "EXAMPLE_API_KEY",
            "enabled": False,
        })
        self.assertEqual(200, resp.status_code, resp.text)
        view = resp.json()
        self.assertEqual("新名字", view["display_name"])
        self.assertEqual("https://api.example.com/v2", view["base_url"])
        self.assertFalse(view["enabled"])
        self.assertTrue(view["api_key_set"], "改配置不该顺带清掉密钥")
        self.assertEqual("stored", view["api_key_source"])

    def test_put_unknown_provider_404(self):
        """PUT 只改已存在的提供方；未知 → 404（新建走 POST）。"""
        resp = self.client.put("/api/models/providers/ghost", json={
            "display_name": "x",
            "base_url": "https://api.example.com/v1",
            "protocol": "openai",
            "key_env": "",
            "enabled": True,
        })
        self.assertEqual(404, resp.status_code)
        self.assertIsInstance(resp.json()["detail"], str)

    def test_delete_builtin_provider_400_unknown_404(self):
        """内置不可删（400 说理由）；未知 → 404；自定义删除后密钥一并清掉。"""
        builtin = self.client.delete("/api/models/providers/longcat")
        self.assertEqual(400, builtin.status_code)
        self.assertIn("内置", builtin.json()["detail"])

        ghost = self.client.delete("/api/models/providers/ghost")
        self.assertEqual(404, ghost.status_code)

        self.create_provider("acme", api_key=_SECRET)
        ok = self.client.delete("/api/models/providers/acme")
        self.assertEqual(200, ok.status_code, ok.text)
        self.assertEqual({"deleted": True, "id": "acme"}, ok.json())
        full = self.client.get("/api/models").json()
        self.assertNotIn("acme", {view["id"] for view in full["providers"]},
                         "删除后不该再出现在视图里")
        stored = json.loads(self.secrets_path.read_text(encoding="utf-8"))
        self.assertNotIn("acme", stored["secrets"], "删提供方应连带清掉它的密钥")

    def test_secret_write_clear_and_missing_field(self):
        """密钥写/清；未知提供方 → 404；缺 api_key 字段 → 400（不当成清除）。"""
        self.create_provider("acme")
        written = self.client.put("/api/models/providers/acme/secret",
                                  json={"api_key": _SECRET})
        self.assertEqual(200, written.status_code, written.text)
        view = written.json()
        self.assertTrue(view["api_key_set"])
        self.assertEqual("stored", view["api_key_source"])
        self.assertNotIn(_SECRET, written.text)

        cleared = self.client.put("/api/models/providers/acme/secret",
                                  json={"api_key": ""})
        self.assertEqual(200, cleared.status_code, cleared.text)
        view = cleared.json()
        self.assertFalse(view["api_key_set"])
        self.assertEqual("none", view["api_key_source"])
        self.assertEqual("", view["api_key_masked"])

        unknown = self.client.put("/api/models/providers/ghost/secret",
                                  json={"api_key": _SECRET})
        self.assertEqual(404, unknown.status_code)

        missing = self.client.put("/api/models/providers/acme/secret", json={})
        self.assertEqual(400, missing.status_code)
        self.assertIn("api_key", missing.json()["detail"])


class ActivateApiTest(ModelsApiTestBase):
    """设为当前提供方：未知 404、非 openai 400、正常 200。"""

    def test_activate_unknown_non_openai_and_ok(self):
        unknown = self.client.post("/api/models/providers/ghost/activate", json={})
        self.assertEqual(404, unknown.status_code)

        self.create_provider("anthropic-gw", protocol="anthropic", key_env="")
        blocked = self.client.post("/api/models/providers/anthropic-gw/activate", json={})
        self.assertEqual(400, blocked.status_code)
        detail = blocked.json()["detail"]
        self.assertIn("协议", detail)
        self.assertIn("openai", detail)

        self.create_provider("acme")
        ok = self.client.post("/api/models/providers/acme/activate", json={})
        self.assertEqual(200, ok.status_code, ok.text)
        self.assertTrue(ok.json()["is_active"])
        active = self.client.get("/api/models").json()["active"]
        self.assertEqual("acme", active["provider"])

    def test_activate_with_explicit_model(self):
        """activate 带 model：该模型成为当前模型（is_active 归位）。"""
        self.create_provider("acme")
        self.client.post("/api/models/providers/acme/models", json=_model("m-1"))
        self.client.post("/api/models/providers/acme/models", json=_model("m-2"))
        resp = self.client.post("/api/models/providers/acme/activate",
                                json={"model": "m-2"})
        self.assertEqual(200, resp.status_code, resp.text)
        active = self.client.get("/api/models").json()["active"]
        self.assertEqual("acme", active["provider"])
        self.assertEqual("m-2", active["model"])


class ModelCatalogApiTest(ModelsApiTestBase):
    """单个提供方的模型目录：增/删/恢复默认 + 只读字段拒绝 + :path 转换器。"""

    def test_upsert_delete_and_restore_models(self):
        self.create_provider("acme")

        created = self.client.post("/api/models/providers/acme/models",
                                   json=_model("m-1"))
        self.assertEqual(200, created.status_code, created.text)
        models = created.json()["models"]
        self.assertEqual(["m-1"], [item["id"] for item in models])
        self.assertFalse(models[0]["is_active"])

        # 同 id 再发一次 = 覆盖，不新增
        updated = self.client.post("/api/models/providers/acme/models",
                                   json=_model("m-1", display_name="改过的名字"))
        self.assertEqual(200, updated.status_code, updated.text)
        models = updated.json()["models"]
        self.assertEqual(1, len(models))
        self.assertEqual("改过的名字", models[0]["display_name"])

        # 未知提供方 → 404
        unknown = self.client.post("/api/models/providers/ghost/models",
                                   json=_model("m-1"))
        self.assertEqual(404, unknown.status_code)

        deleted = self.client.delete("/api/models/providers/acme/models/m-1")
        self.assertEqual(200, deleted.status_code, deleted.text)
        self.assertEqual({"deleted": True, "id": "m-1"}, deleted.json())
        again = self.client.delete("/api/models/providers/acme/models/m-1")
        self.assertEqual(404, again.status_code)
        ghost = self.client.delete("/api/models/providers/ghost/models/m-1")
        self.assertEqual(404, ghost.status_code)

        # 内置提供方恢复默认目录 = providers.json 声明的那一个模型
        restored = self.client.post("/api/models/providers/deepseek/models/restore")
        self.assertEqual(200, restored.status_code, restored.text)
        deepseek = restored.json()
        self.assertEqual(["deepseek-chat"], [item["id"] for item in deepseek["models"]])
        # default_models 是"出厂目录"（不带 is_active 派生字段），只比 ID 清单
        self.assertEqual([item["id"] for item in deepseek["default_models"]],
                         [item["id"] for item in deepseek["models"]],
                         "恢复默认后当前目录应与内置默认目录一致")

        # 自定义提供方没有内置目录 → 恢复成空目录（如实为空，不拿内置目录假装）
        empty = self.client.post("/api/models/providers/acme/models/restore")
        self.assertEqual(200, empty.status_code, empty.text)
        self.assertEqual([], empty.json()["models"])

        unknown_restore = self.client.post("/api/models/providers/ghost/models/restore")
        self.assertEqual(404, unknown_restore.status_code)

    def test_model_id_with_slash_uses_path_converter(self):
        """模型 ID 含 `/`：前端会 encodeURIComponent，:path 转换器必须原样接住。"""
        self.create_provider("acme")
        created = self.client.post("/api/models/providers/acme/models",
                                   json=_model("vendor/model-x"))
        self.assertEqual(200, created.status_code, created.text)
        self.assertEqual(["vendor/model-x"],
                         [item["id"] for item in created.json()["models"]])

        deleted = self.client.delete(
            "/api/models/providers/acme/models/vendor%2Fmodel-x")
        self.assertEqual(200, deleted.status_code, deleted.text)
        self.assertEqual({"deleted": True, "id": "vendor/model-x"}, deleted.json())

    def test_read_only_and_unknown_model_fields_rejected(self):
        """GET 派生字段（is_active）与未知字段都要被点名拒绝（400 中文 detail）。"""
        self.create_provider("acme")
        read_only = self.client.post("/api/models/providers/acme/models",
                                     json=_model("m-1", is_active=True))
        self.assertEqual(400, read_only.status_code)
        self.assertIn("is_active", read_only.json()["detail"])

        unknown = self.client.post("/api/models/providers/acme/models",
                                   json=_model("m-1", context_windowt=65536))
        self.assertEqual(400, unknown.status_code)
        self.assertIn("context_windowt", unknown.json()["detail"])


class DiscoverApiTest(ModelsApiTestBase):
    """/discover：成功原样返回、上游失败 502、未授权外发 403 且零外发。"""

    def test_discover_unknown_provider_404(self):
        resp = self.client.post("/api/models/providers/ghost/discover", json={})
        self.assertEqual(404, resp.status_code)

    def test_discover_success_returns_payload_as_is(self):
        """成功：200 原样返回（不写盘——落盘由用户勾选后另行 POST .../models）。"""
        self.create_provider("acme")
        payload = {"provider": "acme", "base_url": "https://api.example.com/v1",
                   "models": [{"id": "m-1", "owned_by": "acme"}], "count": 1, "error": ""}
        with mock.patch.object(registry, "available_models",
                               return_value=payload) as patched:
            resp = self.client.post("/api/models/providers/acme/discover",
                                    json={"timeout_s": 5})
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(payload, resp.json())
        self.assertEqual("acme", patched.call_args.args[0])
        self.assertEqual(5.0, patched.call_args.kwargs.get("timeout_s"))
        # 获取与保存分开：discover 自己不该把模型写进目录
        acme_after = self.provider_of(self.client.get("/api/models").json(), "acme")
        self.assertEqual([], acme_after["models"], "discover 不该自动写盘")

    def test_discover_upstream_failure_502_with_backend_detail(self):
        """上游/网络失败 → 502，detail 用后端原文（前端原样展示）。"""
        self.create_provider("acme")
        failed = {"provider": "acme", "base_url": "https://api.example.com/v1",
                  "models": [], "count": 0,
                  "error": "上游返回 HTTP 401：https://api.example.com/v1/models。"
                           "常见原因：密钥无效/余额不足/该地址不是 OpenAI 兼容接口。"}
        with mock.patch.object(registry, "available_models", return_value=failed):
            resp = self.client.post("/api/models/providers/acme/discover", json={})
        self.assertEqual(502, resp.status_code)
        self.assertEqual(failed["error"], resp.json()["detail"])

    def test_discover_unauthorized_403_and_nothing_sent(self):
        """未授权外发 → 403，且 urlopen 一次都没被调用（fail-closed 的直接证据）。

        这里**不** patch available_models：让真的注册表逻辑走完
        "解析提供方 → 隐私授权拒绝" 的全过程，只把 urlopen 换成"调用即失败"，
        从而证明 403 发生在任何网络动作之前。
        """
        self.create_provider("acme")     # 默认偏好 = 外部处理关闭 → require_provider 拒绝
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("不该外发")) as urlopen:
            resp = self.client.post("/api/models/providers/acme/discover", json={})
        self.assertEqual(403, resp.status_code, resp.text)
        urlopen.assert_not_called()
        self.assertIn("授权", resp.json()["detail"])

    def test_discover_authorized_without_key_502(self):
        """已授权但没配密钥：注册表在发请求前就返回 error → 502（依然不外发）。"""
        self.create_provider("acme")
        self.authorize("acme")
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("没有密钥不该外发")) as urlopen:
            resp = self.client.post("/api/models/providers/acme/discover", json={})
        self.assertEqual(502, resp.status_code, resp.text)
        urlopen.assert_not_called()
        self.assertIn("密钥", resp.json()["detail"])

    def test_discover_timeout_must_be_number(self):
        """timeout_s 不是数字 → 400（而不是被静默当成默认值）。"""
        self.create_provider("acme")
        for bad in ("abc", True, [1], {"a": 1}):
            resp = self.client.post("/api/models/providers/acme/discover",
                                    json={"timeout_s": bad})
            self.assertEqual(400, resp.status_code, f"timeout_s={bad!r} 应被拒绝")
            self.assertIn("timeout_s", resp.json()["detail"])


class DiscoverAdhocApiTest(ModelsApiTestBase):
    """POST /api/models/discover（增补 §8.3）：未保存提供方也能取一次模型目录，且不写盘。"""

    def test_discover_adhoc_success_shape_and_no_write(self):
        """成功：200 原样返回注册表结果；api_key 只进调用参数，响应里绝不出现明文。"""
        payload = {"provider": "draft-gw", "base_url": "https://api.example.com/v1",
                   "models": [{"id": "m-1", "owned_by": "acme"}], "count": 1, "error": ""}
        with mock.patch.object(registry, "available_models_adhoc",
                                return_value=payload) as patched:
            resp = self.client.post("/api/models/discover", json={
                "provider": "draft-gw",
                "base_url": "https://api.example.com/v1",
                "api_key": _SECRET,
                "timeout_s": 5,
            })
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(payload, resp.json())
        self.assertEqual(("draft-gw", "https://api.example.com/v1", _SECRET),
                         patched.call_args.args)
        self.assertEqual({"timeout_s": 5.0}, patched.call_args.kwargs)
        self.assertNotIn(_SECRET, resp.text, "响应里出现了明文密钥")
        # 「获取」与「保存」分开：这条路径不该写 models.json / model_secrets.json
        self.assertFalse(self.models_path.exists(), "discover 不该写 models.json")
        self.assertFalse(self.secrets_path.exists(), "discover 不该写密钥文件")

    def test_discover_adhoc_provider_defaults_to_draft(self):
        """provider 缺省/空串都按 \"draft\" 处理（只用于隐私授权键与响应回显）。"""
        payload = {"provider": "draft", "base_url": "https://api.example.com/v1",
                   "models": [], "count": 0, "error": ""}
        for body in ({"base_url": "https://api.example.com/v1"},
                     {"provider": "", "base_url": "https://api.example.com/v1"},
                     {"provider": "   ", "base_url": "https://api.example.com/v1"}):
            with mock.patch.object(registry, "available_models_adhoc",
                                    return_value=payload) as patched:
                resp = self.client.post("/api/models/discover", json=body)
            self.assertEqual(200, resp.status_code, f"body={body!r} → {resp.text}")
            self.assertEqual("draft", patched.call_args.args[0])

    def test_discover_adhoc_requires_base_url(self):
        """base_url 缺失/不是字符串 → 400 点名（不是 422，也不是静默用默认地址）。"""
        for body in ({"provider": "draft-gw"},
                     {"base_url": None},
                     {"base_url": 123},
                     {"base_url": ["https://api.example.com/v1"]}):
            resp = self.client.post("/api/models/discover", json=body)
            self.assertEqual(400, resp.status_code, f"body={body!r} 应被拒绝")
            self.assertIn("base_url", resp.json()["detail"])

    def test_discover_adhoc_bad_base_url_400_before_anything_sent(self):
        """base_url 形状非法 → 注册表抛 ModelRegistryError → 400，且一个字节都没外发。"""
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("非法地址不该外发")) as urlopen:
            resp = self.client.post("/api/models/discover",
                                    json={"base_url": "ftp://api.example.com/v1"})
        self.assertEqual(400, resp.status_code, resp.text)
        self.assertIn("base_url", resp.json()["detail"])
        urlopen.assert_not_called()

    def test_discover_adhoc_unauthorized_403_and_nothing_sent(self):
        """未授权 → 403，detail 给出「隐私与授权」的操作指引，且 urlopen 一次没被调用。

        这里**不** patch available_models_adhoc：让真的注册表逻辑先撞隐私授权。
        """
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("未授权不该外发")) as urlopen:
            resp = self.client.post("/api/models/discover", json={
                "provider": "draft-gw",
                "base_url": "https://api.example.com/v1",
                "api_key": _SECRET,
            })
        self.assertEqual(403, resp.status_code, resp.text)
        urlopen.assert_not_called()
        detail = resp.json()["detail"]
        self.assertIn("隐私与授权", detail)
        self.assertIn("勾选", detail)
        self.assertIn("draft-gw", detail)
        self.assertNotIn(_SECRET, resp.text, "403 文案里不该出现明文密钥")

    def test_discover_adhoc_authorized_but_unreachable_502(self):
        """已授权但上游不通 → 502（error 非空），detail 是后端原文。"""
        self.authorize("draft-gw")
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("boom")) as urlopen:
            resp = self.client.post("/api/models/discover", json={
                "provider": "draft-gw",
                "base_url": "https://api.example.com/v1",
                "api_key": _SECRET,
            })
        self.assertEqual(502, resp.status_code, resp.text)
        self.assertEqual(1, urlopen.call_count, "已授权应真的发一次请求")
        self.assertIn("网络请求失败", resp.json()["detail"])

    def test_discover_adhoc_loopback_without_key_passes_authorization(self):
        """loopback 地址本就免授权：不配密钥也照常发请求（不是 403），失败如实报 502。"""
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("connection refused")) as urlopen:
            resp = self.client.post("/api/models/discover",
                                    json={"base_url": "http://127.0.0.1:11434/v1"})
        self.assertEqual(502, resp.status_code, resp.text)
        detail = resp.json()["detail"]
        self.assertNotIn("未获授权", detail, "localhost/127.0.0.1 不该要求外发授权")
        self.assertIn("网络请求失败", detail)
        self.assertEqual(1, urlopen.call_count, "loopback 无密钥时应真的发一次请求")

    def test_discover_adhoc_upstream_error_passthrough_502(self):
        """注册表返回 error（上游 4xx/5xx 等）→ 502，detail 用后端原文。"""
        failed = {"provider": "draft-gw", "base_url": "https://api.example.com/v1",
                  "models": [], "count": 0,
                  "error": "上游返回 HTTP 401：https://api.example.com/v1/models。"}
        with mock.patch.object(registry, "available_models_adhoc", return_value=failed):
            resp = self.client.post("/api/models/discover",
                                    json={"base_url": "https://api.example.com/v1"})
        self.assertEqual(502, resp.status_code)
        self.assertEqual(failed["error"], resp.json()["detail"])

    def test_discover_adhoc_api_key_type_rejected_without_echo(self):
        """api_key 不是字符串 → 400 且**不回显收到的值**（那个值可能就是密钥）。"""
        resp = self.client.post("/api/models/discover", json={
            "base_url": "https://api.example.com/v1",
            "api_key": 1234567890,
        })
        self.assertEqual(400, resp.status_code, resp.text)
        self.assertIn("api_key", resp.json()["detail"])
        self.assertNotIn("1234567890", resp.text, "错误信息里不该回显 api_key 的原始值")

    def test_discover_adhoc_timeout_must_be_number(self):
        """timeout_s 复用既有校验：不是数字 → 400 点名。"""
        for bad in ("abc", True, [1], {"a": 1}):
            resp = self.client.post("/api/models/discover", json={
                "base_url": "https://api.example.com/v1",
                "timeout_s": bad,
            })
            self.assertEqual(400, resp.status_code, f"timeout_s={bad!r} 应被拒绝")
            self.assertIn("timeout_s", resp.json()["detail"])

    def test_discover_adhoc_bad_provider_type_400(self):
        """provider 不是字符串 → 400 点名（缺省才走 draft，类型错不静默兜底）。"""
        resp = self.client.post("/api/models/discover",
                                json={"provider": 7, "base_url": "https://api.example.com/v1"})
        self.assertEqual(400, resp.status_code)
        self.assertIn("provider", resp.json()["detail"])


class ReorderModelsApiTest(ModelsApiTestBase):
    """POST /api/models/providers/{pid}/models/reorder（增补 §8.3）：目录顺序有语义。"""

    def _provider_with_models(self, pid: str = "acme", *model_ids: str) -> None:
        self.create_provider(pid)
        for model_id in model_ids:
            resp = self.client.post(f"/api/models/providers/{pid}/models",
                                    json=_model(model_id))
            self.assertEqual(200, resp.status_code, resp.text)

    def test_reorder_success_returns_provider_view_in_new_order(self):
        """order 与目录一一对应 → 200，返回的 provider view 顺序即新顺序，且已落盘。"""
        self._provider_with_models("acme", "m-1", "m-2", "m-3")
        resp = self.client.post("/api/models/providers/acme/models/reorder",
                                json={"order": ["m-3", "m-1", "m-2"]})
        self.assertEqual(200, resp.status_code, resp.text)
        view = resp.json()
        self.assertEqual("acme", view["id"])
        self.assertEqual(["m-3", "m-1", "m-2"], [item["id"] for item in view["models"]])
        # 刷新过缓存：重新 GET 也是新顺序（不是只改了返回值）
        acme = self.provider_of(self.client.get("/api/models").json(), "acme")
        self.assertEqual(["m-3", "m-1", "m-2"], [item["id"] for item in acme["models"]])

    def test_reorder_unknown_provider_404(self):
        resp = self.client.post("/api/models/providers/ghost/models/reorder",
                                json={"order": []})
        self.assertEqual(404, resp.status_code)
        self.assertIsInstance(resp.json()["detail"], str)

    def test_reorder_mismatch_400_with_registry_detail(self):
        """order 与目录不一致（少/多/重复）→ 400，detail 转述注册表原文。"""
        self._provider_with_models("acme", "m-1", "m-2")
        missing = self.client.post("/api/models/providers/acme/models/reorder",
                                   json={"order": ["m-1"]})
        self.assertEqual(400, missing.status_code)
        detail = missing.json()["detail"]
        self.assertIn("不一致", detail)
        self.assertIn("缺少", detail)
        self.assertIn("m-2", detail)

        extra = self.client.post("/api/models/providers/acme/models/reorder",
                                 json={"order": ["m-1", "m-2", "m-9"]})
        self.assertEqual(400, extra.status_code)
        self.assertIn("m-9", extra.json()["detail"])

        repeated = self.client.post("/api/models/providers/acme/models/reorder",
                                    json={"order": ["m-1", "m-1"]})
        self.assertEqual(400, repeated.status_code)
        self.assertIn("重复", repeated.json()["detail"])

        # 失败的排序不能改坏原目录
        acme = self.provider_of(self.client.get("/api/models").json(), "acme")
        self.assertEqual(["m-1", "m-2"], [item["id"] for item in acme["models"]])

    def test_reorder_order_shape_400(self):
        """order 缺失/非数组/非字符串元素 → 400 且点名 order。"""
        self._provider_with_models("acme", "m-1")
        for body in ({}, {"order": "m-1"}, {"order": [1]}, {"order": {"m-1": 0}}):
            resp = self.client.post("/api/models/providers/acme/models/reorder", json=body)
            self.assertEqual(400, resp.status_code, f"body={body!r} 应被拒绝")
            self.assertIn("order", resp.json()["detail"])


class ConfigPathApiTest(ModelsApiTestBase):
    """GET /api/models/config-path（增补 §8.3）：只回路径与是否存在，不读文件内容。"""

    def test_config_path_matches_env_and_existence_flags(self):
        resp = self.client.get("/api/models/config-path")
        self.assertEqual(200, resp.status_code, resp.text)
        body = resp.json()
        self.assertEqual({"models", "secrets", "models_exists", "secrets_exists"}, set(body))
        self.assertEqual(str(self.models_path), body["models"])
        self.assertEqual(str(self.secrets_path), body["secrets"])
        self.assertFalse(body["models_exists"], "还没保存过时不该报存在")
        self.assertFalse(body["secrets_exists"])

    def test_config_path_flips_after_first_save(self):
        """保存一次提供方/密钥后两个 exists 各自变 True（与 GET /api/models 的 paths 一致）。"""
        self.create_provider("acme", api_key=_SECRET)
        body = self.client.get("/api/models/config-path").json()
        self.assertTrue(body["models_exists"])
        self.assertTrue(body["secrets_exists"])
        full = self.client.get("/api/models").json()
        self.assertEqual({"models": body["models"], "secrets": body["secrets"]},
                         full["paths"], "config-path 与全量视图的 paths 必须同一口径")

    def test_config_path_does_not_read_file_content(self):
        """坏 JSON 也只是 exists=True：这条路由不读内容，不能被坏配置拖成 500。"""
        self.models_path.write_text(_BAD_JSON, encoding="utf-8")
        resp = self.client.get("/api/models/config-path")
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertTrue(resp.json()["models_exists"])


class ConfigOpenApiTest(ModelsApiTestBase):
    """POST /api/models/config/open（增补 §8.3）：只接受枚举，路径由服务端解析。"""

    def _patch_startfile(self):
        """把"真的打开文件"打掉，换成只记录调用参数的替身（Windows 分支）。"""
        patcher = mock.patch.object(models_api.os, "startfile", create=True)
        startfile = patcher.start()
        self.addCleanup(patcher.stop)
        return startfile

    def test_config_open_rejects_unknown_target(self):
        """target 不在枚举里（含缺失/非字符串）→ 400 点名，且不需要任何文件存在。"""
        for body in ({}, {"target": "passwd"}, {"target": 123},
                     {"target": ["models"]}, {"target": None}):
            resp = self.client.post("/api/models/config/open", json=body)
            self.assertEqual(400, resp.status_code, f"body={body!r} 应被拒绝")
            self.assertIn("target", resp.json()["detail"])

    def test_config_open_missing_file_404(self):
        """目标文件不存在 → 404，并说明"保存一次配置后才会生成"。"""
        self._patch_startfile()          # 就算真去调也不该被调到
        resp = self.client.post("/api/models/config/open", json={"target": "models"})
        self.assertEqual(404, resp.status_code, resp.text)
        detail = resp.json()["detail"]
        self.assertIn("不存在", detail)
        self.assertIn("保存一次配置后才会生成", detail)

    def test_config_open_models_uses_server_resolved_path_only(self):
        """打开 models.json：只把服务端解析的路径交给系统，客户端传的 path 被彻底忽略。"""
        self.create_provider("acme")
        startfile = self._patch_startfile()
        resp = self.client.post("/api/models/config/open", json={
            "target": "models",
            "path": "C:\\Windows\\System32\\calc.exe",     # 恶意/误传的路径，必须不起作用
        })
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual({"opened": True, "target": "models",
                          "path": str(self.models_path)}, resp.json())
        startfile.assert_called_once_with(str(self.models_path))
        self.assertNotIn("calc.exe", json.dumps(resp.json(), ensure_ascii=False))

    def test_config_open_secrets_uses_secrets_path(self):
        """打开密钥文件：同样是服务端解析出来的 secrets 路径。"""
        self.client.put("/api/models/providers/deepseek/secret",
                        json={"api_key": _SECRET})
        self.assertTrue(self.secrets_path.exists())
        startfile = self._patch_startfile()
        resp = self.client.post("/api/models/config/open", json={"target": "secrets"})
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(str(self.secrets_path), resp.json()["path"])
        startfile.assert_called_once_with(str(self.secrets_path))
        self.assertNotIn(_SECRET, resp.text, "打开配置文件不该回显密钥")

    def test_config_open_system_failure_500_with_reason(self):
        """系统无法打开 → 500，中文 detail 带路径与失败原因。"""
        self.create_provider("acme")
        with mock.patch.object(models_api.os, "startfile", create=True,
                               side_effect=OSError("没有关联的程序")) as startfile:
            resp = self.client.post("/api/models/config/open", json={"target": "models"})
        self.assertEqual(500, resp.status_code, resp.text)
        startfile.assert_called_once_with(str(self.models_path))
        detail = resp.json()["detail"]
        self.assertIn(str(self.models_path), detail)
        self.assertIn("没有关联的程序", detail)

    def test_config_open_posix_falls_back_to_xdg_open_then_open(self):
        """非 Windows：先 xdg-open，缺命令时退到 open；都失败 → 500。"""
        self.create_provider("acme")
        ok = subprocess.CompletedProcess(args=[], returncode=0)
        with mock.patch.object(models_api, "_IS_WINDOWS", False), \
                mock.patch.object(models_api.subprocess, "run", return_value=ok) as run:
            resp = self.client.post("/api/models/config/open", json={"target": "models"})
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(["xdg-open", str(self.models_path)], run.call_args.args[0])

        with mock.patch.object(models_api, "_IS_WINDOWS", False), \
                mock.patch.object(models_api.subprocess, "run",
                                  side_effect=[FileNotFoundError("没有 xdg-open"), ok]) as run:
            resp = self.client.post("/api/models/config/open", json={"target": "models"})
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual("open", run.call_args.args[0][0])

        with mock.patch.object(models_api, "_IS_WINDOWS", False), \
                mock.patch.object(models_api.subprocess, "run",
                                  side_effect=[FileNotFoundError("没有 xdg-open"),
                                               FileNotFoundError("没有 open")]):
            resp = self.client.post("/api/models/config/open", json={"target": "models"})
        self.assertEqual(500, resp.status_code, resp.text)
        self.assertIn("xdg-open", resp.json()["detail"])


class PayloadValidationApiTest(ModelsApiTestBase):
    """结构/取值非法 → 400，detail 是中文原文且点名字段。"""

    def test_create_provider_rejects_bad_payloads(self):
        base = _provider_payload("acme")
        unknown = self.client.post("/api/models/providers",
                                   json=dict(base, foo=1))
        self.assertEqual(400, unknown.status_code)
        detail = unknown.json()["detail"]
        self.assertIsInstance(detail, str, "detail 必须是字符串（前端原样展示）")
        self.assertIn("foo", detail)

        missing_url = dict(base)
        missing_url.pop("base_url")
        no_url = self.client.post("/api/models/providers", json=missing_url)
        self.assertEqual(400, no_url.status_code)
        self.assertIn("base_url", no_url.json()["detail"])

        bad_protocol = self.client.post("/api/models/providers",
                                        json=dict(base, protocol="grpc"))
        self.assertEqual(400, bad_protocol.status_code)
        self.assertIn("protocol", bad_protocol.json()["detail"])

        bad_id = self.client.post("/api/models/providers",
                                  json=_provider_payload("Bad-ID"))
        self.assertEqual(400, bad_id.status_code)
        self.assertIn("提供方 ID", bad_id.json()["detail"])

    def test_create_provider_requires_id_in_body(self):
        """POST 的提供方 ID 只能来自 body；缺 id → 400 点名。"""
        payload = _provider_payload("acme")
        payload.pop("id")
        resp = self.client.post("/api/models/providers", json=payload)
        self.assertEqual(400, resp.status_code)
        # 注册表的中文原文点名"提供方 ID"（前端原样展示，所以别在这里改写文案）
        self.assertIn("ID", resp.json()["detail"])


class PrivacyPreferencesApiTest(ModelsApiTestBase):
    """PUT /api/privacy/preferences（设置面板「隐私与授权」保存）：成功视图 + 400 校验。

    覆盖 HTTP 层契约（web/privacy_api.py）；校验规则本体在 core/privacy.py 的
    _validated。夹具沿用 ModelsApiTestBase：偏好文件重定向到临时目录，
    绝不碰真实 data/。
    """

    def _payload(self, **overrides) -> dict:
        raw = json.loads(json.dumps(DEFAULT_PREFERENCES))
        raw.update(overrides)
        return raw

    def test_put_preferences_saves_and_returns_view_with_warning(self):
        """保存成功 → 200，返回视图含 warning 字段；GET 读回磁盘现状；llm 缓存被重置。"""
        from mylocalai.web import privacy_api
        payload = self._payload(external_processing_enabled=True,
                                external_processing_providers=["acme", "deepseek"])
        with mock.patch.object(privacy_api.llm_client, "reset_cache") as reset:
            resp = self.client.put("/api/privacy/preferences", json=payload)
        self.assertEqual(200, resp.status_code, resp.text)
        reset.assert_called_once()          # 保存后自动 reset_cache：面板"保存即生效"的根据
        view = resp.json()
        for key in ("schema_version", "save_enabled", "retention_days",
                    "external_processing_enabled", "external_processing_providers", "warning"):
            self.assertIn(key, view, f"PUT /api/privacy/preferences 响应缺少 {key}")
        self.assertTrue(view["external_processing_enabled"])
        self.assertEqual(["acme", "deepseek"], view["external_processing_providers"])
        self.assertEqual("", view["warning"], "写盘正常的保存成功路径 warning 应为空")

        # GET 读回的是磁盘现状（同一份临时文件里的偏好）
        got = self.client.get("/api/privacy/preferences")
        self.assertEqual(200, got.status_code, got.text)
        self.assertEqual(view["external_processing_providers"],
                         got.json()["external_processing_providers"])

    def test_put_preferences_rejects_providers_while_external_disabled(self):
        """总开关关闭 + 提供方非空 → 400（fail-closed），且不写盘。"""
        payload = self._payload(external_processing_enabled=False,
                                external_processing_providers=["acme"])
        resp = self.client.put("/api/privacy/preferences", json=payload)
        self.assertEqual(400, resp.status_code, resp.text)
        self.assertIn("external processing is disabled", resp.json()["detail"])
        self.assertFalse(self.prefs_path.exists(), "校验失败的请求不该写盘")


class RouteRegistrationTest(ModelsApiTestBase):
    """路由表自洽：规格 §3.2 + 增补 §8.3 的端点真的注册进了 app（含方法）。"""

    def test_model_routes_registered_in_app(self):
        registered = _app_route_table(self.webapp.app)
        expected = {
            ("GET", "/api/models"),
            ("POST", "/api/models/providers"),
            ("PUT", "/api/models/providers/{}"),
            ("DELETE", "/api/models/providers/{}"),
            ("PUT", "/api/models/providers/{}/secret"),
            ("POST", "/api/models/providers/{}/activate"),
            ("POST", "/api/models/providers/{}/discover"),
            ("POST", "/api/models/providers/{}/models"),
            ("DELETE", "/api/models/providers/{}/models/{}"),
            ("POST", "/api/models/providers/{}/models/restore"),
            # 增补 §8.3：未保存提供方取模型 / 目录排序 / 配置文件定位与打开
            ("POST", "/api/models/discover"),
            ("POST", "/api/models/providers/{}/models/reorder"),
            ("GET", "/api/models/config-path"),
            ("POST", "/api/models/config/open"),
        }
        self.assertEqual(set(), expected - registered,
                         f"有端点没注册进 app：{sorted(expected - registered)}")
        # GET 必须是 /api/models 而不是 /api/models/：前端 SETTINGS_API.models 无尾斜杠
        self.assertIn(("GET", "/api/models"), registered)

    def test_health_reports_models_warnings_without_breaking_fields(self):
        """/api/health 新增 models.warnings 计数，既有字段一个没少。"""
        resp = self.client.get("/api/health")
        self.assertEqual(200, resp.status_code, resp.text)
        body = resp.json()
        for key in ("status", "time", "history_count", "llm", "llm_cache", "models"):
            self.assertIn(key, body, f"/api/health 缺少字段 {key}")
        self.assertIsInstance(body["models"]["warnings"], int)
        # WS2：llm.error_kind 与调用链同序（隐私授权是第一道闸）。本夹具没写隐私偏好
        # （外部处理默认关闭，fail-closed），所以第一道闸就卡住，类别是 privacy，
        # 而不是排在它后面的 key——"诊断与真实失败原因一致"正是这个字段的目的。
        self.assertIn("error_kind", body["llm"], "/api/health 的 llm 段缺少 error_kind")
        self.assertFalse(body["llm"]["usable"])
        self.assertEqual("privacy", body["llm"]["error_kind"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
