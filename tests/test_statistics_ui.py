# -*- coding: utf-8 -*-
"""统计说法核查卡片前端测试（T5，恰好 12 例）。

覆盖（契约 §7，同 tests/test_research_ui.py 风格）：
   1. 渲染器与分发：renderStatistics 存在、renderCard 按 card_type 精确匹配、
      window.ChatCards 导出（缺任一前端就调不到）；
   2. 字段消费与后端契约一致：statistics 卡消费 text/state/claims/limits/gaps/
      errors/counts，主张组消费 claim_id/excerpt/number/unit/verdict/
      verdict_label/source_number/source_url/source_title/quote/as_of/basis；
   3. 口径诚实铁律：四态判语只做中文文字徽章（consistent/inconsistent/
      caliber_mismatch/unverifiable），不展示任何数值分；不写"是真的/假的/属实"
      固定判定；引文逐字透传（escapeHtml 后仍是同一段文字）；来源 URL 是外部
      内容，一律纯文本呈现（与 renderMonitorChange 同口径，不造可点链接）；
   4. 降级与空结果：as_of 缺省明说「未知」，空主张列表不崩不编；
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


class TestStatisticsRenderer(unittest.TestCase):
    """统计说法核查卡（renderStatistics）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cards = _read("cards.js")
        cls.css = _read("chat.css")
        # 物理边界：第 12 节（统计说法核查卡）→ 第 13 节（档案对比卡）
        cls.block = _block(cls.cards, "12. 统计说法核查卡", "13. 档案对比卡")

    def test_renderer_dispatch_and_export(self):
        self.assertTrue(self.block, "cards.js 缺少「12. 统计说法核查卡」渲染区")
        self.assertIn("function renderStatistics(", self.block)
        self.assertIn('if (type === "statistics") return renderStatistics(payload);',
                      self.cards)
        tail = self.cards.split("window.ChatCards")[-1]
        self.assertIn("renderStatistics,", tail, "renderStatistics 未导出，chat.js 调不到")

    def test_consumes_pipeline_fields(self):
        for field in ("c.status", "c.task_status", "c.as_of",
                      "c.text", "c.state", "c.claims", "c.limits", "c.gaps",
                      "c.errors", "c.counts"):
            self.assertIn(field, self.block, f"statistics 卡未消费字段：{field}")
        # degraded 经共用 degradedBadge(c) 消费（与全文件其他渲染器同口径）
        self.assertIn("degradedBadge(c)", self.block)

    def test_four_state_verdict_badges(self):
        """四态判语是封闭枚举 + 中文文字徽章，未知判语不猜。"""
        for key, text in (("consistent", "一致"), ("inconsistent", "不一致"),
                          ("caliber_mismatch", "口径不符"), ("unverifiable", "无法核实")):
            self.assertIn(f"{key}: {{", self.block, f"缺少四态判语：{key}")
            self.assertIn(text, self.block, f"缺少四态判语文案：{text}")
        self.assertIn("statisticsVerdict(cl)", self.block)
        self.assertIn("cl.verdict_label", self.block, "判语未逐字透传后端 verdict_label")

    def test_claim_group_fields(self):
        """每条数字主张一组：摘录 + 数值 | 来源数值 + 引文 | 判定徽章。"""
        for field in ("cl.claim_id", "cl.excerpt", "cl.number", "cl.unit",
                      "cl.metric", "cl.qualifiers", "cl.source_number",
                      "cl.source_title", "cl.source_url", "cl.quote",
                      "cl.as_of", "cl.basis"):
            self.assertIn(field, self.block, f"主张组未消费字段：{field}")
        self.assertIn("chat-stat-claim", self.block)
        self.assertIn("口径判定：", self.block)
        self.assertIn("比对依据：", self.block)

    def test_no_score_or_truth_labels(self):
        for banned in ("score", "confidence", "risk_score", "是真的", "是假的",
                       "属实", "谣言为真"):
            self.assertNotIn(banned, self.block,
                             f"statistics 卡出现禁用字段/判定词：{banned}")

    def test_source_urls_plain_text(self):
        """来源 URL 是外部内容：纯文本展示，不造可点链接（监测卡同口径）。"""
        self.assertIn("monitorUrlText(cl.source_url)", self.block)
        self.assertNotIn('target="_blank"', self.block)
        self.assertNotIn("<a href", self.block)

    def test_quote_escaped_verbatim(self):
        self.assertIn("escapeHtml(String(cl.quote))", self.block)
        self.assertIn("escapeHtml(String(cl.source_number))", self.block)

    def test_as_of_unknown_fallback(self):
        self.assertIn("asOfLine(c.as_of)", self.block)
        self.assertIn('cl.as_of || "未知"', self.block,
                      "主张级 as_of 为空时未如实显示「未知」")

    def test_counts_and_limits_tail(self):
        for marker in ("次搜索", "次读页", "次模型调用", "本轮的比对限制"):
            self.assertIn(marker, self.block, f"卡尾缺少：{marker}")

    def test_empty_claims_honest(self):
        """零主张是合法空出口：如实说没有，不强行比对。"""
        self.assertIn("合法空出口", self.block)

    def test_css_classes_defined(self):
        for cls in (".chat-card-statistics", ".chat-stat-claims",
                    ".chat-stat-claim", ".chat-stat-claim-head",
                    ".chat-stat-excerpt", ".chat-stat-qual",
                    ".chat-stat-source", ".chat-stat-basis"):
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
