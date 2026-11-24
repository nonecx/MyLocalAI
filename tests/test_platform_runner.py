# -*- coding: utf-8 -*-
"""巡检编排测试（P4）：两阶段流程、预算封顶、去重、取消、降级、隐私 fail-closed。

全部离线：stub provider（不联网）+ stub agent（不调 LLM）+ 临时 SQLite 库。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.core.privacy import PrivacyPreferences  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402
from mylocalai.llm import llm_client, model_registry  # noqa: E402  测试隔离用（见 setUp）
from mylocalai.platform.candidates import FeedCandidate  # noqa: E402
from mylocalai.platform.runner import PlatformRunner  # noqa: E402
from mylocalai.platform.sources import (PatrolSourceProvider,  # noqa: E402
                                        ProviderResult)
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

    def discover(self, query, k, query_id=None):
        self.calls += 1
        cands = self.by_query.get(query, self.candidates)
        return ProviderResult(candidates=cands[:max(1, int(k))],
                              degraded=self.degraded, error=self.error)


class StubAgent:
    """桩 RumorAgent：记录每次 analyze 的 text 与 domain_hints（透传断言用）。"""

    def __init__(self, label="疑似谣言", risk_score=62.0, raise_exc=False):
        self.label = label
        self.risk_score = risk_score
        self.raise_exc = raise_exc
        self.calls = []          # [{"text": ..., "domain_hints": ...}]

    def analyze(self, text, cancel_check=None, domain_hints=None):
        self.calls.append({"text": text, "domain_hints": domain_hints})
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
        # 与真实模型注册表隔离：runner 的领域自动推断（domains.resolve_domain）在
        # 注册表有可用密钥时会真实调用 LLM（2026-11-22 用户配置真实模型后本组用例
        # 挂起 75s+ 的根因）。隔离后 llm 不可用 → 领域推断走 fail-soft 快速回落，
        # 与"无 key 时代"的用例前提一致。
        self._saved_ml = {k: os.environ.get(k) for k in
                          ("MYLOCALAI_MODELS_PATH", "MYLOCALAI_MODEL_SECRETS_PATH")}
        os.environ["MYLOCALAI_MODELS_PATH"] = "Z:/nonexistent/models.json"
        os.environ["MYLOCALAI_MODEL_SECRETS_PATH"] = "Z:/nonexistent/secrets.json"
        model_registry.reset_cache()
        llm_client.reset_cache()
        self.addCleanup(self._restore_ml)

    def _restore_ml(self):
        for k, v in self._saved_ml.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        model_registry.reset_cache()
        llm_client.reset_cache()

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

    def test_watch_query_deleted_mid_scan_is_degraded_not_dedup(self):
        """巡检中途删掉监控词：其余候选诚实跳过并记 degraded，
        不许把外键冲突伪装成 deduped（冒烟测试抓到的真实 race）。"""
        cands = [_cand(f"热门 {i}", f"https://news.example.com/{i}",
                       snippet="阅读量 1200万") for i in range(3)]
        provider = StubProvider(cands)
        runner = self._runner(providers=[provider])
        # 在 discover 返回前删掉监控词：模拟"巡检中途用户删词"的竞态
        original_discover = provider.discover

        def discover_then_delete(query, k, query_id=None):
            for row in runner.store.list_watch_queries():
                runner.store.delete_watch_query(row["id"])
            return original_discover(query, k, query_id=query_id)

        provider.discover = discover_then_delete
        summary = runner.scan(queries=["热门"])
        self.assertEqual(summary["counters"]["deduped"], 0)      # 不是去重
        self.assertEqual(summary["counters"]["candidates"], 0)   # 候选全部跳过
        self.assertGreaterEqual(summary["counters"]["degraded"], 1)
        self.assertTrue(any("删除" in w for w in summary["warnings"]))

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


class StubDomainStore:
    """领域接线用的桩 store：只实现 runner 巡检用到的存储面
    （真库语义由 tests/test_platform_store.py 覆盖）。"""

    def __init__(self, watch_rows=None):
        self.watch_rows = {}
        for row in (watch_rows or []):
            self.watch_rows[int(row["id"])] = dict(row)
        self.feed_entries = []          # add_feed_entry 收到的字段
        self.entry_updates = []         # update_feed_entry 收到的字段
        self.inferred_writes = []       # set_inferred_domain 收到的参数
        self._next_id = 1
        self._urls = set()

    # ---- 监控词 ----

    def ensure_watch_query(self, query, note=""):
        for rid, row in self.watch_rows.items():
            if row["query"] == query:
                return rid
        rid = max(self.watch_rows, default=0) + 1
        self.watch_rows[rid] = {"id": rid, "query": query, "domain": "",
                                "enabled": True}
        return rid

    def get_watch_query(self, query_id):
        row = self.watch_rows.get(int(query_id))
        return dict(row) if row else None

    def watch_query_exists(self, query_id):
        return int(query_id) in self.watch_rows

    def list_watch_queries(self, enabled_only=False):
        return [dict(r) for r in self.watch_rows.values()
                if not enabled_only or r.get("enabled", True)]

    def set_inferred_domain(self, query_id, domain, at=""):
        self.inferred_writes.append((query_id, domain, at))
        row = self.watch_rows.get(int(query_id))
        if row is not None:
            row["inferred_domain"] = domain
            row["inferred_at"] = at

    def domain_histogram(self, limit=200):
        return {}

    # ---- 巡检记账 ----

    def start_scan_run(self):
        return 1

    def finish_scan_run(self, *args, **kwargs):
        return True

    def latest_e_for_query(self, query_id):
        return None

    # ---- 信息流（签名与数据层 v7 一致：收 topic_domain/topic_domain_source）----

    def add_feed_entry(self, *, query_id: int, title: str, url: str, snippet: str = "",
                       domain: str = "", domain_tier: str = "", topic_domain: str = "",
                       topic_domain_source: str = "", discovered_at: str = "",
                       e_score=None, **extra):
        self.feed_entries.append({"query_id": query_id, "title": title, "url": url,
                                  "domain": domain, "topic_domain": topic_domain,
                                  "topic_domain_source": topic_domain_source})
        if url in self._urls:
            return None
        self._urls.add(url)
        self._next_id += 1
        return self._next_id - 1

    def update_feed_entry(self, entry_id, **fields):
        self.entry_updates.append({"entry_id": entry_id, **fields})
        return True


class LegacyFeedStore(StubDomainStore):
    """老形状 store（数据层 v7 落地前）：add_feed_entry 还不收 topic_domain。
    runner 应自动降级——不硬传该字段（update 的字段被老白名单忽略），
    巡检其余流程不变。"""

    def __init__(self, watch_rows=None):
        super().__init__(watch_rows)
        self.legacy_add_extras = None

    def add_feed_entry(self, *, query_id: int, title: str, url: str, snippet: str = "",
                       domain: str = "", domain_tier: str = "", discovered_at: str = "",
                       e_score=None, **extra):
        self.legacy_add_extras = dict(extra)
        return StubDomainStore.add_feed_entry(
            self, query_id=query_id, title=title, url=url, snippet=snippet,
            domain=domain, domain_tier=domain_tier, discovered_at=discovered_at,
            e_score=e_score, topic_domain="", topic_domain_source="")


class FakeDomains:
    """桩领域模块：按任务书链路实现 resolve_domain（顺序断言用），记录调用参数。
    真实链路（TTL/封闭枚举/LLM 适配）由 tests/test_platform_domains.py 覆盖。"""

    HINTS = {"public_health": ["nhc.gov.cn", "chinacdc.cn"],
             "finance": ["pbc.gov.cn", "csrc.gov.cn"]}

    def __init__(self, classify_result=None, histogram=None):
        self.classify_result = classify_result
        self.histogram = dict(histogram or {})
        self.resolve_calls = []
        self.hints_calls = []

    def resolve_domain(self, user_domain, query_text, row, store, llm_fn=None,
                       now_iso="", ttl_days=30, history_min=2):
        self.resolve_calls.append({
            "user_domain": user_domain, "query_text": query_text, "row": row,
            "store": store, "llm_fn": llm_fn, "now_iso": now_iso,
            "ttl_days": ttl_days, "history_min": history_min})
        if user_domain:
            return user_domain, "user"
        inferred = str((row or {}).get("inferred_domain") or "")
        if inferred:
            return inferred, "inferred_text"
        if self.classify_result:
            setter = getattr(store, "set_inferred_domain", None)
            if setter is not None:
                setter(int((row or {}).get("id") or 0), self.classify_result, now_iso)
            return self.classify_result, "inferred_text"
        top, count = self._top()
        if top and count >= history_min:
            return top, "inferred_history"
        return "", "none"

    def _top(self):
        if not self.histogram:
            return "", 0
        top = max(self.histogram, key=lambda k: self.histogram[k])
        return top, self.histogram[top]

    def domain_hints(self, domain_id):
        self.hints_calls.append(domain_id)
        return list(self.HINTS.get(domain_id, []))


class _DomainTestBase(_RunnerTestBase):
    def _domain_runner(self, store, domains, rules=None, providers=None, agent=None):
        dossiers = DossierService(store=DossierStore(self.db), privacy=PREFS_ON)
        self.addCleanup(dossiers.close)
        return PlatformRunner(store=store, rules=rules or TEST_RULES, privacy=PREFS_ON,
                              dossiers=dossiers,
                              providers=providers or [StubProvider([])],
                              agent=agent or StubAgent(), domains=domains)


class TestDomainWiring(_DomainTestBase):
    """runner 领域接线：解析顺序、参数透传、hints、落库、降级。"""

    def test_user_domain_wins_over_every_inference(self):
        store = StubDomainStore([{"id": 1, "query": "自来水", "domain": "public_health",
                                  "inferred_domain": "finance"}])
        domains = FakeDomains(classify_result="technology", histogram={"finance": 9})
        provider = StubProvider([_cand("某市自来水检测异常", "https://news.example.com/1")])
        runner = self._domain_runner(store, domains, providers=[provider])
        summary = runner.scan(queries=["自来水"])
        q = summary["queries"][0]
        self.assertEqual((q["domain"], q["domain_source"]), ("public_health", "user"))
        call = domains.resolve_calls[0]
        self.assertEqual(call["user_domain"], "public_health")   # 用户设定走第 1 参
        self.assertEqual(call["row"]["id"], 1)
        self.assertEqual(call["query_text"], "自来水")
        self.assertIs(call["store"], store)
        self.assertIsNone(call["llm_fn"])
        self.assertEqual(store.inferred_writes, [])              # 命中 user 不写缓存

    def test_cached_inference_used_without_reclassify(self):
        store = StubDomainStore([{"id": 1, "query": "自来水", "domain": "",
                                  "inferred_domain": "finance",
                                  "inferred_at": "2026-11-21T10:00:00+08:00"}])
        domains = FakeDomains(classify_result="technology")
        provider = StubProvider([_cand("某市自来水检测异常", "https://news.example.com/1")])
        runner = self._domain_runner(store, domains, providers=[provider])
        summary = runner.scan(queries=["自来水"])
        q = summary["queries"][0]
        self.assertEqual((q["domain"], q["domain_source"]), ("finance", "inferred_text"))
        self.assertEqual(store.inferred_writes, [])              # 缓存命中不重写

    def test_text_classification_writes_cache(self):
        store = StubDomainStore([{"id": 1, "query": "自来水", "domain": ""}])
        domains = FakeDomains(classify_result="public_health")
        provider = StubProvider([_cand("某市自来水检测异常", "https://news.example.com/1")])
        runner = self._domain_runner(store, domains, providers=[provider])
        summary = runner.scan(queries=["自来水"])
        q = summary["queries"][0]
        self.assertEqual((q["domain"], q["domain_source"]),
                         ("public_health", "inferred_text"))
        self.assertEqual(len(store.inferred_writes), 1)          # 判定结果写缓存
        self.assertEqual(store.inferred_writes[0][:2], (1, "public_health"))

    def test_history_fallback_and_threshold(self):
        store = StubDomainStore([{"id": 1, "query": "自来水", "domain": ""}])
        domains = FakeDomains(classify_result=None, histogram={"finance": 3})
        runner = self._domain_runner(store, domains)
        summary = runner.scan(queries=["自来水"])
        self.assertEqual(summary["queries"][0]["domain_source"], "inferred_history")
        # 计数不够 history_min（默认 2）→ 未判定
        domains2 = FakeDomains(classify_result=None, histogram={"finance": 1})
        runner2 = self._domain_runner(store, domains2)
        summary2 = runner2.scan(queries=["自来水"])
        self.assertEqual((summary2["queries"][0]["domain"],
                          summary2["queries"][0]["domain_source"]), ("", "none"))

    def test_ttl_and_history_params_from_rules(self):
        store = StubDomainStore([{"id": 1, "query": "自来水", "domain": "public_health"}])
        domains = FakeDomains()
        runner = self._domain_runner(store, domains)             # TEST_RULES 无 domain 段
        runner.scan(queries=["自来水"])
        call = domains.resolve_calls[0]
        self.assertEqual(call["ttl_days"], 30.0)                 # 默认 TTL
        self.assertEqual(call["history_min"], 2)                 # 默认 history 门槛
        self.assertTrue(call["now_iso"])                         # 传了 now_iso
        rules = dict(TEST_RULES)
        rules["domain"] = {"infer_ttl_days": 7, "history_min": 3}
        runner2 = self._domain_runner(store, domains, rules=rules)
        runner2.scan(queries=["自来水"])
        call2 = domains.resolve_calls[1]
        self.assertEqual(call2["ttl_days"], 7.0)                 # rules 覆盖
        self.assertEqual(call2["history_min"], 3)

    def test_domain_hints_passed_to_agent(self):
        store = StubDomainStore([{"id": 1, "query": "热门", "domain": "public_health"}])
        domains = FakeDomains()
        agent = StubAgent()
        provider = StubProvider([_cand("热门话题", "https://news.example.com/1",
                                       snippet="阅读量 1200万")])
        runner = self._domain_runner(store, domains, providers=[provider], agent=agent)
        summary = runner.scan(queries=["热门"])
        self.assertEqual(summary["counters"]["verified"], 1)
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(agent.calls[0]["domain_hints"],
                         FakeDomains.HINTS["public_health"])     # hints 透传给 agent
        self.assertEqual(domains.hints_calls, ["public_health"])

    def test_no_domain_means_no_hints(self):
        store = StubDomainStore([{"id": 1, "query": "热门", "domain": ""}])
        domains = FakeDomains()                                  # 全空 → ("", "none")
        agent = StubAgent()
        provider = StubProvider([_cand("热门话题", "https://news.example.com/1",
                                       snippet="阅读量 1200万")])
        runner = self._domain_runner(store, domains, providers=[provider], agent=agent)
        summary = runner.scan(queries=["热门"])
        self.assertEqual(summary["counters"]["verified"], 1)
        self.assertIsNone(agent.calls[0]["domain_hints"])
        self.assertEqual(domains.hints_calls, [])                # 不查提示

    def test_entry_and_update_carry_topic_domain(self):
        store = StubDomainStore([{"id": 1, "query": "话题", "domain": "public_health"}])
        domains = FakeDomains()
        provider = StubProvider([_cand("某市自来水检测异常", "https://news.example.com/1")])
        runner = self._domain_runner(store, domains, providers=[provider])
        summary = runner.scan(queries=["话题"])
        entry = summary["entries"][0]
        self.assertEqual(entry["topic_domain"], "public_health")
        self.assertEqual(entry["topic_domain_source"], "user")
        self.assertEqual(entry["domain"], "news.example.com")    # 主机域键不受影响
        # 落库：add 收领域字段；深查回填的 update 也带领域字段
        self.assertEqual(store.feed_entries[0]["topic_domain"], "public_health")
        self.assertEqual(store.feed_entries[0]["topic_domain_source"], "user")
        upd = store.entry_updates[0]
        self.assertEqual(upd["topic_domain"], "public_health")
        self.assertEqual(upd["topic_domain_source"], "user")

    def test_domains_unavailable_means_no_domain_mode(self):
        store = StubDomainStore([{"id": 1, "query": "话题", "domain": "public_health"}])
        domains = FakeDomains()
        provider = StubProvider([_cand("某市自来水检测异常", "https://news.example.com/1")])
        runner = self._domain_runner(store, domains, providers=[provider])
        with mock.patch.object(PlatformRunner, "domains",
                               new_callable=mock.PropertyMock, return_value=None):
            summary = runner.scan(queries=["话题"])
        q = summary["queries"][0]
        self.assertEqual((q["domain"], q["domain_source"]), ("", "none"))
        entry = summary["entries"][0]
        self.assertEqual(entry["topic_domain"], "")

    def test_domain_resolution_failure_is_warning_not_crash(self):
        store = StubDomainStore([{"id": 1, "query": "话题", "domain": "public_health"}])
        domains = FakeDomains()
        domains.resolve_domain = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("domain boom"))
        provider = StubProvider([_cand("某市自来水检测异常", "https://news.example.com/1")])
        runner = self._domain_runner(store, domains, providers=[provider])
        summary = runner.scan(queries=["话题"])
        self.assertEqual(summary["status"], "completed")         # 巡检不被打断
        self.assertEqual(summary["queries"][0]["domain"], "")
        self.assertTrue(any("领域解析失败" in w for w in summary["warnings"]))

    def test_domains_property_lazy_and_cached(self):
        """未注入 domains：property 惰性 import 真模块并缓存（模块在场返模块，
        不在场返 None）——两种落地状态都不抛、不重复 import。"""
        runner = self._domain_runner(StubDomainStore(), None)
        first = runner.domains
        self.assertIs(runner.domains, first)

    def test_legacy_store_without_topic_domain_still_scans(self):
        """数据层 v7 落地前的老 store：add 不收 topic_domain（不硬传），
        update 多带的字段被老白名单忽略——巡检其余流程不变。"""
        store = LegacyFeedStore([{"id": 1, "query": "话题", "domain": "public_health"}])
        domains = FakeDomains()
        provider = StubProvider([_cand("某市自来水检测异常", "https://news.example.com/1")])
        runner = self._domain_runner(store, domains, providers=[provider])
        summary = runner.scan(queries=["话题"])
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(store.feed_entries[0]["topic_domain"], "")
        self.assertEqual(store.legacy_add_extras, {})          # 没把新字段硬传给老签名


class TestPatrolProviderInRunner(_RunnerTestBase):
    def test_default_providers_include_patrol(self):
        """缺省装配：search + rss + patrol（store 一定在场）。"""
        store = PlatformStore(self.db)
        self.addCleanup(store.close)
        dossiers = DossierService(store=DossierStore(self.db), privacy=PREFS_ON)
        self.addCleanup(dossiers.close)
        rules = dict(TEST_RULES)
        rules["rss_sources"] = [{"name": "s", "url": "https://f.example.com/rss"}]
        runner = PlatformRunner(store=store, rules=rules, privacy=PREFS_ON,
                                dossiers=dossiers, agent=StubAgent())
        self.assertEqual([p.name for p in runner.providers],
                         ["search", "rss", "patrol"])
        self.assertIsInstance(runner.providers[-1], PatrolSourceProvider)


if __name__ == "__main__":
    unittest.main(verbosity=2)
