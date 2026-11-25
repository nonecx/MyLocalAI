# -*- coding: utf-8 -*-
"""巡检报告技能核心（platform/report/pipeline.py）：一轮巡检 → 结构化报告 payload。

三段式（契约冻结）：
  1. `_aggregate()`：纯代码汇总，零 LLM——counts / topic_stats / highlights /
     changes / gaps 全部由代码计算（项目铁律：大模型不参与打分/分类/选站/计数）；
  2. `_narrate()`：LLM 一次调用（预算 rules["max_llm_calls"]，默认 1），只做
     语言组织与逻辑判断；system = SKILL.md 原文 + 机器契约；
  3. 代码校验器（纯函数，与 assistant/skills/research/pipeline.py 同口径**内嵌**
     实现——平台包不 import assistant）：安全闸（真假判定词/过度断言词/URL）+
     截断 + 白名单（query 集合 / entry_id 集合），不通过即丢弃并记 error。

降级文化（每个出口都有降级，build 永不抛）：
  - 前置门：report.enabled 假 → disabled；entries 空 → skipped_empty；
    取消 → cancelled（都不调 LLM）；
  - LLM 失败：err 含 budget → budget_exhausted；其他 err / 返回非 dict /
    缺全部关键键 → unavailable；调用抛异常 → error；一律记 error + gaps 原因；
  - title/summary 由代码兜底生成（确定性概述），counts 等代码汇总不受影响。

llm_status 封闭集与 platform/store.py 的 REPORT_LLM_STATUSES 逐字一致
（store.save_report 按同一集合校验，越界即 ValueError——两处漂移即写库失败）。

依赖方向：只 import 标准库；默认 llm_fn 惰性 import mylocalai.llm.llm_client
（import 失败/调用异常都不抛，返回 (None, err)，写法参照 platform/domains.py）。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["PatrolReport", "REPORT_LLM_STATUSES", "DEFAULT_REPORT_RULES"]

#: llm_status 封闭集（契约冻结；与 platform/store.py REPORT_LLM_STATUSES 同值）
REPORT_LLM_STATUSES = ("ok", "disabled", "unavailable", "budget_exhausted",
                       "cancelled", "skipped_empty", "error")

#: report 段内置默认（与 platform/rules.py DEFAULT_RULES["report"] 逐字同值；
#: 文件整体缺失/段被摘掉时用它，改口径改 rules.py 与 platform_rules.json）
DEFAULT_REPORT_RULES: Dict[str, Any] = {
    "enabled": True,
    "max_llm_calls": 1,
    "max_entries": 20,
    "max_chars_per_entry": 300,
    "deadline_s": 60,
    "max_tokens": 1200,
}

#: 风险档位（升序；聚合排序口径 urgent>high>suspect>notice）
_RISK_LEVELS = ("notice", "suspect", "high", "urgent")

#: LLM 输出的关键键（缺全部 → unavailable；缺个别由代码兜底）
_LLM_KEYS = ("title", "summary", "topic_notes", "analysis", "risks", "followups")

#: 真假判定词（安全闸；与 assistant/skills/research/pipeline.py 的 _VERDICT_WORDS 同口径）
_TRUTH_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")
#: 过度断言词（安全闸；"下断论"语气一律挡在报告之外）
_OVERCLAIM_WORDS = ("证明", "足以说明", "无疑是", "铁证", "实锤", "盖棺定论")
_TRUTH_VERDICT_RE = re.compile("|".join(re.escape(w) for w in _TRUTH_VERDICT_WORDS))
_OVERCLAIM_RE = re.compile("|".join(re.escape(w) for w in _OVERCLAIM_WORDS))
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）】]+", re.I)

#: LLM 自由文本截断上限（字符；超长即截，不为塞内容而破上限）
_CAPS = {"title": 80, "summary": 600, "note": 300, "point": 400,
         "risks": 600, "followup": 120}

#: 代码侧条目标题的截断（标题来自外部检索结果，同样要有界）
_TITLE_CHARS = 80
#: top_titles：风险档最高、E 最高的前几条、每条多少字
_TOP_TITLES = 3
_TOP_TITLE_CHARS = 40
#: highlights 条数上限
_HIGHLIGHTS = 5
#: followups 条数上限
_FOLLOWUPS = 5

_SKILL_PATH = Path(__file__).resolve().parent / "SKILL.md"
_SKILL_FALLBACK = ("你是监测平台的巡检报告技能：只依据传入的清单做语言组织与逻辑判断，"
                   "不判真假、不自造数值与链接，没有就如实写缺口。")

#: 机器契约（pipeline 拼在 SKILL.md 之后进 system；JSON 键名锁定）
_MACHINE_CONTRACT = (
    '只输出 JSON：{"title":..,"summary":..,"topic_notes":[{"query","note"}],'
    '"analysis":[{"point","entry_ids"}],"risks":..,"followups":[..]}；'
    "entry_ids/query 只能来自传入清单；不得出现真假判定词与 URL；followups ≤5。"
)


# ---------------------------------------------------------------------------
# 模块级纯助手（规整 / 排序 / 兜底；全部永不抛）
# ---------------------------------------------------------------------------

def _default_clock() -> str:
    """缺省时钟：本地时间 ISO 秒级（带偏移；与 store._now 同时格式）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """宽松整数化：int 原样；整值浮点/整数字符串转 int；其他 → default。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else default
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _as_float(value: Any) -> Optional[float]:
    """有限实数或 None；布尔/NaN/inf/不可解析 → None（无热度不猜）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str):
        try:
            out = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _e_or_zero(value: Any) -> float:
    """E 分或 0.0（排序键用：None 当 0）。"""
    num = _as_float(value)
    return 0.0 if num is None else num


def _qkey(value: Any) -> str:
    """query_id 分组键（统一成字符串，容忍 int/str 混用）。"""
    return str(value if value is not None else "")


def _query_label(query: Any) -> str:
    """监控词展示名；缺 query 文本时用占位（不静默丢弃整行统计）。"""
    text = str(query.get("query") or "").strip() if isinstance(query, dict) else ""
    return text or "（未命名监控词）"


def _risk_labels() -> Dict[str, str]:
    """档位中文名（与 platform/risk.py LEVEL_LABELS 同口径；import 失败用内嵌副本）。"""
    try:
        from mylocalai.platform.risk import LEVEL_LABELS
        return dict(LEVEL_LABELS)
    except Exception:  # noqa: BLE001  risk 模块不可用时保底（值逐字一致）
        return {"notice": "关注", "suspect": "疑似", "high": "高危", "urgent": "紧急"}


def _risk_level_or_notice(level: Any) -> str:
    """档位规整：合法档位原样；缺失/越界 → notice（聚合侧保守取最低档）。"""
    text = str(level or "").strip()
    return text if text in _RISK_LEVELS else "notice"


def _growth_percent(e: Optional[float], e_prev: Optional[float]) -> float:
    """环比增幅（百分比）：(e-e_prev)/e_prev*100；e 或 e_prev 为 None/0 → 0.0（不猜）。"""
    if e is None or e_prev is None or e_prev == 0:
        return 0.0
    return round((e - e_prev) / e_prev * 100.0, 2)


def _cancelled(cancel_check: Optional[Callable[[], bool]]) -> bool:
    """取消探测：未传/探测失败 → 未取消（探测失败不误杀报告生成）。"""
    try:
        return bool(cancel_check()) if cancel_check is not None else False
    except Exception:  # noqa: BLE001
        return False


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    """整数夹取到 [lo, hi]；布尔/非整数 → 默认。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, lo), hi)


def _clamp_num(value: Any, default: Any, lo: float, hi: float) -> Any:
    """数值夹取到 [lo, hi]（保留 int/float 类型）；布尔/非数值 → 默认。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _coerce_report_rules(raw: Any) -> Dict[str, Any]:
    """report 段规整（与 platform/rules.py _normalize_report 同口径夹取）。

    缺键退默认、坏类型退默认、越界夹取；**永不抛**（构造器不能炸调用方）。
    调用方传进来的 rules 通常是 load_rules() 规整过的 report 段，这里再夹一层
    只是保险：手传裸 dict 也烧不爆预算。
    """
    out: Dict[str, Any] = dict(DEFAULT_REPORT_RULES)
    if not isinstance(raw, dict):
        return out
    enabled = raw.get("enabled", out["enabled"])
    out["enabled"] = enabled if isinstance(enabled, bool) else DEFAULT_REPORT_RULES["enabled"]
    out["max_llm_calls"] = _clamp_int(raw.get("max_llm_calls"), out["max_llm_calls"], 1, 5)
    out["max_entries"] = _clamp_int(raw.get("max_entries"), out["max_entries"], 1, 50)
    out["max_chars_per_entry"] = _clamp_int(raw.get("max_chars_per_entry"),
                                            out["max_chars_per_entry"], 50, 2000)
    out["deadline_s"] = _clamp_num(raw.get("deadline_s"), out["deadline_s"], 10, 600)
    out["max_tokens"] = _clamp_int(raw.get("max_tokens"), out["max_tokens"], 200, 4000)
    return out


def _fallback_summary(counts: Dict[str, Any]) -> str:
    """代码兜底摘要（确定性概述：几个话题、几条发现、几条深查、高危+紧急几条）。"""
    risk = counts.get("risk") or {}
    return (f"本轮巡检覆盖 {counts.get('queries', 0)} 个监控话题，"
            f"发现 {counts.get('candidates', 0)} 条候选信息，"
            f"完成深查 {counts.get('verified', 0)} 条；"
            f"按传播风险分级，高危 {risk.get('high', 0)} 条、紧急 {risk.get('urgent', 0)} 条"
            f"（关注 {risk.get('notice', 0)} 条、疑似 {risk.get('suspect', 0)} 条）。")


def _rank_key(entry: Dict[str, Any]) -> Tuple[int, float]:
    """重点度排序键：风险档位降序 → E 降序（None 当 0）。

    sorted 稳定，同键条目保持传入顺序（runner 的 entries 顺序即发现顺序）。
    """
    return (-_RISK_LEVELS.index(_risk_level_or_notice(entry.get("risk_level"))),
            -_e_or_zero(entry.get("e_score")))


def _entry_snippet(entry: Dict[str, Any]) -> str:
    """条目摘要素材：优先 entry 自带 snippet；没有则用 risk_reasons（代码推导的
    可解释理由，正好是 LLM 做语言组织的原料）；再没有返回空串（摘要段省略）。"""
    snippet = str(entry.get("snippet") or "").strip()
    if snippet:
        return snippet
    reasons = entry.get("risk_reasons")
    if isinstance(reasons, list):
        return "；".join(str(r).strip() for r in reasons if str(r or "").strip())
    return ""


# ---------------------------------------------------------------------------
# 输入规整（坏输入 → 合法空结果 + 如实记 error；全部永不抛）
# ---------------------------------------------------------------------------

def _coerce_scan_id(value: Any, errors: List[Dict[str, str]]) -> int:
    """scan_id 规整：整数（含整数字符串/整值浮点）原样；其他 → 0 + 记 error。"""
    if isinstance(value, bool):
        errors.append({"stage": "report", "category": "bad_scan_id",
                       "detail": f"scan_id 是布尔值（{value!r}），按 0 处理"})
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    errors.append({"stage": "report", "category": "bad_scan_id",
                   "detail": f"scan_id 不是整数（{value!r}），按 0 处理"})
    return 0


def _sanitize_entries(raw: Any, errors: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """entries 规整：非列表 → 空（记 error）；非对象元素跳过（记 error）。"""
    if not isinstance(raw, list):
        errors.append({"stage": "report", "category": "bad_entries",
                       "detail": f"entries 不是列表（{type(raw).__name__}），按空列表处理"})
        return []
    out: List[Dict[str, Any]] = []
    bad = 0
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
        else:
            bad += 1
    if bad:
        errors.append({"stage": "report", "category": "bad_entry_item",
                       "detail": f"entries 有 {bad} 个非对象元素，已跳过"})
    return out


def _sanitize_queries(raw: Any, errors: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """queries 规整：None → 空；非列表/非对象元素 → 跳过（记 error）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append({"stage": "report", "category": "bad_queries",
                       "detail": f"queries 不是列表（{type(raw).__name__}），按空列表处理"})
        return []
    out = [q for q in raw if isinstance(q, dict)]
    bad = len(raw) - len(out)
    if bad:
        errors.append({"stage": "report", "category": "bad_query_item",
                       "detail": f"queries 有 {bad} 个非对象元素，已跳过"})
    return out


def _sanitize_counters(raw: Any, errors: List[Dict[str, str]]) -> Dict[str, Any]:
    """counters 规整：None → {}；非对象 → {}（记 error，按零值处理）。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append({"stage": "report", "category": "bad_counters",
                       "detail": f"counters 不是对象（{type(raw).__name__}），按零值处理"})
        return {}
    return raw


def _sanitize_prev_scan(raw: Any, errors: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """prev_scan 规整：None/dict 原样；其他类型 → None（记 error，不算环比）。"""
    if raw is None or isinstance(raw, dict):
        return raw
    errors.append({"stage": "report", "category": "bad_prev_scan",
                   "detail": f"prev_scan 不是对象（{type(raw).__name__}），不计算环比"})
    return None


def _sanitize_warnings(raw: Any, errors: List[Dict[str, str]]) -> List[str]:
    """warnings 规整：非列表 → 忽略（记 error）；元素统一成非空字符串。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append({"stage": "report", "category": "bad_warnings",
                       "detail": f"warnings 不是列表（{type(raw).__name__}），已忽略"})
        return []
    return [text for text in (str(w or "").strip() for w in raw) if text]


# ---------------------------------------------------------------------------
# 代码校验器（纯函数；与 assistant/skills/research/pipeline.py 同口径内嵌实现）
# ---------------------------------------------------------------------------

def _truth_verdict_unsafe(text: Any) -> bool:
    """真假判定词闸：命中 ("是真的","是假的","属实","谣言为真","基本属实","不实")
    任一 → True（报告只述证据与传播风险，不判真假）。"""
    return bool(_TRUTH_VERDICT_RE.search(str(text or "")))


def _overclaim_unsafe(text: Any) -> bool:
    """过度断言词闸：命中 ("证明","足以说明","无疑是","铁证","实锤","盖棺定论")
    任一 → True（断论语气不给报告留出口）。"""
    return bool(_OVERCLAIM_RE.search(str(text or "")))


def _has_url(text: Any) -> bool:
    """URL 闸：模型自由文本里出现链接一律按"新增证据"处理（只许引用传入清单）。"""
    return bool(_URL_RE.search(str(text or "")))


def _guard_text(value: Any, kind: str, errors: List[Dict[str, str]]) -> Optional[str]:
    """安全闸 + 截断（LLM 自由文本进输出结构的唯一出口）。

    命中真假判定词/过度断言词/URL → 整条丢弃并记 error；空值 → None（缺字段由
    代码兜底，不算模型犯错，不记 error）；超长 → 截到 _CAPS[kind]。
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if _truth_verdict_unsafe(text) or _overclaim_unsafe(text) or _has_url(text):
        errors.append({"stage": "report", "category": "unsafe_text",
                       "detail": f"{kind} 含真假判定词/过度断言/URL，已丢弃：{text[:60]}"})
        return None
    return text[:_CAPS[kind]]


def _as_rows(raw: Any, field: str, errors: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """LLM 数组字段 → dict 行列表；None 按空（缺字段不记 error），坏类型记 error。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append({"stage": "report", "category": "bad_llm_field",
                       "detail": f"{field} 不是数组，已按空处理"})
        return []
    return [row for row in raw if isinstance(row, dict)]


def _filter_entry_ids(raw: Any, whitelist: set,
                      errors: List[Dict[str, str]]) -> List[int]:
    """entry_ids 白名单收敛：只保留本轮 entries 的真实落库 id；未知 id 记 error
    并移除（未落库条目在 user 文本里只占序号、不进白名单，引用即被移除）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append({"stage": "report", "category": "bad_llm_field",
                       "detail": "analysis.entry_ids 不是数组，已按空处理"})
        return []
    out: List[int] = []
    for item in raw:
        entry_id = _as_int(item, None)
        if entry_id is None or entry_id not in whitelist:
            errors.append({"stage": "report", "category": "unknown_entry_id",
                           "detail": "analysis 引用了不在本轮清单内的条目 id，"
                                     f"已移除：{item!r}"})
            continue
        if entry_id not in out:
            out.append(entry_id)
    return out


def _unpack_llm(result: Any) -> Tuple[Any, str]:
    """llm_fn 返回值 → (obj, err)。契约是 (obj, err) 二元组；容忍裸 dict；
    其他形状 → (None, 形状说明)。"""
    if isinstance(result, tuple) and len(result) == 2:
        obj, err = result
        return obj, str(err or "")
    if isinstance(result, dict):
        return result, ""
    return None, f"llm_bad_shape:{type(result).__name__}"


# ---------------------------------------------------------------------------
# 聚合纯函数（零 LLM；铁律：计数/分级/排序全部代码算）
# ---------------------------------------------------------------------------

def _counts(entries: List[Dict[str, Any]], queries: List[Dict[str, Any]],
            counters: Dict[str, Any]) -> Dict[str, Any]:
    """计数汇总：runner 记账的五个标量 + risk/verdicts/domains 三个分布。

    risk 按档位计数（缺失/越界档位按 notice——保守取最低档，不夸大）；
    verdicts/domains 只计非空值（未深查/未判定的条目不计入分布，由
    candidates-verified 的差与 counts 本身体现）。
    """
    risk = {level: 0 for level in _RISK_LEVELS}
    verdicts: Dict[str, int] = {}
    domains: Dict[str, int] = {}
    for entry in entries:
        risk[_risk_level_or_notice(entry.get("risk_level"))] += 1
        label = str(entry.get("verdict_label") or "").strip()
        if label:
            verdicts[label] = verdicts.get(label, 0) + 1
        domain = str(entry.get("topic_domain") or "").strip()
        if domain:
            domains[domain] = domains.get(domain, 0) + 1
    return {
        "queries": _as_int(counters.get("queries"), len(queries)),
        "candidates": _as_int(counters.get("candidates"), 0),
        "verified": _as_int(counters.get("verified"), 0),
        "deduped": _as_int(counters.get("deduped"), 0),
        "degraded": _as_int(counters.get("degraded"), 0),
        "risk": risk,
        "verdicts": verdicts,
        "domains": domains,
    }


def _group_growth(group: List[Dict[str, Any]]) -> float:
    """监控词环比：queries 没带 growth 时，用该词下条目的 e/e_prev 现算。"""
    for entry in group:
        e = _as_float(entry.get("e_score"))
        if e is None:
            continue
        return _growth_percent(e, _as_float(entry.get("e_prev")))
    return 0.0


def _max_risk_level(group: List[Dict[str, Any]]) -> str:
    """该监控词下条目的最高风险档；无条目 → notice（最低档，不夸大）。"""
    best = "notice"
    for entry in group:
        level = _risk_level_or_notice(entry.get("risk_level"))
        if _RISK_LEVELS.index(level) > _RISK_LEVELS.index(best):
            best = level
    return best


def _top_titles(group: List[Dict[str, Any]]) -> List[str]:
    """代表标题：风险档位最高、E 最高的前 3 条（各 ≤40 字；无标题条目跳过）。"""
    out: List[str] = []
    for entry in sorted(group, key=_rank_key):
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        out.append(title[:_TOP_TITLE_CHARS])
        if len(out) >= _TOP_TITLES:
            break
    return out


def _topic_stats(entries: List[Dict[str, Any]], queries: List[Dict[str, Any]],
                 growth_by_qid: Dict[str, float]) -> List[Dict[str, Any]]:
    """分监控词统计（按 queries 顺序）：E/环比/候选数/最高风险档/代表标题。

    growth：queries 里有就用；否则用该词下条目的 e/e_prev 现算（_group_growth）。
    growth_by_qid 是给 _narrate 拼条目行用的内部数据（query_id → growth）。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(_qkey(entry.get("query_id")), []).append(entry)
    out: List[Dict[str, Any]] = []
    for query in queries:
        qid = _qkey(query.get("id"))
        group = grouped.get(qid, [])
        growth = _as_float(query.get("growth"))
        if growth is None:
            growth = _group_growth(group)
        growth_by_qid[qid] = growth
        e = _as_float(query.get("e"))
        out.append({
            "query": _query_label(query),
            "e": None if e is None else round(e, 2),
            "growth": growth,
            "candidates": _as_int(query.get("candidates"), len(group)),
            "max_risk_level": _max_risk_level(group),
            "top_titles": _top_titles(group),
        })
    return out


def _highlights(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """重点条目：全部 entries 按（风险档降序, E 降序）取前 5。"""
    out: List[Dict[str, Any]] = []
    for entry in sorted(entries, key=_rank_key)[:_HIGHLIGHTS]:
        out.append({
            "entry_id": _as_int(entry.get("id"), None),
            "title": str(entry.get("title") or "").strip()[:_TITLE_CHARS],
            "risk_level": _risk_level_or_notice(entry.get("risk_level")),
            "e_score": _as_float(entry.get("e_score")),
            "verdict_label": str(entry.get("verdict_label") or "").strip(),
            "topic_domain": str(entry.get("topic_domain") or "").strip(),
        })
    return out


def _changes(counters: Dict[str, Any],
             prev_scan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """与上一轮的计数差：prev_scan 不是 dict（首轮/无基准）→ {}（不假装有对比）。"""
    if not isinstance(prev_scan, dict):
        return {}
    return {
        "prev_scan_id": _as_int(prev_scan.get("id", prev_scan.get("scan_id")), None),
        "candidates_delta": _as_int(counters.get("candidates"), 0)
        - _as_int(prev_scan.get("candidates"), 0),
        "verified_delta": _as_int(counters.get("verified"), 0)
        - _as_int(prev_scan.get("verified"), 0),
        "degraded_delta": _as_int(counters.get("degraded"), 0)
        - _as_int(prev_scan.get("degraded"), 0),
    }


def _aggregate_gaps(counts: Dict[str, Any]) -> List[str]:
    """代码侧确定性缺口：降级计数、未深查计数（只述事实，不猜原因）。

    "候选远大于已深查且无降级"时只写确定性计数对比（典型是深查预算用尽），
    不替系统猜一个原因。
    """
    gaps: List[str] = []
    if counts["degraded"] > 0:
        gaps.append(f"本轮有 {counts['degraded']} 次降级")
    if counts["degraded"] == 0 and counts["candidates"] > counts["verified"]:
        gaps.append(f"本轮 {counts['candidates']} 条候选中仅 {counts['verified']} 条完成深查，"
                    f"其余 {counts['candidates'] - counts['verified']} 条未进入深查")
    return gaps


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class PatrolReport:
    """巡检报告技能（契约冻结）：一轮巡检 → 结构化报告 payload。

    参数：
      rules   report 段 dict（**不是**整份平台规则）；缺省用内置默认
              （DEFAULT_REPORT_RULES，与 platform/rules.py 同值），越界值夹取。
      llm_fn  (system, user) -> (obj, err)；缺省 _default_llm_fn（chat_json 适配，
              temperature=0、max_tokens=rules["max_tokens"]、
              namespace="platform_report"）；import 失败/调用异常都不抛。
      clock   () -> ISO 本地时间串；缺省 datetime.now().astimezone()
              .isoformat(timespec="seconds")。

    build() 永不抛：任何失败都落成 llm_status / errors / gaps，绝不为有输出而编。
    """

    def __init__(self, *, rules: Optional[Dict[str, Any]] = None,
                 llm_fn: Optional[Callable[[str, str], Tuple[Any, Any]]] = None,
                 clock: Optional[Callable[[], str]] = None) -> None:
        self.rules = _coerce_report_rules(rules)
        self.llm_fn = llm_fn or self._default_llm_fn
        self.clock = clock or _default_clock

    # ---- 入口 ----

    def build(self, *, scan_id: Any, scan_status: Any, entries: Any, queries: Any,
              counters: Any, warnings: Any = None, prev_scan: Any = None,
              cancel_check: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        """生成一轮巡检报告 payload。**永不抛异常**（契约冻结）。

        入参形状（runner 现状，字段可能缺）：
          entries  [{"query_id","title","url","domain","topic_domain",...,"id"
                    (落库后有，未落库为缺)}]
          queries  [{"id","query","e","candidates","domain",...,"growth"(可能缺)}]
          counters {"queries","candidates","verified","deduped","degraded"}
          prev_scan 上一轮 scan_runs 行（dict，含 candidates/verified/degraded）
          warnings  runner 的告警列表，原样并入 gaps（前缀「巡检告警：」）——
                    payload 键名冻结、没有 warnings 键，告警进缺口区如实呈现。
        """
        try:
            return self._build(scan_id=scan_id, scan_status=scan_status, entries=entries,
                               queries=queries, counters=counters, warnings=warnings,
                               prev_scan=prev_scan, cancel_check=cancel_check)
        except Exception as exc:  # noqa: BLE001  最后的保险：任何意外都不炸调用方
            return self._emergency_payload(scan_id, scan_status, exc)

    def _build(self, *, scan_id: Any, scan_status: Any, entries: Any, queries: Any,
               counters: Any, warnings: Any, prev_scan: Any,
               cancel_check: Optional[Callable[[], bool]]) -> Dict[str, Any]:
        """build 主体（异常由 build 的兜底出口接管）。"""
        errors: List[Dict[str, str]] = []
        gaps: List[str] = []
        as_of = self._now()
        scan_id_v = _coerce_scan_id(scan_id, errors)
        scan_status_v = str(scan_status or "").strip()
        entries_s = _sanitize_entries(entries, errors)
        queries_s = _sanitize_queries(queries, errors)
        counters_s = _sanitize_counters(counters, errors)
        prev_s = _sanitize_prev_scan(prev_scan, errors)
        warn_list = _sanitize_warnings(warnings, errors)

        agg = self._aggregate(entries_s, queries_s, counters_s, prev_s)
        counts = agg["counts"]
        gaps.extend(agg["gaps"])
        for text in warn_list:
            gaps.append(f"巡检告警：{text}")

        llm_status, fields = self._narrate(
            entries=entries_s, queries=queries_s, counts=counts,
            topic_stats=agg["topic_stats"], growth_by_qid=agg["growth_by_qid"],
            cancel_check=cancel_check, errors=errors, gaps=gaps)

        return {
            "scan_id": scan_id_v,
            "as_of": as_of,
            "scan_status": scan_status_v,
            "llm_status": llm_status,
            "title": fields.get("title") or f"第 {scan_id_v} 轮巡检报告",
            "summary": fields.get("summary") or _fallback_summary(counts),
            "counts": counts,
            "topic_stats": agg["topic_stats"],
            "highlights": agg["highlights"],
            "changes": agg["changes"],
            "topic_notes": fields.get("topic_notes", []),
            "analysis": fields.get("analysis", []),
            "risks": fields.get("risks", "") or "",
            "followups": fields.get("followups", []),
            "gaps": gaps,
            "errors": errors,
        }

    def _emergency_payload(self, scan_id: Any, scan_status: Any,
                           exc: BaseException) -> Dict[str, Any]:
        """意外异常的兜底出口：合法空结果 + 如实标注（绝不为有输出而编）。"""
        scan_id_v = _coerce_scan_id(scan_id, [])
        counts = {"queries": 0, "candidates": 0, "verified": 0, "deduped": 0,
                  "degraded": 0, "risk": {lv: 0 for lv in _RISK_LEVELS},
                  "verdicts": {}, "domains": {}}
        return {
            "scan_id": scan_id_v,
            "as_of": self._now(),
            "scan_status": str(scan_status or "").strip(),
            "llm_status": "error",
            "title": f"第 {scan_id_v} 轮巡检报告",
            "summary": _fallback_summary(counts),
            "counts": counts,
            "topic_stats": [],
            "highlights": [],
            "changes": {},
            "topic_notes": [],
            "analysis": [],
            "risks": "",
            "followups": [],
            "gaps": [f"报告生成遇到未预期异常（{type(exc).__name__}），仅输出代码汇总空壳"],
            "errors": [{"stage": "report", "category": "unexpected_exception",
                        "detail": f"{type(exc).__name__}: {exc}"}],
        }

    # ---- (1) 聚合：纯代码，零 LLM ----

    def _aggregate(self, entries: List[Dict[str, Any]], queries: List[Dict[str, Any]],
                   counters: Dict[str, Any],
                   prev_scan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """纯代码汇总（零 LLM）。返回 counts/topic_stats/highlights/changes/gaps/
        growth_by_qid（末键是内部数据，供 _narrate 拼条目行，不进 payload）。"""
        growth_by_qid: Dict[str, float] = {}
        topic_stats = _topic_stats(entries, queries, growth_by_qid)
        counts = _counts(entries, queries, counters)
        return {
            "counts": counts,
            "topic_stats": topic_stats,
            "highlights": _highlights(entries),
            "changes": _changes(counters, prev_scan),
            "gaps": _aggregate_gaps(counts),
            "growth_by_qid": growth_by_qid,
        }

    # ---- (2) 叙述：LLM 一次调用 ----

    def _narrate(self, *, entries: List[Dict[str, Any]], queries: List[Dict[str, Any]],
                 counts: Dict[str, Any], topic_stats: List[Dict[str, Any]],
                 growth_by_qid: Dict[str, float],
                 cancel_check: Optional[Callable[[], bool]],
                 errors: List[Dict[str, str]],
                 gaps: List[str]) -> Tuple[str, Dict[str, Any]]:
        """LLM 叙述（一次调用，预算 rules["max_llm_calls"]）。

        返回 (llm_status, 清洗后的字段 dict)；任何不调用/调用失败的出口都记
        error + gaps 原因，title/summary 由调用方走代码兜底。
        """
        if not self.rules["enabled"]:
            gaps.append("本轮未生成叙述性报告：报告功能未启用（report.enabled=false）")
            return "disabled", {}
        if not entries:
            gaps.append("本轮没有候选条目，未生成叙述性报告")
            return "skipped_empty", {}
        if _cancelled(cancel_check):
            gaps.append("本轮未生成叙述性报告：生成前已取消")
            return "cancelled", {}
        if int(self.rules["max_llm_calls"]) < 1:
            # 夹取保证 ≥1，这里是双保险：预算为零等价于"不调用"
            errors.append({"stage": "report", "category": "llm_budget",
                           "detail": "模型调用预算不足，未发起调用"})
            gaps.append("本轮未生成叙述性报告：模型调用预算已用尽")
            return "budget_exhausted", {}

        system = self._system_prompt()
        user = self._user_text(entries, queries, counts, topic_stats, growth_by_qid)
        try:
            result = self.llm_fn(system, user)
        except Exception as exc:  # noqa: BLE001  模型侧异常按 error 记，不外抛
            errors.append({"stage": "report", "category": "llm_exception",
                           "detail": f"模型调用抛异常：{type(exc).__name__}: {exc}"})
            gaps.append("本轮未生成叙述性报告：模型调用异常")
            return "error", {}

        obj, err = _unpack_llm(result)
        if err:
            if "budget" in err.lower():
                errors.append({"stage": "report", "category": "llm_budget",
                               "detail": f"模型返回预算耗尽：{err}"})
                gaps.append("本轮未生成叙述性报告：模型调用预算已用尽")
                return "budget_exhausted", {}
            errors.append({"stage": "report", "category": "llm_unavailable",
                           "detail": f"模型不可用：{err}"})
            gaps.append("本轮未生成叙述性报告：模型不可用")
            return "unavailable", {}
        if not isinstance(obj, dict):
            errors.append({"stage": "report", "category": "llm_bad_shape",
                           "detail": f"模型返回不是 JSON 对象（{type(obj).__name__}）"})
            gaps.append("本轮未生成叙述性报告：模型返回形状不符")
            return "unavailable", {}
        if not any(key in obj for key in _LLM_KEYS):
            errors.append({"stage": "report", "category": "llm_no_keys",
                           "detail": "模型返回缺少全部关键键（title/summary/"
                                     "topic_notes/analysis/risks/followups）"})
            gaps.append("本轮未生成叙述性报告：模型返回缺少关键字段")
            return "unavailable", {}
        return "ok", self._validate_llm_fields(obj, entries=entries, queries=queries,
                                               errors=errors)

    def _default_llm_fn(self, system: str,
                        user: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """缺省 llm_fn（写法参照 platform/domains.py）：chat_json 适配成 (obj, err)。

        temperature=0、max_tokens=rules["max_tokens"]、namespace="platform_report"；
        import 失败（未装依赖/模块被摘）/调用异常都不抛，返回 (None, err)。
        """
        try:
            from mylocalai.llm.llm_client import chat_json
            obj, err = chat_json(system, user, temperature=0,
                                 max_tokens=int(self.rules["max_tokens"]),
                                 namespace="platform_report")
        except Exception as exc:  # noqa: BLE001  import 失败/调用异常都不抛
            return None, str(exc)
        if obj is None:
            return None, (err or "模型未返回可用 JSON")
        return obj, (err or "")

    @staticmethod
    def _system_prompt() -> str:
        """system = SKILL.md 原文 + 机器契约（文件缺失用精简兜底，不让运行崩）。"""
        try:
            text = _SKILL_PATH.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001  指令文件缺失 = 指令变短，不是运行失败
            text = _SKILL_FALLBACK
        return (text.strip() + "\n\n" + _MACHINE_CONTRACT).strip()

    def _user_text(self, entries: List[Dict[str, Any]], queries: List[Dict[str, Any]],
                   counts: Dict[str, Any], topic_stats: List[Dict[str, Any]],
                   growth_by_qid: Dict[str, float]) -> str:
        """user 文本 = 监控词清单 + counts 摘要 + 条目清单（契约冻结的三段）。

        条目 ≤ max_entries 条、每条 ≤ max_chars_per_entry 字；超出部分明说
        "仅展示前 N 条"——不让模型以为看到了全部。
        """
        max_entries = int(self.rules["max_entries"])
        max_chars = int(self.rules["max_chars_per_entry"])
        lines: List[str] = ["监控词清单："]
        for idx, stat in enumerate(topic_stats, 1):
            query = queries[idx - 1] if idx - 1 < len(queries) else {}
            domain = str(query.get("domain") or "").strip() if isinstance(query, dict) else ""
            e_txt = "暂无" if stat["e"] is None else f"{stat['e']:g}"
            lines.append(f"{idx}. {stat['query']}｜E={e_txt}｜环比={stat['growth']:g}%"
                         f"｜候选 {stat['candidates']} 条｜领域：{domain or '未判定'}")
        risk = counts["risk"]
        lines.append("")
        lines.append(f"计数：话题 {counts['queries']}｜候选 {counts['candidates']}"
                     f"｜已深查 {counts['verified']}｜跨轮去重 {counts['deduped']}"
                     f"｜降级 {counts['degraded']}｜风险分布：关注 {risk['notice']}、"
                     f"疑似 {risk['suspect']}、高危 {risk['high']}、紧急 {risk['urgent']}")
        lines.append("")
        lines.append("条目清单（[E数字] 即条目 id，可被 analysis 引用）：")
        for idx, entry in enumerate(entries[:max_entries], 1):
            lines.append(_entry_line(_entry_tag(entry, idx), entry,
                                     _entry_growth(entry, growth_by_qid), max_chars))
        if len(entries) > max_entries:
            lines.append(f"（仅展示前 {max_entries} 条，共 {len(entries)} 条）")
        return "\n".join(lines)

    # ---- (3) 代码校验：安全闸 + 截断 + 白名单 ----

    def _validate_llm_fields(self, obj: Dict[str, Any], *, entries: List[Dict[str, Any]],
                             queries: List[Dict[str, Any]],
                             errors: List[Dict[str, str]]) -> Dict[str, Any]:
        """校验 LLM 输出：白名单（query 集合 / entry_id 集合）+ 安全闸 + 截断。"""
        whitelist = {e["id"] for e in entries
                     if isinstance(e.get("id"), int) and not isinstance(e.get("id"), bool)}
        query_texts = {str(q.get("query") or "").strip() for q in queries}
        query_texts.discard("")
        return {
            "title": _guard_text(obj.get("title"), "title", errors),
            "summary": _guard_text(obj.get("summary"), "summary", errors),
            "risks": _guard_text(obj.get("risks"), "risks", errors) or "",
            "topic_notes": self._clean_topic_notes(obj.get("topic_notes"),
                                                   query_texts, errors),
            "analysis": self._clean_analysis(obj.get("analysis"), whitelist, errors),
            "followups": self._clean_followups(obj.get("followups"), errors),
        }

    @staticmethod
    def _clean_topic_notes(raw: Any, query_texts: set,
                           errors: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """topic_notes：query 必须在本轮监控词清单内（否则整条丢弃记 error）；
        note 过安全闸 + 截断到 300 字。"""
        out: List[Dict[str, str]] = []
        for row in _as_rows(raw, "topic_notes", errors):
            query = str(row.get("query") or "").strip()
            if query not in query_texts:
                errors.append({"stage": "report", "category": "unknown_query",
                               "detail": "topic_note 的 query 不在本轮监控词清单内，"
                                         f"整条丢弃：{query or '（空）'}"})
                continue
            note = _guard_text(row.get("note"), "note", errors)
            if note is None:
                continue
            out.append({"query": query, "note": note})
        return out

    @staticmethod
    def _clean_analysis(raw: Any, whitelist: set,
                        errors: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """analysis：point 过安全闸 + 截断到 400 字；entry_ids 按白名单收敛。"""
        out: List[Dict[str, Any]] = []
        for row in _as_rows(raw, "analysis", errors):
            point = _guard_text(row.get("point"), "point", errors)
            if point is None:
                continue
            out.append({"point": point,
                        "entry_ids": _filter_entry_ids(row.get("entry_ids"),
                                                       whitelist, errors)})
        return out

    @staticmethod
    def _clean_followups(raw: Any, errors: List[Dict[str, str]]) -> List[str]:
        """followups：至多 5 条，每条过安全闸 + 截断到 120 字。"""
        if raw is None:
            return []
        if not isinstance(raw, list):
            errors.append({"stage": "report", "category": "bad_llm_field",
                           "detail": "followups 不是数组，已按空处理"})
            return []
        out: List[str] = []
        for item in raw[:_FOLLOWUPS]:
            text = _guard_text(item, "followup", errors)
            if text is not None:
                out.append(text)
        return out

    # ---- 内部小助手 ----

    def _now(self) -> str:
        """当前时间（时钟不可用/抛异常时退系统默认，绝不外抛）。"""
        if not callable(self.clock):
            return _default_clock()
        try:
            return str(self.clock())
        except Exception:  # noqa: BLE001  时钟坏了不该让报告崩
            return _default_clock()


def _entry_tag(entry: Dict[str, Any], idx: int) -> str:
    """条目引用标签：有落库 id → E{id}；无 id → 序号占位并明标"未落库"
    （它不进 analysis 白名单——模型引用它会被代码移除并记 error）。"""
    entry_id = _as_int(entry.get("id"), None)
    return f"E{entry_id}" if entry_id is not None else f"E{idx}(未落库)"


def _entry_growth(entry: Dict[str, Any], growth_by_qid: Dict[str, float]) -> float:
    """条目环比：优先所属监控词的 growth；查不到再用条目自己的 e/e_prev 现算。"""
    key = _qkey(entry.get("query_id"))
    if key in growth_by_qid:
        return growth_by_qid[key]
    return _growth_percent(_as_float(entry.get("e_score")), _as_float(entry.get("e_prev")))


def _entry_line(tag: str, entry: Dict[str, Any], growth: float, limit: int) -> str:
    """单条目行（契约冻结格式，超长截到 limit）：

    [E{id}] {risk_label}｜《{title}》｜{domain}｜E={e}｜环比={growth}%｜
    深查：{verdict_label}({risk_score})｜领域：{topic_domain}｜摘要：{snippet}
    """
    level = _risk_level_or_notice(entry.get("risk_level"))
    label = _risk_labels().get(level, level)
    title = str(entry.get("title") or "").strip() or "（无标题）"
    domain = str(entry.get("domain") or "").strip() or "未知域名"
    e = _as_float(entry.get("e_score"))
    e_txt = "暂无" if e is None else f"{e:g}"
    verdict = str(entry.get("verdict_label") or "").strip()
    score = _as_float(entry.get("risk_score"))
    if verdict:
        deep = (f"深查：{verdict}({score:g})" if score is not None
                else f"深查：{verdict}(无风险分)")
    else:
        deep = "深查：未深查"
    topic = str(entry.get("topic_domain") or "").strip() or "未判定"
    line = (f"[{tag}] {label}｜《{title}》｜{domain}｜E={e_txt}｜环比={growth:g}%"
            f"｜{deep}｜领域：{topic}")
    snippet = _entry_snippet(entry)
    if snippet:
        line += f"｜摘要：{snippet}"
    return line[:limit]
