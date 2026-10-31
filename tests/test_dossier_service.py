# -*- coding: utf-8 -*-
"""档案域服务编排测试（工作指导 §6 M2-7）：建档、结论/证据落库、recheck 版本化、降级。

DossierService 是 store 之上的业务语义层：verify/recheck 跑完 agent 后把卡片化结论、
证据批次、运行记录写进档案库。这里用 stub agent result（形状与 RumorAgent.analyze()
的返回一致：label/risk_score/evidence_book.entries/...）验证：
  - 首次 record → conclusions v1 + 证据 batch=1 + analysis_runs ok；
  - 再 record（recheck 语义）→ v2 + batch=2，旧版本/旧批次都还在；
  - 章节懒加载（热度/观点/深读）落库后 store 可读回；
  - 档案库写不进时（如 store 已关闭）不抛异常、返回 degraded/error 标记——
    档案写失败不影响主结论。
全程离线临时库（TemporaryDirectory），绝不触碰项目 data/ 目录。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402


def _entry(url: str, kind: str, tier: str, stance: str) -> dict:
    return {"url": url, "domain": url.split("/")[2], "domain_tier": tier,
            "source_kind": kind, "stance": stance, "title": "标题：" + url,
            "snippet": "摘要……", "fetched_at": "2026-10-30T22:00:00+08:00"}


# 第一批证据：1 官方 + 2 非官方 → official=1/total=3 → confidence=medium、复审提条目数
ENTRIES_V1 = [
    _entry("https://www.gov.cn/x/a", "官方（政府/权威机构）", "官方权威", "反驳"),
    _entry("https://news.example.com/b", "非官方（权威媒体）", "权威媒体/辟谣平台", "支持"),
    _entry("https://weibo.com/c", "非官方", "自媒体/UGC", "中立"),
]
# 第二批（recheck）：2 官方 + 2 非官方 + 1 其他 → official=2/total=5 → confidence=high、
# 复审只提"新增权威条目≥2"
ENTRIES_V2 = ENTRIES_V1[:1] + [
    _entry("https://www.gov.cn/y/d", "官方（政府/权威机构）", "官方权威", "反驳"),
    _entry("https://news.example.com/e", "非官方（权威媒体）", "权威媒体/辟谣平台", "支持"),
    _entry("https://weibo.com/f", "非官方", "自媒体/UGC", "中立"),
    _entry("https://unknown.example.com/g", "其他", "", "中立"),
]


def agent_result(label: str = "疑似谣言", risk: float = 62, entries=None,
                 degraded=None, agent_error: str = "") -> dict:
    """stub RumorAgent.analyze() 返回：只保留 DossierService 真正消费的字段。"""
    return {
        "label": label, "risk_score": risk,
        "signals": ["情绪化表述", "无来源断言"],
        "speech_tier": "疑似情绪化谣言",
        "degraded_components": list(degraded or []),
        "agent_error": agent_error,
        "evidence_book": {"claim": "自来水被下毒", "queries": [],
                          "entries": list(entries or []), "skipped": None,
                          "error": None, "llm_error": None,
                          "stance_summary": {}, "verdict_hint": ""},
    }


class DossierServiceTestBase(unittest.TestCase):
    """公共脚手架：每个用例独立的临时库 + 服务门面。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = DossierStore(Path(self.tmp.name) / "dossier.db")
        self.svc = DossierService(store=self.store)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()


class TestRecordAnalysis(DossierServiceTestBase):
    def test_start_dossier_defaults_and_validation(self):
        """建档：标题默认取正文前 24 字；空正文兜底"未命名核查"；input_kind 走白名单。"""
        text = "0123456789" * 3  # 30 字
        did = self.svc.start_dossier(text)
        d = self.store.get_dossier(did)
        self.assertEqual(d["title"], "0123456789" * 2 + "0123")
        self.assertEqual(d["input_kind"], "text")
        self.assertEqual(self.svc.start_dossier("   ", title=None), did + 1)
        self.assertEqual(self.store.get_dossier(did + 1)["title"], "未命名核查")
        self.assertEqual(self.svc.start_dossier("话题", title="自定义", input_kind="topic"), did + 2)
        with self.assertRaises(AssertionError):
            self.svc.start_dossier("x", input_kind="audio")

    def test_first_record_writes_conclusion_evidence_and_run(self):
        """首次 record_analysis：conclusions v1、证据 batch=1、analysis_runs ok；
        卡片口径（confidence/充分度/复审条件）与证据分布一致，raw_verdict 原样留存。"""
        did = self.svc.start_dossier("自来水谣言", "听说自来水被下毒了")
        out = self.svc.record_analysis(did, agent_result(entries=ENTRIES_V1),
                                       kind="verify", duration_s=1.5, requests=6, tokens=800)
        self.assertEqual(out["version"], 1)
        self.assertEqual(out["batch"], 1)
        card = out["card"]
        self.assertEqual(card["label"], "疑似谣言")
        self.assertEqual(card["risk_score"], 62)
        self.assertEqual(card["confidence"], "medium")           # 1 官方、总数 3 → 规则2
        self.assertEqual(card["evidence_sufficiency"],
                         {"official": 1, "unofficial": 2, "unknown": 0, "total": 3})
        self.assertEqual(card["recheck_conditions"], ["证据本独立条目≥5"])
        self.assertTrue(card["as_of"])
        # 落库读回：结论、证据、运行记录三处都对得上
        cs = self.store.conclusions(did)
        self.assertEqual(len(cs), 1)
        c = cs[0]
        self.assertEqual(c["version"], 1)
        self.assertEqual(c["label"], "疑似谣言")
        self.assertEqual(c["confidence"], "medium")
        self.assertEqual(c["evidence_sufficiency"]["total"], 3)   # JSON 往返
        self.assertEqual(c["recheck_conditions"], ["证据本独立条目≥5"])
        self.assertEqual(c["as_of"], card["as_of"])
        self.assertEqual(c["raw_verdict"]["signals"], ["情绪化表述", "无来源断言"])
        self.assertEqual(c["raw_verdict"]["speech_tier"], "疑似情绪化谣言")
        ev = self.store.evidence(did)
        self.assertEqual(len(ev), 3)
        self.assertEqual({r["batch"] for r in ev}, {1})
        self.assertEqual(ev[0]["source_kind"], "官方（政府/权威机构）")
        self.assertEqual(ev[0]["stance"], "反驳")
        runs = self.store.runs(did)
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["kind"], runs[0]["status"]), ("verify", "ok"))
        self.assertFalse(runs[0]["degraded"])
        self.assertEqual(runs[0]["duration_s"], 1.5)

    def test_recheck_appends_v2_and_batch2(self):
        """recheck 语义：同一档案再 record → 结论 v2、证据 batch=2，v1/批次1 不被覆盖。"""
        did = self.svc.start_dossier("t", "x")
        self.svc.record_analysis(did, agent_result(entries=ENTRIES_V1))
        out = self.svc.record_analysis(
            did, agent_result(label="存疑待核实", risk=40, entries=ENTRIES_V2), kind="recheck")
        self.assertEqual((out["version"], out["batch"]), (2, 2))
        cs = self.store.conclusions(did)
        self.assertEqual([c["version"] for c in cs], [1, 2])
        self.assertEqual([c["label"] for c in cs], ["疑似谣言", "存疑待核实"])
        self.assertEqual(cs[1]["confidence"], "high")             # 2 官方、总数 5 → 规则1
        self.assertEqual(cs[1]["recheck_conditions"], ["证据本新增权威条目≥2"])
        self.assertEqual(len(self.store.evidence(did, batch=1)), 3)
        self.assertEqual(len(self.store.evidence(did, batch=2)), 5)
        self.assertEqual(len(self.store.evidence(did)), 8)
        runs = self.store.runs(did)
        self.assertEqual([r["kind"] for r in runs], ["verify", "recheck"])

    def test_degraded_components_mark_run_degraded(self):
        """组件降级（如联网取证失败）：结论照常落库，运行记录如实标 degraded，不编造。"""
        did = self.svc.start_dossier("t", "x")
        out = self.svc.record_analysis(
            did, agent_result(entries=ENTRIES_V1, degraded=["evidence"],
                              agent_error="联网取证超时"))
        self.assertEqual(out["version"], 1)                        # 卡片/结论不受影响
        run = self.store.runs(did)[0]
        self.assertEqual(run["status"], "degraded")
        self.assertTrue(run["degraded"])

    def test_agent_error_only_also_marks_degraded(self):
        """只有 agent_error（无 degraded_components）时同样标 degraded。"""
        did = self.svc.start_dossier("t", "x")
        self.svc.record_analysis(did, agent_result(entries=[], agent_error="LLM 不可用"))
        self.assertTrue(self.store.runs(did)[0]["degraded"])


class TestRecordSections(DossierServiceTestBase):
    def test_record_speech_analysis_classic(self):
        """classic（言论土壤）建档：四档结论作标签、risk_score=None、无证据本 →
        confidence 只能是 low（规则4），复审条件含"出现官方…"与条目数。"""
        did = self.svc.start_dossier("言论土壤", "这条评论带节奏")
        result = {"emotion": {"emotion_score": 8.0}, "attack": {"aggression_level": "中"},
                  "logic": {"logic_relevance": "弱"}, "conclusion": "疑似情绪化谣言"}
        out = self.svc.record_speech_analysis(did, result, duration_s=0.4)
        self.assertEqual(out["version"], 1)
        card = out["card"]
        self.assertEqual(card["label"], "疑似情绪化谣言")
        self.assertIsNone(card["risk_score"])
        self.assertEqual(card["confidence"], "low")
        self.assertEqual(card["evidence_sufficiency"]["total"], 0)
        self.assertEqual(card["recheck_conditions"],
                         ["出现官方/权威媒体级信源的通报或报道", "证据本独立条目≥5"])
        c = self.store.conclusions(did)[0]
        self.assertIsNone(c["risk_score"])
        self.assertEqual(c["raw_verdict"]["conclusion"], "疑似情绪化谣言")
        self.assertIn("emotion", c["raw_verdict"])
        run = self.store.runs(did)[0]
        self.assertEqual((run["kind"], run["status"], run["duration_s"]), ("speech", "ok", 0.4))

    def test_record_speech_analysis_degraded(self):
        did = self.svc.start_dossier("t", "x")
        self.svc.record_speech_analysis(
            did, {"conclusion": "理性探讨", "degraded_components": ["logic"]})
        run = self.store.runs(did)[0]
        self.assertEqual(run["status"], "degraded")
        self.assertTrue(run["degraded"])

    def test_record_heat_snapshot(self):
        """热度快照落库后可读回：坐标/E 分/档位 profile/代理标记原样往返。"""
        did = self.svc.start_dossier("t", "x")
        row_id = self.svc.record_heat(did, {"x": 1.5, "y": -2.0, "z": 3.0, "score": 55.1,
                                            "profile": "spec", "proxy_flags": ["proxy:y"]})
        self.assertIsInstance(row_id, int)
        snap = self.store.snapshots(did)[0]
        self.assertEqual((snap["x"], snap["y"], snap["z"], snap["e_score"]),
                         (1.5, -2.0, 3.0, 55.1))
        self.assertEqual(snap["profile"], "spec")
        self.assertEqual(snap["proxy_flags"], ["proxy:y"])

    def test_record_opinions_uses_latest_batch(self):
        """观点光谱挂到当前最新证据批次上：首档 batch=1，recheck 后再记 → batch=2。"""
        did = self.svc.start_dossier("t", "x")
        self.svc.record_analysis(did, agent_result(entries=ENTRIES_V1))
        self.svc.record_opinions(did, {"distribution": {"支持": 3, "反对": 2, "中立": 1},
                                       "summary": "两极分化", "degraded": False})
        op = self.store.opinions(did)[0]
        self.assertEqual(op["batch"], 1)
        self.assertEqual(op["distribution"], {"支持": 3, "反对": 2, "中立": 1})
        self.assertEqual(op["summary"], "两极分化")
        self.assertFalse(op["degraded"])
        # recheck 出 batch=2 之后再记观点 → 挂到 2
        self.svc.record_analysis(did, agent_result(label="存疑待核实", risk=40,
                                                   entries=ENTRIES_V2))
        self.svc.record_opinions(did, {"distribution": {"支持": 1}, "summary": "样本少",
                                       "degraded": True})
        ops = self.store.opinions(did)
        self.assertEqual([o["batch"] for o in ops], [1, 2])
        self.assertTrue(ops[1]["degraded"])

    def test_record_deep_read(self):
        """深读 payload 原样往返（含降级标记与护栏丢弃清单）。"""
        did = self.svc.start_dossier("t", "x")
        payload = {"sections": [{"heading": "信源", "text": "……"}], "degraded": True,
                   "guard_dropped": ["越权小节"]}
        self.svc.record_deep_read(did, payload)
        dr = self.store.deep_reads(did)[0]
        self.assertEqual(dr["payload"]["sections"][0]["heading"], "信源")
        self.assertTrue(dr["degraded"])
        self.assertEqual(dr["guard_dropped"], ["越权小节"])


class TestCompareAndDegradation(DossierServiceTestBase):
    def test_compare_versions_changed_with_batch_counts(self):
        """版本对比：标签变了 → changed=True；versions 全量、证据批次计数正确。"""
        did = self.svc.start_dossier("t", "x")
        self.svc.record_analysis(did, agent_result(label="疑似谣言", risk=62, entries=ENTRIES_V1))
        self.svc.record_analysis(did, agent_result(label="存疑待核实", risk=40,
                                                   entries=ENTRIES_V2))
        cmp = self.svc.compare_versions(did)
        self.assertTrue(cmp["changed"])
        self.assertEqual([(v["version"], v["label"], v["risk_score"]) for v in cmp["versions"]],
                         [(1, "疑似谣言", 62), (2, "存疑待核实", 40)])
        self.assertTrue(all(v["as_of"] for v in cmp["versions"]))
        self.assertEqual(cmp["evidence_batches"], {1: 3, 2: 5})

    def test_compare_versions_single_not_changed(self):
        """只有一版 → changed=False（首查没有"变化"可言）。"""
        did = self.svc.start_dossier("t", "x")
        self.svc.record_analysis(did, agent_result(entries=ENTRIES_V1))
        cmp = self.svc.compare_versions(did)
        self.assertFalse(cmp["changed"])
        self.assertEqual(len(cmp["versions"]), 1)
        self.assertEqual(cmp["evidence_batches"], {1: 3})

    def test_compare_versions_same_label_not_changed(self):
        """两版但标签相同（分数变了）→ changed=False：对比看的是标签口径变化。"""
        did = self.svc.start_dossier("t", "x")
        self.svc.record_analysis(did, agent_result(label="疑似谣言", risk=62, entries=ENTRIES_V1))
        self.svc.record_analysis(did, agent_result(label="疑似谣言", risk=58, entries=ENTRIES_V2))
        self.assertFalse(self.svc.compare_versions(did)["changed"])

    def test_closed_store_degrades_without_raise(self):
        """降级路径：档案库已关闭 → record 不抛异常，返回 degraded/error 标记，
        version/card/batch 为 None（写不进的部分如实缺省，不编造）。"""
        did = self.svc.start_dossier("t", "x")
        self.svc.close()  # 底层连接已关
        out = self.svc.record_analysis(did, agent_result(entries=ENTRIES_V1))
        self.assertTrue(out.get("degraded"))
        self.assertTrue(out.get("error"))
        self.assertIn("ProgrammingError", out["error"])            # sqlite 连接已关
        self.assertIsNone(out["version"])
        self.assertIsNone(out["card"])
        self.assertIsNone(out["batch"])
        out2 = self.svc.record_speech_analysis(did, {"conclusion": "理性探讨"})
        self.assertTrue(out2.get("degraded"))
        self.assertIsNone(out2["version"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
