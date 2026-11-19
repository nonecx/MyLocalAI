# -*- coding: utf-8 -*-
"""2026-10-22 检验报告 P0 修复的回归测试。

对应关系：
  P0-1  src/mylocalai/rumor/rules.py       配置告警不再被形状校验抹掉
  P0-2  src/mylocalai/rumor/rules.py       阈值等标量写坏要留告警
  P0-3  mylocalai/llm/llm_cache.py   关缓存不再连带关掉 single-flight
  P0-4  mylocalai/llm/llm_cache.py   预算下限可配、等待者不再空等 30s
  P0-5  mylocalai/llm/llm_client.py  瞬时故障不再被当成"参数不支持"；回落不再叠乘
  P0-6  src/mylocalai/rumor/tools.py       read_source 取消后不再抓取
  P0-7  src/mylocalai/classic/judge.py              空输入 0 次 API 调用；边界复核；degraded_components
  P0-8  src/mylocalai/util/console_io.py  非 UTF-8 控制台规整
  P0-9  src/mylocalai/web/app.py          /api/health 暴露上游可用性
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mylocalai.classic.judge as core_judge# noqa: E402
from mylocalai.config import rules
from mylocalai.util import console_io
from mylocalai.llm import llm_cache
from mylocalai.llm import llm_client


def _write_json(data, name="case.json"):
    path = ROOT / ".tmp" / "verify" / "p0_tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class TestConfigWarnings(unittest.TestCase):
    """P0-1 / P0-2：配置写坏必须"退回默认 + 留告警"。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("RUMOR_RULES_PATH", "RUMOR_RULES_EXTRA_PATH",
                        "RUMOR_SOURCES_PATH", "RUMOR_SOURCES_EXTRA_PATH")}
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import importlib
        importlib.reload(rules)

    def test_default_config_has_no_warnings(self):
        import importlib
        importlib.reload(rules)
        self.assertEqual(rules.load_warnings(), [], f"默认配置不该有告警：{rules.load_warnings()}")

    def test_threshold_scalar_typo_leaves_a_warning(self):
        """P0-2：thresholds.high 写成字符串 -> 退回默认且留告警。"""
        bad = _write_json({"thresholds": {"high": "不是数字"}})
        self.addCleanup(os.remove, bad)
        os.environ["RUMOR_RULES_EXTRA_PATH"] = str(bad)
        import importlib
        importlib.reload(rules)
        loaded = rules.load_rules()
        self.assertEqual(loaded["thresholds"]["high"], 70, "坏阈值应退回默认")
        self.assertTrue(any("thresholds" in w and "high" in w for w in rules.load_warnings()),
                        f"坏阈值必须留告警：{rules.load_warnings()}")

    def test_full_replacement_missing_segments_keeps_warning(self):
        """P0-1：整套替换漏段时，_load() 写的告警不能再被形状校验抹掉。"""
        partial = _write_json({"tiers": {"医疗权威": ["www.nhc.gov.cn"]}}, "partial_sources.json")
        self.addCleanup(os.remove, partial)
        os.environ["RUMOR_SOURCES_PATH"] = str(partial)
        import importlib
        importlib.reload(rules)
        rules.load_sources()          # 触发 _load()，才会写 #replaced 告警
        warnings = rules.load_warnings()
        self.assertTrue(any("#replaced" in w or "未包含这些段" in w for w in warnings),
                        f"整套替换漏段必须留告警：{warnings}")


class TestCacheAndBudget(unittest.TestCase):
    """P0-3 / P0-4。"""

    def setUp(self):
        llm_cache.clear()
        self._saved = {k: os.environ.get(k) for k in ("LLM_CACHE_MAX", "LLM_CACHE_TTL_S",
                                                      "LLM_MIN_ATTEMPT_S")}
        os.environ.pop("LLM_CACHE_MAX", None)
        os.environ.pop("LLM_CACHE_TTL_S", None)
        os.environ.pop("LLM_MIN_ATTEMPT_S", None)

    def tearDown(self):
        llm_cache.clear()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_single_flight_survives_cache_being_disabled(self):
        """P0-3：关缓存只该失去跨请求复用，不该失去并发去重。

        用一道屏障保证 4 个线程**同时**进入 call()：否则先完成的 leader 会摘掉
        _INFLIGHT 记录，晚到的线程会当成新 leader 再算一次（这是 single-flight 的
        固有边界，不是缺陷），测试就会随机变红。
        """
        calls = []
        gate = threading.Barrier(4)

        def produce():
            calls.append(1)
            time.sleep(0.15)
            return ("ok", "")

        def accept(result):
            return bool(result[0]) and not result[1]

        def worker(_i):
            gate.wait()
            return llm_cache.call("key-no-cache", produce, accept)

        os.environ["LLM_CACHE_MAX"] = "0"
        llm_cache.clear()
        with ThreadPoolExecutor(max_workers=4) as ex:
            outs = list(ex.map(worker, range(4)))
        self.assertEqual(len(calls), 1, "4 个并发同请求只应真正算一次")
        self.assertEqual(outs, [("ok", "")] * 4)

    def test_attempt_timeout_never_exceeds_the_remaining_budget(self):
        """P0-4：单次尝试超时不能突破剩余预算（原下限写死 1.0s）。

        断言带浮点容差：剩余预算由 `at - time.monotonic()` 相减得出，
        monotonic 基数越大（机器开机越久），0.3s 的表示误差可达 1e-12 量级，
        严格 `<= 0.3` 会在长开机机器上误报。容差不放松语义（1e-9 秒）。
        """
        with llm_cache.deadline_scope(0.3):
            timeout = llm_cache.attempt_timeout(30.0)
        self.assertLessEqual(timeout, 0.3 + 1e-9)
        self.assertGreater(timeout, 0.0)

    def test_attempt_timeout_floor_is_configurable(self):
        with llm_cache.deadline_scope(0.05):
            self.assertLessEqual(llm_cache.attempt_timeout(30.0), 0.25)
        os.environ["LLM_MIN_ATTEMPT_S"] = "0.5"
        try:
            with llm_cache.deadline_scope(0.05):
                self.assertLessEqual(llm_cache.attempt_timeout(30.0), 0.5)
        finally:
            os.environ.pop("LLM_MIN_ATTEMPT_S", None)

    def test_wait_budget_follows_the_request_budget(self):
        """P0-4：预算已用尽时等待者不再空等 30s。"""
        self.assertEqual(llm_cache._wait_budget(), 120.0, "未设预算时的兜底")
        with llm_cache.deadline_scope(0.05):
            self.assertLessEqual(llm_cache._wait_budget(), 0.05 + 1e-9)
            self.assertGreaterEqual(llm_cache._wait_budget(), 0.0)


class TestProviderFallback(unittest.TestCase):
    """P0-5：瞬时故障不标记"参数不支持"；参数被拒才回落且不叠乘。"""

    URL = "http://p0-test.invalid/v1"
    MODEL = "p0-model"

    def setUp(self):
        llm_cache.clear()
        llm_client._THINKING_SUPPORT.pop((self.URL, self.MODEL), None)

    def tearDown(self):
        llm_cache.clear()
        llm_client._THINKING_SUPPORT.pop((self.URL, self.MODEL), None)

    @staticmethod
    def _client(status, calls):
        class _Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                if kwargs.get("extra_body"):
                    err = RuntimeError("bad extra_body")
                    err.status_code = status
                    raise err
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='{"ok": 1}'))])
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))

    def test_transient_500_does_not_disable_the_thinking_flag(self):
        calls = []
        content, err = llm_client._chat_raw_once(
            self._client(500, calls), "sys", "text", 0.0, 64, False, self.URL, self.MODEL,
            make_fallback_client=lambda: self._client(200, calls))
        self.assertTrue(err)
        self.assertEqual(len(calls), 1, "瞬时故障不该回落再打一次")
        self.assertNotIn((self.URL, self.MODEL), llm_client._THINKING_SUPPORT,
                         "5xx/超时/DNS 不该被永久标记为不支持关闭思维链")

    def test_param_rejection_falls_back_once_and_marks_it(self):
        calls = []
        content, err = llm_client._chat_raw_once(
            self._client(400, calls), "sys", "text", 0.0, 64, False, self.URL, self.MODEL,
            make_fallback_client=lambda: self._client(200, calls))
        self.assertEqual(err, "")
        self.assertEqual(content, '{"ok": 1}')
        self.assertEqual(len(calls), 2, "回落只应多打一次（不再叠乘 max_retries）")
        self.assertIs(llm_client._THINKING_SUPPORT.get((self.URL, self.MODEL)), False)


class TestJudgeGuards(unittest.TestCase):
    """P0-7：空输入短路、边界复核、降级清单。"""

    def setUp(self):
        llm_cache.clear()
        llm_client.reset_cache()

    def tearDown(self):
        llm_cache.clear()
        llm_client.reset_cache()

    def test_empty_input_makes_no_api_call_at_all(self):
        guards = [mock.patch.object(core_judge, name, side_effect=AssertionError("不该调用"))
                  for name in ("analyze_emotion", "analyze_aggression", "analyze_logic",
                               "generate_report")]
        with guards[0], guards[1], guards[2], guards[3]:
            out = core_judge.judge("   ")
        self.assertTrue(out["empty_input"])
        self.assertIn("【分析结论】", out["report"])
        self.assertIn("report", out["degraded_components"])
        self.assertIn("emotion", out["degraded_components"])

    def test_empty_input_short_circuit_beats_the_report_call(self):
        """报告接口也算 API 调用：空输入必须一次都不发。"""
        with mock.patch.object(core_judge, "generate_report",
                               side_effect=AssertionError("空输入不该生成报告")):
            out = core_judge.judge("")
        self.assertTrue(out["empty_input"])

    def test_boundary_score_is_rechecked_with_an_independent_sample(self):
        """贴着档位线的分数要再取一个**独立样本**（绕过缓存）后用中位数定档。"""
        scores = iter([4.9, 5.1])
        seen = []

        def fake_emotion(text, use_cache=True):
            seen.append(use_cache)
            return {"emotion_score": next(scores), "emotion_tendency": "情绪化宣泄", "keywords": []}

        with mock.patch.object(core_judge, "analyze_emotion", side_effect=fake_emotion), \
             mock.patch.object(core_judge, "analyze_aggression",
                               return_value={"attack_target": "无", "aggression_level": "低",
                                             "attack_type": "无"}), \
             mock.patch.object(core_judge, "analyze_logic",
                               return_value={"argument": "a", "conclusion": "c",
                                             "logic_relevance": "弱", "logic_fallacy": "无"}), \
             mock.patch.object(core_judge, "generate_report", return_value="R"):
            out = core_judge.judge("文本")
        self.assertEqual(seen, [True, False], "第二次必须是绕过缓存的独立样本")
        self.assertEqual(out["conclusion"], "疑似情绪化谣言", "中位数 5.0 应落在 >=5 且逻辑弱这一档")

    def test_degraded_components_lists_failed_modules_and_fallback_report(self):
        with mock.patch.object(core_judge, "analyze_emotion",
                               return_value={"emotion_score": 0.0, "error": "boom"}), \
             mock.patch.object(core_judge, "analyze_aggression",
                               return_value={"attack_target": "无", "aggression_level": "低",
                                             "attack_type": "无"}), \
             mock.patch.object(core_judge, "analyze_logic",
                               return_value={"argument": "a", "conclusion": "c",
                                             "logic_relevance": "强", "logic_fallacy": "无"}), \
             mock.patch.object(core_judge, "generate_report",
                               return_value="（提示：本报告由本地模板回退生成，未调用大模型 API。）"):
            out = core_judge.judge("文本")
        self.assertEqual(out["degraded_components"], ["emotion", "report"])


class TestReadSourceAfterCancel(unittest.TestCase):
    """P0-6：Agent 超时后残留的 read_source 不得再发真实抓取。"""

    def test_read_source_does_not_fetch_after_cancel(self):
        from mylocalai.rumor.state import AnalysisState
        from mylocalai.rumor.tools import build_tools

        fetched = []

        class FakeBrowser:
            def read(self, index):
                fetched.append(index)
                return True, {"url": "http://example.invalid", "text": "t"}

        funcs = {name: (lambda _t: {"ok": True})
                 for name in ("markers", "emotion", "aggression", "logic", "claims", "evidence")}
        state = AnalysisState("文本", funcs)
        state.cancel("模拟超时")

        tools = build_tools(state)
        read = [t for t in tools if t.name == "read_source"][0]
        with mock.patch.object(AnalysisState, "get_browser", lambda self: FakeBrowser()):
            out = read.invoke({"index": 0})
        self.assertEqual(fetched, [], "取消后不应再发真实 HTTP 抓取")
        self.assertIn("error", out)


class TestConsoleAndHealth(unittest.TestCase):
    """P0-8 / P0-9。"""

    def test_force_utf8_stdio_is_safe_without_reconfigure(self):
        class NoReconfigure:
            write = staticmethod(lambda *_a, **_k: None)

        original = sys.stdout
        try:
            sys.stdout = NoReconfigure()          # 没有 reconfigure 的流应被静默跳过
            console_io.force_utf8_stdio()
        finally:
            sys.stdout = original

    def test_health_reports_llm_availability(self):
        import mylocalai.web.app as app
        body = app.health()
        self.assertIn("llm", body, "健康检查必须暴露上游可用性")
        self.assertIn("usable", body["llm"])
        self.assertIn("provider", body["llm"])
        self.assertIn("model", body["llm"])
        self.assertIn("base_url", body["llm"])
        self.assertIsInstance(body["llm"]["usable"], bool)


if __name__ == "__main__":
    unittest.main()
