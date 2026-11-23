# -*- coding: utf-8 -*-
"""统计说法核查流水线（第五波，服务 layperson 角色）：只比对数字口径，不判内容真假。

改了什么、为什么、锁定什么：
   - 新增 `assistant.skills.statistics.pipeline`：把"预检 → 代码抽数字主张 →
     逐主张检索读页 → 代码口径比对 → 汇总"编排成一条轻流水线（`check_statistics` /
     `StatisticsSkills.run`）。用户贴一段带数字的说法，逐条回答"这个数字与已读
     来源正文对得上吗"。
   - **判定全部由代码**：数值表达由正则抽取（百分比/百分点/倍数/约·超过·近 等
     约数表述/带单位绝对值/年份），数值归一（千分位/精度/单位进率）后比对，
     verdict ∈ consistent|inconsistent|caliber_mismatch|unverifiable 四态封闭
     枚举；LLM 只许把摘录结构化成 claim 对象（封闭字段，坏形状丢弃）和在已读
     正文里定位相关段落（quote 不逐字锚定即丢弃），不参与任何数值判定。
   - **不判内容真假（铁律）**：digest/report 不出现真假判定词（词表仿 source
     流水线 `_VERDICT_WORDS`）；「未核实到」只表示本次没有定位到可比对的来源
     数字，绝不说成「数字为假」；没有分数、不建档、不写库。
   - **每个出口都有降级**：输入为空/超长 → needs_clarification；原文没有数字
     表达 → task_status=no_factual_claim（不是失败）；检索未授权/不可用 →
     零外发 + 如实记 error，主张全部落 unverifiable；预算/死线/取消保留已取得
     的主张与比对结果；模型结构化结果坏形状 → 丢弃并退回代码抽取结果。
   - 全离线可测：search_fn / fetch_fn / llm_fn / transport / clock / on_progress
     全部可注入，测试零网络零 LLM。

与 collect_core 的关系：预算、运行状态、引文锚定、SKILL.md 装配、来源分类、
计数行复用共享底座；本模块只做编排与"数值归一 + 比对"的口径实现，不重复那套东西。
外发授权闸（public-search / public-web，fail-closed）由服务工厂
（loop 侧 `statistics_skills`，T4 接线；模型段 `statistics_model`，
cache_namespace `assistant_statistics`，由 T4 写入 assistant_rules.json）
在注入 search_fn/fetch_fn/llm_fn 时把守；本流水线把"检索降级/未授权"与
"搜过了没找到"分开记，未授权时零取页。

依赖方向：只 import 标准库与 assistant/retrieval 内部包，**不 import web /
platform / dossier**。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from mylocalai.assistant.skills.collect_core import (
    STATUSES,
    TASK_STATUSES,
    CollectBudget,
    CollectRunState,
    anchor_quote,
    classify_source,
    load_collect_rules,
    load_skill_prompt,
    render_counts_line,
    url_key,
    utc_now_iso,
)
from mylocalai.assistant.skills.pipeline import (
    locate_quote,
    normalize_text,
    strip_pii,
)
from mylocalai.assistant.skills.transport import strip_query_operators
from mylocalai.retrieval.sources import tier_rank

__all__ = ["StatisticsSkills", "check_statistics", "render_report", "load_rules",
           "VERDICTS", "STATUSES", "TASK_STATUSES"]

#: 口径比对结论（封闭枚举，四态；全部由代码产出，模型不参与判定）
VERDICTS = ("consistent", "inconsistent", "caliber_mismatch", "unverifiable")
#: 四态中文标签（禁真假判定词："一致/不一致"只描述数字口径，不描述内容真假）
VERDICT_LABELS = {"consistent": "与来源一致", "inconsistent": "与来源不一致",
                  "caliber_mismatch": "口径不一致", "unverifiable": "未核实到"}

#: 用户可见的六个进度阶段；不暴露内部技能名
_PROGRESS_STAGES = ("解析输入", "抽取数字主张", "检索资料", "阅读正文",
                    "口径比对", "汇总输出")

#: 固定真假判定词（铁律 1）：摘要/补充说明/报告里一个都不许出现
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")
_VERDICT_RE = re.compile("|".join(re.escape(w) for w in _VERDICT_WORDS))
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）】]+", re.I)
#: 模型补充说明里不允许出现的"升级"表述（把未核实到说成确定性结论）
_NOTE_BANNED = ("为真", "为假", "造假", "编造", "可以认定", "能够认定",
                "证明为真", "证明为假", "结论为真", "结论为假")

_HERE = Path(__file__).resolve().parent
_RULES_PATH = _HERE / "rules.json"
_SKILL_DIR = _HERE

# 内置默认值：与 rules.json 保持一致。rules.json 缺失/损坏时以此兜底，保证流水线可用。
DEFAULT_STATISTICS_RULES: Dict[str, Any] = {
    "max_input_chars": 4000,
    "max_claims": 5,
    "max_searches": 8,
    "max_pages": 8,
    "per_claim_max_queries": 2,
    "per_claim_max_pages": 2,
    "max_llm_calls": 12,
    "total_deadline_s": 90,
    "io_timeout_s": 10,
    "max_page_bytes": 1500000,
    "max_text_chars": 20000,
    "max_redirect_hops": 3,
    "max_quote_chars": 300,
}

# 数值安全区间（键名锁定；未列出的键按 defaults 类型校验，不夹取）
_RULE_LIMITS: Dict[str, Tuple[float, float]] = {
    "max_input_chars": (50, 200000),
    "max_claims": (1, 50),
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
}

#: 补充说明的机器契约（可选受约束生成；越界即丢弃，退纯模板摘要）
_NOTE_CONTRACT = ('只输出 JSON：{"note":"<不超过100字的补充说明>"}；'
                  "只许转述上面代码已给出的判定与来源数字，不得新增主张、数字、"
                  "URL、日期或结论；不得出现真假判定词；「未核实到」不得改述为"
                  "任何真假结论。")

#: 返回 dict 与 data/claims/counts 的键名锁定（写进 SKILL.md；测试按此锁定）
_DATA_KEYS = ("text", "state", "claims", "limits", "counts")
_CLAIM_KEYS = ("claim_id", "excerpt", "number", "unit", "metric",
               "qualifiers", "verdict", "source_number", "source_url",
               "source_title", "quote", "as_of", "basis")
_QUALIFIER_KEYS = ("time", "place", "entity", "base")
_COUNTS_KEYS = ("claims", "searches", "pages", "llm_calls")

#: 摘录（分句/窗口）长度上限；报告引文上限走 rules.max_quote_chars
_EXCERPT_MAX_CHARS = 200
_SENTENCE_DELIMS = "。！？；\n"


# ---------------------------------------------------------------------------
# 规则加载
# ---------------------------------------------------------------------------

def load_rules(path: Optional[Any] = None) -> Dict[str, Any]:
    """加载 statistics/rules.json：缺键回退默认、数值夹取、坏类型回退、未知键丢弃。

    传 dict 按"覆盖项"处理（便于测试按需收紧额度）；传路径读该文件；不传读包内
    rules.json。文件缺失/不可解析 → 内置默认值（不抛）：取不到预算配置时"按默认
    额度跑"比"整个工具不可用"更符合降级文化。
    """
    if isinstance(path, Mapping):
        merged = dict(DEFAULT_STATISTICS_RULES)
        merged.update({k: v for k, v in path.items() if k in DEFAULT_STATISTICS_RULES})
        return _clamp_rules(merged)
    target = _RULES_PATH if path is None else Path(path)
    return load_collect_rules(target, DEFAULT_STATISTICS_RULES, _RULE_LIMITS)


def _clamp_rules(rules: Mapping[str, Any]) -> Dict[str, Any]:
    """与 collect_core.load_collect_rules 同口径的夹取（dict 覆盖项没有文件可读）。"""
    out: Dict[str, Any] = {}
    for key, default in DEFAULT_STATISTICS_RULES.items():
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
# 数值抽取与归一（全部代码；模型不参与）
# ---------------------------------------------------------------------------

#: 数字本体：千分位逗号 + 小数。归一时剥掉逗号（"1,234.5" 与 "1234.5" 同值）。
_NUM = r"(?P<num>\d+(?:,\d{3})*(?:\.\d+)?)"
#: 百分比：5.2% / 5.2％（半角与全角百分号都认）
_PERCENT_RE = re.compile(_NUM + r"\s*(?P<unit>%|％)")
#: 百分点：3 个百分点 / 3个百分点
_POINT_RE = re.compile(_NUM + r"\s*(?P<unit>个百分点|百分点)")
#: 倍数：2 倍
_MULTIPLE_RE = re.compile(_NUM + r"\s*(?P<unit>倍)")
#: 翻了 N 番 = 2^N 倍（"翻了一番"=2 倍；数字可缺省，也可写中文数词）
_FAN_RE = re.compile(r"翻了?\s*(?P<num>\d+|[一二两三四五])?\s*番")
_FAN_WORDS = {"一": 1.0, "两": 2.0, "二": 2.0, "三": 3.0, "四": 4.0, "五": 5.0}
#: 金额：3.5 万亿元 / 500 万元 / 1200 元 / 20 亿美元（进率：万亿 1e12、亿 1e8、万 1e4）
_MONEY_RE = re.compile(_NUM + r"\s*(?P<scale>万亿|亿|万)?\s*(?P<unit>元|美元|欧元|人民币|日元|港元)")
#: 人数：902 万人 / 14 亿人 / 1200 人
_PEOPLE_RE = re.compile(_NUM + r"\s*(?P<scale>亿|万)?\s*(?P<unit>人)")
#: 年份：2023 年（只认 19xx/20xx 四位年份，避免把"5 年"当年份）
_YEAR_RE = re.compile(r"(?P<num>(?:19|20)\d{2})\s*(?P<unit>年)")
#: 其他带单位绝对值（封闭单位表；表外单位不抽，宁可少抽不可猜）
_GENERIC_RE = re.compile(
    _NUM + r"\s*(?P<unit>吨|千瓦时|辆|架|艘|公顷|平方公里|公里|千米|千克|公斤|克|升|毫升|件|台|部|册|名|户|家|座|栋|平方米)")

_SCALES = {"万亿": 1e12, "亿": 1e8, "万": 1e4, "": 1.0}
#: 口径族：同族不同单位（如 百分比 vs 百分点）判 caliber_mismatch，
#: 跨族（如 金额 vs 比率）不作数值比对，落 unverifiable。
_FAMILIES = {"percent": "ratio", "percentage_point": "ratio", "multiple": "multiple",
             "money": "money", "people": "people", "year": "time"}

#: 约数/限定表述（hedge）：数字前后各看一小窗，决定比对容差
_HEDGE_BEFORE: Tuple[Tuple[str, str], ...] = (
    ("约为", "about"), ("大约", "about"), ("约", "about"), ("接近", "about"),
    ("将近", "about"), ("差不多", "about"),
    ("超过", "ge"), ("超出", "ge"), ("至少", "ge"), ("最低", "ge"),
    ("不足", "le"), ("不到", "le"), ("低于", "le"),
)
_HEDGE_AFTER: Tuple[Tuple[str, str], ...] = (
    ("以上", "ge"), ("以下", "le"), ("以内", "le"),
    ("左右", "about"), ("上下", "about"), ("前后", "about"),
)


def _parse_number(raw: Any) -> Optional[float]:
    """数字原文 → float：剥千分位逗号（半/全角）后解析；失败返回 None。"""
    text = str(raw or "").replace(",", "").replace("，", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _family_of(kind: str) -> str:
    """单位种类 → 口径族（generic:* 合成一族：表外单位之间不猜换算）。"""
    if kind.startswith("generic:"):
        return "generic"
    return _FAMILIES.get(kind, kind)


def _unit_label(kind: str, unit: str) -> str:
    """单位中文标签（报告/摘要里的人类可读口径名）。"""
    if kind.startswith("generic:"):
        return unit or kind[len("generic:"):]
    return {"percent": "百分比", "percentage_point": "百分点", "multiple": "倍",
            "money": "金额", "people": "人数", "year": "年份"}.get(kind, unit or kind)


def _sentence_around(text: str, start: int, end: int) -> str:
    """取数字所在的分句（逐字子串）；过长则退成数字前后的定长窗口（仍逐字连续）。"""
    lo = start
    while lo > 0 and text[lo - 1] not in _SENTENCE_DELIMS:
        lo -= 1
    hi = end
    while hi < len(text) and text[hi] not in _SENTENCE_DELIMS:
        hi += 1
    sentence = text[lo:hi].strip()
    if len(sentence) <= _EXCERPT_MAX_CHARS:
        return sentence
    window_lo = max(lo, start - 60)
    window_hi = min(hi, end + 120)
    return text[window_lo:window_hi].strip()[:_EXCERPT_MAX_CHARS]


def _hedge_around(text: str, start: int, end: int) -> str:
    """数字前后小窗里的约数表述：""=精确、"about"=约/近/"ge"=超过·以上/"le"=不足·以下。"""
    before = text[max(0, start - 5):start].strip()
    after = text[end:end + 4].strip()
    for word, hedge in _HEDGE_BEFORE:
        if before.endswith(word):
            return hedge
    for word, hedge in _HEDGE_AFTER:
        if after.startswith(word):
            return hedge
    return ""


def _extract_numbers(text: Any) -> List[Dict[str, Any]]:
    """代码抽取数值表达（唯一入口）：返回按出现位置排序的候选列表。

    每条候选含：number（float）、unit（逐字单位）、kind/scale（归一口径）、
    norm（number×进率，比对用）、start/end（数字在文中的位置）、excerpt（所在
    分句，逐字）、hedge（约数表述）、raw（数字原文）。
    """
    body = normalize_text(text)
    found: List[Tuple[int, Dict[str, Any]]] = []
    for match in _PERCENT_RE.finditer(body):
        number = _parse_number(match.group("num"))
        if number is not None:
            found.append((match.start("num"), _candidate(
                body, match, number, match.group("unit"), "percent", 1.0)))
    for match in _POINT_RE.finditer(body):
        number = _parse_number(match.group("num"))
        if number is not None:
            found.append((match.start("num"), _candidate(
                body, match, number, match.group("unit"), "percentage_point", 1.0)))
    for match in _MULTIPLE_RE.finditer(body):
        number = _parse_number(match.group("num"))
        if number is not None:
            found.append((match.start("num"), _candidate(
                body, match, number, match.group("unit"), "multiple", 1.0)))
    for match in _FAN_RE.finditer(body):
        raw_times = match.group("num")
        times = _parse_number(raw_times)
        if times is None:
            times = _FAN_WORDS.get(str(raw_times or "").strip(), 1.0)
        # "翻了一番" 没有数字组：位置退到整个匹配上（start("num") 会是 -1）
        if raw_times:
            found.append((match.start("num"), _candidate(
                body, match, float(2 ** int(times)), "倍", "multiple", 1.0)))
        else:
            found.append((match.start(), _candidate(
                body, match, float(2 ** int(times)), "倍", "multiple", 1.0,
                num_span=(match.start(), match.end()))))
    for match in _MONEY_RE.finditer(body):
        number = _parse_number(match.group("num"))
        if number is not None:
            scale = _SCALES.get(match.group("scale") or "", 1.0)
            unit = f"{match.group('scale') or ''}{match.group('unit')}"
            found.append((match.start("num"), _candidate(
                body, match, number, unit, "money", scale)))
    for match in _PEOPLE_RE.finditer(body):
        number = _parse_number(match.group("num"))
        if number is not None:
            scale = _SCALES.get(match.group("scale") or "", 1.0)
            unit = f"{match.group('scale') or ''}{match.group('unit')}"
            found.append((match.start("num"), _candidate(
                body, match, number, unit, "people", scale)))
    for match in _YEAR_RE.finditer(body):
        number = _parse_number(match.group("num"))
        if number is not None:
            found.append((match.start("num"), _candidate(
                body, match, number, match.group("unit"), "year", 1.0)))
    for match in _GENERIC_RE.finditer(body):
        number = _parse_number(match.group("num"))
        if number is not None:
            unit = match.group("unit")
            found.append((match.start("num"), _candidate(
                body, match, number, unit, f"generic:{unit}", 1.0)))
    # 同一数字位置只保留一条（不同模式单位要求不同，正常不会撞；
    # 撞上时先percent/point/money 等模式优先，保底不重复计一个主张）
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for position, item in sorted(found, key=lambda pair: pair[0]):
        key = (item["start"], item["end"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _candidate(body: str, match: "re.Match[str]", number: float, unit: str,
               kind: str, scale: float,
               num_span: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
    """一个数值候选的完整形状（excerpt/hedge 都从正文原位计算，逐字可锚）。

    hedge 的"后置表述"窗口从**整匹配尾**开始（含单位）："5% 以上"的"以上"在
    百分号之后，只看数字尾会把单位后的约数表述漏掉。
    """
    if num_span is None:
        start, end = match.start("num"), match.end("num")
    else:
        start, end = num_span
    return {"number": number, "unit": unit, "kind": kind, "scale": scale,
            "norm": number * scale, "start": start, "end": end,
            "raw": match.group("num") or str(number), "excerpt": _sentence_around(body, start, end),
            "hedge": _hedge_around(body, start, match.end())}


def _normalize_unit(unit: Any) -> Tuple[str, float]:
    """单位文本 → (kind, scale)：与代码抽取同口径，供校验模型给出的单位。"""
    text = str(unit or "").strip()
    if not text:
        return "", 1.0
    if text in ("%", "％"):
        return "percent", 1.0
    if text in ("个百分点", "百分点"):
        return "percentage_point", 1.0
    if text == "倍":
        return "multiple", 1.0
    if text == "年":
        return "year", 1.0
    money = re.fullmatch(r"(万亿|亿|万)?(元|美元|欧元|人民币|日元|港元)", text)
    if money:
        return "money", _SCALES.get(money.group(1) or "", 1.0)
    people = re.fullmatch(r"(亿|万)?人", text)
    if people:
        return "people", _SCALES.get(people.group(1) or "", 1.0)
    return f"generic:{text}", 1.0


def _values_match(claim: Mapping[str, Any], source: Mapping[str, Any]) -> bool:
    """数值相等判定（归一后）：精确相等；约数表述按容差（约±10%、
    超过=来源不低于主张、不足=来源不高于主张）。"""
    a = float(claim.get("norm") or 0.0)
    b = float(source.get("norm") or 0.0)
    tol = max(abs(a) * 1e-9, 1e-9)
    hedge = str(claim.get("hedge") or "")
    if hedge == "ge":
        return b >= a - tol
    if hedge == "le":
        return b <= a + tol
    if hedge == "about":
        return abs(b - a) <= max(abs(a) * 0.1, tol)
    return abs(b - a) <= tol


def _compare_claim(claim: Mapping[str, Any],
                   source_numbers: Sequence[Mapping[str, Any]]
                   ) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """口径比对矩阵（全部代码，模型不参与）：

    1. 来源段落没有数字 → unverifiable（没核实到，绝不说成数字为假）；
    2. 同单位且值相等（含约数容差）→ consistent；
    3. 同单位但值不等 → inconsistent；
    4. 同口径族但单位不同（如 百分比 vs 百分点）→ caliber_mismatch；
    5. 有数字但跨口径族（如 金额 vs 比率）→ unverifiable（未作数值比对）。
    """
    kind = str(claim.get("kind") or "")
    family = _family_of(kind)
    claim_label = _unit_label(kind, str(claim.get("unit") or ""))
    claim_raw = f"{claim.get('number')}{claim.get('unit') or ''}"
    if not source_numbers:
        return "unverifiable", None, "已读正文里没有可比对的数字"
    same = [n for n in source_numbers if n["kind"] == kind]
    if same:
        for item in same:
            if _values_match(claim, item):
                basis = (f"来源段落数字「{item['raw']}{item['unit']}」与主张数字"
                         f"「{claim_raw}」一致（{claim_label}）")
                hedge = str(claim.get("hedge") or "")
                if hedge == "about":
                    basis += "；主张带约数表述，按 10% 容差比对"
                elif hedge == "ge":
                    basis += "；主张为下限表述，来源数字不低于主张"
                elif hedge == "le":
                    basis += "；主张为上限表述，来源数字不高于主张"
                return "consistent", item, basis
        return ("inconsistent", same[0],
                f"来源段落数字「{same[0]['raw']}{same[0]['unit']}」与主张数字"
                f"「{claim_raw}」不一致（同口径{claim_label}）")
    cross = [n for n in source_numbers if _family_of(n["kind"]) == family]
    if cross:
        item = cross[0]
        return ("caliber_mismatch", item,
                f"来源段落只有不同口径的数字「{item['raw']}{item['unit']}」"
                f"（{_unit_label(item['kind'], item['unit'])}），未与主张"
                f"（{claim_label}）作数值比对")
    return ("unverifiable", None,
            "已读正文的数字与主张不同口径族（如绝对值与比率），未作数值比对")


def _code_locate_quote(claim: Mapping[str, Any], body: str) -> str:
    """无模型时的确定性段落定位：已读正文里第一个含同口径族数字的分句。

    与模型定位同口径——只许在**已读正文**里逐字取句，不改写、不新增数字；
    优先取含指标名的分句（指标名缺失时退化为任意含同族数字的分句）。
    """
    family = _family_of(str(claim.get("kind") or ""))
    metric = str(claim.get("metric") or "")
    sentences = [s.strip() for s in re.split("[" + _SENTENCE_DELIMS + "]", body)
                 if s.strip()]

    def _pick(require_metric: bool) -> str:
        for sentence in sentences:
            if require_metric and metric and metric not in sentence:
                continue
            for item in _extract_numbers(sentence):
                if _family_of(item["kind"]) == family:
                    return sentence
        return ""

    return _pick(True) or _pick(False)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _clean(value: Any, limit: int = 200) -> str:
    """压空白 + 截断：所有进入输出结构的自由文本都过这里。"""
    return normalize_text(value)[:limit]


def _first_list(obj: Any, keys: Tuple[str, ...]) -> Optional[list]:
    """从模型返回里取第一个命中键的列表（容忍键名写歪）。"""
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


def _host_of(url: Any) -> str:
    """取主机名（小写、去尾点、去 www.）；解析失败返回空串。"""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:  # noqa: BLE001  畸形 URL 只求一个域名，失败按空串处理
        return ""
    return host[4:] if host.startswith("www.") else host


def _candidate_rank(url: str) -> Tuple[int, int, str]:
    """候选排序键：信源目录锚点优先，其次档位（与 authority 同口径，代码判定）。"""
    info = classify_source(url)
    return (0 if info.verification == "catalog_anchor" else 1,
            tier_rank(info.tier), url)


# ---------------------------------------------------------------------------
# 运行态
# ---------------------------------------------------------------------------

class _RunContext:
    """一次运行的内部上下文（不进输出结构，避免内部字段泄漏到卡片）。"""

    def __init__(self) -> None:
        self.pages: Dict[str, Tuple[Optional[Dict[str, Any]],
                                    Optional[Dict[str, str]]]] = {}


class _RunState:
    """一次运行的状态：错误/缺口/审计/终止原因（公共部分委托 collect_core.CollectRunState）。

    data 键在一次运行里始终存在（空出口也要有完整形状，键名锁定见 _DATA_KEYS）。
    """

    def __init__(self, budget: CollectBudget,
                 cancel_check: Optional[Callable[[], bool]] = None,
                 clock: Optional[Callable[[], str]] = None) -> None:
        self.core = CollectRunState(budget, cancel_check)
        self.clock = clock or utc_now_iso
        self.cancel_check = cancel_check
        self.clarified = False
        self.clarify_reason = ""
        self.failed = False
        self.no_factual = False
        # 受约束补充说明（不进 data：data 键锁定为 text/state/claims/limits/counts，
        # 补充说明只追加进报告末尾）
        self.note = ""
        # data 键在一次运行里始终存在（空出口也要有完整形状，键名锁定见 _DATA_KEYS）
        self.data: Dict[str, Any] = {
            "text": "", "state": "ok", "claims": [], "limits": [],
            "counts": {"claims": 0, "searches": 0, "pages": 0, "llm_calls": 0},
        }

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

class StatisticsSkills:
    """统计说法核查流水线（预检 → 抽数字主张 → 检索读页 → 口径比对 → 汇总）。

    全部 IO 可注入：`llm_fn(system, user) -> (obj, err)`（chat_json 契约）、
    `search_fn(query, k) -> {"results": [...], "degraded": bool, "error": str?}`、
    `fetch_fn(url, *, allowed_domains, cancel_check, deadline) -> transport.fetch 形状`。
    未注入 fetch_fn 时延迟 import `assistant.skills.transport` 的默认实现，import
    失败降级为"取页不可用"错误条目（并行开发期 transport 可能尚未就位）。
    外发授权闸（public-search/public-web）由服务工厂在注入 search_fn/fetch_fn 时
    把守；本流水线只负责把"未授权/降级"与"搜过了没找到"分开如实记。
    """

    def __init__(self, *, rules: Optional[Any] = None,
                 search_fn: Optional[Callable[[str, int], Dict[str, Any]]] = None,
                 fetch_fn: Optional[Callable[..., Dict[str, Any]]] = None,
                 llm_fn: Optional[Callable[[str, str], Any]] = None,
                 transport: Optional[Any] = None,
                 clock: Optional[Callable[[], str]] = None,
                 on_progress: Optional[Callable[[str, int, int], Any]] = None) -> None:
        self.rules = load_rules(rules)
        self.search_fn = search_fn
        self.fetch_fn = fetch_fn
        self.llm_fn = llm_fn
        self.transport = transport
        self.clock = clock or utc_now_iso
        self.on_progress = on_progress
        self._fetch_probed = fetch_fn is not None
        self._on_progress: Optional[Callable[[str, int, int], Any]] = None

    # -- 入口 ---------------------------------------------------------------

    def run(self, text: str, *, cancel_check: Optional[Callable[[], bool]] = None,
            deadline: Optional[float] = None,
            on_progress: Optional[Callable[[str, int, int], Any]] = None) -> Dict[str, Any]:
        """跑完整条流水线，返回统一运行外壳（data 键名锁定，见 _DATA_KEYS）。"""
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
            self._summarize(st)
            return self._finish(st, started_at)
        self._progress(st, _PROGRESS_STAGES[0], 1)

        claims = self._stage_extract_claims(st, raw)
        self._progress(st, _PROGRESS_STAGES[1], 2)
        if not claims:
            # 原文没有数字表达：合法空出口（no_factual_claim），不检索不读页
            self._summarize(st)
            self._progress(st, _PROGRESS_STAGES[5], 6)
            return self._finish(st, started_at)

        self._stage_search(st, claims)
        self._progress(st, _PROGRESS_STAGES[2], 3)
        self._stage_read(st, claims, ctx)
        self._progress(st, _PROGRESS_STAGES[3], 4)
        self._stage_compare(st, claims, ctx)
        self._progress(st, _PROGRESS_STAGES[4], 5)

        st.data["claims"] = [_public_claim(record) for record in claims]
        if st.data["claims"] and int(budget.used.get("max_pages") or 0) == 0:
            # 有主张但一页都没读到：如实标 unreachable，绝不假装核过
            st.data["state"] = "unreachable"
        self._summarize(st)
        self._progress(st, _PROGRESS_STAGES[5], 6)
        return self._finish(st, started_at)

    # -- 阶段 1：预检 -------------------------------------------------------

    def _precheck(self, st: _RunState, raw: str) -> bool:
        if not raw.strip():
            st.error("precheck", "empty_input", "输入为空或只有空白字符")
            st.clarify("输入为空：请提供一段含数字的说法（如“失业率为 5.2%”）")
            st.data["state"] = "unclear"
            return False
        limit = int(self.rules["max_input_chars"])
        if len(raw) > limit:
            st.error("precheck", "input_too_long",
                     f"输入 {len(raw)} 字符，超过上限 {limit}")
            st.clarify(f"输入长度 {len(raw)} 超过单次上限 {limit}：请只贴要核查的那段说法")
            st.data["state"] = "unclear"
            return False
        return True

    # -- 阶段 2：数字主张抽取（代码抽取 + 可选结构化） ----------------------

    def _stage_extract_claims(self, st: _RunState, raw: str) -> List[Dict[str, Any]]:
        """代码正则抽数值表达 → LLM 仅把摘录结构化为 claim（坏形状丢弃）。

        没有 LLM 或结构化结果全部未过校验时，退回代码抽取结果（确定性兜底，
        不放大失败）；原文一个数字表达都没有 → no_factual_claim（合法空出口）。
        """
        normalized = normalize_text(raw)
        st.data["text"] = _clean(raw, int(self.rules["max_input_chars"]))
        candidates = _extract_numbers(normalized)
        if not candidates:
            st.mark_no_factual_claim()
            st.gap("原文没有可核对的数字表达（百分比/百分点/倍数/带单位绝对值/年份）")
            return []
        cap = int(self.rules["max_claims"])
        contract = (
            '只输出 JSON：{"claims":[{"excerpt":"<原文逐字片段>",'
            '"number":"<数字原文，与代码抽取一致>",'
            '"unit":"<数字单位，与代码口径一致>","metric":"<指标名或空串>",'
            '"qualifiers":{"time":"","place":"","entity":"","base":""}}]}；'
            f"最多 {cap} 条；excerpt 必须逐字来自原文；number/unit 必须与代码抽取"
            "到的某一条一致；metric/qualifiers 缺失时给空串；不做真假判断、不给数值分。")
        listing = "\n".join(
            f"- 数字 {c['number']}{c['unit']}｜摘录「{c['excerpt']}」"
            for c in candidates[:20])
        obj, err = self._call_llm(
            st, load_skill_prompt(_SKILL_DIR, contract),
            f"用户原文：\n{normalized}\n\n代码抽取到的数字候选（number/unit 必须与其中某条一致）：\n{listing}",
            "claims")

        rows = _first_list(obj, ("claims", "items")) if isinstance(obj, dict) else None
        records: List[Dict[str, Any]] = []
        if isinstance(rows, list) and rows:
            if len(rows) > cap:
                st.error("claims", "too_many_claims",
                         f"识别出 {len(rows)} 条数字主张，超过单次上限 {cap}，已取前 {cap} 条")
                st.gap(f"数字主张超过单次上限 {cap}，本次只核查前 {cap} 条")
            for index, row in enumerate(rows[:cap], start=1):
                record = self._build_claim(st, row, normalized, index)
                if record is not None:
                    records.append(record)
        if not records:
            # LLM 未注入/形状不符/全部未过校验 → 代码抽取结果兜底（不放大失败）
            if err == "llm_unavailable":
                st.data["limits"].append("模型未注入，数字主张按代码抽取结果处理")
            elif not isinstance(rows, list):
                st.data["limits"].append("模型结构化结果形状不符，已按代码抽取结果处理")
            else:
                st.data["limits"].append(
                    "模型结构化结果全部未通过代码校验，已按代码抽取结果处理")
            records = [_code_claim(item, index)
                       for index, item in enumerate(candidates[:cap], start=1)]
        return records

    def _build_claim(self, st: _RunState, row: Any, normalized: str,
                     index: int) -> Optional[Dict[str, Any]]:
        """LLM 结构化行 → claim 内部记录；任一封闭字段不过校验即整条丢弃。

        number/unit 一律以**代码从该摘录里抽到的结果**为准（摘录逐字来自原文），
        模型只能补 metric/qualifiers 这类描述性字段。
        """
        if not isinstance(row, dict):
            st.error("claims", "bad_claim_shape", "主张条目不是对象，已丢弃")
            return None
        excerpt = _clean(row.get("excerpt"), _EXCERPT_MAX_CHARS)
        if not excerpt or locate_quote(normalized, excerpt) is None:
            st.error("claims", "claim_not_anchored",
                     f"摘录未在原文中逐字定位，已丢弃：{excerpt[:60]}")
            return None
        numbers = _extract_numbers(excerpt)
        if not numbers:
            st.error("claims", "claim_without_number",
                     f"摘录里没有代码可识别的数字表达，已丢弃：{excerpt[:60]}")
            return None
        given = _parse_number(row.get("number"))
        match = next((n for n in numbers
                      if given is not None and abs(n["number"] - given) <= 1e-9), None)
        if match is None:
            # 数值只认代码抽取结果：模型报的数字必须等于代码从该摘录抽到的某一个
            st.error("claims", "number_not_from_code",
                     "模型给出的数字不是代码从摘录中抽取的结果，已丢弃")
            return None
        unit = _clean(row.get("unit"), 20)
        if unit:
            kind, scale = _normalize_unit(unit)
            if (kind, scale) != (match["kind"], match["scale"]):
                st.error("claims", "unit_mismatch",
                         f"模型给出的单位与代码抽取口径不符，已丢弃：{unit}")
                return None
        qualifiers = row.get("qualifiers")
        qualifiers = qualifiers if isinstance(qualifiers, dict) else {}
        record = dict(match)
        record.update({
            "claim_id": f"C{index}",
            "excerpt": excerpt,
            "number": match["number"],
            "unit": match["unit"],
            "metric": _clean(row.get("metric"), 60),
            "qualifiers": {key: _clean(qualifiers.get(key), 60)
                           for key in _QUALIFIER_KEYS},
            "verdict": "unverifiable", "source_number": "", "source_url": "",
            "source_title": "", "quote": "", "as_of": "",
            "basis": "未定位到含可比数字的来源段落，未作数值比对",
        })
        return record

    # -- 阶段 3：逐主张检索 ------------------------------------------------

    def _stage_search(self, st: _RunState, claims: List[Dict[str, Any]]) -> None:
        """逐主张生成检索词（脱敏 + 剥范围操作符）并检索；未授权零外发。"""
        per_claim_queries = int(self.rules["per_claim_max_queries"])
        for record in claims:
            if st.is_cancelled() or st.budget.blocked_reason:
                break
            queries = self._claim_queries(st, record)
            rows: List[Dict[str, Any]] = []
            seen: set = set()
            for query in queries[:per_claim_queries]:
                if st.is_cancelled() or st.budget.blocked_reason:
                    break
                if not st.budget.allow("max_searches"):
                    reason = st.budget.why("max_searches")
                    st.budget.block(reason)
                    st.gap(f"提前停止检索（{reason}），部分主张未检索")
                    break
                results = self._do_search(st, query, 6, "search")
                if results is None:
                    continue      # 检索不可用/降级：错误与缺口已由 _do_search 记录
                for row in results:
                    if not isinstance(row, dict):
                        continue
                    url = _clean(row.get("url"), 300)
                    key = url_key(url)
                    if not url or key in seen:
                        continue
                    seen.add(key)
                    rows.append(row)
            rows.sort(key=lambda row: _candidate_rank(_clean(row.get("url"), 300)))
            record["_rows"] = rows

    def _claim_queries(self, st: _RunState, record: Mapping[str, Any]) -> List[str]:
        """检索词：主张摘录 + 指标/实体补充；先脱敏，再剥范围操作符。"""
        cap = int(self.rules["per_claim_max_queries"])
        qualifiers = record.get("qualifiers") or {}
        metric = str(record.get("metric") or "")
        extra = " ".join(x for x in (metric, str(qualifiers.get("entity") or ""),
                                     str(qualifiers.get("time") or "")) if x)
        out: List[str] = []
        for raw in [record.get("excerpt"), extra]:
            query = strip_query_operators(strip_pii(raw))
            if query and query not in out:
                out.append(query)
            if len(out) >= cap:
                break
        if not out:
            st.gap(f"主张 {record.get('claim_id')} 脱敏后无可用检索词")
        return out

    # -- 阶段 4：逐主张读页 ------------------------------------------------

    def _stage_read(self, st: _RunState, claims: List[Dict[str, Any]],
                    ctx: _RunContext) -> None:
        """逐主张读页（每主张 per_claim_max_pages 页，总页数受 max_pages 约束）。"""
        per_claim_pages = int(self.rules["per_claim_max_pages"])
        for record in claims:
            if st.is_cancelled() or st.budget.blocked_reason:
                break
            pages: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            for row in record.get("_rows") or []:
                if len(pages) >= per_claim_pages or st.is_cancelled() \
                        or st.budget.blocked_reason:
                    break
                if not st.budget.allow("max_pages"):
                    reason = st.budget.why("max_pages")
                    st.budget.block(reason)
                    st.gap(f"提前停止读页（{reason}），部分主张未读取页面")
                    break
                url = _clean(row.get("url"), 300)
                host = _host_of(url)
                page, _ = self._do_fetch(st, url, [host] if host else None,
                                         "fetch", ctx)
                if page is not None:
                    pages.append((page, row))
            record["_pages"] = pages

    # -- 阶段 5：口径比对（定位段落 + 数值比对，全部代码判定） --------------

    def _stage_compare(self, st: _RunState, claims: List[Dict[str, Any]],
                       ctx: _RunContext) -> None:
        """逐主张：LLM 只许在已读正文定位段落，quote 不逐字锚定即丢弃；
        数值比对全部由代码完成。第一个给出可结论段页的页面定判。

        这里不再因"预算阻塞/取消"整体跳过：页面在阶段4 已取回，比对是本地计算
        （模型定位自带预算/取消检查）——否则后面主张一被预算卡住，前面已取回
        页面的主张也白白丢掉判语。
        """
        for record in claims:
            fallback: Optional[Dict[str, Any]] = None
            for page, row in record.get("_pages") or []:
                found = self._locate_and_compare(st, record, page, row)
                if found is None:
                    continue
                if found["verdict"] == "unverifiable":
                    # 定位到了段落但没有可比数字：先记着，看看后续页面有没有更明确的
                    if fallback is None:
                        fallback = found
                    continue
                self._apply_verdict(record, found)
                break
            else:
                if fallback is not None:
                    self._apply_verdict(record, fallback)

    def _locate_and_compare(self, st: _RunState, record: Mapping[str, Any],
                            page: Mapping[str, Any],
                            row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """在一页已读正文里定位段落 → 逐字锚定 → 代码抽数字 → 口径比对。"""
        body = normalize_text(page.get("text") or "")
        url = _clean(page.get("final_url") or page.get("url"), 300)
        title = _clean(page.get("title") or _clean(row.get("title"), 120), 120)
        if not body:
            st.error("fetch", "empty_page_text",
                     f"{url or '页面'} 读取成功但正文为空，未定位到段落")
            return None
        quote = self._locate_quote(st, record, body)
        if not quote:
            return None
        max_quote = int(self.rules["max_quote_chars"])
        if len(quote) > max_quote:
            shortened = quote[:max_quote]
            if locate_quote(body, shortened) is None:
                st.error("evidence", "quote_too_long",
                         f"段落超过 {max_quote} 字符且截断后仍无法定位，已丢弃")
                return None
            quote = shortened
        span = anchor_quote(body, quote)
        if span is None:
            # 只有"读到了正文但引文定位失败"才算引文未锚定；取页失败走 _do_fetch
            st.error("evidence", "quote_not_anchored",
                     "段落未在已读正文中逐字定位，已丢弃")
            return None
        verdict, source, basis = _compare_claim(record, _extract_numbers(quote))
        return {"verdict": verdict,
                "source_number": f"{source['raw']}{source['unit']}" if source else "",
                "source_url": url, "source_title": title, "quote": quote,
                "as_of": _clean(page.get("fetched_at"), 40) or self.clock(),
                "basis": basis}

    def _locate_quote(self, st: _RunState, record: Mapping[str, Any],
                      body: str) -> str:
        """段落定位：注入了 llm_fn 时由模型在已读正文里逐字选取；未注入时按
        代码确定性定位（第一个含同口径族数字的分句）。两条路都不许改写正文。"""
        if self.llm_fn is None:
            return _code_locate_quote(record, body)
        if not st.budget.allow("max_llm_calls"):
            st.budget.block(st.budget.why("max_llm_calls"))
            return ""
        if st.is_cancelled():
            return ""
        st.budget.spend("max_llm_calls")
        entry = st.operation("llm", f"quote:{record.get('claim_id')}", False)
        contract = ('只输出 JSON：{"quote":"<已读正文里与主张相关的逐字原句>"}；'
                    "quote 必须逐字来自上面正文；只许定位段落，不得改写、总结或"
                    "新增数字；不做真假判断。")
        user = (f"主张：{record.get('excerpt')}\n"
                f"数字：{record.get('number')}{record.get('unit') or ''}\n"
                f"指标：{record.get('metric') or '未注明'}\n"
                f"已读正文（前4000字）：\n{body[:4000]}")
        try:
            out = self.llm_fn(load_skill_prompt(_SKILL_DIR, contract), user)
        except Exception as exc:  # noqa: BLE001  模型异常只影响该页定位，不影响其他主张
            st.data["limits"].append(
                f"主张 {record.get('claim_id')} 段落定位模型调用失败"
                f"（{type(exc).__name__}），该页未比对")
            return ""
        obj = out[0] if isinstance(out, tuple) and len(out) == 2 \
            else (out if isinstance(out, dict) else None)
        quote = normalize_text(obj.get("quote")) if isinstance(obj, dict) else ""
        if not quote:
            st.error("evidence", "quote_unavailable",
                     f"主张 {record.get('claim_id')} 未取得可定位段落")
            return ""
        entry["ok"] = True
        return quote

    @staticmethod
    def _apply_verdict(record: Dict[str, Any], found: Mapping[str, Any]) -> None:
        """把一次比对结果写进主张记录（判语/来源数字/引文/依据）。"""
        for key in ("verdict", "source_number", "source_url", "source_title",
                    "quote", "as_of", "basis"):
            record[key] = found[key]

    # -- 阶段 6：汇总（代码模板即报告；可选受约束补充说明） -------------------

    def _summarize(self, st: _RunState) -> None:
        """阶段6 汇总：默认代码模板 = render_report 的确定性纯文本报告；llm_fn
        注入且有数字主张时才生成一段 ≤100 字受约束补充说明（越界丢弃退模板），
        只追加进报告末尾，不进 data（data 键锁定，不设 summary）。"""
        st.note = self._constrained_note(st)
        self._update_counts(st)

    def _verdict_context(self, st: _RunState) -> str:
        """代码判定上下文（喂给受约束补充说明的"上面代码已给出的判定"）。"""
        lines: List[str] = []
        for claim in st.data["claims"]:
            lines.append(f"{claim['claim_id']}「{claim['excerpt']}」"
                         f"数字 {claim['number']}{claim['unit']} → "
                         f"{VERDICT_LABELS.get(claim['verdict'], claim['verdict'])}；"
                         f"{claim['basis']}")
            if claim["source_url"]:
                lines.append(f"  来源：《{claim['source_title']}》{claim['source_url']}"
                             f"｜来源数字：{claim['source_number']}")
        return "\n".join(lines)

    def _constrained_note(self, st: _RunState) -> str:
        """可选的一段 ≤100 字受约束补充说明（authority/adjudicate 同族校验）。

        llm_fn 未注入 / 无数字主张 / 预算到顶 / 取消 / 输出越界（真假判定词、
        URL、判定性表述、超 100 字）→ 一律丢弃并退回纯模板报告，主结论不受影响。
        """
        if self.llm_fn is None or not st.data["claims"]:
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
                              "代码判定：\n" + self._verdict_context(st))
        except Exception as exc:  # noqa: BLE001  模型异常只影响补充说明，不影响主结论
            st.data["limits"].append(f"模型补充说明未生成（{type(exc).__name__}），"
                                     "报告以代码模板为准")
            return ""
        obj = out[0] if isinstance(out, tuple) and len(out) == 2 \
            else (out if isinstance(out, dict) else None)
        note = _clean(obj.get("note") if isinstance(obj, dict) else "", 200)
        if not note:
            st.data["limits"].append("模型补充说明为空或形状不符，报告以代码模板为准")
            return ""
        if len(note) > 100:
            st.data["limits"].append("模型补充说明超过 100 字，已丢弃")
            return ""
        if _VERDICT_RE.search(note) or _URL_RE.search(note):
            st.data["limits"].append("模型补充说明含 URL 或真假判定词，已丢弃")
            return ""
        if any(word in note for word in _NOTE_BANNED):
            st.data["limits"].append("模型补充说明含判定性表述，已丢弃")
            return ""
        entry["ok"] = True
        return note

    # -- 收尾 ---------------------------------------------------------------

    def _update_counts(self, st: _RunState) -> None:
        """counts（键名锁定）：主张数 + 预算实耗（与 authority 同口径）。"""
        used = st.budget.used
        st.data["counts"] = {
            "claims": len(st.data["claims"]),
            "searches": int(used.get("max_searches") or 0),
            "pages": int(used.get("max_pages") or 0),
            "llm_calls": int(used.get("max_llm_calls") or 0),
        }

    def _progress(self, st: _RunState, stage: str, done: int) -> None:
        hook = self._on_progress
        if hook is None:
            return
        try:
            hook(stage, done, len(_PROGRESS_STAGES))
        except Exception:  # noqa: BLE001  进度回调异常不影响核查结果本身
            pass

    def _finish(self, st: _RunState, started_at: str) -> Dict[str, Any]:
        task_status = "completed"
        if st.clarified:
            task_status = "needs_clarification"
        elif st.no_factual:
            task_status = "no_factual_claim"
        status = self._resolve_status(st, task_status)
        budget = st.budget.snapshot()
        result = {
            "tool_id": "check_statistics",
            "status": status,
            "task_status": task_status,
            "data": st.data,
            "gaps": list(st.gaps),
            "errors": list(st.errors),
            # 计数取预算实耗（与 authority/research 同口径）：operations 行的 ok
            # 是事后回填的，audit 只按创建时点计数，不可靠。
            "audit": {"operations": list(st.audit["operations"]),
                      "searches": int(budget["used"]["max_searches"]),
                      "pages": int(budget["used"]["max_pages"]),
                      "llm_calls": int(budget["used"]["max_llm_calls"])},
            "budget": budget,
            "report": "",
            "started_at": started_at,
            "finished_at": self.clock(),
        }
        if st.clarified:
            result["gaps"].insert(0, f"需要用户补充：{st.clarify_reason or '一段含数字的说法'}")
        if st.is_cancelled():
            result["gaps"].insert(0, "运行被取消，已保留此前取得的主张与比对结果")
        if st.budget.blocked_reason:
            result["gaps"].insert(0, f"预算/死线到顶提前停止（{st.budget.blocked_reason}），"
                                     "已保留此前取得的比对结果")
        result["report"] = render_report(result)
        if st.note:
            # 受约束补充说明只追加进报告末尾（data 键锁定，不设 summary 字段）
            result["report"] += ("\n补充（受约束模型说明，只转述代码判定）："
                                 + _report_field(st.note, 100))
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
            # 原文没有数字表达不是失败：连"没有可比对数字"的缺口都不记成 partial
            return "completed"
        if st.errors or st.gaps:
            return "partial"
        return "completed"

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
        except Exception as exc:  # noqa: BLE001  模型异常按"本次调用失败"处理
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
        """一次检索。返回 None=检索不可用/降级（已记 error+gap），[]=合法空结果。"""
        if self.search_fn is None:
            st.error(stage, "search_unavailable", "未注入检索实现，检索未执行")
            st.gap("检索未执行（未配置检索实现），未发现来源页面")
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
        except Exception as exc:  # noqa: BLE001  检索侧异常按降级处理
            st.error(stage, "search_unavailable", f"检索异常：{type(exc).__name__}")
            st.gap("检索未执行/降级，未发现来源页面")
            return None
        if not isinstance(result, dict):
            st.error(stage, "search_unavailable", "检索返回形状不符")
            st.gap("检索未执行/降级，未发现来源页面")
            return None
        degraded = bool(result.get("degraded"))
        err = str(result.get("error") or "").strip()
        results = result.get("results")
        results = list(results) if isinstance(results, list) else []
        if degraded or err:
            # "没搜/搜降级"与"搜过了没找到"必须分开记（外发授权 fail-closed：
            # 未授权零外发，且绝不把"没搜"说成"搜过了没找到"）
            st.error(stage, "search_unavailable",
                     err or "检索降级（外部检索未授权或后端故障）")
            st.gap("检索未执行/降级，未发现来源页面")
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
        """一次读页（带运行内缓存：同一地址不重复抓、不重复扣预算）。"""
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
        except Exception as exc:  # noqa: BLE001  取页异常按失败记
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


def _code_claim(candidate: Mapping[str, Any], index: int) -> Dict[str, Any]:
    """代码抽取候选 → claim 内部记录（无 LLM 结构化时的确定性兜底）。"""
    record = dict(candidate)
    record.update({
        "claim_id": f"C{index}",
        "metric": "",
        "qualifiers": {key: "" for key in _QUALIFIER_KEYS},
        "verdict": "unverifiable", "source_number": "", "source_url": "",
        "source_title": "", "quote": "", "as_of": "",
        "basis": "未定位到含可比数字的来源段落，未作数值比对",
    })
    return record


def _public_claim(record: Mapping[str, Any]) -> Dict[str, Any]:
    """内部记录 → 对外 claim（键名锁定，见 _CLAIM_KEYS；内部归一字段不外泄）。"""
    qualifiers = record.get("qualifiers")
    qualifiers = qualifiers if isinstance(qualifiers, dict) else {}
    return {
        "claim_id": str(record.get("claim_id") or ""),
        "excerpt": str(record.get("excerpt") or ""),
        "number": record.get("number"),
        "unit": str(record.get("unit") or ""),
        "metric": str(record.get("metric") or ""),
        "qualifiers": {key: str(qualifiers.get(key) or "")
                       for key in _QUALIFIER_KEYS},
        "verdict": str(record.get("verdict") or "unverifiable"),
        "source_number": str(record.get("source_number") or ""),
        "source_url": str(record.get("source_url") or ""),
        "source_title": str(record.get("source_title") or ""),
        "quote": str(record.get("quote") or ""),
        "as_of": str(record.get("as_of") or ""),
        "basis": str(record.get("basis") or ""),
    }


def check_statistics(text: str, **kw) -> Dict[str, Any]:
    """便捷函数 = StatisticsSkills(**kw).run(text)。工具层用它。"""
    return StatisticsSkills(**kw).run(text)


# ---------------------------------------------------------------------------
# 确定性纯文本报告
# ---------------------------------------------------------------------------

def _report_field(value: Any, limit: int = 160) -> str:
    """非引文字段的渲染：去控制字符、把 HTML/markdown 标记字符换成全角等价物、压空白、截断。

    为什么换全角而不是删掉：报告是确定性纯文本，标题/URL/说明里混进 `<script>` 或
    `[x](url)` 就有注入 GUI 的风险；换成全角等价物既保留信息量，又让标记语法失效。
    引文不走本函数——引文要逐字保留。
    """
    text = normalize_text(value)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    for raw, safe in (("<", "＜"), (">", "＞"), ("[", "［"), ("]", "］")):
        text = text.replace(raw, safe)
    return text[:limit]


def _report_quote(value: Any, limit: int) -> str:
    """引文渲染：逐字保留（引文必须可回原文核对），只去控制字符并压空白。"""
    text = normalize_text(value)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    return text[:limit]


def render_report(result: Dict[str, Any]) -> str:
    """把一次核查结果渲染成确定性纯文本报告（无 HTML、无 markdown 链接语法）。

    结构：执行状态 → 逐主张口径比对（数字/四态/来源/引文）→ 限制 → 缺口 → 错误 →
    预算。全文不出现固定真假判定词；四态判语只描述数字口径，不描述内容真假。
    """
    if not isinstance(result, dict):
        return "统计说法核查报告：（结果形状不符，无可渲染内容）"
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    budget = result.get("budget") if isinstance(result.get("budget"), dict) else {}
    used = budget.get("used") if isinstance(budget.get("used"), dict) else {}
    limits_budget = budget.get("limits") if isinstance(budget.get("limits"), dict) else {}
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}

    lines: List[str] = ["统计说法核查报告（只比对数字口径，不判内容真假）"]
    lines.append(f"执行状态：{_report_field(result.get('status'), 40) or '（未知）'}"
                 f"　任务状态：{_report_field(result.get('task_status'), 40) or '（未知）'}")
    lines.append(f"开始：{_report_field(result.get('started_at'), 40)}"
                 f"　结束：{_report_field(result.get('finished_at'), 40)}")
    lines.append(f"输入：{_report_field(data.get('text'), 300) or '（未提供）'}")

    claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
    lines.append("")
    lines.append(f"一、数字主张（共 {len(claims)} 条）")
    if not claims:
        lines.append("  （没有可核对的数字表达）")
    for item in claims:
        qualifiers = item.get("qualifiers") if isinstance(item.get("qualifiers"), dict) else {}
        scope = "；".join(f"{label}：{qualifiers.get(key) or '未注明'}"
                          for label, key in (("时间", "time"), ("地点", "place"),
                                             ("实体", "entity"), ("基数", "base"))
                          if qualifiers.get(key))
        lines.append(f"  {_report_field(item.get('claim_id'), 12)} 「"
                     f"{_report_quote(item.get('excerpt'), 200)}」"
                     f"数字 {_report_field(item.get('number'), 20)}"
                     f"{_report_field(item.get('unit'), 20)}"
                     f"｜指标：{_report_field(item.get('metric'), 60) or '未注明'}")
        if scope:
            lines.append(f"      限定：{_report_field(scope, 200)}")
        verdict = str(item.get("verdict") or "unverifiable")
        lines.append(f"      口径比对：{_report_field(verdict, 20)}"
                     f"（{_report_field(VERDICT_LABELS.get(verdict, verdict), 20)}）"
                     f"｜依据：{_report_field(item.get('basis'), 200) or '（无）'}")
        lines.append(f"      来源数字：{_report_field(item.get('source_number'), 40) or '（无）'}"
                     f"｜来源：《{_report_field(item.get('source_title'), 80)}》"
                     f"{_report_field(item.get('source_url'), 200) or '（无）'}"
                     f"｜as_of：{_report_field(item.get('as_of'), 40) or '未注明'}")
        quote = _report_quote(item.get("quote"), int(DEFAULT_STATISTICS_RULES["max_quote_chars"]))
        lines.append(f"      段落：「{quote}」" if quote else "      段落：（未定位到）")

    limits = [_report_field(x, 200) for x in (data.get("limits") or []) if x]
    lines.append("")
    lines.append(f"二、限制（共 {len(limits)} 条）")
    if not limits:
        lines.append("  （无）")
    for index, text in enumerate(limits, start=1):
        lines.append(f"  {index}. {text}")

    gaps = [_report_field(g, 240) for g in (result.get("gaps") or [])]
    lines.append("")
    lines.append(f"三、缺口（共 {len(gaps)} 条）")
    if not gaps:
        lines.append("  （无）")
    for index, text in enumerate(gaps, start=1):
        lines.append(f"  {index}. {text}")

    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    lines.append("")
    lines.append(f"四、错误（共 {len(errors)} 条）")
    if not errors:
        lines.append("  （无）")
    for item in errors:
        lines.append(f"  - [{_report_field(item.get('stage'), 20)}] "
                     f"{_report_field(item.get('category'), 40)}："
                     f"{_report_field(item.get('detail'), 200)}")

    lines.append("")
    lines.append("五、预算与可观测")
    lines.append("  " + render_counts_line(counts))
    lines.append(f"  主张 {int(counts.get('claims') or 0)}"
                 f"/{limits_budget.get('max_claims', '-')}"
                 f"；检索 {int(counts.get('searches') or 0)}"
                 f"/{limits_budget.get('max_searches', '-')}"
                 f"；读页 {int(counts.get('pages') or 0)}"
                 f"/{limits_budget.get('max_pages', '-')}"
                 f"；模型调用 {int(counts.get('llm_calls') or 0)}"
                 f"/{limits_budget.get('max_llm_calls', '-')}")
    if result.get("status") in ("cancelled", "budget_exhausted"):
        lines.append("  本次提前停止，上表为停止时点的真实消耗。")
    lines.append("口径边界：本报告只比对数字与单位口径，不判内容真假；"
                 "「未核实到」只表示本次没有定位到可比对的来源数字。"
                 "要判断说法本身能否被权威资料证明/反驳，请走 "
                 "verify_claim / verify_authority。")
    return "\n".join(lines)
