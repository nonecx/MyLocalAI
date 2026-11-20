# -*- coding: utf-8 -*-
"""取证三技能确定性编排（总览 §5 / 契约 §3）：只读取证，回答"谈什么、去哪查、查到什么"。

改了什么、为什么、锁定什么：
  - 新增 `assistant.skills.pipeline`：把领域识别 → 信源发现 → 证据检索三阶段编排成一条
    确定性流水线（`collect_evidence` / `EvidenceSkills.run`）。LLM 只做分类、选站、拆主张、
    抽引文；标签白名单、basis/引文/claim 的原文逐字定位、信源身份核验、`site:` 拼接、PII
    剥离、正文哈希+引文归一化去重、预算与取消、报告渲染全部由代码完成（总览 §5.4"代码约束，
    不依赖模型判断"）。
  - 锁定"不判真假、只述证据"：输出与 `render_report` 只述证据与关系，不出现真假判定词，
    也没有任何分数（铁律 1/2）。
  - 锁定"每个出口都有降级"：证据不足/不适用/检索降级/预算耗尽/取消都是合法输出，全部保留
    已取得的部分结果，绝不为有输出而编（铁律 3）。
  - 全离线可测：`search_fn` / `fetch_fn` / `llm_fn` / `transport` / `clock` 全部可注入，
    测试零网络零 LLM。

返回形状（契约 §3.3）与状态判定：
  status ∈ STATUSES；task_status ∈ TASK_STATUSES。取消与预算耗尽保留已取得证据；
  `task_status=no_factual_claim` 时 `status=completed`（没有可核查主张不是失败）。
  字符偏移口径：basis / claim / 引文的 span 都是**归一化正文**（normalize_text 之后）里的
  Unicode 码点偏移，与 `core.claim_checking` 的 offset_unit 一致；调用方若要用该校验器，
  需传入同一份归一化文本。

依赖方向：本模块只 import 标准库；取页默认实现走**延迟 import** 的
`assistant.skills.transport`（agent-transport 交付），import 失败时降级为"取页不可用"
错误条目，不影响其他阶段。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

__all__ = ["collect_evidence", "EvidenceSkills", "load_rules", "render_report",
           "strip_pii", "normalize_text", "normalize_quote", "locate_quote",
           "STATUSES", "TASK_STATUSES", "RELATIONS", "CLAIM_STATUSES"]

# 执行状态（总览 §5.5）：六种取值，取消/预算耗尽都保留已取得证据
STATUSES = ("completed", "partial", "needs_clarification", "failed",
            "cancelled", "budget_exhausted")
# 任务状态：no_factual_claim 与 needs_clarification 是两个特殊出口，其余都是 completed
TASK_STATUSES = ("completed", "no_factual_claim", "needs_clarification")
# 证据关系封闭枚举（《规格》§5.2，与 core.claim_checking.EVIDENCE_RELATIONS 一致）
RELATIONS = ("supports", "refutes", "partial", "background", "unclear")
CLAIM_STATUSES = ("checkable", "not_checkable")

_HERE = Path(__file__).resolve().parent

# 领域识别分类状态：四态是需要用户补充信息的出口，ok 才继续检索（契约 §3.2）
_DOMAIN_STATES = ("ok", "insufficient", "ambiguous", "uncovered", "multi_topic")
_DOMAIN_PROBLEM_STATES = ("insufficient", "ambiguous", "uncovered", "multi_topic")
_STATE_HINTS = {
    "insufficient": "原文信息不足（太短或只有情绪），无法判断领域",
    "ambiguous": "领域可判但主要议题不清，直接检索会发散",
    "uncovered": "原文领域不在既定领域表覆盖范围内",
    "multi_topic": "多个议题分量相当，无法确定主次",
}

# 档位排序（tier 取名来自 config/sources_registry.json；未知档名排最后，不写死档数）
_TIER_ORDER = ("官方权威", "权威媒体/辟谣平台", "一般门户媒体", "自媒体/UGC")

_RELATION_LABELS = {"supports": "支持", "refutes": "反驳", "partial": "部分一致",
                    "background": "背景", "unclear": "关系不明"}

# ---------------------------------------------------------------------------
# 规则（预算与上限）
# ---------------------------------------------------------------------------

# 内置默认值：与 rules.json 保持一致。rules.json 缺失/损坏时以此兜底，保证流水线可用。
DEFAULT_RULES: Dict[str, Any] = {
    "max_input_chars": 4000,
    "max_claims": 5,
    "max_sources": 6,
    "max_candidates": 8,
    "max_searches": 12,
    "max_pages": 12,
    "max_llm_calls": 20,
    "per_claim_max_queries": 2,
    "per_claim_max_pages": 3,
    "max_followup_rounds": 1,
    "total_deadline_s": 90,
    "io_timeout_s": 10,
    "max_page_bytes": 1500000,
    "max_text_chars": 20000,
    "max_redirect_hops": 3,
    "max_quote_chars": 300,
}

# 键名锁定（契约 §3.6）：count 类必须正整数，seconds 类必须正数；越界夹到安全区间。
# max_followup_rounds 为追问轮预算，本次单遍流程不消耗（留给后续多轮取证），
# io_timeout_s / max_page_bytes / max_text_chars / max_redirect_hops 由默认 transport 消费。
_RULE_LIMITS: Dict[str, Tuple[float, float]] = {
    "max_input_chars": (50, 200000),
    "max_claims": (1, 50),
    "max_sources": (1, 20),
    "max_candidates": (1, 30),
    "max_searches": (1, 200),
    "max_pages": (1, 200),
    "max_llm_calls": (1, 500),
    "per_claim_max_queries": (1, 10),
    "per_claim_max_pages": (1, 20),
    "max_followup_rounds": (0, 10),
    "total_deadline_s": (1, 3600),
    "io_timeout_s": (0.5, 300),
    "max_page_bytes": (1000, 50000000),
    "max_text_chars": (200, 500000),
    "max_redirect_hops": (0, 10),
    "max_quote_chars": (10, 5000),
}
_COUNT_KEYS = frozenset({"max_input_chars", "max_claims", "max_sources", "max_candidates",
                         "max_searches", "max_pages", "max_llm_calls", "per_claim_max_queries",
                         "per_claim_max_pages", "max_followup_rounds", "max_page_bytes",
                         "max_text_chars", "max_redirect_hops", "max_quote_chars"})


def _sanitize_rules(rules: Any) -> Dict[str, Any]:
    """把（可能来串自外部/被手改坏的）规则字典夹到安全区间。

    为什么夹而不是抛：rules.json 是运维可改的配置文件，改坏了不该让整条流水线崩掉；
    夹到区间内最多是"这次查得少一点"，比完全不查更符合降级文化。
    """
    src = rules if isinstance(rules, dict) else {}
    out: Dict[str, Any] = {}
    for key, default in DEFAULT_RULES.items():
        lo, hi = _RULE_LIMITS[key]
        value = src.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = default
        value = float(value)
        if key in _COUNT_KEYS:
            if value != int(value):
                value = float(int(value))
            value = int(value)
        value = max(lo, min(hi, value))
        out[key] = int(value) if isinstance(value, int) else value
    return out


def load_rules(path: Optional[Any] = None) -> Dict[str, Any]:
    """加载 rules.json（默认取包内相对路径），并与内置默认值合并后做区间校验。

    传 dict 时按"覆盖项"处理（与内置默认值合并后再校验），便于测试与调用方按需收紧额度；
    传路径时读该文件。文件缺失或不可解析 → 退回内置默认值（与 rules.json 现值一致），
    不抛异常：取不到预算配置时"按默认额度跑"比"整个工具不可用"更符合降级文化。
    """
    merged = dict(DEFAULT_RULES)
    target = _HERE / "rules.json"
    if isinstance(path, dict):
        merged.update({k: v for k, v in path.items() if k in DEFAULT_RULES})
    else:
        if path is not None:
            target = Path(path)
        try:
            raw = json.loads(Path(target).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001  配置文件缺失/损坏：降级到内置默认值
            return _sanitize_rules(merged)
        if isinstance(raw, dict):
            merged.update({k: v for k, v in raw.items() if k in DEFAULT_RULES})
    return _sanitize_rules(merged)


# ---------------------------------------------------------------------------
# 文本归一与定位（basis / claim / 引文共用同一套口径）
# ---------------------------------------------------------------------------

def normalize_text(value: Any) -> str:
    """空白归一：连续空白（含全角空格/换行/制表）压成单个半角空格并去首尾。

    定位一律在归一化文本上做：正文与模型输出只要空白不同就判"定位失败"会误伤太多，
    归一化后仍要求逐字一致（字符级，不做近义匹配）。
    """
    return " ".join(str(value or "").split())


def normalize_quote(value: Any) -> str:
    """引文归一：normalize_text 后再剥掉包裹引号与句读，用于去重比较。"""
    text = normalize_text(value)
    return text.strip("“”‘’\"'「」『』（）。，、；：！？.,;:!?—…- \t")


def locate_quote(body: Any, quote: Any) -> Optional[Dict[str, int]]:
    """在**已归一化**正文里逐字定位 quote，返回半开区间 {"start","end"}；找不到返回 None。"""
    hay = normalize_text(body)
    needle = normalize_text(quote)
    if not hay or not needle:
        return None
    index = hay.find(needle)
    if index < 0:
        return None
    return {"start": index, "end": index + len(needle)}


# ---------------------------------------------------------------------------
# 外发脱敏与检索词清洗（总览 §5.4）
# ---------------------------------------------------------------------------

_PII_PATTERNS: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "「邮箱」"),
    # 手机号：11 位、1 开头、第二位 3-9；两侧不许再跟数字，避免吃掉更长数字串
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "「手机号」"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "「证件号」"),
    (re.compile(r"(?<![A-Za-z0-9-])(?:\d[ -]?){13,19}(?![A-Za-z0-9-])"), "「长数字账号」"),
    (re.compile(r"https?://[^\s，。；、）】\"'<>]+"), "「链接」"),
    (re.compile(r"(?<!\w)www\.[^\s，。；、）】\"'<>]+"), "「链接」"),
)

# 模型生成的范围操作符：域名范围只能由代码按已核验信源拼 site:，模型写的一律剥掉
_QUERY_OPERATORS = ("site:", "inurl:", "allinurl:", "intitle:", "allintitle:",
                    "filetype:", "inanchor:", "intext:", "link:", "related:", "cache:")


def strip_pii(value: Any) -> str:
    """剥掉邮箱/手机号/证件号/长数字账号/URL，替换为中文占位（不含 ASCII 标记字符）。"""
    text = str(value or "")
    for pattern, placeholder in _PII_PATTERNS:
        text = pattern.sub(placeholder, text)
    return normalize_text(text)


def _strip_query_operators(query: Any) -> str:
    """剥掉模型生成的 site:/inurl: 等范围操作符（总览 §5.4"会被剥掉"）。"""
    text = str(query or "")
    for operator in _QUERY_OPERATORS:
        text = re.sub(re.escape(operator) + r"\s*\S+", " ", text, flags=re.IGNORECASE)
    return normalize_text(text)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _clean(value: Any, limit: int = 200) -> str:
    """压空白 + 截断：所有进入输出结构的自由文本都过这里。"""
    return normalize_text(value)[:limit]


def _host_of(url: Any) -> str:
    """取主机名（小写、去尾点、去 www.）；解析失败返回空串。"""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except Exception:  # noqa: BLE001  urlsplit 对畸形输入可能抛；此处只求一个域名
        return ""
    return host[4:] if host.startswith("www.") else host


def _domain_of(host: str, domain: str) -> bool:
    """host 是否落在 domain 内（相等或子域）：a.b.com 命中 b.com。"""
    host = (host or "").lower().rstrip(".")
    domain = (domain or "").lower().rstrip(".")
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


def _tier_rank(tier: Any) -> int:
    name = str(tier or "")
    return _TIER_ORDER.index(name) if name in _TIER_ORDER else len(_TIER_ORDER) + 1


def _body_group(body: str) -> str:
    """正文哈希组：同一篇文章转载/镜像到别处也落进同一组（转载不产生新证据）。"""
    digest = hashlib.sha256(normalize_text(body).encode("utf-8")).hexdigest()[:12]
    return "G-" + digest


def _utc_now_iso() -> str:
    """默认时钟：带时区的 ISO-8601 秒级串（core.claim_checking 要求带偏移）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


# ---------------------------------------------------------------------------
# 资源文件（taxonomy / sources / 三个 SKILL.md）
# ---------------------------------------------------------------------------

def _read_json_resource(name: str) -> Dict[str, Any]:
    """读包内 JSON 资源；缺失/损坏返回 {}（调用方按"没有资源"降级）。"""
    try:
        raw = json.loads((_HERE / name).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  资源缺失不应让流水线崩，降级成"无目录/无领域表"
        return {}
    return raw if isinstance(raw, dict) else {}


def _load_taxonomy() -> Dict[str, Any]:
    return _read_json_resource("taxonomy.json")


def _load_sources() -> Dict[str, Any]:
    return _read_json_resource("sources.json")


def _skill_prompt(skill: str, contract: str) -> str:
    """把 SKILL.md 正文当作 system prompt 素材，再接一段机器可解析的 JSON 契约。

    为什么还读文件：SKILL.md 是给模型的技能指令（契约 §3.8），口径只在那一处维护；
    pipeline 只补"输出必须是这个 JSON"的硬契约，避免同一口径两处漂移。
    文件读不到时退回内置精简指令，保证阶段仍可跑。
    """
    body = ""
    try:
        raw = (_HERE / skill / "SKILL.md").read_text(encoding="utf-8")
        body = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL).strip()
    except Exception:  # noqa: BLE001  SKILL.md 缺失：退回内置精简指令
        body = _FALLBACK_PROMPTS.get(skill, "")
    return f"{body}\n\n{contract}".strip()


_FALLBACK_PROMPTS = {
    "domain": "你是领域识别助理：只判断原文在谈什么，不判真假、不联网、不评分。",
    "sources": "你是信源发现助理：从给定目录里挑相关信源，身份判断必须引用已读正文原句。",
    "evidence": "你是证据检索助理：拆主张、抽引文，引文必须逐字来自已读正文。",
}


# ---------------------------------------------------------------------------
# 预算与运行态
# ---------------------------------------------------------------------------

class _Budget:
    """计数预算 + 绝对死线。blocked 表示"有计划内操作因预算/死线被跳过"。"""

    def __init__(self, rules: Dict[str, Any], deadline: Optional[float]) -> None:
        self.limits: Dict[str, Any] = dict(rules)
        self.used: Dict[str, Any] = {key: 0 for key in rules}
        self.deadline = deadline
        self.blocked = False
        self.blocked_reason = ""

    def remaining(self, key: str) -> int:
        return max(0, int(self.limits.get(key, 0)) - int(self.used.get(key, 0)))

    def allow(self, key: str, cost: int = 1) -> bool:
        if self.remaining(key) < cost:
            return False
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return False
        return True

    def why(self, key: str) -> str:
        """本次被拦住的原因：分不清"次数到顶"和"死线到了"会让报告说谎。"""
        if self.remaining(key) < 1:
            return f"{key} 已达上限"
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return "总死线已到"
        return "预算不足"

    def spend(self, key: str, cost: int = 1) -> None:
        self.used[key] = int(self.used.get(key, 0)) + cost

    def block(self, reason: str) -> None:
        self.blocked = True
        if not self.blocked_reason:
            self.blocked_reason = reason

    def snapshot(self) -> Dict[str, Any]:
        """used 与 limits 同键（契约 §3.3），便于逐项对拍。"""
        return {"used": {key: int(self.used.get(key, 0)) for key in self.limits},
                "limits": dict(self.limits)}


class _RunState:
    """一次运行的共享状态：错误、缺口、审计、预算、终止原因。"""

    def __init__(self, rules: Dict[str, Any], cancel_check: Optional[Callable[[], bool]],
                 deadline: Optional[float], clock: Callable[[], str], raw_text: str) -> None:
        self.rules = rules
        self.cancel_check = cancel_check
        self.clock = clock
        self.raw_text = raw_text
        self.deadline = deadline
        self.budget = _Budget(rules, deadline)
        self.errors: List[Dict[str, str]] = []
        self.gaps: List[str] = []
        self.operations: List[Dict[str, Any]] = []
        self._seen_messages: set = set()
        self.cancelled = False
        self.clarify_reason = ""
        self.failed_reason = ""
        self.checkable_claims = 0
        self.claims_assessed = False   # 阶段3 是否已得出"有没有可核查主张"的结论
        self.data: Dict[str, Any] = {
            "domain": {"primary": "", "secondary": [], "region": "", "time": "",
                       "labels": [], "basis": []},
            "sources": [], "claims": [], "evidence": [], "chains": [],
        }

    # -- 记录 ---------------------------------------------------------------
    def error(self, stage: str, category: str, detail: str) -> None:
        key = (stage, category, detail)
        if key in self._seen_messages:      # 同一错误重复出现只记一次，报告不刷屏
            return
        self._seen_messages.add(key)
        self.errors.append({"stage": stage, "category": _clean(category, 60),
                            "detail": _clean(detail, 240)})

    def gap(self, text: str) -> None:
        message = _clean(text, 240)
        if message and message not in self.gaps:
            self.gaps.append(message)

    def clarify(self, reason: str) -> None:
        if not self.clarify_reason:
            self.clarify_reason = _clean(reason, 240)

    def fail(self, reason: str) -> None:
        if not self.failed_reason:
            self.failed_reason = _clean(reason, 240)

    def operation(self, kind: str, target: str, ok: bool) -> Dict[str, Any]:
        entry = {"kind": kind, "target": _clean(target, 120), "ok": bool(ok)}
        self.operations.append(entry)
        return entry

    # -- 终止判定 -----------------------------------------------------------
    def is_cancelled(self) -> bool:
        if self.cancelled or self.cancel_check is None:
            return self.cancelled
        try:
            hit = bool(self.cancel_check())
        except Exception:  # noqa: BLE001  cancel_check 本身抛错时按"未取消"继续，不让用户取消机制反咬
            return False
        if hit:
            self.cancelled = True
        return self.cancelled


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class EvidenceSkills:
    """取证三技能流水线（领域识别 → 信源发现 → 证据检索 → 去重 → 报告）。

    全部 IO 可注入：`llm_fn(system, user) -> (obj, err)`（chat_json 契约）、
    `search_fn(query, k) -> {"results": [...], "degraded": bool, "error": str?}`、
    `fetch_fn(url, *, allowed_domains, cancel_check, deadline) -> transport.fetch 形状`。
    未注入 fetch_fn 时延迟 import `assistant.skills.transport` 的默认实现，import 失败
    降级为"取页不可用"错误条目（并行开发期 transport 可能尚未就位）。
    """

    def __init__(self, *, rules: Optional[Dict[str, Any]] = None,
                 search_fn: Optional[Callable[[str, int], Dict[str, Any]]] = None,
                 fetch_fn: Optional[Callable[..., Dict[str, Any]]] = None,
                 llm_fn: Optional[Callable[[str, str], Any]] = None,
                 transport: Optional[Any] = None,
                 clock: Optional[Callable[[], str]] = None) -> None:
        self.rules = _sanitize_rules(rules) if rules is not None else load_rules()
        self.search_fn = search_fn
        self.fetch_fn = fetch_fn
        self.llm_fn = llm_fn
        self.transport = transport
        self.clock = clock or _utc_now_iso
        self._fetch_probed = fetch_fn is not None

    # -- 入口 ---------------------------------------------------------------
    def run(self, text: str, *, cancel_check: Optional[Callable[[], bool]] = None,
            deadline: Optional[float] = None) -> Dict[str, Any]:
        """跑完整条流水线，返回契约 §3.3 的统一运行外壳。"""
        started_at = self.clock()
        raw = text if isinstance(text, str) else ("" if text is None else str(text))
        if deadline is None:
            # 未传死线时按 total_deadline_s 自建：预算上限不该因为调用方没传就失效
            deadline = time.monotonic() + float(self.rules["total_deadline_s"])
        st = _RunState(self.rules, cancel_check, deadline, self.clock, raw)

        if not self._precheck(st):
            return self._finish(st, started_at)
        if st.is_cancelled():
            st.gap("运行在读入原文后被取消，未开始检索")
            return self._finish(st, started_at)

        if self._stage_domain(st):
            if self._stage_sources(st):
                self._stage_evidence(st)

        self._dedup(st)
        return self._finish(st, started_at)

    # -- 预检 ---------------------------------------------------------------
    def _precheck(self, st: _RunState) -> bool:
        if not st.raw_text.strip():
            st.error("precheck", "empty_input", "原文为空或只有空白字符")
            st.clarify("原文为空：请提供需要取证的内容")
            return False
        limit = int(self.rules["max_input_chars"])
        if len(st.raw_text) > limit:
            st.error("precheck", "input_too_long",
                     f"原文 {len(st.raw_text)} 字符，超过上限 {limit}")
            st.clarify(f"原文长度 {len(st.raw_text)} 超过单次上限 {limit}："
                       "请缩小范围或分段提交")
            return False
        return True

    # -- 阶段 1：领域识别 ----------------------------------------------------
    def _stage_domain(self, st: _RunState) -> bool:
        taxonomy = _load_taxonomy()
        domains = [d for d in (taxonomy.get("domains") or []) if isinstance(d, dict)]
        ids = [str(d.get("id") or "") for d in domains if d.get("id")]
        examples: List[str] = []
        for entry in domains:
            examples.extend(str(x) for x in (entry.get("examples") or []) if x)
        whitelist = set(ids) | set(examples)
        contract = (
            '只输出 JSON，键名锁定：{"state":"ok|insufficient|ambiguous|uncovered|multi_topic",'
            '"primary":"<领域id>","secondary":["<领域id>"],"region":"CN|GLOBAL|US|EU|OTHER|\'\'",'
            '"time":"<原文时间锚点或空串>","labels":["<白名单标签>"],'
            '"basis":["<原文逐字片段>"]}\n'
            f"领域 id 只能取：{'、'.join(ids) or '（无）'}；labels 与 basis 同样受白名单与"
            "原文定位校验，不通过即被代码丢弃。")
        obj, err = self._call_llm(st, _skill_prompt("domain", contract),
                                  f"用户原文：\n{st.raw_text}", "domain")
        if obj is None:
            if err == "llm_budget_exhausted":
                st.gap("模型调用预算已达上限，领域识别未执行")
                return False
            if st.is_cancelled():
                return False
            # 连阶段1都进不去（契约 §3.3 的 failed 定义）：没有分类就没有选站依据
            st.error("domain", "llm_unavailable", err or "模型不可用，无法进行领域识别")
            st.fail("领域识别无法开始：" + (err or "模型不可用"))
            st.gap("领域识别未执行（模型不可用），后续阶段未开始")
            return False
        if not isinstance(obj, dict):
            st.error("domain", "llm_bad_shape", "阶段1返回不是 JSON 对象")
            st.fail("领域识别结果无法解析（模型输出损坏）")
            return False

        state = str(obj.get("state") or obj.get("status") or "").strip().lower()
        primary = str(obj.get("primary") or "").strip()
        secondary = [str(x).strip() for x in (obj.get("secondary") or [])
                     if isinstance(x, str)]
        region = _clean(obj.get("region"), 16)
        time_text = _clean(obj.get("time"), 60)

        labels: List[str] = []
        for item in _string_items(obj.get("labels"), 24, 40):
            if item not in whitelist:
                st.error("domain", "label_not_in_taxonomy",
                         f"标签不在白名单，已丢弃：{item}")
                continue
            if item not in labels:
                labels.append(item)

        normalized = normalize_text(st.raw_text)
        basis: List[str] = []
        for item in _string_items(obj.get("basis"), 12, 120):
            if locate_quote(normalized, item) is None:
                st.error("domain", "basis_not_anchored",
                         f"判断依据未在原文中逐字定位，已丢弃：{item}")
                continue
            if item not in basis:
                basis.append(item)

        if state not in _DOMAIN_STATES:
            if state:
                st.error("domain", "bad_state", f"未知分类状态：{state}，按歧义处理")
            state = "ok" if (primary in set(ids) and labels) else "ambiguous"

        if primary and primary not in set(ids):
            st.error("domain", "domain_not_in_taxonomy",
                     f"主领域不在领域表内，已丢弃：{primary}")
            primary = ""
        kept_secondary: List[str] = []
        for item in secondary:
            if item not in set(ids) or item == primary or item in kept_secondary:
                if item not in set(ids):
                    st.error("domain", "domain_not_in_taxonomy",
                             f"次领域不在领域表内，已丢弃：{item}")
                continue
            kept_secondary.append(item)
        if not primary:
            state = "uncovered"

        st.data["domain"] = {"primary": primary, "secondary": kept_secondary,
                             "region": region, "time": time_text,
                             "labels": labels, "basis": basis}

        if state in _DOMAIN_PROBLEM_STATES:
            hint = _STATE_HINTS.get(state, state)
            detail = f"领域识别状态：{state}（{hint}）"
            st.error("domain", f"state_{state}", detail)
            st.clarify(f"{hint}。请补充一句说明这段内容在讲什么（涉及时间/地点/主体更有用）")
            return False
        if not labels:
            st.error("domain", "labels_empty", "白名单校验后标签为空")
            st.clarify("未能从原文识别出可用议题标签：请补充这段内容的核心议题")
            return False
        if not basis:
            st.error("domain", "basis_empty", "白名单校验后判断依据为空")
        return True

    # -- 阶段 2：信源发现 ----------------------------------------------------
    def _stage_sources(self, st: _RunState) -> bool:
        catalog = [s for s in (_load_sources().get("sources") or []) if isinstance(s, dict)]
        info = st.data["domain"]
        matches = [entry for entry in catalog
                   if self._domain_match(entry, info) and self._region_match(entry, info)]
        matches.sort(key=lambda e: (0 if e.get("anchor") else 1,
                                    _tier_rank(e.get("tier"))))
        if not matches:
            # 目录没有匹配条目不等于没有信源：中性机构搜索仍要跑一次（契约 §3.2 的三路候选）
            st.gap("信源目录里没有与该领域/地区匹配的条目，仅尝试一次中性机构搜索")
        selected = self._select_sources(st, matches, info) if matches else []
        searched = self._neutral_search(st, info)

        candidates: List[Dict[str, Any]] = []
        seen_domains: set = set()
        for entry in list(selected) + list(searched):
            domain = str(entry.get("domain") or "").strip().lower().rstrip(".")
            if not domain or domain in seen_domains:
                # 同一域名只产生一个信源：换子站/镜像不构成第二个权威
                continue
            seen_domains.add(domain)
            candidates.append(entry)
            if len(candidates) >= int(self.rules["max_candidates"]):
                break
        if not candidates:
            st.gap("没有可评估的信源候选")
            return True

        pages: Dict[str, Dict[str, Any]] = {}
        for cand in candidates:
            if st.is_cancelled():
                break
            if not st.budget.allow("max_pages"):
                st.budget.block(st.budget.why("max_pages"))
                st.gap("读页预算已达上限，部分信源首页未读取")
                break
            page, _ = self._do_fetch(st, f"https://{cand['domain']}/",
                                     [cand["domain"]], "sources")
            if page is not None:
                pages[cand["source_id"]] = page

        notes = self._judge_identities(st, candidates, pages)
        self._finalize_sources(st, candidates, pages, notes)
        return True

    @staticmethod
    def _domain_match(entry: Dict[str, Any], info: Dict[str, Any]) -> bool:
        wanted = [info.get("primary")] + list(info.get("secondary") or [])
        wanted = [w for w in wanted if w]
        domains = [str(d) for d in (entry.get("domains") or []) if d]
        if not domains:
            return True
        return any(w in domains for w in wanted)

    @staticmethod
    def _region_match(entry: Dict[str, Any], info: Dict[str, Any]) -> bool:
        region = str(info.get("region") or "").strip()
        regions = [str(r) for r in (entry.get("regions") or []) if r]
        if not region or not regions:
            return True
        return region in regions or "GLOBAL" in regions

    def _select_sources(self, st: _RunState, matches: List[Dict[str, Any]],
                        info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """让模型从目录候选里挑信源；挑不动/挑坏了按目录序取前 N 个（确定性兜底）。"""
        cap = int(self.rules["max_sources"])
        by_id = {str(e.get("source_id")): e for e in matches}
        listing = "\n".join(
            f"- {e.get('source_id')} | {_clean(e.get('name'), 60)} | {e.get('domain')} | "
            f"{_clean(e.get('tier'), 30)} | 资料类型：{'、'.join(str(x) for x in (e.get('material_types') or []))}"
            for e in matches[:20])
        contract = (f'只输出 JSON：{{"selected": ["<source_id>", ...]}}，'
                    f"最多 {cap} 个，且只能从上面清单里选。")
        user = (f"领域：{info.get('primary') or ''} 次领域：{'、'.join(info.get('secondary') or [])}\n"
                f"议题标签：{'、'.join(info.get('labels') or [])}　地区：{info.get('region') or ''}\n"
                f"候选信源目录：\n{listing}\n请挑出最相关的信源。")
        obj, err = self._call_llm(st, _skill_prompt("sources", contract), user,
                                  "sources:select")
        picked: List[str] = []
        rows = _first_list(obj, ("selected", "sources", "picked")) if isinstance(obj, dict) else None
        if not isinstance(rows, list):
            st.error("sources", "select_fallback",
                     err or f"模型未给出可解析的选站结果，已按目录序取前 {cap} 个")
        else:
            for item in _string_items(rows, cap, 60):
                if item in by_id:
                    picked.append(item)
                else:
                    st.error("sources", "unknown_source_id",
                             f"模型选了候选清单外的 source_id，已忽略：{item}")
        if not picked:
            picked = [str(e.get("source_id")) for e in matches[:cap]]
        return [by_id[s] for s in dict.fromkeys(picked) if s in by_id]

    def _neutral_search(self, st: _RunState, info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """一次中性机构搜索：只用来发现目录外域名，结果仍要过同一套核验。"""
        labels = list(info.get("labels") or [])[:2]
        primary = str(info.get("primary") or "")
        taxonomy = {str(d.get("id")): str(d.get("name") or "")
                    for d in (_load_taxonomy().get("domains") or []) if isinstance(d, dict)}
        parts = [taxonomy.get(primary) or primary] + labels + ["官方", "机构"]
        query = _strip_query_operators(strip_pii(" ".join(p for p in parts if p)))
        if not query:
            return []
        results = self._do_search(st, query, 6, "sources")
        if not results:
            return []
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for row in results[:8]:
            if not isinstance(row, dict):
                continue
            host = _host_of(row.get("url"))
            if not host or host in seen:
                continue
            seen.add(host)
            out.append({"source_id": "CAND-" + host.replace(".", "-"),
                        "name": _clean(row.get("title") or host, 60) or host,
                        "domain": host, "tier": "未登记",
                        "domains": [], "regions": [], "material_types": [],
                        "anchor": False, "from_search": True})
            if len(out) >= 3:
                break
        return out

    def _judge_identities(self, st: _RunState, candidates: List[Dict[str, Any]],
                          pages: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
        """一次批量调用让模型给身份/适用范围；引文必须逐字在已读正文里定位。"""
        notes: Dict[str, Dict[str, str]] = {}
        fetched = [c for c in candidates if c["source_id"] in pages]
        if not fetched:
            return notes
        blocks = []
        for index, cand in enumerate(fetched):
            body = normalize_text(pages[cand["source_id"]].get("text") or "")
            blocks.append(f"[{index}] source_id={cand['source_id']} 名称={cand.get('name')} "
                          f"域名={cand['domain']} 档位={cand.get('tier')}\n"
                          f"已读正文（前1500字）：{body[:1500]}")
        contract = ('只输出 JSON：{"notes":[{"source_id":"<候选id>","identity":"<机构身份一句话>",'
                    '"scope":"<适用范围一句话>","basis_quote":"<摘自该正文的原句>"}]}；'
                    "basis_quote 必须逐字出现在对应正文里，否则该条会被代码丢弃。")
        obj, err = self._call_llm(st, _skill_prompt("sources", contract),
                                  "\n\n".join(blocks), "sources:identity")
        rows = _first_list(obj, ("notes", "identities", "items")) if isinstance(obj, dict) else None
        if not isinstance(rows, list):
            # 模型没给出可解析的身份判断：所有候选都会退成未核验，必须留一条错误说明原因
            st.error("sources", "identity_unavailable",
                     err or "模型未给出可解析的信源身份判断")
            return notes
        if not rows:
            st.error("sources", "identity_unavailable", "模型未给出任何信源身份判断")
            return notes
        by_id = {c["source_id"]: c for c in fetched}
        for row in rows[:len(fetched)]:
            if not isinstance(row, dict):
                continue
            source_id = str(row.get("source_id") or "").strip()
            cand = by_id.get(source_id)
            if cand is None:
                st.error("sources", "unknown_source_id",
                         f"身份判断指向未知候选，已忽略：{source_id}")
                continue
            quote = _clean(row.get("basis_quote"), 200)
            body = normalize_text(pages[source_id].get("text") or "")
            if not quote or locate_quote(body, quote) is None:
                st.error("sources", "basis_quote_not_anchored",
                         f"{cand['domain']} 的身份依据未在已读正文中定位，该候选降级为未核验")
                continue
            notes[source_id] = {"identity": _clean(row.get("identity"), 120),
                                "scope": _clean(row.get("scope"), 160),
                                "basis": quote}
        return notes

    def _finalize_sources(self, st: _RunState, candidates: List[Dict[str, Any]],
                          pages: Dict[str, Dict[str, Any]],
                          notes: Dict[str, Dict[str, str]]) -> None:
        """程序独立判定可信锚点：目录 anchor，或已核验页面里直接给出的链接。"""
        verification: Dict[str, str] = {}
        for cand in candidates:
            if cand.get("anchor"):
                verification[cand["source_id"]] = "catalog_anchor"
        # 从已核验页面正文里抽站外链接（代码抽取，不靠模型）；未核验页面不产生链接证据
        for _ in range(len(candidates) + 1):
            changed = False
            linked: set = set()
            for cand in candidates:
                if verification.get(cand["source_id"]) not in (None, "unverified_candidate"):
                    page = pages.get(cand["source_id"])
                    if page:
                        linked |= self._links_in(normalize_text(page.get("text") or ""))
            for cand in candidates:
                if verification.get(cand["source_id"]):
                    continue
                if any(_domain_of(host, cand["domain"]) for host in linked):
                    verification[cand["source_id"]] = "verified_site_link"
                    changed = True
            if not changed:
                break

        verified_count = 0
        max_sources = int(self.rules["max_sources"])
        for cand in candidates:
            source_id = cand["source_id"]
            kind = verification.get(source_id) or "unverified_candidate"
            note = notes.get(source_id) or {}
            page = pages.get(source_id)
            if kind == "unverified_candidate":
                if page is None and not cand.get("anchor"):
                    reason = "首页未读取或读取失败，且不在可信锚点之列"
                elif source_id not in notes:
                    reason = "身份判断缺少可在已读正文定位的依据（来源不自我认证）"
                else:
                    reason = "既非目录锚点，也未从已核验站点页面直接链接到"
                scope_note = reason
                basis = ""
            else:
                if verified_count >= max_sources:
                    # 已核验信源数量到顶：不再进阶段3，但仍如实记录它是可信锚点
                    kind = "unverified_candidate"
                    scope_note = f"已核验信源数量达到上限 {max_sources}，本次未用于证据检索"
                    basis = ""
                else:
                    verified_count += 1
                    basis = note.get("basis", "")
                    scope_note = note.get("scope", "")
                    if page is None:
                        st.error("sources", "homepage_unavailable",
                                 f"{cand['domain']} 首页未读取，身份依据缺失")
                        scope_note = scope_note or "首页未读取，未取得身份判断依据"
            st.data["sources"].append({
                "source_id": source_id,
                "name": _clean(cand.get("name"), 80) or cand["domain"],
                "domain": cand["domain"],
                "tier": _clean(cand.get("tier"), 30),
                "verified": kind != "unverified_candidate",
                "verification": kind,
                "basis": basis,
                "scope_note": _clean(scope_note, 200),
            })
        if verified_count == 0:
            st.gap("没有已核验信源，未执行证据检索")
        st.budget.spend("max_sources", verified_count)   # 记账用：让 budget.used 与报告可对拍

    @staticmethod
    def _links_in(body: str) -> set:
        """从已读正文里抽取站外链接主机名（代码抽取，不靠模型）。"""
        hosts: set = set()
        for match in re.finditer(r"https?://[^\s，。；、）】\"'<>]+", body):
            host = _host_of(match.group(0))
            if host:
                hosts.add(host)
        return hosts

    # -- 阶段 3：证据检索 ----------------------------------------------------
    def _stage_evidence(self, st: _RunState) -> None:
        verified = [s for s in st.data["sources"] if s.get("verified")]
        cap = int(self.rules["max_claims"])
        contract = ('只输出 JSON：{"claims":[{"text":"<原文逐字片段>",'
                    '"claim_status":"checkable|not_checkable",'
                    '"qualifiers":{"time":null,"place":null,"entity":null},'
                    '"queries":["<中性检索词>"]}]}；'
                    f"最多 {cap} 条；text 必须逐字来自原文，queries 不许带 site:/inurl: 等操作符。")
        obj, err = self._call_llm(st, _skill_prompt("evidence", contract),
                                  f"用户原文：\n{st.raw_text}", "evidence:claims")
        rows = _first_list(obj, ("claims", "items")) if isinstance(obj, dict) else None
        if not isinstance(rows, list):
            if err == "llm_budget_exhausted":
                st.gap("模型调用预算已达上限，主张拆分未执行")
                return
            st.error("evidence", "claims_unavailable",
                     err or "模型未给出可解析的主张拆分")
            st.gap("主张拆分未完成（模型不可用），未执行证据检索")
            return

        if len(rows) > cap:
            st.error("evidence", "too_many_claims",
                     f"识别出 {len(rows)} 条主张，超过单次上限 {cap}")
            st.clarify(f"识别出 {len(rows)} 条主张，超过单次上限 {cap}："
                       "请缩小范围，或指定要先查哪一条")
            rows = rows[:cap]

        normalized = normalize_text(st.raw_text)
        claims: List[Dict[str, Any]] = []
        for index, row in enumerate(rows[:cap], start=1):
            if not isinstance(row, dict):
                continue
            text = _clean(row.get("text"), 300)
            span = locate_quote(normalized, text) if text else None
            if span is None:
                st.error("evidence", "claim_not_anchored",
                         f"主张未在原文中逐字定位，已丢弃：{text[:60]}")
                continue
            status = str(row.get("claim_status") or "").strip().lower()
            if status not in CLAIM_STATUSES:
                st.error("evidence", "bad_claim_status",
                         f"未知主张状态：{status or '（空）'}，按不可核查处理")
                status = "not_checkable"
            qualifiers = row.get("qualifiers")
            qualifiers = qualifiers if isinstance(qualifiers, dict) else {}
            queries = _string_items(row.get("queries"), 8, 80)
            claims.append({
                "claim_id": f"C{index}",
                "text": text,
                "source_span": span,
                "qualifiers": {"time": _clean(qualifiers.get("time"), 60) or None,
                               "place": _clean(qualifiers.get("place"), 60) or None,
                               "entity": _clean(qualifiers.get("entity"), 60) or None},
                "claim_status": status,
                "_queries": queries,
            })
        st.data["claims"] = [{k: v for k, v in c.items() if not k.startswith("_")}
                             for c in claims]
        st.claims_assessed = True
        st.checkable_claims = sum(1 for c in claims if c["claim_status"] == "checkable")
        st.budget.spend("max_claims", len(claims))   # 记账用：让 budget.used 与报告可对拍

        if not st.checkable_claims:
            # 没有可核查主张不是失败（契约 §3.3）：任务状态记 no_factual_claim
            if claims:
                st.gap("原文只有观点/情绪/诉求，没有可核查的事实主张")
            else:
                st.gap("未能从原文拆出可定位的主张")
            return
        if not verified:
            return

        self._retrieve(st, claims, verified)

    def _retrieve(self, st: _RunState, claims: List[Dict[str, Any]],
                  verified: List[Dict[str, Any]]) -> None:
        evidence: List[Dict[str, Any]] = []
        seen_groups: Dict[str, str] = {}       # 正文哈希组 → 首个 evidence_id
        seen_quotes: Dict[Tuple[str, str], str] = {}   # (claim_id, 归一化引文) → evidence_id
        max_quote = int(self.rules["max_quote_chars"])

        for claim in claims:
            if claim["claim_status"] != "checkable":
                continue
            if st.is_cancelled():
                break
            if st.budget.blocked:
                break
            queries = self._claim_queries(st, claim)
            pages_used = 0
            per_claim_pages = int(self.rules["per_claim_max_pages"])
            had_evidence = False
            claim_urls: set = set()   # 同一主张不重复读同一个地址：省预算，也免得同页产出重复证据
            for source in verified:
                if pages_used >= per_claim_pages or st.budget.blocked or st.is_cancelled():
                    break
                for query in queries:
                    if pages_used >= per_claim_pages or st.budget.blocked:
                        break
                    if st.is_cancelled():
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
                    usable = [r for r in results[:4]
                              if isinstance(r, dict) and r.get("url")
                              and _domain_of(_host_of(r.get("url")), source["domain"])]
                    if not usable:
                        st.gap(f"主张 {claim['claim_id']} 在信源 {source['name']}"
                               f"（{source['domain']}）未检索到站内结果")
                        continue
                    for row in usable:
                        if pages_used >= per_claim_pages or st.budget.blocked:
                            break
                        if st.is_cancelled():
                            break
                        url = _clean(row.get("url"), 300)
                        if not url or url in claim_urls:
                            continue
                        claim_urls.add(url)
                        page, _ = self._do_fetch(st, url, [source["domain"]], "evidence")
                        if page is None:
                            continue
                        pages_used += 1
                        record = self._extract_quote(st, claim, source, page, evidence,
                                                     seen_groups, seen_quotes, max_quote)
                        if record is not None:
                            evidence.append(record)
                            had_evidence = True
            if not had_evidence:
                # 读到了页但引文定位失败，同样算"该主张没有可用证据"：缺口与错误各记各的
                st.gap(f"主张 {claim['claim_id']} 尚未找到已核验信源中的对应引文")
        st.data["evidence"] = evidence

    def _claim_queries(self, st: _RunState, claim: Dict[str, Any]) -> List[str]:
        """检索词：主张原文 + 中性改写；先脱敏，再剥范围操作符，最后按上限截断。"""
        cap = int(self.rules["per_claim_max_queries"])
        out: List[str] = []
        for raw in [claim["text"]] + list(claim.get("_queries") or []):
            query = _strip_query_operators(strip_pii(raw))
            if query and query not in out:
                out.append(query)
            if len(out) >= cap:
                break
        if not out:
            st.gap(f"主张 {claim['claim_id']} 脱敏后无可用检索词")
        return out

    def _extract_quote(self, st: _RunState, claim: Dict[str, Any], source: Dict[str, Any],
                       page: Dict[str, Any], evidence: List[Dict[str, Any]],
                       seen_groups: Dict[str, str], seen_quotes: Dict[Tuple[str, str], str],
                       max_quote: int) -> Optional[Dict[str, Any]]:
        body = normalize_text(page.get("text") or "")
        if not body:
            st.error("evidence", "empty_page_text",
                     f"{page.get('url') or source['domain']} 读取成功但正文为空，已丢弃")
            return None
        contract = ('只输出 JSON：{"quote":"<摘自已读正文的原句>",'
                    '"relation":"supports|refutes|partial|background|unclear",'
                    '"limitations":["<该条证据的局限>"]}；'
                    "quote 必须逐字来自上面正文，relation 只能取封闭枚举。")
        qualifiers = claim.get("qualifiers") or {}
        user = (f"主张：{claim['text']}\n"
                f"限定：时间={qualifiers.get('time') or '未注明'}，"
                f"地点={qualifiers.get('place') or '未注明'}，"
                f"实体={qualifiers.get('entity') or '未注明'}\n"
                f"信源：{source.get('name')}（{source.get('domain')}）\n"
                f"已读正文（前4000字）：\n{body[:4000]}")
        obj, err = self._call_llm(st, _skill_prompt("evidence", contract), user,
                                  "evidence:quote")
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
        if len(quote) > max_quote:
            shortened = quote[:max_quote]
            if locate_quote(body, shortened) is None:
                st.error("evidence", "quote_too_long",
                         f"引文超过 {max_quote} 字符且截断后仍无法定位，已丢弃")
                return None
            quote = shortened
        span = locate_quote(body, quote)
        if span is None:
            # 只有"读到了正文但引文定位失败"才算引文未锚定；取页失败走 _do_fetch 的错误分类
            st.error("evidence", "quote_not_anchored",
                     "模型给出的引文未在已读正文中逐字定位，已丢弃")
            return None

        limitations = _string_items(obj.get("limitations"), 4, 120)
        if len(quote) < 20:
            limitations.insert(0, "引文较短，可支撑的上下文有限")
        group = _body_group(body)
        evidence_id = f"E{len(evidence) + 1}"
        if group in seen_groups:
            limitations.append(f"与 {seen_groups[group]} 正文相同（转载/镜像），不构成独立证据")
        quote_key = (claim["claim_id"], normalize_quote(quote))
        if quote_key in seen_quotes:
            # 同一主张下引文归一化后相同 → 并进同一条证据链（引文归一化去重）
            merged_into = seen_quotes[quote_key]
            first_group = _group_of(evidence, merged_into)
            if first_group:
                group = first_group
            limitations.append(f"与 {merged_into} 引文相同，不构成独立证据")
        seen_groups.setdefault(group, evidence_id)
        seen_quotes.setdefault(quote_key, evidence_id)

        published = page.get("published_at")
        return {
            "evidence_id": evidence_id,
            "claim_ids": [claim["claim_id"]],
            "relation": relation,
            "source_id": source.get("source_id", ""),
            "url": _clean(page.get("final_url") or page.get("url"), 300),
            "title": _clean(page.get("title") or source.get("name"), 120),
            "quote": quote,
            "quote_span": span,
            "published_at": _clean(published, 40) if published else None,
            "fetched_at": _clean(page.get("fetched_at"), 40) or st.clock(),
            "limitations": list(dict.fromkeys(limitations)),
            "independence_group": group,
            "domain": _clean(page.get("domain") or _host_of(page.get("final_url")), 80),
        }

    # -- 去重与证据链 -------------------------------------------------------
    def _dedup(self, st: _RunState) -> None:
        chains: Dict[str, Dict[str, Any]] = {}
        for item in st.data["evidence"]:
            group = item.get("independence_group") or ""
            chain = chains.setdefault(group, {"group": group, "urls": [], "evidence_ids": []})
            if item["url"] and item["url"] not in chain["urls"]:
                chain["urls"].append(item["url"])
            if item["evidence_id"] not in chain["evidence_ids"]:
                chain["evidence_ids"].append(item["evidence_id"])
        st.data["chains"] = [chains[key] for key in sorted(chains)]

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
            # "没搜/搜降级"与"搜过了没找到"必须分开记（契约补充）
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

    def _do_fetch(self, st: _RunState, url: str, allowed_domains: List[str], stage: str
                  ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
        """一次读页。返回 (page|None, error|None)；只有 status=ok 才计入读页预算。"""
        fetch = self._fetch_callable()
        if fetch is None:
            st.error(stage, "fetch_unavailable",
                     "取页不可用：未注入 fetch_fn 且默认 transport 不可用")
            return None, {"stage": stage, "category": "fetch_unavailable",
                          "detail": "取页不可用"}
        if not st.budget.allow("max_pages"):
            st.budget.block(st.budget.why("max_pages"))
            return None, None
        if st.is_cancelled():
            return None, None
        entry = st.operation("fetch", url, False)
        try:
            page = fetch(url, allowed_domains=allowed_domains,
                         cancel_check=st.cancel_check, deadline=st.deadline)
        except Exception as exc:  # noqa: BLE001  取页异常按失败记，不当成"页面无引文"
            st.error(stage, "fetch_exception", f"取页异常：{type(exc).__name__}")
            return None, {"stage": stage, "category": "fetch_exception",
                          "detail": type(exc).__name__}
        if not isinstance(page, dict):
            st.error(stage, "fetch_unavailable", "取页返回形状不符")
            return None, {"stage": stage, "category": "fetch_unavailable",
                          "detail": "取页返回形状不符"}
        if page.get("status") != "ok":
            category = _clean(page.get("category") or "fetch_error", 60) or "fetch_error"
            detail = _clean(page.get("detail") or "取页失败", 200)
            st.error(stage, category, detail)
            return None, {"stage": stage, "category": category, "detail": detail}
        st.budget.spend("max_pages")
        entry["ok"] = True
        return page, None

    # -- 收尾 ---------------------------------------------------------------
    def _finish(self, st: _RunState, started_at: str) -> Dict[str, Any]:
        task_status = "completed"
        if st.clarify_reason:
            task_status = "needs_clarification"
        elif st.claims_assessed and not st.checkable_claims:
            # 没有可核查主张不是失败（契约 §3.3）：任务状态单独记一个出口
            task_status = "no_factual_claim"
        status = self._resolve_status(st, task_status)
        budget = st.budget.snapshot()
        result = {
            "tool_id": "collect_evidence",
            "status": status,
            "task_status": task_status,
            "data": st.data,
            "gaps": list(st.gaps),
            "errors": list(st.errors),
            "audit": {
                "operations": list(st.operations),
                "searches": int(budget["used"]["max_searches"]),
                "pages": int(budget["used"]["max_pages"]),
                "llm_calls": int(budget["used"]["max_llm_calls"]),
            },
            "budget": budget,
            "report": "",
            "started_at": started_at,
            "finished_at": st.clock(),
        }
        if st.clarify_reason:
            result["gaps"].insert(0, f"需要用户补充：{st.clarify_reason}")
        if st.failed_reason:
            result["gaps"].insert(0, f"运行失败：{st.failed_reason}")
        if st.cancelled:
            result["gaps"].insert(0, "运行被取消，已保留此前取得的结果")
        if st.budget.blocked:
            result["gaps"].insert(0, f"预算/死线到顶提前停止（{st.budget.blocked_reason}），"
                                     "已保留此前取得的结果")
        result["report"] = render_report(result)
        return result

    @staticmethod
    def _resolve_status(st: _RunState, task_status: str) -> str:
        if st.is_cancelled():
            return "cancelled"
        if st.budget.blocked:
            return "budget_exhausted"
        if st.failed_reason:
            return "failed"
        if st.clarify_reason:
            return "needs_clarification"
        if task_status == "no_factual_claim" and not st.errors:
            # 没有可核查主张不是失败：连"没有可定位引文"的缺口都不记成 partial
            return "completed"
        if st.errors or st.gaps:
            return "partial"
        return "completed"


def _group_of(evidence: List[Dict[str, Any]], evidence_id: str) -> str:
    for item in evidence:
        if item.get("evidence_id") == evidence_id:
            return item.get("independence_group", "")
    return ""


def collect_evidence(text: str, **kw) -> Dict[str, Any]:
    """便捷函数 = EvidenceSkills(**kw).run(text)。工具层用它。"""
    return EvidenceSkills(**kw).run(text)


# ---------------------------------------------------------------------------
# 确定性纯文本报告（契约 §3.5）
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
    """引文渲染：逐字保留（总览要求报告引用逐字保留），只去控制字符并压空白。"""
    text = normalize_text(value)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    return text[:limit]


def _domain_label(primary: str) -> str:
    if not primary:
        return "（未判定）"
    for entry in (_load_taxonomy().get("domains") or []):
        if isinstance(entry, dict) and str(entry.get("id")) == primary:
            name = _report_field(entry.get("name"), 40)
            return f"{name}（{primary}）" if name else primary
    return primary


def _qualifier_text(qualifiers: Dict[str, Any]) -> str:
    parts = []
    for key, label in (("time", "时间"), ("place", "地点"), ("entity", "实体")):
        value = qualifiers.get(key)
        parts.append(f"{label}={_report_field(value, 60) if value else '未注明'}")
    return "，".join(parts)


def render_report(result: Dict[str, Any]) -> str:
    """把一次运行结果渲染成确定性纯文本报告（无 HTML、无 markdown 链接语法）。

    结构：领域 → 信源（已核验/未核验分开）→ 主张 → 证据 → 证据链 → 缺口 → 错误 → 预算。
    全文不出现真假判定词：只述"是什么领域、去了哪里、读到了什么、还缺什么"。
    """
    if not isinstance(result, dict):
        return "取证报告：（结果形状不符，无可渲染内容）"
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    budget = result.get("budget") if isinstance(result.get("budget"), dict) else {}
    used = budget.get("used") if isinstance(budget.get("used"), dict) else {}
    limits = budget.get("limits") if isinstance(budget.get("limits"), dict) else {}

    lines: List[str] = ["取证报告（只述证据，不判真假）"]
    lines.append(f"执行状态：{_report_field(result.get('status'), 40) or '（未知）'}"
                 f"　任务状态：{_report_field(result.get('task_status'), 40) or '（未知）'}")
    lines.append(f"开始：{_report_field(result.get('started_at'), 40)}"
                 f"　结束：{_report_field(result.get('finished_at'), 40)}")

    # 一、领域
    domain = data.get("domain") if isinstance(data.get("domain"), dict) else {}
    lines.append("")
    lines.append("一、领域识别")
    lines.append(f"  主领域：{_domain_label(str(domain.get('primary') or ''))}")
    secondary = [_report_field(s, 40) for s in (domain.get("secondary") or [])]
    lines.append(f"  次领域：{'、'.join(secondary) if secondary else '（无）'}")
    lines.append(f"  地区：{_report_field(domain.get('region'), 20) or '（未注明）'}"
                 f"　时间：{_report_field(domain.get('time'), 60) or '（未注明）'}")
    labels = [_report_field(s, 40) for s in (domain.get("labels") or [])]
    lines.append(f"  议题标签：{'、'.join(labels) if labels else '（无）'}")
    basis = [_report_quote(s, 120) for s in (domain.get("basis") or [])]
    lines.append("  判断依据：" + ("；".join(f"「{b}」" for b in basis) if basis else "（无）"))

    # 二、信源
    sources = [s for s in (data.get("sources") or []) if isinstance(s, dict)]
    verified = [s for s in sources if s.get("verified")]
    unverified = [s for s in sources if not s.get("verified")]
    lines.append("")
    lines.append(f"二、信源（已核验 {len(verified)} / 未核验 {len(unverified)}）")
    if not sources:
        lines.append("  （本次没有评估任何信源）")
    for item in verified:
        lines.append(f"  [已核验] {_report_field(item.get('name'), 80)}"
                     f"（{_report_field(item.get('domain'), 80)}）"
                     f"　档位：{_report_field(item.get('tier'), 30) or '（未登记）'}"
                     f"　判定：{_report_field(item.get('verification'), 40)}")
        if item.get("basis"):
            lines.append(f"      依据：「{_report_quote(item.get('basis'), 160)}」")
        if item.get("scope_note"):
            lines.append(f"      适用范围：{_report_field(item.get('scope_note'), 200)}")
    for item in unverified:
        lines.append(f"  [未核验] {_report_field(item.get('name'), 80)}"
                     f"（{_report_field(item.get('domain'), 80)}）"
                     f"　判定：{_report_field(item.get('verification'), 40)}")
        if item.get("scope_note"):
            lines.append(f"      说明：{_report_field(item.get('scope_note'), 200)}")

    # 三、主张
    claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
    checkable = [c for c in claims if c.get("claim_status") == "checkable"]
    lines.append("")
    lines.append(f"三、主张（共 {len(claims)} 条，可核查 {len(checkable)} 条）")
    if not claims:
        lines.append("  （未拆出主张）")
    for item in claims:
        span = item.get("source_span") if isinstance(item.get("source_span"), dict) else {}
        status = "可核查" if item.get("claim_status") == "checkable" else "暂不核查"
        lines.append(f"  {_report_field(item.get('claim_id'), 12)} 「"
                     f"{_report_quote(item.get('text'), 200)}」"
                     f"（原文第 {span.get('start', 0)}-{span.get('end', 0)} 字符）状态：{status}")
        qualifiers = item.get("qualifiers") if isinstance(item.get("qualifiers"), dict) else {}
        lines.append(f"      限定：{_qualifier_text(qualifiers)}")

    # 四、证据
    evidence = [e for e in (data.get("evidence") or []) if isinstance(e, dict)]
    lines.append("")
    lines.append(f"四、证据（共 {len(evidence)} 条）")
    if not evidence:
        lines.append("  （没有可定位引文）")
    for item in evidence:
        span = item.get("quote_span") if isinstance(item.get("quote_span"), dict) else {}
        relation = _RELATION_LABELS.get(str(item.get("relation")),
                                        _report_field(item.get("relation"), 20))
        lines.append(f"  {_report_field(item.get('evidence_id'), 12)} 主张 "
                     f"{'、'.join(_report_field(c, 12) for c in (item.get('claim_ids') or []))}"
                     f"｜关系：{relation}｜信源：{_report_field(item.get('source_id'), 40)}")
        lines.append(f"      引文：「{_report_quote(item.get('quote'), 300)}」"
                     f"（正文第 {span.get('start', 0)}-{span.get('end', 0)} 字符）")
        lines.append(f"      地址：{_report_field(item.get('url'), 300)}"
                     f"　标题：{_report_field(item.get('title'), 120) or '（无）'}")
        lines.append(f"      时点：发布于 {_report_field(item.get('published_at'), 40) or '未注明'}"
                     f"，抓取于 {_report_field(item.get('fetched_at'), 40) or '未注明'}")
        limits_text = "；".join(_report_field(x, 160) for x in (item.get("limitations") or []))
        lines.append(f"      独立组：{_report_field(item.get('independence_group'), 40)}"
                     f"｜局限：{limits_text or '（无）'}")

    # 五、证据链
    chains = [c for c in (data.get("chains") or []) if isinstance(c, dict)]
    lines.append("")
    lines.append(f"五、证据链（转载/同源合并，共 {len(chains)} 组）")
    if not chains:
        lines.append("  （无）")
    for chain in chains:
        ids = "、".join(_report_field(i, 12) for i in (chain.get("evidence_ids") or []))
        urls = [_report_field(u, 200) for u in (chain.get("urls") or [])]
        shown = "、".join(urls[:3]) + (f" 等 {len(urls)} 个地址" if len(urls) > 3 else "")
        lines.append(f"  {_report_field(chain.get('group'), 40)}：{ids}"
                     f"（{len(urls)} 个地址：{shown}）")

    # 六、缺口
    gaps = [_report_field(g, 240) for g in (result.get("gaps") or [])]
    lines.append("")
    lines.append(f"六、缺口（共 {len(gaps)} 条）")
    if not gaps:
        lines.append("  （无）")
    for index, text in enumerate(gaps, start=1):
        lines.append(f"  {index}. {text}")

    # 七、错误
    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    lines.append("")
    lines.append(f"七、错误（共 {len(errors)} 条）")
    if not errors:
        lines.append("  （无）")
    for item in errors:
        lines.append(f"  - [{_report_field(item.get('stage'), 20)}] "
                     f"{_report_field(item.get('category'), 40)}："
                     f"{_report_field(item.get('detail'), 200)}")

    # 八、预算与可观测
    searches = int(audit.get("searches") or 0)
    pages = int(audit.get("pages") or 0)
    llm_calls = int(audit.get("llm_calls") or 0)
    lines.append("")
    lines.append("八、预算与可观测")
    lines.append(f"  已执行 {searches} 次搜索、{pages} 次读页、{llm_calls} 次模型调用")
    lines.append(f"  搜索 {searches}/{limits.get('max_searches', '-')}"
                 f"；读页 {pages}/{limits.get('max_pages', '-')}"
                 f"；模型调用 {llm_calls}/{limits.get('max_llm_calls', '-')}")
    lines.append(f"  主张 {used.get('max_claims', 0)}/{limits.get('max_claims', '-')}"
                 f"；信源 {used.get('max_sources', 0)}/{limits.get('max_sources', '-')}")
    if result.get("status") in ("cancelled", "budget_exhausted"):
        lines.append("  本次提前停止，上表为停止时点的真实消耗。")
    return "\n".join(lines)
