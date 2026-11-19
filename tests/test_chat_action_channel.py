# -*- coding: utf-8 -*-
"""结构化 action 通道测试：POST /api/chat 的 action 字段 + AssistantLoop.handle(action=...)。

覆盖（全离线：路由/叙述/公式服务全部 stub，会话与档案共用临时 SQLite）：
  - action=compute_heat：跳过路由模型（route_llm 零调用）、事件协议与顺序不变、出 heat 卡；
  - action=insights：出 insights 卡（kind=patterns）；
  - 未知工具 / 写工具 / 坏参数 / 坏形状 → 400 {"detail": ...}，不进 SSE、不执行工具；
  - 无 action 时行为与改动前一致（仍走路由模型，事件序列与 action 版逐位一致）；
  - 通道永远不放行写工具：注册表里所有 permission="write" 的工具逐个断言被拒。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))

import mylocalai.assistant as assistant_pkg  # noqa: E402
from mylocalai.assistant.events import EVENT_NAMES  # noqa: E402
from mylocalai.assistant.loop import AssistantLoop  # noqa: E402
from mylocalai.assistant.registry import ToolNotAllowed  # noqa: E402
from mylocalai.assistant.session_store import SessionStore  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402

ASSISTANT_DIR = Path(assistant_pkg.__file__).resolve().parent


def load_json(name: str) -> dict:
    return json.loads((ASSISTANT_DIR / name).read_text(encoding="utf-8"))


def parse_sse_frames(text: str):
    """把 SSE 文本拆回 [(event_name, payload_dict), ...]（与 events.format_sse 对偶）。"""
    frames = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip("\r\n")
        if not chunk:
            continue
        name, data_lines = "message", []
        for line in chunk.split("\n"):
            line = line.rstrip("\r")
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip(" "))
        frames.append((name, json.loads("\n".join(data_lines)) if data_lines else {}))
    return frames


# --------------------------------------------------------------- stub 服务

def stub_route(obj):
    """可编程 route_llm 桩：记录调用并返回固定路由结果。"""
    calls = []

    def _route(system, user):
        calls.append({"system": system, "user": user})
        return (dict(obj) if isinstance(obj, dict) else obj, "")

    _route.calls = calls
    return _route


def stub_narrate(text="结论见上方卡片。"):
    calls = []

    def _narrate(system, user):
        calls.append({"system": system, "user": user})
        return (text, "")

    _narrate.calls = calls
    return _narrate


def stub_heat_formula(x, y, z):
    """compute_heat 的确定性公式桩（形状与 attention.score_detail 一致）。"""
    return {"x": float(x), "y": float(y), "z": float(z), "score": 51.2,
            "band": "中热", "profile": "spec", "proxy_flags": [], "degraded": False}


# --------------------------------------------------------------- 公共夹具

class ChatActionTestBase(unittest.TestCase):
    """TestClient + 真实 AssistantLoop（服务 stub）+ 临时会话/档案库。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001  fastapi 缺失时整组跳过而非报错
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.client = TestClient(webapp.app)

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CHAT_TURN_DEADLINE_S", None)   # 环境隔离：走 rules 缺省预算

        db = Path(self.tmp.name) / "app.db"
        self.sessions = SessionStore(db)
        self.addCleanup(self.sessions.close)
        self.dossiers = DossierService(DossierStore(db))
        self.addCleanup(self.dossiers.close)

        self.rules = load_json("assistant_rules.json")
        # 指纹闸与本模块无关（本模块测 action 通道）：阈值 >1.0 永不达阈 = 关闸。
        self.rules["fingerprint_threshold"] = 1.1
        self.prompts = load_json("prompts.json")
        self.router = stub_route({"action": "compute_heat", "x": 100, "y": 1000, "z": 10})
        self.narrate = stub_narrate()
        self.loop = AssistantLoop(
            sessions=self.sessions, dossiers=self.dossiers,
            services={"route_llm": self.router, "narrate_llm": self.narrate,
                      "heat_formula": stub_heat_formula},
            rules=self.rules, prompts=self.prompts)

        for patcher in (
            mock.patch.object(self.webapp, "get_session_store", lambda: self.sessions),
            mock.patch.object(self.webapp, "build_assistant_loop", lambda: self.loop),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post_chat(self, **body):
        return self.client.post("/api/chat", json=body)

    def run_turn(self, **body):
        """一次成功的 SSE 回合：断言 200 并返回解析后的事件帧。"""
        resp = self.post_chat(**body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.headers["content-type"].startswith("text/event-stream"))
        return parse_sse_frames(resp.text)

    def assert_rejected(self, action, needle: str = ""):
        """非法 action 必须 400 + JSON {"detail": ...}（不是 SSE 流里的 error 事件）。"""
        resp = self.post_chat(message="前端附的人类可读说明", action=action)
        self.assertEqual(resp.status_code, 400, f"action={action!r} 应 400：{resp.text[:300]}")
        self.assertTrue(resp.headers["content-type"].startswith("application/json"))
        detail = str(resp.json()["detail"])
        self.assertTrue(detail)
        if needle:
            self.assertIn(needle, detail)
        return detail


# ===========================================================================
# 1. 合法 action：跳过路由模型，事件协议与顺序不变
# ===========================================================================
class TestActionSkipsRouter(ChatActionTestBase):
    def test_compute_heat_action_emits_heat_card_without_router(self):
        frames = self.run_turn(
            message="算一下 x=100 y=1000 z=10 的关注度",
            action={"name": "compute_heat", "args": {"x": 100, "y": 1000, "z": 10}})
        names = [n for n, _ in frames]
        self.assertEqual(self.router.calls, [], "结构化 action 不得调用路由模型")
        # 事件协议与顺序（§5.3）一个都不新增
        self.assertEqual(names[0], "meta")
        self.assertEqual(names[-1], "done")
        self.assertTrue(set(names) <= set(EVENT_NAMES))
        self.assertLess(names.index("tool_start"), names.index("card"))
        self.assertLess(names.index("card"), names.index("narrative"))
        self.assertLess(names.index("narrative"), names.index("tool_end"))
        card = [p for n, p in frames if n == "card"][0]
        self.assertEqual(card["card_type"], "heat")
        self.assertEqual(card["band"], "中热")
        self.assertAlmostEqual(card["score"], 51.2)
        # 只读工具：不建档
        self.assertEqual(self.dossiers.store.list_dossiers(limit=10), [])
        # 回合如实落库（tool_runs 一条 compute_heat）
        sid = int(frames[0][1]["session_id"])
        runs = self.sessions.runs_for_session(sid)
        self.assertEqual([r["tool"] for r in runs], ["compute_heat"])

    def test_insights_action_emits_insights_card(self):
        self.dossiers.store.create_dossier("传闻", "传闻的原文", input_kind="text")
        frames = self.run_turn(message="看看我的画像",
                               action={"name": "insights", "args": {"kind": "patterns"}})
        self.assertEqual(self.router.calls, [])
        card = [p for n, p in frames if n == "card"][0]
        self.assertEqual(card["card_type"], "insights")
        self.assertEqual(card["card"]["card_type"], "insights")
        self.assertEqual(card["card"]["kind"], "patterns")
        self.assertIn("patterns", card["card"])
        tool_end = [p for n, p in frames if n == "tool_end"][0]
        self.assertEqual(tool_end["tool"], "insights")

    def test_narrate_still_runs_after_action_tool(self):
        """卡片之后照常走叙述（复用既有流水线，不是"只出卡不出话"）。"""
        self.run_turn(message="算关注度",
                      action={"name": "compute_heat", "args": {"x": 1, "y": 2, "z": 3}})
        self.assertEqual(len(self.narrate.calls), 1)
        self.assertIn("[热度评估]", self.narrate.calls[0]["user"])


# ===========================================================================
# 2. 非法 action：400（未知工具 / 写工具 / 坏参数 / 坏形状），且不执行任何工具
# ===========================================================================
class TestActionRejections(ChatActionTestBase):
    def test_unknown_tool_is_400(self):
        detail = self.assert_rejected({"name": "delete_all_dossiers", "args": {}})
        self.assertIn("未注册", detail)

    def test_write_tools_are_all_rejected(self):
        """写工具经 action 通道一律 400：写档案必须走"自然语言 → 路由 → 词表"的正路。"""
        write_tools = [(t["name"], {}) for t in self.loop.registry.catalog()
                       if t["permission"] == "write"]
        self.assertTrue(write_tools, "注册表里应存在写工具，否则本用例失去意义")
        self.assertIn("verify_claim", [n for n, _ in write_tools])
        for name, args in write_tools:
            detail = self.assert_rejected({"name": name, "args": args})
            self.assertIn("只读", detail)
        # 拒绝的回合没有执行任何工具、没有叙述调用、没有消息落库
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.narrate.calls, [])
        for session in self.sessions.list_sessions():
            self.assertEqual(self.sessions.messages(int(session["id"])), [])

    def test_bad_args_are_400(self):
        self.assert_rejected({"name": "compute_heat", "args": {"x": -1, "y": 1, "z": 1}},
                             needle="x")
        self.assert_rejected({"name": "compute_heat", "args": {"x": 1, "y": 1}})  # 缺 z
        self.assert_rejected({"name": "insights", "args": {"kind": "all"}}, needle="kind")
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.narrate.calls, [])

    def test_bad_shapes_are_400_and_leave_no_session(self):
        for bad in ("compute_heat", 123, [], {"name": ""}, {"name": 5},
                    {"name": "compute_heat", "args": [1, 2, 3]}):
            self.assert_rejected(bad, needle="action")
        self.assertEqual(self.sessions.list_sessions(), [], "坏形状的请求不该建会话")
        self.assertEqual(self.router.calls, [])


# ===========================================================================
# 3. 无 action：行为与改动前一致（仍走路由，事件序列与 action 版逐位一致）
# ===========================================================================
class TestNoActionUnchanged(ChatActionTestBase):
    def test_no_action_still_uses_router_and_same_event_sequence(self):
        plain = self.run_turn(message="算一下 x=100 y=1000 z=10 的关注度")
        self.assertEqual(len(self.router.calls), 1, "无 action 时必须仍走路由模型")
        frames = self.run_turn(
            message="算一下 x=100 y=1000 z=10 的关注度",
            action={"name": "compute_heat", "args": {"x": 100, "y": 1000, "z": 10}})
        self.assertEqual([n for n, _ in plain], [n for n, _ in frames],
                         "action 通道不得改变事件序列")
        plain_card = [p for n, p in plain if n == "card"][0]
        action_card = [p for n, p in frames if n == "card"][0]
        self.assertEqual(set(plain_card), set(action_card), "卡片字段口径应一致")

    def test_message_validation_still_applies(self):
        """message 仍必填、仍走 8000 字上限（action 不能替代人类可读说明）。"""
        resp = self.post_chat(message="   ", action={"name": "compute_heat",
                                                    "args": {"x": 1, "y": 2, "z": 3}})
        self.assertEqual(resp.status_code, 400)
        resp = self.post_chat(message="a" * 8001,
                              action={"name": "compute_heat", "args": {"x": 1, "y": 2, "z": 3}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("8000", resp.json()["detail"])


# ===========================================================================
# 4. AssistantLoop.resolve_action 契约（web 400 之外的 loop 层保证）
# ===========================================================================
class TestLoopResolveActionContract(ChatActionTestBase):
    def test_read_tool_plan_is_single_step_with_clean_args(self):
        plan = self.loop.resolve_action({"name": "compute_heat",
                                         "args": {"x": 1, "y": 2, "z": 3, "bogus": 9}})
        self.assertEqual(plan["kind"], "tools")
        self.assertEqual(plan["steps"], [{"action": "compute_heat",
                                          "args": {"x": 1.0, "y": 2.0, "z": 3.0}}])
        plan = self.loop.resolve_action({"name": "insights", "args": {"kind": "WEEKLY"}})
        self.assertEqual(plan["steps"][0]["args"], {"kind": "weekly"})

    def test_write_tools_raise_tool_not_allowed(self):
        for spec in self.loop.registry.catalog():
            if spec["permission"] != "write":
                continue
            self.assertIn(spec["name"], self.rules["tool_allowlist"])
            with self.assertRaises(ToolNotAllowed, msg=f"写工具 {spec['name']} 必须被拒"):
                self.loop.resolve_action({"name": spec["name"], "args": {}})

    def test_unregistered_or_not_allowlisted_raise(self):
        with self.assertRaises(ToolNotAllowed):
            self.loop.resolve_action({"name": "no_such_tool", "args": {}})
        rules = dict(self.rules, tool_allowlist=[
            a for a in self.rules["tool_allowlist"] if a != "insights"])
        loop = AssistantLoop(registry=self.loop.registry, sessions=self.sessions,
                             dossiers=self.dossiers,
                             services={"route_llm": self.router,
                                       "narrate_llm": self.narrate},
                             rules=rules, prompts=self.prompts)
        with self.assertRaises(ToolNotAllowed):
            loop.resolve_action({"name": "insights", "args": {"kind": "patterns"}})

    def test_handle_raises_instead_of_swallowing_invalid_action(self):
        """handle 对非法 action 必须抛给调用方，不能被"永不抛"的兜底折成 error 事件。"""
        session_id = self.sessions.create_session("动作通道")
        with self.assertRaises(ToolNotAllowed):
            self.loop.handle(session_id, "说明文本", None, None,
                             action={"name": "verify_claim", "args": {"text": "x"}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
