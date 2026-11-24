# -*- coding: utf-8 -*-
"""会话域三表读写测试（工作指导 §4.4 / §7.2）：sessions / messages / tool_runs。

SessionStore 只做忠实的表读写（裁剪/摘要在 context.py），这里锁：
  - 三表 CRUD 与白名单校验；
  - messages 按 id 正序、limit 取"最近 N 条仍正序"（上下文构建器依赖这一语义）；
  - tool_runs 的 args JSON 往返、degraded bool 化、start→finish 生命周期；
  - 归档会话默认不出现在 list_sessions（UI 默认视图不含历史归档）；
  - 并发 add_message 无丢失（RLock + 共享连接，WAL 单写者）。
  - 标题派生 derive_session_title：截断 24 + 乱码防护（2026-11-19 数据事故，
    web /api/chat 与 MCP assistant_chat 共用这一个口径）。
全部离线临时库；时间敏感断言用可控 _now 桩，不靠 sleep（避免秒边界抖动）。
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mylocalai.assistant.session_store as ss_mod  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402
from mylocalai.assistant.session_store import SessionStore  # noqa: E402

PAST = "2000-01-01T00:00:00+08:00"  # 可控历史时间：把"更新过没有"变成确定性断言


class SessionStoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = SessionStore(Path(self.tmp.name) / "chat.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class TestSessions(SessionStoreTestBase):
    def test_create_get_rename(self):
        sid = self.store.create_session("第一次对话")
        s = self.store.get_session(sid)
        self.assertEqual(s["title"], "第一次对话")
        self.assertEqual(s["archived"], 0)
        self.assertIsNone(self.store.get_session(424242))
        # rename 也刷新 updated_at（用可控时间把"刷新过没有"变成确定性断言）
        with mock.patch.object(ss_mod, "_now", return_value=PAST):
            sid2 = self.store.create_session("旧时间")
        self.assertEqual(self.store.get_session(sid2)["updated_at"], PAST)
        self.store.rename_session(sid2, "改名了")
        s2 = self.store.get_session(sid2)
        self.assertEqual(s2["title"], "改名了")
        self.assertNotEqual(s2["updated_at"], PAST)

    def test_list_sessions_newest_first_and_limit(self):
        """列表按 updated_at 倒序（同时刻按 id 倒序）；limit 只截前 N 条。"""
        ids = [self.store.create_session(f"s{i}") for i in range(3)]
        listed = [s["id"] for s in self.store.list_sessions()]
        self.assertEqual(listed, list(reversed(ids)))
        self.assertEqual([s["id"] for s in self.store.list_sessions(limit=2)],
                         list(reversed(ids))[:2])

    def test_archived_hidden_by_default(self):
        """归档会话默认不出现在列表；include_archived=True 才可见；取消归档即恢复。"""
        a, b = self.store.create_session("要归档"), self.store.create_session("活跃")
        self.store.set_archived(a, True)
        self.assertEqual([s["id"] for s in self.store.list_sessions()], [b])
        self.assertEqual({s["id"] for s in self.store.list_sessions(include_archived=True)},
                         {a, b})
        self.assertEqual(self.store.get_session(a)["archived"], 1)
        self.store.set_archived(a, False)
        self.assertIn(a, [s["id"] for s in self.store.list_sessions()])


class TestMessages(SessionStoreTestBase):
    def test_role_and_kind_whitelist(self):
        """role/kind 走封闭词表（§4.4）：非法值当场拒绝，不给脏数据进库的机会。"""
        sid = self.store.create_session("t")
        mid = self.store.add_message(sid, "user", "自来水被下毒了？")
        self.assertIsInstance(mid, int)
        self.store.add_message(sid, "assistant", "**结论**：…", kind="card")
        self.store.add_message(sid, "system_note", "工具 verify_claim 开始", kind="tool_event")
        self.store.add_message(sid, "assistant", "我不能帮忙写…", kind="refusal")
        with self.assertRaises(AssertionError):
            self.store.add_message(sid, "bot", "非法角色")
        with self.assertRaises(AssertionError):
            self.store.add_message(sid, "user", "x", kind="emoji")

    def test_messages_order_and_recent_limit(self):
        """全量按 id 正序；limit=N 取最近 N 条但仍正序返回（上下文构建器依赖）；
        limit 比条数大 → 全部返回；limit 下限截到 1。"""
        sid = self.store.create_session("t")
        for i in range(5):
            self.store.add_message(sid, "user" if i % 2 == 0 else "assistant", f"m{i}")
        full = self.store.messages(sid)
        self.assertEqual([m["content"] for m in full], [f"m{i}" for i in range(5)])
        recent = self.store.messages(sid, limit=3)
        self.assertEqual([m["content"] for m in recent], ["m2", "m3", "m4"])
        self.assertEqual([m["id"] for m in self.store.messages(sid, limit=99)],
                         [m["id"] for m in full])
        self.assertEqual([m["content"] for m in self.store.messages(sid, limit=0)], ["m4"])
        empty = self.store.create_session("空会话")
        self.assertEqual(self.store.messages(empty), [])

    def test_add_message_touches_session(self):
        """add_message 必须刷新会话的 updated_at（会话列表按它排序）。"""
        with mock.patch.object(ss_mod, "_now", return_value=PAST):
            sid = self.store.create_session("t")
            self.assertEqual(self.store.get_session(sid)["updated_at"], PAST)
        self.store.add_message(sid, "user", "你好")
        s = self.store.get_session(sid)
        self.assertNotEqual(s["updated_at"], PAST)
        self.assertNotEqual(self.store.messages(sid)[0]["created_at"], PAST)

    def test_touch_session_updates_updated_at(self):
        with mock.patch.object(ss_mod, "_now", return_value=PAST):
            sid = self.store.create_session("t")
        self.store.touch_session(sid)
        self.assertNotEqual(self.store.get_session(sid)["updated_at"], PAST)


class TestToolRuns(SessionStoreTestBase):
    def test_run_lifecycle_and_args_roundtrip(self):
        """start_run(status=running) → finish_run → get_run/runs_for_session：
        args 嵌套 dict 经 JSON 往返不失真；dossier_id 有外键约束（必须是真实档案），
        且重复 finish 不丢已写的 dossier_id（COALESCE 语义）。"""
        sid = self.store.create_session("t")
        # 会话域与档案域同库：tool_runs.dossier_id → dossiers(id) 外键，须先有真档案
        dstore = DossierStore(Path(self.tmp.name) / "chat.db")
        did = dstore.create_dossier("自来水核查", "听说自来水被下毒了")
        dstore.close()
        args = {"text": "自来水被下毒？", "limit": 3,
                "opts": {"engines": ["360", "bing"], "deep": True}}
        run_id = self.store.start_run(sid, "verify_claim", args)
        run = self.store.get_run(run_id)
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["args"], args)                 # JSON 往返
        self.assertIsNone(run["dossier_id"])
        self.store.finish_run(run_id, "ok", degraded=True, duration_s=1.25,
                              requests=4, tokens=256, dossier_id=did)
        run = self.store.get_run(run_id)
        self.assertEqual(run["status"], "ok")
        self.assertTrue(run["degraded"])                    # DB 里是 0/1，真值判定成立
        self.assertEqual(run["duration_s"], 1.25)
        self.assertEqual((run["requests"], run["tokens"], run["dossier_id"]), (4, 256, did))
        # 再 finish 一次：dossier_id 走 COALESCE 不丢已存的值。注意 degraded 是
        # "最后一次 finish 说了算"（没有 COALESCE）——loop 层对同一 run 只应 finish 一次，
        # 这里显式传 degraded=True 与第一次保持一致。
        self.store.finish_run(run_id, "ok", degraded=True)
        run = self.store.get_run(run_id)
        self.assertEqual(run["dossier_id"], did)
        self.assertTrue(run["degraded"])
        # runs_for_session 是 loop 层直接消费的口子：degraded 必须 bool 化
        runs = self.store.runs_for_session(sid)
        self.assertEqual(len(runs), 1)
        self.assertIsInstance(runs[0]["degraded"], bool)
        self.assertTrue(runs[0]["degraded"])
        self.assertEqual(runs[0]["args"], args)
        self.assertIsNone(self.store.get_run(999))

    def test_runs_isolated_per_session(self):
        """会话间不串台；degraded=False 读回也必须是 bool 的 False。"""
        s1, s2 = self.store.create_session("a"), self.store.create_session("b")
        r1 = self.store.start_run(s1, "verify_claim", {"q": 1})
        self.store.start_run(s2, "compute_heat", {})
        self.store.finish_run(r1, "ok")
        self.assertEqual(len(self.store.runs_for_session(s1)), 1)
        self.assertEqual(self.store.runs_for_session(s1)[0]["tool"], "verify_claim")
        self.assertEqual(len(self.store.runs_for_session(s2)), 1)
        self.assertEqual(self.store.runs_for_session(s2)[0]["tool"], "compute_heat")
        self.assertIs(self.store.runs_for_session(s1)[0]["degraded"], False)


class TestDeriveSessionTitle(unittest.TestCase):
    """会话标题派生（web /api/chat 与 MCP assistant_chat 的唯一口径）。

    2026-11-19 数据事故：四条历史会话标题被 mangling 成字面 "?"（每非 ASCII
    字符一个 "?"）。管道已不复现，但 create_session 的调用点必须都经这个纯函数，
    否则乱码标题会再次写进列表。这里锁：正常截断、乱码判空、空输入、边界占比。
    """

    def test_chinese_message_truncated_to_24(self):
        title = "自来水被人下毒了？官方为什么不敢通报，速转全家看看"
        self.assertEqual(ss_mod.derive_session_title(title), title[:24])
        self.assertEqual(len(ss_mod.derive_session_title(title)), 24)

    def test_custom_limit_is_honored(self):
        self.assertEqual(ss_mod.derive_session_title("一二三四五六", limit=3), "一二三")

    def test_pure_question_marks_are_treated_as_mojibake(self):
        """全是 '?' → 乱码产物 → 空串（前端显示「（未命名会话）」，用户可自助改名）。"""
        self.assertEqual(ss_mod.derive_session_title("????????????????????????"), "")
        self.assertEqual(ss_mod.derive_session_title("?"), "")

    def test_half_question_marks_is_boundary_and_counts_as_mojibake(self):
        """占比恰好一半即判乱码（>= 口径，不含"过半才算"的侥幸）。"""
        self.assertEqual(ss_mod.derive_session_title("??"), "")
        self.assertEqual(ss_mod.derive_session_title("a?"), "")     # 1/2：边界
        self.assertEqual(ss_mod.derive_session_title("ab?"), "ab?")  # 1/3：正常保留

    def test_empty_or_blank_input_is_empty_title(self):
        self.assertEqual(ss_mod.derive_session_title(""), "")
        self.assertEqual(ss_mod.derive_session_title(None), "")
        self.assertEqual(ss_mod.derive_session_title("   \n\t "), "")

    def test_mixed_chinese_english_is_kept(self):
        """中英混合的正常消息一个字符都不丢（乱码防护只盯 '?' 占比）。"""
        text = "AI 裁员传闻是真的吗？官方回应来了"
        self.assertEqual(ss_mod.derive_session_title(text), text[:24])
        self.assertNotIn("?", ss_mod.derive_session_title("API 用法求助：怎么查档案"))

    def test_non_string_input_is_stringified(self):
        """调用点可能传来非字符串（容错口径：str() 后再派生，不抛）。"""
        self.assertEqual(ss_mod.derive_session_title(12345), "12345")
        self.assertEqual(ss_mod.derive_session_title(None), "")

    def test_whitespace_is_stripped_before_limit(self):
        """先 strip 再截断：开头的空白不占 24 字额度。"""
        self.assertEqual(ss_mod.derive_session_title("   abcd   "), "abcd")

    def test_exported_in_all(self):
        self.assertIn("derive_session_title", ss_mod.__all__)


class TestConcurrency(SessionStoreTestBase):
    def test_concurrent_add_message_no_loss(self):
        """4 线程 × 10 条并发 add_message：RLock 生效 → 40 条全在、id 唯一、内容不串。"""
        sid = self.store.create_session("并发")
        errors: list = []

        def worker(idx: int) -> None:
            try:
                for j in range(10):
                    self.store.add_message(sid, "user", f"w{idx}-m{j}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        msgs = self.store.messages(sid)
        self.assertEqual(len(msgs), 40)
        self.assertEqual(len({m["id"] for m in msgs}), 40)
        expected = sorted(f"w{i}-m{j}" for i in range(4) for j in range(10))
        self.assertEqual(sorted(m["content"] for m in msgs), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
