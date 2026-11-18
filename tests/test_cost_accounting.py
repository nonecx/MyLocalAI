# -*- coding: utf-8 -*-
"""M4 成本真实计数测试（工作指导 §6 M4 / §8，DoD：每会话/档案可见 requests+tokens）。

三层都锁：
  - llm_client 进程计数器：requests 只在真实上游调用时 +1（缓存命中/single-flight
    等待不计）；tokens 从响应 usage 取、取不到如实 0（不估算）；
  - loop 接线：tool_runs.finish_run / done.cost / 档案 analysis_runs 拿到工具段真实
    delta；计数面不可用时逐段退回 M4 前的粗口径（路由/叙述/摘要各 1 次、工具 out.cost）；
  - web 汇总端点：/api/sessions、/api/sessions/{sid}/messages、/api/dossiers/{id}
    附 cost 汇总（sum 真实落库值）。

全部离线：上游用 stub 客户端（monkeypatch llm_client.create_client/_client_for，
照 .tmp/verify-2026-10-22/mock_upstream.py 的"假上游"手法收敛到进程内替身）；
会话/档案库用 tempfile 临时 SQLite。零网络零配额。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_cost_accounting -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到 web/app.py

import mylocalai.assistant as assistant_pkg  # noqa: E402
from mylocalai.llm import llm_cache  # noqa: E402
from mylocalai.llm import llm_client  # noqa: E402

ASSISTANT_DIR = Path(assistant_pkg.__file__).resolve().parent

#: 每个缓存敏感测试都钉住缓存开启（防外部环境把 TTL/上限设成 0）
CACHE_ENV = {"LLM_CACHE_TTL_S": "900", "LLM_CACHE_MAX": "256"}


def _allow_external(*providers: str):
    """测试用偏好快照：显式授权给定提供方（生产默认 fail-closed，不读本机偏好文件）。"""
    from mylocalai.core.privacy import PrivacyPreferences
    return PrivacyPreferences({
        "schema_version": 1,
        "save_enabled": {"results": False, "raw_input": False, "external_cache": False},
        "retention_days": {"results": 90, "raw_input": 30, "external_cache": 7},
        "external_processing_enabled": True,
        "external_processing_providers": list(providers),
    })

OFFICIAL_ENTRY = {"url": "https://gov.example/1", "domain": "gov.example",
                  "domain_tier": "官方权威", "source_kind": "官方（新华网）",
                  "stance": "反驳", "title": "官方辟谣", "snippet": "不实信息",
                  "fetched_at": "2026-10-31T08:00:00+08:00"}
UNOFFICIAL_ENTRY = {"url": "https://bbs.example/2", "domain": "bbs.example",
                    "source_kind": "非官方（微博）", "stance": "支持",
                    "title": "网帖", "snippet": "听说……"}

VERIFY_TEXT = "气死了！听说自来水被人下毒了，官方根本不敢说！速转！"


class _StubUpstream:
    """进程内假上游客户端：chat.completions.create 返回固定正文 + 可编程 usage/失败。"""

    def __init__(self, total_tokens: int = 321, fail: bool = False,
                 content: str = "模拟正文") -> None:
        self.total_tokens = total_tokens
        self.fail = fail
        self.content = content
        self.calls: list = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("上游失败（测试注入）")
        usage = None if self.total_tokens is None \
            else SimpleNamespace(total_tokens=self.total_tokens)
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _load_json(name: str) -> dict:
    return json.loads((ASSISTANT_DIR / name).read_text(encoding="utf-8"))


# ===========================================================================
# 1. llm_client 进程计数器
# ===========================================================================

class UsageCounterBase(unittest.TestCase):
    """计数器测试公共夹具：清计数 + 清缓存 + 钉住缓存开启，结束后还原。"""

    def setUp(self) -> None:
        env_patch = mock.patch.dict(os.environ, CACHE_ENV)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        llm_client.reset_usage()
        self.addCleanup(llm_client.reset_usage)
        llm_cache.clear()
        self.addCleanup(llm_cache.clear)


class TestUsageCounter(UsageCounterBase):
    def test_usage_snapshot_initial_zero(self):
        """reset 之后快照恒为 {"requests": 0, "tokens": 0}（形状契约）。"""
        llm_client.record_usage(5, 50)
        llm_client.reset_usage()
        self.assertEqual(llm_client.usage_snapshot(), {"requests": 0, "tokens": 0})

    def test_record_usage_accumulates_and_reset(self):
        """record_usage 按次/token 累加（requests=1 是默认形参）；reset 清零。"""
        llm_client.record_usage(2, 5)
        llm_client.record_usage(0, 7)
        self.assertEqual(llm_client.usage_snapshot(), {"requests": 2, "tokens": 12})
        llm_client.record_usage()          # 缺省 = 1 次请求 0 token
        self.assertEqual(llm_client.usage_snapshot(), {"requests": 3, "tokens": 12})
        llm_client.reset_usage()
        self.assertEqual(llm_client.usage_snapshot(), {"requests": 0, "tokens": 0})

    def test_real_call_counts_request_and_usage_tokens(self):
        """真实调用 → requests +1；tokens = 响应 usage.total_tokens（不估算）。"""
        stub = _StubUpstream(total_tokens=321)
        with mock.patch.object(llm_client, "create_client", return_value=stub):
            text, err = llm_client.chat_text("系统提示", "成本计数探针文本一",
                                             namespace="cost_probe")
        self.assertEqual(err, "")
        self.assertEqual(text, "模拟正文")
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(llm_client.usage_snapshot(), {"requests": 1, "tokens": 321})

    def test_missing_usage_reports_zero_tokens(self):
        """响应不带 usage → tokens 如实记 0（口径诚实，不按字数估算）；requests 照计。"""
        stub = _StubUpstream(total_tokens=None)
        with mock.patch.object(llm_client, "create_client", return_value=stub):
            llm_client.chat_text("系统提示", "无 usage 探针文本", namespace="cost_probe")
        self.assertEqual(llm_client.usage_snapshot(), {"requests": 1, "tokens": 0})

    def test_cache_hit_does_not_count(self):
        """同文本二次调用命中进程内缓存 → requests 不再增加（缓存不计费）。"""
        stub = _StubUpstream(total_tokens=321)
        with mock.patch.object(llm_client, "create_client", return_value=stub):
            llm_client.chat_text("系统提示", "同文本缓存探针", namespace="cost_probe")
            first = llm_client.usage_snapshot()
            llm_client.chat_text("系统提示", "同文本缓存探针", namespace="cost_probe")
            second = llm_client.usage_snapshot()
        self.assertEqual(first, {"requests": 1, "tokens": 321})
        self.assertEqual(second, first)          # 缓存命中：requests/tokens 都不变
        self.assertEqual(len(stub.calls), 1)

    def test_concurrent_same_request_counts_once(self):
        """4 线程并发同请求（single-flight）→ 上游只被真实调用 1 次、计数 1 次。"""
        stub = _StubUpstream(total_tokens=321)
        with mock.patch.object(llm_client, "create_client", return_value=stub):
            with ThreadPoolExecutor(max_workers=4) as ex:
                outs = list(ex.map(
                    lambda _i: llm_client.chat_text("系统提示", "并发同请求探针",
                                                    namespace="cost_probe"),
                    range(4)))
        self.assertTrue(all(err == "" for _t, err in outs))
        self.assertEqual(llm_client.usage_snapshot(), {"requests": 1, "tokens": 321})
        self.assertEqual(len(stub.calls), 1)

    def test_fallback_provider_success_counts(self):
        """备用链：主提供方失败（不计）→ 备用提供方成功调用计 1 次 + tokens 照取。"""
        primary = _StubUpstream(fail=True)
        fallback = _StubUpstream(total_tokens=88)
        with mock.patch.object(llm_client, "_provider_names",
                               return_value=["longcat", "deepseek"]), \
             mock.patch.object(llm_client, "create_client", return_value=primary), \
             mock.patch.object(llm_client, "_client_for", return_value=fallback), \
             mock.patch("mylocalai.core.privacy.load_preferences",
                        return_value=_allow_external("longcat", "deepseek")):
            text, err = llm_client.chat_text("系统提示", "备用链探针文本",
                                             namespace="cost_probe")
        self.assertEqual(err, "")
        self.assertEqual(text, "模拟正文")
        self.assertEqual(len(primary.calls), 1)   # 主提供方被真实尝试过（但失败未计费）
        self.assertEqual(len(fallback.calls), 1)
        self.assertEqual(llm_client.usage_snapshot(), {"requests": 1, "tokens": 88})


# ===========================================================================
# 2. loop 接线：tool_runs / done.cost / analysis_runs 拿真实 delta
# ===========================================================================

class LoopCostBase(unittest.TestCase):
    """loop 级夹具：临时同库 + 包内默认 rules/prompts + 真实 registry。"""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env_patch = mock.patch.dict(os.environ, CACHE_ENV)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("CHAT_TURN_DEADLINE_S", None)
        llm_client.reset_usage()
        self.addCleanup(llm_client.reset_usage)
        llm_cache.clear()
        self.addCleanup(llm_cache.clear)

        from mylocalai.assistant.loop import AssistantLoop
        from mylocalai.assistant.session_store import SessionStore
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        db = Path(self.tmp.name) / "app.db"
        self.sessions = SessionStore(db)
        self.addCleanup(self.sessions.close)
        self.dossiers = DossierService(DossierStore(db))
        self.addCleanup(self.dossiers.close)
        self.rules = _load_json("assistant_rules.json")
        self.prompts = _load_json("prompts.json")
        self.session_id = self.sessions.create_session("成本接线会话")

    def make_loop(self, services):
        from mylocalai.assistant.loop import AssistantLoop
        return AssistantLoop(sessions=self.sessions, dossiers=self.dossiers,
                             services=services, rules=self.rules, prompts=self.prompts)


class TestLoopCostWiring(LoopCostBase):
    def test_tool_run_done_cost_and_analysis_runs_real_delta(self):
        """stub verify 真实走了 1 次 chat 调用 → tool_runs / done.cost /
        analysis_runs 三处都记真实 delta (1, 321)，而不是 llm_turns 粗口径。"""
        stub = _StubUpstream(total_tokens=321)

        def route_llm(system, user):
            return ({"action": "verify_claim", "text": VERIFY_TEXT}, "")

        def verify(text, deep=False, on_component_done=None, cancel_check=None):
            # 工具内部真实发起 1 次上游调用（经 llm_client，计数器 +1）
            out, err = llm_client.chat_text("成本接线探针系统提示",
                                            f"成本接线探针 {text}",
                                            namespace="assistant_cost_probe")
            self.assertEqual(err, "")
            return {"label": "疑似谣言", "risk_score": 62, "signals": [],
                    "degraded_components": [], "agent_error": "", "llm_turns": 7,
                    "evidence_book": {"claim": text,
                                      "entries": [OFFICIAL_ENTRY, UNOFFICIAL_ENTRY]}}

        def narrate_llm(system, user):
            return ("证据只支持以下判断。", "")

        loop = self.make_loop({"route_llm": route_llm, "verify": verify,
                               "narrate_llm": narrate_llm})
        with mock.patch.object(llm_client, "create_client", return_value=stub):
            from mylocalai.assistant.events import EventStream
            events = EventStream()
            out = loop.handle(self.session_id, VERIFY_TEXT, events, None)

        # tool_runs：finish_run 拿到工具段真实 delta（不是 llm_turns=7 的粗口径）
        run = self.sessions.runs_for_session(self.session_id)[0]
        self.assertEqual((run["requests"], run["tokens"]), (1, 321))
        # done.cost：路由 0（stub 没走真实上游）+ 工具 1 + 叙述 0
        self.assertEqual(out["cost"], {"requests": 1, "tokens": 321})
        # 档案 analysis_runs：record_analysis 调用点注入的也是真实 delta
        runs = self.dossiers.store.runs(out["dossier_id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["requests"], runs[0]["tokens"]), (1, 321))
        self.assertEqual(len(stub.calls), 1)

    def test_coarse_fallback_when_snapshot_unavailable(self):
        """计数面不可用（usage_snapshot 炸）→ 逐段退回 M4 前粗口径：
        chitchat 成本=路由 1；verify 回合 = 路由1 + llm_turns 3 + 叙述1 = 5。"""
        def _boom():
            raise RuntimeError("计数面不可用（测试注入）")

        def route_llm(system, user):
            return ({"action": "chitchat", "reply": "你好。"}, "")

        def verify(text, deep=False, on_component_done=None, cancel_check=None):
            return {"label": "疑似谣言", "risk_score": 62, "signals": [],
                    "degraded_components": [], "agent_error": "", "llm_turns": 3,
                    "evidence_book": {"claim": text,
                                      "entries": [OFFICIAL_ENTRY, UNOFFICIAL_ENTRY]}}

        def narrate_llm(system, user):
            return ("证据截至今天，仍缺官方细节。", "")

        with mock.patch.object(llm_client, "usage_snapshot", side_effect=_boom):
            loop = self.make_loop({"route_llm": route_llm, "narrate_llm": narrate_llm})
            from mylocalai.assistant.events import EventStream
            out = loop.handle(self.session_id, "你好呀", EventStream(), None)
            self.assertEqual(out["cost"], {"requests": 1, "tokens": 0})

            # verify 回合：粗口径 = 路由1 + 工具 out.cost(llm_turns=3) + 叙述1
            def route_verify(system, user):
                return ({"action": "verify_claim", "text": VERIFY_TEXT}, "")

            loop2 = self.make_loop({"route_llm": route_verify, "verify": verify,
                                    "narrate_llm": narrate_llm})
            out2 = loop2.handle(self.session_id, VERIFY_TEXT, EventStream(), None)
            self.assertEqual(out2["cost"], {"requests": 5, "tokens": 0})
            run = self.sessions.runs_for_session(self.session_id)[-1]
            self.assertEqual((run["requests"], run["tokens"]), (3, 0))


# ===========================================================================
# 3. web 汇总端点形状
# ===========================================================================

class ApiCostSummaryBase(unittest.TestCase):
    """web 层夹具：TestClient + 临时库注入（沿 test_website_chat 同一手法）。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001  fastapi 缺失时整组跳过
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.client = TestClient(webapp.app)

    def setUp(self) -> None:
        from mylocalai.assistant.session_store import SessionStore
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "dossier.db"
        self.store = SessionStore(db)
        self.addCleanup(self.store.close)
        self.service = DossierService(DossierStore(db))
        self.addCleanup(self.service.close)
        for name, obj in (("get_session_store", lambda: self.store),
                          ("get_dossier_service", lambda: self.service)):
            patcher = mock.patch.object(self.webapp, name, obj)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestApiCostSummary(ApiCostSummaryBase):
    def test_session_messages_and_list_cost_summary(self):
        """/api/sessions/{sid}/messages 与 /api/sessions 附 cost = Σ tool_runs 落库值。"""
        sid = self.store.create_session("成本汇总会话")
        rid1 = self.store.start_run(sid, "verify_claim", {"text": "x"})
        self.store.finish_run(rid1, "ok", requests=2, tokens=99)
        rid2 = self.store.start_run(sid, "check_heat", {"topic": "y"})
        self.store.finish_run(rid2, "ok", requests=1, tokens=11)

        msgs = self.client.get(f"/api/sessions/{sid}/messages").json()
        self.assertEqual(msgs["session_id"], sid)
        self.assertEqual(msgs["cost"], {"requests": 3, "tokens": 110})
        listing = self.client.get("/api/sessions").json()["sessions"]
        item = next(s for s in listing if s["id"] == sid)
        self.assertEqual(item["cost"], {"requests": 3, "tokens": 110})

    def test_session_without_tool_runs_reports_zero_cost(self):
        """纯 chitchat 会话（无 tool_runs）→ cost 如实全零（不编粗口径数字）。"""
        sid = self.store.create_session("闲聊会话")
        self.store.add_message(sid, "user", "你好")
        self.store.add_message(sid, "assistant", "你好。")
        msgs = self.client.get(f"/api/sessions/{sid}/messages").json()
        self.assertEqual(msgs["cost"], {"requests": 0, "tokens": 0})

    def test_dossier_detail_cost_summary(self):
        """/api/dossiers/{id} 附 cost = Σ analysis_runs 落库值（多批次累加）。"""
        did = self.service.store.create_dossier("成本档案", "网传某说法")
        self.service.store.add_run(did, "verify", "ok", requests=3, tokens=77)
        self.service.store.add_run(did, "recheck", "ok", requests=1, tokens=23)
        detail = self.client.get(f"/api/dossiers/{did}").json()
        self.assertEqual(detail["cost"], {"requests": 4, "tokens": 100})


if __name__ == "__main__":
    unittest.main(verbosity=2)
