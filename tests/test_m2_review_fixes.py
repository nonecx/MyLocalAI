# -*- coding: utf-8 -*-
"""M2 独立复核修复（2026-10-31）回归测试。

按《docs/工作指导-智能助手重塑-2026-10-30》§5.3/§5.4 复核报告修复四条：

  修复1（P1-1）引擎级协作取消接线（§5.3）：
    - RumorAgent.analyze(cancel_check=...)：主线程等 worker 的循环里轮询取消位，
      命中 → state.cancel() + 提前收工（mode="cancelled"，沿用超时路径形态：
      已完成组件结果保留、未完成组件如实缺席）；
    - 不传 cancel_check 的守卫：analyze 结果与基线逐位一致（对拍既有离线样本）；
    - classic judge(cancel_check=...)：阶段间/边界复核前轮询，命中 → 降级结果
      （缺哪段如实标注）且不抛异常，不再发起边界复核/报告等新调用；
    - loop 层：慢 verify 服务内部引擎式轮询命中 → 回合以 error{cancelled} 收尾。

  修复2（P1-2）活跃档案卡进路由 + 缺 id 的确定性兜底（§5.4 ②）：
    - loop 组装"活跃档案卡 + 最近对话摘要"作 history_hint 传入 router.route；
    - 路由产出 recheck_dossier/get_dossier/export_card 且缺 id → 取本会话最近
      一次 tool_run 的 dossier_id 兜底（无则照旧 clarify，绝不瞎猜其它数字）。

  修复3（P2）test_import_rules 的 FORBIDDEN 补 assistant/dossier/web 三行。
  修复4（P2）core/card.py 合并前校验 card 段形状：形状不合法 → 整段弃用退内置
    默认（与坏 JSON 同路径），derive_card 不抛。

全离线：stub LLM / stub 服务 / tempfile 库，零网络零配额。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ===========================================================================
# 修复1（P1-1）：引擎级协作取消
# ===========================================================================

from langchain_core.messages import AIMessage  # noqa: E402

import mylocalai.classic.judge as core_judge  # noqa: E402
from mylocalai.core import tiering  # noqa: E402
from mylocalai.rumor import verdict as V  # noqa: E402
from mylocalai.rumor.agent import RumorAgent  # noqa: E402

from tests.test_assistant_loop import (  # noqa: E402
    VERIFY_TEXT,
    LoopTestBase,
    stub_narrate,
    stub_route,
    stub_verify,
)
from tests.test_rumor_agent_offline import (  # noqa: E402
    RUMOR_TEXT,
    ScriptedModel,
    call_all_tools,
    honest_final,
    stub_funcs,
)

BASE_EMOTION = {"emotion_score": 8.5, "emotion_tendency": "恐惧", "keywords": ["气死了"]}
BASE_ATTACK = {"attack_target": "针对部门", "aggression_level": "中", "attack_type": "指控"}
BASE_LOGIC = {"argument": "a", "conclusion": "c", "logic_relevance": "弱",
              "logic_fallacy": "诉诸恐惧"}


def patch_judge_modules(emotion=None, aggression=None, logic=None, report="报告正文"):
    """把 judge 的三个模块与报告出口换成离线 stub（test_core_judge_attention 同法）。

    传 dict → return_value 桩；传可调用对象 → 直接替换（new=），供测取消时序。
    """
    def _stub(name, value, default):
        if callable(value):
            return mock.patch.object(core_judge, name, new=value)
        return mock.patch.object(core_judge, name,
                                 return_value=value if value is not None else default)

    return (_stub("analyze_emotion", emotion, dict(BASE_EMOTION)),
            _stub("analyze_aggression", aggression, dict(BASE_ATTACK)),
            _stub("analyze_logic", logic, dict(BASE_LOGIC)),
            mock.patch.object(core_judge, "generate_report", return_value=report))


class TestAgentCooperativeCancel(unittest.TestCase):
    """RumorAgent.analyze(cancel_check=...)：轮询命中 → 提前收工 + 不再开新调用。"""

    def test_cancel_hit_returns_partial_quickly_and_stops_calls(self):
        """取消命中：mode=cancelled、已完成组件结果保留、未完成如实缺席；
        返回后 stub 组件的调用计数冻结（fill_missing 跳过 + state.cancel 生效）。"""
        funcs = stub_funcs(delay=0.0)
        evidence_started = threading.Event()
        probes = {"n": 0}

        def slow_evidence(text):
            evidence_started.set()   # 与快速组件同时开工；2s 才返回 → 取消时仍在跑
            time.sleep(2.0)
            return {"claim": "x", "entries": [], "error": None, "queries": [],
                    "stance_summary": {}, "verdict_hint": "无证据", "skipped": None,
                    "llm_error": None}

        funcs["evidence"] = slow_evidence
        model = ScriptedModel(script=[call_all_tools, honest_final], delay=0.05)
        agent = RumorAgent(llm=model, funcs=funcs, use_default_llm=False, deadline_s=10)

        def cancel_check():
            probes["n"] += 1
            # 第一次轮询不命中（组件刚开工）；之后命中——此时快速组件必已入库
            return probes["n"] >= 2 and evidence_started.is_set()

        t0 = time.perf_counter()
        r = agent.analyze(RUMOR_TEXT, cancel_check=cancel_check)
        elapsed = time.perf_counter() - t0

        self.assertEqual(r["mode"], "cancelled")
        self.assertIn("取消", r["agent_error"])
        self.assertLess(elapsed, 5.0, f"取消应提前收工而非等满时限：{elapsed:.2f}s")
        # 已完成组件的结果如实保留（emotion/aggression 是即时桩，取消前必已完成）
        self.assertEqual((r.get("emotion") or {}).get("emotion_tendency"), "情绪化宣泄")
        self.assertEqual((r.get("attack") or {}).get("aggression_level"), "中")
        # 未完成的 evidence 如实缺席：evidence_book=None。assess 的既有语义里
        # "证据缺席"不进 degraded_components（只有带 error 的证据结果才算），
        # 取消本身由 mode="cancelled" + agent_error 如实标注——与超时路径同形。
        self.assertIsNone(r["evidence_book"])
        self.assertEqual(r["mode"], "cancelled")
        self.assertIn("取消", r["agent_error"])
        # 结论仍由确定性组件给出（与既有离线样本同判）
        self.assertEqual(r["label"], V.LABEL_HIGH)
        # stub 的后续调用不再发生：返回前后组件计数逐位一致（证明真的停了）
        before = {k: getattr(v, "calls", None) for k, v in funcs.items()}
        time.sleep(2.5)   # 超过 evidence 桩的剩余时长，给后台线程足够的"犯错"窗口
        after = {k: getattr(v, "calls", None) for k, v in funcs.items()}
        self.assertEqual(before, after,
                         "取消后不应再有组件被调用（fill_missing 跳过 + ensure 拒开新组件）")

    def test_no_cancel_check_matches_baseline(self):
        """守卫：不传 cancel_check 与显式传 None 的结果逐位一致，且对得上既有离线样本
        （mode=agent、label=高风险谣言、llm_turns=2、解释来自 agent）。"""
        def run(**kw):
            funcs = stub_funcs(delay=0.0)
            model = ScriptedModel(script=[call_all_tools, honest_final])
            r = RumorAgent(llm=model, funcs=funcs, use_default_llm=False).analyze(RUMOR_TEXT, **kw)
            return r

        base = run()
        explicit_none = run(cancel_check=None)
        # 计时类字段天然随运行波动，其余字段必须逐位一致
        volatile = {"elapsed_s", "component_timings", "component_spans"}
        strip = lambda d: {k: v for k, v in d.items() if k not in volatile}
        self.assertEqual(strip(base), strip(explicit_none),
                         "不传 cancel_check 的行为必须与旧版逐位一致")
        # 对拍既有离线样本的稳定口径（tests/test_rumor_agent_offline 同款断言）
        self.assertEqual(base["mode"], "agent")
        self.assertEqual(base["label"], V.LABEL_HIGH)
        self.assertEqual(base["llm_turns"], 2)
        self.assertEqual(base["explanation_source"], "agent")
        self.assertEqual(base["degraded_components"], [])


class TestJudgeCooperativeCancel(unittest.TestCase):
    """classic judge(cancel_check=...)：阶段间/边界复核前轮询，命中降级不抛。"""

    def test_cancel_before_first_stage_degrades_all(self):
        """第 0 位轮询命中：三个模块都不发起，全段如实降级 + 本地模板报告，不抛。"""
        emo_calls, report_calls = [], []

        def emo(text):
            emo_calls.append(text)
            return dict(BASE_EMOTION)

        patches = patch_judge_modules(emotion=emo)
        with patches[0], patches[1], patches[2], patches[3] as rep_mock:
            out = core_judge.judge("一段测试文本", cancel_check=lambda: True)
        self.assertEqual(emo_calls, [], "取消命中后不应再发起任何模块调用")
        rep_mock.assert_not_called()
        for comp in ("emotion", "attack", "logic"):
            self.assertIn("取消", str(out[comp].get("error")), comp)
        self.assertIn(out["conclusion"], tiering._ALL_TIERS)
        self.assertEqual(out["degraded_components"], ["emotion", "attack", "logic", "report"])
        self.assertIn("本地模板回退", out["report"])

    def test_cancel_between_stages_collects_completed(self):
        """并行等待期间命中：已完成阶段（attack/logic）原样收下，未完成（emotion）
        如实标注，边界复核与报告 LLM 不再发起，快速返回且不抛异常。"""
        probes = {"n": 0}

        def slow_emotion(text):
            time.sleep(0.5)   # 取消时（~0.2s）必然未完成
            return dict(BASE_EMOTION)

        def cancel_check():
            probes["n"] += 1
            return probes["n"] >= 3   # 预检(1)不命中，两次等待片后命中（给快桩留足完成窗口）

        patches = patch_judge_modules(emotion=slow_emotion)
        t0 = time.perf_counter()
        with patches[0], patches[1], patches[2], patches[3] as rep_mock:
            out = core_judge.judge("一段测试文本", cancel_check=cancel_check)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 2.0, f"取消应快速返回（不等慢桩跑完）：{elapsed:.2f}s")
        rep_mock.assert_not_called()          # 报告 LLM 不再发起
        self.assertEqual(out["attack"], dict(BASE_ATTACK))   # 已完成阶段收下
        self.assertEqual(out["logic"], dict(BASE_LOGIC))
        self.assertIn("取消", str(out["emotion"].get("error")))  # 未完成如实标注
        self.assertIn("emotion", out["degraded_components"])
        self.assertIn("report", out["degraded_components"])
        self.assertIn(out["conclusion"], tiering._ALL_TIERS)
        self.assertIn("本地模板回退", out["report"])

    def test_cancel_before_report_stage_skips_report_llm(self):
        """报告阶段前的轮询命中：组件全部完成（预计算），只省掉报告那次 LLM 调用。"""
        probes = {"n": 0}

        def cancel_check():
            probes["n"] += 1
            return probes["n"] >= 2   # 预检(1)不命中；报告阶段(2)命中

        patches = patch_judge_modules()
        with patches[0], patches[1], patches[2], patches[3] as rep_mock:
            out = core_judge.judge("一段测试文本", emotion=dict(BASE_EMOTION),
                                   attack=dict(BASE_ATTACK), logic=dict(BASE_LOGIC),
                                   cancel_check=cancel_check)
        rep_mock.assert_not_called()
        self.assertEqual(out["emotion"], dict(BASE_EMOTION))   # 预计算组件原样保留
        self.assertEqual(out["attack"], dict(BASE_ATTACK))
        self.assertEqual(out["logic"], dict(BASE_LOGIC))
        self.assertEqual(out["degraded_components"], ["report"])
        self.assertIn("本地模板回退", out["report"])

    def test_no_cancel_check_matches_none_and_uses_report_llm(self):
        """守卫：不传 cancel_check 与显式传 None 逐位一致，且正常路径仍走报告 LLM。"""
        patches = patch_judge_modules()
        with patches[0], patches[1], patches[2], patches[3] as rep_mock:
            base = core_judge.judge("一段测试文本")
            again = core_judge.judge("一段测试文本", cancel_check=None)
        rep_mock.assert_called()   # 正常路径报告调用照旧
        self.assertEqual(base, again)
        self.assertEqual(base["degraded_components"], [])
        self.assertEqual(base["report"], "报告正文")


class TestLoopCancelDuringSlowTool(LoopTestBase):
    """loop 层：慢 verify 服务内部引擎式轮询命中 → 回合以 error{cancelled} 收尾。"""

    def test_cancel_during_slow_verify_finishes_with_error_cancelled(self):
        """工具执行中用户断开：服务内探测命中提前收工（降级结果），回合层随后
        error{cancelled} 收尾，不再叙述、run 记 cancelled。"""
        flag = {"cancelled": False}
        service_calls = []

        def _slow_verify(text, deep=False, on_component_done=None, cancel_check=None):
            service_calls.append({"text": text, "cancel_check": cancel_check})
            time.sleep(0.05)
            flag["cancelled"] = True          # 模拟用户此刻断开（drain 置位取消）
            if cancel_check is not None and cancel_check():
                # 引擎式协作取消：提前收工，返回已完成组件的降级结论（同超时形态）
                return {"label": "存疑", "risk_score": 40, "signals": ["取消收工"],
                        "degraded_components": ["evidence"],
                        "agent_error": "分析已取消（用户中断）", "llm_turns": 1,
                        "evidence_book": {"entries": []}}
            return {"label": "疑似谣言", "risk_score": 62, "degraded_components": [],
                    "agent_error": "", "llm_turns": 1, "evidence_book": {"entries": []}}

        out, events = self.run_turn(
            {"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
             "verify": _slow_verify, "narrate_llm": stub_narrate()},
            VERIFY_TEXT, cancel_check=lambda: flag["cancelled"])
        names = [e["event"] for e in events]
        self.assertEqual(names, ["meta", "status", "tool_start", "scan", "error"])
        self.assertEqual(events[-1]["code"], "cancelled")
        self.assertTrue(events[-1]["recoverable"])
        self.assertNotIn("card", names)
        self.assertNotIn("narrative", names)
        self.assertNotIn("tool_end", names)
        # cancel_check 已透传进服务（引擎轮询位真实接线）
        self.assertIsNotNone(service_calls[0]["cancel_check"])
        run = self.sessions.runs_for_session(self.session_id)[0]
        self.assertEqual(run["status"], "cancelled")
        self.assertTrue(out["degraded"])
        self.assertEqual(out["tool_run_id"], run["id"])


# ===========================================================================
# 修复2（P1-2）：活跃档案卡进路由 + 缺 id 的确定性兜底（§5.4 ②）
# ===========================================================================

from mylocalai.assistant import router as router_mod  # noqa: E402
from mylocalai.assistant.loop import AssistantLoop  # noqa: E402


def seed_dossier_with_run(base: LoopTestBase, text: str = VERIFY_TEXT,
                          label: str = "疑似谣言", as_of: str = "2026-10-31T08:00:00+08:00") -> int:
    """给测试会话造"本会话最近档案"：建档 + v1 结论 + 一次带 dossier_id 的 tool_run。"""
    did = base.dossiers.start_dossier(text)
    base.dossiers.store.add_conclusion(did, label, 62.0, confidence="中", as_of=as_of)
    run_id = base.sessions.start_run(base.session_id, "verify_claim", {"text": text})
    base.sessions.finish_run(run_id, status="ok", dossier_id=did)
    return did


class TestRouteHistoryHint(LoopTestBase):
    """活跃档案卡 + 最近对话摘要作为 history_hint 进入路由上下文。"""

    def test_history_hint_carries_dossier_cards_and_conversation(self):
        """history_hint 非空：route_llm 收到的 user 文本含档案卡（id/标题/结论/时间）
        与最近对话，且【用户消息】保留原文。"""
        did = seed_dossier_with_run(self)
        self.sessions.add_message(self.session_id, "user", "上一条我想核实自来水的事")
        self.sessions.add_message(self.session_id, "assistant", "已核查完毕，结论见卡片。")

        seen = []

        def _route_llm(system, user):
            seen.append(user)
            return ({"action": "chitchat", "reply": "好的。"}, "")

        self.run_turn({"route_llm": _route_llm, "narrate_llm": stub_narrate()}, "刚才那条怎么样了")
        user_text = seen[0]
        self.assertIn("【会话背景】", user_text)
        self.assertIn("【用户消息】刚才那条怎么样了", user_text)
        # 活跃档案卡：#id《标题》(label, as_of)
        self.assertIn(f"#{did}《", user_text)
        self.assertIn("(疑似谣言, 2026-10-31T08:00:00+08:00)", user_text)
        self.assertIn("活跃档案：", user_text)
        # 最近对话摘要：本回合之前的 user/assistant 消息（各截 80 字）
        self.assertIn("最近对话：", user_text)
        self.assertIn("user: 上一条我想核实自来水的事", user_text)
        self.assertIn("assistant: 已核查完毕，结论见卡片。", user_text)
        # 本回合的用户消息只出现在【用户消息】段，不重复出现在对话摘要里
        self.assertEqual(user_text.count("刚才那条怎么样了"), 1)

    def test_first_turn_without_context_keeps_user_text_identical(self):
        """守卫：首回合（无档案 run、无历史）history_hint 为空 → 路由 user 文本与
        旧版逐位一致（无【会话背景】前缀）。"""
        seen = []

        def _route_llm(system, user):
            seen.append(user)
            return ({"action": "verify_claim", "text": VERIFY_TEXT}, "")

        self.run_turn({"route_llm": _route_llm, "verify": stub_verify(),
                       "narrate_llm": stub_narrate()}, VERIFY_TEXT)
        self.assertEqual(seen[0], VERIFY_TEXT)


class TestRouteIdFallback(LoopTestBase):
    """缺 id 的确定性兜底：只填本会话最近一次 tool_run 的 dossier_id。"""

    def test_recheck_without_id_falls_back_to_last_session_dossier(self):
        """"刚才那条再查一遍"（路由返回 recheck_dossier 无 id）→ 兜底填入会话
        最近档案 id 并成功 recheck（跑的是档案原文）。"""
        did = seed_dossier_with_run(self)
        verify = stub_verify()
        out, events = self.run_turn(
            {"route_llm": stub_route("recheck_dossier"),   # 路由没给 id
             "verify": verify, "narrate_llm": stub_narrate()},
            "刚才那条再查一遍")
        self.assertEqual(out["action"], "recheck_dossier")
        names = [e["event"] for e in events]
        self.assertIn("tool_start", names)
        run = self.sessions.runs_for_session(self.session_id)[-1]
        self.assertEqual(run["tool"], "recheck_dossier")
        self.assertEqual(run["args"], {"id": did})       # 兜底填的正是会话最近档案
        self.assertEqual(run["dossier_id"], did)
        self.assertEqual(verify.calls[0]["text"], VERIFY_TEXT)   # 用档案原文重查
        self.assertEqual(out["dossier_id"], did)

    def test_recheck_without_id_and_no_session_dossier_clarifies(self):
        """会话没有任何档案 run：同消息照旧 clarify（绝不瞎猜数字）。"""
        out, events = self.run_turn(
            {"route_llm": stub_route("recheck_dossier"),
             "verify": stub_verify(), "narrate_llm": stub_narrate()},
            "刚才那条再查一遍")
        self.assertEqual(out["action"], "clarify")
        names = [e["event"] for e in events]
        self.assertNotIn("tool_start", names)
        self.assertEqual(self.sessions.runs_for_session(self.session_id), [])

    def test_explicit_bad_id_is_not_replaced_by_fallback(self):
        """用户显式给了非法 id（0）→ 照旧被校验拦下 clarify，兜底绝不替换显式参数。"""
        seed_dossier_with_run(self)
        out, events = self.run_turn(
            {"route_llm": stub_route("recheck_dossier", id=0),
             "verify": stub_verify(), "narrate_llm": stub_narrate()},
            "把 #0 那条再查一遍")
        self.assertEqual(out["action"], "clarify")
        self.assertNotIn("tool_start", [e["event"] for e in events])


class TestRouterDirectGuards(unittest.TestCase):
    """router.route 的新参数直接对拍：hint 拼装形状与兜底触发条件。"""

    def setUp(self):
        self.registry = AssistantLoop().registry
        import mylocalai.assistant as assistant_pkg
        base = Path(assistant_pkg.__file__).resolve().parent
        self.rules = json.loads((base / "assistant_rules.json").read_text(encoding="utf-8"))
        self.prompts = json.loads((base / "prompts.json").read_text(encoding="utf-8"))

    def route(self, obj, user="网传消息", **kw):
        calls = []

        def _llm(system, user_):
            calls.append({"system": system, "user": user_})
            return (obj, "")

        out = router_mod.route(user, self.registry, _llm, self.rules, self.prompts, **kw)
        out["_calls"] = calls
        return out

    def test_empty_history_hint_keeps_user_text_identical(self):
        """守卫：history_hint 为空（缺省）→ route_llm 的 user 与旧版逐位一致。"""
        r = self.route({"action": "verify_claim", "text": "x"})
        self.assertEqual(r["_calls"][0]["user"], "网传消息")
        r2 = self.route({"action": "verify_claim", "text": "x"}, history_hint="")
        self.assertEqual(r2["_calls"][0]["user"], "网传消息")

    def test_history_hint_wraps_user_text_with_background(self):
        r = self.route({"action": "verify_claim", "text": "x"},
                       history_hint="活跃档案：#3《某消息》(疑似谣言, 2026-10-31T…)")
        self.assertEqual(r["_calls"][0]["user"],
                         "【会话背景】活跃档案：#3《某消息》(疑似谣言, 2026-10-31T…)\n【用户消息】网传消息")

    def test_id_fallback_fills_missing_id_for_id_tools(self):
        for action, expected in (("recheck_dossier", {"id": 3}),
                                 ("get_dossier", {"id": 3}),
                                 # export_card 的 schema 校验会把缺省 format 收口为 md
                                 ("export_card", {"id": 3, "format": "md"})):
            with self.subTest(action=action):
                r = self.route({"action": action}, id_fallback=3)
                self.assertEqual(r["action"], action)
                self.assertEqual(r["args"], expected)

    def test_id_fallback_not_applied_when_id_present_or_none(self):
        """显式合法 id 不被替换；无兜底（None）缺 id 照旧 clarify。"""
        r = self.route({"action": "recheck_dossier", "id": 5}, id_fallback=3)
        self.assertEqual(r["args"], {"id": 5})
        r2 = self.route({"action": "recheck_dossier"})
        self.assertEqual(r2["action"], "clarify")

    def test_id_fallback_never_guesses_other_numbers(self):
        """兜底值非正整数（0/负数/坏类型）→ 不填，照旧 clarify。"""
        for bad in (0, -2, "abc", 1.5):
            with self.subTest(fallback=bad):
                r = self.route({"action": "recheck_dossier"}, id_fallback=bad)
                self.assertEqual(r["action"], "clarify")


# ===========================================================================
# 修复3（P2）：import 方向约束补新包
# ===========================================================================

from tests.test_import_rules import FORBIDDEN, _violations  # noqa: E402


class TestImportRulesExtended(unittest.TestCase):
    """FORBIDDEN 补三行（§4.1 依赖方向）：assistant 禁 web、dossier 禁 web+assistant、
    web 为顶层无上游可禁（空集）。2026-11-20 平台包（platform）落地后再补一行：
    上游各包禁 platform、platform 自身禁 web+assistant。"""

    def test_new_forbidden_entries(self):
        self.assertEqual(FORBIDDEN["mylocalai.assistant"], {"web", "platform"})
        self.assertEqual(FORBIDDEN["mylocalai.dossier"], {"web", "assistant", "platform"})
        self.assertEqual(FORBIDDEN["mylocalai.platform"], {"web", "assistant"})
        self.assertEqual(FORBIDDEN["mylocalai.web"], set())

    def test_whole_tree_still_violation_free(self):
        """新规则下全仓 AST 检查仍零违规（含新增的 dossier/assistant/web/platform 四包）。"""
        self.assertEqual(_violations(), [])


# ===========================================================================
# 修复4（P2）：card.py 规则形状校验
# ===========================================================================

from mylocalai.core import card as card_mod  # noqa: E402
from mylocalai.core.card import derive_card  # noqa: E402


def mk_entries(official=0, unofficial=0):
    es = [{"url": f"https://gov.example/{i}", "source_kind": "官方（新华网）"}
          for i in range(official)]
    es += [{"url": f"https://bbs.example/{i}", "source_kind": "非官方（微博）"}
           for i in range(unofficial)]
    return es


class TestCardRuleShapeValidation(unittest.TestCase):
    """合法 JSON 但 card 段形状错 → 整段弃用退内置默认，derive_card 不抛
    （修复前 confidence_rules 写成 dict/元素非 dict 会在 rule.get 处 AttributeError）。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self._old = os.environ.get("RUMOR_RULES_PATH")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("RUMOR_RULES_PATH", None)
        else:
            os.environ["RUMOR_RULES_PATH"] = self._old
        card_mod.reset_card_rules_cache()
        self.tmp.cleanup()

    def _point_rules_to(self, card_section) -> None:
        path = Path(self.tmp.name) / "verdict_rules.json"
        path.write_text(json.dumps({"card": card_section}, ensure_ascii=False),
                        encoding="utf-8")
        os.environ["RUMOR_RULES_PATH"] = str(path)
        card_mod.reset_card_rules_cache()

    def test_confidence_rules_as_dict_falls_back_to_builtin(self):
        """confidence_rules 写成 dict（不是 list）→ 内置默认，不抛。"""
        self._point_rules_to({"confidence_rules": {"min_total": 0, "confidence": "必高"}})
        card = derive_card({"label": "x", "risk_score": 1}, mk_entries(official=2, unofficial=3))
        self.assertEqual(card["confidence"], "high")   # 内置规则 1：2 官方、总数 5

    def test_confidence_rules_non_dict_element_falls_back(self):
        """元素非 dict（字符串档位名）→ 内置默认，不抛。"""
        self._point_rules_to({"confidence_rules": ["铁证如山", {"min_total": 0, "confidence": "low"}]})
        card = derive_card({"label": "x", "risk_score": 1}, mk_entries(unofficial=2))
        self.assertEqual(card["confidence"], "low")

    def test_confidence_rules_missing_confidence_key_falls_back(self):
        """元素缺 confidence 键 → 整段弃用（形状不合法），不抛。"""
        self._point_rules_to({"confidence_rules": [{"min_official": 1, "min_total": 1}]})
        # 1 官方、总数 1：内置规则 1/2/3 都不满足 → 落到规则 4（min_total=0）low
        card = derive_card({"label": "x", "risk_score": 1}, mk_entries(official=1))
        self.assertEqual(card["confidence"], "low")

    def test_recheck_non_dict_falls_back_whole_section(self):
        """recheck 写成 list → 整段弃用（confidence 一并退内置），不抛。"""
        self._point_rules_to({
            "confidence_rules": [{"min_total": 0, "confidence": "必高"}],
            "recheck": ["不是字典"],
        })
        card = derive_card({"label": "x", "risk_score": 1}, mk_entries(unofficial=2))
        self.assertEqual(card["confidence"], "low")
        self.assertEqual(card["recheck_conditions"],
                         ["出现官方/权威媒体级信源的通报或报道", "证据本独立条目≥5"])

    def test_well_shaped_rules_still_apply(self):
        """守卫：形状合法的自定义规则仍生效（校验不能误伤正常配置）。"""
        self._point_rules_to({"confidence_rules": [{"min_total": 0, "confidence": "铁证如山"}]})
        card = derive_card({"label": "x", "risk_score": 1}, mk_entries(unofficial=1))
        self.assertEqual(card["confidence"], "铁证如山")


if __name__ == "__main__":
    unittest.main(verbosity=2)
