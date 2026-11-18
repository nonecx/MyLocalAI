# -*- coding: utf-8 -*-
"""助手外层循环测试（工作指导 §4.2/§5.3/§6 M2-3）：事件序列、落库、降级、取消、上限。

对着真实事故形态锁定：
  - verify 全链事件序列快照：meta → status → tool_start → scan → progress* → card →
    narrative* → tool_end → done，**card 在 narrative 前**、tool_end 在叙述后（§5.3）；
  - 工具抛异常：error 事件 + 诚实降级回复 + run status=error + 会话不崩（下一轮还能用）；
  - 叙述失败：fallback 文案兜底且 card 事件仍在（永不因叙述失败丢结论，§5.7）；
  - 取消：cancel_check 命中 → run status=cancelled + error{code:"cancelled"}；
  - 单轮工具上限封顶：超限计划项不执行（M3 指纹第二槽位预留）。
全部离线：verify/speech/heat/route_llm/narrate_llm/summarize_llm 全部注入 stub，
SessionStore/DossierService 用 :memory: 库（tempfile 兜底），零网络零配额。
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mylocalai.assistant as assistant_pkg  # noqa: E402
from mylocalai.assistant.loop import AssistantLoop  # noqa: E402
from mylocalai.core.privacy import DEFAULT_PREFERENCES, PrivacyPreferences, _validated  # noqa: E402
from mylocalai.assistant.events import EventStream  # noqa: E402
from mylocalai.assistant.session_store import SessionStore  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402
from mylocalai.llm import llm_cache  # noqa: E402

ASSISTANT_DIR = Path(assistant_pkg.__file__).resolve().parent


def load_json(name: str) -> dict:
    return json.loads((ASSISTANT_DIR / name).read_text(encoding="utf-8"))


VERIFY_TEXT = "气死了！听说自来水被人下毒了，官方根本不敢说！速转！"

OFFICIAL_ENTRY = {"url": "https://gov.example/1", "domain": "gov.example",
                  "domain_tier": "官方权威", "source_kind": "官方（新华网）",
                  "stance": "反驳", "title": "官方辟谣", "snippet": "不实信息",
                  "fetched_at": "2026-10-31T08:00:00+08:00"}
UNOFFICIAL_ENTRY = {"url": "https://bbs.example/2", "domain": "bbs.example",
                    "source_kind": "非官方（微博）", "stance": "支持",
                    "title": "网帖", "snippet": "听说……"}


def stub_route(action="verify_claim", reply="", **extra):
    calls = []

    def _llm(system, user):
        calls.append((system, user))
        return ({"action": action, "reply": reply, **extra}, "")

    _llm.calls = calls
    return _llm


def stub_verify(fail=False, components=("emotion", "evidence"), label="疑似谣言"):
    calls = []

    def _svc(text, deep=False, on_component_done=None, cancel_check=None):
        calls.append({"text": text, "deep": deep, "cancelled_cb": cancel_check})
        if fail:
            raise RuntimeError("上游取证爆炸")
        if on_component_done:
            for c in components:
                on_component_done(c)
        return {"label": label, "risk_score": 62, "signals": ["来源模糊"],
                "degraded_components": [], "agent_error": "", "llm_turns": 3,
                "evidence_book": {"claim": text, "entries": [OFFICIAL_ENTRY, UNOFFICIAL_ENTRY]}}

    _svc.calls = calls
    return _svc


def stub_narrate(text="证据支持以下判断。证据截至今天，仍缺官方细节。"):
    calls = []

    def _svc(system, user):
        calls.append({"system": system, "user": user})
        if text is None:
            return (None, "narrate 上游抽风")
        return (text, "")

    _svc.calls = calls
    return _svc


def stub_summarize(text="用户核查了自来水下毒的传闻。"):
    calls = []

    def _svc(system, user):
        calls.append((system, user))
        if text is None:
            return (None, "sum 上游抽风")
        return (text, "")

    _svc.calls = calls
    return _svc


def stub_speech():
    # cancel_check：M2 复核修复后 speech 服务契约带引擎协作取消位（真实服务透传 judge）；
    # stub 只需接受并忽略（离线测试不取消）。
    def _svc(text, cancel_check=None):
        return {"conclusion": "疑似情绪化谣言", "degraded_components": [],
                "emotion": {"emotion_tendency": "恐惧", "emotion_score": 8.5, "keywords": []},
                "attack": {"attack_target": "相关部门", "aggression_level": "中", "attack_type": "指控"},
                "logic": {"argument": "x", "conclusion": "y", "logic_relevance": "弱",
                          "logic_fallacy": "诉诸恐惧"}}

    return _svc


def stub_heat(detail=None):
    def _svc(topic):
        return detail if detail is not None else {
            "x": 5, "y": 100000, "z": 500, "score": 44.8, "band": "低关注",
            "profile": "spec", "proxy_flags": ["y_proxy"], "quality": "exact"}

    return _svc


class LoopTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 会话域与档案域同库（生产形状：web 层两个门面都指 data/dossier.db）。
        # tool_runs.dossier_id 有外键，分库会让 finish_run 撞 FK——这也是对
        # "同库"这一生产假设的间接锁定。
        db = Path(self.tmp.name) / "app.db"
        self.sessions = SessionStore(db)
        self.addCleanup(self.sessions.close)
        self.dossiers = DossierService(DossierStore(db))
        self.addCleanup(self.dossiers.close)
        self.rules = load_json("assistant_rules.json")
        self.prompts = load_json("prompts.json")
        self.session_id = self.sessions.create_session("测试会话")

    def make_loop(self, services, rules=None, prompts=None):
        return AssistantLoop(sessions=self.sessions, dossiers=self.dossiers,
                             services=services, rules=rules or self.rules,
                             prompts=prompts or self.prompts)

    def run_turn(self, services, user_text, rules=None, prompts=None, cancel_check=None):
        loop = self.make_loop(services, rules, prompts)
        events = EventStream()
        out = loop.handle(self.session_id, user_text, events, cancel_check)
        return out, events.snapshot()


class TestChitchatFlow(LoopTestBase):
    def test_chitchat_full_flow(self):
        """chitchat 全链：事件 [meta,status,narrative*,done]、消息落库、无工具、无 run。"""
        narrate = stub_narrate()
        out, events = self.run_turn(
            {"route_llm": stub_route("chitchat", reply="你好。想查什么直接说。"),
             "narrate_llm": narrate},
            "你好呀")
        # 事件序列：无 tool_start/scan/card/tool_end
        names = [e["event"] for e in events]
        self.assertEqual(names, ["meta", "status"] + ["narrative"] * names.count("narrative") + ["done"])
        self.assertNotIn("tool_start", names)
        self.assertNotIn("card", names)
        # meta 带会话 id，run_id 为 None（无工具回合）
        meta = events[0]
        self.assertEqual((meta["session_id"], meta["run_id"]), (self.session_id, None))
        # 落库：user + assistant 两条
        msgs = self.sessions.messages(self.session_id)
        self.assertEqual([(m["role"], m["kind"]) for m in msgs],
                         [("user", "text"), ("assistant", "text")])
        self.assertEqual(msgs[1]["content"], "你好。想查什么直接说。")
        # 无工具运行记录；成本 = 路由的真实上游调用（M4 真实计数：stub 路由不走
        # 真实 llm_client → delta 0；原口径"路由咨询 1 次"是快照不可用时的 fallback）
        self.assertEqual(self.sessions.runs_for_session(self.session_id), [])
        self.assertEqual(out["cost"]["requests"], 0)
        self.assertEqual(out["action"], "chitchat")
        self.assertEqual(narrate.calls, [])   # 路由已给 reply，不补叙述

    def test_chitchat_empty_reply_narrates_with_fallback(self):
        """chitchat 空回复 → narrate_llm 补一句；叙述也失败 → 固定话术兜底（不空转）。"""
        out, events = self.run_turn(
            {"route_llm": stub_route("chitchat", reply=""),
             "narrate_llm": stub_narrate(None)},
            "在吗")
        names = [e["event"] for e in events]
        self.assertEqual(names, ["meta", "status"] + ["narrative"] * names.count("narrative") + ["done"])
        self.assertGreaterEqual(names.count("narrative"), 1)   # 兜底话术按句切分
        deltas = [e["delta"] for e in events if e["event"] == "narrative"]
        self.assertEqual("".join(deltas), self.prompts["chitchat_fallback"])
        msgs = self.sessions.messages(self.session_id)
        self.assertEqual(msgs[-1]["content"], self.prompts["chitchat_fallback"])


class TestVerifyFlow(LoopTestBase):
    def test_verify_event_sequence_snapshot(self):
        """§5.3 全事件序列快照：card 在 narrative 前、tool_end 在叙述后、done 带 cost。"""
        verify, narrate = stub_verify(), stub_narrate()
        out, events = self.run_turn(
            {"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
             "verify": verify, "narrate_llm": narrate},
            VERIFY_TEXT)
        names = [e["event"] for e in events]
        self.assertEqual(names, ["meta", "status", "tool_start", "scan",
                                 "progress", "progress", "card",
                                 "narrative", "narrative", "tool_end", "done"])
        # 关键顺序：卡片永远在叙述前；tool_end 收在叙述之后（§5.3 原文顺序）
        self.assertLess(names.index("card"), names.index("narrative"))
        self.assertGreater(names.index("tool_end"), names.index("narrative"))
        # meta：session_id 回填 + run_id=start_run 的 id
        runs = self.sessions.runs_for_session(self.session_id)
        self.assertEqual(events[0]["run_id"], runs[0]["id"])
        self.assertEqual(events[0]["session_id"], self.session_id)
        # tool_start 带 est_s 与 args 预览；scan 是零成本 markers 列表
        ts = events[2]
        self.assertEqual(ts["tool"], "verify_claim")
        self.assertGreater(ts["est_s"], 0)
        self.assertIn(VERIFY_TEXT, ts["args_preview"])
        scan = events[3]
        self.assertIn("来源模糊×1", scan["markers"])
        self.assertIn("初扫", scan["text"])
        # progress 按组件完成顺序，done 单调递增，total = 组件总数
        prog = [e for e in events if e["event"] == "progress"]
        self.assertEqual([p["component"] for p in prog], ["emotion", "evidence"])
        self.assertEqual([p["done"] for p in prog], [1, 2])
        self.assertTrue(all(p["total"] >= 2 for p in prog))
        # card 载荷 = {card_type, card} 包装 + 顶层并置（web 两种读取路径都成立）
        card_evt = events[names.index("card")]
        self.assertEqual(card_evt["card_type"], "conclusion")
        self.assertEqual(card_evt["card"]["label"], "疑似谣言")
        self.assertEqual(card_evt["label"], "疑似谣言")
        self.assertTrue(card_evt["card"]["verdict_basis"].startswith("verdict"))
        # done 成本 = 本回合真实上游调用（M4 真实计数：路由/verify llm_turns/叙述都按
        # 计数器 delta 计——stub 全离线没有真实调用 → 全 0；原口径 1+3+1=5 只在
        # 快照不可用的 fallback 下出现，粗口径已不再入账）
        self.assertEqual(events[-1]["cost"], {"requests": 0, "tokens": 0})

    def test_verify_persistence_and_run_record(self):
        """verify 全链落库：卡片消息、tool_runs 完整、档案 v1+证据批次、返回 dict 形状。"""
        out, events = self.run_turn(
            {"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
             "verify": stub_verify(), "narrate_llm": stub_narrate()},
            VERIFY_TEXT)
        # 消息：user / card(json 载荷) / assistant 叙述
        msgs = self.sessions.messages(self.session_id)
        self.assertEqual([m["kind"] for m in msgs], ["text", "card", "text"])
        card_payload = json.loads(msgs[1]["content"])
        self.assertEqual(card_payload["card_type"], "conclusion")
        self.assertEqual(card_payload["card"]["label"], "疑似谣言")
        # tool_runs 记录完整（§6 M2-3 验收）
        run = self.sessions.runs_for_session(self.session_id)[0]
        self.assertEqual(run["tool"], "verify_claim")
        self.assertEqual(run["args"], {"text": VERIFY_TEXT, "deep": False})
        self.assertEqual(run["status"], "ok")
        self.assertFalse(run["degraded"])
        self.assertGreaterEqual(run["duration_s"], 0.0)
        # M4 真实计数：run.requests/tokens 记工具段真实 delta（stub 无真实上游 → 0）；
        # 原 llm_turns=3 的粗口径只在快照不可用时作 fallback
        self.assertEqual(run["requests"], 0)
        self.assertEqual(run["tokens"], 0)
        # 建档：结论 v1 + 证据批次 1（2 条）
        d_id = out["dossier_id"]
        self.assertEqual(run["dossier_id"], d_id)
        dossier = self.dossiers.store.get_dossier(d_id)
        self.assertEqual(dossier["input_kind"], "text")
        self.assertEqual(self.dossiers.store.conclusions(d_id)[0]["label"], "疑似谣言")
        self.assertEqual(len(self.dossiers.store.evidence(d_id)), 2)
        # 返回 dict 形状（编排者契约的字段集；verify 段另带 claim_answer 过渡答案对象）
        self.assertEqual(set(out.keys()),
                         {"session_id", "reply", "action", "card", "tool_run_id",
                          "dossier_id", "degraded", "cost", "claim_answer"})
        self.assertEqual(out["action"], "verify_claim")
        self.assertFalse(out["degraded"])
        self.assertEqual(out["tool_run_id"], run["id"])
        # finish_run 只调一次且 dossier_id 已补（run 状态不再是 running）
        self.assertEqual(run["status"], "ok")

    def test_verify_service_exception_degrades_honestly(self):
        """verify 抛异常：error 事件 + 诚实降级回复 + run=error + 会话不崩。"""
        out, events = self.run_turn(
            {"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
             "verify": stub_verify(fail=True), "narrate_llm": stub_narrate()},
            VERIFY_TEXT)
        names = [e["event"] for e in events]
        self.assertIn("error", names)
        err_evt = next(e for e in events if e["event"] == "error")
        self.assertEqual(err_evt["code"], "tool_failed")
        self.assertTrue(err_evt["recoverable"])
        self.assertIn("上游取证爆炸", err_evt["message"])
        # 诚实降级回复落库（不是编一个结论）
        self.assertIn("暂时不可用", out["reply"])
        self.assertTrue(out["degraded"])
        msgs = self.sessions.messages(self.session_id)
        self.assertEqual(msgs[-1]["kind"], "text")
        self.assertEqual(msgs[-1]["content"], out["reply"])
        # run 状态 error；无卡片
        run = self.sessions.runs_for_session(self.session_id)[0]
        self.assertEqual(run["status"], "error")
        self.assertTrue(run["degraded"])
        self.assertEqual(run["dossier_id"], None)
        self.assertIsNone(out["card"])
        # done 仍收尾（drain 契约：done/error 才终止）
        self.assertEqual(names[-1], "done")
        # 会话不崩：下一轮 chitchat 正常
        out2, events2 = self.run_turn(
            {"route_llm": stub_route("chitchat", reply="还好吗")},
            "还在吗")
        self.assertEqual([e["event"] for e in events2], ["meta", "status", "narrative", "done"])

    def test_narration_failure_fallback_keeps_card(self):
        """叙述失败（None/err）→ fallback 文案 + card 事件仍在（永不丢结论）。"""
        out, events = self.run_turn(
            {"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
             "verify": stub_verify(), "narrate_llm": stub_narrate(None)},
            VERIFY_TEXT)
        names = [e["event"] for e in events]
        self.assertIn("card", names)
        self.assertLess(names.index("card"), names.index("narrative"))
        narrative = "".join(e["delta"] for e in events if e["event"] == "narrative")
        self.assertIn("核查完成，结论见上方卡片", narrative)
        self.assertIn("官方 1", narrative)   # digest 要点进了 fallback
        self.assertEqual(out["reply"], narrative)
        # 落库的回复也是 fallback（不是空）
        self.assertEqual(self.sessions.messages(self.session_id)[-1]["content"], narrative)

    def test_narrate_context_carries_tool_digest(self):
        """叙述上下文 = build_context(narrate_system, 最近历史, tool_digest)。"""
        narrate = stub_narrate()
        self.run_turn(
            {"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
             "verify": stub_verify(), "narrate_llm": narrate},
            VERIFY_TEXT)
        call = narrate.calls[0]
        self.assertIn("核查研究员", call["system"])       # narrate_system 人设
        self.assertIn(VERIFY_TEXT, call["user"])          # 本轮用户消息
        self.assertIn("[本轮工具结果]", call["user"])     # digest 注入
        self.assertIn("疑似谣言", call["user"])           # 工具结论要点

    def test_verify_passes_cancel_check_to_service(self):
        """cancel_check 透传给 verify 服务（引擎协作取消的接线位）。"""
        seen = {}

        def _svc(text, deep=False, on_component_done=None, cancel_check=None):
            seen["cb"] = cancel_check
            return {"label": "疑似谣言", "risk_score": 0, "degraded_components": [],
                    "agent_error": "", "llm_turns": 0,
                    "evidence_book": {"entries": []}}

        flag = {"cancelled": False}
        self.run_turn({"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
                       "verify": _svc, "narrate_llm": stub_narrate()},
                      VERIFY_TEXT, cancel_check=lambda: flag["cancelled"])
        self.assertIsNotNone(seen["cb"])
        self.assertIs(seen["cb"](), False)
        flag["cancelled"] = True
        self.assertIs(seen["cb"](), True)


class TestOtherToolFlows(LoopTestBase):
    def test_speech_flow_card_and_dossier(self):
        """analyze_speech：speech 卡（四档+三维摘要）+ classic 建档 + tool_end 带 dossier_id。"""
        out, events = self.run_turn(
            {"route_llm": stub_route("analyze_speech", text="大家都别买！"),
             "speech": stub_speech(), "narrate_llm": stub_narrate()},
            "大家都别买！")
        names = [e["event"] for e in events]
        # speech 不属 verify 类：无 scan
        self.assertNotIn("scan", names)
        self.assertEqual(names.index("card"), names.index("narrative") - 1)
        card_evt = next(e for e in events if e["event"] == "card")
        self.assertEqual(card_evt["card_type"], "speech")
        self.assertEqual(card_evt["card"]["conclusion"], "疑似情绪化谣言")
        self.assertEqual(card_evt["card"]["emotion"]["emotion_tendency"], "恐惧")
        tool_end = next(e for e in events if e["event"] == "tool_end")
        self.assertEqual(tool_end["dossier_id"], out["dossier_id"])
        # classic 结论按四档标签建档
        cs = self.dossiers.store.conclusions(out["dossier_id"])
        self.assertEqual(cs[0]["label"], "疑似情绪化谣言")
        self.assertIsNone(cs[0]["risk_score"])

    def test_check_heat_snapshot_and_no_scan(self):
        """check_heat：话题建档 + 快照落库（值逐字段一致）+ degraded 如实透传。"""
        detail = {"x": 5, "y": 100000, "z": 500, "score": 44.8, "band": "低关注",
                  "profile": "spec", "proxy_flags": ["y_proxy"], "quality": "exact",
                  "degraded": True, "error": "检索部分失败"}
        out, events = self.run_turn(
            {"route_llm": stub_route("check_heat", topic="某话题"),
             "heat": stub_heat(detail), "narrate_llm": stub_narrate()},
            "某话题现在多热")
        card_evt = next(e for e in events if e["event"] == "card")
        self.assertEqual(card_evt["card_type"], "heat")
        self.assertTrue(card_evt["card"]["degraded"])
        self.assertEqual(card_evt["card"]["proxy_flags"], ["y_proxy"])
        # 档案是话题型；快照与 stub 值逐字段一致（与 store 对拍）
        dossier = self.dossiers.store.get_dossier(out["dossier_id"])
        self.assertEqual(dossier["input_kind"], "topic")
        snaps = self.dossiers.store.snapshots(out["dossier_id"])
        self.assertEqual(len(snaps), 1)
        self.assertEqual((snaps[0]["x"], snaps[0]["y"], snaps[0]["z"]),
                         (5.0, 100000.0, 500.0))
        self.assertEqual(snaps[0]["e_score"], 44.8)
        self.assertEqual(snaps[0]["profile"], "spec")
        self.assertEqual(snaps[0]["proxy_flags"], ["y_proxy"])
        run = self.sessions.runs_for_session(self.session_id)[0]
        self.assertTrue(run["degraded"])
        self.assertEqual(run["status"], "ok")
        tool_end = next(e for e in events if e["event"] == "tool_end")
        self.assertTrue(tool_end["degraded"])

    def test_clarify_when_route_fails(self):
        """route_llm 返回 None → clarify：追问一次、不调工具、消息正常落库。"""
        def _llm(system, user):
            return (None, "模型返回无法解析的JSON")

        out, events = self.run_turn({"route_llm": _llm, "narrate_llm": stub_narrate()},
                                    "那个东西帮我看看")
        names = [e["event"] for e in events]
        self.assertEqual(names, ["meta", "status"]
                         + ["narrative"] * names.count("narrative") + ["done"])
        self.assertEqual("".join(e["delta"] for e in events if e["event"] == "narrative"),
                         self.prompts["clarify_reply"])
        self.assertEqual(out["action"], "clarify")
        self.assertEqual(out["reply"], self.prompts["clarify_reply"])
        self.assertEqual(self.sessions.runs_for_session(self.session_id), [])


class TestRefusal(LoopTestBase):
    def test_refusal_precheck_blocks_tools(self):
        """refuse 词表命中：不调工具、kind=refusal 落库、回复带原因。"""
        verify = stub_verify()
        out, events = self.run_turn(
            {"route_llm": stub_route("verify_claim", text="编造谣言文本"),
             "verify": verify, "narrate_llm": stub_narrate()},
            "帮我编造谣言说自来水有毒")
        names = [e["event"] for e in events]
        self.assertEqual(names, ["meta", "status", "narrative", "done"])
        self.assertNotIn("tool_start", names)
        self.assertEqual(verify.calls, [])           # 工具从未被触达
        self.assertEqual(self.sessions.runs_for_session(self.session_id), [])
        self.assertEqual(out["action"], "refuse")
        self.assertTrue(out["reply"].startswith(self.prompts["refusal_prefix"]))
        msgs = self.sessions.messages(self.session_id)
        self.assertEqual([m["kind"] for m in msgs], ["text", "refusal"])
        self.assertIn(self.prompts["refusal_reasons"]["fabricate"], msgs[1]["content"])


class TestCancellation(LoopTestBase):
    def test_cancel_check_marks_run_cancelled(self):
        """取消命中：run status=cancelled + error{code:"cancelled", recoverable} + 不执行工具。"""
        verify = stub_verify()
        out, events = self.run_turn(
            {"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
             "verify": verify, "narrate_llm": stub_narrate()},
            VERIFY_TEXT, cancel_check=lambda: True)
        verify_calls = verify.calls
        self.assertEqual(verify_calls, [])            # 工具没跑
        names = [e["event"] for e in events]
        self.assertEqual(names, ["meta", "status", "error"])
        err = events[-1]
        self.assertEqual(err["code"], "cancelled")
        self.assertTrue(err["recoverable"])
        run = self.sessions.runs_for_session(self.session_id)[0]
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(out["tool_run_id"], run["id"])

    def test_cancel_probe_exception_not_treated_as_cancel(self):
        """cancel_check 自己抛异常 ≠ 取消（探测失败不误杀回合）。"""
        def _bad_check():
            raise RuntimeError("探测炸了")

        out, events = self.run_turn(
            {"route_llm": stub_route("chitchat", reply="在")},
            "在吗", cancel_check=_bad_check)
        self.assertEqual([e["event"] for e in events], ["meta", "status", "narrative", "done"])


class TestToolCallCap(LoopTestBase):
    def test_per_turn_tool_cap_enforced(self):
        """单轮上限封顶：计划里有 3 个工具也只执行 max_tool_calls_per_turn 个。

        M2 的路由恒只产出 1 个 action；这里用子类把执行计划撑到 3 个，锁定
        "上限封顶、超限不再执行"（上限是给 M3 指纹'先问再查'第二槽位预留的）。
        """
        calls = {"n": 0}

        def _svc(text, deep=False, on_component_done=None, cancel_check=None):
            calls["n"] += 1
            return {"label": "疑似谣言", "risk_score": 1, "degraded_components": [],
                    "agent_error": "", "llm_turns": 0, "evidence_book": {"entries": []}}

        class MultiPlanLoop(AssistantLoop):
            def _route_plan(self, user_text, **kwargs):
                # 模拟 M3 多槽位路由：一轮产出 3 个工具 action
                # （**kwargs：复核修复后基类签名带 history_hint/id_fallback 透传参数）
                return {"kind": "tools", "steps": [
                    {"action": "verify_claim", "args": {"text": f"第{i}条"}} for i in range(3)]}

        rules = dict(self.rules)
        rules["max_tool_calls_per_turn"] = 2
        loop = MultiPlanLoop(sessions=self.sessions, dossiers=self.dossiers,
                             services={"verify": _svc, "narrate_llm": stub_narrate()},
                             rules=rules, prompts=self.prompts)
        events = EventStream()
        out = loop.handle(self.session_id, "批量核查", events, None)
        self.assertEqual(calls["n"], 2)               # 第 3 个计划项被上限拦下
        runs = self.sessions.runs_for_session(self.session_id)
        self.assertEqual(len(runs), 2)
        self.assertEqual(len([e for e in events.snapshot() if e["event"] == "tool_start"]), 2)
        self.assertEqual(len([e for e in events.snapshot() if e["event"] == "tool_end"]), 2)


class TestSummaryTrigger(LoopTestBase):
    def test_summary_fires_once_per_threshold_and_cached(self):
        """trigger_messages=3：第 2 轮触发一次摘要（system_note 落库），第 3 轮不重复。"""
        rules = dict(self.rules)
        rules["summary_model"] = dict(self.rules["summary_model"], trigger_messages=3)
        summarize = stub_summarize()
        services = {"route_llm": stub_route("chitchat", reply="好的。"),
                    "summarize_llm": summarize}
        self.run_turn(services, "第一问", rules=rules)      # 2 条消息 < 3：不触发
        self.assertEqual(summarize.calls, [])
        self.run_turn(services, "第二问", rules=rules)      # 累计 4 条 ≥ 3：触发一次
        self.assertEqual(len(summarize.calls), 1)
        msgs = self.sessions.messages(self.session_id)
        notes = [m for m in msgs if m["role"] == "system_note"]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0]["content"].startswith("[早期对话摘要]"))
        self.assertIn("自来水", notes[0]["content"])
        self.run_turn(services, "第三问", rules=rules)      # 摘要后仅 2 条新消息：不重复
        self.assertEqual(len(summarize.calls), 1)

    def test_summary_failure_falls_back_silently(self):
        """摘要失败：不落备注（context.py 的确定性拼接兜底），回合照常收尾。"""
        rules = dict(self.rules)
        rules["summary_model"] = dict(self.rules["summary_model"], trigger_messages=2)
        out, events = self.run_turn(
            {"route_llm": stub_route("chitchat", reply="好。"),
             "summarize_llm": stub_summarize(None)},
            "第一问", rules=rules)
        msgs = self.sessions.messages(self.session_id)
        self.assertFalse([m for m in msgs if m["role"] == "system_note"])
        self.assertEqual(events[-1]["event"], "done")


class TestRobustness(LoopTestBase):
    def test_handle_never_raises_and_emits_error(self):
        """会话库炸穿（未预期异常）→ error{internal} 收尾，handle 不向外抛、会话不崩。"""

        class BoomStore:
            """所有方法都炸的会话库替身：模拟最坏的基础设施故障。"""

            def add_message(self, *a, **k):
                raise RuntimeError("库炸了")

            def messages(self, *a, **k):
                raise RuntimeError("库炸了")

        loop = AssistantLoop(sessions=BoomStore(), dossiers=self.dossiers,
                             services={"route_llm": stub_route("chitchat", reply="嗯。")},
                             rules=self.rules, prompts=self.prompts)
        events = EventStream()
        out = loop.handle(self.session_id, "随便说", events, None)
        names = [e["event"] for e in events.snapshot()]
        self.assertEqual(names, ["error"])
        self.assertEqual(events.snapshot()[-1]["code"], "internal")
        self.assertFalse(events.snapshot()[-1]["recoverable"])
        self.assertTrue(out["degraded"])

    def test_return_dict_shape_for_meta_turn(self):
        out, events = self.run_turn({"route_llm": stub_route("chitchat", reply="嗯。")}, "嗯")
        self.assertEqual(set(out.keys()),
                         {"session_id", "reply", "action", "card", "tool_run_id",
                          "dossier_id", "degraded", "cost"})
        self.assertIsNone(out["card"])
        self.assertIsNone(out["tool_run_id"])


# ===========================================================================
# 轮预算（工作指导 §8 新增契约，M2 整合追加）：
#   handle 进门即 deadline_scope_at(monotonic()+budget)，环境变量 CHAT_TURN_DEADLINE_S
#   优先、缺省 rules["turn_deadline_s"]=180、<=0 不限；工具执行期间 scope 处于激活
#   （stub 里 llm_cache.deadline_at() 非 None）；超时 → error{deadline} + 卡片如实
#   缺失（永不因超时丢结论，也绝不补编）+ tool_run status="timeout" + 诚实降级文案。
# ===========================================================================

def budget_registry(sleep_s: float = 0.0, probe=None, ok: bool = True):
    """带可编程慢查桩的注册表：verify_claim 桩不产卡片，可探测预算激活状态。"""
    from mylocalai.assistant.registry import ToolRegistry, deep_param, text_param

    reg = ToolRegistry()

    def _slow_verify(ctx, text, deep=False):
        if probe is not None:
            probe(llm_cache.deadline_at(), llm_cache.remaining_seconds())
        if sleep_s:
            time.sleep(sleep_s)
        return {"ok": ok, "card": None, "digest": "预算桩无结论",
                "cost": {"requests": 0, "tokens": 0}, "dossier_id": None,
                "degraded": not ok}

    reg.register("verify_claim", _slow_verify, params=["text", "deep"],
                 permission="write", cost="heavy", est_s=60.0,
                 description="轮预算测试桩（不产卡片）",
                 validators={"text": text_param(), "deep": deep_param})
    return reg


class TestTurnBudget(LoopTestBase):
    """轮预算契约：scope 全回合激活、超时诚实降级、0=不限、缺省走 rules 值。"""

    def make_budget_loop(self, services, reg):
        return AssistantLoop(registry=reg, sessions=self.sessions, dossiers=self.dossiers,
                             services=services, rules=self.rules, prompts=self.prompts)

    @staticmethod
    def _without_env():
        """patch.dict 上下文内清掉 CHAT_TURN_DEADLINE_S（退出时自动还原）。"""
        return mock.patch.dict(os.environ)

    def test_budget_scope_active_during_tool_and_normal_turn_unaffected(self):
        """预算内完成不受影响：scope 在工具执行期间处于激活（deadline_at 非 None）。"""
        seen = {}

        def _probe(at, left):
            seen["at"], seen["left"] = at, left

        reg = budget_registry(probe=_probe)
        loop = self.make_budget_loop(
            {"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
             "narrate_llm": stub_narrate()}, reg)
        with self._without_env():
            os.environ.pop("CHAT_TURN_DEADLINE_S", None)   # 走 rules 缺省预算（180s）
            events = EventStream()
            out = loop.handle(self.session_id, VERIFY_TEXT, events, None)
        names = [e["event"] for e in events.snapshot()]
        self.assertEqual(names[-1], "done")
        self.assertNotIn("error", names)
        self.assertEqual(out["action"], "verify_claim")
        # 工具执行期间回合预算已激活（stub 里 llm_cache.deadline_at() 非 None）
        self.assertIsNotNone(seen["at"])
        self.assertGreater(seen["left"], 0)
        run = self.sessions.runs_for_session(self.session_id)[0]
        self.assertEqual(run["status"], "ok")

    def test_overrun_budget_deadline_error_timeout_run_and_no_card(self):
        """stub 慢查超预算：error(deadline) + 卡片如实缺失 + run 记 timeout + 诚实降级文案。"""
        reg = budget_registry(sleep_s=0.6)
        loop = self.make_budget_loop(
            {"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
             "narrate_llm": stub_narrate()}, reg)
        with mock.patch.dict(os.environ, {"CHAT_TURN_DEADLINE_S": "0.2"}):
            events = EventStream()
            out = loop.handle(self.session_id, VERIFY_TEXT, events, None)
        events = events.snapshot()
        names = [e["event"] for e in events]
        self.assertIn("error", names)
        err = next(e for e in events if e["event"] == "error")
        self.assertEqual(err["code"], "deadline")
        self.assertTrue(err["recoverable"])
        self.assertEqual(names[-1], "done")           # 仍以 done 收尾（drain 契约）
        # 卡片如实缺失：桩没产卡片，超时链路也不编一张（永不因超时丢结论 ≠ 编结论）
        self.assertNotIn("card", names)
        self.assertIsNone(out["card"])
        # 回复 = prompts.deadline_reply（诚实降级，不发起叙述、不编结论）
        self.assertEqual(out["reply"], self.prompts["deadline_reply"])
        self.assertEqual("".join(e["delta"] for e in events if e["event"] == "narrative"),
                         self.prompts["deadline_reply"])
        self.assertTrue(out["degraded"])
        # run 记录：status=timeout、degraded、无 dossier
        run = self.sessions.runs_for_session(self.session_id)[0]
        self.assertEqual(run["status"], "timeout")
        self.assertTrue(run["degraded"])
        self.assertIsNone(run["dossier_id"])
        self.assertEqual(out["tool_run_id"], run["id"])
        # 成本只剩路由段的调用（M4 真实计数：stub 下 delta=0；预算已尽不再发起
        # 叙述等新的上游调用——与旧口径"只剩路由 1 次"同一意图）
        self.assertEqual(out["cost"]["requests"], 0)

    def test_zero_budget_disables_limit(self):
        """CHAT_TURN_DEADLINE_S=0 → 不限：超过"名义预算"的慢查照常完成、无 error。"""
        reg = budget_registry(sleep_s=0.4)
        loop = self.make_budget_loop(
            {"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
             "narrate_llm": stub_narrate()}, reg)
        with mock.patch.dict(os.environ, {"CHAT_TURN_DEADLINE_S": "0"}):
            events = EventStream()
            out = loop.handle(self.session_id, VERIFY_TEXT, events, None)
        names = [e["event"] for e in events.snapshot()]
        self.assertNotIn("error", names)
        self.assertEqual(names[-1], "done")
        self.assertEqual(out["action"], "verify_claim")
        self.assertFalse(out["degraded"])
        run = self.sessions.runs_for_session(self.session_id)[0]
        self.assertEqual(run["status"], "ok")

    def test_budget_exhausted_before_tool_skips_execution(self):
        """预算开局即尽（如路由耗尽）：不开新工具（无 tool_start）、run 记 timeout。"""
        reg = budget_registry()
        loop = self.make_budget_loop(
            {"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
             "narrate_llm": stub_narrate()}, reg)
        with mock.patch.object(loop, "_budget_left", return_value=-1.0):
            events = EventStream()
            out = loop.handle(self.session_id, VERIFY_TEXT, events, None)
        events = events.snapshot()
        names = [e["event"] for e in events]
        self.assertNotIn("tool_start", names)
        self.assertEqual(next(e for e in events if e["event"] == "error")["code"], "deadline")
        self.assertEqual(names[-1], "done")
        self.assertEqual(out["reply"], self.prompts["deadline_reply"])
        run = self.sessions.runs_for_session(self.session_id)[0]
        self.assertEqual(run["status"], "timeout")
        self.assertEqual(out["tool_run_id"], run["id"])


# ===========================================================================
# L4 参谋装配（source_profile）：_qualified_profile 纯函数 + _verify 生产路径透传。
# 口径：空库 → {}（照传，引擎对空 dict 逐位不变）；读取故障 → None；
# owner 缺位 → None（行为与装配前逐位一致）。
# ===========================================================================

def _seed_refute_history(service, domain: str, n: int = 2) -> None:
    """预置 n 个高风险档案，各带同一域名的一条反驳证据（refuted_dossiers=n）。"""
    for i in range(n):
        did = service.store.create_dossier(f"证伪史{i}", f"传闻{i}")
        service.store.add_conclusion(did, "高风险谣言", 80.0,
                                     as_of="2026-10-31T10:00:00+08:00")
        service.store.add_evidence(did, 1, [{
            "url": f"https://{domain}/post{i}", "domain": domain,
            "domain_tier": "非官方（门户）", "source_kind": "非官方（门户）",
            "stance": "反驳", "title": "辟谣", "snippet": "经查不实",
            "fetched_at": "2026-10-31T09:00:00+08:00"}])


def capture_agent_factory(captured: dict):
    """RumorAgent 替身工厂：只捕获构造 kwargs，analyze 返回最小合法结果。"""

    class _CaptureAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def analyze(self, text, cancel_check=None):
            return {"label": "疑似谣言", "risk_score": 10, "degraded_components": [],
                    "agent_error": "", "llm_turns": 0, "evidence_book": {"entries": []}}

    return _CaptureAgent


class TestQualifiedProfile(LoopTestBase):
    """_qualified_profile 单测：达标线缺省/配置/兜底、空库 {}、读取故障 None。"""

    def _func(self):
        from mylocalai.assistant.loop import _qualified_profile
        return _qualified_profile

    def test_empty_store_returns_empty_dict(self):
        """空档案库 → {}（不是 None）：画像启用但暂无达标信源，引擎逐位不变。"""
        self.assertEqual(self._func()(self.dossiers.store, self.rules), {})

    def test_refute_history_within_threshold_qualifies(self):
        """两个高风险档案均被同一域名反驳 → {domain: {"refutes": 2}}（rules 显式给 2）。"""
        _seed_refute_history(self.dossiers, "bad.example.com", n=2)
        rules = dict(self.rules, source_profile={"min_refutes": 2})
        self.assertEqual(self._func()(self.dossiers.store, rules),
                         {"bad.example.com": {"refutes": 2}})

    def test_higher_min_refutes_excludes_domain(self):
        """min_refutes=3：只证伪 2 个档案的域名不入选 → {}。"""
        _seed_refute_history(self.dossiers, "bad.example.com", n=2)
        rules = dict(self.rules, source_profile={"min_refutes": 3})
        self.assertEqual(self._func()(self.dossiers.store, rules), {})

    def test_missing_or_broken_section_defaults_to_two(self):
        """缺 source_profile 段 / min_refutes 写坏 → 都按 2 兜底，不抛。"""
        _seed_refute_history(self.dossiers, "bad.example.com", n=2)
        func = self._func()
        self.assertEqual(func(self.dossiers.store, {}),
                         {"bad.example.com": {"refutes": 2}})
        self.assertEqual(func(self.dossiers.store, {"source_profile": {}}),
                         {"bad.example.com": {"refutes": 2}})
        self.assertEqual(func(self.dossiers.store, {"source_profile": {"min_refutes": "abc"}}),
                         {"bad.example.com": {"refutes": 2}})
        self.assertEqual(func(self.dossiers.store, {"source_profile": "junk"}),
                         {"bad.example.com": {"refutes": 2}})

    def test_store_failure_returns_none(self):
        """画像读取故障（source_profiles 炸）→ None：装配是增益面，绝不挡核查。"""
        with mock.patch("mylocalai.dossier.insights.source_profiles",
                        side_effect=RuntimeError("库炸了")):
            self.assertIsNone(self._func()(self.dossiers.store, self.rules))


class TestVerifySourceProfileWiring(LoopTestBase):
    """装配透传：经生产路径 _real_service("verify", rules, owner) 取 _verify 调一次。"""

    def _verify_via_production_path(self, owner):
        from mylocalai.assistant.loop import _real_service
        return _real_service("verify", self.rules, owner)

    def test_production_verify_passes_qualified_profile(self):
        """生产路径 _verify 把 source_profile= 传给 RumorAgent，值来自 _qualified_profile。"""
        _seed_refute_history(self.dossiers, "bad.example.com", n=2)
        captured = {}
        loop = self.make_loop({})
        verify = self._verify_via_production_path(loop)
        with mock.patch("mylocalai.rumor.agent.RumorAgent",
                        capture_agent_factory(captured)):
            out = verify(VERIFY_TEXT)
        self.assertEqual(captured.get("source_profile"),
                         {"bad.example.com": {"refutes": 2}})
        # 值确实来自 _qualified_profile（与纯函数直接调用对拍）
        from mylocalai.assistant.loop import _qualified_profile
        self.assertEqual(captured.get("source_profile"),
                         _qualified_profile(self.dossiers.store, self.rules))
        self.assertEqual(out["label"], "疑似谣言")   # 引擎返回值原样透传

    def test_production_verify_empty_store_still_passes_empty_profile(self):
        """空库（{}）也照传——装配不缺席；引擎对空 dict 逐位不变。"""
        captured = {}
        loop = self.make_loop({})
        verify = self._verify_via_production_path(loop)
        with mock.patch("mylocalai.rumor.agent.RumorAgent",
                        capture_agent_factory(captured)):
            verify(VERIFY_TEXT)
        self.assertEqual(captured.get("source_profile"), {})

    def test_verify_without_owner_passes_none(self):
        """owner=None（无档案库可取）→ source_profile=None，行为与装配前一致。"""
        captured = {}
        verify = self._verify_via_production_path(None)
        with mock.patch("mylocalai.rumor.agent.RumorAgent",
                        capture_agent_factory(captured)):
            verify(VERIFY_TEXT)
        self.assertIsNone(captured.get("source_profile"))

    def test_rules_json_has_source_profile_section(self):
        """assistant_rules.json 同步带 source_profile 段（用户可改的缺省口径）。"""
        self.assertEqual(self.rules.get("source_profile"), {"min_refutes": 2})


if __name__ == "__main__":
    unittest.main()
