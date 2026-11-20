# -*- coding: utf-8 -*-
"""权威核验流水线（契约 §6）：把“这段说法，权威资料能说明到什么程度”做成一条确定性流水线。

改了什么、为什么、锁定什么：
  - 新增 `assistant.skills.authority.pipeline`：预检（含纯链接抓取）→ 拆主张（复用
    `skills/evidence/SKILL.md` 指令）→ 权威源发现（目录锚点 + 动态核验）→ 逐主张检索/读页/
    抽引文（relation 封闭枚举、引文逐字锚定）→ **代码**算 balance/lean 与 conclusion →
    受约束 analysis（只能引用已存在 evidence_id）→ 确定性报告。
  - 为什么单独成模块：权威核验与资料收集共用采集底座（`collect_core`）与受限循环
    （`agent_loop`），但结论口径完全不同——权威侧多一层“来源资格”判定，且只有决定性证据
    才允许“可以证明/可以直接反驳”的模板。把这层判定写死在代码里，模型只写分析文字。
  - 锁定“不判真假、不打分”：`balance` / `lean` / `conclusion` 全部由 `decide_verdict`
    按契约 §6.1 的决策表逐字生成；输出里没有真假标签、没有数值分，模型改不动。
  - 锁定“权威不能自封”：类别与核验方式来自目录锚点或代码可见的动态信号（官方后缀 /
    官方正文标记 / 已核验页面直链 / 多来源交叉提及）；页面自称“官网”、模型说“匹配”都不算。
    单点动态来源不进决定性证据（除非有独立已核验来源同向佐证或两个独立动态来源）。
  - 锁定“每个出口都有降级”：空输入/超长/纯链接抓不到 → needs_clarification；检索未授权 /
    拆主张阶段模型不可用 → failed；读页失败、引文丢弃、部分主张未覆盖、后续模型不可用 →
    partial；取消/预算到顶保留已完成结果；零结果 → balance=insufficient（不是失败）。
  - 全离线可测：`search_fn` / `fetch_fn` / `llm_fn` / `transport` / `clock` 全部可注入，
    测试零网络零 LLM。

两种模式：`mode="pipeline"`（固定阶段）与 `mode="agent"`（§7 受限循环）。两者共用同一套
确定性收口（分类 → 决策表 → analysis 校验 → 报告），差别只在“下一步做什么由谁决定”。

依赖方向：只 import 标准库 + 本包既有模块（collect_core / agent_loop / pipeline /
retrieval.sources），不 import web / platform。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from mylocalai.assistant.skills.agent_loop import run_agent_loop
from mylocalai.assistant.skills.collect_core import (
    AUTHORITY_CLASSES,
    DEFAULT_COLLECT_RULES,
    RELATIONS,
    VERIFICATIONS,
    CollectBudget,
    CollectRunState,
    SourceInfo,
    anchor_quote,
    classify_source,
    dynamic_authority_signals,
    is_decisive_source,
    load_authority_config,
    load_collect_rules,
    load_skill_prompt,
    render_counts_line,
    render_evidence_line,
    url_key,
    utc_now_iso,
)
from mylocalai.assistant.skills.pipeline import normalize_text, strip_pii
from mylocalai.retrieval.sources import tier_rank

__all__ = [
    "MODES", "ORIGINS", "BALANCES", "LEANS", "BALANCE_LABELS", "LEAN_LABELS",
    "DEFAULT_AUTHORITY_RULES", "decide_verdict", "AuthoritySkills",
    "verify_authority", "render_report",
]

_HERE = Path(__file__).resolve().parent
_EVIDENCE_DIR = _HERE.parent / "evidence"
_SOURCES_PATH = _HERE.parent / "sources.json"
_REGISTRY_PATH = _HERE.parents[2] / "config" / "sources_registry.json"
_RULES_PATH = _HERE / "rules.json"

#: 工作流模式（封闭枚举，设置页只给这两个）
MODES = ("pipeline", "agent")
#: 主张来源（封闭枚举）：用户输入 / 研究卡自动接力 / 用户给的链接正文
ORIGINS = ("user", "auto_from_research", "user_link")
#: 证据倾向（五态，契约 §6.1）
BALANCES = ("supported", "refuted", "contested", "insufficient", "not_checkable")
#: 可靠证据倾向（四态，契约 §6.1）
LEANS = ("supports", "refutes", "mixed", "none")

#: 展示标签：只描述“权威资格/证据倾向”，不出现真假字样
BALANCE_LABELS = {
    "supported": "权威资料可以证明",
    "refuted": "权威资料可以直接反驳",
    "contested": "权威资料之间存在直接冲突",
    "insufficient": "缺少权威确认，不能据此证明或反驳",
    "not_checkable": "不是可核查的事实主张",
}
LEAN_LABELS = {
    "supports": "可靠证据倾向支持",
    "refutes": "可靠证据倾向反驳",
    "mixed": "可靠证据互相矛盾",
    "none": "没有可用的可靠证据倾向",
}
_RELATION_LABELS = {"supports": "支持", "refutes": "反驳", "partial": "部分一致",
                    "background": "背景", "unclear": "关系不明"}
_VERIFICATION_LABELS = {
    "catalog_anchor": "目录锚点",
    "verified_site_link": "已核验站点直链",
    "session_verified": "本次运行内动态核验",
    "unverified": "未核验",
}
_ANCHOR_VERIFICATIONS = ("catalog_anchor", "verified_site_link", "session_verified")

#: 分析文字里不允许出现的“真假标签”词（出现即整条丢弃并记 error）
_VERDICT_WORDS = ("属实", "不属实", "判定为真", "判定为假", "确认为真", "确认为假",
                  "真的", "假的", "真实无疑", "百分之百")

#: 内置默认规则（与 rules.json 同文化；文件缺失/坏值逐键回退这里）
DEFAULT_AUTHORITY_RULES: Dict[str, Any] = {
    **DEFAULT_COLLECT_RULES,
    "max_input_chars": 4000,
    "max_claims": 4,
    "per_claim_max_queries": 2,
    "per_claim_max_pages": 3,
    "max_catalog_sources_per_claim": 3,
    "agent_max_steps": 8,
    "dynamic_authority": True,
}

#: 数值安全区间（只夹这里列出的键；未列出的键按默认值类型校验）
_RULE_LIMITS: Dict[str, Tuple[float, float]] = {
    "max_input_chars": (10, 200000),
    "max_claims": (1, 10),
    "per_claim_max_queries": (1, 5),
    "per_claim_max_pages": (1, 10),
    "max_catalog_sources_per_claim": (1, 10),
    "agent_max_steps": (1, 20),
}

#: 模型生成的范围操作符：域名范围由代码按候选权威源拼 site:，模型写的一律剥掉
_QUERY_OPERATORS = ("site:", "inurl:", "allinurl:", "intitle:", "allintitle:",
                    "filetype:", "inanchor:", "intext:", "link:", "related:", "cache:")

_LINK_ONLY_RE = re.compile(r"^\s*https?://[^\s<>\"'，。；、）】]+\s*$", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）】]+", re.I)
_PUBLIC_CLAIM_KEYS = ("claim_id", "text", "origin", "claim_status", "balance",
                      "balance_label", "lean", "lean_label", "conclusion",
                      "supporting_ids", "refuting_ids", "context_ids",
                      "authority_basis", "limitations")


# ---------------------------------------------------------------------------
# 配置加载（坏配置只降级，不抛）
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Dict[str, Any]:
    """读一个 JSON 对象资源；缺失/写坏返回 {}（调用方按“没有该资源”降级）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  资源缺失/损坏 = 少一份目录，不该让流水线崩
        return {}
    return data if isinstance(data, dict) else {}


def _load_rules(rules: Any) -> Dict[str, Any]:
    """加载规则：dict 视为覆盖项，路径视为配置文件，None 读包内 rules.json。

    为什么统一走 `load_collect_rules`：坏值夹取/坏类型回退的口径只在一处维护，
    避免权威侧与收集侧各写一份“夹到多少算安全”。
    """
    if isinstance(rules, Mapping):
        merged = dict(DEFAULT_AUTHORITY_RULES)
        for key, value in rules.items():
            if key not in merged:
                continue
            default = merged[key]
            # 覆盖项只接受与默认值同族的类型：写坏的覆盖值丢弃，而不是把默认值本身污染成字符串
            if isinstance(default, bool):
                if isinstance(value, bool):
                    merged[key] = value
            elif isinstance(default, int):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    merged[key] = value
            elif isinstance(default, float):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    merged[key] = value
            elif isinstance(value, type(default)):
                merged[key] = value
        return load_collect_rules(None, merged, _RULE_LIMITS)
    return load_collect_rules(rules if rules is not None else _RULES_PATH,
                              DEFAULT_AUTHORITY_RULES, _RULE_LIMITS)


def _merge_authority_config(raw: Any) -> Dict[str, Any]:
    """把调用方给的权威配置按默认配置的类型逐键合并（类型不符的键丢弃）。"""
    config = load_authority_config()
    if not isinstance(raw, Mapping):
        return config
    for key, value in raw.items():
        default = config.get(key)
        if isinstance(default, bool):
            if isinstance(value, bool):
                config[key] = value
        elif isinstance(default, int):
            if isinstance(value, int) and not isinstance(value, bool):
                config[key] = max(1, value)
        elif isinstance(default, list):
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                items = [str(item) for item in value if isinstance(item, str) and item]
                if items:
                    config[key] = items
        elif isinstance(default, dict):
            if isinstance(value, Mapping) and all(isinstance(k, str) and isinstance(v, str)
                                                  for k, v in value.items()):
                config[key] = dict(value)
    return config


def _authority_config(authorities: Any) -> Dict[str, Any]:
    """权威配置入口：dict 覆盖项 / 路径 / None（读 authorities.json）。"""
    if authorities is None:
        return load_authority_config()
    if isinstance(authorities, Mapping):
        return _merge_authority_config(authorities)
    return load_authority_config(authorities)


# ---------------------------------------------------------------------------
# 文本小工具（确定性、无 IO）
# ---------------------------------------------------------------------------

def _clean(value: Any, limit: int = 200) -> str:
    """压空白 + 截断：所有进入输出结构的自由文本都过这里。"""
    return normalize_text(value)[:limit]


def _strip_query_operators(query: Any) -> str:
    """剥掉模型生成的 site:/inurl: 等范围操作符（与取证流水线同口径）。"""
    text = str(query or "")
    for operator in _QUERY_OPERATORS:
        text = re.sub(re.escape(operator) + r"\s*\S+", " ", text, flags=re.IGNORECASE)
    return normalize_text(text)


def _clean_query(value: Any) -> str:
    """检索词：先脱敏（PII/URL），再剥范围操作符。"""
    return _strip_query_operators(strip_pii(value))


def _host_of(url: Any) -> str:
    """取主机名（小写、去尾点、去一层 www.）；解析失败返回空串。"""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except Exception:  # noqa: BLE001  urlsplit 对畸形输入可能抛；这里只求一个域名
        return ""
    return host[4:] if host.startswith("www.") else host


def _within(host: str, domain: str) -> bool:
    """host 是否落在 domain 内（相等或子域）。"""
    host = (host or "").lower().rstrip(".")
    domain = (domain or "").lower().rstrip(".")
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def _group_of(body: Any) -> str:
    """正文哈希独立组：同一篇文章转载/镜像到别处也落进同一组。"""
    digest = hashlib.sha256(normalize_text(body).encode("utf-8")).hexdigest()[:12]
    return "G-" + digest


def _bigrams(text: Any) -> set:
    """中文没有空格，用字符二元组做粗粒度主题重叠（只用于目录候选排序）。"""
    value = normalize_text(text)
    return {value[index:index + 2] for index in range(max(0, len(value) - 1))}


def _relevance(claim_text: Any, entry_text: Any) -> float:
    """主张与目录条目的二元组重叠率（0~1）；算不出按 0 处理。"""
    left = _bigrams(claim_text)
    if not left:
        return 0.0
    right = _bigrams(entry_text)
    return len(left & right) / float(len(left))


def _safe_text(value: Any, limit: int = 160) -> str:
    """报告里的非引文字段：去控制字符、把 HTML/markdown 标记换成全角等价物。

    为什么换全角而不是删掉：报告是确定性纯文本，标题/域名/说明里混进 `<script>` 或
    `[x](url)` 就有注入 GUI 的风险；换成全角既保留信息量，又让标记语法失效。
    引文不走这里——引文必须逐字保留。
    """
    text = normalize_text(value)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    for raw, safe in (("<", "＜"), (">", "＞"), ("[", "［"), ("]", "］")):
        text = text.replace(raw, safe)
    return text[:limit]


def _safe_quote(value: Any, limit: int) -> str:
    """报告里的引文：逐字保留（只去控制字符并压空白）。"""
    text = normalize_text(value)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    return text[:limit]


def _first_list(obj: Any, keys: Sequence[str]) -> Optional[list]:
    """从模型返回里取第一个命中键的列表（容忍键名写歪）。"""
    if not isinstance(obj, Mapping):
        return None
    for key in keys:
        value = obj.get(key)
        if isinstance(value, list):
            return value
    if len(obj) == 1:
        only = next(iter(obj.values()))
        if isinstance(only, list):
            return only
    return None


def _string_items(value: Any, limit: int, item_limit: int) -> List[str]:
    """把模型返回的数组收敛成去重字符串列表（保序、去空白项）。"""
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value[:limit]:
        text = _clean(item, item_limit)
        if text and text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# 决策表（契约 §6.1，纯计算）
# ---------------------------------------------------------------------------

def decide_verdict(claim_status: str, decisive_supports: int, decisive_refutes: int,
                   reliable_supports: int, reliable_refutes: int, *,
                   scope: str = "", summary: str = "") -> Dict[str, str]:
    """按契约 §6.1 的决策表算 balance / lean / 结论模板（逐字实现，模型不参与）。

    `decisive_*` 只数“有资格作决定性证据、且关系为 supports/refutes”的条数；
    `reliable_*` 数全部已核验来源（目录锚点 / 已核验直链 / 会话内动态核验）的同向条数。
    `scope` 缺省写“现有资料覆盖范围”；`summary` 取首条决定性引文前 60 字。
    """
    scope_text = _clean(scope, 40) or "现有资料覆盖"
    # 模板是“在{scope}范围内”：缺省口径本身已含“范围”二字，避免拼成“覆盖范围范围内”
    if scope_text in ("现有资料覆盖范围", "现有资料覆盖"):
        scope_text = "现有资料覆盖"
    summary_text = _clean(summary, 60) or "相关权威表述"

    if claim_status == "not_checkable":
        return {"balance": "not_checkable", "lean": "none",
                "balance_label": BALANCE_LABELS["not_checkable"],
                "lean_label": LEAN_LABELS["none"],
                "conclusion": "这不是可核查的事实主张，不做证据倾向判断。"}

    if decisive_supports and decisive_refutes:
        balance = "contested"
    elif decisive_supports:
        balance = "supported"
    elif decisive_refutes:
        balance = "refuted"
    else:
        balance = "insufficient"

    if reliable_supports and reliable_refutes:
        lean = "mixed"
    elif reliable_supports:
        lean = "supports"
    elif reliable_refutes:
        lean = "refutes"
    else:
        lean = "none"

    if balance == "supported":
        conclusion = f"在{scope_text}范围内，权威资料可以证明该说法：{summary_text}。"
    elif balance == "refuted":
        conclusion = f"在{scope_text}范围内，权威资料可以直接反驳该说法：{summary_text}。"
    elif balance == "contested":
        conclusion = "权威资料之间存在直接冲突，目前既不能证明也不能反驳。"
    elif lean == "supports":
        conclusion = "现有可靠证据倾向支持，但缺少权威确认，不能据此证明。"
    elif lean == "refutes":
        conclusion = "现有可靠证据倾向反驳，但缺少权威确认，不能据此反驳。"
    else:
        conclusion = "没有找到能证明或反驳该说法的权威资料（或可靠证据互相矛盾）。"

    return {"balance": balance, "lean": lean,
            "balance_label": BALANCE_LABELS[balance],
            "lean_label": LEAN_LABELS[lean],
            "conclusion": conclusion}


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class AuthoritySkills:
    """权威核验流水线（预检 → 拆主张 → 权威源发现 → 检索/读页/抽引文 → 决策 → 分析 → 报告）。

    全部 IO 可注入：`llm_fn(system, user) -> (obj, err)`（chat_json 契约）、
    `search_fn(query, k) -> {"results": [...], "degraded": bool, "error": str?}`、
    `fetch_fn(url, *, allowed_domains, cancel_check, deadline) -> transport.fetch 形状`。
    `clock` 只负责时间戳（ISO 串）；预算/死线用 `time.monotonic()`，与 CollectBudget 同基准。
    """

    def __init__(self, *, rules: Optional[Any] = None,
                 search_fn: Optional[Callable[[str, int], Any]] = None,
                 fetch_fn: Optional[Callable[..., Any]] = None,
                 llm_fn: Optional[Callable[[str, str], Any]] = None,
                 transport: Optional[Any] = None,
                 clock: Optional[Callable[[], str]] = None,
                 preferences: Optional[Mapping[str, Any]] = None,
                 authorities: Optional[Any] = None) -> None:
        self.rules = _load_rules(rules)
        self.authority_config = _authority_config(authorities)
        self.search_fn = search_fn
        self.fetch_fn = fetch_fn
        self.llm_fn = llm_fn
        self.transport = transport
        self.now = clock or utc_now_iso
        self.preferences = dict(preferences) if isinstance(preferences, Mapping) else {}
        self._fetch_probed = fetch_fn is not None

    # -- 入口 ---------------------------------------------------------------
    def run(self, text: str, *, mode: str = "pipeline",
            prior: Optional[Mapping[str, Any]] = None, followup: bool = False,
            origin: str = "user", claim_context: str = "",
            cancel_check: Optional[Callable[[], bool]] = None,
            deadline: Optional[float] = None,
            on_progress: Optional[Callable[[str, int, int], None]] = None) -> Dict[str, Any]:
        """跑一次权威核验，返回契约 §2 的统一外壳（`data` 见 §6.2）。"""
        started_at = self.now()
        st = self._new_state(cancel_check, deadline, text, origin, mode)
        st.on_progress = on_progress
        st.claim_context = _clean(claim_context, 500)
        base = self._prior_base(prior) if followup else None

        self._stage(st, "precheck")
        if not self._precheck(st, base):
            return self._finish(st, started_at)
        if st.is_cancelled():
            st.gap("运行在读入原文后被取消，未开始核验")
            return self._finish(st, started_at)

        if base is not None:
            self._load_prior(st, base)
        if st.raw_text.strip() and not self._same_as_prior(st, base):
            self._stage(st, "claims")
            if not self._split_claims(st, append=base is not None):
                return self._finish(st, started_at)
        elif base is None:
            self._stage(st, "claims")
            if not self._split_claims(st, append=False):
                return self._finish(st, started_at)

        targets = self._retrieval_targets(st)
        if not targets:
            if not any(c["claim_status"] == "checkable" for c in st.claims):
                st.no_factual_claim()
            self._finalize_claims(st)
            return self._finish(st, started_at)

        if st.mode == "agent":
            self._run_agent(st, targets)
        else:
            self._run_pipeline(st, targets)
        self._stage(st, "verdict")
        self._finalize_claims(st)
        if st.hard_failed:
            return self._finish(st, started_at)

        self._stage(st, "analysis")
        self._analyze(st)
        return self._finish(st, started_at)

    # -- 状态与预检 ---------------------------------------------------------
    def _new_state(self, cancel_check: Optional[Callable[[], bool]],
                   deadline: Optional[float], text: Any, origin: Any,
                   mode: Any) -> CollectRunState:
        """建运行态并把内部工作字段挂上去（CollectRunState 只负责状态/审计/预算）。"""
        rules = self.rules
        if deadline is None:
            deadline = time.monotonic() + float(rules["total_deadline_s"])
        budget = CollectBudget(rules, deadline=deadline, clock=time.monotonic)
        st = CollectRunState(budget, cancel_check, time.monotonic)
        st.cancel_check = cancel_check
        st.raw_text = text if isinstance(text, str) else ("" if text is None else str(text))
        st.origin = origin if origin in ORIGINS else "user"
        st.mode = self._resolve_mode(mode)
        st.claim_context = ""
        st.on_progress = None
        st.claims = []
        st.claims_assessed = False
        st.evidence_internal = []
        st.evidence_seq = 1
        st.pages = {}
        st.page_infos = {}
        st.dynamic_done = {}
        st.dynamic_probes = 0
        st.search_ok = 0
        st.search_failed = 0
        st.hard_failed = False
        st.incremental = False
        st.base_as_of = ""
        st.stage_log = []
        st.notes = []
        st.agent_result = None
        st.link_url = ""
        st.data.update({
            "claims": [],
            "evidence": [],
            "sources": {"verified": [], "unverified": []},
            "conflicts": [],
            "analysis": [],
            "limitations": [],
        })
        return st

    def _resolve_mode(self, mode: Any) -> str:
        """模式解析：调用方给了合法值就用它；否则读偏好；再不行退 pipeline。"""
        value = str(mode or "").strip().lower()
        if value in MODES:
            return value
        authority = self.preferences.get("authority")
        if isinstance(authority, Mapping):
            preferred = str(authority.get("workflow_mode") or "").strip().lower()
            if preferred in MODES:
                return preferred
        return "pipeline"

    def _precheck(self, st: CollectRunState, base: Optional[Dict[str, Any]]) -> bool:
        """预检：非空、不超长、纯链接先安全抓取（抓不到就请用户贴原文，不猜）。"""
        limit = int(self.rules["max_input_chars"])
        raw = st.raw_text.strip()
        if not raw and base is None:
            st.error("precheck", "empty_input", "未提供需要核验的文本")
            st.clarify("请提供需要核验的说法或原文（也可以直接给一个可公开访问的链接）")
            self._progress(st, "precheck", 1, 1)
            return False
        if raw and len(st.raw_text) > limit:
            st.error("precheck", "input_too_long",
                     f"原文 {len(st.raw_text)} 字符，超过上限 {limit}")
            st.clarify(f"原文长度 {len(st.raw_text)} 超过单次上限 {limit}："
                       "请缩小到需要核验的那几句")
            self._progress(st, "precheck", 1, 1)
            return False
        if raw and _LINK_ONLY_RE.match(raw):
            page = self._fetch_link(st, raw)
            if page is None:
                st.clarify("无法读取该链接的正文：请把需要核验的原文直接粘贴过来")
                self._progress(st, "precheck", 1, 1)
                return False
            body = normalize_text(page.get("text"))
            if not body:
                st.error("precheck", "link_empty", "链接正文为空")
                st.clarify("该链接没有可读正文：请把需要核验的原文直接粘贴过来")
                self._progress(st, "precheck", 1, 1)
                return False
            st.raw_text = body[:limit]
            st.origin = "user_link"
            st.link_url = _clean(raw, 300)
            self._note(st, "输入是链接：已按页面正文核验，正文可能不完整")
        self._progress(st, "precheck", 1, 1)
        return True

    def _fetch_link(self, st: CollectRunState, url: str) -> Optional[Dict[str, Any]]:
        """纯链接输入的抓取：只抓这一个地址（不扩链），失败如实记录。"""
        return self._do_fetch(st, url, None, "precheck")

    # -- 增量：上一张卡 -----------------------------------------------------
    def _prior_base(self, prior: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        """从上一张同类型卡里取出可复用的 claims / evidence；不可用返回 None。"""
        if not isinstance(prior, Mapping):
            return None
        data = prior.get("data")
        if not isinstance(data, Mapping):
            return None
        claims = [dict(row) for row in (data.get("claims") or [])
                  if isinstance(row, Mapping) and str(row.get("text") or "").strip()]
        if not claims:
            return None
        evidence = [dict(row) for row in (data.get("evidence") or [])
                    if isinstance(row, Mapping) and str(row.get("evidence_id") or "")]
        as_of = str(prior.get("as_of") or prior.get("finished_at") or "").strip()
        return {"claims": claims, "evidence": evidence, "as_of": as_of}

    def _load_prior(self, st: CollectRunState, base: Mapping[str, Any]) -> None:
        """复用上一张卡：主张/证据原样带过来（旧 evidence_id 保留），只补未定主张。"""
        st.incremental = True
        st.base_as_of = str(base.get("as_of") or "")
        st.claims_assessed = True
        for index, row in enumerate(base.get("claims") or [], start=1):
            claim_id = _clean(row.get("claim_id"), 12) or f"C{index}"
            status = str(row.get("claim_status") or "").strip().lower()
            origin = str(row.get("origin") or "user").strip()
            qualifiers = row.get("qualifiers")
            st.claims.append({
                "claim_id": claim_id,
                "text": _clean(row.get("text"), 300),
                "origin": origin if origin in ORIGINS else "user",
                "claim_status": status if status in ("checkable", "not_checkable")
                else "checkable",
                "qualifiers": dict(qualifiers) if isinstance(qualifiers, Mapping) else {},
                "_queries": _string_items(row.get("queries"), 8, 80),
                "_anchor": row.get("source_span") if isinstance(row.get("source_span"),
                                                                 Mapping) else None,
                "_from_prior": True,
                "_prior_balance": str(row.get("balance") or "insufficient"),
            })
        for row in base.get("evidence") or []:
            item = self._normalize_prior_evidence(row)
            if item is not None:
                st.evidence_internal.append(item)
                st.evidence_seq = max(st.evidence_seq, self._next_seq(item["evidence_id"]))
        if not st.base_as_of:
            self._note(st, "上一张卡没有可用的截止时点，增量基线按空处理")

    def _normalize_prior_evidence(self, row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """把上一张卡的证据行归一成本轮内部形状（缺失字段补保守值）。"""
        evidence_id = _clean(row.get("evidence_id"), 16)
        if not evidence_id:
            return None
        relation = str(row.get("relation") or "").strip().lower()
        verification = str(row.get("verification") or "").strip()
        authority_class = str(row.get("authority_class") or "").strip()
        item: Dict[str, Any] = {
            "evidence_id": evidence_id,
            "claim_ids": [_clean(cid, 12) for cid in (row.get("claim_ids") or []) if str(cid)],
            "relation": relation if relation in RELATIONS else "unclear",
            "source_id": _clean(row.get("source_id"), 60),
            "name": _clean(row.get("name"), 80),
            "domain": _clean(row.get("domain"), 80),
            "tier": _clean(row.get("tier"), 30),
            "authority_class": authority_class if authority_class in AUTHORITY_CLASSES
            else "unverified",
            "verification": verification if verification in VERIFICATIONS else "unverified",
            "authority_basis": _string_items(row.get("authority_basis"), 4, 160),
            "title": _clean(row.get("title"), 120),
            "url": _clean(row.get("url"), 300),
            "quote": normalize_text(row.get("quote"))[:int(self.rules["max_quote_chars"])],
            "published_at": _clean(row.get("published_at"), 40) or None,
            "fetched_at": _clean(row.get("fetched_at"), 40) or "",
            "independence_group": _clean(row.get("independence_group"), 40)
            or _group_of(row.get("quote")),
            "limitations": _string_items(row.get("limitations"), 4, 160),
            "_info": None,
        }
        item["_eligible"] = self._eligible_from_fields(item)
        return item

    @staticmethod
    def _next_seq(evidence_id: str) -> int:
        """E12 → 13；非数字编号不参与序号推进。"""
        digits = re.sub(r"\D", "", str(evidence_id or ""))
        try:
            return int(digits) + 1 if digits else 1
        except ValueError:
            return 1

    def _eligible_from_fields(self, item: Mapping[str, Any]) -> bool:
        """按存储字段重算“类别是否够格”（动态核验的佐证另算）。"""
        decisive = set(self.authority_config.get("decisive_classes") or [])
        if item.get("authority_class") not in decisive:
            return False
        return item.get("verification") in _ANCHOR_VERIFICATIONS

    @staticmethod
    def _same_as_prior(st: CollectRunState, base: Optional[Mapping[str, Any]]) -> bool:
        """原文是否就是上一张卡里的主张文本（是则不再重复拆主张）。"""
        if base is None or not st.raw_text.strip():
            return False
        prior_text = " ".join(normalize_text(row.get("text")) for row in base.get("claims") or [])
        return normalize_text(st.raw_text) in prior_text

    # -- 拆主张 -------------------------------------------------------------
    def _split_claims(self, st: CollectRunState, *, append: bool) -> bool:
        """复用 evidence/SKILL.md 指令拆主张；text 必须逐字定位原文。"""
        cap = int(self.rules["max_claims"])
        contract = (
            '只输出 JSON：{"claims":[{"text":"<原文逐字片段>",'
            '"claim_status":"checkable|not_checkable",'
            '"qualifiers":{"time":null,"place":null,"entity":null},'
            '"queries":["<中性检索词>"]}]}；'
            f"最多 {cap} 条；text 必须逐字来自原文（含标点），"
            "queries 不许带 site:/inurl:/intitle:/filetype: 等操作符。")
        user = f"用户原文：\n{st.raw_text}"
        if st.claim_context:
            user += f"\n\n补充背景（只帮助理解，不能当证据）：\n{st.claim_context}"
        obj, err = self._call_llm(st, load_skill_prompt(_EVIDENCE_DIR, contract),
                                  user, "claims")
        rows = _first_list(obj, ("claims", "items")) if isinstance(obj, Mapping) else None
        if not isinstance(rows, list):
            if err == "llm_budget_exhausted":
                st.gap("模型调用预算已达上限，主张拆分未执行")
                return False
            st.error("claims", "claims_unavailable", err or "模型未给出可解析的主张拆分")
            self._hard_fail(st, "拆主张阶段模型不可用：" + (err or "模型输出不可解析"))
            return False

        if len(rows) > cap:
            st.error("claims", "too_many_claims",
                     f"识别出 {len(rows)} 条主张，超过单次上限 {cap}")
            st.gap(f"主张数超过单次上限 {cap}：仅核验前 {cap} 条，其余未覆盖")
            rows = rows[:cap]

        normalized = normalize_text(st.raw_text)
        existing = {normalize_text(claim["text"]) for claim in st.claims}
        seq = max([self._next_seq(claim["claim_id"]) for claim in st.claims] or [1])
        kept = 0
        for row in rows[:cap]:
            if not isinstance(row, Mapping):
                continue
            text = _clean(row.get("text"), 300)
            if not text:
                st.error("claims", "claim_empty", "主张文本为空，已丢弃")
                continue
            if text in existing:
                st.error("claims", "duplicate_claim", f"主张与已有条目重复，已跳过：{text[:60]}")
                continue
            span = anchor_quote(normalized, text)
            if span is None:
                st.error("claims", "claim_not_anchored",
                         f"主张未在原文中逐字定位，已丢弃：{text[:60]}")
                continue
            status = str(row.get("claim_status") or "").strip().lower()
            if status not in ("checkable", "not_checkable"):
                st.error("claims", "bad_claim_status",
                         f"未知主张状态：{status or '（空）'}，按不可核查处理")
                status = "not_checkable"
            qualifiers = row.get("qualifiers")
            qualifiers = qualifiers if isinstance(qualifiers, Mapping) else {}
            st.claims.append({
                "claim_id": f"C{seq}",
                "text": text,
                "origin": st.origin,
                "claim_status": status,
                "qualifiers": {"time": _clean(qualifiers.get("time"), 60) or None,
                               "place": _clean(qualifiers.get("place"), 60) or None,
                               "entity": _clean(qualifiers.get("entity"), 60) or None},
                "_queries": _string_items(row.get("queries"), 8, 80),
                "_anchor": span,
                "_from_prior": False,
                "_prior_balance": "",
            })
            existing.add(text)
            seq += 1
            kept += 1
        st.claims_assessed = True
        if append:
            st.gap(f"增量核验：新增 {kept} 条主张，旧主张与旧证据保留")
        if not st.claims:
            st.no_factual_claim()
            self._note(st, "未能从原文拆出可定位的主张")
        self._progress(st, "claims", 1, 1)
        return True

    def _retrieval_targets(self, st: CollectRunState) -> List[Dict[str, Any]]:
        """要检索的主张：新增的 + 上一张卡里 balance 未定（insufficient）的。"""
        targets: List[Dict[str, Any]] = []
        for claim in st.claims:
            if claim["claim_status"] != "checkable":
                continue
            if claim.get("_from_prior") and claim.get("_prior_balance") not in (
                    "insufficient", "", None):
                continue
            targets.append(claim)
        return targets

    # -- 模式 A：固定阶段 ---------------------------------------------------
    def _run_pipeline(self, st: CollectRunState, targets: List[Dict[str, Any]]) -> None:
        """逐主张跑：目录候选站内检索 → 动态/目录外发现 → 读页 → 抽引文。"""
        total = len(targets)
        for index, claim in enumerate(targets, start=1):
            if st.is_cancelled() or st.budget.blocked_reason:
                break
            self._retrieve_claim(st, claim)
            self._progress(st, "evidence", index, total)
        if (targets and st.search_ok == 0 and st.search_failed > 0
                and not st.evidence_internal):
            self._hard_fail(st, "外部检索未授权或检索后端全部失败，未取得任何证据")
        elif targets and st.search_ok == 0 and self.search_fn is None:
            self._hard_fail(st, "未配置检索实现（检索未授权），无法发现权威来源")

    def _retrieve_claim(self, st: CollectRunState, claim: Mapping[str, Any]) -> None:
        """单条主张的证据检索（确定性顺序，便于复现与测试）。"""
        queries = self._claim_queries(st, claim)
        if not queries:
            st.gap(f"主张 {claim['claim_id']} 脱敏后没有可用检索词")
            return
        per_claim_pages = int(self.rules["per_claim_max_pages"])
        pages_used = 0
        seen_urls = set()

        # 1) 目录锚点候选：代码拼 site:，逐域取第一条站内结果
        for entry in self._catalog_pool(claim["text"])[
                :int(self.rules["max_catalog_sources_per_claim"])]:
            if pages_used >= per_claim_pages or st.budget.blocked_reason or st.is_cancelled():
                break
            urls = self._site_urls(st, entry["domain"], queries, claim["claim_id"])
            if not urls:
                continue
            url = urls[0]
            key = url_key(url)
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            page = self._page_for(st, url, entry["domain"], "evidence")
            if page is None:
                continue
            pages_used += 1
            st.page_infos[key] = entry["info"]
            self._record_evidence(st, claim, page, entry["info"])

        # 2) 动态核验 / 目录外发现：先分类，再看是否够格升格
        for cand in self._discover(st, claim, queries):
            if pages_used >= per_claim_pages or st.budget.blocked_reason or st.is_cancelled():
                break
            key = url_key(cand["url"])
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            page = cand.get("page") or self._page_for(st, cand["url"], cand["domain"], "evidence")
            if page is None:
                continue
            pages_used += 1
            info = cand["info"]
            st.page_infos[key] = info
            self._record_evidence(st, claim, page, info)

        found = any(claim["claim_id"] in item["claim_ids"]
                    for item in st.evidence_internal)
        if not found:
            st.gap(f"主张 {claim['claim_id']} 尚未找到可定位的权威引文")

    def _claim_queries(self, st: CollectRunState, claim: Mapping[str, Any]) -> List[str]:
        """检索词：主张原文 + 中性改写；先脱敏再剥范围操作符，按上限截断。"""
        cap = int(self.rules["per_claim_max_queries"])
        out: List[str] = []
        for raw in [claim.get("text")] + list(claim.get("_queries") or []):
            query = _clean_query(raw)
            if query and query not in out:
                out.append(query)
            if len(out) >= cap:
                break
        return out

    def _site_urls(self, st: CollectRunState, domain: str, queries: Sequence[str],
                   claim_id: str) -> List[str]:
        """在单个权威域内检索：`site:` 由代码拼，返回域内前几条 URL。"""
        for query in queries:
            if st.budget.blocked_reason or st.is_cancelled():
                return []
            full_query = f"site:{domain} {query}".strip()
            results = self._do_search(st, full_query, 6, "authority")
            if results is None:
                continue
            urls = [str(row.get("url")) for row in results[:6]
                    if isinstance(row, Mapping) and row.get("url")
                    and _within(_host_of(row.get("url")), domain)]
            if urls:
                return urls
        self._note(st, f"主张 {claim_id} 在权威源 {domain} 未检索到站内结果")
        return []

    def _discover(self, st: CollectRunState, claim: Mapping[str, Any],
                  queries: Sequence[str]) -> List[Dict[str, Any]]:
        """一次中性检索发现目录外候选；够格的走动态核验，不够格的只作线索。"""
        results = self._do_search(st, queries[0], 8, "discover")
        if not results:
            return []
        out: List[Dict[str, Any]] = []
        for row in results[:8]:
            if not isinstance(row, Mapping):
                continue
            url = _clean(row.get("url"), 300)
            if not url:
                continue
            info = classify_source(url, authority_config=self.authority_config)
            if info.authority_class in set(self.authority_config.get("decisive_classes") or []):
                out.append({"url": url, "info": info, "domain": info.domain, "page": None})
                continue
            if info.authority_class == "media":
                out.append({"url": url, "info": info, "domain": info.domain, "page": None})
                continue
            info, page = self._dynamic_info(st, url, None, "discover")
            if page is None:
                continue
            out.append({"url": url, "info": info, "domain": info.domain, "page": page})
        return out

    def _dynamic_info(self, st: CollectRunState, url: str, page: Optional[Mapping[str, Any]],
                      stage: str) -> Tuple[SourceInfo, Optional[Dict[str, Any]]]:
        """目录外候选的动态核验：≥2 个代码信号才记 session_verified（仅会话内）。"""
        info = classify_source(url, authority_config=self.authority_config)
        host = info.domain
        if host in st.dynamic_done:
            cached = st.dynamic_done[host]
            if page is None:
                page = st.pages.get(url_key(url))
            return cached, page
        enabled = (bool(self.rules.get("dynamic_authority", True))
                   and bool(self.authority_config.get("dynamic_enabled", True)))
        try:
            max_domains = max(1, int(self.authority_config.get("max_dynamic_domains", 4)))
        except (TypeError, ValueError):
            max_domains = 4
        if not enabled or st.dynamic_probes >= max_domains:
            return info, page
        if page is None:
            page = self._page_for(st, url, host, stage)
        if page is None:
            return info, None
        st.dynamic_probes += 1
        signals = dynamic_authority_signals(
            url, [page], authority_config=self.authority_config,
            verified_pages=self._verified_pages(st))
        if signals.get("verified"):
            info = replace(info,
                           authority_class=str(signals.get("authority_class") or "official"),
                           verification="session_verified",
                           verification_basis=str(signals.get("basis") or ""),
                           anchor=True)
        st.dynamic_done[host] = info
        return info, page

    def _verified_pages(self, st: CollectRunState) -> List[Dict[str, Any]]:
        """已读且来源已核验的页面（动态信号的 inbound_link / cross_mention 依据）。"""
        out: List[Dict[str, Any]] = []
        for key, page in st.pages.items():
            info = st.page_infos.get(key)
            if info is not None and info.verification in _ANCHOR_VERIFICATIONS:
                out.append(page)
        return out

    def _catalog_pool(self, claim_text: Any = "") -> List[Dict[str, Any]]:
        """目录候选池：skills/sources.json + config/sources_registry.json 的权威档位。

        排序：决定性类别优先 → 与主张的主题重叠高优先 → 档位高优先 → 目录原序。
        只保留能进证据链的类别（决定性五类 + media）；unverified 目录条目不当候选。
        """
        decisive = set(self.authority_config.get("decisive_classes") or [])
        rows = self._catalog_rows()
        scored: List[Tuple[int, float, int, Dict[str, Any]]] = []
        seen: set = set()
        for index, row in enumerate(rows):
            domain = str(row.get("domain") or "").strip().lower().rstrip(".")
            if not domain or domain in seen:
                continue
            seen.add(domain)
            info = classify_source(domain, authority_config=self.authority_config)
            if info.authority_class in decisive:
                bucket = 0
            elif info.authority_class == "media":
                bucket = 1
            else:
                continue
            hay = " ".join([str(row.get("name") or ""), domain,
                            " ".join(str(x) for x in (row.get("material_types") or [])),
                            " ".join(str(x) for x in (row.get("domains") or []))])
            scored.append((bucket, -_relevance(claim_text, hay), index,
                           {"domain": info.domain, "info": info,
                            "name": info.name or domain}))
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in scored]

    def _catalog_rows(self) -> List[Dict[str, Any]]:
        """读目录：sources.json 的 sources 数组 + 注册表里未收录的官方/可靠域名。"""
        rows = [row for row in (_read_json(_SOURCES_PATH).get("sources") or [])
                if isinstance(row, Mapping)]
        registry = _read_json(_REGISTRY_PATH)
        known = {str(row.get("domain") or "").strip().lower().rstrip(".")
                 for row in rows}
        for key in ("official_tiers", "reliable_tiers"):
            names = registry.get(key)
            if not isinstance(names, list):
                continue
            tiers = registry.get("tiers") if isinstance(registry.get("tiers"), Mapping) else {}
            for name in names:
                domains = tiers.get(name) if isinstance(tiers, Mapping) else None
                if not isinstance(domains, list):
                    continue
                for domain in domains:
                    host = str(domain or "").strip().lower().rstrip(".")
                    if not host or "." not in host or host in known:
                        continue
                    known.add(host)
                    rows.append({"domain": host, "name": host, "anchor": True})
        return rows

    # -- 模式 B：受限 Agent 循环 --------------------------------------------
    def _run_agent(self, st: CollectRunState, targets: List[Dict[str, Any]]) -> None:
        """claim split 之后进入 §7 受限循环；结论仍由确定性收口生成。"""
        pool: Dict[str, Dict[str, Any]] = {}
        for entry in self._catalog_pool(" ".join(claim["text"] for claim in targets))[:6]:
            url = f"https://{entry['domain']}/"
            pool[url_key(url)] = {"url": url, "domain": entry["domain"],
                                  "info": entry["info"]}
        pages: Dict[str, Dict[str, Any]] = {}

        contract = (
            "每步只输出一个 JSON："
            '{"action":"search|read|extract|finish","args":{...},"reason":"一句话"}。'
            "read.url 必须在候选池；extract.quote 必须逐字来自已读正文；"
            "extract 可带 claim_id/relation（supports|refutes|partial|background|unclear）。")
        system_prompt = load_skill_prompt(_HERE / "agent", contract)
        goal = ("核验以下主张，只用候选池里的权威来源：\n"
                + "\n".join(f"- {claim['claim_id']}：{claim['text']}" for claim in targets))
        max_steps = int(self.rules["agent_max_steps"])
        st.agent_result = run_agent_loop(
            llm_fn=lambda system, user: self._call_llm(st, system, user, "agent"),
            system_prompt=system_prompt,
            goal=goal,
            describe_state=lambda: self._agent_describe(st, targets, pool, pages),
            apply_action=lambda action, args: self._agent_apply(st, targets, pool, pages,
                                                                action, args),
            max_steps=max_steps,
            budget=st.budget,
            state=st,
            on_step=lambda payload: self._progress(st, "agent",
                                                   int(payload.get("n") or 0), max_steps),
        )
        result = st.agent_result
        if result.get("status") != "completed":
            st.gap(f"受限循环提前结束（{result.get('stop_reason') or 'unknown'}），"
                   "已保留此前的证据")
        if not any(claim["claim_id"] in item["claim_ids"]
                   for item in st.evidence_internal for claim in targets):
            st.gap("受限循环未产出任何可定位引文")
        if st.search_ok == 0 and st.search_failed > 0 and not st.evidence_internal:
            self._hard_fail(st, "外部检索未授权或检索后端全部失败，未取得任何证据")

    def _agent_describe(self, st: CollectRunState, targets: Sequence[Mapping[str, Any]],
                        pool: Mapping[str, Dict[str, Any]],
                        pages: Mapping[str, Dict[str, Any]]) -> str:
        """每步给模型看的程序状态（候选池/已读/已摘/剩余预算）。"""
        lines: List[str] = ["待核验主张："]
        for claim in targets:
            lines.append(f"- {claim['claim_id']}：{claim['text']}")
        lines.append("候选池（可 read 的 URL）：")
        if pool:
            for item in list(pool.values())[:12]:
                lines.append(f"- {item['url']}（{item['domain']}）")
        else:
            lines.append("- （空：先 search 或使用目录候选）")
        lines.append(f"已读页面：{len(pages)} 页")
        for key, page in list(pages.items())[:6]:
            title = _clean(page.get("title"), 60) or _clean(page.get("url"), 80)
            lines.append(f"- {title}")
        evidence = st.evidence_internal
        lines.append(f"已摘证据：{len(evidence)} 条")
        for item in evidence[-6:]:
            lines.append("  " + render_evidence_line(item))
        lines.append(f"剩余预算：搜索 {st.budget.remaining('max_searches')}、"
                     f"读页 {st.budget.remaining('max_pages')}、"
                     f"模型调用 {st.budget.remaining('max_llm_calls')}")
        return "\n".join(lines)

    def _agent_apply(self, st: CollectRunState, targets: Sequence[Mapping[str, Any]],
                     pool: Dict[str, Dict[str, Any]], pages: Dict[str, Dict[str, Any]],
                     action: str, args: Mapping[str, Any]) -> Dict[str, Any]:
        """执行一步动作：候选池/锚定/预算校验都在这里（引擎不碰 IO）。"""
        if action == "search":
            query = _clean_query(args.get("query"))
            if not query:
                return {"ok": False, "observation": "", "error": "empty_query"}
            results = self._do_search(st, query, 6, "agent:search")
            if results is None:
                return {"ok": False, "observation": "", "error": "search_unavailable"}
            added = 0
            for row in results[:6]:
                if not isinstance(row, Mapping):
                    continue
                url = _clean(row.get("url"), 300)
                key = url_key(url)
                if not key or key in pool:
                    continue
                info = classify_source(url, authority_config=self.authority_config)
                pool[key] = {"url": url, "domain": info.domain, "info": info}
                added += 1
            listing = "；".join(item["url"] for item in
                                (list(pool.values())[-added:] if added else [])) or "无新增"
            return {"ok": True, "observation": f"检索到 {len(results)} 条，新增 {added} 个候选："
                                               f"{listing}", "step_label": "检索"}
        if action == "read":
            url = _clean(args.get("url"), 300)
            key = url_key(url)
            if not key or key not in pool:
                return {"ok": False, "observation": "",
                        "error": "url_not_in_pool（只能读候选池里的 URL）"}
            page = self._page_for(st, url, pool[key]["domain"], "agent:read")
            if page is None:
                return {"ok": False, "observation": "", "error": "fetch_failed"}
            pages[key] = page
            st.page_infos[key] = pool[key]["info"]
            excerpt = _clean(page.get("text"), 240)
            return {"ok": True, "observation": f"已读 {url}；正文摘要：{excerpt}",
                    "step_label": "读页"}
        if action == "extract":
            return self._agent_extract(st, targets, pool, pages, args)
        if action == "finish":
            return {"ok": True, "observation": "收尾", "done": True, "step_label": "收尾"}
        return {"ok": False, "observation": "", "error": "unknown_action"}

    def _agent_extract(self, st: CollectRunState, targets: Sequence[Mapping[str, Any]],
                       pool: Mapping[str, Dict[str, Any]],
                       pages: Mapping[str, Dict[str, Any]],
                       args: Mapping[str, Any]) -> Dict[str, Any]:
        """extract：引文必须锚定已读正文；来源资格仍由代码判定。"""
        url = _clean(args.get("url"), 300)
        key = url_key(url)
        if not key or key not in pages:
            return {"ok": False, "observation": "",
                    "error": "url_not_read（必须先 read 再 extract）"}
        claim = self._pick_claim(targets, args.get("claim_id"))
        if claim is None:
            return {"ok": False, "observation": "", "error": "no_checkable_claim"}
        quote = args.get("quote")
        relation = str(args.get("relation") or "unclear").strip().lower()
        if relation not in RELATIONS:
            relation = "unclear"
        page = pages[key]
        info = pool[key]["info"] if key in pool else None
        if info is None or info.verification == "unverified":
            info, page = self._dynamic_info(st, url, page, "agent:extract")
        item = self._make_evidence(st, claim, page, info, quote, relation, [])
        if item is None:
            return {"ok": False, "observation": "", "error": "quote_not_anchored"}
        return {"ok": True, "observation": f"已记录 {item['evidence_id']}"
                                           f"（{_RELATION_LABELS.get(relation, relation)}）",
                "step_label": "抽引文"}

    @staticmethod
    def _pick_claim(targets: Sequence[Mapping[str, Any]], claim_id: Any
                    ) -> Optional[Mapping[str, Any]]:
        """动作里的 claim_id：命中就用它；没给就落到第一条可核查主张。"""
        wanted = _clean(claim_id, 12)
        for claim in targets:
            if wanted and claim["claim_id"] == wanted:
                return claim
        if wanted:
            return None
        return targets[0] if targets else None

    # -- 证据抽取与记录 -----------------------------------------------------
    def _record_evidence(self, st: CollectRunState, claim: Mapping[str, Any],
                         page: Mapping[str, Any], info: SourceInfo) -> Optional[Dict[str, Any]]:
        """让模型从一页正文里摘一句引文并给关系；锚定失败即丢弃。"""
        body = normalize_text(page.get("text"))[:int(self.rules["max_text_chars"])]
        if not body:
            st.error("evidence", "empty_body", "页面正文为空，未抽取引文")
            return None
        contract = (
            '只输出 JSON：{"quote":"<逐字来自已读正文的原句>",'
            '"relation":"supports|refutes|partial|background|unclear",'
            '"limitations":["<该条证据的局限>"]}。'
            "quote 必须逐字出现在正文里；relation 只能取这五个值；找不到就返回空 quote。")
        user = (f"主张：{claim['text']}\n\n已读正文（{page.get('final_url') or page.get('url')}）：\n"
                f"{body}")
        obj, err = self._call_llm(st, load_skill_prompt(_HERE / "adjudicate", contract),
                                  user, f"extract:{claim['claim_id']}")
        if obj is None:
            st.gap(f"主张 {claim['claim_id']} 的引文抽取未执行（{err or '模型不可用'}）")
            return None
        quote = obj.get("quote")
        if not normalize_text(quote):
            st.gap(f"主张 {claim['claim_id']} 在 {info.domain} 未摘到可定位引文")
            return None
        relation = str(obj.get("relation") or "").strip().lower()
        if relation not in RELATIONS:
            st.error("evidence", "bad_relation",
                     f"未知关系：{relation or '（空）'}，按 unclear 处理")
            relation = "unclear"
        limitations = _string_items(obj.get("limitations"), 4, 160)
        if len(normalize_text(quote)) > int(self.rules["max_quote_chars"]):
            limitations.append("引文超长，已截断到上限后再定位")
        return self._make_evidence(st, claim, page, info, quote, relation, limitations)

    def _make_evidence(self, st: CollectRunState, claim: Mapping[str, Any],
                       page: Mapping[str, Any], info: SourceInfo, quote: Any,
                       relation: str, limitations: Sequence[str]) -> Optional[Dict[str, Any]]:
        """校验引文锚定并落库（关系/类别/核验方式全部封闭枚举）。"""
        body = page.get("text")
        max_quote = int(self.rules["max_quote_chars"])
        quote_text = normalize_text(quote)[:max_quote]
        span = anchor_quote(body, quote_text)
        if span is None:
            st.error("evidence", "quote_not_anchored",
                     f"引文未逐字出现在已读正文，已丢弃：{quote_text[:60]}")
            return None
        for item in st.evidence_internal:
            if (claim["claim_id"] in item["claim_ids"]
                    and normalize_text(item["quote"]) == quote_text
                    and item["domain"] == info.domain):
                st.error("evidence", "duplicate_quote", "同一主张同一来源的重复引文，已跳过")
                return None
        evidence_id = f"E{st.evidence_seq}"
        st.evidence_seq += 1
        limitations_out = list(dict.fromkeys(_clean(x, 160) for x in limitations if _clean(x, 160)))
        if len(normalize_text(body)) > int(self.rules["max_text_chars"]):
            limitations_out.append("正文超长，仅读取并引用前段内容")
        basis = [info.verification_basis] if info.verification_basis else []
        if info.authority_class:
            basis.append(f"权威类别：{info.authority_class}")
        item: Dict[str, Any] = {
            "evidence_id": evidence_id,
            "claim_ids": [claim["claim_id"]],
            "relation": relation,
            "source_id": info.source_id,
            "name": info.name,
            "domain": info.domain or _host_of(page.get("final_url") or page.get("url")),
            "tier": info.tier,
            "authority_class": info.authority_class,
            "verification": info.verification,
            "authority_basis": basis,
            "title": _clean(page.get("title"), 120) or info.name,
            "url": _clean(page.get("final_url") or page.get("url"), 300),
            "quote": quote_text,
            "published_at": _clean(page.get("published_at"), 40) or None,
            "fetched_at": _clean(page.get("fetched_at"), 40) or self.now(),
            "independence_group": _group_of(body),
            "limitations": limitations_out,
            "_info": info,
            "_eligible": is_decisive_source(info, authority_config=self.authority_config,
                                            corroborated=True),
        }
        st.evidence_internal.append(item)
        return item

    # -- 决策与收口 ---------------------------------------------------------
    def _finalize_claims(self, st: CollectRunState) -> None:
        """按决策表算每条主张的 balance/lean/conclusion（代码算，模型不参与）。"""
        config = self.authority_config
        requires = bool(config.get("dynamic_requires_corroboration", True))
        for claim in st.claims:
            peers = [item for item in st.evidence_internal
                     if claim["claim_id"] in item["claim_ids"]]
            decisive_supports: List[Dict[str, Any]] = []
            decisive_refutes: List[Dict[str, Any]] = []
            reliable_supports: List[Dict[str, Any]] = []
            reliable_refutes: List[Dict[str, Any]] = []
            for item in peers:
                corroborated, _basis = self._corroboration(st, item, peers)
                decisive = bool(item.get("_eligible")) and (
                    item["verification"] != "session_verified"
                    or corroborated or not requires)
                if decisive and item["relation"] == "supports":
                    decisive_supports.append(item)
                elif decisive and item["relation"] == "refutes":
                    decisive_refutes.append(item)
                if item["verification"] in _ANCHOR_VERIFICATIONS:
                    if item["relation"] == "supports":
                        reliable_supports.append(item)
                    elif item["relation"] == "refutes":
                        reliable_refutes.append(item)
            summary_source = (decisive_supports or decisive_refutes or reliable_supports
                              or reliable_refutes)
            summary = normalize_text(summary_source[0]["quote"]) if summary_source else ""
            scope = self._scope_text(claim, decisive_supports + decisive_refutes)
            verdict = decide_verdict(
                claim["claim_status"], len(decisive_supports), len(decisive_refutes),
                len(reliable_supports), len(reliable_refutes), scope=scope, summary=summary)
            supporting_ids = [item["evidence_id"] for item in decisive_supports]
            refuting_ids = [item["evidence_id"] for item in decisive_refutes]
            context_ids = [item["evidence_id"] for item in peers
                           if item["evidence_id"] not in supporting_ids + refuting_ids]
            basis: List[str] = []
            for item in (decisive_supports + decisive_refutes) or peers:
                for text in item.get("authority_basis") or []:
                    if text and text not in basis:
                        basis.append(text)
            claim.update({
                "balance": verdict["balance"],
                "balance_label": verdict["balance_label"],
                "lean": verdict["lean"],
                "lean_label": verdict["lean_label"],
                "conclusion": verdict["conclusion"],
                "supporting_ids": supporting_ids,
                "refuting_ids": refuting_ids,
                "context_ids": context_ids,
                "authority_basis": basis[:4],
                "limitations": self._claim_limitations(claim, peers, decisive_supports,
                                                       decisive_refutes),
            })

    def _corroboration(self, st: CollectRunState, item: Mapping[str, Any],
                       peers: Sequence[Mapping[str, Any]]) -> Tuple[bool, str]:
        """动态核验来源的佐证判定：独立已核验同向来源，或两个独立动态来源。"""
        same_dir = [peer for peer in peers
                    if peer["evidence_id"] != item["evidence_id"]
                    and peer["relation"] == item["relation"]
                    and peer["independence_group"] != item["independence_group"]]
        decisive = set(self.authority_config.get("decisive_classes") or [])
        anchored = [peer for peer in same_dir
                    if peer["verification"] in ("catalog_anchor", "verified_site_link")
                    and peer["authority_class"] in decisive
                    and peer["domain"] != item["domain"]]
        if anchored:
            return True, "独立已核验来源交叉佐证"
        dynamic_domains = {peer["domain"] for peer in same_dir
                           if peer["verification"] == "session_verified"
                           and peer["domain"] != item["domain"]}
        if dynamic_domains:
            return True, "两个独立动态来源互相印证"
        return False, ""

    @staticmethod
    def _scope_text(claim: Mapping[str, Any],
                    decisive: Sequence[Mapping[str, Any]]) -> str:
        """结论里的 scope：优先主张限定（时间/地点），其次决定性证据的时点。"""
        qualifiers = claim.get("qualifiers") if isinstance(claim.get("qualifiers"),
                                                           Mapping) else {}
        parts = [str(qualifiers.get(key) or "").strip() for key in ("time", "place")]
        scope = "、".join(part for part in parts if part)
        if scope:
            return scope
        for item in decisive:
            published = str(item.get("published_at") or "").strip()
            if published:
                return f"{published} 的资料"
        return "现有资料覆盖"

    @staticmethod
    def _claim_limitations(claim: Mapping[str, Any], peers: Sequence[Mapping[str, Any]],
                           decisive_supports: Sequence[Mapping[str, Any]],
                           decisive_refutes: Sequence[Mapping[str, Any]]) -> List[str]:
        """单条主张的局限：说清“缺什么”，不写评语。"""
        out: List[str] = []
        if claim["claim_status"] != "checkable":
            out.append("不可核查：观点、情绪、诉求或预测，不做证据倾向判断")
        if not peers:
            out.append("未检索到可定位引文")
        if peers and not decisive_supports and not decisive_refutes:
            out.append("缺少可作决定性证据的权威来源（未核验来源只作线索）")
        if any(item["authority_class"] == "media" for item in peers):
            out.append("权威媒体只能形成证据倾向，不能单独证明或反驳")
        if any(item["verification"] == "session_verified" for item in peers):
            out.append("含本次运行内动态核验来源，仅会话内有效")
        for item in peers:
            for text in item.get("limitations") or []:
                if text and text not in out:
                    out.append(text)
        return out[:5]

    def _analyze(self, st: CollectRunState) -> None:
        """受约束分析：模型只写文字，引用必须落在已存在且属于该主张的 evidence_id。"""
        evidence = st.evidence_internal
        if not evidence:
            return
        claim_by_id = {claim["claim_id"]: claim for claim in st.claims}
        lines: List[str] = []
        for claim in st.claims:
            peers = [item for item in evidence if claim["claim_id"] in item["claim_ids"]]
            if not peers:
                continue
            lines.append(f"- {claim['claim_id']}：{claim['text']}（{claim['balance']}）")
            for item in peers:
                lines.append("  " + render_evidence_line(item))
        if not lines:
            return
        contract = (
            '只输出 JSON：{"analysis":[{"claim_id":"C1","text":"<不超过300字的分析>",'
            '"evidence_ids":["E1"]}]}。'
            "evidence_ids 只能引用上面实际存在的编号，且必须属于该主张；"
            "不要下真假结论、不要写“属实/不属实”、不要给数值分。")
        obj, err = self._call_llm(st, load_skill_prompt(_HERE / "authority", contract),
                                  "待分析的主张与证据：\n" + "\n".join(lines), "analysis")
        rows = _first_list(obj, ("analysis", "items")) if isinstance(obj, Mapping) else None
        if not isinstance(rows, list):
            st.gap("分析未生成（" + (err or "模型未给出可解析分析") + "）")
            return
        kept: List[Dict[str, Any]] = []
        known = {item["evidence_id"]: item for item in evidence}
        for row in rows[:8]:
            if not isinstance(row, Mapping):
                continue
            claim_id = _clean(row.get("claim_id"), 12)
            if claim_id not in claim_by_id:
                st.error("analysis", "unknown_claim",
                         f"分析引用了不存在的主张，已丢弃：{claim_id or '（空）'}")
                continue
            text = _clean(row.get("text"), 300)
            if not text:
                continue
            if any(word in text for word in _VERDICT_WORDS):
                st.error("analysis", "verdict_label",
                         "分析文字里出现真假标签，已丢弃")
                continue
            cited = _string_items(row.get("evidence_ids"), 8, 16)
            valid = [eid for eid in cited
                     if eid in known and claim_id in known[eid]["claim_ids"]]
            dropped = [eid for eid in cited if eid not in valid]
            if dropped:
                st.error("analysis", "unknown_evidence",
                         "分析引用了不存在或不属于该主张的证据，已丢弃："
                         + "、".join(dropped[:4]))
            if not valid:
                st.error("analysis", "analysis_without_evidence",
                         "分析没有任何有效证据引用，已丢弃")
                continue
            kept.append({"claim_id": claim_id, "text": text, "evidence_ids": valid})
        st.data["analysis"] = kept

    # -- 输出组装 -----------------------------------------------------------
    def _publish(self, st: CollectRunState) -> None:
        """把内部工作字段整理成契约 §6.2 的 data 形状（下划线字段不外泄）。"""
        st.data["claims"] = [{key: claim.get(key) for key in _PUBLIC_CLAIM_KEYS}
                             for claim in st.claims]
        st.data["evidence"] = [{key: value for key, value in item.items()
                                if not key.startswith("_")}
                               for item in st.evidence_internal]
        st.data["sources"] = self._build_sources(st)
        st.data["conflicts"] = self._conflicts(st)
        st.data["analysis"] = st.data.get("analysis") or []
        st.data["limitations"] = self._limitations(st)

    def _build_sources(self, st: CollectRunState) -> Dict[str, List[Dict[str, Any]]]:
        """来源清单：已核验 / 未核验分开（未核验只作线索）。"""
        verified: List[Dict[str, Any]] = []
        unverified: List[Dict[str, Any]] = []
        seen: set = set()
        for item in st.evidence_internal:
            key = item.get("domain") or item.get("source_id")
            if not key or key in seen:
                continue
            seen.add(key)
            info = item.get("_info")
            if info is not None:
                row = info.to_dict()
            else:
                row = {"source_id": item.get("source_id", ""),
                       "name": item.get("name", ""),
                       "domain": item.get("domain", ""),
                       "tier": item.get("tier", ""),
                       "source_kind": "",
                       "authority_class": item.get("authority_class", "unverified"),
                       "verification": item.get("verification", "unverified"),
                       "verification_basis": "、".join(item.get("authority_basis") or []),
                       "anchor": item.get("verification") in _ANCHOR_VERIFICATIONS}
            row["evidence_count"] = sum(1 for peer in st.evidence_internal
                                        if peer.get("domain") == key)
            if row.get("verification") in _ANCHOR_VERIFICATIONS:
                verified.append(row)
            else:
                unverified.append(row)
        return {"verified": verified, "unverified": unverified}

    @staticmethod
    def _conflicts(st: CollectRunState) -> List[Dict[str, Any]]:
        """冲突：权威资料之间直接对立，或可靠证据互相矛盾（只列事实，不下结论）。"""
        out: List[Dict[str, Any]] = []
        for claim in st.claims:
            supporting = list(claim.get("supporting_ids") or [])
            refuting = list(claim.get("refuting_ids") or [])
            if claim.get("balance") == "contested":
                out.append({
                    "summary": (f"主张 {claim['claim_id']} 的权威资料之间存在直接冲突："
                                f"支持 {'、'.join(supporting) or '无'}；"
                                f"反驳 {'、'.join(refuting) or '无'}。"),
                    "evidence_ids": supporting + refuting,
                })
                continue
            if claim.get("lean") == "mixed":
                peers = [item["evidence_id"] for item in st.evidence_internal
                         if claim["claim_id"] in item["claim_ids"]
                         and item["verification"] in _ANCHOR_VERIFICATIONS
                         and item["relation"] in ("supports", "refutes")]
                out.append({
                    "summary": f"主张 {claim['claim_id']} 的可靠证据互相矛盾（仅作倾向参考）。",
                    "evidence_ids": peers,
                })
        return out

    def _limitations(self, st: CollectRunState) -> List[str]:
        """整轮运行的局限：增量基线、缺口、无碍观察、未核验来源口径、空证据。"""
        out: List[str] = []
        if st.incremental:
            out.append(f"增量核验：复用上一张卡（截至 {st.base_as_of or '未注明'}），"
                       "仅补查未定主张。")
        for gap in list(st.gaps) + list(getattr(st, "notes", [])):
            if gap and gap not in out:
                out.append(gap)
        if any(item["verification"] == "unverified" for item in st.evidence_internal):
            out.append("未核验来源只作线索，未计入决定性证据。")
        if not st.evidence_internal and st.claims:
            out.append("本次未取得可定位引文。")
        return out[:8]

    # -- IO 适配层 ----------------------------------------------------------
    def _call_llm(self, st: CollectRunState, system: str, user: str,
                  target: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """一次模型调用：记账 + 审计；返回 (obj, err)，与 chat_json 契约同族。"""
        if self.llm_fn is None:
            return None, "llm_unavailable"
        if not st.budget.allow("max_llm_calls"):
            self._block(st, "max_llm_calls")
            return None, "llm_budget_exhausted"
        if st.is_cancelled():
            return None, "cancelled"
        st.budget.spend("max_llm_calls")
        entry = st.operation("llm", target, False)
        try:
            out = self.llm_fn(system, user)
        except Exception as exc:  # noqa: BLE001  模型侧异常按“本次调用失败”处理，不中断流水线
            return None, f"llm_exception:{type(exc).__name__}"
        if isinstance(out, tuple) and len(out) == 2:
            obj, err = out
        elif isinstance(out, Mapping):
            obj, err = out, ""
        else:
            return None, "llm_bad_shape"
        if not isinstance(obj, Mapping):
            return None, (str(err) if err else "llm_no_object")
        entry["ok"] = True
        # operation() 只在创建时按 ok 计一次；这里把“先记失败、后成功”的调用补回汇总计数，
        # 否则报告/卡片会把真实发生的模型调用显示成 0（可观测口径失真）。
        st.audit["llm_calls"] = int(st.audit.get("llm_calls", 0)) + 1
        return dict(obj), ""

    def _do_search(self, st: CollectRunState, query: str, k: int,
                   stage: str) -> Optional[List[Any]]:
        """一次检索：None=不可用/降级（已记 error），[]=合法空结果，[...]=有结果。"""
        query = _clean(query, 200)
        if not query:
            return None
        if self.search_fn is None:
            st.error(stage, "search_unavailable", "未注入检索实现，检索未执行")
            st.search_failed += 1
            return None
        if not st.budget.allow("max_searches"):
            self._block(st, "max_searches")
            return None
        if st.is_cancelled():
            return None
        st.budget.spend("max_searches")
        entry = st.operation("search", query, False)
        try:
            result = self.search_fn(query, k)
        except Exception as exc:  # noqa: BLE001  检索侧异常按降级处理，不当成“搜过了没找到”
            st.error(stage, "search_unavailable", f"检索异常：{type(exc).__name__}")
            st.search_failed += 1
            return None
        if not isinstance(result, Mapping):
            st.error(stage, "search_unavailable", "检索返回形状不符")
            st.search_failed += 1
            return None
        degraded = bool(result.get("degraded"))
        err = str(result.get("error") or "").strip()
        rows = result.get("results")
        rows = list(rows) if isinstance(rows, list) else []
        if degraded or err:
            st.error(stage, "search_unavailable", err or "检索降级")
            st.search_failed += 1
            return rows or None
        entry["ok"] = True
        st.search_ok += 1
        st.audit["searches"] = int(st.audit.get("searches", 0)) + 1
        return rows

    def _fetch_callable(self) -> Optional[Callable[..., Any]]:
        """取页实现：优先注入，其次默认 transport（延迟 import，失败即降级）。"""
        if self.fetch_fn is not None:
            return self.fetch_fn
        if self._fetch_probed:
            return None
        self._fetch_probed = True
        try:
            from mylocalai.assistant.skills.transport import default_transport
            transport = self.transport or default_transport(
                max_bytes=int(self.rules["max_page_bytes"]),
                max_text_chars=int(self.rules["max_text_chars"]),
                timeout_s=float(self.rules["io_timeout_s"]),
                max_hops=int(self.rules["max_redirect_hops"]))
            self.fetch_fn = getattr(transport, "fetch", None)
        except Exception:  # noqa: BLE001  transport 未就位/导入失败：降级为“取页不可用”
            self.fetch_fn = None
        return self.fetch_fn

    def _do_fetch(self, st: CollectRunState, url: str, allowed_domains: Optional[List[str]],
                  stage: str) -> Optional[Dict[str, Any]]:
        """一次读页：只有 status=ok 才计入读页预算。"""
        fetch = self._fetch_callable()
        if fetch is None:
            st.error(stage, "fetch_unavailable",
                     "取页不可用：未注入 fetch_fn 且默认 transport 不可用")
            return None
        if not st.budget.allow("max_pages"):
            self._block(st, "max_pages")
            return None
        if st.is_cancelled():
            return None
        entry = st.operation("fetch", url, False)
        try:
            page = fetch(url, allowed_domains=allowed_domains,
                         cancel_check=st.cancel_check, deadline=st.budget.deadline)
        except Exception as exc:  # noqa: BLE001  取页异常按失败记，不当成“页面无引文”
            st.error(stage, "fetch_exception", f"取页异常：{type(exc).__name__}")
            return None
        if not isinstance(page, Mapping):
            st.error(stage, "fetch_unavailable", "取页返回形状不符")
            return None
        if page.get("status") != "ok":
            category = _clean(page.get("category"), 60) or "fetch_error"
            detail = _clean(page.get("detail"), 200) or "取页失败"
            st.error(stage, category, detail)
            return None
        st.budget.spend("max_pages")
        entry["ok"] = True
        st.audit["pages"] = int(st.audit.get("pages", 0)) + 1
        return dict(page)

    def _page_for(self, st: CollectRunState, url: str, domain: str,
                  stage: str) -> Optional[Dict[str, Any]]:
        """带缓存的读页：同一 URL 只读一次（探测过的页面直接复用）。"""
        key = url_key(url)
        if key and key in st.pages:
            return st.pages[key]
        page = self._do_fetch(st, url, [domain] if domain else None, stage)
        if page is None:
            return None
        if key:
            st.pages[key] = page
        return page

    # -- 状态辅助 -----------------------------------------------------------
    @staticmethod
    def _note(st: CollectRunState, text: Any) -> None:
        """无碍观察（某个域没搜到、链接正文可能不完整）：写进局限，不影响状态判定。"""
        value = _clean(text, 240)
        if value and value not in st.notes:
            st.notes.append(value)

    @staticmethod
    def _block(st: CollectRunState, key: str) -> None:
        """预算/死线到顶：写原因并让受限循环也能看见（blocked 供引擎探测）。"""
        st.budget.block(st.budget.why(key))
        st.budget.blocked = True

    def _hard_fail(self, st: CollectRunState, reason: str) -> None:
        """硬失败：检索未授权/拆主张模型不可用（契约 §2 的 failed）。"""
        st.hard_failed = True
        st.fail(reason)

    def _stage(self, st: CollectRunState, name: str) -> None:
        if name not in st.stage_log:
            st.stage_log.append(name)

    def _progress(self, st: CollectRunState, stage: str, done: int, total: int) -> None:
        """进度回调：调用方异常只吞掉，不影响主流程。"""
        callback = st.on_progress
        if callback is None:
            return
        try:
            callback(str(stage), int(done), int(total))
        except Exception:  # noqa: BLE001  GUI/SSE 侧异常不能反咬流水线
            return

    # -- 收尾 ---------------------------------------------------------------
    def _finish(self, st: CollectRunState, started_at: str) -> Dict[str, Any]:
        """统一外壳：状态判定 + 报告 + 执行信息（六态由 CollectRunState 判，这里只做覆盖）。"""
        self._publish(st)
        self._stage(st, "report")
        shell = st.finish()
        status = self._resolve_status(st, shell)
        finished_at = self.now()
        execution = self._execution(st, status)
        result: Dict[str, Any] = {
            "tool_id": "verify_authority",
            "status": status,
            "task_status": shell["task_status"],
            "data": shell["data"],
            "gaps": list(shell["gaps"]),
            "errors": list(shell["errors"]),
            "audit": dict(shell["audit"]),
            "budget": shell["budget"],
            "report": "",
            "execution": execution,
            "incremental": bool(st.incremental),
            "base_as_of": st.base_as_of,
            "started_at": started_at,
            "finished_at": finished_at,
            "as_of": finished_at,
        }
        result["report"] = render_report(result)
        self._progress(st, "report", 1, 1)
        return result

    def _resolve_status(self, st: CollectRunState, shell: Mapping[str, Any]) -> str:
        """在 CollectRunState 六态之上做两处契约覆盖（硬失败 / 无可核查主张）。"""
        status = str(shell.get("status") or "completed")
        task_status = str(shell.get("task_status") or "completed")
        if status in ("cancelled", "budget_exhausted", "needs_clarification"):
            return status
        if st.hard_failed and status in ("completed", "partial"):
            return "failed" if not st.evidence_internal else "partial"
        if task_status == "no_factual_claim" and status == "partial":
            # 没有可核查主张不是失败（契约 §2）：连“未摘到引文”的缺口都不记成 partial
            return "completed"
        return status

    def _execution(self, st: CollectRunState, status: str) -> Dict[str, Any]:
        """执行信息：agent 模式用循环真实步数；pipeline 模式记阶段数与停止原因。"""
        if st.mode == "agent" and isinstance(st.agent_result, Mapping):
            return {"mode": "agent",
                    "steps": len(st.agent_result.get("steps") or []),
                    "stop_reason": str(st.agent_result.get("stop_reason") or "")}
        if st.budget.deadline_hit() and status == "budget_exhausted":
            stop_reason = "deadline"
        else:
            stop_reason = {"cancelled": "cancelled", "budget_exhausted": "budget_exhausted",
                           "needs_clarification": "needs_clarification",
                           "failed": "failed"}.get(status, "completed")
        return {"mode": "pipeline", "steps": len(st.stage_log),
                "stop_reason": stop_reason}


# ---------------------------------------------------------------------------
# 便捷入口 + 确定性报告（契约 §6.2）
# ---------------------------------------------------------------------------

def verify_authority(text: str, **kw: Any) -> Dict[str, Any]:
    """便捷函数 = AuthoritySkills(**kw).run(text)。工具层用它。"""
    return AuthoritySkills(**kw).run(text)


def _verification_text(item: Mapping[str, Any]) -> str:
    """来源资格的展示串：类别 / 核验方式 / 依据。"""
    authority_class = _safe_text(item.get("authority_class"), 24) or "unverified"
    verification = _safe_text(item.get("verification"), 24) or "unverified"
    label = _VERIFICATION_LABELS.get(str(item.get("verification")), verification)
    basis = "；".join(_safe_text(text, 120) for text in (item.get("authority_basis") or []))
    return (f"{authority_class} / {label}"
            + (f"（{basis}）" if basis else ""))


def render_report(result: Dict[str, Any]) -> str:
    """把一次权威核验渲染成确定性纯文本报告（无 HTML、无 markdown 链接语法）。

    结构：状态 → 主张与证据倾向 → 证据明细 → 来源 → 冲突 → 局限 → 执行计数。
    全文只述“权威资格与证据倾向”，不出现真假标签与数值分。
    """
    if not isinstance(result, dict):
        return "权威核验报告：（结果形状不符，无可渲染内容）"
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    budget = result.get("budget") if isinstance(result.get("budget"), dict) else {}
    used = budget.get("used") if isinstance(budget.get("used"), dict) else {}
    limits = budget.get("limits") if isinstance(budget.get("limits"), dict) else {}
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}

    lines: List[str] = ["权威核验报告（只述证据与权威依据）"]
    lines.append(f"执行状态：{_safe_text(result.get('status'), 40) or '（未知）'}"
                 f"　任务状态：{_safe_text(result.get('task_status'), 40) or '（未知）'}")
    lines.append(f"模式：{_safe_text(execution.get('mode'), 20) or 'pipeline'}"
                 f"　步数：{execution.get('steps', 0)}"
                 f"　停止原因：{_safe_text(execution.get('stop_reason'), 40) or '（无）'}")
    lines.append(f"开始：{_safe_text(result.get('started_at'), 40)}"
                 f"　结束：{_safe_text(result.get('finished_at'), 40)}")
    if result.get("incremental"):
        lines.append(f"增量核验：是（基线 {_safe_text(result.get('base_as_of'), 40) or '未注明'}）")

    claims = [claim for claim in (data.get("claims") or []) if isinstance(claim, dict)]
    evidence = [item for item in (data.get("evidence") or []) if isinstance(item, dict)]
    by_id = {item.get("evidence_id"): item for item in evidence}
    lines.append("")
    lines.append(f"一、主张与证据倾向（共 {len(claims)} 条）")
    if not claims:
        lines.append("  （没有可核验的主张）")
    for claim in claims:
        lines.append(f"  {_safe_text(claim.get('claim_id'), 12)} 「"
                     f"{_safe_quote(claim.get('text'), 200)}」")
        lines.append(f"      权威资格：{_safe_text(claim.get('balance_label'), 60)}"
                     f"（{_safe_text(claim.get('balance'), 24)}）"
                     f"　可靠证据倾向：{_safe_text(claim.get('lean_label'), 60)}"
                     f"（{_safe_text(claim.get('lean'), 24)}）")
        lines.append(f"      结论：{_safe_text(claim.get('conclusion'), 300)}")
        for label, key in (("支持", "supporting_ids"), ("反驳", "refuting_ids"),
                           ("背景/线索", "context_ids")):
            ids = [str(item) for item in (claim.get(key) or [])]
            if ids:
                lines.append(f"      {label}：" + "、".join(_safe_text(i, 12) for i in ids))
        basis = "；".join(_safe_text(text, 160) for text in (claim.get("authority_basis") or []))
        if basis:
            lines.append(f"      权威依据：{basis}")

    lines.append("")
    lines.append(f"二、证据明细（共 {len(evidence)} 条）")
    if not evidence:
        lines.append("  （没有可定位引文）")
    for item in evidence:
        relation = _RELATION_LABELS.get(str(item.get("relation")),
                                        _safe_text(item.get("relation"), 20))
        lines.append(f"  {_safe_text(item.get('evidence_id'), 12)} 主张 "
                     f"{'、'.join(_safe_text(cid, 12) for cid in (item.get('claim_ids') or []))}"
                     f"｜关系：{relation}")
        lines.append(f"      来源：{_safe_text(item.get('name'), 80)}"
                     f"（{_safe_text(item.get('domain'), 80)}）"
                     f"　{_verification_text(item)}")
        lines.append(f"      引文：「{_safe_quote(item.get('quote'), 300)}」")
        lines.append(f"      地址：{_safe_text(item.get('url'), 300)}"
                     f"　标题：{_safe_text(item.get('title'), 120) or '（无）'}")
        lines.append(f"      时点：发布于 {_safe_text(item.get('published_at'), 40) or '未注明'}"
                     f"，读取于 {_safe_text(item.get('fetched_at'), 40) or '未注明'}"
                     f"　独立组：{_safe_text(item.get('independence_group'), 40)}")
        limits_text = "；".join(_safe_text(text, 160) for text in (item.get("limitations") or []))
        lines.append(f"      局限：{limits_text or '（无）'}")

    sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    verified = [row for row in (sources.get("verified") or []) if isinstance(row, dict)]
    unverified = [row for row in (sources.get("unverified") or []) if isinstance(row, dict)]
    lines.append("")
    lines.append(f"三、来源（已核验 {len(verified)} / 未核验 {len(unverified)}）")
    for row in verified:
        lines.append(f"  [已核验] {_safe_text(row.get('name'), 80)}"
                     f"（{_safe_text(row.get('domain'), 80)}）"
                     f"　档位：{_safe_text(row.get('tier'), 30) or '（未登记）'}"
                     f"　{_verification_text(row)}")
    for row in unverified:
        lines.append(f"  [未核验] {_safe_text(row.get('name'), 80)}"
                     f"（{_safe_text(row.get('domain'), 80)}）"
                     f"　说明：只作线索，未计入决定性证据")

    conflicts = [row for row in (data.get("conflicts") or []) if isinstance(row, dict)]
    lines.append("")
    lines.append(f"四、冲突（共 {len(conflicts)} 条）")
    if not conflicts:
        lines.append("  （无）")
    for row in conflicts:
        lines.append(f"  - {_safe_text(row.get('summary'), 240)}")

    limitations = [_safe_text(text, 240) for text in
                   ((data.get("limitations") or result.get("gaps") or []))]
    lines.append("")
    lines.append(f"五、局限与缺口（共 {len(limitations)} 条）")
    if not limitations:
        lines.append("  （无）")
    for index, text in enumerate(limitations, start=1):
        lines.append(f"  {index}. {text}")

    lines.append("")
    lines.append("六、执行计数")
    lines.append("  " + render_counts_line(audit))
    lines.append(f"  搜索 {audit.get('searches', 0)}/{limits.get('max_searches', '-')}"
                 f"；读页 {audit.get('pages', 0)}/{limits.get('max_pages', '-')}"
                 f"；模型调用 {audit.get('llm_calls', 0)}/{limits.get('max_llm_calls', '-')}")
    lines.append(f"  主张 {used.get('max_claims', 0) if 'max_claims' in used else len(claims)}"
                 f"/{limits.get('max_claims', '-')}")
    if result.get("status") in ("cancelled", "budget_exhausted"):
        lines.append("  本次提前停止，上表为停止时点的真实消耗。")
    return "\n".join(lines)
