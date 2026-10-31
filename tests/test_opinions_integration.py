# -*- coding: utf-8 -*-
"""opinions/deep_read 章节化与档案证据本复用的集成测试（真实引擎，llm=False 离线路径）。

验证 M3-3/M3-5 的引擎侧事实（handler 层见 test_chapters_tools；服务接线规格见
tools_chapters.py 顶部《deep_read 章节化调用方规格》）：
  1. 混合证据本（官方/权威媒体/UGC）→ 立场分桶正确；有权威来源时如实不触发
     （"已有官方或权威媒体来源"是合法出口）；
  2. 纯 UGC 双边证据本 → collect_opinion_analysis(llm=False) 零 LLM 调用仍给出
     两组分布与论据（分布计数的原料），降级记录如实；
  3. should_trigger 触发语义矩阵（空本/无条目/权威在场/纯私人看法/全无关）；
  4. deep_read 端 evidence 传档案证据行后：档案行（snippet 形状）成为编号证据，
     护栏有效证据集=传入集——集内引用保留、集外引用剔除并记 guard_dropped；
  5. 空证据本回退路径：opinions 无证据本时回退 collect_evidence 自取（llm_judge
     同步关闭），回退后仍空则如实报"证据本没有条目"；deep_read 无编号证据时
     prompt 明示"未提供编号证据"，模型输出 applicable=false 如实透传（不硬编）；
  6. opinions_signals 传入后进入深读 prompt（诉求/归因/行动倾向）；LLM 不可用
     时如实降级为模板（degraded=true），不假装有模型解读。
全部离线：llm=False / mock chat_text / fetch_fn 注入，零网络零真实 API。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mylocalai.attention import attention as A  # noqa: E402
from mylocalai.attention import deep_read as D  # noqa: E402
from mylocalai.rumor import opinions as O  # noqa: E402
from mylocalai.retrieval import sources as SRC  # noqa: E402


def book_entry(url, stance, tier=SRC.TIER_UGC, title="标题", snippet="摘要内容"):
    """引擎形状的证据条目（与档案 verify 批次落库前的形状一致）。"""
    return {"url": url, "title": title, "snippet": snippet, "stance": stance,
            "domain_tier": tier, "source_kind": SRC.source_kind(url),
            "date": None, "full_text": None}


def dossier_evidence(n=12):
    """档案证据行形状（dossier.store.evidence 的输出形状，带 id/batch，无 text 有 snippet）。"""
    return [{"id": 100 + i, "batch": 1,
             "url": f"https://bbs.example/post/{i}", "domain": "bbs.example",
             "domain_tier": SRC.TIER_UGC, "source_kind": "非官方（论坛）",
             "stance": "质疑" if i % 2 else "支持",
             "title": f"网帖{i}",
             "snippet": f"发言{i}：通报时间比网友爆料晚了快一天",
             "fetched_at": "2026-10-31T08:00:00+08:00"}
            for i in range(1, n + 1)]


def detail_of(x, y, z):
    """触发深读用的 score_detail（与 test_deep_read_controlled 同一测法）。"""
    d = A.score_detail(x, y, z)
    d["threshold"] = A.DEEP_READ_THRESHOLD
    return d


def good_output():
    """合规的模型输出（对照 test_deep_read_controlled.GOOD_OUTPUT，证据 id 换成档案行编号）。"""
    return {
        "applicable": True,
        "issue_tags": [{"id": "info_transparency_trust", "confidence": "medium",
                        "evidence_ids": ["e1", "e3"]}],
        "tensions": [{"a": "信息发布节奏", "b": "公众知情需求",
                      "evidence_ids": ["e1", "e3"], "level": "L2"}],
        "phenomena": [{"text": "多条发言指向通报时间晚于网络传闻", "level": "L1",
                       "evidence_ids": ["e1", "e3"], "count": "6/12"}],
        "counter_reading": {"text": "2 条发言认为质疑源于误解", "evidence_ids": ["e2"]},
        "boundary": {"scope": "sample", "sample_size": 12, "proxy_flags": []},
        "uncertainty": ["样本来自单一论坛帖子，代表性有限"],
        "degraded": False,
    }


class TestMixedBookBucketing(unittest.TestCase):
    """要点①：官方/非官方/UGC 混合证据本的立场分桶与触发语义（分布推导的原料）。"""

    def test_mixed_book_bucketing_and_honest_no_trigger(self):
        entries = [book_entry("https://www.gov.cn/x", "反驳", SRC.TIER_OFFICIAL,
                              title="官方通报"),
                   book_entry("https://www.xinhuanet.com/a", "反驳", SRC.TIER_MEDIA,
                              title="权威媒体报道"),
                   book_entry("https://zhihu.com/b", "支持", title="知乎答主"),
                   book_entry("https://weibo.com/c", "反驳", title="微博网友"),
                   book_entry("https://tieba.baidu.com/d", "无关", title="贴吧灌水")]
        # 立场分桶（真实服务推导 distribution 用的就是这组计数）
        split = O.split_by_stance(entries)
        self.assertEqual(len(split["支持"]), 1)
        self.assertEqual(len(split["反驳"]), 3, "官方条目的立场也计入分布（按立场不按档次）")
        self.assertEqual(split["neutral"], 1)
        self.assertEqual(split["group_mode"], "two-sided")
        # 有权威来源 → 如实不触发（这是合法出口，不是失败）
        ok, reason = O.should_trigger({"entries": entries, "skipped": None, "error": None})
        self.assertFalse(ok)
        self.assertIn("官方或权威媒体", reason)
        res = O.collect_opinion_analysis("某说法", evidence_book={"entries": entries},
                                         fetch_fn=lambda u: None, llm=False)
        self.assertFalse(res["triggered"])
        self.assertIn("官方或权威媒体", res["skip_reason"])


class TestUgcTwoSidedOffline(unittest.TestCase):
    """要点②：纯 UGC 双边证据本在 llm=False 下跑通全程，零 LLM 调用。"""

    def test_two_sided_run_no_llm_calls(self):
        book = {"claim": "自来水被下毒", "entries": [
                    book_entry("https://zhihu.com/a", "支持", title="自来水真的被下毒了"),
                    book_entry("https://weibo.com/b", "支持", title="转发提醒"),
                    book_entry("https://tieba.baidu.com/c", "反驳", title="下毒是谣言"),
                    book_entry("https://bbs.example/d", "反驳", title="别传了")],
                "skipped": None, "error": None}
        with mock.patch("mylocalai.llm.llm_client.chat_json") as m:
            res = O.collect_opinion_analysis("自来水被人下毒了", evidence_book=book,
                                             fetch_fn=lambda u: None, llm=False, seed=7)
        self.assertEqual(m.call_count, 0, "llm=False 必须零 LLM 调用")
        self.assertTrue(res["triggered"])
        self.assertEqual(res["group_mode"], "two-sided")
        # 分布计数的原料：两组的篇数与分桶一致
        self.assertEqual(len(res["groups"]["支持"]), 2)
        self.assertEqual(len(res["groups"]["反驳"]), 2)
        for key in ("支持", "反驳"):
            grp = res["per_group"][key]
            self.assertEqual(grp["overlap"]["method"], "rule")
            self.assertTrue(grp["overlap"]["items"], "规则聚类仍应给出论据条目")
            self.assertEqual(grp["contradictions"]["contradictions"], [])
        # 抓取全部失败（fetch_fn 返回 None）→ 摘要兜底 + 降级记录如实（含"未启用大模型"）
        self.assertTrue(all(a["read_from"] == "摘要" for a in res["per_article"]))
        self.assertTrue(any(isinstance(d, dict) and "未启用大模型" in d.get("reason", "")
                            for d in res["degraded"]))


class TestShouldTriggerMatrix(unittest.TestCase):
    """要点③：should_trigger 语义矩阵（真实服务据它决定"出分布还是如实说不可析"。"""

    def test_trigger_semantics(self):
        cases = [
            (None, False, "证据本为空"),
            ({}, False, "证据本为空"),
            ({"entries": []}, False, "没有条目"),
            ({"skipped": "无可核实的事实性声明，未搜索", "entries": []}, False, "无可核实"),
            ({"entries": [book_entry("https://zhihu.com/a", "无关")]}, False, "没有可分析的私人看法"),
            ({"entries": [book_entry("https://zhihu.com/a", "支持"),
                          book_entry("https://weibo.com/b", "反驳")]}, True, ""),
        ]
        for book, want_ok, want_reason in cases:
            with self.subTest(book=book):
                ok, reason = O.should_trigger(book)
                self.assertEqual(ok, want_ok)
                if want_reason:
                    self.assertIn(want_reason, reason)
                else:
                    self.assertEqual(reason, "")


class TestDeepReadGuardUsesDossierEvidence(unittest.TestCase):
    """要点④：档案证据行传入 deep_read 后，护栏有效证据集=传入集。"""

    def test_dossier_rows_become_numbered_evidence_and_guard_enforces_set(self):
        rows = dossier_evidence(12)
        seen = {}

        def cap(system, user, **kw):
            seen["system"], seen["user"] = system, user
            # 模型引用了集内 e1/e3 与集外 e999：护栏应保前者剔后者
            raw = good_output()
            raw["phenomena"][0]["evidence_ids"] = ["e1", "e3", "e999"]
            return json.dumps(raw, ensure_ascii=False), ""

        with mock.patch("mylocalai.llm.llm_client.chat_text", side_effect=cap):
            r = D.deep_read("某地通报事件", detail_of(1000, 1e8, 1e6), use_llm=True,
                            evidence=rows)
        # 档案行（snippet 形状）按行序编号进 prompt：e1..e12
        self.assertIn("e1: 发言1：通报时间比网友爆料晚了快一天", seen["user"])
        self.assertIn("e12: 发言12", seen["user"])
        # 触发且适用：正文由受控生成产出
        self.assertTrue(r["triggered"])
        self.assertTrue(r["applicable"])
        self.assertEqual(r["source"], "llm")
        # 集内引用保留、集外引用剔除并记入 guard_dropped（护栏有效证据集=传入集）
        self.assertEqual(r["result"]["phenomena"][0]["evidence_ids"], ["e1", "e3"])
        self.assertTrue(any("e999" in d for d in r["guard_dropped"]))
        self.assertTrue(r["degraded"])

    def test_only_out_of_set_references_drop_entries(self):
        """引用全部集外 → 该条目整条丢弃（不为凑条目而保留无效引用）。"""
        rows = dossier_evidence(3)

        def fake(system, user, **kw):
            raw = good_output()
            raw["tensions"][0]["evidence_ids"] = ["e999"]
            raw["tensions"][0]["level"] = "L1"
            return json.dumps(raw, ensure_ascii=False), ""

        with mock.patch("mylocalai.llm.llm_client.chat_text", side_effect=fake):
            r = D.deep_read("t", detail_of(1000, 1e8, 1e6), use_llm=True, evidence=rows)
        self.assertTrue(r["degraded"])
        self.assertTrue(any("e999" in d for d in r["guard_dropped"]))
        self.assertEqual(r["result"]["tensions"], [])


class TestEmptyEvidenceFallback(unittest.TestCase):
    """要点⑤：空证据本的回退与诚实出口。"""

    def test_opinions_falls_back_to_collect_evidence_then_reports_empty(self):
        """evidence_book=None → 回退 collect_evidence 自取（llm_judge 同步关）；
        回退后仍空 → 如实报"证据本没有条目"。"""
        captured = {}

        def fake_collect(text, **kwargs):
            captured.update(kwargs)
            return {"entries": [], "skipped": None, "error": None}

        with mock.patch.object(O, "collect_evidence", side_effect=fake_collect), \
             mock.patch("mylocalai.llm.llm_client.chat_json") as m:
            res = O.collect_opinion_analysis("自来水被下毒", llm=False,
                                             search_fn=lambda q, k: {"results": [], "engines": {}},
                                             fetch_fn=lambda u: None)
        self.assertEqual(m.call_count, 0)
        self.assertEqual(captured.get("llm_judge"), False, "回退自取时立场判定也必须关 LLM")
        self.assertFalse(res["triggered"])
        self.assertIn("没有条目", res["skip_reason"])

    def test_deep_read_without_evidence_asks_not_applicable(self):
        """无编号证据 → prompt 明示"未提供编号证据"；模型输出 applicable=false 如实透传。"""
        seen = {}

        def cap(system, user, **kw):
            seen["user"] = user
            return json.dumps({"applicable": False, "reason": "缺少可引用的编号证据"},
                              ensure_ascii=False), ""

        with mock.patch("mylocalai.llm.llm_client.chat_text", side_effect=cap):
            r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True, evidence=None)
        self.assertIn("未提供编号证据", seen["user"])
        self.assertTrue(r["triggered"])
        self.assertFalse(r["applicable"])
        self.assertEqual(r["text"], "")
        self.assertEqual(r["reason"], "缺少可引用的编号证据")
        self.assertFalse(r["degraded"], "applicable=false 是合法出口，不算降级")


class TestOpinionsSignalsWiring(unittest.TestCase):
    """要点⑥：opinions_signals 进深读 prompt；LLM 不可用如实降级为模板。"""

    SIGNALS = {"appeals": ["要求公布完整检测报告"],
               "attribution_targets": ["信息发布方"],
               "action_tendencies": ["持续追问检测结果"]}

    def test_signals_enter_prompt_and_output_still_guarded(self):
        seen = {}

        def cap(system, user, **kw):
            seen["user"] = user
            return json.dumps(good_output(), ensure_ascii=False), ""

        with mock.patch("mylocalai.llm.llm_client.chat_text", side_effect=cap):
            r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True,
                            evidence=dossier_evidence(12), opinions_signals=self.SIGNALS)
        self.assertIn("核心诉求", seen["user"])
        self.assertIn("要求公布完整检测报告", seen["user"])
        self.assertIn("归因对象", seen["user"])
        self.assertIn("行动倾向", seen["user"])
        self.assertTrue(r["applicable"])
        self.assertIn("信息发布节奏", r["text"], "正文由护栏清洗后的受控产出拼出")

    def test_llm_failure_degrades_to_template_honestly(self):
        """LLM 不可用 → 模板回退 degraded=true、来源如实标 template，不假装有模型解读。"""
        with mock.patch("mylocalai.llm.llm_client.chat_text",
                        return_value=("", "网关超时")):
            r = D.deep_read("某话题", detail_of(1000, 1e8, 1e6), use_llm=True,
                            evidence=dossier_evidence(12), opinions_signals=self.SIGNALS)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["source"], "template")
        self.assertTrue(r["degraded"])
        self.assertIn("大模型不可用", r["degraded_reason"])
        self.assertTrue(r["text"], "模板回退仍有结构化文本，不留空白")


if __name__ == "__main__":
    unittest.main()
