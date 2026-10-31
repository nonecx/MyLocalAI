# -*- coding: utf-8 -*-
"""SSE 事件协议测试（工作指导 §5.3 / §7.2）：封闭枚举、帧格式、线程安全、取消位。

events.py 是事件名/帧格式的唯一权威（web 层 chat.js 的 SSE_EVENTS 与
tests/test_website_chat.py 都以它为准），这里锁四件事：
  1. 事件名封闭：枚举外名字当场拒绝（协议外事件进不了流）；
  2. format_sse 帧格式逐字节稳定（`event: <name>\\ndata: <json>\\n\\n`，中文不转义）；
  3. EventStream 多线程 emit 无丢失（loop 工作线程 + drain 协程两端读写的现实形状）；
  4. snapshot 顺序稳定、返回副本（drain 循环靠"seen 指针 + snapshot"增量吐帧）。
全离线，无网络、无 LLM。
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.events import (  # noqa: E402
    EVENT_NAMES,
    EventStream,
    format_sse,
    make_event,
)

#: §5.3 协议全集（工作指导原文的事件清单，events.py 必须全覆盖）
PROTOCOL_EVENTS = {"meta", "status", "tool_start", "scan", "progress",
                   "card", "narrative", "tool_end", "done", "error"}


class TestMakeEvent(unittest.TestCase):
    def test_rejects_unknown_name(self):
        """枚举外事件名当场 ValueError：协议外事件（如伪造的 tool_event）进不了流。"""
        for bad in ("message", "tool_event", "card_v2", "", "CARD"):
            with self.assertRaises(ValueError, msg=bad):
                make_event(bad, foo=1)

    def test_payload_passthrough(self):
        """载荷键原样保留，event 名固定在 "event" 键上。"""
        evt = make_event("progress", component="emotion", done=1, total=6)
        self.assertEqual(evt["event"], "progress")
        self.assertEqual(evt["component"], "emotion")
        self.assertEqual((evt["done"], evt["total"]), (1, 6))

    def test_all_protocol_events_accepted(self):
        """§5.3 的每个事件名都能构造（协议全集可用，不是只开了半扇门）。"""
        for name in PROTOCOL_EVENTS:
            self.assertEqual(make_event(name)["event"], name)


class TestFormatSse(unittest.TestCase):
    def test_frame_shape_exact(self):
        """帧格式逐字节：`event: <name>\\ndata: <json>\\n\\n`（SSE 规范的两个换行收尾）。"""
        frame = format_sse({"event": "done", "cost": {"requests": 3, "tokens": 456}})
        self.assertEqual(
            frame,
            'event: done\ndata: {"cost": {"requests": 3, "tokens": 456}}\n\n')

    def test_unicode_not_escaped(self):
        """ensure_ascii=False：中文原样进帧（前端不用再解一层 \\uXXXX）。"""
        frame = format_sse(make_event("narrative", delta="证据只支持以下判断。"))
        self.assertIn("证据只支持以下判断。", frame)
        self.assertNotIn("\\u", frame)

    def test_missing_event_key_defaults_message(self):
        """缺 event 键的裸 dict 按 SSE 惯例落 "message"（不崩）。"""
        self.assertTrue(format_sse({"a": 1}).startswith("event: message\n"))


class TestEventStream(unittest.TestCase):
    def test_snapshot_order_preserved(self):
        """emit 顺序 = snapshot 顺序（drain 的 seen 指针语义依赖这一点）。"""
        es = EventStream()
        for name in ("meta", "status", "tool_start", "scan", "progress",
                     "card", "narrative", "tool_end", "done"):
            es.emit(name)
        self.assertEqual([e["event"] for e in es.snapshot()],
                         ["meta", "status", "tool_start", "scan", "progress",
                          "card", "narrative", "tool_end", "done"])

    def test_snapshot_returns_copy(self):
        """snapshot 返回列表副本：旧快照不受后续 emit 影响（drain 增量吐帧的前提）。"""
        es = EventStream()
        es.emit("meta", session_id=1)
        snap = es.snapshot()
        es.emit("done", cost={})
        self.assertEqual(len(snap), 1)          # 旧快照停在拍快照那一刻
        self.assertEqual(len(es.snapshot()), 2)  # 新快照继续增长
        self.assertEqual([e["event"] for e in snap], ["meta"])

    def test_multithread_emit_no_loss(self):
        """8 线程 × 50 次 emit 无丢失（loop 线程 emit / drain 读的真实并发形状）。"""
        es = EventStream()
        threads, per_thread, n = 8, 50, 8 * 50
        lock = threading.Barrier(threads)

        def worker(tid: int) -> None:
            lock.wait()
            for i in range(per_thread):
                es.emit("narrative", delta=f"t{tid}-{i}")

        ts = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        snap = es.snapshot()
        self.assertEqual(len(snap), n)
        # 每条 delta 都完整可辨（无交叉写坏）
        deltas = {e["delta"] for e in snap}
        self.assertEqual(len(deltas), n)

    def test_request_cancel_flag(self):
        """request_cancel 置位后 cancelled 为 True（SSE 断开 → loop 协作取消的开关）。"""
        es = EventStream()
        es.emit("meta", session_id=1)
        self.assertFalse(es.cancelled)
        es.request_cancel()
        self.assertTrue(es.cancelled)
        # 取消位不影响已 emit 事件的读取
        self.assertEqual(es.snapshot()[0]["event"], "meta")


class TestProtocolCoverage(unittest.TestCase):
    def test_event_names_superset_of_protocol(self):
        """EVENT_NAMES ⊇ §5.3 全集：协议里的每个事件都有封闭枚举位。"""
        self.assertTrue(PROTOCOL_EVENTS.issubset(set(EVENT_NAMES)),
                        f"协议事件缺失: {PROTOCOL_EVENTS - set(EVENT_NAMES)}")

    def test_event_names_is_closed_tuple(self):
        """EVENT_NAMES 是封闭元组（新增事件必须改协议，而不是随手 emit）。"""
        self.assertIsInstance(EVENT_NAMES, tuple)
        self.assertEqual(len(EVENT_NAMES), len(set(EVENT_NAMES)))


if __name__ == "__main__":
    unittest.main()
