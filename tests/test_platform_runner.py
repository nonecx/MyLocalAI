# -*- coding: utf-8 -*-
"""巡检编排测试（P4）：两阶段流程、预算封顶、去重、取消、降级、隐私 fail-closed。

全部离线：stub provider（不联网）+ stub agent（不调 LLM）+ 临时 SQLite 库。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.core.privacy import PrivacyPreferences  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402
from mylocalai.platform.candidates import FeedCandidate  # noqa: E402
from mylocalai.platform.runner import PlatformRunner  # noqa: E402
from mylocalai.platform.sources import ProviderResult  # noqa: E402
from mylocalai.platform.store import PlatformStore  # noqa: E402

TEST_RULES = {
    "enabled": True,
    "scan_interval_s": 900,
    "max_queries_per_scan": 5,
    "max_candidates_per_query": 8,
    "screen": {"heat_screen_e": 40},
    "deep_verify": {"max_per_scan": 2, "deadline_s": 60},
    "dedup": {"fingerprint_threshold": 0.72},
    "risk": {
        "weights": {"heat": 0.4, "growth": 0.15, "breadth": 0.2, "rumor": 0.25},
        "breadth_full_sources": 10,
        "levels": {"suspect": 35, "high": 60, "urgent": 80},
        "floors": [],
    },
    "watch_queries": [],
    "rss_sources": [],
}

PREFS_ON = PrivacyPreferences({
    "schema_version": 1,
    "retention_days": {"results": 90, "raw_input": 30, "external_cache": 7},
    "save_enabled": {"results": True, "raw_input": False, "external_cache": False},
    "external_processing_enabled": True,
    "external_processing_providers": ["public-search"],
})

PREFS_NO_SAVE = PrivacyPreferences({
    "schema_version": 1,
    "retention_days": {"results": 90, "raw_input": 30, "external_cache": 7},
    "save_enabled": {"results": False, "raw_input": False, "external_cache": False},
    "external_processing_enabled": True,
    "external_processing_providers": ["public-search"],
})

PREFS_NO_EXTERNAL = PrivacyPreferences({
    "schema_version": 1,
    "retention_days": {"results": 90, "raw_input": 30, "external_cache": 7},
    "save_enabled": {"results": True, "raw_input": False, "external_cache": False},
    "external_processing_enabled": False,
    "external_processing_providers": [],
})


class StubProvider:
    """可按查询词返回不同候选的 stub（热度是话题级：整批共享一个 E）。"""

    name = "stub"

    def __init__(self, candidates=None, by_query=None, degraded=False, error=""):
        self.candidates = candidates or []
        self.by_query = by_query or {}
        self.degraded = degraded
        self.error = error
        self.calls = 0

    def discover(self, query, k):
        self.calls += 1
        cands = self.by_query.get(query, self.candidates)
        return ProviderResult(candidates=cands[:max(1, int(k))],
                              degraded=self.degraded, error=self.error)


class StubAgent:
    def __init__(self, label="疑似谣言", risk_score=62.0, raise_exc=False):
        self.label = label
        self.risk_score = risk_score
        self.raise_exc = raise_exc
        self.calls = []

    def analyze(self, text, cancel_check=None):
        self.calls.append(text)
        if self.raise_exc:
            raise RuntimeError("引擎炸了")
        return {"label": self.label, "risk_score": self.risk_score,
                "degraded_components": [], "evidence_book": {"entries": []}}


def _cand(title, url, tier="一般门户媒体", snippet=""):
    return FeedCandidate(title=title, url=url, snippet=snippet, domain=url.split("/")[2],
                         domain_tier=tier, published_at="2026-11-20")


class _RunnerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Path(self.tmp.name) / "dossier.db"
        self.addCleanup(self.tmp.cleanup)

    def _runner(self, rules=None, privacy=PREFS_ON, providers=None, agent=None):
        store = PlatformStore(self.db)
        dossiers = DossierService(store=DossierStore(self.db), privacy=privacy)
        self.addCleanup(store.close)
        self.addCleanup(dossiers.close)
        return PlatformRunner(store=store, rules=rules or TEST_RULES, privacy=privacy,
                              dossiers=dossiers, providers=providers or [StubProvider([])],
                              agent=agent or StubAgent())


class TestPrivacyGates(_RunnerTestBase):
    def test_external_not_authorized_skips_scan(self):
        provider = StubProvider([_cand("x", "https://a.example.com/1")])
        runner = self._runner(privacy=PREFS_NO_EXTERNAL, providers=[provider])
        summary = runner.scan(queries=["话题"])
        self.assertEqual(summary["status"], "skipped")
        self.assertIn("external_processing_not_authorized", summary["error"])
        self.assertEqual(provider.calls, 0)              # 不外发
        self.assertEqual(summary["entries"], [])
        run = runner.store.get_scan_run(summary["scan_id"])
        self.assertEqual(run["status"], "skipped")       # 跳过也有账

    def test_results_saving_off_returns_entries_without_persisting(self):
        provider = StubProvider([_cand("网传某物致癌速转", "https://a.example.com/1")])
        runner = self._runner(privacy=PREFS_NO_SAVE, providers=[provider])
        summary = runner.scan(queries=["话题"])
        self.assertEqual(summary["status"], "completed")
        self.assertTrue(summary["entries"])              # 当次返回有结果
        self.assertEqual(runner.store.list_feed_entries(), [])   # 但不落库
        self.assertTrue(any("不落库" in w for w in summary["warnings"]))


class TestTwoStageFlow(_RunnerTestBase):
    def test_screen_and_deep_verify(self):
        """热门候选（过热度门槛）深查出 verdict；冷候选只登记。"""
        """热度是话题级（整批共享一个 E）：冷话题（单条普通候选 E≈31<40）只登记；
        热话题（摘要含公开大阅读量 E≈44≥40）深查出 verdict。"""
        hot = _cand("某市发布高温红色预警", "https://news.example.com/1",
                    snippet="阅读量 4831.6万")
        cold = _cand("无关财经简讯", "https://news.example.com/2")
        provider = StubProvider(by_query={"热门话题": [hot], "冷门话题": [cold]})
        agent = StubAgent()
        runner = self._runner(providers=[provider], agent=agent)
        summary = runner.scan(queries=["热门话题", "冷门话题"])
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["counters"]["candidates"], 2)
        self.assertEqual(summary["counters"]["verified"], 1)
        self.assertEqual(len(agent.calls), 1)            # 只深查热话题那条
        entries = {e["title"]: e for e in summary["entries"]}
        self.assertEqual(entries[hot.title]["status"], "verified")
        self.assertEqual(entries[hot.title]["verdict_label"], "疑似谣言")
        self.assertEqual(entries[cold.title]["status"], "screened")
        # 落库：条目、verdict、风险分级都在
        rows = runner.store.list_feed_entries(limit=10)
        self.assertEqual(len(rows), 2)
        verified = [r for r in rows if r["status"] == "verified"]
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["risk_score"], 62.0)
        self.assertTrue(verified[0]["risk_level"])
        self.assertTrue(verified[0]["risk_reasons"])
        # 深查建立了 topic 档案
        dossiers = runner.dossiers.store.list_dossiers()
        self.assertEqual(len(dossiers), 1)
        self.assertEqual(dossiers[0]["input_kind"], "topic")

    def test_marker_hit_triggers_deep_even_when_cold(self):
        """热度不够但话术特征命中（零 LLM 初扫）也值得深查。"""
        rules = dict(TEST_RULES)
        rules["screen"] = {"heat_screen_e": 99}          # 热度门槛高到过不去
        provider = StubProvider([_cand("网传某物致癌，速转家人！", "https://a.example.com/1")])
        agent = StubAgent()
        runner = self._runner(rules=rules, providers=[provider], agent=agent)
        summary = runner.scan(queries=["某物"])
        self.assertEqual(summary["counters"]["verified"], 1)

    def test_budget_caps_deep_verifications(self):
        """四条都过热门槛（摘要含公开阅读量），深查预算封顶只查 1 条。"""
        cands = [_cand(f"热门话题 {i}", f"https://news.example.com/{i}",
                       snippet="阅读量 1200万") for i in range(4)]
        rules = dict(TEST_RULES)
        rules["deep_verify"] = {"max_per_scan": 1, "deadline_s": 60}
        runner = self._runner(rules=rules, providers=[StubProvider(cands)],
                              agent=StubAgent())
        summary = runner.scan(queries=["热门"])
        self.assertEqual(summary["counters"]["candidates"], 4)
        self.assertEqual(summary["counters"]["verified"], 1)   # 封顶 1 次深查

    def test_cross_scan_url_dedup_and_prev_heat(self):
        provider = StubProvider([_cand("话题甲", "https://news.example.com/1")])
        runner = self._runner(providers=[provider])
        runner.scan(queries=["话题"])
        first = runner.store.list_feed_entries()[0]
        # 第二次：同 URL → 去重；换新 URL → 新条目带 e_prev（环比基准）
        provider.candidates = [_cand("话题甲", "https://news.example.com/1"),
                               _cand("话题乙", "https://news.example.com/2")]
        summary = runner.scan(queries=["话题"])
        self.assertEqual(summary["counters"]["deduped"], 1)
        rows = runner.store.list_feed_entries()
        self.assertEqual(len(rows), 2)
        new_entry = [r for r in rows if r["url"].endswith("/2")][0]
        self.assertAlmostEqual(new_entry["e_prev"], first["e_score"], places=6)

    def test_batch_fingerprint_dedup_reposts(self):
        """同一事件多家转载（URL 不同、标题近似）只保留第一条。"""
        a = _cand("某市自来水检测异常 官方通报排除投毒", "https://a.example.com/1")
        b = _cand("网传某市自来水检测异常，官方通报排除投毒", "https://b.example.com/2")
        runner = self._runner(providers=[StubProvider([a, b])])
        summary = runner.scan(queries=["自来水"])
        self.assertEqual(summary["counters"]["candidates"], 1)

    def test_seed_queries_from_rules_when_store_empty(self):
        rules = dict(TEST_RULES)
        rules["watch_queries"] = [{"query": "种子词", "enabled": True}]
        provider = StubProvider([_cand("种子词相关", "https://a.example.com/1")])
        runner = self._runner(rules=rules, providers=[provider])
        summary = runner.scan()                          # 不传 queries
        self.assertEqual(summary["counters"]["queries"], 1)
        self.assertEqual(summary["queries"][0]["query"], "种子词")
        rows = runner.store.list_watch_queries()
        self.assertEqual(len(rows), 1)


class TestDegradeAndCancel(_RunnerTestBase):
    def test_agent_failure_marks_verify_failed_without_verdict(self):
        provider = StubProvider([_cand("热门话题", "https://news.example.com/1",
                                       snippet="阅读量 1200万")])
        runner = self._runner(providers=[provider], agent=StubAgent(raise_exc=True))
        summary = runner.scan(queries=["热门"])
        entry = summary["entries"][0]
        self.assertEqual(entry["status"], "verify_failed")
        self.assertEqual(entry["verdict_label"], "")     # 不编 verdict
        self.assertGreaterEqual(summary["counters"]["degraded"], 1)
        row = runner.store.list_feed_entries()[0]
        self.assertEqual(row["status"], "verify_failed")
        self.assertTrue(row["risk_level"])               # 风险仍由热度/来源推导

    def test_provider_degraded_recorded(self):
        provider = StubProvider([], degraded=True, error="captcha")
        runner = self._runner(providers=[provider])
        summary = runner.scan(queries=["话题"])
        self.assertGreaterEqual(summary["counters"]["degraded"], 1)
        self.assertTrue(any("降级" in w for w in summary["warnings"]))

    def test_cancel_stops_and_keeps_completed(self):
        cands = [_cand(f"热门 {i}", f"https://news.example.com/{i}") for i in range(3)]
        state = {"n": 0}

        def cancel_check():
            state["n"] += 1
            return state["n"] > 3                          # 查询级+前两个候选后取消

        runner = self._runner(providers=[StubProvider(cands)])
        summary = runner.scan(queries=["热门"], cancel_check=cancel_check)
        self.assertEqual(summary["status"], "cancelled")
        self.assertEqual(summary["counters"]["candidates"], 2)   # 已完成的保留

    def test_no_queries_completed_with_warning(self):
        runner = self._runner()
        summary = runner.scan()
        self.assertEqual(summary["status"], "completed")
        self.assertTrue(any("监控词" in w for w in summary["warnings"]))

    def test_scan_never_raises_on_store_failure(self):
        """库层炸掉也只落成 failed 状态，不向上抛。"""
        runner = self._runner(providers=[StubProvider([])])
        runner.store.start_scan_run = lambda: (_ for _ in ()).throw(RuntimeError("db gone"))
        summary = runner.scan(queries=["话题"])
        self.assertEqual(summary["status"], "completed")   # 记账失败不影响巡检本身
        self.assertTrue(any("scan_runs" in w for w in summary["warnings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
