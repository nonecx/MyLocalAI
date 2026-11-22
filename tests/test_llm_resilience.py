# -*- coding: utf-8 -*-
"""备用链演练 + 空 content 连续计数器（P1-1 / P1-2，全程离线打桩，零网络零配额）。

对应 2026-11-22 优化方案的 P1 两项，把"已存在但从未被演练验证过"的机制钉进测试：
  - P1-1 备用链（LLM_FALLBACKS）演练：主链 5xx → 备用链接管（场景 A）、主链空正文 →
    备用链接管（场景 B，含"仅思考内容"不回落的既有行为刻画）、非法名过滤与前向去重
    （场景 C）、逐家尝试与错误汇总（A2/C2）；
  - P1-2 空 content 连续计数器：计数、清零、只记连续值、线程安全（场景 D），
    并确认 /api/health 的 llm 块暴露 empty_content_streak（场景 E）。

实测结论（演练锁定的既有行为，实现见 src/mylocalai/llm/llm_client.py）：
  - 完全空正文（无正文且无思考内容）返回错误"模型未返回正文…"，备用链会换下一家——
    空 content **会**触发回落；
  - content 为空但 reasoning_content 有内容时被当作成功（返回思考正文），**不**回落，
    但空 content 计数器仍 +1（上游确实给了空正文，LongCat 前科的可观测面）；
  - 5xx/超时类异常不经"空正文"探测点（没有响应），不计入 streak。

隔离手法与 tests/test_stability_config.py 的 TestProviderBinding 一致：
MYLOCALAI_MODELS_PATH / MYLOCALAI_MODEL_SECRETS_PATH 指向不存在的路径 +
model_registry.reset_cache()，真实 data/models.json 与密钥文件完全不参与；
上游全部用进程内替身（monkeypatch llm_client.create_client/_client_for，
照 test_cost_accounting 的"假上游"手法），隐私偏好用测试快照显式授权。

运行（worktree 根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_llm_resilience -v
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.llm import llm_cache  # noqa: E402
from mylocalai.llm import llm_client  # noqa: E402
from mylocalai.llm import model_registry  # noqa: E402


def _allow_external(*providers: str):
    """测试用偏好快照：显式授权给定提供方（生产默认 fail-closed，不读本机偏好文件）。"""
    from mylocalai.core.privacy import PrivacyPreferences
    return PrivacyPreferences({
        "schema_version": 1,
        "save_enabled": {"results": False, "raw_input": False, "external_cache": False},
        "retention_days": {"results": 90, "raw_input": 30, "external_cache": 7},
        "external_processing_enabled": True,
        "external_processing_providers": list(providers),
    })


class HttpishError(Exception):
    """带 status_code 的假上游错误（openai APIStatusError 的最小替身）。"""

    def __init__(self, status_code: int, message: str = ""):
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


class _FakeMessage:
    """假的响应 message：content 之外还能给出 reasoning_content（走 model_dump 形状）。"""

    def __init__(self, content, reasoning=""):
        self.content = content
        self._reasoning = reasoning

    def model_dump(self):
        return {"reasoning_content": self._reasoning or ""}


class _FakeCompletion:
    """假的 chat.completions：记录调用次数，可控制返回值/异常。"""

    def __init__(self, reply='{"ok": 1}', reasoning="", error=None):
        self.reply = reply
        self.reasoning = reasoning
        self.error = error
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        msg = _FakeMessage(self.reply, self.reasoning)
        return type("Resp", (), {"choices": [type("Ch", (), {"message": msg})()]})()


class _FakeClient:
    def __init__(self, **kw):
        self.completions = _FakeCompletion(**kw)
        self.chat = type("Chat", (), {"completions": self.completions})()


class TestFallbackDrill(unittest.TestCase):
    """场景 A/B/C/E 的共用夹具：隔离真实注册表与密钥，替身客户端按提供方分发。"""

    KEYS = ("LONGCAT_API_KEY", "LONGCAT_BASE_URL", "LONGCAT_MODEL",
            "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
            "LLM_PROVIDER", "LLM_FALLBACKS")

    #: 与真实注册表隔离（照 TestProviderBinding 手法）：不能只 pop——pop 之后
    # registry_path() 会回退到真实 data/models.json，用户在面板里配的提供方/密钥
    # 就会参与解析，演练前提（"只有测试显式给的提供方"）就不成立了。
    _ML_KEYS = ("MYLOCALAI_MODELS_PATH", "MYLOCALAI_MODEL_SECRETS_PATH")
    _ML_ISOLATED = {"MYLOCALAI_MODELS_PATH": "Z:/nonexistent/models.json",
                    "MYLOCALAI_MODEL_SECRETS_PATH": "Z:/nonexistent/secrets.json"}

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        self._saved_ml = {k: os.environ.get(k) for k in self._ML_KEYS}
        os.environ.update(self._ML_ISOLATED)
        llm_client.reset_cache()
        model_registry.reset_cache()
        llm_cache.clear()
        llm_client.reset_usage()
        llm_client.reset_empty_content_streak()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in self._saved_ml.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        llm_client.reset_cache()
        model_registry.reset_cache()
        llm_cache.clear()
        llm_client.reset_usage()
        llm_client.reset_empty_content_streak()

    # ---- 打桩工具：主链走 create_client（保留既有测试手法），备用链走 _client_for ----

    def _stub_chain(self, built, clients_by_provider):
        """按提供方分发替身客户端；built 记录客户端构造顺序（即链路尝试顺序）。"""
        def fake_create_client():
            built.append("primary")
            return clients_by_provider.get("primary")

        def fake_client_for(provider, max_retries=None):
            built.append(provider)
            return clients_by_provider.get(provider)

        return fake_create_client, fake_client_for

    def _patch_chain(self, built, clients_by_provider, allowed):
        fake_create, fake_for = self._stub_chain(built, clients_by_provider)
        return [
            mock.patch.object(llm_client, "create_client", side_effect=fake_create),
            mock.patch.object(llm_client, "_client_for", side_effect=fake_for),
            mock.patch("mylocalai.core.privacy.load_preferences",
                       return_value=_allow_external(*allowed)),
        ]

    # ------------------------------------------------------------------
    # 场景 A：主链 5xx → 备用链接管
    # ------------------------------------------------------------------

    def test_a1_primary_5xx_falls_back_to_backup(self):
        """主链 5xx 后按 LLM_FALLBACKS 换备用方，最终拿到备用方结果；记录如实。"""
        os.environ["LLM_PROVIDER"] = "longcat"
        os.environ["LONGCAT_API_KEY"] = "sk-longcat"
        os.environ["LLM_FALLBACKS"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-openai"
        primary = _FakeClient(error=HttpishError(500, "primary 5xx"))
        backup = _FakeClient(reply='{"verdict": "backup"}')
        built = []
        patches = self._patch_chain(built, {"primary": primary, "openai": backup},
                                    ("longcat", "openai"))
        with patches[0], patches[1], patches[2]:
            obj, err = llm_client.chat_json("sys", "演练A1")
        self.assertEqual(err, "")
        self.assertEqual(obj, {"verdict": "backup"}, "最终结果应来自备用提供方")
        self.assertEqual(built, ["primary", "openai"],
                         "主链失败后应按备用链顺序构造备用方客户端")
        self.assertEqual(primary.completions.calls, 1)
        self.assertEqual(backup.completions.calls, 1)
        # 记录如实：5xx 尝试没有真实返回，不计入真实用量；只有备用方的成功调用计 1
        self.assertEqual(llm_client.usage_snapshot()["requests"], 1)
        # 5xx 是"没有响应"，不经过空正文探测点，streak 保持 0
        self.assertEqual(llm_client.empty_content_streak(), 0)

    def test_a2_all_providers_5xx_reports_every_error(self):
        """主链与备用链都 5xx：错误按序如实汇总，不虚报成功。"""
        os.environ["LLM_PROVIDER"] = "longcat"
        os.environ["LONGCAT_API_KEY"] = "sk-longcat"
        os.environ["LLM_FALLBACKS"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-openai"
        primary = _FakeClient(error=HttpishError(500))
        backup = _FakeClient(error=HttpishError(503))
        built = []
        patches = self._patch_chain(built, {"primary": primary, "openai": backup},
                                    ("longcat", "openai"))
        with patches[0], patches[1], patches[2]:
            obj, err = llm_client.chat_json("sys", "演练A2")
        self.assertIsNone(obj)
        self.assertIn("longcat:", err)
        self.assertIn("openai:", err)
        self.assertIn("；", err, "两家的错误应合并成一条汇总错误")
        self.assertEqual(built, ["primary", "openai"], "两家都应被真实尝试")
        self.assertEqual(llm_client.usage_snapshot()["requests"], 0,
                         "全部失败（无真实返回）不计入真实用量")
        self.assertEqual(llm_client.empty_content_streak(), 0)

    # ------------------------------------------------------------------
    # 场景 B：主链空 content → 备用链（先读实现后演练，行为如实刻画）
    # ------------------------------------------------------------------

    def test_b1_primary_empty_content_falls_back_to_backup(self):
        """完全空正文（无正文且无思考内容）→ 报"模型未返回正文"→ 换备用方接管。"""
        os.environ["LLM_PROVIDER"] = "longcat"
        os.environ["LONGCAT_API_KEY"] = "sk-longcat"
        os.environ["LLM_FALLBACKS"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-openai"
        primary = _FakeClient(reply="")                       # 空 content、无思考内容
        backup = _FakeClient(reply='{"ok": "backup"}')
        built = []
        patches = self._patch_chain(built, {"primary": primary, "openai": backup},
                                    ("longcat", "openai"))
        with patches[0], patches[1], patches[2]:
            obj, err = llm_client.chat_json("sys", "演练B1")
        self.assertEqual(err, "")
        self.assertEqual(obj, {"ok": "backup"}, "空正文应触发回落，最终拿到备用方结果")
        self.assertEqual(built, ["primary", "openai"])
        # 记录如实：两次尝试都真实返回了响应（即使第一次正文为空），各计 1
        self.assertEqual(llm_client.usage_snapshot()["requests"], 2)
        # 备用方返回非空正文 → 连续计数被清零（只记连续值）
        self.assertEqual(llm_client.empty_content_streak(), 0)

    def test_b2_empty_content_counts_each_provider_attempt(self):
        """主链与备用链都返回空正文：逐家计数（每家一次真实尝试各 +1）。"""
        os.environ["LLM_PROVIDER"] = "longcat"
        os.environ["LONGCAT_API_KEY"] = "sk-longcat"
        os.environ["LLM_FALLBACKS"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-openai"
        primary = _FakeClient(reply="")
        backup = _FakeClient(reply="   ")                     # 纯空白也按空正文算
        built = []
        patches = self._patch_chain(built, {"primary": primary, "openai": backup},
                                    ("longcat", "openai"))
        with patches[0], patches[1], patches[2]:
            obj, err = llm_client.chat_json("sys", "演练B2")
        self.assertIsNone(obj)
        self.assertIn("longcat:", err)
        self.assertIn("openai:", err)
        self.assertIn("模型未返回正文", err)
        self.assertEqual(built, ["primary", "openai"])
        self.assertEqual(llm_client.empty_content_streak(), 2,
                         "连续两次空正文（主链 + 备用链各一次尝试）应计 2")

    def test_b3_reasoning_only_content_does_not_fall_back(self):
        """行为刻画：content 为空但 reasoning_content 有内容 → 当作成功，不回落。

        这是"空 content 是否触发回落"的边界：思考内容兜底救回了正文，现有机制按
        成功处理（不换备用方）；但上游确实给了空 content，streak 仍 +1（P1-2）。
        不擅自扩行为，只把现状钉住供后续决策。
        """
        os.environ["LLM_PROVIDER"] = "longcat"
        os.environ["LONGCAT_API_KEY"] = "sk-longcat"
        os.environ["LLM_FALLBACKS"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-openai"
        primary = _FakeClient(reply="", reasoning="思考内容兜底")
        backup = _FakeClient(reply='{"ok": "backup"}')
        built = []
        patches = self._patch_chain(built, {"primary": primary, "openai": backup},
                                    ("longcat", "openai"))
        with patches[0], patches[1], patches[2]:
            text, err = llm_client.chat_text("sys", "演练B3")
        self.assertEqual(err, "")
        self.assertEqual(text, "思考内容兜底")
        self.assertEqual(built, ["primary"], "思考内容兜底时不应尝试备用提供方")
        self.assertEqual(backup.completions.calls, 0)
        self.assertEqual(llm_client.empty_content_streak(), 1,
                         "上游确实返回了空 content，应 +1（尽管调用被兜底救回）")

    # ------------------------------------------------------------------
    # 场景 C：LLM_FALLBACKS 非法名过滤与前向去重
    # ------------------------------------------------------------------

    def test_c1_fallback_names_normalized_deduped_forward(self):
        """纯函数级：小写归一、去空段、按主链+备用链前向去重；未知名不在此过滤。"""
        os.environ["LLM_PROVIDER"] = "deepseek"
        os.environ["LLM_FALLBACKS"] = " DeepSeek , , no-such-provider , DEEPSEEK , openai ,,  "
        names = llm_client._provider_names()
        self.assertEqual(names, ["deepseek", "no-such-provider", "openai"],
                         "应小写归一、丢弃空段、主链+备用链前向去重（未知名留给调用期跳过）")

    def test_c2_unknown_fallback_name_skipped_chain_continues(self):
        """链路级：非法名在调用期跳过并如实记"未知提供方"，下一个合法名继续接管。"""
        os.environ["LLM_PROVIDER"] = "longcat"
        os.environ["LONGCAT_API_KEY"] = "sk-longcat"
        os.environ["LLM_FALLBACKS"] = "no-such-provider,openai"
        os.environ["OPENAI_API_KEY"] = "sk-openai"
        primary = _FakeClient(error=HttpishError(500))
        backup = _FakeClient(reply='{"ok": "after-illegal"}')
        built = []
        patches = self._patch_chain(built, {"primary": primary, "openai": backup},
                                    ("longcat", "openai"))
        with patches[0], patches[1], patches[2]:
            obj, err = llm_client.chat_json("sys", "演练C2")
        self.assertEqual(err, "")
        self.assertEqual(obj, {"ok": "after-illegal"})
        self.assertEqual(built, ["primary", "openai"],
                         "未知名不应打断备用链：跳过后下一个合法提供方照常接管")

    # ------------------------------------------------------------------
    # 场景 E：health 暴露（/api/health 的 llm.empty_content_streak）
    # ------------------------------------------------------------------

    def test_e1_health_exposes_empty_content_streak(self):
        """/api/health 的 llm 块应带 empty_content_streak（int），与计数器同源。"""
        import mylocalai.web.app as web_app
        llm_client._note_empty_content()
        self.assertEqual(llm_client.empty_content_streak(), 1)
        body = web_app.health()
        self.assertIn("llm", body)
        self.assertIn("empty_content_streak", body["llm"])
        self.assertIsInstance(body["llm"]["empty_content_streak"], int)
        self.assertEqual(body["llm"]["empty_content_streak"], 1,
                         "health 字段应直接取自 llm_client.empty_content_streak()")


class TestEmptyContentStreak(unittest.TestCase):
    """场景 D：空 content 连续计数器的计数、清零与线程安全（P1-2）。"""

    KEYS = ("LONGCAT_API_KEY", "LLM_PROVIDER", "LLM_FALLBACKS")
    _ML_KEYS = ("MYLOCALAI_MODELS_PATH", "MYLOCALAI_MODEL_SECRETS_PATH")
    _ML_ISOLATED = {"MYLOCALAI_MODELS_PATH": "Z:/nonexistent/models.json",
                    "MYLOCALAI_MODEL_SECRETS_PATH": "Z:/nonexistent/secrets.json"}

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        self._saved_ml = {k: os.environ.get(k) for k in self._ML_KEYS}
        os.environ.update(self._ML_ISOLATED)
        os.environ["LLM_PROVIDER"] = "longcat"
        os.environ["LONGCAT_API_KEY"] = "sk-longcat"
        llm_client.reset_cache()
        model_registry.reset_cache()
        llm_cache.clear()
        llm_client.reset_empty_content_streak()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in self._saved_ml.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        llm_client.reset_cache()
        model_registry.reset_cache()
        llm_cache.clear()
        llm_client.reset_empty_content_streak()

    def _with_stub(self, stub):
        """单提供方链 + 替身客户端（无备用链，聚焦计数器本身）。"""
        return [
            mock.patch.object(llm_client, "create_client", return_value=stub),
            mock.patch("mylocalai.core.privacy.load_preferences",
                       return_value=_allow_external("longcat")),
        ]

    def test_d1_streak_counts_and_resets_through_real_path(self):
        """经 chat_json/chat_text 真实链路：空正文 +1、纯空白 +1、非空清零、再空再 +1。"""
        stub = _FakeClient(reply="")
        patches = self._with_stub(stub)
        with patches[0], patches[1]:
            self.assertEqual(llm_client.empty_content_streak(), 0)

            _, err = llm_client.chat_json("sys", "演练D1-空", use_cache=False)
            self.assertIn("模型未返回正文", err)
            self.assertEqual(llm_client.empty_content_streak(), 1)

            stub.completions.reply = "   "                    # 纯空白也算空正文
            _, err = llm_client.chat_json("sys", "演练D1-空白", use_cache=False)
            self.assertIn("模型未返回正文", err)
            self.assertEqual(llm_client.empty_content_streak(), 2, "连续空正文应累加")

            stub.completions.reply = '{"ok": 1}'              # 非空 → 清零
            obj, err = llm_client.chat_json("sys", "演练D1-非空", use_cache=False)
            self.assertEqual(err, "")
            self.assertEqual(obj, {"ok": 1})
            self.assertEqual(llm_client.empty_content_streak(), 0, "非空正文应清零")

            stub.completions.reply = ""                       # 再空 → 从 0 重新 +1
            stub.completions.reasoning = "只有思考"
            text, err = llm_client.chat_text("sys", "演练D1-思考兜底")
            self.assertEqual(err, "")
            self.assertEqual(text, "只有思考")
            self.assertEqual(llm_client.empty_content_streak(), 1,
                             "清零后重新计数；思考内容兜底也算上游空 content")

    def test_d2_streak_is_thread_safe(self):
        """多线程并发 +1 不丢计数（对齐 M4 计数器的 threading.Lock 口径）。"""
        errors = []

        def bump():
            try:
                for _ in range(200):
                    llm_client._note_empty_content()
                    value = llm_client.empty_content_streak()
                    if not isinstance(value, int) or value < 0:
                        errors.append(value)
            except Exception as exc:  # noqa: BLE001  线程内异常收集后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=bump) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(llm_client.empty_content_streak(), 8 * 200,
                         "并发累加不得丢计数")
        llm_client._note_nonempty_content()
        self.assertEqual(llm_client.empty_content_streak(), 0, "清零路径同样生效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
