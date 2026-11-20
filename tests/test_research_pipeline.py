# -*- coding: utf-8 -*-
"""资料收集技能测试（research/pipeline）：离线、脚本化检索/取页/模型，零网络零 LLM。

锁定（抓真实缺陷的标准）：
  - 预检与过宽：空/过短/超长主题直接 needs_clarification，不进检索；
  - 检索词脱敏：邮箱/手机号/URL 被替换为占位，site:/inurl: 等操作符被剥掉；
  - 状态六态：completed / partial / needs_clarification / failed / cancelled /
    budget_exhausted 都有独立用例；零结果是 completed + gaps，不是失败；
  - 授权失败：检索降级/未授权、取页域名不允许 → failed（不是“空结果”）；
  - 读页失败：单页网络失败 → partial，错误与缺口如实记录；
  - 引文未锚定：摘录/要点里定位不到的引文被丢弃并记 error；
  - 未核验只进 leads：只引用未核验来源的要点会被降级成线索；
  - 时间线/冲突/分析/后续方向结构稳定；
  - 补检索：未覆盖子问题会触发有限轮补检索（max_followup_rounds）；
  - prior 增量：复用 topic/scope/证据，旧 evidence_id 保留，incremental=true、base_as_of=prior.as_of；
  - 两种模式同卡 schema：pipeline 与 agent 的 data/result 键集合一致；
  - 预算/取消/死线：保留已得结果，状态如实；
  - 无真假词、无分数：报告与卡片不出现判断词与置信/评分字段。

全部 IO 用桩：FakeSearch / FakeFetch / ScriptedLLM；时钟固定，不触网、不调模型。
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills import research as R  # noqa: E402

CLOCK = "2026-11-21T10:00:00+00:00"
RESEARCH_DIR = ROOT / "src" / "mylocalai" / "assistant" / "skills" / "research"


# ---------------------------------------------------------------------------
# 桩：可记录检索、可失败取页、脚本化模型
# ---------------------------------------------------------------------------

class FakeSearch:
    """检索桩：可整体失败/降级，也可按检索词返回不同结果。"""

    def __init__(self, results=None, by_query=None, degraded=False, error="",
                 raises=False):
        self.results = list(results or [])
        self.by_query = by_query
        self.degraded = degraded
        self.error = error
        self.raises = raises
        self.calls = []

    def __call__(self, query, k):
        self.calls.append(query)
        if self.raises:
            raise RuntimeError("search boom")
        if self.degraded or self.error:
            return {"results": self.results[:k], "degraded": True, "error": self.error}
        rows = self.by_query(query) if callable(self.by_query) else self.results
        return {"results": list(rows or [])[:k], "degraded": False}


class FakeFetch:
    """取页桩：成功返回 transport 形状；可整体或按 URL 失败。"""

    def __init__(self, pages=None, fail_category="", fail_urls=(), raises=False):
        self.pages = dict(pages or {})
        self.fail_category = fail_category
        self.fail_urls = set(fail_urls or ())
        self.raises = raises
        self.calls = []

    def __call__(self, url, *, allowed_domains=None, cancel_check=None, deadline=None):
        self.calls.append(url)
        if self.raises:
            raise RuntimeError("fetch boom")
        if self.fail_category or url in self.fail_urls:
            return {"status": "error", "category": self.fail_category or "network",
                    "detail": "桩故意失败", "url": url, "final_url": ""}
        page = self.pages.get(url)
        if page is None:
            return {"status": "error", "category": "network", "detail": "HTTP 404",
                    "url": url, "final_url": ""}
        return {"status": "ok", "url": url, "final_url": url,
                "domain": url.split("/")[2], "title": page.get("title", ""),
                "text": page.get("text", ""), "fetched_at": CLOCK,
                "content_type": "text/html", "bytes": len(page.get("text", "")),
                "hops": []}


class ScriptedLLM:
    """模型桩：按 system 里的阶段标识返回脚本内容；agent 模式按决策脚本逐步返回。"""

    def __init__(self, *, scope=None, extract=None, synthesis=None, decisions=None):
        self.scope = scope
        self.extract = extract
        self.synthesis = synthesis
        self.decisions = list(decisions or [])
        self.calls = []
        self.agent_calls = 0

    def __call__(self, system, user):
        self.calls.append({"system": system, "user": user})
        if "search|read|extract|finish" in system:
            index = self.agent_calls
            self.agent_calls += 1
            if index >= len(self.decisions):
                return {"action": "finish", "args": {}}, ""
            item = self.decisions[index]
            if isinstance(item, Exception):
                raise item
            return item, ""
        if '"key_findings"' in system:
            return self._value(self.synthesis, user)
        if '"quotes"' in system:
            return self._value(self.extract, user)
        if '"sub_questions"' in system:
            return self._value(self.scope, user)
        return None, "unknown_stage"

    @staticmethod
    def _value(value, user):
        if value is None:
            return None, "stub_missing"
        if callable(value):
            value = value(user)
        if isinstance(value, tuple):
            return value
        return value, ""


# ---------------------------------------------------------------------------
# 固定语料与脚本
# ---------------------------------------------------------------------------

SCOPE = {
    "state": "ok",
    "question": "电动车电池安全监管的公开资料",
    "time_window": "2024-2026",
    "region": "CN",
    "basis": ["主题聚焦电池安全与监管"],
    "sub_questions": [
        {"text": "近两年电动车起火事故的公开通报"},
        {"text": "电池安全强制标准与监管要求"},
    ],
}

NHC_URL = "https://www.nhc.gov.cn/a.html"
XH_URL = "https://www.xinhuanet.com/b.html"
BAD_URL = "https://example.org/c.html"

NHC_TEXT = ("2025年3月，国家卫生健康委发布通报：电动车电池起火事故共报告十二起，均已开展调查。"
            "通报要求各地加强充电设施检查，相关排查结果将陆续公布。")
XH_TEXT = ("2025年4月，新华网报道：新版电动自行车安全技术规范将于年内实施，"
           "电池安全要求进一步提高，生产企业需要重新送检。")
BAD_TEXT = ("网传某品牌电动车电池存在严重问题，尚未得到当地监管部门回应。"
            "该内容仅在个人主页出现，未标注信息来源。")

NHC_QUOTE = "电动车电池起火事故共报告十二起，均已开展调查"
XH_QUOTE = "新版电动自行车安全技术规范将于年内实施"
BAD_QUOTE = "网传某品牌电动车电池存在严重问题"

PAGES = {
    NHC_URL: {"title": "卫健委通报", "text": NHC_TEXT},
    XH_URL: {"title": "新华网报道", "text": XH_TEXT},
    BAD_URL: {"title": "个人主页", "text": BAD_TEXT},
}

SEARCH_ROWS = [
    {"title": "卫健委通报", "url": NHC_URL, "snippet": "通报原文"},
    {"title": "新华网报道", "url": XH_URL, "snippet": "规范报道"},
    {"title": "个人主页", "url": BAD_URL, "snippet": "网传说法"},
]


def extract_by_page(user):
    """按已读正文里的机构名返回对应引文（模拟模型按页摘录）。"""
    if "国家卫生健康委" in user:
        return {"quotes": [{"quote": NHC_QUOTE, "sq_ids": ["Q1"], "role": "fact"}],
                "published_at": "2025-03-15"}
    if "新华网" in user:
        return {"quotes": [{"quote": XH_QUOTE, "sq_ids": ["Q2"], "role": "fact"}],
                "published_at": "2025-04-02"}
    return {"quotes": [{"quote": BAD_QUOTE, "role": "context"}], "published_at": ""}


SYNTHESIS = {
    "key_findings": [
        {"statement": "监管通报记录了电动车电池起火事故并要求排查",
         "sq_ids": ["Q1"], "evidence_ids": ["E1"], "source_quote": NHC_QUOTE},
        {"statement": "新版安全技术规范将提高电池安全要求",
         "sq_ids": ["Q2"], "evidence_ids": ["E2"], "source_quote": XH_QUOTE},
    ],
    "leads": [{"statement": "有网传说法涉及某品牌电池问题", "evidence_ids": ["E3"],
               "reason": "来源未核验"}],
    "timeline": [
        {"date": "2025-03-15", "event": "卫健委发布通报", "evidence_ids": ["E1"]},
        {"date": "2025-04-02", "event": "新华网报道新规范", "evidence_ids": ["E2"]},
    ],
    "conflicts": [{"summary": "事故数量的口径在两条资料中不同",
                   "evidence_ids": ["E1", "E2"]}],
    "analysis": {"text": "公开资料集中在监管通报与标准更新两条线。",
                 "evidence_ids": ["E1", "E2"]},
    "followups": ["继续查地方监管细则"],
}

SYNTHESIS_VERIFIED = dict(SYNTHESIS, leads=[])


def make_skills(**kw):
    """默认装配：检索返回三条候选、取页成功、模型按阶段脚本回答。"""
    kw.setdefault("search_fn", FakeSearch(results=SEARCH_ROWS))
    kw.setdefault("fetch_fn", FakeFetch(pages=PAGES))
    kw.setdefault("llm_fn", ScriptedLLM(scope=SCOPE, extract=extract_by_page,
                                        synthesis=SYNTHESIS))
    kw.setdefault("clock", lambda: CLOCK)
    return R.ResearchSkills(**kw)


AGENT_DECISIONS = [
    {"action": "search", "args": {"query": "电动车电池 起火 通报"}},
    {"action": "read", "args": {"url": NHC_URL}},
    {"action": "extract", "args": {"url": NHC_URL, "quote": NHC_QUOTE, "sq_ids": ["Q1"]}},
    {"action": "search", "args": {"query": "电动自行车 安全技术规范"}},
    {"action": "read", "args": {"url": XH_URL}},
    {"action": "extract", "args": {"url": XH_URL, "quote": XH_QUOTE, "sq_ids": ["Q2"]}},
    {"action": "finish", "args": {}},
]


# ---------------------------------------------------------------------------
# 规则 / 文档
# ---------------------------------------------------------------------------

class TestRulesAndDocs(unittest.TestCase):
    def test_defaults_match_contract(self):
        rules = R.load_research_rules()
        expected = {
            "max_input_chars": 500, "max_sub_questions": 4, "max_searches": 10,
            "max_pages": 10, "max_pages_per_domain": 2, "max_llm_calls": 16,
            "max_followup_rounds": 1, "total_deadline_s": 90.0, "io_timeout_s": 10.0,
            "max_page_bytes": 1500000, "max_text_chars": 20000,
            "max_redirect_hops": 3, "max_quote_chars": 300,
        }
        for key, value in expected.items():
            self.assertEqual(rules[key], value, key)

    def test_bad_rules_file_is_clamped(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps({"max_searches": 9999, "max_pages": "many",
                                        "total_deadline_s": -5}), encoding="utf-8")
            rules = R.load_research_rules(path)
        self.assertEqual(rules["max_searches"], 200)
        self.assertEqual(rules["max_pages"], 10)
        self.assertEqual(rules["total_deadline_s"], 1.0)

    def test_skill_docs_have_boundaries_and_code_checks(self):
        for name in ("scope", "digest", "agent"):
            path = RESEARCH_DIR / name / "SKILL.md"
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            self.assertGreaterEqual(len(lines), 50, name)
            self.assertLessEqual(len(lines), 120, name)
            self.assertIn("边界案例", text, name)
            self.assertIn("代码会校验", text, name)


# ---------------------------------------------------------------------------
# 预检 / 过宽 / 脱敏
# ---------------------------------------------------------------------------

class TestPrecheck(unittest.TestCase):
    def test_empty_topic_needs_clarification(self):
        search = FakeSearch(results=SEARCH_ROWS)
        result = make_skills(search_fn=search).run("")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["task_status"], "needs_clarification")
        self.assertEqual(search.calls, [])
        self.assertTrue(any(row["stage"] == "precheck" for row in result["errors"]))

    def test_too_short_topic(self):
        result = make_skills().run("电")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertTrue(result["gaps"])

    def test_too_long_topic(self):
        result = make_skills().run("长" * 501)
        self.assertEqual(result["status"], "needs_clarification")
        self.assertTrue(any(row["category"] == "topic_too_long" for row in result["errors"]))

    def test_scope_too_broad(self):
        search = FakeSearch(results=SEARCH_ROWS)
        llm = ScriptedLLM(scope={"state": "too_broad", "sub_questions": []},
                          extract=extract_by_page, synthesis=SYNTHESIS)
        result = make_skills(search_fn=search, llm_fn=llm).run("食品安全")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(search.calls, [])
        self.assertTrue(any(row["category"] == "scope_too_broad" for row in result["errors"]))

    def test_search_query_strips_pii_and_operators(self):
        search = FakeSearch(results=SEARCH_ROWS)
        topic = "联系 zhang@example.com 或 13800138000 了解 site:evil.com 电动车电池安全"
        make_skills(search_fn=search).run(topic)
        self.assertTrue(search.calls)
        first = search.calls[0]
        self.assertNotIn("zhang@example.com", first)
        self.assertNotIn("13800138000", first)
        self.assertNotIn("site:", first.lower())
        self.assertNotIn("evil.com", first)
        self.assertIn("「邮箱」", first)
        self.assertIn("「手机号」", first)


# ---------------------------------------------------------------------------
# 外部依赖失败：授权 / 读页 / 零结果
# ---------------------------------------------------------------------------

class TestExternalFailures(unittest.TestCase):
    def test_search_unauthorized_is_failed(self):
        search = FakeSearch(error="未授权：public-search")
        result = make_skills(search_fn=search).run("电动车电池安全")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(row["stage"] == "search" for row in result["errors"]))
        self.assertTrue(result["gaps"])

    def test_search_exception_is_failed(self):
        result = make_skills(search_fn=FakeSearch(raises=True)).run("电动车电池安全")
        self.assertEqual(result["status"], "failed")

    def test_zero_results_completed_with_gaps(self):
        result = make_skills(search_fn=FakeSearch(results=[])).run("电动车电池安全")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["key_findings"], [])
        self.assertTrue(result["gaps"])
        # 2 个子问题各查一次 + 补检索各一次（零结果不是失败，也不跳过补检索）
        self.assertEqual(result["audit"]["searches"], 4)
        self.assertEqual(result["audit"]["pages"], 0)

    def test_page_failure_is_partial(self):
        result = make_skills(fetch_fn=FakeFetch(fail_category="network")).run("电动车电池安全")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["audit"]["pages"], 0)
        self.assertTrue(any(row["stage"] == "read" for row in result["errors"]))

    def test_fetch_unauthorized_is_failed(self):
        result = make_skills(fetch_fn=FakeFetch(fail_category="domain_not_allowed")).run(
            "电动车电池安全")
        self.assertEqual(result["status"], "failed")

    def test_quote_not_anchored_is_dropped(self):
        def bad_extract(user):
            return {"quotes": [{"quote": "这句话并不在正文里出现", "sq_ids": ["Q1"]}]}

        llm = ScriptedLLM(scope=SCOPE, extract=bad_extract, synthesis=SYNTHESIS)
        result = make_skills(llm_fn=llm).run("电动车电池安全")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["evidence"], [])
        self.assertTrue(any(row["category"] == "quote_not_anchored"
                            for row in result["errors"]))


# ---------------------------------------------------------------------------
# 正常产出：要点 / 线索 / 时间线 / 冲突 / 报告
# ---------------------------------------------------------------------------

class TestHappyPath(unittest.TestCase):
    def setUp(self):
        self.result = make_skills().run("电动车电池安全监管")

    def test_status_and_shell(self):
        result = self.result
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(result["tool_id"], "collect_research")
        self.assertEqual(result["card_type"], "research")
        required = {"tool_id", "status", "task_status", "data", "gaps", "errors", "audit",
                    "budget", "report", "execution", "incremental", "base_as_of",
                    "started_at", "finished_at", "card_type", "as_of", "degraded", "counts"}
        self.assertTrue(required <= set(result))

    def test_data_schema(self):
        data = self.result["data"]
        self.assertEqual(set(data), {"topic", "scope", "sub_questions", "key_findings",
                                     "leads", "evidence", "sources", "timeline",
                                     "conflicts", "analysis", "followups"})
        self.assertEqual(set(data["scope"]), {"question", "time_window", "region", "basis"})
        self.assertEqual(set(data["sub_questions"][0]),
                         {"sq_id", "text", "status", "evidence_count"})
        finding = data["key_findings"][0]
        self.assertEqual(set(finding), {"finding_id", "sq_ids", "statement", "evidence_ids",
                                        "support_level", "source_quote", "checkable"})
        evidence = data["evidence"][0]
        self.assertEqual(set(evidence), {"evidence_id", "sq_ids", "source_id", "name",
                                         "domain", "tier", "verification", "title", "url",
                                         "quote", "published_at", "fetched_at", "role",
                                         "limitations"})

    def test_findings_anchor_to_evidence_quotes(self):
        data = self.result["data"]
        quotes = {row["evidence_id"]: row["quote"] for row in data["evidence"]}
        self.assertTrue(data["key_findings"])
        for finding in data["key_findings"]:
            self.assertTrue(finding["evidence_ids"])
            self.assertIn(finding["source_quote"],
                          [quotes[item] for item in finding["evidence_ids"]])
            self.assertIn(finding["support_level"], R.SUPPORT_LEVELS)
            self.assertTrue(finding["checkable"])

    def test_source_levels(self):
        data = self.result["data"]
        verified_domains = {row["domain"] for row in data["sources"]["verified"]}
        unverified_domains = {row["domain"] for row in data["sources"]["unverified"]}
        self.assertIn("nhc.gov.cn", verified_domains)
        self.assertIn("xinhuanet.com", verified_domains)
        self.assertIn("example.org", unverified_domains)

    def test_timeline_and_conflicts(self):
        data = self.result["data"]
        self.assertEqual(len(data["timeline"]), 2)
        self.assertEqual(data["timeline"][0]["date"], "2025-03-15")
        self.assertEqual(len(data["conflicts"]), 1)
        self.assertEqual(len(data["conflicts"][0]["evidence_ids"]), 2)
        self.assertEqual(data["analysis"]["label"], "基于资料的整理/推断")
        self.assertIn("时间线", self.result["report"])
        self.assertIn("不一致", self.result["report"])

    def test_audit_counts_and_budget(self):
        audit = self.result["audit"]
        self.assertEqual(audit["searches"], 2)
        self.assertEqual(audit["pages"], 3)
        self.assertEqual(audit["llm_calls"], 1 + 3 + 1)
        self.assertEqual(self.result["budget"]["used"]["max_searches"], 2)
        self.assertFalse(self.result["degraded"])

    def test_report_is_deterministic(self):
        first = self.result["report"]
        self.assertEqual(first, R.render_report(self.result))
        self.assertIn("资料收集报告", first)
        self.assertIn(NHC_QUOTE, first)

    def test_no_verdict_words_or_scores(self):
        blob = self.result["report"] + json.dumps(self.result["data"], ensure_ascii=False)
        for word in ("属实", "谣言", "真假", "评分", "分数", "置信", "可信度",
                     "score", "confidence", "probability"):
            self.assertNotIn(word, blob, word)


class TestUnverifiedOnlyLeads(unittest.TestCase):
    def test_unverified_finding_becomes_lead(self):
        synthesis = dict(SYNTHESIS)
        synthesis["key_findings"] = [
            {"statement": "某品牌电池存在严重问题", "sq_ids": [],
             "evidence_ids": ["E3"], "source_quote": BAD_QUOTE},
        ]
        llm = ScriptedLLM(scope=SCOPE, extract=extract_by_page, synthesis=synthesis)
        result = make_skills(llm_fn=llm).run("电动车电池安全监管")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["key_findings"], [])
        leads = result["data"]["leads"]
        self.assertTrue(any(row["evidence_ids"] == ["E3"] for row in leads))
        self.assertTrue(any("未核验" in row["reason"] for row in leads))
        self.assertIn("线索", result["report"])


# ---------------------------------------------------------------------------
# 补检索 / 增量
# ---------------------------------------------------------------------------

class TestFollowupRound(unittest.TestCase):
    def test_uncovered_sub_question_triggers_followup_search(self):
        def by_query(query):
            if "官方 通报" in query:
                return [{"title": "新华网报道", "url": XH_URL}]
            if "标准" in query:
                return []
            return [{"title": "卫健委通报", "url": NHC_URL}]

        search = FakeSearch(by_query=by_query)
        result = make_skills(search_fn=search).run("电动车电池安全监管")
        self.assertGreaterEqual(len(search.calls), 3)
        self.assertTrue(any("官方 通报" in call for call in search.calls))
        statuses = {row["sq_id"]: row["status"] for row in result["data"]["sub_questions"]}
        self.assertEqual(statuses["Q2"], "covered")

    def test_followup_disabled_by_zero_rounds(self):
        search = FakeSearch(results=[{"title": "卫健委通报", "url": NHC_URL}])
        result = make_skills(search_fn=search,
                             rules={"max_followup_rounds": 0}).run("电动车电池安全监管")
        self.assertEqual(len(search.calls), 2)


class TestIncremental(unittest.TestCase):
    def test_prior_followup_reuses_topic_scope_and_evidence(self):
        first = make_skills().run("电动车电池安全监管")
        self.assertFalse(first["incremental"])
        self.assertEqual(first["base_as_of"], "")

        llm = ScriptedLLM(scope=SCOPE, extract=extract_by_page,
                          synthesis=dict(SYNTHESIS, key_findings=[], leads=[],
                                         timeline=[], conflicts=[],
                                         analysis={"text": "", "evidence_ids": []},
                                         followups=[]))
        search = FakeSearch(results=[])
        second = make_skills(search_fn=search, llm_fn=llm).run(
            "", prior=first, followup=True)
        self.assertTrue(second["incremental"])
        self.assertEqual(second["base_as_of"], first["as_of"])
        self.assertEqual(second["data"]["topic"], first["data"]["topic"])
        self.assertEqual(second["data"]["scope"]["question"],
                         first["data"]["scope"]["question"])
        old_ids = [row["evidence_id"] for row in first["data"]["evidence"]]
        new_ids = [row["evidence_id"] for row in second["data"]["evidence"]]
        for evidence_id in old_ids:
            self.assertIn(evidence_id, new_ids)
        # 上一张卡已覆盖全部子问题：本次不再检索，也不重复界定范围
        self.assertEqual(search.calls, [])
        self.assertEqual(second["audit"]["searches"], 0)
        self.assertEqual(second["status"], "completed")

    def test_prior_without_followup_is_fresh_run(self):
        first = make_skills().run("电动车电池安全监管")
        second = make_skills().run("另一个主题", prior=first, followup=False)
        self.assertFalse(second["incremental"])
        self.assertEqual(second["data"]["topic"], "另一个主题")

    def test_followup_without_prior_is_normal_first_run(self):
        result = make_skills().run("", prior=None, followup=True)
        self.assertFalse(result["incremental"])
        self.assertEqual(result["status"], "needs_clarification")

    def test_incremental_new_evidence_ids_continue(self):
        prior = {
            "card_type": "research",
            "as_of": "2026-11-20T09:00:00+00:00",
            "data": {
                "topic": "电动车电池安全监管",
                "scope": {"question": "旧研究问题", "time_window": "", "region": "", "basis": []},
                "sub_questions": [
                    {"sq_id": "Q1", "text": "近两年电动车起火事故的公开通报",
                     "status": "covered", "evidence_count": 1},
                    {"sq_id": "Q2", "text": "电池安全强制标准与监管要求",
                     "status": "uncovered", "evidence_count": 0},
                ],
                "key_findings": [
                    {"finding_id": "F1", "sq_ids": ["Q1"], "statement": "上一张卡的要点",
                     "evidence_ids": ["E7"], "support_level": "authoritative",
                     "source_quote": NHC_QUOTE, "checkable": True},
                ],
                "leads": [],
                "evidence": [
                    {"evidence_id": "E7", "sq_ids": ["Q1"], "source_id": "SRC-NHC",
                     "name": "国家卫生健康委员会", "domain": "nhc.gov.cn", "tier": "官方权威",
                     "verification": "catalog_anchor", "title": "旧通报", "url": NHC_URL,
                     "quote": NHC_QUOTE, "published_at": "2025-03-15",
                     "fetched_at": "2026-11-20T09:00:00+00:00", "role": "fact",
                     "limitations": []},
                ],
                "sources": {"verified": [], "unverified": []},
                "timeline": [], "conflicts": [],
                "analysis": {"text": "", "evidence_ids": [],
                             "label": "基于资料的整理/推断"},
                "followups": [],
            },
        }
        search = FakeSearch(results=[{"title": "新华网报道", "url": XH_URL}])
        llm = ScriptedLLM(scope=SCOPE, extract=extract_by_page,
                          synthesis=dict(SYNTHESIS_VERIFIED, key_findings=[]))
        result = make_skills(search_fn=search, llm_fn=llm).run(
            "", prior=prior, followup=True)
        ids = [row["evidence_id"] for row in result["data"]["evidence"]]
        self.assertIn("E7", ids)
        self.assertIn("E8", ids)      # 新证据从已有最大编号往后排，不覆盖旧 id
        self.assertEqual(result["base_as_of"], "2026-11-20T09:00:00+00:00")
        self.assertEqual(result["data"]["key_findings"][0]["finding_id"], "F1")
        self.assertEqual(result["status"], "completed")

    def test_pages_per_domain_cap(self):
        rows = [{"title": f"通报{i}", "url": f"https://www.nhc.gov.cn/{i}.html"}
                for i in range(4)]
        pages = {row["url"]: {"title": row["title"], "text": NHC_TEXT} for row in rows}
        fetch = FakeFetch(pages=pages)
        llm = ScriptedLLM(scope=SCOPE, extract=extract_by_page,
                          synthesis=SYNTHESIS_VERIFIED)
        result = make_skills(search_fn=FakeSearch(results=rows), fetch_fn=fetch,
                             llm_fn=llm).run("电动车电池安全监管")
        self.assertEqual(len(fetch.calls), 2)     # 每域最多读 2 页
        self.assertEqual(result["audit"]["pages"], 2)


# ---------------------------------------------------------------------------
# 两种模式
# ---------------------------------------------------------------------------

class TestModes(unittest.TestCase):
    def test_both_modes_share_card_schema(self):
        pipeline = make_skills().run("电动车电池安全监管")
        agent_llm = ScriptedLLM(scope=SCOPE, extract=extract_by_page,
                                synthesis=SYNTHESIS_VERIFIED, decisions=AGENT_DECISIONS)
        agent = make_skills(llm_fn=agent_llm).run("电动车电池安全监管", mode="agent")
        self.assertEqual(pipeline["execution"]["mode"], "pipeline")
        self.assertEqual(agent["execution"]["mode"], "agent")
        self.assertEqual(set(pipeline["data"]), set(agent["data"]))
        self.assertEqual(set(pipeline), set(agent))
        self.assertEqual(agent["execution"]["steps"], len(AGENT_DECISIONS))
        self.assertEqual(agent["status"], "completed")
        self.assertTrue(agent["data"]["evidence"])

    def test_preferences_choose_mode(self):
        agent_llm = ScriptedLLM(scope=SCOPE, extract=extract_by_page,
                                synthesis=SYNTHESIS_VERIFIED, decisions=AGENT_DECISIONS)
        skills = make_skills(llm_fn=agent_llm,
                             preferences={"research": {"workflow_mode": "agent"}})
        result = skills.run("电动车电池安全监管")
        self.assertEqual(result["execution"]["mode"], "agent")
        forced = skills.run("电动车电池安全监管", mode="pipeline")
        self.assertEqual(forced["execution"]["mode"], "pipeline")

    def test_agent_rejects_read_outside_pool(self):
        decisions = [
            {"action": "read", "args": {"url": "https://not-in-pool.example/x"}},
            {"action": "finish", "args": {}},
        ]
        llm = ScriptedLLM(scope=SCOPE, extract=extract_by_page,
                          synthesis=SYNTHESIS_VERIFIED, decisions=decisions)
        result = make_skills(llm_fn=llm).run("电动车电池安全监管", mode="agent")
        steps = result["execution"]
        self.assertEqual(steps["mode"], "agent")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(row["category"] == "rejected_url_not_in_pool"
                            for row in result["errors"]))

    def test_agent_extract_must_anchor(self):
        decisions = [
            {"action": "search", "args": {"query": "电动车电池 通报"}},
            {"action": "read", "args": {"url": NHC_URL}},
            {"action": "extract", "args": {"url": NHC_URL, "quote": "正文里没有这句"}},
            {"action": "finish", "args": {}},
        ]
        llm = ScriptedLLM(scope=SCOPE, extract=extract_by_page,
                          synthesis=SYNTHESIS_VERIFIED, decisions=decisions)
        result = make_skills(llm_fn=llm).run("电动车电池安全监管", mode="agent")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["evidence"], [])
        self.assertTrue(any(row["category"] == "rejected_quote_not_anchored"
                            for row in result["errors"]))


# ---------------------------------------------------------------------------
# 预算 / 取消 / 进度
# ---------------------------------------------------------------------------

class TestBudgetAndCancel(unittest.TestCase):
    def test_search_budget_exhausted(self):
        result = make_skills(rules={"max_searches": 1}).run("电动车电池安全监管")
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["audit"]["searches"], 1)
        self.assertEqual(result["budget"]["used"]["max_searches"], 1)
        self.assertTrue(any("预算" in gap for gap in result["gaps"]))

    def test_llm_budget_exhausted_in_agent_mode(self):
        decisions = [{"action": "search", "args": {"query": "电动车电池 通报"}}]
        llm = ScriptedLLM(scope=SCOPE, extract=extract_by_page,
                          synthesis=SYNTHESIS_VERIFIED, decisions=decisions)
        result = make_skills(llm_fn=llm, rules={"max_llm_calls": 2}).run(
            "电动车电池安全监管", mode="agent")
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["budget"]["used"]["max_llm_calls"], 2)

    def test_cancelled_keeps_partial_results(self):
        search = FakeSearch(results=SEARCH_ROWS)
        result = make_skills(search_fn=search).run(
            "电动车电池安全监管", cancel_check=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(search.calls, [])
        self.assertTrue(any("取消" in gap for gap in result["gaps"]))

    def test_deadline_exhausted(self):
        result = make_skills().run("电动车电池安全监管",
                                   deadline=time.monotonic() - 1)
        self.assertEqual(result["status"], "budget_exhausted")

    def test_on_progress_called(self):
        events = []
        make_skills().run("电动车电池安全监管",
                          on_progress=lambda stage, cur, total: events.append((stage, cur, total)))
        self.assertTrue(events)
        for stage, current, total in events:
            self.assertIsInstance(stage, str)
            self.assertIsInstance(current, int)
            self.assertIsInstance(total, int)
        self.assertEqual(events[0][1], 1)


class TestConvenience(unittest.TestCase):
    def test_collect_research_helper(self):
        result = R.collect_research("电动车电池安全监管", search_fn=FakeSearch(results=[]),
                                    fetch_fn=FakeFetch(), llm_fn=ScriptedLLM(scope=SCOPE),
                                    clock=lambda: CLOCK)
        self.assertEqual(result["tool_id"], "collect_research")
        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
