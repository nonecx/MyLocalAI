# -*- coding: utf-8 -*-
"""统计说法核查流水线测试（skills/statistics/pipeline）：mock 检索/取页，全离线。

分组：
    1. 数字抽取与归一（7）：百分比/百分点/倍数/翻番/金额进率/人数千分位/年份 +
       约数表述（约·超过·不足·以上·左右）；
    2. 结构化与坏形状丢弃（6）：合规结构化逐字入 data、摘录未锚定丢弃、模型编数字
       丢弃、单位不符丢弃、空 claims 退回代码、无模型走代码抽取；
    3. 口径判定矩阵（10）：一致（精确/精度/千分位/进率/约数/下限）、不一致
       （同单位异值/进率不同值）、口径不一致（同值异单位/异值异单位）、未核实到
       （无比对数字/跨口径族/无页可读）；
    4. 段落定位与锚定（3）：未锚定丢弃、数字只从已锚定段落抽、超长引文截断后定位；
    5. 检索/预算/取消（5）：未授权零外发、未注入检索零外发、页数预算到顶保留、
       死线已过零外发、取消保留形状；
    6. 零主张与降级出口（3）：无数字表达 no_factual_claim、空输入、超长输入；
    7. 纪律锁定（5）：data/claims/counts 键名锁定、不建档（零 dossier 依赖）、
       任何出口无真假判定词；
    8. 汇总/报告/便捷函数/进度/规则（6）：模板摘要、受约束补充说明合规/越界丢弃、
       永不抛同壳、六段中文进度、load_rules 夹取与兜底、报告渲染。

全离线：search_fn / fetch_fn / llm_fn / clock 全部注入假件，零网络零 LLM 零档案。
"""

from __future__ import annotations

import inspect
import json
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills.statistics import (  # noqa: E402
    StatisticsSkills,
    check_statistics,
)
from mylocalai.assistant.skills.statistics import pipeline as T  # noqa: E402

CLOCK = "2026-11-23T00:00:00+00:00"

PAGE_A = "https://stats.example.gov.cn/tongji-2023.html"
PAGE_B = "https://data.example.org.cn/yearbook.html"

#: 固定真假判定词（铁律 1）：任何出口一个都不许出现
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")


def _page(url: str, title: str, text: str) -> Dict[str, Any]:
    """transport.fetch 成功形状的最小替身。"""
    host = (urlsplit(url).hostname or "").lower()
    return {"status": "ok", "url": url, "final_url": url, "domain": host,
            "title": title, "text": text, "bytes": len(text.encode("utf-8")),
            "content_type": "text/html", "hops": [], "fetched_at": CLOCK}


class _FakeLLM:
    """按 user 提示内容分流的假模型：结构化 / 段落定位 / 补充说明。"""

    def __init__(self, *, claims: Any = None, quote: Any = None,
                 note: Any = None, raises: Any = None) -> None:
        self.claims = claims      # None → 返回 {"claims": []}（触发代码兜底）
        self.quote = quote        # None → 返回 {}（无可定位段落）
        self.note = note          # None → 返回 {}（无补充说明）
        self.raises = raises
        self.calls: List[str] = []

    def __call__(self, system: str, user: str) -> Any:
        self.calls.append(user)
        if self.raises is not None:
            raise self.raises
        if "用户原文" in user:
            return {"claims": self.claims if self.claims is not None else []}
        if "已读正文" in user:
            return {"quote": self.quote} if self.quote is not None else {}
        if "代码判定" in user:
            return {"note": self.note} if self.note is not None else {}
        return {}


class _Env:
    """一次核查的假环境：检索/取页 stub，记录调用，全离线。"""

    def __init__(self, *, pages: Optional[Dict[str, Dict[str, str]]] = None,
                 fail_urls: Any = (), search_degraded: bool = False,
                 search_error: str = "", search_raises: Any = None,
                 fetch_raises: Any = None) -> None:
        self.pages = pages or {}
        self.fail_urls = set(fail_urls)
        self.search_degraded = search_degraded
        self.search_error = search_error
        self.search_raises = search_raises
        self.fetch_raises = fetch_raises
        self.searches: List[Dict[str, Any]] = []
        self.fetches: List[Dict[str, Any]] = []

    def search(self, query: str, k: int) -> Dict[str, Any]:
        self.searches.append({"query": query, "k": k})
        if self.search_raises is not None:
            raise self.search_raises
        if self.search_degraded:
            return {"results": [], "degraded": True,
                    "error": self.search_error or "consent_denied: 外部检索未授权"}
        rows = [{"url": url, "title": row.get("title", ""), "snippet": ""}
                for url, row in self.pages.items()]
        return {"results": rows, "degraded": False, "error": ""}

    def fetch(self, url: str, *, allowed_domains=None, cancel_check=None,
              deadline=None) -> Dict[str, Any]:
        self.fetches.append({"url": url, "allowed_domains": allowed_domains})
        if self.fetch_raises is not None:
            raise self.fetch_raises
        row = self.pages.get(url)
        if url in self.fail_urls or row is None:
            return {"status": "error", "category": "network", "detail": "404 Not Found",
                    "url": url, "final_url": ""}
        return _page(url, row.get("title", ""), row.get("text", ""))

    def skills(self, *, search: bool = True, fetch: bool = True,
               llm: Any = None, **kw) -> StatisticsSkills:
        """装配流水线；search/fetch 传 False 表示不注入（零外发场景）。"""
        if search:
            kw.setdefault("search_fn", self.search)
        if fetch:
            kw.setdefault("fetch_fn", self.fetch)
        if llm is not None:
            kw["llm_fn"] = llm
        return StatisticsSkills(clock=lambda: CLOCK, **kw)


# ===========================================================================
# 1. 数字抽取与归一（7）
# ===========================================================================
class TestNumberExtraction(unittest.TestCase):
    def test_percent_extraction(self):
        found = T._extract_numbers("2023 年本地失业率平均为 5.2%")
        percent = [n for n in found if n["kind"] == "percent"]
        self.assertEqual(len(percent), 1)
        self.assertEqual(percent[0]["number"], 5.2)
        self.assertEqual(percent[0]["unit"], "%")
        self.assertEqual(percent[0]["norm"], 5.2)

    def test_percentage_point_extraction(self):
        found = T._extract_numbers("较上年下降 0.6 个百分点")
        self.assertEqual([n["kind"] for n in found], ["percentage_point"])
        self.assertEqual(found[0]["number"], 0.6)
        self.assertEqual(found[0]["unit"], "个百分点")

    def test_multiple_and_fan_extraction(self):
        found = T._extract_numbers("增长了 2 倍，产值翻了一番")
        self.assertEqual([n["number"] for n in found if n["kind"] == "multiple"],
                         [2.0, 2.0])
        self.assertTrue(all(n["unit"] == "倍" for n in found))

    def test_money_scale_normalization(self):
        found = T._extract_numbers("总额达 3.5 万亿元")
        self.assertEqual(found[0]["kind"], "money")
        self.assertEqual(found[0]["unit"], "万亿元")
        self.assertEqual(found[0]["norm"], 3.5e12)

    def test_thousands_separator_and_people_scale(self):
        found = T._extract_numbers("新增就业 1,234.5 万人")
        self.assertEqual(found[0]["kind"], "people")
        self.assertEqual(found[0]["unit"], "万人")
        self.assertEqual(found[0]["norm"], 1234.5 * 1e4)

    def test_year_extraction(self):
        found = T._extract_numbers("数据发布于 2023 年")
        self.assertEqual([(n["kind"], n["number"]) for n in found], [("year", 2023.0)])

    def test_hedge_detection(self):
        cases = (("失业率约为 5.2%", "about"), ("失业率超过 5%", "ge"),
                 ("失业率 5% 以上", "ge"), ("失业率不足 5%", "le"),
                 ("失业率 5% 左右", "about"), ("失业率为 5.2%", ""),
                 ("较近五年平均 3%", ""))
        for text, expected in cases:
            with self.subTest(text=text):
                found = T._extract_numbers(text)
                self.assertEqual(found[0]["hedge"], expected)


# ===========================================================================
# 2. 结构化与坏形状丢弃（6）
# ===========================================================================
TEXT_SINGLE = "2023 年本地失业率为 5.2%"


class TestClaimStructuring(unittest.TestCase):
    def test_valid_llm_structuring_enters_data_verbatim(self):
        llm = _FakeLLM(claims=[{"excerpt": "失业率为 5.2%", "number": "5.2",
                                "unit": "%", "metric": "城镇调查失业率",
                                "qualifiers": {"time": "2023 年", "place": "本地",
                                               "entity": "", "base": "全国"}}])
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "2023 年本地失业率平均为 5.2%。"}})
        result = env.skills(llm=llm).run(TEXT_SINGLE)
        claims = result["data"]["claims"]
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim["excerpt"], "失业率为 5.2%")
        self.assertEqual(claim["number"], 5.2)
        self.assertEqual(claim["unit"], "%")
        self.assertEqual(claim["metric"], "城镇调查失业率")
        self.assertEqual(claim["qualifiers"],
                         {"time": "2023 年", "place": "本地", "entity": "", "base": "全国"})
        # 结构化 1 次 + 段落定位 1 次 + 补充说明 1 次（llm 注入即尝试）
        self.assertEqual(result["audit"]["llm_calls"], 3)

    def test_excerpt_not_anchored_is_dropped(self):
        llm = _FakeLLM(claims=[{"excerpt": "失业率大概 9.9%", "number": "9.9",
                                "unit": "%", "metric": "", "qualifiers": {}}],
                       quote="2023 年本地失业率平均为 5.2%")
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "2023 年本地失业率平均为 5.2%。"}})
        result = env.skills(llm=llm).run(TEXT_SINGLE)
        self.assertTrue(any(e["category"] == "claim_not_anchored"
                            for e in result["errors"]))
        # 结构化结果被丢弃 → 退回代码抽取结果（数字仍来自原文）
        claims = result["data"]["claims"]
        self.assertEqual([c["number"] for c in claims], [2023.0, 5.2])
        self.assertTrue(any("代码抽取" in x for x in result["data"]["limits"]))

    def test_invented_number_is_dropped(self):
        llm = _FakeLLM(claims=[{"excerpt": "失业率为 5.2%", "number": "9.9",
                                "unit": "%", "metric": "", "qualifiers": {}}],
                       quote="2023 年本地失业率平均为 5.2%")
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "2023 年本地失业率平均为 5.2%。"}})
        result = env.skills(llm=llm).run(TEXT_SINGLE)
        self.assertTrue(any(e["category"] == "number_not_from_code"
                            for e in result["errors"]))

    def test_unit_mismatch_is_dropped(self):
        llm = _FakeLLM(claims=[{"excerpt": "失业率为 5.2%", "number": "5.2",
                                "unit": "个百分点", "metric": "", "qualifiers": {}}],
                       quote="2023 年本地失业率平均为 5.2%")
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "2023 年本地失业率平均为 5.2%。"}})
        result = env.skills(llm=llm).run(TEXT_SINGLE)
        self.assertTrue(any(e["category"] == "unit_mismatch"
                            for e in result["errors"]))

    def test_empty_claims_falls_back_to_code_extraction(self):
        llm = _FakeLLM(claims=[], quote="2023 年本地失业率平均为 5.2%")
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "2023 年本地失业率平均为 5.2%。"}})
        result = env.skills(llm=llm).run(TEXT_SINGLE)
        self.assertEqual([c["number"] for c in result["data"]["claims"]], [2023.0, 5.2])
        self.assertTrue(any("全部未通过代码校验" in x
                            for x in result["data"]["limits"]))

    def test_no_llm_uses_code_candidates(self):
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "2023 年本地失业率平均为 5.2%。"}})
        result = env.skills().run(TEXT_SINGLE)
        claims = result["data"]["claims"]
        self.assertEqual([c["number"] for c in claims], [2023.0, 5.2])
        # 代码兜底：无指标、无限定语；判语仍走完整检索比对
        self.assertEqual(claims[0]["metric"], "")
        self.assertEqual(claims[0]["qualifiers"],
                         {"time": "", "place": "", "entity": "", "base": ""})
        self.assertEqual(result["audit"]["llm_calls"], 0)


# ===========================================================================
# 3. 口径判定矩阵（10）
# ===========================================================================
class TestVerdictMatrix(unittest.TestCase):
    def _run(self, text: str, page_text: str, *, llm: Any = None,
             pages: Optional[Dict[str, Dict[str, str]]] = None, **kw):
        env = _Env(pages=pages or {PAGE_A: {"title": "公报", "text": page_text}})
        return env.skills(llm=llm, **kw).run(text), env

    def test_consistent_exact(self):
        result, _ = self._run("失业率为 5.2%",
                              "2023 年本地失业率平均为 5.2%。")
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "consistent")
        self.assertEqual(claim["source_number"], "5.2%")
        self.assertIn("一致", claim["basis"])

    def test_consistent_after_precision_normalization(self):
        result, _ = self._run("失业率为 5.2%",
                              "2023 年本地失业率平均为 5.20%。")
        self.assertEqual(result["data"]["claims"][0]["verdict"], "consistent")

    def test_consistent_after_thousands_separator(self):
        result, _ = self._run("全年新增就业 1,234.5 万人",
                              "全年新增就业 1234.5 万人，同比回升。")
        self.assertEqual(result["data"]["claims"][0]["verdict"], "consistent")

    def test_consistent_after_money_scale_conversion(self):
        result, _ = self._run("全年总额达 3.5 万亿元",
                              "全年总额约 35000 亿元，创历史新高。")
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "consistent")
        self.assertEqual(claim["source_number"], "35000亿元")

    def test_consistent_with_about_hedge_tolerance(self):
        result, _ = self._run("失业率约为 5.2%",
                              "2023 年本地失业率平均为 5.5%。")
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "consistent")
        self.assertIn("约数表述", claim["basis"])

    def test_consistent_with_lower_bound_hedge(self):
        result, _ = self._run("失业率超过 5%",
                              "2023 年本地失业率平均为 5.2%。")
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "consistent")
        self.assertIn("下限表述", claim["basis"])

    def test_inconsistent_same_unit_different_value(self):
        result, _ = self._run("失业率为 5.2%",
                              "2023 年本地失业率平均为 4.9%。")
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "inconsistent")
        self.assertEqual(claim["source_number"], "4.9%")

    def test_inconsistent_after_money_scale_conversion(self):
        result, _ = self._run("全年总额 5 亿元",
                              "全年总额 5000 万元。")
        self.assertEqual(result["data"]["claims"][0]["verdict"], "inconsistent")

    def test_inconsistent_with_lower_bound_hedge(self):
        result, _ = self._run("失业率超过 5%",
                              "2023 年本地失业率平均为 4.8%。")
        self.assertEqual(result["data"]["claims"][0]["verdict"], "inconsistent")

    def test_caliber_mismatch_same_value_different_unit(self):
        result, _ = self._run("青年失业率上升 5%",
                              "2023 年青年失业率上升 5 个百分点。")
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "caliber_mismatch")
        self.assertEqual(claim["source_number"], "5个百分点")
        self.assertIn("未与主张", claim["basis"])

    def test_caliber_mismatch_different_value_same_family(self):
        result, _ = self._run("青年失业率上升 5%",
                              "2023 年青年失业率上升 3 个百分点。")
        self.assertEqual(result["data"]["claims"][0]["verdict"], "caliber_mismatch")

    def test_unverifiable_when_quote_has_no_numbers(self):
        result, _ = self._run("失业率为 5.2%",
                              "本次公报未公布分年龄组的数据，详见附录。")
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "unverifiable")
        self.assertEqual(claim["source_number"], "")
        self.assertEqual(claim["quote"], "")

    def test_unverifiable_when_numbers_cross_family(self):
        llm = _FakeLLM(quote="2023 年辖区总人口 14 亿人")
        result, _ = self._run("失业率为 5.2%",
                              "2023 年辖区总人口 14 亿人，结构保持稳定。", llm=llm)
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "unverifiable")
        self.assertIn("不同口径族", claim["basis"])

    def test_unverifiable_when_no_page_read(self):
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "2023 年本地失业率平均为 5.2%。"}},
                   search_degraded=True)
        result = env.skills().run("失业率为 5.2%")
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "unverifiable")
        self.assertEqual(env.fetches, [])          # 未授权零外发


# ===========================================================================
# 4. 段落定位与锚定（3）
# ===========================================================================
class TestQuoteAnchoring(unittest.TestCase):
    def test_quote_not_anchored_is_dropped(self):
        llm = _FakeLLM(quote="失业率大概为 9.9%（正文里没有这句）")
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "2023 年本地失业率平均为 5.2%。"}})
        result = env.skills(llm=llm).run("失业率为 5.2%")
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "unverifiable")
        self.assertTrue(any(e["category"] == "quote_not_anchored"
                            for e in result["errors"]))

    def test_numbers_only_come_from_anchored_quote(self):
        """页面里另有 9.9%，但已锚定段落只有 5.2% → 判语只用段落内数字。"""
        llm = _FakeLLM(quote="2023 年本地失业率平均为 5.2%")
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "全区参考值为 9.9%。2023 年本地失业率平均为 5.2%。"}})
        result = env.skills(llm=llm).run("失业率为 5.2%")
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "consistent")
        self.assertEqual(claim["source_number"], "5.2%")

    def test_overlong_quote_truncated_when_still_locatable(self):
        long_tail = "补充说明" * 80
        body = f"2023 年本地失业率平均为 5.2%，{long_tail}。"
        self.assertGreater(len(body), 300)
        llm = _FakeLLM(quote=f"2023 年本地失业率平均为 5.2%，{long_tail}")
        env = _Env(pages={PAGE_A: {"title": "公报", "text": body}})
        result = env.skills(llm=llm).run("失业率为 5.2%")
        claim = result["data"]["claims"][0]
        self.assertEqual(len(claim["quote"]), 300)     # 截断到 max_quote_chars
        self.assertEqual(claim["verdict"], "consistent")


# ===========================================================================
# 5. 检索/预算/取消（5）
# ===========================================================================
TEXT_TWO = "失业率为 5.2%，全年新增就业 902 万人。"


class TestBudgetCancel(unittest.TestCase):
    def test_unauthorized_search_means_zero_external(self):
        """检索降级（未授权）→ 零取页、如实记 error，主张全部未核实到。"""
        env = _Env(pages={PAGE_A: {"title": "公报", "text": "失业率为 5.2%。"}},
                   search_degraded=True, search_error="consent_denied: 外部检索未授权")
        result = env.skills().run("失业率为 5.2%")
        self.assertEqual(env.fetches, [])
        self.assertEqual(env.searches[0]["query"], "失业率为 5.2%")
        self.assertTrue(any(e["category"] == "search_unavailable"
                            and "consent_denied" in e["detail"]
                            for e in result["errors"]))
        self.assertEqual(result["data"]["state"], "unreachable")
        self.assertEqual(result["data"]["claims"][0]["verdict"], "unverifiable")
        self.assertEqual(result["status"], "partial")

    def test_no_search_injection_means_zero_external(self):
        env = _Env(pages={PAGE_A: {"title": "公报", "text": "失业率为 5.2%。"}})
        result = env.skills(search=False).run("失业率为 5.2%")
        self.assertEqual(env.fetches, [])
        self.assertTrue(any(e["category"] == "search_unavailable"
                            for e in result["errors"]))
        self.assertEqual(result["data"]["state"], "unreachable")

    def test_page_budget_exhausted_preserves_first_claim(self):
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "2023 年本地失业率平均为 5.2%，新增就业 902 万人。"}})
        result = env.skills(rules={"max_pages": 1}).run(TEXT_TWO)
        verdicts = {c["claim_id"]: c["verdict"] for c in result["data"]["claims"]}
        self.assertEqual(len(env.fetches), 1)            # 只读 1 页就停
        self.assertIn("consistent", verdicts.values())   # 已取得的比对保留
        self.assertIn("unverifiable", verdicts.values())
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertIn("预算/死线", "\n".join(result["gaps"]))

    def test_expired_deadline_means_zero_external(self):
        env = _Env(pages={PAGE_A: {"title": "公报", "text": "失业率为 5.2%。"}})
        result = env.skills().run("失业率为 5.2%", deadline=time.monotonic() - 1.0)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(env.searches, [])
        self.assertEqual(env.fetches, [])
        self.assertEqual(result["data"]["claims"][0]["verdict"], "unverifiable")
        self.assertIn("预算/死线", "\n".join(result["gaps"]))

    def test_cancelled_run_preserves_shape(self):
        env = _Env(pages={PAGE_A: {"title": "公报", "text": "失业率为 5.2%。"}})
        result = env.skills().run("失业率为 5.2%", cancel_check=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(env.fetches, [])
        self.assertEqual(len(result["data"]["claims"]), 1)   # 主张形状保留
        self.assertEqual(result["data"]["claims"][0]["verdict"], "unverifiable")
        self.assertIn("取消", "\n".join(result["gaps"]))


# ===========================================================================
# 6. 零主张与降级出口（3）
# ===========================================================================
class TestEmptyExits(unittest.TestCase):
    def test_no_numeric_expression_is_no_factual_claim(self):
        env = _Env(pages={PAGE_A: {"title": "公报", "text": "失业率为 5.2%。"}})
        result = env.skills().run("今天天气不错，心情很好。")
        self.assertEqual(result["task_status"], "no_factual_claim")
        self.assertEqual(result["status"], "completed")   # 没有数字不是失败
        self.assertEqual(result["data"]["claims"], [])
        self.assertEqual(result["data"]["state"], "ok")
        self.assertEqual(result["data"]["counts"]["claims"], 0)
        self.assertEqual(env.searches, [])                 # 零外发
        self.assertEqual(env.fetches, [])
        self.assertIn("没有可核对的数字表达", result["report"])

    def test_empty_input_needs_clarification(self):
        env = _Env()
        result = env.skills().run("   ")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["task_status"], "needs_clarification")
        self.assertEqual(result["data"]["state"], "unclear")
        self.assertEqual(env.fetches, [])
        self.assertIn("需要用户补充", "\n".join(result["gaps"]))

    def test_too_long_input_needs_clarification(self):
        env = _Env()
        result = env.skills().run("数" * 4001)
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["data"]["state"], "unclear")


# ===========================================================================
# 7. 纪律锁定（5）
# ===========================================================================
class TestDiscipline(unittest.TestCase):
    def test_data_and_claim_keys_locked(self):
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "2023 年本地失业率平均为 5.2%。"}})
        result = env.skills().run(TEXT_SINGLE)
        self.assertEqual(tuple(sorted(result["data"])),
                         tuple(sorted(T._DATA_KEYS)))
        claim = result["data"]["claims"][0]
        self.assertEqual(tuple(sorted(claim)), tuple(sorted(T._CLAIM_KEYS)))
        self.assertEqual(tuple(sorted(claim["qualifiers"])),
                         tuple(sorted(T._QUALIFIER_KEYS)))
        self.assertEqual(tuple(sorted(result["data"]["counts"])),
                         tuple(sorted(T._COUNTS_KEYS)))

    def test_no_dossier_dependency_and_no_write_params(self):
        """不建档：模块没有任何 dossier/写库 import；构造参数里没有"写"接口。"""
        source = inspect.getsource(T)
        import_lines = [ln.strip() for ln in source.splitlines()
                        if ln.strip().startswith(("import ", "from "))]
        self.assertTrue(import_lines)
        for line in import_lines:
            self.assertNotIn("dossier", line.lower(), line)
        params = inspect.signature(StatisticsSkills.__init__).parameters
        self.assertNotIn("dossier", params)
        self.assertNotIn("write_fn", params)

    def test_no_verdict_words_in_any_exit(self):
        """任何出口（report/basis/gaps/limits）都不出现真假判定词。"""
        pages = {PAGE_A: {"title": "公报",
                          "text": "2023 年本地失业率平均为 5.2%，较上年下降 0.6 个百分点。"}}
        cases = [
            (_Env(pages=pages), TEXT_SINGLE),
            (_Env(pages=pages, search_degraded=True), TEXT_SINGLE),
            (_Env(), "今天天气不错。"),
            (_Env(), ""),
            (_Env(pages=pages), TEXT_SINGLE),
        ]
        for index, (env, query) in enumerate(cases):
            with self.subTest(case=index):
                result = env.skills().run(query)
                texts = [result["report"],
                         "\n".join(result["gaps"]),
                         "\n".join(result["data"]["limits"])]
                for claim in result["data"]["claims"]:
                    texts.extend([claim["basis"], claim["excerpt"], claim["quote"]])
                for text in texts:
                    for word in _VERDICT_WORDS:
                        self.assertNotIn(word, str(text))

    def test_unverifiable_never_phrased_as_false(self):
        """「没核实到」绝不说成「数字为假」：未核实场景的措辞锁定。"""
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "本次公报未公布分年龄组数据。"}})
        result = env.skills().run("失业率为 5.2%")
        claim = result["data"]["claims"][0]
        self.assertEqual(claim["verdict"], "unverifiable")
        self.assertNotIn("为假", claim["basis"])
        self.assertNotIn("为真", claim["basis"])
        self.assertIn("未定位到", claim["basis"])

    def test_zero_llm_path_is_fully_offline(self):
        """不注入任何模型：结构化与段落定位都走代码路径，判语照常产出。"""
        env = _Env(pages={PAGE_A: {"title": "公报",
                                   "text": "2023 年本地失业率平均为 4.9%。"}})
        result = env.skills().run("失业率为 5.2%")
        self.assertEqual(result["audit"]["llm_calls"], 0)
        self.assertEqual(result["data"]["claims"][0]["verdict"], "inconsistent")


# ===========================================================================
# 8. 汇总/报告/便捷函数/进度/规则（6）
# ===========================================================================
class TestSummaryAndShell(unittest.TestCase):
    PAGES = {PAGE_A: {"title": "2023 年统计公报",
                      "text": "2023 年本地失业率平均为 5.2%。"}}

    def test_summary_template_contains_verdicts_and_route(self):
        env = _Env(pages=self.PAGES)
        result = env.skills().run(TEXT_SINGLE)
        report = result["report"]
        self.assertIn("统计说法核查报告", report)
        self.assertIn("与来源一致", report)
        self.assertIn("不判内容真假", report)
        # 报告末尾导流核查流程
        self.assertIn("verify_claim", report)

    def test_compliant_note_appended_to_report(self):
        llm = _FakeLLM(quote="2023 年本地失业率平均为 5.2%",
                       note="以上仅比对数字口径，未涉及指标定义差异。")
        env = _Env(pages=self.PAGES)
        result = env.skills(llm=llm).run(TEXT_SINGLE)
        self.assertIn("以上仅比对数字口径", result["report"])
        # 结构化 1 次 + 两条主张各定位 1 次 + 补充说明 1 次 = 4，计数与 audit 一致
        self.assertEqual(result["data"]["counts"]["llm_calls"], 4)
        self.assertEqual(result["audit"]["llm_calls"], 4)

    def test_out_of_bound_notes_dropped(self):
        cases = (
            "所以这个数字是真的",            # 真假判定词
            "该数字为真",                    # 判定性表述
            "详情见 https://x.example",      # 新增 URL
            "补充" * 60,                     # 120 字，超过 100 字上限
        )
        for note in cases:
            with self.subTest(note=note[:20]):
                llm = _FakeLLM(quote="2023 年本地失业率平均为 5.2%", note=note)
                env = _Env(pages=self.PAGES)
                result = env.skills(llm=llm).run(TEXT_SINGLE)
                self.assertNotIn(note, result["report"])
                self.assertTrue(any("补充说明" in x
                                    for x in result["data"]["limits"]))

    def test_run_never_raises_and_convenience_function_same_shell(self):
        env = _Env(pages={PAGE_A: {"title": "公报", "text": "失业率为 5.2%。"}},
                   fetch_raises=RuntimeError("网络炸了"))
        result = env.skills().run(TEXT_SINGLE)
        self.assertEqual(result["status"], "partial")     # 取页异常折成降级，不外泄
        self.assertTrue(any(e["category"] == "fetch_exception"
                            for e in result["errors"]))
        self.assertEqual(result["tool_id"], "check_statistics")
        # 便捷函数同壳
        again = check_statistics(TEXT_SINGLE, fetch_fn=env.fetch,
                                 search_fn=env.search, clock=lambda: CLOCK)
        self.assertEqual(again["tool_id"], "check_statistics")

    def test_progress_emits_six_chinese_stages(self):
        env = _Env(pages=self.PAGES)
        seen: list = []
        env.skills(on_progress=lambda stage, done, total:
                   seen.append((stage, done, total))).run(TEXT_SINGLE)
        self.assertEqual([s[0] for s in seen], list(T._PROGRESS_STAGES))
        self.assertEqual([s[1] for s in seen], [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(s[2] == 6 for s in seen))

    def test_load_rules_clamping_and_fallbacks(self):
        rules = T.load_rules({"max_pages": 999, "max_llm_calls": 9999,
                              "total_deadline_s": 99999, "max_input_chars": "bad",
                              "unknown_key": 1})
        self.assertEqual(rules["max_pages"], 200)          # 夹到安全区间上界
        self.assertEqual(rules["max_llm_calls"], 500)
        self.assertEqual(rules["total_deadline_s"], 3600.0)
        self.assertEqual(rules["max_input_chars"], 4000)  # 坏类型回退默认
        self.assertNotIn("unknown_key", rules)             # 未知键丢弃
        missing = T.load_rules(Path("___no_such_rules__.json"))
        self.assertEqual(missing, dict(T.DEFAULT_STATISTICS_RULES))

    def test_package_rules_json_matches_defaults(self):
        raw = json.loads((Path(T._HERE) / "rules.json").read_text(encoding="utf-8"))
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            self.assertEqual(T.DEFAULT_STATISTICS_RULES[key], value, key)

    def test_render_report_is_deterministic_and_clean(self):
        env = _Env(pages=self.PAGES)
        result = env.skills().run(TEXT_SINGLE)
        report = result["report"]
        self.assertIn("统计说法核查报告", report)
        self.assertIn("与来源一致", report)
        self.assertIn("来源数字", report)
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, report)
        # 确定性：同一结果渲染两遍逐字相同
        self.assertEqual(T.render_report(result), report)
        # 坏形状不抛
        self.assertIn("形状不符", T.render_report("not-a-dict"))


if __name__ == "__main__":
    unittest.main()
