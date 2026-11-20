# -*- coding: utf-8 -*-
"""资料收集技能（research）确定性编排：范围 → 检索 → 读页 → 摘录 → 综述 → 报告。

改了什么、为什么、锁定什么：
  - 新增 `assistant.skills.research` 包：把“按主题收集公开资料”编排成一条可测试的流水线，
    并为 `mode="agent"` 复用同一套收口。受限循环只决定“下一步做什么”，要点/状态/引文
    这些结论性字段全部由代码写（契约 §5/§7）。两套模式共享去重、锚定、综述与报告，
    差异只剩“谁决定下一步”，避免两种模式产出不同口径的卡。
  - 锁定“只述证据、不下结论”：每条要点至少引用一条**已锚定引文**，`source_quote`
    必须是所引证据 quote 之一（逐字）；未核验来源的证据只能进 leads；报告与卡片不出现
    判断词、不出现任何数值分（铁律 1/2）。
  - 锁定“每个出口都有降级”（契约 §2）：主题为空/过短/过宽 → needs_clarification；
    检索未授权/检索后端全败、取页未授权、范围阶段模型不可用 → failed；读页失败、引文
    未锚定被丢弃、子问题未覆盖、后续模型不可用 → partial；预算耗尽/取消保留已得结果；
    **零结果是合法输出** → completed + gaps。
  - 增量（prior+followup）：复用上一张卡的 topic/scope/子问题/证据/要点/来源，只补未覆盖
    的缺口；旧 evidence_id 原样保留，新 id 从已有最大号往后排；新卡 incremental=true、
    base_as_of=prior.as_of；prior 为空按普通首轮处理。
  - 预算与上限全部来自 `research/rules.json`（`load_research_rules` 逐键回退 + 夹取），
    代码只读配置，不写死额度。
  - 全离线可测：search_fn / fetch_fn / llm_fn / transport / clock 全部可注入，测试零网络零
    模型；本模块不 import web、不 import platform。

状态口径说明（与 `collect_core.CollectRunState.finish()` 的关系）：
  `finish()` 的 failed 只在 data 为空时成立，且 gaps 一律降级 partial。research 的失败
  定义包含“检索/取页未授权、检索后端全败”（契约 §2），零结果又必须是 completed + gaps，
  因此本模块在 `_resolve_status` 里按契约把这两类边界说清楚：取消/预算/澄清优先，
  其次失败，最后“本次没有新增引文”视为合法空结果。除此之外仍沿用 `finish()` 的判定。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from mylocalai.assistant.skills.agent_loop import run_agent_loop
from mylocalai.assistant.skills.collect_core import (
    CollectBudget,
    CollectRunState,
    SourceInfo,
    anchor_quote,
    classify_source,
    dedupe_sources,
    load_collect_rules,
    load_skill_prompt,
    near_duplicate_source,
    render_counts_line,
    render_evidence_line,
    url_key,
    utc_now_iso,
)
from mylocalai.assistant.skills.pipeline import normalize_text, strip_pii
from mylocalai.assistant.skills.transport import strip_query_operators

__all__ = [
    "DEFAULT_RESEARCH_RULES",
    "RESEARCH_RULES_PATH",
    "SUPPORT_LEVELS",
    "ROLES",
    "ResearchSkills",
    "collect_research",
    "load_research_rules",
    "render_report",
]

_HERE = Path(__file__).resolve().parent
RESEARCH_RULES_PATH = _HERE / "rules.json"

#: 要点支撑等级（封闭枚举；由代码按来源等级计算，模型写什么都不算）
SUPPORT_LEVELS = ("authoritative", "reliable")
#: 证据角色（封闭枚举；模型越界按 fact 处理并记 error）
ROLES = ("fact", "context", "conflict")
#: 来源等级排序（读页优先级：权威 > 可靠 > 未核验）
_LEVEL_RANK = {"authoritative": 0, "reliable": 1, "unverified": 2}
#: 可算“已核验”的核验方式（与 collect_core.SourceInfo.anchor 同口径）
_ANCHORED = ("catalog_anchor", "verified_site_link", "session_verified")
#: 取页失败里属于“未授权/不可用”的类别（全败即 failed，而不是 partial）
_FATAL_FETCH = frozenset({"fetch_unavailable", "domain_not_allowed", "unauthorized"})
#: 一次运行的阶段总数（on_progress 的 total；pipeline 模式固定 6 段）
_STAGE_TOTAL = 6

#: 内置默认规则：与 research/rules.json 保持一致（契约 §6.1 的额度）。
#: 额外几个键（候选池/要点/线索/agent 步数）是编排必需的软上限，同样可被配置覆盖。
DEFAULT_RESEARCH_RULES: Dict[str, Any] = {
    "max_input_chars": 500,
    "max_sub_questions": 4,
    "max_searches": 10,
    "max_pages": 10,
    "max_pages_per_domain": 2,
    "max_llm_calls": 16,
    "max_followup_rounds": 1,
    "total_deadline_s": 90.0,
    "io_timeout_s": 10.0,
    "max_page_bytes": 1500000,
    "max_text_chars": 20000,
    "max_redirect_hops": 3,
    "max_quote_chars": 300,
    "max_candidates": 12,
    "max_findings": 6,
    "max_leads": 8,
    "agent_max_steps": 8,
}

#: 数值安全区间（键名与 rules.json 一致；越界夹回，坏值回退默认）
_RULE_LIMITS: Dict[str, Tuple[float, float]] = {
    "max_input_chars": (2, 20000),
    "max_sub_questions": (1, 12),
    "max_searches": (1, 200),
    "max_pages": (1, 200),
    "max_pages_per_domain": (1, 20),
    "max_llm_calls": (1, 500),
    "max_followup_rounds": (0, 10),
    "total_deadline_s": (1, 3600),
    "io_timeout_s": (0.5, 300),
    "max_page_bytes": (1000, 50000000),
    "max_text_chars": (200, 500000),
    "max_redirect_hops": (0, 10),
    "max_quote_chars": (10, 5000),
    "max_candidates": (1, 60),
    "max_findings": (1, 30),
    "max_leads": (1, 60),
    "agent_max_steps": (1, 40),
}


# ---------------------------------------------------------------------------
# 规则加载（缺键回退 + 数值夹取；坏配置只让“这次查得少一点”）
# ---------------------------------------------------------------------------

def _clamp_value(key: str, default: Any, value: Any) -> Any:
    """把单个配置值夹到默认类型的合法区间；坏值返回默认值。"""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        if isinstance(value, bool):
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number != number or number in (float("inf"), float("-inf")):
            return default
        low, high = _RULE_LIMITS.get(key, (float(default), float(default)))
        return int(round(min(max(number, float(low)), float(high))))
    if isinstance(default, float):
        if isinstance(value, bool):
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number != number or number in (float("inf"), float("-inf")):
            return default
        low, high = _RULE_LIMITS.get(key, (float(default), float(default)))
        return min(max(number, float(low)), float(high))
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    return value if value is not None else default


def _clamp_rules(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """对内存里的规则字典做与 load_collect_rules 同口径的夹取。"""
    out: Dict[str, Any] = {}
    for key, default in DEFAULT_RESEARCH_RULES.items():
        out[key] = _clamp_value(key, default, raw.get(key, default))
    return out


def load_research_rules(path: Optional[Any] = None) -> Dict[str, Any]:
    """读 research/rules.json：缺失/坏 JSON → 内置默认，数值越界夹回。"""
    target = Path(path) if path is not None else RESEARCH_RULES_PATH
    return load_collect_rules(target, DEFAULT_RESEARCH_RULES, _RULE_LIMITS)


# ---------------------------------------------------------------------------
# 纯文本小工具（数据字段与报告共用同一套清洗口径）
# ---------------------------------------------------------------------------

def _plain(value: Any, limit: int = 200) -> str:
    """压空白、去控制字符、截断：所有进入卡片的自由文本都过这里。"""
    text = normalize_text(value)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    return text[:limit]


def _safe(value: Any, limit: int = 200) -> str:
    """报告渲染用：把 HTML/markdown 标记字符换成全角等价物，避免注入 GUI。"""
    text = _plain(value, limit)
    for raw, safe in (("<", "＜"), (">", "＞"), ("[", "［"), ("]", "］")):
        text = text.replace(raw, safe)
    return text


def _string_items(value: Any, limit: int, item_limit: int) -> List[str]:
    """把模型返回的数组收敛成去重后的字符串列表（保序、去空白项）。"""
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in list(value)[:limit]:
        text = _plain(item, item_limit)
        if text and text not in out:
            out.append(text)
    return out


def _unpack_llm(raw: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """把 llm_fn 的返回折成 (obj, err)；兼容 (obj, err) 与裸 dict 两种写法。"""
    err: Any = ""
    if isinstance(raw, tuple) and len(raw) == 2:
        obj, err = raw
    elif isinstance(raw, Mapping):
        obj = raw
    else:
        return None, "llm_bad_shape"
    if not isinstance(obj, Mapping):
        return None, _plain(err, 200) or "llm_no_object"
    return dict(obj), ""


def _empty_card(topic: str) -> Dict[str, Any]:
    """契约 §5.1 的 data 骨架：所有键先就位，失败/澄清时也能渲染成空卡。"""
    return {
        "topic": topic,
        "scope": {"question": "", "time_window": "", "region": "", "basis": []},
        "sub_questions": [],
        "key_findings": [],
        "leads": [],
        "evidence": [],
        "sources": {"verified": [], "unverified": []},
        "timeline": [],
        "conflicts": [],
        "analysis": {"text": "", "evidence_ids": [], "label": "基于资料的整理/推断"},
        "followups": [],
    }


def _level_of_source(info: SourceInfo) -> str:
    """来源等级：authoritative > reliable > unverified（契约 §5）。

    权威 = 已核验（目录锚点/站点直链/会话内动态核验）**且**不是媒体或未分类；
    媒体/目录内可靠站点算 reliable；其余只能进 leads。
    """
    if info.verification in _ANCHORED and info.authority_class not in ("media", "unverified"):
        return "authoritative"
    if info.verification in _ANCHORED or info.authority_class == "media":
        return "reliable"
    return "unverified"


def _level_of_evidence(row: Mapping[str, Any]) -> str:
    """从已落卡的证据行反推等级（增量复用时用；口径与 _level_of_source 对齐）。"""
    verification = str(row.get("verification") or "unverified")
    tier = str(row.get("tier") or "")
    if verification in _ANCHORED and tier and tier not in ("未知来源", "未登记"):
        if tier in ("官方权威",):
            return "authoritative"
        return "reliable"
    if verification in _ANCHORED:
        return "reliable"
    return "unverified"


def _prior_data(prior: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """从上一张卡里取 data：兼容整卡（含 data 键）与裸 data 两种形状。"""
    if not isinstance(prior, Mapping):
        return {}
    data = prior.get("data")
    if isinstance(data, Mapping):
        return dict(data)
    if any(key in prior for key in ("topic", "evidence", "sub_questions", "scope")):
        return dict(prior)
    return {}


# ---------------------------------------------------------------------------
# 提示词机器契约（SKILL.md 只讲口径，JSON 键名只在这一处）
# ---------------------------------------------------------------------------

_SCOPE_CONTRACT = """只输出一个 JSON 对象，键名锁定：
{"state":"ok|too_broad|needs_clarification",
 "question":"<一句话核心研究问题>",
 "time_window":"<时间范围，未注明写空串>",
 "region":"<地区，未注明写空串>",
 "basis":["<界定范围的依据，一句话一条>"],
 "sub_questions":[{"text":"<一个可以用公开资料检索的子问题>"}]}
要求：sub_questions 最多 {cap} 个，每个都要能靠公开资料检索回答；state=too_broad 表示
主题过大、一次收集覆盖不了；state=needs_clarification 表示缺少必要限定。不要下结论、不要评分。"""

_EXTRACT_CONTRACT = """只输出一个 JSON 对象，键名锁定：
{"quotes":[{"quote":"<逐字摘自上面正文的原句>",
            "sq_ids":["<子问题编号，如 Q1>"],
            "role":"fact|context|conflict",
            "limitations":["<这条引文的局限>"]}],
 "published_at":"<页面发布日期，未注明写空串>"}
要求：quote 必须逐字出现在上面正文里（代码会做逐字定位校验，定位不到就丢弃）；
最多 {cap} 条；role 只能取 fact/context/conflict；不要改写、不要拼接两段、不要给评分。"""

_SYNTHESIS_CONTRACT = """只输出一个 JSON 对象，键名锁定：
{"key_findings":[{"statement":"<一句话要点>","sq_ids":["Q1"],
                   "evidence_ids":["E1"],"source_quote":"<所引证据的引文原文>"}],
 "leads":[{"statement":"<尚未有可用引文的线索>","evidence_ids":["E1"],"reason":"<为什么只能当线索>"}],
 "timeline":[{"date":"YYYY-MM-DD","event":"<事件>","evidence_ids":["E1"]}],
 "conflicts":[{"summary":"<资料之间的不一致>","evidence_ids":["E1","E2"]}],
 "analysis":{"text":"<基于所引证据的整理，可为空串>","evidence_ids":["E1"]},
 "followups":["<还可以继续查的方向>"]}
要求：key_findings 最多 {cap} 条；每条要点至少 1 个 evidence_id，且 source_quote 必须是
所引证据引文之一（逐字）；没有可锚定引文的内容只能放进 leads；不要输出评分或结论性判断。"""

_AGENT_CONTRACT = """你在一轮受限资料收集里决定下一步动作。只输出一个 JSON 对象：
{"action":"search|read|extract|finish","args":{...},"reason":"<一句话>"}
- search：args {"query":"检索词","k":条数}；
- read：args {"url":"必须是本轮候选池里出现过的 URL"}；
- extract：args {"url":"已读过的页面","quote":"逐字来自该页正文的引文","sq_ids":["Q1"]}；
- finish：信息已经足够时收尾。
候选池、已读页面、已摘录证据与剩余预算都由程序给出；越界的 read/extract 会被拒绝。
不要编造 URL 或引文，不要给评分或结论性判断。"""


# ---------------------------------------------------------------------------
# 一次运行的状态（每次 run 新建，实例可被复用/并发调用）
# ---------------------------------------------------------------------------

class _Run:
    """一次资料收集运行的全部可变状态。"""

    def __init__(self, skills: "ResearchSkills", *, topic: str, mode: str,
                 prior: Optional[Mapping[str, Any]], followup: bool,
                 cancel_check: Optional[Callable[[], bool]],
                 deadline: Optional[float],
                 on_progress: Optional[Callable[[str, int, int], None]]) -> None:
        self.skills = skills
        self.rules = skills.rules
        self.started_at = skills.clock()
        self.topic = topic
        self.mode = mode
        self.on_progress = on_progress
        self.deadline = deadline
        self.cancel_check = cancel_check
        budget = CollectBudget(self.rules, deadline=deadline)
        self.budget = budget
        self.st = CollectRunState(budget, cancel_check=cancel_check)
        self.card = _empty_card(topic)
        self.prior_data = _prior_data(prior) if followup else {}
        self.incremental = bool(self.prior_data)
        self.base_as_of = str(
            (prior.get("as_of") or prior.get("base_as_of") or "")
            if isinstance(prior, Mapping) else ""
        )
        self.prior_findings = 0
        self.prior_leads = 0
        # 候选池与已读页面都按 url_key 收敛：同一地址只读一次、只摘一次
        self.candidates: Dict[str, Dict[str, Any]] = {}
        self.pages: Dict[str, Dict[str, Any]] = {}
        self.extracted_keys: set = set()
        self.domain_reads: Dict[str, int] = {}
        self.evidence: List[Dict[str, Any]] = []
        self.levels: Dict[str, str] = {}
        self.seen_quotes: set = set()
        self.next_evidence = 1
        self.next_finding = 1
        self.next_lead = 1
        self.new_evidence_count = 0
        self.search_attempts = 0
        self.search_ok = 0
        self.search_failed = 0
        self.fetch_attempts = 0
        self.fetch_ok = 0
        self.fetch_categories: List[str] = []
        self.fatal = False
        self.degraded = False
        self.stage_no = 0
        self.agent_steps: List[Dict[str, Any]] = []
        self.agent_stop = ""
        self.agent_rejections = 0

    # -- 入口 ---------------------------------------------------------------
    def execute(self) -> Dict[str, Any]:
        """跑完整条流水线（或受限循环），任何情况下都返回统一外壳。"""
        # 增量必须在预检之前：追问时 topic 可能为空，主题要从上一张卡里取（契约 §9）。
        if self.incremental:
            self._load_prior()
        if not self._precheck():
            return self._finish()
        if self.st.is_cancelled():
            return self._finish()
        self._progress("范围与子问题", 1, _STAGE_TOTAL)
        if not self._stage_scope():
            return self._finish()
        if self.mode == "agent":
            self._run_agent()
        else:
            if not self._stage_search():
                return self._finish()
            self._stage_read()
            self._stage_extract()
            self._stage_followup()
        self._stage_synthesis()
        self._collect_sources()
        return self._finish()

    # -- 预检 ---------------------------------------------------------------
    def _precheck(self) -> bool:
        if not self.topic:
            self.st.error("precheck", "empty_topic", "主题为空或只有空白字符")
            self.st.clarify("请给出想收集资料的主题（一句话即可，例如：某地电动车电池安全监管）")
            return False
        limit = int(self.rules["max_input_chars"])
        if len(self.topic) > limit:
            self.st.error("precheck", "topic_too_long",
                          f"主题 {len(self.topic)} 字符，超过上限 {limit}")
            self.st.clarify(f"主题长度 {len(self.topic)} 超过单次上限 {limit}：请缩成一句话")
            return False
        if len(self.topic) < 2:
            self.st.error("precheck", "topic_too_short", "主题少于 2 个字符")
            self.st.clarify("主题太短：请补充一句说明想了解什么")
            return False
        return True

    # -- 增量：复用上一张卡 --------------------------------------------------
    def _load_prior(self) -> None:
        """把 prior 的 topic/scope/子问题/证据/要点/来源搬进本次卡片（旧 id 原样保留）。"""
        data = self.prior_data
        if not self.topic:
            self.topic = _plain(data.get("topic"), 500)
        self.card["topic"] = self.topic
        scope = data.get("scope")
        if isinstance(scope, Mapping):
            self.card["scope"] = {
                "question": _plain(scope.get("question"), 200),
                "time_window": _plain(scope.get("time_window"), 80),
                "region": _plain(scope.get("region"), 80),
                "basis": _string_items(scope.get("basis"), 6, 120),
            }
        sub_questions: List[Dict[str, Any]] = []
        for index, row in enumerate(data.get("sub_questions") or [], start=1):
            if not isinstance(row, Mapping):
                continue
            text = _plain(row.get("text"), 160)
            if not text:
                continue
            sub_questions.append({"sq_id": f"Q{index}", "text": text,
                                  "status": "uncovered", "evidence_count": 0})
        self.card["sub_questions"] = sub_questions
        for row in data.get("evidence") or []:
            if not isinstance(row, Mapping) or not row.get("quote"):
                continue
            item = dict(row)
            evidence_id = _plain(item.get("evidence_id"), 20) or f"E{self.next_evidence}"
            item["evidence_id"] = evidence_id
            item.setdefault("sq_ids", [])
            item.setdefault("limitations", [])
            self.evidence.append(item)
            self.levels[evidence_id] = _level_of_evidence(item)
            self._bump_evidence(evidence_id)
        for row in data.get("key_findings") or []:
            if isinstance(row, Mapping) and row.get("statement"):
                self.card["key_findings"].append(dict(row))
        for row in data.get("leads") or []:
            if isinstance(row, Mapping) and row.get("statement"):
                self.card["leads"].append(dict(row))
        self.prior_findings = len(self.card["key_findings"])
        self.prior_leads = len(self.card["leads"])
        self.next_finding = max(self.next_finding,
                                self._next_index(self.card["key_findings"], "finding_id"))
        self.next_lead = max(self.next_lead, self._next_index(self.card["leads"], "lead_id"))
        for key in ("timeline", "conflicts"):
            for row in data.get(key) or []:
                if isinstance(row, Mapping):
                    self.card[key].append(dict(row))
        analysis = data.get("analysis")
        if isinstance(analysis, Mapping) and analysis.get("text"):
            self.card["analysis"] = {
                "text": _plain(analysis.get("text"), 600),
                "evidence_ids": [str(x) for x in (analysis.get("evidence_ids") or [])],
                "label": "基于资料的整理/推断",
            }
        self.card["followups"] = _string_items(data.get("followups"), 6, 120)
        sources = data.get("sources")
        if isinstance(sources, Mapping):
            for bucket in ("verified", "unverified"):
                rows = sources.get(bucket)
                if isinstance(rows, list):
                    self.card["sources"][bucket] = [dict(row) for row in rows
                                                    if isinstance(row, Mapping)]
        if not self.card["sub_questions"]:
            self.st.gap("上一张卡没有可复用的子问题，本次按首轮重新界定范围")
        self._refresh_coverage()

    @staticmethod
    def _next_index(rows: Sequence[Mapping[str, Any]], key: str) -> int:
        """已有 F/L 编号的最大值 + 1（旧编号保留，新编号往后排）。"""
        largest = 0
        for row in rows:
            value = str(row.get(key) or "")
            digits = "".join(ch for ch in value if ch.isdigit())
            if digits:
                largest = max(largest, int(digits))
        return largest + 1

    def _bump_evidence(self, evidence_id: str) -> None:
        digits = "".join(ch for ch in str(evidence_id) if ch.isdigit())
        if digits:
            self.next_evidence = max(self.next_evidence, int(digits) + 1)

    # -- 阶段 1：范围与子问题 ------------------------------------------------
    def _stage_scope(self) -> bool:
        if self.card["sub_questions"]:
            # 增量复用：上一张卡的子问题编号必须原样保留（旧 evidence_id 还挂在上面），
            # 所以这里不再花一次模型调用重新界定范围。
            if not self.card["scope"]["question"]:
                self.card["scope"]["question"] = self.topic
            return True
        cap = int(self.rules["max_sub_questions"])
        system = load_skill_prompt(_HERE / "scope",
                                   _SCOPE_CONTRACT.replace("{cap}", str(cap)))
        user = f"用户主题：{self.topic}"
        if self.prior_data:
            user += f"\n上一张卡的研究问题：{self.card['scope'].get('question') or '（无）'}"
            user += "\n用户希望在此基础上补充资料，请把主题收敛成可检索的子问题。"
        obj, err = self._call_llm("scope", system, user, "scope")
        if obj is None:
            if self.st.is_cancelled():
                self.st.gap("运行在范围阶段被取消")
                return False
            if err == "llm_budget_exhausted":
                self.st.gap("模型调用预算已达上限，范围与子问题未完成")
                return False
            self.st.error("scope", "llm_unavailable", err or "模型不可用，无法界定范围")
            self._fail("范围与子问题阶段模型不可用，后续阶段未开始")
            return False
        state = str(obj.get("state") or "").strip().lower()
        if state in ("too_broad", "needs_clarification"):
            category = "scope_too_broad" if state == "too_broad" else "scope_needs_detail"
            self.st.error("scope", category, f"模型判定主题状态：{state}")
            self.st.clarify("主题过宽或缺少限定：请补充对象、时间或地区后重试")
            return False
        question = _plain(obj.get("question"), 200) or self.topic
        basis = _string_items(obj.get("basis"), 4, 120)
        rows = obj.get("sub_questions")
        if not isinstance(rows, list):
            rows = obj.get("questions") if isinstance(obj.get("questions"), list) else []
        texts: List[str] = []
        for row in rows[: max(cap + 1, 1)]:
            text = row if isinstance(row, str) else (row.get("text") if isinstance(row, Mapping)
                                                      else "")
            text = _plain(text, 160)
            if text and text not in texts:
                texts.append(text)
        if len(rows) > cap:
            self.st.error("scope", "too_many_sub_questions",
                          f"模型给出 {len(rows)} 个子问题，已截到上限 {cap}")
        if not texts:
            texts = [f"{question} 的公开资料有哪些"]
            self.st.error("scope", "sub_questions_fallback",
                          "模型未给出可解析的子问题，已按研究问题生成 1 个")
        self.card["scope"] = {"question": question,
                              "time_window": _plain(obj.get("time_window"), 80),
                              "region": _plain(obj.get("region"), 80),
                              "basis": basis}
        self.card["sub_questions"] = [
            {"sq_id": f"Q{index}", "text": text, "status": "uncovered", "evidence_count": 0}
            for index, text in enumerate(texts, start=1)
        ]
        return True

    # -- 阶段 2（pipeline）：检索 -------------------------------------------
    def _stage_search(self) -> bool:
        self._progress("检索公开资料", 2, _STAGE_TOTAL)
        targets = [sq for sq in self.card["sub_questions"] if sq["status"] != "covered"]
        for sq in targets:
            if self._stop():
                break
            query = self._query_for(self.topic, sq["text"])
            if not query:
                self.st.gap(f"子问题 {sq['sq_id']} 脱敏后没有可用检索词")
                continue
            rows, status = self._search(query, 6, "search", sq["sq_id"])
            if status in ("budget", "cancelled"):
                break
            self._add_candidates(rows, sq["sq_id"])
            if not rows and status == "ok":
                self.st.gap(f"子问题 {sq['sq_id']} 未检索到公开资料")
        if self.search_ok == 0 and self.search_failed > 0:
            self._fail("检索后端全部失败或未授权，未能收集资料")
            return False
        return True

    # -- 阶段 3（pipeline）：读页 -------------------------------------------
    def _stage_read(self) -> None:
        self._progress("读取页面正文", 3, _STAGE_TOTAL)
        ordered = sorted(self.candidates.values(),
                         key=lambda item: (_LEVEL_RANK.get(item["level"], 9), item["rank"]))
        per_domain = int(self.rules["max_pages_per_domain"])
        for cand in ordered:
            if self._stop():
                break
            key = url_key(cand["url"])
            if key in self.pages:
                continue
            if self.domain_reads.get(cand["domain"], 0) >= per_domain:
                continue
            if not self.budget.allow("max_pages"):
                self.budget.block(self.budget.why("max_pages"))
                self.st.gap("读页预算已达上限，部分候选未读取")
                break
            page, category = self._do_fetch(cand["url"], [cand["domain"]], "read")
            if page is None:
                if category == "cancelled":
                    break
                continue
            self.pages[key] = page
            self.domain_reads[cand["domain"]] = self.domain_reads.get(cand["domain"], 0) + 1
        if (self.fetch_attempts and self.fetch_ok == 0
                and all(item in _FATAL_FETCH for item in self.fetch_categories)):
            self._fail("取页未授权或不可用，未能读取任何页面")

    # -- 阶段 4（pipeline）：摘录 -------------------------------------------
    def _stage_extract(self) -> None:
        self._progress("摘录原文引文", 4, _STAGE_TOTAL)
        cap = int(self.rules["max_quote_chars"])
        for key, page in list(self.pages.items()):
            if self._stop():
                break
            if key in self.extracted_keys:
                continue
            body = normalize_text(page.get("text") or "")
            if not body:
                self.st.error("extract", "empty_page_text",
                              f"{_plain(page.get('url'), 200)} 读取成功但正文为空，已丢弃")
                self.extracted_keys.add(key)
                continue
            cand = self.candidates.get(key) or {}
            system = load_skill_prompt(_HERE / "digest",
                                       _EXTRACT_CONTRACT.replace("{cap}", "2"))
            user = self._extract_user(cand, page, body)
            obj, err = self._call_llm("extract", system, user,
                                      f"extract:{cand.get('domain') or 'page'}")
            self.extracted_keys.add(key)
            if obj is None:
                self.st.error("extract", "quote_unavailable",
                              err or "模型未给出可解析的引文")
                continue
            self._take_quotes(obj, page, cand, body, cap)

    def _extract_user(self, cand: Mapping[str, Any], page: Mapping[str, Any],
                      body: str) -> str:
        """摘录提示：子问题清单 + 来源画像 + 已读正文（截断到 4000 字）。"""
        lines = ["子问题清单："]
        for sq in self.card["sub_questions"]:
            lines.append(f"- {sq['sq_id']} {sq['text']}")
        source = cand.get("source")
        name = getattr(source, "name", "") or cand.get("domain", "")
        tier = getattr(source, "tier", "")
        verification = getattr(source, "verification", "")
        lines.append("")
        lines.append(f"信源：{name}（{cand.get('domain') or ''}）档位：{tier} "
                     f"核验方式：{verification}")
        lines.append(f"标题：{_plain(page.get('title'), 120)}")
        lines.append(f"已读正文（前 4000 字）：\n{body[:4000]}")
        return "\n".join(lines)

    def _take_quotes(self, obj: Mapping[str, Any], page: Mapping[str, Any],
                     cand: Mapping[str, Any], body: str, cap: int) -> None:
        """校验并落库模型给出的引文：逐字锚定、长度截断、引文去重。"""
        rows = obj.get("quotes")
        if not isinstance(rows, list):
            rows = [obj] if obj.get("quote") else []
        published = _plain(obj.get("published_at"), 40) or _plain(page.get("published_at"), 40)
        valid_sq = {sq["sq_id"] for sq in self.card["sub_questions"]}
        for row in rows[:2]:
            if not isinstance(row, Mapping):
                continue
            quote = normalize_text(row.get("quote"))
            if not quote:
                self.st.error("extract", "quote_empty", "模型未给出引文")
                continue
            if len(quote) > cap:
                shortened = quote[:cap]
                if anchor_quote(body, shortened) is None:
                    self.st.error("extract", "quote_too_long",
                                  f"引文超过 {cap} 字符且截断后无法定位，已丢弃")
                    continue
                quote = shortened
            if anchor_quote(body, quote) is None:
                self.st.error("extract", "quote_not_anchored",
                              f"{cand.get('domain') or ''} 的引文未在已读正文中逐字定位，已丢弃")
                continue
            role = str(row.get("role") or "").strip().lower()
            if role not in ROLES:
                if role:
                    self.st.error("extract", "bad_role",
                                  f"未知证据角色：{role}，按 fact 处理")
                role = "fact"
            sq_ids = [str(item) for item in (row.get("sq_ids") or [])
                      if str(item) in valid_sq]
            if not sq_ids:
                sq_ids = list(cand.get("sq_ids") or [])
            quote_key = (url_key(page.get("final_url") or page.get("url") or ""),
                         normalize_text(quote))
            if quote_key in self.seen_quotes:
                continue    # 同一页同一句重复摘录：不算新证据，也不报错
            self.seen_quotes.add(quote_key)
            self._add_evidence(page, cand, quote, sq_ids, role, published,
                               _string_items(row.get("limitations"), 4, 120), body)

    def _add_evidence(self, page: Mapping[str, Any], cand: Mapping[str, Any],
                      quote: str, sq_ids: Sequence[str], role: str, published: str,
                      limitations: Sequence[str], body: str) -> str:
        """写一条证据（含来源画像、时点、局限），返回 evidence_id。"""
        source: Optional[SourceInfo] = cand.get("source")
        level = cand.get("level") or "unverified"
        evidence_id = f"E{self.next_evidence}"
        self.next_evidence += 1
        limits = list(dict.fromkeys(str(item) for item in limitations if str(item)))
        if len(quote) < 20:
            limits.insert(0, "引文较短，可支撑的上下文有限")
        if level == "unverified":
            limits.append("来源未核验（目录外域名或信号不足），只能作为线索")
        if len(normalize_text(body)) >= int(self.rules["max_text_chars"]):
            limits.append("页面正文可能被截断，引文之外的上下文未核对")
        record = {
            "evidence_id": evidence_id,
            "sq_ids": list(dict.fromkeys(sq_ids)),
            "source_id": getattr(source, "source_id", "") or cand.get("domain", ""),
            "name": getattr(source, "name", "") or cand.get("domain", ""),
            "domain": getattr(source, "domain", "") or cand.get("domain", ""),
            "tier": getattr(source, "tier", "") or "未知来源",
            "verification": getattr(source, "verification", "unverified"),
            "title": _plain(page.get("title"), 120),
            "url": _plain(page.get("final_url") or page.get("url"), 300),
            "quote": _plain(quote, int(self.rules["max_quote_chars"])),
            "published_at": published,
            "fetched_at": _plain(page.get("fetched_at"), 40) or self.started_at,
            "role": role,
            "limitations": list(dict.fromkeys(limits)),
        }
        self.evidence.append(record)
        self.levels[evidence_id] = level
        self.new_evidence_count += 1
        return evidence_id

    # -- 阶段 5（pipeline）：补检索 -----------------------------------------
    def _stage_followup(self) -> None:
        """对未覆盖子问题做有限轮补检索（`max_followup_rounds`，默认 1 轮）。"""
        rounds = int(self.rules["max_followup_rounds"])
        if rounds <= 0:
            return
        self._refresh_coverage()
        for _ in range(rounds):
            gaps = [sq for sq in self.card["sub_questions"] if sq["status"] != "covered"]
            if not gaps or self._stop():
                break
            self._progress("补检索未覆盖子问题", 5, _STAGE_TOTAL)
            before = len(self.candidates)
            for sq in gaps[:2]:
                if self._stop():
                    break
                query = self._query_for(self.topic, f"{sq['text']} 官方 通报")
                rows, status = self._search(query, 6, "followup", sq["sq_id"])
                if status in ("budget", "cancelled"):
                    break
                self._add_candidates(rows, sq["sq_id"])
            if len(self.candidates) == before:
                break     # 没有新候选：再查一轮也是同一批结果
            self._stage_read()
            self._stage_extract()
            self._refresh_coverage()

    # -- agent 模式 ---------------------------------------------------------
    def _run_agent(self) -> None:
        """受限循环：模型决定动作，代码执行并校验候选池/锚定/预算。"""
        self._progress("受限检索循环", 2, int(self.rules["agent_max_steps"]))
        system = load_skill_prompt(_HERE / "agent", _AGENT_CONTRACT)
        loop = run_agent_loop(
            llm_fn=self._agent_llm,
            system_prompt=system,
            goal=self._agent_goal(),
            describe_state=self._describe_state,
            apply_action=self._apply_action,
            max_steps=int(self.rules["agent_max_steps"]),
            budget=self.budget,
            state=_AgentStateView(self.st, self.budget, self.deadline),
            on_step=self._on_agent_step,
        )
        self.agent_steps = list(result_steps(loop))
        self.agent_stop = str(loop.get("stop_reason") or "")
        if self.agent_stop in ("budget_exhausted", "deadline") and not self.budget.blocked_reason:
            # 循环因预算/死线停下时把预算标记为到顶：否则收口会把“查不动了”说成 ordinary partial
            self.budget.block("受限检索的预算或死线到顶")
        if self.agent_stop in ("max_steps", "illegal_actions", "finish_failed",
                               "llm_unavailable", "apply_action_unavailable"):
            self.st.error("agent", f"agent_{self.agent_stop}",
                          "受限检索未正常收尾，已保留已取得资料")
            self.st.gap("检索循环在未收尾的情况下停止，结果可能不完整")
        self._refresh_coverage()

    def _agent_goal(self) -> str:
        questions = "；".join(f"{sq['sq_id']} {sq['text']}"
                             for sq in self.card["sub_questions"])
        return (f"围绕主题《{self.topic}》收集公开资料。研究问题："
                f"{self.card['scope'].get('question') or self.topic}。子问题：{questions}")

    def _describe_state(self) -> str:
        """给模型看的程序状态：候选池、已读、已摘录、剩余预算（只报事实）。"""
        lines = [f"主题：{self.topic}"]
        lines.append("子问题：" + "；".join(
            f"{sq['sq_id']}={sq['text']}（{sq['status']}）"
            for sq in self.card["sub_questions"]) or "（无）")
        pool = list(self.candidates.values())[:10]
        lines.append(f"候选池（{len(self.candidates)} 条，最多显示 10 条）：")
        for cand in pool:
            lines.append(f"- [{cand['level']}] {cand['url']} 《{cand['title']}》")
        lines.append(f"已读页面（{len(self.pages)}）：" + "；".join(
            _plain(page.get("url"), 120) for page in list(self.pages.values())[:6]))
        ids = [ev["evidence_id"] for ev in self.evidence]
        lines.append(f"已摘录证据：{'、'.join(ids) if ids else '（无）'}")
        lines.append("剩余预算："
                     f"搜索 {self.budget.remaining('max_searches')}、"
                     f"读页 {self.budget.remaining('max_pages')}、"
                     f"模型调用 {self.budget.remaining('max_llm_calls')}")
        return "\n".join(lines)

    def _agent_llm(self, system: str, user: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """agent 循环的模型调用：记账 + 审计（引擎只读预算，记账归这里）。"""
        if self.skills.llm_fn is None:
            return None, "llm_unavailable"
        if not self.budget.allow("max_llm_calls"):
            self.budget.block(self.budget.why("max_llm_calls"))
            return None, "llm_budget_exhausted"
        if self.st.is_cancelled():
            return None, "cancelled"
        self.budget.spend("max_llm_calls")
        entry = self.st.operation("llm", "agent:step", False)
        try:
            out = self.skills.llm_fn(system, user)
        except Exception as exc:  # noqa: BLE001 模型侧异常折成本步失败，不中断循环
            return None, f"llm_exception:{type(exc).__name__}"
        obj, err = _unpack_llm(out)
        if obj is None:
            return None, err or "llm_no_object"
        entry["ok"] = True
        return obj, ""

    def _on_agent_step(self, payload: Mapping[str, Any]) -> None:
        label = str(payload.get("step_label") or payload.get("action") or "步骤")
        self._progress(label, int(payload.get("n") or 0),
                       int(self.rules["agent_max_steps"]))

    def _apply_action(self, action: str, args: Mapping[str, Any]) -> Dict[str, Any]:
        """执行一个 agent 动作；越界/未锚定一律拒绝并记错（如实 partial）。"""
        if action == "search":
            return self._action_search(args)
        if action == "read":
            return self._action_read(args)
        if action == "extract":
            return self._action_extract(args)
        if action == "finish":
            return {"ok": True, "done": True, "observation": "模型宣布收尾",
                    "error": "", "step_label": "收尾"}
        return self._reject("unknown_action", f"未知动作：{action}")

    def _action_search(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        query = self._query_for(args.get("query"), "")
        if not query:
            return self._reject("query_empty", "检索词为空或脱敏后为空")
        try:
            k = int(args.get("k") or 6)
        except (TypeError, ValueError):
            k = 6
        rows, status = self._search(query, max(1, min(k, 10)), "agent", "")
        if status == "budget":
            return self._reject("budget_exhausted", "搜索预算已用尽")
        if status == "cancelled":
            return self._reject("cancelled", "运行已取消")
        added = self._add_candidates(rows, "")
        observation = f"检索到 {len(rows or [])} 条，新增候选 {added} 个。"
        if self.candidates:
            observation += "候选池：" + "；".join(
                f"[{c['level']}] {c['url']}" for c in list(self.candidates.values())[:6])
        return {"ok": True, "observation": observation, "error": "", "step_label": "检索"}

    def _action_read(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        url = str(args.get("url") or "").strip()
        key = url_key(url)
        if not key:
            return self._reject("bad_url", "缺少 URL")
        if key not in self.candidates:
            return self._reject("url_not_in_pool", "该地址不在本轮候选池：请先 search")
        if key in self.pages:
            return self._reject("already_read", "该页面已经读过了")
        cand = self.candidates[key]
        per_domain = int(self.rules["max_pages_per_domain"])
        if self.domain_reads.get(cand["domain"], 0) >= per_domain:
            return self._reject("domain_page_limit",
                                f"该域名本次已读满 {per_domain} 页")
        if not self.budget.allow("max_pages"):
            self.budget.block(self.budget.why("max_pages"))
            return self._reject("budget_exhausted", "读页预算已用尽")
        page, category = self._do_fetch(cand["url"], [cand["domain"]], "agent")
        if page is None:
            return self._reject(category or "fetch_error", "取页失败，无法读取该地址")
        self.pages[key] = page
        self.domain_reads[cand["domain"]] = self.domain_reads.get(cand["domain"], 0) + 1
        body = normalize_text(page.get("text") or "")
        observation = (f"已读《{_plain(page.get('title'), 80)}》，正文 {len(body)} 字。"
                       f"开头：{body[:200]}")
        return {"ok": True, "observation": observation, "error": "", "step_label": "读页"}

    def _action_extract(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        url = str(args.get("url") or "").strip()
        key = url_key(url)
        if key not in self.pages:
            return self._reject("url_not_read", "只能从已读过的页面摘录")
        page = self.pages[key]
        body = normalize_text(page.get("text") or "")
        quote = normalize_text(args.get("quote"))
        if not quote:
            return self._reject("quote_empty", "引文为空")
        cap = int(self.rules["max_quote_chars"])
        if len(quote) > cap:
            quote = quote[:cap]
        if anchor_quote(body, quote) is None:
            return self._reject("quote_not_anchored", "引文未在已读正文中逐字定位")
        valid_sq = {sq["sq_id"] for sq in self.card["sub_questions"]}
        sq_ids = [str(item) for item in (args.get("sq_ids") or []) if str(item) in valid_sq]
        cand = self.candidates.get(key) or {}
        quote_key = (key, normalize_text(quote))
        if quote_key in self.seen_quotes:
            return {"ok": True, "observation": "这条引文已经摘录过，未重复计入。",
                    "error": "", "step_label": "摘录"}
        self.seen_quotes.add(quote_key)
        evidence_id = self._add_evidence(page, cand, quote,
                                         sq_ids or list(cand.get("sq_ids") or []),
                                         "fact", _plain(page.get("published_at"), 40),
                                         [], body)
        return {"ok": True, "observation": f"已记录证据 {evidence_id}。",
                "error": "", "step_label": "摘录"}

    def _reject(self, code: str, message: str) -> Dict[str, Any]:
        """拒绝一个越界/非法动作：记 error + 缺口（结果如实 partial）。"""
        self.agent_rejections += 1
        self.st.error("agent", f"rejected_{code}", message)
        self.st.gap("受限检索中有动作被拒绝，结果可能不完整")
        return {"ok": False, "observation": message, "error": code, "step_label": "拒绝"}

    # -- 阶段 6：综述 -------------------------------------------------------
    def _stage_synthesis(self) -> None:
        self._progress("综述与要点", 6 if self.mode == "pipeline" else 3, _STAGE_TOTAL)
        if not self.evidence:
            self.st.gap("没有可综述的已锚定引文，本次未形成要点")
            self._refresh_coverage()
            return
        system = load_skill_prompt(
            _HERE / "digest",
            _SYNTHESIS_CONTRACT.replace("{cap}", str(int(self.rules["max_findings"]))))
        obj, err = self._call_llm("synthesis", system, self._synthesis_user(), "synthesis")
        if obj is None:
            self.st.error("synthesis", "llm_unavailable", err or "模型未给出可解析的综述")
            self.st.gap("综述未完成（模型不可用），只保留已锚定引文")
            self._refresh_coverage()
            return
        self._apply_synthesis(obj)
        self._refresh_coverage()

    def _synthesis_user(self) -> str:
        """综述提示：主题/范围/子问题/全部可引证据（含引文）+ 增量时的已有要点。"""
        scope = self.card["scope"]
        lines = [f"主题：{self.topic}",
                 f"研究问题：{scope.get('question') or self.topic}",
                 f"时间范围：{scope.get('time_window') or '未注明'}　"
                 f"地区：{scope.get('region') or '未注明'}",
                 "子问题："]
        for sq in self.card["sub_questions"]:
            lines.append(f"- {sq['sq_id']} {sq['text']}（{sq['status']}）")
        lines.append("")
        lines.append("已锚定引文（只能引用这些 evidence_id）：")
        for ev in self.evidence:
            lines.append(render_evidence_line(ev))
        if self.card["key_findings"]:
            lines.append("")
            lines.append("上一张卡已有的要点（不要原样重复，只补新增）：")
            for row in self.card["key_findings"]:
                lines.append(f"- {row.get('finding_id')} {row.get('statement')}")
        return "\n".join(lines)

    def _apply_synthesis(self, obj: Mapping[str, Any]) -> None:
        """校验综述输出：只有“引文可锚定 + 来源等级够”的内容能成为要点。"""
        known = {ev["evidence_id"]: ev for ev in self.evidence}
        valid_sq = {sq["sq_id"] for sq in self.card["sub_questions"]}
        budget = max(0, int(self.rules["max_findings"]) - len(self.card["key_findings"]))
        rows = obj.get("key_findings")
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            if budget <= 0:
                break
            if not isinstance(row, Mapping):
                continue
            statement = _plain(row.get("statement"), 300)
            if not statement:
                continue
            raw_ids = [str(item) for item in (row.get("evidence_ids") or [])
                       if isinstance(item, (str, int))]
            ids = [item for item in dict.fromkeys(raw_ids) if item in known]
            unknown = [item for item in raw_ids if item not in known]
            if unknown:
                self.st.error("synthesis", "unknown_evidence_id",
                              f"要点引用了不存在的证据，已忽略：{'、'.join(unknown[:4])}")
            if not ids:
                self._to_lead(statement, [], "综述未给出可用的 evidence_id，只能作为线索")
                self.st.error("synthesis", "finding_without_evidence",
                              "要点没有引用任何已存在证据，已降级为线索")
                continue
            levels = [self.levels.get(item, "unverified") for item in ids]
            if all(level == "unverified" for level in levels):
                self._to_lead(statement, ids, "只引用了未核验来源，按规则只能作为线索")
                continue
            quote_map = {normalize_text(known[item].get("quote")): known[item].get("quote")
                         for item in ids}
            quote = normalize_text(row.get("source_quote"))
            if not quote or quote not in quote_map:
                self._to_lead(statement, ids, "综述未给出可锚定的原文引文，只能作为线索")
                self.st.error("synthesis", "finding_quote_not_anchored",
                              "要点的引文不是所引证据的引文，已降级为线索")
                continue
            support = "authoritative" if "authoritative" in levels else "reliable"
            self.card["key_findings"].append({
                "finding_id": f"F{self.next_finding}",
                "sq_ids": [str(item) for item in (row.get("sq_ids") or [])
                           if str(item) in valid_sq],
                "statement": statement,
                "evidence_ids": ids,
                "support_level": support,
                "source_quote": quote_map[quote],
                "checkable": True,
            })
            self.next_finding += 1
            budget -= 1
        self._take_leads(obj.get("leads"))
        self._take_timeline(obj.get("timeline"), known)
        self._take_conflicts(obj.get("conflicts"), known)
        self._take_analysis(obj.get("analysis"), known)
        for text in _string_items(obj.get("followups"), 4, 120):
            if text not in self.card["followups"]:
                self.card["followups"].append(text)
        self.card["followups"] = self.card["followups"][:6]

    def _take_leads(self, rows: Any) -> None:
        if not isinstance(rows, list):
            return
        known = {ev["evidence_id"] for ev in self.evidence}
        for row in rows:
            if len(self.card["leads"]) >= int(self.rules["max_leads"]):
                return
            if not isinstance(row, Mapping):
                continue
            statement = _plain(row.get("statement"), 300)
            if not statement:
                continue
            ids = [str(item) for item in (row.get("evidence_ids") or [])
                   if str(item) in known]
            self._to_lead(statement, ids, _plain(row.get("reason"), 120)
                          or "综述给出的线索，暂无可直接引用的原文")

    def _to_lead(self, statement: str, evidence_ids: Sequence[str], reason: str) -> None:
        """写一条线索（同一句不重复；条数受 max_leads 限制）。"""
        if len(self.card["leads"]) >= int(self.rules["max_leads"]):
            return
        for row in self.card["leads"]:
            if row.get("statement") == statement:
                return
        self.card["leads"].append({
            "lead_id": f"L{self.next_lead}",
            "statement": statement,
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "reason": _plain(reason, 160),
        })
        self.next_lead += 1

    def _take_timeline(self, rows: Any, known: Mapping[str, Mapping[str, Any]]) -> None:
        if not isinstance(rows, list):
            return
        seen = {(row.get("date"), row.get("event")) for row in self.card["timeline"]}
        for row in rows[:8]:
            if not isinstance(row, Mapping):
                continue
            date = _plain(row.get("date"), 40)
            event = _plain(row.get("event"), 200)
            ids = [str(item) for item in (row.get("evidence_ids") or [])
                   if str(item) in known]
            if not date or not event or not ids:
                continue
            if (date, event) in seen:
                continue
            seen.add((date, event))
            self.card["timeline"].append({"date": date, "event": event,
                                          "evidence_ids": ids})

    def _take_conflicts(self, rows: Any, known: Mapping[str, Mapping[str, Any]]) -> None:
        if not isinstance(rows, list):
            return
        seen = {row.get("summary") for row in self.card["conflicts"]}
        for row in rows[:4]:
            if not isinstance(row, Mapping):
                continue
            summary = _plain(row.get("summary"), 240)
            ids = [str(item) for item in (row.get("evidence_ids") or [])
                   if str(item) in known]
            ids = list(dict.fromkeys(ids))
            if not summary or len(ids) < 2 or summary in seen:
                continue
            seen.add(summary)
            self.card["conflicts"].append({"summary": summary, "evidence_ids": ids})

    def _take_analysis(self, row: Any, known: Mapping[str, Mapping[str, Any]]) -> None:
        if not isinstance(row, Mapping):
            return
        text = _plain(row.get("text"), 600)
        ids = [str(item) for item in (row.get("evidence_ids") or [])
               if str(item) in known]
        if not text or not ids:
            return
        self.card["analysis"] = {"text": text, "evidence_ids": list(dict.fromkeys(ids)),
                                 "label": "基于资料的整理/推断"}

    # -- 覆盖度与来源清单 ---------------------------------------------------
    def _refresh_coverage(self) -> None:
        """按已锚定证据重算子问题覆盖度（covered/partial/uncovered）。"""
        total: Dict[str, int] = {}
        strong: Dict[str, int] = {}
        for ev in self.evidence:
            for sq_id in ev.get("sq_ids") or []:
                total[sq_id] = total.get(sq_id, 0) + 1
                if self.levels.get(ev.get("evidence_id"), "unverified") != "unverified":
                    strong[sq_id] = strong.get(sq_id, 0) + 1
        for sq in self.card["sub_questions"]:
            count = total.get(sq["sq_id"], 0)
            sq["evidence_count"] = count
            if strong.get(sq["sq_id"]):
                sq["status"] = "covered"
            elif count:
                sq["status"] = "partial"
            else:
                sq["status"] = "uncovered"

    def _collect_sources(self) -> None:
        """把实际读到过的候选按“已核验/未核验”分组写进卡片（增量时保留旧条目）。"""
        seen = {str(row.get("source_id") or row.get("domain"))
                for bucket in ("verified", "unverified")
                for row in self.card["sources"][bucket] if isinstance(row, Mapping)}
        for key, cand in self.candidates.items():
            if key not in self.pages:
                continue
            source: Optional[SourceInfo] = cand.get("source")
            level = cand.get("level") or "unverified"
            source_id = getattr(source, "source_id", "") or cand.get("domain", "")
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            bucket = "unverified" if level == "unverified" else "verified"
            self.card["sources"][bucket].append({
                "source_id": source_id,
                "name": getattr(source, "name", "") or cand.get("domain", ""),
                "domain": getattr(source, "domain", "") or cand.get("domain", ""),
                "tier": getattr(source, "tier", "") or "未知来源",
                "verification": getattr(source, "verification", "unverified"),
                "level": level,
                "url": cand.get("url", ""),
            })

    # -- IO 适配层 ----------------------------------------------------------
    def _call_llm(self, stage: str, system: str, user: str, target: str
                  ) -> Tuple[Optional[Dict[str, Any]], str]:
        """一次模型调用：预算 + 审计；返回 (obj, err)，与 chat_json 契约同族。"""
        if self.skills.llm_fn is None:
            return None, "llm_unavailable"
        if not self.budget.allow("max_llm_calls"):
            self.budget.block(self.budget.why("max_llm_calls"))
            return None, "llm_budget_exhausted"
        if self.st.is_cancelled():
            return None, "cancelled"
        self.budget.spend("max_llm_calls")
        entry = self.st.operation("llm", target or stage, False)
        try:
            out = self.skills.llm_fn(system, user)
        except Exception as exc:  # noqa: BLE001 模型侧异常按本次调用失败处理，不中断流水线
            return None, f"llm_exception:{type(exc).__name__}"
        obj, err = _unpack_llm(out)
        if obj is None:
            return None, err or "llm_no_object"
        entry["ok"] = True
        return obj, ""

    def _search(self, query: str, k: int, stage: str, sq_id: str
                ) -> Tuple[List[Mapping[str, Any]], str]:
        """一次检索：返回 (结果行, 状态)；状态 ok/degraded/unavailable/budget/cancelled。"""
        if self.skills.search_fn is None:
            self.search_attempts += 1
            self.search_failed += 1
            self.st.error(stage, "search_unavailable", "未注入检索实现，检索未执行")
            self.st.gap("检索未执行（未配置检索实现），覆盖不完整")
            return [], "unavailable"
        if not self.budget.allow("max_searches"):
            self.budget.block(self.budget.why("max_searches"))
            return [], "budget"
        if self.st.is_cancelled():
            return [], "cancelled"
        self.budget.spend("max_searches")
        self.search_attempts += 1
        entry = self.st.operation("search", query, False)
        try:
            raw = self.skills.search_fn(query, k)
        except Exception as exc:  # noqa: BLE001 检索侧异常按降级处理，不当成“搜过了没找到”
            self.search_failed += 1
            self.degraded = True
            self.st.error(stage, "search_unavailable", f"检索异常：{type(exc).__name__}")
            self.st.gap("检索未执行/降级，覆盖不完整")
            return [], "unavailable"
        if not isinstance(raw, Mapping):
            self.search_failed += 1
            self.degraded = True
            self.st.error(stage, "search_unavailable", "检索返回形状不符")
            self.st.gap("检索未执行/降级，覆盖不完整")
            return [], "unavailable"
        rows = raw.get("results")
        rows = [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
        degraded = bool(raw.get("degraded"))
        error = _plain(raw.get("error"), 200)
        if degraded or error:
            self.search_failed += 1
            self.degraded = True
            self.st.error(stage, "search_unavailable", error or "检索降级")
            self.st.gap("检索未执行/降级，覆盖不完整")
            return rows, "degraded"
        self.search_ok += 1
        entry["ok"] = True
        if sq_id:
            entry["target"] = f"{sq_id} {query}"[:300]
        return rows, "ok"

    def _fetch_callable(self) -> Optional[Callable[..., Dict[str, Any]]]:
        """取页实现：优先注入件，其次默认 transport（按 rules 构造，延迟 import）。"""
        if self.skills.fetch_fn is not None:
            return self.skills.fetch_fn
        if self.skills._fetch_probed:
            return None
        self.skills._fetch_probed = True
        try:
            from mylocalai.assistant.skills.transport import default_transport
            transport = self.skills.transport or default_transport(
                max_bytes=int(self.rules["max_page_bytes"]),
                max_text_chars=int(self.rules["max_text_chars"]),
                timeout_s=float(self.rules["io_timeout_s"]),
                max_hops=int(self.rules["max_redirect_hops"]))
            self.skills.fetch_fn = getattr(transport, "fetch", None)
        except Exception:  # noqa: BLE001 transport 未就位/导入失败：降级为“取页不可用”
            self.skills.fetch_fn = None
        return self.skills.fetch_fn

    def _do_fetch(self, url: str, allowed: Sequence[str], stage: str
                  ) -> Tuple[Optional[Dict[str, Any]], str]:
        """一次读页：返回 (page|None, 失败类别)；只有成功才计入读页预算。"""
        fetch = self._fetch_callable()
        if fetch is None:
            self.fetch_attempts += 1
            self.fetch_categories.append("fetch_unavailable")
            self.st.error(stage, "fetch_unavailable",
                          "取页不可用：未注入 fetch_fn 且默认 transport 不可用")
            return None, "fetch_unavailable"
        if self.st.is_cancelled():
            return None, "cancelled"
        entry = self.st.operation("fetch", url, False)
        try:
            page = fetch(url, allowed_domains=list(allowed),
                         cancel_check=self.cancel_check, deadline=self.deadline)
        except Exception as exc:  # noqa: BLE001 取页异常按失败记，不当成“页面无引文”
            self.fetch_attempts += 1
            self.fetch_categories.append("fetch_exception")
            self.st.error(stage, "fetch_exception", f"取页异常：{type(exc).__name__}")
            return None, "fetch_exception"
        if not isinstance(page, Mapping):
            self.fetch_attempts += 1
            self.fetch_categories.append("fetch_unavailable")
            self.st.error(stage, "fetch_unavailable", "取页返回形状不符")
            return None, "fetch_unavailable"
        if page.get("status") != "ok":
            category = _plain(page.get("category") or "fetch_error", 60) or "fetch_error"
            detail = _plain(page.get("detail") or "取页失败", 200)
            self.fetch_attempts += 1
            self.fetch_categories.append(category)
            self.degraded = True
            self.st.error(stage, category, f"{_plain(url, 200)}：{detail}")
            return None, category
        self.fetch_attempts += 1
        self.fetch_ok += 1
        self.budget.spend("max_pages")   # 只有真正读到正文才计入读页预算
        entry["ok"] = True
        return dict(page), ""

    # -- 候选池 -------------------------------------------------------------
    def _query_for(self, *parts: Any) -> str:
        """拼检索词：脱敏（strip_pii）+ 剥 site:/inurl: 等操作符 + 压长。"""
        raw = " ".join(str(part) for part in parts if part)
        query = strip_query_operators(strip_pii(raw))
        return _plain(query, 120)

    def _add_candidates(self, rows: Sequence[Mapping[str, Any]], sq_id: str) -> int:
        """把检索结果并入候选池（URL 去重 + 标题近似去重 + 上限）。"""
        added = 0
        cap = int(self.rules["max_candidates"])
        existing_titles = [{"title": cand["title"]} for cand in self.candidates.values()]
        for row in dedupe_sources(list(rows or [])):
            if len(self.candidates) >= cap:
                break
            url = str(row.get("url") or row.get("link") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                continue
            key = url_key(url)
            if not key or key in self.candidates:
                if key in self.candidates and sq_id:
                    if sq_id not in self.candidates[key]["sq_ids"]:
                        self.candidates[key]["sq_ids"].append(sq_id)
                continue
            title = _plain(row.get("title") or row.get("name"), 120)
            if title and near_duplicate_source({"title": title}, existing_titles,
                                               threshold=0.92):
                continue
            info = classify_source(url)
            self.candidates[key] = {
                "url": url,
                "title": title or info.name or info.domain,
                "snippet": _plain(row.get("snippet") or row.get("description"), 200),
                "domain": info.domain,
                "source": info,
                "level": _level_of_source(info),
                "rank": len(self.candidates),
                "sq_ids": [sq_id] if sq_id else [],
            }
            existing_titles.append({"title": self.candidates[key]["title"]})
            added += 1
        return added

    # -- 进度 / 终止 / 收口 --------------------------------------------------
    def _progress(self, stage: str, current: int, total: int) -> None:
        self.stage_no = max(self.stage_no, int(current))
        if self.on_progress is None:
            return
        try:
            self.on_progress(str(stage), int(current), int(total))
        except Exception:  # noqa: BLE001 进度回调失败不影响主流程（GUI/SSE 侧异常不能反咬）
            pass

    def _stop(self) -> bool:
        """是否该停止推进：取消、预算被挡、死线到点都算。"""
        if self.st.is_cancelled():
            return True
        if self.budget.blocked_reason:
            return True
        return self.budget.deadline_hit()

    def _fail(self, reason: str) -> None:
        """记一次“运行失败”（契约 §2 的 failed 出口），保留已取得的部分。"""
        self.fatal = True
        self.st.fail(reason)

    def _resolve_status(self, shell_status: str) -> str:
        """按契约 §2 收口六态；只覆盖 `CollectRunState.finish()` 表达不了的两种情况。"""
        if shell_status in ("cancelled", "budget_exhausted", "needs_clarification"):
            return shell_status
        if self.fatal:
            return "failed"
        if self.st.errors:
            return "partial"
        if not self.new_evidence_count:
            return "completed"      # 零结果 / 本次没有新增引文：合法空结果，只记 gaps
        return "partial" if self.st.gaps else "completed"

    def _finish(self) -> Dict[str, Any]:
        """统一外壳（契约 §2）+ 卡片字段（§5.1 的工具层补充）。"""
        self.card["evidence"] = list(self.evidence)
        self._refresh_coverage()
        self.st.data = self.card
        shell = self.st.finish()
        status = self._resolve_status(str(shell.get("status") or ""))
        finished_at = self.skills.clock()
        # 计数以预算实际消耗为准：CollectRunState.operation 只在创建时 ok=True 才累加，
        # 而这里的成功与否是调用返回后才知道的，直接用 used 才与真实 IO 次数一致。
        audit = {
            "operations": list((shell.get("audit") or {}).get("operations") or []),
            "searches": int(self.budget.used.get("max_searches", 0)),
            "pages": int(self.budget.used.get("max_pages", 0)),
            "llm_calls": int(self.budget.used.get("max_llm_calls", 0)),
        }
        gaps = list(shell.get("gaps") or [])
        if status == "cancelled":
            gaps.insert(0, "运行被取消，已保留此前取得的结果")
        if status == "budget_exhausted":
            gaps.insert(0, "预算或死线到顶提前停止，已保留此前取得的结果")
        stop_reason = self.agent_stop if self.mode == "agent" else status
        result = {
            "tool_id": "collect_research",
            "card_type": "research",
            "status": status,
            "task_status": str(shell.get("task_status") or "completed"),
            "data": self.card,
            "gaps": gaps,
            "errors": list(shell.get("errors") or []),
            "audit": audit,
            "budget": shell.get("budget") or self.budget.snapshot(),
            "report": "",
            "execution": {"mode": self.mode,
                          "steps": len(self.agent_steps) if self.mode == "agent"
                          else self.stage_no,
                          "stop_reason": stop_reason or status},
            "incremental": bool(self.incremental),
            "base_as_of": str(self.base_as_of or ""),
            "as_of": finished_at,
            "degraded": bool(self.degraded),
            "counts": {
                "sub_questions": len(self.card["sub_questions"]),
                "findings": len(self.card["key_findings"]),
                "leads": len(self.card["leads"]),
                "evidence": len(self.card["evidence"]),
                "searches": int(audit.get("searches") or 0),
                "pages": int(audit.get("pages") or 0),
                "llm_calls": int(audit.get("llm_calls") or 0),
            },
            "started_at": self.started_at,
            "finished_at": finished_at,
        }
        result["report"] = render_report(result)
        return result


class _AgentStateView:
    """给 `run_agent_loop` 的只读状态视图（它读 is_cancelled/deadline/budget）。"""

    def __init__(self, st: CollectRunState, budget: CollectBudget,
                 deadline: Optional[float]) -> None:
        self.st = st
        self.budget = budget
        self.deadline = deadline

    def is_cancelled(self) -> bool:
        return self.st.is_cancelled()


def result_steps(loop_result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """从循环结果里取 steps（坏形状按空列表处理，不影响收口）。"""
    steps = loop_result.get("steps")
    return [dict(step) for step in steps if isinstance(step, Mapping)] \
        if isinstance(steps, list) else []


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

class ResearchSkills:
    """资料收集技能（pipeline / agent 两种模式，共用同一套收口）。

    全部 IO 可注入：`llm_fn(system, user) -> (obj, err)`、
    `search_fn(query, k) -> {"results": [...], "degraded": bool, "error": str?}`、
    `fetch_fn(url, *, allowed_domains, cancel_check, deadline) -> transport.fetch 形状`。
    未注入 fetch_fn 时延迟 import `assistant.skills.transport` 的默认实现。
    """

    def __init__(self, *, rules: Optional[Any] = None,
                 search_fn: Optional[Callable[[str, int], Mapping[str, Any]]] = None,
                 fetch_fn: Optional[Callable[..., Mapping[str, Any]]] = None,
                 llm_fn: Optional[Callable[[str, str], Any]] = None,
                 transport: Optional[Any] = None,
                 clock: Optional[Callable[[], str]] = None,
                 preferences: Optional[Mapping[str, Any]] = None) -> None:
        if isinstance(rules, Mapping):
            self.rules = _clamp_rules(rules)
        elif rules is None:
            self.rules = load_research_rules()
        else:
            self.rules = load_research_rules(rules)
        self.search_fn = search_fn
        self.fetch_fn = fetch_fn
        self.llm_fn = llm_fn
        self.transport = transport
        self.clock = clock or utc_now_iso
        self.preferences = preferences if isinstance(preferences, Mapping) else {}
        self._fetch_probed = fetch_fn is not None

    # -- 模式 ---------------------------------------------------------------
    def _resolve_mode(self, mode: Optional[str]) -> str:
        """模式：显式值优先，否则取 preferences.research.workflow_mode，最后 pipeline。

        为什么默认不是写死 "pipeline"：设置页的模式选择必须真的生效（契约 §3/§5），
        所以 `mode` 的空值/None 表示“按偏好走”，显式传入才覆盖偏好。
        """
        value = str(mode or "").strip().lower()
        if value in ("pipeline", "agent"):
            return value
        research = self.preferences.get("research") if isinstance(self.preferences, Mapping) else None
        preferred = str((research or {}).get("workflow_mode") or "").strip().lower() \
            if isinstance(research, Mapping) else ""
        return preferred if preferred in ("pipeline", "agent") else "pipeline"

    def run(self, topic: str, *, mode: Optional[str] = None,
            prior: Optional[Dict[str, Any]] = None, followup: bool = False,
            cancel_check: Optional[Callable[[], bool]] = None,
            deadline: Optional[float] = None,
            on_progress: Optional[Callable[[str, int, int], None]] = None) -> Dict[str, Any]:
        """跑一次资料收集；返回契约 §2 的统一外壳（带 research 卡片字段）。"""
        raw = topic if isinstance(topic, str) else ("" if topic is None else str(topic))
        raw = normalize_text(raw)
        if deadline is None:
            # 未传死线时按 total_deadline_s 自建：预算上限不该因为调用方没传就失效
            deadline = time.monotonic() + float(self.rules["total_deadline_s"])
        run = _Run(self, topic=raw, mode=self._resolve_mode(mode), prior=prior,
                   followup=followup, cancel_check=cancel_check, deadline=deadline,
                   on_progress=on_progress)
        try:
            return run.execute()
        except Exception as exc:  # noqa: BLE001 兜底：编排层任何意外都折成 failed，绝不抛给对话
            run.st.error("pipeline", "internal_error", f"{type(exc).__name__}: {exc}")
            run._fail("内部错误，已中止本次收集")
            return run._finish()


def collect_research(topic: str, **kw: Any) -> Dict[str, Any]:
    """便捷函数 = ResearchSkills(**kw).run(topic)：工具层直接用它。"""
    return ResearchSkills(**kw).run(topic)


# ---------------------------------------------------------------------------
# 确定性纯文本报告（无 HTML / 无 markdown 链接语法 / 无判断词与分数）
# ---------------------------------------------------------------------------

def _support_label(level: Any) -> str:
    return {"authoritative": "官方或权威来源", "reliable": "可靠来源"}.get(
        str(level or ""), "来源等级未注明")


def render_report(result: Mapping[str, Any]) -> str:
    """把一次运行结果渲染成确定性纯文本报告（报告只整理资料，不下结论）。"""
    if not isinstance(result, Mapping):
        return "资料收集报告：（结果形状不符，无可渲染内容）"
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), Mapping) else {}
    budget = result.get("budget") if isinstance(result.get("budget"), Mapping) else {}
    limits = budget.get("limits") if isinstance(budget.get("limits"), Mapping) else {}
    execution = result.get("execution") if isinstance(result.get("execution"), Mapping) else {}

    lines: List[str] = ["资料收集报告（按主题整理公开资料）"]
    lines.append(f"执行状态：{_safe(result.get('status'), 40) or '（未知）'}"
                 f"　任务状态：{_safe(result.get('task_status'), 40) or '（未知）'}"
                 f"　模式：{_safe(execution.get('mode'), 20) or 'pipeline'}")
    lines.append(f"开始：{_safe(result.get('started_at'), 40)}"
                 f"　结束：{_safe(result.get('finished_at'), 40)}")
    if result.get("incremental"):
        lines.append(f"增量收集：基于上一张卡（时点 {_safe(result.get('base_as_of'), 40) or '未注明'}）")

    scope = data.get("scope") if isinstance(data.get("scope"), Mapping) else {}
    lines.append("")
    lines.append("一、主题与范围")
    lines.append(f"  主题：{_safe(data.get('topic'), 300) or '（空）'}")
    lines.append(f"  研究问题：{_safe(scope.get('question'), 200) or '（未界定）'}")
    lines.append(f"  时间范围：{_safe(scope.get('time_window'), 80) or '（未注明）'}"
                 f"　地区：{_safe(scope.get('region'), 80) or '（未注明）'}")
    basis = [_safe(item, 120) for item in (scope.get("basis") or [])]
    lines.append("  范围依据：" + ("；".join(basis) if basis else "（无）"))

    sub_questions = [row for row in (data.get("sub_questions") or []) if isinstance(row, Mapping)]
    lines.append("")
    lines.append(f"二、子问题与覆盖（共 {len(sub_questions)} 个）")
    if not sub_questions:
        lines.append("  （未拆出子问题）")
    label = {"covered": "已覆盖", "partial": "部分覆盖", "uncovered": "未覆盖"}
    for row in sub_questions:
        status = label.get(str(row.get("status")), str(row.get("status") or "未覆盖"))
        lines.append(f"  {_safe(row.get('sq_id'), 12)} [{status}] "
                     f"证据 {int(row.get('evidence_count') or 0)} 条 — "
                     f"{_safe(row.get('text'), 160)}")

    findings = [row for row in (data.get("key_findings") or []) if isinstance(row, Mapping)]
    lines.append("")
    lines.append(f"三、要点（共 {len(findings)} 条，每条都带可锚定引文）")
    if not findings:
        lines.append("  （本次没有形成可用要点）")
    for row in findings:
        ids = "、".join(_safe(item, 16) for item in (row.get("evidence_ids") or []))
        lines.append(f"  {_safe(row.get('finding_id'), 12)}［{_support_label(row.get('support_level'))}］"
                     f"（证据 {ids or '无'}）{_safe(row.get('statement'), 300)}")
        if row.get("source_quote"):
            lines.append(f"      引文：「{_safe(row.get('source_quote'), 300)}」")

    leads = [row for row in (data.get("leads") or []) if isinstance(row, Mapping)]
    lines.append("")
    lines.append(f"四、线索（共 {len(leads)} 条，未写入要点）")
    if not leads:
        lines.append("  （无）")
    for row in leads:
        ids = "、".join(_safe(item, 16) for item in (row.get("evidence_ids") or []))
        lines.append(f"  {_safe(row.get('lead_id'), 12)} {_safe(row.get('statement'), 300)}"
                     f"（{_safe(row.get('reason'), 160) or '暂无可用引文'}；证据 {ids or '无'}）")

    evidence = [row for row in (data.get("evidence") or []) if isinstance(row, Mapping)]
    lines.append("")
    lines.append(f"五、证据摘录（共 {len(evidence)} 条）")
    if not evidence:
        lines.append("  （没有可定位引文）")
    for row in evidence:
        lines.append(f"  {render_evidence_line(dict(row), with_quote=False)}")
        lines.append(f"      引文：「{_safe(row.get('quote'), 300)}」")

    timeline = [row for row in (data.get("timeline") or []) if isinstance(row, Mapping)]
    lines.append("")
    lines.append(f"六、时间线（共 {len(timeline)} 条）")
    if not timeline:
        lines.append("  （无）")
    for row in timeline:
        ids = "、".join(_safe(item, 16) for item in (row.get("evidence_ids") or []))
        lines.append(f"  {_safe(row.get('date'), 40)} {_safe(row.get('event'), 200)}"
                     f"（证据 {ids}）")

    conflicts = [row for row in (data.get("conflicts") or []) if isinstance(row, Mapping)]
    lines.append("")
    lines.append(f"七、资料之间不一致之处（共 {len(conflicts)} 条）")
    if not conflicts:
        lines.append("  （无）")
    for row in conflicts:
        ids = "、".join(_safe(item, 16) for item in (row.get("evidence_ids") or []))
        lines.append(f"  {_safe(row.get('summary'), 240)}（证据 {ids}）")

    analysis = data.get("analysis") if isinstance(data.get("analysis"), Mapping) else {}
    lines.append("")
    lines.append("八、整理与推断")
    if analysis.get("text"):
        ids = "、".join(_safe(item, 16) for item in (analysis.get("evidence_ids") or []))
        lines.append(f"  {_safe(analysis.get('text'), 600)}（证据 {ids}；"
                     f"{_safe(analysis.get('label'), 40)}）")
    else:
        lines.append("  （无）")

    followups = [_safe(item, 160) for item in (data.get("followups") or [])]
    lines.append("")
    lines.append(f"九、可以继续查的方向（共 {len(followups)} 条）")
    if not followups:
        lines.append("  （无）")
    for text in followups:
        lines.append(f"  - {text}")

    gaps = [_safe(item, 240) for item in (result.get("gaps") or [])]
    lines.append("")
    lines.append(f"十、缺口（共 {len(gaps)} 条）")
    if not gaps:
        lines.append("  （无）")
    for index, text in enumerate(gaps, start=1):
        lines.append(f"  {index}. {text}")

    errors = [row for row in (result.get("errors") or []) if isinstance(row, Mapping)]
    lines.append("")
    lines.append(f"十一、错误（共 {len(errors)} 条）")
    if not errors:
        lines.append("  （无）")
    for row in errors:
        lines.append(f"  - [{_safe(row.get('stage'), 20)}] {_safe(row.get('category'), 40)}："
                     f"{_safe(row.get('detail'), 200)}")

    lines.append("")
    lines.append("十二、预算与执行")
    lines.append("  " + render_counts_line(audit))
    lines.append(f"  搜索 {int(audit.get('searches') or 0)}/{limits.get('max_searches', '-')}"
                 f"；读页 {int(audit.get('pages') or 0)}/{limits.get('max_pages', '-')}"
                 f"；模型调用 {int(audit.get('llm_calls') or 0)}/{limits.get('max_llm_calls', '-')}")
    lines.append(f"  执行步数：{int(execution.get('steps') or 0)}"
                 f"　停止原因：{_safe(execution.get('stop_reason'), 40) or '（未知）'}")
    if result.get("degraded"):
        lines.append("  本次有检索或取页降级，覆盖可能不完整。")
    return "\n".join(lines)
