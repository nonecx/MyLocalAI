# -*- coding: utf-8 -*-
"""能力使用率审计工具测试（`scripts/audit_capability_usage.py`）。

覆盖：
  一、纯函数：`classify_run` 归类优先级；`summarize_tool_runs` 次数/最近一次/状态计数；
      `summarize_entries` 的 0 与 None 区分；
  二、能力清单对照：`diff_wiring` 能认出"注册但不在 TOOL_ACTIONS"（用真实包里的
      `platform_feed` 断言）与"在 TOOL_ACTIONS 但未注册"（构造假数据）；
  三、端到端只读：空库（只有 schema）→ 全 0 不抛；有数据 → 聚合正确；
      缺表 / 坏库文件 → 降级 + 中文原因不崩；运行前后库文件 mtime 与行数不变；
  四、`--json` 输出结构稳定（顶层与各节键集合冻结，`schema_version` 对得上）。

全程离线、不写仓库 `data/`：所有测试库都建在 `TemporaryDirectory` 里。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_capability_audit -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "audit_capability_usage.py"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def _load_script() -> Any:
    """按文件路径加载审计脚本（`scripts/` 不是包，不能 import scripts.xxx）。

    用独立模块名加载，避免与包内同名模块冲突；脚本本身无 import-time 副作用。
    """
    spec = importlib.util.spec_from_file_location("audit_capability_usage", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load_script()

#: 只建 schema 不灌数据的建表语句（够覆盖审计要数的十张入口表 + tool_runs）。
_EMPTY_SCHEMA = """
CREATE TABLE tool_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL DEFAULT 0,
    dossier_id INTEGER,
    tool       TEXT NOT NULL DEFAULT '',
    args       TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT '',
    degraded   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE dossiers (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT '');
CREATE TABLE analysis_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, dossier_id INTEGER NOT NULL DEFAULT 0);
CREATE TABLE batches (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE batch_items (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE subscription_runs (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE scan_runs (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE scan_reports (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE attention_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE claim_fingerprints (id INTEGER PRIMARY KEY AUTOINCREMENT);
"""


def _make_db(path: Path, schema: str = _EMPTY_SCHEMA,
             rows: List[tuple] = ()) -> None:
    """建一个测试库（只在本测试自己建，写的是 TemporaryDirectory）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(schema)
        for row in rows:
            conn.execute(
                "INSERT INTO tool_runs (session_id, tool, status, degraded, created_at)"
                " VALUES (?, ?, ?, ?, ?)", row)
        conn.commit()
    finally:
        conn.close()


class ScriptContractTest(unittest.TestCase):
    """脚本自身约定：纯函数化、只读、依赖方向。"""

    def test_script_exists_and_is_pure_io_split(self):
        """脚本存在，且统计逻辑以纯函数暴露（不靠 main 才能测）。"""
        self.assertTrue(SCRIPT_PATH.is_file(), f"缺少 {SCRIPT_PATH}")
        for name in ("classify_run", "summarize_tool_runs", "summarize_entries",
                     "diff_wiring", "build_report", "render_report", "collect",
                     "open_readonly"):
            self.assertTrue(callable(getattr(audit, name, None)), f"缺少可调用 {name}")

    def test_readonly_uri_carries_mode_ro(self):
        """只读 URI 必须显式带 mode=ro（不是"我们不会写"的口头承诺）。"""
        uri = audit._readonly_uri(Path("data/dossier.db"))
        self.assertIn("mode=ro", uri)
        self.assertTrue(uri.startswith("file:"), uri)

    def test_script_does_not_import_scripts_from_package(self):
        """依赖方向：scripts 可以 import 包内模块，包内模块不得反向 import scripts。"""
        package_src = ROOT / "src" / "mylocalai"
        offenders = []
        for path in package_src.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "audit_capability_usage" in text or "scripts.audit" in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], f"包内模块反向 import 了审计脚本：{offenders}")


class ClassifyRunTest(unittest.TestCase):
    """单条记录归类：失败 > 降级 > 成功 > 其它。"""

    def test_failed_statuses_win_over_degraded_flag(self):
        """超时/取消的记录常同时置 degraded=1：必须算失败，不能算"降级但跑完了"。"""
        for status in audit.FAILED_STATUSES:
            self.assertEqual(audit.classify_run(status, True), "failed", status)
            self.assertEqual(audit.classify_run(status, False), "failed", status)

    def test_degraded_flag_and_status(self):
        self.assertEqual(audit.classify_run("degraded", False), "degraded")
        self.assertEqual(audit.classify_run("ok", True), "degraded")

    def test_ok_and_unknown(self):
        self.assertEqual(audit.classify_run("ok", 0), "ok")
        self.assertEqual(audit.classify_run("ok", None), "ok")
        self.assertEqual(audit.classify_run("", 0), "other")
        self.assertEqual(audit.classify_run(None, None), "other")
        self.assertEqual(audit.classify_run("weird", 0), "other")


class SummarizeToolRunsTest(unittest.TestCase):
    """聚合：次数 / 最近一次 / 成功失败降级计数。"""

    def test_aggregates_counts_last_at_and_statuses(self):
        rows = [
            {"tool": "compute_heat", "status": "ok", "degraded": 0,
             "created_at": "2026-11-19T13:02:55+08:00"},
            {"tool": "compute_heat", "status": "ok", "degraded": 0,
             "created_at": "2026-11-24T21:39:12+08:00"},
            {"tool": "compute_heat", "status": "error", "degraded": 0,
             "created_at": "2026-11-20T08:00:00+08:00"},
            {"tool": "insights", "status": "degraded", "degraded": 1,
             "created_at": "2026-11-18T09:00:00+08:00"},
            {"tool": "insights", "status": "timeout", "degraded": 1,
             "created_at": "2026-11-25T10:00:00+08:00"},
        ]
        out = audit.summarize_tool_runs(rows)
        self.assertEqual(out["total"], 5)
        self.assertEqual(out["unnamed"], 0)
        heat = out["tools"]["compute_heat"]
        self.assertEqual(heat["calls"], 3)
        self.assertEqual(heat["ok"], 2)
        self.assertEqual(heat["failed"], 1)
        self.assertEqual(heat["degraded"], 0)
        # 最近一次取时间最大值，不是"最后一行"
        self.assertEqual(heat["last_at"], "2026-11-24T21:39:12+08:00")
        self.assertEqual(heat["statuses"], {"ok": 2, "error": 1})
        insights = out["tools"]["insights"]
        self.assertEqual(insights["calls"], 2)
        self.assertEqual(insights["degraded"], 1)
        self.assertEqual(insights["failed"], 1)
        self.assertEqual(insights["last_at"], "2026-11-25T10:00:00+08:00")

    def test_counts_sum_equals_calls(self):
        """四类计数之和必须等于调用次数（口径不许漏行）。"""
        rows = [
            {"tool": "a", "status": "ok", "degraded": 0, "created_at": "2026-11-01T00:00:00"},
            {"tool": "a", "status": "error", "degraded": 1, "created_at": "2026-11-02T00:00:00"},
            {"tool": "a", "status": "degraded", "degraded": 0, "created_at": "2026-11-03T00:00:00"},
            {"tool": "a", "status": "", "degraded": 0, "created_at": ""},
        ]
        out = audit.summarize_tool_runs(rows)
        bucket = out["tools"]["a"]
        self.assertEqual(sum(bucket[k] for k in audit.RUN_CLASSES), bucket["calls"])
        self.assertEqual(bucket["other"], 1)
        self.assertEqual(bucket["last_at"], "2026-11-03T00:00:00")

    def test_unnamed_rows_are_counted_but_not_attributed(self):
        """工具名为空的行计入总数但归 unnamed，不混进任何工具。"""
        out = audit.summarize_tool_runs([
            {"tool": "", "status": "ok", "degraded": 0, "created_at": "2026-11-01T00:00:00"},
            {"tool": "insights", "status": "ok", "degraded": 0,
             "created_at": "2026-11-01T00:00:00"},
        ])
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["unnamed"], 1)
        self.assertEqual(sorted(out["tools"]), ["insights"])

    def test_empty_input(self):
        out = audit.summarize_tool_runs([])
        self.assertEqual(out, {"total": 0, "unnamed": 0, "tools": {}})

    def test_bad_timestamps_do_not_raise(self):
        """坏时间戳不参与比较但不崩：能解析的仍然胜出。"""
        out = audit.summarize_tool_runs([
            {"tool": "x", "status": "ok", "degraded": 0, "created_at": "不是时间"},
            {"tool": "x", "status": "ok", "degraded": 0, "created_at": "2026-11-20T00:00:00"},
        ])
        self.assertEqual(out["tools"]["x"]["last_at"], "2026-11-20T00:00:00")


class SummarizeEntriesTest(unittest.TestCase):
    """入口计数：0（表在但没记录）与 None（没读到）必须区分。"""

    def test_zero_is_not_missing(self):
        counts: Dict[str, Any] = {table: 0 for table, _l, _c in audit.ENTRY_TABLES}
        out = audit.summarize_entries(counts)
        self.assertEqual(out["missing_tables"], [])
        self.assertEqual(out["non_empty"], [])
        self.assertEqual(out["readable_total"], len(audit.ENTRY_TABLES))

    def test_none_marks_missing_and_is_not_zero(self):
        counts: Dict[str, Any] = {table: 0 for table, _l, _c in audit.ENTRY_TABLES}
        counts["dossiers"] = None
        out = audit.summarize_entries(counts)
        self.assertEqual(out["missing_tables"], ["dossiers"])
        self.assertIsNone(out["entries"]["dossiers"]["rows"])
        self.assertFalse(out["entries"]["dossiers"]["readable"])
        self.assertTrue(out["entries"]["batches"]["readable"])

    def test_entries_carry_label_and_capability(self):
        counts = {table: 1 for table, _l, _c in audit.ENTRY_TABLES}
        out = audit.summarize_entries(counts)
        self.assertEqual(out["entries"]["dossiers"]["capability"], "claim-checking")
        self.assertIn("建档", out["entries"]["dossiers"]["label"])
        self.assertEqual(out["non_empty"], sorted(t for t, _l, _c in audit.ENTRY_TABLES))


class DiffWiringTest(unittest.TestCase):
    """能力清单对照：四类错配都要能被认出来。"""

    def test_platform_feed_is_registered_but_not_routed(self):
        """真实包：`platform_feed` 能注册但不在 router.TOOL_ACTIONS（本仓既定事实）。"""
        registerable, _modules, reason = audit.discover_registerable_tools()
        self.assertEqual(reason, "", reason)
        self.assertIn("platform_feed", registerable,
                      "反射没扫到 tools_platform.register_all —— 审计口径失效")
        routed, routed_reason = audit.discover_routed_actions()
        self.assertEqual(routed_reason, "", routed_reason)
        self.assertNotIn("platform_feed", routed,
                         "platform_feed 出现在路由枚举里了：自然语言路由结论需要重写")

        runtime, handlers, runtime_reason = audit.discover_runtime_tools()
        self.assertEqual(runtime_reason, "", runtime_reason)
        self.assertNotIn("platform_feed", runtime, "platform_feed 已接线进运行时注册表")

        wiring = audit.diff_wiring(runtime, routed, registerable, [], [], handlers)
        self.assertIn("platform_feed", wiring["registered_not_routed"])
        self.assertIn("platform_feed", wiring["registerable_not_wired"])

    def test_routed_but_not_registered_is_detected(self):
        """构造假数据：路由枚举里有、运行时注册表里没有 → 必须被认出来。"""
        wiring = audit.diff_wiring(
            runtime_tools=["insights", "deep_read"],
            routed_actions=["insights", "deep_read", "ghost_tool"],
            registerable_tools=["insights", "deep_read", "platform_feed"],
            capability_actions=["deep_read"],
            horizontal_tools=["insights"],
        )
        self.assertEqual(wiring["routed_not_registered"], ["ghost_tool"])
        self.assertEqual(wiring["registered_not_routed"], ["platform_feed"])
        self.assertEqual(wiring["routed_without_handler"], [])

    def test_routed_without_handler_is_detected(self):
        """在路由枚举且已注册，但没有可调用 handler → 无服务实现。"""
        wiring = audit.diff_wiring(
            runtime_tools=["insights", "half_baked"],
            routed_actions=["insights", "half_baked"],
            registerable_tools=["insights", "half_baked"],
            capability_actions=["insights"],
            horizontal_tools=[],
            handler_names=["insights"],          # half_baked 的 handler 不可调用
        )
        self.assertEqual(wiring["routed_without_handler"], ["half_baked"])

    def test_capability_actions_and_orphans(self):
        """能力入口不在枚举 → capability_actions_not_routed；枚举里两边都不沾 → orphan。"""
        wiring = audit.diff_wiring(
            runtime_tools=["verify_claim", "stray"],
            routed_actions=["verify_claim", "stray"],
            registerable_tools=["verify_claim", "stray"],
            capability_actions=["verify_claim", "missing_cap_action"],
            horizontal_tools=[],
        )
        self.assertEqual(wiring["capability_actions_not_routed"], ["missing_cap_action"])
        self.assertEqual(wiring["routed_orphan_actions"], ["stray"])

    def test_counts_are_sets_not_lists(self):
        """同名重复不重复计数（清单是集合语义）。"""
        wiring = audit.diff_wiring(
            runtime_tools=["a", "a"], routed_actions=["a", "a"],
            registerable_tools=["a"], capability_actions=["a"],
            horizontal_tools=[])
        self.assertEqual(wiring["counts"]["runtime"], 1)
        self.assertEqual(wiring["counts"]["routed"], 1)


class MarkUsageTest(unittest.TestCase):
    """本机调用覆盖：路由内/路由外两类"从未调用"分开列。"""

    def test_never_called_splits_routed_and_unrouted(self):
        wiring = {"registered_not_routed": ["platform_feed"]}
        usage = audit.mark_usage(wiring, ["insights"], ["insights", "deep_read"])
        self.assertEqual(usage["called_actions"], ["insights"])
        self.assertEqual(usage["never_called_routed"], ["deep_read"])
        self.assertEqual(usage["never_called_unrouted"], ["platform_feed"])
        self.assertEqual(usage["coverage"], "1/2")

    def test_called_outside_known_lists(self):
        usage = audit.mark_usage({}, ["mystery"], ["insights"])
        self.assertEqual(usage["called_outside_router"], ["mystery"])
        self.assertEqual(usage["coverage"], "0/1")

    def test_empty_lists_give_zero_coverage(self):
        usage = audit.mark_usage({}, [], [])
        self.assertEqual(usage["coverage"], "0/0")
        self.assertEqual(usage["called_actions"], [])


class ReportShapeTest(unittest.TestCase):
    """`--json` 结构稳定：键集合冻结（加键必须升 SCHEMA_VERSION）。"""

    #: 顶层键（顺序即输出顺序，但断言按集合）
    TOP_KEYS = {"schema_version", "generated_at", "db", "tool_runs", "entries",
                "wiring", "usage", "capabilities", "notes"}
    DB_KEYS = {"path", "exists", "opened_readonly", "degraded", "reason", "tables"}
    RUN_KEYS = {"total", "unnamed", "tools"}
    TOOL_KEYS = {"calls", "last_at", "ok", "failed", "degraded", "other", "statuses"}
    ENTRIES_KEYS = {"readable_total", "missing_tables", "non_empty", "entries"}
    ENTRY_KEYS = {"label", "capability", "rows", "readable"}
    WIRING_KEYS = {"counts", "routed_and_registered", "registered_not_routed",
                   "runtime_not_routed", "routed_not_registered",
                   "registerable_not_wired", "routed_without_handler",
                   "capability_actions_not_routed", "routed_orphan_actions"}
    USAGE_KEYS = {"called_actions", "never_called_routed", "never_called_unrouted",
                  "called_outside_router", "coverage"}
    CAPS_KEYS = {"total", "items"}
    CAP_KEYS = {"channel", "category", "entry_actions", "composite"}

    def _report(self) -> Dict[str, Any]:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dossier.db"
            _make_db(path)
            return audit.collect(path, now="2026-11-25T23:00:00+08:00")

    def test_top_level_and_nested_keys_are_frozen(self):
        report = self._report()
        self.assertEqual(set(report), self.TOP_KEYS)
        self.assertEqual(set(report["db"]), self.DB_KEYS)
        self.assertEqual(set(report["tool_runs"]), self.RUN_KEYS)
        self.assertEqual(set(report["entries"]), self.ENTRIES_KEYS)
        self.assertEqual(set(report["wiring"]), self.WIRING_KEYS)
        self.assertEqual(set(report["usage"]), self.USAGE_KEYS)
        self.assertEqual(set(report["capabilities"]), self.CAPS_KEYS)
        self.assertEqual(report["schema_version"], audit.SCHEMA_VERSION)

    def test_tool_bucket_and_entry_keys_are_frozen(self):
        """有数据时才出现的二级键单独锁（空库测不到）。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dossier.db"
            _make_db(path, rows=[(1, "insights", "ok", 0, "2026-11-19T13:59:18+08:00")])
            report = audit.collect(path, now="2026-11-25T23:00:00+08:00")
        bucket = report["tool_runs"]["tools"]["insights"]
        self.assertEqual(set(bucket), self.TOOL_KEYS)
        self.assertEqual(set(report["entries"]["entries"]["dossiers"]), self.ENTRY_KEYS)

    def test_json_round_trip_is_serializable(self):
        """`--json` 的输出必须能 json.dumps 且键序稳定（同一输入两次相同）。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dossier.db"
            _make_db(path)
            first = json.dumps(audit.collect(path, now="2026-11-25T23:00:00+08:00"),
                               ensure_ascii=False, sort_keys=False)
            second = json.dumps(audit.collect(path, now="2026-11-25T23:00:00+08:00"),
                                ensure_ascii=False, sort_keys=False)
        self.assertEqual(first, second, "同一输入两次输出不一致（有非确定性来源）")
        self.assertEqual(json.loads(first)["schema_version"], audit.SCHEMA_VERSION)

    def test_capability_view_keys(self):
        report = self._report()
        items = report["capabilities"]["items"]
        self.assertTrue(items, "能力注册表读不到（反射失效）")
        for cap_id, item in items.items():
            self.assertEqual(set(item), self.CAP_KEYS, cap_id)


class EmptyDbTest(unittest.TestCase):
    """空库（只有 schema、无数据）→ 全 0 且不抛。"""

    def test_empty_schema_gives_zeros_without_raising(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dossier.db"
            _make_db(path)
            report = audit.collect(path, now="2026-11-25T23:00:00+08:00")
        self.assertFalse(report["db"]["degraded"], report["db"]["reason"])
        self.assertTrue(report["db"]["opened_readonly"])
        self.assertEqual(report["tool_runs"]["total"], 0)
        self.assertEqual(report["tool_runs"]["tools"], {})
        self.assertEqual(report["entries"]["missing_tables"], [])
        self.assertEqual(report["entries"]["non_empty"], [])
        for table, item in report["entries"]["entries"].items():
            self.assertEqual(item["rows"], 0, table)

    def test_zero_row_db_still_renders(self):
        """空库也要能出人读表格（渲染不因空数据抛或漏节）。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dossier.db"
            _make_db(path)
            text = audit.render_report(audit.collect(path, now="2026-11-25T23:00:00+08:00"))
        for marker in ("[1]", "[2]", "[3]", "[4]", "[5]"):
            self.assertIn(marker, text)
        self.assertIn("没有任何工具调用记录", text)


class DegradedInputTest(unittest.TestCase):
    """缺表 / 坏库文件 → 降级输出 + 中文原因，不崩。"""

    def test_missing_db_file_degrades_with_chinese_reason(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-there.db"
            report = audit.collect(path, now="2026-11-25T23:00:00+08:00")
            self.assertFalse(path.exists(), "审计不该创建库文件")
        self.assertTrue(report["db"]["degraded"])
        self.assertFalse(report["db"]["exists"])
        self.assertFalse(report["db"]["opened_readonly"])
        self.assertIn("库文件不存在", report["db"]["reason"])
        self.assertEqual(report["tool_runs"]["total"], 0)
        self.assertIn("库文件不存在", " ".join(report["notes"]))

    def test_corrupt_file_degrades_with_chinese_reason(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.db"
            path.write_bytes("这不是一个 SQLite 库，只是一串字节".encode("utf-8") * 40)
            report = audit.collect(path, now="2026-11-25T23:00:00+08:00")
        self.assertTrue(report["db"]["degraded"])
        self.assertTrue(report["db"]["exists"])
        self.assertFalse(report["db"]["opened_readonly"])
        self.assertIn("SQLite", report["db"]["reason"])
        self.assertEqual(report["tool_runs"]["total"], 0)
        self.assertEqual(report["tool_runs"]["tools"], {})

    def test_missing_tables_are_reported_not_faked_as_zero(self):
        """库能开但入口表缺一半 → 缺的标 None，有的给真数，notes 里点名。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.db"
            conn = sqlite3.connect(str(path))
            try:
                conn.executescript("""
                    CREATE TABLE tool_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tool TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
                        degraded INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT '');
                    CREATE TABLE dossiers (id INTEGER PRIMARY KEY AUTOINCREMENT);
                """)
                conn.execute("INSERT INTO dossiers DEFAULT VALUES")
                conn.commit()
            finally:
                conn.close()
            report = audit.collect(path, now="2026-11-25T23:00:00+08:00")
        self.assertFalse(report["db"]["degraded"])
        missing = set(report["entries"]["missing_tables"])
        self.assertIn("batches", missing)
        self.assertIn("scan_reports", missing)
        self.assertNotIn("dossiers", missing)
        self.assertEqual(report["entries"]["entries"]["dossiers"]["rows"], 1)
        self.assertIsNone(report["entries"]["entries"]["batches"]["rows"])
        self.assertEqual(report["entries"]["non_empty"], ["dossiers"])

    def test_missing_tool_runs_table_is_named_in_notes(self):
        """没有 tool_runs 表 → 计数器为 0，notes 明说表不存在而不是假装没调用。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "no-runs.db"
            conn = sqlite3.connect(str(path))
            try:
                conn.execute("CREATE TABLE dossiers (id INTEGER PRIMARY KEY AUTOINCREMENT)")
                conn.commit()
            finally:
                conn.close()
            report = audit.collect(path, now="2026-11-25T23:00:00+08:00")
        self.assertEqual(report["tool_runs"]["total"], 0)
        self.assertIn("tool_runs 表不存在", " ".join(report["notes"]))

    def test_tool_runs_missing_columns_do_not_break(self):
        """老 schema 少 status/degraded 列 → 退化为 other，不整表读失败。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "old-schema.db"
            conn = sqlite3.connect(str(path))
            try:
                conn.executescript("""
                    CREATE TABLE tool_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tool TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT '');
                """)
                conn.execute("INSERT INTO tool_runs (tool, created_at) VALUES ('insights',"
                             " '2026-11-01T00:00:00')")
                conn.commit()
            finally:
                conn.close()
            report = audit.collect(path, now="2026-11-25T23:00:00+08:00")
        bucket = report["tool_runs"]["tools"]["insights"]
        self.assertEqual(bucket["calls"], 1)
        self.assertEqual(bucket["other"], 1)
        self.assertEqual(bucket["ok"], 0)

    def test_collect_never_raises_for_weird_paths(self):
        """目录当库文件、空文件都不抛（审计工具自己不能崩）。"""
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / "a-dir"
            directory.mkdir()
            empty = Path(tmp) / "empty.db"
            empty.write_bytes(b"")
            for path in (directory, empty):
                report = audit.collect(path, now="2026-11-25T23:00:00+08:00")
                self.assertEqual(report["tool_runs"]["total"], 0, str(path))
                self.assertEqual(set(report), ReportShapeTest.TOP_KEYS, str(path))


class ReadOnlyTest(unittest.TestCase):
    """脚本不改库：运行前后 mtime / 行数不变，且连接是只读的。"""

    def test_mtime_and_rows_unchanged(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dossier.db"
            _make_db(path, rows=[(1, "insights", "ok", 0, "2026-11-19T13:59:18+08:00"),
                                 (2, "compute_heat", "ok", 0, "2026-11-24T21:39:12+08:00")])
            before_mtime = path.stat().st_mtime_ns
            before_size = path.stat().st_size
            for _ in range(3):
                audit.collect(path, now="2026-11-25T23:00:00+08:00")
            after_mtime = path.stat().st_mtime_ns
            after_size = path.stat().st_size
            conn = sqlite3.connect(str(path))
            try:
                rows_after = conn.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(before_mtime, after_mtime, "审计改动了库文件 mtime")
        self.assertEqual(before_size, after_size, "审计改动了库文件大小")
        self.assertEqual(rows_after, 2, "审计改动 tool_runs 行数")

    def test_no_new_tables_are_created(self):
        """审计不建表：跑完表集合与跑前逐位一致。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dossier.db"
            _make_db(path)
            conn = sqlite3.connect(str(path))
            try:
                before = sorted(r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"))
            finally:
                conn.close()
            report = audit.collect(path, now="2026-11-25T23:00:00+08:00")
            self.assertEqual(sorted(report["db"]["tables"]), before)

    def test_connection_rejects_writes(self):
        """连接自带 query_only：即便有人拿它写，内核也拒（只读是机制不是承诺）。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dossier.db"
            _make_db(path)
            conn, reason = audit.open_readonly(path)
            self.assertEqual(reason, "")
            self.assertIsNotNone(conn)
            try:
                with self.assertRaises(sqlite3.Error):
                    conn.execute("CREATE TABLE should_not_exist (id INTEGER)")
                with self.assertRaises(sqlite3.Error):
                    conn.execute("INSERT INTO dossiers (created_at) VALUES ('x')")
            finally:
                conn.close()

    def test_no_files_created_beside_db(self):
        """只读打开不在库旁边留临时/日志文件（-wal / -shm 由写入方产生，审计不产）。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dossier.db"
            _make_db(path)
            before = sorted(p.name for p in Path(tmp).iterdir())
            audit.collect(path, now="2026-11-25T23:00:00+08:00")
            after = sorted(p.name for p in Path(tmp).iterdir())
        self.assertEqual(before, after, f"审计在库旁新增了文件：{set(after) - set(before)}")


class WiringAgainstRealPackageTest(unittest.TestCase):
    """对着真实包跑一遍端到端：口径与仓库现状一致才说明审计有效。"""

    def test_real_package_counts_match_repo_state(self):
        report = audit.collect(Path("不存在的库.db"), now="2026-11-25T23:00:00+08:00")
        counts = report["wiring"]["counts"]
        self.assertEqual(counts["routed"], 20, "router.TOOL_ACTIONS 不再是 20 个 action")
        self.assertEqual(counts["runtime"], 20, "运行时注册表不再是 20 个工具")
        self.assertEqual(counts["capability_actions"], 14, "能力入口 action 不再是 14 个")
        self.assertEqual(counts["horizontal"], 6, "横向工具不再是 6 个")
        self.assertEqual(report["capabilities"]["total"], 13, "能力注册表不再是 13 条")

    def test_real_package_has_no_routing_gap_in_router_side(self):
        """路由枚举与运行时注册表当前一一对应（platform_feed 是模块级未接线）。"""
        report = audit.collect(Path("不存在的库.db"), now="2026-11-25T23:00:00+08:00")
        self.assertEqual(report["wiring"]["routed_not_registered"], [])
        self.assertEqual(report["wiring"]["runtime_not_routed"], [])
        self.assertEqual(report["wiring"]["routed_without_handler"], [])
        self.assertEqual(report["wiring"]["capability_actions_not_routed"], [])
        self.assertIn("platform_feed", report["wiring"]["registered_not_routed"])


class CliTest(unittest.TestCase):
    """CLI 薄封装：--json 只出 JSON、默认出表格、返回码 0。"""

    def test_json_mode_emits_parseable_json(self):
        import contextlib
        import io
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dossier.db"
            _make_db(path)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = audit.main(["--db", str(path), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["schema_version"], audit.SCHEMA_VERSION)
        self.assertEqual(set(payload), ReportShapeTest.TOP_KEYS)

    def test_table_mode_emits_headers(self):
        import contextlib
        import io
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dossier.db"
            _make_db(path)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = audit.main(["--db", str(path)])
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("能力使用率审计", text)
        self.assertIn("注册但不在路由枚举", text)

    def test_db_path_env_is_honoured(self):
        """DOSSIER_DB_PATH 覆盖默认路径（与包内 default_db_path 同源）。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "env.db"
            _make_db(path)
            previous = os.environ.get("DOSSIER_DB_PATH")
            os.environ["DOSSIER_DB_PATH"] = str(path)
            try:
                resolved = audit.resolve_db_path(None)
            finally:
                if previous is None:
                    os.environ.pop("DOSSIER_DB_PATH", None)
                else:
                    os.environ["DOSSIER_DB_PATH"] = previous
        self.assertEqual(str(resolved), str(path))

    def test_default_path_is_repo_data_dir(self):
        """无 --db 无 env 时落到包的 data/dossier.db（不是脚本自己拍一个）。"""
        previous = os.environ.pop("DOSSIER_DB_PATH", None)
        try:
            resolved = audit.resolve_db_path(None)
        finally:
            if previous is not None:
                os.environ["DOSSIER_DB_PATH"] = previous
        self.assertEqual(resolved.name, "dossier.db")
        self.assertEqual(resolved.parent.name, "data")


if __name__ == "__main__":
    unittest.main(verbosity=2)