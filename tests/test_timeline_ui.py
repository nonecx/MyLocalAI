# -*- coding: utf-8 -*-
"""事件时间线卡片前端测试（T5，恰好 12 例）。

覆盖（契约 §7，同 tests/test_research_ui.py 风格）：
   1. 渲染器与分发：renderTimeline 存在、renderCard 按 card_type 精确匹配、
      window.ChatCards 导出（缺任一前端就调不到）；
   2. 字段消费与后端契约一致：timeline 卡消费 query/state/events/undated/range/
      limits/gaps/errors/counts，事件条目消费 date/precision/event/source_url/
      source_title/quote/evidence_id；
   3. 口径诚实铁律：日期精度只做中文文字徽章（day/month/year 三值封闭枚举）；
      引文逐字透传（折叠呈现，escapeHtml 后仍是同一段文字）；来源 URL 是外部
      内容，一律纯文本呈现（与 renderMonitorChange 同口径，不造可点链接）；
      「时间不详」事件单独成区，不混入主线；不展示任何数值分，不写"是真的/
      假的/属实"固定判定；
   4. 降级与空结果：as_of / 时间范围缺省明说「未知」，空事件列表不崩不编；
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


class TestTimelineRenderer(unittest.TestCase):
    """事件时间线卡（renderTimeline）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cards = _read("cards.js")
        cls.css = _read("chat.css")
        # 物理边界：第 14 节（事件时间线卡）→ 分发段
        cls.block = _block(cls.cards, "14. 事件时间线卡", "/* ---------- 分发")

    def test_renderer_dispatch_and_export(self):
        self.assertTrue(self.block, "cards.js 缺少「14. 事件时间线卡」渲染区")
        self.assertIn("function renderTimeline(", self.block)
        self.assertIn('if (type === "timeline") return renderTimeline(payload);',
                      self.cards)
        tail = self.cards.split("window.ChatCards")[-1]
        self.assertIn("renderTimeline,", tail, "renderTimeline 未导出，chat.js 调不到")

    def test_consumes_pipeline_fields(self):
        for field in ("c.status", "c.task_status", "c.as_of",
                      "c.query", "c.state", "c.events", "c.undated", "c.range",
                      "c.limits", "c.gaps", "c.errors", "c.counts"):
            self.assertIn(field, self.block, f"timeline 卡未消费字段：{field}")
        # degraded 经共用 degradedBadge(c) 消费（与全文件其他渲染器同口径）
        self.assertIn("degradedBadge(c)", self.block)

    def test_event_item_fields(self):
        """纵向时间线条目：日期徽章 + 精度 + 事件 + 引文 + 来源 + evidence_id。"""
        for field in ("ev.date", "ev.precision", "ev.event", "ev.quote",
                      "ev.source_title", "ev.source_url", "ev.evidence_id"):
            self.assertIn(field, self.block, f"事件条目未消费字段：{field}")
        self.assertIn("chat-timeline-item", self.block)
        self.assertIn("chat-timeline-date", self.block)

    def test_precision_badges(self):
        """日期精度只做中文文字徽章（day/month/year 封闭枚举）。"""
        for key, text in (("day", "精确到日"), ("month", "精确到月"),
                          ("year", "精确到年")):
            self.assertIn(f"{key}: \"{text}\"", self.block,
                          f"缺少日期精度徽章：{key}")
        self.assertIn("TIMELINE_PRECISION_META", self.block)

    def test_undated_separate_section(self):
        """「时间不详」事件单独成区，明确不混入主线。"""
        self.assertIn("时间不详", self.block)
        self.assertIn("不混入主线", self.block)
        self.assertIn("c.undated", self.block)

    def test_quote_fold_and_escape(self):
        """引文折叠呈现 + 逐字转义（不改写、不补造）。"""
        self.assertIn("chat-evidence-fold", self.block)
        self.assertIn("escapeHtml(String(ev.quote))", self.block)

    def test_urls_plain_text(self):
        """来源 URL 是外部内容：纯文本展示，不造可点链接（监测卡同口径）。"""
        self.assertIn("monitorUrlText(ev.source_url)", self.block)
        self.assertNotIn('target="_blank"', self.block)
        self.assertNotIn("<a href", self.block)

    def test_no_score_or_truth_labels(self):
        for banned in ("score", "risk_score", "是真的", "是假的", "属实", "谣言为真"):
            self.assertNotIn(banned, self.block,
                             f"timeline 卡出现禁用字段/判定词：{banned}")

    def test_as_of_and_range_unknown_fallback(self):
        self.assertIn("asOfLine(c.as_of)", self.block)
        self.assertIn('range.earliest || "未知"', self.block,
                      "时间范围起点为空时未如实显示「未知」")
        self.assertIn('range.latest || "未知"', self.block,
                      "时间范围终点为空时未如实显示「未知」")

    def test_counts_tail(self):
        for marker in ("次搜索", "次读页", "次模型调用", "收录", "丢弃"):
            self.assertIn(marker, self.block, f"卡尾缺少：{marker}")

    def test_css_classes_defined(self):
        for cls in (".chat-card-timeline", ".chat-timeline-events",
                    ".chat-timeline-item", ".chat-timeline-head",
                    ".chat-timeline-date", ".chat-timeline-event",
                    ".chat-timeline-undated-title"):
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
