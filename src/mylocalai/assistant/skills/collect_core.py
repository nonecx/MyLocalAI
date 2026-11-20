# -*- coding: utf-8 -*-
"""共享采集底座（信息收集 / 权威核验两套流水线的公共部分）。

改了什么、为什么、锁定什么：
  - 新增 `assistant.skills.collect_core`：把两套新流水线都要用的东西收口到一处——
    规则加载（坏配置段级降级 + 数值夹取）、预算、运行状态、来源分类、动态权威信号、
    去重、引文锚定、SKILL.md 提示词装配、纯文本报告辅助。两套 pipeline 只编排，
    不再各写一份预算/状态/来源判定，避免口径漂移。
  - **来源判定是代码，不是模型**：目录锚点来自 `skills/sources.json` 与
    `config/sources_registry.json` 的档位；目录外站点只有代码信号（官方后缀 /
    官方正文标记 / 已核验页面直链 / 多来源交叉提及）满足下限才可记为
    `session_verified`（仅会话内、不写回目录）。页面自称权威、模型说“匹配”都不算。
  - **每个出口都有降级**：配置缺失/写坏不抛；预算与死线只让调用方少查，不让程序崩；
    状态判定把取消/预算/澄清/失败/部分完成/完成分开记录，绝不把失败说成空结果。
  - 全离线可测：本模块只做纯逻辑（除读 JSON 资源），网络/模型由 pipeline 注入。

与既有取证三技能的关系：复用 `skills.pipeline` 的 normalize_text / locate_quote /
strip_pii（引文逐字锚定与脱敏同口径）、`retrieval.sources` 的域名分级、`dossier.store`
的标题近似函数；**不动**既有 pipeline 的任何行为。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from mylocalai.assistant.skills.pipeline import (
    locate_quote,
    normalize_quote,
    normalize_text,
    strip_pii,
)
from mylocalai.assistant.fingerprint import jaccard, normalize_claim, shingles
from mylocalai.retrieval.sources import (
    OFFICIAL_TIERS,
    RELIABLE_TIERS,
    TIER_UNKNOWN,
    classify_domain,
    normalize_host,
    source_kind,
    tier_rank,
)

__all__ = [
    "STATUSES", "TASK_STATUSES", "RELATIONS", "AUTHORITY_CLASSES", "VERIFICATIONS",
    "DEFAULT_COLLECT_RULES", "load_collect_rules", "load_authority_config",
    "CollectBudget", "CollectRunState",
    "SourceInfo", "classify_source", "dynamic_authority_signals", "is_decisive_source",
    "url_key", "dedupe_sources", "near_duplicate_source",
    "anchor_quote", "load_skill_prompt", "render_evidence_line", "render_counts_line",
    "utc_now_iso",
]

_HERE = Path(__file__).resolve().parent
_SOURCES_PATH = _HERE / "sources.json"
_AUTHORITY_PATH = _HERE / "authority" / "authorities.json"

#: 执行状态（与 skills.pipeline.STATUSES 同值，测试锁定）
STATUSES = ("completed", "partial", "needs_clarification", "failed",
            "cancelled", "budget_exhausted")
#: 任务状态（与取证三技能同口径）
TASK_STATUSES = ("completed", "no_factual_claim", "needs_clarification")
#: 证据关系封闭枚举
RELATIONS = ("supports", "refutes", "partial", "background", "unclear")
#: 权威类别封闭枚举
AUTHORITY_CLASSES = ("official", "international_org", "academic", "standards",
                     "factcheck", "media", "unverified")
#: 来源核验方式封闭枚举
VERIFICATIONS = ("catalog_anchor", "verified_site_link", "session_verified",
                 "unverified")

#: 公共默认规则（两套 pipeline 各自在 defaults 里覆盖自己的键）
DEFAULT_COLLECT_RULES: Dict[str, Any] = {
    "max_searches": 12,
    "max_pages": 12,
    "max_llm_calls": 20,
    "total_deadline_s": 90.0,
    "io_timeout_s": 10.0,
    "max_page_bytes": 1500000,
    "max_text_chars": 20000,
    "max_redirect_hops": 3,
    "max_quote_chars": 300,
}

#: 数值安全区间（键名锁定；未列出的键按 defaults 类型校验，不夹取）
_RULE_LIMITS: Dict[str, Tuple[float, float]] = {
    "max_searches": (1, 200),
    "max_pages": (1, 200),
    "max_llm_calls": (1, 500),
    "total_deadline_s": (1, 3600),
    "io_timeout_s": (0.5, 300),
    "max_page_bytes": (1000, 50000000),
    "max_text_chars": (200, 500000),
    "max_redirect_hops": (0, 10),
    "max_quote_chars": (10, 5000),
}

#: 审计里可计数的键（CollectBudget 为这些键维护 used）
_COUNT_KEYS = frozenset({"max_searches", "max_pages", "max_llm_calls"})


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 串（秒精度）；报告与卡片时点用。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001  配置文件缺失/写坏 = 回退默认，不能让运行崩
        return {}


def load_collect_rules(path: Optional[Any], defaults: Mapping[str, Any],
                       limits: Optional[Mapping[str, Tuple[float, float]]] = None
                       ) -> Dict[str, Any]:
    """读 rules.json：缺键回退 defaults，数值夹到 limits，未知键丢弃。

    为什么夹而不是抛：rules.json 是运维可改的配置，改坏了最多“这次查得少一点”，
    不该让整条流水线崩掉（与既有 skills.pipeline.load_rules 同一降级文化）。
    """
    base = dict(defaults or DEFAULT_COLLECT_RULES)
    raw = _read_json(Path(path)) if path else {}
    bounds = dict(_RULE_LIMITS)
    bounds.update(dict(limits or {}))
    out: Dict[str, Any] = {}
    for key, default in base.items():
        value = raw.get(key, default)
        if isinstance(default, bool):
            out[key] = value if isinstance(value, bool) else bool(default)
            continue
        if isinstance(default, (int, float)):
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = float(default)
            if number != number or number in (float("inf"), float("-inf")):
                number = float(default)
            if key in bounds:
                low, high = bounds[key]
                number = min(max(number, float(low)), float(high))
            out[key] = int(round(number)) if isinstance(default, int) else number
            continue
        if isinstance(default, str):
            out[key] = value if isinstance(value, str) else default
            continue
        out[key] = value
    return out


def load_authority_config(path: Optional[Any] = None) -> Dict[str, Any]:
    """加载 authorities.json（类别覆盖、决定性类别、官方后缀/标记、动态开关）。

    缺文件/坏类型逐键回退内置默认；返回新 dict，调用方可安全修改。
    """
    default: Dict[str, Any] = {
        "version": 1,
        "class_overrides": {},
        "decisive_classes": ["official", "international_org", "academic",
                             "standards", "factcheck"],
        "official_suffixes": ["gov.cn", "gov", "mil.cn"],
        "official_markers": ["政府网站标识码", "网站标识码", "主办单位",
                             "公网安备", "ICP备"],
        "media_classes": ["media"],
        "dynamic_min_signals": 2,
        "dynamic_requires_corroboration": True,
        "max_dynamic_domains": 4,
        "dynamic_enabled": True,
    }
    raw = _read_json(Path(path) if path else _AUTHORITY_PATH)
    out = dict(default)
    for key, value in default.items():
        if key not in raw:
            continue
        got = raw[key]
        if isinstance(value, list):
            if isinstance(got, list) and all(isinstance(x, str) for x in got):
                out[key] = [x for x in got if x]
        elif isinstance(value, dict):
            if isinstance(got, dict) and all(isinstance(k, str) and isinstance(v, str)
                                            for k, v in got.items()):
                out[key] = dict(got)
        elif isinstance(value, bool):
            if isinstance(got, bool):
                out[key] = got
        elif isinstance(value, int):
            try:
                out[key] = max(1, int(got))
            except (TypeError, ValueError):
                pass
        else:
            out[key] = got
    return out


class CollectBudget:
    """计数预算 + 绝对死线。

    `allow`/`spend` 只对 max_searches/max_pages/max_llm_calls 计数；其他规则键不占额度。
    死线是 time.monotonic 绝对时刻（None = 不限）；一旦到点，所有 allow 返回 False。
    """

    def __init__(self, rules: Mapping[str, Any], deadline: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.rules = dict(rules or {})
        self._clock = clock
        self.deadline = float(deadline) if deadline is not None else None
        self.used: Dict[str, int] = {key: 0 for key in _COUNT_KEYS}
        self.blocked_reason = ""

    def deadline_hit(self) -> bool:
        return self.deadline is not None and self._clock() >= self.deadline

    def allow(self, key: str, cost: int = 1) -> bool:
        if self.blocked_reason:
            return False
        if key not in self.used:
            return not self.deadline_hit()
        if self.deadline_hit():
            return False
        try:
            limit = int(self.rules.get(key, 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        return self.used[key] + max(0, int(cost)) <= limit

    def spend(self, key: str, cost: int = 1) -> None:
        if key in self.used:
            self.used[key] += max(0, int(cost))

    def remaining(self, key: str) -> int:
        if key not in self.used:
            return 0
        try:
            limit = int(self.rules.get(key, 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        return max(0, limit - self.used[key])

    def why(self, key: str) -> str:
        return f"{key} 预算已达上限（{self.rules.get(key, 0)}）"

    def block(self, reason: str) -> None:
        self.blocked_reason = str(reason or "").strip() or "budget_exhausted"

    def snapshot(self) -> Dict[str, Any]:
        return {"used": dict(self.used),
                "limits": {key: self.rules.get(key) for key in self.used}}


class CollectRunState:
    """一次运行的状态机：错误/缺口/审计/取消 + 六态判定。"""

    def __init__(self, budget: CollectBudget, cancel_check: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.budget = budget
        self._cancel_check = cancel_check
        self._clock = clock
        self.data: Dict[str, Any] = {}
        self.errors: List[Dict[str, str]] = []
        self.gaps: List[str] = []
        self.audit: Dict[str, Any] = {"operations": [], "searches": 0, "pages": 0,
                                      "llm_calls": 0}
        self._cancelled = False
        self._clarified = False
        self._failed = False
        self._no_factual = False

    def error(self, stage: str, category: str, detail: str) -> None:
        self.errors.append({"stage": str(stage or ""), "category": str(category or ""),
                            "detail": str(detail or "")[:300]})

    def gap(self, text: Any) -> None:
        value = str(text or "").strip()
        if value and value not in self.gaps:
            self.gaps.append(value[:300])

    def clarify(self, reason: str) -> None:
        self._clarified = True
        self.gap(reason)

    def fail(self, reason: str) -> None:
        self._failed = True
        self.gap(reason)

    def no_factual_claim(self) -> None:
        self._no_factual = True

    def operation(self, kind: str, target: str, ok: bool = True) -> Dict[str, Any]:
        row = {"kind": str(kind or ""), "target": str(target or "")[-300:],
               "ok": bool(ok)}
        self.audit["operations"].append(row)
        if kind == "search" and ok:
            self.audit["searches"] = int(self.audit.get("searches", 0)) + 1
        elif kind == "fetch" and ok:
            self.audit["pages"] = int(self.audit.get("pages", 0)) + 1
        elif kind == "llm":
            self.audit["llm_calls"] = int(self.audit.get("llm_calls", 0)) + (1 if ok else 0)
        return row

    def is_cancelled(self) -> bool:
        if self._cancelled:
            return True
        try:
            if self._cancel_check is not None and bool(self._cancel_check()):
                self._cancelled = True
        except Exception:  # noqa: BLE001  取消探测自身失败不视为取消
            return False
        return self._cancelled

    def finish(self) -> Dict[str, Any]:
        """按契约判定六态/三态，返回统一外壳里的状态字段。"""
        cancelled = self.is_cancelled()
        if cancelled:
            status = "cancelled"
        elif self.budget.deadline_hit() or self.budget.blocked_reason:
            status = "budget_exhausted"
        elif self._clarified:
            status = "needs_clarification"
        elif self._failed and not self.data:
            status = "failed"
        elif self.errors or self.gaps:
            status = "partial"
        else:
            status = "completed"
        if status == "needs_clarification":
            task_status = "needs_clarification"
        elif self._no_factual:
            task_status = "no_factual_claim"
        else:
            task_status = "completed"
        return {"status": status, "task_status": task_status,
                "data": self.data, "gaps": list(self.gaps),
                "errors": list(self.errors), "audit": dict(self.audit),
                "budget": self.budget.snapshot()}


@dataclass(frozen=True)
class SourceInfo:
    """一个来源的确定性画像（域名边界匹配 + 档位 + 权威类别 + 核验方式）。"""

    source_id: str
    name: str
    domain: str
    tier: str
    source_kind: str
    authority_class: str
    verification: str
    verification_basis: str
    anchor: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"source_id": self.source_id, "name": self.name, "domain": self.domain,
                "tier": self.tier, "source_kind": self.source_kind,
                "authority_class": self.authority_class,
                "verification": self.verification,
                "verification_basis": self.verification_basis,
                "anchor": self.anchor}


def _host_of(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "/" in text or ":" in text:
        return normalize_host(text)
    return text.lower().rstrip(".")


def _domain_matches(host: str, domain: str) -> bool:
    d = str(domain or "").strip().lower().rstrip(".")
    return bool(host and d and (host == d or host.endswith("." + d)))


def _load_catalog() -> List[Dict[str, Any]]:
    rows = _read_json(_SOURCES_PATH).get("sources")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _match_catalog(host: str) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_len = -1
    for entry in _load_catalog():
        domain = str(entry.get("domain") or "").strip().lower().rstrip(".")
        if _domain_matches(host, domain) and len(domain) > best_len:
            best, best_len = entry, len(domain)
    return best


def _override_class(host: str, config: Mapping[str, Any]) -> str:
    overrides = config.get("class_overrides") or {}
    best, best_len = "", -1
    if isinstance(overrides, dict):
        for domain, cls in overrides.items():
            if _domain_matches(host, str(domain)) and len(str(domain)) > best_len:
                best, best_len = str(cls), len(str(domain))
    return best if best in AUTHORITY_CLASSES else ""


def classify_source(url_or_host: Any, *, authority_config: Optional[Mapping[str, Any]] = None,
                    verification_override: Optional[str] = None) -> SourceInfo:
    """域名 → SourceInfo（档位/权威类别/核验方式全部代码判定，不看模型）。"""
    host = _host_of(url_or_host)
    if not host:
        return SourceInfo("", "", "", TIER_UNKNOWN, "", "unverified", "unverified",
                          "空主机，无法判定来源", False)
    config = dict(authority_config or load_authority_config())
    tier = classify_domain(host) or TIER_UNKNOWN
    kind = source_kind(host)
    entry = _match_catalog(host) or {}
    domain = str(entry.get("domain") or host).strip().lower().rstrip(".")
    name = str(entry.get("name") or "").strip()
    source_id = str(entry.get("source_id") or "").strip()
    if not name:
        # 目录外站点的展示名只取域名本身；不编机构名（编了就是“自封身份”）
        name = host
    if not source_id:
        source_id = "SRC-" + re.sub(r"[^a-z0-9]+", "-", host)[:60].strip("-")

    override = _override_class(host, config)
    if override:
        authority_class = override
    elif tier in OFFICIAL_TIERS:
        authority_class = "official"
    elif tier in RELIABLE_TIERS:
        authority_class = "media"
    else:
        authority_class = "unverified"

    verification = str(verification_override or "").strip()
    basis = ""
    if verification in VERIFICATIONS:
        basis = {
            "catalog_anchor": "目录锚点（人工登记的权威来源）",
            "verified_site_link": "已核验站点页面直接给出的链接",
            "session_verified": "本次运行内代码信号核验通过",
            "unverified": "未核验（陌生域名或信号不足）",
        }.get(verification, "")
    elif tier in OFFICIAL_TIERS or tier in RELIABLE_TIERS:
        verification = "catalog_anchor"
        basis = f"信源分级目录锚点（{tier}）"
    elif bool(entry.get("anchor")):
        verification = "catalog_anchor"
        basis = "信源目录锚点"
    else:
        verification = "unverified"
        basis = "未核验（目录外域名；页面自称权威不算）"

    anchor = verification in ("catalog_anchor", "verified_site_link", "session_verified")
    return SourceInfo(source_id=source_id, name=name, domain=domain, tier=tier,
                      source_kind=kind, authority_class=authority_class,
                      verification=verification, verification_basis=basis,
                      anchor=anchor)


_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）】]+", re.I)


def _page_texts(pages: Any) -> List[Dict[str, Any]]:
    """把 fetched_pages 统一成 [{url, domain, title, text}]，坏形状忽略。"""
    if isinstance(pages, Mapping):
        values = list(pages.values())
    elif isinstance(pages, (list, tuple, set)):
        values = list(pages)
    else:
        values = [pages]
    out: List[Dict[str, Any]] = []
    for page in values:
        if not isinstance(page, Mapping):
            continue
        text = str(page.get("text") or "")
        title = str(page.get("title") or "")
        url = str(page.get("final_url") or page.get("url") or "")
        domain = _host_of(url)
        out.append({"url": url, "domain": domain, "title": title, "text": text})
    return out


def dynamic_authority_signals(url: Any, fetched_pages: Any, *,
                              authority_config: Optional[Mapping[str, Any]] = None,
                              verified_pages: Optional[Iterable[Any]] = None
                              ) -> Dict[str, Any]:
    """目录外候选的动态核验信号（代码可见、可测试）。

    信号（至少 dynamic_min_signals 个才算 session_verified）：
      official_suffix / official_marker / inbound_link / cross_mention。
    页面自称权威、模型说“匹配”都不在信号里——身份不能自封。
    """
    config = dict(authority_config or load_authority_config())
    host = _host_of(url)
    pages = _page_texts(fetched_pages)
    text = "\n".join(page["text"] for page in pages)
    title = "\n".join(page["title"] for page in pages)
    hay = text + "\n" + title
    signals: List[str] = []

    if any(_domain_matches(host, str(s)) for s in (config.get("official_suffixes") or [])):
        signals.append("official_suffix")
    markers = [str(m) for m in (config.get("official_markers") or []) if str(m)]
    if markers and any(marker in hay for marker in markers):
        signals.append("official_marker")

    verified = _page_texts(list(verified_pages or []))
    inbound_domains = set()
    for page in verified:
        for linked in _URL_RE.findall(page["text"]):
            if _domain_matches(_host_of(linked), host):
                inbound_domains.add(page["domain"] or page["url"])
    if inbound_domains:
        signals.append("inbound_link")

    mention_domains = {page["domain"] for page in verified
                       if host and host in page["text"]}
    if len(mention_domains) >= 2:
        signals.append("cross_mention")

    signals = list(dict.fromkeys(signals))
    if "official_suffix" in signals and "official_marker" in signals:
        authority_class = "official"
    elif "official_marker" in signals and ("inbound_link" in signals or "cross_mention" in signals):
        authority_class = "official"
    elif "official_suffix" in signals and ("inbound_link" in signals or "cross_mention" in signals):
        authority_class = "official"
    else:
        authority_class = "unverified"
    try:
        need = max(1, int(config.get("dynamic_min_signals", 2)))
    except (TypeError, ValueError):
        need = 2
    enabled = bool(config.get("dynamic_enabled", True))
    verified_flag = enabled and authority_class != "unverified" and len(signals) >= need
    basis = "动态核验信号：" + ("、".join(signals) if signals else "无")
    return {"host": host, "signals": signals, "authority_class": authority_class,
            "verified": verified_flag, "basis": basis}


def is_decisive_source(info: SourceInfo, *,
                       authority_config: Optional[Mapping[str, Any]] = None,
                       corroborated: bool = False) -> bool:
    """该来源能否作为“决定性”证据（证明/反驳）的资格判定。"""
    config = dict(authority_config or load_authority_config())
    decisive = set(config.get("decisive_classes") or [])
    if info.authority_class not in decisive:
        return False
    if info.verification in ("catalog_anchor", "verified_site_link"):
        return True
    if info.verification == "session_verified":
        if not bool(config.get("dynamic_requires_corroboration", True)):
            return True
        return bool(corroborated)
    return False


def url_key(url: Any) -> str:
    """URL 去重键：小写主机 + 去尾斜杠的 path（与 platform.candidates 同口径）。"""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return str(url or "").strip()
    if not parsed.hostname:
        return str(url or "").strip()
    path = parsed.path or "/"
    return f"{parsed.hostname.lower()}{path.rstrip('/') or '/'}"


def dedupe_sources(items: Sequence[Mapping[str, Any]],
                   key: Optional[Callable[[Mapping[str, Any]], str]] = None
                   ) -> List[Mapping[str, Any]]:
    """批内 URL 去重，保留首次出现顺序（检索排序即优先级）。"""
    seen = set()
    out: List[Mapping[str, Any]] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        k = (key(item) if key else url_key(item.get("url") or item.get("final_url") or ""))
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


def _title_similarity(a: Any, b: Any) -> float:
    """标题近似度：与谣言库指纹同口径（normalize_claim + shingles + jaccard）。"""
    try:
        left = shingles(normalize_claim(a))
        right = shingles(normalize_claim(b))
        return float(jaccard(left, right))
    except Exception:  # noqa: BLE001  相似度只是去重增益：算不出按“不相似”处理
        return 0.0


def near_duplicate_source(item: Mapping[str, Any],
                          others: Iterable[Mapping[str, Any]], threshold: float) -> bool:
    """标题近似判定（同一事件的转载）；坏阈值按不相似处理。"""
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.0
    title = str(item.get("title") or "")[:120]
    if not title:
        return False
    for other in others or []:
        if not isinstance(other, Mapping):
            continue
        if other is item:
            continue
        if _title_similarity(title, str(other.get("title") or "")[:120]) >= threshold:
            return True
    return False


def anchor_quote(body: Any, quote: Any) -> Optional[Dict[str, int]]:
    """引文必须逐字出现在已读正文；返回归一化字符偏移，定位不到返回 None。"""
    if not normalize_quote(quote):
        return None
    return locate_quote(body, quote)


def load_skill_prompt(skill_dir: Any, contract: str) -> str:
    """读阶段 SKILL.md 并追加机器契约；文件缺失时用精简兜底，不让运行崩。"""
    text = ""
    try:
        path = Path(skill_dir) / "SKILL.md"
        text = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001  指令文件缺失 = 指令变短，不是运行失败
        text = ("你是谨慎的取证助手：只依据实际读取过的正文工作，"
                "不确定就如实说明，不编造来源、引文、URL 或时点。")
    return (text.strip() + "\n\n" + str(contract or "")).strip()


def render_evidence_line(ev: Mapping[str, Any], *, with_quote: bool = True) -> str:
    """单条证据的纯文本行（无 HTML / 无 markdown 链接语法风险）。"""
    ev = ev if isinstance(ev, Mapping) else {}
    parts: List[str] = []
    if ev.get("evidence_id"):
        parts.append(str(ev["evidence_id"]))
    if ev.get("relation"):
        parts.append(f"关系 {ev['relation']}")
    source = str(ev.get("name") or ev.get("domain") or ev.get("source_id") or "来源未知")
    parts.append(source)
    title = str(ev.get("title") or "")
    if title:
        parts.append(f"《{title}》")
    if ev.get("url"):
        parts.append(str(ev["url"]))
    if ev.get("published_at"):
        parts.append(f"发布 {ev['published_at']}")
    if ev.get("fetched_at"):
        parts.append(f"读取 {ev['fetched_at']}")
    line = " · ".join(parts)
    if with_quote and ev.get("quote"):
        line += f"：「{str(ev['quote'])[:300]}」"
    return line


def render_counts_line(counts: Mapping[str, Any]) -> str:
    """执行计数行（报告末尾诚实可观测口径）。"""
    counts = counts if isinstance(counts, Mapping) else {}
    return (f"已执行 {int(counts.get('searches') or 0)} 次搜索、"
            f"{int(counts.get('pages') or 0)} 次读页、"
            f"{int(counts.get('llm_calls') or 0)} 次模型调用")