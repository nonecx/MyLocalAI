# -*- coding: utf-8 -*-
"""共享采集底座测试（collect_core）：规则/预算/状态/来源/动态权威/去重/锚定。

全离线：只读本地 JSON 资源与注入假时钟，不触网、不调模型。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills import collect_core as C  # noqa: E402
from mylocalai.assistant.skills import pipeline as P  # noqa: E402


class TestConstants(unittest.TestCase):
    def test_statuses_match_pipeline(self):
        self.assertEqual(C.STATUSES, P.STATUSES)

    def test_relations_and_task_statuses(self):
        self.assertEqual(C.RELATIONS, P.RELATIONS)
        self.assertEqual(C.TASK_STATUSES, P.TASK_STATUSES)

    def test_authority_enums_closed(self):
        self.assertEqual(set(C.AUTHORITY_CLASSES),
                         {"official", "international_org", "academic", "standards",
                          "factcheck", "media", "unverified"})
        self.assertEqual(set(C.VERIFICATIONS),
                         {"catalog_anchor", "verified_site_link", "session_verified",
                          "unverified"})


class TestRules(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, payload):
        path = Path(self.tmp.name) / "rules.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_missing_file_returns_defaults(self):
        out = C.load_collect_rules(Path(self.tmp.name) / "nope.json",
                                   {"max_searches": 5, "total_deadline_s": 30.0})
        self.assertEqual(out["max_searches"], 5)
        self.assertEqual(out["total_deadline_s"], 30.0)

    def test_bad_json_returns_defaults(self):
        path = Path(self.tmp.name) / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        out = C.load_collect_rules(path, {"max_pages": 3})
        self.assertEqual(out["max_pages"], 3)

    def test_numeric_clamped_to_limits(self):
        path = self._write({"max_searches": 9999, "total_deadline_s": -1})
        out = C.load_collect_rules(path, {"max_searches": 5, "total_deadline_s": 30.0})
        self.assertEqual(out["max_searches"], 200)
        self.assertEqual(out["total_deadline_s"], 1.0)

    def test_bad_type_falls_back(self):
        path = self._write({"max_searches": "many", "max_pages": None})
        out = C.load_collect_rules(path, {"max_searches": 5, "max_pages": 4})
        self.assertEqual(out["max_searches"], 5)
        self.assertEqual(out["max_pages"], 4)

    def test_unknown_keys_dropped(self):
        path = self._write({"mystery": 1})
        out = C.load_collect_rules(path, {"max_searches": 5})
        self.assertNotIn("mystery", out)

    def test_authority_config_defaults_and_bad_file(self):
        cfg = C.load_authority_config(Path(self.tmp.name) / "missing.json")
        self.assertIn("official", cfg["decisive_classes"])
        self.assertTrue(cfg["dynamic_enabled"])
        self.assertGreaterEqual(cfg["dynamic_min_signals"], 1)


class TestBudget(unittest.TestCase):
    def test_allow_spend_remaining(self):
        b = C.CollectBudget({"max_searches": 2, "max_pages": 1})
        self.assertTrue(b.allow("max_searches"))
        b.spend("max_searches")
        self.assertEqual(b.remaining("max_searches"), 1)
        b.spend("max_searches")
        self.assertFalse(b.allow("max_searches"))
        self.assertEqual(b.remaining("max_searches"), 0)

    def test_deadline_blocks(self):
        b = C.CollectBudget({"max_searches": 5}, deadline=10.0,
                            clock=lambda: 10.0)
        self.assertTrue(b.deadline_hit())
        self.assertFalse(b.allow("max_searches"))

    def test_block_reason(self):
        b = C.CollectBudget({"max_searches": 5})
        b.block("测试原因")
        self.assertEqual(b.blocked_reason, "测试原因")
        self.assertFalse(b.allow("max_searches"))


class TestRunState(unittest.TestCase):
    def _state(self, cancel=None):
        return C.CollectRunState(C.CollectBudget({"max_searches": 2}), cancel_check=cancel)

    def test_completed(self):
        st = self._state()
        st.data["x"] = 1
        out = st.finish()
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["task_status"], "completed")

    def test_partial_on_error(self):
        st = self._state()
        st.data["x"] = 1
        st.error("s", "c", "d")
        self.assertEqual(st.finish()["status"], "partial")

    def test_clarify(self):
        st = self._state()
        st.clarify("太宽")
        self.assertEqual(st.finish()["status"], "needs_clarification")
        self.assertEqual(st.finish()["task_status"], "needs_clarification")

    def test_failed_without_data(self):
        st = self._state()
        st.fail("未授权")
        self.assertEqual(st.finish()["status"], "failed")

    def test_budget_exhausted(self):
        st = self._state()
        st.budget.block("额度用尽")
        self.assertEqual(st.finish()["status"], "budget_exhausted")

    def test_cancelled(self):
        st = self._state(cancel=lambda: True)
        self.assertEqual(st.finish()["status"], "cancelled")

    def test_no_factual_claim(self):
        st = self._state()
        st.no_factual_claim()
        out = st.finish()
        self.assertEqual(out["task_status"], "no_factual_claim")
        self.assertEqual(out["status"], "completed")

    def test_audit_counts(self):
        st = self._state()
        st.operation("search", "q")
        st.operation("fetch", "u")
        st.operation("llm", "c")
        self.assertEqual(st.audit["searches"], 1)
        self.assertEqual(st.audit["pages"], 1)
        self.assertEqual(st.audit["llm_calls"], 1)


class TestSources(unittest.TestCase):
    def test_official_registry_domain_is_catalog_anchor(self):
        info = C.classify_source("https://www.nhc.gov.cn/x")
        self.assertEqual(info.tier, "官方权威")
        self.assertEqual(info.authority_class, "official")
        self.assertEqual(info.verification, "catalog_anchor")
        self.assertTrue(info.anchor)

    def test_reliable_media_is_media(self):
        info = C.classify_source("https://www.xinhuanet.com/x")
        self.assertEqual(info.authority_class, "media")
        self.assertEqual(info.verification, "catalog_anchor")

    def test_unknown_domain_unverified(self):
        info = C.classify_source("https://example.org/x")
        self.assertEqual(info.authority_class, "unverified")
        self.assertEqual(info.verification, "unverified")
        self.assertFalse(info.anchor)

    def test_lookalike_domain_not_authoritative(self):
        info = C.classify_source("https://xinhuanet.com.evil.com/x")
        self.assertEqual(info.authority_class, "unverified")
        self.assertEqual(info.verification, "unverified")

    def test_gov_lookalike_not_catalog_anchor(self):
        """铁律：目录锚点只认 skills/sources.json 登记过的域名。

        检索层分级表对 gov 后缀是宽匹配（伪造站点同样命中"官方权威"档），
        所以这里不能用"档位=官方"当锚点依据——否则就是来源自我认证。
        """
        info = C.classify_source("https://www.fake.gov.cn/notice")
        self.assertEqual(info.tier, "官方权威")
        self.assertEqual(info.authority_class, "official")
        self.assertEqual(info.verification, "unverified")
        self.assertFalse(info.anchor)
        self.assertIn("目录外", info.verification_basis)

    def test_class_override(self):
        info = C.classify_source("https://www.piyao.org.cn/x")
        self.assertEqual(info.authority_class, "factcheck")

    def test_non_anchor_catalog_entry_is_not_catalog_anchor(self):
        """目录里 anchor=false 的条目（丁香园/果壳/法治日报）只作可靠来源。

        两条流水线的 _catalog_anchor_domains 都只收 anchor=true，collect_core
        必须同一口径，否则同一 URL 在底座与流水线里得到相反结论。
        """
        info = C.classify_source("https://www.dxy.cn/x")
        self.assertEqual(info.verification, "unverified")
        self.assertFalse(info.anchor)
        self.assertIn("非锚点", info.verification_basis)
        self.assertEqual(info.authority_class, "media")

    def test_decisive_matrix(self):
        cfg = C.load_authority_config()
        official = C.classify_source("https://www.nhc.gov.cn/x")
        media = C.classify_source("https://www.xinhuanet.com/x")
        unknown = C.classify_source("https://example.org/x")
        self.assertTrue(C.is_decisive_source(official, authority_config=cfg))
        self.assertFalse(C.is_decisive_source(media, authority_config=cfg))
        self.assertFalse(C.is_decisive_source(unknown, authority_config=cfg))

    def test_session_verified_requires_corroboration(self):
        cfg = C.load_authority_config()
        info = C.SourceInfo("S", "某市政府", "x.gov.cn", "官方权威", "官方", "official",
                            "session_verified", "动态", True)
        self.assertFalse(C.is_decisive_source(info, authority_config=cfg))
        self.assertTrue(C.is_decisive_source(info, authority_config=cfg, corroborated=True))


class TestDynamicAuthority(unittest.TestCase):
    PAGE = {"url": "https://www.fake.gov.cn/", "text":
            "政府网站标识码 1234567890 主办单位：某市人民政府 公网安备 123号"}

    def test_suffix_plus_marker_verified(self):
        out = C.dynamic_authority_signals("https://www.fake.gov.cn/", {"u": self.PAGE})
        self.assertIn("official_suffix", out["signals"])
        self.assertIn("official_marker", out["signals"])
        self.assertTrue(out["verified"])
        self.assertEqual(out["authority_class"], "official")

    def test_marker_only_unverified(self):
        page = dict(self.PAGE, url="https://nice-looking.com/")
        out = C.dynamic_authority_signals("https://nice-looking.com/", {"u": page})
        self.assertEqual(out["signals"], ["official_marker"])
        self.assertFalse(out["verified"])
        self.assertEqual(out["authority_class"], "unverified")

    def test_suffix_only_unverified(self):
        page = {"url": "https://fake.gov.cn/", "text": "欢迎访问本站"}
        out = C.dynamic_authority_signals("https://fake.gov.cn/", {"u": page})
        self.assertEqual(out["signals"], ["official_suffix"])
        self.assertFalse(out["verified"])

    def test_weak_footer_markers_do_not_count(self):
        """ICP备案号/主办单位是大陆所有合法网站的页脚法定字段，不算身份信号。

        只认政府网站标识码：gov 后缀 + 页脚只有 ICP 备案号 → 仍判未核验。
        """
        page = {"url": "https://fake.gov.cn/",
                "text": "版权所有 主办单位：某某公司 ICP备12345678号 公网安备 123号"}
        out = C.dynamic_authority_signals("https://fake.gov.cn/", {"u": page})
        self.assertNotIn("official_marker", out["signals"])
        self.assertFalse(out["verified"])

    def test_government_site_id_code_counts(self):
        page = {"url": "https://real.gov.cn/", "text": "政府网站标识码 1234567890"}
        out = C.dynamic_authority_signals("https://real.gov.cn/", {"u": page})
        self.assertIn("official_marker", out["signals"])
        self.assertTrue(out["verified"])

    def test_cross_mention_signal(self):
        verified = [{"url": "https://www.nhc.gov.cn/a", "text": "见 https://x.gov.cn/page"},
                    {"url": "https://www.samr.gov.cn/b", "text": "另见 x.gov.cn"}]
        out = C.dynamic_authority_signals("https://x.gov.cn/", {},
                                          verified_pages=verified)
        self.assertIn("cross_mention", out["signals"])
        self.assertTrue(out["verified"])

    def test_cross_mention_needs_two_sources(self):
        verified = [{"url": "https://www.nhc.gov.cn/a", "text": "见 https://x.gov.cn/page"}]
        out = C.dynamic_authority_signals("https://x.gov.cn/", {},
                                          verified_pages=verified)
        self.assertNotIn("cross_mention", out["signals"])

    def test_inbound_link_adds_signal(self):
        verified = [{"url": "https://www.nhc.gov.cn/", "text":
                     "相关链接 https://www.fake.gov.cn/notice"}]
        out = C.dynamic_authority_signals("https://www.fake.gov.cn/", {"u": self.PAGE},
                                          verified_pages=verified)
        self.assertIn("inbound_link", out["signals"])
        self.assertTrue(out["verified"])

    def test_dynamic_disabled(self):
        cfg = C.load_authority_config()
        cfg["dynamic_enabled"] = False
        out = C.dynamic_authority_signals("https://www.fake.gov.cn/", {"u": self.PAGE},
                                          authority_config=cfg)
        self.assertFalse(out["verified"])


class TestDedupAndAnchor(unittest.TestCase):
    def test_url_key_and_dedupe(self):
        rows = [{"url": "https://A.com/x/"}, {"url": "https://a.com/x"},
                {"url": "https://a.com/y"}]
        self.assertEqual(C.url_key(rows[0]["url"]), "a.com/x")
        self.assertEqual(len(C.dedupe_sources(rows)), 2)

    def test_near_duplicate(self):
        self.assertTrue(C.near_duplicate_source(
            {"title": "某地自来水被投毒"}, [{"title": "某地自来水被投毒"}], 0.9))
        self.assertFalse(C.near_duplicate_source(
            {"title": "某地自来水被投毒"}, [{"title": "天气预报"}], 0.9))

    def test_anchor_quote(self):
        self.assertIsNotNone(C.anchor_quote("今天 天气很好。", "天气很好"))
        self.assertIsNone(C.anchor_quote("今天天气很好。", "明天"))
        self.assertIsNone(C.anchor_quote("正文", "   "))

    def test_skill_prompt_fallback(self):
        text = C.load_skill_prompt(Path("no-such-dir"), '{"x":1}')
        self.assertIn('{"x":1}', text)
        self.assertTrue(text)

    def test_render_lines_no_verdict_words(self):
        line = C.render_evidence_line({"evidence_id": "E1", "relation": "supports",
                                       "name": "某市政府", "quote": "原文"})
        self.assertIn("E1", line)
        self.assertIn("原文", line)
        for word in ("是真的", "是假的", "属实"):
            self.assertNotIn(word, line)
        self.assertIn("已执行 1 次搜索", C.render_counts_line(
            {"searches": 1, "pages": 2, "llm_calls": 3}))


if __name__ == "__main__":
    unittest.main(verbosity=2)