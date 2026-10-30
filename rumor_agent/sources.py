# -*- coding: utf-8 -*-
"""信源分级与日期提取（纯规则，离线）。

证据的分量取决于"谁说的"。这里按域名把来源分成 5 级，用于证据本里的判定规则：

    官方权威        政府网站、官方辟谣平台、世卫组织等
    权威媒体/辟谣平台 国家级与主流新闻媒体、专业科普/医学辟谣机构
    一般门户媒体    综合门户、地方媒体、医疗资讯站
    自媒体/UGC      公众号、百家号、头条号、知乎、微博等任何人都能发布的平台
    未知来源        不在名单内的域名

只有前两级算"可靠来源"（RELIABLE_TIERS）。**名单本身在 rumor_agent/sources_registry.json**，
不在本文件里：增删来源改 JSON 即可（也可用 RUMOR_SOURCES_PATH 指向别处的同名文件）。
名单是人工整理的、不完整：名单外的好来源会被归为"未知"，因此宁可低估也不会把陌生域名
当权威——这是有意的保守取向。

**哪几档算可靠 / 可抓全文 / 可向外扩链 / 算官方，以及各档的渠道权重，全部按档名写在
同一个 JSON 里**（`reliable_tiers` / `fetch_tiers` / `link_tiers` / `official_tiers` / `channel_score`），
代码只读不写死：换行业把档位重命名（而不只是增删域名）时不会静默失效。
下面几个 TIER_* 常量是**默认档名**，用作配置缺失时的兜底，不是成员判断的依据。

域名匹配按"域名边界"做后缀匹配并取最长命中：xinhuanet.com.evil.com 与
http://xinhuanet.com@evil.com/ 都不会被当成新华网。
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Dict, Optional
from urllib.parse import urlparse

from .rules import add_warning, load_sources

# 默认档名（与 sources_registry.json 的 tiers 键一一对应）。它们只是**配置缺失时的兜底值**，
# 不是判定依据：哪些档算可靠/可抓/可扩链/算官方，一律从配置按档名读（见 _resolve_roles）。
TIER_OFFICIAL = "官方权威"
TIER_MEDIA = "权威媒体/辟谣平台"
TIER_PORTAL = "一般门户媒体"
TIER_UGC = "自媒体/UGC"
TIER_UNKNOWN = "未知来源"

_REGISTRY = load_sources()


# 哪些档算"可靠来源/可抓全文/可向外扩链/算官方"、按什么顺序排，全部按档名从配置读。
# 换行业把档位重命名（而不只是增删域名）时，这些判定不会静默失效。


def _declared_tiers() -> tuple:
    """按 registry 里 tiers 的**声明顺序**取档名（换行业改顺序即改优先级）。"""
    tiers = _REGISTRY.get("tiers")
    if not isinstance(tiers, dict):
        return ()
    return tuple(str(t) for t in tiers if not str(t).startswith("_"))


# 档位优先级：越靠前越权威。搜索结果排序等"谁排前面"的判断都用它，不再把档名写进 Python。
DECLARED_TIERS: tuple = _declared_tiers()


def _resolve_roles(key: str, depth: int) -> frozenset:
    """定"哪几档算可靠 / 可抓全文 / 可扩链 / 算官方"：

    1. 配置里显式列了档名 → 只认**确实在 tiers 里声明过**的那些（换了名单还留着旧档名会被丢掉）；
    2. 配置没写、或写的档名一个都不在名单里 → **按 tiers 的声明顺序取前 depth 档**。

    第 2 条是关键兜底：换行业时只把名单换掉（档位角色段忘了写或还写着旧档名），
    判定也不会退化成"哪个档都不可靠"——那正是 2026-10-15 检验报告发现 1 的静默失效场景。
    """
    declared = DECLARED_TIERS
    value = _REGISTRY.get(key)
    if isinstance(value, (list, tuple, set, frozenset)) and all(isinstance(x, str) for x in value):
        picked = frozenset(t for t in value if t in declared)
        stale = [t for t in value if t not in declared]
        if picked:
            if stale:
                parts = "、".join(stale)
                current = "、".join(declared) or "无"
                add_warning(f"sources_registry.json#{key}",
                            f"{key} 里的 {parts} 不在 tiers 声明里，已忽略（当前档位：{current}）")
            return picked
        listed = "、".join(value)
        current = "、".join(declared) or "无"
        add_warning(f"sources_registry.json#{key}",
                    f"{key} 列的档名 {listed} 一个都不在 tiers 声明里（当前档位：{current}），"
                    f"已按声明顺序取前 {depth} 档兜底")
    return frozenset(declared[:max(0, depth)])


RELIABLE_TIERS = _resolve_roles("reliable_tiers", 2)
FETCH_TIERS = _resolve_roles("fetch_tiers", 3)
LINK_TIERS = _resolve_roles("link_tiers", 2)
OFFICIAL_TIERS = _resolve_roles("official_tiers", 1)


def tier_rank(tier: str) -> int:
    """档位的排序位次（按 tiers 声明顺序）；未声明的档（含"未知来源"）排最后。"""
    try:
        return DECLARED_TIERS.index(tier)
    except ValueError:
        return len(DECLARED_TIERS)

# 名单外域名（以及空主机）统一返回这个值。它是判定契约的一部分：
# tests/test_rumor_agent_offline.py 锁定 unknown_kind=「其他」。
# 抗伪装的安全属性由"不等于官方"保证，不要求它必须写成"非官方"。
UNKNOWN_KIND = str(_REGISTRY.get("unknown_kind") or "其他")

# 域名单表：类型写坏（例如写成列表/字符串）时退回内置默认，绝不让 import 期抛异常
_OFFICIAL_RAW = _REGISTRY.get("official_sites")
_COMMERCIAL_RAW = _REGISTRY.get("commercial_sites")


def _site_map(raw, default: Dict[str, str]) -> Dict[str, str]:
    if isinstance(raw, dict) and all(isinstance(k, str) and isinstance(v, str)
                                     for k, v in raw.items() if not str(k).startswith("_")):
        return {k: v for k, v in raw.items() if not str(k).startswith("_")}
    return dict(default)


OFFICIAL_SITES: Dict[str, str] = _site_map(_OFFICIAL_RAW, {
    "cctv.com": "央视新闻", "cctv.cn": "央视新闻", "xinhuanet.com": "新华网",
    "news.cn": "新华网", "cnr.cn": "央广网", "people.com.cn": "人民网", "people.cn": "人民网",
})
COMMERCIAL_SITES: Dict[str, str] = _site_map(_COMMERCIAL_RAW, {
    "sina.com.cn": "新浪网", "qq.com": "腾讯网", "qianzhan.com": "前瞻网",
    "bwchinese.com": "BWCHINESE中文网", "baidu.com": "百度",
})

_DOMAIN_TIERS: Dict[str, str] = {}


def _load_domain_tiers() -> None:
    """把 JSON 里的 tiers（档名 → 域名列表）摊平成 域名 → 档名 表。"""
    tiers = _REGISTRY.get("tiers")
    if not isinstance(tiers, dict):
        return
    for tier, domains in tiers.items():
        if not isinstance(domains, (list, tuple)):
            continue
        for domain in domains or []:
            _DOMAIN_TIERS[str(domain)] = str(tier)


# 渠道权重（民间观点聚合的论据可信度用）。默认值与 sources_registry.json 一致；
# 未知档名统一取 UNKNOWN_KIND 的权重（缺省 0.5）。
_DEFAULT_CHANNEL_SCORE = {"官方权威": 3.0, "权威媒体/辟谣平台": 2.0, "一般门户媒体": 1.5,
                          "自媒体/UGC": 1.0, "未知来源": 0.5}


def channel_score_table() -> Dict[str, float]:
    """各档位的渠道权重表（key 是档名）。配置里写坏就退回内置默认。"""
    raw = _REGISTRY.get("channel_score")
    if isinstance(raw, dict) and raw and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                             for v in raw.values()):
        return {str(k): float(v) for k, v in raw.items()}
    return dict(_DEFAULT_CHANNEL_SCORE)


_load_domain_tiers()


def _match_site(host: str, table: Dict[str, str]) -> Optional[str]:
    """在 域名→站名 表里按域名边界取最长命中（baijiahao.baidu.com 优先于 baidu.com）。"""
    best, best_len = None, -1
    for domain, name in table.items():
        if (host == domain or host.endswith("." + domain)) and len(domain) > best_len:
            best, best_len = name, len(domain)
    return best


def source_kind(url_or_host: str) -> str:
    """按用户口径给出"官方/非官方"标注：官方 = 央视新闻/新华网/央广网/人民网（及政府级域名）。

    命名上虽是"官方/非官方"两分，实际会返回三类值，别指望只有两种：
      - "官方（站名/政府权威机构）"   官方档
      - "非官方（站名/权威媒体）"     名单内的非官方来源
      - UNKNOWN_KIND（默认"其他"）    空主机或名单外的域名
    第三类是**有意的**：名单外域名既不是官方、也不断言其为非官方，于是统一归"其他"。
    抗伪装靠的是"判不到官方"（`"官方" in kind` 为假、tier 不在 OFFICIAL_TIERS），
    而不是靠把陌生域名写成"非官方"——`xinhuanet.com.evil.com` 落到"其他/未知来源"即安全。

    哪几档算官方、哪几档算权威媒体，都按档名从配置读（OFFICIAL_TIERS / RELIABLE_TIERS），
    所以换行业重命名档位后这里不会静默失效。用于证据本与报告展示。
    """
    host = normalize_host(url_or_host) if ("/" in url_or_host or ":" in url_or_host) \
        else url_or_host.lower().rstrip(".")
    if not host:
        return UNKNOWN_KIND
    name = _match_site(host, OFFICIAL_SITES)
    if name:
        return f"官方（{name}）"
    tier = classify_domain(host)
    if tier in OFFICIAL_TIERS:
        return "官方（政府/权威机构）"
    name = _match_site(host, COMMERCIAL_SITES)
    if name:
        return f"非官方（{name}）"
    if tier in RELIABLE_TIERS:
        return "非官方（权威媒体）"
    if tier in DECLARED_TIERS:
        # 名单里认得的档，但不是官方、也不是权威媒体 → 统一"非官方"。
        # 用"是否在 tiers 声明里"判断，而不是把门户/UGC 的档名写死在这里（换行业重命名后依然成立）。
        return "非官方"
    return UNKNOWN_KIND


def normalize_host(url: str) -> str:
    """取 URL 的主机名（小写、去 www.）；解析不了返回空串。"""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def classify_domain(url_or_host: str) -> str:
    """返回来源等级。按域名边界后缀匹配，取最长命中；未命中为"未知来源"。"""
    host = normalize_host(url_or_host) if "/" in url_or_host or ":" in url_or_host else url_or_host.lower().rstrip(".")
    if not host:
        return TIER_UNKNOWN
    best, best_len = TIER_UNKNOWN, -1
    for domain, tier in _DOMAIN_TIERS.items():
        if "/" in domain:        # 带路径的条目只用于文档说明，不参与主机名匹配
            continue
        if (host == domain or host.endswith("." + domain)) and len(domain) > best_len:
            best, best_len = tier, len(domain)
    return best


_DATE_TEXT = re.compile(r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})")
_MONTH_TEXT = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")
_DATE_URL = re.compile(r"(?<!\d)(20\d{2})[-/]?(0[1-9]|1[0-2])[-/]?(0[1-9]|[12]\d|3[01])(?!\d)")


def _valid(y: int, m: int, d: int = 1) -> bool:
    today = _dt.date.today()
    try:
        return 2005 <= y <= today.year + 1 and bool(_dt.date(y, m, d))
    except ValueError:
        return False


def extract_date(text: str = "", url: str = "") -> Optional[str]:
    """从摘要文字或 URL 里取发布日期，返回 YYYY-MM-DD 或 YYYY-MM；取不到返回 None。"""
    text = text or ""
    m = _DATE_TEXT.search(text)
    if m and _valid(int(m[1]), int(m[2]), int(m[3])):
        return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"
    m = _MONTH_TEXT.search(text)
    if m and _valid(int(m[1]), int(m[2])):
        return f"{int(m[1]):04d}-{int(m[2]):02d}"
    m = _DATE_URL.search(url or "")
    if m and _valid(int(m[1]), int(m[2]), int(m[3])):
        return f"{m[1]}-{m[2]}-{m[3]}"
    return None
