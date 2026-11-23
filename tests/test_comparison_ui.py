# -*- coding: utf-8 -*-
"""档案对比卡片前端测试（T5，恰好 12 例）。

覆盖（契约 §7，同 tests/test_research_ui.py 风格）：
   1. 渲染器与分发：renderComparison 存在、renderCard 按 card_type 精确匹配、
      window.ChatCards 导出（缺任一前端就调不到）；
   2. 字段消费与后端契约一致：comparison 卡消费 left/right/similarity/
      conclusion_delta/evidence_diff/source_diff/limits/gaps；单侧消费
      id/title/label/confidence/as_of/evidence_count；
   3. 口径诚实铁律：置信度只出 high/medium/low 档位徽章（无数值分）；相似度只标
      「疑似同源」徽章，卡内不出任何相似度数值，也不出 risk_score；不写
      "是真的/假的/属实"固定判定，不做升级/降级判断；证据/信源 URL 是外部内容，
       一律纯文本呈现（与 renderMonitorChange 同口径，不造可点链接）；
   4. 降级与缺章：单侧读失败如实说不可用，缺结论说「尚无结论」，as_of 缺省
      明说「未知」；整块字段缺失时整块不显示，不编造；
   5. CSS：chat.css 定义了新卡用到的关键 class（否则样式丢），且只用既有令牌。

全离线：只做源码级静态断言，不执行 JS。
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


class TestComparisonRenderer(unittest.TestCase):
    """档案对比卡（renderComparison）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cards = _read("cards.js")
        cls.css = _read("chat.css")
        # 物理边界：第 13 节（档案对比卡）→ 第 14 节（事件时间线卡）
        cls.block = _block(cls.cards, "13. 档案对比卡", "14. 事件时间线卡")

    def test_renderer_dispatch_and_export(self):
        self.assertTrue(self.block, "cards.js 缺少「13. 档案对比卡」渲染区")
        self.assertIn("function renderComparison(", self.block)
        self.assertIn('if (type === "comparison") return renderComparison(payload);',
                      self.cards)
        tail = self.cards.split("window.ChatCards")[-1]
        self.assertIn("renderComparison,", tail, "renderComparison 未导出，chat.js 调不到")

    def test_consumes_pipeline_fields(self):
        for field in ("c.status", "c.left", "c.right",
                      "c.similarity", "c.conclusion_delta", "c.evidence_diff",
                      "c.source_diff", "c.limits", "c.gaps"):
            self.assertIn(field, self.block, f"comparison 卡未消费字段：{field}")
        # degraded 经共用 degradedBadge(c) 消费（与全文件其他渲染器同口径）
        self.assertIn("degradedBadge(c)", self.block)

    def test_side_columns_fields(self):
        """左右两列：#id/标题/标签/as_of/置信度档位/证据数。"""
        for field in ("side.id", "side.title", "side.label",
                      "side.as_of", "side.evidence_count"):
            self.assertIn(field, self.block, f"单侧列未消费字段：{field}")
        # 置信度档位经共用 confTierBadge 消费（与 CONF_CLS/CONF_LABEL 同口径）
        self.assertIn("confTierBadge(side)", self.block)
        self.assertIn("function confTierBadge(", self.cards)
        badge = _block(self.cards, "function confTierBadge(", "function degradedBadge(")
        self.assertIn("CONF_CLS[v]", badge)
        self.assertIn("CONF_LABEL[v]", badge)
        self.assertIn("尚无结论", self.block, "缺结论未如实标注「尚无结论」")
        self.assertNotIn("Number(side.confidence)", self.block,
                         "置信度被当成数值展示（应为档位徽章）")

    def test_label_evolution_and_similarity_badge(self):
        """标签演进只述两端标签与 as_of；相似度只出「疑似同源」徽章。"""
        self.assertIn("标签演进", self.block)
        self.assertIn("delta.from_label", self.block)
        self.assertIn("delta.to_label", self.block)
        self.assertIn('delta.from_as_of || "未知"', self.block)
        self.assertIn('delta.to_as_of || "未知"', self.block)
        self.assertIn("sim.suspected_same_source", self.block)
        self.assertIn("疑似同源", self.block)
        self.assertIn("sim.basis", self.block)
        self.assertIn("不作同一性断言", self.block)

    def test_evidence_and_source_diff(self):
        """差异区：证据增减（计数 + 条目）+ 信源域名增减。"""
        self.assertIn("证据增减", self.block)
        self.assertIn("信源域名增减", self.block)
        self.assertIn("evDiff.added_count", self.block)
        self.assertIn("evDiff.removed_count", self.block)
        self.assertIn("comparisonEvidenceEntry(", self.block)
        self.assertIn("entry.url", self.block)
        self.assertIn("entry.title", self.block)
        self.assertIn("entry.relation", self.block)
        self.assertIn("srcDiff.added_domains", self.block)
        self.assertIn("srcDiff.removed_domains", self.block)

    def test_no_score_or_truth_labels(self):
        for banned in ("score", "risk_score", "是真的", "是假的", "属实", "谣言为真"):
            self.assertNotIn(banned, self.block,
                             f"comparison 卡出现禁用字段/判定词：{banned}")

    def test_urls_plain_text(self):
        """证据/信源 URL 是外部内容：纯文本展示，不造可点链接（监测卡同口径）。"""
        self.assertIn("monitorUrlText(entry.url)", self.block)
        self.assertNotIn('target="_blank"', self.block)
        self.assertNotIn("<a href", self.block)

    def test_as_of_unknown_fallback(self):
        self.assertIn("asOfLine(c.as_of)", self.block)
        self.assertIn('side.as_of || "未知"', self.block,
                      "单侧 as_of 为空时未如实显示「未知」")

    def test_missing_side_not_fabricated(self):
        """单侧读失败（null）如实说不可用，不编造内容。"""
        self.assertIn("读取失败或不可用", self.block)
        self.assertIn('typeof side !== "object"', self.block)

    def test_absent_diff_sections_hidden(self):
        """evidence_diff/source_diff 整块缺失时整块不显示，不补造。"""
        self.assertIn("c.evidence_diff === undefined", self.block)
        self.assertIn("c.source_diff === undefined", self.block)

    def test_css_classes_defined(self):
        for cls in (".chat-card-comparison", ".chat-cmp-sides",
                    ".chat-cmp-side", ".chat-cmp-side-name",
                    ".chat-cmp-side-label", ".chat-cmp-diff",
                    ".chat-cmp-diff-list", ".chat-cmp-diff-item"):
            self.assertIn(cls, self.css, f"chat.css 缺少样式定义：{cls}")
        # 渲染区用到的每个 chat-* class 都必须在 chat.css 有定义（否则样式丢）
        used = set()
        for chunk in re.findall(r'class="([^"]+)"', self.block):
            used.update(re.findall(r"[A-Za-z][\w-]*", chunk))
        missing = [c for c in sorted(used) if c.startswith("chat-")
                   and f".{c}" not in self.css]
        self.assertEqual(missing, [], f"chat.css 缺少定义：{missing}")

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
