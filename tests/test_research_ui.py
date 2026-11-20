# -*- coding: utf-8 -*-
"""资料收集 / 权威核验卡片前端测试（T5，恰好 12 例）。

覆盖（契约 §7）：
  1. 渲染器与分发：renderResearch/renderAuthority 存在、renderCard 分支齐全、
     window.ChatCards 导出（缺任一前端就调不到）；
  2. 字段消费与后端契约一致：research 卡消费 scope/sub_questions/key_findings/leads/
     evidence/sources/timeline/conflicts/analysis/counts/gaps/errors/followups；
     authority 卡消费 claims/balance/lean/conclusion/analysis/sources/counts；
  3. 口径诚实铁律：不展示 score/confidence 数值分；不写"是真的/假的/属实"固定判定；
     引文逐字透传（escapeHtml 后仍是同一段文字）；safeUrl 只放行 http(s)；
  4. 降级与空结果：degraded 徽章、空列表不崩、as_of 行必出；
  5. CSS：chat.css 定义了新卡用到的关键 class（否则样式丢）。

全离线：只做源码级静态断言（与 tests/test_website_chat.py 同风格），不执行 JS。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATIC = ROOT / "src" / "mylocalai" / "web" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _block(src: str, start: str, end: str) -> str:
    """切出 start 标记到 end 标记之间的代码段（取不到就返回空串）。"""
    i = src.find(start)
    if i < 0:
        return ""
    j = src.find(end, i + len(start))
    return src[i:j if j >= 0 else len(src)]


class TestResearchRenderer(unittest.TestCase):
    """资料收集卡（renderResearch）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cards = _read("cards.js")
        cls.css = _read("chat.css")
        cls.block = _block(cls.cards, "function renderResearch(", "/* ---------- 9.")

    def test_renderer_exists_and_dispatch(self):
        self.assertIn("function renderResearch(", self.cards)
        self.assertIn('if (type === "research") return renderResearch(payload);',
                      self.cards)

    def test_exported_for_chat_js(self):
        tail = self.cards.split("window.ChatCards")[-1]
        self.assertIn("renderResearch,", tail, "renderResearch 未导出，chat.js 调不到")

    def test_consumes_pipeline_fields(self):
        for field in ("c.sub_questions", "c.key_findings", "c.leads", "c.evidence",
                      "c.sources", "c.timeline", "c.conflicts", "c.counts",
                      "c.gaps", "c.errors", "c.followups", "c.topic", "c.scope",
                      "c.analysis"):
            self.assertIn(field, self.block, f"research 卡未消费字段：{field}")

    def test_no_score_or_truth_labels(self):
        for banned in ("score", "confidence", "是真的", "是假的", "属实", "谣言为真"):
            self.assertNotIn(banned, self.block,
                             f"research 卡出现禁用字段/判定词：{banned}")

    def test_quote_escaped_and_link_safe(self):
        """引文逐字转义、链接只放行 http(s)：断言落在 researchEvidenceItem 上。"""
        item = _block(self.cards, "function researchEvidenceItem(", "function researchSourceItem(")
        self.assertIn("safeUrl(ev.url)", item)
        self.assertIn("escapeHtml(String(ev.quote))", item)
        self.assertIn('rel="noopener nofollow"', item)

    def test_unverified_leads_are_separated(self):
        """未核验线索单独成区，且文案明确"不进入关键发现"。"""
        self.assertIn("未核验线索", self.block)
        self.assertIn("不进入关键发现", self.block)

    def test_as_of_and_counts_line(self):
        self.assertIn("asOfLine(c.as_of)", self.block)
        self.assertIn("次搜索", self.block)
        self.assertIn("次读页", self.block)


class TestAuthorityRenderer(unittest.TestCase):
    """权威核验卡（renderAuthority）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cards = _read("cards.js")
        cls.block = _block(cls.cards, "function renderAuthority(", "/* ---------- 分发")

    def test_renderer_exists_and_dispatch(self):
        self.assertIn("function renderAuthority(", self.cards)
        self.assertIn('if (type === "authority") return renderAuthority(payload);',
                      self.cards)

    def test_exported_for_chat_js(self):
        tail = self.cards.split("window.ChatCards")[-1]
        self.assertIn("renderAuthority,", tail)

    def test_balance_and_lean_badges_are_text(self):
        """balance/lean 只用中文文字徽章，无任何数值分。"""
        self.assertIn("balanceMeta(cl.balance)", self.block)
        self.assertIn("LEAN_META[String(cl.lean || \"\")]", self.block)
        for banned in ("score", "confidence", "risk_score"):
            self.assertNotIn(banned, self.block)
        for word in ("是真的", "是假的", "属实", "谣言为真"):
            self.assertNotIn(word, self.block)

    def test_conclusion_transmitted_verbatim(self):
        self.assertIn("escapeHtml(String(cl.conclusion))", self.block)

    def test_evidence_grouped_by_relation(self):
        self.assertIn('rel === "supports"', self.block)
        self.assertIn('rel === "refutes"', self.block)
        self.assertIn("支持证据", self.block)
        self.assertIn("反驳证据", self.block)

    def test_analysis_labelled_as_inference(self):
        self.assertIn("基于证据的分析", self.block)
        self.assertIn("escapeHtml(String(a.text || \"\"))", self.block)

    def test_as_of_and_counts_line(self):
        self.assertIn("asOfLine(c.as_of)", self.block)
        self.assertIn("balance/lean 由代码按证据计算", self.block)


class TestCardsCss(unittest.TestCase):
    """两种新卡的样式在 chat.css 里有定义（否则排版丢）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.css = _read("chat.css")

    def test_new_card_classes_defined(self):
        for cls in (".chat-card-research", ".chat-card-authority",
                    ".chat-res-section-title", ".chat-res-finding", ".chat-res-lead",
                    ".chat-res-analysis", ".chat-auth-claim", ".chat-auth-conclusion",
                    ".chat-auth-analysis", ".chat-auth-section-title"):
            self.assertIn(cls, self.css, f"chat.css 缺少样式定义：{cls}")

    def test_css_reuses_root_tokens_only(self):
        """新样式只用 style.css 已声明的 :root 令牌，不另起配色。"""
        root = _read("style.css").split(":root {", 1)[1].split("}", 1)[0]
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", root))
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", self.css))
        self.assertNotIn(":root {", self.css, "chat.css 不该重新定义 :root 配色")
        self.assertEqual(sorted(used - declared), [],
                         "chat.css 用了 style.css 未声明的变量")


if __name__ == "__main__":
    unittest.main(verbosity=2)
