# -*- coding: utf-8 -*-
"""T6 前端自洽测试：资料收集 / 权威核验 / 复合卡（cards.js + chat.css）。

三张新卡由后端 collect_research / verify_authority / collect_and_verify 产出
（卡片形状见 `.tmp/info-skills-contract.md` §5.1 / §6.2，接线见 §9）。本模块只做
**静态自洽**：不联网、不起浏览器、不依赖尚未落地的后端模块，直接读源码断言
渲染器、分发、导出、字段消费、转义与安全外链、口径（无评分/无固定结论标签/
引文逐字/已核验与未核验分块）、asOfLine 与移动端样式类。

这样锁定的错：改了后端卡字段忘了改前端、新增卡片走错渲染器、有人把
risk_score/confidence 又搬回新卡、引文被"顺手截断"、未核验来源混进已核验区。

运行（项目根目录下）：
    $env:PYTHONPATH='D:\\working_space\\MyLocalAI-info-skills-v2\\src'
    & 'D:\\working_space\\MyLocalAI\\.venv\\Scripts\\python.exe' -X utf8 -m unittest tests.test_research_ui -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "mylocalai" / "web" / "static"
CARDS_JS = STATIC / "cards.js"
CHAT_CSS = STATIC / "chat.css"
STYLE_CSS = STATIC / "style.css"

#: cards.js 里三张新卡的物理边界（"8~10. 信息收集…" → 分发函数之前）
SECTION_START = "8~10. 信息收集"
SECTION_END = "/* ---------- 分发"

#: 渲染器正文里允许出现的"裸插值"（不带 escapeHtml）：全部是枚举文案/数组长度，
#: 不含任何来自后端的自由文本；出现新的裸插值就说明有人绕过了转义。
BARE_INTERP_OK = re.compile(
    r"^(?:[A-Za-z_$][\w$]*\.length|(?:meta|rel)\.(?:text|cls))$")

#: 会被当成自由文本展示的字段：只要被插值，必须裹在 escapeHtml( 里。
#: 不含 "text"——枚举文案（meta.text/rel.text）本身就是固定中文常量，不是外部输入。
TEXT_FIELDS = (
    "statement", "quote", "title", "summary", "topic", "conclusion", "event",
    "reason", "name", "domain", "label", "detail", "category", "stage",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_block_comments(js: str) -> str:
    """去掉 /* ... */ 注释：口径断言只针对真正的渲染代码（注释里提到不算消费）。"""
    return re.sub(r"/\*.*?\*/", "", js, flags=re.S)


class ResearchUiStaticTest(unittest.TestCase):
    """cards.js / chat.css 的静态自洽（T6 三渲染器）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cards = _read(CARDS_JS)
        cls.css = _read(CHAT_CSS)
        cls.style_css = _read(STYLE_CSS)
        start = cls.cards.find(SECTION_START)
        if start < 0:
            raise AssertionError("cards.js 缺少信息收集/权威核验/复合卡的渲染区标记")
        # 从该区注释的 `/*` 起切，保证注释头也被 _strip_block_comments 去掉
        start = cls.cards.rfind("/*", 0, start)
        end = cls.cards.find(SECTION_END, start)
        if end < 0:
            raise AssertionError("cards.js 渲染区后找不到分发区标记")
        cls.section = cls.cards[start:end]
        cls.code = _strip_block_comments(cls.section)
        #: chat.css 里新卡样式区的边界（文件末尾追加，直到文件结束）
        cls.css_tail = cls.css.split("资料收集 / 权威核验 / 复合卡", 1)[1]

    # ------------------------------------------------------------ 工具
    def _fn(self, name: str) -> str:
        """切出某个函数的源码（渲染器粒度断言用）；找不到直接失败。"""
        marker = f"function {name}("
        start = self.code.find(marker)
        self.assertGreaterEqual(start, 0, f"渲染区缺少 function {name}")
        rest = self.code[start + len(marker):]
        stops = [idx for idx in (rest.find("\n  function "), rest.find("\n  /* ---"))
                 if idx >= 0]
        return rest[:min(stops)] if stops else rest

    def _assert_fields(self, where: str, fields) -> None:
        for field in fields:
            self.assertRegex(where, rf"\.{re.escape(field)}\b",
                             f"渲染区没有消费字段：{field}")

    # ------------------------------------------------------------ 1. 渲染器
    def test_three_renderers_and_bodies_exist(self):
        """三渲染器 + 两个正文渲染器齐全（复合卡复用正文，避免嵌套重复标题）。"""
        for fn in ("renderResearch", "renderAuthority", "renderComposite",
                   "researchBody", "authorityBody"):
            self.assertIn(f"function {fn}(", self.cards, f"cards.js 缺少 {fn}")
        # 三个渲染器都必须带降级徽章与 as_of（口径诚实：数字旁必须有截至时间）
        for fn in ("renderResearch", "renderAuthority", "renderComposite"):
            body = self._fn(fn)
            self.assertIn("degradedBadge(c)", body, f"{fn} 缺少降级徽章")
            self.assertIn("asOfLine(c.as_of)", body, f"{fn} 缺少 as_of 行")

    def test_dispatch_reads_card_type(self):
        """renderCard 按 payload.card_type 分发（历史回放走整包 payload）。"""
        self.assertIn("function renderCard(payload)", self.cards)
        self.assertIn('const type = String(payload.card_type || payload.type || "").toLowerCase();',
                      self.cards,
                      "renderCard 未按 payload.card_type 分发（历史回放会走错渲染器）")
        for branch in ('if (type === "research" || type === "collect_research") return renderResearch(payload);',
                       'if (type === "authority" || type === "verify_authority") return renderAuthority(payload);',
                       'if (type === "composite" || type === "collect_and_verify") return renderComposite(payload);'):
            self.assertIn(branch, self.cards, f"renderCard 缺少分发分支：{branch}")

    def test_shape_fallback_for_live_card_path(self):
        """实时路径 chat.js 会先解出 d.card（card_type 只在事件顶层）——必须能按字段形态认出三卡。"""
        for marker in ("payload.research !== undefined && payload.authority !== undefined",
                       "payload.key_findings !== undefined",
                       "payload.sub_questions !== undefined",
                       "payload.timeline !== undefined",
                       "cl.balance !== undefined"):
            self.assertIn(marker, self.cards, f"缺少字段形态兜底：{marker}")
        # 兜底判据不能盖住既有卡型：热度/言论土壤/画像/批次分支仍在
        for legacy in ("payload.score !== undefined", "payload.emotion || payload.attack",
                       "payload.patterns !== undefined", "payload.by_label !== undefined"):
            self.assertIn(legacy, self.cards, f"既有卡片兜底判据被挤掉了：{legacy}")

    def test_exported_in_chat_cards(self):
        """window.ChatCards 导出三项（chat.js 只认这个入口）。"""
        block = self.cards.split("window.ChatCards = {", 1)[1].split("};", 1)[0]
        for name in ("renderResearch", "renderAuthority", "renderComposite"):
            self.assertIn(name, block, f"window.ChatCards 未导出 {name}")

    # ------------------------------------------------------------ 2. 字段消费
    def test_research_fields_consumed(self):
        """research 卡字段全部被消费（§5.1 + 工具层外壳字段）。"""
        research = self.cards.split("function researchBody(", 1)[1].split("function renderResearch(", 1)[0]
        render = self._fn("renderResearch")
        self._assert_fields(research, (
            "scope", "question", "time_window", "region", "basis",
            "sub_questions", "sq_id", "status", "evidence_count",
            "key_findings", "finding_id", "sq_ids", "statement", "evidence_ids",
            "support_level", "source_quote", "checkable",
            "leads", "lead_id", "reason",
            "evidence", "role", "verification", "published_at", "fetched_at", "limitations",
            "timeline", "date", "event", "conflicts", "analysis",
            "gaps", "errors", "execution",
            "handoff_skipped", "counts",
        ))
        # 分析块 label（"基于资料的整理/推断"）在 analysisBlock 里消费
        self._assert_fields(self._fn("analysisBlock"), ("text", "label", "evidence_ids"))
        # 执行三件套在 executionLine 里消费（mode/steps/stop_reason）
        self._assert_fields(self._fn("executionLine"), ("mode", "steps", "stop_reason"))
        self._assert_fields(render, ("topic", "as_of"))
        # 外壳字段分散在共享小函数里：状态六态 / 审计计数 / 信源两级
        self._assert_fields(self._fn("runBadges"), ("status", "task_status", "incremental", "base_as_of"))
        self._assert_fields(self._fn("auditLine"), ("searches", "pages", "llm_calls"))
        self._assert_fields(self._fn("sourceGroups"), ("verified", "unverified"))
        self.assertIn("sourceGroups(c.sources)", research, "研究卡没有消费 sources")
        self.assertIn("runBadges(c)", render)
        self.assertIn("degradedBadge(c)", render)

    def test_authority_fields_consumed(self):
        """authority 卡字段全部被消费（§6.2：claims/evidence/sources/analysis/limitations）。"""
        authority = self.cards.split("function authorityBody(", 1)[1].split("function renderAuthority(", 1)[0]
        evidence_fn = self._fn("renderAuthorityEvidence")
        self._assert_fields(authority, (
            "claims", "claim_id", "text", "origin",
            "conclusion", "supporting_ids", "refuting_ids",
            "context_ids", "authority_basis", "limitations",
            "evidence", "claim_ids", "analysis", "conflicts", "sources",
            "gaps", "errors", "counts", "execution",
        ))
        # 证据条目：权威类别 / 核验方式 / 逐字引文都在
        self._assert_fields(evidence_fn, (
            "authority_class", "verification", "quote", "relation", "title", "url",
            "name", "domain", "published_at", "fetched_at", "authority_basis",
            "limitations", "evidence_id",
        ))
        # claim_status 在 leanBadge 的"不可核查"判定里；balance 在 balanceBadge 里；
        # 增量字段在共享 runBadges 里
        self._assert_fields(self._fn("leanBadge"), ("claim_status", "lean"))
        self._assert_fields(self._fn("balanceBadge"), ("balance", "balance_label"))
        self._assert_fields(self._fn("renderAuthority"), ("as_of", "counts"))

    def test_composite_fields_and_body_reuse(self):
        """composite 卡消费 research/authority/summary，正文复用两个 body（不重复标题）。"""
        comp = self._fn("renderComposite")
        self.assertIn("researchBody(research)", comp, "复合卡没有复用资料收集正文")
        self.assertIn("authorityBody(authority)", comp, "复合卡没有复用权威核验正文")
        self.assertNotIn("renderResearch(", comp, "复合卡套了整张研究卡（会重复标题）")
        self.assertNotIn("renderAuthority(", comp, "复合卡套了整张权威卡（会重复标题）")
        self.assertEqual(comp.count('class="chat-card-title"'), 1,
                         "复合卡应只有一个卡片标题，子卡用 chat-card-subtitle")
        self.assertEqual(comp.count('class="chat-card-subtitle"'), 2,
                         "复合卡应有资料收集/权威核验两个子卡标题")
        self._assert_fields(comp, ("research", "authority", "summary", "gaps", "errors", "counts"))
        self.assertIn("runBadges(c)", comp, "复合卡没有状态徽章")
        self.assertIn("asOfLine(c.as_of)", comp, "复合卡缺少 as_of 行")

    # ------------------------------------------------------------ 3. 转义与安全外链
    def test_free_text_always_escaped(self):
        """渲染区的自由文本插值必须裹在 escapeHtml( 里（含 topic/statement/quote/conclusion）。"""
        self.assertGreaterEqual(self.code.count("escapeHtml("), 40,
                                "渲染区 escapeHtml 调用过少（有人绕过转义？）")
        # 裸插值只允许枚举文案/数组长度
        for expr in re.findall(r"\$\{\s*([A-Za-z_$][\w$]*(?:\.[\w$]+)+)\s*\}", self.code):
            self.assertRegex(expr, BARE_INTERP_OK,
                             f"渲染区出现未转义的裸插值：${{{expr}}}")
        # 逐字段核对：被插值的自由文本都在 escapeHtml( 括号内
        for match in re.finditer(
                r"\$\{[^}]*\.(?:%s)\b" % "|".join(TEXT_FIELDS), self.code):
            self.assertIn("escapeHtml(", match.group(0),
                          f"自由文本未转义：{match.group(0)[:80]}")
        # 主张/分析正文（text 字段）逐个点名：必须 String() 后 escapeHtml
        for marker in ("escapeHtml(String(cl.text", "escapeHtml(String(a.text",
                       "escapeHtml(text)"):
            self.assertIn(marker, self.code, f"正文未转义：{marker}")

    def test_external_links_use_safe_url(self):
        """外链只走 safeUrl（http(s) 白名单），不给 javascript:/data: 生成 href。"""
        for fn in ("renderAuthorityEvidence", "researchBody"):
            self.assertIn("safeUrl(", self._fn(fn), f"{fn} 未使用 safeUrl")
        self.assertNotRegex(self.code, r'href="\$\{(?!url\})',
                            "渲染区出现非 safeUrl 的 href 插值")
        self.assertGreaterEqual(self.code.count("safeUrl("), 2)
        self.assertIn('rel="noopener nofollow"', self.code,
                      "外部链接缺少 rel=noopener nofollow")

    def test_no_innerhtml_in_rendering_section(self):
        """新卡渲染区只用模板字符串拼 HTML（innerHTML 只允许出现在 mountCard）。"""
        self.assertNotIn("innerHTML", self.code, "渲染区出现 innerHTML 注入点")
        self.assertNotIn("insertAdjacentHTML", self.code)

    # ------------------------------------------------------------ 4. 口径
    def test_no_scores_or_confidence_displayed(self):
        """不显示 risk_score / confidence / 任何数值评分（注释提到不算，代码里不得消费）。"""
        for token in ("risk_score", "confidence", "score-bar", "score-num",
                      "置信度", "评分", "分数", "/ 100"):
            self.assertNotIn(token, self.code, f"新卡渲染区出现评分口径：{token}")
        self.assertNotRegex(self.code, r"\bscore\b", "新卡渲染区出现 score 变量/字段")

    def test_no_unconditional_truth_labels(self):
        """不输出无条件的"真/假"标签（三张卡只述证据，结论由后端模板/枚举给出）。"""
        for token in ("真", "假", "属实", "谣言"):
            self.assertNotIn(token, self.code, f"新卡渲染区出现真假类标签：{token}")

    def test_balance_badge_is_closed_enum(self):
        """balance 徽章只用五种封闭中文枚举，后端自由文本只能落在枚举内。"""
        block = self.cards.split("const BALANCE_META = {", 1)[1].split("};", 1)[0]
        for label in ("权威证据支持", "权威证据反驳", "权威证据冲突",
                      "权威证据不足", "不可核查"):
            self.assertIn(label, block, f"balance 封闭枚举缺少：{label}")
        badge = self._fn("balanceBadge")
        self.assertIn("BALANCE_META[String(cl.balance", badge)
        self.assertIn("balance_label", badge, "balance_label 未被消费")
        self.assertIn("Object.keys(BALANCE_META)", badge,
                      "balance_label 未做封闭枚举白名单校验（自由文本会冒充徽章）")
        self.assertIn("未标注", badge, "未知 balance 缺少兜底文案")

    def test_lean_labeled_separately(self):
        """lean 单独标"证据倾向"，不与 balance 混成一个徽章。"""
        lean = self._fn("leanBadge")
        self.assertIn("证据倾向", lean)
        for label in ("倾向支持", "倾向反驳"):
            self.assertIn(label, self.cards, f"lean 文案缺少：{label}")
        self.assertIn("不适用", lean, "不可核查主张的 lean 应明说「不适用」")
        self.assertIn("LEAN_TEXT[String(cl.lean", lean)

    def test_quotes_are_verbatim(self):
        """引文逐字：只 escapeHtml，不 slice/substring/改写。"""
        quote = self._fn("verbatimQuote")
        self.assertIn("escapeHtml(text)", quote)
        for forbidden in (".slice(", ".substring(", ".substr(", "…", "已截断"):
            self.assertNotIn(forbidden, quote, f"引文渲染出现截断/改写：{forbidden}")
        research = self._fn("researchBody")
        self.assertIn("verbatimQuote(f.source_quote)", research,
                      "关键发现必须展示逐字 source_quote")
        self.assertIn("verbatimQuote(ev.quote)", research, "证据引文必须逐字展示")
        self.assertIn("verbatimQuote(ev.quote)", self._fn("renderAuthorityEvidence"),
                      "权威证据引文必须逐字展示")
        for fn in ("researchBody", "renderAuthorityEvidence"):
            self.assertNotIn(".slice(", self._fn(fn), f"{fn} 截断了正文/引文")

    def test_verified_and_unverified_sources_separated(self):
        """已核验信源与未核验来源分块展示，后者只作线索。"""
        groups = self._fn("sourceGroups")
        self.assertIn("已核验信源", groups)
        self.assertIn("未核验来源", groups)
        self.assertIn("只作线索", groups)
        self.assertLess(groups.index("已核验信源"), groups.index("未核验来源"),
                        "已核验/未核验的展示顺序被调换")
        self.assertIn("verified", groups)
        self.assertIn("unverified", groups)
        # 两张卡都必须真的调用它（否则信源分块在卡上消失）
        self.assertIn("sourceGroups(c.sources)", self._fn("researchBody"))
        self.assertIn("sourceGroups(c.sources)", self._fn("authorityBody"))

    def test_asof_line_present_for_new_cards(self):
        """asOfLine 保留并覆盖三张新卡（口径诚实：每张卡都写证据截至时间）。"""
        for fn in ("renderResearch", "renderAuthority", "renderComposite"):
            self.assertIn("asOfLine(c.as_of)", self._fn(fn), f"{fn} 缺少 asOfLine")
        self.assertGreaterEqual(self.cards.count("asOfLine("), 11,
                                "asOfLine 调用总数不足（7 张既有卡 + 3 张新卡 + 定义）")
        self.assertIn("function asOfLine(", self.cards)

    def test_run_states_and_handoff_are_honest(self):
        """六态复用同一份中文映射；取消/预算用尽带"保留已取得证据"；接力跳过有原因。"""
        self.assertIn("const RUN_STATUS = EVIDENCE_STATUS;", self.cards,
                      "新卡状态没有复用统一六态映射")
        for state in ("completed", "partial", "needs_clarification", "failed",
                      "cancelled", "budget_exhausted"):
            self.assertIn(state, self.cards, f"缺少执行状态：{state}")
        self.assertIn("保留已取得证据", self.cards)
        self.assertIn("handoff_skipped", self.cards)
        self.assertIn("未自动核验", self.cards)

    # ------------------------------------------------------------ 5. 样式
    def test_new_card_css_classes_defined(self):
        """渲染区用到的每个 chat-* class 在 chat.css 都有定义（否则新卡裸奔）。"""
        used = set()
        for chunk in re.findall(r'class="([^"]+)"', self.section):
            used.update(name for name in chunk.split() if name.startswith("chat-"))
        self.assertTrue(used, "解析不到新卡渲染区的 chat-* class")
        missing = sorted(name for name in used if f".{name}" not in self.css)
        self.assertEqual(missing, [], f"chat.css 缺少新卡样式：{missing}")
        for base in (".chat-card-research", ".chat-card-authority", ".chat-card-composite",
                     ".chat-card-sub", ".chat-card-subtitle"):
            self.assertIn(base, self.css, f"chat.css 缺少基础样式：{base}")

    def test_mobile_rules_prevent_overflow(self):
        """窄屏规则存在：换行 + flex 改列 + 子卡缩进，长引文/URL 不撑破卡片。"""
        tail = self.css_tail
        self.assertIn("@media (max-width: 900px)", tail, "新卡缺少窄屏规则")
        self.assertGreaterEqual(tail.count("overflow-wrap"), 5,
                                "新卡缺少长文本换行（移动端会溢出）")
        self.assertIn("word-break", tail)
        self.assertIn("min-width: 0", tail)
        mobile = tail.split("@media (max-width: 900px)", 1)[1]
        self.assertIn("flex-direction: column", mobile,
                      "窄屏下子问题/时间线/主张头没有改成竖排")
        for cls in (".chat-research-sq", ".chat-research-timeline li", ".chat-auth-claim-head"):
            self.assertIn(cls, mobile, f"窄屏规则未覆盖 {cls}")

    def test_css_reuses_root_tokens_only(self):
        """新卡样式只用 style.css 已声明的 :root 令牌，不另起配色、不重定义 :root。"""
        root_block = self.style_css.split(":root {", 1)[1].split("}", 1)[0]
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", root_block))
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", self.css_tail))
        self.assertTrue(used, "新卡样式没有使用任何 :root 令牌")
        self.assertEqual(sorted(used - declared), [],
                         "新卡样式用了 style.css 未声明的变量")
        self.assertNotRegex(self.css_tail, r"(?m)^\s*:root\s*\{",
                            "新卡样式不该重新定义 :root 配色")


if __name__ == "__main__":
    unittest.main(verbosity=2)
