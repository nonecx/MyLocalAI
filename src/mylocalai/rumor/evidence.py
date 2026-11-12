# -*- coding: utf-8 -*-
"""证据本：围绕文本主张搜索相关事件，把搜到的资料整理成可追溯的证据，并给出证据倾向。

流程（collect_evidence）：
  1. 取主张 —— 优先用 claims 组件提取的第一条事实性声明构造查询词；无事实性声明
     时不搜索（没有可核的东西，证据本记 skipped）。
  2. 第一轮中性搜索 "{查询词}"，了解事件的全貌。
  3. 若第一轮没有任何可靠来源反驳，自动补搜一轮 "{查询词} 辟谣"——谣言识别场景里
     定向找辟谣信息往往一击即中（隔夜茶致癌、微波炉致癌都有现成辟谣）。
  4. 对可靠级别且摘要太短的条目机会式抓全文（fetch 失败不影响证据成立）。
  5. 立场判定：一次 LLM 调用批量判断每条证据对主张的态度（反驳/支持/无关）；
     LLM 失败时回退到关键词规则（可靠来源 + 标题/摘要含"辟谣/不实"→ 反驳）。
  6. 汇总：可靠反驳数（按独立域名去重）、可靠支持数、证据倾向 hint。

安全与工程约定与包内其他模块一致：
  - 不把待测原文交给搜索引擎或立场判定 LLM——只用从原文提取出的主张短句。
  - 全部失败不抛异常，返回带 error 的结构，由 verdict 记为降级组件。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from mylocalai.retrieval.search import fetch_article, search_all
from mylocalai.retrieval.sources import RELIABLE_TIERS, TIER_UNKNOWN, classify_domain, normalize_host, source_kind

STANCES = ("反驳", "支持", "无关")
REFUTE_WORDS = ("辟谣", "谣言", "不实", "假的", "别再传", "误导", "并不属实", "无稽之谈", "不正确")
SUPPORT_WORDS = ("属实", "证实", "确有其事", "确有此事")
_MAX_QUERY_CHARS = 24          # 查询词截断：太长会稀释搜索结果
_SNIPPET_FOR_LLM = 180         # 送入立场判定的摘要长度
_LLM_FULL_TEXT = 400           # 送入立场判定的全文长度
_STANCE_BATCH = 5              # 立场判定分批大小：条数太多模型容易数错

STANCE_SYSTEM_PROMPT = (
    "你是事实核查助理。用户会给出一条待核主张和若干条搜索到的资料（按[序号]列出）。"
    "请对每条资料判断它对待核主张的立场，只能取：反驳/支持/无关。\n"
    "判定锚点：资料直接指出主张内容是谣言、不实信息、存在误解→反驳；"
    "资料确认主张描述的事实属实→支持；资料只是同主题讨论、广告、词典页、"
    "或辟的是另一件事（对象/事件与主张不同）→无关。资料本身来自辟谣平台或标题含\"辟谣\""
    "不自动等于反驳，仍要看它辟的内容是否就是待核主张。\n"
    '请严格按照以下JSON格式输出，键名必须是 stances，数组按资料序号顺序、长度必须等于资料条数：\n'
    '{"stances": ["反驳", "支持", "无关"]}'
)


# ---------------------------------------------------------------------------
# 查询词构造
# ---------------------------------------------------------------------------

def build_query(claim: str) -> str:
    """从主张短句构造搜索查询：去转发话术/表情/空白，截断到 24 字。"""
    q = re.sub(r"[\s\r\n]+", " ", str(claim or "")).strip()
    q = re.sub(r"(转发|扩散|速转|紧急|震惊|赶紧|家人们|朋友们)[，,。！! ]*", "", q)
    q = re.sub(r"[#【】《》\"'“”‘’…·|]+", " ", q).strip()
    return q[:_MAX_QUERY_CHARS].strip()


# ---------------------------------------------------------------------------
# 立场判定：LLM 分批 + 规则回退
# ---------------------------------------------------------------------------

def _has_topic_overlap(claim: str, hay: str) -> bool:
    """主张与资料是否有实质性内容重叠。

    以主张的 2 字滑窗片段在标题+摘要中的命中情况衡量：
      - 命中片段按位置合并成组，不重叠的组 ≥2（命中了两个不同的关键词），或
      - 命中片段总数 ≥4（主张整句基本被资料包含）
    只命中单个词（如"图书馆"给出相邻的"图书"+"书馆"）不算重叠——那只是同主题。
    """
    claim, hay = claim or "", hay or ""
    groups, in_group, hits = 0, False, 0
    for i in range(len(claim) - 1):
        if claim[i:i + 2] in hay:
            hits += 1
            if not in_group:
                groups += 1
            in_group = True
        else:
            in_group = False
    return groups >= 2 or hits >= 4


def rule_stance(claim: str, title: str, snippet: str, domain_tier: str) -> str:
    """关键词规则：可靠来源 + 辟谣词汇 + 与主张有实质内容重叠 → 反驳/支持。

    只作为 LLM 失败时的回退。重叠门槛来自实测教训：补搜"辟谣"会捞来很多
    同主题但辟的是另一件事的文章（如查"图书馆讲座"搜出国图收费报告会辟谣），
    只认"可靠+辟谣词"会把它们误判成反驳。
    """
    hay = f"{title} {snippet}"
    if domain_tier not in RELIABLE_TIERS or not _has_topic_overlap(claim, hay):
        return "无关"
    if any(w in hay for w in REFUTE_WORDS):
        return "反驳"
    if any(w in hay for w in SUPPORT_WORDS):
        return "支持"
    return "无关"


# ---------------------------------------------------------------------------
# 检索范围：具体事件 vs 泛现象
#
# 规则（来自用户要求）：主张指向**具体事件**时，只有明确在讲该事件的评论和报道才能
# 进证据本——同主题但谈的是别处同类事件/泛泛现象的文章一律不收；主张指向**泛现象**
# （隔夜茶致癌、晚上吃姜等于吃砒霜这类）时没有这个限制。
#
# 判定顺序是"规则优先、只在真需要时问大模型"：抽不到任何具体指向的按泛现象处理，
# 直接跳过模型调用；抽到时间/地点/事件词才认定为事件，再让模型确认一次并给出事件概述。
# ---------------------------------------------------------------------------

SCOPE_EVENT = "event"
SCOPE_PHENOMENON = "phenomenon"

_EVENT_HINT = re.compile(
    r"(事件|事故|案件|通报|发布会|发生后|现场|投毒|下毒|中毒|污染|爆炸|起火|坍塌|泄漏|食物中毒)"
)
# 时间锚点分强弱：给了年月的才可能指向一个可指认的事件；"最近/近日"这类模糊时间
# 谁都能用，不足以定位事件，只作弱锚点（弱锚点不参与"是不是同一件事"的判定）
_TIME_STRONG = re.compile(r"(\d{4}\s*年\s*\d{1,2}\s*月)|(\d{1,2}\s*月\s*\d{1,2}\s*日)"
                          r"|(今年|去年|前年|本月|上月|这个月|上个月|昨天|前天|今天)")
_TIME_WEAK = re.compile(r"(最近|近日|近期|这几天|这几天里|上周|本周|这周|前几天)")
_PLACE_SUFFIXES = ("街道", "小区", "社区", "集团", "机场", "车站", "景区", "大学", "中学", "小学",
                   "学校", "医院", "公司", "工厂", "省", "市", "县", "区", "镇", "乡", "村", "厂")
_PLACE_ANCHOR = re.compile(r"([\u4e00-\u9fa5]{2,7}(?:" + "|".join(_PLACE_SUFFIXES) + r"))")
# 命中的字符串可能把前面的动词/时间词一起吞进来（"听说咱们小区"），从左侧剥掉这些前缀
_LEAD_TOKENS = ("听说", "网传", "据说", "传闻", "有人", "大家都说", "据说", "下午", "上午",
                "晚上", "早上", "中午", "半夜", "凌晨")
_GENERIC_NAME = {"咱们", "我们", "你们", "他们", "我", "你", "他", "本", "这家", "那家", "某",
                 "一个", "这个", "那个", "当地", "附近", "这里", "这边", "楼上", "楼下"}
_NAME_STOPCHARS = set("在的了是到把被和与上下中里这那着过很都也就还")
# 名字里带这些词说明是"听说/网传"带出来的泛指对象，无法据以指认具体事件
_GENERIC_TOKENS = ("听说", "网传", "据说", "朋友", "同事", "邻居", "亲戚", "家人", "别人",
                   "有人", "大家", "某个", "某些")


def _trim_lead(name: str) -> str:
    """剥掉地名前面的动词/时段前缀（"听说咱们小区"→"咱们小区"，"下午北京朝阳区"→"北京朝阳区"）。"""
    out = name
    for _ in range(3):
        before = out
        for t in _LEAD_TOKENS:
            if out.startswith(t) and len(out) > len(t):
                out = out[len(t):]
        while out and out[0] in _NAME_STOPCHARS:
            out = out[1:]
        if out == before:
            break
    return out


def _extract_anchors(claim: str) -> List[Dict[str, str]]:
    """从主张里抽出可用来判断"是不是同一个事件"的锚点。

    strength=strong 表示锚点带专名（杭州余杭、济南、北京朝阳区）或明确年月，可以据它在
    资料里找到同一个事件；weak 表示只有"咱们小区/我们学校/最近"这类泛指，无法据以指认。
    """
    text = str(claim or "")
    out: List[Dict[str, str]] = []
    seen = set()
    for m in _TIME_STRONG.finditer(text):
        key = m.group(0).strip()
        if key and key not in seen:
            seen.add(key)
            out.append({"text": key, "kind": "时间", "strength": "strong"})
    for m in _TIME_WEAK.finditer(text):
        key = m.group(0).strip()
        if key and key not in seen:
            seen.add(key)
            out.append({"text": key, "kind": "时间", "strength": "weak"})
    # 地名扫描前先把时间锚点从文本里挖掉，否则贪婪匹配会把"昨天下午北京朝阳区"
    # 切成"天下午北京朝阳区"，前缀剥离认不出来（实测踩过）
    place_text = text
    for a in out:
        place_text = place_text.replace(a["text"], " " * len(a["text"]))
    for m in _PLACE_ANCHOR.finditer(place_text):
        full = _trim_lead(m.group(1))
        suffix = next((s for s in _PLACE_SUFFIXES if full.endswith(s)), "")
        name = full[: -len(suffix)] if suffix else full
        strong = ("strong" if len(name) >= 2 and name not in _GENERIC_NAME
                  and not (set(name) & _NAME_STOPCHARS)
                  and not any(t in name for t in _GENERIC_TOKENS) else "weak")
        if full and full not in seen:
            seen.add(full)
            out.append({"text": full, "kind": "地点/机构", "strength": strong})
    return out


SCOPE_SYSTEM_PROMPT = (
    "你是事实核查助理。请判断用户给出的待核主张指向的是\"某个具体事件\"还是\"某种泛泛的说法/现象\"。\n"
    "具体事件＝可以指认时间、地点、单位或人物（例如某地某时发生的投毒、某机构的某次通报）；"
    "泛现象＝对一类事物的普遍说法，时间地点都不特定（例如隔夜茶致癌、晚上吃姜等于吃砒霜、5G 辐射有害）。\n"
    "字段要求：\n"
    "1. scope：只能取 event（具体事件）或 phenomenon（泛现象）。\n"
    "2. event：scope 为 event 时给出该事件的一句话概述，不超过40字；否则空字符串。\n"
    "3. why：一句话说明判断依据。\n"
    '请严格按照以下JSON格式输出，不要输出任何其他内容：\n'
    '{"scope": "event", "event": "...", "why": "..."}'
)

EVENT_RELEVANCE_SYSTEM_PROMPT = (
    "你是事实核查助理。用户会给出一个具体事件（含时间/地点/主体）和若干条搜索到的资料（按[序号]列出）。"
    "请逐条判断该资料是否**明确在讲这个事件**。\n"
    "判定锚点：资料提到该事件的时间、地点、单位或人物，讲的确实是同一件事→该事件；"
    "资料只是讨论同类现象、讲的是别处发生的同类事件、或者与该事件无关→非该事件。"
    "只讨论同一类现象（例如都在说自来水安全）而无法确认是同一个事件的，一律判非该事件。\n"
    '请严格按照以下JSON格式输出，matches 数组长度必须等于资料条数，元素只能取\"该事件\"或\"非该事件\"：\n'
    '{"matches": ["该事件", "非该事件"]}'
)


def _tolerant_list(obj: Any) -> Optional[list]:
    """从模型返回里取唯一一个列表值（容忍键名写歪，如实测出现过的 "_instances"）。"""
    if not isinstance(obj, dict):
        return None
    for key in ("stances", "matches", "clusters", "items"):
        if isinstance(obj.get(key), list):
            return obj[key]
    if len(obj) == 1:
        only = next(iter(obj.values()))
        if isinstance(only, list):
            return only
    return None


def classify_scope(text: str, claim: str = "", llm: bool = True) -> Dict[str, Any]:
    """判断主张指向具体事件还是泛现象：{"scope","event","anchors","method","reason"}。

    规则优先：抽不到时间/地点/事件词 → 泛现象（不调用大模型）；抽到 → 具体事件，
    启用大模型时再让模型确认一次，模型失败就沿用规则结果。
    """
    base = str(claim or "").strip() or build_query(text)
    probe = f"{base} {str(text or '')[:120]}"
    anchors = _extract_anchors(probe)
    if not anchors and not _EVENT_HINT.search(probe):
        return {"scope": SCOPE_PHENOMENON, "event": "", "anchors": [], "method": "rule",
                "reason": "主张未指向具体时间/地点/事件，按泛现象处理（不做事件范围过滤）"}

    result: Dict[str, Any] = {
        "scope": SCOPE_EVENT, "event": base[:80], "anchors": anchors, "method": "rule",
        "reason": "主张含时间/地点/事件等具体指向，按具体事件处理（只收录明确谈该事件的资料）",
    }
    if not llm:
        return result
    from mylocalai.llm.llm_client import chat_json

    obj, err = chat_json(SCOPE_SYSTEM_PROMPT, f"待核主张：{base}\n文本片段：{str(text or '')[:200]}",
                         temperature=0.0, max_tokens=256)
    if isinstance(obj, dict):
        scope = str(obj.get("scope") or "").strip().lower()
        if scope in (SCOPE_EVENT, SCOPE_PHENOMENON):
            result["scope"] = scope
            result["method"] = "llm"
            result["reason"] = str(obj.get("why") or result["reason"])[:120]
            if scope == SCOPE_EVENT:
                result["event"] = str(obj.get("event") or base)[:80] or base[:80]
            else:
                result["event"] = ""
    elif err:
        result["llm_error"] = err
    return result


def _rule_event_relevance(anchors: List[Dict[str, str]], entry: Dict[str, Any]) -> str:
    """规则判定资料是否明确在讲该事件：命中强锚点才算，弱锚点（咱们小区）不算。"""
    hay = f"{entry.get('title', '')} {entry.get('snippet', '')} {entry.get('full_text') or ''}"
    strong = [a["text"] for a in anchors if a.get("strength") == "strong"]
    return "该事件" if any(a in hay for a in strong) else "非该事件"


def judge_event_relevance(scope: Dict[str, Any], entries: List[Dict[str, Any]]) -> Tuple[Optional[List[Optional[str]]], Optional[str]]:
    """分批让模型判断每条资料是否明确在讲该事件。返回 (逐条判定或 None, 错误)。"""
    from mylocalai.llm.llm_client import chat_json

    anchors = scope.get("anchors") or []
    event_desc = str(scope.get("event") or "")
    anchor_desc = "、".join(f"{a['text']}({a['kind']}{'·专名' if a['strength'] == 'strong' else '·泛指'})"
                            for a in anchors) or "（无）"
    matches: List[Optional[str]] = [None] * len(entries)
    errors: List[str] = []
    for start in range(0, len(entries), _STANCE_BATCH):
        batch = entries[start:start + _STANCE_BATCH]
        lines = []
        for i, e in enumerate(batch):
            body = e.get("snippet") or e.get("full_text") or ""
            lines.append(f"[{i}] 来源等级:{e.get('domain_tier', TIER_UNKNOWN)} "
                         f"标题:{e.get('title', '')[:60]} 内容:{body[:_SNIPPET_FOR_LLM]}")
        user = (f"待核事件：{event_desc}\n可指认线索：{anchor_desc}\n资料：\n" + "\n".join(lines))
        obj, err = chat_json(EVENT_RELEVANCE_SYSTEM_PROMPT, user, temperature=0.0, max_tokens=384)
        vals = _tolerant_list(obj)
        if not isinstance(vals, list) or len(vals) != len(batch):
            errors.append(err or "matches数量与资料条数不符")
            continue
        for j, v in enumerate(vals):
            v = str(v).strip()
            matches[start + j] = v if v in ("该事件", "非该事件") else "非该事件"
    if all(m is None for m in matches):
        return None, "；".join(errors)[:120] or "全部批次失败"
    return matches, ("；".join(errors)[:120] if errors else None)


def filter_by_event(scope: Dict[str, Any], entries: List[Dict[str, Any]],
                    llm: bool = True) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], Optional[str]]:
    """事件范围过滤：只放行"明确为该事件"的条目，其余连同原因记入 excluded。"""
    if not entries:
        return [], [], None
    anchors = scope.get("anchors") or []
    matches, err = (None, None)
    if llm:
        matches, err = judge_event_relevance(scope, entries)
    if matches is None:
        matches = [_rule_event_relevance(anchors, e) for e in entries]
    kept: List[Dict[str, Any]] = []
    excluded: List[Dict[str, str]] = []
    for e, m in zip(entries, matches):
        if m == "该事件":
            kept.append(e)
        else:
            excluded.append({"url": e.get("url", ""), "title": (e.get("title") or "")[:60],
                             "relevance": m or "非该事件",
                             "reason": "同主题但非该事件，按事件范围排除"})
    return kept, excluded, err



def judge_stances_llm(claim: str, entries: List[Dict[str, Any]]) -> Tuple[Optional[List[Optional[str]]], Optional[str]]:
    """分批判定所有证据的立场（每批 _STANCE_BATCH 条）。

    返回 (每条立场或 None 的列表, 错误)。整体失败为 (None, 错误)；部分批次失败时
    对应条目为 None、错误说明拼接返回，由调用方按规则回退补齐。
    条数多时一次全判容易数错 stances 数量（实测 10 条时翻车），分批显著更稳。
    """
    from mylocalai.llm.llm_client import chat_json

    stances: List[Optional[str]] = [None] * len(entries)
    errors: List[str] = []
    for start in range(0, len(entries), _STANCE_BATCH):
        batch = entries[start:start + _STANCE_BATCH]
        lines = []
        for i, e in enumerate(batch):
            body = e.get("snippet") or ""
            if not body and e.get("full_text"):
                body = e["full_text"]
            lines.append(f"[{i}] 来源等级:{e.get('domain_tier', TIER_UNKNOWN)} "
                         f"标题:{e.get('title', '')[:60]} 内容:{body[:_SNIPPET_FOR_LLM]}")
        user = f"待核主张：{claim[:120]}\n资料：\n" + "\n".join(lines)
        obj, err = chat_json(STANCE_SYSTEM_PROMPT, user, temperature=0.0, max_tokens=512)
        vals = obj.get("stances") if isinstance(obj, dict) else None
        if vals is None and isinstance(obj, dict) and len(obj) == 1:
            # 模型偶尔会把键名写歪（实测出现过 "_instances"），内容正确时不应整批作废
            only = next(iter(obj.values()))
            if isinstance(only, list):
                vals = only
        if not isinstance(vals, list) or len(vals) != len(batch):
            errors.append(err or "stances数量与资料条数不符")
            continue
        for j, s in enumerate(vals):
            s = str(s).strip()
            stances[start + j] = s if s in STANCES else "无关"
    if all(s is None for s in stances):
        return None, "；".join(errors)[:120] or "全部批次失败"
    return stances, ("；".join(errors)[:120] if errors else None)


# ---------------------------------------------------------------------------
# 证据本主流程
# ---------------------------------------------------------------------------

def _entry_from_result(r: Dict[str, Any], query: str) -> Dict[str, Any]:
    return {
        "query": query, "engine": r.get("engine"), "title": r.get("title", ""),
        "url": r.get("url", ""), "snippet": r.get("snippet", ""),
        "date": r.get("date"), "domain_tier": r.get("domain_tier", TIER_UNKNOWN),
        "source_kind": source_kind(r.get("url", "")),
        "full_text": r.get("full_text"), "stance": "", "stance_source": "",
    }


def _profile_host(key: str) -> str:
    """信源画像键的域名归一（与条目侧 normalize_host 同口径比对）。

    画像键通常来自 qualified_source_profile，是裸域名（如 "rumor-mill.example.com"）；
    normalize_host 按 URL 解析对裸域名返回空串（urlparse 需要 scheme），故与
    classify_domain 同一判别：带 scheme/路径的走 normalize_host，裸域名按主机名
    规整（小写、去尾点、去 www.）。
    """
    key = str(key or "").strip()
    host = normalize_host(key) if ("/" in key or ":" in key) else key.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def collect_evidence(
    text: str,
    claims: Optional[Dict[str, Any]] = None,
    markers: Optional[Dict[str, Any]] = None,
    search_fn: Optional[Callable[[str, int], Dict[str, Any]]] = None,
    fetch_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    llm_judge: bool = True,
    source_profile: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict[str, Any]:
    """搜索并整理证据本。text 仅用于在 claims 未提供时做兜底，不发给搜索引擎。

    search_fn/fetch_fn 默认在调用时解析为模块级 search_all/fetch_article，
    便于测试与部署替换检索实现。

    source_profile（L4 参谋，可选）：档案库沉淀的信源画像，形状
    {域名: {"refutes": 反驳档案数}}。仅当传入**非空** dict 时生效：把"当前条目
    立场为反驳、域名（normalize_host 后）在画像里、且尚未计入可靠反驳"的域名
    并入 reliable 反驳集合，并写入 stance_summary 的 profile_refute_domains /
    profile_refute 两个键——个人档案库里反复辟谣同一类谣言的信源值得信任加权。
    不传 / 传 None / 传 {} 时不加任何键，输出与没有此参数时逐位一致
    （official 口径不受影响：画像域名只进 reliable 集合）。
    """
    search_fn = search_fn or search_all
    fetch_fn = fetch_fn or fetch_article
    book: Dict[str, Any] = {
        "queries": [], "entries": [], "claim": "", "skipped": None, "error": None,
        "scope": SCOPE_PHENOMENON, "scope_event": "", "scope_reason": "", "scope_anchors": [],
        "scope_method": "rule", "scope_filter": "", "excluded_not_event": [],
        "stance_summary": {"reliable_refute": 0, "reliable_refute_domains": [],
                           "official_refute": 0, "official_refute_domains": [],
                           "other_refute": 0, "reliable_support": 0, "irrelevant": 0,
                           "stance_source": ""},
        "filtered_out": 0, "verdict_hint": "无证据", "llm_error": None,
    }

    # 1. 主张：取 claims 组件的第一条事实性声明；无事实性声明时不搜索
    claim = ""
    if isinstance(claims, dict):
        claim_list = claims.get("claims") if isinstance(claims.get("claims"), list) else []
        if claim_list and isinstance(claim_list[0], dict):
            claim = str(claim_list[0].get("claim", "") or "")
        if claims.get("has_factual_claim") is False:
            book["skipped"] = "无可核实的事实性声明，未搜索"
            return book
    if not claim:
        claim = build_query(text)
    book["claim"] = claim
    query = build_query(claim)
    if not query:
        book["skipped"] = "无法构造搜索查询词"
        return book

    # 2. 第一轮中性搜索；若无任何可靠来源的反驳信号，自动补搜一轮"辟谣"
    seen_urls: set = set()
    rounds: List[List[Any]] = [[query, False]]      # [查询词, 是否补搜轮]
    i = 0
    while i < len(rounds):
        q, extra = rounds[i]
        i += 1
        try:
            result = search_fn(q, 6)
        except Exception as exc:  # noqa: BLE001
            book["error"] = f"搜索异常: {type(exc).__name__}"
            break
        if result.get("blocked"):
            book["error"] = "外部搜索未授权，未执行网络检索"
            book["search_status"] = "blocked"
            break
        book["queries"].append({"query": q, "extra": extra,
                                "engines": result.get("engines", {})})
        for r in result.get("results", []):
            if hasattr(r, "to_dict"):   # search_all 返回 SearchResult 对象；测试桩可能直接给 dict
                r = r.to_dict()
            url = (r.get("url") or "").rstrip("/")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            book["entries"].append(_entry_from_result(r, q))
        if not extra and not _has_rule_refute(book) and len(rounds) < 2:
            rounds.append([query + " 辟谣", True])

    if not book["entries"]:
        if book.get("search_status") == "blocked":
            book["verdict_hint"] = "外部搜索未授权，未执行核查"
            return book
        if book["error"] is None:
            book["error"] = "搜索无结果"
        book["verdict_hint"] = "无证据"
        return book

    # 3. 检索范围：主张指向具体事件时，只保留明确在讲该事件的资料（泛现象不做该过滤）
    scope = classify_scope(text, claim, llm=llm_judge)
    book["scope"] = scope["scope"]
    book["scope_event"] = scope.get("event", "")
    book["scope_reason"] = scope.get("reason", "")
    book["scope_anchors"] = [a["text"] for a in (scope.get("anchors") or [])]
    book["scope_method"] = scope.get("method", "rule")
    if scope.get("llm_error"):
        book["llm_error"] = scope["llm_error"]
    book["excluded_not_event"] = []
    strong = [a for a in (scope.get("anchors") or []) if a.get("strength") == "strong"]
    if scope["scope"] == SCOPE_EVENT and strong:
        # 有专名/明确年月这类可指认线索，才能据以判断"资料是不是在讲同一件事"
        kept_entries, excluded, ev_err = filter_by_event(scope, book["entries"], llm=llm_judge)
        book["entries"] = kept_entries
        book["excluded_not_event"] = excluded
        book["scope_filter"] = "已按事件范围过滤：只收明确为该事件的报道与评论"
        if ev_err and not book["llm_error"]:
            book["llm_error"] = f"事件范围判定: {ev_err}"
        if not book["entries"]:
            book["error"] = f"排除 {len(excluded)} 条非该事件的资料后没有可用证据"
            book["verdict_hint"] = "无证据"
            return book
    elif scope["scope"] == SCOPE_EVENT:
        # 是事件但没有可指认的专名（"咱们小区""我们学校"）——无法判断资料讲的是不是
        # 同一件事，硬过滤会把证据清空，因此退回只按主题相关性过滤，并如实标注
        book["scope_filter"] = "未做事件范围过滤：主张未给出可指认的时间/地点，无法判定是否同一事件"
    else:
        book["scope_filter"] = "未做事件范围过滤：主张针对的是泛现象"

    # 4. 机会式抓全文：可靠级别且摘要过短的条目
    for e in book["entries"]:
        if e["domain_tier"] in RELIABLE_TIERS and len(e.get("snippet") or "") < 40:
            try:
                art = fetch_fn(e["url"])
            except Exception:      # noqa: BLE001  抓取失败只是少一段全文
                art = None
            if art and art.get("text"):
                e["full_text"] = art["text"][:_LLM_FULL_TEXT]

    # 5. 立场判定：LLM 分批 → 失败的批次逐条回退规则
    llm_stances, err = (None, "未启用LLM判定") if not llm_judge else judge_stances_llm(claim, book["entries"])
    stances: List[str] = []
    srcs: List[str] = []
    if llm_stances is None:
        if err:
            book["llm_error"] = err
        stances = [rule_stance(claim, e["title"], e["snippet"] or (e.get("full_text") or ""),
                               e["domain_tier"]) for e in book["entries"]]
        srcs = ["rule"] * len(stances)
    else:
        if err:
            book["llm_error"] = err
        for s, e in zip(llm_stances, book["entries"]):
            if s is None:      # 该批次 LLM 失败，单条回退规则
                s = rule_stance(claim, e["title"], e["snippet"] or (e.get("full_text") or ""),
                                e["domain_tier"])
                srcs.append("rule")
            else:
                srcs.append("llm")
            stances.append(s)
    for e, s, sc in zip(book["entries"], stances, srcs):
        e["stance"], e["stance_source"] = s, sc

    # 5. 汇总（可靠来源按独立域名去重）
    s = book["stance_summary"]
    s["stance_source"] = ("llm" if all(x == "llm" for x in srcs)
                          else "rule" if all(x == "rule" for x in srcs) else "llm+rule")
    refutes = [e for e in book["entries"]
               if e["stance"] == "反驳" and e["domain_tier"] in RELIABLE_TIERS]
    refute_domains = {normalize_host(e["url"]) for e in refutes}
    official_domains = {normalize_host(e["url"]) for e in refutes
                        if e.get("source_kind", "").startswith("官方")}
    # L4 参谋：信源画像反哺抬分（最小侵入）。仅当传入非空画像时生效：把"当前条目
    # 立场为反驳、域名在画像里、且尚未计入可靠反驳"的域名并入 reliable 集合；
    # official 口径不受影响（画像域名只进 reliable 集合）。不传/传空时下面整段
    # 跳过、不加任何键，输出与原来逐位一致（tests/test_source_profile_lift.py 锁定）。
    profile_refute: set = set()
    if isinstance(source_profile, dict) and source_profile:
        profile_keys = {_profile_host(k) for k in source_profile}
        profile_keys.discard("")
        for e in book["entries"]:
            if e.get("stance") != "反驳":
                continue
            host = normalize_host(e.get("url") or "")
            if host and host in profile_keys and host not in refute_domains:
                profile_refute.add(host)
        refute_domains |= profile_refute
    s["reliable_refute"] = len(refute_domains)
    s["reliable_refute_domains"] = sorted(d for d in refute_domains if d)
    s["official_refute"] = len(official_domains)
    s["official_refute_domains"] = sorted(d for d in official_domains if d)
    s["other_refute"] = len(refute_domains - official_domains)
    if isinstance(source_profile, dict) and source_profile:
        s["profile_refute_domains"] = sorted(profile_refute)   # 滤空串：profile_refute 里不会有空串
        s["profile_refute"] = len(profile_refute)
    s["reliable_support"] = sum(1 for e in book["entries"]
                                if e["stance"] == "支持" and e["domain_tier"] in RELIABLE_TIERS)
    s["irrelevant"] = sum(1 for e in book["entries"] if e["stance"] == "无关")

    # 6. 信息过滤：证据本只保留与主张直接相关的内容——即"支持该说法"与"反对该说法"两类，
    #    "无关"条目（同主题科普、问答、广告、另一件事的辟谣）全部剔除，避免稀释判定。
    had_entries = bool(book["entries"])   # 先记下"原本搜到了条目"：下一行就覆盖掉了
    kept = [e for e in book["entries"] if e["stance"] in ("支持", "反驳")]
    book["filtered_out"] = len(book["entries"]) - len(kept)
    book["entries"] = kept
    # 全部条目都被判为"无关"而剔除 = 证据本等于空的：这属于取证失败的一种。
    # 不标出来的话，verdict 会按"没找到 ≠ 不是谣言"处理，置信度虚高
    # （2026-10-22 检验报告发现 F6：0 条证据仍是 confidence=高。此前判断写在
    #  覆盖 entries 之后恒为假，标记从未生效，2026-10-24 复核时发现并修正）。
    if had_entries and not kept:
        book["empty_after_filter"] = True

    if s["reliable_refute"] >= 1:
        extra = f"，其中官方来源 {s['official_refute']} 个" if s["official_refute"] else ""
        book["verdict_hint"] = f"已有可靠来源反驳（{s['reliable_refute']}个独立域名{extra}）"
    elif s["reliable_support"] >= 1:
        book["verdict_hint"] = "有可靠来源支持（主张可能属实，但仍需甄别）"
    elif kept:
        book["verdict_hint"] = "仅有非可靠来源表态，未形成可信定论"
    else:
        book["verdict_hint"] = "未找到可靠来源的直接定论"
    return book


def _has_rule_refute(book: Dict[str, Any]) -> bool:
    """是否已有可靠来源条目带反驳信号（用于决定是否补搜辟谣）。"""
    for e in book["entries"]:
        hay = f"{e.get('title', '')} {e.get('snippet', '')}"
        if e.get("domain_tier") in RELIABLE_TIERS and any(w in hay for w in REFUTE_WORDS):
            return True
    return False
