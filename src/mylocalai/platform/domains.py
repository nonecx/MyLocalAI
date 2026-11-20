# -*- coding: utf-8 -*-
"""平台领域表（platform/domains.py）：platform_domains.json → 领域查询 + LLM 领域判定。

与全项目"口径外置"文化一致（参照 attention/deep_read.py 的 load_rules 与
platform/rules.py 的 load_rules）：
  - 文件缺失 / JSON 写坏 / 形状不合法 → 退回内置默认（同 id 的 12 项精简版）并留
    中文告警，**既不崩、也不静默生效**；
  - `PLATFORM_DOMAINS_PATH` 整套替换、`PLATFORM_DOMAINS_EXTRA_PATH` 增量覆盖
    （经 config/config_loader.py 统一合并）；
  - 所有公开函数**永不抛异常**。

领域 id 与 src/mylocalai/assistant/skills/taxonomy.json 对齐（只读参考，本模块不
import 它）：换领域只改 JSON，代码不写死领域名。

取证文化与 rumor 包一致：**LLM 不可用就不猜领域**——classify_domain 在调用失败、
返回自造领域、返回空串时一律 None，由上层走 history/none 兜底，绝不按关键词拍一个。

已知边界（config_loader 的"列表取并集"语义）：增量文件（PLATFORM_DOMAINS_EXTRA_PATH）
只能**新增**领域 id，改不了已有领域的 definition/search_hints；要改请用
PLATFORM_DOMAINS_PATH 整套替换。同一 id 重复声明时先声明者生效
（内置默认 → 主文件 → 增量文件）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["DOMAINS_PATH", "load_domains", "domain_ids", "domain_name", "domain_hints",
           "classify_domain", "resolve_domain"]

DOMAINS_PATH: Path = Path(__file__).resolve().parent / "platform_domains.json"

#: 内置最小默认（与 platform_domains.json 同 id 的 12 项精简版；文件整体不可用时用它）。
#: 定义/示例是精简写法，search_hints 与 JSON 保持一致——坏配置退回时"定向检索"能力不丢。
DEFAULT_DOMAINS: List[Dict[str, Any]] = [
    {"id": "public_health", "name": "公共卫生",
     "definition": "疾病预防、疫苗接种、医疗服务与健康类传言。",
     "examples": ["疫苗", "传染病", "疫情防控"],
     "search_hints": ["nhc.gov.cn", "chinacdc.cn", "piyao.org.cn"]},
    {"id": "food_safety", "name": "食品安全",
     "definition": "食品生产、流通、餐饮与食品添加相关的安全传言。",
     "examples": ["食品添加剂", "隔夜菜", "农药残留"],
     "search_hints": ["samr.gov.cn", "piyao.org.cn", "gov.cn"]},
    {"id": "technology", "name": "科技",
     "definition": "数码产品、通信、人工智能与科学常识类传言。",
     "examples": ["5G", "辐射", "人工智能"],
     "search_hints": ["cas.cn", "gov.cn", "piyao.org.cn"]},
    {"id": "finance", "name": "财经",
     "definition": "金融市场、投资理财、企业经营与价格类信息。",
     "examples": ["股市", "理财", "汇率"],
     "search_hints": ["gov.cn", "cctv.com", "xinhuanet.com"]},
    {"id": "policy_law", "name": "政策法规",
     "definition": "法律法规、政府规章、司法案件与公共政策类信息。",
     "examples": ["新规", "处罚", "社保"],
     "search_hints": ["gov.cn", "people.com.cn", "12377.cn"]},
    {"id": "education", "name": "教育",
     "definition": "招生考试、学校管理、教师与学生类信息。",
     "examples": ["高考", "学区", "双减"],
     "search_hints": ["moe.gov.cn", "gov.cn", "people.com.cn"]},
    {"id": "social_events", "name": "社会事件",
     "definition": "突发事件、事故、治安与公共安全类信息。",
     "examples": ["事故", "火灾", "救援"],
     "search_hints": ["gov.cn", "cctv.com", "mem.gov.cn"]},
    {"id": "environment", "name": "环境",
     "definition": "环境污染、生态保护、气象与自然灾害类信息。",
     "examples": ["雾霾", "水质", "地震"],
     "search_hints": ["mee.gov.cn", "gov.cn", "xinhuanet.com"]},
    {"id": "history", "name": "历史",
     "definition": "历史事件、人物、文物与史料考据类信息。",
     "examples": ["文物", "考古", "史料"],
     "search_hints": ["people.com.cn", "china.com.cn", "gov.cn"]},
    {"id": "international", "name": "国际时事",
     "definition": "国际关系、外国政经、地区冲突与国际组织类信息。",
     "examples": ["大选", "制裁", "峰会"],
     "search_hints": ["xinhuanet.com", "cctv.com", "people.com.cn"]},
    {"id": "celebrity_entertainment", "name": "名人娱乐",
     "definition": "公众人物、影视综艺、体育与娱乐产业类信息。",
     "examples": ["票房", "恋情", "综艺"],
     "search_hints": ["people.com.cn", "xinhuanet.com", "piyao.org.cn"]},
    {"id": "other", "name": "其他",
     "definition": "无法归入上述任一领域的杂项；拿不准时用本项。",
     "examples": ["生活常识", "宠物", "天气"],
     "search_hints": []},
]

_DEFAULT_TABLE: Dict[str, Any] = {"version": 1, "domains": DEFAULT_DOMAINS}

#: 领域判定的封闭枚举提示词（头/尾分开拼，避免定义里的花括号撞上 str.format）
_CLASSIFY_HEAD = (
    "你是领域分类助理。用户会给出一条待核查的文本（查询词或原文片段），"
    "请判断它最可能属于哪个领域。\n"
    "可选领域如下（domain 只能取其中的 id）：\n"
)
_CLASSIFY_TAIL = (
    "\n要求：\n"
    '1. domain：只能取上面列出的领域 id；拿不准时取 "other"，不要自造领域。\n'
    "2. reason：一句话说明判定依据，不超过40字。\n"
    '3. 文本信息不足、无法判断领域时，domain 输出空字符串 ""（这不同于 other：'
    'other 表示"知道不属于前面任何一类"，空串表示"根本没东西可判"）。\n'
    '请严格按照以下JSON格式输出，不要输出任何其他内容：\n'
    '{"domain": "public_health", "reason": "..."}'
)

_DEFAULT_TTL_DAYS = 30.0
_DEFAULT_HISTORY_MIN = 2


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def _builtin_table() -> Dict[str, Any]:
    """内置默认表的深拷贝（调用方改返回值不会污染模块常量）。"""
    return json.loads(json.dumps(_DEFAULT_TABLE))


def _clean_str_list(raw: Any) -> List[str]:
    """规整成"非空字符串列表"：去空白、去重（保留顺序）、坏元素跳过。"""
    out: List[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _normalize_domain(item: Any, pos: int) -> Tuple[Optional[Dict[str, Any]], str]:
    """单领域规整；返回 (领域或 None, 问题说明)。"""
    if not isinstance(item, dict):
        return None, f"领域表第 {pos} 项不是对象，已跳过"
    did = str(item.get("id") or "").strip()
    name = str(item.get("name") or "").strip()
    if not did or not name:
        return None, f"领域表第 {pos} 项缺少 id/name，已跳过"
    return {
        "id": did,
        "name": name,
        "definition": str(item.get("definition") or "").strip(),
        "examples": _clean_str_list(item.get("examples")),
        "search_hints": _clean_str_list(item.get("search_hints")),
    }, ""


def _normalize_table(data: Any) -> Tuple[Dict[str, Any], List[str]]:
    """把加载到的配置规整成合法结构；整表不合法时退内置默认。"""
    if not isinstance(data, dict):
        return _builtin_table(), ["领域表根节点必须是对象，已全部退回内置默认"]
    raw_domains = data.get("domains")
    if not isinstance(raw_domains, list):
        return _builtin_table(), ["领域表缺少 domains 数组，已全部退回内置默认"]

    domains: List[Dict[str, Any]] = []
    seen: set = set()
    issues: List[str] = []
    for idx, item in enumerate(raw_domains):
        domain, issue = _normalize_domain(item, idx + 1)
        if domain is None:
            issues.append(issue)
            continue
        if domain["id"] in seen:
            issues.append(f"领域 id 重复声明：{domain['id']}（第 {idx + 1} 项），"
                          f"先声明者生效，已忽略后者")
            continue
        seen.add(domain["id"])
        domains.append(domain)
    if not domains:
        issues.append("领域表没有合法领域条目，已全部退回内置默认")
        return _builtin_table(), issues

    version = data.get("version")
    table: Dict[str, Any] = {k: v for k, v in data.items()
                             if not str(k).startswith("_") and k not in ("domains", "version")}
    table["version"] = (version if isinstance(version, int) and not isinstance(version, bool)
                        else 1)
    table["domains"] = domains
    return table, issues


def load_domains(path: Optional[str] = None) -> Tuple[Dict[str, Any], List[str]]:
    """加载平台领域表，返回 (表, 告警列表)。**永不抛异常。**

    表形状：{"version": int, "domains": [{"id","name","definition","examples",
    "search_hints"}, ...]}（其余顶层原样保留，`_` 开头说明键由加载器剥掉）。
    注意 builtin 传空字典：config_loader 对"列表"取并集，若把内置 12 项当底稿，
    主文件里改过 search_hints 的领域会被并成 24 条重复 id——默认化一律交给
    `_normalize_table` 自己做（文件读不到时它才退内置默认）。
    """
    from mylocalai.config import config_loader

    target = Path(path) if path is not None else DOMAINS_PATH
    try:
        data, loader_warnings = config_loader.load_config(
            str(target),
            builtin={},
            required_sections=("domains",),
            env_full="PLATFORM_DOMAINS_PATH",
            env_extra="PLATFORM_DOMAINS_EXTRA_PATH",
        )
        warnings: List[str] = list(loader_warnings.values())
        table, issues = _normalize_table(data)
        warnings.extend(issues)
        return table, warnings
    except Exception as exc:  # noqa: BLE001  加载器自身异常也不能炸调用方
        return _builtin_table(), [f"领域表加载异常（{type(exc).__name__}: {exc}），已用内置默认"]


# ---------------------------------------------------------------------------
# 查询（越界/空值一律给安全默认，永不抛）
# ---------------------------------------------------------------------------

def _current_domains() -> List[Dict[str, Any]]:
    table, _warnings = load_domains()
    domains = table.get("domains")
    return domains if isinstance(domains, list) else []


def domain_ids() -> List[str]:
    """全部领域 id（按表内声明顺序）。"""
    return [d["id"] for d in _current_domains()
            if isinstance(d, dict) and str(d.get("id") or "").strip()]


def domain_name(domain_id: Any) -> str:
    """领域 id → 中文名；越界/空 → 空串。"""
    did = str(domain_id or "").strip()
    if not did:
        return ""
    for d in _current_domains():
        if isinstance(d, dict) and d.get("id") == did:
            return str(d.get("name") or "")
    return ""


def domain_hints(domain_id: Any) -> List[str]:
    """领域 id → 检索提示域名（host 清单）；越界/空 → []。

    提示只用于构造 site: 定向检索，**不是权威认定**——资料可不可信仍由证据链负责。
    """
    did = str(domain_id or "").strip()
    if not did:
        return []
    for d in _current_domains():
        if isinstance(d, dict) and d.get("id") == did:
            hints = d.get("search_hints")
            return list(hints) if isinstance(hints, list) else []
    return []


# ---------------------------------------------------------------------------
# LLM 领域判定（封闭枚举 + 白名单校验；LLM 不可用不猜）
# ---------------------------------------------------------------------------

def _default_llm_fn(system: str, user: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """默认 llm_fn：把 llm_client.chat_json 适配成 (obj, err) 形状。

    chat_json 自身承诺不抛；这里再兜一层，import 失败（未装依赖/模块被摘）同样
    返回 (None, 原因)，让 classify_domain 走"不猜领域"。
    """
    try:
        from mylocalai.llm.llm_client import chat_json
        obj, err = chat_json(system, user, temperature=0, max_tokens=200,
                             namespace="platform_domain")
    except Exception as exc:  # noqa: BLE001  import 失败/调用异常都不猜领域
        return None, str(exc)
    if obj is None:
        return None, (err or "模型未返回可用 JSON")
    return obj, (err or "")


def _classify_system_prompt(domains: List[Dict[str, Any]]) -> str:
    """封闭枚举 system prompt：列出全部领域 id + 名称 + 定义。"""
    lines = [f"- {d['id']}（{d['name']}）：{str(d.get('definition') or '').strip()}"
             for d in domains if isinstance(d, dict) and d.get("id")]
    return _CLASSIFY_HEAD + "\n".join(lines) + _CLASSIFY_TAIL


def classify_domain(text: Any,
                    llm_fn: Optional[Callable[[str, str], Tuple[Any, Any]]] = None
                    ) -> Optional[str]:
    """判定文本领域，返回领域 id；判不了/LLM 不可用 → None（**不猜**）。

    llm_fn(system, user) → (obj, err)。obj 必须是 dict 且 domain 取领域表内的 id
    （空串表示"信息不足"，同样返回 None）；自造领域、越界值、err 非空、调用抛异常、
    文本为空——全部 None。
    """
    body = str(text or "").strip()
    if not body:
        return None
    domains = _current_domains()
    allowed = {str(d.get("id") or "").strip() for d in domains if isinstance(d, dict)}
    allowed.discard("")
    fn = llm_fn or _default_llm_fn
    try:
        obj, err = fn(_classify_system_prompt(domains), f"待判定文本：{body[:600]}")
    except Exception:  # noqa: BLE001  LLM 不可用不猜领域（与取证文化一致）
        return None
    if err or not isinstance(obj, dict):
        return None
    domain = str(obj.get("domain") or "").strip()
    if not domain or domain not in allowed:
        return None
    return domain


# ---------------------------------------------------------------------------
# 领域裁决（user → 缓存 → LLM → 历史直方图 → 未判定）
# ---------------------------------------------------------------------------

def _parse_iso(value: Any) -> Optional[datetime]:
    """ISO8601 → aware datetime；解析失败/空 → None。naive 按 UTC 解释。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _within_ttl(inferred_at: Any, now_iso: Any, ttl_days: Any) -> bool:
    """缓存是否在 TTL 内。任一端解析失败都视为过期（无法核验时效，宁可不命中）。

    时钟漂移（缓存时间戳在未来）不算过期：age 为负时仍 <= ttl。比较在绝对时刻上
    做，因此"本地带偏移"与"UTC"两种 now_iso 混用也正确。
    """
    stamp = _parse_iso(inferred_at)
    if stamp is None:
        return False
    raw_now = str(now_iso or "").strip()
    now_dt = _parse_iso(raw_now) if raw_now else datetime.now(timezone.utc)
    if now_dt is None:
        return False
    try:
        days = float(ttl_days)
    except (TypeError, ValueError):
        days = _DEFAULT_TTL_DAYS
    if days < 0:
        days = 0.0
    return (now_dt - stamp) <= timedelta(days=days)


def _write_inferred(store: Any, query_id: Any, domain: str, now_iso: Any) -> None:
    """把 LLM 判定写回缓存；store 缺失/方法缺失/写失败都不阻断判定。"""
    qid = str(query_id or "").strip()
    if store is None or not qid:
        return
    setter = getattr(store, "set_inferred_domain", None)
    if not callable(setter):
        return
    try:
        setter(qid, domain, str(now_iso or ""))
    except Exception:  # noqa: BLE001  写缓存失败只是少一次缓存命中
        pass


def _histogram(store: Any) -> Dict[str, Any]:
    """store.domain_histogram() → dict；不可用/抛异常/形状不对 → {}。"""
    if store is None:
        return {}
    getter = getattr(store, "domain_histogram", None)
    if not callable(getter):
        return {}
    try:
        data = getter()
    except Exception:  # noqa: BLE001  统计不可用只当"没有历史"
        return {}
    return data if isinstance(data, dict) else {}


def _top_domain(hist: Dict[str, Any], ids: List[str],
                history_min: int) -> Tuple[str, int]:
    """直方图里计数最高的已知领域（并列取先声明者）；不够门槛返回 ("", 计数)。"""
    best, best_n = "", 0
    for key, value in hist.items():
        kid = str(key or "").strip()
        if kid not in ids:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        count = int(value)
        if count > best_n:
            best, best_n = kid, count
    return (best, best_n) if best and best_n >= max(1, history_min) else ("", best_n)


def resolve_domain(user_domain: Any, query_text: Any, row: Any, store: Any,
                   llm_fn: Optional[Callable[[str, str], Tuple[Any, Any]]] = None,
                   now_iso: Any = "", ttl_days: Any = _DEFAULT_TTL_DAYS,
                   history_min: Any = _DEFAULT_HISTORY_MIN) -> Tuple[str, str]:
    """裁决领域，返回 (domain, source)。domain 为空串表示未判定。**永不抛异常。**

    链（前一级给出答案就不再往下走）：
      1. user_domain 非空且是已知领域      → (user_domain, "user")
      2. row.inferred_domain 在 TTL 内     → (inferred_domain, "inferred_text")
      3. classify_domain 判定成功          → 写 store 缓存后 (d, "inferred_text")
      4. store.domain_histogram() 最高项计数 ≥ history_min → (d, "inferred_history")
      5. 都没有                            → ("", "none")

    row：本次查询的档案行（dict）。用到的键："id"（写缓存的 query_id）、
    "inferred_domain" / "inferred_at"（缓存）；用户设定的领域由调用方从 row 里取出后
    经第 1 参传入，本函数不读 row 的用户领域键。row 为 None/非 dict/{} 时安全跳过。
    llm_fn=None 用默认实现（chat_json 适配，见 _default_llm_fn）。
    """
    ids = domain_ids()
    user = str(user_domain or "").strip()
    if user and user in ids:
        return user, "user"

    record = row if isinstance(row, dict) else {}
    inferred = str(record.get("inferred_domain") or "").strip()
    if inferred and inferred in ids and _within_ttl(record.get("inferred_at"),
                                                    now_iso, ttl_days):
        return inferred, "inferred_text"

    domain = classify_domain(query_text, llm_fn)
    if domain:
        _write_inferred(store, record.get("id"), domain, now_iso)
        return domain, "inferred_text"

    try:
        threshold = int(history_min)
    except (TypeError, ValueError):
        threshold = _DEFAULT_HISTORY_MIN
    top, _count = _top_domain(_histogram(store), ids, threshold)
    if top:
        return top, "inferred_history"
    return "", "none"
