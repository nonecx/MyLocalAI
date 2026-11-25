# -*- coding: utf-8 -*-
"""角色权限层测试：角色工具白名单（route 裁剪 + 目录过滤）+ 面板服务端角色闸。

语义基线（用户已拍板，测试逐条锁定）：
  - 横向工具 6 个 + 通用 7 能力的入口 action → 全角色可用；
  - 专用工具按主角色分发（第五波起 analyst 与 monitor 各含自家专用，不再同集）：
    verify_claim / recheck_dossier / check_statistics → 仅 layperson + full
    （单条说法核查与统计说法核查是普通用户工作区的主责）；
    compare_dossiers → 仅 analyst + full（档案对比是专业用户·材料分析的主责）；
    event_timeline → 仅 monitor + full（事件时间线是专业用户·领域监测的主责）；
  - fail-open：assistant_rules 缺 role_tool_allowlists 段 / 角色缺项 / 非列表 /
    空 → 回落全局 tool_allowlist（不裁剪）；full 恒全量；
  - 面板通道服务端闸：batch → {analyst, full}、monitor → {monitor, full}，
    403 带诚实中文 detail；读端点不设角色闸（只按 profile_id 归属分家）。

全离线：路由 LLM 用 stub、registry 用真实注册表；REST 用临时 SQLite + 注入点，
不碰默认库、不联网。
运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_role_allowlists -v
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到 web/app.py

import mylocalai.assistant as assistant_pkg  # noqa: E402
from mylocalai.assistant import router as router_mod  # noqa: E402
from mylocalai.assistant.loop import AssistantLoop  # noqa: E402
from mylocalai.assistant.registry import ToolRegistry, ValidationError  # noqa: E402
from mylocalai.assistant.workflow import registry as workflow_registry  # noqa: E402

ASSISTANT_DIR = Path(assistant_pkg.__file__).resolve().parent

#: 语义基线里的"专用"工具 → 主角色（full 恒全量，取各家之并）。
#: 与 assistant.workflow.registry.CAPABILITIES 的 cap.roles 对拍：通用能力入口与
#: 横向工具全角色可用；专用入口只在主角色 + full。
SPECIAL_TOOLS = {
    "layperson": ("verify_claim", "recheck_dossier", "check_statistics"),
    "analyst": ("compare_dossiers",),
    "monitor": ("event_timeline",),
}
#: full 的专用工具 = 全部专用入口（恒全量）
FULL_SPECIAL_TOOLS = tuple(t for tools in SPECIAL_TOOLS.values() for t in tools)


def load_json(name: str) -> dict:
    return json.loads((ASSISTANT_DIR / name).read_text(encoding="utf-8"))


def _prefs():
    """隐私偏好（REST 测试的 DossierService 装配用，外发与保存全开）。"""
    from mylocalai.core.privacy import DEFAULT_PREFERENCES, PrivacyPreferences
    data = json.loads(json.dumps(DEFAULT_PREFERENCES))
    data["save_enabled"] = {"results": True, "raw_input": True, "external_cache": True}
    data["external_processing_enabled"] = True
    data["external_processing_providers"] = ["public-search", "public-web"]
    return PrivacyPreferences(data)


# ===========================================================================
# 1. 配置对拍：assistant_rules.role_tool_allowlists 与封闭枚举的关系
# ===========================================================================
class TestRoleAllowlistConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = load_json("assistant_rules.json")
        cls.section = cls.rules["role_tool_allowlists"]

    def test_layperson_and_full_are_supersets(self):
        """full ⊇ 每个角色（恒全量）；三角色互不为超集是允许的（各家专用不同）。"""
        full = set(self.section["full"])
        for role in ("layperson", "analyst", "monitor"):
            self.assertTrue(full >= set(self.section[role]),
                            f"full 应包含 {role} 全部工具")
        # 逐角色精确对拍：白名单 == 通用入口 ∪ 横向工具 ∪ 该角色专用入口 ∪ META
        generic = set()
        for cap_id, cap in workflow_registry.CAPABILITIES.items():
            if cap.category == "通用":
                generic |= set(cap.entry_actions)
        generic |= set(workflow_registry.HORIZONTAL_TOOLS)
        generic |= set(router_mod.META_ACTIONS)
        for role in ("layperson", "analyst", "monitor"):
            expected = generic | set(SPECIAL_TOOLS[role])
            self.assertEqual(set(self.section[role]), expected,
                            f"{role} 白名单应等于 通用+横向+META+自家专用，"
                            f"多出 {set(self.section[role]) - expected}，"
                            f"缺少 {expected - set(self.section[role])}")
        # full = 通用 + 横向 + META + 全部专用入口
        self.assertEqual(set(self.section["full"]),
                         generic | set(FULL_SPECIAL_TOOLS))

    def test_analyst_monitor_scoped_by_dedicated_tools(self):
        """两角色不再同集：差集 = 各自专用对话工具。

        analyst − monitor = {compare_dossiers}、monitor − analyst = {event_timeline}；
        通用能力入口 + 横向工具 → 全角色可用，专用入口只在主角色 + full
        （与 registry CAPABILITIES 的 cap.roles 逐条对拍）。
        """
        for cap_id, cap in workflow_registry.CAPABILITIES.items():
            if cap.composite or cap.channel != "对话":
                continue    # 面板复合能力不占对话路由（服务端闸在 web 层）
            for action in cap.entry_actions:
                for role in ("layperson", "analyst", "monitor"):
                    expected = role in cap.roles
                    self.assertEqual(action in self.section[role], expected,
                                     f"{cap_id}.{action} 在 {role} 应为 {expected}")
                self.assertIn(action, self.section["full"])
        for tool in workflow_registry.HORIZONTAL_TOOLS:
            for role in ("layperson", "analyst", "monitor", "full"):
                self.assertIn(tool, self.section[role],
                              f"横向工具 {tool} 必须对 {role} 可用")
        # 两个专业角色确实分家了：差集正好是各自的专用入口
        analyst, monitor = set(self.section["analyst"]), set(self.section["monitor"])
        self.assertEqual(analyst - monitor, {"compare_dossiers"})
        self.assertEqual(monitor - analyst, {"event_timeline"})

    def test_special_tools_only_for_layperson_and_full(self):
        """专用工具只在主角色 + full；别家角色一律不含。"""
        for owner, tools in SPECIAL_TOOLS.items():
            for tool in tools:
                self.assertIn(tool, self.section[owner],
                              f"{tool} 应出现在主角色 {owner}")
                self.assertIn(tool, self.section["full"],
                              f"{tool} 应出现在 full")
                for role in ("layperson", "analyst", "monitor"):
                    if role == owner:
                        continue
                    self.assertNotIn(tool, self.section[role],
                                     f"{tool} 是 {owner} 的专用工具，不应出现在 {role}")

    def test_all_entries_within_closed_enum(self):
        """白名单任何一项都必须 ∈ TOOL_ACTIONS ∪ META_ACTIONS（封闭枚举，不认新词）。"""
        enum = set(router_mod.TOOL_ACTIONS) | set(router_mod.META_ACTIONS)
        for role, entries in self.section.items():
            if role.startswith("_"):
                continue
            unknown = [a for a in entries if a not in enum]
            self.assertEqual(unknown, [], f"{role} 白名单出现枚举外条目: {unknown}")

    def test_layperson_full_equal_global_allowlist(self):
        """full = 全局 tool_allowlist（恒全量）；三角色 = 全局 − 别家专用入口。

        第五波前 layperson 曾与全局同集；现在三个专用能力分属三个主角色，
        layperson/analyst/monitor 各自裁掉别家的专用入口（详见 SPECIAL_TOOLS）。
        """
        global_list = set(self.rules["tool_allowlist"])
        self.assertEqual(set(self.section["full"]), global_list,
                         "full 必须等于全局 tool_allowlist（恒全量）")
        for role in ("layperson", "analyst", "monitor"):
            others = set(FULL_SPECIAL_TOOLS) - set(SPECIAL_TOOLS[role])
            self.assertEqual(set(self.section[role]), global_list - others,
                             f"{role} 应等于全局减去别家专用入口 {sorted(others)}")

    def test_meta_actions_always_routable(self):
        """chitchat/clarify/refuse 在每个角色的白名单里（元动作永不裁剪）。"""
        for role, entries in self.section.items():
            if role.startswith("_"):
                continue
            for meta in router_mod.META_ACTIONS:
                self.assertIn(meta, entries, f"{role} 缺元动作 {meta}")


# ===========================================================================
# 2. fail-open 解析：_role_allowlist 的回落路径
# ===========================================================================
class TestRoleAllowlistResolution(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = load_json("assistant_rules.json")

    def test_none_role_uses_global(self):
        """role=None → 全局 tool_allowlist（与加角色前逐位一致）。"""
        out = router_mod._role_allowlist(self.rules, None)
        self.assertEqual(out, [str(a) for a in self.rules["tool_allowlist"]])

    def test_full_role_always_global(self):
        """role="full" 恒全量：即使配置段被写坏也直接用全局清单。"""
        broken = dict(self.rules, role_tool_allowlists={"full": ["verify_claim"]})
        out = router_mod._role_allowlist(broken, "full")
        self.assertEqual(out, [str(a) for a in broken["tool_allowlist"]])

    def test_known_role_uses_role_list(self):
        """已知角色 → 该角色白名单（ analyst 不含 verify_claim）。"""
        out = router_mod._role_allowlist(self.rules, "analyst")
        self.assertIn("collect_evidence", out)
        self.assertNotIn("verify_claim", out)

    def test_missing_section_falls_back(self):
        """缺 role_tool_allowlists 段 → 回落全局（不裁剪）。"""
        rules = {k: v for k, v in self.rules.items() if k != "role_tool_allowlists"}
        out = router_mod._role_allowlist(rules, "analyst")
        self.assertEqual(out, [str(a) for a in rules["tool_allowlist"]])

    def test_missing_role_or_bad_shape_falls_back(self):
        """角色缺项 / 值非列表 / 空列表 → 回落全局（fail-open，不裁剪）。"""
        global_list = [str(a) for a in self.rules["tool_allowlist"]]
        cases = (
            {"analyst": None},                              # 值为 None
            {"analyst": "verify_claim"},                    # 非列表
            {"analyst": []},                                # 空列表
            {"monitor": {"a": 1}},                          # 形状坏
        )
        for section in cases:
            with self.subTest(section=section):
                rules = dict(self.rules, role_tool_allowlists=section)
                role = "analyst" if "analyst" in section else "monitor"
                self.assertEqual(router_mod._role_allowlist(rules, role), global_list)

    def test_unknown_role_falls_back(self):
        """未知角色（白名单段里没有的名字）→ 回落全局。"""
        self.assertEqual(router_mod._role_allowlist(self.rules, "no_such_role"),
                         [str(a) for a in self.rules["tool_allowlist"]])


# ===========================================================================
# 3. registry.catalog(allowlist=...) 过滤
# ===========================================================================
class TestCatalogAllowlist(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register("t_b", lambda: None, [], permission="read",
                               description="beta")
        self.registry.register("t_a", lambda: None, [], permission="read",
                               description="alpha")

    def test_none_returns_all(self):
        """allowlist=None → 全量目录（按注册顺序，与加角色前逐位一致）。"""
        self.assertEqual([t["name"] for t in self.registry.catalog()],
                         ["t_b", "t_a"])

    def test_allowlist_filters_by_name(self):
        """给定 allowlist → 只返回名字在清单内的条目（字段形状不变）。"""
        out = self.registry.catalog(allowlist=["t_b"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "t_b")
        self.assertEqual(out[0]["description"], "beta")
        self.assertIn("permission", out[0])

    def test_allowlist_with_unknown_names(self):
        """清单里带未注册的名字 → 对应条目直接不出现（不报错、不造空条目）。"""
        self.assertEqual([t["name"] for t in self.registry.catalog(
            allowlist=["t_a", "ghost"])], ["t_a"])

    def test_empty_allowlist_filters_everything(self):
        """空 allowlist → 空目录（router 层的 fail-open 不会走到这里，直接调用才触发）。"""
        self.assertEqual(self.registry.catalog(allowlist=[]), [])


# ===========================================================================
# 4. route(role=...) 行为：枚举与目录按角色裁剪，role=None 逐位同旧
# ===========================================================================
class RouteRoleTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = load_json("assistant_rules.json")
        self.prompts = load_json("prompts.json")
        self.registry = AssistantLoop().registry   # 真实注册表（校验器同源）

    def route(self, obj, role=None, rules=None):
        """stub route_llm：返回固定路由 JSON，并记录收到的 system 供目录断言。"""
        calls = []

        def _llm(system, user):
            calls.append({"system": system, "user": user})
            return (dict(obj), "")

        out = router_mod.route("u", self.registry, _llm, rules or self.rules,
                               self.prompts, role=role)
        return out, calls


class TestRouteRoleFiltering(RouteRoleTestBase):
    def test_analyst_verify_claim_clarifies(self):
        """role=analyst：路由 LLM 输出被裁剪的 verify_claim → 走枚举外降级（clarify）。"""
        out, _ = self.route({"action": "verify_claim", "text": "网传自来水被下毒"},
                            role="analyst")
        self.assertEqual(out["action"], "clarify")
        self.assertIn("verify_claim", out["error"])

    def test_analyst_recheck_dossier_clarifies(self):
        """role=analyst：recheck_dossier 同样被裁（专用工具仅 layperson/full）。"""
        out, _ = self.route({"action": "recheck_dossier", "id": 3}, role="monitor")
        self.assertEqual(out["action"], "clarify")

    def test_analyst_horizontal_tool_passes(self):
        """role=analyst：横向/通用工具（check_heat）照常路由出参。"""
        out, _ = self.route({"action": "check_heat", "topic": "某话题"}, role="analyst")
        self.assertEqual(out["action"], "check_heat")
        self.assertEqual(out["args"], {"topic": "某话题"})

    def test_analyst_catalog_hides_trimmed_tools(self):
        """role=analyst：路由 system 的工具目录（catalog JSON 段）只列该角色可用的工具。"""
        _, calls = self.route({"action": "chitchat", "reply": "x"}, role="analyst")
        system = calls[0]["system"]
        catalog_json = system.split("可用工具目录", 1)[-1]   # 基座 prompt 文本仍会点名工具，只看目录段
        self.assertNotIn('"verify_claim"', catalog_json, "被裁工具不应进目录")
        self.assertNotIn('"recheck_dossier"', catalog_json)
        self.assertIn('"check_heat"', catalog_json, "可用工具应在目录里")
        self.assertIn('"collect_evidence"', catalog_json)

    def test_analyst_meta_actions_still_routable(self):
        """role=analyst：元动作（chitchat/clarify/refuse）永不裁剪。"""
        out, _ = self.route({"action": "chitchat", "reply": "你好"}, role="analyst")
        self.assertEqual(out["action"], "chitchat")
        self.assertEqual(out["reply"], "你好")

    def test_layperson_verify_claim_passes(self):
        """role=layperson：专用工具 verify_claim 正常出参（普通用户主责）。"""
        out, _ = self.route({"action": "verify_claim", "text": "网传自来水被下毒",
                             "hacker_key": "drop table"}, role="layperson")
        self.assertEqual(out["action"], "verify_claim")
        self.assertEqual(out["args"], {"text": "网传自来水被下毒", "deep": False})

    def test_full_role_verify_claim_passes(self):
        """role=full：恒全量，verify_claim 可用。"""
        out, _ = self.route({"action": "verify_claim", "text": "x"}, role="full")
        self.assertEqual(out["action"], "verify_claim")

    def test_role_none_matches_legacy_behavior(self):
        """role=None / 不传 role：两者逐位一致，verify_claim 照常出参、目录全量。"""
        obj = {"action": "verify_claim", "text": "x"}
        legacy_calls = []

        def _legacy_llm(system, user):
            legacy_calls.append({"system": system, "user": user})
            return (dict(obj), "")

        legacy = router_mod.route("u", self.registry, _legacy_llm, self.rules,
                                  self.prompts)   # 旧调用形状：不传 role
        with_role, calls_with = self.route(obj, role=None)
        self.assertEqual(legacy["action"], "verify_claim")
        self.assertEqual(legacy["args"], with_role["args"])
        self.assertEqual(legacy_calls[0]["system"], calls_with[0]["system"])
        self.assertIn("verify_claim", calls_with[0]["system"])

    def test_fail_open_unknown_role_keeps_verify_claim(self):
        """未知角色（白名单缺项）→ 回落全局，verify_claim 仍可用。"""
        out, calls = self.route({"action": "verify_claim", "text": "x"},
                                role="no_such_role")
        self.assertEqual(out["action"], "verify_claim")
        self.assertIn("verify_claim", calls[0]["system"])

    def test_fail_open_missing_section_keeps_verify_claim(self):
        """rules 删掉 role_tool_allowlists 段 → 回落全局（不裁剪）。"""
        rules = {k: v for k, v in self.rules.items() if k != "role_tool_allowlists"}
        out, _ = self.route({"action": "verify_claim", "text": "x"},
                            role="analyst", rules=rules)
        self.assertEqual(out["action"], "verify_claim")

    def test_bad_args_still_clarify_under_role(self):
        """角色裁剪不改变参数校验：analyst + check_heat 缺 topic → clarify。"""
        out, _ = self.route({"action": "check_heat"}, role="analyst")
        self.assertEqual(out["action"], "clarify")

    def test_validation_error_still_raises_for_direct_use(self):
        """registry.validate 的契约不受本改动影响（供防护链②直接调用）。"""
        with self.assertRaises(ValidationError):
            self.registry.validate("verify_claim", {})


# ===========================================================================
# 5. 面板服务端角色闸（REST）：batch → {analyst, full}，monitor → {monitor, full}
# ===========================================================================
class PanelGateTestBase(unittest.TestCase):
    """TestClient + 临时 SQLite + 注入点（batch/monitor/profiles 三store 同库）。"""

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
        from mylocalai.dossier.profiles import ProfileStore
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        from mylocalai.web import profiles_api
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "dossier.db"
        self.profiles = ProfileStore(db)
        self.addCleanup(self.profiles.close)
        self._patch(profiles_api, "get_profile_store", lambda: self.profiles)
        self._patch(self.webapp, "get_dossier_service",
                    lambda: DossierService(DossierStore(db), privacy=_prefs()))

    def _patch(self, module, attr, value) -> None:
        patcher = mock.patch.object(module, attr, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def activate(self, key: str, display: str, role: str) -> int:
        """建一个指定角色的档案工作区并激活（写 app_state，web 闸随之生效）。"""
        pid = self.profiles.create_profile(key, display, role)
        self.profiles.set_active(pid)
        return pid


class TestBatchPanelRoleGate(PanelGateTestBase):
    def setUp(self) -> None:
        super().setUp()
        from mylocalai.dossier.batches import BatchStore
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        from mylocalai.web import batch_api
        self.batch_api = batch_api
        db = Path(self.tmp.name) / "dossier.db"
        self.store = BatchStore(db)
        self.addCleanup(self.store.close)
        self.service = DossierService(DossierStore(db), privacy=_prefs())
        self.addCleanup(self.service.close)
        self._patch(batch_api, "get_batch_store", lambda: self.store)
        # 批次 worker 用注入的 stub loop（不碰真实默认库）
        stub_loop = mock.Mock()
        stub_loop.dossiers = self.service
        stub_loop.services = {"verify": self._stub_verify()}
        self._patch(batch_api, "_loop_factory", lambda: stub_loop)
        self.addCleanup(self._join_workers)

    @staticmethod
    def _stub_verify():
        def _svc(text, deep=False, on_component_done=None, cancel_check=None):
            return {"label": "疑似谣言", "risk_score": 62.0, "signals": ["来源模糊"],
                    "degraded_components": [], "agent_error": "", "llm_turns": 1,
                    "evidence_book": {"claim": text, "entries": []}}
        return _svc

    @staticmethod
    def _join_workers(timeout: float = 10.0) -> None:
        for thread in threading.enumerate():
            if thread.name.startswith("batch-") and thread.is_alive():
                thread.join(timeout)

    def test_analyst_can_create_batch(self):
        """analyst：建批次 202（写/运行端点放行专业用户·材料分析）。"""
        self.activate("ws-a", "材料分析区", "analyst")
        resp = self.client.post("/api/batch", json={"items": ["材料甲"]})
        self.assertEqual(resp.status_code, 202, resp.text)
        # 202 的语义是"已受理并开跑"：后台线程跑得快（单条 stub 材料）时
        # 断言前就已 done，这里两种都接受——本条测的是角色闸放行，不是调度时序
        # （全量套件并发跑时曾因这条竞态偶发失败）。
        self.assertIn(self.store.get_batch(resp.json()["batch"]["id"])["status"],
                      ("running", "done"))

    def test_full_can_create_batch(self):
        """full：建批次 202（恒全量）。"""
        self.activate("ws-f", "全量区", "full")
        resp = self.client.post("/api/batch", json={"items": ["材料甲"]})
        self.assertEqual(resp.status_code, 202, resp.text)

    def test_monitor_and_layperson_create_batch_403(self):
        """monitor/layperson：建批次 403 + 诚实中文 detail（写端点角色闸）。"""
        for key, role in (("ws-m", "monitor"), ("ws-l", "layperson")):
            with self.subTest(role=role):
                self.activate(key, f"{role}区", role)
                resp = self.client.post("/api/batch", json={"items": ["材料甲"]})
                self.assertEqual(resp.status_code, 403, resp.text)
                detail = str(resp.json()["detail"])
                self.assertIn("材料批次", detail)
                self.assertTrue(detail.strip())
                # 403 的请求不落库（闸在存储之前）
                self.assertEqual(self.store.list_batches(), [])

    def test_read_endpoints_not_role_gated(self):
        """读端点（GET 列表/详情）不做角色闸：monitor 角色也能看本工作区批次。"""
        self.activate("ws-m", "领域监测区", "monitor")
        bid = self.store.create_batch("监测区批次", "text", [{"text": "甲", "url": ""}])
        resp = self.client.get("/api/batch")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(bid, [b["id"] for b in resp.json()["batches"]])
        self.assertEqual(self.client.get(f"/api/batch/{bid}").status_code, 200)

    def test_active_role_fail_open(self):
        """profile store 读不到（旧库/故障）→ _active_role 返 full，闸放行。"""
        from mylocalai.web import profiles_api

        def _boom():
            raise RuntimeError("profile store 不可用")

        self._patch(profiles_api, "get_profile_store", _boom)
        self.assertEqual(self.batch_api._active_role(), "full")


class TestMonitorPanelRoleGate(PanelGateTestBase):
    def setUp(self) -> None:
        super().setUp()
        from mylocalai.dossier.service import DossierService
        from mylocalai.dossier.store import DossierStore
        from mylocalai.dossier.subscriptions import SubscriptionStore
        from mylocalai.web import monitor_api
        self.monitor_api = monitor_api
        db = Path(self.tmp.name) / "dossier.db"
        self.store = SubscriptionStore(db)
        self.addCleanup(self.store.close)
        self.service = DossierService(DossierStore(db), privacy=_prefs())
        self.addCleanup(self.service.close)
        self._patch(monitor_api, "get_subscription_store", lambda: self.store)
        self._patch(monitor_api, "_dossiers_factory", lambda: self.service)

    def _make_sub(self, profile_id: int | None = None) -> int:
        return self.store.create_subscription("某地自来水", [], [], 3600,
                                              profile_id=profile_id)

    def test_monitor_can_crud_and_run_now(self):
        """monitor：建/改/立即跑全放行（run_worker 用 no-op stub，不联网）。"""
        self.activate("ws-m", "领域监测区", "monitor")
        resp = self.client.post("/api/subscriptions",
                                json={"topic": "某地自来水", "frequency_s": 1800})
        self.assertEqual(resp.status_code, 200, resp.text)
        sid = resp.json()["subscription"]["id"]
        resp = self.client.put(f"/api/subscriptions/{sid}", json={"frequency_s": 3600})
        self.assertEqual(resp.status_code, 200, resp.text)
        with mock.patch.object(self.monitor_api.tools_monitor, "run_subscription",
                               lambda *a, **k: None):
            resp = self.client.post(f"/api/subscriptions/{sid}/run-now")
            self.assertEqual(resp.status_code, 202, resp.text)
        resp = self.client.delete(f"/api/subscriptions/{sid}")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_analyst_write_endpoints_403(self):
        """analyst：建/改/删/立即跑一律 403 + 诚实中文 detail。"""
        self.activate("ws-a", "材料分析区", "analyst")
        sid = self._make_sub()   # 直接落库造一条（归属 analyst 工作区）
        detail_403 = "领域监测"
        cases = (
            ("post", "/api/subscriptions", {"topic": "x", "frequency_s": 3600}, None),
            ("put", f"/api/subscriptions/{sid}", {"enabled": False}, None),
            ("delete", f"/api/subscriptions/{sid}", None, None),
            ("post", f"/api/subscriptions/{sid}/run-now", None, None),
        )
        for method, url, body, _ in cases:
            with self.subTest(method=method):
                if body is None:
                    resp = getattr(self.client, method)(url)
                else:
                    resp = getattr(self.client, method)(url, json=body)
                self.assertEqual(resp.status_code, 403, f"{method} {url}: {resp.text}")
                self.assertIn(detail_403, str(resp.json()["detail"]))
        # 订阅还在（DELETE 被 403 挡下，未真删）
        self.assertIsNotNone(self.store.get_subscription(sid))

    def test_layperson_create_403(self):
        """layperson：建订阅 403（角色闸拦在存储之前）。"""
        self.activate("ws-l", "普通用户区", "layperson")
        resp = self.client.post("/api/subscriptions",
                                json={"topic": "某地自来水", "frequency_s": 1800})
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(self.store.list_subscriptions(profile_id=None), [])

    def test_full_can_create(self):
        """full：建订阅放行。"""
        self.activate("ws-f", "全量区", "full")
        resp = self.client.post("/api/subscriptions",
                                json={"topic": "某地自来水", "frequency_s": 1800})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_read_endpoints_not_role_gated(self):
        """读端点（GET 列表/详情/历史）不做角色闸：analyst 也能看本工作区订阅。"""
        self.activate("ws-a", "材料分析区", "analyst")
        sid = self._make_sub()
        resp = self.client.get("/api/subscriptions")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(sid, [s["id"] for s in resp.json()["subscriptions"]])
        self.assertEqual(self.client.get(f"/api/subscriptions/{sid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/subscriptions/{sid}/runs").status_code,
                         200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
