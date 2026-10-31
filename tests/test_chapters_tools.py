# -*- coding: utf-8 -*-
"""章节类工具测试（工作指导 §5.2/§6 M3-3/M3-5）：read_opinions / deep_read handler。

对着真实事故形态锁定：
  - 正例：stub 服务返回 payload → record_opinions / record_deep_read 落库，
    store 读回一致（工具不是第二个事实源，只是服务的落库出口）；
  - 不存在 id → ok=False + not_found，不新建档案、不落任何行；
  - 服务缺失（KeyError）/ 抛错 → 诚实 error 返回、不抛（loop 层不会因此崩）；
  - 未触发 / applicable=false 是合法出口：ok=True 且 degraded=False，digest 如实说明；
  - degraded 透传：服务契约的 bool 形状与引擎原始的降级记录 list 形状都认；
  - digest 免责口径：不含真假判定词（只述立场与解读，不判真假）；
  - registry 规格：permission="write"、cost="medium"、参数只有 id（id_param 校验）。
全离线：:memory: 库 + stub services，零网络零 LLM。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import tools_chapters  # noqa: E402
from mylocalai.assistant.registry import ToolRegistry, ValidationError  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402

# digest 里不允许出现的真假判定词（铁律：不判真假，只述证据）
_FORBIDDEN_VERDICT_WORDS = ("是谣言", "是假的", "确认为真", "确认为假",
                            "该转发", "建议转发", "判定为真", "判定为假")


def stub_service(payload=None, error=None):
    """按契约形状 stub 的章节服务：记录调用参数；error 非 None 时抛。"""
    calls = []

    def _svc(dossier_id):
        calls.append(dossier_id)
        if isinstance(error, Exception):
            raise error
        return dict(payload or {})

    _svc.calls = calls
    return _svc


class ChaptersToolsTestBase(unittest.TestCase):
    def setUp(self):
        self.service = DossierService(DossierStore(":memory:"))
        self.addCleanup(self.service.close)
        self.registry = ToolRegistry()
        tools_chapters.register_all(self.registry, None)
        self.dossier_id = self.service.start_dossier("自来水被下毒的说法", input_kind="text")

    def ctx(self, services):
        return SimpleNamespace(sessions=None, dossiers=self.service, events=None,
                               cancel_check=None, services=services)

    def call(self, name, services, **args):
        # 经注册表 validate → handler（与 loop 的真实调用路径同源）
        clean = self.registry.validate(name, args)
        spec = self.registry.get(name)
        assert spec is not None, name
        return spec["handler"](self.ctx(services), **clean)


class TestChaptersRegistry(ChaptersToolsTestBase):
    def test_registry_spec_and_id_validation(self):
        """两工具：write 权限、medium 成本、est_s>0、参数只有 id；坏 id 在校验层拒绝。"""
        for name in ("read_opinions", "deep_read"):
            spec = self.registry.get(name)
            with self.subTest(tool=name):
                self.assertIsNotNone(spec)
                self.assertEqual(spec["permission"], "write")
                self.assertEqual(spec["cost"], "medium")
                self.assertGreater(spec["est_s"], 0.0)
                self.assertEqual(spec["params"], ["id"])
        # 校验器：0 / 负数 / 字符串 / None / bool 都拒绝，正整数放行
        for bad in (0, -1, "3", None, True, 1.5):
            for name in ("read_opinions", "deep_read"):
                with self.subTest(tool=name, id=bad):
                    with self.assertRaises(ValidationError):
                        self.registry.validate(name, {"id": bad})
        clean = self.registry.validate("deep_read", {"id": 7, "extra": "丢弃"})
        self.assertEqual(clean, {"id": 7})

    def test_missing_dossier_honest_not_found(self):
        """不存在 id → ok=False + not_found，不新建档案、不落任何章节行。"""
        before = self.service.store.list_dossiers(limit=50)
        for name, key in (("read_opinions", "opinions"), ("deep_read", "deep_read")):
            with self.subTest(tool=name):
                svc = stub_service(payload={"distribution": {}, "triggered": True})
                out = self.call(name, {key: svc}, id=99999)
                self.assertFalse(out["ok"])
                self.assertTrue(out.get("not_found"))
                self.assertFalse(out["degraded"])
                self.assertIn("不存在", out["digest"])
                self.assertEqual(svc.calls, [], "档案不存在时不应调用章节服务")
                self.assertEqual(self.service.store.opinions(99999), [])
                self.assertEqual(self.service.store.deep_reads(99999), [])
        self.assertEqual(self.service.store.list_dossiers(limit=50), before,
                         "不存在 id 不得新建档案")


class TestReadOpinions(ChaptersToolsTestBase):
    def test_positive_writes_chapter_and_digest(self):
        """正例：distribution/summary 落库读回一致；digest 带分布计数与免责口径。"""
        payload = {"distribution": {"支持": 3, "反驳": 2, "无关": 1},
                   "summary": "两派说法并存，均未给出可核实来源。",
                   "degraded": False, "triggered": True}
        svc = stub_service(payload)
        out = self.call("read_opinions", {"opinions": svc}, id=self.dossier_id)
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertIsNone(out["card"])
        self.assertEqual(out["dossier_id"], self.dossier_id)
        self.assertEqual(svc.calls, [self.dossier_id])
        self.assertEqual(out["cost"]["requests"], 1, "触发的一次完整分析粗记 1 轮请求")
        # 落库读回一致（record_opinions 只存 distribution/summary/degraded 三列）
        rows = self.service.store.opinions(self.dossier_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["distribution"], {"支持": 3, "反驳": 2, "无关": 1})
        self.assertEqual(rows[0]["summary"], payload["summary"])
        self.assertFalse(rows[0]["degraded"])
        # digest：立场分布计数 + 要点 + 免责口径
        self.assertIn("支持 3", out["digest"])
        self.assertIn("反驳 2", out["digest"])
        self.assertIn("共 6 条", out["digest"])
        self.assertIn(payload["summary"], out["digest"])
        self.assertIn("不构成真假判定", out["digest"])
        for word in _FORBIDDEN_VERDICT_WORDS:
            self.assertNotIn(word, out["digest"])

    def test_not_triggered_recorded_with_reason(self):
        """未触发（如已有权威来源）是合法出口：ok=True、degraded=False；
        落库的 summary 如实带上未触发原因（读回一致），digest 说明"不硬析"。"""
        payload = {"distribution": {}, "summary": "", "degraded": False,
                   "triggered": False,
                   "skip_reason": "已有官方或权威媒体来源，不走民间观点分析"}
        svc = stub_service(payload)
        out = self.call("read_opinions", {"opinions": svc}, id=self.dossier_id)
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["cost"]["requests"], 0, "未触发只读了档案证据，零网络")
        self.assertIn("未触发", out["digest"])
        self.assertIn("已有官方或权威媒体来源", out["digest"])
        rows = self.service.store.opinions(self.dossier_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "未触发：已有官方或权威媒体来源，不走民间观点分析")
        self.assertEqual(rows[0]["distribution"], {})

    def test_degraded_bool_and_engine_list_shapes(self):
        """degraded 透传：服务契约 bool 与引擎原始降级记录 list 两种形状都如实标注。"""
        bool_payload = {"distribution": {"支持": 1}, "summary": "s", "degraded": True,
                        "degraded_reason": "未启用大模型：论据抽取与聚类走规则",
                        "triggered": True}
        out = self.call("read_opinions", {"opinions": stub_service(bool_payload)},
                        id=self.dossier_id)
        self.assertTrue(out["degraded"])
        self.assertIn("未启用大模型", out["digest"])
        rows = self.service.store.opinions(self.dossier_id)
        self.assertTrue(rows[-1]["degraded"])

        list_payload = {"distribution": {"反驳": 2}, "summary": "s", "triggered": True,
                        "degraded": [{"url": "https://a/1", "reason": "抓取失败，改用摘要兜底"}]}
        out2 = self.call("read_opinions", {"opinions": stub_service(list_payload)},
                         id=self.dossier_id)
        self.assertTrue(out2["degraded"])
        self.assertIn("抓取失败", out2["digest"])
        self.assertTrue(self.service.store.opinions(self.dossier_id)[-1]["degraded"])

    def test_service_missing_or_error_returned_not_raised(self):
        """服务缺失（KeyError）/ 抛错 → 诚实 error 返回、不抛；不落任何行。"""
        out = self.call("read_opinions", {}, id=self.dossier_id)
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIn("未接入", out["digest"])

        svc = stub_service(error=RuntimeError("上游超时"))
        out2 = self.call("read_opinions", {"opinions": svc}, id=self.dossier_id)
        self.assertFalse(out2["ok"])
        self.assertTrue(out2["degraded"])
        self.assertIn("RuntimeError", out2["digest"])
        self.assertEqual(self.service.store.opinions(self.dossier_id), [],
                         "服务失败时不得落任何章节行")

        out3 = self.call("read_opinions", {"opinions": stub_service(None)},
                         id=self.dossier_id)
        self.assertFalse(out3["ok"])
        self.assertTrue(out3["degraded"])
        self.assertIn("无输出", out3["digest"])


class TestDeepReadTool(ChaptersToolsTestBase):
    def test_triggered_text_roundtrip(self):
        """正例：triggered+applicable → 落库读回 payload 逐字段一致；digest 带解读要点。"""
        payload = {"triggered": True, "applicable": True, "text": "张力：信息发布节奏与公众知情需求。",
                   "degraded": False, "guard_dropped": [], "source": "llm",
                   "result": {"issue_tags": [{"id": "info_transparency_trust"}]}}
        svc = stub_service(payload)
        out = self.call("deep_read", {"deep_read": svc}, id=self.dossier_id)
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["dossier_id"], self.dossier_id)
        self.assertTrue(out["triggered"])
        self.assertIn("解读要点", out["digest"])
        self.assertIn("信息发布节奏", out["digest"])
        rows = self.service.store.deep_reads(self.dossier_id)
        self.assertEqual(len(rows), 1)
        got = rows[0]["payload"]
        self.assertEqual(got["triggered"], True)
        self.assertEqual(got["text"], payload["text"])
        self.assertEqual(got["result"], payload["result"])
        self.assertEqual(got["guard_dropped"], [])
        self.assertFalse(rows[0]["degraded"])

    def test_not_triggered_is_legal_exit_not_failure(self):
        """triggered=False（未达阈值）是合法出口：ok=True、degraded=False、digest 如实说明。"""
        payload = {"triggered": False, "applicable": False, "text": "", "source": "",
                   "degraded": False, "guard_dropped": [],
                   "reason": "E=50.3415 未超过深读阈值 66（档位 默认，absolute 模式），不生成解读"}
        out = self.call("deep_read", {"deep_read": stub_service(payload)}, id=self.dossier_id)
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["cost"]["requests"], 0)
        self.assertIn("未触发深读", out["digest"])
        self.assertIn("未超过深读阈值", out["digest"])
        self.assertIn("合法出口", out["digest"])
        row = self.service.store.deep_reads(self.dossier_id)[0]
        self.assertFalse(row["payload"]["triggered"])
        self.assertFalse(row["degraded"])

    def test_applicable_false_exit_with_reason(self):
        """applicable=False（纯情绪/证据不足）：digest 如实"不适用"+原因，不是失败。"""
        payload = {"triggered": True, "applicable": False, "text": "", "source": "llm",
                   "degraded": False, "guard_dropped": [],
                   "reason": "纯情绪宣泄，无社会议题可分析"}
        out = self.call("deep_read", {"deep_read": stub_service(payload)}, id=self.dossier_id)
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertIn("深读不适用", out["digest"])
        self.assertIn("纯情绪宣泄", out["digest"])
        self.assertIn("不为有输出而编", out["digest"])
        row = self.service.store.deep_reads(self.dossier_id)[0]
        self.assertFalse(row["payload"]["applicable"])

    def test_guard_dropped_and_degraded_passthrough(self):
        """护栏丢弃 + degraded 透传：digest 记丢弃条数，degraded 如实，落库一致。"""
        payload = {"triggered": True, "applicable": True,
                   "text": "张力：A 与 B 之间的张力。",
                   "degraded": True, "degraded_reason": "部分条目被护栏丢弃，详见 guard_dropped",
                   "guard_dropped": ["phenomena 的 evidence_ids ['e999'] 不在有效证据集内，已剔除"],
                   "source": "llm"}
        out = self.call("deep_read", {"deep_read": stub_service(payload)}, id=self.dossier_id)
        self.assertTrue(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIn("护栏丢弃 1 条", out["digest"])
        self.assertIn("e999", out["digest"])
        row = self.service.store.deep_reads(self.dossier_id)[0]
        self.assertTrue(row["degraded"])
        self.assertEqual(row["guard_dropped"], payload["guard_dropped"])

    def test_service_missing_or_error_returned_not_raised(self):
        """服务缺失 / 抛错 / 无输出 → 诚实 error 返回、不抛；不落任何行。"""
        out = self.call("deep_read", {}, id=self.dossier_id)
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIn("未接入", out["digest"])

        svc = stub_service(error=ValueError("bad detail"))
        out2 = self.call("deep_read", {"deep_read": svc}, id=self.dossier_id)
        self.assertFalse(out2["ok"])
        self.assertTrue(out2["degraded"])
        self.assertIn("ValueError", out2["digest"])

        out3 = self.call("deep_read", {"deep_read": stub_service(None)}, id=self.dossier_id)
        self.assertFalse(out3["ok"])
        self.assertTrue(out3["degraded"])
        self.assertEqual(self.service.store.deep_reads(self.dossier_id), [],
                         "服务失败时不得落任何章节行")

    def test_digest_disclaimer_no_verdict_words(self):
        """digest 免责口径：口径声明在场，且不含任何真假判定词。"""
        payload = {"triggered": True, "applicable": True,
                   "text": "多条发言指向通报时间晚于网络传闻。", "degraded": False,
                   "guard_dropped": [], "source": "llm"}
        out = self.call("deep_read", {"deep_read": stub_service(payload)}, id=self.dossier_id)
        self.assertIn("不构成真假判定", out["digest"])
        self.assertIn("不改变关注度分数", out["digest"])
        for word in _FORBIDDEN_VERDICT_WORDS:
            self.assertNotIn(word, out["digest"])


if __name__ == "__main__":
    unittest.main()
