# -*- coding: utf-8 -*-
"""角色提示词层与能力 Skill 文档测试（v6 角色扩展，2026-11-20）。

覆盖：
  一、_prompts_for_role 合并规则（addon 追加 / 覆盖 / full 逐位不变 / 未知角色回落）；
  二、loop 集成：三角色会话的 router/narrate system 按角色不同，full 与基座逐位一致；
  三、导流口径：analyst/monitor 的路由附加段包含面板导流与"不拼工具"指令；
  四、SKILL.md 结构 lint（8 份能力文档的头字段与必备章节）；
  五、对拍：prompts.json 的 roles 键 ↔ dossier/profiles.py 的 ROLES；
     SKILL.md 的能力 id ↔ capabilities 目录名。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_role_prompts -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.loop import AssistantLoop, _prompts_for_role  # noqa: E402
from mylocalai.assistant.events import EventStream  # noqa: E402
from mylocalai.assistant.session_store import SessionStore  # noqa: E402
from mylocalai.core.privacy import DEFAULT_PREFERENCES, PrivacyPreferences  # noqa: E402
from mylocalai.dossier.profiles import ROLES, ProfileStore  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402

PROMPTS_PATH = ROOT / "src" / "mylocalai" / "assistant" / "prompts.json"
CAPS_DIR = ROOT / "src" / "mylocalai" / "assistant" / "capabilities"

#: 八个用户能力（目录名 ↔ 能力 id 的连字符形式）
EXPECTED_CAPS = {
    "claim_checking": "claim-checking",
    "evidence_research": "evidence-research",
    "topic_understanding": "topic-understanding",
    "speech_understanding": "speech-understanding",
    "material_analysis": "material-analysis",
    "domain_monitoring": "domain-monitoring",
    "information_collection": "information-collection",
    "authority_verification": "authority-verification",
}

#: SKILL.md 头注释必备字段与正文必备章节
REQUIRED_HEADER_FIELDS = ("能力 id", "版本", "一句话目标", "文档性质")
REQUIRED_SECTIONS = ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6.", "## 7.")


def _load_prompts() -> dict:
    return json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))


def _prefs() -> PrivacyPreferences:
    data = json.loads(json.dumps(DEFAULT_PREFERENCES))
    data["save_enabled"] = {"results": True, "raw_input": True, "external_cache": True}
    return PrivacyPreferences(data)


def _stub_route(action="verify_claim", text="网传喝隔夜茶会致癌", **extra):
    """路由 stub：把收到的 system 记下来，供角色断言。"""
    seen = {}

    def _llm(system, user):
        seen["system"] = system
        return ({"action": action, "text": text, **extra}, "")

    _llm.seen = seen
    return _llm


def _stub_narrate(text="核查完成，结论见卡片。"):
    seen = {}

    def _llm(system, user):
        seen["system"] = system
        return (text, "")

    _llm.seen = seen
    return _llm


def _stub_verify(label="疑似谣言", risk=62.0):
    def _svc(text, deep=False, on_component_done=None, cancel_check=None):
        return {"label": label, "risk_score": risk, "signals": ["来源模糊"],
                "degraded_components": [], "agent_error": "", "llm_turns": 2,
                "evidence_book": {"claim": text, "entries": []}}

    return _svc


# ---------------------------------------------------------------------------
# 一、合并规则
# ---------------------------------------------------------------------------

class TestPromptsForRole(unittest.TestCase):
    def setUp(self):
        self.prompts = _load_prompts()

    def test_full_and_unknown_return_base_object(self):
        """full / 未知角色 / roles 段缺失 → 基座原对象（逐位不变）。"""
        self.assertIs(_prompts_for_role(self.prompts, "full"), self.prompts)
        self.assertIs(_prompts_for_role(self.prompts, "wizard"), self.prompts)
        self.assertIs(_prompts_for_role(self.prompts, ""), self.prompts)
        no_roles = {k: v for k, v in self.prompts.items() if k != "roles"}
        self.assertIs(_prompts_for_role(no_roles, "layperson"), no_roles)

    def test_addon_appends_and_override_replaces(self):
        """*_addon 追加到基座同名键之后；普通键覆盖。"""
        merged = _prompts_for_role(self.prompts, "layperson")
        base_router = self.prompts["router_system"]
        self.assertTrue(merged["router_system"].startswith(base_router))
        self.assertIn("普通用户", merged["router_system"])
        self.assertIn("面向普通用户", merged["narrate_system"])
        self.assertNotEqual(merged["chitchat_fallback"],
                            self.prompts["chitchat_fallback"])
        # 基座未被改动（合并返回新 dict；roles 段为浅拷贝同一引用）
        self.assertEqual(self.prompts["router_system"], base_router)
        self.assertIsNot(merged, self.prompts)
        self.assertIs(merged["roles"], self.prompts["roles"])

    def test_mission_and_label_available(self):
        """label/mission 新键可直接 .get 取值。"""
        for role in ("layperson", "analyst", "monitor"):
            merged = _prompts_for_role(self.prompts, role)
            self.assertTrue(merged.get("mission"), role)
            self.assertTrue(merged.get("label"), role)

    def test_every_role_has_addons_bounded(self):
        """每个非 full 角色都有 router/narrate 两个 addon 且长度有界（防提示词膨胀）。"""
        roles = self.prompts.get("roles") or {}
        for role, cfg in roles.items():
            if role == "full" or not isinstance(cfg, dict):
                continue
            for key in ("router_system_addon", "narrate_system_addon"):
                addon = str(cfg.get(key) or "")
                self.assertTrue(addon, f"{role} 缺 {key}")
                self.assertLessEqual(len(addon), 200, f"{role}.{key} 过长")

    def test_analyst_monitor_addons_mention_panels_and_no_tool_stitching(self):
        """analyst/monitor 附加段必须包含面板导流与“不拼工具”式指令。"""
        analyst = _prompts_for_role(self.prompts, "analyst")["router_system"]
        monitor = _prompts_for_role(self.prompts, "monitor")["router_system"]
        layperson = _prompts_for_role(self.prompts, "layperson")["router_system"]
        self.assertIn("材料批次", analyst)
        self.assertIn("不要逐条手动拼", analyst)          # 不拼工具
        self.assertIn("领域监测", monitor)
        self.assertIn("仍按封闭枚举路由", monitor)          # 不新开 action
        self.assertIn("不要拼工具", layperson)
        self.assertIn("不要新开 action", layperson)


# ---------------------------------------------------------------------------
# 二、loop 集成
# ---------------------------------------------------------------------------

class TestLoopRolePrompts(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "app.db"
        self.sessions = SessionStore(db)
        self.addCleanup(self.sessions.close)
        self.dossiers = DossierService(DossierStore(db), privacy=_prefs())
        self.addCleanup(self.dossiers.close)
        self.profiles = ProfileStore(db)
        self.addCleanup(self.profiles.close)
        self.rules = json.loads(
            (ROOT / "src" / "mylocalai" / "assistant" / "assistant_rules.json")
            .read_text(encoding="utf-8"))
        self.prompts = _load_prompts()
        self.default_id = self.profiles.active_id()

    def _session_for(self, role):
        if role == "full":
            return self.sessions.create_session("全能力会话")
        pid = self.profiles.create_profile(f"ws-{role}", f" {role} ", role)
        return self.sessions.create_session(f"{role} 会话", profile_id=pid)

    def _turn(self, session_id, route, narrate, text="网传喝隔夜茶会致癌，真的吗"):
        loop = AssistantLoop(sessions=self.sessions, dossiers=self.dossiers,
                             services={"route_llm": route, "verify": _stub_verify(),
                                       "narrate_llm": narrate},
                             rules=self.rules, prompts=self.prompts)
        events = EventStream()
        loop.handle(session_id, text, events)
        return events

    def test_role_prompts_reach_router_and_narrate(self):
        """三角色：router system 含各自导流关键词，narrate system 含各自 posture。"""
        cases = {
            "layperson": ("当前工作区用户是普通用户", "面向普通用户"),
            "analyst": ("当前工作区用户是专业用户（材料分析）", "面向专业用户"),
            "monitor": ("当前工作区用户是专业用户（领域监测）", "只述检索结果与变化"),
        }
        for index, (role, (router_mark, narrate_mark)) in enumerate(cases.items()):
            sid = self._session_for(role)
            route = _stub_route(text=f"网传某说法{index}是真的吗？")
            narrate = _stub_narrate()
            self._turn(sid, route, narrate)
            self.assertIn(router_mark, route.seen["system"], f"{role} router 缺角色提示词")
            self.assertIn(narrate_mark, narrate.seen["system"], f"{role} narrate 缺角色 posture")

    def test_full_role_prompts_match_unknown_role(self):
        """full 角色：router/narrate system 与未知角色逐位一致（都走基座，不加角色段）。

        注：router system 由 router._router_system 在基座 router_system 之后追加
        工具目录 JSON，因此不与 prompts.json 原值相等——用未知角色会话做对照组。
        两个回合用不同原文（stub 路由的 text 也错开）：同库会沉淀指纹，
        同文本会撞"先问再查"闸走不到叙述。
        """
        sid_full = self._session_for("full")
        route_full = _stub_route(text="网传喝隔夜茶会致癌")
        narrate_full = _stub_narrate()
        self._turn(sid_full, route_full, narrate_full)
        # 未知角色会话：_session_profile 解析不到 → role=full 同路径
        sid_unknown = self.sessions.create_session("野生会话")
        route_unknown = _stub_route(text="据称某地猪肉涨价与疫情有关")
        narrate_unknown = _stub_narrate()
        self._turn(sid_unknown, route_unknown, narrate_unknown)
        self.assertEqual(route_full.seen["system"], route_unknown.seen["system"])
        self.assertEqual(narrate_full.seen["system"], narrate_unknown.seen["system"])
        # 且不含任何角色附加段标记
        for marker in ("当前工作区用户是", "面向普通用户", "面向专业用户"):
            self.assertNotIn(marker, route_full.seen["system"])
            self.assertNotIn(marker, narrate_full.seen["system"])
        # narrate system 与基座逐位一致（narrate 不走 _router_system 包装）
        self.assertEqual(narrate_full.seen["system"], self.prompts["narrate_system"])

    def test_layperson_card_simplified_in_role_flow(self):
        """layperson 会话端到端：结论卡无 risk_score、带 presentation=simple。"""
        sid = self._session_for("layperson")
        events = self._turn(sid, _stub_route(), _stub_narrate())
        cards = [e for e in events.snapshot() if e["event"] == "card"]
        self.assertEqual(len(cards), 1)
        self.assertNotIn("risk_score", cards[0]["card"])
        self.assertEqual(cards[0]["card"].get("presentation"), "simple")


# ---------------------------------------------------------------------------
# 四、SKILL.md 结构 lint
# ---------------------------------------------------------------------------

class TestCapabilitySkillDocs(unittest.TestCase):
    def test_eight_capability_docs_exist_with_header_and_sections(self):
        """八个能力目录各有一份 SKILL.md：头字段齐全、必备章节齐全。"""
        dirs = {p.name for p in CAPS_DIR.iterdir()
                if p.is_dir() and p.name != "__pycache__"}
        self.assertEqual(dirs, set(EXPECTED_CAPS), f"能力目录集合漂移: {dirs}")
        for dirname, cap_id in EXPECTED_CAPS.items():
            path = CAPS_DIR / dirname / "SKILL.md"
            self.assertTrue(path.is_file(), f"缺少 {path}")
            text = path.read_text(encoding="utf-8")
            head = text.split("-->", 1)[0]
            for field in REQUIRED_HEADER_FIELDS:
                self.assertIn(field, head, f"{dirname} 头注释缺字段: {field}")
            self.assertIn(f"能力 id：{cap_id}", head,
                          f"{dirname} 的能力 id 与目录名不一致")
            for section in REQUIRED_SECTIONS:
                self.assertIn(section, text, f"{dirname} 缺章节: {section}")

    def test_no_dangling_overview_references(self):
        """能力文档不再引用不在库内的《项目工作流与 Skill 总览》。"""
        for dirname in EXPECTED_CAPS:
            text = (CAPS_DIR / dirname / "SKILL.md").read_text(encoding="utf-8")
            self.assertNotIn("《总览》", text, f"{dirname} 仍有悬空总览引用")
            self.assertNotIn("总览 §", text, f"{dirname} 仍有悬空总览引用")

    def test_roles_keys_match_profiles_roles(self):
        """对拍：prompts.json 的 roles 键 ⊆ dossier/profiles.py 的 ROLES（_ 开头为注释键）。"""
        roles_cfg = _load_prompts().get("roles") or {}
        self.assertTrue(roles_cfg, "prompts.json 缺 roles 段")
        for key in roles_cfg:
            if key.startswith("_"):
                continue
            self.assertIn(key, ROLES, f"roles 段有 profiles.ROLES 之外的键: {key}")
        # 三角色必须都有配置（full 可以是空 dict）
        for role in ("layperson", "analyst", "monitor"):
            self.assertIn(role, roles_cfg, f"roles 段缺 {role}")


if __name__ == "__main__":
    unittest.main()
