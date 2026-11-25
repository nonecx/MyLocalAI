# -*- coding: utf-8 -*-
"""路由降级与确定性兜底测试（WS1，2026-11-24）：LLM 不可用时的 salvage + 诚实降级话术。

锁定（对着真实事故形态：longcat 未获隐私授权 → 路由 LLM 全灭 → 热度问题被误导性
澄清话术挡住、系统坏了却零信号）：
   - salvage 正例：热度疑问 → check_heat（话题词经过去填充词/问句词/指示代词清理）、
     x/y/z 直算 → compute_heat（float 收口）；结果带 salvaged=True 且走过与正常路由
     完全相同的防护链（registry.validate + check_allowed）；
   - carve-out 全族：真假+热度同句（mixed_choice 先命中）、档案引用（"档案#3的热度
     快照"是记忆类意图）、纯指代话题（"这件事的热度"）、空话题（"有多热"）→ 不兜底；
   - 仅在 LLM 失败时触发：LLM 正常返回 dict 时行为逐位不变（不抢路由）；
   - 角色裁剪：check_heat 被角色白名单裁掉 → 兜底同样落回 clarify；
   - llm_failed 标记：LLM 失败分支的 clarify 带标记；"dict 但枚举外/参数坏"不带；
   - loop 级：热度问题 salvage 出卡（叙述失败也不丢卡）；无热度模式 + 路由失败 →
     route_unavailable_reply 诚实降级话术 + degraded=True；模型自选 clarify 不变；
   - _route_failure_guidance 四类映射（隐私/密钥/网络/其它）+ 提供方名提取；
   - route 永不抛异常（怪输入 + 失败 LLM）。

全离线：route_llm/heat/heat_formula/narrate_llm 全部 stub，registry 用真实注册表
（校验器与生产同源）；loop 级用 :memory: 临时同库。零网络零配额。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import router as router_mod  # noqa: E402
from mylocalai.assistant.loop import (_ROUTE_UNAVAILABLE_FALLBACK,  # noqa: E402
                                      _route_failure_guidance)
from tests.test_assistant_loop import (LoopTestBase, stub_heat,  # noqa: E402
                                       stub_narrate)
from tests.test_assistant_router import RouterTestBase  # noqa: E402

#: 实测事故原文（提供方未获隐私授权、无密钥）：路由 LLM 一律返回 (None, err)。
PRIVACY_ERR = ("longcat: PrivacyAuthorizationError: External processing is "
               "disabled for provider 'longcat'")
KEY_ERR = "deepseek: 未配置 DEEPSEEK_API_KEY（提供方 deepseek）"
NET_ERR = "deepseek: 连接超时（read timeout）"
OTHER_ERR = "模型返回无法解析的JSON"


def run_route(user_text, registry, rules, prompts, *, obj=None, err="",
              raises=False, role=None, history_hint=""):
    """stub route_llm 的路由帮手：可编程返回/抛错；记录收到的 system/user 供断言。"""
    calls = []

    def _llm(system, user):
        calls.append({"system": system, "user": user})
        if raises:
            raise RuntimeError("路由模型不可用")
        if err:
            return (None, err)
        return (obj, "")

    out = router_mod.route(user_text, registry, _llm, rules, prompts,
                           history_hint=history_hint, role=role)
    out["_calls"] = calls
    return out


class TestSalvagePositives(RouterTestBase):
    """LLM 失败时两类零 LLM 依赖意图被确定性捞回（salvage）。"""

    def test_heat_question_salvages_check_heat(self):
        r = run_route("deep seek配额事件热度如何", self.registry, self.rules,
                      self.prompts, err=PRIVACY_ERR)
        self.assertEqual(r["action"], "check_heat")
        self.assertEqual(r["args"], {"topic": "deep seek配额事件"})
        self.assertTrue(r["salvaged"])
        self.assertEqual(r["reply"], "")
        self.assertEqual(r["error"], "")
        self.assertNotIn("llm_failed", r)   # 兜底成功 ≠ 路由失败

    def test_xyz_direct_compute_salvages(self):
        """x/y/z 三赋值齐全 → compute_heat；数值经 registry 校验收口成 float。"""
        r = run_route("算一下关注度 x=1000, y=100000000, z=1000000", self.registry,
                      self.rules, self.prompts, err=PRIVACY_ERR)
        self.assertEqual(r["action"], "compute_heat")
        self.assertEqual(r["args"], {"x": 1000.0, "y": 100000000.0, "z": 1000000.0})
        for v in r["args"].values():
            self.assertIsInstance(v, float)
        self.assertTrue(r["salvaged"])

    def test_heat_topic_cleanup_variants(self):
        """话题清理：填充词/问句词/指示代词逐层剥掉，只剩话题词。"""
        cases = {
            "XX话题多热": "XX话题",
            "deep seek配额事件受关注吗": "deep seek配额事件",
            "帮我查一下某话题的热度": "某话题的",
            "帮我查一下新冠疫情有多热": "新冠疫情",
        }
        for text, topic in cases.items():
            with self.subTest(text=text):
                r = run_route(text, self.registry, self.rules, self.prompts,
                              err=PRIVACY_ERR)
                self.assertEqual(r["action"], "check_heat")
                self.assertEqual(r["args"], {"topic": topic})
                self.assertTrue(r["salvaged"])

    def test_salvage_also_fires_when_llm_raises(self):
        """route_llm 抛异常与返回 None 同形：同样先兜底再 clarify。"""
        r = run_route("XX话题多热", self.registry, self.rules, self.prompts,
                      raises=True)
        self.assertEqual(r["action"], "check_heat")
        self.assertEqual(r["args"], {"topic": "XX话题"})
        self.assertTrue(r["salvaged"])


class TestSalvageCarveOuts(RouterTestBase):
    """carve-out 全族：LLM 失败也不兜底，落回 clarify 且带 llm_failed。"""

    def assert_clarified(self, r):
        self.assertEqual(r["action"], "clarify")
        self.assertTrue(r.get("llm_failed"))
        self.assertTrue(r["error"])
        self.assertNotIn("salvaged", r)

    def test_archive_reference_not_stolen(self):
        """档案引用是记忆类意图（"档案#3的热度快照"），兜底不能抢。"""
        for text in ("帮我看看档案#3的热度快照", "刚才那条的热度",
                     "这份材料的热度", "上次那个话题有多热"):
            with self.subTest(text=text):
                self.assert_clarified(
                    run_route(text, self.registry, self.rules, self.prompts,
                              err=PRIVACY_ERR))

    def test_reference_only_topic_not_salvaged(self):
        """去掉热度短语后只剩指代（"这件事"）→ 没有可检索的话题，不兜底。"""
        for text in ("有多热", "这件事的热度", "这事儿的热度", "这个话题的热度",
                     "消息的热度", "新闻的热度", "事情的热度", "它的热度",
                     "该事件的热度"):
            with self.subTest(text=text):
                self.assert_clarified(
                    run_route(text, self.registry, self.rules, self.prompts,
                              err=PRIVACY_ERR))

    def test_truth_plus_heat_goes_to_mixed_choice(self):
        """真假+热度同句：mixed_choice 先命中（路由 LLM 之前），返回选择话术。"""
        r = run_route("这事儿有这么火吗？是真的还是假的？", self.registry,
                      self.rules, self.prompts, err=PRIVACY_ERR)
        self.assertEqual(r["action"], "clarify")
        self.assertEqual(r["reply"], self.prompts["mixed_choose_reply"])
        self.assertNotIn("llm_failed", r)   # 没走到 LLM 失败分支
        self.assertNotIn("salvaged", r)

    def test_truth_question_not_salvaged(self):
        """真假疑问（"是不是真的"）→ carve-out，交给 mixed_choice/LLM，不兜底。"""
        r = run_route("帮我核实这个热搜是不是真的", self.registry, self.rules,
                      self.prompts, err=PRIVACY_ERR)
        self.assert_clarified(r)


class TestSalvageOnlyWhenLlmFails(RouterTestBase):
    """LLM 正常返回 dict 时兜底不启用：行为逐位不变（不抢路由）。"""

    def test_llm_dict_result_not_overridden(self):
        """stub 返回 get_dossier + "档案#3的热度" → get_dossier（证明不抢路由）。"""
        r = run_route("档案#3的热度", self.registry, self.rules, self.prompts,
                      obj={"action": "get_dossier", "id": 3})
        self.assertEqual(r["action"], "get_dossier")
        self.assertEqual(r["args"], {"id": 3})
        self.assertNotIn("salvaged", r)

    def test_llm_chitchat_not_overridden(self):
        """stub 返回 chitchat + "XX多热" → chitchat（模型说闲聊就闲聊）。"""
        r = run_route("XX多热", self.registry, self.rules, self.prompts,
                      obj={"action": "chitchat", "reply": "在"})
        self.assertEqual(r["action"], "chitchat")
        self.assertEqual(r["reply"], "在")

    def test_llm_dict_with_error_still_normal_path(self):
        """返回 dict 但带 err（chat_json 包装层的告警）→ 正常路径，不加 llm_failed。"""
        def _llm(system, user):
            return ({"action": "clarify", "reply": ""}, "some warning")

        out = router_mod.route("那个东西帮我看看", self.registry, _llm,
                               self.rules, self.prompts)
        self.assertEqual(out["action"], "clarify")
        self.assertNotIn("llm_failed", out)

    def test_history_hint_does_not_block_salvage(self):
        """兜底判定用原始 user_text：history_hint 里的"#3《…》"不误触档案 carve-out。"""
        r = run_route("deep seek配额事件热度如何", self.registry, self.rules,
                      self.prompts, err=PRIVACY_ERR,
                      history_hint="活跃档案：#3《某消息》(疑似谣言, 2026-10-31T08:00:00+08:00)")
        self.assertEqual(r["action"], "check_heat")
        self.assertTrue(r["salvaged"])
        # LLM 收到的 user 文本仍带会话背景（既有契约不变）
        self.assertIn("【会话背景】", r["_calls"][0]["user"])


class TestSalvageRoleTrimming(RouterTestBase):
    """角色裁剪：兜底产物同样过角色白名单（check_allowed(allowlist=...)）。"""

    def test_layperson_allowlist_keeps_salvage(self):
        """rules 换成 layperson 白名单（含 check_heat）→ salvage 生效。"""
        rules = dict(self.rules)
        rules["role_tool_allowlists"] = {"layperson": ["check_heat", "chitchat",
                                                       "clarify", "refuse"]}
        r = run_route("XX话题多热", self.registry, rules, self.prompts,
                      err=PRIVACY_ERR, role="layperson")
        self.assertEqual(r["action"], "check_heat")
        self.assertTrue(r["salvaged"])

    def test_allowlist_without_check_heat_falls_back_to_clarify(self):
        """自造一个不含 check_heat 的白名单 → salvage 落回 clarify（防护链同权）。"""
        rules = dict(self.rules)
        rules["tool_allowlist"] = [a for a in self.rules["tool_allowlist"]
                                   if a != "check_heat"]
        r = run_route("XX话题多热", self.registry, rules, self.prompts,
                      err=PRIVACY_ERR)
        self.assertEqual(r["action"], "clarify")
        self.assertTrue(r.get("llm_failed"))

    def test_role_allowlist_without_check_heat_falls_back(self):
        """角色白名单裁掉 check_heat → 兜底同样落回 clarify。"""
        rules = dict(self.rules)
        rules["role_tool_allowlists"] = {"monitor": ["event_timeline", "chitchat",
                                                     "clarify", "refuse"]}
        r = run_route("XX话题多热", self.registry, rules, self.prompts,
                      err=PRIVACY_ERR, role="monitor")
        self.assertEqual(r["action"], "clarify")
        self.assertTrue(r.get("llm_failed"))


class TestLlmFailedFlag(RouterTestBase):
    """llm_failed 标记的边界：只有"路由模型不可用"的 clarify 才带。"""

    def test_bad_json_clarify_carries_flag_and_error(self):
        r = run_route("u", self.registry, self.rules, self.prompts, err=OTHER_ERR)
        self.assertEqual(r["action"], "clarify")
        self.assertTrue(r.get("llm_failed"))
        self.assertEqual(r["error"], OTHER_ERR)

    def test_enum_outside_action_has_no_flag(self):
        """枚举外 action（LLM 返回 dict）→ clarify 但不带 llm_failed：确实意图不清。"""
        r = run_route("u", self.registry, self.rules, self.prompts,
                      obj={"action": "delete_dossier", "id": 1})
        self.assertEqual(r["action"], "clarify")
        self.assertNotIn("llm_failed", r)

    def test_bad_params_has_no_flag(self):
        """参数坏（LLM 返回 dict）→ clarify 但不带 llm_failed。"""
        r = run_route("u", self.registry, self.rules, self.prompts,
                      obj={"action": "compute_heat", "x": -1, "y": 0, "z": 0})
        self.assertEqual(r["action"], "clarify")
        self.assertNotIn("llm_failed", r)

    def test_llm_raises_carries_flag(self):
        r = run_route("u", self.registry, self.rules, self.prompts, raises=True)
        self.assertEqual(r["action"], "clarify")
        self.assertTrue(r.get("llm_failed"))
        self.assertTrue(r["error"])


class TestRouteFailureGuidance(unittest.TestCase):
    """_route_failure_guidance 四类映射 + 提供方名提取（纯函数）。"""

    def test_privacy_authorization_mapping(self):
        reason, fix = _route_failure_guidance(PRIVACY_ERR)
        self.assertEqual(reason, "外部处理未授权（提供方 longcat）")
        self.assertIn("隐私与授权", fix)

    def test_privacy_provider_fallback_chain(self):
        """provider 'xxx' 提取不到 → 冒号前的片段；再不行 → "当前"。"""
        reason, _ = _route_failure_guidance("PrivacyAuthorizationError:  boom")
        self.assertEqual(reason, "外部处理未授权（提供方 PrivacyAuthorizationError）")
        reason2, _ = _route_failure_guidance(": PrivacyAuthorizationError")
        self.assertEqual(reason2, "外部处理未授权（提供方 当前）")

    def test_missing_key_mapping(self):
        """未配置密钥：reason 带提供方名（冒号前缀即提供方，真实格式
        "longcat: 未配置 LONGCAT_API_KEY（提供方 longcat）"），fix 指引导配密钥。"""
        cases = (KEY_ERR,
                 "longcat: 未配置 LONGCAT_API_KEY（提供方 longcat）",
                 "openai: 未配置密钥（OPENAI_API_KEY）")
        for err in cases:
            with self.subTest(err=err):
                reason, fix = _route_failure_guidance(err)
                self.assertIn("未配置 API 密钥", reason)
                self.assertIn("设置 → 模型", fix)
        # 提供方名从冒号前缀提取（真实报错格式）
        reason, _ = _route_failure_guidance(
            "longcat: 未配置 LONGCAT_API_KEY（提供方 longcat）")
        self.assertEqual(reason, "提供方 longcat 未配置 API 密钥")

    def test_network_mapping(self):
        """网络/超时：含"超时"/"timeout"/"连接"即映射（真实格式
        "deepseek: API 调用失败: ConnectTimeout: …" 走 timeout 分支）。"""
        cases = (NET_ERR, "read timeout", "连接被拒绝",
                 "deepseek: API 调用失败: ConnectTimeout: Connection timed out")
        for err in cases:
            with self.subTest(err=err):
                reason, fix = _route_failure_guidance(err)
                self.assertIn("网络/超时", reason)
                self.assertIn("检查网络", fix)

    def test_other_mapping_and_truncation(self):
        long_err = "啊" * 200
        reason, fix = _route_failure_guidance(long_err)
        self.assertEqual(reason, "啊" * 80)   # 截断 80 字
        self.assertIn("服务日志", fix)
        reason2, _ = _route_failure_guidance("")
        self.assertEqual(reason2, "路由模型无响应")

    def test_fallback_constant_matches_prompts(self):
        """内置兜底与 prompts.json 新键同文（改文案只改一处生效优先）。"""
        import json
        data = json.loads((Path(router_mod.__file__).parent / "prompts.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(_ROUTE_UNAVAILABLE_FALLBACK, data["route_unavailable_reply"])
        self.assertIn("{reason}", _ROUTE_UNAVAILABLE_FALLBACK)
        self.assertIn("{fix}", _ROUTE_UNAVAILABLE_FALLBACK)


class TestRouteNeverRaises(RouterTestBase):
    """怪输入 + 失败 LLM：route 永不抛异常，最坏也是 clarify（降级文化）。"""

    def test_weird_inputs_with_failing_llm(self):
        for text in (None, 123, ["list"], {}, "正常消息"):
            with self.subTest(text=text):
                try:
                    out = run_route(text, self.registry, self.rules, self.prompts,
                                    err=PRIVACY_ERR)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"route 对 {text!r} 抛了异常: {exc}")
                self.assertEqual(out["action"], "clarify")
                self.assertTrue(out.get("llm_failed"))

    def test_deterministic_action_never_raises(self):
        for text in (None, 123, ["x=1"], {}, "多热", "x=1 y=2 z=3"):
            with self.subTest(text=text):
                try:
                    router_mod._deterministic_action(text)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"_deterministic_action 对 {text!r} 抛了异常: {exc}")


def failing_route_llm(err):
    """恒定失败的 route_llm 桩（模拟提供方不可用）。"""

    def _llm(system, user):
        return (None, err)

    return _llm


def expected_unavailable_reply(prompts, err):
    """按 loop 层同款规则算出期望的诚实降级话术（reason/fix 替换）。"""
    reason, fix = _route_failure_guidance(err)
    tpl = str(prompts.get("route_unavailable_reply") or "").strip() \
        or _ROUTE_UNAVAILABLE_FALLBACK
    return tpl.replace("{reason}", reason).replace("{fix}", fix)


class TestLoopRouteDegradation(LoopTestBase):
    """loop 级：salvage 出卡（叙述失败也不丢卡）+ 诚实降级话术。"""

    def test_heat_question_salvaged_card_and_tool_run(self):
        """热度问题 + 路由隐私失败 → salvage check_heat：有 card（heat）、有 tool_run、
        digest 含"[热度评估]"（经叙述上下文透传）。"""
        narrate = stub_narrate()
        out, events = self.run_turn(
            {"route_llm": failing_route_llm(PRIVACY_ERR), "heat": stub_heat(),
             "narrate_llm": narrate},
            "deep seek配额事件热度如何")
        names = [e["event"] for e in events]
        self.assertEqual(out["action"], "check_heat")
        self.assertIn("card", names)
        card_evt = next(e for e in events if e["event"] == "card")
        self.assertEqual(card_evt["card_type"], "heat")
        self.assertLess(names.index("card"), names.index("narrative"))  # 卡在叙述前
        runs = self.sessions.runs_for_session(self.session_id)
        self.assertEqual([r["tool"] for r in runs], ["check_heat"])
        self.assertEqual(runs[0]["status"], "ok")
        self.assertIn("[热度评估]", narrate.calls[0]["user"])   # digest 进叙述上下文

    def test_xyz_salvage_survives_narrate_failure(self):
        """x/y/z 直算 + 叙述也失败 → narrative_fallback 收尾但卡仍到（不丢结论）。"""
        out, events = self.run_turn(
            {"route_llm": failing_route_llm(PRIVACY_ERR),
             "heat_formula": lambda x, y, z: {
                 "x": x, "y": y, "z": z, "score": 51.2, "band": "中热",
                 "profile": "spec", "proxy_flags": [], "degraded": False},
             "narrate_llm": stub_narrate(None)},
            "算一下关注度 x=1000, y=100000000, z=1000000")
        names = [e["event"] for e in events]
        self.assertEqual(out["action"], "compute_heat")
        self.assertIn("card", names)
        self.assertIn("核查完成，结论见上方卡片", out["reply"])
        self.assertIn("[热度评估]", out["reply"])   # fallback 带 digest 要点

    def test_unroutable_message_gets_honest_degraded_reply(self):
        """无热度模式 + 路由隐私失败 → 诚实降级话术（不是澄清追问）、无工具、degraded。"""
        out, events = self.run_turn(
            {"route_llm": failing_route_llm(PRIVACY_ERR),
             "narrate_llm": stub_narrate()},
            "那个东西帮我看看")
        expected = expected_unavailable_reply(self.prompts, PRIVACY_ERR)
        self.assertEqual(out["action"], "clarify")
        self.assertEqual(out["reply"], expected)
        self.assertIn("路由模型这会不可用", out["reply"])
        self.assertIn("隐私与授权", out["reply"])   # fix 文案进话术
        self.assertNotEqual(out["reply"], self.prompts["clarify_reply"])
        self.assertIs(out["degraded"], True)
        self.assertEqual(self.sessions.runs_for_session(self.session_id), [])
        self.assertNotIn("tool_start", [e["event"] for e in events])
        # 落库的回复与返回一致（不是空）
        self.assertEqual(self.sessions.messages(self.session_id)[-1]["content"],
                         expected)

    def test_model_chosen_clarify_reply_unchanged(self):
        """模型自选 clarify（stub 返回 clarify、reply 空）→ 仍用 clarify_reply（不变）。"""

        def chosen(system, user):
            return ({"action": "clarify", "reply": ""}, "")

        out, _ = self.run_turn({"route_llm": chosen}, "那个东西帮我看看")
        self.assertEqual(out["reply"], self.prompts["clarify_reply"])
        self.assertFalse(out["degraded"])

    def test_route_unavailable_reply_missing_placeholder_is_safe(self):
        """模板缺占位符也不炸：replace 而非 format，话术原样输出。"""
        prompts = dict(self.prompts)
        prompts["route_unavailable_reply"] = "路由模型不可用，请稍后重试。"
        out, _ = self.run_turn({"route_llm": failing_route_llm(PRIVACY_ERR)},
                               "那个东西帮我看看", prompts=prompts)
        self.assertEqual(out["reply"], "路由模型不可用，请稍后重试。")
        self.assertIs(out["degraded"], True)

    def test_route_unavailable_reply_fallback_when_key_missing(self):
        """prompts 缺 route_unavailable_reply 键 → 内置兜底常量（与 JSON 同文）。"""
        prompts = {k: v for k, v in self.prompts.items()
                   if k != "route_unavailable_reply"}
        out, _ = self.run_turn({"route_llm": failing_route_llm(PRIVACY_ERR)},
                               "那个东西帮我看看", prompts=prompts)
        self.assertEqual(out["reply"],
                         expected_unavailable_reply(prompts, PRIVACY_ERR))
        self.assertIn("路由模型这会不可用", out["reply"])


if __name__ == "__main__":
    unittest.main()
