# -*- coding: utf-8 -*-
"""档案库 API 离线测试（M2 后端先行，M3 档案库页消费）：
   /api/dossiers 列表 / 详情 / Markdown 导出。

数据用 DossierStore 临时库预置，monkeypatch app.get_dossier_service 注入
（工厂是 web 层与默认库路径的解耦点，与 build_assistant_loop 同一模式）；
数据一律经 DossierService/store 读取，不直连引擎。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_dossier_api -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到 web/app.py


class DossierApiTestBase(unittest.TestCase):
    """公共夹具：临时档案库 + 预置数据 + 服务工厂注入。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001  fastapi 缺失时整组跳过而非报错
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.client = TestClient(webapp.app)

    def setUp(self) -> None:
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DossierStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)
        self.service = DossierService(self.store)
        patcher = mock.patch.object(self.webapp, "get_dossier_service",
                                    lambda: self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

        # ---- 预置两个档案 ----
        self.did_tea = self.store.create_dossier(
            "隔夜茶致癌？", "网传喝隔夜茶会致癌，已经有人确诊", input_kind="text")
        self.store.add_conclusion(
            self.did_tea, "存疑待核实", 40.0, confidence="low",
            evidence_sufficiency={"official": 0, "unofficial": 3, "unknown": 1, "total": 4},
            as_of="2026-10-31T10:00:00+08:00",
            recheck_conditions=["出现官方/权威媒体级信源的通报或报道", "证据本独立条目≥5"],
            raw_verdict={"label": "存疑待核实", "risk_score": 40.0})
        self.store.add_evidence(self.did_tea, 1, [
            {"url": "https://example.gov.cn/a", "domain": "example.gov.cn",
             "domain_tier": "官方", "source_kind": "官方（监管部门）", "stance": "反驳",
             "title": "监管部门辟谣", "snippet": "官方通报称无此案", "fetched_at": "2026-10-31T09:00:00+08:00"},
            {"url": "https://news.example.com/b", "domain": "news.example.com",
             "domain_tier": "门户", "source_kind": "非官方（门户）", "stance": "支持",
             "title": "网文流传", "snippet": "仍在传播", "fetched_at": "2026-10-31T09:10:00+08:00"},
        ])
        self.store.add_snapshot(self.did_tea, 3.0, 12000.0, 45.0, 51.2,
                                profile="spec", proxy_flags=["proxy:y"])
        self.did_weather = self.store.create_dossier("今日天气", "今天天气不错", input_kind="topic")

    def get(self, path: str):
        return self.client.get(path)


class TestDossierList(DossierApiTestBase):
    def test_list_query_and_limit(self):
        """列表默认全量（updated_at 倒序）；query 过滤；limit 截断。"""
        resp = self.get("/api/dossiers")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["dossiers"]
        self.assertEqual({d["id"] for d in items}, {self.did_tea, self.did_weather})
        for d in items:
            self.assertIn("title", d)
            self.assertIn("updated_at", d)
            self.assertIn("input_kind", d)

        hits = self.get("/api/dossiers?query=隔夜茶").json()["dossiers"]
        self.assertEqual([d["id"] for d in hits], [self.did_tea])
        self.assertEqual(len(self.get("/api/dossiers?limit=1").json()["dossiers"]), 1)


class TestDossierDetail(DossierApiTestBase):
    def test_detail_shape_and_compare_versions(self):
        """档案头 + conclusions（JSON 已还原）+ 证据批次 + 快照计数 + compare_versions 版本。"""
        resp = self.get(f"/api/dossiers/{self.did_tea}")
        self.assertEqual(resp.status_code, 200)
        d = resp.json()
        self.assertEqual(d["title"], "隔夜茶致癌？")
        self.assertEqual(d["input_kind"], "text")
        self.assertIn("input_text", d)
        self.assertIn("updated_at", d)

        self.assertEqual(len(d["conclusions"]), 1)
        c = d["conclusions"][0]
        self.assertEqual(c["version"], 1)
        self.assertEqual(c["label"], "存疑待核实")
        self.assertEqual(c["evidence_sufficiency"]["total"], 4)
        self.assertEqual(c["recheck_conditions"],
                         ["出现官方/权威媒体级信源的通报或报道", "证据本独立条目≥5"])
        self.assertEqual(c["as_of"], "2026-10-31T10:00:00+08:00")

        self.assertEqual(d["evidence_batches"], {"1": 2})
        self.assertEqual(d["snapshot_count"], 1)
        self.assertEqual([v["version"] for v in d["versions"]], [1])
        self.assertFalse(d["changed"])

    def test_versioned_dossier_reports_changed(self):
        """追加 v2 后：versions 两行、changed=True（recheck 版本化的数据面）。"""
        self.store.add_conclusion(self.did_tea, "疑似谣言", 62.0, confidence="medium",
                                  as_of="2026-10-31T11:00:00+08:00")
        d = self.get(f"/api/dossiers/{self.did_tea}").json()
        self.assertEqual([v["version"] for v in d["versions"]], [1, 2])
        self.assertTrue(d["changed"])
        self.assertEqual(len(d["conclusions"]), 2)

    def test_detail_invalid_id_is_404(self):
        """非法 id（不存在 / 非整数 / 非正数）→ 一律 404。"""
        self.assertEqual(self.get("/api/dossiers/999999").status_code, 404)
        self.assertEqual(self.get("/api/dossiers/abc").status_code, 404)
        self.assertEqual(self.get("/api/dossiers/-1").status_code, 404)


class TestDossierExport(DossierApiTestBase):
    def test_export_md_contains_as_of_and_disclaimer(self):
        """format=md → Markdown 文本：含标题/结论/as_of/复审条件/证据/免责语。"""
        resp = self.get(f"/api/dossiers/{self.did_tea}/export?format=md")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/markdown"))
        text = resp.text
        for needle in ("# 档案：隔夜茶致癌？", "存疑待核实", "as_of",
                       "2026-10-31T10:00:00+08:00", "出现官方/权威媒体级信源的通报或报道",
                       "example.gov.cn", "批次 1", "免责声明", "只述证据"):
            self.assertIn(needle, text, f"导出缺少：{needle}")

    def test_export_rejects_other_formats_and_missing_id(self):
        self.assertEqual(
            self.get(f"/api/dossiers/{self.did_tea}/export?format=html").status_code, 400)
        self.assertEqual(self.get("/api/dossiers/999999/export").status_code, 404)


# ===========================================================================
# 以下为 M3 追加用例（原 M2 用例零改动）：详情 chapters 六章节、导出附件头、
# /api/sessions/{sid}/reminders 被动提醒端点、前后端自洽（chat.js/index.html/chat.css）。
# ===========================================================================

import re  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

STATIC_DIR = ROOT / "src" / "mylocalai" / "web" / "static"


def _read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


class TestDossierDetailChapters(DossierApiTestBase):
    """GET /api/dossiers/{id} 响应扩 chapters（M3-2：经 service.chapters）。"""

    def test_detail_contains_six_chapter_keys(self):
        resp = self.get(f"/api/dossiers/{self.did_tea}")
        self.assertEqual(resp.status_code, 200)
        chapters = resp.json()["chapters"]
        self.assertEqual(set(chapters),
                         {"conclusion", "evidence", "speech", "heat", "opinions", "deep_read"})
        # 预置数据：conclusion 1 版 + 证据批次 + 1 快照；speech/opinions/deep_read 未生成
        self.assertEqual(chapters["conclusion"]["count"], 1)
        self.assertEqual(chapters["evidence"]["total"], 2)
        self.assertEqual(chapters["heat"]["count"], 1)
        self.assertIn("trend", chapters["heat"])
        for name in ("speech", "opinions", "deep_read"):
            self.assertEqual(chapters[name], {"chapter_status": "absent"})

    def test_detail_chapters_absent_for_empty_dossier(self):
        chapters = self.get(f"/api/dossiers/{self.did_weather}").json()["chapters"]
        self.assertEqual(chapters["conclusion"]["count"], 0)
        self.assertEqual(chapters["evidence"], {"total": 0, "batches": []})
        self.assertEqual(chapters["heat"]["count"], 0)
        self.assertNotIn("trend", chapters["heat"])
        for name in ("speech", "opinions", "deep_read"):
            self.assertEqual(chapters[name], {"chapter_status": "absent"})

    def test_detail_chapters_absent_shape_is_json_safe(self):
        """absent 是普通 dict（chapter_status 键），前端可稳定判别。"""
        chapters = self.get(f"/api/dossiers/{self.did_weather}").json()["chapters"]
        self.assertEqual(chapters["speech"].get("chapter_status"), "absent")


class TestDossierExportAttachment(DossierApiTestBase):
    """M3-6 导出走 service.export_markdown，Content-Disposition attachment。"""

    def test_export_has_attachment_disposition(self):
        resp = self.get(f"/api/dossiers/{self.did_tea}/export?format=md")
        self.assertEqual(resp.status_code, 200)
        disposition = resp.headers.get("content-disposition", "")
        self.assertIn("attachment", disposition)
        self.assertIn(f"dossier-{self.did_tea}.md", disposition)

    def test_export_contains_six_sections_for_full_dossier(self):
        """完整档案导出含六节标题 + 固定免责语段（M3-6 验收口径）。"""
        self.store.add_conclusion(self.did_tea, "疑似谣言", 62.0, confidence="medium",
                                  as_of="2026-10-31T11:00:00+08:00")
        resp = self.get(f"/api/dossiers/{self.did_tea}/export?format=md")
        text = resp.text
        for needle in ("## 一、核查结论（版本化）", "## 二、证据本",
                       "## 三、言论土壤（classic 四档）", "## 四、热度快照与趋势",
                       "## 五、观点光谱", "## 六、背景解读（深读）",
                       "本报告只述证据与立场分布，不对消息真假下结论；请以官方通报为准",
                       "本节暂无数据", "生成时间"):
            self.assertIn(needle, text, f"导出缺少：{needle}")


class TestSessionRemindersApi(DossierApiTestBase):
    """GET /api/sessions/{sid}/reminders（M3-7 被动提醒：会话涉及档案 → lines）。"""

    def setUp(self) -> None:
        super().setUp()
        from mylocalai.assistant.session_store import SessionStore
        # 会话域与档案域同库（tool_runs.dossier_id 外键指向 dossiers 表）
        self.sessions = SessionStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.sessions.close)
        patcher = mock.patch.object(self.webapp, "get_session_store", lambda: self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _age_dossier(self, did: int, days_ago: float) -> None:
        stamp = (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")
        self.store._conn.execute("UPDATE dossiers SET updated_at = ? WHERE id = ?", (stamp, did))
        self.store._conn.commit()

    def test_reminders_empty_session_returns_no_lines(self):
        sid = self.sessions.create_session(title="新会话")
        resp = self.get(f"/api/sessions/{sid}/reminders")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["session_id"], sid)
        self.assertEqual(data["dossier_ids"], [])
        self.assertEqual(data["lines"], [])

    def test_reminders_invalid_sid_is_404(self):
        self.assertEqual(self.get("/api/sessions/999999/reminders").status_code, 404)
        self.assertEqual(self.get("/api/sessions/abc/reminders").status_code, 404)
        self.assertEqual(self.get("/api/sessions/-1/reminders").status_code, 404)

    def test_reminders_age_line_for_session_dossier(self):
        """会话跑过 verify（tool_runs 回填 dossier_id）且档案超 14 天 → 一行年龄提醒。"""
        self._age_dossier(self.did_tea, days_ago=20)
        sid = self.sessions.create_session(title="核查隔夜茶")
        run_id = self.sessions.start_run(sid, "verify_claim", {"text": "隔夜茶致癌？"})
        self.sessions.finish_run(run_id, "ok", dossier_id=self.did_tea)
        data = self.get(f"/api/sessions/{sid}/reminders").json()
        self.assertEqual(data["dossier_ids"], [self.did_tea])
        self.assertEqual(len(data["lines"]), 1)
        self.assertIn(f"#{self.did_tea}", data["lines"][0])
        self.assertIn("天未复核", data["lines"][0])

    def test_reminders_dedup_same_dossier_from_multiple_runs(self):
        """同一档案被 verify 又被 get_dossier（args.id）引用 → 提醒行去重。"""
        self._age_dossier(self.did_tea, days_ago=20)
        sid = self.sessions.create_session(title="反复回看")
        r1 = self.sessions.start_run(sid, "verify_claim", {"text": "x"})
        self.sessions.finish_run(r1, "ok", dossier_id=self.did_tea)
        r2 = self.sessions.start_run(sid, "get_dossier", {"id": self.did_tea})
        self.sessions.finish_run(r2, "ok")   # 未回填 dossier_id，只有 args.id
        data = self.get(f"/api/sessions/{sid}/reminders").json()
        self.assertEqual(data["dossier_ids"], [self.did_tea])
        self.assertEqual(len(data["lines"]), 1)

    def test_reminders_heat_change_line(self):
        """快照最新两分值变化幅度超 0.5 → 一行"热度变化明显"（档案是新的，无年龄行）。"""
        sid = self.sessions.create_session(title="热度骤变")
        run_id = self.sessions.start_run(sid, "check_heat", {"topic": "隔夜茶"})
        self.sessions.finish_run(run_id, "ok", dossier_id=self.did_tea)
        # 夹具已有一条"现在"的快照；两次新快照放到未来两天，保证它们是序列最后两个
        base = datetime.now().astimezone()
        self.store.add_snapshot(self.did_tea, 1, 2, 3, 10.0,
                                taken_at=(base + timedelta(days=1)).isoformat(timespec="seconds"))
        self.store.add_snapshot(self.did_tea, 1, 2, 3, 90.0,
                                taken_at=(base + timedelta(days=2)).isoformat(timespec="seconds"))
        data = self.get(f"/api/sessions/{sid}/reminders").json()
        self.assertEqual(len(data["lines"]), 1)
        self.assertIn("热度变化明显", data["lines"][0])

    def test_reminders_absent_when_rules_not_hit(self):
        """档案新鲜 + 快照无变化 → lines 为空（不制造新压力）。"""
        sid = self.sessions.create_session(title="刚查完")
        run_id = self.sessions.start_run(sid, "verify_claim", {"text": "x"})
        self.sessions.finish_run(run_id, "ok", dossier_id=self.did_tea)
        self.assertEqual(self.get(f"/api/sessions/{sid}/reminders").json()["lines"], [])


class TestM3FrontendConsistency(unittest.TestCase):
    """M3 前后端自洽（沿 test_website_chat 先例）：新端点引用一致、档案库元素在位。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.chat_js = _read_static("chat.js")
        cls.chat_css = _read_static("chat.css")
        cls.index_html = _read_static("index.html")

    @staticmethod
    def _normalize(path: str) -> str:
        path = path.split("?")[0]
        return re.sub(r"\{[^}]*\}", "{}", path)

    def test_reminders_endpoint_wired_end_to_end(self):
        """chat.js 引用 /api/sessions/{sid}/reminders，后端路由存在，渲染函数在位。"""
        self.assertIn("/api/sessions/{sid}/reminders", self.chat_js)
        from app import app as fastapi_app
        routes = {self._normalize(getattr(r, "path", "")) for r in fastapi_app.routes}
        self.assertIn("/api/sessions/{}/reminders", routes)
        for fn in ("loadSessionReminders", "renderReminderLine"):
            self.assertIn(f"function {fn}", self.chat_js)
        self.assertIn(".chat-reminder", self.chat_css)

    def test_dossier_chapter_renderers_and_styles_consistent(self):
        """六个章节渲染器齐全；chat.js 用到的 chat-* class 都在 chat.css 有定义。"""
        for fn in ("renderDossierChapters", "renderConclusionChapter", "renderEvidenceChapter",
                   "renderSpeechChapter", "renderHeatChapter", "renderOpinionsChapter",
                   "renderDeepReadChapter"):
            self.assertIn(f"function {fn}", self.chat_js, f"chat.js 缺少 {fn}")
        used = set()
        for chunk in re.findall(r'class="([^"]+)"', self.chat_js):
            used.update(re.findall(r"[A-Za-z][\w-]*", chunk))
        chat_classes = sorted(c for c in used if c.startswith("chat-"))
        missing = [c for c in chat_classes if f".{c}" not in self.chat_css]
        self.assertEqual(missing, [], f"chat.css 缺少定义：{missing}")

    def test_index_dossier_view_markers(self):
        """index.html 档案库视图元素在位（六章节提示 + 列表 id + 导出链接）。"""
        for marker in ('id="chatDossierList"', 'id="chatDossierCount"',
                       "六章节", "chat-dossier-panel"):
            self.assertIn(marker, self.index_html, f"index.html 缺少：{marker}")
        self.assertIn("format=md", self.chat_js)
        self.assertIn("download=", self.chat_js)
        self.assertIn("本节暂无数据", self.chat_js)   # 缺章如实文案与后端口径一致

    def test_chatjs_element_ids_exist_in_html(self):
        used = set(re.findall(r'\$\("#([A-Za-z][\w]*)"\)', self.chat_js))
        used |= set(re.findall(r'querySelector\("#([A-Za-z][\w]*)"\)', self.chat_js))
        missing = sorted(i for i in used if f'id="{i}"' not in self.index_html)
        self.assertEqual(missing, [], f"chat.js 引用了 HTML 里不存在的元素 id: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
