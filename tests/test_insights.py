# -*- coding: utf-8 -*-
"""L4 参谋聚合层测试（insights）：话术模式库 / 信源画像 / 达标信源 / 个人信息环境周报。

全部纯函数、全离线临时库（TemporaryDirectory），零 LLM、零网络：
  - pattern_library：计数 / by_label 取最新 version / hits 空的类目不出现 /
    share 两位小数 / common_pairs 阈值与排序 / 空库全零不抛；
  - source_profiles：url 噪声（path/query/端口/www/大小写）归一、同域名去重、
    tier 众数（并列取字母序第一）、refute/support 计数、refuted_dossiers 只数
    高风险/疑似档案、last_seen 取 max、排序、空库 []、解析不出域名的条目跳过；
  - qualified_source_profile：达标线边界（==min 入选、<min 不选、空列表 {}）；
  - weekly_digest：窗口闭区间边界（恰好等于 start 算窗内、start 前一小时不算）、
    各子计数与汇总、全零窗口、now 非法串不抛。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.dossier.insights import (  # noqa: E402
    pattern_library, qualified_source_profile, source_profiles, weekly_digest,
)
from mylocalai.dossier.store import DossierStore  # noqa: E402
from mylocalai.rumor.verdict import LABEL_HIGH, LABEL_LOW, LABEL_SUSPECT  # noqa: E402

#: 周报测试用的固定"当前时刻"（days=7 → start=2026-10-24T12:00:00+08:00）
NOW = "2026-10-31T12:00:00+08:00"
START = "2026-10-24T12:00:00+08:00"
BEFORE_START = "2026-10-24T11:00:00+08:00"


class InsightsTestBase(unittest.TestCase):
    """公共脚手架：独立临时库 + 可控时间戳的直接 SQL 预置（窗口测试需要固定时间）。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = DossierStore(Path(self.tmp.name) / "dossier.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # ---- 直接 SQL 预置（走 _conn 同包私有访问先例；都包在 with 里自动提交） ----

    def add_dossier(self, title="t", created_at=NOW, input_kind="text", input_text="x") -> int:
        with self.store._conn:
            cur = self.store._conn.execute(
                "INSERT INTO dossiers (created_at, updated_at, title, input_text, input_kind)"
                " VALUES (?, ?, ?, ?, ?)",
                (created_at, created_at, title, input_text, input_kind))
            return int(cur.lastrowid)

    def add_conclusion(self, did, label, created_at=NOW) -> int:
        with self.store._conn:
            row = self.store._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM conclusions"
                " WHERE dossier_id = ?", (did,)).fetchone()
            version = int(row["v"]) + 1
            self.store._conn.execute(
                "INSERT INTO conclusions (dossier_id, version, label, created_at)"
                " VALUES (?, ?, ?, ?)", (did, version, label, created_at))
            return version

    def add_marker(self, did, risk, cred=None, marker_points=0, credibility_points=0,
                   created_at=NOW) -> None:
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO dossier_markers (dossier_id, risk_hits, credibility_hits,"
                " marker_points, credibility_points, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (did, json.dumps(risk, ensure_ascii=False),
                 json.dumps(cred or {}, ensure_ascii=False),
                 marker_points, credibility_points, created_at))

    def add_evidence(self, did, url, stance, tier="", fetched_at=NOW) -> None:
        self.store.add_evidence(did, 1, [{"url": url, "domain_tier": tier, "stance": stance,
                                          "fetched_at": fetched_at}])

    def add_run(self, did, kind="verify", degraded=False, requests=0, tokens=0,
                created_at=NOW) -> None:
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO analysis_runs (dossier_id, kind, status, degraded, duration_s,"
                " requests, tokens, created_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (did, kind, "degraded" if degraded else "ok", 1 if degraded else 0,
                 requests, tokens, created_at))

    def add_tool_run(self, session_id, requests=0, tokens=0, created_at=NOW) -> None:
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO tool_runs (session_id, tool, requests, tokens, created_at)"
                " VALUES (?, 'search', ?, ?, ?)", (session_id, requests, tokens, created_at))

    def add_session(self, created_at=NOW) -> int:
        with self.store._conn:
            cur = self.store._conn.execute(
                "INSERT INTO sessions (title, created_at, updated_at) VALUES ('s', ?, ?)",
                (created_at, created_at))
            return int(cur.lastrowid)

    def add_snapshot(self, did, taken_at=NOW) -> None:
        self.store.add_snapshot(did, 0.0, 0.0, 0.0, 0.0, taken_at=taken_at)

    def add_fingerprint(self, did, normalized="x", created_at=NOW) -> None:
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO claim_fingerprints (dossier_id, normalized, shingles, label,"
                " created_at) VALUES (?, ?, '[]', '', ?)", (did, normalized, created_at))


class TestPatternLibrary(InsightsTestBase):
    def seed(self):
        """三档案：d1 高风险（两类话术+一个空 hits 类目）、d2 最新结论高风险（两版）、
        d3 低风险（一类话术+一条可信度线索）。source_vague 三档案全中。"""
        d1 = self.store.create_dossier("d1", "x")
        self.store.add_conclusion(d1, LABEL_HIGH, None)
        self.add_marker(d1, {"source_vague": {"label": "来源模糊", "hits": ["听说"], "points": 10},
                             "fear_claim": {"label": "恐吓诉求", "hits": ["不敢说"], "points": 12},
                             "empty_cat": {"label": "空类目", "hits": [], "points": 0}})
        d2 = self.store.create_dossier("d2", "x")
        self.store.add_conclusion(d2, LABEL_SUSPECT, None)
        self.store.add_conclusion(d2, LABEL_HIGH, None)    # v2 → 最新
        self.add_marker(d2, {"source_vague": {"label": "来源模糊", "hits": ["听说"], "points": 10},
                             "fear_claim": {"label": "恐吓诉求", "hits": ["速转"], "points": 12}})
        d3 = self.store.create_dossier("d3", "x")
        self.store.add_conclusion(d3, LABEL_LOW, None)
        self.add_marker(d3, {"source_vague": {"label": "来源模糊", "hits": ["据说"], "points": 10}},
                        cred={"official_channel": {"label": "官方渠道线索",
                                                   "hits": ["gov.cn"], "points": -8}})
        return d1, d2, d3

    def test_pattern_library_full_fields(self):
        """预置数据全字段断言：计数、by_label 取最新 version、hits 空类目不出现、
        share 两位小数、排序（dossiers 降序）、common_pairs 组合与标签。"""
        self.seed()
        lib = pattern_library(self.store)
        self.assertEqual(lib["total_dossiers"], 3)
        self.assertEqual(lib["with_markers"], 3)
        self.assertEqual(lib["by_label"], {LABEL_HIGH: 2, LABEL_LOW: 1})
        # risk_patterns：只统计 hits 非空的类目；按 dossiers 降序
        risk = lib["risk_patterns"]
        self.assertEqual([p["key"] for p in risk], ["source_vague", "fear_claim"])
        vague, fear = risk
        self.assertEqual((vague["label"], vague["dossiers"], vague["share"]),
                         ("来源模糊", 3, 1.0))
        self.assertEqual(vague["by_label"], {LABEL_HIGH: 2, LABEL_LOW: 1})
        self.assertEqual((fear["label"], fear["dossiers"], fear["share"]),
                         ("恐吓诉求", 2, 0.67))
        self.assertEqual(fear["by_label"], {LABEL_HIGH: 2})
        self.assertNotIn("empty_cat", [p["key"] for p in risk])   # hits 空的类目不出现
        # credibility_patterns 同构
        cred = lib["credibility_patterns"]
        self.assertEqual(len(cred), 1)
        self.assertEqual((cred[0]["key"], cred[0]["dossiers"], cred[0]["share"]),
                         ("official_channel", 1, 0.33))
        self.assertEqual(cred[0]["by_label"], {LABEL_LOW: 1})
        # common_pairs：同档案同时命中的组合，dossiers≥2 才收录；keys 升序
        pairs = lib["common_pairs"]
        self.assertEqual(pairs, [{"keys": ["fear_claim", "source_vague"],
                                  "labels": ["恐吓诉求", "来源模糊"], "dossiers": 2}])

    def test_pattern_library_empty_db_all_zero(self):
        """空库：全零结构、合法输出、不抛。"""
        lib = pattern_library(self.store)
        self.assertEqual(lib, {"total_dossiers": 0, "with_markers": 0, "by_label": {},
                               "risk_patterns": [], "credibility_patterns": [],
                               "common_pairs": []})

    def test_pattern_library_markers_without_conclusions(self):
        """只有 marker 行、没有结论：total_dossiers=0、by_label 空，模式仍按命中档案计数。"""
        did = self.store.create_dossier("d", "x")
        self.add_marker(did, {"source_vague": {"label": "来源模糊", "hits": ["听说"], "points": 10}})
        lib = pattern_library(self.store)
        self.assertEqual(lib["total_dossiers"], 0)
        self.assertEqual(lib["with_markers"], 1)
        self.assertEqual(lib["by_label"], {})
        self.assertEqual(lib["risk_patterns"][0]["dossiers"], 1)
        self.assertEqual(lib["risk_patterns"][0]["share"], 1.0)
        self.assertEqual(lib["risk_patterns"][0]["by_label"], {})

    def test_pattern_library_common_pairs_under_threshold_excluded(self):
        """组合只在单档案出现（dossiers=1）→ 不收录。"""
        did = self.store.create_dossier("d", "x")
        self.add_marker(did, {"a": {"label": "甲", "hits": ["x"], "points": 5},
                              "b": {"label": "乙", "hits": ["y"], "points": 5}})
        self.assertEqual(pattern_library(self.store)["common_pairs"], [])


class TestSourceProfiles(InsightsTestBase):
    def test_source_profiles_url_noise_dedup_and_tier_mode(self):
        """url 带 www/大小写/path/query/端口噪声都归一到同一域名；tier 取众数；
        refute/support 条数、last_seen 取 max。"""
        did = self.store.create_dossier("d", "x")
        self.store.add_conclusion(did, LABEL_HIGH, None)
        self.add_evidence(did, "https://www.Example.com/a?x=1", "反驳", tier="官方权威",
                          fetched_at="2026-10-01T08:00:00+08:00")
        self.add_evidence(did, "http://example.com:8080/b", "支持", tier="自媒体/UGC",
                          fetched_at="2026-10-03T08:00:00+08:00")
        self.add_evidence(did, "https://example.com/c#frag", "无关", tier="官方权威",
                          fetched_at="2026-10-02T08:00:00+08:00")
        profiles = source_profiles(self.store)
        self.assertEqual(len(profiles), 1)
        p = profiles[0]
        self.assertEqual(p["domain"], "example.com")
        self.assertEqual(p["tier"], "官方权威")            # 2:1 众数
        self.assertEqual(p["total_entries"], 3)
        self.assertEqual((p["refute_entries"], p["support_entries"]), (1, 1))
        self.assertEqual(p["last_seen"], "2026-10-03T08:00:00+08:00")   # 取 max
        self.assertEqual(p["refuted_dossiers"], 1)          # 最新结论=高风险 → 入数
        self.assertFalse(p["qualified"])                    # 只反驳过 1 个档案，未达内置线 2

    def test_source_profiles_tier_tie_alphabetical(self):
        """tier 计数并列 → 取字母序第一个（"自媒体/UGC" < "门户"，按码点比较）。"""
        did = self.store.create_dossier("d", "x")
        self.add_evidence(did, "https://a.example.com/x", "无关", tier="门户")
        self.add_evidence(did, "https://a.example.com/y", "无关", tier="自媒体/UGC")
        self.assertEqual(source_profiles(self.store)[0]["tier"], "自媒体/UGC")

    def test_source_profiles_refuted_dossiers_only_high_or_suspect(self):
        """refuted_dossiers 只数最新结论为 高风险/疑似 的档案；supporting_dossiers
        只数最新结论为低风险的档案；排序 refuted 降序。"""
        d_high = self.add_dossier("high")
        self.add_conclusion(d_high, LABEL_HIGH)
        d_sus = self.add_dossier("sus")
        self.add_conclusion(d_sus, LABEL_SUSPECT)
        d_low1 = self.add_dossier("low1")
        self.add_conclusion(d_low1, LABEL_LOW)
        d_low2 = self.add_dossier("low2")
        self.add_conclusion(d_low2, LABEL_LOW)
        # bad.example.com：反驳过高风险+疑似档案 → 战绩 2，达标
        self.add_evidence(d_high, "https://bad.example.com/a", "反驳")
        self.add_evidence(d_sus, "https://bad.example.com/b", "反驳")
        # good.example.com：反驳的档案最新结论都是低风险 → 不算战绩；支持过低风险档案
        self.add_evidence(d_low1, "https://good.example.com/a", "反驳")
        self.add_evidence(d_low2, "https://good.example.com/b", "反驳")
        self.add_evidence(d_low1, "https://good.example.com/c", "支持")
        profiles = source_profiles(self.store)
        self.assertEqual([p["domain"] for p in profiles], ["bad.example.com", "good.example.com"])
        bad, good = profiles
        self.assertEqual((bad["refuted_dossiers"], bad["refute_entries"], bad["qualified"]),
                         (2, 2, True))
        self.assertEqual((good["refuted_dossiers"], good["supporting_dossiers"],
                          good["support_entries"], good["qualified"]), (0, 1, 1, False))

    def test_source_profiles_empty_db(self):
        """空库 → []。"""
        self.assertEqual(source_profiles(self.store), [])

    def test_source_profiles_unparsable_url_skipped(self):
        """url 解析不出域名的条目跳过（domain 列恒空串，一律从 url 现算）。"""
        did = self.store.create_dossier("d", "x")
        self.store.add_evidence(did, 1, [
            {"url": "not a url", "domain": "should-be-ignored", "stance": "反驳"},
            {"url": "http://", "stance": "反驳"},
            {"url": "", "stance": "反驳"},
        ])
        self.assertEqual(source_profiles(self.store), [])


class TestQualifiedSourceProfile(unittest.TestCase):
    def test_threshold_boundaries(self):
        """达标线边界：refuted_dossiers==2 且 min_refutes=2 → 入选；1 → 不入选；
        min_refutes=3 时 2 不入选；空列表 → {}。"""
        profiles = [{"domain": "a.com", "refuted_dossiers": 2},
                    {"domain": "b.com", "refuted_dossiers": 1}]
        self.assertEqual(qualified_source_profile(profiles), {"a.com": {"refutes": 2}})
        self.assertEqual(qualified_source_profile(profiles, min_refutes=3), {})
        self.assertEqual(qualified_source_profile([]), {})


class TestWeeklyDigest(InsightsTestBase):
    def test_window_boundary_inclusive_start_and_end(self):
        """窗口 [start, end] 双端闭区间：恰好等于 start 的档案算窗内；start 前一
        小时的不算；恰好等于 end（=now）的结论算窗内。"""
        in_did = self.add_dossier("in", created_at=START)
        self.add_dossier("out", created_at=BEFORE_START)
        self.add_conclusion(in_did, LABEL_SUSPECT, created_at=NOW)      # == end
        digest = weekly_digest(self.store, NOW, days=7)
        self.assertEqual(digest["period"], {"start": START, "end": NOW, "days": 7})
        self.assertEqual(digest["as_of"], NOW)
        self.assertEqual(digest["dossiers"]["new_count"], 1)
        self.assertEqual(digest["conclusions"]["count"], 1)
        self.assertEqual(digest["conclusions"]["by_label"], {LABEL_SUSPECT: 1})

    def test_full_counts_and_sums(self):
        """窗口内各子计数与汇总：建档/结论/运行（含降级与请求、token 求和）/
        工具运行/话术模式 top/反驳域名 top/热度快照/声明指纹；窗外数据一律不计。"""
        d1 = self.add_dossier("d1", input_kind="text")
        d2 = self.add_dossier("d2", input_kind="topic")
        self.add_conclusion(d1, LABEL_SUSPECT)
        self.add_conclusion(d2, LABEL_LOW)
        self.add_run(d1, kind="verify", requests=3, tokens=100)
        self.add_run(d1, kind="verify", degraded=True, requests=2, tokens=50)
        self.add_run(d2, kind="recheck", requests=1, tokens=10)
        self.add_run(d1, kind="verify", requests=99, tokens=99, created_at=BEFORE_START)  # 窗外
        self.add_tool_run(self.add_session(created_at=START), requests=2, tokens=20)
        self.add_tool_run(self.add_session(created_at=START), requests=1, tokens=5)
        self.add_marker(d1, {"source_vague": {"label": "来源模糊", "hits": ["听说"], "points": 10},
                             "fear_claim": {"label": "恐吓诉求", "hits": ["速转"], "points": 12}},
                        marker_points=22)
        self.add_marker(d2, {"source_vague": {"label": "来源模糊", "hits": ["据说"], "points": 10}})
        d_out = self.add_dossier("out", created_at=BEFORE_START)
        self.add_marker(d_out, {"source_vague": {"label": "来源模糊", "hits": ["听说"], "points": 10}},
                        created_at=BEFORE_START)                            # 窗外（一档案一行）
        self.add_evidence(d1, "https://a.example.com/x", "反驳")
        self.add_evidence(d1, "https://a.example.com/y", "反驳")
        self.add_evidence(d2, "https://b.example.com/z", "反驳")
        self.add_snapshot(d1, taken_at=NOW)
        self.add_snapshot(d1, taken_at=BEFORE_START)                        # 窗外
        self.add_fingerprint(d1, normalized="窗内说法")
        self.add_fingerprint(d1, normalized="窗外说法", created_at=BEFORE_START)

        digest = weekly_digest(self.store, NOW, days=7)
        self.assertEqual(digest["dossiers"],
                         {"new_count": 2, "by_input_kind": {"text": 1, "topic": 1}})
        self.assertEqual(digest["conclusions"]["count"], 2)
        self.assertEqual(digest["conclusions"]["by_label"],
                         {LABEL_SUSPECT: 1, LABEL_LOW: 1})
        self.assertEqual(digest["runs"], {"count": 3, "degraded_count": 1,
                                          "by_kind": {"verify": 2, "recheck": 1},
                                          "requests": 6, "tokens": 160})
        self.assertEqual(digest["tool_runs"], {"count": 2, "requests": 3, "tokens": 25})
        top = digest["patterns"]["top"]
        self.assertEqual(top, [{"key": "source_vague", "label": "来源模糊", "dossiers": 2},
                               {"key": "fear_claim", "label": "恐吓诉求", "dossiers": 1}])
        self.assertEqual(digest["sources"]["top_refute"],
                         [{"domain": "a.example.com", "refute_entries": 2},
                          {"domain": "b.example.com", "refute_entries": 1}])
        self.assertEqual(digest["heat"]["snapshot_count"], 1)
        self.assertEqual(digest["fingerprints"]["count"], 1)

    def test_zero_window_far_future(self):
        """窗口内无数据：各计数为 0、列表为空，结构完整。"""
        self.add_dossier("d", created_at=NOW)
        digest = weekly_digest(self.store, "2027-06-01T00:00:00+08:00", days=7)
        self.assertEqual(digest["period"], {"start": "2027-05-25T00:00:00+08:00",
                                            "end": "2027-06-01T00:00:00+08:00", "days": 7})
        self.assertEqual(digest["dossiers"]["new_count"], 0)
        self.assertEqual(digest["conclusions"], {"count": 0, "by_label": {}})
        self.assertEqual(digest["runs"], {"count": 0, "degraded_count": 0, "by_kind": {},
                                          "requests": 0, "tokens": 0})
        self.assertEqual(digest["tool_runs"], {"count": 0, "requests": 0, "tokens": 0})
        self.assertEqual(digest["patterns"]["top"], [])
        self.assertEqual(digest["sources"]["top_refute"], [])
        self.assertEqual(digest["heat"]["snapshot_count"], 0)
        self.assertEqual(digest["fingerprints"]["count"], 0)

    def test_invalid_now_returns_zero_structure_without_raise(self):
        """now 非法串 / None：不抛，返回 period 为空的全零结构。"""
        for bad in ("not-a-date", "", None):
            digest = weekly_digest(self.store, bad)
            self.assertEqual(digest["period"], {"start": "", "end": "", "days": 7})
            self.assertEqual(digest["dossiers"]["new_count"], 0)
            self.assertEqual(digest["runs"]["count"], 0)
            self.assertEqual(digest["patterns"]["top"], [])
            self.assertEqual(digest["heat"]["snapshot_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
