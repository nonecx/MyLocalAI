# -*- coding: utf-8 -*-
"""M3 档案库读面测试（§6 M3-2 / M3-6 / M3-7，§7.2 test_reminders/export 一组）：
   chapters() 六章节聚合（含 absent / trend pending 分支）、export_markdown()
   六章节导出（六节标题 + 免责语 + as_of + 徽章 + absent 文案）、
   reminders 被动提醒纯函数矩阵（年龄 / 快照变化 / 规则写坏退默认 / 聚合去重）。

全程离线临时库（TemporaryDirectory），不联网、不调 LLM；trend 组件由并行工作流
交付——pending 分支用 sys.modules 注入 None 强制 ImportError 模拟，成功分支注入
stub 模块，两个分支都不依赖 trend.py 是否已经落地。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_dossier_reminders -v
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import reminders as reminders_mod  # noqa: E402
from mylocalai.assistant.reminders import (  # noqa: E402
    DEFAULT_RULES,
    aggregate_reminder_lines,
    load_reminder_rules,
    reminder_lines,
)
from mylocalai.dossier.service import DossierService, EXPORT_DISCLAIMER  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402

SIX_CHAPTER_KEYS = {"conclusion", "evidence", "speech", "heat", "opinions", "deep_read"}

ABSENT_NOTE = "本节暂无数据"

ENTRY = {"url": "https://example.gov.cn/a", "domain": "example.gov.cn",
         "domain_tier": "官方", "source_kind": "官方（监管部门）", "stance": "反驳",
         "title": "官方辟谣", "snippet": "通报称无此案", "fetched_at": "2026-10-30T22:00:00+08:00"}


def _entry(url: str, stance: str = "支持", kind: str = "非官方") -> dict:
    e = dict(ENTRY)
    e.update({"url": url, "domain": url.split("/")[2], "stance": stance, "source_kind": kind})
    return e


def agent_result(label: str = "疑似谣言", risk: float = 62.0, entries=None,
                 degraded=None) -> dict:
    return {"label": label, "risk_score": risk,
            "degraded_components": list(degraded or []),
            "evidence_book": {"entries": list(entries or [])}}


class DossierReadTestBase(unittest.TestCase):
    """公共脚手架：独立临时库 + 服务门面 + 一个预置齐全数据的档案。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DossierStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)
        self.svc = DossierService(store=self.store)
        self.addCleanup(self.svc.close)

    def make_full_dossier(self, title: str = "隔夜茶致癌？") -> int:
        """建一个六章都有数据的档案（verify 结论 + 言论土壤 + 快照 + 观点 + 深读）。"""
        did = self.svc.start_dossier(title, f"{title}（网传）")
        self.svc.record_analysis(did, agent_result(entries=[_entry("https://a.example.com/1")]),
                                 kind="verify")
        self.svc.record_speech_analysis(did, {
            "conclusion": "疑似情绪化谣言",
            "emotion": {"emotion_tendency": "焦虑", "emotion_score": 7},
            "attack": {"aggression_level": "中", "attack_target": "专家"},
            "logic": {"logic_relevance": "弱", "logic_fallacy": "诉诸恐惧"}})
        self.svc.record_heat(did, {"x": 3, "y": 12000, "z": 45, "score": 51.2,
                                   "profile": "spec", "proxy_flags": ["proxy:y"]})
        self.svc.record_opinions(did, {"distribution": {"支持": 3, "反对": 2, "中立": 1},
                                       "summary": "两极分化", "degraded": False})
        self.svc.record_deep_read(did, {"triggered": True, "text": "张力：信息节奏 vs 知情需求",
                                        "degraded": True, "guard_dropped": ["越权小节"]})
        return did

    def set_updated_at(self, did: int, days_ago: float,
                       now: datetime | None = None) -> None:
        """把档案 updated_at 拨回 days_ago 天前（提醒年龄规则的测试钩子）。

        锚点默认真实当前时间；测试注入固定 now 时必须传同一个 now，保证
        elapsed 恰为 days_ago 天（int(days) 不出现 19.99→19 的漂移）。
        """
        anchor = now or datetime.now().astimezone()
        stamp = (anchor - timedelta(days=days_ago)).isoformat(timespec="seconds")
        self.store._conn.execute("UPDATE dossiers SET updated_at = ? WHERE id = ?", (stamp, did))
        self.store._conn.commit()


# ================================================================
# 1. chapters() 六章节聚合
# ================================================================
class TestChapters(DossierReadTestBase):
    def test_full_dossier_has_all_six_chapters(self):
        """六章齐全：conclusion 带全部版本与 compare；evidence 按批次；speech 四档字段。"""
        did = self.make_full_dossier()
        ch = self.svc.chapters(did)
        self.assertEqual(set(ch), SIX_CHAPTER_KEYS)

        concl = ch["conclusion"]
        self.assertEqual(concl["count"], 2)   # verify + speech 各落一版
        self.assertEqual([v["version"] for v in concl["compare"]["versions"]], [1, 2])
        self.assertTrue(concl["compare"]["changed"])
        self.assertFalse(concl["degraded"])
        self.assertEqual(len(concl["runs"]), 2)

        self.assertEqual(ch["evidence"]["total"], 1)
        self.assertEqual(ch["evidence"]["batches"][0]["count"], 1)
        self.assertEqual(ch["evidence"]["batches"][0]["entries"][0]["url"],
                         "https://a.example.com/1")

        speech = ch["speech"]
        self.assertNotIn("chapter_status", speech)
        self.assertEqual(speech["conclusion"], "疑似情绪化谣言")
        self.assertEqual(speech["emotion"]["emotion_tendency"], "焦虑")
        self.assertEqual(speech["attack"]["aggression_level"], "中")
        self.assertEqual(speech["logic"]["logic_fallacy"], "诉诸恐惧")
        self.assertEqual(speech["version"], 2)
        self.assertTrue(speech["as_of"])

        heat = ch["heat"]
        self.assertEqual(heat["count"], 1)
        self.assertEqual(heat["snapshots"][0]["e_score"], 51.2)
        self.assertIn("trend", heat)   # 有快照必附 trend 区块（形状见专项用例）

        ops = ch["opinions"]
        self.assertEqual(ops["count"], 1)
        self.assertEqual(ops["rows"][0]["distribution"], {"支持": 3, "反对": 2, "中立": 1})

        deep = ch["deep_read"]
        self.assertEqual(deep["payload"]["text"], "张力：信息节奏 vs 知情需求")
        self.assertTrue(deep["degraded"])
        self.assertEqual(deep["guard_dropped"], ["越权小节"])

    def test_absent_branches_for_bare_dossier(self):
        """没生成过的章节如实 absent：speech/opinions/deep_read；heat 无 trend 键。"""
        did = self.svc.start_dossier("空档案", "还没核查过")
        ch = self.svc.chapters(did)
        self.assertEqual(set(ch), SIX_CHAPTER_KEYS)
        self.assertEqual(ch["conclusion"]["count"], 0)
        self.assertEqual(ch["conclusion"]["compare"]["versions"], [])
        self.assertEqual(ch["evidence"], {"total": 0, "batches": []})
        for name in ("speech", "opinions", "deep_read"):
            self.assertEqual(ch[name], {"chapter_status": "absent"}, f"{name} 应 absent")
        self.assertEqual(ch["heat"]["count"], 0)
        self.assertNotIn("trend", ch["heat"])   # 没快照就不带趋势区块

    def test_verify_verdict_does_not_count_as_speech(self):
        """verify 的 raw_verdict（label/risk_score/signals）不冒充言论土壤章节。"""
        did = self.svc.start_dossier("t", "x")
        self.svc.record_analysis(did, agent_result(entries=[_entry("https://a.example.com/1")]))
        self.assertEqual(self.svc.chapters(did)["speech"], {"chapter_status": "absent"})

    def test_evidence_preview_capped_per_batch(self):
        """每批只预览前 5 条，计数如实给全量（口径与 tools_memory 一致）。"""
        did = self.svc.start_dossier("t", "x")
        self.store.add_evidence(did, 1, [_entry(f"https://u{i}.example.com/{i}")
                                         for i in range(7)])
        batch = self.svc.chapters(did)["evidence"]["batches"][0]
        self.assertEqual(batch["count"], 7)
        self.assertEqual(len(batch["entries"]), 5)

    def test_trend_pending_when_import_blocked(self):
        """trend 模块导入失败（并行交付未收口的模拟）→ {"trend_status": "pending"}。"""
        did = self.make_full_dossier()
        import mylocalai.attention as attn_pkg
        sentinel = getattr(attn_pkg, "trend", None)
        if sentinel is not None:
            delattr(attn_pkg, "trend")     # 防真实模块已在本进程被 import 缓存
            self.addCleanup(setattr, attn_pkg, "trend", sentinel)
        with mock.patch.dict(sys.modules, {"mylocalai.attention.trend": None}):
            trend = self.svc.chapters(did)["heat"]["trend"]
        self.assertEqual(trend, {"trend_status": "pending"})

    def test_trend_success_with_stub_module(self):
        """trend 模块在场（stub 注入）→ svg 字符串与 summary dict 原样透传。"""
        did = self.make_full_dossier()
        fake = types.ModuleType("mylocalai.attention.trend")
        fake.render_trend_svg = lambda snaps: "<svg><rect/></svg>"
        fake.trend_summary = lambda snaps: {"min": 51.2, "max": 51.2, "points": len(snaps)}
        import mylocalai.attention as attn_pkg
        with mock.patch.object(attn_pkg, "trend", fake, create=True), \
                mock.patch.dict(sys.modules, {"mylocalai.attention.trend": fake}):
            trend = self.svc.chapters(did)["heat"]["trend"]
        self.assertEqual(trend["svg"], "<svg><rect/></svg>")
        self.assertEqual(trend["summary"], {"min": 51.2, "max": 51.2, "points": 1})

    def test_trend_runtime_error_degrades_to_pending(self):
        """trend 在场但运行时炸 → 仍退 pending（带 error 说明），章节不崩。"""
        did = self.make_full_dossier()
        fake = types.ModuleType("mylocalai.attention.trend")
        fake.render_trend_svg = lambda snaps: 1 / 0
        fake.trend_summary = lambda snaps: {}
        import mylocalai.attention as attn_pkg
        with mock.patch.object(attn_pkg, "trend", fake, create=True), \
                mock.patch.dict(sys.modules, {"mylocalai.attention.trend": fake}):
            trend = self.svc.chapters(did)["heat"]["trend"]
        self.assertEqual(trend["trend_status"], "pending")
        self.assertIn("ZeroDivisionError", trend.get("error", ""))

    def test_chapters_never_raise(self):
        """全读取路径永不抛：不存在的 id / 关闭的库 / 垃圾输入都返回六章形状。"""
        for bad_id in (999999, -1, "abc", None):
            ch = self.svc.chapters(bad_id)   # 不抛即可
            self.assertEqual(set(ch), SIX_CHAPTER_KEYS)
        self.store.close()
        ch = self.svc.chapters(1)
        self.assertEqual(set(ch), SIX_CHAPTER_KEYS)
        self.assertEqual(ch["conclusion"]["count"], 0)


# ================================================================
# 2. export_markdown() 六章节导出
# ================================================================
class TestExportMarkdown(DossierReadTestBase):
    def test_export_contains_six_sections_disclaimer_and_as_of(self):
        """六节标题 + 固定免责语 + 每节 as_of + 生成时间页脚。"""
        did = self.make_full_dossier()
        text = self.svc.export_markdown(did)
        for needle in ("## 一、核查结论（版本化）", "## 二、证据本",
                       "## 三、言论土壤（classic 四档）", "## 四、热度快照与趋势",
                       "## 五、观点光谱", "## 六、背景解读（深读）",
                       "免责声明", EXPORT_DISCLAIMER, "as_of", "生成时间",
                       "证据截至（as_of）"):
            self.assertIn(needle, text, f"导出缺少：{needle}")
        # 免责语是固定段，逐字出现两次（页首引用 + 页脚）
        self.assertEqual(text.count(EXPORT_DISCLAIMER), 2)

    def test_export_conclusion_badges_and_version_compare(self):
        """结论节：徽章行（置信度/证据充分度）+ 版本对比行（标签变化）+ 复审条件。"""
        did = self.make_full_dossier()
        self.svc.record_analysis(did, agent_result(label="存疑待核实", risk=40.0,
                                                   entries=[_entry("https://b.example.com/2")]),
                                 kind="recheck")
        text = self.svc.export_markdown(did)
        self.assertIn("- 徽章：置信度", text)
        self.assertIn("证据充分度：官方", text)
        self.assertIn("【疑似谣言】→【存疑待核实】（有变化）", text)
        self.assertIn("批次 1×1、批次 2×1", text)
        self.assertIn("复审条件：", text)

    def test_export_degraded_badge(self):
        """降级运行 → 导出里如实带降级徽章（徽章体系沿用卡片字段）。"""
        did = self.svc.start_dossier("t", "x")
        self.svc.record_analysis(did, agent_result(entries=[_entry("https://a.example.com/1")],
                                                   degraded=["search"]), kind="verify")
        text = self.svc.export_markdown(did)
        self.assertIn("降级", text)

    def test_export_proxy_flags_badge(self):
        """快照的代理值标记随数展示（口径诚实铁律）。"""
        did = self.svc.start_dossier("t", "x")
        self.svc.record_heat(did, {"x": 3, "y": 12000, "z": 45, "score": 51.2,
                                   "profile": "spec", "proxy_flags": ["proxy:y"]})
        self.assertIn("代理值：proxy:y", self.svc.export_markdown(did))

    def test_export_absent_sections_marked_honestly(self):
        """空档案：各 absent 节写"本节暂无数据"，六节标题一个不少。"""
        did = self.svc.start_dossier("空档案", "还没核查过")
        text = self.svc.export_markdown(did)
        for needle in ("## 一、核查结论（版本化）", "## 二、证据本",
                       "## 三、言论土壤（classic 四档）", "## 四、热度快照与趋势",
                       "## 五、观点光谱", "## 六、背景解读（深读）"):
            self.assertIn(needle, text)
        self.assertGreaterEqual(text.count(ABSENT_NOTE), 4)
        self.assertIn(EXPORT_DISCLAIMER, text)

    def test_export_trend_pending_placeholder(self):
        """trend 未收口 → 导出热节给"趋势组件加载中"占位，不编趋势数字。"""
        did = self.make_full_dossier()
        import mylocalai.attention as attn_pkg
        sentinel = getattr(attn_pkg, "trend", None)
        if sentinel is not None:
            delattr(attn_pkg, "trend")
            self.addCleanup(setattr, attn_pkg, "trend", sentinel)
        with mock.patch.dict(sys.modules, {"mylocalai.attention.trend": None}):
            text = self.svc.export_markdown(did)
        self.assertIn("趋势组件加载中", text)

    def test_export_never_raises_and_is_pure_string(self):
        """导出是纯字符串拼接：坏输入/关闭的库也返回 str（诚实说明），绝不抛。"""
        out = self.svc.export_markdown(999999)
        self.assertIsInstance(out, str)
        self.assertIn("档案编号：999999", out)
        self.store.close()
        self.assertIsInstance(self.svc.export_markdown(1), str)


# ================================================================
# 3. reminder_lines() 被动提醒纯函数矩阵
# ================================================================
class TestReminderLines(DossierReadTestBase):
    NOW = datetime(2026, 10, 31, 12, 0, 0).astimezone()

    def test_fresh_dossier_has_no_reminder(self):
        """年龄未超（14 天内）且快照无变化 → []。"""
        did = self.svc.start_dossier("新档案", "x")
        self.assertEqual(reminder_lines(did, self.store, now=self.NOW), [])

    def test_age_exceeded_emits_prompt_template_line(self):
        """updated_at 距今 > dossier_age_days 天 → 一行提示，文案走 prompts.reminder_line。"""
        did = self.svc.start_dossier("隔夜茶致癌？")   # title 默认取正文前 24 字
        self.set_updated_at(did, days_ago=20, now=self.NOW)
        lines = reminder_lines(did, self.store, now=self.NOW,
                               rules={"dossier_age_days": 14},
                               prompt="R:{dossier_id}|{title}|{days}")
        self.assertEqual(len(lines), 1)
        self.assertIn(f"R:{did}|隔夜茶致癌？|20", lines[0])

    def test_age_boundary_not_exceeded(self):
        """恰好等于阈值不算超（严格大于才提醒）。"""
        did = self.svc.start_dossier("t", "x")
        self.set_updated_at(did, days_ago=14, now=self.NOW)
        self.assertEqual(reminder_lines(did, self.store, now=self.NOW,
                                        rules={"dossier_age_days": 14}), [])

    def test_snapshot_change_exceeded_emits_heat_line(self):
        """最新两分值 10→90（幅度 0.89 > 0.5）→ 一行"热度变化明显"。"""
        did = self.svc.start_dossier("热度骤升", "x")
        self.store.add_snapshot(did, 1, 2, 3, 10.0, taken_at="2026-10-01T00:00:00+08:00")
        self.store.add_snapshot(did, 1, 2, 3, 90.0, taken_at="2026-10-30T00:00:00+08:00")
        lines = reminder_lines(did, self.store, now=self.NOW,
                               rules=DEFAULT_RULES, prompt="P")
        self.assertEqual(len(lines), 1)
        self.assertIn("热度变化明显", lines[0])
        self.assertIn(f"#{did}", lines[0])
        self.assertIn("E 10 → 90", lines[0])

    def test_snapshot_change_not_exceeded(self):
        """10→12（幅度 0.17 < 0.5）→ 无热度提醒。"""
        did = self.svc.start_dossier("t", "x")
        self.store.add_snapshot(did, 1, 2, 3, 10.0)
        self.store.add_snapshot(did, 1, 2, 3, 12.0)
        self.assertEqual(reminder_lines(did, self.store, now=self.NOW,
                                        rules=DEFAULT_RULES, prompt="P"), [])

    def test_single_snapshot_and_zero_scores_stay_silent(self):
        """单快照不触发；两快照同为 0 幅度为 0 也不触发。"""
        did = self.svc.start_dossier("t", "x")
        self.store.add_snapshot(did, 1, 2, 3, 66.0)
        self.assertEqual(reminder_lines(did, self.store, now=self.NOW,
                                        rules=DEFAULT_RULES, prompt="P"), [])
        did2 = self.svc.start_dossier("t2", "x")
        self.store.add_snapshot(did2, 0, 0, 0, 0.0)
        self.store.add_snapshot(did2, 0, 0, 0, 0.0)
        self.assertEqual(reminder_lines(did2, self.store, now=self.NOW,
                                        rules=DEFAULT_RULES, prompt="P"), [])

    def test_both_rules_can_fire_together(self):
        """年龄超 + 热度变化同时命中 → 两行，顺序固定（年龄在前）。"""
        did = self.svc.start_dossier("双命中", "x")
        self.set_updated_at(did, days_ago=30, now=self.NOW)
        self.store.add_snapshot(did, 1, 2, 3, 5.0)
        self.store.add_snapshot(did, 1, 2, 3, 80.0)
        lines = reminder_lines(did, self.store, now=self.NOW,
                               rules=DEFAULT_RULES, prompt="A:{days}")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("A:30"))
        self.assertIn("热度变化明显", lines[1])

    def test_broken_rules_fall_back_to_defaults(self):
        """规则写坏（类型不对/垃圾值）逐键退默认 14/0.5，不抛、照常提醒。"""
        did = self.svc.start_dossier("旧档案", "x")
        self.set_updated_at(did, days_ago=20, now=self.NOW)
        for garbage in (None, "写坏了", 42,
                        {"dossier_age_days": "很多天", "snapshot_change_ratio": None},
                        {"dossier_age_days": -3, "snapshot_change_ratio": "一半"}):
            lines = reminder_lines(did, self.store, now=self.NOW, rules=garbage)
            self.assertEqual(len(lines), 1, f"rules={garbage!r} 应按默认 14 天提醒")
        # 模块级加载器同样容错：包配置读成垃圾 → 默认值
        with mock.patch.object(reminders_mod, "_pkg_json", lambda name: {"reminders": "垃圾"}):
            self.assertEqual(load_reminder_rules(), DEFAULT_RULES)
        with mock.patch.object(reminders_mod, "_pkg_json", lambda name: (_ for _ in ()).throw(OSError)):
            self.assertEqual(load_reminder_rules(), DEFAULT_RULES)

    def test_broken_prompt_falls_back_to_default_template(self):
        """prompt 模板占位符写坏 → 退内置默认文案，行内容仍完整。"""
        did = self.svc.start_dossier("旧档案", "x")
        self.set_updated_at(did, days_ago=20, now=self.NOW)
        lines = reminder_lines(did, self.store, now=self.NOW,
                               rules=DEFAULT_RULES, prompt="喂 {不存在的占位符}")
        self.assertEqual(len(lines), 1)
        self.assertIn(f"#{did}", lines[0])
        self.assertIn("已 20 天未复核", lines[0])
        # prompts.json 读不到/坏掉 → load_reminder_prompt 退默认
        with mock.patch.object(reminders_mod, "_pkg_json", lambda name: {}):
            self.assertEqual(reminders_mod.load_reminder_prompt(),
                             reminders_mod.DEFAULT_REMINDER_LINE)

    def test_missing_dossier_and_closed_store_never_raise(self):
        """不存在的档案 / 关闭的库 / 坏 id → []，永不抛。"""
        self.assertEqual(reminder_lines(999999, self.store, now=self.NOW), [])
        self.assertEqual(reminder_lines(-1, self.store, now=self.NOW), [])
        self.assertEqual(reminder_lines("abc", self.store, now=self.NOW), [])
        self.assertEqual(reminder_lines(1, None, now=self.NOW), [])
        self.store.close()
        self.assertEqual(reminder_lines(1, self.store, now=self.NOW), [])


class TestAggregateReminderLines(DossierReadTestBase):
    NOW = datetime(2026, 10, 31, 12, 0, 0).astimezone()

    def _aged(self, title: str) -> int:
        did = self.svc.start_dossier(title, "x")
        stamp = (self.NOW - timedelta(days=20)).isoformat(timespec="seconds")
        self.store._conn.execute("UPDATE dossiers SET updated_at = ? WHERE id = ?", (stamp, did))
        self.store._conn.commit()
        return did
    def test_empty_input_returns_empty(self):
        self.assertEqual(aggregate_reminder_lines([], self.store, now=self.NOW), [])

    def test_multiple_dossiers_aggregated_in_order(self):
        d1 = self._aged("档案一")
        d2 = self._aged("档案二")
        lines = aggregate_reminder_lines([d1, d2], self.store, now=self.NOW)
        self.assertEqual(len(lines), 2)
        self.assertIn(f"#{d1}", lines[0])
        self.assertIn(f"#{d2}", lines[1])

    def test_duplicate_dossier_ids_deduplicated(self):
        """同一档案出现多次（会话里查了又回看）→ 提醒行去重只留一条。"""
        d1 = self._aged("同一档案")
        lines = aggregate_reminder_lines([d1, d1, d1], self.store, now=self.NOW)
        self.assertEqual(len(lines), 1)

    def test_cap_keeps_pressure_low(self):
        """多档案多规则同时命中 → 最多 3 行（§2.2 不制造新压力）。"""
        ids = [self._aged(f"档案{i}") for i in range(3)]
        for did in ids:
            self.store.add_snapshot(did, 1, 2, 3, 5.0)
            self.store.add_snapshot(did, 1, 2, 3, 90.0)
        lines = aggregate_reminder_lines(ids, self.store, now=self.NOW)
        self.assertEqual(len(lines), reminders_mod.MAX_AGGREGATED_LINES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
