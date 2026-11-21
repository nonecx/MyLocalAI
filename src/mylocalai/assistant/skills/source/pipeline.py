# -*- coding: utf-8 -*-
"""信源可信度速查流水线（通用技能）：用户给一个 URL/域名/机构名，只评信源身份不评内容。

改了什么、为什么、锁定什么：
  - 新增 `assistant.skills.source.pipeline`：把"解析输入 → 安全取页（1 页）→ 权威信号
    判定 → 历史战绩 → 摘要汇总"编排成一条轻流水线（`check_source` / `SourceSkills.run`）。
  - **判定全部由代码**：权威类别与核验方式复用 `collect_core.classify_source` /
    `collect_core.dynamic_authority_signals`（官方后缀/正文官方标记/已核验站点直链/
    多来源交叉提及，≥2 个代码信号才算动态核验通过）；页面自称权威、模型说"匹配"
    都不算。本模块不复制那套口径，只做编排与"1+3"判语选择。
  - **"1+3"输出口径**（SKILL.md 正文锁定）：有决定性代码信号（目录锚点或动态核验
    达标）才说"官方/权威"（verified=true）；否则只给倾向（档位/目录线索）或"未核验"；
    页面不可达/输入无法识别时如实降级，不猜。
  - **摘要优先代码模板拼接（零 LLM，最稳）**：llm_fn 注入时才允许一段极简受约束
    补充说明——不得新增信号、不得改变类别、不得出现真假判定词，越界即丢弃并退回
    纯模板摘要。
  - **历史战绩外置**：通过构造参数注入可选查询函数（如 dossier 信源战绩）；注入了
    且查到才输出，否则一律"无记录"。本流水线自身**不 import 任何档案/检索模块**，
    不建档、不写库（只读，铁律：不扩权）。
  - **每个出口都有降级**：输入无法识别 → needs_clarification；取页失败/正文为空/
    预算死线到顶 → unreachable（类别退回目录/档位口径并在 limits 写明原因）；
    取消保留已取得结果；绝不把"没读到页面"说成"核验过没问题"。
  - 全离线可测：fetch_fn / llm_fn / transport / clock / history_fn 全部可注入。

与 collect_core 的关系：预算、运行状态、来源分类、动态权威信号、SKILL.md 装配
全部复用共享底座；本模块只做编排与判语模板，不重复实现那套口径。

依赖方向：只 import 标准库与 assistant/retrieval 内部包，**不 import web / platform /
dossier**。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from mylocalai.assistant.skills.collect_core import (
    AUTHORITY_CLASSES,
    CollectBudget,
    CollectRunState,
    classify_source,
    dynamic_authority_signals,
    load_authority_config,
    load_collect_rules,
    load_skill_prompt,
    utc_now_iso,
)
from mylocalai.assistant.skills.pipeline import normalize_text
from mylocalai.retrieval.sources import normalize_host

__all__ = ["SourceSkills", "check_source", "render_report", "load_rules",
           "CATEGORY_LABELS", "SIGNAL_LABELS", "STATUSES", "TASK_STATUSES",
           "AUTHORITY_CLASSES"]

#: 权威类别中文标签（七值封闭枚举，与 collect_core.AUTHORITY_CLASSES 一一对应）
CATEGORY_LABELS = {"official": "官方", "international_org": "国际组织",
                   "academic": "学术机构", "standards": "标准机构",
                   "factcheck": "事实核查", "media": "权威媒体",
                   "unverified": "未核验"}
#: 代码信号中文标签（与 collect_core.dynamic_authority_signals 的信号名对应）
SIGNAL_LABELS = {"official_suffix": "官方域名后缀", "official_marker": "正文官方标记",
                 "inbound_link": "已核验站点直链", "cross_mention": "多来源交叉提及"}

#: 用户可见的四个进度阶段；不暴露内部技能名
_PROGRESS_STAGES = ("解析输入", "读取页面", "信号判定", "汇总输出")

#: 固定真假判定词（铁律 1）：摘要/补充说明/报告里一个都不许出现
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")
_VERDICT_RE = re.compile("|".join(re.escape(w) for w in _VERDICT_WORDS))
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）】]+", re.I)
#: 裸域名形态（整段输入就是一个域名，没有 scheme、没有空格）
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.[a-z]{2,}$", re.I)
#: 未核验时不允许出现的"升级"表述（补充说明越界即丢弃）
_UPGRADE_WORDS = ("官方认定", "权威认定", "已核实", "可信")

_HERE = Path(__file__).resolve().parent
_RULES_PATH = _HERE / "rules.json"
_SKILL_DIR = _HERE
_SOURCES_PATH = _HERE.parent / "sources.json"

# 内置默认值：与 rules.json 保持一致。rules.json 缺失/损坏时以此兜底。
# 页数锁定 1（速查不做超预算深挖）、时限取小值（比权威核验的 90s/10s 更紧）。
DEFAULT_SOURCE_RULES: Dict[str, Any] = {
    "max_input_chars": 500,
    "max_pages": 1,
    "max_llm_calls": 1,
    "total_deadline_s": 20.0,
    "io_timeout_s": 8.0,
    "max_page_bytes": 1500000,
    "max_text_chars": 20000,
    "max_redirect_hops": 3,
}

# 数值安全区间（键名锁定；未列出的键按 defaults 类型校验，不夹取）
_RULE_LIMITS: Dict[str, Tuple[float, float]] = {
    "max_input_chars": (50, 200000),
    "max_pages": (1, 4),
    "max_llm_calls": (1, 10),
    "total_deadline_s": (1, 3600),
    "io_timeout_s": (0.5, 300),
    "max_page_bytes": (1000, 50000000),
    "max_text_chars": (200, 500000),
    "max_redirect_hops": (0, 10),
}

#: 补充说明的机器契约（可选受约束生成；authority/adjudicate 同族的"越界即丢弃"）
_NOTE_CONTRACT = ('只输出 JSON：{"note":"<不超过100字的补充说明>"}；'
                  "只许转述上面代码已给出的判定/信号/类别，不得新增信号、类别、"
                  "URL、日期或数字；不得出现真假判定词；代码未判'已核实'时不得出现"
                  "官方认定/权威认定/已核实等升级表述。")

#: 返回 dict 与 resolved 的键名锁定（写进 SKILL.md；测试按此锁定）
_RESOLVED_KEYS = ("kind", "url", "host", "name", "catalog", "catalog_anchor",
                  "source_id", "tier")
_DATA_KEYS = ("query", "resolved", "state", "authority_category", "category_label",
              "verified", "basis", "signals", "history", "summary", "limits")

_SUMMARY_MAX_CHARS = 800
_QUERY_MAX_CHARS = 300


# ---------------------------------------------------------------------------
# 规则加载
# ---------------------------------------------------------------------------

def load_rules(path: Optional[Any] = None) -> Dict[str, Any]:
    """加载 source/rules.json：缺键回退默认、数值夹取、坏类型回退、未知键丢弃。

    传 dict 按"覆盖项"处理（便于测试按需收紧额度）；传路径读该文件；不传读包内
    rules.json。文件缺失/不可解析 → 内置默认值（不抛）：取不到预算配置时"按默认
    额度跑"比"整个工具不可用"更符合降级文化。
    """
    if isinstance(path, Mapping):
        merged = dict(DEFAULT_SOURCE_RULES)
        merged.update({k: v for k, v in path.items() if k in DEFAULT_SOURCE_RULES})
        return _clamp_rules(merged)
    target = _RULES_PATH if path is None else Path(path)
    return load_collect_rules(target, DEFAULT_SOURCE_RULES, _RULE_LIMITS)


def _clamp_rules(rules: Mapping[str, Any]) -> Dict[str, Any]:
    """与 collect_core.load_collect_rules 同口径的夹取（dict 覆盖项没有文件可读）。"""
    out: Dict[str, Any] = {}
    for key, default in DEFAULT_SOURCE_RULES.items():
        value = rules.get(key, default)
        if isinstance(default, bool):
            out[key] = value if isinstance(value, bool) else default
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = default
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            number = float(default)
        low, high = _RULE_LIMITS[key]
        number = min(max(number, low), high)
        out[key] = int(round(number)) if isinstance(default, int) else number
    return out


# ---------------------------------------------------------------------------
# 纯函数：输入解析与模板
# ---------------------------------------------------------------------------

def _clean(value: Any, limit: int = 200) -> str:
    """压空白 + 截断：所有进入输出结构的自由文本都过这里。"""
    return normalize_text(value)[:limit]


def _catalog_by_name(text: str) -> Optional[Mapping[str, Any]]:
    """机构名 → skills/sources.json 目录条目（精确匹配优先，其次包含匹配）。

    只做字符串匹配，不做任何"听起来像官方"的推断：目录没登记的名字一律返回
    None，由调用方按"无法识别"降级。
    """
    name = str(text or "").strip()
    if len(name) < 2:
        return None
    try:
        import json
        raw = json.loads(_SOURCES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  目录缺失/损坏 = 没有目录可查，不抛
        return None
    rows = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return None
    exact: Optional[Mapping[str, Any]] = None
    partial: Optional[Mapping[str, Any]] = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry_name = str(row.get("name") or "").strip()
        if not entry_name:
            continue
        if entry_name == name:
            exact = row
            break
        if (len(name) >= 4 and (entry_name in name or name in entry_name)
                and partial is None):
            partial = row
    return exact or partial


def _resolve_query(raw: str, authority_config: Mapping[str, Any]) -> Tuple[Dict[str, Any], str]:
    """把原文解析成可核查对象。返回 (resolved, state)；state 为 "" 或 "unclear"。

    优先级：URL > 裸域名 > 目录机构名 > 无法识别。解析不出任何可核查对象时
    state="unclear"，由 run() 走 needs_clarification 出口。
    """
    text = str(raw or "").strip()
    url = ""
    kind = "unknown"
    found = _URL_RE.search(text)
    if found:
        url = found.group(0)
        kind = "url"
    elif _DOMAIN_RE.match(text):
        url = f"https://{text.lower().rstrip('.')}/"
        kind = "domain"
    host = normalize_host(url) if url else ""
    name = ""
    catalog: Dict[str, Any] = {}
    if not host:
        entry = _catalog_by_name(text)
        if entry is not None:
            name = str(entry.get("name") or "").strip()
            domain = str(entry.get("domain") or "").strip().lower().rstrip(".")
            if domain:
                url = f"https://{domain}/"
                host = domain
                kind = "name"
                catalog = {"name": name,
                           "anchor": bool(entry.get("anchor")),
                           "source_id": str(entry.get("source_id") or ""),
                           "tier": str(entry.get("tier") or "")}
    if not host:
        return {"kind": "unknown", "url": "", "host": "", "name": _clean(text, 80),
                "catalog": False, "catalog_anchor": False, "source_id": "",
                "tier": ""}, "unclear"
    info = classify_source(host, authority_config=authority_config)
    resolved = {"kind": kind, "url": url, "host": host,
                "name": name or info.name or host,
                "catalog": bool(catalog) or info.verification == "catalog_anchor",
                "catalog_anchor": bool(catalog.get("anchor")) or info.verification == "catalog_anchor",
                "source_id": catalog.get("source_id") or info.source_id,
                "tier": catalog.get("tier") or info.tier}
    return {key: resolved.get(key) for key in _RESOLVED_KEYS}, ""


def _history_view(record: Mapping[str, Any]) -> Dict[str, Any]:
    """战绩记录的透传视图：字符串/数值原样（截断），嵌套结构转字符串。

    战绩形状由注入方（如 dossier 信源战绩）定义，本模块不解释字段含义，
    只做长度收敛，保证卡片里不塞进超长对象。
    """
    out: Dict[str, Any] = {}
    for key, value in list(record.items())[:20]:
        name = str(key)[:60]
        if isinstance(value, str):
            out[name] = value[:200]
        elif isinstance(value, bool) or value is None:
            out[name] = value
        elif isinstance(value, (int, float)):
            out[name] = value
        elif isinstance(value, (list, tuple)):
            out[name] = [str(x)[:100] for x in list(value)[:10]]
        else:
            out[name] = str(value)[:200]
    return out


def _history_line(history: Optional[Mapping[str, Any]]) -> str:
    """摘要里的战绩行：有记录逐项列出（最多 4 项），否则统一"无记录"。"""
    if not history:
        return "无记录"
    parts = [f"{k}={v}" for k, v in list(history.items())[:4]]
    suffix = "…" if len(history) > 4 else ""
    return "有记录：" + "；".join(parts) + suffix


def _verdict_text(state: str, category: str, verified: bool) -> str:
    """"1+3"判语：只有 verified=True 才允许"官方/权威"级别的表述。"""
    label = CATEGORY_LABELS.get(category, category)
    if state == "unclear":
        return "无法识别：输入不是可核查的 URL/域名，也未命中已登记机构名"
    if state == "unreachable":
        if verified:
            return f"页面不可达：身份判定退回目录口径（{label}），未读到本次正文"
        return f"页面不可达：未取得正文，不给可信度结论"
    if verified:
        return f"已核实：可按{label}信源对待（代码信号达标）"
    if category and category != "unverified":
        return f"倾向：{label}（未核验，代码信号不足，不作官方认定）"
    return "未核验：目录外且无代码信号，页面自称权威不算"


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class SourceSkills:
    """信源可信度速查流水线（解析输入 → 取 1 页 → 代码信号判定 → 战绩 → 摘要）。

    全部 IO 可注入：`fetch_fn(url, *, allowed_domains, cancel_check, deadline) ->
    transport.fetch 形状`、`llm_fn(system, user) -> (obj, err)`（chat_json 契约）、
    `history_fn(key) -> dict|None`（如 dossier 信源战绩查询）。
    未注入 fetch_fn 时延迟 import `assistant.skills.transport` 的默认实现，import
    失败降级为"取页不可用"（并行开发期 transport 可能尚未就位）。
    """

    def __init__(self, *, rules: Optional[Any] = None,
                 fetch_fn: Optional[Callable[..., Dict[str, Any]]] = None,
                 llm_fn: Optional[Callable[[str, str], Any]] = None,
                 transport: Optional[Any] = None,
                 clock: Optional[Callable[[], str]] = None,
                 on_progress: Optional[Callable[[str, int, int], Any]] = None,
                 authority_config: Optional[Mapping[str, Any]] = None,
                 history_fn: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
                 verified_pages: Optional[Sequence[Any]] = None) -> None:
        self.rules = load_rules(rules)
        self.fetch_fn = fetch_fn
        self.llm_fn = llm_fn
        self.transport = transport
        self.clock = clock or utc_now_iso
        self.on_progress = on_progress
        self.authority_config = (dict(authority_config)
                                 if isinstance(authority_config, Mapping)
                                 else load_authority_config())
        self.history_fn = history_fn
        self.verified_pages = [p for p in (verified_pages or [])
                               if isinstance(p, Mapping)]
        self._fetch_probed = fetch_fn is not None
        self._on_progress: Optional[Callable[[str, int, int], Any]] = None

    # -- 入口 ---------------------------------------------------------------

    def run(self, query: str, *, cancel_check: Optional[Callable[[], bool]] = None,
            deadline: Optional[float] = None,
            on_progress: Optional[Callable[[str, int, int], Any]] = None) -> Dict[str, Any]:
        """跑一次信源速查，返回统一运行外壳（data 键名锁定，见 _DATA_KEYS）。"""
        started_at = self.clock()
        self._on_progress = on_progress or self.on_progress
        raw = "" if query is None else str(query)
        if deadline is None:
            # 未传死线时按 total_deadline_s 自建：预算上限不该因为调用方没传就失效
            deadline = time.monotonic() + float(self.rules["total_deadline_s"])
        budget = CollectBudget(self.rules, deadline)
        st = CollectRunState(budget, cancel_check)
        st.data = {"query": _clean(raw, _QUERY_MAX_CHARS),
                   "resolved": {key: "" if key not in ("catalog", "catalog_anchor")
                                else False for key in _RESOLVED_KEYS},
                   "state": "unclear", "authority_category": "unverified",
                   "category_label": CATEGORY_LABELS["unverified"],
                   "verified": False, "basis": "", "signals": [],
                   "history": None, "summary": "", "limits": []}

        if not self._precheck(st, raw):
            return self._finish(st, started_at)
        resolved, state = _resolve_query(raw, self.authority_config)
        st.data["resolved"] = resolved
        self._progress(st, _PROGRESS_STAGES[0], 1)
        if state == "unclear":
            # 输入无法识别不是失败：问用户要一个具体的网址/域名（合法空出口）
            st.clarify("输入无法识别为 URL/域名，也未命中已登记机构名："
                       "请提供一个具体的网址或域名")
            self._render(st)
            self._progress(st, _PROGRESS_STAGES[3], 4)
            return self._finish(st, started_at)
        if st.is_cancelled():
            st.gap("运行在读取页面前被取消，未取得页面正文")
            self._render(st)
            return self._finish(st, started_at)

        page, error = self._do_fetch(st, resolved, cancel_check)
        self._progress(st, _PROGRESS_STAGES[1], 2)
        if page is None:
            # 页面不可达如实降级：类别退回目录/档位口径，绝不猜一个"可信"
            st.data["state"] = "unreachable"
            if error is not None:
                st.data["limits"].append(
                    f"页面未读取（{error.get('category') or 'fetch_error'}）："
                    "本次没有正文可查，身份判定只有目录/档位口径")
            self._classify(st, resolved, None)
            self._collect_history(st, resolved)
            self._render(st)
            self._progress(st, _PROGRESS_STAGES[3], 4)
            return self._finish(st, started_at)

        self._classify(st, resolved, page)
        self._progress(st, _PROGRESS_STAGES[2], 3)
        self._collect_history(st, resolved)
        self._render(st)
        self._progress(st, _PROGRESS_STAGES[3], 4)
        return self._finish(st, started_at)

    # -- 阶段：预检 ---------------------------------------------------------

    def _precheck(self, st: CollectRunState, raw: str) -> bool:
        if not raw.strip():
            st.error("precheck", "empty_input", "输入为空或只有空白字符")
            st.clarify("输入为空：请提供需要速查的 URL、域名或机构名")
            return False
        limit = int(self.rules["max_input_chars"])
        if len(raw) > limit:
            st.error("precheck", "input_too_long",
                     f"输入 {len(raw)} 字符，超过上限 {limit}")
            st.clarify(f"输入长度 {len(raw)} 超过单次上限 {limit}：请只提供一个网址或域名")
            return False
        return True

    # -- 阶段：取页 ---------------------------------------------------------

    def _fetch_callable(self) -> Optional[Callable[..., Dict[str, Any]]]:
        if self.fetch_fn is not None:
            return self.fetch_fn
        if self._fetch_probed:
            return None
        self._fetch_probed = True
        try:
            from mylocalai.assistant.skills.transport import default_transport
            # rules.json 是取页上限的唯一来源：默认 transport 按本实例的规则构造，
            # 避免"代码里一份默认值、rules.json 里一份默认值"两处漂移
            transport = self.transport or default_transport(
                max_bytes=int(self.rules["max_page_bytes"]),
                max_text_chars=int(self.rules["max_text_chars"]),
                timeout_s=float(self.rules["io_timeout_s"]),
                max_hops=int(self.rules["max_redirect_hops"]))
            self.fetch_fn = getattr(transport, "fetch", None)
        except Exception:  # noqa: BLE001  transport 未就位/导入失败：降级为"取页不可用"
            self.fetch_fn = None
        return self.fetch_fn

    def _do_fetch(self, st: CollectRunState, resolved: Mapping[str, Any],
                  cancel_check: Optional[Callable[[], bool]]) -> Tuple[Optional[Dict[str, Any]],
                                                                       Optional[Dict[str, str]]]:
        """读 1 页（速查不做深挖）。返回 (page|None, error|None)。"""
        url = str(resolved.get("url") or "")
        host = str(resolved.get("host") or "")
        fetch = self._fetch_callable()
        if fetch is None:
            error = {"stage": "fetch", "category": "fetch_unavailable",
                     "detail": "取页不可用：未注入 fetch_fn 且默认 transport 不可用"}
            st.error("fetch", error["category"], error["detail"])
            return None, error
        if not st.budget.allow("max_pages"):
            st.budget.block(st.budget.why("max_pages"))
            return None, None
        if st.is_cancelled():
            st.gap("运行在读取页面前被取消，未取得页面正文")
            return None, None
        entry = st.operation("fetch", url, False)
        try:
            # 域名允许集锁定为输入信源自己的域：速查只读这一站，重定向跳到别站
            # 一律拒绝（跳走了评的就不是用户问的那个信源）。
            page = fetch(url, allowed_domains=[host] if host else None,
                         cancel_check=cancel_check, deadline=st.budget.deadline)
        except Exception as exc:  # noqa: BLE001  取页异常按失败记，不当成"页面无信号"
            error = {"stage": "fetch", "category": "fetch_exception",
                     "detail": f"取页异常：{type(exc).__name__}"}
            st.error("fetch", error["category"], error["detail"])
            return None, error
        if not isinstance(page, dict):
            error = {"stage": "fetch", "category": "fetch_unavailable",
                     "detail": "取页返回形状不符"}
            st.error("fetch", error["category"], error["detail"])
            return None, error
        if page.get("status") != "ok":
            category = _clean(page.get("category") or "fetch_error", 60) or "fetch_error"
            detail = _clean(page.get("detail") or "取页失败", 200)
            st.error("fetch", category, detail)
            return None, {"stage": "fetch", "category": category, "detail": detail}
        st.budget.spend("max_pages")
        entry["ok"] = True
        if not normalize_text(page.get("text") or ""):
            st.error("fetch", "empty_page_text",
                     f"{url or host} 读取成功但正文为空，未取得可判定信号")
            return None, {"stage": "fetch", "category": "empty_page_text",
                          "detail": "页面正文为空"}
        return page, None

    # -- 阶段：权威信号判定（全部复用 collect_core，不复制口径） -------------

    def _classify(self, st: CollectRunState, resolved: Mapping[str, Any],
                  page: Optional[Mapping[str, Any]]) -> None:
        """目录/档位分类 + 动态核验信号 → "1+3"判别（category / verified / basis）。"""
        host = str(resolved.get("host") or "")
        url = str(resolved.get("url") or host)
        info = classify_source(host, authority_config=self.authority_config)
        pages = [page] if page is not None else []
        # 注入的 verified_pages 只是"本会话已核验页面"的只读上下文
        #（inbound_link / cross_mention 信号的依据），不写回、不建档。
        signals = dynamic_authority_signals(
            url, pages, authority_config=self.authority_config,
            verified_pages=self.verified_pages)
        signal_names = [str(s) for s in signals.get("signals") or []]
        dynamic_verified = bool(signals.get("verified"))
        catalog_anchor = info.verification == "catalog_anchor"
        verified = bool(catalog_anchor or dynamic_verified)
        if catalog_anchor:
            category = info.authority_class
            basis = f"信源目录锚点：{info.name or info.domain}（{info.tier or '未登记档'}）"
            if signal_names:
                basis += f"；{signals.get('basis') or ''}"
        elif dynamic_verified:
            category = str(signals.get("authority_class") or info.authority_class)
            # signals.basis 自带"动态核验信号："前缀，这里只补达标结论，避免重复
            basis = (str(signals.get("basis") or "、".join(signal_names))
                     + "（信号达标，已核实）")
            if not ("cross_mention" in signal_names or "inbound_link" in signal_names):
                st.data["limits"].append(
                    "动态核验仅自证信号（域名后缀/正文标记），如需作为决定性证据"
                    "还需交叉佐证（由权威核验流程判定）")
        else:
            category = info.authority_class
            basis = info.verification_basis or "未核验"
            if signal_names:
                basis += f"；{signals.get('basis') or ''}（信号不足，未核验）"
            if signal_names and page is not None:
                st.data["limits"].append(
                    "仅有单点自证信号（域名后缀/正文标记），不作可信度认定；"
                    "如需作为决定性证据还需交叉佐证（由权威核验流程判定）")
        if category not in AUTHORITY_CLASSES:
            st.error("classify", "bad_category",
                     f"未知权威类别：{category or '（空）'}，按 unverified 处理")
            category = "unverified"
        if page is not None:
            # 只有真的读到了正文才算 ok；页面不可达路径保持 unreachable，不 here覆盖
            st.data["state"] = "ok"
        st.data["authority_category"] = category
        st.data["category_label"] = CATEGORY_LABELS.get(category, category)
        st.data["verified"] = verified
        st.data["basis"] = _clean(basis, 300)
        st.data["signals"] = signal_names
        if not verified and page is not None:
            st.data["limits"].append("页面自称权威不算信号：本速查只认代码可验证的"
                                     "目录锚点或动态核验信号")

    # -- 阶段：历史战绩（注入才查，查到才有） -------------------------------

    def _collect_history(self, st: CollectRunState, resolved: Mapping[str, Any]) -> None:
        """历史战绩：history_fn 由构造注入；注入了且查到才输出，否则"无记录"。"""
        if self.history_fn is None:
            return
        key = str(resolved.get("host") or resolved.get("name") or "")
        try:
            record = self.history_fn(key) if key else None
        except Exception as exc:  # noqa: BLE001  战绩查询失败不影响身份判定本身
            st.data["limits"].append(f"历史战绩查询失败（{type(exc).__name__}），"
                                     "本次不提供战绩")
            return
        if isinstance(record, Mapping) and record:
            st.data["history"] = _history_view(record)
        elif record:
            st.data["limits"].append("历史战绩返回形状不符（需要 dict），本次不提供战绩")

    # -- 阶段：摘要（代码模板优先，可选受约束补充） -------------------------

    def _render(self, st: CollectRunState) -> None:
        """摘要输出：代码模板拼接（零 LLM 最稳）；llm_fn 注入且查到页面时才追加
        一段受约束补充说明（越界丢弃，退回纯模板）。"""
        data = st.data
        resolved = data["resolved"] if isinstance(data["resolved"], dict) else {}
        display = str(resolved.get("name") or resolved.get("host")
                      or data["query"] or "（未识别）")
        kind_label = {"url": "网址", "domain": "域名", "name": "已登记机构名",
                      "unknown": "未识别"}.get(str(resolved.get("kind")), "未识别")
        if resolved.get("catalog_anchor"):
            catalog_text = f"目录锚点（{resolved.get('name') or resolved.get('host')}）"
        elif resolved.get("catalog"):
            catalog_text = "目录登记（非锚点）"
        else:
            catalog_text = "目录外"
        lines = [
            f"信源速查：{display}",
            f"- 解析：{kind_label} {resolved.get('host') or display}",
            f"- 判定：{_verdict_text(str(data['state']), str(data['authority_category']), bool(data['verified']))}",
            f"- 权威类别：{data['category_label']}（{data['authority_category']}）｜目录：{catalog_text}",
            f"- 代码信号：" + ("、".join(
                SIGNAL_LABELS.get(s, s) for s in data["signals"]) if data["signals"] else "无"),
            f"- 依据：{data['basis'] or '（无）'}",
            f"- 历史战绩：{_history_line(data['history'])}",
            "- 边界：本速查只评信源身份，不判内容真假；需要核查具体说法请走"
            " verify_claim / verify_authority。",
        ]
        note = self._constrained_note(st)
        if note:
            lines.append(f"- 补充：{note}")
        data["summary"] = _clean("\n".join(lines), _SUMMARY_MAX_CHARS)

    def _constrained_note(self, st: CollectRunState) -> str:
        """可选的一段极简受约束补充说明（authority/adjudicate 同族校验）。

        llm_fn 未注入 / 预算到顶 / 取消 / 输出越界（新增 URL、真假判定词、
        未核验时出现升级表述、超长）→ 一律丢弃并退回纯模板摘要，主结论不受影响。
        """
        if self.llm_fn is None or st.data["state"] != "ok":
            return ""
        if not st.budget.allow("max_llm_calls"):
            st.budget.block(st.budget.why("max_llm_calls"))
            return ""
        if st.is_cancelled():
            return ""
        st.budget.spend("max_llm_calls")
        entry = st.operation("llm", "summary_note", False)
        try:
            out = self.llm_fn(load_skill_prompt(_SKILL_DIR, _NOTE_CONTRACT),
                              "代码判定：\n" + st.data["summary"])
        except Exception as exc:  # noqa: BLE001  模型侧异常只影响补充说明，不影响主结论
            st.data["limits"].append(f"模型补充说明未生成（{type(exc).__name__}），"
                                     "摘要以代码模板为准")
            return ""
        obj = None
        if isinstance(out, tuple) and len(out) == 2:
            obj, _err = out
        elif isinstance(out, dict):
            obj = out
        note = _clean(obj.get("note") if isinstance(obj, dict) else "", 120)
        if not note:
            st.data["limits"].append("模型补充说明为空或形状不符，摘要以代码模板为准")
            return ""
        if _URL_RE.search(note) or _VERDICT_RE.search(note):
            st.data["limits"].append("模型补充说明含 URL 或真假判定词，已丢弃")
            return ""
        if not st.data["verified"] and any(w in note for w in _UPGRADE_WORDS):
            st.data["limits"].append("模型补充说明出现升级表述（代码未判已核实），已丢弃")
            return ""
        entry["ok"] = True
        return note

    # -- 收尾 ---------------------------------------------------------------

    def _progress(self, st: CollectRunState, stage: str, done: int) -> None:
        hook = self._on_progress
        if hook is None:
            return
        try:
            hook(stage, done, len(_PROGRESS_STAGES))
        except Exception:  # noqa: BLE001  进度回调异常不影响速查结果本身
            pass

    def _finish(self, st: CollectRunState, started_at: str) -> Dict[str, Any]:
        core = st.finish()
        audit = st.audit
        budget = st.budget
        result = {
            "tool_id": "check_source",
            "status": core["status"],
            "task_status": core["task_status"],
            "data": core["data"],
            "gaps": list(core["gaps"]),
            "errors": list(core["errors"]),
            # 计数取预算实耗（与 authority/research 同口径）：operations 行的 ok
            # 是事后回填的，audit["pages"]/["llm_calls"] 只按创建时点计数，不可靠。
            "audit": {"operations": list(audit.get("operations") or []),
                      "searches": 0,
                      "pages": int(budget.used.get("max_pages") or 0),
                      "llm_calls": int(budget.used.get("max_llm_calls") or 0)},
            "budget": core["budget"],
            "report": "",
            "started_at": started_at,
            "finished_at": self.clock(),
        }
        if core["status"] == "needs_clarification":
            result["gaps"].insert(0, "需要用户补充：请提供一个具体的网址或域名")
        if st.is_cancelled():
            result["gaps"].insert(0, "运行被取消，已保留此前取得的判定")
        if st.budget.blocked_reason:
            result["gaps"].insert(0, f"预算/死线到顶提前停止（{st.budget.blocked_reason}），"
                                     "已保留目录/档位口径的判定")
        result["report"] = render_report(result)
        return result


def check_source(query: str, **kw) -> Dict[str, Any]:
    """便捷函数 = SourceSkills(**kw).run(query)。工具层用它。"""
    return SourceSkills(**kw).run(query)


# ---------------------------------------------------------------------------
# 确定性纯文本报告
# ---------------------------------------------------------------------------

def _report_field(value: Any, limit: int = 160) -> str:
    """非引文字段的渲染：去控制字符、把 HTML/markdown 标记字符换成全角等价物、压空白、截断。"""
    text = normalize_text(value)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    for raw, safe in (("<", "＜"), (">", "＞"), ("[", "［"), ("]", "］")):
        text = text.replace(raw, safe)
    return text[:limit]


def render_report(result: Dict[str, Any]) -> str:
    """把一次速查结果渲染成确定性纯文本报告（无 HTML、无 markdown 链接语法）。"""
    if not isinstance(result, dict):
        return "信源速查报告：（结果形状不符，无可渲染内容）"
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    budget = result.get("budget") if isinstance(result.get("budget"), dict) else {}
    used = budget.get("used") if isinstance(budget.get("used"), dict) else {}

    resolved = data.get("resolved") if isinstance(data.get("resolved"), dict) else {}
    signals = [str(s) for s in (data.get("signals") or [])]
    history = data.get("history") if isinstance(data.get("history"), dict) else None

    lines: List[str] = ["信源速查报告（只评信源身份，不判内容真假）"]
    lines.append(f"执行状态：{_report_field(result.get('status'), 40) or '（未知）'}"
                 f"　任务状态：{_report_field(result.get('task_status'), 40) or '（未知）'}")
    lines.append(f"开始：{_report_field(result.get('started_at'), 40)}"
                 f"　结束：{_report_field(result.get('finished_at'), 40)}")
    lines.append(f"输入：{_report_field(data.get('query'), 300)}")
    lines.append(f"解析：{_report_field(resolved.get('kind'), 20)}"
                 f"　主机：{_report_field(resolved.get('host'), 120)}"
                 f"　目录锚点：{'是' if resolved.get('catalog_anchor') else '否'}")
    lines.append(f"判定：{_report_field(_verdict_text(str(data.get('state') or 'unclear'),
                                                      str(data.get('authority_category') or 'unverified'),
                                                      bool(data.get('verified'))), 200)}")
    lines.append(f"权威类别：{_report_field(data.get('authority_category'), 20)}"
                 f"（{_report_field(data.get('category_label'), 20)}）"
                 f"｜代码信号：{_report_field('、'.join(signals), 120) or '无'}")
    lines.append(f"依据：{_report_field(data.get('basis'), 200) or '（无）'}")
    lines.append(f"历史战绩：{_report_field(_history_line(history), 200)}")
    summary = _report_field(data.get("summary"), 800)
    if summary:
        lines.append("摘要：")
        for line in summary.splitlines():
            lines.append(f"  {line}")
    limits = [_report_field(x, 200) for x in (data.get("limits") or []) if x]
    lines.append(f"限制（共 {len(limits)} 条）")
    for index, text in enumerate(limits, start=1):
        lines.append(f"  {index}. {text}")
    gaps = [_report_field(g, 240) for g in (result.get("gaps") or [])]
    lines.append(f"缺口（共 {len(gaps)} 条）")
    for index, text in enumerate(gaps, start=1):
        lines.append(f"  {index}. {text}")
    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    lines.append(f"错误（共 {len(errors)} 条）")
    for item in errors:
        lines.append(f"  - [{_report_field(item.get('stage'), 20)}] "
                     f"{_report_field(item.get('category'), 40)}："
                     f"{_report_field(item.get('detail'), 200)}")
    lines.append("预算与可观测："
                 f"读页 {int(used.get('max_pages') or 0)}"
                 f"/{budget.get('limits', {}).get('max_pages', '-') if isinstance(budget.get('limits'), dict) else '-'}"
                 f"；模型调用 {int(used.get('max_llm_calls') or 0)}；检索 0 次（本技能不检索）")
    return "\n".join(lines)
