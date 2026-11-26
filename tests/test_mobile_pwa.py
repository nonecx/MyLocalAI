# -*- coding: utf-8 -*-
"""手机端 / 移动电脑端「快捷入口」（PWA 添加到桌面 + 小屏适配）离线测试。

覆盖五件事（全部离线，不联网、不调大模型）：
  1. manifest.webmanifest 是合法 JSON，字段与 PWA 安装门槛一致
     （name/short_name/start_url/scope/display + 192/512/maskable 三种图标）；
  2. manifest 引用的图标文件真实存在、确实是 PNG（防止只写 JSON 不给图）；
  3. 后端把这两个根路径资源按正确的 Content-Type 送出——
     manifest 不是 application/manifest+json、SW 不是 JS、或 SW 少了
     Service-Worker-Allowed，浏览器会静默拒绝安装/拒绝接管，页面上看不出报错；
  4. index.html / install.js / sw.js / mobile.css 四段接线自洽：
     入口按钮与引导弹层在骨架里、安装逻辑注册 SW、SW 绝不缓存 /api/*、
     小屏覆盖层最后引入且真的覆盖了顶栏与输入区；
  5. 启动脚本支持 --lan（不显式开放局域网时手机连不上，"快捷入口"就是死链）。

运行（项目根目录下）：
    ./.venv/Scripts/python.exe -m unittest tests.test_mobile_pwa -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "mylocalai" / "web"))

STATIC = ROOT / "src" / "mylocalai" / "web" / "static"
ICONS = STATIC / "icons"
MANIFEST = STATIC / "manifest.webmanifest"
SW_JS = STATIC / "sw.js"
INSTALL_JS = STATIC / "install.js"
MOBILE_CSS = STATIC / "mobile.css"
INDEX_HTML = STATIC / "index.html"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------- 1. manifest
class TestManifest(unittest.TestCase):
    """manifest 本身：字段齐不齐、图标在不在。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not MANIFEST.is_file():
            raise unittest.SkipTest(f"缺少 {MANIFEST}")
        cls.manifest = json.loads(_read(MANIFEST))

    def test_required_fields(self):
        """PWA 安装门槛字段：name/short_name/start_url/scope/display/icons。"""
        for key in ("name", "short_name", "start_url", "scope", "display",
                    "background_color", "theme_color", "icons"):
            self.assertIn(key, self.manifest, f"manifest 缺少 {key}")
        self.assertEqual(self.manifest["start_url"], "/",
                         "start_url 必须是绝对根路径，否则从桌面图标启动会落到子路径")
        self.assertEqual(self.manifest["scope"], "/", "scope 必须覆盖整站")
        # 只接受 standalone / fullscreen：minimal-ui 仍带浏览器地址栏，不像"快捷入口"
        self.assertIn(self.manifest["display"], ("standalone", "fullscreen"))

    def test_icons_cover_sizes_and_maskable(self):
        """192 与 512 各一张 + 一张 maskable（Android 自适应裁切需要）。"""
        icons = self.manifest["icons"]
        sizes = {icon.get("sizes") for icon in icons}
        for want in ("192x192", "512x512"):
            self.assertIn(want, sizes, f"manifest 缺 {want} 图标")
        purposes = {icon.get("purpose", "any") for icon in icons}
        self.assertIn("maskable", purposes, "缺 maskable 图标，Android 桌面会显示成白边方块")
        for icon in icons:
            self.assertTrue(str(icon.get("src", "")).startswith("/"),
                            f"图标 src 必须绝对路径：{icon.get('src')}")
            self.assertEqual(icon.get("type"), "image/png")

    def test_icon_files_exist_and_are_png(self):
        """JSON 里点名的每个图标都真有文件，且是真的 PNG（不是空文件/占位文本）。"""
        for icon in self.manifest["icons"]:
            # src 是站点绝对路径（/static/icons/...），web/ 目录才是它的根
            path = STATIC.parent / str(icon["src"]).lstrip("/")
            self.assertTrue(path.is_file(), f"manifest 引用了不存在的图标：{icon['src']}")
            data = path.read_bytes()
            self.assertTrue(data.startswith(PNG_MAGIC), f"不是 PNG：{icon['src']}")
            self.assertGreater(len(data), 500, f"图标太小，疑似占位：{icon['src']}")

    def test_apple_touch_icon_exists(self):
        """iOS 不读 manifest 的图标，必须另有 apple-touch-icon。"""
        path = ICONS / "apple-touch-icon.png"
        self.assertTrue(path.is_file(), "缺少 static/icons/apple-touch-icon.png")
        self.assertTrue(path.read_bytes().startswith(PNG_MAGIC))


# ----------------------------------------------------------------- 2. 后端路由
class TestPwaRoutes(unittest.TestCase):
    """两个根路径资源的 Content-Type / 响应头（浏览器静默拒绝的常见原因）。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            from app import app
        except Exception as exc:  # noqa: BLE001  fastapi 缺失时整组跳过而非报错
            raise unittest.SkipTest(f"无法导入 mylocalai.web.app: {type(exc).__name__}: {exc}")
        cls.client = TestClient(app)

    def test_manifest_served_with_manifest_mime(self):
        resp = self.client.get("/manifest.webmanifest")
        self.assertEqual(resp.status_code, 200)
        ctype = resp.headers.get("content-type", "")
        self.assertIn("application/manifest+json", ctype,
                      f"manifest 的 Content-Type 必须是 application/manifest+json，实际 {ctype}")
        self.assertEqual(json.loads(resp.text)["short_name"], "MyLocalAI")

    def test_service_worker_served_as_js_at_root_scope(self):
        resp = self.client.get("/sw.js")
        self.assertEqual(resp.status_code, 200)
        ctype = resp.headers.get("content-type", "")
        self.assertIn("javascript", ctype, f"sw.js 必须以 JS 类型送出，实际 {ctype}")
        self.assertEqual(resp.headers.get("service-worker-allowed"), "/",
                         "缺少 Service-Worker-Allowed: /（决定 SW 能否接管全站）")
        self.assertIn("no-cache", resp.headers.get("cache-control", ""),
                      "sw.js 必须回源校验，否则浏览器长期跑旧版 SW")

    def test_shell_assets_reachable(self):
        """页面真正引用的新资源都能取到（漏了任何一个，手机上就是白屏/无样式）。"""
        for path in ("/static/mobile.css", "/static/ui-polish.css",
                     "/static/install.js", "/sw.js", "/manifest.webmanifest",
                     "/static/icons/icon-192.png", "/static/icons/apple-touch-icon.png"):
            self.assertEqual(self.client.get(path).status_code, 200, path)


# ------------------------------------------------------------------ 3. 前端接线
class TestPageWiring(unittest.TestCase):
    """index.html / install.js / sw.js / frontend styles 之间的契约。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.polish_css = STATIC / "ui-polish.css"
        for path in (INDEX_HTML, INSTALL_JS, SW_JS, MOBILE_CSS, cls.polish_css):
            if not path.is_file():
                raise unittest.SkipTest(f"缺少 {path}")
        cls.html = _read(INDEX_HTML)
        cls.install = _read(INSTALL_JS)
        cls.sw = _read(SW_JS)
        cls.mobile = _read(MOBILE_CSS)
        cls.polish = _read(cls.polish_css)

    def test_head_declares_pwa(self):
        """head 里的 PWA 声明：manifest / theme-color / iOS 元信息 / 图标。"""
        for marker in ('rel="manifest" href="/manifest.webmanifest"',
                       'name="theme-color"',
                       'name="apple-mobile-web-app-capable"',
                       'rel="apple-touch-icon"'):
            self.assertIn(marker, self.html, f"index.html 缺少 {marker}")
        self.assertIn("viewport-fit=cover", self.html,
                      "缺少 viewport-fit=cover，iPhone 圆角屏下会露出白边")

    def test_install_entry_and_dialog_in_skeleton(self):
        """入口按钮 + 引导弹层骨架（初始 hidden，由 install.js 决定显隐）。"""
        for node_id in ("installBtn", "installDialog", "installBody", "installCloseBtn"):
            self.assertIn(f'id="{node_id}"', self.html, f"index.html 缺少 #{node_id}")
        btn = self.html.split('id="installBtn"', 1)[1].split(">", 1)[0]
        self.assertIn("hidden", btn, "安装按钮初始必须是 hidden（由 JS 判定是否显示）")
        self.assertIn('aria-controls="installDialog"', btn)
        # 脚本顺序：install.js 紧跟 shell.js（外壳之后、业务之前）
        self.assertLess(self.html.index('src="static/shell.js"'),
                        self.html.index('src="static/install.js"'))
        self.assertLess(self.html.index('src="static/install.js"'),
                        self.html.index('src="static/chat.js"'))

    def test_install_js_contract(self):
        """install.js：注册 SW、识别已安装、接 beforeinstallprompt、给手动引导。"""
        self.assertIn('"use strict";', self.install)
        for marker in ('serviceWorker.register("/sw.js"',
                       "beforeinstallprompt",
                       "display-mode: standalone",
                       "appinstalled",
                       "添加到主屏幕",
                       'setAttribute("aria-expanded"'):
            self.assertIn(marker, self.install, f"install.js 缺少 {marker}")
        # 与既有前端一致：普通 script，不用 ES module
        self.assertNotIn("import ", self.install)
        self.assertNotIn("export ", self.install)

    def test_sw_never_caches_api(self):
        """SW 的重点不是"能缓存"，而是"绝不缓存实时数据"。"""
        self.assertIn('"use strict";', self.sw)
        self.assertIn("/api/", self.sw, "sw.js 里看不到 /api/ 的排除逻辑")
        self.assertIn("request.method !== \"GET\"", self.sw, "非 GET 必须直接放行")
        self.assertIn("navigate", self.sw, "缺少导航请求的离线兜底")
        # 允许出现 caches.put，但必须挂在"非 /api"的分支之后——这里做粗粒度校验：
        # 排除判断的函数名必须真的在 fetch 里被调用
        self.assertIn("_isCacheable(request, url)", self.sw)

    def test_mobile_css_overrides(self):
        """小屏覆盖层：手机断点、顶栏与输入区、iOS 16px 门槛、消息流前置。"""
        self.assertIn("@media (max-width: 720px)", self.mobile, "缺手机断点")
        self.assertIn("@media (max-width: 900px)", self.mobile, "缺平板断点")
        self.assertIn(".chat-main-panel { order: -1; }", self.mobile,
                      "单列后必须把对话区排到左栏之前，否则手机首屏看不到对话")
        self.assertIn("font-size: 16px", self.mobile,
                      "输入框必须 16px，否则 iOS 聚焦时会自动放大页面")
        self.assertIn("env(safe-area-inset-bottom", self.mobile,
                      "缺安全区，iPhone 底部按钮会被小横条压住")

    def test_visual_theme_is_referenced_and_cached(self):
        """主题样式可从页面加载，且离线壳会预缓存它。"""
        self.assertIn('href="static/ui-polish.css"', self.html)
        self.assertIn('"/static/ui-polish.css"', self.sw)
        self.assertIn("prefers-reduced-motion: reduce", self.polish)
        self.assertIn(":focus-visible", self.polish)
        # 卷宗纸感主题的三根支柱：墨蓝主色、朱砂强调色、衬线标题字体
        self.assertIn("--primary: #1f3a5f", self.polish)
        self.assertIn("--accent: #b03a2e", self.polish)
        self.assertIn("--font-display: Georgia", self.polish)
        self.assertIn("--paper: #f4efe4", self.polish)

    def test_pwa_theme_color_matches_stylesheet(self):
        """manifest 与 <meta theme-color> 必须与样式表主色同源，避免安装后色块割裂。"""
        self.assertIn('name="theme-color" content="#1f3a5f"', self.html)
        manifest = (STATIC / "manifest.webmanifest").read_text(encoding="utf-8")
        self.assertIn('"theme_color": "#1f3a5f"', manifest)
        self.assertIn('"background_color": "#f4efe4"', manifest)

    def test_apple_touch_icon_is_precached(self):
        """离线壳预缓存 HTML 声明的 iOS 主屏图标。"""
        self.assertIn('rel="apple-touch-icon" href="static/icons/apple-touch-icon.png"', self.html)
        self.assertIn('"/static/icons/apple-touch-icon.png"', self.sw)

    def test_mobile_css_loaded_last(self):
        """手机覆盖层仍最后加载，新主题样式必须在它之前。"""
        idx = self.html.index('href="static/mobile.css"')
        for other in ("style.css", "chat.css", "platform.css",
                      "settings.css", "profiles.css", "ui-polish.css"):
            self.assertGreater(idx, self.html.index(f'href="static/{other}"'),
                               f"mobile.css 必须晚于 {other} 引入")


# --------------------------------------------------------------- 4. 启动脚本
class TestStartScriptsLan(unittest.TestCase):
    """--lan 是"手机真能连上"的前提；默认仍只监听本机（安全默认）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bat = ROOT / "my_website" / "start.bat"
        cls.sh = ROOT / "my_website" / "start.sh"
        for path in (cls.bat, cls.sh):
            if not path.is_file():
                raise unittest.SkipTest(f"缺少 {path}")
        cls.bat_text = _read(cls.bat)
        cls.sh_text = _read(cls.sh)

    def test_bat_supports_lan_flag(self):
        self.assertIn("--lan", self.bat_text, "start.bat 不支持 --lan")
        self.assertIn("0.0.0.0", self.bat_text, "start.bat 没有局域网监听地址")
        self.assertIn("127.0.0.1", self.bat_text, "start.bat 默认仍应是本机地址（安全默认）")
        self.assertIn("--no-browser", self.bat_text, "--no-browser 测试开关不能丢")
        self.assertIn("--host %HOST%", self.bat_text, "uvicorn 启动参数没有用解析出的 HOST")

    def test_sh_supports_lan_flag(self):
        self.assertIn("--lan", self.sh_text, "start.sh 不支持 --lan")
        self.assertIn('HOST="0.0.0.0"', self.sh_text, "start.sh 没有局域网监听地址")
        self.assertIn('--host "$HOST"', self.sh_text, "uvicorn 启动参数没有用解析出的 HOST")
        self.assertIn("--no-browser", self.sh_text, "--no-browser 测试开关不能丢")


if __name__ == "__main__":
    unittest.main(verbosity=2)