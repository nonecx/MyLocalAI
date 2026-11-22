# -*- coding: utf-8 -*-
"""跨层一致性对拍（2026-11-22 角色工作流）：

  一、能力注册表 ↔ capabilities/SKILL.md 头注释（分类/适用角色逐字对拍）；
  二、role_tool_allowlists ↔ 能力注册表（按 skill 分类语义裁剪的对拍）；
  三、执行层 SKILL.md 的服务能力 ↔ 注册表 entry_actions 的闭环。

机器可读分类的唯一来源是 assistant.workflow.registry.CAPABILITIES；能力文档头注释
（capabilities/*/SKILL.md）与角色白名单（assistant_rules.json 的 role_tool_allowlists）
都必须与它对得上——任何一处漂移，本文件直接红。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_workflow_integration -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.workflow import (  # noqa: E402
    CAPABILITIES, HORIZONTAL_TOOLS, entry_action_set,
)
from mylocalai.assistant.router import META_ACTIONS, TOOL_ACTIONS  # noqa: E402

CAPS_DIR = ROOT / "src" / "mylocalai" / "assistant" / "capabilities"
RULES_PATH = ROOT / "src" / "mylocalai" / "assistant" / "assistant_rules.json"

#: 能力目录名 ↔ capability_id 的连字符形式（与 test_role_prompts.EXPECTED_CAPS 同源）
EXPECTED_CAPS = {
    "claim_checking": "claim-checking",
    "evidence_research": "evidence-research",
    "topic_understanding": "topic-understanding",
    "speech_understanding": "speech-understanding",
    "material_analysis": "material-analysis",
    "domain_monitoring": "domain-monitoring",
    "information_collection": "information-collection",
    "authority_verification": "authority-verification",
    "source_credibility": "source-credibility",
    "fact_check_guide": "fact-check-guide",
}


def _load_rules() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _skill_head(cap_id: str) -> str:
    path = CAPS_DIR / cap_id.replace("-", "_") / "SKILL.md"
    return path.read_text(encoding="utf-8").split("-->", 1)[0]


class TestRegistryMatchesSkillDocs(unittest.TestCase):
    """能力注册表 ↔ 人读能力文档头注释（分类与适用角色逐字对拍）。"""

    def test_registry_covers_exactly_ten_capabilities(self):
        self.assertEqual(set(CAPABILITIES), set(EXPECTED_CAPS.values()),
                         "能力注册表与 capabilities/ 目录集合漂移")

    def test_skill_doc_header_matches_registry(self):
        for cap_id, cap in CAPABILITIES.items():
            head = _skill_head(cap_id)
            self.assertIn(f"能力 id：{cap_id}", head,
                          f"{cap_id} 能力文档头 id 与注册表不一致")
            self.assertIn(f"分类：{cap.category}", head,
                          f"{cap_id} 分类漂移（注册表={cap.category}）")
            # 适用角色：通用=全部；专用=主角色（文档写主角色，注册表 roles=主角色+full）
            if cap.category == "通用":
                self.assertIn("适用角色：全部", head, f"{cap_id} 标通用但文档未写全部")
                self.assertEqual(set(cap.roles), {"layperson", "analyst", "monitor", "full"})
            else:
                primary = [r for r in cap.roles if r != "full"]
                self.assertEqual(len(primary), 1, f"{cap_id} 专用能力应有且只有一个主角色")
                self.assertIn(f"适用角色：{primary[0]}", head,
                              f"{cap_id} 适用角色漂移（注册表主角色={primary[0]}）")
                self.assertIn("full", cap.roles, f"{cap_id} 专用能力的 roles 应含 full")


class TestRoleAllowlistsMatchRegistry(unittest.TestCase):
    """role_tool_allowlists ↔ 能力注册表：按 skill 分类语义裁剪的对拍。"""

    def setUp(self):
        self.rules = _load_rules()
        self.allowlists = {k: v for k, v in (self.rules.get("role_tool_allowlists") or {}).items()
                           if not k.startswith("_")}
        self.assertEqual(set(self.allowlists),
                         {"layperson", "analyst", "monitor", "full"},
                         "role_tool_allowlists 必须覆盖四角色")

    def test_semantics_per_role(self):
        """通用能力的入口 action 全角色可用；专用能力只在主角色+full。"""
        for cap_id, cap in CAPABILITIES.items():
            if cap.composite or cap.channel != "对话":
                continue    # 面板复合能力不占对话路由（服务端闸在 web 层）
            for action in cap.entry_actions:
                for role, allowlist in self.allowlists.items():
                    allowed = role in cap.roles
                    self.assertEqual(action in allowlist, allowed,
                                     f"{cap_id}.{action} 在 {role} 的可用性应为 {allowed}")
        # 横向工具（对话跟进机制）全角色可用
        for tool in HORIZONTAL_TOOLS:
            for role, allowlist in self.allowlists.items():
                self.assertIn(tool, allowlist,
                              f"横向工具 {tool} 必须对 {role} 可用")

    def test_every_entry_action_is_routable(self):
        """所有入口 action 都在 TOOL_ACTIONS 里，且至少在一个角色白名单内。"""
        self.assertTrue(entry_action_set() <= set(TOOL_ACTIONS),
                        f"入口 action 越界: {entry_action_set() - set(TOOL_ACTIONS)}")
        union = set().union(*[set(v) for v in self.allowlists.values() if isinstance(v, list)])
        self.assertTrue(entry_action_set() <= union | set(META_ACTIONS),
                        "有入口 action 没进任何角色白名单")

    def test_full_is_superset_and_specialists_equal(self):
        """full 恒全量；analyst==monitor（裁剪语义相同）；layperson=专用+通用+横向。"""
        full = set(self.allowlists["full"])
        self.assertEqual(full, set(self.rules["tool_allowlist"]),
                         "full 必须等于全局白名单（恒全量）")
        self.assertEqual(set(self.allowlists["analyst"]), set(self.allowlists["monitor"]))
        self.assertTrue(full >= set(self.allowlists["analyst"]))
        lay = set(self.allowlists["layperson"])
        self.assertTrue(lay >= set(self.allowlists["analyst"]),
                        "layperson 应包含 analyst 的全部工具（专用核查 + 通用 + 横向）")
        self.assertIn("verify_claim", lay)
        self.assertNotIn("verify_claim", self.allowlists["analyst"])


if __name__ == "__main__":
    unittest.main()
