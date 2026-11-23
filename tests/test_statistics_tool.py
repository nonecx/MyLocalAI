# -*- coding: utf-8 -*-
"""统计说法核查工具层测试（tools_statistics）：mock 服务与流水线替身，全离线。

分组：
    1. 注册与校验（4）：注册元数据（params/permission/cost/est_s/描述）、text 参数
       校验、首尾空白剥掉、未声明键丢弃；
    2. 服务缺失诚实降级（5）：ctx.services 缺失、工厂键缺失、工厂抛异常、run 抛
       异常、返回坏形状——全部 ok=False + 诚实 digest，handler 永不抛；
    3. 卡片字段逐字（3）：card_type/status/as_of/degraded 透传、claims 视图逐字、
       counts 键名与取值；
    4. digest（4）：逐字转述判语与计数、无真假判定词、零主张合法空出口措辞、
       末尾导流 verify_claim；
    5. 死线/取消/进度透传（3）：cancel_check 同一实例、llm_cache 死线透传、
       events → progress 发射器（None → None）；
    6. 不建档（2）：dossier_id=None/not_saved/save_reason、不返 claim_answer；
    7. 端到端（1）：真流水线经服务工厂跑通，零网络。

全离线：服务工厂返回注入了假 search_fn/fetch_fn 的真流水线或替身，零网络零 LLM。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import tools_statistics  # noqa: E402
from mylocalai.assistant.registry import ToolRegistry, ValidationError  # noqa: E402
from mylocalai.assistant.skills.statistics import StatisticsSkills  # noqa: E402
from mylocalai.llm import llm_cache  # noqa: E402

CLOCK = "2026-11-23T00:00:00+00:00"
PAGE = "https://stats.example.gov.cn/tongji-2023.html"

#: 固定真假判定词（铁律 1）：任何出口一个都不许出现
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")


def _tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    tools_statistics.register_all(reg, None)
    return reg


def _ctx(services: Any, cancel_check=None, events=None) -> SimpleNamespace:
    return SimpleNamespace(sessions=None, dossiers=None, events=events,
                           cancel_check=cancel_check, services=services)


def _stub_skills(result: Any, raises: Any = None):
    """StatisticsSkills 替身：记录 run 实参后回预设结果（工具层透传口径锁定）。"""
    calls: list = []

    def _run(text, *, cancel_check=None, deadline=None, on_progress=None):
        calls.append({"text": text, "cancel_check": cancel_check,
                      "deadline": deadline, "on_progress": on_progress})
        if raises is not None:
            raise raises
        return result

    def _factory():
        return SimpleNamespace(run=_run)

    _factory.calls = calls
    return _factory


def _sample_result(status: str = "completed", task_status: str = "completed",
                   state: str = "ok") -> Dict[str, Any]:
    """一次成功核查的运行结果替身（字段与 pipeline 实际返回逐字一致）。"""
    claim = {"claim_id": "C1", "excerpt": "失业率为 5.2%", "number": 5.2,
             "unit": "%", "metric": "城镇调查失业率",
             "qualifiers": {"time": "2023 年", "place": "本地",
                            "entity": "", "base": "全国"},
             "verdict": "consistent", "source_number": "5.2%",
             "source_url": PAGE, "source_title": "2023 年统计公报",
             "quote": "2023 年本地失业率平均为 5.2%", "as_of": CLOCK,
             "basis": "来源段落数字「5.2%」与主张数字「5.2%」一致（百分比）"}
    return {
        "tool_id": "check_statistics", "status": status, "task_status": task_status,
        "data": {"text": "2023 年本地失业率为 5.2%", "state": state,
                 "claims": [claim],
                 "limits": ["本核查只依据本次实际读取的页面正文"],
                 "counts": {"claims": 1, "searches": 1, "pages": 1,
                            "llm_calls": 0}},
        "gaps": [], "errors": [],
        "audit": {"operations": [], "searches": 1, "pages": 1, "llm_calls": 0},
        "budget": {"used": {"max_searches": 1, "max_pages": 1, "max_llm_calls": 0},
                   "limits": {}},
        "report": "统计说法核查报告（只比对数字口径，不判内容真假）",
        "started_at": CLOCK, "finished_at": CLOCK,
    }


def _real_services() -> Any:
    """真流水线 + 假检索/取页（零网络）；返回服务工厂。"""
    def _search(query, k):
        return {"results": [{"url": PAGE, "title": "2023 年统计公报",
                             "snippet": ""}], "degraded": False, "error": ""}

    def _fetch(url, *, allowed_domains=None, cancel_check=None, deadline=None):
        return {"status": "ok", "url": url, "final_url": url, "domain": "stats.example.gov.cn",
                "title": "2023 年统计公报",
                "text": "2023 年本地失业率平均为 5.2%。",
                "bytes": 10, "content_type": "text/html", "hops": [],
                "fetched_at": CLOCK}

    def _factory():
        return StatisticsSkills(search_fn=_search, fetch_fn=_fetch,
                                clock=lambda: CLOCK)

    return _factory


# ===========================================================================
# 1. 注册与校验（4）
# ===========================================================================
class TestRegistration(unittest.TestCase):
    def test_registered_metadata(self):
        reg = _tool_registry()
        spec = reg.get("check_statistics")
        self.assertEqual(spec["params"], ["text"])
        self.assertEqual(spec["permission"], "read")     # 只读核查：不建档
        self.assertEqual(spec["cost"], "medium")
        self.assertEqual(spec["est_s"], 30.0)
        self.assertIn("check_statistics", reg.names())
        self.assertIn("统计", spec["description"])
        self.assertIn("不判内容真假", spec["description"])

    def test_text_param_validation(self):
        reg = _tool_registry()
        for bad in ("", "   ", None, 123, ["x"], "长" * 4001):
            with self.subTest(text=bad):
                with self.assertRaises(ValidationError):
                    reg.validate("check_statistics", {"text": bad})
        clean = reg.validate("check_statistics", {"text": "  2023 年失业率为 5.2%  "})
        self.assertEqual(clean, {"text": "2023 年失业率为 5.2%"})   # 首尾空白剥掉
        junk = reg.validate("check_statistics", {"text": "失业率为 5.2%", "hack": 1})
        self.assertEqual(junk, {"text": "失业率为 5.2%"})           # 未声明键丢弃


# ===========================================================================
# 2. 服务缺失诚实降级（5）
# ===========================================================================
class TestDegradation(unittest.TestCase):
    def _call(self, services, text="2023 年本地失业率为 5.2%"):
        reg = _tool_registry()
        spec = reg.get("check_statistics")
        assert spec is not None, "check_statistics 未注册"
        return spec["handler"](_ctx(services), text=text)

    def test_missing_services_degrades(self):
        out = self._call(None)
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIsNone(out["card"])
        self.assertIn("不可用", out["digest"])
        self.assertIn("统计说法核查", out["digest"])

    def test_missing_factory_key_degrades(self):
        out = self._call({})
        self.assertFalse(out["ok"])
        self.assertIn("装配失败", out["digest"])

    def test_factory_exception_degrades(self):
        def _boom():
            raise RuntimeError("装配炸了")
        out = self._call({"statistics_skills": _boom})
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])

    def test_run_exception_degrades(self):
        broken = _stub_skills(None, raises=RuntimeError("核查爆炸"))
        out = self._call({"statistics_skills": broken})
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIsNone(out["card"])
        self.assertIn("不可用", out["digest"])
        self.assertIn("执行异常", out["digest"])

    def test_bad_shape_degrades(self):
        out = self._call({"statistics_skills": _stub_skills("not-a-dict")})
        self.assertFalse(out["ok"])
        self.assertIn("形状不符", out["digest"])


# ===========================================================================
# 3. 卡片字段逐字（3）
# ===========================================================================
class TestCard(unittest.TestCase):
    def _call(self, services, text="2023 年本地失业率为 5.2%"):
        reg = _tool_registry()
        spec = reg.get("check_statistics")
        return spec["handler"](_ctx(services), text=text)

    def test_card_shell_fields_passthrough(self):
        out = self._call({"statistics_skills": _stub_skills(_sample_result())})
        card = out["card"]
        self.assertEqual(card["card_type"], "statistics")
        self.assertEqual(card["status"], "completed")
        self.assertEqual(card["task_status"], "completed")
        self.assertEqual(card["as_of"], CLOCK)
        self.assertFalse(card["degraded"])
        self.assertEqual(card["state"], "ok")
        self.assertEqual(card["state_label"], "已完成口径比对（含「未核实到」的合法结论）")
        self.assertEqual(card["text"], "2023 年本地失业率为 5.2%")

    def test_card_claims_verbatim(self):
        sample = _sample_result()
        out = self._call({"statistics_skills": _stub_skills(sample)})
        claim = out["card"]["claims"][0]
        source = sample["data"]["claims"][0]
        for key in ("claim_id", "excerpt", "number", "unit", "metric",
                    "qualifiers", "verdict", "source_number", "source_url",
                    "source_title", "quote", "as_of", "basis"):
            self.assertEqual(claim[key], source[key], key)
        self.assertEqual(claim["verdict_label"], "与来源一致")

    def test_card_counts_and_degraded(self):
        out = self._call({"statistics_skills":
                          _stub_skills(_sample_result(status="partial"))})
        card = out["card"]
        self.assertEqual(tuple(sorted(card["counts"])),
                         ("claims", "llm_calls", "pages", "searches"))
        self.assertEqual(card["counts"],
                         {"claims": 1, "searches": 1, "pages": 1, "llm_calls": 0})
        self.assertTrue(card["degraded"])                 # partial 如实标降级
        self.assertTrue(out["ok"])                        # partial 不算失败


# ===========================================================================
# 4. digest（4）
# ===========================================================================
class TestDigest(unittest.TestCase):
    def _digest(self, result, **kw):
        reg = _tool_registry()
        spec = reg.get("check_statistics")
        out = spec["handler"](_ctx({"statistics_skills": _stub_skills(result)}),
                              text=result["data"].get("text") or "x", **kw)
        return out["digest"], out

    def test_digest_transcribes_verdicts_and_counts(self):
        digest, _ = self._digest(_sample_result())
        self.assertIn("[统计说法核查]", digest)
        self.assertIn("不判内容真假", digest)
        self.assertIn("与来源一致", digest)
        self.assertIn("5.2%", digest)
        self.assertIn("主张 1 条", digest)
        self.assertIn("来源数字", digest)

    def test_digest_has_no_verdict_words(self):
        digest, _ = self._digest(_sample_result())
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, digest)

    def test_digest_empty_exit_wording(self):
        sample = _sample_result(task_status="no_factual_claim")
        sample["data"]["claims"] = []
        sample["data"]["counts"]["claims"] = 0
        digest, out = self._digest(sample)
        self.assertIn("没有识别到可核对的数字主张", digest)
        self.assertTrue(out["ok"])                        # 合法空出口不算失败

    def test_digest_routes_to_verify_claim(self):
        digest, _ = self._digest(_sample_result())
        self.assertIn("verify_claim", digest)
        self.assertIn("verify_authority", digest)


# ===========================================================================
# 5. 死线/取消/进度透传（3）
# ===========================================================================
class TestPassthrough(unittest.TestCase):
    def _call(self, services, cancel_check=None, events=None):
        reg = _tool_registry()
        spec = reg.get("check_statistics")
        return spec["handler"](_ctx(services, cancel_check, events),
                               text="2023 年本地失业率为 5.2%")

    def test_cancel_and_deadline_passthrough(self):
        skills = _stub_skills(_sample_result())

        def cancel_check():
            return False

        with llm_cache.deadline_scope(30):
            expected = llm_cache.deadline_at()
            self._call({"statistics_skills": skills}, cancel_check=cancel_check)
        call = skills.calls[-1]
        self.assertEqual(call["text"], "2023 年本地失业率为 5.2%")
        self.assertIs(call["cancel_check"], cancel_check)
        self.assertEqual(call["deadline"], expected)

    def test_progress_emitter_with_events(self):
        emitted: List[Dict[str, Any]] = []

        class _Events:
            def emit(self, name, **fields):
                emitted.append({"name": name, **fields})

        skills = _stub_skills(_sample_result())
        self._call({"statistics_skills": skills}, events=_Events())
        emitter = skills.calls[-1]["on_progress"]
        self.assertTrue(callable(emitter))
        emitter("检索资料", 3, 6)
        self.assertEqual(emitted, [{"name": "progress", "component": "检索资料",
                                    "done": 3, "total": 6}])

    def test_no_events_means_no_progress_hook(self):
        skills = _stub_skills(_sample_result())
        self._call({"statistics_skills": skills}, events=None)
        self.assertIsNone(skills.calls[-1]["on_progress"])


# ===========================================================================
# 6. 不建档（2）
# ===========================================================================
class TestNoDossier(unittest.TestCase):
    def _call(self, services):
        reg = _tool_registry()
        spec = reg.get("check_statistics")
        return spec["handler"](_ctx(services), text="2023 年本地失业率为 5.2%")

    def test_read_only_never_saves(self):
        out = self._call({"statistics_skills": _stub_skills(_sample_result())})
        self.assertIsNone(out["dossier_id"])              # 只读核查：不建档
        self.assertTrue(out["not_saved"])
        self.assertEqual(out["save_reason"], "read_only_capability_no_dossier")
        self.assertNotIn("claim_answer", out)            # 不返核查结论式字段
        self.assertNotIn("score", out)
        self.assertNotIn("dossier", out)

    def test_degraded_exit_also_never_saves(self):
        broken = _stub_skills(None, raises=RuntimeError("炸了"))
        out = self._call({"statistics_skills": broken})
        self.assertIsNone(out["dossier_id"])
        self.assertTrue(out["not_saved"])
        self.assertNotIn("claim_answer", out)


# ===========================================================================
# 7. 端到端（1）
# ===========================================================================
class TestEndToEnd(unittest.TestCase):
    def test_real_pipeline_through_service_factory(self):
        reg = _tool_registry()
        spec = reg.get("check_statistics")
        out = spec["handler"](_ctx({"statistics_skills": _real_services()}),
                              text="2023 年本地失业率为 5.2%")
        self.assertTrue(out["ok"])
        self.assertEqual(out["card"]["card_type"], "statistics")
        self.assertEqual(out["card"]["claims"][0]["verdict"], "consistent")
        self.assertIn("与来源一致", out["digest"])
        self.assertIn("verify_claim", out["digest"])
        self.assertIsNone(out["dossier_id"])
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, out["digest"])
            self.assertNotIn(word, out["report"])


if __name__ == "__main__":
    unittest.main()
