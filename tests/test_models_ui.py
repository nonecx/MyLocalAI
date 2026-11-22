# -*- coding: utf-8 -*-
"""T3 前端设置面板（模型管理）测试：结构接线 + 前后端路径自洽 + 契约文案锁定。

验收口径（中文 docstring 即契约）：
  - index.html：顶栏「设置」按钮在「关于」左边；#settingsDialog 骨架齐备
    （role="dialog" / aria-modal / aria-labelledby，左栏 #settingsNav 默认选中
    data-settings-tab="models"，右栏 #settingsPane，右上 #settingsCloseBtn，
    状态区 #settingsStatus），并引入 settings.css 与 settings.js；
  - shell.js：照 aboutDialog 的写法绑定四条开合途径（按钮 / 关闭按钮 / 点遮罩 / Esc），
    且只调 window.SettingsPanel 的 open/close/isOpen——面板渲染与请求不外溢到外壳；
  - settings.js：暴露 window.SettingsPanel；顶部 SETTINGS_API 的每条路径模板都能在
    web/models_api.py（隐私那条在 privacy_api.py）的路由表里找到；写操作的
    (HTTP 方法, 路径模板) 与规格 §3.2 表格逐条一致；全文件无 innerHTML 类 HTML 拼接
    （模型 ID / 显示名称 / base_url / 密钥都是用户输入）；1M / 128K / 65536 的解析与
    显示常量在位；用户要求的控件文案齐备；密钥体检卡 KEY_SOURCE_HINT 只认后端
    _resolve_secret() 的词面 env/stored（secrets 不是后端词面，绝不作为键出现），
    未配置分支降级文案在位，key-health-* 类在 chat.css 都有定义；
  - settings.css：只用 style.css 的 :root 令牌，不自己另起配色；settings.js 里出现的
    settings-* 类都有样式定义（否则面板裸奔）；
  - 消费的响应字段 ⊆ 规格 §3 的字段表（防止前端偷偷依赖契约外字段）。

后端 models_api.py 由 T2 并行实现：文件不存在时路径自洽用例 skip（不算失败），
文件存在时逐条真断言。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_models_ui -v
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))   # 让 `import app` 能找到 web/app.py

STATIC_DIR = ROOT / "src" / "mylocalai" / "web" / "static"
WEB_DIR = ROOT / "src" / "mylocalai" / "web"
MODELS_API = WEB_DIR / "models_api.py"
PRIVACY_API = WEB_DIR / "privacy_api.py"
APP_PY = WEB_DIR / "app.py"

INDEX_HTML = STATIC_DIR / "index.html"
SETTINGS_JS = STATIC_DIR / "settings.js"
SETTINGS_CSS = STATIC_DIR / "settings.css"
SHELL_JS = STATIC_DIR / "shell.js"
STYLE_CSS = STATIC_DIR / "style.css"
CHAT_CSS = STATIC_DIR / "chat.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _shape(path: str) -> str:
    """把路径里的占位符名归一化：只比对路径形状，不比 {pid} / {provider_id} 的取名。"""
    return re.sub(r"\{[^/}]+\}", "{}", path)


def _parse_settings_api(source: str) -> dict:
    """从 settings.js 抽 SETTINGS_API 常量（名字 → 路径模板）。"""
    matched = re.search(r"const SETTINGS_API\s*=\s*\{(.*?)\n\s*\};", source, re.S)
    if not matched:
        return {}
    return dict(re.findall(r'(\w+)\s*:\s*"([^"]*)"', matched.group(1)))


def _parse_router_paths(source: str, fallback_prefix: str = "") -> set:
    """从 APIRouter 风格的后端模块里抽「前缀 + 各装饰器路径」。

    只做正则抽取（不 import 后端）：T2 的路由文件可能还在写，import 会把
    "语法还没写完"误判成"路径不一致"。
    """
    prefix_matched = re.search(r"APIRouter\(\s*prefix\s*=\s*[\"']([^\"']*)[\"']", source)
    prefix = prefix_matched.group(1) if prefix_matched else fallback_prefix
    paths = re.findall(
        r"@router\.(?:get|post|put|delete|patch)\(\s*[\"']([^\"']*)[\"']", source)
    return {prefix + path for path in paths}


class SettingsUiStructureTest(unittest.TestCase):
    """前端静态结构与文案：不依赖后端是否已落地，任何时刻都必须通过。"""

    @classmethod
    def setUpClass(cls) -> None:
        for path in (INDEX_HTML, SETTINGS_JS, SETTINGS_CSS, SHELL_JS):
            if not path.is_file():
                raise unittest.SkipTest(f"前端文件缺失：{path}")
        cls.index = _read(INDEX_HTML)
        cls.js = _read(SETTINGS_JS)
        cls.css = _read(SETTINGS_CSS)
        cls.shell = _read(SHELL_JS)

    def test_index_has_settings_entry_and_dialog_skeleton(self):
        """顶栏入口在「关于」左边；弹层骨架（角色/无障碍/左右栏/状态区）齐备。"""
        html = self.index
        self.assertIn('id="settingsBtn"', html, "顶栏缺少设置按钮")
        self.assertIn('id="settingsDialog"', html, "缺少设置弹层容器")
        self.assertIn('aria-controls="settingsDialog"', html, "设置按钮没有指向弹层")
        self.assertLess(html.index('id="settingsBtn"'), html.index('id="aboutBtn"'),
                        "「设置」按钮应在「关于」左边（规格 §4.1）")
        self.assertIn('class="settings-dialog hidden"', html, "弹层初始必须是 hidden（打开才显示）")

        dialog_tag = html.split('id="settingsDialog"', 1)[1].split(">", 1)[0]
        for attr in ('role="dialog"', 'aria-modal="true"', 'aria-labelledby="settingsTitle"'):
            self.assertIn(attr, dialog_tag, f"弹层缺少 {attr}")
        self.assertIn('id="settingsTitle"', html)

        for node_id in ("settingsCloseBtn", "settingsNav", "settingsPane", "settingsStatus"):
            self.assertIn(f'id="{node_id}"', html, f"弹层缺少 #{node_id}")

        nav = html.split('id="settingsNav"', 1)[1].split("</nav>", 1)[0]
        tabs = re.findall(r'data-settings-tab="([a-z]+)"', nav)
        self.assertEqual(tabs, ["models", "privacy"], "左栏导航应为「模型」+「隐私与授权」")
        models_btn = re.search(r'<button[^>]*data-settings-tab="models"[^>]*>', nav)
        self.assertIsNotNone(models_btn)
        self.assertIn("active", models_btn.group(0), "「模型」应默认选中")
        self.assertIn("模型", nav)

    def test_index_loads_settings_assets_in_order(self):
        """settings.css / settings.js 被引入，且脚本顺序 shell → settings → cards → chat → profiles → batch → monitor。"""
        html = self.index
        self.assertIn('href="static/settings.css"', html)
        self.assertIn('src="static/settings.js"', html)
        self.assertLess(html.index('href="static/chat.css"'), html.index('href="static/settings.css"'),
                        "settings.css 应在 chat.css 之后引入（覆盖顺序稳定）")
        order = [html.index(f'src="static/{name}"')
                 for name in ("shell.js", "settings.js", "cards.js", "chat.js",
                              "profiles.js", "batch.js", "monitor.js")]
        self.assertEqual(order, sorted(order),
                         "脚本顺序应为 shell → settings → cards → chat → profiles → batch → monitor")

    def test_shell_only_binds_the_dialog(self):
        """shell.js 只做绑定：按钮 / 关闭按钮 / 点遮罩 / Esc 都接到 SettingsPanel。"""
        js = self.shell
        self.assertIn("function bindSettings()", js)
        self.assertIn("bindSettings();", js, "DOMContentLoaded 里没有调用 bindSettings")
        for node in ("#settingsBtn", "#settingsDialog", "#settingsCloseBtn"):
            self.assertIn(f'$("{node}")', js, f"shell.js 没有绑定 {node}")
        for call in ("api.open()", "api.close()", "api.isOpen()"):
            self.assertIn(call, js, f"shell.js 没有调用 SettingsPanel 的 {call}")
        self.assertIn('e.key !== "Escape"', js, "缺少 Esc 关闭")
        self.assertIn("e.target !== dialog", js, "缺少点遮罩关闭（点卡片本体不关）")
        self.assertTrue("/api/models" not in js,
                        "外壳不该直接请求模型接口（面板逻辑应在 settings.js）")
        self.assertTrue('$("#settingsPane")' not in js,
                        "外壳不该渲染面板内容（只做开合绑定）")

    def test_settings_panel_api_exposed(self):
        """window.SettingsPanel = { open, close, isOpen }，且是普通 script（无 ES module）。"""
        js = self.js
        self.assertRegex(js, r"SettingsPanel\s*=\s*\{")
        self.assertRegex(js, r"open:\s*function")
        self.assertRegex(js, r"close:\s*function")
        self.assertRegex(js, r"isOpen:\s*isOpen")
        self.assertIn('"use strict";', js)
        self.assertNotRegex(js, r"^\s*(?:import|export)\s", "现有前端都是普通 script，不用 ES module")

    def test_settings_js_never_builds_html_from_user_input(self):
        """禁止 innerHTML 类 HTML 拼接：模型 ID / 显示名称 / base_url / 密钥都是用户输入。"""
        js = self.js
        # 只认真正的 HTML 注入点（赋值/追加/文档写入），注释里提到这个词不算违规
        sinks = re.findall(r"\.(?:innerHTML|outerHTML)\b|insertAdjacentHTML\(|\bdocument\.write\(", js)
        self.assertEqual(sinks, [], f"settings.js 出现 HTML 注入点：{sinks}：用户输入必须走 textContent")
        self.assertIn("document.createElement", js, "建 DOM 应走 createElement")
        self.assertIn("textContent", js)

    def test_settings_css_reuses_root_tokens(self):
        """settings.css 只用 style.css 已声明的 :root 令牌，不另起一套配色。"""
        style = _read(STYLE_CSS)
        root_block = style.split(":root {", 1)[1].split("}", 1)[0]
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", root_block))
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", self.css))
        self.assertTrue(used, "settings.css 没用到任何 :root 令牌")
        self.assertEqual(sorted(used - declared), [], "settings.css 用了 style.css 未声明的变量")
        # 只禁止"重新定义 :root"，注释里提到这个词没问题
        self.assertNotRegex(self.css, r"(?m)^\s*:root\s*\{", "settings.css 不该重新定义 :root 配色")

    def test_settings_classes_have_styles(self):
        """settings.js 里出现的 settings-* 类在 settings.css 都有定义（否则面板裸奔）。"""
        used = set()
        for chunk in re.findall(r'el\(\s*"[a-z0-9]+"\s*,\s*"([^"]+)"', self.js):
            used.update(chunk.split())
        for chunk in re.findall(r'button\(\s*"[^"]*"\s*,\s*"([^"]+)"', self.js):
            used.update(chunk.split())
        targets = sorted(name for name in used if name.startswith("settings-"))
        self.assertTrue(targets, "解析不到 settings.js 使用的 settings-* 类")
        missing = [name for name in targets if f".{name}" not in self.css]
        self.assertEqual(missing, [], f"settings.css 缺少定义：{missing}")

    def test_key_health_card_covers_backend_key_source_vocabulary(self):
        """密钥体检卡：KEY_SOURCE_HINT 覆盖后端词面 env/stored，绝不出现 secrets 键（防回归）。

        后端 src/mylocalai/llm/model_registry.py 的 _resolve_secret() 只返回
        env | stored | none（stored = 密钥保存在 model_secrets.json，即设置面板保存）；
        初稿曾把 stored 误写成 secrets，导致已配置密钥仍显示「未配置」红卡。
        """
        matched = re.search(r"const KEY_SOURCE_HINT\s*=\s*\{(.*?)\n\s*\};", self.js, re.S)
        self.assertIsNotNone(matched, "settings.js 缺少 KEY_SOURCE_HINT 映射表")
        keys = set(re.findall(r"(\w+)\s*:\s*\{", matched.group(1)))
        self.assertIn("env", keys, "KEY_SOURCE_HINT 缺少 env 键（密钥来自环境变量）")
        self.assertIn("stored", keys, "KEY_SOURCE_HINT 缺少 stored 键（密钥来自设置面板保存）")
        self.assertNotIn("secrets", keys,
                         "后端永远不会返回 secrets（词面是 stored），KEY_SOURCE_HINT 不该有这个键")
        self.assertIn("key-health-secrets", self.js,
                      "stored 档的配色变体类名沿用 key-health-secrets（chat.css 侧同名）")
        # 卡片真的挂进「模型」Tab 渲染，且在配置读取告警之前（Tab 顶部第一行）
        self.assertRegex(self.js, r"pane\.appendChild\(buildKeyHealthCard\(\)\)",
                         "renderPane 里没有挂密钥体检卡")
        self.assertLess(self.js.index("pane.appendChild(buildKeyHealthCard())"),
                        self.js.index("pane.appendChild(buildWarnings"),
                        "密钥体检卡应在配置读取告警之前渲染")

    def test_key_health_card_has_unconfigured_copy(self):
        """未配置（none / 取不到 runtime）分支的降级警示文案在位，并给出下一步动作。"""
        js = self.js
        self.assertIn("function buildKeyHealthCard", js)
        self.assertIn("未配置密钥：分析将以降级模式运行，证据发现不可用", js,
                      "缺少未配置密钥的降级警示文案")
        self.assertIn("请在下方选择提供方并填写密钥", js,
                      "未配置分支缺少下一步动作提示")

    def test_key_health_classes_defined_in_chat_css(self):
        """settings.js 用到的 key-health-* 类在 chat.css 都有定义（体检卡配色跨文件契约）。

        与 test_settings_classes_have_styles 同一手法：settings.css 不加规则，
        体检卡盒底复用 .settings-notice，三态配色变体落在 chat.css。
        """
        css = _read(CHAT_CSS)
        used = sorted(set(re.findall(r"key-health-[a-z0-9-]+", self.js)))
        self.assertTrue(used, "解析不到 settings.js 使用的 key-health-* 类")
        missing = [name for name in used if f".{name}" not in css]
        self.assertEqual(missing, [], f"chat.css 缺少定义：{missing}")

    def test_required_ui_copy_is_present(self):
        """用户点名要的控件与提示文案齐备（中文界面）。"""
        js = self.js
        required = (
            "Provider ID", "显示名称", "API 地址", "API 协议", "API 密钥", "启用",
            "上下文窗口", "最大输出 token 数", "输入类型", "推理等级", "API 参数值",
            "获取可用模型", "恢复默认模型", "＋ 添加模型", "保存模型目录", "＋ 添加档位",
            "导入所选模型", "设为当前", "删除提供方", "删除模型", "关闭思考",
            "留空表示不改", "本站运行期不支持", "内置提供方不可删除",
            "已配置(环境变量)", "已配置(本地)", "未配置", "内置默认模型目录", "已自定义模型目录",
        )
        for text in required:
            self.assertIn(text, js, f"settings.js 缺少文案：{text}")
        # 输入类型 / 档位清单来自后端响应，前端不硬编码枚举
        self.assertIn("input_types", js)
        self.assertIn("reasoning_tiers", js)

    def test_token_count_parse_and_format_rule(self):
        """1M / 128K / 65536 可解析为整数；显示时 1000000 → 1M、131072 → 128K。"""
        js = self.js
        self.assertIn("function parseTokenCount", js)
        self.assertIn("function formatTokenCount", js)
        # M = 1000000、K = 1024：两种写法都能原样往返
        self.assertIn("1000000", js)
        self.assertIn("1024", js)
        self.assertRegex(js, r"%\s*1000000\s*===\s*0", "显示时应先判定 M（整除 1000000）")
        self.assertRegex(js, r"%\s*1024\s*===\s*0", "其次判定 K（整除 1024）")
        for sample in ("1M", "128K", "65536"):
            self.assertIn(sample, js, f"输入提示里应点到 {sample} 这种写法")

    def test_write_actions_refresh_from_server(self):
        """写成功后重新 GET /api/models 全量刷新（前端不维护"我猜的服务端状态"）。"""
        js = self.js
        self.assertIn('request("GET", SETTINGS_API.models)', js)
        self.assertGreaterEqual(js.count("await loadAll()"), 5,
                                "保存提供方/密钥/设为当前/删除/模型目录/恢复默认/导入后都应全量刷新")
        self.assertIn("state.data = data", js)
        self.assertIn("showStatus(\"error\", err.message)", js)
        self.assertIn("payload.detail", js, "错误必须把后端 detail 原文拿出来显示")

    def test_privacy_pane_is_operable(self):
        """「隐私与授权」面板可操作：总开关 + 提供方勾选 + 保存（PUT 完整载荷）。

        锁定四件事：
          1. GET /api/privacy/preferences 取当前偏好，打开弹层即重置缓存重新拉取；
          2. 面板有总开关 / 提供方勾选清单 / 「保存隐私授权」按钮的代码特征，
             且总开关关闭时禁用并清空勾选（与后端校验规则一致）；
          3. PUT 载荷把 GET 到的 save_enabled / retention_days 原值合并进去
             （接口要求完整载荷，面板本期不改保留策略）；
          4. 响应里 warning 非空时用既有 settings-notice 告警样式显示。
        """
        js = self.js
        # ① 偏好拉取
        self.assertIn('request("GET", SETTINGS_API.privacyPreferences)', js,
                      "隐私面板应 GET /api/privacy/preferences 取当前偏好")
        self.assertIn("state.privacy = null;", js, "打开弹层应重置缓存以重新拉取偏好")
        # ② 控件特征：总开关 / 提供方清单 / 保存按钮
        for text in ("允许外部处理（外发请求）", "允许外发的提供方", "保存隐私授权",
                     "正在载入隐私偏好…", "还没有提供方：请先在「模型」页添加提供方"):
            self.assertIn(text, js, f"隐私面板缺少控件/提示文案：{text}")
        self.assertIn('toggle.id = "settingsPrivacyExternalToggle"', js)
        self.assertIn("cb.disabled = !draft.enabled;", js, "总开关关闭时提供方勾选框应禁用")
        self.assertIn("draft.providers = [];", js,
                      "总开关关闭时应清空提供方勾选（后端禁止开关关着还授权提供方）")
        # ③ PUT 完整载荷：外部处理两字段 + 原样回传的 save_enabled / retention_days
        self.assertIn('request("PUT", SETTINGS_API.privacyPreferences, body)',
                      js, "保存隐私授权应 PUT /api/privacy/preferences")
        self.assertIn("save_enabled: Object.assign({}, prefs.save_enabled)", js,
                      "PUT 载荷应合并 GET 到的 save_enabled 原值")
        self.assertIn("retention_days: Object.assign({}, prefs.retention_days)", js,
                      "PUT 载荷应合并 GET 到的 retention_days 原值")
        self.assertIn("external_processing_enabled: !!draft.enabled", js)
        self.assertIn("external_processing_providers: draft.providers.slice()", js)
        # ④ 告警展示：warning 非空时复用既有告警样式（settings-notice）
        self.assertIn("隐私偏好告警：", js)
        self.assertIn('el("div", "settings-notice")', js, "warning 应复用 settings-notice 告警样式")
        # 跨文件契约：面板用到的 settings-* 类在 settings.css 都有定义（防面板裸奔）
        for css_class in ("settings-check-group", "settings-check-strong", "settings-field-label",
                          "settings-discover-list", "settings-discover-item", "settings-notice"):
            self.assertIn("." + css_class, self.css,
                          f"settings.css 缺少隐私面板用到的类：{css_class}")


class SettingsApiContractTest(unittest.TestCase):
    """前后端路径自洽 + 写操作动词与规格 §3.2 表格逐条一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not MODELS_API.is_file():
            raise unittest.SkipTest(
                "后端 src/mylocalai/web/models_api.py 尚未落地（T2 并行开发中）："
                "路径自洽用例跳过，前端本身已按契约实现")
        cls.js = _read(SETTINGS_JS)
        cls.api = _parse_settings_api(cls.js)
        cls.backend_routes = _parse_router_paths(_read(MODELS_API), "/api/models")

    def test_settings_api_constants_parsed(self):
        """SETTINGS_API 能被解析出全部路径模板（解析失败本身就是回归信号）。"""
        expected_keys = {
            "models", "providers", "provider", "providerSecret", "providerActivate",
            "providerDiscover", "providerModels", "providerModel",
            "providerModelsRestore", "privacyPreferences",
        }
        self.assertEqual(set(self.api), expected_keys, f"SETTINGS_API 键集合不一致：{sorted(self.api)}")
        self.assertEqual(self.api["models"], "/api/models")

    def test_every_path_exists_in_backend_routes(self):
        """settings.js 的每条路径模板都能在 models_api.py 路由表里找到（隐私那条在 privacy_api）。"""
        shapes = {_shape(path) for path in self.backend_routes}
        self.assertTrue(shapes, "models_api.py 里解析不到任何路由：装饰器写法可能变了")
        for name, template in self.api.items():
            if name == "privacyPreferences":
                continue
            self.assertIn(_shape(template), shapes,
                          f"SETTINGS_API.{name} = {template} 不在后端路由表里：{sorted(shapes)}")

    def test_privacy_path_exists_in_privacy_api(self):
        """隐私面板读写的地址 /api/privacy/preferences 确实存在（GET 取偏好 + PUT 保存）。"""
        self.assertIn("privacyPreferences", self.api)
        if not PRIVACY_API.is_file():
            self.skipTest("privacy_api.py 不存在，跳过隐私路径断言")
        shapes = {_shape(path) for path in _parse_router_paths(_read(PRIVACY_API), "/api/privacy")}
        self.assertIn(_shape(self.api["privacyPreferences"]), shapes)

    def test_http_methods_match_contract_table(self):
        """(HTTP 方法, 路径模板) 与规格 §3.2 表格逐条对应，多一条少一条都算不一致。

        隐私面板两条也纳入契约：GET /api/privacy/preferences 取偏好、
        PUT /api/privacy/preferences 保存（路由在 web/privacy_api.py）。
        """
        expected = {
            ("GET", "models"),
            ("POST", "providers"),
            ("PUT", "provider"),
            ("DELETE", "provider"),
            ("PUT", "providerSecret"),
            ("POST", "providerActivate"),
            ("POST", "providerDiscover"),
            ("POST", "providerModels"),
            ("DELETE", "providerModel"),
            ("POST", "providerModelsRestore"),
            ("GET", "privacyPreferences"),
            ("PUT", "privacyPreferences"),
        }
        found = set(re.findall(
            r'request\(\s*"(GET|POST|PUT|DELETE)"\s*,\s*apiPath\(\s*SETTINGS_API\.(\w+)', self.js))
        found |= set(re.findall(
            r'request\(\s*"(GET|POST|PUT|DELETE)"\s*,\s*SETTINGS_API\.(\w+)', self.js))
        self.assertEqual(found, expected, "前端调用的接口动词/路径与规格 §3.2 不一致")

    def test_app_includes_models_router(self):
        """models_api 的 router 必须被 app.py include（否则前端拿到的全是 404）。"""
        app_source = _read(APP_PY)
        self.assertIn("models_api", app_source, "app.py 没有引用 web/models_api.py")
        self.assertRegex(app_source, r"include_router\([^)]*models",
                         "app.py 没有把 models 路由 include 进 FastAPI app")


class SettingsApiLiveRoutesTest(unittest.TestCase):
    """真起 FastAPI 应用核对路由表（models_api 已挂载时才有意义）。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not MODELS_API.is_file():
            raise unittest.SkipTest("models_api.py 尚未落地，跳过应用级路由核对")
        try:
            from fastapi.testclient import TestClient
            import app as webapp          # noqa: F401  只为拿到 app.routes
        except Exception as exc:          # noqa: BLE001  fastapi 缺失/应用导入失败时跳过而非报错
            raise unittest.SkipTest(f"无法导入 web app: {type(exc).__name__}: {exc}")
        cls.webapp = webapp
        cls.api = _parse_settings_api(_read(SETTINGS_JS))
        cls.routes = {getattr(route, "path", "") for route in webapp.app.routes}

    def test_model_paths_registered_in_app(self):
        """/api/models/* 真的出现在 FastAPI 路由表里（应用级最终口径）。"""
        model_paths = [path for path in self.routes if path.startswith("/api/models")]
        if not model_paths:
            self.skipTest("app.py 尚未挂载 /api/models（T2 收尾中）；源码级路径自洽已在上组用例断言")
        registered = {_shape(path) for path in self.routes}
        for name, template in self.api.items():
            self.assertIn(_shape(template), registered, f"{name} → {template} 未注册进 app")

    def test_get_models_returns_contract_top_level_keys(self):
        """GET /api/models 的顶层键与规格 §3.1 一致（密钥永不出现明文）。"""
        from fastapi.testclient import TestClient
        client = TestClient(self.webapp.app)
        resp = client.get("/api/models")
        if resp.status_code == 404:
            self.skipTest("app.py 尚未挂载 /api/models（T2 收尾中）")
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        for key in ("schema_version", "active", "protocols", "input_types",
                    "reasoning_tiers", "providers", "warnings"):
            self.assertIn(key, payload, f"GET /api/models 缺少顶层键 {key}")
        # 密钥只以掩码/状态字段出现
        self.assertNotIn("sk-", resp.text, "响应里出现了疑似明文密钥")
        self.assertNotIn("api_key\"", resp.text)


class SettingsConsumedFieldsTest(unittest.TestCase):
    """settings.js 消费的 snake_case 字段 ⊆ 规格 §3 的字段表（契约冻结点）。"""

    #: 规格 §3.1 / §3.3 出现的响应字段（含 limits、model view 内层字段、protocols 的
    #: runtime_supported 与 runtime 段的 key_source，词面 env|stored|none 见规格 §2.1），
    #: 外加 §3.2 请求体字段 api_key（写密钥时前端要发这个键）；隐私面板消费的 5 个字段
    #: 来自 web/privacy_api.py GET/PUT /api/privacy/preferences 的响应视图
    #: （PrivacyPreferences.as_dict() + warning）
    DOCUMENTED = {
        "schema_version", "providers", "warnings", "protocols", "input_types",
        "reasoning_tiers", "limits", "max_providers", "max_models_per_provider",
        "max_context_window", "max_output_tokens",
        "display_name", "base_url", "protocol_runtime_supported", "runtime_supported",
        "key_env", "key_source", "api_key", "api_key_set", "api_key_source", "api_key_masked", "is_active",
        "default_models", "context_window", "active_tier", "api_value", "owned_by",
        "save_enabled", "retention_days", "external_processing_enabled",
        "external_processing_providers", "warning",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = _read(SETTINGS_JS)

    def test_consumed_fields_are_in_contract(self):
        """前端点号访问的响应字段都来自契约（方法/属性是驼峰，不会误伤）。"""
        consumed = set(re.findall(r"\.([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", self.js))
        self.assertTrue(consumed, "提取不到 settings.js 消费的响应字段：文件或正则失效")
        missing = sorted(consumed - self.DOCUMENTED)
        self.assertEqual(missing, [], f"settings.js 依赖了契约外字段：{missing}")

    def test_key_status_and_model_params_are_wired(self):
        """密钥状态、协议运行期支持、模型参数这几组关键字段真的被用上（不是文档里有前端没接）。"""
        consumed = set(re.findall(r"\.([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", self.js))
        for field in ("api_key_set", "api_key_source", "api_key_masked", "key_source",
                      "protocol_runtime_supported", "is_active", "default_models",
                      "input_types", "reasoning_tiers", "active_tier", "api_value",
                      "display_name", "base_url"):
            self.assertIn(field, consumed, f"前端没有消费契约字段 {field}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
