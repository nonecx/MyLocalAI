# -*- coding: utf-8 -*-
"""M2 web 层离线测试：/api/chat（SSE）+ /api/sessions + 前后端自洽。

不联网、不调用大模型。助手核心 AssistantLoop 由并行工作流开发——本模块通过
monkeypatch ``app.build_assistant_loop`` 注入 stub loop（编排者钉死的解耦点），
断言 SSE 输出与 mylocalai.assistant.events 的封闭枚举协议一致。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_website_chat -v
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到 web/app.py

from mylocalai.assistant.events import EVENT_NAMES, EventStream, format_sse  # noqa: E402

STATIC = ROOT / "src" / "mylocalai" / "web" / "static"
INDEX_HTML = STATIC / "index.html"
CHAT_JS = STATIC / "chat.js"
CARDS_JS = STATIC / "cards.js"
CHAT_CSS = STATIC / "chat.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


# --------------------------------------------------------------- stub 助手核心
CARD_PAYLOAD = {
    "card_type": "conclusion",
    "label": "疑似谣言",
    "risk_score": 62,
    "confidence": "medium",
    "evidence_sufficiency": {"official": 1, "unofficial": 3, "unknown": 1, "total": 5},
    "as_of": "2026-10-31T12:00:00+08:00",
    "recheck_conditions": ["出现官方/权威媒体级信源的通报或报道"],
    "verdict_basis": "verdict.assess() 纯函数输出，大模型不参与打分",
    "degraded": False,
}

#: §5.3 协议里除 error 外的全事件序列（含 card 与 narrative，契约要求）
STUB_SCRIPT = [
    ("meta", {"session_id": 0, "run_id": 7}),      # session_id 在 handle 里回填真实值
    ("status", {"stage": "routing"}),
    ("tool_start", {"tool": "verify_claim", "args_preview": "自来水下毒？", "est_s": 60}),
    ("scan", {"markers": ["煽动话术×2", "绝对化断言×1"]}),
    ("progress", {"component": "search", "done": 1, "total": 3}),
    ("card", dict(CARD_PAYLOAD)),
    ("narrative", {"delta": "证据只支持以下判断。"}),
    ("narrative", {"delta": "证据截至 2026-10-31，缺官方信源。"}),
    ("tool_end", {"tool": "verify_claim", "duration_s": 12.3, "degraded": False,
                  "dossier_id": 5}),
    ("done", {"cost": {"requests": 3, "tokens": 456}}),
]


class StubLoop:
    """按预置脚本依序 emit 的假 AssistantLoop（handle 契约与 §4.2 真实现一致）。"""

    def __init__(self, script=None, fail: bool = False) -> None:
        self.script = list(script if script is not None else STUB_SCRIPT)
        self.calls = []
        self.fail = fail

    def handle(self, session_id, message, events, cancel_check) -> None:
        self.calls.append({"session_id": session_id, "message": message,
                           "cancel_check": cancel_check})
        if self.fail:
            raise RuntimeError("stub boom")
        for name, payload in self.script:
            payload = dict(payload)
            if name == "meta":
                payload["session_id"] = session_id   # 真实 loop 会回填会话 id
            events.emit(name, **payload)


class _FakeRequest:
    """drain_event_stream 的最小 request 替身：is_disconnected 可编程。"""

    def __init__(self, disconnected: bool = False, on_poll=None) -> None:
        self._disconnected = disconnected
        self._on_poll = on_poll
        self.polls = 0

    async def is_disconnected(self) -> bool:
        self.polls += 1
        if self._on_poll:
            self._on_poll(self.polls)
        return self._disconnected


async def _collect(agen):
    return [frame async for frame in agen]


# --------------------------------------------------------------- 1. /api/chat
class ChatApiTestBase(unittest.TestCase):
    """公共夹具：TestClient + 临时会话库 + 工厂注入。"""

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
        from mylocalai.assistant.session_store import SessionStore
        self.tmp = TemporaryDirectory()
        self.store = SessionStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        patcher = mock.patch.object(self.webapp, "get_session_store", lambda: self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def inject_loop(self, loop) -> None:
        patcher = mock.patch.object(self.webapp, "build_assistant_loop", lambda: loop)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post_chat(self, **body):
        return self.client.post("/api/chat", json=body)


class TestChatSse(ChatApiTestBase):
    """POST /api/chat 的 SSE 行为（stub loop 注入）。"""

    def test_sse_sequence_matches_events_protocol(self):
        """事件序列 = stub 依序 emit 的序列；card 在 narrative 前；done 带 cost。"""
        stub = StubLoop()
        self.inject_loop(stub)
        resp = self.post_chat(message="网传自来水被人下毒了")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/event-stream"))
        frames = parse_sse_frames(resp.text)
        names = [n for n, _ in frames]
        self.assertEqual(names, [n for n, _ in STUB_SCRIPT])
        # 协议一致性：全部事件名都在 events.py 封闭枚举内
        self.assertTrue(set(names) <= set(EVENT_NAMES))
        # 卡片永远在叙述之前（§5.3 顺序契约）
        self.assertLess(names.index("card"), names.index("narrative"))
        # meta 回传了自动新建的会话 id，且 loop 收到的就是它
        meta_payload = dict(frames[0][1])
        self.assertEqual(meta_payload["session_id"], stub.calls[0]["session_id"])
        self.assertEqual(stub.calls[0]["message"], "网传自来水被人下毒了")
        # done 带成本徽章口径
        done_payload = frames[[n for n, _ in frames].index("done")][1]
        self.assertEqual(done_payload["cost"], {"requests": 3, "tokens": 456})
        # 帧格式与 events.format_sse 逐字节一致
        for name, payload in frames:
            self.assertIn(f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}",
                          resp.text)

    def test_missing_session_id_auto_creates_session(self):
        """无 session_id → 先建会话，meta 回传其 id；会话出现在 /api/sessions。"""
        stub = StubLoop()
        self.inject_loop(stub)
        resp = self.post_chat(message="这条消息是真的吗？")
        self.assertEqual(resp.status_code, 200)
        meta = parse_sse_frames(resp.text)[0]
        self.assertEqual(meta[0], "meta")
        sid = meta[1]["session_id"]
        sessions = self.client.get("/api/sessions").json()["sessions"]
        self.assertEqual([s["id"] for s in sessions], [sid])
        self.assertEqual(sessions[0]["title"], "这条消息是真的吗？"[:24])
        self.assertIn("updated_at", sessions[0])

    def test_existing_session_reused_and_validated(self):
        """带合法 session_id → 复用会话不新建；不存在 → 404。"""
        stub = StubLoop()
        self.inject_loop(stub)
        sid = self.client.post("/api/sessions", json={"title": "已有会话"}).json()["id"]
        resp = self.post_chat(session_id=sid, message="再来一条")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(stub.calls[0]["session_id"], sid)
        self.assertEqual(len(self.client.get("/api/sessions").json()["sessions"]), 1)
        bad = self.post_chat(session_id=999999, message="x")
        self.assertEqual(bad.status_code, 404)

    def test_message_validation_is_400(self):
        """沿用网站现有校验风格：非空、无代理项、≤8000 字。"""
        stub = StubLoop()
        self.inject_loop(stub)
        self.assertEqual(self.post_chat(message="   ").status_code, 400)
        long_msg = "a" * 8001
        resp = self.post_chat(message=long_msg)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("8000", resp.json()["detail"])
        # 孤立代理项（无效 Unicode）与 /api/analyze 同口径 400
        resp = self.client.post("/api/chat", content=json.dumps({"message": "ok\ud800bad"}),
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)
        # 校验失败发生在流开始之前：会话没有被创建
        self.assertEqual(self.client.get("/api/sessions").json()["sessions"], [])

    def test_loop_unavailable_degrades_in_stream(self):
        """build_assistant_loop() → None：SSE 里发 error{assistant_unavailable} 后收尾。"""
        patcher = mock.patch.object(self.webapp, "build_assistant_loop", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        resp = self.post_chat(message="任何消息")
        self.assertEqual(resp.status_code, 200)
        frames = parse_sse_frames(resp.text)
        names = [n for n, _ in frames]
        self.assertEqual(names[-1], "error")
        err = frames[-1][1]
        self.assertEqual(err["code"], "assistant_unavailable")
        self.assertTrue(err["recoverable"])
        self.assertTrue(err["message"])
        self.assertTrue(set(names) <= set(EVENT_NAMES))

    def test_worker_exception_emits_error_and_terminates(self):
        """loop.handle 抛协议外异常 → web 兜底 error 收尾，流必然终止（不挂死）。"""
        stub = StubLoop(fail=True)
        self.inject_loop(stub)
        resp = self.post_chat(message="触发 stub 异常")
        self.assertEqual(resp.status_code, 200)
        frames = parse_sse_frames(resp.text)
        self.assertEqual(frames[-1][0], "error")
        self.assertEqual(frames[-1][1]["code"], "internal")
        self.assertFalse(frames[-1][1]["recoverable"])


# --------------------------------------------------------------- 2. /api/sessions
class TestSessionsApi(ChatApiTestBase):
    """/api/sessions 三端点。"""

    def test_create_list_messages(self):
        """POST 新建返回 id；GET 列表含 title/updated_at；messages 正序；非法 sid 404。"""
        s1 = self.client.post("/api/sessions", json={}).json()
        self.assertIsInstance(s1["id"], int)
        self.assertGreater(s1["id"], 0)
        s2 = self.client.post("/api/sessions", json={"title": "手工会话"}).json()
        self.assertEqual(s2["title"], "手工会话")

        listing = self.client.get("/api/sessions").json()["sessions"]
        self.assertEqual({s["id"] for s in listing}, {s1["id"], s2["id"]})
        for s in listing:
            self.assertIn("title", s)
            self.assertIn("updated_at", s)
            self.assertIn("created_at", s)

        self.store.add_message(s1["id"], "user", "第一条")
        self.store.add_message(s1["id"], "assistant", "第二条")
        msgs = self.client.get(f"/api/sessions/{s1['id']}/messages").json()["messages"]
        self.assertEqual([m["content"] for m in msgs], ["第一条", "第二条"])

    def test_messages_invalid_sid_is_404(self):
        """非法 sid（不存在 / 非整数 / 非正数）→ 一律 404，而非 422/500。"""
        self.assertEqual(self.client.get("/api/sessions/999999/messages").status_code, 404)
        self.assertEqual(self.client.get("/api/sessions/abc/messages").status_code, 404)
        self.assertEqual(self.client.get("/api/sessions/-1/messages").status_code, 404)


# --------------------------------------------------------------- 3. drain 单测
class TestDrainEventStream(unittest.TestCase):
    """拆出的 drain 协程：终止契约 + 断开取消（不经过 HTTP 层）。"""

    def setUp(self) -> None:
        from app import drain_event_stream
        self.drain = drain_event_stream

    def test_yields_frames_until_done_and_worker_done(self):
        """done/error 事件 + 线程结束双条件满足才关流；帧与 format_sse 逐字节一致。"""
        events = EventStream()
        for name, payload in STUB_SCRIPT:
            events.emit(name, **payload)
        frames = asyncio.run(_collect(self.drain(events, _FakeRequest(), lambda: True,
                                                 poll_interval=0.001)))
        self.assertEqual(frames, [format_sse(e) for e in events.snapshot()])

    def test_done_without_worker_done_keeps_polling(self):
        """线程未结束时即使看到 done 也继续轮询（等 loop 补完落库），收尾后正常关流。"""
        events = EventStream()
        events.emit("meta", session_id=1, run_id=1)
        events.emit("done", cost={"requests": 0, "tokens": 0})
        state = {"finished": False}

        def on_poll(n: int) -> None:
            if n >= 2:      # 第二次轮询起工作线程才算收尾
                state["finished"] = True

        req = _FakeRequest(on_poll=on_poll)
        frames = asyncio.run(_collect(self.drain(events, req, lambda: state["finished"],
                                                 poll_interval=0.001)))
        self.assertEqual(len(frames), 2)
        self.assertGreaterEqual(req.polls, 2)

    def test_disconnect_requests_cancel_and_stops_stream(self):
        """客户端断开 → events.cancelled 被置位（loop 的 cancel_check 据此协作取消）。"""
        events = EventStream()
        events.emit("meta", session_id=1, run_id=1)
        events.emit("status", stage="routing")
        frames = asyncio.run(_collect(self.drain(events, _FakeRequest(disconnected=True),
                                                 lambda: False, poll_interval=0.001)))
        self.assertEqual(frames, [])          # 断开后不再投递
        self.assertTrue(events.cancelled)     # 取消已请求

    def test_cancelled_worker_done_terminates(self):
        """取消置位 + 线程收尾 → 关流（即使最后一个事件不是 done/error）。"""
        events = EventStream()
        events.emit("meta", session_id=1, run_id=1)
        events.request_cancel()
        frames = asyncio.run(_collect(self.drain(events, _FakeRequest(), lambda: True,
                                                 poll_interval=0.001)))
        self.assertEqual(frames, [format_sse(events.snapshot()[0])])


# --------------------------------------------------------------- 4. 前后端自洽
class TestChatFrontendConsistency(unittest.TestCase):
    """沿 test_website_attention 先例：抓"改了后端忘了改前端"类错。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = _read(INDEX_HTML)
        cls.chat_js = _read(CHAT_JS)
        cls.cards_js = _read(CARDS_JS)

    @staticmethod
    def _normalize(path: str) -> str:
        path = path.split("?")[0]
        return re.sub(r"\{[^}]*\}", "{}", path)

    def test_chatjs_api_paths_exist_in_backend(self):
        """chat.js 引用的每个 /api/* 路径（含 {id} 模板）都在 app.py 路由表里。"""
        from app import app as fastapi_app
        routes = {self._normalize(getattr(r, "path", "")) for r in fastapi_app.routes}
        paths = re.findall(r'"(/api/[^"]+)"', self.chat_js)
        self.assertTrue(paths, "没从 chat.js 里解析到 API 路径")
        for path in paths:
            self.assertIn(self._normalize(path), routes,
                          f"chat.js 引用了后端不存在的接口：{path}")

    def test_chatjs_events_subset_of_event_names(self):
        """chat.js 监听的事件名集合 = SSE_EVENTS 常量，且 ⊆ events.py EVENT_NAMES。"""
        m = re.search(r"const SSE_EVENTS = \[([^\]]+)\]", self.chat_js, re.S)
        self.assertIsNotNone(m, "chat.js 缺少 SSE_EVENTS 常量")
        listened = set(re.findall(r'"(\w+)"', m.group(1)))
        self.assertTrue(listened)
        self.assertLessEqual(listened, set(EVENT_NAMES),
                             f"chat.js 监听了协议之外的事件：{listened - set(EVENT_NAMES)}")
        handlers = set(re.findall(r"^    (\w+): on", self.chat_js, re.M))
        self.assertEqual(handlers, listened, "SSE_HANDLERS 与 SSE_EVENTS 不同步")

    def test_chat_css_classes_used_by_js_are_defined(self):
        """chat.js 动态拼出的 chat-* class 必须在 chat.css 里有定义（否则样式丢）。"""
        css = _read(CHAT_CSS)
        used = set(re.findall(r'class="([^"]+)"', self.chat_js))
        names = set()
        for chunk in used:
            names.update(re.findall(r"[A-Za-z][\w-]*", chunk))
        chat_classes = sorted(c for c in names if c.startswith("chat-"))
        self.assertTrue(chat_classes)
        missing = [c for c in chat_classes if f".{c}" not in css]
        self.assertEqual(missing, [], f"chat.css 缺少定义：{missing}")

    def test_cards_renderers_and_honesty_markers(self):
        """cards.js 六个渲染器齐全（含单页化的 renderInsights 与两张只读新卡），as_of / 降级 / 代理 / verdict_basis 口径都在。"""
        for fn in ("renderConclusion", "renderSpeech", "renderHeat", "renderInsights",
                   "renderResearch", "renderAuthority", "renderCard"):
            self.assertIn(f"function {fn}", self.cards_js, f"cards.js 缺少 {fn}")
        self.assertIn("window.ChatCards", self.cards_js)
        for marker in ("as_of", "降级", "代理", "verdict_basis", "置信度",
                       "evidence_sufficiency", "recheck_conditions"):
            self.assertIn(marker, self.cards_js, f"cards.js 缺少口径元素：{marker}")
        # 渲染器都会输出 as_of 行（口径诚实：数字旁必须展示 as_of）
        self.assertGreaterEqual(self.cards_js.count("asOfLine("), 4)
        # 两张只读新卡：dispatch 分支 + 导出名单（缺任一前端就调不到）
        self.assertIn('if (type === "research") return renderResearch(payload);', self.cards_js)
        self.assertIn('if (type === "authority") return renderAuthority(payload);', self.cards_js)
        tail = self.cards_js.split("window.ChatCards")[-1]
        self.assertIn("renderResearch,", tail)
        self.assertIn("renderAuthority,", tail)

    def test_cards_exports_shared_api_path(self):
        """cards.js 收口的 apiPath 必须真的实现并导出（batch/monitor/profiles 共用）。

        历史教训：它曾被三处引用却没人实现，一跑就抛 `apiPath is not a function`，
        切换档案/角色、批次进度流、监测详情全部哑火。这里锁三件事：函数在、
        在导出名单里、三处调用方仍在复用它。
        """
        self.assertIn("function apiPath(template, values)", self.cards_js)
        tail = self.cards_js.split("window.ChatCards")[-1]
        self.assertIn("apiPath,", tail)
        for name in ("batch.js", "monitor.js", "profiles.js"):
            js = _read(STATIC / name)
            self.assertIn("apiPath", js, f"{name} 应复用 cards.js 的 apiPath")
        # 单值模式要替换"全部"占位符（正则带 g），不能只换第一个
        self.assertIn(r"/\{[^{}]*\}/g", self.cards_js)

    def test_claim_answer_renderer_contract(self):
        """新答案先于 legacy score card；未验证时不展示旧风险数值。"""
        self.assertIn("function renderClaimAnswer", self.cards_js)
        self.assertIn("payload.claim_answer", self.cards_js)
        self.assertIn("insufficient_data", _read(ROOT / "src" / "mylocalai" / "assistant" / "prompts.json"))
        self.assertIn("不得自行补状态", _read(ROOT / "src" / "mylocalai" / "assistant" / "prompts.json"))
        self.assertIn("疑似谣言（尚未证实）", self.cards_js)
        # The insufficient-data branch must not call renderConclusion.
        branch = self.cards_js.split('if (answer.answer_status !== "validated")', 1)[1].split("const rows", 1)[0]
        self.assertNotIn("renderConclusion", branch)
        self.assertIn("rel=\"noopener noreferrer\"", self.cards_js)
        self.assertIn("escapeHtml(claim.claim_excerpt", self.cards_js)
        self.assertIn("本轮没有足够的已核验材料形成完整判断", self.cards_js)

    def test_index_references_chat_assets_exist(self):
        """单页对话结构：index.html 引用 shell.js/cards.js/chat.js/chat.css 且文件都在。"""
        for ref in ("static/shell.js", "static/cards.js", "static/chat.js", "static/chat.css"):
            self.assertIn(ref, self.html, f"index.html 未引用 {ref}")
            self.assertTrue((STATIC / Path(ref).name).exists(), f"文件不存在：{ref}")
        # 单页化：Tab 切换容器与旧前端脚本全部下线（旧三 Tab 结构不再存在）
        self.assertNotIn("data-tab=", self.html, "index.html 仍有旧 Tab 切换按钮")
        for old in ('id="toolsTab"', 'id="chatTab"', "static/app.js", "static/insights.js"):
            self.assertNotIn(old, self.html, f"index.html 仍残留旧前端标记：{old}")
        # 主结构：单页对话布局 → 左栏（会话/档案库/我的画像）+ 主列（消息流 + composer）
        self.assertIn('<main class="chat-layout">', self.html)
        for marker in ('id="chatSessionList"', 'id="chatNewSession"',
                       'id="chatDossierList"', 'id="chatDossierCount"',
                       'id="chatInsights"', 'id="chatMessages"',
                       'id="chatError"', 'id="chatInput"', 'id="chatSendBtn"'):
            self.assertIn(marker, self.html, f"页面缺少单页对话组成：{marker}")
        # 「关于系统」面板迁到 header 弹层，不再占右栏
        for marker in ('id="aboutBtn"', 'id="aboutDialog"', 'id="aboutCloseBtn"'):
            self.assertIn(marker, self.html, f"页面缺少关于弹层组成：{marker}")

    def test_chatjs_element_ids_exist_in_html(self):
        """chat.js 的 $("#id") / querySelector[All]("#id") 引用的每个 id 在 index.html 里都有。"""
        used = set(re.findall(r'\$\("#([A-Za-z][\w]*)', self.chat_js))
        used |= set(re.findall(r'querySelector(?:All)?\("#([A-Za-z][\w]*)', self.chat_js))
        self.assertTrue(used, "chat.js 里没引用任何元素 id")
        missing = sorted(i for i in used if f'id="{i}"' not in self.html)
        self.assertEqual(missing, [], f"chat.js 引用了 HTML 里不存在的元素 id: {missing}")
        # 核心接线点逐个点名：漏一个页面就是半残（这些 id 全由 chat.js 读写）
        for core in ("chatMessages", "chatError", "chatInput", "chatSendBtn",
                     "chatCancelBtn", "chatCharCount", "chatNewSession",
                     "chatSessionList", "chatDossierList", "chatDossierCount",
                     "chatInsights", "chatHeatChip", "chatHeatPanel",
                     "chatHeatX", "chatHeatY", "chatHeatZ",
                     "chatHeatCalc", "chatHeatReset"):
            self.assertIn(core, used, f"chat.js 未接线 {core}（或接线方式换了，测试需同步）")

    def test_build_assistant_loop_contract(self):
        """解耦点自检：不注入时返回 None（loop 未落地）或带 handle 的对象，绝不抛。"""
        from app import build_assistant_loop
        loop = build_assistant_loop()
        self.assertTrue(loop is None or callable(getattr(loop, "handle", None)))

    def test_old_frontend_contract_moved_to_shell_js(self):
        """旧 app.js 的页面外壳职责迁到 shell.js：健康检查 + 关于弹层；旧文件/旧 DOM 已删除。"""
        shell_js = _read(STATIC / "shell.js")
        # 1) 服务状态 chip 的健康检查（原 app.js API.health 的等价职责）
        self.assertIn('health: "/api/health"', shell_js)
        self.assertIn('$("#apiStatus")', shell_js)
        self.assertIn('id="apiStatus"', self.html)
        # 2) 关于弹层（原「关于系统」右栏面板 → header 弹层）
        self.assertIn('$("#aboutDialog")', shell_js)
        self.assertIn('$("#aboutBtn")', shell_js)
        for marker in ('id="aboutBtn"', 'id="aboutDialog"', 'id="aboutCloseBtn"'):
            self.assertIn(marker, self.html, f"页面缺少关于弹层组成：{marker}")
        # 3) 样式表引用原样保留（style.css + chat.css 都还在）
        for href in ('href="static/style.css"', 'href="static/chat.css"'):
            self.assertIn(href, self.html)
        # 4) 旧前端文件必须真的删除（不能留一份没被引用的死代码）
        for name in ("app.js", "insights.js"):
            self.assertFalse((STATIC / name).exists(), f"应当删除的旧前端文件仍在：{name}")
        # 5) 旧工具模式 Tab 的 DOM（关注度独立面板 / 分析页）不再残留
        for marker in ('id="attentionPanel"', 'id="attnTabs"', 'id="inputText"',
                       'id="analyzeBtn"', 'id="historyList"', 'static/app.js'):
            self.assertNotIn(marker, self.html, f"index.html 仍残留旧前端标记：{marker}")


# --------------------------------------------------------------- 5. 档案工作区前端自洽
class TestProfilesFrontendConsistency(unittest.TestCase):
    """profiles.js / profiles.css 与后端 /api/profiles、index.html 的自洽（v6 角色扩展）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = _read(INDEX_HTML)
        cls.profiles_js = _read(STATIC / "profiles.js")
        cls.profiles_css = _read(STATIC / "profiles.css")
        cls.style_css = _read(STATIC / "style.css")

    @staticmethod
    def _normalize(path: str) -> str:
        path = path.split("?")[0]
        return re.sub(r"\{[^}]*\}", "{}", path)

    def test_index_references_profiles_assets(self):
        """index.html 引用 profiles.js/profiles.css，且文件真实存在。"""
        for ref in ("static/profiles.js", "static/profiles.css"):
            self.assertIn(ref, self.html, f"index.html 未引用 {ref}")
            self.assertTrue((STATIC / Path(ref).name).exists(), f"文件不存在：{ref}")

    def test_profiles_js_api_paths_exist_in_backend(self):
        """profiles.js 引用的每个 /api/* 路径（含 {id} 模板）都在 app.py 路由表里。

        路由表经 openapi.json 取：FastAPI 0.163 把 include_router 进来的路由包成
        _IncludedRouter（app.routes 里 .path 为空），直接迭代会漏掉整组路由。
        """
        from app import app as fastapi_app
        from fastapi.testclient import TestClient
        spec = TestClient(fastapi_app).get("/openapi.json").json()
        routes = {self._normalize(p) for p in (spec.get("paths") or {})}
        paths = re.findall(r'"(/api/[^"]+)"', self.profiles_js)
        self.assertTrue(paths, "没从 profiles.js 里解析到 API 路径")
        for path in paths:
            self.assertIn(self._normalize(path), routes,
                          f"profiles.js 引用了后端不存在的接口：{path}")
        # 五个端点逐个点名（少一个前端就是半残；路径参数名被 normalize 成 {}）
        for endpoint in ("/api/profiles", "/api/profiles/{}",
                         "/api/profiles/{}/activate"):
            self.assertIn(endpoint, routes, f"后端缺少 Profile 端点：{endpoint}")

    def test_profiles_js_element_ids_exist_in_html(self):
        """profiles.js 的 $("#id") 引用的每个 id 在 index.html 里都有。"""
        used = set(re.findall(r'\$\("#([A-Za-z][\w]*)', self.profiles_js))
        self.assertTrue(used, "profiles.js 里没引用任何元素 id")
        missing = sorted(i for i in used if f'id="{i}"' not in self.html)
        self.assertEqual(missing, [], f"profiles.js 引用了 HTML 里不存在的元素 id: {missing}")
        for core in ("profileBtn", "profileMenu", "profileDialog", "profileDialogClose",
                     "profileList", "profileCreateForm", "profileNewKey",
                     "profileNewName", "profileNewRole", "profileStatus",
                     "profileName", "profileRoleIcon"):
            self.assertIn(core, used, f"profiles.js 未接线 {core}")

    def test_profiles_js_never_builds_html_from_user_input(self):
        """纪律同 settings.js：禁止 innerHTML 类 HTML 拼接，用户输入走 textContent。"""
        sinks = re.findall(r"\.(?:innerHTML|outerHTML)\b|insertAdjacentHTML\(|\bdocument\.write\(",
                           self.profiles_js)
        self.assertEqual(sinks, [], f"profiles.js 出现 HTML 注入点：{sinks}")
        self.assertIn("document.createElement", self.profiles_js)
        self.assertIn("textContent", self.profiles_js)

    def test_profile_changed_event_name_matches_across_files(self):
        """切换事件名在 profiles.js（派发方）与 chat.js（监听方）一致。"""
        chat_js = _read(CHAT_JS)
        m = re.search(r'const CHANGED_EVENT = "([^"]+)"', self.profiles_js)
        self.assertIsNotNone(m, "profiles.js 缺少 CHANGED_EVENT 常量")
        event = m.group(1)
        self.assertIn(f'const PROFILE_CHANGED_EVENT = "{event}"', chat_js,
                      "chat.js 与 profiles.js 的切换事件名不一致")
        self.assertIn(f'new CustomEvent(CHANGED_EVENT', self.profiles_js)
        self.assertIn(f'addEventListener(PROFILE_CHANGED_EVENT', chat_js)

    def test_role_switcher_affordances(self):
        """v8 角色可见化：顶栏常驻角色标签 + 左栏「切换角色」入口 + 菜单角色区。

        角色不再只能藏在管理弹层里换：顶栏按钮显示「档案名 · 角色短名」，
        菜单顶部四个角色一键换（PUT /api/profiles/{id} 的 role 字段），
        左栏按钮打开同一个菜单并把焦点送到当前角色项。
        """
        for marker in ('id="profileRoleLabel"', 'id="chatRoleSwitchBtn"',
                       'class="profile-role-chip"', 'aria-controls="profileMenu"'):
            self.assertIn(marker, self.html, f"index.html 缺角色切换入口：{marker}")
        # ROLE_META 四角色齐全，各带 icon/label/short（与后端 ROLES 同序）
        for role in ("layperson", "analyst", "monitor", "full"):
            self.assertRegex(
                self.profiles_js,
                rf'{role}: \{{ icon: "[^"]+", label: "[^"]+", short: "[^"]+" \}}',
                f"profiles.js 的 ROLE_META 缺角色或缺 short：{role}")
        # 菜单角色区、当前项勾选、结果行、按 role 找回焦点
        for marker in ("切换角色（当前档案）", "profile-role-item", "profile-role-check",
                       "profile-role-note", "button[data-role]", "focusRoleItem",
                       'body: JSON.stringify({ role: target })'):
            self.assertIn(marker, self.profiles_js, f"profiles.js 缺角色区实现：{marker}")
        # 角色说明只认后端 roles[].description（不在前端写死能力文案）
        self.assertIn('description: (r && r.description) || ""', self.profiles_js)
        self.assertIn("roleDescription", self.profiles_js)
        # 左栏入口确实接线到 profiles.js（不是只在 HTML 里摆个按钮）
        self.assertIn('$("#chatRoleSwitchBtn")', self.profiles_js)
        self.assertIn("openRoleSection", self.profiles_js)
        # 首载回填事件：profiles.js 的 fetch 晚于 chat.js 首屏渲染，名字必须两侧一致，
        # 否则左栏"当前档案"行在首屏永远是空的（新按钮旁边一片空白）
        ready = re.search(r'const READY_EVENT = "([^"]+)"', self.profiles_js)
        self.assertIsNotNone(ready, "profiles.js 缺少 READY_EVENT")
        chat_js = _read(CHAT_JS)
        self.assertIn(f'const PROFILES_READY_EVENT = "{ready.group(1)}"', chat_js)
        self.assertIn("addEventListener(PROFILES_READY_EVENT", chat_js)

    def test_profiles_css_reuses_root_tokens(self):
        """profiles.css 只用 style.css 已声明的 :root 令牌，不另起一套配色。"""
        root_block = self.style_css.split(":root {", 1)[1].split("}", 1)[0]
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", root_block))
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", self.profiles_css))
        self.assertTrue(used, "profiles.css 没用到任何 :root 令牌")
        self.assertEqual(sorted(used - declared), [],
                         "profiles.css 用了 style.css 未声明的变量")
        self.assertNotRegex(self.profiles_css, r"(?m)^\s*:root\s*\{",
                            "profiles.css 不该重新定义 :root 配色")

    def test_profiles_css_classes_have_styles(self):
        """profiles.js 动态用到的 profile-* class 在 profiles.css 都有定义。"""
        used = set()
        for chunk in re.findall(r'className = "([^"]+)"', self.profiles_js):
            used.update(chunk.split())
        for chunk in re.findall(r'classList\.(?:add|toggle|remove)\("([^"]+)"', self.profiles_js):
            used.add(chunk)
        targets = sorted(n for n in used if n.startswith("profile-"))
        self.assertTrue(targets, "解析不到 profiles.js 使用的 profile-* 类")
        missing = [n for n in targets if f".{n}" not in self.profiles_css]
        self.assertEqual(missing, [], f"profiles.css 缺少定义：{missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
