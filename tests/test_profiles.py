# -*- coding: utf-8 -*-
"""Profile 工作区离线测试（v6 多 Profile 分家，2026-11-20 角色扩展阶段 1）。

覆盖：
  一、迁移：新库建默认档案；v3 老库升级后历史数据回填到默认档案；
  二、ProfileStore：CRUD / 激活态 / 删除保护 / 参数校验；
  三、分家：会话、档案、指纹召回、画像聚合按 profile_id 过滤；
  四、跨工作区防护：tools_guard.dossier_visible；
  五、loop：layperson 结论卡简化（无 risk_score + presentation=simple），
     full 角色逐位不变；
  六、web API：/api/profiles CRUD/激活/删除语义，会话与档案跨工作区 404。

全部离线：不联网、不调真实 LLM（loop 用例走 stub 服务）。
运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_profiles -v
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到 web/app.py


class ProfileStoreTestBase(unittest.TestCase):
    """临时库夹具：一个默认档案 + 一个可自由操作的第二档案。"""

    def setUp(self):
        from mylocalai.dossier.profiles import ProfileStore
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProfileStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)

    def make_second(self, key="analyst-ws", name="分析工作区", role="analyst"):
        return self.store.create_profile(key, name, role)


# ---------------------------------------------------------------------------
# 一、迁移与回填
# ---------------------------------------------------------------------------

class TestMigrationAndBackfill(unittest.TestCase):
    def test_fresh_db_gets_default_profile_and_active_state(self):
        """新库：migrate 建默认档案（role=full）并设激活态；重复 migrate 幂等。"""
        from mylocalai.dossier.store import SCHEMA_VERSION, connect, migrate
        from mylocalai.dossier.profiles import active_profile_id
        with TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "fresh.db")
            try:
                self.assertEqual(migrate(conn), SCHEMA_VERSION)
                self.assertEqual(migrate(conn), 0)   # 幂等
                row = conn.execute("SELECT key, role FROM profiles").fetchone()
                self.assertEqual((row["key"], row["role"]), ("default", "full"))
                self.assertGreater(active_profile_id(conn), 0)
            finally:
                conn.close()

    def test_legacy_rows_backfill_to_default_profile(self):
        """v5 老库（无 profile 列时代的存量行）升级后：会话/档案回填到默认档案。"""
        from mylocalai.dossier import store as ds
        from mylocalai.dossier.store import connect
        with TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "legacy.db")
            try:
                # 手工只跑前 5 段（v1..v5）并登记，模拟升级前的真实老库
                conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""")
                for i, script in enumerate(ds.MIGRATIONS[:5], start=1):
                    with conn:
                        conn.executescript(script)
                        conn.execute("INSERT INTO schema_migrations (version, applied_at)"
                                     " VALUES (?, '2026-10-01T00:00:00+08:00')", (i,))
                did = conn.execute(
                    "INSERT INTO dossiers (created_at, updated_at, title, input_text)"
                    " VALUES ('2026-10-01T08:00:00+08:00', '2026-10-01T08:00:00+08:00',"
                    " '旧档', '老正文')").lastrowid
                sid = conn.execute(
                    "INSERT INTO sessions (title, created_at, updated_at)"
                    " VALUES ('旧会话', '2026-10-01T08:00:00+08:00', '2026-10-01T08:00:00+08:00')"
                ).lastrowid
                ds.migrate(conn)
                prof = conn.execute("SELECT profile_id FROM dossiers WHERE id = ?",
                                    (did,)).fetchone()
                sess = conn.execute("SELECT profile_id FROM sessions WHERE id = ?",
                                    (sid,)).fetchone()
                self.assertEqual(prof["profile_id"], sess["profile_id"])
                self.assertIsNotNone(prof["profile_id"])
                role = conn.execute("SELECT role FROM profiles WHERE id = ?",
                                    (prof["profile_id"],)).fetchone()
                self.assertEqual(role["role"], "full")
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# 二、ProfileStore CRUD / 激活 / 删除保护
# ---------------------------------------------------------------------------

class TestProfileStoreCrud(ProfileStoreTestBase):
    def test_list_contains_default_and_counts(self):
        """列表含默认档案（激活）；计数随会话/档案增长。"""
        profiles = self.store.list_profiles()
        self.assertEqual(len(profiles), 1)
        default = profiles[0]
        self.assertEqual(default["key"], "default")
        self.assertEqual(default["role"], "full")
        self.assertTrue(default["is_active"])
        self.assertEqual(default["role_label"], "全部能力")

        second = self.make_second()
        # 同库第二个连接（生产形状：会话域与档案域各自持有连接、同指一个文件）
        from mylocalai.assistant.session_store import SessionStore
        from mylocalai.dossier.store import DossierStore
        sessions = SessionStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(sessions.close)
        dossiers = DossierStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(dossiers.close)
        sessions.create_session("甲会话", profile_id=default["id"])
        sessions.create_session("乙会话", profile_id=second)
        dossiers.create_dossier("甲档案", "甲正文", profile_id=second)

        by_id = {p["id"]: p for p in self.store.list_profiles()}
        self.assertEqual(by_id[default["id"]]["session_count"], 1)
        self.assertEqual(by_id[default["id"]]["dossier_count"], 0)
        self.assertEqual(by_id[second]["session_count"], 1)
        self.assertEqual(by_id[second]["dossier_count"], 1)

    def test_create_validation(self):
        """key/角色/名称校验：坏 key、重复 key、越界角色、空名称、超长名称 → ProfileError。"""
        from mylocalai.dossier.profiles import ProfileError
        with self.assertRaises(ProfileError):
            self.store.create_profile("Bad Key", "x")            # 大写/空格
        with self.assertRaises(ProfileError):
            self.store.create_profile("default", "重名")          # key 重复
        with self.assertRaises(ProfileError):
            self.store.create_profile("ok-key", "名", "wizard")   # 角色越界
        with self.assertRaises(ProfileError):
            self.store.create_profile("ok-key", "   ")            # 空名称
        with self.assertRaises(ProfileError):
            self.store.create_profile("ok-key", "名" * 41)        # 超长
        self.assertEqual(self.store.create_profile("ok-key", "好名字", "layperson"),
                         self.store.get_by_key("ok-key")["id"])

    def test_update_name_and_role_keeps_data(self):
        """改名/改角色不动数据；角色封闭枚举。"""
        from mylocalai.dossier.profiles import ProfileError
        second = self.make_second()
        row = self.store.update_profile(second, display_name="新名字", role="monitor")
        self.assertEqual((row["display_name"], row["role"]), ("新名字", "monitor"))
        with self.assertRaises(ProfileError):
            self.store.update_profile(second, role="nope")
        with self.assertRaises(ProfileError):
            self.store.update_profile(999, display_name="x")      # 不存在

    def test_set_active_switches_active(self):
        """激活态切换：set_active 后 active()/active_id() 指向新档案。"""
        second = self.make_second()
        row = self.store.set_active(second)
        self.assertEqual(row["id"], second)
        self.assertEqual(self.store.active_id(), second)
        self.assertEqual(self.store.active()["id"], second)
        listed = {p["id"]: p["is_active"] for p in self.store.list_profiles()}
        self.assertTrue(listed[second])
        self.assertFalse(listed[1])
        from mylocalai.dossier.profiles import ProfileError
        with self.assertRaises(ProfileError):
            self.store.set_active(999)

    def test_delete_protections(self):
        """删除保护：最后一个/激活态/有数据 → ProfileError；空且非激活可删。"""
        from mylocalai.assistant.session_store import SessionStore
        from mylocalai.dossier.profiles import ProfileError
        with self.assertRaises(ProfileError):
            self.store.delete_profile(1)                          # 最后一个
        second = self.make_second()
        self.store.set_active(second)                             # 激活态 → 不可删
        with self.assertRaises(ProfileError):
            self.store.delete_profile(second)
        sessions = SessionStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(sessions.close)
        sessions.create_session("占位", profile_id=second)
        self.store.set_active(1)                                  # 切走激活态
        with self.assertRaises(ProfileError):
            self.store.delete_profile(second)                     # 有会话数据
        with self.assertRaises(ProfileError):
            self.store.delete_profile(999)                        # 不存在
        # 建一个真空档案：可删
        empty = self.store.create_profile("empty-ws", "空档案")
        self.store.delete_profile(empty)
        self.assertIsNone(self.store.get_profile(empty))
        # 删除后激活态自愈：仍指向存在的档案
        self.assertGreater(self.store.active_id(), 0)


# ---------------------------------------------------------------------------
# 三、分家：会话 / 档案 / 指纹 / 画像
# ---------------------------------------------------------------------------

class TestProfileScoping(ProfileStoreTestBase):
    def setUp(self):
        super().setUp()
        from mylocalai.assistant.session_store import SessionStore
        from mylocalai.dossier.store import DossierStore
        self.sessions = SessionStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.sessions.close)
        self.dossiers = DossierStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.dossiers.close)
        self.default_id = self.store.active_id()
        self.second = self.make_second()

    def test_sessions_and_dossiers_scoped(self):
        """list_sessions / list_dossiers 只返回本工作区数据。"""
        s1 = self.sessions.create_session("默认区会话", profile_id=self.default_id)
        s2 = self.sessions.create_session("二区会话", profile_id=self.second)
        d1 = self.dossiers.create_dossier("默认区档案", "甲", profile_id=self.default_id)
        self.dossiers.create_dossier("二区档案", "乙", profile_id=self.second)
        self.assertEqual([s["id"] for s in self.sessions.list_sessions(
            profile_id=self.default_id)], [s1])
        self.assertEqual([s["id"] for s in self.sessions.list_sessions(
            profile_id=self.second)], [s2])
        self.assertEqual([d["id"] for d in self.dossiers.list_dossiers(
            profile_id=self.default_id)], [d1])
        # None = 不过滤（全库，程序内部路径）
        self.assertEqual(len(self.dossiers.list_dossiers(profile_id=None)), 2)

    def test_create_defaults_to_active_profile(self):
        """不传 profile_id 的会话/建档归当前激活档案。"""
        self.store.set_active(self.second)
        sid = self.sessions.create_session("跟随激活态")
        did = self.dossiers.create_dossier("跟随激活态", "正文")
        self.assertEqual(self.sessions.session_profile(sid)["profile_id"], self.second)
        self.assertEqual(self.dossiers.get_dossier(did)["profile_id"], self.second)

    def test_fingerprint_recall_scoped(self):
        """指纹召回按工作区过滤：别区沉淀的说法不在本区被秒回。"""
        text = "网传某地自来水被下毒，官方不敢通报"
        did_a = self.dossiers.create_dossier("A", text, profile_id=self.default_id)
        did_b = self.dossiers.create_dossier("B", text, profile_id=self.second)
        self.dossiers.add_claim_fingerprint(did_a, text, label="疑似谣言")
        self.dossiers.add_claim_fingerprint(did_b, text, label="低风险")
        hits_default = self.dossiers.find_fingerprints(text, 0.5,
                                                        profile_id=self.default_id)
        hits_second = self.dossiers.find_fingerprints(text, 0.5,
                                                      profile_id=self.second)
        self.assertEqual([h["dossier_id"] for h in hits_default], [did_a])
        self.assertEqual([h["dossier_id"] for h in hits_second], [did_b])
        self.assertEqual(len(self.dossiers.find_fingerprints(text, 0.5)), 2)  # None=全库

    def test_insights_scoped(self):
        """画像三聚合按工作区过滤；None 时全库（逐位不变）。"""
        from mylocalai.dossier.insights import pattern_library, source_profiles, \
            weekly_digest
        did_a = self.dossiers.create_dossier("A", "甲", profile_id=self.default_id)
        self.dossiers.add_conclusion(did_a, "高风险谣言", 88.0, confidence="high",
                                     as_of="2026-11-20T10:00:00+08:00")
        self.dossiers.add_evidence(did_a, 1, [
            {"url": "https://gov.example/x", "domain_tier": "官方权威",
             "source_kind": "官方（新华网）", "stance": "反驳", "title": "通报",
             "snippet": "已辟谣", "fetched_at": "2026-11-20T09:00:00+08:00"}])
        self.dossiers.record_markers(did_a, {
            "risk_hits": {"source_vague": {"label": "来源模糊", "hits": ["听说"],
                                           "points": 10}},
            "credibility_hits": {}, "marker_points": 10, "credibility_points": 0})

        p_default = pattern_library(self.dossiers, self.default_id)
        p_second = pattern_library(self.dossiers, self.second)
        self.assertEqual(p_default["total_dossiers"], 1)
        self.assertEqual(p_second["total_dossiers"], 0)
        self.assertEqual(len(source_profiles(self.dossiers, self.default_id)), 1)
        self.assertEqual(source_profiles(self.dossiers, self.second), [])
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        self.assertEqual(weekly_digest(self.dossiers, now=now, days=7,
                                       profile_id=self.second)["dossiers"]["new_count"], 0)
        self.assertEqual(weekly_digest(self.dossiers, now=now, days=7,
                                       profile_id=self.default_id)["dossiers"]["new_count"], 1)
        # None = 全库
        self.assertEqual(pattern_library(self.dossiers, None)["total_dossiers"], 1)


# ---------------------------------------------------------------------------
# 四、跨工作区防护
# ---------------------------------------------------------------------------

class TestDossierVisibleGuard(ProfileStoreTestBase):
    def setUp(self):
        super().setUp()
        from mylocalai.assistant.session_store import SessionStore
        from mylocalai.dossier.store import DossierStore
        from types import SimpleNamespace
        self.sessions = SessionStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.sessions.close)
        self.dossiers = DossierStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.dossiers.close)
        self.default_id = self.store.active_id()
        self.second = self.make_second()
        self.did_a = self.dossiers.create_dossier("A", "甲", profile_id=self.default_id)
        self.did_b = self.dossiers.create_dossier("B", "乙", profile_id=self.second)
        self.ctx_a = SimpleNamespace(dossiers=SimpleNamespace(store=self.dossiers),
                                     profile_id=self.default_id)
        self.ctx_b = SimpleNamespace(dossiers=SimpleNamespace(store=self.dossiers),
                                     profile_id=self.second)
        self.ctx_none = SimpleNamespace(dossiers=SimpleNamespace(store=self.dossiers),
                                        profile_id=None)

    def test_visible_matrix(self):
        """本区可见、别区不可见（与不存在同形）、无注入时全可见。"""
        from mylocalai.assistant.tools_guard import dossier_visible
        self.assertEqual(dossier_visible(self.ctx_a, self.did_a), (True, ""))
        self.assertEqual(dossier_visible(self.ctx_a, self.did_b), (False, ""))
        self.assertEqual(dossier_visible(self.ctx_b, self.did_b), (True, ""))
        self.assertEqual(dossier_visible(self.ctx_b, self.did_a), (False, ""))
        self.assertEqual(dossier_visible(self.ctx_a, 99999), (False, ""))
        self.assertEqual(dossier_visible(self.ctx_none, self.did_b), (True, ""))


# ---------------------------------------------------------------------------
# 五、loop：layperson 卡片简化
# ---------------------------------------------------------------------------

def _loop_stubs():
    """loop 离线 stub（路由 → verify_claim；叙述/摘要固定文案）。

    注意：router 从 route_llm 返回 dict 的**顶层**读工具参数（不是嵌套 args），
    与 test_assistant_loop.stub_route 同形状。
    """
    def route(system, user):
        return ({"action": "verify_claim", "text": "网传喝隔夜茶会致癌"}, "")

    def verify(text, deep=False, on_component_done=None, cancel_check=None):
        return {"label": "疑似谣言", "risk_score": 62, "signals": ["来源模糊"],
                "degraded_components": [], "agent_error": "", "llm_turns": 3,
                "evidence_book": {"claim": text, "entries": [
                    {"url": "https://gov.example/1", "domain": "gov.example",
                     "domain_tier": "官方权威", "source_kind": "官方（新华网）",
                     "stance": "反驳", "title": "官方辟谣", "snippet": "不实信息",
                     "fetched_at": "2026-11-20T08:00:00+08:00"}]}}

    def narrate(system, user):
        return ("核查完成，结论见卡片。", "")

    return {"route_llm": route, "verify": verify, "narrate_llm": narrate}


class TestLoopRolePresentation(unittest.TestCase):
    def setUp(self):
        from mylocalai.assistant.events import EventStream
        from mylocalai.assistant.loop import AssistantLoop
        from mylocalai.assistant.session_store import SessionStore
        from mylocalai.dossier.profiles import ProfileStore
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "app.db"
        self.sessions = SessionStore(db)
        self.addCleanup(self.sessions.close)
        self.dossiers = DossierService(DossierStore(db))
        self.addCleanup(self.dossiers.close)
        self.profiles = ProfileStore(db)
        self.addCleanup(self.profiles.close)
        assistant_dir = Path(__file__).resolve().parent.parent / "src" / "mylocalai" / "assistant"
        self.rules = json.loads((assistant_dir / "assistant_rules.json").read_text("utf-8"))
        self.prompts = json.loads((assistant_dir / "prompts.json").read_text("utf-8"))
        self.EventStream = EventStream
        self.AssistantLoop = AssistantLoop

    def _turn(self, session_id):
        loop = self.AssistantLoop(sessions=self.sessions, dossiers=self.dossiers,
                                  services=_loop_stubs(), rules=self.rules,
                                  prompts=self.prompts)
        events = self.EventStream()
        out = loop.handle(session_id, "网传喝隔夜茶会致癌，真的吗", events)
        cards = [e for e in events.snapshot() if e["event"] == "card"]
        return out, cards

    def test_layperson_card_hides_risk_score(self):
        """layperson 工作区：结论卡无 risk_score、带 presentation=simple，其余字段在。"""
        lay = self.profiles.create_profile("lay", "普通用户", "layperson")
        sid = self.sessions.create_session("普通会话", profile_id=lay)
        _out, cards = self._turn(sid)
        self.assertEqual(len(cards), 1)
        card = cards[0]["card"]
        self.assertNotIn("risk_score", card)
        self.assertEqual(card.get("presentation"), "simple")
        self.assertEqual(card.get("label"), "疑似谣言")
        self.assertIn("confidence", card)          # 口径诚实：置信度徽章保留
        self.assertIn("as_of", card)
        # 落库的卡片消息同样是简化形状（历史回放一致）
        stored = [m for m in self.sessions.messages(sid) if m["kind"] == "card"]
        self.assertEqual(len(stored), 1)
        payload = json.loads(stored[-1]["content"])
        self.assertNotIn("risk_score", payload["card"])

    def test_full_role_card_keeps_risk_score(self):
        """full 角色（默认档案）：卡片与加角色分家前逐位一致（有 risk_score）。"""
        sid = self.sessions.create_session("全能力会话")
        _out, cards = self._turn(sid)
        card = cards[0]["card"]
        self.assertEqual(card.get("risk_score"), 62)
        self.assertNotEqual(card.get("presentation"), "simple")

    def test_dossier_lands_in_session_profile(self):
        """建档归会话所属工作区（切过激活态也不跟跑）。"""
        second = self.profiles.create_profile("second", "二区", "analyst")
        self.profiles.set_active(second)            # 激活态切到二区
        sid = self.sessions.create_session("一区会话", profile_id=1)
        out, _cards = self._turn(sid)
        did = out["dossier_id"]
        self.assertEqual(self.dossiers.store.get_dossier(did)["profile_id"], 1)

    def test_cross_profile_get_dossier_refused(self):
        """对话里拿别区的档案号 → 按不存在处理（tools_guard 经 loop 生效）。"""
        other = self.profiles.create_profile("other", "别区", "analyst")
        did = self.dossiers.store.create_dossier("别区档案", "乙", profile_id=other)
        sid = self.sessions.create_session("本区会话", profile_id=1)
        loop = self.AssistantLoop(sessions=self.sessions, dossiers=self.dossiers,
                                  services={"route_llm": lambda s, u: (
                                      {"action": "get_dossier", "id": did}, ""),
                                      "narrate_llm": lambda s, u: ("好吧。", "")},
                                  rules=self.rules, prompts=self.prompts)
        events = self.EventStream()
        out = loop.handle(sid, f"看看档案 #{did}", events)
        self.assertTrue(out.get("degraded"))
        self.assertTrue(self.sessions.runs_for_session(sid))
        # 卡片不出现（按不存在处理），档案号不外泄
        self.assertFalse([e for e in events.snapshot() if e["event"] == "card"])


# ---------------------------------------------------------------------------
# 六、web API
# ---------------------------------------------------------------------------

class TestProfilesApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            import app as webapp
        except Exception as exc:  # noqa: BLE001  fastapi 缺失整组跳过
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.client = TestClient(webapp.app)

    def setUp(self):
        from mylocalai.dossier.profiles import ProfileStore
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        from mylocalai.assistant.session_store import SessionStore
        from mylocalai.web import profiles_api
        from unittest import mock
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "dossier.db"
        self.store = DossierStore(db)
        self.addCleanup(self.store.close)
        self.service = DossierService(self.store)
        self.sessions = SessionStore(db)
        self.addCleanup(self.sessions.close)
        self.profiles = ProfileStore(db)
        self.addCleanup(self.profiles.close)
        # 三处工厂各在其定义模块里替换：web 端点用 app.*，Profile 路由用
        # profiles_api.*（router 在导入期绑定的是 profiles_api 模块全局）。
        for module, attr, factory in (
                (self.webapp, "get_dossier_service", lambda: self.service),
                (self.webapp, "get_session_store", lambda: self.sessions),
                (profiles_api, "get_profile_store", lambda: self.profiles)):
            patcher = mock.patch.object(module, attr, factory)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_crud_and_activate(self):
        """列表/新建/改名/激活/删除全链 + 错误语义（400/404/409）。"""
        listed = self.client.get("/api/profiles").json()
        self.assertEqual(len(listed["profiles"]), 1)
        self.assertEqual(listed["active"]["key"], "default")
        self.assertEqual({r["role"] for r in listed["roles"]},
                         {"layperson", "analyst", "monitor", "full"})

        created = self.client.post("/api/profiles", json={
            "key": "health", "display_name": "健康领域", "role": "monitor"}).json()
        self.assertEqual(created["profile"]["key"], "health")
        pid = created["profile"]["id"]

        bad_key = self.client.post("/api/profiles", json={
            "key": "Bad Key", "display_name": "x", "role": "full"})
        self.assertEqual(bad_key.status_code, 400)
        dup = self.client.post("/api/profiles", json={
            "key": "default", "display_name": "x", "role": "full"})
        self.assertEqual(dup.status_code, 400)
        bad_role = self.client.post("/api/profiles", json={
            "key": "ok", "display_name": "x", "role": "wizard"})
        self.assertEqual(bad_role.status_code, 400)

        renamed = self.client.put(f"/api/profiles/{pid}",
                                  json={"display_name": "健康监测"}).json()
        self.assertEqual(renamed["profile"]["display_name"], "健康监测")
        self.assertEqual(self.client.put("/api/profiles/999",
                                         json={"display_name": "x"}).status_code, 404)

        activated = self.client.post(f"/api/profiles/{pid}/activate").json()
        self.assertEqual(activated["profile"]["id"], pid)
        self.assertTrue(self.client.get("/api/profiles").json()["active"]["id"] == pid)
        self.assertEqual(self.client.post("/api/profiles/999/activate").status_code, 404)

        # 激活态不能被删；有数据不能被删；空档案可删
        self.assertEqual(self.client.delete(f"/api/profiles/{pid}").status_code, 409)
        self.sessions.create_session("占位", profile_id=pid)
        self.client.post("/api/profiles/1/activate")
        self.assertEqual(self.client.delete(f"/api/profiles/{pid}").status_code, 409)
        self.assertEqual(self.client.delete("/api/profiles/999").status_code, 404)
        empty = self.client.post("/api/profiles", json={
            "key": "empty", "display_name": "空", "role": "full"}).json()["profile"]["id"]
        self.assertEqual(self.client.delete(f"/api/profiles/{empty}").status_code, 200)

    def test_sessions_and_dossiers_scoped_by_active(self):
        """列表按激活档案过滤；跨工作区的会话/档案详情 404（不泄露存在性）。"""
        pid = self.client.post("/api/profiles", json={
            "key": "second", "display_name": "二区", "role": "analyst"}).json()["profile"]["id"]
        s_default = self.sessions.create_session("默认区", profile_id=1)
        s_second = self.sessions.create_session("二区", profile_id=pid)
        d_default = self.store.create_dossier("默认档案", "甲", profile_id=1)
        d_second = self.store.create_dossier("二区档案", "乙", profile_id=pid)

        sessions = self.client.get("/api/sessions").json()["sessions"]
        self.assertEqual({s["id"] for s in sessions}, {s_default})
        dossiers = self.client.get("/api/dossiers").json()["dossiers"]
        self.assertEqual({d["id"] for d in dossiers}, {d_default})

        self.assertEqual(self.client.get(
            f"/api/sessions/{s_second}/messages").status_code, 404)
        self.assertEqual(self.client.get(f"/api/dossiers/{d_second}").status_code, 404)
        self.assertEqual(self.client.get(
            f"/api/dossiers/{d_second}/export").status_code, 404)
        self.assertEqual(self.client.get(
            f"/api/sessions/{s_default}/messages").status_code, 200)
        self.assertEqual(self.client.get(f"/api/dossiers/{d_default}").status_code, 200)

        # 切到二区：视图整体换边
        self.client.post(f"/api/profiles/{pid}/activate")
        sessions = self.client.get("/api/sessions").json()["sessions"]
        self.assertEqual({s["id"] for s in sessions}, {s_second})
        self.assertEqual(self.client.get(
            f"/api/dossiers/{d_default}").status_code, 404)

        # 新建会话归激活档案
        created = self.client.post("/api/sessions", json={"title": "新会话"}).json()
        self.assertEqual(created["profile_id"], pid)

    def test_insights_scoped_by_active(self):
        """/api/insights/* 按激活档案聚合。"""
        pid = self.client.post("/api/profiles", json={
            "key": "second", "display_name": "二区", "role": "analyst"}).json()["profile"]["id"]
        did = self.store.create_dossier("A", "甲", profile_id=1)
        self.store.add_conclusion(did, "高风险谣言", 88.0, confidence="high",
                                  as_of="2026-11-20T10:00:00+08:00")
        patterns = self.client.get("/api/insights/patterns").json()["patterns"]
        self.assertEqual(patterns["total_dossiers"], 1)
        self.client.post(f"/api/profiles/{pid}/activate")
        patterns = self.client.get("/api/insights/patterns").json()["patterns"]
        self.assertEqual(patterns["total_dossiers"], 0)


if __name__ == "__main__":
    unittest.main()
