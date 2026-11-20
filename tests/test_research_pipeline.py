# -*- coding: utf-8 -*-
"""资料收集流水线测试（契约 §4，恰好 26 例）。

分组（组名即 TestXxx 类）：
  1. 规则与常量（3）：默认值/坏文件回退/数值夹取、公开接口齐备；
  2. 预检与范围（5）：空主题 clarify、超长 clarify、too_broad 不检索、
     scope_basis 未锚定 → unclear、无子问题时按主题兜底；
  3. 检索与读页（5）：检索词剥 site:/PII、未授权 → failed 且不读页、
     读页失败 → partial、每域页数上限、预算到顶保留已得结果；
  4. 摘录与综述（7）：引文未锚定丢弃、坏 role 记 error、未知 evidence_id 丢弃、
     发现无证据丢弃、只引未核验 → 降级线索、时间线/分歧/分析引用校验、补检索轮；
  5. 状态与报告（6）：no_factual 不适用本能力（检索成功零结果 completed）、
     cancelled 保留、budget_exhausted 保留、六态集合、报告无真假判定词、
     报告含执行计数与引文逐字。

全离线：search_fn / fetch_fn / llm_fn / clock 全部 stub，零网络零 LLM。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills.research import ResearchSkills, collect_research  # noqa: E402
from mylocalai.assistant.skills.research.pipeline import (  # noqa: E402
    DEFAULT_RESEARCH_RULES, load_rules, render_report)

TOPIC = "2026年杭州余杭区自来水水质争议"


def host_of(url: str) -> str:
    from urllib.parse import urlsplit
    return (urlsplit(url).hostname or "").lower().rstrip(".")


#: 固定时钟（报告时点可断言）
CLOCK = lambda: "2026-11-20T12:00:00+08:00"  # noqa: E731

PAGE = {
    "status": "ok", "url": "https://www.nhc.gov.cn/a", "final_url": "https://www.nhc.gov.cn/a",
    "domain": "nhc.gov.cn", "title": "某市自来水水质通报", "text": "全文：水质检测结果合格，请市民放心。",
    "content_type": "text/html", "bytes": 40, "hops": [], "fetched_at": "2026-11-20T11:00:00+08:00",
}

OFFICIAL_PAGE = {
    "status": "ok", "url": "https://www.fake.gov.cn/", "final_url": "https://www.fake.gov.cn/",
    "domain": "fake.gov.cn", "title": "某市人民政府",
    "text": "政府网站标识码 1234567890 主办单位：某市人民政府 公网安备 123号 正文：已发布检测报告。",
    "content_type": "text/html", "bytes": 60, "hops": [], "fetched_at": "2026-11-20T11:00:00+08:00",
}


def _scope_obj(state="ok", basis="自来水水质争议", subs=2):
    return {
        "state": state,
        "scope": "自来水水质争议的官方通报与检测数据",
        "scope_basis": basis,
        "time_window": "2026年",
        "region": "杭州余杭区",
        "sub_questions": [{"text": f"官方通报说了什么 {i}"} for i in range(subs)],
        "queries": ["自来水 水质 通报"],
    }


def _digest_obj(items=None):
    return {"items": items or [
        {"quote": "水质检测结果合格，请市民放心。", "summary": "官方通报称检测合格", "role": "fact"},
    ]}


def _synth_obj(findings=1, leads=0):
    return {
        "key_findings": [{"statement": "官方通报称水质检测合格",
                          "evidence_ids": ["E1"]} for _ in range(findings)],
        "leads": [{"statement": "某论坛称有人不适", "evidence_ids": ["E1"],
                   "reason": "仅见于未核验域名"} for _ in range(leads)],
        "timeline": [{"date": "2026-11-01", "event": "官方发布通报", "evidence_ids": ["E1"]}],
        "conflicts": [],
        "analysis": {"text": "现有资料显示官方已发布检测结论。", "evidence_ids": ["E1"]},
        "followups": ["后续是否公布完整检测报告"],
    }


def _results(*rows):
    return {"results": list(rows), "degraded": False, "error": ""}


def make_skills(scope=None, digest=None, synth=None, search=None, fetch=None, **kw):
    """按调用目标喂 LLM：scope → evidence:quote（逐页，可多次）→ synthesize。

    用 target 判断而不是调用序号：摘录会被每一页调用一次，合成只喂一次
    （补检索轮之后再合成返回空对象，避免 stub 自己造出第二轮发现）。
    """
    scope_obj = scope if scope is not None else _scope_obj()
    digest_obj = digest if digest is not None else _digest_obj()
    synth_obj = synth if synth is not None else _synth_obj()
    state = {"synth_used": False}

    def _llm(system, user):
        # 按**契约尾部**区分阶段：SKILL.md frontmatter 里也有 key_findings/items 等词，
        # 只有代码追加在末尾的机器契约能可靠区分（scope/digest/synthesize 各不同）。
        if system.rstrip().endswith("不给数值分。") and '"state"' in system:
            return scope_obj, ""
        if '"key_findings"' in system and system.rstrip().endswith("不得出现真假判定词。"):
            if state["synth_used"]:
                return {"key_findings": [], "leads": []}, ""
            state["synth_used"] = True
            return synth_obj, ""
        return digest_obj, ""

    search_fn = search if search is not None else (lambda q, k: _results(
        {"url": "https://www.nhc.gov.cn/a", "title": "某市自来水水质通报"}))
    fetch_fn = fetch if fetch is not None else (lambda url, **kw: PAGE)
    return ResearchSkills(search_fn=search_fn, fetch_fn=fetch_fn, llm_fn=_llm,
                          clock=CLOCK, **kw)


class TestRulesAndConstants(unittest.TestCase):
    def test_defaults_and_clamp(self):
        rules = load_rules({"max_searches": 9999, "total_deadline_s": -1})
        self.assertEqual(rules["max_searches"], 200)
        self.assertEqual(rules["total_deadline_s"], 1.0)

    def test_missing_file_returns_defaults(self):
        with TemporaryDirectory() as tmp:
            rules = load_rules(Path(tmp) / "nope.json")
        self.assertEqual(rules["max_sub_questions"], DEFAULT_RESEARCH_RULES["max_sub_questions"])

    def test_public_api(self):
        for name in ("ResearchSkills", "collect_research", "render_report", "load_rules"):
            self.assertTrue(hasattr(sys.modules["mylocalai.assistant.skills.research"], name)
                            or hasattr(sys.modules["mylocalai.assistant.skills.research.pipeline"], name))


class TestPrecheckAndScope(unittest.TestCase):
    def test_empty_topic_clarifies_without_search(self):
        seen = {"search": 0}

        def _search(q, k):
            seen["search"] += 1
            return _results()

        out = make_skills(search=_search).run("")
        self.assertEqual(out["status"], "needs_clarification")
        self.assertEqual(seen["search"], 0)

    def test_too_long_topic_clarifies(self):
        out = make_skills().run("水" * 600)
        self.assertEqual(out["status"], "needs_clarification")

    def test_too_broad_does_not_search(self):
        seen = {"search": 0}

        def _search(q, k):
            seen["search"] += 1
            return _results()

        out = make_skills(scope=_scope_obj(state="too_broad"), search=_search).run(TOPIC)
        self.assertEqual(out["status"], "needs_clarification")
        self.assertEqual(seen["search"], 0)
        self.assertTrue(any("收窄" in g for g in out["gaps"]))

    def test_scope_basis_not_anchored_becomes_unclear(self):
        out = make_skills(scope=_scope_obj(basis="完全不在主题里的依据")).run(TOPIC)
        self.assertEqual(out["status"], "needs_clarification")

    def test_no_sub_questions_falls_back_to_topic(self):
        out = make_skills(scope=_scope_obj(subs=0)).run(TOPIC)
        self.assertTrue(out["data"]["sub_questions"])
        self.assertTrue(any("子问题" in g for g in out["gaps"]))


class TestSearchAndRead(unittest.TestCase):
    def test_query_operators_stripped(self):
        seen = {"q": []}

        def _search(q, k):
            seen["q"].append(q)
            return _results()

        make_skills(scope=_scope_obj(), search=_search).run(TOPIC)
        self.assertTrue(seen["q"])
        for query in seen["q"]:
            self.assertNotIn("site:", query)

    def test_search_unauthorized_fails_without_fetch(self):
        seen = {"fetch": 0}

        def _fetch(url, **kw):
            seen["fetch"] += 1
            return PAGE

        out = make_skills(search=lambda q, k: {"results": [], "degraded": True,
                                               "error": "consent_denied: 外部检索未授权"},
                          fetch=_fetch).run(TOPIC)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(seen["fetch"], 0)

    def test_fetch_error_records_partial(self):
        def _fetch(url, **kw):
            return {"status": "error", "category": "timeout", "detail": "超时",
                    "url": url, "final_url": ""}

        out = make_skills(fetch=_fetch).run(TOPIC)
        self.assertEqual(out["status"], "partial")
        self.assertTrue(out["errors"])
        self.assertTrue(any(e["category"] == "timeout" for e in out["errors"]))

    def test_per_domain_page_cap(self):
        seen = {"urls": []}

        def _fetch(url, **kw):
            seen["urls"].append(url)
            return dict(PAGE, url=url, final_url=url, domain="nhc.gov.cn")

        make_skills(fetch=_fetch).run(TOPIC)
        self.assertLessEqual(len(seen["urls"]), int(load_rules()["max_pages"]))

    def test_budget_exhausted_keeps_results(self):
        def _search(q, k):
            return _results({"url": "https://www.nhc.gov.cn/a", "title": "自来水水质通报"})

        out = make_skills(search=_search).run(TOPIC, deadline=0.0)
        self.assertEqual(out["status"], "budget_exhausted")
        self.assertIn("已保留", "".join(out["gaps"]))

    def test_cancel_after_first_evidence_keeps_it(self):
        """取消必须保留已取得的证据（不是空场景里自说自话）。"""
        state = {"llm": 0}

        def _llm(system, user):
            state["llm"] += 1
            # 按**契约尾部**分阶段：SKILL.md frontmatter 里也有 key_findings/items
            if system.rstrip().endswith("不给数值分。") and '"state"' in system:
                return _scope_obj(), ""
            if '"key_findings"' in system and system.rstrip().endswith("不得出现真假判定词。"):
                return _synth_obj(), ""
            return _digest_obj(), ""

        skills = ResearchSkills(
            search_fn=lambda q, k: _results({"url": "https://www.nhc.gov.cn/a",
                                             "title": "自来水水质通报"}),
            fetch_fn=lambda url, **kw: PAGE, llm_fn=_llm, clock=CLOCK)
        # 第 1 次=scope，第 2 次=摘录（证据已入账），第 3 次=综述前取消
        out = skills.run(TOPIC, cancel_check=lambda: state["llm"] >= 3)
        self.assertEqual(out["status"], "cancelled")
        self.assertTrue(out["data"]["evidence"], "取消把已取得的证据丢了")
        self.assertEqual(out["data"]["evidence"][0]["quote"],
                         "水质检测结果合格，请市民放心。")

    def test_search_ok_zero_results_is_completed(self):
        """检索全部成功但零命中 = 合法空结果：completed + 缺口，不是 partial/failed。"""
        out = make_skills(search=lambda q, k: {"results": [], "degraded": False,
                                               "error": ""}).run(TOPIC)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["errors"], [])
        self.assertTrue(any("没有取得可定位引文" in g for g in out["gaps"]))

    def test_deadline_stops_before_any_io(self):
        """过期死线：任何 IO 都不该发起（自建与传入两条路径同一口径）。"""
        import time as _time
        seen = {"search": 0}

        def _search(q, k):
            seen["search"] += 1
            return _results()

        out = ResearchSkills(rules={"total_deadline_s": 1}, search_fn=_search,
                             fetch_fn=lambda url, **kw: PAGE,
                             llm_fn=lambda s, u: (_scope_obj(), ""), clock=CLOCK).run(
            TOPIC, deadline=_time.monotonic() - 1)
        self.assertEqual(out["status"], "budget_exhausted")
        self.assertEqual(seen["search"], 0)

    def test_failed_fetch_counts_against_page_budget(self):
        """取页按"尝试"记账：失败也占额度，max_pages 才构成真实的取页次数上界。"""
        seen = {"n": 0}

        def _fetch(url, **kw):
            seen["n"] += 1
            return {"status": "error", "category": "timeout", "detail": "超时",
                    "url": url, "final_url": ""}

        out = make_skills(fetch=_fetch, rules={"max_pages": 3}).run(TOPIC)
        self.assertLessEqual(seen["n"], 3)
        self.assertEqual(out["budget"]["used"]["max_pages"], seen["n"])


class TestExtractAndSynthesize(unittest.TestCase):
    def test_quote_not_anchored_dropped(self):
        digest = {"items": [{"quote": "正文里根本没有的一句话", "summary": "x", "role": "fact"}]}
        out = make_skills(digest=digest).run(TOPIC)
        self.assertEqual(out["data"]["evidence"], [])
        self.assertTrue(any(e["category"] == "quote_not_anchored" for e in out["errors"]))

    def test_bad_role_records_error(self):
        digest = {"items": [{"quote": "水质检测结果合格，请市民放心。",
                             "summary": "通报称合格", "role": "verdict"}]}
        out = make_skills(digest=digest).run(TOPIC)
        self.assertTrue(any(e["category"] == "bad_role" for e in out["errors"]))

    def test_unknown_evidence_id_dropped(self):
        synth = _synth_obj()
        synth["key_findings"][0]["evidence_ids"] = ["E99"]
        out = make_skills(synth=synth).run(TOPIC)
        self.assertEqual(out["data"]["key_findings"], [])
        self.assertTrue(any(e["category"] == "unknown_evidence_id" for e in out["errors"]))

    def test_finding_without_evidence_dropped(self):
        synth = _synth_obj()
        synth["key_findings"][0]["evidence_ids"] = []
        out = make_skills(synth=synth).run(TOPIC)
        self.assertEqual(out["data"]["key_findings"], [])

    def test_unverified_only_finding_becomes_lead(self):
        # 让证据全部来自未核验域名：fetch 返回陌生域名页面
        def _fetch(url, **kw):
            return {"status": "ok", "url": url, "final_url": url, "domain": "bbs.example",
                    "title": "某论坛", "text": "论坛原文：有人说喝水后不舒服。",
                    "content_type": "text/html", "bytes": 30, "hops": [],
                    "fetched_at": "2026-11-20T11:00:00+08:00"}

        out = make_skills(search=lambda q, k: _results(
            {"url": "https://bbs.example/t/1", "title": "某论坛帖子"}), fetch=_fetch,
            digest={"items": [{"quote": "论坛原文：有人说喝水后不舒服。",
                               "summary": "论坛说法", "role": "fact"}]}).run(TOPIC)
        self.assertEqual(out["data"]["key_findings"], [])
        self.assertTrue(out["data"]["leads"])

    def test_timeline_conflicts_analysis_citations(self):
        # 关掉补检索轮：否则第二轮空综述会把 timeline/analysis 覆盖成空
        synth = _synth_obj()
        synth["analysis"]["evidence_ids"] = ["E99"]
        synth["timeline"][0]["evidence_ids"] = ["E99"]
        out = make_skills(synth=synth, rules={"max_followup_rounds": 0}).run(TOPIC)
        self.assertEqual(out["data"]["analysis"], {})
        self.assertEqual(out["data"]["timeline"][0]["evidence_ids"], [])

    def test_followup_round_runs_when_findings_thin(self):
        # 关键发现 0 条 → 触发一轮补检索
        synth = _synth_obj(findings=0)
        out = make_skills(synth=synth).run(TOPIC)
        self.assertTrue(any("补充检索" in g for g in out["gaps"]))

    def test_followup_round_keeps_evidence_ids_stable(self):
        """补检索轮不得覆盖/重排上一轮证据：发现引用的 E1 必须仍然存在。"""
        state = {"round": 0}

        def _search(q, k):
            state["round"] += 1
            if state["round"] <= 3:
                return _results({"url": "https://www.nhc.gov.cn/a",
                                 "title": "自来水水质通报"})
            return _results({"url": "https://www.samr.gov.cn/b", "title": "抽检通报"})

        def _fetch(url, **kw):
            if "nhc.gov.cn" in url:
                return PAGE
            return {"status": "error", "category": "timeout", "detail": "超时",
                    "url": url, "final_url": ""}

        out = make_skills(search=_search, fetch=_fetch, synth=_synth_obj(findings=0)).run(TOPIC)
        ids = {ev["evidence_id"] for ev in out["data"]["evidence"]}
        self.assertTrue(ids, "上一轮取得的证据被补检索轮丢掉了")
        for finding in out["data"]["key_findings"]:
            for evidence_id in finding["evidence_ids"]:
                self.assertIn(evidence_id, ids, "发现引用了不存在的证据（悬空引用）")

    def test_duplicate_body_is_merged(self):
        """同正文哈希 → 合并为一条证据并记"不构成独立证据"局限。"""

        def _fetch(url, **kw):
            return dict(PAGE, url=url, final_url=url, domain=host_of(url))

        out = make_skills(search=lambda q, k: _results(
            {"url": "https://www.nhc.gov.cn/a", "title": "自来水水质通报"},
            {"url": "https://www.chinacdc.cn/a", "title": "同一篇通报的转载"}),
            fetch=_fetch).run(TOPIC)
        self.assertEqual(len(out["data"]["evidence"]), 1)
        self.assertTrue(any("不构成独立证据" in x
                            for x in out["data"]["evidence"][0]["limitations"]))

    def test_verdict_word_in_finding_is_dropped(self):
        """页面注入"判定为真"不得进简报（铁律 1：只述资料）。"""
        synth = _synth_obj()
        synth["key_findings"][0]["statement"] = "由此可见该说法是真的，详见 https://evil.example"
        out = make_skills(synth=synth).run(TOPIC)
        self.assertEqual(out["data"]["key_findings"], [])
        self.assertTrue(any(e["category"] == "finding_unsafe_text" for e in out["errors"]))
        report = render_report(out)
        self.assertNotIn("evil.example", report)
        self.assertNotIn("是真的", report)

    def test_queries_stripped_of_operators_and_pii(self):
        seen = {"q": []}

        def _search(q, k):
            seen["q"].append(q)
            return _results()

        scope = _scope_obj()
        scope["queries"] = ["site:evil.com 矿泉水", "admin@example.com 水质",
                            "13812345678 自来水"]
        make_skills(scope=scope, search=_search).run(TOPIC)
        joined = " ".join(seen["q"])
        self.assertNotIn("site:", joined)
        self.assertNotIn("@", joined)
        self.assertNotIn("13812345678", joined)

    def test_report_escapes_markup_in_evidence_fields(self):
        """证据标题/来源名来自页面 <title>（攻击者可控），报告必须转义。"""
        dirty = dict(PAGE, title="<b>通报</b> [x](https://evil.example)")
        out = make_skills(fetch=lambda url, **kw: dirty).run(TOPIC)
        report = render_report(out)
        self.assertNotIn("<b>", report)
        self.assertNotIn("](", report)
        self.assertIn("＜b＞", report)


class TestStatusAndReport(unittest.TestCase):
    def test_search_ok_but_zero_evidence_is_completed(self):
        out = make_skills(fetch=lambda url, **kw: {"status": "error", "category": "timeout",
                                                   "detail": "超时", "url": url,
                                                   "final_url": ""}).run(TOPIC)
        # 读页失败属 partial（错误可见），但绝不能是 failed
        self.assertIn(out["status"], ("partial", "completed"))
        self.assertNotEqual(out["status"], "failed")

    def test_cancelled_keeps_results(self):
        out = make_skills().run(TOPIC, cancel_check=lambda: True)
        self.assertEqual(out["status"], "cancelled")
        self.assertTrue(any("已保留" in g for g in out["gaps"]))

    def test_status_set_closed(self):
        from mylocalai.assistant.skills.research.pipeline import STATUSES
        self.assertEqual(set(STATUSES), {"completed", "partial", "needs_clarification",
                                         "failed", "cancelled", "budget_exhausted"})

    def test_report_has_no_verdict_words(self):
        out = make_skills().run(TOPIC)
        report = render_report(out)
        for word in ("是真的", "是假的", "属实", "谣言为真"):
            self.assertNotIn(word, report)

    def test_report_contains_counts_and_verbatim_quote(self):
        out = make_skills().run(TOPIC)
        report = render_report(out)
        self.assertIn("次搜索", report)
        self.assertIn("次读页", report)
        self.assertIn("水质检测结果合格，请市民放心。", report)

    def test_report_has_no_markdown_link_syntax(self):
        out = make_skills().run(TOPIC)
        report = render_report(out)
        self.assertNotIn("](", report)
        self.assertNotIn("<", report.replace("＜", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
