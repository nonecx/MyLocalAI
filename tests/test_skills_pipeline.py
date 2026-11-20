# -*- coding: utf-8 -*-
"""取证三技能流水线测试（契约 §3.10 / 总览 §10.3）：26 例。

锁定（抓真实缺陷的标准）：
   - 分类标签白名单与原文定位：模型自造标签、依据指不回原文，一律记 error 并丢弃；
   - 信息不足先追问：四态分类在检索之前就停，绝不为有输出而编；
   - 信源身份不可自封：陌生域名自称权威最多 unverified_candidate，进不了阶段3；
   - 同域名不产生第二个权威：镜像/子站不虚增覆盖；
   - 跨站转载去重：正文哈希 + 引文归一化合并成一条证据链；
   - 引文定位与关系枚举：引文指不回已读正文即丢弃，关系越界按 unclear 处理；
   - PII/范围操作符剥离：外发前脱敏，site: 只能由代码拼；
   - 预算耗尽/取消保留部分结果：绝不整批丢弃；
   - 报告转义与不出现真假判定：确定性纯文本，无 HTML/markdown 注入面；
   - 检索降级 ≠ 合法空结果：没搜/搜降级记 error+gap，搜过了没找到只记 gap。
全离线：llm_fn / search_fn / fetch_fn 全部注入假件，零网络零 LLM。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills.pipeline import (  # noqa: E402
    CLAIM_STATUSES,
    RELATIONS,
    STATUSES,
    TASK_STATUSES,
    EvidenceSkills,
    collect_evidence,
    load_rules,
    locate_quote,
    normalize_quote,
    normalize_text,
    render_report,
    strip_pii,
)

FETCHED_AT = "2026-11-20T05:00:00+00:00"
# 原文里预埋三段可逐字定位的主张，供拆分/超限/预算类用例复用
TEXT = ("网传某市自来水厂水质不达标，2026年11月有居民反映自来水有异味，请大家不要喝。"
        "另有说法称当地医院接诊量翻倍，还有人称供水公司已暂停供水。")
CLAIM_1 = "网传某市自来水厂水质不达标"
CLAIM_2 = "当地医院接诊量翻倍"
CLAIM_3 = "供水公司已暂停供水"
HOME_KEPU = "欢迎访问科普中国网站，本站为中国科学技术协会科普平台，发布权威科普与辟谣内容。"
NEWS_KEPU = "2026年11月水质监测数据显示该市出厂水符合标准，居民可正常使用自来水。"
# 报告渲染用的“脏”标题：带 HTML 与 markdown 链接载荷，用于锁定转义
DIRTY_TITLE = "通报 <script>alert(1)</script> [更多](javascript:alert(2))"

# 判定词清单：报告里一个都不许出现（铁律 1“不判真假，只述证据”）
VERDICT_WORDS = ("真的", "假的", "属实", "谣言为真", "证实", "证明", "确凿", "千真万确", "铁定")


def _domain(system, user):
    return {"state": "ok", "primary": "environment", "secondary": [], "region": "CN",
            "time": "2026年11月", "labels": ["水质"], "basis": ["自来水有异味"]}, ""


def _select(system, user):
    return {"selected": ["SRC-KEPUCHINA"]}, ""


def _notes(system, user):
    return {"notes": [{"source_id": "SRC-KEPUCHINA", "identity": "科普中国",
                       "scope": "科普与辟谣", "basis_quote": "本站为中国科学技术协会科普平台"}]}, ""


def _claims_one(system, user):
    return {"claims": [{"text": CLAIM_1, "claim_status": "checkable",
                        "qualifiers": {"place": "某市"}, "queries": []}]}, ""


def _quote(system, user):
    return {"quote": "水质监测数据显示该市出厂水符合标准", "relation": "refutes",
            "limitations": []}, ""


# 分派标记与契约字符串一一对应（pipeline 的 system prompt 里唯一）
HANDLERS = [
    ('"state":"', _domain),
    ('"selected":', _select),
    ('"notes":', _notes),
    ('"claims":[', _claims_one),
    ('"quote":', _quote),
]


class StubLLM:
    """按 system prompt 里的 JSON 契约标记分派的假模型：返回 (obj, err)，零网络零 LLM。"""

    def __init__(self, **overrides):
        self.overrides = overrides      # 标记 -> (obj, err) 或 callable(system, user)
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        for marker, handler in HANDLERS:
            if marker in system:
                value = self.overrides.get(marker, handler)
                return value(system, user) if callable(value) else value
        return None, "stub_no_handler"


def stub_search(mapping=None, degraded=None, degrade_prefix=None):
    """按查询前缀命中的假检索。

    degraded 非空时进入降级分支；degrade_prefix 限定只降级某类查询（用于把中性机构
    搜索与主张检索分开断言）。返回形状含 degraded / error，对齐生产侧检索适配器。
    """
    mapping = mapping or {}
    state = {"queries": []}

    def _search(query, k):
        state["queries"].append(query)
        if degraded is not None and (degrade_prefix is None
                                     or query.startswith(degrade_prefix)):
            return {"results": [], "degraded": True, "error": degraded}
        for prefix, results in mapping.items():
            if query.startswith(prefix):
                return {"results": results, "degraded": False}
        return {"results": [], "degraded": False}

    _search.state = state
    return _search


def ok_page(url, text, domain="kepuchina.cn", title="页面标题"):
    return {"status": "ok", "url": url, "final_url": url, "domain": domain,
            "title": title, "text": text, "bytes": len(text),
            "content_type": "text/html", "hops": [], "fetched_at": FETCHED_AT}


def stub_fetch(pages=None, error=None, record=None):
    """按 URL 子串命中的假取页；error 非空时所有取页都返回该错误 dict。

    键按长度降序匹配：否则 "https://kepuchina.cn/" 会先命中 "https://kepuchina.cn/news/1"，
    把新闻页当成首页返回（实测踩过）。
    """
    pages = pages or {}
    keys = sorted(pages, key=len, reverse=True)
    state = {"calls": []}

    def _fetch(url, **kw):
        state["calls"].append({"url": url, "kw": kw})
        if record is not None:
            record.append(url)
        if error is not None:
            out = dict(error)
            out.setdefault("url", url)
            out.setdefault("final_url", "")
            return out
        for key in keys:
            if key in url:
                value = pages[key]
                return ok_page(url, value["text"], value.get("domain", "kepuchina.cn"),
                               value.get("title", "页面标题"))
        return ok_page(url, HOME_KEPU)

    _fetch.state = state
    return _fetch


def default_pages():
    return {
        "https://kepuchina.cn/": {"text": HOME_KEPU, "domain": "kepuchina.cn"},
        "https://kepuchina.cn/news": {"text": NEWS_KEPU, "domain": "kepuchina.cn",
                                      "title": "水质通报"},
    }


def default_search():
    return stub_search({"site:kepuchina.cn": [{"url": "https://kepuchina.cn/news/1",
                                               "title": "水质通报"}]})


def run(text=TEXT, **kw):
    """一次完整运行：默认给足能走通的假件，kwargs 可整体替换某一层。"""
    cancel_check = kw.pop("cancel_check", None)
    deadline = kw.pop("deadline", None)
    skills = EvidenceSkills(llm_fn=kw.pop("llm", None) or StubLLM(**kw.pop("llm_overrides", {})),
                            search_fn=kw.pop("search", None) or default_search(),
                            fetch_fn=kw.pop("fetch", None) or stub_fetch(default_pages()),
                            clock=lambda: FETCHED_AT, **kw)
    return skills.run(text, cancel_check=cancel_check, deadline=deadline)


def categories(result, stage=None):
    return {e["category"] for e in result["errors"]
            if stage is None or e.get("stage") == stage}


class EvidencePipelineTests(unittest.TestCase):
    """26 例：按契约 §3.10 清单逐条覆盖，每个用例 docstring 写“锁定什么”。"""

    # 1. 形状与状态集合 -----------------------------------------------------
    def test_return_shell_shape_and_status_sets(self):
        """锁定返回外壳形状、六种状态/三态任务/关系与主张状态封闭集合、budget 与 limits 同键。"""
        result = run()
        self.assertEqual(set(result), {"tool_id", "status", "task_status", "data", "gaps",
                                       "errors", "audit", "budget", "report",
                                       "started_at", "finished_at"})
        self.assertEqual(result["tool_id"], "collect_evidence")
        self.assertIn(result["status"], STATUSES)
        self.assertIn(result["task_status"], TASK_STATUSES)
        self.assertEqual(set(result["data"]),
                         {"domain", "sources", "claims", "evidence", "chains"})
        self.assertEqual(set(result["data"]["domain"]),
                         {"primary", "secondary", "region", "time", "labels", "basis"})
        self.assertEqual(set(result["audit"]), {"operations", "searches", "pages", "llm_calls"})
        for op in result["audit"]["operations"]:
            self.assertIn(op["kind"], ("search", "fetch", "llm"))
            self.assertIn(op, [{"kind": op["kind"], "target": op["target"], "ok": op["ok"]}])
        self.assertEqual(set(result["budget"]), {"used", "limits"})
        self.assertEqual(set(result["budget"]["used"]), set(result["budget"]["limits"]))
        self.assertEqual(len(result["budget"]["limits"]), 16)
        for item in result["data"]["evidence"]:
            self.assertIn(item["relation"], RELATIONS)
            self.assertEqual(set(item), {"evidence_id", "claim_ids", "relation", "source_id",
                                         "url", "title", "quote", "quote_span", "published_at",
                                         "fetched_at", "limitations", "independence_group",
                                         "domain"})
        for item in result["data"]["claims"]:
            self.assertIn(item["claim_status"], CLAIM_STATUSES)
            self.assertEqual(set(item), {"claim_id", "text", "source_span", "qualifiers",
                                         "claim_status"})
        # 纯函数口径：归一化、引文归一、半开区间定位
        self.assertEqual(normalize_text("  a\t\nb  "), "a b")
        self.assertEqual(normalize_quote("“引文。”"), "引文")
        self.assertEqual(locate_quote("甲乙丙丁", "丙"), {"start": 2, "end": 3})
        self.assertIsNone(locate_quote("甲乙丙丁", "丙乙"))
        # 规则加载：键名锁定 + 越界夹取 + 传入覆盖生效
        self.assertEqual(len(load_rules()), 16)
        self.assertEqual(load_rules({"max_claims": 999})["max_claims"], 50)
        self.assertEqual(EvidenceSkills(rules={"max_claims": 2}).rules["max_claims"], 2)
        # 便捷函数与类等价
        self.assertEqual(collect_evidence(TEXT, llm_fn=StubLLM(), search_fn=default_search(),
                                          fetch_fn=stub_fetch(default_pages()),
                                          clock=lambda: FETCHED_AT)["tool_id"],
                         "collect_evidence")

    # 2-3. 预检 -------------------------------------------------------------
    def test_precheck_empty_input_asks_before_any_io(self):
        """锁定空原文/纯空白在检索之前就追问，且一次 IO 都不发起。"""
        for text in ("", "   \n\t"):
            result = run(text)
            self.assertEqual(result["status"], "needs_clarification")
            self.assertEqual(result["task_status"], "needs_clarification")
            self.assertEqual(result["audit"]["searches"], 0)
            self.assertEqual(result["audit"]["pages"], 0)
            self.assertEqual(result["audit"]["llm_calls"], 0)
            self.assertIn("empty_input", categories(result))

    def test_precheck_overlong_input_asks_before_any_io(self):
        """锁定超长原文按 max_input_chars 追问（说明原因，不检索），而不是硬着头皮查。"""
        result = run("水" * 5000, rules={"max_input_chars": 4000})
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("input_too_long", categories(result, "precheck"))
        self.assertEqual(result["audit"]["llm_calls"], 0)
        self.assertTrue(any("上限" in gap for gap in result["gaps"]))

    # 4-5. 标签白名单与原文定位 --------------------------------------------
    def test_domain_labels_outside_taxonomy_are_dropped(self):
        """锁定模型自造标签记 error 并丢弃；标签被丢空时以 needs_clarification 收尾。"""
        result = run(llm_overrides={'"state":"': (
            {"state": "ok", "primary": "environment", "labels": ["水质", "某小区自来水"],
             "basis": ["自来水有异味"]}, "")})
        self.assertEqual(result["data"]["domain"]["labels"], ["水质"])
        self.assertIn("label_not_in_taxonomy", categories(result, "domain"))
        # 丢弃白名单外标签是一次真实降级：状态记 partial，不当成干净完成
        self.assertEqual(result["status"], "partial")

        all_bad = run(llm_overrides={'"state":"': (
            {"state": "ok", "primary": "environment", "labels": ["不存在的议题"],
             "basis": ["自来水有异味"]}, "")})
        self.assertEqual(all_bad["status"], "needs_clarification")
        self.assertEqual(all_bad["data"]["domain"]["labels"], [])
        self.assertEqual(all_bad["audit"]["searches"], 0)

    def test_domain_basis_must_be_located_in_original_text(self):
        """锁定分类依据必须能逐字定位原文：定位不到的记 error 丢弃，不当作判断依据。"""
        result = run(llm_overrides={'"state":"': (
            {"state": "ok", "primary": "environment", "labels": ["水质"],
             "basis": ["自来水有异味", "原文里根本没有这一句"]}, "")})
        self.assertEqual(result["data"]["domain"]["basis"], ["自来水有异味"])
        self.assertIn("basis_not_anchored", categories(result, "domain"))
        self.assertEqual(result["status"], "partial")

    # 6. 信息不足先追问 -----------------------------------------------------
    def test_domain_problem_states_ask_first_without_retrieval(self):
        """锁定 insufficient/ambiguous/uncovered/multi_topic 四态在检索前追问，不联网。"""
        cases = [("insufficient", "environment"), ("ambiguous", "environment"),
                 ("multi_topic", "environment"), ("ok", "不在领域表里的id")]
        for state, primary in cases:
            with self.subTest(state=state):
                result = run(llm_overrides={'"state":"': (
                    {"state": state, "primary": primary, "labels": ["水质"],
                     "basis": ["自来水有异味"]}, "")})
                self.assertEqual(result["status"], "needs_clarification")
                self.assertEqual(result["task_status"], "needs_clarification")
                self.assertEqual(result["audit"]["searches"], 0)
                self.assertEqual(result["audit"]["pages"], 0)
                self.assertEqual(result["data"]["sources"], [])
                self.assertTrue(any("state_" in e["category"] for e in result["errors"]))

    # 7. LLM 不可用 ---------------------------------------------------------
    def test_domain_llm_unavailable_is_failed_not_clarification(self):
        """锁定连阶段1都进不去时判 failed（契约 §3.3），不伪装成用户歧义、也不猜领域。"""
        for skills in (EvidenceSkills(clock=lambda: FETCHED_AT),
                       EvidenceSkills(llm_fn=lambda *_: (None, "网络错误"),
                                      search_fn=default_search(),
                                      fetch_fn=stub_fetch(default_pages()),
                                      clock=lambda: FETCHED_AT)):
            with self.subTest(kind=type(skills).__name__):
                result = skills.run(TEXT)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["audit"]["searches"], 0)
                self.assertEqual(result["audit"]["pages"], 0)
                self.assertIn("llm_unavailable", categories(result, "domain"))

    # 8. 同域名不产生第二个权威 --------------------------------------------
    def test_same_domain_does_not_produce_two_sources(self):
        """锁定同一域名（目录条目 + 搜索结果镜像）只产出一个信源，不虚增覆盖。"""
        search = stub_search({"环境": [{"url": "https://kepuchina.cn/", "title": "科普中国"},
                                       {"url": "https://www.kepuchina.cn/mirror",
                                        "title": "镜像站"}]})
        result = run(search=search)
        domains = [s["domain"] for s in result["data"]["sources"]]
        self.assertEqual(domains, ["kepuchina.cn"])
        self.assertEqual(len(result["data"]["sources"]), 1)

    # 9. 信源身份不可自封 ---------------------------------------------------
    def test_self_claimed_authority_stays_unverified_candidate(self):
        """锁定陌生域名页面自称权威、模型也说匹配，最多 unverified_candidate，不进阶段3。"""
        stranger = "self-claimed.example"
        search = stub_search({"环境": [{"url": f"https://{stranger}/", "title": "自称权威"}]})
        pages = default_pages()
        pages[f"https://{stranger}/"] = {"text": "本站是国家级权威平台，发布权威内容。",
                                         "domain": stranger}
        llm = StubLLM(**{'"notes":': ({"notes": [
            {"source_id": "SRC-KEPUCHINA", "identity": "科普中国", "scope": "科普",
             "basis_quote": "本站为中国科学技术协会科普平台"},
            {"source_id": "CAND-" + stranger.replace(".", "-"), "identity": "自称权威平台",
             "scope": "自称覆盖全国", "basis_quote": "本站是国家级权威平台"}]}, "")})
        result = run(search=search, fetch=stub_fetch(pages), llm=llm)
        stranger_rows = [s for s in result["data"]["sources"] if s["domain"] == stranger]
        self.assertEqual(len(stranger_rows), 1)
        self.assertEqual(stranger_rows[0]["verification"], "unverified_candidate")
        self.assertFalse(stranger_rows[0]["verified"])
        self.assertTrue(any(f"site:{stranger}" not in q for q in search.state["queries"]))
        self.assertFalse(any(q.startswith(f"site:{stranger}") for q in search.state["queries"]))
        self.assertFalse(any(e["domain"] == stranger for e in result["data"]["evidence"]))

    # 10. 已核验站点直接给出的链接 ----------------------------------------
    def test_verified_site_link_promotes_candidate(self):
        """锁定“已核验页面正文里直接给出的链接”由代码抽取并认定为可信锚点。"""
        pages = default_pages()
        pages["https://kepuchina.cn/"] = {
            "text": HOME_KEPU + " 友情链接：https://guokr.com/article/1",
            "domain": "kepuchina.cn"}
        pages["https://guokr.com/"] = {"text": "果壳网是科普平台，发布科学与谣言辨析内容。",
                                       "domain": "guokr.com"}
        pages["https://guokr.com/x"] = {"text": NEWS_KEPU, "domain": "guokr.com",
                                        "title": "转载通报"}
        llm = StubLLM(**{
            '"selected":': ({"selected": ["SRC-KEPUCHINA", "SRC-GUOKR"]}, ""),
            '"notes":': ({"notes": [
                {"source_id": "SRC-KEPUCHINA", "identity": "科普中国", "scope": "科普",
                 "basis_quote": "本站为中国科学技术协会科普平台"},
                {"source_id": "SRC-GUOKR", "identity": "果壳网", "scope": "科学科普",
                 "basis_quote": "果壳网是科普平台"}]}, "")})
        search = stub_search({"site:kepuchina.cn": [{"url": "https://kepuchina.cn/news/1",
                                                     "title": "水质通报"}],
                              "site:guokr.com": [{"url": "https://guokr.com/x",
                                                  "title": "转载通报"}]})
        result = run(search=search, fetch=stub_fetch(pages), llm=llm)
        guokr = [s for s in result["data"]["sources"] if s["domain"] == "guokr.com"]
        self.assertEqual(guokr[0]["verification"], "verified_site_link")
        self.assertTrue(guokr[0]["verified"])
        self.assertTrue(any(q.startswith("site:guokr.com") for q in search.state["queries"]))
        self.assertTrue(any(e["domain"] == "guokr.com" for e in result["data"]["evidence"]))

    # 11. 阶段3只接受已核验信源 -------------------------------------------
    def test_stage_three_only_accepts_verified_sources(self):
        """锁定未核验候选不进入阶段3：没有已核验信源时只记缺口，不发起站内检索。"""
        with mock.patch("mylocalai.assistant.skills.pipeline._load_sources",
                        return_value={"sources": [{"source_id": "S1", "name": "陌生站点",
                                                   "domain": "stranger.example",
                                                   "tier": "未登记", "domains": [],
                                                   "regions": [], "material_types": [],
                                                   "anchor": False}]}):
            search = default_search()
            result = run(search=search)
        self.assertEqual([s["verification"] for s in result["data"]["sources"]],
                         ["unverified_candidate"])
        self.assertEqual(result["data"]["evidence"], [])
        self.assertEqual(result["audit"]["pages"], 1)     # 只读了它的首页做身份判断
        # 阶段2 的中性机构搜索可以发生，但任何 site: 限定检索都不许出现
        self.assertFalse(any(q.startswith("site:") for q in search.state["queries"]))
        self.assertTrue(any("没有已核验信源" in gap for gap in result["gaps"]))
        self.assertEqual(result["status"], "partial")

    # 12. 引文必须逐字定位已读正文 ----------------------------------------
    def test_quote_must_be_anchored_in_fetched_body(self):
        """锁定模型给的引文指不回已读正文时记 quote_not_anchored 并丢弃，不用转述补位。"""
        result = run(llm_overrides={'"quote":': (
            {"quote": "水质监测数据显示该市出厂水完全达标无忧", "relation": "refutes",
             "limitations": []}, "")})
        self.assertEqual(result["data"]["evidence"], [])
        self.assertIn("quote_not_anchored", categories(result, "evidence"))
        # 读到了页但引文指不回去：错误与缺口各记各的，不把"引文丢了"说成"没查"
        self.assertTrue(any("尚未找到" in gap for gap in result["gaps"]))

    # 13. 关系枚举 ---------------------------------------------------------
    def test_relation_outside_enum_is_rejected_as_unclear(self):
        """锁定关系必须落在 RELATIONS 封闭枚举内，越界值记 error 后按 unclear 处理。"""
        result = run(llm_overrides={'"quote":': (
            {"quote": "水质监测数据显示该市出厂水符合标准", "relation": "基本属实",
             "limitations": []}, "")})
        self.assertEqual(len(result["data"]["evidence"]), 1)
        self.assertEqual(result["data"]["evidence"][0]["relation"], "unclear")
        self.assertIn("bad_relation", categories(result, "evidence"))
        for item in result["data"]["evidence"]:
            self.assertIn(item["relation"], RELATIONS)

    def test_quote_longer_than_limit_is_truncated_then_located(self):
        """锁定超过 max_quote_chars 的引文先截断再定位；截不断定不上的才丢弃。"""
        long_quote = "2026年11月水质监测数据显示该市出厂水符合标准，居民可正常使用自来水，无异味无异常"
        result = run(llm_overrides={'"quote":': (
            {"quote": long_quote, "relation": "refutes", "limitations": []}, "")},
            rules={"max_quote_chars": 20})
        self.assertEqual(len(result["data"]["evidence"]), 1)
        self.assertEqual(result["data"]["evidence"][0]["quote"], long_quote[:20])
        self.assertLessEqual(len(result["data"]["evidence"][0]["quote"]), 20)

    # 15. PII 剥离 ---------------------------------------------------------
    def test_pii_is_stripped_before_query_generation(self):
        """锁定外发前剥邮箱/手机号/证件号/URL：检索词里不出现原文中的隐私片段。"""
        dirty = "某市自来水有异味，联系人 zhang@example.com，电话 13812345678，身份证 110101199003077618，详见 https://secret.example/path?id=9"
        claims = {"claims": [{"text": dirty[:20], "claim_status": "checkable",
                              "qualifiers": {}, "queries": [dirty]}]}
        search = default_search()
        result = run(text=dirty, llm_overrides={
            '"claims":[': (claims, ""),
            '"state":"': ({"state": "ok", "primary": "environment", "labels": ["水质"],
                           "basis": [dirty[:20]]}, "")}, search=search)
        joined = " ".join(search.state["queries"])
        for secret in ("zhang@example.com", "13812345678", "110101199003077618",
                       "https://secret.example"):
            self.assertNotIn(secret, joined)
        self.assertIn("「邮箱」", strip_pii(dirty))
        self.assertIn("「手机号」", strip_pii(dirty))
        self.assertIn("「证件号」", strip_pii(dirty))
        self.assertIn("「链接」", strip_pii(dirty))

    # 16. 范围操作符与 site: 拼接 -----------------------------------------
    def test_model_query_operators_stripped_and_site_code_joined(self):
        """锁定模型写的 site:/inurl: 被剥掉，域名范围由代码按已核验信源拼接。"""
        claims = {"claims": [{"text": CLAIM_1, "claim_status": "checkable",
                              "qualifiers": {},
                              "queries": ["site:kepuchina.cn 自来水 水质 inurl:news",
                                          "自来水 水质"]}]}
        search = default_search()
        result = run(llm_overrides={'"claims":[': (claims, "")}, search=search)
        evidence_queries = [q for q in search.state["queries"] if q.startswith("site:")]
        self.assertTrue(evidence_queries)
        for query in evidence_queries:
            # 只允许代码拼的那一个 site:，且剥掉之后不能再出现第二个范围操作符
            self.assertEqual(query.count("site:"), 1)
            self.assertTrue(query.startswith("site:kepuchina.cn "))
            self.assertNotIn("inurl:", query)
        self.assertEqual(result["data"]["evidence"][0]["url"],
                         "https://kepuchina.cn/news/1")

    # 17-18. 检索降级 vs 合法空结果 --------------------------------------
    def test_search_degraded_is_error_and_gap_not_empty_result(self):
        """锁定“没搜/降级”记 search_unavailable + 覆盖缺口，绝不当成“搜过了没找到”。"""
        result = run(search=stub_search(degraded="外部检索未授权", degrade_prefix="site:"))
        self.assertIn("search_unavailable", categories(result, "evidence"))
        self.assertTrue(any("检索未执行/降级" in gap for gap in result["gaps"]))
        self.assertFalse(any("未检索到站内结果" in gap for gap in result["gaps"]))
        self.assertEqual(result["status"], "partial")
        # 降级尝试也如实计入 searches：中性机构搜索 1 次（正常空结果）+ 主张检索 1 次（降级）
        self.assertEqual(result["audit"]["searches"], 2)
        failed = [op for op in result["audit"]["operations"]
                  if op["kind"] == "search" and not op["ok"]]
        self.assertEqual(len(failed), 1)

    def test_search_empty_without_degradation_is_legal_empty(self):
        """锁定未降级的空结果是合法空结果：只记覆盖缺口，不记 error。"""
        result = run(search=stub_search())
        self.assertEqual(categories(result, "evidence"), set())
        self.assertTrue(any("未检索到站内结果" in gap for gap in result["gaps"]))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["audit"]["searches"], 2)   # 中性机构搜索 + 主张检索

    # 19. 取页错误分类 -----------------------------------------------------
    def test_fetch_error_category_is_recorded_as_fetch_error(self):
        """锁定取页失败按返回的 category 记 error，不与“引文未锚定”混为一谈。"""
        result = run(fetch=stub_fetch(error={"status": "error", "category": "too_large",
                                            "detail": "响应体超过 1500000 字节"}))
        self.assertIn("too_large", categories(result))
        self.assertNotIn("quote_not_anchored", categories(result))
        self.assertTrue(any("1500000" in e["detail"] for e in result["errors"]))

    # 20. 主张超限 ---------------------------------------------------------
    def test_claims_over_limit_asks_to_narrow_and_keeps_split(self):
        """锁定主张数超过 max_claims 时请求缩小范围，且已拆出的部分不丢弃。"""
        claims = {"claims": [{"text": t, "claim_status": "checkable", "qualifiers": {},
                              "queries": []}
                             for t in (CLAIM_1, CLAIM_2, CLAIM_3, CLAIM_1, CLAIM_2,
                                       CLAIM_3, CLAIM_1)]}
        result = run(llm_overrides={'"claims":[': (claims, "")}, rules={"max_claims": 3})
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["task_status"], "needs_clarification")
        self.assertEqual(len(result["data"]["claims"]), 3)
        self.assertIn("too_many_claims", categories(result, "evidence"))
        self.assertTrue(any("缩小范围" in gap for gap in result["gaps"]))

    # 21. 没有可核查主张 ---------------------------------------------------
    def test_no_checkable_claim_is_completed_no_factual_claim(self):
        """锁定只有观点/情绪时 task_status=no_factual_claim 且 status=completed（不是失败）。"""
        pages = {"https://cctv.com/": {"text": "央视新闻网站，发布新闻与调查报道。",
                                       "domain": "cctv.com"}}
        result = run(text="网上说有个孩子走失了，太可怕了，大家快帮忙转发！",
                     fetch=stub_fetch(pages),
                     llm_overrides={
                         '"state":"': ({"state": "ok", "primary": "social_events",
                                        "labels": ["走失"], "basis": ["太可怕了"]}, ""),
                         '"selected":': ({"selected": ["SRC-CCTV"]}, ""),
                         '"notes":': ({"notes": [
                             {"source_id": "SRC-CCTV", "identity": "央视新闻", "scope": "新闻",
                              "basis_quote": "央视新闻网站"}]}, ""),
                         '"claims":[': ({"claims": [
                             {"text": "太可怕了", "claim_status": "not_checkable",
                              "qualifiers": {}, "queries": []}]}, "")})
        self.assertEqual(result["task_status"], "no_factual_claim")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["evidence"], [])
        self.assertEqual(result["data"]["claims"][0]["claim_status"], "not_checkable")
        self.assertTrue(result["report"])
        self.assertTrue(any("没有可核查的事实主张" in gap for gap in result["gaps"]))

    # 22. 预算耗尽保留部分结果 --------------------------------------------
    def test_budget_exhausted_keeps_partial_evidence(self):
        """锁定检索次数到顶即停：保留已取得证据，状态 budget_exhausted，不整批丢弃。"""
        claims = {"claims": [{"text": t, "claim_status": "checkable", "qualifiers": {},
                              "queries": []} for t in (CLAIM_1, CLAIM_2, CLAIM_3)]}
        search = default_search()
        result = run(llm_overrides={'"claims":[': (claims, "")}, search=search,
                      rules={"max_searches": 3})
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(len(result["data"]["evidence"]), 2)
        self.assertEqual(result["audit"]["searches"], 3)
        self.assertTrue(any("预算/死线到顶" in gap for gap in result["gaps"]))

    # 23. 取消保留 ---------------------------------------------------------
    def test_cancel_keeps_collected_evidence(self):
        """锁定取消后不再发起新调用，但已取得的证据与审计全部保留。"""
        claims = {"claims": [{"text": t, "claim_status": "checkable", "qualifiers": {},
                              "queries": []} for t in (CLAIM_1, CLAIM_2)]}
        fetched = []
        llm = StubLLM(**{'"claims":[': (claims, "")})
        # 抽到第一条引文之后才取消：锁的是"取消不抹掉已拿到的证据"
        cancel = lambda: any('"quote":' in system for system, _ in llm.calls)  # noqa: E731
        result = run(llm=llm, fetch=stub_fetch(default_pages(), record=fetched),
                     cancel_check=cancel)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(result["data"]["evidence"]), 1)
        self.assertEqual(len(fetched), 2)      # 取消后没有第三次取页
        self.assertGreaterEqual(len(result["audit"]["operations"]), 3)
        self.assertTrue(any("取消" in gap for gap in result["gaps"]))

    # 24. 跨站转载去重 -----------------------------------------------------
    def test_cross_site_reprint_shares_one_chain(self):
        """锁定同一正文跨站转载只算一条证据链：independence_group 相同、URL 并列、不虚增。"""
        pages = default_pages()
        pages["https://cas.cn/"] = {"text": "中国科学院网站，发布科研动态与科普内容。",
                                    "domain": "cas.cn"}
        pages["https://cas.cn/x"] = {"text": NEWS_KEPU, "domain": "cas.cn",
                                     "title": "转载通报"}
        llm = StubLLM(**{
            '"selected":': ({"selected": ["SRC-KEPUCHINA", "SRC-CAS"]}, ""),
            '"notes":': ({"notes": [
                {"source_id": "SRC-KEPUCHINA", "identity": "科普中国", "scope": "科普",
                 "basis_quote": "本站为中国科学技术协会科普平台"},
                {"source_id": "SRC-CAS", "identity": "中国科学院", "scope": "科研",
                 "basis_quote": "中国科学院网站"}]}, "")})
        search = stub_search({"site:kepuchina.cn": [{"url": "https://kepuchina.cn/news/1",
                                                     "title": "水质通报"}],
                              "site:cas.cn": [{"url": "https://cas.cn/x",
                                               "title": "转载通报"}]})
        result = run(search=search, fetch=stub_fetch(pages), llm=llm)
        evidence = result["data"]["evidence"]
        self.assertEqual(len(evidence), 2)
        self.assertEqual(len({e["independence_group"] for e in evidence}), 1)
        self.assertEqual(len(result["data"]["chains"]), 1)
        chain = result["data"]["chains"][0]
        self.assertEqual(set(chain["urls"]), {"https://kepuchina.cn/news/1",
                                              "https://cas.cn/x"})
        self.assertEqual(chain["evidence_ids"], ["E1", "E2"])
        self.assertTrue(any("转载" in x for x in evidence[1]["limitations"]))

    # 25. 报告转义 ---------------------------------------------------------
    def test_report_escapes_markup_and_markdown_payloads(self):
        """锁定确定性纯文本渲染：HTML/markdown 载荷被转成全角等价物，不形成可执行标记。"""
        pages = default_pages()
        pages["https://kepuchina.cn/news/1"] = {"text": NEWS_KEPU, "domain": "kepuchina.cn",
                                                "title": DIRTY_TITLE}
        result = run(fetch=stub_fetch(pages))
        report = result["report"]
        self.assertNotIn("<script", report)
        self.assertNotIn("](", report)
        self.assertIn("＜script＞", report)      # 信息不丢，但语法失效
        self.assertIn("［更多］", report)
        # 同一结果渲染两次必须逐字相同（确定性）
        self.assertEqual(render_report(result), report)

    # 26. 报告不出现真假判定 ----------------------------------------------
    def test_report_structure_and_no_verdict_words(self):
        """锁定报告八段结构、末尾三计数如实，且全文不出现真假判定词。"""
        result = run()
        report = result["report"]
        for section in ("一、领域识别", "二、信源", "三、主张", "四、证据", "五、证据链",
                        "六、缺口", "七、错误", "八、预算与可观测"):
            self.assertIn(section, report)
        for word in VERDICT_WORDS:
            self.assertNotIn(word, report)
        self.assertIn("已执行 %d 次搜索、%d 次读页、%d 次模型调用"
                      % (result["audit"]["searches"], result["audit"]["pages"],
                         result["audit"]["llm_calls"]), report)
        self.assertIn(CLAIM_1, report)          # 主张逐字保留
        self.assertIn("水质监测数据显示该市出厂水符合标准", report)   # 引文逐字保留
        self.assertIn("https://kepuchina.cn/news/1", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
