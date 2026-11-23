# -*- coding: utf-8 -*-
"""档案对比技能测试（skills/comparison/compare.py + tools_compare 集成行为）。

对着真实事故形态锁定：
   - 同 id 拒绝 / 坏 id 拒绝（不猜用户想比什么，返回拒绝形状不外抛）；
   - 跨区与不存在同形「档案 #N 不存在」（tools_guard 契约，不泄露别区档案号）；
   - 单边读失败折 partial + gaps 明示（缺章按 absent 处理，不编造）；
   - 标签演进（左首版 → 右末版）+ as_of + 置信度档位，只陈述不做升级/降级判断；
   - 证据差集按 url 去重（url/title/relation）与信源域名增/删；
   - 指纹相似度徽章：只标「疑似同源」不断言同一说法，阈值可经 rules 覆盖；
   - 纯函数与 store 直读对拍（工具/技能不是第二个事实源）；
   - data 键名锁定、纯函数永不抛。
全离线：临时目录里的真实 SQLite 库（TemporaryDirectory），零网络零 LLM。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import tools_compare  # noqa: E402
from mylocalai.assistant.registry import ToolRegistry  # noqa: E402
from mylocalai.assistant.skills.comparison import compare as C  # noqa: E402
from mylocalai.assistant.skills.comparison import compare_dossiers, load_rules  # noqa: E402
from mylocalai.dossier.profiles import ProfileStore  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402

#: 固定真假判定词（铁律）：任何出口一个都不许出现
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")
#: 升级/降级判断的典型说法（差异只述"标签从 X 变为 Y"，不做这类判断）
_UPGRADE_PHRASES = ("升级为", "降级为", "事态升级", "事态恶化", "风险上升", "风险下降")

#: 相似度用例的两条原文（P 周期 10 的循环串；B = P 前 30 字 + 异字母 10 字）
_TEXT_SAME = "某市自来水公司否认在自来水中投毒，官方通报已介入调查"
_TEXT_PUNCT_A = "自来水 有毒！"
_TEXT_PUNCT_B = "自来水有毒"
_TEXT_P = "甲乙丙丁戊己庚辛壬癸" * 4                 # 40 字，10 个互异 4-gram
_TEXT_MIX = _TEXT_P[:30] + "ABCDEFGHIJ"              # Jaccard 恰为 0.5（10/20）
_TEXT_DISJOINT = "ABCDEFGHIJ" * 4                     # 与 P 无共同 4-gram


def _conclusion(label, confidence="medium", as_of="2026-10-01T08:00:00+08:00"):
    return {"label": label, "confidence": confidence, "as_of": as_of}


def _ev(url, domain, title="", stance=""):
    return {"url": url, "domain": domain, "domain_tier": "", "source_kind": "",
            "stance": stance, "title": title, "snippet": "", "fetched_at": ""}


class _HalfBrokenStore:
    """半边炸的 store 替身：对指定 id 的某个读方法抛错（其余透传真 store）。"""

    def __init__(self, inner, fail_row=(), fail_conclusions=(), fail_evidence=()):
        self._inner = inner
        self._fail_row = set(fail_row)
        self._fail_conclusions = set(fail_conclusions)
        self._fail_evidence = set(fail_evidence)

    def get_dossier(self, did):
        if did in self._fail_row:
            raise RuntimeError("档案表炸了")
        return self._inner.get_dossier(did)

    def conclusions(self, did):
        if did in self._fail_conclusions:
            raise RuntimeError("结论表炸了")
        return self._inner.conclusions(did)

    def evidence(self, did):
        if did in self._fail_evidence:
            raise RuntimeError("证据表炸了")
        return self._inner.evidence(did)

    def close(self):
        self._inner.close()


class ComparisonTestBase(unittest.TestCase):
    """纯函数底座：临时目录 + 真实 SQLite 库（零网络零 LLM）。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DossierStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)

    def make_dossier(self, title, text="", conclusions=(), evidence=()):
        did = self.store.create_dossier(title, text, input_kind="text")
        for c in conclusions:
            self.store.add_conclusion(did, label=c["label"], risk_score=None,
                                      confidence=c.get("confidence", ""),
                                      as_of=c.get("as_of", ""))
        if evidence:
            self.store.add_evidence(did, 1, [dict(e) for e in evidence])
        return did

    def make_pair(self):
        """一对内容完整的档案（甲两版同标签 / 乙两版标签有变 + 证据域名有增有减）。"""
        left = self.make_dossier(
            "甲档案", _TEXT_SAME,
            conclusions=[_conclusion("存疑", "low", "2026-10-01T08:00:00+08:00"),
                         _conclusion("存疑", "medium", "2026-10-15T08:00:00+08:00")],
            evidence=[_ev("https://a.example/1", "a.example", "甲证据一", "支持"),
                      _ev("https://shared.example/2", "shared.example", "共有证据", "反驳")])
        right = self.make_dossier(
            "乙档案", _TEXT_SAME,
            conclusions=[_conclusion("疑似谣言", "medium", "2026-11-01T08:00:00+08:00"),
                         _conclusion("谣言", "high", "2026-11-20T08:00:00+08:00")],
            evidence=[_ev("https://shared.example/2", "shared.example", "共有证据", "反驳"),
                      _ev("https://b.example/3", "b.example", "乙证据一", "支持")])
        return left, right


class ComparisonToolTestBase(ComparisonTestBase):
    """工具层底座：DossierService + 自建注册表（T4 接线前 loop 里还没有本工具）。"""

    def setUp(self):
        super().setUp()
        self.service = DossierService(self.store)
        self.addCleanup(self.service.close)
        self.profiles = ProfileStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.profiles.close)
        self.registry = ToolRegistry()
        tools_compare.register_all(self.registry, None)
        self.default_profile = self.store.active_profile_id()

    def ctx(self, profile_id=None):
        return SimpleNamespace(sessions=None, dossiers=self.service, events=None,
                               cancel_check=None, services={},
                               profile_id=(self.default_profile if profile_id is None
                                           else profile_id))

    def call(self, left_id, right_id, profile_id=None):
        spec = self.registry.get("compare_dossiers")
        assert spec is not None, "compare_dossiers 未注册"
        return spec["handler"](self.ctx(profile_id), left_id=left_id, right_id=right_id)


# ===========================================================================
# 1. 规则加载（4）
# ===========================================================================
class TestLoadRules(unittest.TestCase):
    def test_package_rules_json_matches_defaults(self):
        raw = json.loads((Path(C.__file__).resolve().parent / "rules.json")
                         .read_text(encoding="utf-8"))
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            self.assertEqual(C.DEFAULT_COMPARISON_RULES[key], value, key)

    def test_override_clamped_to_safe_range(self):
        rules = load_rules({"max_versions": 999, "similarity_threshold": 0.1,
                            "max_evidence_items": "bad", "unknown_key": 1})
        self.assertEqual(rules["max_versions"], 50)          # 夹到安全区间上界
        self.assertEqual(rules["similarity_threshold"], 0.3)  # 夹到下界
        self.assertEqual(rules["max_evidence_items"], 20)     # 坏类型回退默认
        self.assertEqual(rules["max_domain_items"], 20)       # 未提供的键取默认
        self.assertNotIn("unknown_key", rules)                # 未知键丢弃

    def test_missing_file_falls_back_to_defaults(self):
        self.assertEqual(load_rules(Path("___no_such_rules___.json")),
                         dict(C.DEFAULT_COMPARISON_RULES))

    def test_broken_json_file_falls_back_to_defaults(self):
        with TemporaryDirectory() as tmp:
            broken = Path(tmp) / "rules.json"
            broken.write_text("{ 这不是合法 JSON", encoding="utf-8")
            self.assertEqual(load_rules(broken), dict(C.DEFAULT_COMPARISON_RULES))


# ===========================================================================
# 2. 同 id 与坏 id 拒绝（4）
# ===========================================================================
class TestRefusals(ComparisonTestBase):
    def test_same_id_refused_without_touching_store(self):
        did = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")])
        out = compare_dossiers(self.store, did, did)
        self.assertEqual(out["status"], "invalid")
        self.assertTrue(any("相同" in g for g in out["data"]["gaps"]))
        self.assertEqual(tuple(sorted(out["data"])), tuple(sorted(C._DATA_KEYS)))

    def test_non_positive_and_garbage_ids_refused(self):
        for bad in (0, -1, None, "abc", 1.5, True, [1]):
            with self.subTest(left=bad):
                out = compare_dossiers(self.store, bad, 2)
                self.assertEqual(out["status"], "invalid")
                self.assertIn("正整数", out["data"]["gaps"][0])

    def test_missing_side_refused_as_not_found(self):
        left = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")])
        out = compare_dossiers(self.store, left, 424242)
        self.assertEqual(out["status"], "not_found")
        self.assertEqual(out["missing_id"], 424242)
        self.assertIsNone(out["data"]["left"])     # 一侧不存在即整体拒绝，不读无关数据

    def test_tool_handler_same_id_degrades_gracefully(self):
        """相同 id：handler 诚实降级（不抛、不摔成系统错误），digest 指路 get_dossier。"""
        did = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")])
        reg = ToolRegistry()
        tools_compare.register_all(reg, None)
        handler = reg.get("compare_dossiers")["handler"]
        ctx = SimpleNamespace(sessions=None, dossiers=SimpleNamespace(store=self.store),
                              events=None, cancel_check=None, services={},
                              profile_id=None)
        out = handler(ctx, left_id=did, right_id=did)
        self.assertFalse(out["ok"])
        self.assertTrue(out.get("invalid_params"))
        self.assertFalse(out["degraded"])
        self.assertIn("不能相同", out["digest"])
        self.assertIn("get_dossier", out["digest"])


# ===========================================================================
# 3. 标签演进与缺章（5）
# ===========================================================================
class TestConclusionDelta(ComparisonTestBase):
    def test_full_compare_matches_store_direct_read(self):
        """技能读数与 store 直读逐字段一致（不是第二个事实源）。"""
        left, right = self.make_pair()
        out = compare_dossiers(self.store, left, right)
        data = out["data"]
        self.assertEqual(out["status"], "ok")
        left_c = self.store.conclusions(left)
        right_c = self.store.conclusions(right)
        left_row = self.store.get_dossier(left)
        self.assertEqual(data["left"]["title"], left_row["title"])
        self.assertEqual(data["left"]["label"], left_c[-1]["label"])
        self.assertEqual(data["left"]["confidence"], left_c[-1]["confidence"])
        self.assertEqual(data["left"]["as_of"], left_c[-1]["as_of"])
        self.assertEqual(data["left"]["evidence_count"], len(self.store.evidence(left)))
        self.assertEqual(data["left"]["domains"],
                         sorted({e["domain"] for e in self.store.evidence(left)}))
        left_urls = {e["url"] for e in self.store.evidence(left)}
        right_urls = {e["url"] for e in self.store.evidence(right)}
        self.assertEqual(data["evidence_diff"]["added_count"], len(right_urls - left_urls))
        self.assertEqual(data["evidence_diff"]["removed_count"], len(left_urls - right_urls))
        left_domains = {e["domain"] for e in self.store.evidence(left)}
        right_domains = {e["domain"] for e in self.store.evidence(right)}
        self.assertEqual(data["source_diff"]["added_domains"],
                         sorted(right_domains - left_domains))
        self.assertEqual(data["source_diff"]["removed_domains"],
                         sorted(left_domains - right_domains))

    def test_delta_takes_left_first_to_right_last(self):
        left, right = self.make_pair()
        delta = compare_dossiers(self.store, left, right)["data"]["conclusion_delta"]
        self.assertTrue(delta["changed"])
        self.assertEqual(delta["from_label"], "存疑")            # 左档案首版
        self.assertEqual(delta["to_label"], "谣言")              # 右档案末版
        self.assertEqual(delta["from_as_of"], "2026-10-01T08:00:00+08:00")
        self.assertEqual(delta["to_as_of"], "2026-11-20T08:00:00+08:00")

    def test_delta_unchanged_when_end_labels_equal(self):
        left = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")])
        right = self.make_dossier("乙", _TEXT_SAME, [_conclusion("存疑")])
        delta = compare_dossiers(self.store, left, right)["data"]["conclusion_delta"]
        self.assertFalse(delta["changed"])
        self.assertEqual(delta["from_label"], "存疑")
        self.assertEqual(delta["to_label"], "存疑")

    def test_absent_conclusion_is_null_not_fabricated(self):
        """没跑过核查的一侧：标签字段为 null、版本历史空、gaps 写明，不编造。"""
        left = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")])
        right = self.make_dossier("乙", _TEXT_SAME)   # 无结论无证据
        out = compare_dossiers(self.store, left, right)
        self.assertEqual(out["status"], "partial")     # 缺章如实标注
        self.assertIsNone(out["data"]["right"]["label"])
        self.assertIsNone(out["data"]["right"]["confidence"])
        self.assertIsNone(out["data"]["right"]["as_of"])
        self.assertEqual(out["data"]["right"]["versions"], [])
        self.assertTrue(any(f"#{right}" in g and "尚无结论" in g
                            for g in out["data"]["gaps"]))
        self.assertFalse(out["data"]["conclusion_delta"]["changed"])
        self.assertIsNone(out["data"]["conclusion_delta"]["to_label"])

    def test_versions_clamped_with_limits_note(self):
        conclusions = [_conclusion(f"标签{i % 3}", as_of=f"2026-10-{i + 1:02d}T00:00:00+08:00")
                       for i in range(12)]
        left = self.make_dossier("甲", _TEXT_SAME, conclusions)
        right = self.make_dossier("乙", _TEXT_SAME, [_conclusion("存疑")])
        data = compare_dossiers(self.store, left, right,
                                rules={"max_versions": 2})["data"]
        self.assertEqual(len(data["left"]["versions"]), 2)     # 只显示最近 2 版
        self.assertEqual(data["left"]["versions"][-1]["version"], 12)
        self.assertTrue(any("版本历史仅显示最近 2 版" in x and "共 12 版" in x
                            for x in data["limits"]))


# ===========================================================================
# 4. 证据与信源差集（4）
# ===========================================================================
class TestEvidenceAndSourceDiff(ComparisonTestBase):
    def test_evidence_diff_by_url_with_entry_keys(self):
        left = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")],
                                 [_ev("https://a.example/1", "a.example", "甲一", "支持"),
                                  _ev("https://shared.example/2", "shared.example", "共有", "反驳")])
        right = self.make_dossier("乙", _TEXT_SAME, [_conclusion("存疑")],
                                  [_ev("https://shared.example/2", "shared.example", "共有", "反驳"),
                                   _ev("https://b.example/3", "b.example", "乙一", "无关")])
        diff = compare_dossiers(self.store, left, right)["data"]["evidence_diff"]
        self.assertEqual(diff["added_count"], 1)
        self.assertEqual(diff["removed_count"], 1)
        self.assertEqual([e["url"] for e in diff["added"]], ["https://b.example/3"])
        self.assertEqual([e["url"] for e in diff["removed"]], ["https://a.example/1"])
        entry = diff["added"][0]
        self.assertEqual(tuple(sorted(entry)), ("relation", "title", "url"))
        self.assertEqual(entry["title"], "乙一")
        self.assertEqual(entry["relation"], "无关")   # relation = 库里的 stance 字段

    def test_evidence_counts_stay_total_when_list_clamped(self):
        left = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")], [])
        right = self.make_dossier("乙", _TEXT_SAME, [_conclusion("存疑")],
                                  [_ev(f"https://b.example/{i}", "b.example", f"乙{i}")
                                   for i in range(3)])
        data = compare_dossiers(self.store, left, right,
                                rules={"max_evidence_items": 1})["data"]
        diff = data["evidence_diff"]
        self.assertEqual(diff["added_count"], 3)          # 总数不失真
        self.assertEqual(len(diff["added"]), 1)           # 展示清单按规则裁剪
        self.assertTrue(any("前 1 条（共 3 条" in x for x in data["limits"]))

    def test_source_diff_domain_sets(self):
        left = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")],
                                 [_ev("https://a.example/1", "a.example"),
                                  _ev("https://s.example/2", "s.example")])
        right = self.make_dossier("乙", _TEXT_SAME, [_conclusion("存疑")],
                                  [_ev("https://s.example/2", "s.example"),
                                   _ev("https://b.example/3", "b.example")])
        src = compare_dossiers(self.store, left, right)["data"]["source_diff"]
        self.assertEqual(src["added_domains"], ["b.example"])
        self.assertEqual(src["removed_domains"], ["a.example"])

    def test_duplicate_url_within_one_side_counted_once(self):
        left = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")], [])
        right = self.make_dossier("乙", _TEXT_SAME, [_conclusion("存疑")],
                                  [_ev("https://b.example/3", "b.example", "第一批"),
                                   _ev("https://b.example/3", "b.example", "第二批")])
        diff = compare_dossiers(self.store, left, right)["data"]["evidence_diff"]
        self.assertEqual(diff["added_count"], 1)          # 同 url 去重
        self.assertEqual(diff["added"][0]["title"], "第一批")   # 首次出现为准


# ===========================================================================
# 5. 指纹相似度徽章（5）
# ===========================================================================
class TestSimilarity(ComparisonTestBase):
    def _sim(self, left_text, right_text, rules=None):
        left = self.make_dossier("甲", left_text, [_conclusion("存疑")])
        right = self.make_dossier("乙", right_text, [_conclusion("存疑")])
        return compare_dossiers(self.store, left, right, rules=rules)["data"]["similarity"]

    def test_identical_text_suspected_same_source(self):
        sim = self._sim(_TEXT_SAME, _TEXT_SAME)
        self.assertEqual(sim["score"], 1.0)
        self.assertEqual(sim["threshold"], 0.72)
        self.assertTrue(sim["suspected_same_source"])
        self.assertIn("疑似同源", sim["basis"])
        self.assertIn("同一性断言", sim["basis"])   # 明说不作同一性断言

    def test_punctuation_variants_still_match(self):
        sim = self._sim(_TEXT_PUNCT_A, _TEXT_PUNCT_B)
        self.assertEqual(sim["score"], 1.0)          # 归一后同形
        self.assertTrue(sim["suspected_same_source"])

    def test_disjoint_text_not_suspected(self):
        sim = self._sim(_TEXT_P, _TEXT_DISJOINT)
        self.assertEqual(sim["score"], 0.0)
        self.assertFalse(sim["suspected_same_source"])

    def test_threshold_override_changes_badge_only(self):
        """0.5 的相似度：默认阈值不标、调低阈值才标——徽章是阈值比较，不是断言。"""
        sim = self._sim(_TEXT_P, _TEXT_MIX)
        self.assertEqual(sim["score"], 0.5)
        self.assertFalse(sim["suspected_same_source"])
        sim_low = self._sim(_TEXT_P, _TEXT_MIX, rules={"similarity_threshold": 0.3})
        self.assertTrue(sim_low["suspected_same_source"])
        self.assertEqual(sim_low["threshold"], 0.3)

    def test_missing_text_is_not_comparable(self):
        left = self.make_dossier("甲", "", [_conclusion("存疑")])   # 原文未保存
        right = self.make_dossier("乙", _TEXT_SAME, [_conclusion("存疑")])
        out = compare_dossiers(self.store, left, right)
        sim = out["data"]["similarity"]
        self.assertIsNone(sim["score"])
        self.assertFalse(sim["suspected_same_source"])
        self.assertTrue(any("声明原文" in g for g in out["data"]["gaps"]))


# ===========================================================================
# 6. 单边失败与永不抛（4）
# ===========================================================================
class TestPartialAndNeverRaises(ComparisonTestBase):
    def test_conclusion_read_failure_degrades_one_side(self):
        left, right = self.make_pair()
        broken = _HalfBrokenStore(self.store, fail_conclusions={left})
        out = compare_dossiers(broken, left, right)
        self.assertEqual(out["status"], "partial")
        self.assertIsNone(out["data"]["left"]["label"])       # 缺章按 absent
        self.assertEqual(out["data"]["left"]["title"], "甲档案")   # 本体仍读到了
        self.assertEqual(out["data"]["right"]["label"], "谣言")    # 另一侧不受影响
        self.assertTrue(any("结论版本读取失败" in g for g in out["data"]["gaps"]))

    def test_evidence_read_failure_degrades_one_side(self):
        left, right = self.make_pair()
        broken = _HalfBrokenStore(self.store, fail_evidence={right})
        out = compare_dossiers(broken, left, right)
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["data"]["right"]["evidence_count"], 0)
        self.assertTrue(any("证据本读取失败" in g for g in out["data"]["gaps"]))

    def test_fatal_row_failure_leaves_side_none(self):
        left, right = self.make_pair()
        broken = _HalfBrokenStore(self.store, fail_row={left})
        out = compare_dossiers(broken, left, right)
        self.assertEqual(out["status"], "partial")
        self.assertIsNone(out["data"]["left"])
        self.assertTrue(any("本体读取失败" in g for g in out["data"]["gaps"]))

    def test_never_raises_on_garbage_store_and_ids(self):
        left, right = self.make_pair()
        cases = [
            (None, left, right),
            (object(), left, right),
            (_HalfBrokenStore(self.store, fail_row={left, right}), left, right),
            (self.store, None, right),
            (self.store, "abc", right),
            (self.store, 0, -1),
            (self.store, True, right),
            (self.store, 1.5, right),
        ]
        for store, a, b in cases:
            with self.subTest(store=type(store).__name__, left=a):
                out = compare_dossiers(store, a, b)
                self.assertIsInstance(out, dict)
                self.assertIn(out["status"], ("ok", "partial", "not_found", "invalid"))
                self.assertEqual(tuple(sorted(out["data"])), tuple(sorted(C._DATA_KEYS)))


# ===========================================================================
# 7. 键名锁定（2）
# ===========================================================================
class TestKeysLocked(ComparisonTestBase):
    def test_data_keys_locked(self):
        left, right = self.make_pair()
        data = compare_dossiers(self.store, left, right)["data"]
        self.assertEqual(tuple(sorted(data)), tuple(sorted(C._DATA_KEYS)))
        self.assertEqual(tuple(sorted(data["left"])), tuple(sorted(C._SIDE_KEYS)))
        self.assertEqual(tuple(sorted(data["right"])), tuple(sorted(C._SIDE_KEYS)))
        self.assertEqual(tuple(sorted(data["similarity"])),
                         tuple(sorted(C._SIMILARITY_KEYS)))
        self.assertEqual(tuple(sorted(data["conclusion_delta"])),
                         tuple(sorted(C._DELTA_KEYS)))
        self.assertEqual(tuple(sorted(data["evidence_diff"])),
                         tuple(sorted(C._EVIDENCE_DIFF_KEYS)))
        self.assertEqual(tuple(sorted(data["source_diff"])),
                         tuple(sorted(C._SOURCE_DIFF_KEYS)))

    def test_refused_shapes_keep_locked_keys(self):
        did = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")])
        for out in (compare_dossiers(self.store, did, did),
                    compare_dossiers(self.store, did, 424242),
                    compare_dossiers(self.store, "x", did)):
            self.assertEqual(tuple(sorted(out["data"])), tuple(sorted(C._DATA_KEYS)))
            self.assertEqual(out["data"]["evidence_diff"],
                             {"added_count": 0, "removed_count": 0, "added": [], "removed": []})


# ===========================================================================
# 8. 工具层集成行为（跨区同形 / 卡片键锁定 / digest 纪律）（5）
# ===========================================================================
class TestToolIntegration(ComparisonToolTestBase):
    def test_cross_workspace_refused_same_shape_as_missing(self):
        """别的工作区的档案号 → 与不存在同形，不泄露别区有哪些档案。"""
        other = self.profiles.create_profile("other-ws", "别的工作区", "analyst")
        theirs = self.store.create_dossier("别区档案", "别区正文", profile_id=other)
        mine = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")])
        for a, b in ((mine, theirs), (theirs, mine)):
            with self.subTest(pair=(a, b)):
                out = self.call(a, b)
                self.assertFalse(out["ok"])
                self.assertTrue(out.get("not_found"))
                self.assertIn(f"档案 #{theirs} 不存在。", out["digest"])
                self.assertIsNone(out["card"])
                self.assertNotIn("别的工作区", out["digest"])
                self.assertNotIn("profile", out["digest"])

    def test_nonexistent_id_same_shape(self):
        mine = self.make_dossier("甲", _TEXT_SAME, [_conclusion("存疑")])
        out = self.call(mine, 424242)
        self.assertFalse(out["ok"])
        self.assertTrue(out.get("not_found"))
        self.assertEqual(out["digest"], "档案 #424242 不存在。")
        self.assertEqual(out["dossier_id"], None)
        self.assertFalse(out["degraded"])     # 不存在不是故障，是合法空出口

    def test_card_keys_locked_and_numeric_free(self):
        left, right = self.make_pair()
        out = self.call(left, right)
        self.assertTrue(out["ok"])
        card = out["card"]
        self.assertEqual(card["card_type"], "comparison")
        self.assertEqual(tuple(sorted(card)),
                         ("card_type", "conclusion_delta", "degraded", "evidence_diff",
                          "gaps", "left", "limits", "right", "similarity",
                          "source_diff", "status"))
        # 卡内不出数值：相似度只有徽章，没有 score/threshold；没有 risk_score
        self.assertEqual(tuple(sorted(card["similarity"])),
                         ("basis", "suspected_same_source"))
        self.assertNotIn("risk_score", card["left"])
        self.assertNotIn("risk_score", card["right"])
        for version in card["left"]["versions"]:
            self.assertNotIn("risk_score", version)

    def test_digest_transcribes_without_verdict_or_upgrade_words(self):
        left, right = self.make_pair()
        out = self.call(left, right)
        digest = out["digest"]
        self.assertIn("标签演进", digest)
        self.assertIn("从「存疑」", digest)
        self.assertIn("变为「谣言」", digest)
        self.assertIn("as_of", digest)
        self.assertIn("疑似同源", digest)
        self.assertIn("verify_claim", digest)      # 指路核查流程
        self.assertNotIn("0.72", digest)           # 卡内/digest 不出阈值数值
        self.assertNotIn("1.0", digest)
        self.assertNotIn("risk_score", digest)
        for word in _VERDICT_WORDS + _UPGRADE_PHRASES:
            self.assertNotIn(word, digest)

    def test_partial_through_tool_states_gap(self):
        left, right = self.make_pair()
        broken_service = DossierService(_HalfBrokenStore(self.store,
                                                         fail_evidence={right}))
        self.addCleanup(broken_service.close)
        out = self._call_with_service(broken_service, left, right)
        self.assertTrue(out["ok"])                 # partial 是合法出口
        self.assertTrue(out["degraded"])
        self.assertEqual(out["status"], "partial")
        self.assertTrue(any("证据本读取失败" in g for g in out["data"]["gaps"]))
        self.assertIn("partial", out["digest"])

    def _call_with_service(self, service, left_id, right_id):
        spec = self.registry.get("compare_dossiers")
        ctx = SimpleNamespace(sessions=None, dossiers=service, events=None,
                              cancel_check=None, services={},
                              profile_id=self.default_profile)
        return spec["handler"](ctx, left_id=left_id, right_id=right_id)


if __name__ == "__main__":
    unittest.main()
