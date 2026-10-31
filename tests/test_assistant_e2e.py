# -*- coding: utf-8 -*-
"""M2 端到端整合测试（工作指导 §6 M2 DoD 核心）：真实 AssistantLoop + web 层 SSE 全链。

与 tests.test_website_chat（stub loop 测 web 层）、tests.test_assistant_loop（stub web
测 loop 层）互补：本模块用**真实 AssistantLoop**（真实 registry/tools_verify/
tools_memory + 真实 events 协议）配**全套 stub 服务**（verify 返回带 evidence_book 的
固定结果、narrate_llm 返回固定文本、route_llm 可编程），经 TestClient POST /api/chat
跑通「用户消息 → SSE 事件流 → 结论卡片 → 档案落库 → 会话落库」全链。

不联网、不调用大模型；会话域与档案域同库（临时 SQLite，tempfile）；测试通过
monkeypatch app.build_assistant_loop / get_session_store / get_dossier_service 注入。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_assistant_e2e -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 与 test_website_chat 同一 import 形态

import mylocalai.assistant as assistant_pkg  # noqa: E402
from mylocalai.assistant.events import EVENT_NAMES, EventStream, format_sse  # noqa: E402
from mylocalai.llm import llm_cache  # noqa: E402

ASSISTANT_DIR = Path(assistant_pkg.__file__).resolve().parent
PROMPTS = json.loads((ASSISTANT_DIR / "prompts.json").read_text(encoding="utf-8"))

VERIFY_TEXT = "气死了！听说本市自来水被人下毒了，官方根本不敢回应！速转家人群！"
NARRATE_TEXT = "证据只支持以下判断：两条独立信源互相矛盾。证据截至今天，仍缺官方通报，出现权威报道后再看一眼。"

OFFICIAL_ENTRY = {"url": "https://gov.example/1", "domain": "gov.example",
                  "domain_tier": "官方权威", "source_kind": "官方（新华网）",
                  "stance": "反驳", "title": "官方辟谣：不实信息", "snippet": "经查不实",
                  "fetched_at": "2026-10-31T08:00:00+08:00"}
UNOFFICIAL_ENTRY = {"url": "https://bbs.example/2", "domain": "bbs.example",
                    "source_kind": "非官方（微博）", "stance": "支持",
                    "title": "网帖称……", "snippet": "听说……"}

#: verify 服务桩的固定结果（带 evidence_book：档案库结论卡/证据本都从这里派生）
DEFAULT_VERIFY_RESULT = {
    "label": "疑似谣言", "risk_score": 62,
    "signals": ["来源模糊", "绝对化断言"],
    "degraded_components": [], "agent_error": "", "llm_turns": 3,
    "evidence_book": {"claim": VERIFY_TEXT, "entries": [OFFICIAL_ENTRY, UNOFFICIAL_ENTRY]},
}

#: chat.js renderConclusion 渲染路径所需字段（整合项1：e2e 锁定卡片载荷齐全）
CONCLUSION_CARD_FIELDS = ("label", "risk_score", "confidence", "evidence_sufficiency",
                          "as_of", "recheck_conditions", "verdict_basis")


def parse_sse(text: str):
    """SSE 文本 → [(event_name, payload_dict), ...]，并逐帧断言 format_sse 往返无损。

    「全程断言 SSE 帧可用 format_sse 解析回事件 dict」：每帧原始字节必须等于
    format_sse({"event": name, **payload}) 的重建结果（协议对偶性，帧损坏即红）。
    """
    frames = []
    for chunk in text.split("\n\n"):
        if not chunk:
            continue
        name, data_lines = "message", []
        for line in chunk.split("\n"):
            line = line.rstrip("\r")
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip(" "))
        payload = json.loads("\n".join(data_lines)) if data_lines else {}
        rebuilt = format_sse({"event": name, **payload})
        assert rebuilt == chunk + "\n\n", \
            f"SSE 帧无法用 format_sse 无损还原：{chunk!r}"
        frames.append((name, payload))
    return frames


class StubServices:
    """可编程 stub 服务集：测试中途可改 route_json / verify_result（recheck 用例）。

    verify 桩模拟 RumorAgent.analyze 的返回形状（label/risk_score/evidence_book/...）
    并回调 on_component_done 产生 SSE progress 事件；verify_hook 供取消用例在
    "慢执行"中途置位取消（等价于前端 abort → drain 置 cancelled）。
    """

    def __init__(self, verify_result=None, narrate=NARRATE_TEXT) -> None:
        self.route_json = {"action": "verify_claim", "text": VERIFY_TEXT}
        self.verify_result = dict(verify_result or DEFAULT_VERIFY_RESULT)
        self.narrate = narrate
        self.verify_hook = None
        self.verify_calls = []

    def route_llm(self, system, user):
        return (dict(self.route_json), "")

    def verify(self, text, deep=False, on_component_done=None, cancel_check=None):
        self.verify_calls.append({"text": text, "cancel_check": cancel_check})
        if self.verify_hook is not None:
            self.verify_hook()
        if on_component_done:
            for comp in ("search", "emotion", "evidence"):
                on_component_done(comp)
        return dict(self.verify_result)

    def narrate_llm(self, system, user):
        return (self.narrate, "")

    def summarize_llm(self, system, user):
        return ("摘要：用户核查了自来水传闻。", "")

    def as_dict(self):
        return {"route_llm": self.route_llm, "verify": self.verify,
                "narrate_llm": self.narrate_llm, "summarize_llm": self.summarize_llm}


class AssistantE2EBase(unittest.TestCase):
    """公共夹具：TestClient + 临时同库（sessions/dossiers）+ 真实 AssistantLoop 注入。"""

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
        from mylocalai.assistant.loop import AssistantLoop
        from mylocalai.assistant.session_store import SessionStore
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "app.db"
        # 生产形状：sessions 与 dossiers 同库（tool_runs.dossier_id 外键依赖它）
        self.store = SessionStore(self.db_path)
        self.addCleanup(self.store.close)
        self.dossiers = DossierService(DossierStore(self.db_path))
        self.addCleanup(self.dossiers.close)

        for name, factory in (("get_session_store", lambda: self.store),
                              ("get_dossier_service", lambda: self.dossiers)):
            p = mock.patch.object(self.webapp, name, factory)
            p.start()
            self.addCleanup(p.stop)
        # 环境隔离：各用例自行设置 CHAT_TURN_DEADLINE_S，退出自动还原
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("CHAT_TURN_DEADLINE_S", None)

    # ---- 组装件 ----

    def install_loop(self, loop) -> None:
        p = mock.patch.object(self.webapp, "build_assistant_loop", lambda: loop)
        p.start()
        self.addCleanup(p.stop)

    def make_loop(self, services: dict, registry=None):
        """真实 AssistantLoop（rules/prompts 走包内默认配置 = 生产形状）+ 注入 stub 服务。"""
        from mylocalai.assistant.loop import AssistantLoop
        return AssistantLoop(sessions=self.store, dossiers=self.dossiers,
                             services=services, registry=registry)

    def post_chat(self, **body):
        return self.client.post("/api/chat", json=body)

    @staticmethod
    def names(frames):
        return [n for n, _ in frames]

    @staticmethod
    def payload(frames, name):
        return frames[[n for n, _ in frames].index(name)][1]


# --------------------------------------------------------------- ① 真实谣言全链
class TestRumorMessageEndToEnd(AssistantE2EBase):
    def test_full_sse_sequence_and_card_payload(self):
        """①真实谣言消息端到端：事件序列完整、card 在 narrative 前、卡片字段齐全。"""
        svc = StubServices()
        self.install_loop(self.make_loop(svc.as_dict()))
        resp = self.post_chat(message=VERIFY_TEXT)          # 无 session_id → 自动新建
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/event-stream"))
        frames = parse_sse(resp.text)
        names = self.names(frames)
        # 全部事件名都在封闭枚举内；序列完整（§5.3）
        self.assertTrue(set(names) <= set(EVENT_NAMES))
        self.assertEqual(names[0], "meta")
        self.assertEqual(names[1], "status")
        for expected in ("tool_start", "scan", "progress", "card", "narrative",
                         "tool_end", "done"):
            self.assertIn(expected, names)
        self.assertEqual(names.count("progress"), 3)        # verify 桩的三个组件
        # 卡片永远在叙述之前（§5.3 顺序契约）
        self.assertLess(names.index("card"), names.index("narrative"))
        self.assertGreater(names.index("tool_end"), names.index("narrative"))
        # 整合项2：自动新建会话后 meta 回传 session_id，前端据此绑定
        meta = frames[0][1]
        sid = meta["session_id"]
        self.assertIsInstance(sid, int)
        self.assertGreater(sid, 0)
        self.assertIsInstance(meta["run_id"], int)
        self.assertEqual([s["id"] for s in self.store.list_sessions()], [sid])
        self.assertEqual(self.store.get_session(sid)["title"], VERIFY_TEXT[:24])
        # meta.run_id = tool_runs 首行（start_run 先于 meta）
        self.assertEqual(meta["run_id"], self.store.runs_for_session(sid)[0]["id"])
        # 整合项1：card 载荷 = {card_type, card} + 顶层并置，chat.js 渲染路径字段齐全
        card_evt = self.payload(frames, "card")
        self.assertEqual(card_evt["card_type"], "conclusion")
        card = card_evt["card"]
        for key in CONCLUSION_CARD_FIELDS:
            self.assertIn(key, card, f"实时路径（d.card）缺字段: {key}")
            self.assertIn(key, card_evt, f"历史回放（顶层并置）缺字段: {key}")
        self.assertEqual(card["label"], "疑似谣言")
        suff = card["evidence_sufficiency"]
        self.assertEqual((suff["official"], suff["unofficial"], suff["total"]), (1, 1, 2))
        self.assertTrue(card["as_of"])
        self.assertTrue(card["verdict_basis"].startswith("verdict"))
        # narrative 逐句 delta 拼回叙述全文；done 带成本
        self.assertEqual("".join(p["delta"] for n, p in frames if n == "narrative"),
                         svc.narrate)
        done = frames[-1][1]
        # M4 真实计数：done.cost = 本回合真实上游调用（stub 全离线 → 0；原口径
        # 路由1+verify llm_turns 3+叙述1 是快照不可用时的粗口径 fallback，不再入账）
        self.assertEqual(done["cost"]["requests"], 0)

    def test_tool_end_dossier_id_and_store_persistence(self):
        """②tool_end 带 dossier_id；直接开库断言 conclusions/evidence_entries/analysis_runs。"""
        svc = StubServices()
        self.install_loop(self.make_loop(svc.as_dict()))
        resp = self.post_chat(message=VERIFY_TEXT)
        frames = parse_sse(resp.text)
        tool_end = self.payload(frames, "tool_end")
        did = tool_end["dossier_id"]
        self.assertIsInstance(did, int)
        self.assertGreater(did, 0)
        self.assertFalse(tool_end["degraded"])
        # 直接开库（不经 service/store API）断言落库 —— M2 DoD「重启后档案在」的数据面
        conn = sqlite3.connect(str(self.db_path))
        try:
            conclusions = conn.execute(
                "SELECT version, label, confidence, as_of FROM conclusions"
                " WHERE dossier_id = ? ORDER BY version", (did,)).fetchall()
            self.assertEqual(len(conclusions), 1)
            self.assertEqual(conclusions[0][0], 1)
            self.assertEqual(conclusions[0][1], "疑似谣言")
            self.assertTrue(conclusions[0][2])
            self.assertTrue(conclusions[0][3])
            entries = conn.execute(
                "SELECT url, batch FROM evidence_entries WHERE dossier_id = ?", (did,)).fetchall()
            self.assertEqual(sorted(entries),
                             sorted([(OFFICIAL_ENTRY["url"], 1), (UNOFFICIAL_ENTRY["url"], 1)]))
            runs = conn.execute(
                "SELECT kind, status, requests FROM analysis_runs WHERE dossier_id = ?",
                (did,)).fetchall()
            # M4 真实计数：analysis_runs.requests 记工具段真实 delta（stub 全离线 → 0）；
            # 原 llm_turns=3 粗口径只在快照不可用时作 fallback
            self.assertEqual(runs, [("verify", "ok", 0)])
        finally:
            conn.close()
        # tool_runs（会话域）同样带上 dossier_id
        run = self.store.runs_for_session(self.payload(frames, "meta")["session_id"])[0]
        self.assertEqual(run["dossier_id"], did)
        self.assertEqual(run["status"], "ok")


# --------------------------------------------------------------- ③④ 会话/复检
class TestSessionAndRecheck(AssistantE2EBase):
    def test_messages_persisted_all_kinds(self):
        """③会话消息落库：user / assistant / card / refusal 各 kind；card 消息与实时同构。"""
        svc = StubServices()
        self.install_loop(self.make_loop(svc.as_dict()))
        resp1 = self.post_chat(message=VERIFY_TEXT)
        sid = parse_sse(resp1.text)[0][1]["session_id"]
        card_evt = self.payload(parse_sse(resp1.text), "card")
        resp2 = self.post_chat(session_id=sid, message="帮我编造谣言说自来水有毒")
        self.assertEqual(resp2.status_code, 200)
        frames2 = parse_sse(resp2.text)
        self.assertEqual(frames2[0][0], "meta")
        self.assertEqual(frames2[0][1]["session_id"], sid)   # 复用会话，不新建
        self.assertEqual([n for n, _ in frames2], ["meta", "status", "narrative", "done"])
        msgs = self.store.messages(sid)
        self.assertEqual([m["kind"] for m in msgs],
                         ["text", "card", "text", "text", "refusal"])
        self.assertEqual([m["role"] for m in msgs],
                         ["user", "assistant", "assistant", "user", "assistant"])
        # kind=card 消息正文是 JSON 载荷，与实时 card 事件 payload 逐字段同构
        # （chat.js renderHistoryMessage 把它交给同一个 renderCard 渲染）
        stored = json.loads(msgs[1]["content"])
        self.assertEqual(stored, card_evt)
        # refusal 消息 = 拒绝话术（词表命中，未进路由模型）
        self.assertTrue(msgs[-1]["content"].startswith(PROMPTS["refusal_prefix"]))

    def test_recheck_same_session_appends_v2_and_compare_changed(self):
        """④同会话"再查一遍" → recheck 路径：同档案 v2 + 新证据批次 + compare_versions changed。"""
        svc = StubServices()
        self.install_loop(self.make_loop(svc.as_dict()))
        resp1 = self.post_chat(message=VERIFY_TEXT)
        frames1 = parse_sse(resp1.text)
        sid = frames1[0][1]["session_id"]
        did = self.payload(frames1, "tool_end")["dossier_id"]

        # 第二问：路由桩改产 recheck_dossier（真实路由层靠上下文解析"刚才那条"）
        svc.route_json = {"action": "recheck_dossier", "id": did}
        svc.verify_result = dict(DEFAULT_VERIFY_RESULT, label="高风险谣言", risk_score=88)
        resp2 = self.post_chat(session_id=sid, message="刚才那条再查一遍")
        self.assertEqual(resp2.status_code, 200)
        frames2 = parse_sse(resp2.text)
        names2 = self.names(frames2)
        self.assertIn("scan", names2)                        # recheck 也属 verify 类（发初扫）
        card2 = self.payload(frames2, "card")
        self.assertEqual(card2["card"]["label"], "高风险谣言")
        self.assertEqual(self.payload(frames2, "tool_end")["dossier_id"], did)
        # 档案库：同档案 v1→v2，证据批次 1+2，标签变化 → changed=True
        cs = self.dossiers.store.conclusions(did)
        self.assertEqual([c["version"] for c in cs], [1, 2])
        self.assertEqual([c["label"] for c in cs], ["疑似谣言", "高风险谣言"])
        batches = {e["batch"] for e in self.dossiers.store.evidence(did)}
        self.assertEqual(batches, {1, 2})
        cmp = self.dossiers.compare_versions(did)
        self.assertTrue(cmp["changed"])
        self.assertEqual(len(cmp["versions"]), 2)
        # tool_runs：两次 run 都指向同一档案
        runs = self.store.runs_for_session(sid)
        self.assertEqual([r["tool"] for r in runs], ["verify_claim", "recheck_dossier"])
        self.assertEqual([r["dossier_id"] for r in runs], [did, did])


# --------------------------------------------------------------- ⑤ 轻链路
class TestChitchatLightPath(AssistantE2EBase):
    def test_chitchat_turn_has_no_tools_and_no_runs(self):
        """⑤chitchat 轻链路：meta→status→narrative→done；无 tool/card/scan；无 run 记录。"""
        svc = StubServices()
        svc.route_json = {"action": "chitchat", "reply": "你好。想查什么直接说。"}
        self.install_loop(self.make_loop(svc.as_dict()))
        resp = self.post_chat(message="你好呀")
        self.assertEqual(resp.status_code, 200)
        frames = parse_sse(resp.text)
        names = self.names(frames)
        # 轻链路形状：meta→status→narrative*（分句假流式）→done；无工具事件
        self.assertEqual(names[0], "meta")
        self.assertEqual(names[1], "status")
        self.assertEqual(names[-1], "done")
        self.assertEqual(set(names), {"meta", "status", "narrative", "done"})
        self.assertEqual("".join(p["delta"] for n, p in frames if n == "narrative"),
                         "你好。想查什么直接说。")
        sid = frames[0][1]["session_id"]
        self.assertIsNone(frames[0][1]["run_id"])
        msgs = self.store.messages(sid)
        self.assertEqual([(m["role"], m["kind"]) for m in msgs],
                         [("user", "text"), ("assistant", "text")])
        self.assertEqual(msgs[-1]["content"], "你好。想查什么直接说。")
        self.assertEqual(self.store.runs_for_session(sid), [])


# --------------------------------------------------------------- ⑥ 取消链
class TestCancelChainE2E(AssistantE2EBase):
    def test_slow_verify_cancelled_mid_tool(self):
        """⑥取消链：stub 慢查执行中置 cancelled → error{cancelled} + tool_run=cancelled。

        等价于前端 AbortController 断开 → drain 置 events.cancelled（这里由
        SpyStream 捕获同一个 EventStream 实例，verify 桩在"慢执行"中途置位）。
        """
        svc = StubServices()
        captured = []

        class SpyStream(EventStream):
            def __init__(self) -> None:
                super().__init__()
                captured.append(self)

        svc.verify_hook = lambda: (time.sleep(0.05), captured[0].request_cancel())
        loop = self.make_loop(svc.as_dict())
        self.install_loop(loop)
        with mock.patch.object(self.webapp, "EventStream", SpyStream):
            resp = self.post_chat(message=VERIFY_TEXT)
        self.assertEqual(resp.status_code, 200)
        frames = parse_sse(resp.text)
        names = self.names(frames)
        self.assertEqual(names[-1], "error")
        err = frames[-1][1]
        self.assertEqual(err["code"], "cancelled")
        self.assertTrue(err["recoverable"])
        self.assertTrue(captured[0].cancelled)
        # 工具执行过（收到 tool_start）但卡片未发出（取消优先，断开后也送不到）
        self.assertIn("tool_start", names)
        self.assertNotIn("card", names)
        sid = frames[0][1]["session_id"]
        run = self.store.runs_for_session(sid)[0]
        self.assertEqual(run["status"], "cancelled")
        self.assertTrue(run["degraded"])


# --------------------------------------------------------------- ⑦ 轮预算超时（SSE 全链）
class TestTurnDeadlineE2E(AssistantE2EBase):
    def test_turn_budget_deadline_over_sse(self):
        """⑦轮预算超时链路（走 SSE 全链）：error{deadline} + 卡片如实缺失 + run=timeout。

        桩 verify_claim 睡过 0.2s 预算：SSE 全链收到 error(deadline) 与诚实降级文案，
        档案库无结论（桩不产卡），tool_run status=timeout。
        """
        from mylocalai.assistant.registry import ToolRegistry, deep_param, text_param

        seen = {}
        reg = ToolRegistry()

        def _slow_verify(ctx, text, deep=False):
            seen["deadline_at"] = llm_cache.deadline_at()   # 回合 scope 在工具期间激活
            time.sleep(0.6)
            return {"ok": False, "card": None, "digest": "",
                    "cost": {"requests": 0, "tokens": 0}, "dossier_id": None,
                    "degraded": True}

        reg.register("verify_claim", _slow_verify, params=["text", "deep"],
                     permission="write", cost="heavy", est_s=60.0,
                     description="超时测试桩", validators={"text": text_param(),
                                                          "deep": deep_param})
        svc = StubServices()
        loop = self.make_loop({"route_llm": svc.route_llm, "narrate_llm": svc.narrate_llm},
                              registry=reg)
        self.install_loop(loop)
        with mock.patch.dict(os.environ, {"CHAT_TURN_DEADLINE_S": "0.2"}):
            resp = self.post_chat(message=VERIFY_TEXT)
        self.assertEqual(resp.status_code, 200)
        frames = parse_sse(resp.text)
        names = self.names(frames)
        # 全链事件形状：…tool_start → scan → error(deadline) → narrative* → tool_end → done
        self.assertIn("error", names)
        err = self.payload(frames, "error")
        self.assertEqual(err["code"], "deadline")
        self.assertTrue(err["recoverable"])
        self.assertEqual(names[-1], "done")
        self.assertNotIn("card", names)                      # 卡片如实缺失，不补编
        self.assertEqual("".join(p["delta"] for n, p in frames if n == "narrative"),
                         PROMPTS["deadline_reply"])
        tool_end = self.payload(frames, "tool_end")
        self.assertTrue(tool_end["degraded"])
        self.assertGreaterEqual(tool_end["duration_s"], 0.5)
        self.assertIsNone(tool_end["dossier_id"])
        sid = frames[0][1]["session_id"]
        run = self.store.runs_for_session(sid)[0]
        self.assertEqual(run["status"], "timeout")
        self.assertTrue(run["degraded"])
        self.assertIsNone(run["dossier_id"])
        # 工具执行期间回合预算处于激活（CHAT_TURN_DEADLINE_S → deadline_scope_at）
        self.assertIsNotNone(seen["deadline_at"])


# --------------------------------------------------------------- ⑧ loop 缺席降级
class TestLoopUnavailableE2E(AssistantE2EBase):
    def test_loop_none_degrades_in_stream(self):
        """⑧build_assistant_loop() → None：meta（绑定会话）+ error{assistant_unavailable}。"""
        p = mock.patch.object(self.webapp, "build_assistant_loop", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        resp = self.post_chat(message=VERIFY_TEXT)
        self.assertEqual(resp.status_code, 200)
        frames = parse_sse(resp.text)
        self.assertEqual(self.names(frames), ["meta", "error"])
        sid = frames[0][1]["session_id"]
        self.assertGreater(sid, 0)
        self.assertIsNotNone(self.store.get_session(sid))    # 会话已建，未丢
        err = frames[1][1]
        self.assertEqual(err["code"], "assistant_unavailable")
        self.assertTrue(err["recoverable"])
        self.assertTrue(err["message"])
        self.assertTrue(set(self.names(frames)) <= set(EVENT_NAMES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
