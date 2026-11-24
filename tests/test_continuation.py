# -*- coding: utf-8 -*-
"""追问承接测试：prior 卡查找（含 composite 子卡）、追问/重跑识别、证据合并。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills import continuation as C  # noqa: E402


def card_message(payload):
    return {"role": "assistant", "kind": "card", "content": json.dumps(payload, ensure_ascii=False)}


class FakeSessions:
    def __init__(self, messages):
        self._messages = messages

    def messages(self, session_id):
        if session_id == 999:
            raise RuntimeError("db down")
        return list(self._messages)


class TestFindPriorCard(unittest.TestCase):
    def test_finds_latest_research_card(self):
        sessions = FakeSessions([
            card_message({"card_type": "research", "card": {"card_type": "research", "topic": "旧"}}),
            card_message({"card_type": "authority", "card": {"card_type": "authority", "claims": []}}),
            card_message({"card_type": "research", "card": {"card_type": "research", "topic": "新"}}),
        ])
        prior = C.find_prior_card(sessions, 1, ("research",))
        self.assertEqual(prior["topic"], "新")

    def test_top_level_payload_shape(self):
        sessions = FakeSessions([card_message({"card_type": "authority", "claims": [{"text": "x"}]})])
        prior = C.find_prior_card(sessions, 1, ("authority",))
        self.assertEqual(prior["claims"][0]["text"], "x")

    def test_composite_subcard(self):
        research = {"card_type": "research", "topic": "自来水"}
        authority = {"card_type": "authority", "claims": [{"text": "网传投毒"}]}
        sessions = FakeSessions([card_message({
            "card_type": "composite", "card": {"card_type": "composite",
                                                "research": research, "authority": authority}})])
        self.assertEqual(C.find_prior_card(sessions, 1, ("research",))["topic"], "自来水")
        self.assertEqual(C.find_prior_card(sessions, 1, ("authority",))["claims"][0]["text"],
                         "网传投毒")
        self.assertEqual(C.find_prior_card(sessions, 1, ("composite",))["card_type"], "composite")

    def test_empty_content_skipped(self):
        sessions = FakeSessions([{"role": "assistant", "kind": "card", "content": ""}])
        self.assertIsNone(C.find_prior_card(sessions, 1, ("research",)))

    def test_bad_json_skipped(self):
        sessions = FakeSessions([{"role": "assistant", "kind": "card", "content": "{bad"}])
        self.assertIsNone(C.find_prior_card(sessions, 1, ("research",)))

    def test_read_failure_returns_none(self):
        self.assertIsNone(C.find_prior_card(FakeSessions([]), 999, ("research",)))

    def test_missing_session_or_types(self):
        self.assertIsNone(C.find_prior_card(None, 1, ("research",)))
        self.assertIsNone(C.find_prior_card(FakeSessions([]), None, ("research",)))
        self.assertIsNone(C.find_prior_card(FakeSessions([]), 1, ()))


class TestFollowupMarkers(unittest.TestCase):
    def test_followup_phrases(self):
        for text in ("补充一下 2025 年的资料", "把第二条再查深一点", "换个角度看看",
                     "只看反驳证据", "继续深入"):
            self.assertTrue(C.looks_like_followup(text), text)

    def test_rerun_phrases_not_followup(self):
        for text in ("重新查一遍", "重跑", "换个主题", "新话题：某地火灾", "从头再来"):
            self.assertTrue(C.looks_like_rerun(text), text)
            self.assertFalse(C.looks_like_followup(text), text)

    def test_plain_new_request_not_followup(self):
        self.assertFalse(C.looks_like_followup("帮我收集某地自来水资料"))
        self.assertFalse(C.looks_like_followup(""))


class TestEvidenceMerge(unittest.TestCase):
    def test_merge_dedup_and_preserve_old_ids(self):
        old = [{"evidence_id": "E1", "url": "https://a", "quote": "甲"}]
        new = [{"evidence_id": "E1", "url": "https://a", "quote": "甲"},
               {"evidence_id": "E2", "url": "https://b", "quote": "乙"},
               {"evidence_id": "E3", "url": "https://b", "quote": "乙"}]
        merged = C.merge_evidence(old, new, 10)
        self.assertEqual([row["evidence_id"] for row in merged], ["E1", "E2"])

    def test_merge_cap(self):
        merged = C.merge_evidence([], [{"evidence_id": f"E{i}"} for i in range(1, 8)], 3)
        self.assertEqual(len(merged), 3)

    def test_merge_bad_shapes_safe(self):
        self.assertEqual(C.merge_evidence(None, "bad", 3), [])
        self.assertEqual(C.merge_evidence([{"x": 1}], [None, 3], 3), [{"x": 1}])


class TestPriorHelpers(unittest.TestCase):
    def test_prior_topic_text_as_of(self):
        research = {"card_type": "research", "topic": "自来水", "as_of": "2026-11-20T10:00:00"}
        authority = {"card_type": "authority", "as_of": "2026-11-20T11:00:00",
                     "claims": [{"text": "网传投毒"}, {"text": "官方否认"}]}
        composite = {"card_type": "composite", "research": research, "authority": authority}
        self.assertEqual(C.prior_topic(research), "自来水")
        self.assertEqual(C.prior_topic(composite), "自来水")
        self.assertEqual(C.prior_text(authority), "网传投毒\n官方否认")
        self.assertEqual(C.prior_as_of(authority), "2026-11-20T11:00:00")
        self.assertEqual(C.prior_as_of(None), "")

    def test_bad_shapes(self):
        self.assertEqual(C.prior_topic(None), "")
        self.assertEqual(C.prior_text({"claims": "bad"}), "")
        self.assertEqual(C.prior_text({"claims": [None, {"text": ""}]}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)