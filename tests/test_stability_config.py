# -*- coding: utf-8 -*-
"""本轮修复的验收测试（离线，不调用真实 API）。

对应 2026-10-14 检验报告的 14 项优化方案，逐条给出可运行的验收断言：
  - 泛用性 1/2：判定口径与信源名单外置可配置；多提供方绑定 + 启动自检 + 备用链路；可选依赖拆分
  - 泛用性 4：不再写死项目绝对路径（换机/换目录即断）
  - 稳定性 5/6：同输入结论可复现（情绪分分桶 + LLM 调用结果复用）
  - 抗压性 9/10/11：请求级时限、并发上限、超时线程可取消
  - 功能性 12/13：展示口径与 README 一致、sources 文档与实现一致

运行：  cd <项目根> && ./.venv/Scripts/python.exe -m unittest tests.test_stability_config -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mylocalai.classic.judge as core_judge# noqa: E402
import mylocalai.classic.language as LM# noqa: E402
import mylocalai.classic.logic as LG# noqa: E402
import mylocalai.retrieval.sources as SRC# noqa: E402
import mylocalai.rumor.verdict as V# noqa: E402
from mylocalai.config import rules
from mylocalai.rumor.state import AnalysisState  # noqa: E402
from mylocalai.llm import llm_cache
from mylocalai.llm import llm_client


def _write_json(data) -> str:
    fd, path = tempfile.mkstemp(suffix=".json", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return path


def _allow_external(*providers: str):
    """测试用偏好快照：显式授权给定提供方。

    生产默认是 fail-closed（外发授权默认关闭），所以夹具不能依赖本机
    data/privacy_preferences.json；要真正走到上游替身的用例必须自己授权。
    """
    from mylocalai.core.privacy import PrivacyPreferences
    return PrivacyPreferences({
        "schema_version": 1,
        "save_enabled": {"results": False, "raw_input": False, "external_cache": False},
        "retention_days": {"results": 90, "raw_input": 30, "external_cache": 7},
        "external_processing_enabled": True,
        "external_processing_providers": list(providers),
    })


class FakeCompletion:
    """假的 chat.completions：记录调用次数，可控制返回值/异常。"""

    def __init__(self, reply='{"ok": 1}', error=None):
        self.reply = reply
        self.error = error
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        msg = type("Msg", (), {"content": self.reply})()
        return type("Resp", (), {"choices": [type("Ch", (), {"message": msg})()]})()


class FakeClient:
    def __init__(self, **kw):
        self.completions = FakeCompletion(**kw)
        self.chat = type("Chat", (), {"completions": self.completions})()


class TestRulesAreConfigurable(unittest.TestCase):
    """发现 1：判定口径不再写死在 .py 里，改配置即可换标准。"""

    def setUp(self):
        llm_client.reset_cache()
        llm_cache.clear()

    def tearDown(self):
        os.environ.pop("RUMOR_RULES_EXTRA_PATH", None)
        os.environ.pop("RUMOR_SOURCES_EXTRA_PATH", None)
        llm_client.reset_cache()

    def test_default_rules_file_is_loaded(self):
        data = rules.load_rules()
        self.assertEqual(data["thresholds"]["high"], 70)
        self.assertEqual(data["no_claim_risk_cap"], 24)
        self.assertEqual(data["evidence_lift"]["multi_source_risk"], 78)
        self.assertFalse(rules.load_warnings(), f"默认配置不应有加载告警：{rules.load_warnings()}")

    def test_overriding_thresholds_changes_the_verdict(self):
        """把高风险档从 70 改到 50，同一份输入立刻换档——不需要改 Python。"""
        self.assertGreaterEqual(V.assess(
            {"marker_points": 40, "credibility_points": 0}, {"emotion_score": 5},
            {"aggression_level": "中"}, {"logic_relevance": "中"}, {"has_factual_claim": True},
        )["risk_score"], 45)
        path = _write_json({"thresholds": {"high": 50, "suspect": 30, "doubt": 10}})
        self.addCleanup(os.remove, path)
        os.environ["RUMOR_RULES_EXTRA_PATH"] = path
        self.assertEqual(rules.load_rules()["thresholds"]["high"], 50)
        import importlib
        reloaded = importlib.reload(V)
        try:
            self.assertEqual(reloaded.THRESHOLD_HIGH, 50)
            self.assertEqual(reloaded.THRESHOLD_SUSPECT, 30)
            self.assertEqual(reloaded._label_for(50), reloaded.LABEL_HIGH)
            self.assertEqual(reloaded._label_for(30), reloaded.LABEL_SUSPECT)
        finally:
            importlib.reload(reloaded)

    def test_broken_config_does_not_crash(self):
        """坏配置（不是 JSON / 根节点不是对象）只留告警，判定链路照常可用。"""
        path = _write_json(["not", "a", "dict"])
        self.addCleanup(os.remove, path)
        os.environ["RUMOR_RULES_EXTRA_PATH"] = path
        data = rules.load_rules()
        self.assertEqual(data["thresholds"]["high"], 70, "坏增量配置应被忽略，保留默认")
        self.assertTrue(any("增量配置" in w for w in rules.load_warnings()))

    def test_missing_default_file_falls_back_with_warning(self):
        os.environ["RUMOR_RULES_PATH"] = str(ROOT / "src" / "mylocalai" / "config" / "no_such_file.json")
        self.addCleanup(os.environ.pop, "RUMOR_RULES_PATH", None)
        data = rules.load_rules()
        self.assertEqual(data["thresholds"]["suspect"], 45)
        self.assertTrue(any("未找到" in w for w in rules.load_warnings()))

    def test_sources_registry_is_external_and_extendable(self):
        self.assertEqual(SRC.classify_domain("https://www.gov.cn/a"), SRC.TIER_OFFICIAL)
        path = _write_json({"tiers": {"官方权威": ["example-agency.int"]},
                            "official_sites": {"example-agency.int": "示例机构"}})
        self.addCleanup(os.remove, path)
        os.environ["RUMOR_SOURCES_EXTRA_PATH"] = path
        import importlib
        reloaded = importlib.reload(SRC)
        try:
            self.assertEqual(reloaded.classify_domain("https://news.example-agency.int/a"),
                             reloaded.TIER_OFFICIAL, "增量名单里的域名应被识别")
            self.assertEqual(reloaded.source_kind("https://www.example-agency.int/a"), "官方（示例机构）")
            self.assertEqual(reloaded.classify_domain("https://www.gov.cn/a"), reloaded.TIER_OFFICIAL,
                             "增量不应破坏原有名单")
        finally:
            importlib.reload(reloaded)


class TestSourceKindContract(unittest.TestCase):
    """发现 12/13：文档与实现一致；名单外域名不被当成官方。"""

    def test_unknown_kind_is_explicit_and_not_official(self):
        self.assertEqual(SRC.source_kind("https://example.com/x"), SRC.UNKNOWN_KIND)
        self.assertNotIn("官方", SRC.UNKNOWN_KIND)
        self.assertEqual(SRC.source_kind(""), SRC.UNKNOWN_KIND, "空主机也走同一个契约值")

    def test_spoofed_domains_never_look_official(self):
        for url in ("https://www.xinhuanet.com.evil.com/a", "https://cctv.com.evil.com/a",
                    "https://www.gov.cn.evil.com/a", "http://xinhuanet.com@evil.com/a"):
            kind = SRC.source_kind(url)
            tier = SRC.classify_domain(url)
            self.assertNotIn("官方", kind, url)
            self.assertNotIn(tier, SRC.RELIABLE_TIERS, url)

    def test_english_mainstream_sources_now_covered(self):
        """发现 1 的复核意见：名单该补上 bbc.co.uk / nytimes.com 等主流英文源。"""
        for url in ("https://www.bbc.co.uk/news/x", "https://www.nytimes.com/2026/x.html"):
            self.assertEqual(SRC.classify_domain(url), SRC.TIER_MEDIA, url)

    def test_module_docstring_matches_implementation(self):
        doc = SRC.source_kind.__doc__ or ""
        self.assertIn("其他", doc, "文档应说明第三类返回值，别只说两分")


class TestStabilityGuards(unittest.TestCase):
    """发现 4/10：同输入结论不再漂移。"""

    def _verdict(self, score):
        return V.assess({"marker_points": 20, "credibility_points": 0},
                        {"emotion_score": score}, {"aggression_level": "中"},
                        {"logic_relevance": "弱"}, {"has_factual_claim": True, "unsourced_unverifiable": 0})

    def test_emotion_bucket_collapses_the_observed_drift(self):
        """检验报告实测到的抖动是 emotion_score 8.0↔9.0；分桶后两者必须同分同档。"""
        a, b = self._verdict(8.0), self._verdict(9.0)
        self.assertEqual(a["risk_score"], b["risk_score"], "8 与 9 分应归并到同一档，不再翻档")
        self.assertEqual(a["label"], b["label"])
        self.assertEqual(V.bucket_score(8.0), V.bucket_score(9.0))
        self.assertEqual(V.bucket_score(7.4), V.bucket_score(7.0), "同一桶内的细碎抖动要归并掉")

    def test_bucketing_is_limited_to_the_score_not_the_rating(self):
        """分桶不掩盖跨档：语言模块的"7-10 分=狂热煽动"档必须仍然分得开。"""
        self.assertNotEqual(V.bucket_score(7.0), V.bucket_score(5.0))
        self.assertLess(V.bucket_score(5.0), V.bucket_score(7.0))

    def test_threshold_drift_no_longer_crosses_45(self):
        """报告里 7.0→44（存疑）与 8.0→46（疑似）正好跨 45；分桶后必须同档。"""
        self.assertEqual(self._verdict(7.0)["label"], self._verdict(8.0)["label"])

    def test_bucket_can_be_disabled_by_config(self):
        path = _write_json({"stability": {"emotion_score_bucket": 1}})
        self.addCleanup(os.remove, path)
        os.environ["RUMOR_RULES_EXTRA_PATH"] = path
        import importlib
        reloaded = importlib.reload(V)
        try:
            self.assertEqual(reloaded.bucket_score(7.4), 7.4, "bucket=1 表示不归并")
            self.assertEqual(reloaded.bucket_score(8.6), 8.6)
        finally:
            os.environ.pop("RUMOR_RULES_EXTRA_PATH", None)
            importlib.reload(reloaded)

    def test_speech_tier_still_uses_raw_score(self):
        """分桶只影响"分数"，不影响与 core_judge 逐条等价的言论分层。"""
        emo = {"emotion_score": 7.0}
        self.assertEqual(V.speech_tier(emo, {"aggression_level": "高"}, {"logic_relevance": "弱"}),
                         "高度疑似水军/煽动性言论")


class TestLlmReplay(unittest.TestCase):
    """发现 4/10 的根治项：同样的请求在进程内只真正调用一次。"""

    #: 本组要隔离的本地状态路径（用户在设置面板的改动态不应影响本组结论）
    PATHS = ("MYLOCALAI_MODELS_PATH", "MYLOCALAI_MODEL_SECRETS_PATH",
             "MYLOCALAI_PRIVACY_PREFERENCES_PATH")

    def setUp(self):
        # 与真实 models.json / model_secrets.json / privacy_preferences.json 隔离：
        # 临时空注册表回到"内置默认提供方"基线，临时偏好写"授权全开"（本组全部
        # mock create_client，不真外发；授权闸只需放行即可）。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self._saved_paths = {k: os.environ.get(k) for k in self.PATHS}
        os.environ["MYLOCALAI_MODELS_PATH"] = str(base / "models.json")
        os.environ["MYLOCALAI_MODEL_SECRETS_PATH"] = str(base / "secrets.json")
        os.environ["MYLOCALAI_PRIVACY_PREFERENCES_PATH"] = str(base / "privacy.json")
        (base / "privacy.json").write_text(json.dumps({
            "schema_version": 1,
            "save_enabled": {"results": True, "raw_input": True, "external_cache": True},
            "retention_days": {"results": 90, "raw_input": 30, "external_cache": 7},
            "external_processing_enabled": True,
            "external_processing_providers": ["longcat", "deepseek", "openai",
                                              "public-search", "public-web"],
        }, ensure_ascii=False), encoding="utf-8")
        llm_cache.clear()
        llm_client.reset_cache()

    def tearDown(self):
        for k, v in self._saved_paths.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        llm_cache.clear()
        llm_client.reset_cache()
        os.environ.pop("LLM_CACHE_MAX", None)

    def test_same_prompt_hits_the_provider_once(self):
        fake = FakeClient(reply='{"label": "x"}')
        with mock.patch.object(llm_client, "create_client", return_value=fake):
            first, err1 = llm_client.chat_json("sys", "同一段文本")
            second, err2 = llm_client.chat_json("sys", "同一段文本")
        self.assertEqual((err1, err2), ("", ""))
        self.assertEqual(first, second)
        self.assertEqual(fake.completions.calls, 1, "重复的相同请求应复用结果，不再打上游")

    def test_different_prompt_does_not_reuse(self):
        fake = FakeClient()
        with mock.patch.object(llm_client, "create_client", return_value=fake):
            llm_client.chat_json("sys", "文本 A")
            llm_client.chat_json("sys", "文本 B")
        self.assertEqual(fake.completions.calls, 2)

    def test_failures_are_not_cached(self):
        failing = FakeClient(error=ConnectionError("down"))
        with mock.patch.object(llm_client, "create_client", return_value=failing):
            _, err1 = llm_client.chat_json("sys", "文本")
            calls_after_first = failing.completions.calls
            _, err2 = llm_client.chat_json("sys", "文本")
        self.assertIn("ConnectionError", err1)
        self.assertIn("ConnectionError", err2)
        self.assertGreater(failing.completions.calls, calls_after_first,
                           "失败不该被缓存，第二次仍应真正打到上游")
        self.assertEqual(llm_cache.stats()["entries"], 0, "缓存里不该留下失败结果")

    def test_cache_can_be_disabled(self):
        os.environ["LLM_CACHE_MAX"] = "0"
        fake = FakeClient()
        with mock.patch.object(llm_client, "create_client", return_value=fake):
            llm_client.chat_json("sys", "文本")
            llm_client.chat_json("sys", "文本")
        self.assertEqual(fake.completions.calls, 2)
        self.assertEqual(llm_cache.stats()["entries"], 0)

    def test_concurrent_identical_calls_are_single_flight(self):
        fake = FakeClient()
        original = fake.completions.create

        def slow(**kwargs):
            time.sleep(0.2)
            return original(**kwargs)

        fake.completions.create = slow
        results = []

        def run():
            results.append(llm_client.chat_json("sys", "并发文本"))

        with mock.patch.object(llm_client, "create_client", return_value=fake):
            threads = [threading.Thread(target=run) for _ in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(len(results), 4)
        self.assertEqual(fake.completions.calls, 1, "并发相同请求应只算一次（single-flight）")

    def test_judge_level_replay_keeps_conclusions_identical(self):
        """端到端验收：同一文本连跑多次，四档结论必须完全一致。"""
        emo = {"emotion_tendency": "情绪化宣泄", "emotion_score": 8.0, "keywords": []}
        atk = {"aggression_level": "高", "attack_target": "针对事件", "attack_type": "煽动对立"}
        lg = {"logic_relevance": "弱", "logic_fallacy": "诉诸情绪", "argument": "a", "conclusion": "c"}
        with mock.patch("mylocalai.classic.judge.generate_report", lambda *a, **k: "R"):
            out = {core_judge.judge("同一段文本", emotion=emo, attack=atk, logic=lg)["conclusion"]
                   for _ in range(10)}
        self.assertEqual(len(out), 1)


class TestRequestBudget(unittest.TestCase):
    """发现 7/10：请求级时限，预算用尽即快速失败，不再一轮轮等满超时。"""

    def setUp(self):
        llm_cache.clear()
        llm_client.reset_cache()

    def tearDown(self):
        llm_cache.clear()
        llm_client.reset_cache()
        os.environ.pop("JUDGE_DEADLINE_S", None)

    def test_exhausted_budget_fails_fast_without_calling(self):
        fake = FakeClient()
        with mock.patch.object(llm_client, "create_client", return_value=fake):
            with llm_cache.deadline_scope(0.05):
                time.sleep(0.1)
                _, err = llm_client.chat_json("sys", "文本")
        self.assertIn("时限预算", err)
        self.assertEqual(fake.completions.calls, 0, "预算用尽后不应再发起调用")

    def test_attempt_timeout_shrinks_to_remaining_budget(self):
        with llm_cache.deadline_scope(5):
            self.assertLessEqual(llm_cache.attempt_timeout(30), 5)
        self.assertEqual(llm_cache.attempt_timeout(30), 30, "无预算时不改变默认超时")

    def test_nested_scope_takes_the_tighter_budget(self):
        with llm_cache.deadline_scope(100):
            with llm_cache.deadline_scope(3):
                self.assertLessEqual(llm_cache.remaining_seconds(), 3)
            self.assertGreater(llm_cache.remaining_seconds(), 3, "退出内层后恢复外层预算")

    def test_judge_default_budget_is_configurable(self):
        os.environ["JUDGE_DEADLINE_S"] = "7.5"
        self.assertEqual(core_judge.judge_deadline_s(), 7.5)
        os.environ["JUDGE_DEADLINE_S"] = "garbage"
        self.assertEqual(core_judge.judge_deadline_s(), core_judge.DEFAULT_JUDGE_DEADLINE_S)

    def test_judge_degrades_within_budget_when_upstream_is_dead(self):
        """上游不可达：judge() 必须在预算内返回（降级），而不是挂几分钟。"""
        def dead(**kwargs):
            raise ConnectionError("unreachable")

        fake = FakeClient()
        fake.completions.create = dead
        t0 = time.perf_counter()
        with mock.patch.object(llm_client, "create_client", return_value=fake):
            with mock.patch("mylocalai.classic.judge.generate_report", lambda *a, **k: "R"):
                out = core_judge.judge("一段文本", deadline_s=3.0)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 30, f"应在预算内降级返回，实际 {elapsed:.1f}s")
        self.assertGreaterEqual(len(out.get("emotion", {}).get("error", "")), 1)
        self.assertTrue(out["conclusion"])


class TestProviderBinding(unittest.TestCase):
    """发现 2：密钥与服务商成组绑定，错配要报错而不是静默发请求。"""

    KEYS = ("LONGCAT_API_KEY", "LONGCAT_BASE_URL", "LONGCAT_MODEL",
            "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
            "LLM_PROVIDER", "LLM_FALLBACKS",
            "MYLOCALAI_MODELS_PATH", "MYLOCALAI_MODEL_SECRETS_PATH",
            "MYLOCALAI_PRIVACY_PREFERENCES_PATH")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        # 注册表/密钥/隐私偏好指向临时空目录：本组测"env 密钥与提供方成组绑定"，
        # 不能读用户在设置面板保存的 models.json——否则 active 提供方与 stored
        # 密钥会让"错配必须报错"的断言失去前提（默认提供方被改成别的了）。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        os.environ["MYLOCALAI_MODELS_PATH"] = str(base / "models.json")
        os.environ["MYLOCALAI_MODEL_SECRETS_PATH"] = str(base / "secrets.json")
        os.environ["MYLOCALAI_PRIVACY_PREFERENCES_PATH"] = str(base / "privacy.json")
        llm_client.reset_cache()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        llm_client.reset_cache()

    def test_mismatched_key_is_reported_not_silently_used(self):
        """只设 OPENAI_API_KEY 而提供方仍是 longcat —— 必须报错，而不是继续发往 LongCat。"""
        os.environ["OPENAI_API_KEY"] = "sk-fake-openai"
        ok, detail = llm_client.validate_config()
        self.assertFalse(ok)
        self.assertIn("不匹配", detail)
        self.assertIn("LONGCAT_API_KEY", detail)
        self.assertIsNone(llm_client.create_client(), "错配时不应给出客户端")

    def test_switching_provider_changes_all_three_at_once(self):
        os.environ["LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-fake-openai"
        ok, detail = llm_client.validate_config()
        self.assertTrue(ok, detail)
        self.assertEqual(llm_client.get_provider(), "openai")
        self.assertIn("api.openai.com", llm_client.get_base_url())
        self.assertNotEqual(llm_client.get_model(), "LongCat-2.5-Preview")
        self.assertIsNotNone(llm_client.create_client())

    def test_provider_specific_overrides_win(self):
        os.environ["LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-x"
        os.environ["OPENAI_BASE_URL"] = "https://proxy.internal/v1"
        os.environ["OPENAI_MODEL"] = "my-model"
        llm_client.reset_cache()
        self.assertEqual(llm_client.get_base_url(), "https://proxy.internal/v1")
        self.assertEqual(llm_client.get_model(), "my-model")

    def test_unknown_provider_is_rejected_with_readable_reason(self):
        os.environ["LLM_PROVIDER"] = "no-such-provider"
        ok, detail = llm_client.validate_config()
        self.assertFalse(ok)
        self.assertIn("未知提供方", detail)

    def test_fallback_chain_is_used_when_primary_is_down(self):
        """主链路失败时依次尝试 LLM_FALLBACKS，并把两家的错误都报出来。"""
        os.environ["LONGCAT_API_KEY"] = "sk-longcat"
        os.environ["LLM_FALLBACKS"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-openai"
        primary, backup = FakeClient(error=ConnectionError("primary down")), FakeClient(reply='{"ok": 1}')
        built = []

        def fake_create_client():
            built.append("primary")
            return primary

        def fake_client_for(provider):
            built.append(provider)
            return backup if provider == "openai" else None

        with mock.patch.object(llm_client, "create_client", side_effect=fake_create_client), \
             mock.patch.object(llm_client, "_client_for", side_effect=fake_client_for), \
             mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_allow_external("longcat", "openai")):
            obj, err = llm_client.chat_json("sys", "文本")
        self.assertEqual(err, "")
        self.assertEqual(obj, {"ok": 1})
        self.assertIn("openai", built, "主链路失败后应尝试备用提供方")

    def test_thinking_param_only_sent_to_longcat(self):
        """关闭思维链是 LongCat 专有参数，不该发给别家。"""
        os.environ["LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-x"
        seen = {}

        class Recorder(FakeClient):
            def create(self, **kwargs):
                seen.update(kwargs)
                return super().create(**kwargs)

        rec = Recorder()
        with mock.patch.object(llm_client, "create_client", return_value=rec):
            llm_client.chat_json("sys", "文本")
        self.assertNotIn("extra_body", seen, "非 LongCat 提供方不应收到 LongCat 专有参数")


class TestConcurrencyCap(unittest.TestCase):
    """发现 7：应用层并发上限 + health 不被拖住。"""

    def setUp(self):
        import mylocalai.web.app as app
        self.app = app
        self._timeout = app.ANALYZE_QUEUE_TIMEOUT_S
        app.ANALYZE_QUEUE_TIMEOUT_S = 0.05   # 测试里不等满 2 秒
        # 让信号量回到"全空"状态
        for _ in range(app.MAX_CONCURRENT_ANALYSES):
            if app._analyze_slots.acquire(blocking=False):
                app._analyze_slots.release()

    def tearDown(self):
        self.app.ANALYZE_QUEUE_TIMEOUT_S = self._timeout

    def test_extra_requests_get_503_instead_of_queueing(self):
        from fastapi import HTTPException
        held = []
        for _ in range(self.app.MAX_CONCURRENT_ANALYSES):
            held.append(self.app._Slot().__enter__())
        try:
            with self.assertRaises(HTTPException) as ctx:
                self.app._Slot().__enter__()
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertIn("Retry-After", ctx.exception.headers or {})
        finally:
            for slot in held:
                slot.__exit__()

    def test_slots_are_released_after_use(self):
        with self.app._Slot():
            self.assertEqual(self.app._active_analyses, 1)
        self.assertEqual(self.app._active_analyses, 0)

    def test_health_does_not_touch_the_llm_chain(self):
        with mock.patch.object(self.app, "judge", side_effect=AssertionError("health 不该调用分析链路")):
            body = self.app.health()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["max_concurrent"], self.app.MAX_CONCURRENT_ANALYSES)
        self.assertIn("llm_cache", body)


class TestAgentCancellation(unittest.TestCase):
    """发现 8/11：超时后不再继续消耗 API/网络，进程也不再被拖住。"""

    def test_cancel_stops_further_component_computation(self):
        started = []

        def slow(_text):
            started.append(1)
            return {"ok": True}

        funcs = {name: slow for name in ("markers", "emotion", "aggression", "logic", "claims", "evidence")}
        st = AnalysisState("text", funcs)
        st.ensure("markers")
        st.cancel("模拟超时")
        result = st.ensure("emotion")
        self.assertIn("error", result)
        self.assertEqual(len(started), 1, "取消后不应再开算新组件（尤其不再发 LLM/联网请求）")

    def test_agent_timeout_is_reported_and_bounded(self):
        from mylocalai.rumor.agent import RumorAgent
        from tests.test_rumor_agent_offline import ScriptedModel, call_all_tools, honest_final, stub_funcs, RUMOR_TEXT

        agent = RumorAgent(llm=ScriptedModel(script=[call_all_tools, honest_final], delay=1.5),
                           funcs=stub_funcs(), use_default_llm=False, deadline_s=0.3)
        t0 = time.perf_counter()
        r = agent.analyze(RUMOR_TEXT)
        self.assertEqual(r["mode"], "timeout")
        self.assertLess(time.perf_counter() - t0, 2.0, "超时后应立刻返回，不等后台线程")
        self.assertIn("时限", r["agent_error"])

    def test_worker_thread_is_daemon(self):
        """后台线程必须是 daemon，否则短命 CLI 进程要等它跑完才退出。"""
        import mylocalai.rumor.agent as A
        captured = {}

        class SpyAgent(A.RumorAgent):
            def _run_agent(self, state):
                captured["thread"] = threading.current_thread()
                raise RuntimeError("stop-here")

        agent = SpyAgent(llm=object(), funcs={}, use_default_llm=False, deadline_s=5)
        agent.analyze("文本")
        thread = captured.get("thread")
        self.assertIsNotNone(thread)
        self.assertTrue(thread.daemon, "Agent 工作线程应为 daemon")

    def test_backfill_threads_are_daemon_and_parallel(self):
        """补算组件的线程也必须是 daemon，且确实是并行起的。

        并行性用一个"四方屏障"判断，而不是墙钟：四个组件都在屏障上等齐才算并行，
        串行执行会让屏障超时（原先用 elapsed<0.35s 判断，机器满负载时会假红）。
        """
        import mylocalai.rumor.agent as A
        seen: list = []
        errors: list = []
        barrier = threading.Barrier(4, timeout=10)

        def record(_text):
            seen.append(threading.current_thread())
            try:
                barrier.wait()
            except threading.BrokenBarrierError as exc:   # 串行执行才会走到这里
                errors.append(exc)
            return {"ok": True}

        funcs = {name: record for name in ("emotion", "aggression", "logic", "claims")}
        state = AnalysisState("文本", funcs)
        A.RumorAgent._fill_missing(state)
        self.assertEqual(len(seen), 4)
        self.assertTrue(all(t.daemon for t in seen), "补算线程应为 daemon")
        self.assertEqual(errors, [], "四个组件应并行补算（屏障超时说明被串行执行）")


class TestOptionalDepsSplit(unittest.TestCase):
    """发现 2：pip install -r requirements.txt 不该再连带装数 GB 的本地推理栈。"""

    def test_main_requirements_exclude_heavy_local_stack(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        active = [line.strip() for line in text.splitlines()
                  if line.strip() and not line.strip().startswith("#")]
        for heavy in ("torch", "transformers", "modelscope"):
            self.assertFalse(any(line.startswith(heavy) for line in active),
                             f"{heavy} 不应出现在主依赖清单的生效行里")

    def test_optional_requirements_hold_the_local_stack(self):
        path = ROOT / "requirements-optional.txt"
        self.assertTrue(path.exists(), "重型可选依赖应有独立的清单文件")
        text = path.read_text(encoding="utf-8")
        for heavy in ("torch", "transformers", "modelscope"):
            self.assertIn(heavy, text)


class TestNoHardcodedPaths(unittest.TestCase):
    """发现 3：不再写死项目绝对路径（换机/换目录/换系统都能跑）。"""

    def test_no_absolute_project_path_in_code(self):
        # 本文件自己也要被扫，所以待查字符串在这里拼出来，避免自我命中
        needles = ["D:" + "/MyLocalAI", "D:" + "\\MyLocalAI"]
        offenders = []
        targets = [ROOT / "tests", ROOT / "src" / "mylocalai" / "web" / "static", ROOT / "src" / "mylocalai" / "web" / "app.py",
                   ROOT / "my_website" / "README.md", ROOT / "src" / "mylocalai" / "web" / "app.py"]
        for target in targets:
            files = [target] if target.is_file() else list(target.rglob("*"))
            for path in files:
                if not path.is_file() or path.suffix not in {".py", ".js", ".html", ".bat", ".md"}:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if any(needle in text for needle in needles):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], f"这些文件仍写死项目绝对路径：{offenders}")

    def test_project_root_is_derived_not_configured(self):
        """站点后端与四个测试入口都必须按自身位置推导项目根目录。"""
        self.assertIn("__file__", (ROOT / "src" / "mylocalai" / "web" / "app.py").read_text(encoding="utf-8"))
        for name in ("run_module_tests.py", "smoke_api.py", "verify_fixes.py"):
            text = (ROOT / "tests" / name).read_text(encoding="utf-8")
            self.assertIn("__file__", text, f"{name} 应按自身位置推导项目根目录")


class TestSentinelsDocumented(unittest.TestCase):
    """发现 5 的口径分歧：哨兵值显式命名，回退可检查。"""

    def test_emotion_sentinel_is_named_and_outside_enum(self):
        self.assertEqual(LM._EMOTION_TENDENCY_DEFAULT, "未知")
        self.assertNotIn(LM._EMOTION_TENDENCY_DEFAULT, LM._EMOTION_TENDENCY_ENUM)
        with mock.patch.object(LM, "chat_json", lambda *a, **k: ({"emotion_tendency": "胡编的"}, "")):
            self.assertEqual(LM.analyze_emotion("文本")["emotion_tendency"], LM._EMOTION_TENDENCY_DEFAULT)

    def test_fallacy_sentinel_is_named_and_outside_enum(self):
        self.assertEqual(LG._FALLACY_SENTINEL, "未知")
        self.assertNotIn(LG._FALLACY_SENTINEL, LG._FALLACY_ENUM)
        self.assertEqual(LG._normalize_fallacy("胡编的"), LG._FALLACY_SENTINEL)
        self.assertEqual(LG._normalize_fallacy(""), LG._FALLACY_SENTINEL)

    def test_rollback_values_are_accepted_by_the_new_criteria(self):
        with mock.patch.object(LM, "chat_json", lambda *a, **k: ({"emotion_tendency": "胡编的"}, "")):
            emo = LM.analyze_emotion("文本")
        self.assertTrue(emo["emotion_tendency"] in LM._EMOTION_TENDENCY_ENUM
                        or emo["emotion_tendency"] == LM._EMOTION_TENDENCY_DEFAULT)


class TestSurrogateSafety(unittest.TestCase):
    """2026-10-15 检验报告 P0 ⑫：孤立代理项不得让任何一层抛异常。"""

    BAD = "ok\ud800bad"

    def test_sanitize_makes_text_encodable(self):
        from mylocalai.util.text_safety import has_surrogates, sanitize_text
        self.assertTrue(has_surrogates(self.BAD))
        fixed = sanitize_text(self.BAD)
        fixed.encode("utf-8")                       # 不抛即通过
        self.assertFalse(has_surrogates(fixed))
        self.assertEqual(sanitize_text("正常文本"), "正常文本", "无代理项时原样返回")

    def test_cache_key_does_not_raise(self):
        self.assertIsInstance(llm_client._cache_key("sys", self.BAD, 0.0, 512, False), str)

    def test_chat_json_never_raises_on_surrogates(self):
        """离线（无可用客户端）也必须返回带 error 的结果，而不是抛 UnicodeEncodeError。"""
        with mock.patch.object(llm_client, "create_client", return_value=None):
            obj, err = llm_client.chat_json("sys", self.BAD)
        self.assertIsNone(obj)
        self.assertTrue(err)

    def test_components_return_error_key_not_exception(self):
        for fn, key in ((LM.analyze_emotion, "emotion_score"),
                        (LM.analyze_aggression, "aggression_level"),
                        (LG.analyze_logic, "logic_relevance")):
            with mock.patch.object(llm_client, "create_client", return_value=None):
                out = fn(self.BAD)
            self.assertIn(key, out, fn.__name__)

    def test_agent_sanitizes_before_analysis(self):
        from mylocalai.rumor.agent import RumorAgent
        seen = {}

        def fake_emotion(text):
            seen["text"] = text
            return {"emotion_tendency": "冷静客观", "emotion_score": 0.0, "keywords": []}

        funcs = {"markers": lambda t: {"marker_points": 0, "credibility_points": 0,
                                       "risk_hits": {}, "credibility_hits": {},
                                       "assertion_signal": False},
                 "emotion": fake_emotion,
                 "aggression": lambda t: {"aggression_level": "低", "attack_target": "无", "attack_type": "无"},
                 "logic": lambda t: {"logic_relevance": "强", "logic_fallacy": "无"},
                 "claims": lambda t: {"has_factual_claim": False, "claims": []},
                 "evidence": lambda t: {"entries": [], "skipped": "无主张"}}
        agent = RumorAgent(llm=None, funcs=funcs, use_default_llm=False)
        out = agent.analyze(self.BAD)
        self.assertIn(out["label"], V.ALL_LABELS)
        self.assertEqual(out["degraded_components"], [], "规整后不应有组件降级")
        self.assertNotIn("\ud800", seen.get("text", ""), "组件拿到的文本应已规整")
        json.dumps(out, ensure_ascii=False)          # 结果可序列化（MCP/网站同路径）

    def test_web_entry_rejects_surrogates_with_400(self):
        import mylocalai.web.app as app
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            app.analyze(app.AnalyzeRequest(text=self.BAD, mode="classic"))
        self.assertEqual(ctx.exception.status_code, 400)


class TestTransportRobustness(unittest.TestCase):
    """2026-10-15 检验报告 P1 ⑤⑥：失败不放大、预算跨线程。"""

    def setUp(self):
        llm_cache.clear()

    def tearDown(self):
        llm_cache.clear()

    def test_failed_produce_is_shared_not_retried_per_waiter(self):
        """首位失败时，等待者拿到同一份失败结果，而不是各自重跑。

        用"屏障同步起跑 + 首位悬停等待释放"把时序钉死：等所有线程都进入 call() 排队后
        才让首位返回，避免"线程还没排上队、首位已经算完"这种被测机器负载决定的假红。
        """
        calls = {"n": 0}
        release = threading.Event()
        started = threading.Barrier(4, timeout=15)
        producing = threading.Event()

        def produce():
            calls["n"] += 1
            producing.set()
            release.wait(timeout=20)
            return ("", "API 调用失败: ConnectionError: down")

        results = []

        def run():
            started.wait()
            results.append(llm_cache.call("failed-key", produce=produce,
                                          accept=lambda r: bool(r[0]) and not r[1]))

        threads = [threading.Thread(target=run) for _ in range(4)]
        [t.start() for t in threads]
        self.assertTrue(producing.wait(timeout=15), "首位应调用 produce")
        time.sleep(1.0)          # 给等待者足够时间进入排队
        release.set()
        [t.join(timeout=30) for t in threads]
        self.assertEqual(calls["n"], 1, "失败结果应在这批等待者之间共享，而不是各跑一次")
        self.assertEqual(len(results), 4)
        self.assertEqual(len(set(map(str, results))), 1)

    def test_leader_exception_is_shared(self):
        calls = {"n": 0}
        release = threading.Event()
        started = threading.Barrier(4, timeout=15)
        producing = threading.Event()

        def boom():
            calls["n"] += 1
            producing.set()
            release.wait(timeout=20)
            raise RuntimeError("上游故障")

        errors = []

        def run():
            started.wait()
            try:
                llm_cache.call("boom-key", produce=boom, accept=lambda r: True)
            except RuntimeError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=run) for _ in range(4)]
        [t.start() for t in threads]
        self.assertTrue(producing.wait(timeout=15), "首位应调用 produce")
        time.sleep(1.0)
        release.set()
        [t.join(timeout=30) for t in threads]
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(errors), 4, "等待者应看到同一个异常")

    def test_deadline_scope_at_inherits_into_threads(self):
        with llm_cache.deadline_scope(0.05):
            time.sleep(0.08)
            inherited = llm_cache.deadline_at()
            box = {}
            thread = threading.Thread(target=lambda: box.update(
                exhausted=llm_cache.budget_exhausted()) or None)
            thread.start()
            thread.join()
            self.assertFalse(box["exhausted"], "不继承时子线程看不到预算（原行为，仅作对照）")
            box2 = {}

            def child():
                with llm_cache.deadline_scope_at(inherited):
                    box2["exhausted"] = llm_cache.budget_exhausted()
                    box2["timeout"] = llm_cache.attempt_timeout(30)

            thread = threading.Thread(target=child)
            thread.start()
            thread.join()
            box3 = {}

            def child_positive_budget():
                with llm_cache.deadline_scope(0.3):
                    box3["timeout"] = llm_cache.attempt_timeout(30)

            thread = threading.Thread(target=child_positive_budget)
            thread.start()
            thread.join()
        self.assertTrue(box2["exhausted"], "继承后子线程应看到预算已用尽")
        # 单次尝试超时的下限改为可配（LLM_MIN_ATTEMPT_S，默认 0.25s），不再写死 1.0s：
        # 预算只剩 0.2s 时发一次完整尝试会让整条链路超出预算（2026-10-22 检验报告发现 4）。
        floor = float(os.environ.get("LLM_MIN_ATTEMPT_S") or 0.25)
        self.assertAlmostEqual(box2["timeout"], floor, places=6)
        # 容差同 test_p0_fixes_2026_10_22 的 _BUDGET_FLOAT_TOL（1e-6）：
        # remaining_seconds() 是 (monotonic()+预算)-monotonic() 两次大数相减，误差随
        # 开机时长增长（monotonic 基数越大表示误差越大；Windows monotonic 还是约
        # 15.6ms 一跳的粗粒度时钟），实测 0.3 会被抬成 0.3000000000029104。1e-6 只吃
        # 浮点噪声，长开机下也不误报，不放松"不得超预算"的契约。
        self.assertLessEqual(box3["timeout"], 0.3 + 1e-6,
                             "有剩余预算时不得超过剩余预算（允许浮点噪声）")


class TestConfigShapeContract(unittest.TestCase):
    """2026-10-15 检验报告 P0 ⑩：坏配置不崩、不静默降级，且留告警。"""

    def test_list_official_sites_falls_back_with_warning(self):
        path = _write_json({"official_sites": ["cctv.com", "xinhuanet.com"]})
        self.addCleanup(os.remove, path)
        os.environ["RUMOR_SOURCES_EXTRA_PATH"] = path
        self.addCleanup(os.environ.pop, "RUMOR_SOURCES_EXTRA_PATH", None)
        import importlib
        reloaded = importlib.reload(SRC)
        try:
            self.assertEqual(reloaded.classify_domain("https://www.gov.cn/x"), reloaded.TIER_OFFICIAL)
            self.assertEqual(reloaded.source_kind("https://www.cctv.com/a"), "官方（央视新闻）")
            self.assertTrue(any("official_sites" in w for w in rules.load_warnings()))
        finally:
            importlib.reload(reloaded)

    def test_string_tiers_falls_back_with_warning(self):
        path = _write_json({"tiers": {"官方权威": "gov.cn"}})
        self.addCleanup(os.remove, path)
        os.environ["RUMOR_SOURCES_EXTRA_PATH"] = path
        self.addCleanup(os.environ.pop, "RUMOR_SOURCES_EXTRA_PATH", None)
        import importlib
        reloaded = importlib.reload(SRC)
        try:
            self.assertEqual(reloaded.classify_domain("https://www.gov.cn/x"), reloaded.TIER_OFFICIAL,
                             "档位值写坏不能把官方域名降级成「未知来源」")
            self.assertEqual(reloaded.classify_domain("https://who.int/x"), reloaded.TIER_OFFICIAL)
            self.assertTrue(any("tiers" in w for w in rules.load_warnings()))
        finally:
            importlib.reload(reloaded)

    def test_tier_roles_are_configurable_by_name(self):
        """换行业把档位重命名（含 official/reliable/fetch/link/channel_score）后，判定不再静默失效。

        这是"泛用性 ①"的常驻契约闸门：同一域名在**五个面**上必须给出一致结论
        （classify_domain / source_kind / OFFICIAL_TIERS / RELIABLE_TIERS / _FETCH_TIERS / LINK_TIERS）。
        修前：档名一改，classify_domain 认得新档名，但可靠/可抓/可扩链三套集合仍是旧档名 → 静默失效。
        """
        import importlib
        from mylocalai.retrieval import browser as B
        from mylocalai.retrieval import search as SE
        from mylocalai.rumor import opinions as O

        path = _write_json({
            "tiers": {"医疗权威": ["nhc.gov.cn"], "专业医学媒体": ["dxy.cn"],
                      "健康门户": ["haodf.com"], "健康号/UGC": ["zhihu.com"]},
            "official_tiers": ["医疗权威"],
            "reliable_tiers": ["医疗权威", "专业医学媒体"],
            "fetch_tiers": ["医疗权威", "专业医学媒体", "健康门户"],
            "link_tiers": ["医疗权威"],
            "channel_score": {"医疗权威": 3.0, "专业医学媒体": 2.0,
                              "健康门户": 1.5, "健康号/UGC": 1.0, "未知来源": 0.5},
        })
        self.addCleanup(os.remove, path)
        prev_env = os.environ.get("RUMOR_SOURCES_PATH")
        os.environ["RUMOR_SOURCES_PATH"] = path
        s = importlib.reload(SRC)
        se = importlib.reload(SE)
        b = importlib.reload(B)
        o = importlib.reload(O)
        try:
            # 声明顺序即优先级：第一个是官方档，第二个是权威媒体档
            self.assertEqual(s.DECLARED_TIERS,
                             ("医疗权威", "专业医学媒体", "健康门户", "健康号/UGC"))
            self.assertEqual(s.tier_rank("医疗权威"), 0)
            self.assertLess(s.tier_rank("医疗权威"), s.tier_rank("健康门户"),
                            "声明在前的档位必须排在前面（排序不再写死档名）")

            # 逐域名核对五个面给出一致结论：(档位, 是否官方, 是否可靠, 是否可抓, 是否可扩链)
            expectations = {
                "https://www.nhc.gov.cn/x": ("医疗权威", True, True, True, True),
                "https://www.dxy.cn/x": ("专业医学媒体", False, True, True, False),
                "https://www.haodf.com/x": ("健康门户", False, False, True, False),
                "https://zhihu.com/x": ("健康号/UGC", False, False, False, False),
                "https://example.com/x": (s.TIER_UNKNOWN, False, False, False, False),
            }
            for url, (tier, official, reliable, fetch, link) in expectations.items():
                got = s.classify_domain(url)
                self.assertEqual(got, tier, url)
                self.assertEqual(got in s.OFFICIAL_TIERS, official, f"{url} 官方档判定不一致")
                self.assertEqual(got in s.RELIABLE_TIERS, reliable, f"{url} 可靠档判定不一致")
                self.assertEqual(got in se._FETCH_TIERS, fetch, f"{url} 可抓档判定不一致")
                self.assertEqual(got in b.LINK_TIERS, link, f"{url} 可扩链档判定不一致")
                self.assertEqual(s.tier_rank(got) < len(s.DECLARED_TIERS), got != s.TIER_UNKNOWN,
                                 f"{url} 排序位次与档位声明不一致")

            # source_kind 的"官方/非官方"必须与 OFFICIAL_TIERS / RELIABLE_TIERS 同源
            self.assertEqual(s.source_kind("https://www.nhc.gov.cn/x"), "官方（政府/权威机构）")
            self.assertEqual(s.source_kind("https://www.dxy.cn/x"), "非官方（权威媒体）")
            self.assertEqual(s.source_kind("https://www.haodf.com/x"), "非官方",
                             "名单里认得的档，但不是官方/权威媒体 → 统一非官方")
            self.assertEqual(s.channel_score_table()["医疗权威"], 3.0)
            self.assertEqual(o.CHANNEL_SCORE.get("医疗权威"), 3.0, "渠道权重与配置同源")

            # 抗伪装属性在改名后依然成立
            self.assertNotIn("官方", s.source_kind("https://www.nhc.gov.cn.evil.com/a"))
            self.assertNotIn(s.classify_domain("https://www.nhc.gov.cn.evil.com/a"), s.RELIABLE_TIERS)
            self.assertFalse(any("一个都不在" in w or "不在 tiers 声明里" in w
                                 for w in rules.load_warnings()),
                             f"显式角色不该有兜底告警：{rules.load_warnings()}")
        finally:
            # 先还原环境变量再重载：否则重载结果会带着替换名单留在这个进程里，
            # 后面的用例（TestRulesAreConfigurable 等）会看到一个漂移过的名单。
            if prev_env is None:
                os.environ.pop("RUMOR_SOURCES_PATH", None)
            else:
                os.environ["RUMOR_SOURCES_PATH"] = prev_env
            s = importlib.reload(s)
            se = importlib.reload(se)
            b = importlib.reload(b)
            o = importlib.reload(o)
        self.assertEqual(SRC.classify_domain("https://www.gov.cn/a"), SRC.TIER_OFFICIAL,
                         "用例结束后必须回到默认名单")

    def test_stale_role_names_fall_back_to_declaration_order(self):
        """整套替换名单、但档位角色段还写着旧档名时，不得退化成"哪个档都不可靠"。"""
        import importlib
        from mylocalai.retrieval import search as SE

        path = _write_json({
            "tiers": {"医疗权威": ["nhc.gov.cn"], "专业医学媒体": ["dxy.cn"], "健康门户": ["haodf.com"]},
            "reliable_tiers": ["官方权威", "权威媒体/辟谣平台"],
            "fetch_tiers": ["官方权威", "权威媒体/辟谣平台", "一般门户媒体"],
            "link_tiers": ["官方权威", "权威媒体/辟谣平台"],
        })
        self.addCleanup(os.remove, path)
        prev_env = os.environ.get("RUMOR_SOURCES_PATH")
        os.environ["RUMOR_SOURCES_PATH"] = path
        s = importlib.reload(SRC)
        se = importlib.reload(SE)
        try:
            self.assertEqual(s.DECLARED_TIERS, ("医疗权威", "专业医学媒体", "健康门户"))
            self.assertEqual(s.RELIABLE_TIERS, frozenset({"医疗权威", "专业医学媒体"}),
                             "旧档名一个都不在名单里 → 按声明顺序取前 2 档兜底")
            self.assertEqual(se._FETCH_TIERS,
                             frozenset({"医疗权威", "专业医学媒体", "健康门户"}))
            self.assertIn("医疗权威", s.OFFICIAL_TIERS)
            self.assertEqual(s.classify_domain("https://www.nhc.gov.cn/x") in s.RELIABLE_TIERS, True)
            self.assertTrue(any("reliable_tiers" in w for w in rules.load_warnings()),
                            "兜底必须留告警，不能静默")
        finally:
            if prev_env is None:
                os.environ.pop("RUMOR_SOURCES_PATH", None)
            else:
                os.environ["RUMOR_SOURCES_PATH"] = prev_env
            s = importlib.reload(s)
            se = importlib.reload(se)
        self.assertEqual(SRC.classify_domain("https://www.gov.cn/a"), SRC.TIER_OFFICIAL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
