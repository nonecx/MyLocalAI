# -*- coding: utf-8 -*-
"""事件时间线流水线测试（第五波契约，共 43 例）：skills/timeline/pipeline。

分组（组名即 TestXxx 类）：
   1. 规则与常量（4）：默认值/坏文件回退/数值夹取、rules.json 与内置默认一致、
      公开接口齐备、六态集合封闭；
   2. 日期归一（8）：绝对日期三种形态与精度、非法日期逐级退化、相对日期（日/周/月/年）、
      无法解析的表达、缺 as_of 不归一、最左优先与绝对优先；
   3. 预检（2）：空主题 clarify 不检索、超长 clarify 不检索；
   4. 检索与读页（6）：检索词剥 site:/PII、未授权 → failed 且零读页、
      读页全败 → partial、读页预算上限、过期死线零 IO、失败取页也占预算；
   5. 事件抽取（11）：锚定事件形状、引文未锚定丢弃、缺引文丢弃、超长引文截断后
      可定位、超长引文截断后仍不可定位丢弃、判定词/URL 丢弃、无日期事件隔离、
      日期从引文兜底抽取、相对日期按 as_of 归一、日期无法归一进无日期区、
      未注入模型 → partial 零事件；
   6. 去重排序与上限（4）：同文副本合并、同域+标题近似合并、按时间排序与 range、
      max_events 上限截断；
   7. 状态与报告（6）：检索零结果 completed、取消保留、预算到顶保留、data 键锁定、
      报告无真假词+引文逐字+计数行、报告转义标题标记；
   8. 受约束补充说明（2）：合规 note 进报告、越界（URL/判定词/新日期）丢弃。

全离线：search_fn / fetch_fn / llm_fn / clock 全部 stub，零网络零 LLM。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills.timeline import (  # noqa: E402
    TimelineSkills, event_timeline, load_rules, render_report)
from mylocalai.assistant.skills.timeline import pipeline as S  # noqa: E402

TOPIC = "2026年杭州余杭区自来水水质争议事件"

#: 固定时钟（as_of = 2026-11-23；相对日期归一基准可断言）
CLOCK = lambda: "2026-11-23T12:00:00+08:00"  # noqa: E731

NHC_URL = "https://www.nhc.gov.cn/a"
NHC_URL2 = "https://www.nhc.gov.cn/a2"
SAMR_URL = "https://www.samr.gov.cn/b"
CDC_URL = "https://www.chinacdc.cn/a"

#: 两段已读正文（各含一个绝对日期表达式）
BODY_NHC = ("2026年11月20日，某市发布自来水水质通报：水质检测结果合格，请市民放心。"
            "11月19日，当地疾控中心介入调查。")
BODY_SAMR = "2025年3月5日，国家市场监管总局公布抽检结果：全部合格。"
#: 长正文（超长引文截断用例用）
BODY_LONG = "2026年11月20日，某市发布自来水水质通报：" + "水质检测结果合格，请市民放心。" * 40

#: 默认抽取结果（引文逐字来自上面的正文）
EVENT_NHC = {"date": "2026年11月20日", "event": "某市发布自来水水质通报",
             "quote": "2026年11月20日，某市发布自来水水质通报"}
EVENT_NHC2 = {"date": "2026年11月19日", "event": "疾控中心介入调查",
              "quote": "11月19日，当地疾控中心介入调查。"}
EVENT_SAMR = {"date": "2025年3月5日", "event": "国家市场监管总局公布抽检结果",
              "quote": "2025年3月5日，国家市场监管总局公布抽检结果"}
#: 合规补充说明（无 URL、无真假判定词、无日期）
NOTE = "以上事件均来自本次读取的页面，未做真假判断。"

#: 固定真假判定词（铁律 1）：任何出口一个都不许出现
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _page(url: str, title: str, text: str) -> dict:
    """transport.fetch 成功形状的最小替身。"""
    return {"status": "ok", "url": url, "final_url": url, "domain": host_of(url),
            "title": title, "text": text,
            "content_type": "text/html", "bytes": len(text.encode("utf-8")),
            "hops": [], "fetched_at": "2026-11-20T11:00:00+08:00"}


def _results(*rows) -> dict:
    return {"results": list(rows), "degraded": False, "error": ""}


def _denied(*_a, **_k) -> dict:
    """未授权检索（consent 门 fail-closed 的口径）。"""
    return {"results": [], "degraded": True, "error": "consent_denied: 外部检索未授权"}


def make_skills(extract=None, note=None, search=None, fetch=None, llm_fn=None, **kw):
    """按调用目标喂 LLM / 检索 / 取页（全离线 stub）。

    extract：user 文本里的匹配串 → 事件列表（缺省两个 stub 页面各一条）；
    note：补充说明 dict（缺省合规 note，传 {} 模拟"模型给了空对象"）。
    """
    table = extract if extract is not None else {"nhc.gov.cn": [EVENT_NHC],
                                                 "samr.gov.cn": [EVENT_SAMR]}
    note_obj = note if note is not None else {"note": NOTE}

    def _llm(system, user):
        # 按**契约尾部**区分阶段：SKILL.md 正文里也有"事件/引文"等词，只有代码
        # 追加在末尾的机器契约能可靠区分（抽取/补充说明各不同）。
        if '"note"' in system and system.rstrip().endswith("越界即丢弃，报告以代码模板为准。"):
            return (note_obj, "")
        if '"events"' in system and system.rstrip().endswith("不得新增 URL。"):
            for key, events in table.items():
                if key in user:
                    return ({"events": events}, "")
            return ({"events": []}, "")
        return ({}, "")

    search_fn = search if search is not None else (lambda q, k: _results(
        {"url": NHC_URL, "title": "某市自来水水质通报"},
        {"url": SAMR_URL, "title": "国家市场监管总局抽检通报"}))
    fetch_fn = fetch if fetch is not None else (lambda url, **k: _page(
        url, "某市自来水水质通报" if "nhc" in url else "国家市场监管总局抽检通报",
        BODY_NHC if "nhc" in url else BODY_SAMR))
    return TimelineSkills(search_fn=search_fn, fetch_fn=fetch_fn,
                          llm_fn=llm_fn or _llm, clock=CLOCK, **kw)


# ===========================================================================
# 1. 规则与常量（4 例）
# ===========================================================================
class TestRulesAndConstants(unittest.TestCase):
    def test_defaults_and_clamp(self):
        rules = load_rules({"max_events": 9999, "total_deadline_s": -1,
                            "max_quote_chars": "bad"})
        self.assertEqual(rules["max_events"], 100)          # 夹到安全区间上界
        self.assertEqual(rules["total_deadline_s"], 1.0)
        self.assertEqual(rules["max_quote_chars"], 300)     # 坏类型回退默认

    def test_missing_file_returns_defaults(self):
        with TemporaryDirectory() as tmp:
            rules = load_rules(Path(tmp) / "nope.json")
        self.assertEqual(rules, dict(S.DEFAULT_TIMELINE_RULES))

    def test_package_rules_json_matches_defaults(self):
        import json
        raw = json.loads(S._RULES_PATH.read_text(encoding="utf-8"))
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            self.assertEqual(S.DEFAULT_TIMELINE_RULES[key], value, key)

    def test_public_api_and_closed_statuses(self):
        from mylocalai.assistant.skills import timeline as pkg
        for name in ("TimelineSkills", "event_timeline", "render_report", "load_rules"):
            self.assertTrue(hasattr(pkg, name), name)
        self.assertEqual(set(S.STATUSES), {"completed", "partial",
                                           "needs_clarification", "failed",
                                           "cancelled", "budget_exhausted"})
        self.assertEqual(set(S.TASK_STATUSES), {"completed", "no_factual_claim",
                                                "needs_clarification"})


# ===========================================================================
# 2. 日期归一（8 例）
# ===========================================================================
class TestDateNormalization(unittest.TestCase):
    def setUp(self):
        self.as_of = S.parse_as_of("2026-11-23T12:00:00+08:00")

    def test_absolute_day_forms(self):
        for text in ("2026年11月20日", "2026-11-20", "2026/11/20", "2026.11.20"):
            with self.subTest(text=text):
                made = S.parse_date_expression(text, self.as_of)
                self.assertEqual(made["date"], "2026-11-20")
                self.assertEqual(made["precision"], "day")

    def test_absolute_month_and_year(self):
        for text in ("2026年11月", "2026-11"):
            with self.subTest(text=text):
                made = S.parse_date_expression(text, self.as_of)
                self.assertEqual(made["date"], "2026-11")
                self.assertEqual(made["precision"], "month")
        made = S.parse_date_expression("2026年", self.as_of)
        self.assertEqual((made["date"], made["precision"]), ("2026", "year"))

    def test_invalid_calendar_date_degrades_not_guessed(self):
        # 整段非法（无年可退）→ None
        self.assertIsNone(S.parse_date_expression("2026-13-45", self.as_of))
        # 日非法退到月、月非法退到年：只退到"仍可确定"的精度，不猜合法日期
        made = S.parse_date_expression("2026年2月30日", self.as_of)
        self.assertEqual((made["date"], made["precision"]), ("2026-02", "month"))
        made = S.parse_date_expression("2026年13月", self.as_of)
        self.assertEqual((made["date"], made["precision"]), ("2026", "year"))

    def test_relative_day_offsets(self):
        cases = (("昨天", "2026-11-22"), ("3天前", "2026-11-20"),
                 ("三天前", "2026-11-20"), ("今天", "2026-11-23"),
                 ("大前天", "2026-11-20"))
        for text, expected in cases:
            with self.subTest(text=text):
                made = S.parse_date_expression(text, self.as_of)
                self.assertEqual(made["date"], expected)
                self.assertEqual(made["precision"], "day")

    def test_relative_month_and_year(self):
        cases = (("上个月", "2026-10"), ("三个月前", "2026-08"), ("本周", "2026-11"),
                 ("上周", "2026-11"), ("去年", "2025"), ("3年前", "2023"))
        for text, expected in cases:
            with self.subTest(text=text):
                made = S.parse_date_expression(text, self.as_of)
                self.assertEqual(made["date"], expected)
        self.assertEqual(S.parse_date_expression("上个月", self.as_of)["precision"], "month")
        self.assertEqual(S.parse_date_expression("上周", self.as_of)["precision"], "month")
        self.assertEqual(S.parse_date_expression("去年", self.as_of)["precision"], "year")

    def test_unparseable_expression_returns_none(self):
        for text in ("最近", "日前", "不久前", "", "   ", "水质争议"):
            with self.subTest(text=text):
                self.assertIsNone(S.parse_date_expression(text, self.as_of))

    def test_relative_without_as_of_not_normalized(self):
        self.assertIsNone(S.parse_date_expression("昨天", None))
        self.assertIsNone(S.parse_date_expression("三个月前", None))
        # 绝对日期不依赖 as_of
        made = S.parse_date_expression("2026年11月", None)
        self.assertEqual((made["date"], made["precision"]), ("2026-11", "month"))

    def test_leftmost_and_absolute_priority(self):
        # 最左优先：句首的旧日期胜过后面的相对表达
        made = S.parse_date_expression("2025年1月2日发布通报，上周又发了一次", self.as_of)
        self.assertEqual((made["date"], made["precision"]), ("2025-01-02", "day"))
        # 绝对优先：'2026年前' 命中绝对年份，不掉进 'N 年前'
        made = S.parse_date_expression("2026年前", self.as_of)
        self.assertEqual((made["date"], made["precision"]), ("2026", "year"))


# ===========================================================================
# 3. 预检（2 例）
# ===========================================================================
class TestPrecheck(unittest.TestCase):
    def test_empty_query_clarifies_without_search(self):
        seen = {"search": 0}

        def _search(q, k):
            seen["search"] += 1
            return _results()

        out = make_skills(search=_search).run("")
        self.assertEqual(out["status"], "needs_clarification")
        self.assertEqual(out["task_status"], "needs_clarification")
        self.assertEqual(out["data"]["state"], "unclear")
        self.assertEqual(seen["search"], 0)
        self.assertTrue(any("需要用户补充" in g for g in out["gaps"]))

    def test_too_long_query_clarifies_without_search(self):
        seen = {"search": 0}

        def _search(q, k):
            seen["search"] += 1
            return _results()

        out = make_skills(search=_search).run("水" * 201)
        self.assertEqual(out["status"], "needs_clarification")
        self.assertEqual(seen["search"], 0)
        self.assertTrue(any(e["category"] == "input_too_long" for e in out["errors"]))


# ===========================================================================
# 4. 检索与读页（6 例）
# ===========================================================================
class TestSearchAndRead(unittest.TestCase):
    def test_query_stripped_of_operators_and_pii(self):
        seen = {"q": []}

        def _search(q, k):
            seen["q"].append(q)
            return _results()

        make_skills(search=_search).run(TOPIC)
        self.assertTrue(seen["q"])
        joined = " ".join(seen["q"])
        self.assertNotIn("site:", joined)
        self.assertNotIn("@", joined)
        self.assertNotIn("13812345678", joined)

    def test_unauthorized_search_fails_without_fetch(self):
        seen = {"fetch": 0}

        def _fetch(url, **kw):
            seen["fetch"] += 1
            return _page(url, "t", BODY_NHC)

        out = make_skills(search=_denied, fetch=_fetch).run(TOPIC)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["data"]["state"], "search_unavailable")
        self.assertEqual(seen["fetch"], 0)          # fail-closed：一次读页都不发起
        self.assertEqual(out["data"]["events"], [])
        self.assertTrue(any("检索不可用" in g for g in out["gaps"]))

    def test_all_pages_unreachable_is_partial(self):
        def _fetch(url, **kw):
            return {"status": "error", "category": "timeout", "detail": "超时",
                    "url": url, "final_url": ""}

        out = make_skills(fetch=_fetch).run(TOPIC)
        self.assertEqual(out["status"], "partial")     # 有错误，但不是 failed
        self.assertEqual(out["data"]["state"], "page_unavailable")
        self.assertEqual(out["data"]["events"], [])
        self.assertTrue(any(e["category"] == "timeout" for e in out["errors"]))

    def test_page_budget_cap(self):
        seen = {"urls": []}

        def _fetch(url, **kw):
            seen["urls"].append(url)
            return _page(url, "某市自来水水质通报", BODY_NHC)

        out = make_skills(fetch=_fetch, rules={"max_pages": 2},
                          search=lambda q, k: _results(
                              {"url": NHC_URL, "title": "甲"},
                              {"url": SAMR_URL, "title": "乙"},
                              {"url": CDC_URL, "title": "丙"})).run(TOPIC)
        self.assertLessEqual(len(seen["urls"]), 2)
        self.assertEqual(out["budget"]["used"]["max_pages"], len(seen["urls"]))
        self.assertEqual(out["status"], "budget_exhausted")
        self.assertTrue(any("已保留" in g for g in out["gaps"]))

    def test_expired_deadline_stops_all_io(self):
        import time as _time
        seen = {"search": 0, "fetch": 0}

        def _search(q, k):
            seen["search"] += 1
            return _results()

        def _fetch(url, **kw):
            seen["fetch"] += 1
            return _page(url, "t", BODY_NHC)

        out = make_skills(search=_search, fetch=_fetch).run(
            TOPIC, deadline=_time.monotonic() - 1)
        self.assertEqual(out["status"], "budget_exhausted")
        self.assertEqual(seen["search"], 0)
        self.assertEqual(seen["fetch"], 0)

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


# ===========================================================================
# 5. 事件抽取（11 例）
# ===========================================================================
class TestEventExtraction(unittest.TestCase):
    def test_anchored_event_shape(self):
        out = make_skills().run(TOPIC)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["errors"], [])
        events = out["data"]["events"]
        self.assertEqual(len(events), 2)
        first = events[-1]                      # 按时间排序：2026-11-20 在后
        self.assertEqual(tuple(sorted(first)), tuple(sorted(S._EVENT_KEYS)))
        self.assertEqual(first["date"], "2026-11-20")
        self.assertEqual(first["precision"], "day")
        self.assertEqual(first["event"], "某市发布自来水水质通报")
        self.assertEqual(first["quote"], EVENT_NHC["quote"])
        self.assertEqual(first["source_url"], NHC_URL)
        self.assertEqual(first["source_title"], "某市自来水水质通报")
        self.assertEqual(first["evidence_id"], "E2")     # 序号按排序后顺序分配
        self.assertEqual(out["data"]["counts"]["events"], 2)
        self.assertEqual(out["data"]["counts"]["dropped"], 0)

    def test_quote_not_anchored_dropped(self):
        extract = {"nhc.gov.cn": [{"date": "2026年11月20日",
                                   "event": "某市发布自来水水质通报",
                                   "quote": "正文里根本没有的一句话"}]}
        out = make_skills(extract=extract).run(TOPIC)
        self.assertEqual(out["data"]["events"], [])
        self.assertTrue(any(e["category"] == "quote_not_anchored"
                            for e in out["errors"]))
        self.assertTrue(any("逐字定位" in x for x in out["data"]["limits"]))
        self.assertEqual(out["data"]["counts"]["dropped"], 1)
        self.assertEqual(out["status"], "partial")

    def test_missing_quote_dropped(self):
        extract = {"nhc.gov.cn": [{"date": "2026年11月20日",
                                   "event": "某市发布自来水水质通报", "quote": ""}]}
        out = make_skills(extract=extract).run(TOPIC)
        self.assertTrue(any(e["category"] == "quote_empty" for e in out["errors"]))
        self.assertTrue(any("逐字锚定引文" in x for x in out["data"]["limits"]))

    def test_over_long_quote_truncated_when_still_anchored(self):
        long_quote = BODY_LONG[:400]
        extract = {"nhc.gov.cn": [{"date": "2026年11月20日",
                                   "event": "某市发布自来水水质通报",
                                   "quote": long_quote}]}
        out = make_skills(extract=extract,
                          fetch=lambda url, **kw: _page(
                              url, "某市自来水水质通报", BODY_LONG)).run(TOPIC)
        kept = [e for e in out["data"]["events"] if e["source_url"] == NHC_URL]
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(kept[0]["quote"]), 300)      # 截到 max_quote_chars

    def test_over_long_quote_dropped_when_truncation_unanchorable(self):
        long_quote = "通报全文如下：" + "与正文无关的文字。" * 80
        extract = {"nhc.gov.cn": [{"date": "2026年11月20日",
                                   "event": "某市发布自来水水质通报",
                                   "quote": long_quote}]}
        out = make_skills(extract=extract).run(TOPIC)
        self.assertTrue(any(e["category"] == "quote_too_long" for e in out["errors"]))
        self.assertEqual([e for e in out["data"]["events"]
                          if e["source_url"] == NHC_URL], [])

    def test_verdict_word_or_url_in_event_dropped(self):
        cases = ("由此可见该说法是真的", "详见 https://evil.example")
        for text in cases:
            with self.subTest(event=text):
                extract = {"nhc.gov.cn": [{"date": "2026年11月20日",
                                           "event": text,
                                           "quote": EVENT_NHC["quote"]}]}
                out = make_skills(extract=extract).run(TOPIC)
                self.assertTrue(any(e["category"] == "event_unsafe_text"
                                    for e in out["errors"]))
                self.assertEqual(out["data"]["events"], [])

    def test_undated_event_isolated_from_main_line(self):
        extract = {"nhc.gov.cn": [{"date": "", "event": "当地疾控中心介入调查",
                                   "quote": "11月19日，当地疾控中心介入调查。"}],
                   "samr.gov.cn": [EVENT_SAMR]}
        out = make_skills(extract=extract).run(TOPIC)
        data = out["data"]
        self.assertEqual([e["source_url"] for e in data["events"]], [SAMR_URL])
        self.assertEqual(len(data["undated"]), 1)      # 无日期事件不混主线
        item = data["undated"][0]
        self.assertEqual(tuple(sorted(item)), tuple(sorted(S._UNDATED_KEYS)))
        self.assertEqual(item["event"], "当地疾控中心介入调查")
        self.assertEqual(item["quote"], "11月19日，当地疾控中心介入调查。")
        self.assertEqual(item["source_url"], NHC_URL)
        self.assertEqual(item["source_title"], "某市自来水水质通报")
        self.assertEqual(data["range"], {"earliest": "2025-03-05",
                                         "latest": "2025-03-05"})

    def test_date_falls_back_to_quote_scan(self):
        extract = {"nhc.gov.cn": [{"date": "", "event": "某市发布自来水水质通报",
                                   "quote": EVENT_NHC["quote"]}]}
        out = make_skills(extract=extract).run(TOPIC)
        kept = [e for e in out["data"]["events"] if e["source_url"] == NHC_URL]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["date"], "2026-11-20")     # 代码从引文里抽到的
        self.assertEqual(kept[0]["precision"], "day")

    def test_relative_date_normalized_with_as_of(self):
        extract = {"nhc.gov.cn": [{"date": "昨天", "event": "当地疾控中心介入调查",
                                   "quote": "11月19日，当地疾控中心介入调查。"}]}
        out = make_skills(extract=extract).run(TOPIC)
        kept = [e for e in out["data"]["events"] if e["source_url"] == NHC_URL]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["date"], "2026-11-22")     # as_of - 1 天
        self.assertEqual(kept[0]["precision"], "day")

    def test_unparsable_date_field_lands_in_undated(self):
        extract = {"nhc.gov.cn": [{"date": "最近几天", "event": "水质检测结果合格",
                                   "quote": "水质检测结果合格，请市民放心。"}]}
        out = make_skills(extract=extract).run(TOPIC)
        self.assertTrue(any("无法归一" in x for x in out["data"]["limits"]))
        self.assertEqual([e for e in out["data"]["events"]
                          if e["source_url"] == NHC_URL], [])
        self.assertTrue(any(u["event"] == "水质检测结果合格"
                            for u in out["data"]["undated"]))

    def test_llm_missing_degrades_to_partial_without_reading(self):
        seen = {"fetch": 0}

        def _fetch(url, **kw):
            seen["fetch"] += 1
            return _page(url, "t", BODY_NHC)

        skills = TimelineSkills(search_fn=lambda q, k: _results(
            {"url": NHC_URL, "title": "某市自来水水质通报"}),
            fetch_fn=_fetch, llm_fn=None, clock=CLOCK)
        out = skills.run(TOPIC)
        self.assertEqual(out["data"]["state"], "extract_unavailable")
        self.assertEqual(out["data"]["events"], [])
        self.assertEqual(seen["fetch"], 0)                  # 没有模型就不读页
        self.assertEqual(out["status"], "partial")
        self.assertTrue(any("未注入事件描述模型" in g for g in out["gaps"]))


# ===========================================================================
# 6. 去重、排序与上限（4 例）
# ===========================================================================
class TestDedupeSortAndCaps(unittest.TestCase):
    def test_same_body_from_two_domains_merged(self):
        def _fetch(url, **kw):
            return _page(url, "同一篇通报的转载", BODY_NHC)

        out = make_skills(extract={"nhc.gov.cn": [EVENT_NHC],
                                   "chinacdc.cn": [EVENT_NHC]},
                          search=lambda q, k: _results(
                              {"url": NHC_URL, "title": "同一篇通报的转载"},
                              {"url": CDC_URL, "title": "同一篇通报的转载"}),
                          fetch=_fetch).run(TOPIC)
        kept = [e for e in out["data"]["events"] if e["source_url"] == NHC_URL]
        merged = [e for e in out["data"]["events"] if e["source_url"] == CDC_URL]
        self.assertEqual(len(kept), 1)
        self.assertEqual(merged, [])                        # 同文副本只留一条
        self.assertTrue(any("同源转载" in x for x in out["data"]["limits"]))
        self.assertEqual(out["data"]["counts"]["dropped"], 1)

    def test_same_domain_similar_title_merged(self):
        # 两个同域候选：标题近似（≥0.9）但引文不同 → 按"同源转载"合并
        extract = {"转载页面二": [EVENT_NHC2], "转载页面": [EVENT_NHC]}

        def _fetch(url, **kw):
            title = ("某市自来水水质通报全文转载页面二" if url.endswith("2")
                     else "某市自来水水质通报全文转载页面")
            return _page(url, title, BODY_NHC)

        out = make_skills(extract=extract,
                          search=lambda q, k: _results(
                              {"url": NHC_URL, "title": "某市自来水水质通报全文转载页面"},
                              {"url": NHC_URL2, "title": "某市自来水水质通报全文转载页面二"}),
                          fetch=_fetch).run(TOPIC)
        kept = [e for e in out["data"]["events"] if e["source_url"] == NHC_URL]
        kept2 = [e for e in out["data"]["events"] if e["source_url"] == NHC_URL2]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept2, [])                         # 同域+标题近似 → 合并
        self.assertTrue(any("同源转载" in x for x in out["data"]["limits"]))
        self.assertEqual(out["data"]["counts"]["dropped"], 1)

    def test_events_sorted_by_date_and_range(self):
        out = make_skills().run(TOPIC)
        dates = [e["date"] for e in out["data"]["events"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(dates, ["2025-03-05", "2026-11-20"])
        self.assertEqual(out["data"]["range"],
                         {"earliest": "2025-03-05", "latest": "2026-11-20"})
        self.assertEqual([e["evidence_id"] for e in out["data"]["events"]],
                         ["E1", "E2"])                      # 序号按排序后顺序

    def test_max_events_cap_drops_later_events(self):
        table = {"nhc.gov.cn": [EVENT_NHC, EVENT_NHC2,
                                {"date": "2026年11月18日", "event": "居民反映异味",
                                 "quote": "当地疾控中心介入调查。"}],
                 "samr.gov.cn": [EVENT_SAMR]}
        out = make_skills(extract=table, rules={"max_events": 2}).run(TOPIC)
        self.assertEqual(len(out["data"]["events"]), 2)
        self.assertEqual([e["date"] for e in out["data"]["events"]],
                         ["2025-03-05", "2026-11-18"])      # 最早的 2 条
        self.assertEqual(out["data"]["counts"]["dropped"], 2)
        self.assertTrue(any("上限" in x for x in out["data"]["limits"]))


# ===========================================================================
# 7. 状态与报告（6 例）
# ===========================================================================
class TestStatusAndReport(unittest.TestCase):
    def test_search_ok_zero_results_is_completed(self):
        out = make_skills(search=lambda q, k: _results()).run(TOPIC)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["data"]["events"], [])
        self.assertTrue(any("候选来源" in g for g in out["gaps"]))

    def test_cancel_keeps_extracted_events(self):
        state = {"llm": 0}

        def _llm(system, user):
            state["llm"] += 1
            if '"note"' in system and system.rstrip().endswith("越界即丢弃，报告以代码模板为准。"):
                return ({"note": NOTE}, "")
            if '"events"' in system and system.rstrip().endswith("不得新增 URL。"):
                return ({"events": [EVENT_NHC]}, "")
            return ({}, "")

        skills = TimelineSkills(search_fn=lambda q, k: _results(
            {"url": NHC_URL, "title": "某市自来水水质通报"},
            {"url": SAMR_URL, "title": "国家市场监管总局抽检通报"}),
            fetch_fn=lambda url, **k: _page(url, "t", BODY_NHC if "nhc" in url
                                            else BODY_SAMR),
            llm_fn=_llm, clock=CLOCK)
        # 抽完第一页就取消：第二个候选不再读、不再抽
        out = skills.run(TOPIC, cancel_check=lambda: state["llm"] >= 1)
        self.assertEqual(out["status"], "cancelled")
        self.assertTrue(out["data"]["events"], "取消把已取得的事件丢了")
        self.assertTrue(any("已保留" in g for g in out["gaps"]))

    def test_budget_exhausted_keeps_events(self):
        out = make_skills(rules={"max_llm_calls": 1}).run(TOPIC)
        self.assertEqual(out["status"], "budget_exhausted")
        self.assertTrue(out["data"]["events"], "预算到顶把已取得的事件丢了")
        self.assertTrue(any("已保留" in g for g in out["gaps"]))

    def test_data_keys_locked(self):
        out = make_skills().run(TOPIC)
        data = out["data"]
        self.assertEqual(tuple(sorted(data)), tuple(sorted(S._DATA_KEYS)))
        for event in data["events"]:
            self.assertEqual(tuple(sorted(event)), tuple(sorted(S._EVENT_KEYS)))
        for item in data["undated"]:
            self.assertEqual(tuple(sorted(item)), tuple(sorted(S._UNDATED_KEYS)))
        self.assertEqual(tuple(sorted(data["counts"])), tuple(sorted(S._COUNT_KEYS)))
        self.assertEqual(tuple(sorted(data["range"])), ("earliest", "latest"))

    def test_report_verbatim_counts_and_no_verdict_words(self):
        out = make_skills().run(TOPIC)
        report = render_report(out)
        self.assertIn(EVENT_NHC["quote"], report)            # 引文逐字
        self.assertIn(EVENT_SAMR["quote"], report)
        self.assertIn("次搜索", report)
        self.assertIn("次读页", report)
        self.assertIn("次模型调用", report)
        self.assertIn("不代表全网", report)                   # 检索范围如实标注
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, report)
        self.assertNotIn("](", report)                        # 无 markdown 链接语法
        self.assertNotIn("<", report.replace("＜", ""))
        # 便捷函数同壳
        def _llm(system, user):
            if '"note"' in system:
                return ({"note": NOTE}, "")
            return ({"events": [EVENT_NHC]}, "")

        shell = event_timeline(TOPIC, search_fn=lambda q, k: _results(
            {"url": NHC_URL, "title": "某市自来水水质通报"}),
            fetch_fn=lambda url, **k: _page(url, "t", BODY_NHC),
            llm_fn=_llm, clock=CLOCK)
        self.assertEqual(shell["tool_id"], "event_timeline")
        self.assertEqual(len(shell["data"]["events"]), 1)

    def test_report_escapes_markup_in_source_title(self):
        """来源标题来自页面 <title>（攻击者可控），报告必须转义。"""
        dirty = _page(NHC_URL, "<b>通报</b> [x](https://evil.example)", BODY_NHC)
        out = make_skills(fetch=lambda url, **kw: dirty).run(TOPIC)
        report = render_report(out)
        self.assertNotIn("<b>", report)
        self.assertNotIn("](", report)
        self.assertIn("＜b＞", report)


# ===========================================================================
# 8. 受约束补充说明（2 例）
# ===========================================================================
class TestConstrainedNote(unittest.TestCase):
    def test_compliant_note_appended_to_report(self):
        out = make_skills().run(TOPIC)
        self.assertIn("补充说明", out["report"])
        self.assertIn(NOTE, out["report"])
        # 2 次抽取（两页各一次）+ 1 次补充说明
        self.assertEqual(out["audit"]["llm_calls"], 3)
        self.assertEqual(out["data"]["counts"]["llm_calls"], 3)
        self.assertFalse(any("补充说明" in x for x in out["data"]["limits"]))

    def test_out_of_bound_note_dropped(self):
        cases = ("详见 https://evil.example", "由此可知该说法是真的",
                 "2027年1月1日还将发布新通报")
        for note in cases:
            with self.subTest(note=note):
                out = make_skills(note={"note": note}).run(TOPIC)
                # 越界 note 一个字符都不进报告；报告的限制区写明丢弃原因
                self.assertNotIn(note, out["report"])
                self.assertNotIn("补充说明（受约束", out["report"])
                self.assertTrue(any("补充说明" in x for x in out["data"]["limits"]))
                self.assertIn("模型补充说明含 URL、真假判定词或新增日期，已丢弃",
                              out["report"])
                # 主结论不受影响：两条事件仍在
                self.assertEqual(len(out["data"]["events"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
