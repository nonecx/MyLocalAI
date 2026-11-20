# -*- coding: utf-8 -*-
"""真实 pipeline → 工具卡片的集成核对（离线夹具来自各 pipeline 测试）。

锁定：pipeline 实际输出能被卡片层完整消费；审计计数与卡片 counts 一致；
权威结论/引文逐字保留；不出现分数键。避免“pipeline 测试绿、工具卡字段对不上”。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mylocalai.assistant.tools_skill_cards import (  # noqa: E402
    authority_card, research_card)
import test_authority_pipeline as authority_fixtures  # noqa: E402
import test_research_pipeline as research_fixtures  # noqa: E402


class RealPipelineCardTest(unittest.TestCase):
    def test_research_pipeline_card_consumes_all_fields(self):
        result = research_fixtures.make_skills().run("电动车电池安全监管")
        card = research_card(result)
        self.assertEqual(card["card_type"], "research")
        self.assertEqual(card["status"], "completed")
        self.assertEqual(card["topic"], "电动车电池安全监管")
        self.assertTrue(card["key_findings"])
        self.assertTrue(card["evidence"])
        self.assertEqual(card["counts"]["searches"], result["audit"]["searches"])
        self.assertEqual(card["counts"]["pages"], result["audit"]["pages"])
        self.assertEqual(card["counts"]["llm_calls"], result["audit"]["llm_calls"])
        self.assertNotIn("score", json.dumps(card, ensure_ascii=False))

    def test_authority_pipeline_card_keeps_verdict_and_counts(self):
        case = authority_fixtures.TestCatalogAnchor()
        result, _search = case._run(
            authority_fixtures.NHC_REFUTE,
            {"quote": "不能治愈近视", "relation": "refutes"})
        card = authority_card(result)
        self.assertEqual(card["card_type"], "authority")
        claim = card["claims"][0]
        self.assertEqual(claim["balance"], "refuted")
        self.assertIn("权威资料可以直接反驳", claim["conclusion"])
        self.assertEqual(card["evidence"][0]["quote"], "不能治愈近视")
        self.assertGreater(card["counts"]["searches"], 0)
        self.assertGreater(card["counts"]["pages"], 0)
        self.assertGreater(card["counts"]["llm_calls"], 0)
        self.assertNotIn("confidence", json.dumps(card, ensure_ascii=False))

    def test_media_only_is_lean_not_decisive(self):
        url = "https://www.xinhuanet.com/health/1.html"
        search = authority_fixtures.FakeSearch(discover=[{"url": url, "title": "报道"}])
        page = {"url": url, "title": "报道",
                "text": "专家表示，护眼仪不能治愈近视，消费者应谨慎。"}
        llm = authority_fixtures.FakeLLM(
            claims=[authority_fixtures.claim_row()],
            extraction={"quote": "护眼仪不能治愈近视", "relation": "refutes"})
        result = authority_fixtures.make_skills(
            search_fn=search, fetch_fn=authority_fixtures.FakeFetch({url: page}),
            llm_fn=llm).run(authority_fixtures.INPUT_ONE)
        card = authority_card(result)
        claim = card["claims"][0]
        self.assertEqual(claim["balance"], "insufficient")
        self.assertEqual(claim["lean"], "refutes")
        self.assertIn("不能据此反驳", claim["conclusion"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
