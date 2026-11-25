# -*- coding: utf-8 -*-
"""巡检调度器与 CLI 测试（P5 + v12 报告透传/CLI 报告段）：启停、单飞、取消传播、
规则热更新、退出码、status 的 last_scan.report 透传、--once 的报告段输出。"""

from __future__ import annotations

import io
import json
import sys
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.platform.__main__ import main as cli_main  # noqa: E402
from mylocalai.platform import scheduler as sched_mod  # noqa: E402
from mylocalai.platform.scheduler import PlatformScheduler  # noqa: E402

OK_SUMMARY = {"scan_id": 1, "status": "completed",
              "counters": {"queries": 1, "candidates": 2, "verified": 1,
                           "deduped": 0, "degraded": 0},
              "entries": [{"title": "某话题", "url": "https://a.example.com/1",
                           "e_score": 44.2, "risk_level": "high",
                           "verdict_label": "疑似谣言", "risk_score": 62}],
              "queries": [], "error": "", "warnings": []}

RULES_ON = ({"enabled": True, "scan_interval_s": 900}, [])
RULES_OFF = ({"enabled": False, "scan_interval_s": 900}, [])
#: 快速循环专用规则（间隔 0 + 把模块下限 Patch 到 0，测试里几毫秒一轮）。
RULES_FAST = ({"enabled": True, "scan_interval_s": 0}, [])


class StubRunner:
    """记录调用的 stub；gate 事件可让 scan 阻塞以测单飞/取消。"""

    def __init__(self, summary=None, gate=None):
        self.summary = summary or OK_SUMMARY
        self.gate = gate
        self.calls = []
        self.cancel_seen = []

    def scan(self, *, queries=None, cancel_check=None):
        self.calls.append(queries)
        if self.gate is not None:
            self.gate.wait(2.0)
        self.cancel_seen.append(bool(cancel_check and cancel_check()))
        return dict(self.summary)


def _wait_for(predicate, timeout=2.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestSchedulerLifecycle(unittest.TestCase):
    def test_start_disabled_is_noop(self):
        runner = StubRunner()
        sched = PlatformScheduler(runner=runner, rules_loader=lambda: RULES_OFF)
        self.assertFalse(sched.start())
        self.assertFalse(_wait_for(lambda: runner.calls, 0.2))
        status = sched.status()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["running"])

    def test_start_runs_immediately_then_stops_cleanly(self):
        runner = StubRunner()
        sched = PlatformScheduler(runner=runner, rules_loader=lambda: RULES_ON)
        self.assertTrue(sched.start())
        self.assertTrue(_wait_for(lambda: len(runner.calls) >= 1))
        self.assertTrue(sched.stop())
        status = sched.status()
        self.assertFalse(status["running"])
        self.assertEqual(status["last_scan"]["status"], "completed")
        calls_after_stop = len(runner.calls)
        self.assertFalse(_wait_for(lambda: len(runner.calls) > calls_after_stop, 0.3))

    def test_interval_loop_scans_repeatedly(self):
        runner = StubRunner()
        with mock.patch.object(sched_mod, "_MIN_INTERVAL_S", 0):
            sched = PlatformScheduler(runner=runner,
                                      rules_loader=lambda: RULES_FAST)
            sched.start()
            self.assertTrue(_wait_for(lambda: len(runner.calls) >= 3, 3.0))
            sched.stop()
            self.assertTrue(runner.calls)

    def test_rules_reloaded_each_round(self):
        """运行中把开关关掉：循环空转不再巡检（热更新，无需重启）。"""
        runner = StubRunner()
        state = {"n": 0}

        def loader():
            state["n"] += 1
            return RULES_FAST if state["n"] <= 1 else RULES_OFF

        with mock.patch.object(sched_mod, "_MIN_INTERVAL_S", 0):
            sched = PlatformScheduler(runner=runner, rules_loader=loader)
            sched.start()
            self.assertTrue(_wait_for(lambda: len(runner.calls) >= 1))
            import time
            time.sleep(0.2)                                # 给关开关后的循环几轮时间
            sched.stop()
            self.assertEqual(len(runner.calls), 1)         # 关开关后不再跑

    def test_stop_cancels_in_flight_scan(self):
        gate = threading.Event()
        runner = StubRunner(gate=gate)
        sched = PlatformScheduler(runner=runner, rules_loader=lambda: RULES_ON)
        sched.start()
        self.assertTrue(_wait_for(lambda: runner.calls))   # 首轮扫描已开始并阻塞
        self.assertTrue(sched.stop())
        self.assertTrue(runner.cancel_seen and runner.cancel_seen[0],
                        "停止时在跑的扫描必须看到取消信号")
        gate.set()

    def test_trigger_async_single_flight(self):
        gate = threading.Event()
        runner = StubRunner(gate=gate)
        sched = PlatformScheduler(runner=runner, rules_loader=lambda: RULES_ON)
        sched.start()
        self.assertTrue(_wait_for(lambda: runner.calls))   # 调度扫描占用锁
        self.assertFalse(sched.trigger_async())            # 单飞：不叠加
        gate.set()
        self.assertTrue(_wait_for(lambda: sched.trigger_async()))   # 空闲后可触发
        sched.stop()

    def test_scan_exception_never_escapes(self):
        class BoomRunner:
            def scan(self, *, queries=None, cancel_check=None):
                raise RuntimeError("boom")

        sched = PlatformScheduler(runner=BoomRunner(), rules_loader=lambda: RULES_ON)
        summary = sched._run_scan(None)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("boom", summary["error"])
        self.assertEqual(sched.status()["last_scan"]["status"], "failed")

    def test_status_shape(self):
        sched = PlatformScheduler(runner=StubRunner(), rules_loader=lambda: RULES_ON)
        status = sched.status()
        for key in ("enabled", "interval_s", "running", "scan_in_progress",
                    "last_scan", "last_error", "rule_warnings"):
            self.assertIn(key, status)
        self.assertEqual(status["interval_s"], 900)

    def test_status_report_passthrough(self):
        """summary 带 report：status().last_scan.report 透传，且只带
        title/llm_status 两个标量（payload 不背进状态响应）。"""
        summary = dict(OK_SUMMARY,
                       report={"title": "第 1 轮巡检报告", "llm_status": "ok",
                               "payload": {"summary": "不该进状态响应",
                                           "counts": {"queries": 1}}})
        sched = PlatformScheduler(runner=StubRunner(summary=summary),
                                  rules_loader=lambda: RULES_ON)
        sched._run_scan(None)
        last = sched.status()["last_scan"]
        self.assertEqual(last["report"],
                         {"title": "第 1 轮巡检报告", "llm_status": "ok"})

    def test_status_without_report_has_no_key(self):
        """summary 无 report（早退路径/技能不可用）：不加该键——前端据此
        区分"没有报告"与"有报告"，不拿空对象冒充。"""
        sched = PlatformScheduler(runner=StubRunner(),
                                  rules_loader=lambda: RULES_ON)
        sched._run_scan(None)
        self.assertNotIn("report", sched.status()["last_scan"])

    def test_trigger_async_rapid_double_trigger_never_overlaps(self):
        """单飞的 check-then-act 窗口（W1 审计 2026-11-22）：trigger_async 先
        locked() 判断、后由线程持锁（scheduler.py trigger_async → _run_scan），
        两次手动触发都可能在窗口内拿到 started=True——"不排队不叠加"在该窗口
        内并不成立（第二条会排队串行跑）。本用例锁定一个在此缺口下仍必须成立
        的不变量：**两次扫描绝不并发重叠**——日志序必须是 start/end 交替，
        绝不出现连续两个 start（_scan_lock 的串行保证）。全 stub runner，
        不联网、不建 store。"""
        import time
        gate = threading.Event()
        log = []
        log_lock = threading.Lock()

        class GatedRunner:
            def scan(self, *, queries=None, cancel_check=None):
                with log_lock:
                    log.append("start")
                gate.wait(5.0)                 # 第一次扫描卡住：放大窗口
                with log_lock:
                    log.append("end")
                return dict(OK_SUMMARY)

        sched = PlatformScheduler(runner=GatedRunner(), rules_loader=lambda: RULES_ON)
        first = sched.trigger_async()
        second = sched.trigger_async()         # 第一条尚未持锁 → 窗口内可能也是 True
        self.assertTrue(first)
        gate.set()
        # 等所有已受理的扫描收尾（受理几条就收尾几条）
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with log_lock:
                if log.count("end") == log.count("start"):
                    break
            time.sleep(0.02)
        with log_lock:
            self.assertTrue(log, "至少第一条扫描应已起跑")
            self.assertEqual(log.count("end"), log.count("start"))
            for prev, cur in zip(log, log[1:]):
                self.assertFalse(prev == "start" and cur == "start",
                                 f"出现并发扫描（连续两个 start）：{log}")
            self.assertLessEqual(len(log), 4)  # 最多两次扫描，不无限叠加


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run_cli(self, argv, runner=None, scheduler=None):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(argv, runner=runner, scheduler=scheduler)
        return code, buf.getvalue()

    def test_once_ok_exit_zero(self):
        code, out = self._run_cli(["--once"], runner=StubRunner())
        self.assertEqual(code, 0)
        self.assertIn("巡检结果", out)
        self.assertIn("深查 1", out)

    def test_once_failed_exit_one(self):
        code, out = self._run_cli(
            ["--once"], runner=StubRunner(summary=dict(OK_SUMMARY, status="failed",
                                                       error="external_processing_not_authorized")))
        self.assertEqual(code, 1)
        self.assertIn("失败", out)

    def test_once_skipped_prints_authorization_hint(self):
        code, out = self._run_cli(
            ["--once"], runner=StubRunner(summary=dict(OK_SUMMARY, status="skipped",
                                                       error="external_processing_not_authorized")))
        self.assertEqual(code, 0)                          # skipped 是合法状态
        self.assertIn("授权", out)

    def test_once_json_output(self):
        code, out = self._run_cli(["--once", "--json"], runner=StubRunner())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["counters"]["verified"], 1)

    def test_once_passes_queries(self):
        runner = StubRunner()
        self._run_cli(["--once", "--query", "话题A", "--query", "话题B"], runner=runner)
        self.assertEqual(runner.calls, [["话题A", "话题B"]])

    def test_once_prints_report_section(self):
        """--once 报告段（llm_status=ok）：标题/摘要/话题要点/分析前 3 条
        （带条目 id）/风险/跟进都打印；第 4 条分析不打印（只述前三）。"""
        report = {
            "scan_id": 1, "as_of": "2026-11-24T22:00:00+08:00",
            "scan_status": "completed", "llm_status": "ok",
            "title": "第 1 轮巡检报告", "summary": "本轮覆盖 1 个话题。",
            "counts": {"queries": 1, "candidates": 2, "verified": 1},
            "topic_notes": [{"query": "话题A", "note": "热度上升"}],
            "analysis": [{"point": "要点一", "entry_ids": [3, 4]},
                         {"point": "要点二", "entry_ids": []},
                         {"point": "要点三", "entry_ids": []},
                         {"point": "第四条不该打印", "entry_ids": []}],
            "risks": "存在传播风险", "followups": ["继续观察"],
            "gaps": [], "errors": []}
        code, out = self._run_cli(["--once"],
                                  runner=StubRunner(summary=dict(OK_SUMMARY,
                                                                 report=report)))
        self.assertEqual(code, 0)
        self.assertIn("巡检报告", out)
        self.assertIn("第 1 轮巡检报告", out)
        self.assertIn("本轮覆盖 1 个话题。", out)
        self.assertIn("话题A：热度上升", out)
        self.assertIn("要点一（条目 3, 4）", out)
        self.assertNotIn("第四条不该打印", out)
        self.assertIn("存在传播风险", out)
        self.assertIn("继续观察", out)

    def test_once_prints_report_degraded_reason(self):
        """llm_status 非 ok：先如实打印未生成叙述性报告的原因，再打印代码
        兜底的标题/摘要；叙事区（analysis 等）不打印。"""
        report = {"title": "第 2 轮巡检报告", "summary": "代码汇总兜底。",
                  "llm_status": "budget_exhausted",
                  "analysis": [{"point": "叙事区不该打印", "entry_ids": [1]}],
                  "topic_notes": [{"query": "q", "note": "n"}]}
        code, out = self._run_cli(["--once"],
                                  runner=StubRunner(summary=dict(OK_SUMMARY,
                                                                 report=report)))
        self.assertEqual(code, 0)
        self.assertIn("本轮未生成叙述性报告（预算耗尽）", out)
        self.assertIn("以下为代码汇总", out)
        self.assertIn("第 2 轮巡检报告", out)
        self.assertIn("代码汇总兜底。", out)
        self.assertNotIn("叙事区不该打印", out)
        self.assertNotIn("q：n", out)

    def test_once_without_report_does_not_crash(self):
        """summary 无 report 键（早退路径/技能不可用）：报告段整段跳过，
        其余输出不变、不炸。"""
        code, out = self._run_cli(["--once"], runner=StubRunner())
        self.assertEqual(code, 0)
        self.assertNotIn("巡检报告", out)
        self.assertIn("巡检结果", out)

    def test_status_command(self):
        stub_sched = mock.Mock()
        stub_sched.status.return_value = {
            "enabled": False, "interval_s": 900, "running": False,
            "scan_in_progress": False, "last_scan": {}, "last_error": "",
            "rule_warnings": []}
        code, out = self._run_cli(["--status"], scheduler=stub_sched)
        self.assertEqual(code, 0)
        self.assertIn("调度器", out)

    def test_serve_disabled_prints_hint(self):
        stub_sched = mock.Mock()
        stub_sched.start.return_value = False
        code, out = self._run_cli(["--serve"], scheduler=stub_sched)
        self.assertEqual(code, 0)
        self.assertIn("enabled=false", out)

    def test_serve_starts_and_stops(self):
        stub_sched = mock.Mock()
        stub_sched.start.return_value = True
        stub_sched.status.return_value = {"running": False}
        code, out = self._run_cli(["--serve"], scheduler=stub_sched)
        self.assertEqual(code, 0)
        stub_sched.start.assert_called_once()
        stub_sched.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
