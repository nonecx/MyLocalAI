# -*- coding: utf-8 -*-
"""2026-10-22 检验报告 P1/P2 修复（行为类）的回归测试。

覆盖：F3 三维并行、F6 证据本空置信度、F7 解释一致性、F8 网站 agent 三维、
S5 多进程可观测、S7 Agent 超时对齐、S8 缓存清扫、G6 依赖上限、G7 启动脚本。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mylocalai.classic.judge as core_judge# noqa: E402
from mylocalai.rumor import verdict
from mylocalai.llm import llm_cache
from mylocalai.llm import llm_client


class TestParallelModules(unittest.TestCase):
    """F3：三个分析模块并行，墙钟不再叠成 3 倍。"""

    def setUp(self):
        llm_cache.clear()
        llm_client.reset_cache()

    def tearDown(self):
        llm_cache.clear()
        llm_client.reset_cache()

    def test_three_modules_run_in_parallel(self):
        barrier = threading.Barrier(3, timeout=15)
        broken = []

        def make(result):
            def run(_text):
                try:
                    barrier.wait()
                except threading.BrokenBarrierError as exc:
                    broken.append(exc)
                return result
            return run

        with mock.patch.object(core_judge, "analyze_emotion",
                               side_effect=make({"emotion_score": 9.0, "emotion_tendency": "恶意辱骂",
                                                 "keywords": []})), \
             mock.patch.object(core_judge, "analyze_aggression",
                               side_effect=make({"attack_target": "针对个人",
                                                 "aggression_level": "高", "attack_type": "人身攻击"})), \
             mock.patch.object(core_judge, "analyze_logic",
                               side_effect=make({"argument": "a", "conclusion": "c",
                                                 "logic_relevance": "弱", "logic_fallacy": "无"})), \
             mock.patch.object(core_judge, "generate_report", return_value="R"):
            out = core_judge.judge("并行测试文本")
        self.assertEqual(broken, [], "三个模块必须同时到达屏障（并行），串行执行会让屏障超时")
        self.assertEqual(out["conclusion"], "高度疑似水军/煽动性言论")

    def test_parallel_run_still_returns_a_report(self):
        with mock.patch.object(core_judge, "analyze_emotion",
                               return_value={"emotion_score": 2.0, "emotion_tendency": "冷静客观",
                                             "keywords": []}), \
             mock.patch.object(core_judge, "analyze_aggression",
                               return_value={"attack_target": "无", "aggression_level": "低",
                                             "attack_type": "无"}), \
             mock.patch.object(core_judge, "analyze_logic",
                               return_value={"argument": "a", "conclusion": "c",
                                             "logic_relevance": "强", "logic_fallacy": "无"}), \
             mock.patch.object(core_judge, "generate_report", return_value="报告内容") as gr:
            out = core_judge.judge("文本")
        self.assertEqual(out["report"], "报告内容")
        self.assertEqual(out["conclusion"], "理性探讨")
        gr.assert_called_once()

    def test_parallel_threads_inherit_the_deadline(self):
        """子线程必须继承同一个绝对截止时刻，否则并行会把预算撑破。"""
        seen = []

        def record(_text):
            seen.append(llm_cache.deadline_at())
            return {"emotion_score": 1.0}

        with mock.patch.object(core_judge, "analyze_emotion", side_effect=record), \
             mock.patch.object(core_judge, "analyze_aggression",
                               return_value={"aggression_level": "低"}), \
             mock.patch.object(core_judge, "analyze_logic",
                               return_value={"logic_relevance": "强"}), \
             mock.patch.object(core_judge, "generate_report", return_value="R"):
            with llm_cache.deadline_scope(5):
                core_judge.judge("文本")
        self.assertTrue(seen and seen[0] is not None, "子线程应看到继承来的预算")


class TestEvidenceConfidence(unittest.TestCase):
    """F6：证据本被全部过滤后不该再报高置信度。"""

    @staticmethod
    def _components(has_claim=True):
        return ({"marker_points": 0, "credibility_points": 0, "assertion_signal": has_claim},
                {"emotion_score": 3.0}, {"aggression_level": "中"},
                {"logic_relevance": "弱"},
                {"has_factual_claim": has_claim, "unsourced_unverifiable": 0})

    def test_all_irrelevant_entries_degrade_evidence(self):
        evidence = {"entries": [], "empty_after_filter": True, "filtered_out": 3}
        v = verdict.assess(*self._components(), evidence=evidence)
        self.assertIn("evidence", v["degraded_components"])
        self.assertNotEqual(v["confidence"], "高")

    def test_skipped_evidence_is_still_not_degradation(self):
        evidence = {"entries": [], "skipped": "无可核实的事实性声明，未搜索"}
        v = verdict.assess(*self._components(has_claim=False), evidence=evidence)
        self.assertNotIn("evidence", v["degraded_components"])
        self.assertEqual(v["confidence"], "高")

    def test_scope_filtering_is_not_degradation(self):
        evidence = {"entries": [], "scope": "event", "excluded_not_event": ["http://x"]}
        v = verdict.assess(*self._components(), evidence=evidence)
        self.assertNotIn("evidence", v["degraded_components"])


class TestExplanationConsistency(unittest.TestCase):
    """F7：Agent 解释与确定性结论一致才采用。"""

    def test_negated_sentence_is_not_a_match(self):
        from mylocalai.rumor.agent import _explanation_matches
        self.assertFalse(_explanation_matches("这条消息不是高风险谣言，请勿转发。", "高风险谣言"))
        self.assertFalse(_explanation_matches("经核实，并非高风险谣言。", "高风险谣言"))

    def test_plain_match_is_used(self):
        from mylocalai.rumor.agent import _explanation_matches
        self.assertTrue(_explanation_matches("结论：高风险谣言，风险分 85。建议不要转发。", "高风险谣言"))

    def test_explanation_from_agent_is_used_when_consistent(self):
        from mylocalai.rumor.agent import RumorAgent
        from tests.test_rumor_agent_offline import (ScriptedModel, call_all_tools,
                                                    honest_final, stub_funcs, RUMOR_TEXT)

        agent = RumorAgent(llm=ScriptedModel(script=[call_all_tools, honest_final], delay=0),
                           funcs=stub_funcs(), use_default_llm=False, deadline_s=30)
        r = agent.analyze(RUMOR_TEXT)
        self.assertEqual(r["mode"], "agent")
        self.assertEqual(r["explanation_source"], "agent")
        self.assertIn(r["label"], r["explanation"])


class TestWebsiteAgentDimensions(unittest.TestCase):
    """F8：agent 模式记录带三维字段，前端可复用同一套渲染。"""

    def test_agent_result_carries_three_dimensions(self):
        from mylocalai.rumor.agent import RumorAgent
        from tests.test_rumor_agent_offline import (ScriptedModel, call_all_tools,
                                                    honest_final, stub_funcs, RUMOR_TEXT)

        agent = RumorAgent(llm=ScriptedModel(script=[call_all_tools, honest_final], delay=0),
                           funcs=stub_funcs(), use_default_llm=False, deadline_s=30)
        r = agent.analyze(RUMOR_TEXT)
        for key in ("emotion", "attack", "logic"):
            self.assertIn(key, r, f"agent 结果应带 {key} 字段")
        self.assertIn("emotion_tendency", r["emotion"])
        self.assertIn("aggression_level", r["attack"])
        self.assertIn("logic_relevance", r["logic"])

    def test_health_exposes_cache_instance(self):
        import mylocalai.web.app as app
        body = app.health()
        self.assertIn("instance", body["llm_cache"], "多进程部署要靠 instance 判断统计归属")
        self.assertTrue(str(body["llm_cache"]["instance"]).split(":")[0].isdigit())


class TestAgentLlmTimeout(unittest.TestCase):
    """S7：Agent 的 LLM 超时/重试与 classic 同源。"""

    def setUp(self):
        llm_client.reset_cache()
        self._saved = {k: os.environ.get(k) for k in
                       ("LLM_TIMEOUT_S", "LLM_MAX_RETRIES", "LLM_PROVIDER", "LONGCAT_API_KEY")}
        os.environ["LLM_PROVIDER"] = "longcat"
        os.environ["LONGCAT_API_KEY"] = "test-key"
        os.environ["LLM_TIMEOUT_S"] = "17"
        os.environ["LLM_MAX_RETRIES"] = "3"
        llm_client.reset_cache()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        llm_client.reset_cache()

    def test_build_llm_uses_classic_timeout_and_retries(self):
        import mylocalai.llm.adapter as agent_llm
        with mock.patch("langchain_openai.ChatOpenAI") as cls:
            agent_llm.build_llm()
        kwargs = cls.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 17)
        self.assertEqual(kwargs["max_retries"], 3)

    def test_build_llm_defaults_match_the_shared_config(self):
        import mylocalai.llm.adapter as agent_llm
        os.environ.pop("LLM_TIMEOUT_S", None)
        os.environ.pop("LLM_MAX_RETRIES", None)
        llm_client.reset_cache()
        with mock.patch("langchain_openai.ChatOpenAI") as cls:
            agent_llm.build_llm()
        kwargs = cls.call_args.kwargs
        self.assertEqual(kwargs["timeout"], llm_client.get_timeout())
        self.assertEqual(kwargs["max_retries"], llm_client.get_max_retries())

    def test_build_llm_does_not_send_thinking_flag_to_providers_without_support(self):
        import mylocalai.llm.adapter as agent_llm
        os.environ["LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "test-openai"
        llm_client.reset_cache()
        try:
            with mock.patch("langchain_openai.ChatOpenAI") as cls:
                agent_llm.build_llm()
            self.assertNotIn("extra_body", cls.call_args.kwargs,
                             "不支持关闭思维链的提供方不该收到 extra_body")
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            llm_client.reset_cache()


class TestCacheSweep(unittest.TestCase):
    """S8：过期条目在读路径上被清扫。"""

    def setUp(self):
        llm_cache.clear()
        self._saved = {k: os.environ.get(k) for k in ("LLM_CACHE_TTL_S", "LLM_CACHE_MAX")}
        os.environ["LLM_CACHE_TTL_S"] = "0.05"
        os.environ["LLM_CACHE_MAX"] = "256"

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        llm_cache.clear()

    def test_expired_entries_are_swept_on_read(self):
        llm_cache.put("k", "v")
        self.assertEqual(llm_cache.stats()["entries"], 1)
        time.sleep(0.12)
        self.assertFalse(llm_cache.get("k")[0], "过期后不该命中")
        self.assertEqual(llm_cache.stats()["entries"], 0,
                         "读路径就该把过期条目清掉，不必等下一次写入")

    def test_over_capacity_entries_are_swept_on_read(self):
        os.environ["LLM_CACHE_MAX"] = "2"
        llm_cache.clear()
        for i in range(5):
            llm_cache.put(f"k{i}", i)
        llm_cache.get("k0")          # 触发读路径清扫
        self.assertLessEqual(llm_cache.stats()["entries"], 2)


class TestDependencyBounds(unittest.TestCase):
    """G6：依赖带上限、无重复、requires-python 与解释器匹配。"""

    @staticmethod
    def _requirements(path):
        out = {}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            name = re.split(r"[<>=!~\[ ]", line, maxsplit=1)[0].strip().lower()
            if name:
                out[name] = line
        return out

    def test_core_dependencies_have_upper_bounds(self):
        core = self._requirements(ROOT / "requirements.txt")
        for name in ("langchain", "langchain-openai", "mcp", "openai", "fastapi", "uvicorn"):
            self.assertIn(name, core)
            self.assertIn("<", core[name], f"{name} 必须带版本上限：{core[name]}")

    def test_optional_local_models_stay_below_breaking_versions(self):
        optional = self._requirements(ROOT / "requirements-optional.txt")
        self.assertIn("<5", optional["transformers"], "transformers 5.x 与本地模型不兼容")
        self.assertIn("<3", optional["torch"])

    def test_no_duplicate_packages_across_requirements_files(self):
        files = [ROOT / "requirements.txt", ROOT / "requirements-optional.txt",
                 ROOT / "my_website" / "requirements.txt"]
        seen = {}
        for path in files:
            for name in self._requirements(path):
                self.assertNotIn(name, seen, f"{name} 在 {seen.get(name)} 与 {path.name} 里重复")
                seen[name] = path.name

    def test_pyproject_requires_python_matches_interpreter(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("requires-python", text)
        match = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
        self.assertIsNotNone(match)
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
        spec = SpecifierSet(match.group(1))
        self.assertIn(Version(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"),
                      spec, f"当前解释器 {sys.version.split()[0]} 应满足 {match.group(1)}")


class TestLauncherScripts(unittest.TestCase):
    """G7：start.bat / start.sh 可移植、端口可配。"""

    def test_start_bat_is_pure_ascii_crlf_with_fallbacks(self):
        raw = (ROOT / "my_website" / "start.bat").read_bytes()
        self.assertTrue(all(b < 128 for b in raw), "start.bat 必须是纯 ASCII（中文 cmd 代码页会崩）")
        self.assertEqual(raw.count(b"\n") - raw.count(b"\r\n"), 0, "start.bat 必须用 CRLF")
        text = raw.decode("ascii")
        self.assertIn("%PORT%", text, "端口应可配")
        self.assertIn('set "PYRUN="', text, "解释器应有回落逻辑")
        self.assertIn("where python", text)
        self.assertIn("set PORT=8000", text.replace('if "%PORT%"=="" set PORT=8000',
                                                    'set PORT=8000'))

    def test_start_sh_has_port_and_interpreter_overrides(self):
        text = (ROOT / "my_website" / "start.sh").read_text(encoding="utf-8")
        self.assertIn("${PORT:-8000}", text)
        self.assertIn("${PYTHON:-}", text)
        self.assertIn("--no-browser", text)
        self.assertIn("uvicorn mylocalai.web.app:app", text)

    def test_start_sh_passes_bash_syntax_check_when_bash_is_available(self):
        import shutil
        import subprocess
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("本机没有 bash，跳过语法检查")
        proc = subprocess.run([bash, "-n", str(ROOT / "my_website" / "start.sh")],
                              capture_output=True, timeout=30)
        stderr = proc.stderr.decode("utf-8", "replace")
        # 沙箱/受限环境下 msys bash 子进程启动即崩（NtCreateDirectoryObject 0xC0000022），
        # 属历史环境项而非脚本语法问题；语法检查改为跳过，与"本机没有 bash"同口径。
        if proc.returncode == 3221225794 or "NtCreateDirectoryObject" in stderr:
            self.skipTest("沙箱子进程 bash 启动崩溃，跳过语法检查")
        self.assertEqual(proc.returncode, 0, stderr)


if __name__ == "__main__":
    unittest.main()
