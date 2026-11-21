# -*- coding: utf-8 -*-
"""三个新工具层的离线测试：注册/卡组装/自动接力/复合聚合/prior/降级。

pipeline 用 stub 服务替代；不触网、不调模型、不读真实 data/。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import (tools_authority, tools_composite, tools_research,
                                 tools_skill_cards)  # noqa: E402
from mylocalai.assistant import loop as loop_mod  # noqa: E402
from mylocalai.assistant import router as router_mod  # noqa: E402
from mylocalai.assistant.registry import ToolRegistry  # noqa: E402


def research_result(quote: str = "") -> dict:
    findings = []
    if quote:
        findings = [{"finding_id": "F1", "sq_ids": ["Q1"], "statement": "某地水质争议有官方通报",
                     "evidence_ids": ["E1"], "support_level": "authoritative",
                     "source_quote": quote, "checkable": True}]
    return {
        "tool_id": "collect_research", "card_type": "research", "status": "completed",
        "task_status": "completed",
        "data": {"topic": "某地自来水水质",
                 "scope": {"question": "水质争议", "time_window": "2026-11", "region": "CN",
                           "basis": ["某地自来水"]},
                 "sub_questions": [{"sq_id": "Q1", "text": "是否有官方通报", "status": "covered",
                                    "evidence_count": 1}],
                 "key_findings": findings,
                 "leads": [], "evidence": ([{"evidence_id": "E1", "sq_ids": ["Q1"],
                                             "source_id": "SRC-NHC", "name": "国家卫健委",
                                             "domain": "nhc.gov.cn", "tier": "官方权威",
                                             "verification": "catalog_anchor", "title": "通报",
                                             "url": "https://www.nhc.gov.cn/a", "quote": quote,
                                             "published_at": "2026-11-20", "fetched_at": "2026-11-21T00:00:00",
                                             "role": "fact", "limitations": []}] if quote else []),
                 "sources": {"verified": [{"source_id": "SRC-NHC", "name": "国家卫健委",
                                            "domain": "nhc.gov.cn", "tier": "官方权威",
                                            "authority_class": "official",
                                            "verification": "catalog_anchor"}],
                             "unverified": []},
                 "timeline": [], "conflicts": [], "analysis": {}, "followups": []},
        "gaps": [], "errors": [],
        "audit": {"searches": 2, "pages": 2, "llm_calls": 3, "operations": []},
        "budget": {"used": {}, "limits": {}},
        "execution": {"mode": "pipeline", "steps": 4, "stop_reason": "completed"},
        "incremental": False, "base_as_of": "",
        "finished_at": "2026-11-21T00:00:00", "started_at": "2026-11-20T23:59:00",
    }


def authority_result(refuted: bool = True) -> dict:
    balance = "refuted" if refuted else "insufficient"
    conclusion = ("在现有资料覆盖范围内，权威资料可以直接反驳该说法：官方通报否认。"
                  if refuted else "没有找到能证明或反驳该说法的权威资料（或可靠证据互相矛盾）。")
    return {
        "tool_id": "verify_authority", "status": "completed", "task_status": "completed",
        "data": {"claims": [{"claim_id": "C1", "text": "网传某地自来水被投毒",
                              "origin": "user", "claim_status": "checkable",
                              "balance": balance, "balance_label": "权威证据反驳",
                              "lean": "refutes", "lean_label": "倾向反驳",
                              "conclusion": conclusion, "supporting_ids": [],
                              "refuting_ids": ["E1"] if refuted else [],
                              "context_ids": [], "authority_basis": ["官方机构"],
                              "limitations": ["只覆盖已读官方通报"]}],
                 "evidence": ([{"evidence_id": "E1", "claim_ids": ["C1"],
                                "relation": "refutes", "source_id": "SRC-NHC",
                                "name": "国家卫健委", "domain": "nhc.gov.cn",
                                "tier": "官方权威", "authority_class": "official",
                                "verification": "catalog_anchor",
                                "authority_basis": ["目录锚点"], "title": "通报",
                                "url": "https://www.nhc.gov.cn/a", "quote": "经核查，该说法不实",
                                "published_at": "2026-11-20", "fetched_at": "2026-11-21T00:00:00",
                                "independence_group": "nhc.gov.cn", "limitations": []}] if refuted else []),
                 "sources": {"verified": [{"source_id": "SRC-NHC", "name": "国家卫健委",
                                            "domain": "nhc.gov.cn", "tier": "官方权威",
                                            "authority_class": "official",
                                            "verification": "catalog_anchor"}],
                             "unverified": []},
                 "conflicts": [], "analysis": [], "limitations": ["只覆盖已读官方通报"]},
        "gaps": [], "errors": [],
        "audit": {"searches": 1, "pages": 1, "llm_calls": 2},
        "budget": {"used": {}, "limits": {}},
        "execution": {"mode": "pipeline", "steps": 4, "stop_reason": "completed"},
        "incremental": False, "base_as_of": "",
        "finished_at": "2026-11-21T00:00:00", "started_at": "2026-11-20T23:59:00",
    }


class StubSkills:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, text, **kw):
        self.calls.append((text, kw))
        return self.result


class FakeSessions:
    def __init__(self, payloads):
        self._messages = [{"role": "assistant", "kind": "card",
                           "content": json.dumps(p, ensure_ascii=False)} for p in payloads]

    def messages(self, session_id):
        return list(self._messages)


def ctx_with(research=None, authority=None, sessions=None, user_text="", session_id=None):
    services = {}
    if research is not None:
        services["research_skills"] = lambda: research
    if authority is not None:
        services["authority_skills"] = lambda: authority
    return SimpleNamespace(services=services, events=None, cancel_check=None,
                           session_id=session_id, user_text=user_text,
                           sessions=sessions, dossiers=None)


class ToolRegistrationTest(unittest.TestCase):
    def test_registered_read_only(self):
        reg = ToolRegistry()
        tools_research.register_all(reg)
        tools_authority.register_all(reg)
        tools_composite.register_all(reg)
        for name in ("collect_research", "verify_authority", "collect_and_verify"):
            spec = reg.get(name)
            self.assertIsNotNone(spec, name)
            self.assertEqual(spec["permission"], "read")
            self.assertEqual(spec["cost"], "heavy")

    def test_router_and_allowlist_and_card_types(self):
        for action in ("collect_research", "verify_authority", "collect_and_verify"):
            self.assertIn(action, router_mod.TOOL_ACTIONS)
            self.assertIn(action, loop_mod._CARD_TYPE)
        rules = json.loads((ROOT / "src/mylocalai/assistant/assistant_rules.json").read_text(encoding="utf-8"))
        for action in ("collect_research", "verify_authority", "collect_and_verify"):
            self.assertIn(action, rules["tool_allowlist"])


class ResearchToolTest(unittest.TestCase):
    def test_empty_topic_clarifies(self):
        out = tools_research._collect_research(ctx_with(), "")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "needs_clarification")
        self.assertIsNone(out["card"])

    def test_research_card_and_dossier_none(self):
        stub = StubSkills(research_result())
        out = tools_research._collect_research(ctx_with(research=stub), "某地自来水水质")
        self.assertTrue(out["ok"])
        self.assertEqual(out["card_type"], "research")
        self.assertEqual(out["card"]["card_type"], "research")
        self.assertIsNone(out["dossier_id"])
        self.assertNotIn("风险分", out["digest"])
        self.assertEqual(stub.calls[0][0], "某地自来水水质")

    def test_auto_handoff_only_direct_quote(self):
        research = StubSkills(research_result("经核查，该说法不实"))
        authority = StubSkills(authority_result())
        out = tools_research._collect_research(ctx_with(research=research, authority=authority),
                                               "某地自来水水质")
        self.assertEqual(out["card_type"], "composite")
        self.assertEqual(out["card"]["card_type"], "composite")
        self.assertTrue(out["card"]["handoff"]["auto"])
        self.assertEqual(authority.calls[0][0], "经核查，该说法不实")
        self.assertEqual(authority.calls[0][1]["origin"], "auto_from_research")

    def test_no_quote_no_handoff(self):
        research = StubSkills(research_result())
        out = tools_research._collect_research(ctx_with(research=research), "某地自来水水质")
        self.assertEqual(out["card_type"], "research")
        self.assertIn("handoff_skipped", out["card"])

    def test_followup_uses_prior(self):
        prior = {"card_type": "research", "topic": "某地自来水水质", "as_of": "2026-11-20T00:00:00"}
        sessions = FakeSessions([{"card_type": "research", "card": prior}])
        stub = StubSkills(research_result())
        out = tools_research._collect_research(ctx_with(research=stub, sessions=sessions,
                                                       session_id=1, user_text="补充一下 2025 年的资料"), "")
        self.assertTrue(out["ok"])
        self.assertEqual(stub.calls[0][0], "某地自来水水质")
        self.assertTrue(stub.calls[0][1]["followup"])
        self.assertIsNotNone(stub.calls[0][1]["prior"])

    def test_missing_service_is_honest(self):
        out = tools_research._collect_research(ctx_with(), "某主题")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "failed")

    def test_followup_without_prior_clarifies(self):
        out = tools_research._collect_research(
            ctx_with(sessions=FakeSessions([]), session_id=1, user_text="补充一下"),
            "", followup=True)
        self.assertEqual(out["status"], "needs_clarification")
        self.assertIn("上一张资料卡", out["digest"])

    def test_rerun_beats_explicit_followup(self):
        prior = {"card_type": "research", "topic": "旧主题", "as_of": "2026-11-20T00:00:00"}
        sessions = FakeSessions([{"card_type": "research", "card": prior}])
        stub = StubSkills(research_result())
        out = tools_research._collect_research(
            ctx_with(research=stub, sessions=sessions, session_id=1,
                     user_text="换个主题重新收集"), "新主题")
        self.assertTrue(out["ok"])
        self.assertEqual(stub.calls[0][0], "新主题")
        self.assertFalse(stub.calls[0][1]["followup"])
        self.assertIsNone(stub.calls[0][1]["prior"])


class AuthorityToolTest(unittest.TestCase):
    def test_empty_text_clarifies(self):
        out = tools_authority._verify_authority(ctx_with(), "")
        self.assertEqual(out["status"], "needs_clarification")

    def test_authority_card(self):
        stub = StubSkills(authority_result())
        out = tools_authority._verify_authority(ctx_with(authority=stub), "网传某地自来水被投毒")
        self.assertTrue(out["ok"])
        self.assertEqual(out["card_type"], "authority")
        self.assertEqual(out["card"]["claims"][0]["balance"], "refuted")
        self.assertIn("直接反驳", out["card"]["claims"][0]["conclusion"])
        self.assertIsNone(out["dossier_id"])

    def test_followup_uses_prior_claim(self):
        prior = {"card_type": "authority", "as_of": "2026-11-20T00:00:00",
                 "claims": [{"text": "网传某地自来水被投毒"}]}
        sessions = FakeSessions([{"card_type": "authority", "card": prior}])
        stub = StubSkills(authority_result())
        out = tools_authority._verify_authority(ctx_with(authority=stub, sessions=sessions,
                                                        session_id=1, user_text="把第二条再查深一点"), "")
        self.assertTrue(out["ok"])
        self.assertEqual(stub.calls[0][0], "网传某地自来水被投毒")
        self.assertTrue(stub.calls[0][1]["followup"])

    def test_followup_without_prior_clarifies(self):
        out = tools_authority._verify_authority(
            ctx_with(sessions=FakeSessions([]), session_id=1, user_text="补充一下"), "",
            followup=True)
        self.assertEqual(out["status"], "needs_clarification")
        self.assertIn("上一张核验卡", out["digest"])

    def test_rerun_beats_explicit_followup(self):
        prior = {"card_type": "authority", "as_of": "2026-11-20T00:00:00",
                 "claims": [{"text": "旧说法"}]}
        sessions = FakeSessions([{"card_type": "authority", "card": prior}])
        stub = StubSkills(authority_result())
        out = tools_authority._verify_authority(
            ctx_with(authority=stub, sessions=sessions, session_id=1,
                     user_text="重新核验这个新说法"), "新说法")
        self.assertTrue(out["ok"])
        self.assertEqual(stub.calls[0][0], "新说法")
        self.assertFalse(stub.calls[0][1]["followup"])
        self.assertIsNone(stub.calls[0][1]["prior"])


class CompositeToolTest(unittest.TestCase):
    def test_composite_merges_two_stages(self):
        research = StubSkills(research_result("官方通报"))
        authority = StubSkills(authority_result())
        out = tools_composite._collect_and_verify(ctx_with(research=research, authority=authority),
                                                  "某地自来水水质", "网传某地自来水被投毒")
        self.assertTrue(out["ok"])
        self.assertEqual(out["card_type"], "composite")
        card = out["card"]
        self.assertEqual(card["card_type"], "composite")
        self.assertEqual(card["status"], "completed")
        self.assertIn("research", card)
        self.assertIn("authority", card)
        self.assertGreaterEqual(card["counts"]["searches"], 3)

    def test_one_stage_failure_is_partial(self):
        research = StubSkills(research_result())
        authority = StubSkills({**authority_result(), "status": "failed", "task_status": ""})
        out = tools_composite._collect_and_verify(ctx_with(research=research, authority=authority),
                                                  "主题", "说法")
        self.assertEqual(out["status"], "partial")
        self.assertTrue(out["degraded"])

    def test_both_failed(self):
        research = StubSkills({**research_result(), "status": "failed", "task_status": ""})
        authority = StubSkills({**authority_result(), "status": "failed", "task_status": ""})
        out = tools_composite._collect_and_verify(ctx_with(research=research, authority=authority),
                                                  "主题", "说法")
        self.assertEqual(out["status"], "failed")
        self.assertFalse(out["ok"])


class CardContractTest(unittest.TestCase):
    def test_no_numeric_scores_in_cards(self):
        research_card = tools_research.research_card(research_result("引文"))
        authority_card = tools_authority.authority_card(authority_result())
        for card in (research_card, authority_card):
            text = json.dumps(card, ensure_ascii=False)
            self.assertNotIn("risk_score", text)
            self.assertNotIn("confidence", text)

    def test_authority_conclusion_is_verbatim(self):
        card = tools_authority.authority_card(authority_result())
        self.assertEqual(card["claims"][0]["conclusion"],
                         "在现有资料覆盖范围内，权威资料可以直接反驳该说法：官方通报否认。")

    def test_authority_card_counts_verified_sources(self):
        card = tools_authority.authority_card(authority_result())
        self.assertEqual(card["counts"]["verified_sources"], 1)
        self.assertEqual(card["counts"]["official_sources"], 1)

    def test_composite_keeps_incremental_marker(self):
        research = tools_research.research_card({**research_result("引文"),
                                                 "incremental": True,
                                                 "base_as_of": "2026-11-20T00:00:00"})
        authority = tools_authority.authority_card(authority_result())
        card = tools_skill_cards.composite_card(research, authority)
        self.assertTrue(card["incremental"])
        self.assertEqual(card["base_as_of"], "2026-11-20T00:00:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
