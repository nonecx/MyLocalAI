# -*- coding: utf-8 -*-
"""平台领域表测试（platform/domains.py + platform_domains.json）。

全程离线：LLM 一律打桩（llm_fn(system, user) -> (obj, err)），store 用假对象，
不联网、不调任何真实 API。

锁定四件事：
  1. 加载形状——默认表 12 个领域、字段齐全、search_hints 是裸 host；坏 JSON /
     坏形状 → 退内置默认（id 不变）+ 中文告警；环境变量整套替换/增量新增；
  2. 查询——domain_ids / domain_name / domain_hints 的越界行为（永不抛）；
  3. classify_domain 白名单——合法 id 放行；自造领域、空串、err、异常、空文本
     一律 None（**LLM 不可用不猜领域**）；
  4. resolve_domain 四级链——user > TTL 缓存 > LLM（写回缓存）> 历史直方图，
     以及各级失败时的 ("", "none")。

运行（项目根目录下）：  ./.venv/Scripts/python.exe -m unittest tests.test_platform_domains -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.platform import domains as D  # noqa: E402

#: 与 assistant/skills/taxonomy.json 对齐的 12 个领域 id（顺序即表内声明顺序）
EXPECTED_IDS = ["public_health", "food_safety", "technology", "finance", "policy_law",
                "education", "social_events", "environment", "history", "international",
                "celebrity_entertainment", "other"]

NOW_ISO = "2026-11-21T10:00:00+08:00"


def _json_text(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def _stub_llm(obj, err=""):
    """固定返回的 llm_fn 桩（记录收到的 system/user 与调用次数，供断言）。"""
    seen = {"system": "", "user": "", "calls": 0}

    def fn(system, user):
        seen["system"], seen["user"] = system, user
        seen["calls"] += 1
        return obj, err
    fn.seen = seen          # type: ignore[attr-defined]
    return fn


class _StubStore:
    """假 store：记录缓存写回，按给定直方图作答。"""

    def __init__(self, histogram=None, fail_write=False, no_histogram=False):
        self.histogram = histogram if histogram is not None else {}
        self.fail_write = fail_write
        self.no_histogram = no_histogram
        self.writes = []

    def set_inferred_domain(self, query_id, domain, now_iso):
        if self.fail_write:
            raise RuntimeError("db down")
        self.writes.append((query_id, domain, now_iso))

    def domain_histogram(self):
        if self.no_histogram:
            raise AttributeError("no histogram")   # 方法缺失由 getattr 兜住，这里测异常
        return dict(self.histogram)


class TestLoadDomains(unittest.TestCase):
    def test_default_table_shape_and_ids(self):
        table, warnings = D.load_domains()
        self.assertEqual(warnings, [], "仓库内置领域表应当是干净的")
        self.assertEqual(table["version"], 1)
        domains = table["domains"]
        self.assertEqual([d["id"] for d in domains], EXPECTED_IDS)
        for d in domains:
            for key in ("id", "name", "definition", "examples", "search_hints"):
                self.assertIn(key, d, f"{d.get('id')} 缺少 {key}")
            self.assertTrue(d["name"])
            self.assertTrue(d["definition"])
            self.assertIsInstance(d["examples"], list)
            self.assertIsInstance(d["search_hints"], list)
            for hint in d["search_hints"]:
                self.assertNotIn("://", hint, "search_hints 只放裸 host")
                self.assertNotIn("/", hint, "search_hints 只放裸 host")

    def test_broken_json_falls_back_to_default_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.json"
            bad.write_text("{ 这不是 JSON", encoding="utf-8")
            with mock.patch.dict(os.environ, {"PLATFORM_DOMAINS_PATH": str(bad)}):
                table, warnings = D.load_domains()
        self.assertEqual([d["id"] for d in table["domains"]], EXPECTED_IDS)
        self.assertTrue(warnings, "坏配置必须留告警，不静默生效")
        self.assertTrue(any("内置默认" in w for w in warnings))

    def test_bad_shape_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "shape.json"
            bad.write_text('{"domains": "不是一个数组"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"PLATFORM_DOMAINS_PATH": str(bad)}):
                table, warnings = D.load_domains()
        self.assertEqual([d["id"] for d in table["domains"]], EXPECTED_IDS)
        self.assertTrue(any("domains" in w for w in warnings))

    def test_duplicate_id_keeps_first_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dup.json"
            path.write_text('{"domains": ['
                            '{"id": "finance", "name": "财经", "search_hints": ["gov.cn"]},'
                            '{"id": "finance", "name": "财经二", "search_hints": ["cctv.com"]}'
                            ']}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"PLATFORM_DOMAINS_PATH": str(path)}):
                table, warnings = D.load_domains()
        self.assertEqual([d["id"] for d in table["domains"]], ["finance"])
        self.assertEqual(table["domains"][0]["name"], "财经", "先声明者生效")
        self.assertTrue(any("重复" in w for w in warnings))

    def test_full_replacement_env(self):
        custom = {"version": 9, "domains": [
            {"id": "finance", "name": "财经", "definition": "钱", "examples": ["股市"],
             "search_hints": ["gov.cn"]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.json"
            path.write_text(_json_text(custom), encoding="utf-8")
            with mock.patch.dict(os.environ, {"PLATFORM_DOMAINS_PATH": str(path)}):
                self.assertEqual(D.domain_ids(), ["finance"])
                self.assertEqual(D.domain_hints("finance"), ["gov.cn"])
                table, _w = D.load_domains()
        self.assertEqual(table["version"], 9)

    def test_extra_path_adds_new_domain_only(self):
        extra = {"domains": [{"id": "sports", "name": "体育", "definition": "赛事",
                              "examples": [], "search_hints": ["gov.cn"]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extra.json"
            path.write_text(_json_text(extra), encoding="utf-8")
            with mock.patch.dict(os.environ, {"PLATFORM_DOMAINS_EXTRA_PATH": str(path)}):
                ids = D.domain_ids()
        self.assertIn("sports", ids, "增量文件应能新增领域")
        self.assertIn("public_health", ids, "增量文件不动默认表其余领域")


class TestDomainQueries(unittest.TestCase):
    def test_lookup_and_out_of_range(self):
        self.assertEqual(D.domain_name("public_health"), "公共卫生")
        self.assertEqual(D.domain_name("other"), "其他")
        self.assertEqual(D.domain_name("不存在的领域"), "")
        self.assertEqual(D.domain_name(""), "")
        self.assertEqual(D.domain_name(None), "")
        self.assertEqual(D.domain_hints("public_health"),
                         ["nhc.gov.cn", "chinacdc.cn", "piyao.org.cn"])
        self.assertEqual(D.domain_hints("other"), [], "other 不给定向提示")
        self.assertEqual(D.domain_hints("不存在的领域"), [])
        self.assertEqual(D.domain_hints(""), [])
        self.assertEqual(D.domain_hints(None), [])

    def test_queries_never_raise_on_odd_input(self):
        for bad in (None, "", "   ", 123, ["x"], {"a": 1}):
            self.assertIsInstance(D.domain_name(bad), str)
            self.assertIsInstance(D.domain_hints(bad), list)


class TestClassifyDomain(unittest.TestCase):
    def test_valid_id_passes(self):
        self.assertEqual(
            D.classify_domain("网传某疫苗会导致不孕不育", _stub_llm({"domain": "public_health"})),
            "public_health")

    def test_self_invented_domain_rejected(self):
        self.assertIsNone(D.classify_domain("某文本", _stub_llm({"domain": "space_news"})))
        self.assertIsNone(D.classify_domain("某文本", _stub_llm({"domain": "公共卫生"})))

    def test_empty_domain_means_cannot_tell_returns_none(self):
        self.assertIsNone(D.classify_domain("某文本", _stub_llm({"domain": ""})))

    def test_llm_error_returns_none(self):
        self.assertIsNone(D.classify_domain(
            "某文本", _stub_llm(None, "API 调用失败: ConnectionError")))

    def test_llm_exception_returns_none(self):
        def boom(system, user):
            raise RuntimeError("net down")
        self.assertIsNone(D.classify_domain("某文本", boom))

    def test_non_dict_object_rejected(self):
        self.assertIsNone(D.classify_domain("某文本", _stub_llm(["public_health"])))

    def test_empty_text_returns_none(self):
        fn = _stub_llm({"domain": "finance"})
        for text in ("", "   ", None):
            self.assertIsNone(D.classify_domain(text, fn))

    def test_prompt_is_closed_enum_listing_all_domains(self):
        fn = _stub_llm({"domain": "other"})
        D.classify_domain("某文本", fn)
        system = fn.seen["system"]                  # type: ignore[attr-defined]
        for did in EXPECTED_IDS:
            self.assertIn(did, system, f"提示词应列出 {did}")
        self.assertIn("其他", system)
        self.assertIn('{"domain"', system)
        self.assertIn("不要自造领域", system)

    def test_default_llm_fn_adapts_chat_json(self):
        with mock.patch("mylocalai.llm.llm_client.chat_json",
                        return_value=({"domain": "finance"}, "")) as j:
            obj, err = D._default_llm_fn("s", "u")
        self.assertEqual(obj, {"domain": "finance"})
        self.assertEqual(err, "")
        self.assertEqual(j.call_args.kwargs.get("namespace"), "platform_domain")
        self.assertEqual(j.call_args.kwargs.get("temperature"), 0)
        self.assertEqual(j.call_args.kwargs.get("max_tokens"), 200)

    def test_default_llm_fn_survives_call_exception(self):
        with mock.patch("mylocalai.llm.llm_client.chat_json",
                        side_effect=RuntimeError("no key")):
            obj, err = D._default_llm_fn("s", "u")
        self.assertIsNone(obj)
        self.assertIn("no key", err)

    def test_default_llm_fn_survives_import_failure(self):
        with mock.patch.dict(sys.modules, {"mylocalai.llm.llm_client": None}):
            obj, err = D._default_llm_fn("s", "u")
        self.assertIsNone(obj)
        self.assertTrue(err)


class TestResolveDomain(unittest.TestCase):
    def test_user_domain_wins(self):
        store = _StubStore(histogram={"finance": 9})
        fn = _stub_llm({"domain": "education"})
        got = D.resolve_domain("technology", "某查询", {"id": "q1"}, store, fn,
                               now_iso=NOW_ISO)
        self.assertEqual(got, ("technology", "user"))
        self.assertEqual(store.writes, [], "user 已定夺，不再调 LLM/写缓存")
        self.assertEqual(fn.seen["calls"], 0, "user 已定夺，不调 LLM")  # type: ignore[attr-defined]

    def test_invalid_user_domain_falls_through(self):
        store = _StubStore(histogram={"finance": 5})
        got = D.resolve_domain("自造领域", "", None, store, None, now_iso=NOW_ISO)
        self.assertEqual(got, ("finance", "inferred_history"))

    def test_cache_hit_within_ttl(self):
        store = _StubStore()
        fn = _stub_llm({"domain": "education"})
        row = {"id": "q1", "inferred_domain": "technology",
               "inferred_at": "2026-11-16T10:00:00+08:00"}       # 5 天前
        got = D.resolve_domain("", "某查询", row, store, fn, now_iso=NOW_ISO)
        self.assertEqual(got, ("technology", "inferred_text"))
        self.assertEqual(store.writes, [], "缓存命中不写缓存")
        self.assertEqual(fn.seen["calls"], 0, "缓存命中不调 LLM")  # type: ignore[attr-defined]

    def test_cache_miss_when_ttl_expired(self):
        store = _StubStore()
        fn = _stub_llm({"domain": "education"})
        row = {"id": "q1", "inferred_domain": "technology",
               "inferred_at": "2026-09-01T10:00:00+08:00"}       # 81 天前
        got = D.resolve_domain("", "某查询", row, store, fn, now_iso=NOW_ISO)
        self.assertEqual(got, ("education", "inferred_text"))
        self.assertEqual(store.writes, [("q1", "education", NOW_ISO)])

    def test_cache_miss_when_inferred_at_unparseable(self):
        store = _StubStore()
        fn = _stub_llm({"domain": "education"})
        row = {"id": "q1", "inferred_domain": "technology", "inferred_at": "上周"}
        got = D.resolve_domain("", "某查询", row, store, fn, now_iso=NOW_ISO)
        self.assertEqual(got, ("education", "inferred_text"))

    def test_cache_miss_when_inferred_domain_unknown(self):
        store = _StubStore()
        fn = _stub_llm({"domain": "education"})
        row = {"id": "q1", "inferred_domain": "space_news",
               "inferred_at": "2026-11-20T10:00:00+08:00"}
        got = D.resolve_domain("", "某查询", row, store, fn, now_iso=NOW_ISO)
        self.assertEqual(got, ("education", "inferred_text"), "脏缓存值不穿透")

    def test_cache_miss_when_now_iso_unparseable(self):
        store = _StubStore()
        fn = _stub_llm({"domain": "education"})
        row = {"id": "q1", "inferred_domain": "technology",
               "inferred_at": "2026-11-20T10:00:00+08:00"}
        got = D.resolve_domain("", "某查询", row, store, fn, now_iso="上周")
        self.assertEqual(got, ("education", "inferred_text"))

    def test_llm_success_writes_cache_back(self):
        store = _StubStore()
        fn = _stub_llm({"domain": "food_safety"})
        got = D.resolve_domain("", "某查询", {"id": "q7"}, store, fn, now_iso=NOW_ISO)
        self.assertEqual(got, ("food_safety", "inferred_text"))
        self.assertEqual(store.writes, [("q7", "food_safety", NOW_ISO)])

    def test_llm_success_with_none_store_or_row(self):
        got = D.resolve_domain("", "某查询", None, None, _stub_llm({"domain": "history"}),
                               now_iso=NOW_ISO)
        self.assertEqual(got, ("history", "inferred_text"))
        got2 = D.resolve_domain("", "某查询", {}, None, _stub_llm({"domain": "history"}),
                                now_iso=NOW_ISO)
        self.assertEqual(got2, ("history", "inferred_text"))

    def test_store_write_failure_does_not_block(self):
        store = _StubStore(fail_write=True)
        got = D.resolve_domain("", "某查询", {"id": "q7"}, store,
                               _stub_llm({"domain": "finance"}), now_iso=NOW_ISO)
        self.assertEqual(got, ("finance", "inferred_text"))

    def test_history_used_when_llm_unavailable(self):
        store = _StubStore(histogram={"public_health": 3, "finance": 1})
        got = D.resolve_domain("", "某查询", None, store,
                               _stub_llm(None, "API 调用失败: ConnectionError"),
                               now_iso=NOW_ISO)
        self.assertEqual(got, ("public_health", "inferred_history"))
        self.assertEqual(store.writes, [])

    def test_history_below_threshold_is_none(self):
        store = _StubStore(histogram={"public_health": 1})
        got = D.resolve_domain("", "某查询", None, store,
                               _stub_llm(None, "down"), now_iso=NOW_ISO)
        self.assertEqual(got, ("", "none"))

    def test_history_min_configurable(self):
        store = _StubStore(histogram={"public_health": 1})
        got = D.resolve_domain("", "某查询", None, store, _stub_llm(None, "down"),
                               now_iso=NOW_ISO, history_min=1)
        self.assertEqual(got, ("public_health", "inferred_history"))

    def test_history_ignores_unknown_and_non_numeric(self):
        store = _StubStore(histogram={"space_news": 9, "finance": "很多"})
        got = D.resolve_domain("", "某查询", None, store, _stub_llm(None, "down"),
                               now_iso=NOW_ISO)
        self.assertEqual(got, ("", "none"))

    def test_all_empty_is_none(self):
        store = _StubStore(histogram={"finance": 1})
        got = D.resolve_domain("", "", None, store, _stub_llm({"domain": ""}),
                               now_iso=NOW_ISO)
        self.assertEqual(got, ("", "none"))

    def test_histogram_unavailable_is_none(self):
        store = _StubStore(no_histogram=True)
        got = D.resolve_domain("", "某查询", None, store, _stub_llm(None, "down"),
                               now_iso=NOW_ISO)
        self.assertEqual(got, ("", "none"))

    def test_no_store_no_llm_is_none(self):
        self.assertEqual(D.resolve_domain("", "某查询", None, None, None,
                                          now_iso=NOW_ISO), ("", "none"))

    def test_empty_now_iso_uses_current_time(self):
        # 不带 now_iso：1 小时前的缓存应仍在 TTL 内，400 天前的应过期
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(hours=1)).isoformat()
        stale = (now - timedelta(days=400)).isoformat()
        row_fresh = {"id": "q", "inferred_domain": "finance", "inferred_at": fresh}
        row_stale = {"id": "q", "inferred_domain": "finance", "inferred_at": stale}
        self.assertEqual(D.resolve_domain("", "x", row_fresh, None, None),
                         ("finance", "inferred_text"))
        self.assertEqual(D.resolve_domain("", "x", row_stale, None,
                                          _stub_llm({"domain": "education"})),
                         ("education", "inferred_text"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
