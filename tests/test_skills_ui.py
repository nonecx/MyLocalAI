# -*- coding: utf-8 -*-
"""设置页“技能”分组静态自洽测试（不跑浏览器）：结构、字段、保存、禁用联动、无注入。"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATIC = ROOT / "src" / "mylocalai" / "web" / "static"
INDEX = STATIC / "index.html"
SETTINGS = STATIC / "settings.js"
CSS = STATIC / "settings.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SkillsUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = _read(INDEX)
        cls.js = _read(SETTINGS)
        cls.css = _read(CSS)

    def test_skills_tab_in_nav(self):
        nav = self.index.split('id="settingsNav"', 1)[1].split("</nav>", 1)[0]
        tabs = re.findall(r'data-settings-tab="([a-z]+)"', nav)
        self.assertEqual(tabs, ["models", "skills", "privacy"])
        self.assertIn("技能", nav)

    def test_skills_api_path_literal(self):
        self.assertIn('skillsPreferences: "/api/skills/preferences"', self.js)

    def test_render_and_save_functions(self):
        for name in ("buildSkillsPane", "skillModeCard", "saveSkills", "loadSkills"):
            self.assertIn(f"function {name}", self.js, f"settings.js 缺少 {name}")
        self.assertIn('state.tab === "skills"', self.js)
        self.assertIn('request("PUT", SETTINGS_API.skillsPreferences', self.js)
        self.assertIn('request("GET", SETTINGS_API.skillsPreferences', self.js)

    def test_form_controls_and_modes(self):
        for marker in ("settingsResearchMode", "settingsAuthorityMode",
                       "settingsAutoVerify", "settingsCompositeEnabled"):
            self.assertIn(marker, self.js, f"settings.js 缺少控件 {marker}")
        self.assertIn('["pipeline"', self.js)
        self.assertIn('["agent"', self.js)
        self.assertIn("固定流水线", self.js)
        self.assertIn("受限 Agent 循环", self.js)

    def test_composite_disables_auto_verify(self):
        self.assertIn("autoBox.disabled = composite.enabled === false", self.js)
        self.assertIn("autoBox.disabled = !compositeBox.checked", self.js)

    def test_no_inner_html_from_user_input(self):
        sinks = re.findall(r"\.(?:innerHTML|outerHTML)\b|insertAdjacentHTML\(|\bdocument\.write\(", self.js)
        self.assertEqual(sinks, [], f"settings.js 出现 HTML 注入点：{sinks}")
        self.assertIn("textContent", self.js)

    def test_privacy_hint_mentions_public_gates(self):
        self.assertIn("public-search", self.js)
        self.assertIn("public-web", self.js)

    def test_css_reuses_tokens(self):
        style = _read(STATIC / "style.css")
        root = style.split(":root {", 1)[1].split("}", 1)[0]
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", root))
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", self.css))
        self.assertTrue(used)
        self.assertEqual(used - declared, set(), f"settings.css 用了未声明令牌：{used - declared}")

    def test_no_es_module_in_settings(self):
        self.assertNotRegex(self.js, r"^\s*(?:import|export)\s", "settings.js 必须是普通 script")


if __name__ == "__main__":
    unittest.main(verbosity=2)
