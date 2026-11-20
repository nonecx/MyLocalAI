# -*- coding: utf-8 -*-
"""权威核验流水线（契约 §5）：用户给说法/链接，找权威资料证明/反驳并做受约束分析。

改了什么、为什么、锁定什么：
  - 新增 `assistant.skills.authority.pipeline`：把"预检/读链接 → 拆主张 → 权威源发现 →
    阅读取证 → 代码算 balance/lean + LLM 受约束分析 → 去重/报告"编排成一条确定性流水线
    （`verify_authority` / `AuthoritySkills.run`）。
  - **判定全部由代码**：来源类别与核验方式走 `collect_core.classify_source` +
    `collect_core.dynamic_authority_signals`；balance/lean 走契约 §5.4 决策表纯函数；
    conclusion 按契约模板逐字生成；LLM 只做拆主张、抽引文、受约束分析，且只能引用
    已存在的 evidence_id，新增 URL/日期/证据 ID 一律丢弃记 error（铁律 1/2）。
  - **动态权威不自我认证**：目录外候选只有官方/国际组织/学术/标准/事实核查类才尝试动态
    核验，且至少 `dynamic_min_signals` 个代码信号（官方后缀/正文官方标记/已核验页面直链/
    多来源交叉提及）；页面自称权威、模型说"匹配"都不算；`session_verified` 仅本次运行
    有效，作决定性证据还需交叉佐证（§5.3）。
  - **每个出口都有降级**：未授权/检索全败/阶段1 模型不可用 → failed；读页/引文/分析失败
    → partial；预算/取消保留已取得结果；没有可核查主张 → no_factual_claim/completed；
    检索成功但无决定性证据 → insufficient/completed（合法空结果，绝不编）。
  - 全离线可测：search_fn / fetch_fn / llm_fn / transport / clock / on_progress 全部可注入。

与 collect_core 的关系：预算、运行状态、来源分类、动态权威信号、去重、引文锚定、
SKILL.md 装配全部复用共享底座；本模块只做编排与 §5.4 决策表，不重复实现那套口径。

依赖方向：只 import 标准库与 assistant/retrieval 内部包，**不 import web / platform**。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from mylocalai.assistant.skills.collect_core import (
    AUTHORITY_CLASSES,
    RELATIONS,
    STATUSES,
    TASK_STATUSES,
    CollectBudget,
    CollectRunState,
    SourceInfo,
    anchor_quote,
    classify_source,
    dedupe_sources,
    dynamic_authority_signals,
    is_decisive_source,
    load_authority_config,
    load_collect_rules,
    load_skill_prompt,
    near_duplicate_source,
    render_counts_line,
    render_evidence_line,
    url_key,
    utc_now_iso,
)
from mylocalai.assistant.skills.pipeline import (
    CLAIM_STATUSES,
    locate_quote,
    normalize_quote,
    normalize_text,
    strip_pii,
)
from mylocalai.assistant.skills.transport import strip_query_operators
from mylocalai.retrieval.sources import tier_rank

__all__ = ["AuthoritySkills", "verify_authority", "render_report", "load_rules",
           "BALANCES", "LEANS", "STATUSES", "TASK_STATUSES"]

#: 权威证据平衡（契约 §5.4 决策表的产出，封闭枚举）
BALANCES = ("supported", "refuted", "contested", "insufficient", "not_checkable")
#: 证据倾向（只作"倾向"展示，不能单独升级为证明/反驳）
LEANS = ("supports", "refutes", "mixed", "none")

_BALANCE_LABELS = {"supported": "权威证据支持", "refuted": "权威证据反驳",
                   "contested": "权威证据冲突", "insufficient": "权威证据不足",
                   "not_checkable": "不可核查"}
_LEAN_LABELS = {"supports": "支持", "refutes": "反驳", "mixed": "冲突", "none": "无"}
_RELATION_LABELS = {"supports": "支持", "refutes": "反驳", "partial": "部分一致",
                    "background": "背景", "unclear": "关系不明"}
_VERIFICATION_LABELS = {"catalog_anchor": "信源目录锚点",
                        "verified_site_link": "已核验站点直链",
                        "session_verified": "本次运行动态核验",
                        "unverified": "未核验"}

#: 用户可见的四个进度阶段（契约 §5.1）；不暴露内部技能名
_PROGRESS_STAGES = ("拆分主张", "核验权威来源", "阅读取证", "整理分析")

#: conclusion 模板（契约 §5.4 逐字锁定）。代码生成，LLM 不得改动。
_CONCLUSION_TEMPLATES = {
    ("supported", "supports"): "在{scope}范围内，权威资料可以证明该说法：{摘要}。",
    ("refuted", "refutes"): "在{scope}范围内，权威资料可以直接反驳该说法：{摘要}。",
    ("contested", "mixed"): "权威资料之间存在直接冲突，目前既不能证明也不能反驳。",
    ("insufficient", "supports"): "现有可靠证据倾向支持，但缺少权威确认，不能据此证明。",
    ("insufficient", "refutes"): "现有可靠证据倾向反驳，但缺少权威确认，不能据此反驳。",
    ("insufficient", "mixed"): "现有可靠证据互相矛盾，不能证明或反驳。",
    ("insufficient", "none"): "没有找到能证明或反驳该说法的权威资料。",
    ("not_checkable", "none"): "这不是可核查的事实主张，不做证据倾向判断。",
}

#: 报告里不允许出现的固定真假判定词（铁律 1）；证明/反驳只允许按上面的模板出现
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")

_HERE = Path(__file__).resolve().parent
_RULES_PATH = _HERE / "rules.json"
_ADJUDICATE_SKILL_DIR = _HERE / "adjudicate"
# 拆主张/抽引文复用取证三技能的 evidence/SKILL.md（契约 §5.6）；authority/SKILL.md 是
# 权威源口径文档（阶段2 全部由代码判定，不调模型），由 adjudicate/SKILL.md 与测试引用。
_EVIDENCE_SKILL_DIR = _HERE.parent / "evidence"
_SOURCES_PATH = _HERE.parent / "sources.json"

# 内置默认值：与 rules.json 保持一致。rules.json 缺失/损坏时以此兜底，保证流水线可用。
DEFAULT_AUTHORITY_RULES: Dict[str, Any] = {
    "max_input_chars": 4000,
    "max_claims": 4,
    "max_authority_sources": 8,
    "max_candidates": 16,
    "max_searches": 12,
    "max_pages": 12,
    "per_claim_max_queries": 2,
    "per_claim_max_pages": 3,
    "max_llm_calls": 20,
    "total_deadline_s": 90.0,
    "io_timeout_s": 10.0,
    "max_page_bytes": 1500000,
    "max_text_chars": 20000,
    "max_redirect_hops": 3,
    "max_quote_chars": 300,
    "max_dynamic_domains": 4,
    "allow_dynamic_authority": True,
    "dynamic_min_signals": 2,
}

# 数值安全区间（键名锁定；未列出的键按 defaults 类型校验，不夹取）
_RULE_LIMITS: Dict[str, Tuple[float, float]] = {
    "max_input_chars": (50, 200000),
    "max_claims": (1, 50),
    "max_authority_sources": (1, 20),
    "max_candidates": (1, 30),
    "max_searches": (1, 200),
    "max_pages": (1, 200),
    "per_claim_max_queries": (1, 10),
    "per_claim_max_pages": (1, 20),
    "max_llm_calls": (1, 500),
    "total_deadline_s": (1, 3600),
    "io_timeout_s": (0.5, 300),
    "max_page_bytes": (1000, 50000000),
    "max_text_chars": (200, 500000),
    "max_redirect_hops": (0, 10),
    "max_quote_chars": (10, 5000),
    "max_dynamic_domains": (1, 10),
    "dynamic_min_signals": (1, 10),
}

_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）】]+", re.I)
_SINGLE_URL_RE = re.compile(r"^https?://\S+$", re.I)
_VERDICT_RE = re.compile("|".join(re.escape(w) for w in _VERDICT_WORDS))


# ---------------------------------------------------------------------------
# 规则加载
# ---------------------------------------------------------------------------

def load_rules(path: Optional[Any] = None) -> Dict[str, Any]:
    """加载 authority/rules.json：缺键回退默认、数值夹取、坏类型回退、未知键丢弃。

    传 dict 按"覆盖项"处理（便于测试按需收紧额度）；传路径读该文件；不传读包内
    rules.json。文件缺失/不可解析 → 内置默认值（不抛）：取不到预算配置时"按默认额度跑"
    比"整个工具不可用"更符合降级文化。
    """
    if isinstance(path, Mapping):
        merged = dict(DEFAULT_AUTHORITY_RULES)
        merged.update({k: v for k, v in path.items() if k in DEFAULT_AUTHORITY_RULES})
        return _clamp_rules(merged)
    target = _RULES_PATH if path is None else Path(path)
    return load_collect_rules(target, DEFAULT_AUTHORITY_RULES, _RULE_LIMITS)


def _clamp_rules(rules: Mapping[str, Any]) -> Dict[str, Any]:
    """与 collect_core.load_collect_rules 同口径的夹取（dict 覆盖项没有文件可读）。"""
    out: Dict[str, Any] = {}
    for key, default in DEFAULT_AUTHORITY_RULES.items():
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
# 纯函数：文本小工具
# ---------------------------------------------------------------------------

def _clean(value: Any, limit: int = 200) -> str:
    """压空白 + 截断：所有进入输出结构的自由文本都过这里。"""
    return normalize_text(value)[:limit]


def _host_of(url: Any) -> str:
    """取主机名（小写、去尾点、去 www.）；解析失败返回空串。"""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:  # urlsplit 对畸形输入可能抛；此处只求一个域名
        return ""
    return host[4:] if host.startswith("www.") else host


def _domain_of(host: Any, domain: Any) -> bool:
    """host 是否落在 domain 内（相等或子域）：a.b.gov.cn 命中 b.gov.cn。"""
    host = str(host or "").lower().rstrip(".")
    domain = str(domain or "").lower().rstrip(".")
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


def _first_list(obj: Any, keys: Tuple[str, ...]) -> Optional[list]:
    """从模型返回里取第一个命中键的列表（容忍键名写歪，同 rumor/evidence.py 的做法）。"""
    if not isinstance(obj, dict):
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
    """把模型返回的数组收敛成去重后的字符串列表（保序、去空白项）。"""
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value[:limit]:
        text = _clean(item, item_limit)
        if text and text not in out:
            out.append(text)
    return out


def _body_group(body: str) -> str:
    """正文哈希组：同一篇文章转载/镜像到别处也落进同一组（转载不产生新证据）。"""
    digest = hashlib.sha256(normalize_text(body).encode("utf-8")).hexdigest()[:12]
    return "G-" + digest


def _page_text(ctx: "_RunContext", url: Any) -> str:
    """运行内已读到的某地址正文（归一化）；没读过返回空串。

    动态核验的交叉佐证/入站直链都只看**实际读过的正文**：没读过就没有信号，
    搜索摘要、页面自称都不算数。
    """
    key = url_key(url)
    cached = ctx.pages.get(key)
    if not cached or cached[0] is None:
        return ""
    return normalize_text(cached[0].get("text") or "")


def _catalog_anchor_domains() -> set:
    """skills/sources.json 里 anchor=true 的域名集合（目录锚点，代码判定，不靠模型）。"""
    domains: set = set()
    try:
        raw = json.loads(_SOURCES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  目录缺失/损坏 = 没有目录锚点，候选全部按目录外走动态核验
        return domains
    rows = raw.get("sources") if isinstance(raw, dict) else None
    for row in rows or []:
        if isinstance(row, dict) and row.get("anchor"):
            domain = str(row.get("domain") or "").strip().lower().rstrip(".")
            if domain:
                domains.add(domain)
    return domains


def _decide_balance_lean(pairs: Sequence[Tuple[str, bool]],
                         checkable: bool = True) -> Tuple[str, str]:
    """契约 §5.4 决策表（纯函数）：pairs = [(relation, 是否决定性来源), ...]。

    relation=partial/background/unclear 只作背景，不进支持/反驳计数——把"部分一致"
    升级成支持或反驳，都是代码替模型下判断。
    """
    if not checkable:
        return "not_checkable", "none"
    supports = [decisive for relation, decisive in pairs if relation == "supports"]
    refutes = [decisive for relation, decisive in pairs if relation == "refutes"]
    if any(supports) and any(refutes):
        return "contested", "mixed"
    if any(supports):
        return "supported", "supports"
    if any(refutes):
        return "refuted", "refutes"
    if supports and refutes:
        return "insufficient", "mixed"
    if supports:
        return "insufficient", "supports"
    if refutes:
        return "insufficient", "refutes"
    return "insufficient", "none"


def _scope_text(claim: Mapping[str, Any]) -> str:
    """conclusion 的 {scope}：只取原文/模型明确写出的限定；一个都没写 → 可核查。"""
    qualifiers = claim.get("qualifiers") if isinstance(claim.get("qualifiers"), dict) else {}
    parts = [_clean(qualifiers.get(key), 60) for key in ("time", "place", "entity")]
    parts = [part for part in parts if part]
    return "、".join(parts) if parts else "可核查"


def _best_summary(evidence: Sequence[Mapping[str, Any]], balance: str,
                  decisive: Mapping[str, bool]) -> str:
    """conclusion 的 {摘要}：最高等级支持/反驳证据的 title 或 quote 前 80 字（代码裁剪）。"""
    want = "supports" if balance == "supported" else "refutes"
    rows = [ev for ev in evidence if ev.get("relation") == want]
    if not rows:
        return "（未见可直接引用的权威原文）"
    rows = sorted(rows, key=lambda ev: (0 if decisive.get(str(ev.get("evidence_id")))
                                        else 1, tier_rank(ev.get("tier") or "")))
    top = rows[0]
    return _clean(top.get("title"), 80) or _clean(top.get("quote"), 80) or "（未见可直接引用的权威原文）"


def _conclusion_text(balance: str, lean: str, claim: Mapping[str, Any],
                     evidence: Sequence[Mapping[str, Any]],
                     decisive: Mapping[str, bool]) -> str:
    """按契约 §5.4 模板逐字生成 conclusion；LLM 无权改动这里。

    用 replace 而不是 str.format：模板占位符是中文（{scope}/{摘要}），replace 不会把
    正文里偶然出现的花括号当成占位符，也不会因为以后加字段而静默错位。
    """
    template = _CONCLUSION_TEMPLATES.get((balance, lean)) \
        or _CONCLUSION_TEMPLATES[("insufficient", "none")]
    if balance in ("supported", "refuted"):
        return (template.replace("{scope}", _scope_text(claim))
                .replace("{摘要}", _best_summary(evidence, balance, decisive)))
    return template


def _candidate_priority(info: SourceInfo, catalog: set, dynamic_classes: frozenset) -> int:
    """候选优先级（代码定序）：目录锚点最先，动态可核验类次之，媒体只作倾向排后。"""
    if info.domain in catalog:
        return 0
    if info.authority_class in dynamic_classes:
        return 1
    if info.authority_class == "media":
        return 2
    return 3


def _source_rank(entry: Mapping[str, Any], dynamic_classes: frozenset) -> int:
    """取证顺序：核验方式越硬越前，同类里非媒体（可作决定性）比媒体（只作倾向）前。"""
    verification = {"catalog_anchor": 0, "verified_site_link": 1,
                    "session_verified": 2}.get(str(entry.get("verification")), 3)
    kind = 0 if entry.get("authority_class") in dynamic_classes else 1
    return verification * 2 + kind


def _dynamic_classes(authority_config: Mapping[str, Any]) -> frozenset:
    """可尝试动态核验的类别 = 决定性类别 - 媒体类（§5.3.2：媒体不动态升格）。"""
    decisive = [c for c in (authority_config.get("decisive_classes") or []) if c in AUTHORITY_CLASSES]
    media = set(authority_config.get("media_classes") or [])
    return frozenset(c for c in decisive if c not in media)


# ---------------------------------------------------------------------------
# 运行态
# ---------------------------------------------------------------------------

class _RunContext:
    """一次运行的内部上下文（不进输出结构，避免内部字段泄漏到卡片）。"""

    def __init__(self) -> None:
        self.infos: Dict[str, SourceInfo] = {}
        self.sources: List[Dict[str, Any]] = []
        self.corroborated: Dict[str, Tuple[bool, str]] = {}
        self.decisive: Dict[str, bool] = {}
        self.pages: Dict[str, Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]] = {}
        self.input_url_keys: set = set()


class _RunState:
    """一次运行的状态：错误/缺口/审计/终止原因（公共部分委托 collect_core.CollectRunState）。"""

    def __init__(self, budget: CollectBudget, cancel_check: Optional[Callable[[], bool]] = None,
                 clock: Optional[Callable[[], str]] = None) -> None:
        self.core = CollectRunState(budget, cancel_check)
        self.clock = clock or utc_now_iso
        self.cancel_check = cancel_check
        self.clarified = False
        self.clarify_reason = ""
        self.failed = False
        self.no_factual = False
        # 契约 §5.5 的 data 键在一次运行里始终存在（空出口也要有完整形状）
        self.data: Dict[str, Any] = {"claims": [], "evidence": [], "sources": [],
                                     "conflicts": [], "analysis": [], "limitations": []}

    @property
    def budget(self) -> CollectBudget:
        return self.core.budget

    @property
    def errors(self) -> List[Dict[str, str]]:
        return self.core.errors

    @property
    def gaps(self) -> List[str]:
        return self.core.gaps

    @property
    def audit(self) -> Dict[str, Any]:
        return self.core.audit

    def error(self, stage: str, category: str, detail: str) -> None:
        self.core.error(stage, category, detail)

    def gap(self, text: Any) -> None:
        self.core.gap(text)

    def operation(self, kind: str, target: str, ok: bool = True) -> Dict[str, Any]:
        return self.core.operation(kind, target, ok)

    def is_cancelled(self) -> bool:
        return self.core.is_cancelled()

    def clarify(self, reason: str) -> None:
        self.clarified = True
        if not self.clarify_reason:
            self.clarify_reason = _clean(reason, 240)
        self.core.clarify(reason)

    def fail(self, reason: str) -> None:
        self.failed = True
        self.core.fail(reason)

    def mark_no_factual_claim(self) -> None:
        self.no_factual = True
        self.core.no_factual_claim()


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class AuthoritySkills:
    """权威核验流水线（预检 → 拆主张 → 权威源发现 → 阅读取证 → 受约束分析 → 报告）。

    全部 IO 可注入：`llm_fn(system, user) -> (obj, err)`（chat_json 契约）、
    `search_fn(query, k) -> {"results": [...], "degraded": bool, "error": str?}`、
    `fetch_fn(url, *, allowed_domains, cancel_check, deadline) -> transport.fetch 形状`。
    未注入 fetch_fn 时延迟 import `assistant.skills.transport` 的默认实现，import 失败
    降级为"取页不可用"错误条目（并行开发期 transport 可能尚未就位）。
    """

    def __init__(self, *, rules: Optional[Any] = None,
                 search_fn: Optional[Callable[[str, int], Dict[str, Any]]] = None,
                 fetch_fn: Optional[Callable[..., Dict[str, Any]]] = None,
                 llm_fn: Optional[Callable[[str, str], Any]] = None,
                 transport: Optional[Any] = None,
                 clock: Optional[Callable[[], str]] = None,
                 on_progress: Optional[Callable[[str, int, int], Any]] = None,
                 authority_config: Optional[Mapping[str, Any]] = None) -> None:
        self.rules = load_rules(rules)
        self.search_fn = search_fn
        self.fetch_fn = fetch_fn
        self.llm_fn = llm_fn
        self.transport = transport
        self.clock = clock or utc_now_iso
        self.on_progress = on_progress
        self.authority_config = (dict(authority_config)
                                 if isinstance(authority_config, Mapping)
                                 else load_authority_config())
        self.dynamic_classes = _dynamic_classes(self.authority_config)
        self._fetch_probed = fetch_fn is not None
        self._on_progress: Optional[Callable[[str, int, int], Any]] = None

    # -- 入口 ---------------------------------------------------------------

    def run(self, text: str, *, cancel_check: Optional[Callable[[], bool]] = None,
            deadline: Optional[float] = None,
            on_progress: Optional[Callable[[str, int, int], Any]] = None) -> Dict[str, Any]:
        """跑完整条流水线，返回契约 §5.5 的统一运行外壳。"""
        started_at = self.clock()
        self._on_progress = on_progress or self.on_progress
        raw = text if isinstance(text, str) else ("" if text is None else str(text))
        if deadline is None:
            # 未传死线时按 total_deadline_s 自建：预算上限不该因为调用方没传就失效
            deadline = time.monotonic() + float(self.rules["total_deadline_s"])
        budget = CollectBudget(self.rules, deadline)
        st = _RunState(budget, cancel_check, clock=self.clock)
        ctx = _RunContext()

        if not self._precheck(st, raw):
            return self._finish(st, started_at)
        raw_text = self._resolve_input(st, raw, ctx)
        if st.is_cancelled():
            st.gap("运行在读入原文后被取消，未开始拆分主张")
            return self._finish(st, started_at)

        claims = self._stage_claims(st, raw_text)
        self._progress(st, _PROGRESS_STAGES[0], 1)
        if any(claim["claim_status"] == "checkable" for claim in claims):
            # 没有可核查主张时不检索不读页（阶段1 已按 no_factual_claim 记账），
            # 但阶段4 仍要跑：让 not_checkable 主张拿到契约模板结论
            sources = self._stage_authority_sources(st, claims, ctx)
            self._progress(st, _PROGRESS_STAGES[1], 2)
            self._stage_evidence(st, claims, sources, ctx)
            self._progress(st, _PROGRESS_STAGES[2], 3)
            # 直链与交叉佐证放在**取证之后**：此刻目录锚点页面也真的读过了，
            # 否则"被已核验站点链接"只有 session_verified 页面可当链接源，
            # 等于让一个自 signals 的动态来源把零身份证据的域名抬成决定性。
            self._apply_inbound_links(sources, ctx)
            ctx.corroborated = self._compute_corroboration(sources, ctx)
        self._stage_adjudicate(st, claims, ctx)
        self._progress(st, _PROGRESS_STAGES[3], 4)
        self._finalize(st, ctx)
        return self._finish(st, started_at)

    # -- 预检与原文解析 -----------------------------------------------------

    def _precheck(self, st: _RunState, raw: str) -> bool:
        if not raw.strip():
            st.error("precheck", "empty_input", "原文为空或只有空白字符")
            st.clarify("原文为空：请提供需要权威核验的说法或链接")
            return False
        limit = int(self.rules["max_input_chars"])
        if len(raw) > limit:
            st.error("precheck", "input_too_long",
                     f"原文 {len(raw)} 字符，超过上限 {limit}")
            st.clarify(f"原文长度 {len(raw)} 超过单次上限 {limit}：请缩小范围或分段提交")
            return False
        return True

    def _resolve_input(self, st: _RunState, raw: str, ctx: _RunContext) -> str:
        """纯链接输入：安全抓取该页作为原文，并标"用户提供链接，未核验"。

        为什么单独处理：用户只丢一个链接时，"说法"在页面里。抓到的正文是**待核验
        对象**而不是权威证据——所以该来源一律记 unverified，绝不因为域名像官方就升级，
        也绝不回头拿它给自己的主张作证（否则就是自己证明自己）。
        """
        link = raw.strip()
        if not _SINGLE_URL_RE.match(link):
            return raw
        host = _host_of(link)
        page, _ = self._do_fetch(st, link, [host] if host else None, "precheck", ctx)
        if page is None:
            st.gap("用户提供的链接读取失败，未能取得页面正文，按链接本身作为原文")
            return raw
        body = normalize_text(page.get("text") or "")
        if not body:
            st.error("precheck", "empty_page_text",
                     "链接页面读取成功但正文为空，未取得可拆分的原文")
            st.gap("用户提供的链接页面正文为空")
            return raw
        info = classify_source(link, authority_config=self.authority_config)
        name = _clean(page.get("title") or host, 80) or host
        st.data["input"] = {"kind": "link", "url": link, "title": name,
                            "fetched_at": _clean(page.get("fetched_at"), 40) or st.clock()}
        st.data["sources"].append({
            "source_id": "SRC-USER-LINK", "name": name, "domain": host,
            "tier": info.tier, "authority_class": "unverified",
            "verification": "unverified",
            "verification_basis": "用户提供链接，未核验（仅作为待核验原文，不作为证据）",
            "verified": False,
        })
        ctx.infos["SRC-USER-LINK"] = SourceInfo(
            "SRC-USER-LINK", name, host, info.tier, info.source_kind, "unverified",
            "unverified", "用户提供链接，未核验（仅作为待核验原文，不作为证据）", False)
        ctx.input_url_keys.add(url_key(link))
        # 同时记一份"主机+路径"键：url_key 用原始 hostname 做键，不剥 www.、不忽略
        # query，用户给 www.nhc.gov.cn/a 而检索返回 nhc.gov.cn/a?utm=1 时键会不匹配，
        # 那一页就会被当成"自己证明自己"的证据。主机比较走 _host_of（剥 www.）。
        from urllib.parse import urlsplit as _split
        try:
            parts = _split(link)
            path = (parts.path or "/").rstrip("/") or "/"
            ctx.input_url_keys.add(f"{_host_of(link)}{path}")
        except ValueError:  # noqa: BLE001  畸形 URL 已由 transport 挡下，这里只求一个键
            pass
        return body

    # -- 阶段 1：拆主张（复用 skills/evidence/SKILL.md） ---------------------

    def _stage_claims(self, st: _RunState, raw_text: str) -> List[Dict[str, Any]]:
        """拆主张：text 必须逐字定位原文；无 checkable 主张 → no_factual_claim/completed。"""
        cap = int(self.rules["max_claims"])
        contract = ('只输出 JSON：{"claims":[{"text":"<原文逐字片段>",'
                    '"claim_status":"checkable|not_checkable",'
                    '"qualifiers":{"time":null,"place":null,"entity":null},'
                    '"queries":["<中性检索词>"]}]}；'
                    f'最多 {cap} 条；text 必须逐字来自原文，queries 不许带 site:/inurl: 等'
                    '范围操作符；不做真假判断、不给数值分。')
        obj, err = self._call_llm(st, load_skill_prompt(_EVIDENCE_SKILL_DIR, contract),
                                  f"用户原文：\n{raw_text}", "claims")
        rows = _first_list(obj, ("claims", "items")) if isinstance(obj, dict) else None
        if not isinstance(rows, list):
            if err == "llm_budget_exhausted":
                st.gap("模型调用预算已达上限，主张拆分未执行")
                return []
            # 连阶段1都进不去（契约 §5.2 的 failed 定义）：没有主张就没有核验对象
            category = "llm_unavailable" if err == "llm_unavailable" else "llm_call_failed"
            st.error("claims", category, err or "模型不可用，无法拆分主张")
            st.fail("主张拆分无法开始：" + (err or "模型不可用"))
            st.gap("主张拆分未执行（模型不可用），后续阶段未开始")
            return []

        if len(rows) > cap:
            st.error("claims", "too_many_claims",
                     f"识别出 {len(rows)} 条主张，超过单次上限 {cap}")
            st.clarify(f"识别出 {len(rows)} 条主张，超过单次上限 {cap}："
                       "请指定要先核验哪一条，或缩小范围")
            rows = rows[:cap]

        normalized = normalize_text(raw_text)
        claims: List[Dict[str, Any]] = []
        for index, row in enumerate(rows[:cap], start=1):
            if not isinstance(row, dict):
                continue
            text = _clean(row.get("text"), 300)
            span = locate_quote(normalized, text) if text else None
            if span is None:
                st.error("claims", "claim_not_anchored",
                         f"主张未在原文中逐字定位，已丢弃：{text[:60]}")
                continue
            status = str(row.get("claim_status") or "").strip().lower()
            if status not in CLAIM_STATUSES:
                st.error("claims", "bad_claim_status",
                         f"未知主张状态：{status or '（空）'}，按不可核查处理")
                status = "not_checkable"
            qualifiers = row.get("qualifiers")
            qualifiers = qualifiers if isinstance(qualifiers, dict) else {}
            claims.append({
                "claim_id": f"C{index}",
                "text": text,
                "source_span": span,
                "qualifiers": {"time": _clean(qualifiers.get("time"), 60) or None,
                               "place": _clean(qualifiers.get("place"), 60) or None,
                               "entity": _clean(qualifiers.get("entity"), 60) or None},
                "claim_status": status,
                "_queries": _string_items(row.get("queries"), 8, 80),
            })

        if not any(claim["claim_status"] == "checkable" for claim in claims):
            # 没有可核查主张不是失败（契约 §5.2）：任务状态单独记一个出口
            st.mark_no_factual_claim()
            if claims:
                st.gap("原文只有观点/情绪/诉求，没有可核查的事实主张")
            else:
                st.gap("未能从原文拆出可定位的主张")
            return claims
        return claims

    # -- 阶段 2：权威源发现 ------------------------------------------------

    def _stage_authority_sources(self, st: _RunState, claims: List[Dict[str, Any]],
                                 ctx: _RunContext) -> List[Dict[str, Any]]:
        """发现权威候选 → 代码分类 → 目录外候选动态核验（§5.3）。

        返回可用于取证的来源（已核验/媒体）；未核验候选仍进 sources 列表作线索，
        但绝不进决定性证据。
        """
        checkable = [c for c in claims if c["claim_status"] == "checkable"]
        catalog = _catalog_anchor_domains()
        rows: List[Dict[str, Any]] = []
        attempts = ok = 0
        for claim in checkable:
            if st.is_cancelled() or st.budget.blocked_reason:
                break
            if not st.budget.allow("max_searches"):
                st.budget.block(st.budget.why("max_searches"))
                st.gap("检索预算已达上限，权威源发现不完整")
                break
            queries = self._claim_queries(st, claim)
            if not queries:
                continue
            attempts += 1
            results = self._do_search(st, queries[0], 8, "sources")
            if st.is_cancelled() or st.budget.blocked_reason:
                break
            if results is None:
                continue      # 检索不可用/降级：错误与缺口已由 _do_search 记录
            ok += 1
            rows.extend(row for row in results if isinstance(row, dict))

        if attempts and not ok:
            # 未授权/检索后端全败：没有任何发现依据，不能把"没搜"说成"搜过了没找到"
            st.error("sources", "search_unavailable",
                     "本次所有检索均不可用或已降级（外部检索未授权或后端故障），未发现权威候选")
            st.fail("检索不可用：无法发现权威资料（外部检索未授权或检索后端故障）")
            return []

        candidates = dedupe_sources(rows, key=lambda row: url_key(row.get("url") or ""))
        infos: List[Dict[str, Any]] = []
        seen_hosts: set = set()
        for row in candidates:
            url = _clean(row.get("url"), 300)
            if not url:
                continue
            info = classify_source(url, authority_config=self.authority_config)
            host = info.domain or _host_of(url)
            if not host or host in seen_hosts:
                # 同一域名只产生一个信源：换子站/镜像不构成第二个权威
                continue
            seen_hosts.add(host)
            infos.append({"url": url, "host": host, "info": info,
                          "title": _clean(row.get("title"), 120) or info.name})
        infos.sort(key=lambda item: (_candidate_priority(item["info"], catalog,
                                                         self.dynamic_classes),
                                     tier_rank(item["info"].tier)))
        infos = infos[:int(self.rules["max_candidates"])]

        sources = self._classify_candidates(st, infos, catalog, ctx)
        # 注意：_apply_inbound_links / _compute_corroboration 不在这里跑——
        # 它们移到 _stage_evidence 之后（此刻目录锚点页面才真的读过），
        # 否则 session_verified 页面就能当链接源，把零身份证据的域名抬成
        # verified_site_link（而它无需交叉佐证即为决定性）。

        # 可用来源：已核验或媒体（作倾向）；未核验只作线索，且不超过 max_authority_sources
        usable = [entry for entry in sources if entry["verification"] != "unverified"]
        usable.sort(key=lambda entry: _source_rank(entry, self.dynamic_classes))
        cap = int(self.rules["max_authority_sources"])
        for index, entry in enumerate(usable):
            entry["usable"] = index < cap
            if index >= cap:
                # 超上限的来源仍如实列出，但把"本次未用于取证"写进依据，不假装用过
                entry["verification_basis"] = (
                    f"{entry['verification_basis']}；权威来源数量达到上限 {cap}，"
                    "本次未用于取证")
        for entry in sources:
            ctx.infos[entry["source_id"]] = SourceInfo(
                entry["source_id"], entry["name"], entry["domain"], entry["tier"],
                entry["source_kind"], entry["authority_class"], entry["verification"],
                entry["verification_basis"], entry["verification"] != "unverified")
        ctx.sources = sources

        public = list(st.data["sources"])
        public.extend({
            "source_id": entry["source_id"], "name": entry["name"],
            "domain": entry["domain"], "tier": entry["tier"],
            "authority_class": entry["authority_class"],
            "verification": entry["verification"],
            "verification_basis": entry["verification_basis"],
            "verified": entry["verification"] != "unverified",
        } for entry in sources)
        st.data["sources"] = public
        return [entry for entry in sources if entry.get("usable")]

    def _classify_candidates(self, st: _RunState, infos: List[Dict[str, Any]],
                             catalog: set, ctx: _RunContext) -> List[Dict[str, Any]]:
        """候选分类：目录锚点直接采信；目录外只对可动态核验的类别尝试动态核验。"""
        sources: List[Dict[str, Any]] = []
        verified_pages: List[Dict[str, Any]] = []
        dynamic_used = 0
        max_dynamic = int(self.rules["max_dynamic_domains"])
        allow_dynamic = (bool(self.rules["allow_dynamic_authority"])
                         and bool(self.authority_config.get("dynamic_enabled", True)))
        for item in infos:
            info: SourceInfo = item["info"]
            host = item["host"]
            verification, basis = "unverified", ""
            if info.domain in catalog:
                verification = "catalog_anchor"
                basis = f"信源目录锚点（{info.tier}）"
            elif info.authority_class in self.dynamic_classes:
                if not allow_dynamic:
                    basis = "动态核验未开启（allow_dynamic_authority=false），按未核验处理"
                elif dynamic_used >= max_dynamic:
                    basis = f"动态核验候选已达上限 {max_dynamic}，本次未做身份核验"
                else:
                    dynamic_used += 1
                    page, _ = self._do_fetch(st, item["url"], [host], "sources", ctx)
                    if page is None:
                        basis = "候选页面未读取，无法进行动态核验"
                    else:
                        signals = dynamic_authority_signals(
                            item["url"], [page], authority_config=self.authority_config,
                            verified_pages=verified_pages)
                        if signals.get("verified"):
                            verification = "session_verified"
                            basis = signals["basis"]
                            verified_pages.append(page)
                        else:
                            basis = signals["basis"] + "（信号不足，未核验）"
            elif info.authority_class == "media":
                basis = "未核验（媒体类不动态升格，只作证据倾向）"
            else:
                basis = "未核验（目录外域名，且不在可动态核验的类别内）"
            sources.append({
                "source_id": info.source_id, "name": item["title"], "domain": host,
                "tier": info.tier, "source_kind": info.source_kind,
                "authority_class": info.authority_class, "verification": verification,
                "verification_basis": basis, "url": item["url"],
                "usable": False,
            })
        return sources

    def _apply_inbound_links(self, sources: List[Dict[str, Any]], ctx: _RunContext) -> None:
        """已核验页面直链：从**目录锚点**页面正文里代码抽链，命中候选即记 verified_site_link。

        链接源只认 catalog_anchor：session_verified 页面自己只是佐证级，拿它当链接源
        就等于让一个"自 signals"的动态来源把零身份证据的域名抬成决定性（§5.3.1）。
        """
        for entry in sources:
            if entry["verification"] != "unverified":
                continue
            host = entry["domain"]
            linked: set = set()
            for other in sources:
                if other["verification"] != "catalog_anchor":
                    continue
                text = _page_text(ctx, other["url"])
                for match in _URL_RE.findall(text):
                    if _domain_of(_host_of(match), host):
                        linked.add(other["domain"])
                        break
            if linked:
                entry["verification"] = "verified_site_link"
                entry["verification_basis"] = ("已核验站点页面直接给出的链接（"
                                               + "、".join(sorted(linked)) + "）")

    def _text_group(self, ctx: "_RunContext", url: Any) -> str:
        """某来源已读正文的哈希组（判"是不是同一份正文"用；没读过返回空串）。"""
        cached = ctx.pages.get(url_key(url))
        if not cached or cached[0] is None:
            return ""
        return _body_group(cached[0].get("text") or "")

    def _compute_corroboration(self, sources: List[Dict[str, Any]],
                               ctx: _RunContext) -> Dict[str, Tuple[bool, str]]:
        """§5.3.4：session_verified 要成为决定性证据，需 ≥1 个独立已核验来源交叉佐证，
        或 ≥2 个**正文不同**的动态来源（互为佐证）。返回 {source_id: (是否佐证, 依据)}。

        "独立"按正文哈希判，不只看域名不同：两个镜像域名投放同一份正文时，
        域名不同但正文同组，证据会在去重阶段合并成一条，不能互为佐证。
        """
        out: Dict[str, Tuple[bool, str]] = {}
        dynamic: List[Dict[str, Any]] = [e for e in sources
                                         if e["verification"] == "session_verified"]
        dynamic_domains = {entry["domain"] for entry in dynamic}
        for entry in dynamic:
            host = entry["domain"]
            my_group = self._text_group(ctx, entry["url"])
            cross: set = set()
            for other in sources:
                if other["verification"] == "unverified" or other["domain"] == host:
                    continue
                if other["verification"] == "session_verified" \
                        and self._text_group(ctx, other["url"]) == my_group:
                    continue      # 同正文组（镜像/转载）：不构成独立佐证
                text = _page_text(ctx, other["url"])
                if host in text or any(_domain_of(_host_of(match), host)
                                       for match in _URL_RE.findall(text)):
                    cross.add(other["domain"])
            if cross:
                out[entry["source_id"]] = (True, "交叉佐证：" + "、".join(sorted(cross)))
                continue
            peers = [other for other in dynamic
                     if other["domain"] != host
                     and self._text_group(ctx, other["url"]) != my_group]
            if peers:
                others = "、".join(sorted(p["domain"] for p in peers))
                out[entry["source_id"]] = (True, f"独立动态来源互为佐证：{others}")
            else:
                reason = ("动态核验单点，无交叉佐证，只作倾向"
                          if len(dynamic_domains) < 2 else
                          "动态来源正文相同（镜像/转载），不构成独立佐证")
                out[entry["source_id"]] = (False, reason)
        return out

    # -- 阶段 3：逐主张检索/读页/抽引文 -------------------------------------

    def _is_user_link(self, url: Any, ctx: "_RunContext") -> bool:
        """该 URL 是否就是用户提供的那个链接（含 www./query 变体）。"""
        if not ctx.input_url_keys:
            return False
        if url_key(url) in ctx.input_url_keys:
            return True
        try:
            parts = urlsplit(str(url or ""))
            path = (parts.path or "/").rstrip("/") or "/"
        except ValueError:  # noqa: BLE001  畸形 URL 不当成用户链接
            return False
        return f"{_host_of(url)}{path}" in ctx.input_url_keys

    def _stage_evidence(self, st: _RunState, claims: List[Dict[str, Any]],
                        sources: List[Dict[str, Any]], ctx: _RunContext) -> None:
        records: List[Dict[str, Any]] = []
        per_claim_pages = int(self.rules["per_claim_max_pages"])
        for claim in claims:
            if claim["claim_status"] != "checkable":
                continue
            if st.is_cancelled():
                break
            queries = self._claim_queries(st, claim)
            pages_used = 0
            had_evidence = False
            claim_urls: set = set()
            for source in sources:
                if pages_used >= per_claim_pages or st.budget.blocked_reason or st.is_cancelled():
                    break
                for query in queries:
                    if pages_used >= per_claim_pages or st.budget.blocked_reason or st.is_cancelled():
                        break
                    # site: 由代码拼接；模型写的范围操作符已在 _claim_queries 剥掉
                    full_query = f"site:{source['domain']} {query}".strip()
                    results = self._do_search(st, full_query, 6, "evidence")
                    if results is None:
                        continue
                    if not results:
                        st.gap(f"主张 {claim['claim_id']} 在信源 {source['name']}"
                               f"（{source['domain']}）未检索到站内结果")
                        continue
                    rows = [row for row in results[:4]
                            if isinstance(row, dict) and row.get("url")
                            and _domain_of(_host_of(row.get("url")), source["domain"])]
                    if not rows:
                        st.gap(f"主张 {claim['claim_id']} 在信源 {source['name']}"
                               f"（{source['domain']}）未检索到站内结果")
                        continue
                    for row in rows:
                        if pages_used >= per_claim_pages or st.budget.blocked_reason \
                                or st.is_cancelled():
                            break
                        url = _clean(row.get("url"), 300)
                        if not url or url in claim_urls:
                            continue
                        if self._is_user_link(url, ctx):
                            # 用户提供的链接只作待核验原文，不回头给它自己作证
                            continue
                        claim_urls.add(url)
                        page, _ = self._do_fetch(st, url, [source["domain"]], "evidence", ctx)
                        if page is None:
                            continue
                        pages_used += 1
                        record = self._extract_quote(st, claim, source, page, row)
                        if record is not None:
                            records.append(record)
                            had_evidence = True
            if not had_evidence and not st.is_cancelled():
                # 检索成功但该主张没有可定位引文 = 合法空结果（不是失败），记限制不记缺口
                st.data["limitations"].append(
                    f"主张 {claim['claim_id']} 未在权威来源中找到可定位引文")
        self._dedupe_evidence(st, records)

    def _claim_queries(self, st: _RunState, claim: Mapping[str, Any]) -> List[str]:
        """检索词：主张原文 + 模型建议；先脱敏，再剥范围操作符，最后按上限截断。"""
        cap = int(self.rules["per_claim_max_queries"])
        out: List[str] = []
        for raw in [claim.get("text")] + list(claim.get("_queries") or []):
            query = strip_query_operators(strip_pii(raw))
            if query and query not in out:
                out.append(query)
            if len(out) >= cap:
                break
        if not out:
            st.gap(f"主张 {claim.get('claim_id')} 脱敏后无可用检索词")
        return out

    def _extract_quote(self, st: _RunState, claim: Mapping[str, Any], source: Mapping[str, Any],
                       page: Mapping[str, Any], row: Mapping[str, Any]
                       ) -> Optional[Dict[str, Any]]:
        """抽引文（复用 skills/evidence/SKILL.md）：quote 必须逐字定位已读正文。"""
        body = normalize_text(page.get("text") or "")
        url = _clean(page.get("final_url") or page.get("url"), 300)
        if not body:
            st.error("evidence", "empty_page_text",
                     f"{url or source['domain']} 读取成功但正文为空，已丢弃")
            return None
        contract = ('只输出 JSON：{"quote":"<摘自已读正文的原句>",'
                    '"relation":"supports|refutes|partial|background|unclear",'
                    '"limitations":["<该条证据的局限>"]}；'
                    "quote 必须逐字来自上面正文，relation 只能取封闭枚举；不做真假判断。")
        qualifiers = claim.get("qualifiers") or {}
        user = (f"主张：{claim['text']}\n"
                f"限定：时间={qualifiers.get('time') or '未注明'}，"
                f"地点={qualifiers.get('place') or '未注明'}，"
                f"实体={qualifiers.get('entity') or '未注明'}\n"
                f"信源：{source['name']}（{source['domain']}）"
                f"｜权威类别：{source['authority_class']}｜核验方式：{source['verification']}\n"
                f"已读正文（前4000字）：\n{body[:4000]}")
        obj, err = self._call_llm(st, load_skill_prompt(_EVIDENCE_SKILL_DIR, contract),
                                  user, "evidence:quote")
        if not isinstance(obj, dict):
            st.error("evidence", "quote_unavailable",
                     err or "模型未给出可解析的引文抽取")
            return None
        quote = normalize_text(obj.get("quote"))
        relation = str(obj.get("relation") or "").strip().lower()
        if relation not in RELATIONS:
            st.error("evidence", "bad_relation",
                     f"未知证据关系：{relation or '（空）'}，按 unclear 处理")
            relation = "unclear"
        if not quote:
            st.error("evidence", "quote_empty", "模型未给出引文")
            return None
        max_quote = int(self.rules["max_quote_chars"])
        if len(quote) > max_quote:
            shortened = quote[:max_quote]
            if locate_quote(body, shortened) is None:
                st.error("evidence", "quote_too_long",
                         f"引文超过 {max_quote} 字符且截断后仍无法定位，已丢弃")
                return None
            quote = shortened
        span = anchor_quote(body, quote)
        if span is None:
            # 只有"读到了正文但引文定位失败"才算引文未锚定；取页失败走 _do_fetch 的错误分类
            st.error("evidence", "quote_not_anchored",
                     "模型给出的引文未在已读正文中逐字定位，已丢弃")
            return None
        limitations = _string_items(obj.get("limitations"), 4, 120)
        if len(quote) < 20:
            limitations.insert(0, "引文较短，可支撑的上下文有限")
        return {
            "claim_ids": [str(claim["claim_id"])],
            "relation": relation,
            "source_id": source["source_id"],
            "name": source["name"], "domain": source["domain"], "tier": source["tier"],
            "authority_class": source["authority_class"],
            "verification": source["verification"],
            "authority_basis": source["verification_basis"],
            "title": _clean(page.get("title") or source["name"], 120),
            "url": url, "quote": quote, "quote_span": span,
            "published_at": _clean(page.get("published_at"), 40) or None,
            "fetched_at": _clean(page.get("fetched_at"), 40) or st.clock(),
            "independence_group": _body_group(body),
            "limitations": list(dict.fromkeys(limitations)),
        }

    def _dedupe_evidence(self, st: _RunState, records: List[Dict[str, Any]]) -> None:
        """阶段5 去重：同主张同引文/同正文只留一条；标题近似只记局限（代码不判等价）。"""
        out: List[Dict[str, Any]] = []
        kept: Dict[str, Dict[str, Any]] = {}
        seen_quote: Dict[Tuple[str, str], str] = {}
        seen_group: Dict[Tuple[str, str], str] = {}
        for record in records:
            claim_id = (record.get("claim_ids") or [""])[0]
            quote_key = (claim_id, normalize_quote(record.get("quote")))
            group_key = (claim_id, str(record.get("independence_group") or ""))
            duplicate = seen_quote.get(quote_key) or seen_group.get(group_key)
            if duplicate:
                kept[duplicate]["limitations"].append(
                    f"与 {duplicate} 引文或正文相同（转载/镜像），不构成独立证据")
                continue
            evidence_id = f"E{len(out) + 1}"
            record["evidence_id"] = evidence_id
            seen_quote[quote_key] = evidence_id
            seen_group[group_key] = evidence_id
            kept[evidence_id] = record
            out.append(record)
        for index, record in enumerate(out):
            for other in out[:index]:
                if near_duplicate_source(record, [other], 0.9):
                    record["limitations"].append(
                        f"与 {other['evidence_id']} 标题近似（可能是转载），独立性有限")
                    break
        for record in out:
            record["limitations"] = list(dict.fromkeys(record["limitations"]))
        st.data["evidence"] = out

    # -- 阶段 4：balance/lean（代码）+ 受约束分析（adjudicate/SKILL.md） -----

    def _stage_adjudicate(self, st: _RunState, claims: List[Dict[str, Any]],
                          ctx: _RunContext) -> None:
        evidence = st.data["evidence"]
        ctx.decisive = {str(ev.get("evidence_id")): self._is_decisive(ev, ctx)
                        for ev in evidence}
        by_claim: Dict[str, List[Dict[str, Any]]] = {}
        for ev in evidence:
            for claim_id in ev.get("claim_ids") or []:
                by_claim.setdefault(str(claim_id), []).append(ev)

        public: List[Dict[str, Any]] = []
        conflicts: List[Dict[str, Any]] = []
        for claim in claims:
            claim_id = str(claim["claim_id"])
            evs = by_claim.get(claim_id, [])
            pairs = [(str(ev.get("relation")), ctx.decisive.get(str(ev.get("evidence_id")), False))
                     for ev in evs]
            balance, lean = _decide_balance_lean(pairs, claim["claim_status"] == "checkable")
            supporting = [ev["evidence_id"] for ev in evs if ev.get("relation") == "supports"]
            refuting = [ev["evidence_id"] for ev in evs if ev.get("relation") == "refutes"]
            context = [ev["evidence_id"] for ev in evs
                       if ev.get("relation") in ("partial", "background", "unclear")]
            basis = list(dict.fromkeys(str(ev.get("authority_basis") or "")
                                       for ev in evs if ev.get("authority_basis")))
            public.append({
                "claim_id": claim_id,
                "text": claim["text"],
                "source_span": claim["source_span"],
                "balance": balance,
                "balance_label": _BALANCE_LABELS[balance],
                "lean": lean,
                "lean_label": _LEAN_LABELS[lean],
                "conclusion": _conclusion_text(balance, lean, claim, evs, ctx.decisive),
                "supporting_ids": supporting,
                "refuting_ids": refuting,
                "context_ids": context,
                "authority_basis": basis,
                "limitations": self._claim_limitations(claim, evs, balance, ctx),
            })
            if balance == "contested":
                conflicts.append({
                    "summary": f"主张 {claim_id} 的支持与反驳证据同时来自权威来源，"
                               "代码不做取舍，交由用户判断",
                    "evidence_ids": list(dict.fromkeys(supporting[:3] + refuting[:3])),
                })
        st.data["claims"] = public
        st.data["conflicts"] = conflicts
        self._stage_analysis(st, claims)

    def _is_decisive(self, ev: Mapping[str, Any], ctx: _RunContext) -> bool:
        info = ctx.infos.get(str(ev.get("source_id") or ""))
        if info is None:
            return False
        corroborated, _basis = ctx.corroborated.get(str(ev.get("source_id")), (False, ""))
        return is_decisive_source(info, authority_config=self.authority_config,
                                  corroborated=corroborated)

    def _claim_limitations(self, claim: Mapping[str, Any], evs: List[Dict[str, Any]],
                           balance: str, ctx: _RunContext) -> List[str]:
        """每条主张的局限（代码生成）：把"为什么只能给倾向"说清楚，不藏。"""
        limits: List[str] = []
        if not evs:
            limits.append("未找到可定位的权威资料引文")
            return limits
        if balance == "insufficient" and claim["claim_status"] == "checkable":
            limits.append("证据来自非决定性来源或未取得交叉佐证，不能据此证明或反驳")
        if not any(ev.get("relation") in ("supports", "refutes") for ev in evs):
            limits.append("已读资料均未直接对主当作证或反驳")
        decisive_groups = {ev.get("independence_group") for ev in evs
                           if ctx.decisive.get(str(ev.get("evidence_id")))}
        relevant = [ev for ev in evs if ev.get("relation") in ("supports", "refutes")]
        if relevant and len(decisive_groups) < len({ev.get("independence_group")
                                                    for ev in relevant}):
            limits.append("部分证据来自同一转载组，不构成独立佐证")
        if any(ev.get("verification") == "session_verified" for ev in evs):
            limits.append("含动态核验来源（仅本次运行有效，不写回信源目录）")
        if any(ev.get("authority_class") == "media" for ev in evs):
            limits.append("媒体来源只作证据倾向，不作为证明或反驳的依据")
        qualifiers = claim.get("qualifiers") or {}
        if not any(qualifiers.get(key) for key in ("time", "place", "entity")):
            limits.append("主张未注明时间/地点/实体，核验范围按可核查范围处理")
        return list(dict.fromkeys(limits))

    def _stage_analysis(self, st: _RunState, claims: List[Dict[str, Any]]) -> None:
        """LLM 受约束分析（adjudicate/SKILL.md）：只能引用已存在的 evidence_id。"""
        if not st.data["evidence"]:
            # 没有证据就没有可分析的对象：这是合法空结果，记限制不记缺口
            st.data["limitations"].append("没有可引用的证据，未生成受约束分析")
            return
        contract = ('只输出 JSON：{"analysis":[{"claim_id":"<主张id>","text":"<受约束分析>",'
                    '"evidence_ids":["<证据id>"]}]}；'
                    "claim_id 与 evidence_id 必须来自上面清单，不得新增 URL/日期/证据 ID；"
                    "不得改动 balance/lean/conclusion；不得出现真假判定词；"
                    "必须写明范围/时点/限制。")
        blocks: List[str] = []
        for claim in st.data["claims"]:
            blocks.append(f"主张 {claim['claim_id']}：「{claim['text']}」"
                          f"｜代码结论：{claim['balance']}（{claim['balance_label']}）"
                          f"｜倾向：{claim['lean']}（{claim['lean_label']}）"
                          f"｜conclusion：{claim['conclusion']}")
        evidence_lines = [
            render_evidence_line(ev) + f"｜权威类别：{ev.get('authority_class')}"
            f"｜核验方式：{ev.get('verification')}"
            for ev in st.data["evidence"]]
        user = ("请对下列主张逐条写受约束分析。\n主张与代码结论：\n" + "\n".join(blocks) +
                "\n\n证据清单：\n" + "\n".join(evidence_lines))
        obj, err = self._call_llm(st, load_skill_prompt(_ADJUDICATE_SKILL_DIR, contract),
                                  user, "analysis")
        rows = _first_list(obj, ("analysis", "items")) if isinstance(obj, dict) else None
        if not isinstance(rows, list):
            if err == "llm_budget_exhausted":
                st.gap("模型调用预算已达上限，受约束分析未执行")
                return
            st.error("analysis", "analysis_unavailable",
                     err or "模型未给出可解析的受约束分析")
            st.gap("受约束分析未生成（模型不可用），balance/lean/conclusion 仍为代码计算结果")
            return
        known_claims = {str(c["claim_id"]) for c in claims}
        known_evidence = {str(ev["evidence_id"]) for ev in st.data["evidence"]}
        out: List[Dict[str, Any]] = []
        for row in rows[:len(claims) * 2]:
            if not isinstance(row, dict):
                continue
            claim_id = str(row.get("claim_id") or "").strip()
            if claim_id not in known_claims:
                st.error("analysis", "unknown_claim_id",
                         f"分析指向未知主张，已丢弃：{claim_id or '（空）'}")
                continue
            text = _clean(row.get("text"), 600)
            if not text:
                st.error("analysis", "analysis_empty", "分析正文为空，已丢弃")
                continue
            if _VERDICT_RE.search(text):
                # 铁律 1：模型哪怕写出真假判定，也不许进输出
                st.error("analysis", "analysis_verdict_word",
                         "分析正文含固定真假判定词，已丢弃")
                continue
            if _URL_RE.search(text):
                st.error("analysis", "analysis_new_url",
                         "分析正文新增了 URL（只许引用已存在的证据），已丢弃")
                continue
            ids: List[str] = []
            for item in row.get("evidence_ids") or []:
                evidence_id = str(item or "").strip()
                if evidence_id not in known_evidence:
                    st.error("analysis", "unknown_evidence_id",
                             f"分析引用了不存在的证据 ID，已丢弃：{evidence_id or '（空）'}")
                    continue
                if evidence_id not in ids:
                    ids.append(evidence_id)
            if not ids:
                st.error("analysis", "analysis_without_evidence",
                         f"分析未引用任何已存在的证据，已丢弃（主张 {claim_id}）")
                continue
            out.append({"claim_id": claim_id, "text": text, "evidence_ids": ids})
        st.data["analysis"] = out

    # -- 阶段 5：收尾 -------------------------------------------------------

    def _finalize(self, st: _RunState, ctx: _RunContext) -> None:
        """运行级限制（代码生成）：把"这次核验没做到什么"写在明处。"""
        limits = st.data["limitations"]
        checkable = [c for c in st.data["claims"] if c.get("balance") != "not_checkable"]
        info = st.data.get("input") if isinstance(st.data.get("input"), dict) else {}
        if info.get("kind") == "link":
            limits.append(f"原文来自用户提供的链接（{_clean(info.get('url'), 200)}），"
                          "该链接未核验，仅作为待核验原文")
        if checkable and not st.data["evidence"]:
            limits.append("未取得任何可定位引文，全部可核查主张的证据倾向均为不足")
        else:
            if any(ev.get("verification") == "session_verified" for ev in st.data["evidence"]):
                limits.append("动态核验来源仅本次运行有效，不写回信源目录")
            if any(ev.get("authority_class") == "media" for ev in st.data["evidence"]):
                limits.append("媒体来源只作证据倾向，不作为证明或反驳的依据")
        limits.append("本核验只依据本次实际读取的页面正文；搜索摘要与页面自称均不作为证据")
        limits.append("本报告只述证据倾向，不作真假判定，也不给数值评分")
        st.data["limitations"] = list(dict.fromkeys(limits))

    def _finish(self, st: _RunState, started_at: str) -> Dict[str, Any]:
        task_status = "completed"
        if st.clarified:
            task_status = "needs_clarification"
        elif st.no_factual:
            task_status = "no_factual_claim"
        status = self._resolve_status(st, task_status)
        budget = st.budget.snapshot()
        result = {
            "tool_id": "verify_authority",
            "status": status,
            "task_status": task_status,
            "data": st.data,
            "gaps": list(st.gaps),
            "errors": list(st.errors),
            "audit": {
                "operations": list(st.audit["operations"]),
                "searches": int(budget["used"]["max_searches"]),
                "pages": int(budget["used"]["max_pages"]),
                "llm_calls": int(budget["used"]["max_llm_calls"]),
            },
            "budget": budget,
            "report": "",
            "started_at": started_at,
            "finished_at": st.clock(),
        }
        if st.clarified:
            reason = st.clarify_reason or "输入需要补充"
            result["gaps"].insert(0, f"需要用户补充：{reason}")
        if st.failed:
            result["gaps"].insert(0, "运行失败：未取得可核验的权威资料（检索不可用或模型不可用）")
        if st.is_cancelled():
            result["gaps"].insert(0, "运行被取消，已保留此前取得的结果")
        if st.budget.blocked_reason:
            result["gaps"].insert(0, f"预算/死线到顶提前停止（{st.budget.blocked_reason}），"
                                     "已保留此前取得的结果")
        result["report"] = render_report(result)
        return result

    @staticmethod
    def _resolve_status(st: _RunState, task_status: str) -> str:
        if st.is_cancelled():
            return "cancelled"
        if st.budget.deadline_hit() or st.budget.blocked_reason:
            return "budget_exhausted"
        if st.failed:
            return "failed"
        if st.clarified:
            return "needs_clarification"
        if task_status == "no_factual_claim" and not st.errors:
            # 没有可核查主张不是失败：连"没有可定位引文"的缺口都不记成 partial
            return "completed"
        if st.errors or st.gaps:
            return "partial"
        return "completed"

    def _progress(self, st: _RunState, stage: str, done: int) -> None:
        hook = self._on_progress
        if hook is None:
            return
        try:
            hook(stage, done, len(_PROGRESS_STAGES))
        except Exception:  # noqa: BLE001  进度回调异常不影响核验结果本身
            pass

    # -- IO 适配层 ----------------------------------------------------------

    def _call_llm(self, st: _RunState, system: str, user: str, target: str
                  ) -> Tuple[Optional[Dict[str, Any]], str]:
        """一次 LLM 调用：记账 + 审计。返回 (obj, err)，与 chat_json 契约同族。"""
        if self.llm_fn is None:
            return None, "llm_unavailable"
        if not st.budget.allow("max_llm_calls"):
            st.budget.block(st.budget.why("max_llm_calls"))
            return None, "llm_budget_exhausted"
        if st.is_cancelled():
            return None, "cancelled"
        st.budget.spend("max_llm_calls")
        entry = st.operation("llm", target, False)
        try:
            out = self.llm_fn(system, user)
        except Exception as exc:  # noqa: BLE001  模型侧异常按"本次调用失败"处理，不中断流水线
            return None, f"llm_exception:{type(exc).__name__}"
        if isinstance(out, tuple) and len(out) == 2:
            obj, err = out
        elif isinstance(out, dict):
            obj, err = out, ""
        else:
            return None, "llm_bad_shape"
        if not isinstance(obj, dict):
            return None, (str(err) if err else "llm_no_object")
        entry["ok"] = True
        return obj, ""

    def _do_search(self, st: _RunState, query: str, k: int, stage: str) -> Optional[list]:
        """一次检索。返回 None=检索不可用/降级（已记 error+gap），[]=合法空结果，[...] = 有结果。"""
        if self.search_fn is None:
            st.error(stage, "search_unavailable", "未注入检索实现，检索未执行")
            st.gap("检索未执行（未配置检索实现），覆盖不完整")
            return None
        if not st.budget.allow("max_searches"):
            st.budget.block(st.budget.why("max_searches"))
            return None
        if st.is_cancelled():
            return None
        st.budget.spend("max_searches")
        entry = st.operation("search", query, False)
        try:
            result = self.search_fn(query, k)
        except Exception as exc:  # noqa: BLE001  检索侧异常按降级处理，不当成"搜过了没找到"
            st.error(stage, "search_unavailable", f"检索异常：{type(exc).__name__}")
            st.gap("检索未执行/降级，覆盖不完整")
            return None
        if not isinstance(result, dict):
            st.error(stage, "search_unavailable", "检索返回形状不符")
            st.gap("检索未执行/降级，覆盖不完整")
            return None
        degraded = bool(result.get("degraded"))
        err = str(result.get("error") or "").strip()
        results = result.get("results")
        results = list(results) if isinstance(results, list) else []
        if degraded or err:
            # "没搜/搜降级"与"搜过了没找到"必须分开记（契约 §0.3）
            st.error(stage, "search_unavailable", err or "检索降级")
            st.gap("检索未执行/降级，覆盖不完整")
            return results or None
        entry["ok"] = True
        return results

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

    def _do_fetch(self, st: _RunState, url: str, allowed_domains: Optional[List[str]],
                  stage: str, ctx: _RunContext) -> Tuple[Optional[Dict[str, Any]],
                                                         Optional[Dict[str, str]]]:
        """一次读页（带运行内缓存：同一地址不重复抓、不重复扣预算）。

        返回 (page|None, error|None)；只有 status=ok 才计入读页预算。
        """
        key = url_key(url)
        if key in ctx.pages:
            return ctx.pages[key]
        fetch = self._fetch_callable()
        if fetch is None:
            error = {"stage": stage, "category": "fetch_unavailable",
                     "detail": "取页不可用：未注入 fetch_fn 且默认 transport 不可用"}
            st.error(stage, error["category"], error["detail"])
            ctx.pages[key] = (None, error)
            return None, error
        if not st.budget.allow("max_pages"):
            st.budget.block(st.budget.why("max_pages"))
            return None, None
        if st.is_cancelled():
            return None, None
        entry = st.operation("fetch", url, False)
        try:
            page = fetch(url, allowed_domains=allowed_domains,
                         cancel_check=st.cancel_check, deadline=st.budget.deadline)
        except Exception as exc:  # noqa: BLE001  取页异常按失败记，不当成"页面无引文"
            error = {"stage": stage, "category": "fetch_exception",
                     "detail": f"取页异常：{type(exc).__name__}"}
            st.error(stage, error["category"], error["detail"])
            ctx.pages[key] = (None, error)
            return None, error
        if not isinstance(page, dict):
            error = {"stage": stage, "category": "fetch_unavailable", "detail": "取页返回形状不符"}
            st.error(stage, error["category"], error["detail"])
            ctx.pages[key] = (None, error)
            return None, error
        if page.get("status") != "ok":
            category = _clean(page.get("category") or "fetch_error", 60) or "fetch_error"
            detail = _clean(page.get("detail") or "取页失败", 200)
            st.error(stage, category, detail)
            ctx.pages[key] = (None, {"stage": stage, "category": category, "detail": detail})
            return None, {"stage": stage, "category": category, "detail": detail}
        st.budget.spend("max_pages")
        entry["ok"] = True
        ctx.pages[key] = (page, None)
        return page, None


def verify_authority(text: str, **kw) -> Dict[str, Any]:
    """便捷函数 = AuthoritySkills(**kw).run(text)。工具层用它。"""
    return AuthoritySkills(**kw).run(text)


# ---------------------------------------------------------------------------
# 确定性纯文本报告（契约 §5.5）
# ---------------------------------------------------------------------------

def _report_field(value: Any, limit: int = 160) -> str:
    """非引文字段的渲染：去控制字符、把 HTML/markdown 标记字符换成全角等价物、压空白、截断。

    为什么换全角而不是删掉：报告是确定性纯文本，标题/域名/说明里混进 `<script>` 或
    `[x](url)` 就有注入 GUI 的风险；换成全角等价物既保留信息量，又让标记语法失效
    （全角括号不是 markdown 链接语法）。引文不走本函数——引文要逐字保留。
    """
    text = normalize_text(value)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    for raw, safe in (("<", "＜"), (">", "＞"), ("[", "［"), ("]", "］")):
        text = text.replace(raw, safe)
    return text[:limit]


def _report_quote(value: Any, limit: int) -> str:
    """引文渲染：逐字保留（契约要求报告引用逐字保留），只去控制字符并压空白。"""
    text = normalize_text(value)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    return text[:limit]


def _report_ids(values: Any) -> str:
    items = [str(v) for v in (values or []) if v]
    return "、".join(items) if items else "（无）"


def render_report(result: Dict[str, Any]) -> str:
    """把一次运行结果渲染成确定性纯文本报告（无 HTML、无 markdown 链接语法）。

    结构：主张（balance/lean/conclusion）→ 证据 → 受约束分析 → 来源 → 冲突 → 限制 →
    缺口 → 错误 → 预算。全文不出现固定真假判定词；"证明/反驳"只允许按契约 §5.4 的
    模板出现，且 insufficient 一律带"不能据此"限定。
    """
    if not isinstance(result, dict):
        return "权威核验报告：（结果形状不符，无可渲染内容）"
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    budget = result.get("budget") if isinstance(result.get("budget"), dict) else {}
    used = budget.get("used") if isinstance(budget.get("used"), dict) else {}
    limits_budget = budget.get("limits") if isinstance(budget.get("limits"), dict) else {}

    lines: List[str] = ["权威核验报告（只述证据倾向，不作真假判定）"]
    lines.append(f"执行状态：{_report_field(result.get('status'), 40) or '（未知）'}"
                 f"　任务状态：{_report_field(result.get('task_status'), 40) or '（未知）'}")
    lines.append(f"开始：{_report_field(result.get('started_at'), 40)}"
                 f"　结束：{_report_field(result.get('finished_at'), 40)}")
    info = data.get("input") if isinstance(data.get("input"), dict) else {}
    if info.get("kind") == "link":
        lines.append(f"原文来源：用户提供链接 {_report_field(info.get('url'), 200)}"
                     "（未核验，仅作为待核验原文）")

    # 一、主张
    claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
    lines.append("")
    lines.append(f"一、主张（共 {len(claims)} 条）")
    if not claims:
        lines.append("  （未拆出主张）")
    for item in claims:
        span = item.get("source_span") if isinstance(item.get("source_span"), dict) else {}
        state = "暂不核查" if item.get("balance") == "not_checkable" else "可核查"
        lines.append(f"  {_report_field(item.get('claim_id'), 12)} 「"
                     f"{_report_quote(item.get('text'), 200)}」"
                     f"（原文第 {span.get('start', 0)}-{span.get('end', 0)} 字符）状态：{state}")
        lines.append(f"      权威证据：{_report_field(item.get('balance'), 20)}"
                     f"（{_report_field(item.get('balance_label'), 20)}）"
                     f"｜倾向：{_report_field(item.get('lean'), 20)}"
                     f"（{_report_field(item.get('lean_label'), 20)}）")
        lines.append(f"      结论：{_report_field(item.get('conclusion'), 300)}")
        lines.append(f"      支持证据：{_report_ids(item.get('supporting_ids'))}"
                     f"｜反驳证据：{_report_ids(item.get('refuting_ids'))}"
                     f"｜背景：{_report_ids(item.get('context_ids'))}")
        basis = [_report_field(b, 120) for b in (item.get("authority_basis") or []) if b]
        if basis:
            lines.append("      权威依据：" + "；".join(basis))
        item_limits = [_report_field(x, 160) for x in (item.get("limitations") or []) if x]
        if item_limits:
            lines.append("      局限：" + "；".join(item_limits))

    # 二、证据
    evidence = [e for e in (data.get("evidence") or []) if isinstance(e, dict)]
    lines.append("")
    lines.append(f"二、证据（共 {len(evidence)} 条）")
    if not evidence:
        lines.append("  （没有可定位引文）")
    for item in evidence:
        relation = _RELATION_LABELS.get(str(item.get("relation")),
                                        _report_field(item.get("relation"), 20))
        # 共用的 render_evidence_line 不做转义；报告层先把非引文字段过一遍 _report_field
        # （引文仍逐字保留），避免页面标题里的 <b>/[x](url) 变成 GUI 里的标记语法
        safe = dict(item)
        for key in ("name", "domain", "title", "url"):
            safe[key] = _report_field(item.get(key), 300)
        lines.append("  " + render_evidence_line(safe) + f"｜关系：{relation}")
        lines.append(f"      权威类别：{_report_field(item.get('authority_class'), 20) or '（未知）'}"
                     f"｜核验方式：{_VERIFICATION_LABELS.get(str(item.get('verification')), _report_field(item.get('verification'), 20))}"
                     f"｜依据：{_report_field(item.get('authority_basis'), 160) or '（无）'}")
        span = item.get("quote_span") if isinstance(item.get("quote_span"), dict) else {}
        limits_text = "；".join(_report_field(x, 160) for x in (item.get("limitations") or []))
        lines.append(f"      独立组：{_report_field(item.get('independence_group'), 40)}"
                     f"｜正文第 {span.get('start', 0)}-{span.get('end', 0)} 字符"
                     f"｜局限：{limits_text or '（无）'}")
    # 三、受约束分析
    analysis = [a for a in (data.get("analysis") or []) if isinstance(a, dict)]
    lines.append("")
    lines.append(f"三、受约束分析（共 {len(analysis)} 条，基于证据的分析）")
    if not analysis:
        lines.append("  （无）")
    for item in analysis:
        lines.append(f"  {_report_field(item.get('claim_id'), 12)}："
                     f"{_report_field(item.get('text'), 600)}"
                     f"（引用 {_report_ids(item.get('evidence_ids'))}）")

    # 四、来源
    sources = [s for s in (data.get("sources") or []) if isinstance(s, dict)]
    verified = [s for s in sources if s.get("verified")]
    unverified = [s for s in sources if not s.get("verified")]
    lines.append("")
    lines.append(f"四、来源（已核验 {len(verified)} / 未核验 {len(unverified)}）")
    if not sources:
        lines.append("  （本次没有评估任何来源）")
    for item in verified:
        lines.append(f"  [已核验] {_report_field(item.get('name'), 80)}"
                     f"（{_report_field(item.get('domain'), 80)}）"
                     f"　档位：{_report_field(item.get('tier'), 30) or '（未登记）'}"
                     f"　权威类别：{_report_field(item.get('authority_class'), 20)}"
                     f"　核验：{_report_field(item.get('verification'), 40)}")
        if item.get("verification_basis"):
            lines.append(f"      依据：{_report_field(item.get('verification_basis'), 200)}")
    for item in unverified:
        lines.append(f"  [未核验] {_report_field(item.get('name'), 80)}"
                     f"（{_report_field(item.get('domain'), 80)}）"
                     f"　权威类别：{_report_field(item.get('authority_class'), 20)}"
                     f"　核验：{_report_field(item.get('verification'), 40)}")
        if item.get("verification_basis"):
            lines.append(f"      依据：{_report_field(item.get('verification_basis'), 200)}")

    # 五、冲突
    conflicts = [c for c in (data.get("conflicts") or []) if isinstance(c, dict)]
    lines.append("")
    lines.append(f"五、冲突（共 {len(conflicts)} 条）")
    if not conflicts:
        lines.append("  （无）")
    for item in conflicts:
        lines.append(f"  {_report_field(item.get('summary'), 200)}"
                     f"（证据 {_report_ids(item.get('evidence_ids'))}）")

    # 六、限制
    limits = [_report_field(x, 200) for x in (data.get("limitations") or []) if x]
    lines.append("")
    lines.append(f"六、限制（共 {len(limits)} 条）")
    if not limits:
        lines.append("  （无）")
    for index, text in enumerate(limits, start=1):
        lines.append(f"  {index}. {text}")

    # 七、缺口
    gaps = [_report_field(g, 240) for g in (result.get("gaps") or [])]
    lines.append("")
    lines.append(f"七、缺口（共 {len(gaps)} 条）")
    if not gaps:
        lines.append("  （无）")
    for index, text in enumerate(gaps, start=1):
        lines.append(f"  {index}. {text}")

    # 八、错误
    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    lines.append("")
    lines.append(f"八、错误（共 {len(errors)} 条）")
    if not errors:
        lines.append("  （无）")
    for item in errors:
        lines.append(f"  - [{_report_field(item.get('stage'), 20)}] "
                     f"{_report_field(item.get('category'), 40)}："
                     f"{_report_field(item.get('detail'), 200)}")

    # 九、预算与可观测
    searches = int(audit.get("searches") or 0)
    pages = int(audit.get("pages") or 0)
    llm_calls = int(audit.get("llm_calls") or 0)
    lines.append("")
    lines.append("九、预算与可观测")
    lines.append("  " + render_counts_line({"searches": searches, "pages": pages,
                                            "llm_calls": llm_calls}))
    lines.append(f"  搜索 {searches}/{limits_budget.get('max_searches', '-')}"
                 f"；读页 {pages}/{limits_budget.get('max_pages', '-')}"
                 f"；模型调用 {llm_calls}/{limits_budget.get('max_llm_calls', '-')}")
    lines.append(f"  主张 {len(claims)} 条；来源 {len(sources)} 个；证据 {len(evidence)} 条")
    if result.get("status") in ("cancelled", "budget_exhausted"):
        lines.append("  本次提前停止，上表为停止时点的真实消耗。")
    return "\n".join(lines)
