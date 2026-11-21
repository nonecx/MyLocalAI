# -*- coding: utf-8 -*-
"""信源可信度速查测试（skills/source/pipeline + tools_source）：mock 取页，全离线。

分组：
   1. 输入解析与规则（4）：URL/裸域名/机构名/无法识别四种解析、needs_clarification、
      load_rules 覆盖项夹取与坏路径兜底；
   2. 判定口径（6）：目录锚点→已核实 official、官方后缀单点→倾向未核验、
      正文官方标记补足→已核实、交叉提及≥2→已核实、页面不可达→unreachable 降级、
      正文为空降级；
   3. 历史战绩（3）：注入且查到→逐字透传、注入但无记录→"无记录"、查询失败→
      limits 说明且不影响身份判定；
   4. 纪律锁定（4）：不建档（模块零 dossier 依赖 + 工具返回 dossier_id=None）、
      只读 1 页（页数预算=1）、无检索注入位、零真假判定词；
   5. 预算/取消/进度（4）：死线已过→budget_exhausted 不取页、取消→cancelled、
      on_progress 四步中文阶段、data 键名锁定；
   6. 受约束补充说明（2）：合规 note 进 summary、越界（判定词/升级表述/URL）丢弃；
   7. 工具层（4）：注册元数据、参数校验、死线透传、run 异常降级不崩、digest 转述。

全离线：fetch_fn / llm_fn / history_fn / clock 全部注入假件，零网络零 LLM 零档案。
"""

from __future__ import annotations

import inspect
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import tools_source  # noqa: E402
from mylocalai.assistant.registry import ToolRegistry, ValidationError  # noqa: E402
from mylocalai.assistant.skills.source import SourceSkills, check_source  # noqa: E402
from mylocalai.assistant.skills.source import pipeline as S  # noqa: E402
from mylocalai.llm import llm_cache  # noqa: E402

CLOCK = "2026-11-21T00:00:00+00:00"

NHC_URL = "https://www.nhc.gov.cn/notice.html"
FOO_URL = "https://foo.gov.cn/a.html"          # 目录外官方后缀域名（宽匹配算官方档）
BAR_URL = "https://bar.gov.cn/z.html"          # 用于交叉提及场景
UNKNOWN_URL = "https://missing.example.org/x"  # 目录外、非官方后缀

#: 固定真假判定词（铁律 1）：任何出口一个都不许出现
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")


def _page(url: str, title: str, text: str) -> Dict[str, Any]:
    """transport.fetch 成功形状的最小替身。"""
    host = (urlsplit(url).hostname or "").lower()
    return {"status": "ok", "url": url, "final_url": url, "domain": host,
            "title": title, "text": text, "bytes": len(text.encode("utf-8")),
            "content_type": "text/html", "hops": [], "fetched_at": CLOCK}


class _Env:
    """一次速查的假环境：取页/战绩 stub，记录调用，全离线。"""

    def __init__(self, *, pages: Optional[Dict[str, Dict[str, str]]] = None,
                 fail_urls: Any = (), empty_urls: Any = (),
                 history: Any = "unset", fetch_raises: Any = None) -> None:
        self.pages = pages or {}
        self.fail_urls = set(fail_urls)
        self.empty_urls = set(empty_urls)
        self.history = history          # "unset"=不注入；None=注入但查无记录
        self.fetch_raises = fetch_raises
        self.fetches: list = []
        self.history_queries: list = []

    def fetch(self, url: str, *, allowed_domains=None, cancel_check=None,
              deadline=None) -> Dict[str, Any]:
        self.fetches.append({"url": url, "allowed_domains": allowed_domains})
        if self.fetch_raises is not None:
            raise self.fetch_raises
        if url in self.fail_urls:
            return {"status": "error", "category": "network", "detail": "404 Not Found",
                    "url": url, "final_url": ""}
        row = self.pages.get(url)
        if row is None:
            return {"status": "error", "category": "network", "detail": "404 Not Found",
                    "url": url, "final_url": ""}
        if url in self.empty_urls:
            return _page(url, row.get("title", ""), "")
        return _page(url, row.get("title", ""), row.get("text", ""))

    def history_fn(self, key: str) -> Any:
        self.history_queries.append(key)
        return None if self.history == "unset" else self.history

    def skills(self, **kw) -> SourceSkills:
        if self.history != "unset":
            kw.setdefault("history_fn", self.history_fn)
        return SourceSkills(fetch_fn=self.fetch, clock=lambda: CLOCK, **kw)


def _verified_pages(*hosts: str) -> list:
    """两页"已核验页面"替身：正文里提到指定主机（触发 cross_mention）。"""
    out = []
    for index, host in enumerate(hosts):
        out.append({"url": f"https://peer{index}.gov.cn/p{index}.html",
                    "title": f"同行通报{index}",
                    "text": f"据 {host} 发布的信息……（第 {index} 方已核验页面）"})
    return out


# ===========================================================================
# 1. 输入解析与规则（4）
# ===========================================================================
class TestQueryResolution(unittest.TestCase):
    def test_url_input_resolves_and_fetches_itself(self):
        env = _Env(pages={NHC_URL: {"title": "通报", "text": "某市通报：情况说明。"}})
        result = env.skills().run(NHC_URL)
        self.assertEqual(result["data"]["resolved"]["kind"], "url")
        self.assertEqual(result["data"]["resolved"]["host"], "nhc.gov.cn")
        self.assertEqual(len(env.fetches), 1)
        # 域名允许集锁定为信源自己的域：跳站一律拒绝
        self.assertEqual(env.fetches[0]["allowed_domains"], ["nhc.gov.cn"])

    def test_bare_domain_input_gets_https_homepage(self):
        env = _Env(pages={"https://nhc.gov.cn/": {"title": "首页", "text": "欢迎访问。"}})
        result = env.skills().run("nhc.gov.cn")
        self.assertEqual(result["data"]["resolved"]["kind"], "domain")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(env.fetches[0]["url"], "https://nhc.gov.cn/")

    def test_institution_name_hits_catalog(self):
        env = _Env(pages={"https://nhc.gov.cn/": {"title": "首页", "text": "欢迎访问。"}})
        result = env.skills().run("国家卫生健康委员会")
        self.assertEqual(result["data"]["resolved"]["kind"], "name")
        self.assertTrue(result["data"]["resolved"]["catalog_anchor"])
        self.assertEqual(result["data"]["resolved"]["host"], "nhc.gov.cn")

    def test_unresolvable_input_needs_clarification_without_fetch(self):
        env = _Env()
        result = env.skills().run("某某神秘机构协会")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["task_status"], "needs_clarification")
        self.assertEqual(result["data"]["state"], "unclear")
        self.assertEqual(env.fetches, [])          # 没有可读对象，绝不盲抓
        self.assertEqual(result["data"]["authority_category"], "unverified")


class TestRules(unittest.TestCase):
    def test_override_clamped_to_safe_range(self):
        rules = S.load_rules({"max_pages": 99, "total_deadline_s": 99999,
                              "max_input_chars": "bad"})
        self.assertEqual(rules["max_pages"], 4)          # 夹到安全区间上界
        self.assertEqual(rules["total_deadline_s"], 3600.0)
        self.assertEqual(rules["max_input_chars"], 500)  # 坏类型回退默认

    def test_missing_file_falls_back_to_defaults(self):
        rules = S.load_rules(Path("___no_such_rules__.json"))
        self.assertEqual(rules, dict(S.DEFAULT_SOURCE_RULES))

    def test_package_rules_json_matches_defaults(self):
        import json
        raw = json.loads((Path(S._HERE) / "rules.json").read_text(encoding="utf-8"))
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            self.assertEqual(S.DEFAULT_SOURCE_RULES[key], value, key)


# ===========================================================================
# 2. 判定口径（6）
# ===========================================================================
class TestVerdictRules(unittest.TestCase):
    def test_catalog_anchor_is_verified_official(self):
        """目录锚点（anchor=true 人工登记）→ 已核实 official，不依赖页面信号。"""
        env = _Env(pages={NHC_URL: {"title": "通报", "text": "某市通报：情况说明。"}})
        result = env.skills().run(NHC_URL)
        data = result["data"]
        self.assertEqual(data["state"], "ok")
        self.assertEqual(data["authority_category"], "official")
        self.assertTrue(data["verified"])
        self.assertIn("目录锚点", data["basis"])
        self.assertIn("已核实", data["summary"])
        self.assertEqual(result["status"], "completed")

    def test_official_suffix_only_is_tendency_not_verified(self):
        """官方后缀单点信号 → authority_category=official（档位宽匹配），但不判已核实。"""
        env = _Env(pages={FOO_URL: {"title": "某局", "text": "欢迎访问我局网站。"}})
        result = env.skills().run(FOO_URL)
        data = result["data"]
        self.assertEqual(data["signals"], ["official_suffix"])
        self.assertEqual(data["authority_category"], "official")   # 档位倾向
        self.assertFalse(data["verified"])                          # 单点信号不足
        self.assertIn("倾向", data["summary"])
        self.assertIn("未核验", data["summary"])
        # 自证信号（后缀/标记）要作决定性证据还需交叉佐证，limit 如实写明
        self.assertTrue(any("交叉佐证" in x for x in data["limits"]))

    def test_official_marker_completes_dynamic_verification(self):
        """正文官方标记 + 官方后缀 = 2 个代码信号 → 动态核验达标，已核实 official。"""
        env = _Env(pages={FOO_URL: {"title": "某局",
                                    "text": "主办单位：某市人民政府。政府网站标识码 X"}})
        result = env.skills().run(FOO_URL)
        data = result["data"]
        self.assertEqual(sorted(data["signals"]),
                         ["official_marker", "official_suffix"])
        self.assertEqual(data["authority_category"], "official")
        self.assertTrue(data["verified"])
        self.assertIn("已核实", data["summary"])

    def test_cross_mention_by_two_verified_pages_is_verified(self):
        """官方后缀 + ≥2 个已核验页面交叉提及 = 2 个代码信号 → 已核实。"""
        env = _Env(pages={BAR_URL: {"title": "某局", "text": "欢迎访问。"}})
        result = env.skills(verified_pages=_verified_pages("bar.gov.cn", "bar.gov.cn")).run(BAR_URL)
        data = result["data"]
        self.assertIn("cross_mention", data["signals"])
        self.assertIn("official_suffix", data["signals"])
        self.assertTrue(data["verified"])
        self.assertEqual(data["authority_category"], "official")

    def test_unreachable_page_degrades_honestly(self):
        """页面不可达 → state=unreachable、status=partial、类别退回目录/档位口径。"""
        env = _Env(pages={UNKNOWN_URL: {"title": "x", "text": "y"}},
                   fail_urls={UNKNOWN_URL})
        result = env.skills().run(UNKNOWN_URL)
        data = result["data"]
        self.assertEqual(data["state"], "unreachable")
        self.assertEqual(result["status"], "partial")
        self.assertFalse(data["verified"])     # 没读到页面，绝不说"已核实"
        self.assertEqual(data["signals"], [])
        self.assertTrue(any("页面未读取" in x for x in data["limits"]))
        self.assertTrue(any(e["category"] == "network" for e in result["errors"]))
        self.assertEqual(len(env.fetches), 1)

    def test_empty_page_text_counts_as_unreachable(self):
        """读取成功但正文为空 = 没拿到可用正文，同样 unreachable 降级。"""
        env = _Env(pages={FOO_URL: {"title": "某局", "text": ""}}, empty_urls={FOO_URL})
        result = env.skills().run(FOO_URL)
        self.assertEqual(result["data"]["state"], "unreachable")
        self.assertTrue(any(e["category"] == "empty_page_text" for e in result["errors"]))


# ===========================================================================
# 3. 历史战绩（3）
# ===========================================================================
class TestHistory(unittest.TestCase):
    RECORD = {"checks": 3, "last_result": "曾转发不实信息", "flags": ["A", "B"]}

    def test_history_record_passthrough_when_found(self):
        env = _Env(pages={NHC_URL: {"title": "通报", "text": "说明。"}}, history=self.RECORD)
        result = env.skills().run(NHC_URL)
        self.assertEqual(result["data"]["history"], self.RECORD)
        self.assertEqual(env.history_queries, ["nhc.gov.cn"])   # 按解析出的主机查询
        self.assertIn("有记录", result["data"]["summary"])

    def test_no_history_record_says_none(self):
        """注入了战绩查询但查无记录 → history=None，摘要写"无记录"。"""
        env = _Env(pages={NHC_URL: {"title": "通报", "text": "说明。"}}, history=None)
        result = env.skills().run(NHC_URL)
        self.assertIsNone(result["data"]["history"])
        self.assertIn("无记录", result["data"]["summary"])
        # 不注入战绩查询 → 同样"无记录"
        env2 = _Env(pages={NHC_URL: {"title": "通报", "text": "说明。"}})
        result2 = env2.skills().run(NHC_URL)
        self.assertIsNone(result2["data"]["history"])
        self.assertIn("无记录", result2["data"]["summary"])

    def test_history_fn_failure_does_not_break_lookup(self):
        def _boom(key):
            raise RuntimeError("dossier 后端挂了")
        env = _Env(pages={NHC_URL: {"title": "通报", "text": "说明。"}})
        result = env.skills(history_fn=_boom).run(NHC_URL)
        self.assertIsNone(result["data"]["history"])
        self.assertEqual(result["data"]["state"], "ok")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any("历史战绩查询失败" in x for x in result["data"]["limits"]))


# ===========================================================================
# 4. 纪律锁定（4）
# ===========================================================================
class TestDiscipline(unittest.TestCase):
    def test_no_dossier_dependency_and_read_only_history(self):
        """不建档：模块没有任何 dossier/写库 import；构造参数里没有"写"接口。"""
        source = inspect.getsource(S)
        # 只盯 import 语句（docstring/注释里提到"dossier 战绩可注入"不算依赖）
        import_lines = [ln.strip() for ln in source.splitlines()
                        if ln.strip().startswith(("import ", "from "))]
        self.assertTrue(import_lines)                      # 前提：确实有 import 可查
        for line in import_lines:
            self.assertNotIn("dossier", line.lower(), line)
            self.assertNotIn("dossier_service", line.lower(), line)
        # 构造参数里没有任何"写"接口
        params = inspect.signature(SourceSkills.__init__).parameters
        self.assertNotIn("dossier", params)
        self.assertNotIn("write_fn", params)

    def test_single_page_budget_no_search_injection(self):
        """速查只读 1 页；流水线没有检索注入位（结构上发不出检索请求）。"""
        env = _Env(pages={NHC_URL: {"title": "通报", "text": "说明。"}})
        result = env.skills().run(NHC_URL)
        self.assertEqual(len(env.fetches), 1)
        self.assertEqual(result["audit"]["pages"], 1)
        self.assertEqual(result["audit"]["searches"], 0)
        params = inspect.signature(SourceSkills.__init__).parameters
        self.assertNotIn("search_fn", params)

    def test_data_keys_locked(self):
        """data 键名锁定（SKILL.md 头部契约与 pipeline._DATA_KEYS 一致）。"""
        env = _Env(pages={NHC_URL: {"title": "通报", "text": "说明。"}})
        result = env.skills().run(NHC_URL)
        self.assertEqual(tuple(sorted(result["data"])),
                         tuple(sorted(S._DATA_KEYS)))
        resolved = result["data"]["resolved"]
        self.assertEqual(tuple(sorted(resolved)), tuple(sorted(S._RESOLVED_KEYS)))

    def test_no_verdict_words_in_any_exit(self):
        """任何出口（summary/basis/report/gaps）都不出现固定真假判定词。"""
        cases = [
            _Env(pages={NHC_URL: {"title": "通报", "text": "说明。"}}),
            _Env(fail_urls={UNKNOWN_URL}),
            _Env(),
        ]
        inputs = [NHC_URL, UNKNOWN_URL, "某某神秘机构协会"]
        for env, query in zip(cases, inputs):
            with self.subTest(query=query):
                result = env.skills().run(query)
                for text in (result["data"]["summary"], result["data"]["basis"],
                             result["report"], "\n".join(result["gaps"])):
                    for word in _VERDICT_WORDS:
                        self.assertNotIn(word, text)


# ===========================================================================
# 5. 预算/取消/进度（4）
# ===========================================================================
class TestBudgetCancelProgress(unittest.TestCase):
    def test_expired_deadline_makes_budget_exhausted_without_fetch(self):
        env = _Env(pages={NHC_URL: {"title": "通报", "text": "说明。"}})
        result = env.skills().run(NHC_URL, deadline=time.monotonic() - 1.0)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(env.fetches, [])            # 死线已过，一次页都不抓
        self.assertEqual(result["data"]["state"], "unreachable")
        self.assertIn("预算/死线", "\n".join(result["gaps"]))

    def test_cancelled_run_preserves_shape(self):
        """取消：一次页都不抓，data 保持"未判定"形状（不冒充已判定）。"""
        env = _Env(pages={NHC_URL: {"title": "通报", "text": "说明。"}})
        result = env.skills().run(NHC_URL, cancel_check=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(env.fetches, [])
        self.assertFalse(result["data"]["verified"])
        self.assertEqual(result["data"]["authority_category"], "unverified")
        self.assertIn("取消", "\n".join(result["gaps"]))

    def test_progress_emits_four_chinese_stages(self):
        env = _Env(pages={NHC_URL: {"title": "通报", "text": "说明。"}})
        seen: list = []
        env.skills(on_progress=lambda stage, done, total: seen.append((stage, done, total))).run(NHC_URL)
        self.assertEqual([s[0] for s in seen], list(S._PROGRESS_STAGES))
        self.assertEqual([s[1] for s in seen], [1, 2, 3, 4])
        self.assertTrue(all(s[2] == 4 for s in seen))

    def test_run_never_raises_and_returns_shell(self):
        """取页函数抛异常也不外泄：折成 unreachable 降级（统一形状出口）。"""
        env = _Env(fetch_raises=RuntimeError("网络炸了"))
        result = env.skills().run(NHC_URL)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["state"], "unreachable")
        self.assertTrue(any(e["category"] == "fetch_exception" for e in result["errors"]))
        self.assertEqual(result["tool_id"], "check_source")
        check_source(NHC_URL, fetch_fn=env.fetch, clock=lambda: CLOCK)  # 便捷函数同壳


# ===========================================================================
# 6. 受约束补充说明（2）
# ===========================================================================
class TestConstrainedNote(unittest.TestCase):
    def test_compliant_note_appended_to_summary(self):
        def llm(system, user):
            self.assertIn("技能名：assess-source", system)     # system = SKILL.md 装配
            self.assertIn("note", system)
            return {"note": "该判定仅针对信源身份，页面内容未做核验。"}, ""
        env = _Env(pages={FOO_URL: {"title": "某局",
                                    "text": "主办单位：某市人民政府。政府网站标识码 X"}})
        result = env.skills(llm_fn=llm).run(FOO_URL)
        self.assertIn("该判定仅针对信源身份", result["data"]["summary"])
        self.assertEqual(result["audit"]["llm_calls"], 1)

    def test_out_of_bound_note_dropped(self):
        """判定词/升级表述/URL 一律丢弃，摘要退回纯模板（主结论不受影响）。

        升级表述用"未核验"场景验证（代码没判已核实时出现升级词即丢弃）；
        判定词与 URL 在任何场景都不允许。
        """
        cases = (
            # (页面文本, note, 未核验场景？)
            ("欢迎访问我局网站。", "所以这个信源是真的", False),
            ("欢迎访问我局网站。", "官方认定可信", False),
            ("欢迎访问我局网站。", "详情见 https://x.example", False),
            ("主办单位：某市人民政府。政府网站标识码 X", "这消息是真的", False),
            ("主办单位：某市人民政府。政府网站标识码 X", "参见 https://x.example", False),
        )
        for text, note, _unverified in cases:
            with self.subTest(note=note):
                env = _Env(pages={FOO_URL: {"title": "某局", "text": text}})
                result = env.skills(llm_fn=lambda s, u, n=note: ({"note": n}, "")).run(FOO_URL)
                self.assertNotIn(note, result["data"]["summary"])
                self.assertTrue(any("补充说明" in x for x in result["data"]["limits"]))


# ===========================================================================
# 7. 工具层（4）
# ===========================================================================
def _tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    tools_source.register_all(reg, None)
    return reg


def _ctx(services: Any, cancel_check=None, events=None) -> SimpleNamespace:
    return SimpleNamespace(sessions=None, dossiers=None, events=events,
                           cancel_check=cancel_check, services=services)


def _stub_skills(result: Any, raises: Any = None):
    """SourceSkills 替身：记录 run 实参后回预设结果（工具层透传口径锁定）。"""
    calls: list = []

    def _run(query, *, cancel_check=None, deadline=None, on_progress=None):
        calls.append({"query": query, "cancel_check": cancel_check,
                      "deadline": deadline, "on_progress": on_progress})
        if raises is not None:
            raise raises
        return result

    def _factory():
        return SimpleNamespace(run=_run)

    _factory.calls = calls
    return _factory


class TestToolLayer(unittest.TestCase):
    QUERY = NHC_URL

    def _call(self, services, query=QUERY, cancel_check=None, events=None):
        reg = _tool_registry()
        spec = reg.get("check_source")
        assert spec is not None, "check_source 未注册"
        return spec["handler"](_ctx(services, cancel_check, events), query=query)

    def _sample_result(self, status="completed", state="ok") -> Dict[str, Any]:
        return {
            "tool_id": "check_source", "status": status, "task_status": "completed",
            "data": {"query": self.QUERY,
                     "resolved": {"kind": "url", "url": self.QUERY, "host": "nhc.gov.cn",
                                  "name": "国家卫生健康委员会", "catalog": True,
                                  "catalog_anchor": True, "source_id": "SRC-NHC",
                                  "tier": "官方权威"},
                     "state": state, "authority_category": "official",
                     "category_label": "官方", "verified": True,
                     "basis": "信源目录锚点：国家卫生健康委员会（官方权威）",
                     "signals": ["official_suffix"], "history": None,
                     "summary": "信源速查：国家卫生健康委员会\n- 判定：已核实",
                     "limits": []},
            "gaps": [], "errors": [],
            "audit": {"operations": [], "searches": 0, "pages": 1, "llm_calls": 0},
            "budget": {"used": {"max_pages": 1, "max_llm_calls": 0}, "limits": {}},
            "report": "信源速查报告（只评信源身份，不判内容真假）", "started_at": CLOCK,
            "finished_at": CLOCK,
        }

    def test_registered_metadata(self):
        reg = _tool_registry()
        spec = reg.get("check_source")
        self.assertEqual(spec["params"], ["query"])
        self.assertEqual(spec["permission"], "read")     # 只读速查：不建档
        self.assertIn("check_source", reg.names())
        self.assertIn("信源", spec["description"])

    def test_query_param_validation(self):
        reg = _tool_registry()
        for bad in ("", "   ", None, 123, ["x"], "长" * 501):
            with self.subTest(query=bad):
                with self.assertRaises(ValidationError):
                    reg.validate("check_source", {"query": bad})
        clean = reg.validate("check_source", {"query": f"  {self.QUERY}  "})
        self.assertEqual(clean, {"query": self.QUERY})   # 首尾空白被剥掉
        junk = reg.validate("check_source", {"query": self.QUERY, "hack": 1})
        self.assertEqual(junk, {"query": self.QUERY})    # 未声明的键一律丢弃

    def test_handler_transcribes_result_and_never_saves(self):
        out = self._call({"source_skills": _stub_skills(self._sample_result())})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["dossier_id"])             # 只读速查：不建档
        self.assertTrue(out["not_saved"])
        self.assertEqual(out["card"]["card_type"], "source")
        self.assertEqual(out["card"]["verified"], True)
        self.assertIn("国家卫生健康委员会", out["digest"])
        self.assertIn("已核实", out["digest"])
        self.assertIn("无记录", out["digest"])
        self.assertIn("verify_claim", out["digest"])     # 指路核查流程
        for word in _VERDICT_WORDS:
            self.assertNotIn(word, out["digest"])

    def test_cancel_and_deadline_passthrough_and_exception_degrade(self):
        skills = _stub_skills(self._sample_result())

        def cancel_check():
            return False

        with llm_cache.deadline_scope(30):
            expected = llm_cache.deadline_at()
            self._call({"source_skills": skills}, cancel_check=cancel_check)
        call = skills.calls[-1]
        self.assertEqual(call["query"], self.QUERY)
        self.assertIs(call["cancel_check"], cancel_check)
        self.assertEqual(call["deadline"], expected)
        # run 抛异常 → ok=False + 诚实 digest，handler 永不抛（二道闸）
        broken = _stub_skills(None, raises=RuntimeError("速查爆炸"))
        out = self._call({"source_skills": broken})
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIsNone(out["card"])
        self.assertIn("不可用", out["digest"])


if __name__ == "__main__":
    unittest.main()
