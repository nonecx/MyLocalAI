# -*- coding: utf-8 -*-
"""Adapter tests: legacy verify results must not masquerade as validated answers."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import tools_verify  # noqa: E402
from tests.test_assistant_loop import isolate_llm  # noqa: E402


class VerifyAnswerAdapterTests(unittest.TestCase):
    #: LLM 隔离（2026-11-24 .env 根因修复后补）：_verify_claim 的 claim 编排
    #: 先做主张提取（真调 LLM）。真实提供方可达时提取成功走"无主张短出口"，
    #: 断言"遗产 claims 保留 + insufficient_data"失去对象——本用例于是 silently
    #: 依赖"开发机上 LLM 不可达/未授权"。共用工具见 test_assistant_loop.isolate_llm。
    def setUp(self):
        isolate_llm(self)

    def test_legacy_verify_has_insufficient_answer_and_keeps_claims(self):
        result = {"label": "高风险谣言", "risk_score": 90, "claims": [
            {"claim": "具体主张 A"}, {"claim": "具体主张 B"}],
            "evidence_book": {"entries": []}, "llm_turns": 1}
        events = SimpleNamespace(emit=lambda *a, **k: None)
        ctx = SimpleNamespace(
            services={"verify": lambda *a, **k: result}, events=events,
            cancel_check=None, dossiers=SimpleNamespace(
                start_dossier=lambda *a, **k: None))
        with mock.patch.object(tools_verify, "_record", return_value={}):
            out = tools_verify._verify_claim(ctx, "原始长文本 A。原始长文本 B。")
        answer = out["claim_answer"]
        self.assertEqual(answer["answer_status"], "insufficient_data")
        self.assertEqual(len(answer["claims"]), 2)
        self.assertTrue(all(item["rumor_state"] is None for item in answer["claims"]))
        self.assertNotIn("risk_score", json.dumps(answer, ensure_ascii=False))
        self.assertTrue(out["not_saved"])

    def test_narrator_prompt_forbids_deriving_state_from_legacy_score(self):
        path = ROOT / "src" / "mylocalai" / "assistant" / "prompts.json"
        prompts = json.loads(path.read_text(encoding="utf-8"))
        prompt = prompts["narrate_system"]
        self.assertIn("insufficient_data", prompt)
        self.assertIn("风险分", prompt)
        self.assertIn("不得", prompt)


if __name__ == "__main__":
    unittest.main()
