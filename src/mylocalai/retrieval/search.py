# -*- coding: utf-8 -*-
"""检索层：向搜索引擎查询与文本相关的事件，抓取原文做证据。

设计要点（来自对本机网络的实测）：
  - 360 搜索（so.com）是唯一稳定可用的搜索后端 → 主力；百度首页搜索可用但
    连续请求会触发验证码（302 跳 wappass.baidu.com）→ 备用，触发后降级 30 分钟。
  - 证据以搜索结果摘要为主：很多原文页（公众号、百家号、澎湃）有反爬，全文抓取
    是机会式的，抓不到不影响判定。
  - fetch_article 做了 SSRF 防护：只允许 http/https、解析主机后拒绝私网/环回/
    链路本地地址，限制响应大小与超时——搜索结果里的 URL 是外部输入，不能盲信。
  - API 型搜索源（P0-1 检索源结构化升级）：清单外置在 search_sources.json，
    密钥走 key_env 指定的环境变量；enabled + 密钥两者齐备才进默认装配，执行层
    另有 skip_reason → skipped 计数兜底（不报错、不算降级主因）。授权闸与爬虫
    源同一条：search_all 的 public-search 闸先于一切 HTTP 外发。
  - 多源融合（P1-5 检索结果多源融合与复用），三件事：
    (1) 健康度动态降权——API 源连续失败 ≥ 阈值（search_sources.json 顶层
        failure_skip_threshold，默认 3）时本次 search_all 先跳过该源（记
        skipped 原因、零外发）；跳过本身给失败账本降温，源不会永久出局，
        成功一次即清零回归（"连续失败源暂时跳过并记数，恢复后自动回归"）。
    (2) 跨源结果融合——多源结果按既有 _dedup_url 归一口径去重合并（先到者
        优先；同 URL 不同摘要不重复计），融合后总数尊重既有 limit 口径。
    (3) 查询级缓存——同一 (query, domains, 源集合) 的成功结果在进程内缓存
        （TTL = query_cache_ttl_s，默认 300s，≤0 显式关闭），命中零外发；
        只缓存"无错误、无跳过、结果非空"的完全成功，且只缓存默认装配路径
        （显式 providers 是测试/本地适配器自有传输，不缓存）。缓存逻辑位于
        授权闸之后：未授权时连缓存都不查，"未授权零外发"语义原封不动。
  - 检索层韧性（2026-11-25，针对实测"中文搜索引擎反爬 → 0 结果但看起来像没查到"）：
    (A) 反爬共用判定 detect_verification_page()——关键词（wappass/captcha/
        geetest/安全验证/异常流量/unusual traffic…）+ 结构特征（captcha 容器、
        challenge 表单、滑块容器）+ 正文长度阈值（可见正文 < 400 字符且解析不出
        结果）三路信号，覆盖百度 wappass、Bing 同意/验证页、360/Sogou 风控页。
        命中即该源进入冷却（cooldown_seconds，search_sources.json 顶层可配，
        默认 1800 秒），engines 槽位标 blocked 并给中文 reason——不抛异常、
        不当成"搜索成功但没有结果"；冷却写在进程级表里（加锁），到期自动恢复。
    (B) 结构化 API 源是"一等公民"：配置缺省不报错，但 search_all 返回
        sources_configured/api_sources/rss_sources 让调用方能判断"只有网页解析源、
        遇反爬无备用"；配置校验（URL 必须 http/https 且有主机、协议白名单、
        超时/重试/权重类型）坏值回默认并告警；失败重试有限且带指数退避
        （默认 0 次 = 既有单次语义，JSON 按源可配），失败计入健康账本。
    (C) RssProvider——search_sources.json 的 rss_sources 段声明的 RSS/Atom 源，
        标准库 xml.etree.ElementTree 解析（不新增第三方依赖），按关键词过滤条目，
        产出与网页源同形状的 SearchResult；单源失败不影响其它源，条目做 URL 去重
        与域名过滤，pubDate/published 解析失败不抛。
    (D) 降级要诚实：search_all 顶层带 status（ok/partial/empty/all_blocked/
        no_sources/all_failed）、degraded、all_blocked、sources_configured、
        degraded_reason，让调用方能区分"真的 0 条 / 全被反爬拦截 / 没有可用源 /
        部分源成功"；engines 每个槽位带 {ok, error, count, blocked, reason}。
        上层（rumor/assistant/platform）复用同一口径，不把"被拦截"显示成"没发现"。

所有函数都不抛异常：失败返回 (空结果, 错误说明) 或 None，由上层记入降级组件。
"""

from __future__ import annotations

import copy
import email.utils
import html as _html
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urldefrag, urlparse

import requests

from . import dns_fallback
from .sources import FETCH_TIERS, TIER_UNKNOWN, classify_domain, extract_date, normalize_host, tier_rank

dns_fallback.install()   # 系统 DNS 偶发解析不了搜索后端域名，公网 DNS 兜底

_logger = logging.getLogger("mylocalai.retrieval.search")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_SEARCH_TIMEOUT = 10          # 搜索请求超时（秒）
_DEGRADE_SECONDS = 1800.0     # 百度触发验证码后的冷却时长（历史默认；现由 cooldown_seconds 覆盖）
_FETCH_TIMEOUT = 6            # 原文抓取超时
_FETCH_MAX_BYTES = 600_000    # 原文抓取大小上限（600KB）
_FETCH_MIN_TEXT = 200         # 提取正文短于此视为反爬页/失败
_FETCH_MAX_TEXT = 2400        # 存入证据本的正文上限（字符）
# 只抓分级较高的来源：自媒体/UGC 域名反爬最凶、内容也不比摘要更有分量。
# 具体哪几档可抓由 sources_registry.json 的 fetch_tiers 声明（换行业重命名档位不再静默失效）。
_FETCH_TIERS = FETCH_TIERS

# ---------------------------------------------------------------------------
# 多源融合（P1-5）共享状态：API 源健康账本 / 查询级缓存 / search_sources.json 顶层设置
# ---------------------------------------------------------------------------

#: API 源健康账本（源 id → 连续失败数）。默认装配每次 search_all 都新建 provider
#: 实例，实例计数器跨不过调用边界；账本按源 id 记账，跨调用、跨实例生效：
#: search() 失败 +1 / 成功清零，健康跳过一次 -1（cool_health_skip）。
_API_SOURCE_HEALTH: Dict[str, int] = {}
#: 健康账本锁：账本跨线程读写（web 多 worker 线程 + 后台调度），必须串行化。
_HEALTH_LOCK = threading.RLock()
#: 查询级缓存：键 = (query, domains, k, 源集合签名)，值 = (过期时刻, 返回值深拷贝)。
#: 只存"完全成功"的查询结果（判定见 search_all 的 cacheable），命中零外发。
_QUERY_CACHE: Dict[Tuple, Tuple[float, Dict[str, object]]] = {}
_QUERY_CACHE_TTL_DEFAULT = 300.0     # 缓存 TTL 默认 300 秒（search_sources.json 可配）
_QUERY_CACHE_MAX = 128               # 缓存条数上限（满时先清过期、再按插入序逐出最旧）
_FAILURE_SKIP_THRESHOLD_DEFAULT = 3  # 连续失败跳过阈值默认值（search_sources.json 可配）
#: search_sources.json 顶层设置（failure_skip_threshold / query_cache_ttl_s / cooldown_seconds）：
#: 每次 load_api_source_configs() 都刷新（改 JSON 即生效）；文件缺失/损坏回默认。
_SOURCE_SETTINGS: Dict[str, object] = {}


# ---------------------------------------------------------------------------
# 检索层韧性（A）：验证页/拦截页共用判定 + 源冷却表
# ---------------------------------------------------------------------------

#: 反爬命中后的冷却时长（秒）：search_sources.json 顶层 cooldown_seconds 可配，默认 1800。
#: 与历史常量 _DEGRADE_SECONDS 同源（百度降级时长沿用同一个默认值，不出现两处漂移）。
_COOLDOWN_SECONDS_DEFAULT = _DEGRADE_SECONDS
_COOLDOWN_SECONDS_MAX = 86_400.0      # 上限 24 小时（防手滑写成一年的"永久出局"）
#: 正文长度阈值：正常结果页有导航/页脚，可见正文不会这么短；短于此且解析不出结果 = 疑似拦截页。
_VERIFY_MIN_TEXT = 400
_VERIFY_PROBE_BYTES = 200_000         # 关键词/结构检测只看页面头部（正常页 < 200KB）
#: 关键词信号：命中即拦截。顺序有意义——先命中先返回（更具体的原因优先）。
_VERIFY_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("wappass", "百度安全验证页（wappass）"),
    ("geetest", "滑块验证页（geetest）"),
    ("nc_1_wrapper", "滑块验证页（nc_1_wrapper）"),
    ("captcha", "人机验证页（captcha）"),
    ("安全验证", "安全验证页"),
    ("人机验证", "人机验证页"),
    ("访问验证", "访问验证页"),
    ("请输入验证码", "验证码页"),
    ("异常流量", "风控页（异常流量）"),
    ("访问过于频繁", "风控页（访问过于频繁）"),
    ("请求过于频繁", "风控页（请求过于频繁）"),
    ("unusual traffic", "风控页（unusual traffic）"),
    ("verify you are human", "人机验证页（verify you are human）"),
    ("before you continue", "同意/前置确认页（before you continue）"),
    ("同意并继续", "同意/前置确认页（同意并继续）"),
    ("接受所有 cookie", "同意/前置确认页（cookie consent）"),
)
#: 结构特征：容器/表单的形状，比关键词更难被文案改动绕过。
_VERIFY_STRUCT: Tuple[Tuple[str, str], ...] = (
    (r'id=["\']?wappass', "结构特征：wappass 容器"),
    (r'id=["\']?captcha', "结构特征：captcha 容器"),
    (r'class=["\'][^"\']*captcha', "结构特征：captcha 样式"),
    (r"geetest|nc_1_wrapper", "结构特征：滑块验证容器"),
    (r"<form[^>]+challenge", "结构特征：challenge 表单"),
    (r"verify[-_]form", "结构特征：验证表单"),
)
#: 源冷却表：源名 → {"until": 到期时刻, "reason": 中文原因, "since": 命中时刻, "hits": 累计命中}。
#: 进程级、跨调用生效（默认装配每次 search_all 都新建 provider 实例，实例计数器靠不住）。
_SOURCE_COOLDOWN: Dict[str, Dict[str, object]] = {}
_COOLDOWN_LOCK = threading.RLock()


def _visible_text_len(page: str) -> int:
    """去脚本/样式/标签后的可见正文字符数（空白不计）——正文长度阈值的度量口径。"""
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    return len(re.sub(r"\s+", "", text))


def detect_verification_page(page: str, *, min_text: int = _VERIFY_MIN_TEXT) -> Optional[str]:
    """共用"验证页/拦截页"判定：命中返回中文原因，正常页返回 None（不抛异常）。

    三路信号，任一命中即视为拦截页（调用方**不得**把它解析成"0 条结果"）：
      1. 关键词：wappass / captcha / geetest / 安全验证 / 人机验证 / 异常流量 /
         unusual traffic / before you continue（Bing 同意页）…
      2. 结构特征：captcha 容器、challenge 表单、滑块容器——比文案更稳。
      3. 正文长度阈值：去掉脚本样式后的可见正文 < min_text（正常结果页有导航/页脚，
         不会这么短）。按设计由调用方在"解析不出结果"时使用，避免误伤真有结果的页。
    只看页面头部 _VERIFY_PROBE_BYTES 字节：验证页的判定信号总在头部，整页扫描没必要。
    """
    probe = (page or "")[:_VERIFY_PROBE_BYTES]
    lowered = probe.lower()
    for keyword, reason in _VERIFY_KEYWORDS:
        if keyword in lowered:
            return reason
    for pattern, reason in _VERIFY_STRUCT:
        if re.search(pattern, lowered):
            return reason
    visible = _visible_text_len(probe)
    if visible < max(0, int(min_text)):
        return f"正文过短（{visible} 字符），疑似拦截页"
    return None


def _cooldown_seconds() -> float:
    """源冷却时长（秒）：search_sources.json 顶层 cooldown_seconds，缺省/非法回默认 1800。"""
    value = _SOURCE_SETTINGS.get("cooldown_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return _COOLDOWN_SECONDS_DEFAULT
    return min(float(value), _COOLDOWN_SECONDS_MAX)


def mark_source_blocked(source: str, reason: str, seconds: Optional[float] = None) -> float:
    """把一个源打上"被反爬拦截"的冷却标记，返回冷却到期时刻（epoch 秒）。

    冷却表加锁读写（跨线程）；reason 只记中文原因，不含密钥/URL 查询串。
    seconds 缺省取 cooldown_seconds（默认 1800）。重复命中会把到期时刻刷新到更晚。
    """
    name = str(source or "").strip() or "unknown"
    duration = _cooldown_seconds() if seconds is None else max(0.0, float(seconds))
    until = time.time() + duration
    with _COOLDOWN_LOCK:
        entry = _SOURCE_COOLDOWN.setdefault(name, {"since": time.time(), "hits": 0})
        entry["until"] = max(float(entry.get("until", 0.0) or 0.0), until)
        entry["reason"] = str(reason or "命中验证页")
        entry["hits"] = int(entry.get("hits", 0) or 0) + 1
    return until


def clear_source_cooldowns(source: Optional[str] = None) -> None:
    """清冷却标记（source=None 清全部）。测试隔离与运维手动"立即重试"用。"""
    with _COOLDOWN_LOCK:
        if source is None:
            _SOURCE_COOLDOWN.clear()
            return
        _SOURCE_COOLDOWN.pop(str(source or "").strip(), None)


def source_cooldown(source: str) -> Dict[str, object]:
    """读一个源的冷却状态（不会改动状态）：active / remaining_s / reason / until。

    到期即视为恢复（惰性过期：读取时发现过期顺手清掉），无需后台定时器。
    """
    name = str(source or "").strip()
    with _COOLDOWN_LOCK:
        entry = _SOURCE_COOLDOWN.get(name)
        if not entry:
            return {"active": False, "remaining_s": 0.0, "reason": "", "until": 0.0}
        remaining = float(entry.get("until", 0.0) or 0.0) - time.time()
        if remaining <= 0:
            _SOURCE_COOLDOWN.pop(name, None)      # 冷却到期：自动恢复，期间零外发
            return {"active": False, "remaining_s": 0.0, "reason": "", "until": 0.0}
        return {"active": True, "remaining_s": remaining,
                "reason": str(entry.get("reason", "") or ""),
                "until": float(entry.get("until", 0.0) or 0.0)}


def cooldown_snapshot() -> Dict[str, Dict[str, object]]:
    """当前仍在冷却的源（如实呈现，供 /api/health 与返回体使用；过期项顺手清掉）。"""
    with _COOLDOWN_LOCK:
        names = list(_SOURCE_COOLDOWN)
    out: Dict[str, Dict[str, object]] = {}
    for name in names:
        state = source_cooldown(name)
        if state["active"]:
            out[name] = {"remaining_s": round(float(state["remaining_s"]), 1),
                         "reason": str(state["reason"])}
    return out


def _note_blocked(source: str, reason: str) -> str:
    """命中验证页：记冷却并返回给上层的错误串（中文、不抛、不当成"0 条结果"）。"""
    mark_source_blocked(source, reason)
    state = source_cooldown(source)
    return f"{reason}（该源冷却 {int(state.get('remaining_s', 0) or 0)} 秒，期间不再请求）"


def reset_resilience_state() -> None:
    """清进程级韧性状态（健康账本 + 冷却表）：测试隔离与运维复位用，不动查询缓存。"""
    with _HEALTH_LOCK:
        _API_SOURCE_HEALTH.clear()
    clear_source_cooldowns()


def _health_get(source: str, default: int = 0) -> int:
    """读健康账本（加锁）：未登记时返回 default（显式装配的实例计数兜底）。"""
    with _HEALTH_LOCK:
        return int(_API_SOURCE_HEALTH.get(str(source or ""), default))


def _health_set(source: str, value: int) -> None:
    """写健康账本（加锁）：失败 +1 / 成功清零 / 健康跳过 -1 都走这里。"""
    with _HEALTH_LOCK:
        _API_SOURCE_HEALTH[str(source or "")] = max(0, int(value))


@dataclass
class SearchResult:
    """一条搜索结果。snippet 是证据的主要载体，全文抓取是机会式补充。"""
    engine: str
    rank: int
    title: str
    url: str
    snippet: str
    date: Optional[str] = None
    # 默认值取档位常量而不是中文字面量：换行业重命名档位后，"判不到档"依然落在未知档
    domain_tier: str = TIER_UNKNOWN
    full_text: Optional[str] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, object]:
        return {
            "engine": self.engine, "rank": self.rank, "title": self.title,
            "url": self.url, "snippet": self.snippet, "date": self.date,
            "domain_tier": self.domain_tier, "full_text": self.full_text,
        }


def _strip_tags(fragment: str) -> str:
    """去标签、反转义实体、压空白——标题和摘要都要过这一遍。"""
    text = re.sub(r"<[^>]+>", "", fragment or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _dedup_url(url: str) -> str:
    """URL 去重键：去 fragment、去尾部斜杠、小写主机。"""
    u = urldefrag((url or "").strip())[0]
    return u.rstrip("/") or u


# ===========================================================================
# 搜索后端
# ===========================================================================

class So360Provider:
    """360 搜索（so.com）。主力后端：实测稳定返回，约 1 秒，无验证码。

    韧性（A）：200 但解析不出结果时过一遍 detect_verification_page——命中风控/验证页
    即记冷却（cooldown_seconds，默认 1800 秒）并返回中文原因；冷却期内零外发。
    """
    name = "360"
    # 视频/图片等垂直类目的卡片，不是网页结果，跳过
    _SKIP_HOSTS = ("tv.360kan.com", "image.so.com", "video.so.com", "www.360kan.com",
                   "leshou.com", "so.com")

    def search(self, query: str, k: int = 6) -> Tuple[List[SearchResult], Optional[str]]:
        cooling = source_cooldown(self.name)
        if cooling.get("active"):
            return [], (f"360后端已冷却（{cooling.get('reason')}，"
                        f"剩余 {int(cooling.get('remaining_s', 0) or 0)} 秒）")
        url = "https://www.so.com/s?q=" + quote(query) + "&pn=1"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_SEARCH_TIMEOUT)
        except requests.RequestException as exc:
            return [], f"360请求失败: {type(exc).__name__}"
        page = getattr(resp, "text", "") or ""
        if resp.status_code in (403, 429):
            # 风控的 HTTP 层信号：403/429 一定是被拦，不等解析。
            reason = detect_verification_page(page) or f"360返回HTTP {resp.status_code}"
            return [], _note_blocked(self.name, reason)
        if resp.status_code != 200:
            return [], f"360返回HTTP {resp.status_code}"
        results = self.parse(page)[:k]
        if not results:
            # 解析不出结果时才有必要判拦截页：有结果的页面即使夹带"验证"字样也不算被拦。
            reason = detect_verification_page(page)
            if reason:
                return [], _note_blocked(self.name, reason)
        return results, None

    def parse(self, page: str) -> List[SearchResult]:
        """解析结果页：条目 li.res-list，真实 URL 在 <a data-mdurl>，摘要在 res-list-summary。"""
        results: List[SearchResult] = []
        for i, block in enumerate(re.findall(r"<li[^>]*class=\"[^\"]*res-list[^\"]*\"[^>]*>(.*?)</li>",
                                              page, re.S)):
            m = re.search(r"data-mdurl=\"([^\"]+)\"", block)
            if not m:
                continue
            url = _html.unescape(m[1])
            host = normalize_host(url)
            if not host or host in self._SKIP_HOSTS:
                continue
            tm = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
            title = _strip_tags(tm[1]) if tm else ""
            sm = (re.search(r"class=\"[^\"]*res-list-summary[^\"]*\"[^>]*>(.*?)</span>", block, re.S)
                  or re.search(r"class=\"[^\"]*res-desc[^\"]*\"[^>]*>(.*?)</(?:span|p|div)>", block, re.S))
            snippet = _strip_tags(sm[1]) if sm else ""
            dm = re.search(r"class=\"[^\"]*g-c-gray[^\"]*\"[^>]*>(.*?)</span>", block, re.S)
            date = extract_date(_strip_tags(dm[1]) if dm else "", url)
            if not title and not snippet:
                continue
            results.append(SearchResult(self.name, i + 1, title, url, snippet, date,
                                        classify_domain(url)))
        return results


class BaiduProvider:
    """百度搜索。备用后端：可用，但连续请求会 302 跳 wappass 验证码。

    触发验证码后类级降级（_degraded_until，时长 = cooldown_seconds，默认 1800 秒），
    期间直接返回错误、不再发请求；同时写入共享冷却表，让 search_all 与 /api/health
    都能看到"这个源正在冷却"（A：如实呈现，不静默）。
    """
    name = "baidu"
    _degraded_until = 0.0

    @classmethod
    def is_degraded(cls) -> bool:
        """是否处于降级期：类级 _degraded_until（历史口径）或共享冷却表任一命中。"""
        if time.time() < cls._degraded_until:
            return True
        return bool(source_cooldown(cls.name).get("active"))

    def search(self, query: str, k: int = 6) -> Tuple[List[SearchResult], Optional[str]]:
        if self.is_degraded():
            return [], "百度后端已降级（近期触发验证码）"
        url = "https://www.baidu.com/s?wd=" + quote(query) + "&rn=20"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_SEARCH_TIMEOUT,
                                allow_redirects=False)
        except requests.RequestException as exc:
            return [], f"百度请求失败: {type(exc).__name__}"
        loc = resp.headers.get("Location", "") if hasattr(resp, "headers") else ""
        page = getattr(resp, "text", "") or ""
        if resp.status_code in (301, 302) and "wappass" in loc:
            self._degrade("百度返回验证码页（wappass）")
            return [], "百度触发验证码，后端降级30分钟"
        if resp.status_code in (403, 429):
            reason = detect_verification_page(page) or f"百度返回HTTP {resp.status_code}"
            self._degrade(reason)
            return [], f"百度触发验证码，后端降级30分钟（{reason}）"
        if resp.status_code != 200:
            return [], f"百度返回HTTP {resp.status_code}"
        results = self.parse(page)[:k]
        if not results:
            # 解析不出结果时才判拦截页：wappass 页面正文也可能出现在 200 响应里。
            reason = detect_verification_page(page)
            if reason:
                self._degrade(reason)
                return [], f"百度返回验证码页，后端降级30分钟（{reason}）"
        return results, None

    @classmethod
    def _degrade(cls, reason: str) -> None:
        """记类级降级 + 共享冷却（两处都写：历史断言看前者，health/上层看后者）。"""
        seconds = _cooldown_seconds()
        cls._degraded_until = time.time() + seconds
        mark_source_blocked(cls.name, reason, seconds=seconds)

    def parse(self, page: str) -> List[SearchResult]:
        """解析结果页：结果块 div.result.c-container，真实 URL 在块级 mu 属性
        （href 是 baidu.com/link 跳转链接，不能直接当来源用）。"""
        results: List[SearchResult] = []
        blocks = re.split(r"(?=<div[^>]*class=\"[^\"]*result c-container)", page)
        rank = 0
        for block in blocks[1:]:
            m = re.search(r"\bmu=\"([^\"]+)\"", block)
            if not m:
                continue
            url = _html.unescape(m[1])
            if not urlparse(url).hostname:
                continue
            rank += 1
            tm = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
            title = _strip_tags(tm[1]) if tm else ""
            sm = re.search(r"class=\"[^\"]*summary-text[^\"]*\"[^>]*>(.*?)(?:</span>|$)",
                           block, re.S)
            snippet = _strip_tags(sm[1]) if sm else ""
            # 去掉百度摘要里的日期前缀"2026年3月24日&nbsp;-"
            snippet = re.sub(r"^20\d{2}年\d{1,2}月\d{1,2}日\s*[-—]?\s*", "", snippet)
            dm = re.search(r"class=\"[^\"]*prefix-time[^\"]*\"[^>]*>(.*?)</span>", block, re.S)
            date = extract_date(_strip_tags(dm[1]) if dm else "", url)
            if not title and not snippet:
                continue
            results.append(SearchResult(self.name, rank, title, url, snippet, date,
                                        classify_domain(url)))
        return results


class GenericAPIProvider:
    """可插拔的"专用搜索 API"后端（Serper/Bing API/自建服务均属此类）。

    通过环境变量配置：
      RUMOR_SEARCH_API_URL    API 地址（必填，含 http/https）
      RUMOR_SEARCH_API_KEY    密钥，放进 X-API-Key 头（可选）
      RUMOR_SEARCH_API_NAME   引擎名（默认 "custom-api"）

    约定响应为 JSON：{"results": [{"title","url","snippet","date"}]}（date 可省）。
    专用 API 优先于网页解析后端：结果结构稳定、无验证码，是生产环境的推荐形态。
    """

    def __init__(self, url: str, key: str = "", name: str = "custom-api") -> None:
        self.url, self.key, self.name = url, key, name

    def search(self, query: str, k: int = 6) -> Tuple[List[SearchResult], Optional[str]]:
        headers = dict(_HEADERS)
        if self.key:
            headers["X-API-Key"] = self.key
        try:
            resp = requests.get(self.url, params={"q": query, "k": k, "num": k},
                                headers=headers, timeout=_SEARCH_TIMEOUT)
        except requests.RequestException as exc:
            return [], f"{self.name}请求失败: {type(exc).__name__}"
        if resp.status_code != 200:
            return [], f"{self.name}返回HTTP {resp.status_code}"
        try:
            data = resp.json()
        except ValueError:
            return [], f"{self.name}返回非JSON响应"
        rows = data.get("results") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return [], f"{self.name}响应缺少 results 数组"
        out: List[SearchResult] = []
        for i, row in enumerate(rows[:k]):
            if not isinstance(row, dict) or not str(row.get("url") or "").strip():
                continue
            url = str(row["url"]).strip()
            out.append(SearchResult(self.name, i + 1, str(row.get("title") or ""),
                                    url, str(row.get("snippet") or ""),
                                    row.get("date"), classify_domain(url)))
        return out, None


# ===========================================================================
# API 型搜索源（search_sources.json 声明 + key_env 环境变量供密钥）
# ===========================================================================

#: 清单文件位置。不做环境变量整套替换（保持改动面最小）；测试用 monkeypatch
#: 换这个常量指向临时文件即可。
_SEARCH_SOURCES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "search_sources.json")
#: 当前支持的清单版本与协议集合；版本不符/协议未知都按 fail-soft 丢弃并留告警。
_API_SOURCES_SCHEMA_VERSION = 1
_API_SOURCE_PROTOCOLS = ("bing", "bocha")
#: API 源失败重试（B）：默认 0 次 = 保留既有"单次请求"语义；JSON 按源可配 max_retries。
#: 有重试时按 retry_backoff_s * 2**attempt 指数退避（有上限），不会把 3 次请求一口气打出去。
_API_RETRY_DEFAULT = 0
_API_RETRY_MAX = 3
_API_RETRY_BACKOFF_DEFAULT = 0.5

#: RSS/Atom 源（C）：抓取上限与条目过滤口径。标准库解析，不新增第三方依赖。
_RSS_TIMEOUT_DEFAULT = 10.0
_RSS_MAX_BYTES = 1_000_000        # 单个 feed 响应上限（1MB）
_RSS_MAX_ITEMS = 40               # 单源最多尝试的条目数（过滤前）
_RSS_MIN_TERM_LEN = 2             # 检索词长度 ≥2 才参与匹配（单字噪音大）
_RSS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

#: 源级字段校验器：合法 → 归一后的配置 dict；不合法 → None（调用方丢弃并告警）。
_API_SOURCE_FIELDS = {
    "id": str, "protocol": str, "base_url": str, "key_env": str,
}


def _warn_sources(reason: str) -> None:
    """对齐 config/rules.py 的告警风格：写进 load_warnings()，不抛异常、不静默。"""
    try:
        from mylocalai.config.rules import add_warning
        add_warning("search_sources.json", reason)
    except Exception:            # noqa: BLE001  告警通道本身不能成为新的故障点
        _logger.warning("search_sources.json: %s", reason)


def _clear_source_warnings() -> None:
    """告警只反映**本次**加载结果（同 config 层"同名只留最新"的口径）：先清上一轮。

    私有借取 `_WARNINGS` 与 add_warning 是同一个存储，清的是且仅是本文件自己的两个键，
    不会碰其它模块的告警（对齐 rules.py "_SHAPE_WARNING_KEYS 只清自己的键"的纪律）。
    """
    try:
        from mylocalai.config.rules import _WARNINGS
        _WARNINGS.pop("search_sources.json", None)
        _WARNINGS.pop("search_sources.json#sources", None)
    except Exception:            # noqa: BLE001
        pass


def _load_sources(path: Optional[str] = None, *,
                  apply_settings: bool = True) -> Dict[str, object]:
    """统一加载 search_sources.json（一次读完：顶层设置 + API 源 + RSS 源）。

    返回 {"exists", "error", "api", "rss"}：api/rss 是**已归一且 enabled** 的配置列表。
    fail-soft 纪律（对齐 config/rules.py 的 _load()）：文件缺失是合法空配置（静默）；
    JSON 损坏 / 根不是对象 / schema_version 不符 → 空配置 + 一条告警（load_warnings 可见）；
    单条坏声明丢弃并合并进同一条告警。**绝不抛异常**：检索主链路不因配置文件笔误而崩。
    告警统一在本函数末尾一次写出（add_warning 同键只留最新一条，分多次写会互相覆盖）。
    apply_settings=False 时只读配置、不回写进程级 _SOURCE_SETTINGS——供
    source_config_summary 这类"只看声明了什么"的只读查询使用（否则每次检索都刷新
    全局设置，会覆盖调用方/测试对 TTL、冷却时长的显式设定）。
    """
    _clear_source_warnings()
    settings: Dict[str, object] = {} if apply_settings else dict(_SOURCE_SETTINGS)
    if apply_settings:
        _SOURCE_SETTINGS.clear()     # 本次加载结果即设置全集（缺失/损坏 → 回默认值）
    warnings: List[str] = []
    result: Dict[str, object] = {"exists": False, "error": "", "api": [], "rss": []}
    file_path = path or _SEARCH_SOURCES_PATH
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        if apply_settings:
            _parse_source_settings({}, warnings, _SOURCE_SETTINGS)
        return result                # 文件被移除 = 明确的不启用，不是故障
    except Exception as exc:         # noqa: BLE001  JSON 损坏、权限问题等
        result["error"] = f"读取失败（{file_path}）：{type(exc).__name__}: {exc}"
        _warn_sources(f"search_sources.json {result['error']}，已退回无结构化源")
        return result
    if not isinstance(data, dict):
        result["error"] = "根节点必须是对象"
        _warn_sources("search_sources.json 根节点必须是对象，已退回无结构化源")
        return result
    version = data.get("schema_version")
    if version != _API_SOURCES_SCHEMA_VERSION:
        result["error"] = f"schema_version 不符（期望 {_API_SOURCES_SCHEMA_VERSION}，实际 {version!r}）"
        _warn_sources(f"search_sources.json {result['error']}，已退回无结构化源")
        return result
    result["exists"] = True
    _parse_source_settings(data, warnings, settings)
    if apply_settings:
        _SOURCE_SETTINGS.update(settings)
    result["api"] = _parse_api_rows(data, warnings, settings)
    result["rss"] = _parse_rss_rows(data, warnings)
    if warnings:
        # 前缀文件名：告警按同一个键存（同键只留最新），读的人要能看出是哪份配置。
        _warn_sources("search_sources.json：" + "；".join(warnings))
    return result


def _parse_source_settings(data: Dict[str, object], warnings: List[str],
                           target: Dict[str, object]) -> None:
    """顶层设置解析（P1-5 + A）：非法值告警 + 回默认；TTL ≤0 是显式关闭开关。

    target 是本次加载的设置字典（通常就是 _SOURCE_SETTINGS；只读查询用局部副本）。
    """
    raw_threshold = data.get("failure_skip_threshold")
    if raw_threshold is None:
        target["failure_skip_threshold"] = _FAILURE_SKIP_THRESHOLD_DEFAULT
    elif (isinstance(raw_threshold, bool) or not isinstance(raw_threshold, (int, float))
          or raw_threshold <= 0):
        warnings.append(f"failure_skip_threshold 不合法（{raw_threshold!r}），"
                        f"回默认 {_FAILURE_SKIP_THRESHOLD_DEFAULT}")
        target["failure_skip_threshold"] = _FAILURE_SKIP_THRESHOLD_DEFAULT
    else:
        target["failure_skip_threshold"] = raw_threshold
    raw_ttl = data.get("query_cache_ttl_s")
    if raw_ttl is None:
        target["query_cache_ttl_s"] = _QUERY_CACHE_TTL_DEFAULT
    elif isinstance(raw_ttl, bool) or not isinstance(raw_ttl, (int, float)):
        warnings.append(f"query_cache_ttl_s 不合法（{raw_ttl!r}），"
                        f"回默认 {_QUERY_CACHE_TTL_DEFAULT:g}")
        target["query_cache_ttl_s"] = _QUERY_CACHE_TTL_DEFAULT
    else:
        target["query_cache_ttl_s"] = float(raw_ttl)
    raw_cooldown = data.get("cooldown_seconds")
    if raw_cooldown is None:
        target["cooldown_seconds"] = _COOLDOWN_SECONDS_DEFAULT
    elif (isinstance(raw_cooldown, bool) or not isinstance(raw_cooldown, (int, float))
          or raw_cooldown <= 0):
        warnings.append(f"cooldown_seconds 不合法（{raw_cooldown!r}），"
                        f"回默认 {_COOLDOWN_SECONDS_DEFAULT:g}")
        target["cooldown_seconds"] = _COOLDOWN_SECONDS_DEFAULT
    else:
        target["cooldown_seconds"] = min(float(raw_cooldown), _COOLDOWN_SECONDS_MAX)


def _as_number(raw: object, default: float, *, minimum: float = 0.0,
               maximum: Optional[float] = None) -> Tuple[float, bool]:
    """数值字段校验：合法返回 (值, True)；非法/越界返回 (默认, False)（调用方决定告警）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default, False
    value = float(raw)
    if value < minimum or (maximum is not None and value > maximum):
        return default, False
    return value, True


def _parse_api_rows(data: Dict[str, object], warnings: List[str],
                    settings: Dict[str, object]) -> List[Dict[str, object]]:
    """API 源段解析 + 逐字段校验（B）：坏值回默认并告警，绝不抛。"""
    rows = data.get("sources")
    if rows is None:
        return []                # 没写 sources 段 = 无 API 源（合法）
    if not isinstance(rows, list):
        warnings.append("search_sources.json#sources 必须是数组，已退回无 API 源")
        return []
    out: List[Dict[str, object]] = []
    dropped: List[str] = []
    threshold = settings.get("failure_skip_threshold", _FAILURE_SKIP_THRESHOLD_DEFAULT)
    for item in rows:
        if not isinstance(item, dict):
            dropped.append("（元素不是对象）")
            continue
        sid = str(item.get("id") or "").strip()
        protocol = str(item.get("protocol") or "").strip().lower()
        base_url = str(item.get("base_url") or "").strip()
        key_env = str(item.get("key_env") or "").strip()
        if not sid:
            dropped.append("（缺 id）")
            continue
        if protocol not in _API_SOURCE_PROTOCOLS:
            dropped.append(f"{sid}（protocol 未知: {protocol or '空'}）")
            continue
        if not _is_http_url(base_url):
            dropped.append(f"{sid}（base_url 非 http/https 或缺主机）")
            continue
        if not key_env:
            dropped.append(f"{sid}（缺 key_env）")
            continue
        weight, weight_ok = _as_number(item.get("weight", 1.0), 1.0, minimum=0.0)
        if not weight_ok:
            warnings.append(f"{sid} 的 weight 不合法（{item.get('weight')!r}），回默认 1.0")
        timeout, timeout_ok = _as_number(item.get("timeout_s", _SEARCH_TIMEOUT),
                                         float(_SEARCH_TIMEOUT), minimum=0.1,
                                         maximum=120.0)
        if not timeout_ok:
            warnings.append(f"{sid} 的 timeout_s 不合法（{item.get('timeout_s')!r}），"
                            f"回默认 {_SEARCH_TIMEOUT}")
        retries_raw = item.get("max_retries", _API_RETRY_DEFAULT)
        retries, retries_ok = _as_number(retries_raw, float(_API_RETRY_DEFAULT),
                                         minimum=0.0, maximum=float(_API_RETRY_MAX))
        if not retries_ok:
            warnings.append(f"{sid} 的 max_retries 不合法（{retries_raw!r}），"
                            f"回默认 {_API_RETRY_DEFAULT}")
        backoff_raw = item.get("retry_backoff_s", _API_RETRY_BACKOFF_DEFAULT)
        backoff, backoff_ok = _as_number(backoff_raw, _API_RETRY_BACKOFF_DEFAULT,
                                         minimum=0.0, maximum=5.0)
        if not backoff_ok:
            warnings.append(f"{sid} 的 retry_backoff_s 不合法（{backoff_raw!r}），"
                            f"回默认 {_API_RETRY_BACKOFF_DEFAULT}")
        enabled = item.get("enabled", True)
        out.append({"id": sid, "protocol": protocol, "base_url": base_url,
                    "key_env": key_env, "enabled": bool(enabled),
                    "weight": weight, "timeout_s": timeout,
                    "max_retries": int(retries), "retry_backoff_s": backoff,
                    "failure_skip_threshold": threshold})
    if dropped:
        warnings.append(f"search_sources.json#sources 里这些条不合规，已丢弃：{'；'.join(dropped)}")
    return [cfg for cfg in out if cfg["enabled"]]


def _parse_rss_rows(data: Dict[str, object],
                    warnings: List[str]) -> List[Dict[str, object]]:
    """RSS/Atom 源段解析（C）：缺省是合法空列表；坏条丢弃 + 告警，绝不抛。"""
    rows = data.get("rss_sources")
    if rows is None:
        return []                # 没写 rss_sources 段 = 无 RSS 源（合法）
    if not isinstance(rows, list):
        warnings.append("search_sources.json#rss_sources 必须是数组，已退回无 RSS 源")
        return []
    out: List[Dict[str, object]] = []
    dropped: List[str] = []
    for item in rows:
        if not isinstance(item, dict):
            dropped.append("（元素不是对象）")
            continue
        url = str(item.get("url") or "").strip()
        name = str(item.get("name") or item.get("id") or "").strip()
        if not _is_http_url(url):
            dropped.append(f"{name or '（未命名）'}（url 非 http/https 或缺主机）")
            continue
        if not name:
            name = normalize_host(url) or "rss"
        timeout, timeout_ok = _as_number(item.get("timeout_s", _RSS_TIMEOUT_DEFAULT),
                                         _RSS_TIMEOUT_DEFAULT, minimum=0.1, maximum=120.0)
        if not timeout_ok:
            warnings.append(f"{name} 的 timeout_s 不合法（{item.get('timeout_s')!r}），"
                            f"回默认 {_RSS_TIMEOUT_DEFAULT:g}")
        weight, weight_ok = _as_number(item.get("weight", 1.0), 1.0, minimum=0.0)
        if not weight_ok:
            warnings.append(f"{name} 的 weight 不合法（{item.get('weight')!r}），回默认 1.0")
        out.append({"name": name, "url": url, "enabled": bool(item.get("enabled", True)),
                    "weight": weight, "timeout_s": timeout,
                    "allow_domains": _domain_list(item.get("allow_domains")),
                    "deny_domains": _domain_list(item.get("deny_domains"))})
    if dropped:
        warnings.append(f"search_sources.json#rss_sources 里这些条不合规，已丢弃："
                        f"{'；'.join(dropped)}")
    return [cfg for cfg in out if cfg["enabled"]]


def _domain_list(raw: object) -> List[str]:
    """域名过滤字段归一：只收非空字符串，小写、去空白；类型写坏当空列表。"""
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(d).strip().lower() for d in raw if str(d).strip()]


def _is_http_url(url: str) -> bool:
    """URL 校验：必须 http/https 且有主机名（ftp://、javascript:、空主机都算非法）。"""
    if not str(url or "").startswith(("http://", "https://")):
        return False
    try:
        return bool(urlparse(str(url)).hostname)
    except ValueError:
        return False


def load_api_source_configs(path: Optional[str] = None) -> List[Dict[str, object]]:
    """读 API 型搜索源清单，返回归一后的配置 dict 列表。

    fail-soft 纪律（对齐 config/rules.py 的 _load()）：
      - 文件不存在 → 合法配置（未启用 API 源），静默返回 []；
      - JSON 损坏 / 根不是对象 / schema_version 不符 / sources 段不是数组
        → 返回 [] 并经 add_warning 留告警（load_warnings() 可见）；
      - 单个源声明缺 id/protocol 未知/base_url 非法（非 http/https 或缺主机）/
        缺 key_env → 丢弃该条并合并告警；
      - 权重/超时/重试/退避字段类型或范围写坏 → 回默认值 + 告警（不再静默）；
      - 绝不抛异常：检索主链路不因一个配置文件的笔误而崩。
    每次调用都重新读盘（文件很小，检索本身是网络 IO 量级）：改 JSON 即生效，
    不需要重启服务。
    顶层设置（P1-5 多源融合 + A 反爬冷却）：failure_skip_threshold（连续失败跳过
    阈值，默认 3）、query_cache_ttl_s（查询缓存 TTL 秒数，默认 300，≤0 = 显式关闭）
    与 cooldown_seconds（反爬命中后的源冷却秒数，默认 1800）在每次加载时刷新进
    _SOURCE_SETTINGS，并随每条源配置附带 threshold；值非法时告警 + 回默认，
    文件缺失/损坏时设置整体回默认。
    """
    return list(_load_sources(path)["api"])          # type: ignore[arg-type]


def load_rss_source_configs(path: Optional[str] = None) -> List[Dict[str, object]]:
    """读 RSS/Atom 源清单（search_sources.json 的 rss_sources 段），返回归一配置列表。

    与 load_api_source_configs 共用同一个读盘/告警通道：缺省是合法空列表，
    坏条丢弃 + 告警，绝不抛异常。字段：name（引擎名，缺省取 feed 主机名）、url、
    enabled、weight（预留融合权重）、timeout_s、allow_domains / deny_domains
    （条目域名白/黑名单，空 = 不过滤）。
    """
    return list(_load_sources(path)["rss"])          # type: ignore[arg-type]


def source_config_summary(path: Optional[str] = None) -> Dict[str, object]:
    """配置层摘要（不看密钥是否就绪）：调用方据此判断"有没有结构化备选源"。

    返回 {"sources_configured": bool, "api_sources": [id…], "rss_sources": [name…],
          "sources_file_exists": bool, "error": str}。
    sources_configured=False 表示只有网页解析源（遇反爬就没有备用通道）——
    这正是 2026-11-22 实测暴露的短板，本字段让它可见而不是静默。
    只读：不回写 _SOURCE_SETTINGS（不覆盖调用方对 TTL/冷却时长的显式设定）。
    """
    doc = _load_sources(path, apply_settings=False)
    api = [str(cfg.get("id") or "") for cfg in (doc.get("api") or [])]
    rss = [str(cfg.get("name") or "") for cfg in (doc.get("rss") or [])]
    return {"sources_configured": bool(api or rss), "api_sources": api,
            "rss_sources": rss, "sources_file_exists": bool(doc.get("exists")),
            "error": str(doc.get("error") or "")}



def _is_deterministic_error(err: str) -> bool:
    """错误串是否"重试也没用"：HTTP 4xx（鉴权/限流/参数）与协议未知。

    连接失败、超时、5xx、非 JSON 响应都可能是一次性抖动，才值得退避重试。
    """
    text = str(err or "")
    if "协议未知" in text:
        return True
    match = re.search(r"返回HTTP (\d{3})", text)
    return bool(match and 400 <= int(match.group(1)) < 500)


def _query_terms(query: str) -> List[str]:
    """检索词 → 关键词列表：按空白/中英文标点切分，保留长度 ≥2 的词（单字噪音大）。

    切不出可用词时退回整串（保持"至少按原文匹配一次"的语义，不静默丢查询）。
    """
    parts = [p.strip() for p in re.split(r"[\s,，、;；:：/|]+", str(query or "").strip())]
    terms = [p for p in parts if len(p) >= _RSS_MIN_TERM_LEN]
    if terms:
        return terms
    whole = str(query or "").strip()
    return [whole] if whole else []


def _feed_date(raw: str, url: str) -> Optional[str]:
    """解析 pubDate/published/updated → YYYY-MM-DD；解析失败退回 extract_date，再失败 None。

    支持 RFC 822（RSS 常见 "Wed, 20 Nov 2026 08:00:00 GMT"）与 ISO 8601（Atom 常见）。
    全程不抛异常：时间字段写坏只是少一个日期，不该让整条结果消失。
    """
    text = str(raw or "").strip()
    if text:
        try:
            return email.utils.parsedate_to_datetime(text).strftime("%Y-%m-%d")
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
        try:
            from datetime import datetime as _datetime
            return _datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OverflowError):
            pass
    try:
        return extract_date(text, url)
    except Exception:            # noqa: BLE001  日期解析永不让检索失败
        return None


def _decode_feed_bytes(raw: bytes) -> str:
    """按 XML 声明 → utf-8 → gbk 顺序解码 feed 内容（中文 RSS 常见 gb2312）。"""
    match = re.search(rb'encoding=["\']([\w-]+)', raw[:2048])
    for encoding in ([match[1].decode("ascii", "ignore")] if match else []) + \
            ["utf-8", "gbk", "gb18030"]:
        try:
            return raw[:_RSS_MAX_BYTES].decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw[:_RSS_MAX_BYTES].decode("utf-8", "replace")


def _localname(tag: object) -> str:
    """XML 标签的本地名（去命名空间前缀），RSS/Atom/RDF 混排时靠它认字段。"""
    return str(tag or "").rsplit("}", 1)[-1]


def _child_text(item: ET.Element, names: Tuple[str, ...]) -> str:
    """取第一个本地名命中的子元素的文本（压空白）；没有就返回空串。"""
    for child in list(item):
        if _localname(child.tag) in names and child.text:
            return re.sub(r"\s+", " ", child.text).strip()
    return ""


def _item_link(item: ET.Element) -> str:
    """RSS 的 <link>文本</link> 与 Atom 的 <link href="…"/> 都认。"""
    for child in list(item):
        if _localname(child.tag) != "link":
            continue
        href = child.get("href")
        if href and href.strip():
            return href.strip()
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


class RssProvider:
    """RSS/Atom 结构化源（C）：按关键词过滤条目，产出与网页源同形状的 SearchResult。

    为什么要有它：中文搜索引擎反爬（百度 wappass、Bing 同意页、360/Sogou 风控）
    会让发现层 0 结果，而 RSS 是**结构化、无验证码、可长期稳定抓取**的备选通道。
    行为约定：
      - XML 解析只用标准库 xml.etree.ElementTree（不新增第三方依赖）；解析前剥掉
        DOCTYPE/ENTITY 声明（stdlib 本就不解析外部实体，剥掉是纵深防御）；
      - 单源失败只影响自己（返回 ([], 中文原因)），search_all 里其它源照常；
      - 条目按 URL 归一化去重，按 allow_domains/deny_domains 做域名过滤，
        非 http(s)/无主机的链接直接丢弃；
      - pubDate/published 解析失败不抛（_feed_date 多路兜底，最差 date=None）；
      - 命中验证页/拦截页（例如 feed 地址被反爬换成了 HTML 验证页）走共用判定
        detect_verification_page，与网页源同一套冷却；
      - 绝不抛异常：任何失败都返回 ([], 原因)。
    """

    def __init__(self, config: Dict[str, object]) -> None:
        self.name = str(config.get("name") or config.get("id") or "rss").strip() or "rss"
        self.url = str(config.get("url") or "").strip()
        self.weight = float(config.get("weight") or 1.0)
        self.timeout_s = float(config.get("timeout_s") or _RSS_TIMEOUT_DEFAULT)
        self.allow_domains = tuple(_domain_list(config.get("allow_domains")))
        self.deny_domains = tuple(_domain_list(config.get("deny_domains")))

    # ---- 跳过判定（与 API 源同一接口口径：RSS 不需要密钥，恒不跳过） ----

    def skip_reason(self) -> str:
        """RSS 源无密钥要求；只有 url 缺失才算配置性跳过（配置层已拦，兜底用）。"""
        return "" if _is_http_url(self.url) else "rss_url_missing"

    def search(self, query: str, k: int = 6) -> Tuple[List[SearchResult], Optional[str]]:
        cooling = source_cooldown(self.name)
        if cooling.get("active"):
            return [], (f"{self.name}已冷却（{cooling.get('reason')}，"
                        f"剩余 {int(cooling.get('remaining_s', 0) or 0)} 秒）")
        items, err = self._fetch_items()
        if err:
            return [], f"{self.name}{err}"
        terms = _query_terms(query)
        if not terms:
            return [], None
        out: List[SearchResult] = []
        seen: set = set()
        for item in items:
            url = str(item.get("url") or "").strip()
            if not _is_http_url(url) or not self._domain_allowed(url):
                continue
            key = _dedup_url(url)
            if not key or key in seen:
                continue
            haystack = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
            if not any(term.lower() in haystack for term in terms):
                continue
            seen.add(key)
            out.append(SearchResult(self.name, len(out) + 1,
                                    str(item.get("title") or ""), url,
                                    str(item.get("snippet") or ""),
                                    _feed_date(str(item.get("published") or ""), url),
                                    classify_domain(url)))
            if len(out) >= max(1, int(k)):
                break
        return out, None

    # ---- 内部：抓取与解析 ----

    def _domain_allowed(self, url: str) -> bool:
        """域名过滤：白名单非空时只收命中的域名；黑名单命中的一律丢弃。"""
        host = normalize_host(url)
        if not host:
            return False
        if self.deny_domains and any(host == d or host.endswith("." + d)
                                     for d in self.deny_domains):
            return False
        if self.allow_domains and not any(host == d or host.endswith("." + d)
                                          for d in self.allow_domains):
            return False
        return True

    def _fetch_items(self) -> Tuple[List[Dict[str, str]], str]:
        """抓一个 feed → (条目列表, 错误串)。任何失败都返回 ([], 原因)，不抛。"""
        try:
            resp = requests.get(self.url, headers=_RSS_HEADERS, timeout=self.timeout_s,
                                stream=True)
        except requests.RequestException as exc:
            return [], f"请求失败: {type(exc).__name__}"
        try:
            status = int(getattr(resp, "status_code", 0) or 0)
            raw = getattr(resp, "content", b"") or b""
        finally:
            close = getattr(resp, "close", None)
            if callable(close):
                close()
        text = _decode_feed_bytes(raw)
        if status in (403, 429):
            reason = detect_verification_page(text) or f"返回HTTP {status}"
            return [], _note_blocked(self.name, reason)
        if status != 200:
            return [], f"返回HTTP {status}"
        if len(raw) > _RSS_MAX_BYTES:
            return [], "响应过大，已丢弃"
        # feed 地址被反爬换成 HTML 验证页：走与网页源同一套判定，不当成"空 feed"。
        # 只认关键词/结构特征（min_text=0 关掉正文长度阈值）——合法 feed 的可见正文
        # 本来就短（几十到几百字符），套长度阈值会把正常 feed 误判成拦截页。
        reason = detect_verification_page(text, min_text=0)
        if reason:
            return [], _note_blocked(self.name, reason)
        return self._parse_feed(text)

    def _parse_feed(self, text: str) -> Tuple[List[Dict[str, str]], str]:
        """解析 RSS 2.0 / Atom / RDF → (条目列表, 错误串)；坏 XML 返回 ([], 原因)。

        坏 XML 与"空 feed"必须区分：前者是源坏了（上层应标 degraded），
        后者是合法的 0 条。两种都不抛异常。
        """
        payload = text or ""
        if "<!DOCTYPE" in payload or "<!ENTITY" in payload:
            payload = re.sub(r"<!DOCTYPE[^>]*>", "", payload, flags=re.I)
            payload = re.sub(r"<!ENTITY[^>]*>", "", payload, flags=re.I)
        try:
            root = ET.fromstring(payload)
        except (ET.ParseError, ValueError):
            return [], "XML 解析失败"
        root_tag = _localname(root.tag)
        items: List[ET.Element] = []
        if root_tag == "rss":
            channel = None
            for child in list(root):
                if _localname(child.tag) == "channel":
                    channel = child
                    break
            if channel is not None:
                items = [c for c in list(channel) if _localname(c.tag) == "item"]
        elif root_tag == "feed":
            items = [c for c in list(root) if _localname(c.tag) == "entry"]
        elif root_tag in ("RDF", "rdf"):
            items = [c for c in list(root) if _localname(c.tag) == "item"]
        out: List[Dict[str, str]] = []
        for item in items[:_RSS_MAX_ITEMS]:
            title = _strip_tags(_child_text(item, ("title",)))
            url = _item_link(item)
            summary = _child_text(item, ("description", "summary", "content", "abstract"))
            snippet = _strip_tags(summary)
            published = _child_text(item, ("pubDate", "published", "updated", "date"))
            if not title and not snippet:
                continue
            out.append({"title": title, "url": url, "snippet": snippet,
                        "published": published})
        return out, ""


def _build_rss_providers() -> List["RssProvider"]:
    """清单 → RSS 源执行器列表（默认装配用）：单条构造失败不拖垮检索（fail-soft）。"""
    providers: List[RssProvider] = []
    for cfg in load_rss_source_configs():
        try:
            providers.append(RssProvider(cfg))
        except Exception:        # noqa: BLE001  配置错误的单个源不拖垮检索
            continue
    return providers


def _build_api_providers() -> List["ApiSearchProvider"]:
    """清单 → 可用执行器实例列表（默认装配用）。

    只装配"enabled 且已配密钥"的源（跳过判定与 ApiSearchProvider.skip_reason()
    同一口径）；单个实例构造失败不拖垮检索（fail-soft）。
    """
    providers: List[ApiSearchProvider] = []
    for cfg in load_api_source_configs():
        try:
            p = ApiSearchProvider(cfg)
        except Exception:        # noqa: BLE001  配置错误的单个源不拖垮检索
            continue
        if not p.skip_reason():
            providers.append(p)
    return providers


def default_providers() -> List[object]:
    """按环境变量组装后端列表（源优先级：结构化 API → RSS → 网页解析）：

      1. search_sources.json 里**已启用且已配密钥**的 API 型源（最前：结构稳定、
         无验证码，生产推荐形态）；
      2. search_sources.json 里**已启用**的 RSS/Atom 源（结构化备选：网页源被反爬
         拦住时仍有结果）；
      3. RUMOR_SEARCH_BACKENDS 声明的网页解析源（逗号分隔：custom-api,so360,baidu，
         默认 so360,baidu）；配了 RUMOR_SEARCH_API_URL 时 custom-api 才生效。
    一个结构化源都没启用时返回值与从前逐位一致（既有装配契约不变，守卫用例锁定）。
    结构化源缺失时 search_all 返回 sources_configured=false，让"只有网页源、遇反爬
    无备用"这件事可见（见 source_config_summary / /api/health）。
    """
    order = [s.strip().lower() for s in
             os.environ.get("RUMOR_SEARCH_BACKENDS", "so360,baidu").split(",") if s.strip()]
    mapping: Dict[str, Callable[[], object]] = {
        "so360": So360Provider,
        "baidu": BaiduProvider,
        "custom-api": lambda: GenericAPIProvider(
            os.environ["RUMOR_SEARCH_API_URL"],
            os.environ.get("RUMOR_SEARCH_API_KEY", ""),
            os.environ.get("RUMOR_SEARCH_API_NAME", "custom-api")),
    }
    out: List[object] = []
    for name in order:
        if name == "custom-api" and not os.environ.get("RUMOR_SEARCH_API_URL"):
            continue
        factory = mapping.get(name)
        if factory:
            try:
                out.append(factory())
            except Exception:      # noqa: BLE001  配置错误的单个后端不拖垮检索
                continue
    structured = _build_api_providers() + _build_rss_providers()
    if structured:
        return structured + out
    return out or [So360Provider(), BaiduProvider()]



class ApiSearchProvider:
    """API 型搜索源执行器：与爬虫源（So360/Baidu）并列的第三类后端。

    - 请求/响应形状按 protocol 分发（bing=GET + Ocp-Apim-Subscription-Key 头、
      响应 webPages.value[]；bocha=POST + Bearer 头、响应 data.webPages.value[]），
      统一 normalize 成与爬虫源相同的 SearchResult 形状（title/url/snippet/date，
      domain 走既有 classify_domain 口径）；
    - 密钥从 key_env 指定的环境变量读取（对齐 providers.json 的 key_env 模式）；
      未配置 → skip_reason() 给出原因，search_all 计一次 skipped 且**不发请求**
      （不报错、不算降级主因）。默认装配（default_providers）只收已配密钥的源，
      skip 机制兜住显式装配与"装配后才失钥"的运行时窗口；
    - 授权闸与爬虫源同一条：HTTP 只会发生在 search_all 的 public-search 闸之后
      （未授权时 search_all 在构建任何 provider 之前就返回 blocked）；
    - fail-soft：单次失败返回 ([], 原因) 并累计 consecutive_failures（实例计数 +
      进程级 _API_SOURCE_HEALTH 账本同写，账本读写加锁）；连续失败 ≥
      failure_skip_threshold（search_sources.json 顶层可配，默认 3）时
      health_skip_reason() 给出原因，search_all 据此做健康度动态降权（P1-5）：
      本次调用先跳过该源（零外发、记 skipped），跳过一次账本降温 1
      （cool_health_skip），源不会永久出局；成功一次即清零回归
      （"连续失败源暂时跳过并记数，恢复后自动回归"）。
    - 失败重试（B）：max_retries（默认 0 = 既有单次语义，JSON 按源可配，上限 3）
      次重试按 retry_backoff_s * 2**attempt 指数退避（time.sleep），**不会把 3 次
      请求一口气打出去**；重试次数计入请求次数，最终失败仍只记一次账本。
    """

    def __init__(self, config: Dict[str, object]) -> None:
        self.id = str(config.get("id") or "api-source")
        # engines 计数键与既有引擎名（"360"/"baidu"）同一槽位口径：用源 id 作引擎名。
        self.name = self.id
        self.protocol = str(config.get("protocol") or "").strip().lower()
        self.base_url = str(config.get("base_url") or "").strip()
        self.key_env = str(config.get("key_env") or "").strip()
        self.weight = float(config.get("weight") or 1.0)
        self.timeout_s = float(config.get("timeout_s") or _SEARCH_TIMEOUT)
        threshold = config.get("failure_skip_threshold", _FAILURE_SKIP_THRESHOLD_DEFAULT)
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or threshold <= 0):
            threshold = _FAILURE_SKIP_THRESHOLD_DEFAULT
        self.failure_skip_threshold = threshold
        retries = config.get("max_retries", _API_RETRY_DEFAULT)
        if isinstance(retries, bool) or not isinstance(retries, (int, float)) \
                or not 0 <= retries <= _API_RETRY_MAX:
            retries = _API_RETRY_DEFAULT
        self.max_retries = int(retries)
        backoff = config.get("retry_backoff_s", _API_RETRY_BACKOFF_DEFAULT)
        if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) \
                or not 0 <= backoff <= 5:
            backoff = _API_RETRY_BACKOFF_DEFAULT
        self.retry_backoff_s = float(backoff)
        self.consecutive_failures = 0

    # ---- 跳过判定（search_all 在发任何请求前检查） ----

    def skip_reason(self) -> str:
        """应跳过本源时返回原因串；可正常运行时返回空串。密钥缺失是主场景。"""
        if not self.key_env or not os.environ.get(self.key_env, "").strip():
            return f"api_key_missing:{self.key_env or 'key_env未配置'}"
        return ""

    # ---- 健康度动态降权（P1-5：连续失败源暂时跳过并记数，恢复后自动回归） ----

    def health_skip_reason(self) -> str:
        """连续失败 ≥ failure_skip_threshold 时返回原因串（本次 search_all 应跳过）。

        失败账本以进程级 _API_SOURCE_HEALTH 为准（默认装配每次 search_all 都新建
        实例，实例计数器跨不过调用边界；账本按源 id 记账、跨调用生效）——无登记
        时回退实例计数（显式装配的持久实例与账本同写，两者一致）。与 skip_reason
        分工：那是配置性跳过（密钥缺失），这是运行时健康度，两者都进 skipped 计数。
        """
        fails = _health_get(self.id, self.consecutive_failures)
        if fails >= self.failure_skip_threshold:
            return (f"unhealthy:consecutive_failures={int(fails)}"
                    f">={self.failure_skip_threshold}")
        return ""

    def cool_health_skip(self) -> None:
        """健康跳过一次给账本降温 1：被跳过的源下一轮即低于阈值、自动回归探针。

        探针成功清零回归；探针失败账本重新累加、回到跳过态（最坏情况跳过/探针
        交替，一半调用省掉该源的超时等待）——保证"暂时跳过"，不会永久出局。
        """
        cooled = max(0, _health_get(self.id, self.consecutive_failures) - 1)
        _health_set(self.id, cooled)
        self.consecutive_failures = cooled

    # ---- 请求与 normalize ----

    def search(self, query: str, k: int = 6) -> Tuple[List[SearchResult], Optional[str]]:
        cooling = source_cooldown(self.id)
        if cooling.get("active"):
            return [], (f"{self.id}已冷却（{cooling.get('reason')}，"
                        f"剩余 {int(cooling.get('remaining_s', 0) or 0)} 秒）")
        results, err = self._request_with_retry(query, max(1, int(k)))
        if err:
            self.consecutive_failures += 1
            _health_set(self.id, self.consecutive_failures)   # 跨调用健康账本（加锁）
            _logger.warning("api-source %s 连续失败 %d 次: %s",
                            self.id, self.consecutive_failures, err)
            return [], err
        self.consecutive_failures = 0
        _health_set(self.id, 0)
        return results, None

    def _request_with_retry(self, query: str,
                            k: int) -> Tuple[List[SearchResult], Optional[str]]:
        """有限次重试 + 指数退避（B）：默认 0 次重试 = 既有单次语义，逐位不变。

        HTTP 4xx（含 401/403/429）是**确定性失败**，重试没有意义，直接返回；
        连接/超时/5xx/坏 JSON 才重试——避免把错误密钥打到限流黑名单里。
        """
        attempt = 0
        while True:
            results, err = self._request(query, k)
            if not err or attempt >= self.max_retries or _is_deterministic_error(err):
                return results, err
            attempt += 1
            delay = min(self.retry_backoff_s * (2 ** (attempt - 1)), 5.0)
            if delay > 0:
                time.sleep(delay)
            _logger.info("api-source %s 第 %d 次重试（退避 %.1fs）：%s",
                         self.id, attempt, delay, err)

    def _request(self, query: str, k: int) -> Tuple[List[SearchResult], Optional[str]]:
        key = os.environ.get(self.key_env, "").strip()
        if self.protocol == "bing":
            headers = dict(_HEADERS)
            headers["Ocp-Apim-Subscription-Key"] = key
            try:
                resp = requests.get(self.base_url, params={"q": query, "count": k, "offset": 0},
                                    headers=headers, timeout=self.timeout_s)
            except requests.RequestException as exc:
                return [], f"{self.id}请求失败: {type(exc).__name__}"
            if resp.status_code != 200:
                return [], f"{self.id}返回HTTP {resp.status_code}"
            try:
                data = resp.json()
            except ValueError:
                return [], f"{self.id}返回非JSON响应"
            rows = data.get("webPages") if isinstance(data, dict) else None
            rows = rows.get("value") if isinstance(rows, dict) else None
            return self._normalize(rows, k, snippet_key="snippet"), None
        if self.protocol == "bocha":
            headers = dict(_HEADERS)
            headers["Authorization"] = f"Bearer {key}"
            headers["Content-Type"] = "application/json"
            payload = {"query": query, "count": k, "summary": True}
            try:
                resp = requests.post(self.base_url, json=payload, headers=headers,
                                     timeout=self.timeout_s)
            except requests.RequestException as exc:
                return [], f"{self.id}请求失败: {type(exc).__name__}"
            if resp.status_code != 200:
                return [], f"{self.id}返回HTTP {resp.status_code}"
            try:
                data = resp.json()
            except ValueError:
                return [], f"{self.id}返回非JSON响应"
            inner = data.get("data") if isinstance(data, dict) else None
            rows = inner.get("webPages") if isinstance(inner, dict) else None
            rows = rows.get("value") if isinstance(rows, dict) else None
            # 博查摘要字段是 summary；兼容个别版本回退 snippet。
            return self._normalize(rows, k, snippet_key="summary"), None
        return [], f"{self.id}协议未知: {self.protocol}"

    def _normalize(self, rows: object, k: int, snippet_key: str) -> List[SearchResult]:
        """API 行 → SearchResult（title/url/snippet/date + 既有 domain 口径）。"""
        if not isinstance(rows, list):
            return []
        out: List[SearchResult] = []
        for i, row in enumerate(rows[:k]):
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            title = _strip_tags(str(row.get("name") or ""))
            snippet = _strip_tags(str(row.get(snippet_key) or row.get("snippet") or ""))
            date = extract_date(str(row.get("dateLastCrawled") or ""), url)
            out.append(SearchResult(self.name, i + 1, title, url, snippet, date,
                                    classify_domain(url)))
        return out


# ---------------------------------------------------------------------------
# 查询级缓存（P1-5 第三件事：重复查询命中缓存，零外发零等待）
# ---------------------------------------------------------------------------

def _provider_sig(p: object) -> str:
    """缓存键里的"源集合"身份：类型 + 引擎名。默认装配每次重建实例，但签名
    稳定（同一装配跨调用同键）；装配变了（改 JSON / 配 key / 换后端）键就变，
    不会拿旧装配的结果糊新装配。"""
    return f"{type(p).__name__}:{getattr(p, 'name', '') or ''}"


def _query_cache_ttl_s() -> float:
    """查询缓存 TTL（秒）：search_sources.json 顶层 query_cache_ttl_s（load 时
    刷新进 _SOURCE_SETTINGS），≤0 表示显式关闭；缺失/非法回默认 300。"""
    ttl = _SOURCE_SETTINGS.get("query_cache_ttl_s")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        return _QUERY_CACHE_TTL_DEFAULT
    return float(ttl)


def _query_cache_get(key: Tuple) -> Optional[Dict[str, object]]:
    """缓存命中返回深拷贝（调用方改动不会污染缓存）；关闭/TTL 过期/未命中 → None。

    只读本地内存，不属于"外发"；但调用点在授权闸之后——未授权时整条检索路径
    在闸上就返回了，连缓存都不查（维持既有"未授权零外发"口径不变）。
    """
    ttl = _query_cache_ttl_s()
    if ttl <= 0:
        return None
    entry = _QUERY_CACHE.get(key)
    if entry is None:
        return None
    expires, payload = entry
    if time.time() >= expires:
        _QUERY_CACHE.pop(key, None)      # TTL 过期：丢弃，走真实检索
        return None
    return copy.deepcopy(payload)


def _query_cache_put(key: Tuple, payload: Dict[str, object]) -> None:
    """写入查询缓存（"只存完全成功"的判定在 search_all）；TTL ≤0 时空操作。"""
    ttl = _query_cache_ttl_s()
    if ttl <= 0:
        return
    if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
        now = time.time()
        for stale in [k for k, (exp, _p) in _QUERY_CACHE.items() if now >= exp]:
            _QUERY_CACHE.pop(stale, None)     # 先清过期
        while len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
            _QUERY_CACHE.pop(next(iter(_QUERY_CACHE)))   # 仍满则按插入序逐出最旧
    _QUERY_CACHE[key] = (time.time() + ttl, copy.deepcopy(payload))


def search_all(query: str, k: int = 6,
               providers: Optional[List[object]] = None,
               *, authorized: Optional[bool] = None,
               domains: Optional[List[str]] = None) -> Dict[str, object]:
    """多后端搜索 + 按 URL 去重合并（跨源融合）。返回 {query, results, engines}，绝不抛异常。

    providers 缺省时用 default_providers()（环境变量可插拔专用 API，外加
    search_sources.json 声明的 API 型源）。API 型源带 skip_reason()：授权闸放行后、
    发任何请求之前先检查——密钥缺失等配置性原因 → 该源计一次 skipped 且零 HTTP
    （不报错、不算降级主因）；顶层仅在 skipped>0 时多一个 "skipped" 键，未配置
    任何 API 源时返回结构与从前逐位不变。
    健康度动态降权（P1-5）：API 型源另带 health_skip_reason()——连续失败 ≥
    failure_skip_threshold（默认 3，JSON 顶层可配）的源本次调用先跳过（零外发、
    记 skipped 原因），跳过一次账本降温 1，源不会永久出局；成功一次即清零回归。
    跨源融合口径不变：多源结果按 _dedup_url 归一化去重合并，先到者优先（同 URL
    不同摘要不重复计），融合后总数尊重既有 limit 口径（不传 domains = k 条，
    传了 = 每变体至多 k 条）；全部源失败时如实返回 0 条，每个引擎槽位留下
    ok=False + error（上层 SearchDiscoveryProvider 据此标 degraded）。
    查询级缓存（P1-5）：只挂默认装配路径（providers=None）——显式 providers 是
    测试/本地适配器自有的传输，缓存会冻结其语义、也会让同签名打桩跨用例串扰，
    故显式路径永不读写缓存。同一 (query, domains, k, 源集合签名) 的"完全成功"
    结果（结果非空、零错误、零跳过）在 TTL（query_cache_ttl_s，默认 300s，≤0
    关闭）内直接复用，命中零外发；缓存读的是本地内存，但调用点在授权闸之后，
    未授权时在闸上就返回，连缓存都不查——"未授权零外发"语义原封不动。
    domains（可选，领域监测用）：为每个域名构造一次 `site:域名` 查询变体
    （≤3 个，与订阅层 MAX_DOMAINS 同口径），结果合并去重后按域名后置过滤；
    不传时只查原 query，与加参数前逐位不变。搜索引擎对 site: 的支持是尽力
    而为：变体全空时兜底保留非限定结果并标注 scope_degraded=True（同时
    domain_scope_applied=False），调用方按实到结果呈现，不谎报"已限定"。
    反爬冷却（A）：每个源发请求前先查共享冷却表（source_cooldown）——冷却期内
    零外发、槽位标 blocked=True 并给中文 reason；命中验证页由各 provider 调
    mark_source_blocked 记冷却（时长 cooldown_seconds，默认 1800 秒），到期自动恢复。
    降级四态（D）：顶层 status ∈ {ok, partial, empty, all_blocked, no_sources,
    all_failed}，配合 degraded / all_blocked / sources_configured / degraded_reason，
    让调用方能区分"真的 0 条 / 全被反爬拦截 / 没有可用源 / 部分源成功"；
    engines 每个槽位固定带 {ok, error, count, blocked, reason}（另可按需带
    skipped/skip_reason），上层按这些字段如实呈现，不把"被拦截"显示成"没发现"。
    """
    if authorized is None:
        if providers is not None:
            # Explicit providers are caller-owned transports (tests/local adapters).
            authorized = True
        else:
            try:
                from mylocalai.core.privacy import load_preferences
                authorized = load_preferences().external_allowed("public-search")
            except Exception:
                authorized = False
    if not authorized:
        return {"query": query, "results": [], "engines": {}, "blocked": True,
                "error": "external_processing_not_authorized"}
    domain_list = [str(d).strip().lower() for d in (domains or []) if str(d).strip()][:3]
    queries = [query]
    for domain in domain_list:
        variant = f"site:{domain} {query}"
        if variant not in queries:
            queries.append(variant)
    # 查询级缓存（P1-5）：只挂默认装配路径。键含源集合签名——装配变了键就变；
    # default_providers() 只做本地装配（读配置 + 建实例，无 HTTP），命中后整个
    # provider 循环都跳过，零外发。授权闸在上面已经把未授权拦掉了，缓存逻辑
    # 在闸之后："未授权零外发"（连缓存都不查）语义不变。
    cache_key: Optional[Tuple] = None
    provs = providers if providers is not None else default_providers()
    if providers is None:
        cache_key = (str(query), tuple(domain_list), int(k),
                     tuple(_provider_sig(p) for p in provs))
        cached = _query_cache_get(cache_key)
        if cached is not None:
            return cached
    summary = _source_summary(provs)
    engines: Dict[str, Dict[str, object]] = {}
    merged: List[SearchResult] = []
    seen = set()
    skipped_total = 0
    for p in provs:
        key = getattr(p, "name", None) or type(p).__name__
        # API 型源的配置性跳过（密钥缺失等）：先于一切 HTTP 检查；命中即计一次
        # skipped 并进下一个源，不进入变体循环（零外发）。没有 skip_reason 的
        # 提供方（爬虫源/custom-api/测试桩）走原路径，逐位不变。
        checker = getattr(p, "skip_reason", None)
        if callable(checker):
            try:
                reason = str(checker() or "")
            except Exception:    # noqa: BLE001  判定器坏了按"不跳过"处理，交给 search() 兜
                reason = ""
            if reason:
                slot = engines.setdefault(key, _new_engine_slot())
                slot["skipped"] = int(slot.get("skipped", 0)) + 1
                slot["skip_reason"] = reason
                slot["reason"] = f"源未就绪：{reason}"
                skipped_total += 1
                continue
        # 健康度动态降权（P1-5）：连续失败 ≥ 阈值（failure_skip_threshold，默认 3）
        # 的源本次调用先跳过——记 skipped 原因、零外发，不算降级主因；跳过本身给
        # 账本降温 1（cool_health_skip），源不会永久出局，成功一次即清零回归。
        # 没有 health_skip_reason 的提供方（爬虫源/custom-api/测试桩）走原路径。
        health = getattr(p, "health_skip_reason", None)
        if callable(health):
            try:
                hreason = str(health() or "")
            except Exception:    # noqa: BLE001  判定器坏了按"不跳过"处理
                hreason = ""
            if hreason:
                slot = engines.setdefault(key, _new_engine_slot())
                slot["skipped"] = int(slot.get("skipped", 0)) + 1
                slot["skip_reason"] = hreason
                slot["reason"] = f"连续失败已暂时跳过：{hreason}"
                skipped_total += 1
                cooler = getattr(p, "cool_health_skip", None)
                if callable(cooler):
                    try:
                        cooler()
                    except Exception:  # noqa: BLE001  降温失败不拖垮检索
                        pass
                continue
        # 反爬冷却（A）：冷却期内零外发——如实记 blocked + 中文原因，不当成"0 条结果"。
        cooling = source_cooldown(key)
        if cooling.get("active"):
            slot = engines.setdefault(key, _new_engine_slot())
            slot["blocked"] = True
            slot["reason"] = (f"反爬冷却中（{cooling.get('reason')}，"
                              f"剩余 {int(cooling.get('remaining_s', 0) or 0)} 秒）")
            continue
        for variant in queries:
            try:
                results, err = p.search(variant, k)
            except Exception as exc:  # noqa: BLE001  解析器 bug 不应炸掉整个检索
                results, err = [], f"{type(exc).__name__}: {exc}"
            slot = engines.setdefault(key, _new_engine_slot())
            if err:
                slot["error"] = str(err)
                slot["reason"] = str(err)
                # provider 命中验证页时会写共享冷却表：据此把槽位标成 blocked（而不是
                # 普通错误），上层就能区分"被拦截"和"网络抖动"。
                state = source_cooldown(key)
                if state.get("active"):
                    slot["blocked"] = True
                    slot["reason"] = (f"反爬拦截（{state.get('reason')}，"
                                      f"剩余 {int(state.get('remaining_s', 0) or 0)} 秒）")
            else:
                slot["ok"] = True
            for r in results or []:
                dedup_key = _dedup_url(r.url)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                merged.append(r)
            slot["count"] = int(slot.get("count", 0)) + len(results or [])
    scoped = bool(domains)
    degraded = False
    if domains:
        kept = [r for r in merged if _in_domains(r.url, domains)]
        if kept:
            merged = kept
        else:
            # site: 变体全空（引擎不支持 site: 或该站暂无收录）：兜底保留非限定
            # 结果照常呈现，但如实标注"未限定 + 降级"（R9），不谎报已限定。
            degraded = True
            scoped = False
    # 截断口径：不传 domains 时与加参数前逐位一致（k 条）；传了 domains 时按
    # 变体数放宽（每变体至多 k 条），总量有界（全空兜底路径口径不变）。
    limit = k if not domains else k * max(1, len(queries))
    out = _finalize(query, merged[:max(1, int(limit))], engines,
                    domain_scoped=scoped, scope_degraded=degraded,
                    skipped_total=skipped_total, source_summary=summary)
    if not summary["sources_configured"] and out["status"] in ("all_blocked", "no_sources",
                                                              "all_failed"):
        # B：结构化源缺位时把"没有备选通道"写进日志（不静默）——这是 2026-11-22
        # 实测里"联网核查看起来什么都没查到"的根因之一。
        _logger.warning("检索降级：%s（未配置 API/RSS 结构化源，遇反爬无备用通道）",
                        out.get("degraded_reason") or out.get("status"))
    # 只缓存"完全成功"：结果非空、零错误、零跳过、零拦截——部分失败/空结果/被跳过
    # 的源都意味着这次装配没跑满，缓存会冻结残缺视图（失败/空结果不缓存）。
    if cache_key is not None and out["results"] and skipped_total == 0 \
            and not any(slot.get("error") or slot.get("blocked")
                        for slot in engines.values()):
        _query_cache_put(cache_key, out)
    return out


def _new_engine_slot() -> Dict[str, object]:
    """engines 槽位的固定形状：ok / error / count / blocked / reason（+ 可选 skipped）。

    blocked=True 表示该源这次是被反爬拦截/冷却跳过（reason 给中文原因），与
    ok=False+error 的"网络/解析失败"区分开——上层据此把"被拦截"显示成"被拦截"。
    """
    return {"ok": False, "error": "", "count": 0, "blocked": False, "reason": ""}


def _source_summary(provs: List[object]) -> Dict[str, object]:
    """本次检索的源结构摘要（B/D）：配置层有没有结构化备选 + 本次装配了哪些源。

    sources_configured 取**配置层**口径（search_sources.json 是否声明了启用的
    API/RSS 源），与密钥是否就绪无关：声明了但缺密钥 = 有配置、无可用源，
    由 skipped 计数如实呈现。api_sources/rss_sources 同样取配置层名字，
    装配实况看 engines 的键。
    """
    try:
        summary = source_config_summary()
    except Exception:            # noqa: BLE001  摘要失败不能拖垮检索
        summary = {"sources_configured": False, "api_sources": [], "rss_sources": [],
                   "sources_file_exists": False, "error": ""}
    summary["web_sources"] = [str(getattr(p, "name", "") or type(p).__name__)
                              for p in provs
                              if isinstance(p, (So360Provider, BaiduProvider,
                                                GenericAPIProvider))]
    return summary


def _in_domains(url: str, domains: List[str]) -> bool:
    """URL 的主机是否落在给定域名集合里（用于 site: 变体的事后过滤）。"""
    host = normalize_host(str(url or ""))
    if not host:
        return False
    wanted = {str(d).strip().lower() for d in domains if str(d).strip()}
    return any(host == d or host.endswith("." + d) for d in wanted)


def _finalize(query: str, merged: List[SearchResult],
              engines: Dict[str, Dict[str, object]],
              domain_scoped: bool = False,
              scope_degraded: bool = False,
              skipped_total: int = 0,
              source_summary: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """排序 + 组装返回（原 search_all 尾段；domain_scoped 时如实标注）。

    scope_degraded（R9）：site: 变体全空兜底的诚实标注——domain_scope_applied
    置 False 并带 scope_degraded=True；不传 domains 时两个键都不出现（逐位不变）。
    skipped_total（P0-1 API 型源）：密钥缺失等原因被跳过的源计数；只在 >0 时
    才加顶层 "skipped" 键。skipped 不是 error：该次调用确实没外发、也没失败，
    是合法的"源不可用"状态。
    降级四态（D）：status + degraded + all_blocked + sources_configured +
    degraded_reason 恒定出现（显式 providers 与默认装配同一形状，调用方不必
    分支判断）。status 口径：
      - ok         ：有结果，且所有源都成功（无 error/skip/blocked）；
      - partial    ：有结果，但至少一个源失败/被跳过/被拦截；
      - empty      ：确实查到了 0 条（至少一个源成功返回空结果）；
      - all_blocked：0 条且所有源都被反爬拦截（"看起来什么都没查到"的真凶）；
      - no_sources ：没有可用源（未装配任何源，或只有网页解析源且全部失败、
                     结构化源一个都没配置）；
      - all_failed ：0 条且所有源都失败（网络/解析错误，不是拦截）。
    截断由调用方做（见 search_all 的 limit 口径），这里只排序不裁。
    """
    # 排序不写死档名：按 registry 里 tiers 的声明顺序排（越靠前越权威），未声明的档排最后。
    merged.sort(key=lambda r: (tier_rank(r.domain_tier), r.rank))
    summary = dict(source_summary or {})
    status, degraded, reason = _degrade_status(merged, engines, summary)
    out: Dict[str, object] = {
        "query": query, "results": merged, "engines": engines,
        "status": status,
        "degraded": degraded,
        "all_blocked": status == "all_blocked",
        "sources_configured": bool(summary.get("sources_configured")),
        "api_sources": list(summary.get("api_sources") or []),
        "rss_sources": list(summary.get("rss_sources") or []),
        "web_sources": list(summary.get("web_sources") or []),
        "degraded_reason": reason,
    }
    if scope_degraded:
        out["domain_scope_applied"] = False
        out["scope_degraded"] = True
    elif domain_scoped:
        out["domain_scope_applied"] = True
    if skipped_total > 0:
        out["skipped"] = int(skipped_total)
    return out


def _degrade_status(merged: List[SearchResult],
                    engines: Dict[str, Dict[str, object]],
                    summary: Dict[str, object]) -> Tuple[str, bool, str]:
    """降级四态判定（D）：返回 (status, degraded, 中文原因)。

    只看本次装配的槽位与配置摘要，不猜：有结果时区分 ok/partial；0 结果时按
    "全被拦截 → 无可用源 → 全失败 → 真空结果"的顺序判（拦截与无源是本次任务
    要显式暴露的两种，优先于笼统的 all_failed）。
    """
    names = [str(name) for name in engines]
    slots = [slot for slot in engines.values() if isinstance(slot, dict)]
    blocked = [name for name, slot in engines.items()
               if isinstance(slot, dict) and slot.get("blocked")]
    # failed 只算"非拦截的失败"：被拦截的槽位也会带 error 串，但它属于 blocked，
    # 不能让它把 all_blocked 误判成 all_failed（否则"被拦截"又退回"源失败"口径）。
    failed = [name for name, slot in engines.items()
              if isinstance(slot, dict) and slot.get("error") and not slot.get("blocked")]
    skipped = [name for name, slot in engines.items()
               if isinstance(slot, dict) and slot.get("skipped")]
    ok = [name for name, slot in engines.items()
          if isinstance(slot, dict) and slot.get("ok")]
    if merged:
        if not failed and not skipped and not blocked:
            return "ok", False, ""
        trouble = failed + blocked + skipped
        return "partial", True, f"部分检索源不可用：{'、'.join(trouble)}"
    if not slots:
        return "no_sources", True, "没有可用检索源（本次未装配任何后端）"
    if blocked and not failed and len(blocked) + len(skipped) == len(slots):
        # 所有"尝试过的"源都被拦、其余源未就绪（缺密钥等）：本质仍是"全被拦截"。
        return "all_blocked", True, (
            f"全部检索源被反爬拦截（{'、'.join(blocked)}），"
            "没有拿到任何结果——这不等于'没有相关发现'")
    if ok:
        return "empty", False, ""
    if skipped and len(skipped) == len(slots):
        return "no_sources", True, (
            "结构化源都未就绪（未配置或密钥缺失），且没有可用网页解析源")
    if not summary.get("sources_configured"):
        return "no_sources", True, (
            "未配置 API/RSS 结构化源，网页解析源全部失败——遇反爬没有备选通道")
    return "all_failed", True, f"所有检索源均失败：{'、'.join(failed or names)}"


# ===========================================================================
# 原文抓取（机会式）
# ===========================================================================

def _host_is_public(host: str) -> bool:
    """SSRF 防护：解析主机名，所有 IP 都必须是公网地址。解析失败视为不安全。"""
    try:
        addr = ipaddress.ip_address(host)     # host 本身就是 IP
        return not (addr.is_private or addr.is_loopback or addr.is_link_local
                    or addr.is_reserved or addr.is_multicast or addr.is_unspecified)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, UnicodeError):
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return bool(infos)


def _decode_html(resp: requests.Response) -> str:
    """按 声明 → utf-8 → gbk 的顺序解码中文页面。"""
    raw = resp.content
    m = re.search(rb"charset=[\"']?([\w-]+)", raw[:2048])
    for enc in ([m[1].decode("ascii", "ignore")] if m else []) + ["utf-8", "gbk", "gb18030"]:
        try:
            return raw[:_FETCH_MAX_BYTES].decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw[:_FETCH_MAX_BYTES].decode("utf-8", "replace")


def _extract_text(page: str) -> str:
    """极简正文提取：去 script/style/标签，压空白。提取不到长文本返回空串。"""
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    title = _strip_tags(tm[1]) if tm else ""
    body = re.sub(r"(?is)</(p|div|li|h[1-6]|tr)>", "\n", page)
    text = _strip_tags(body)
    lines = [ln.strip() for ln in text.split("\n") if len(ln.strip()) >= 8]
    return (title + "\n" + " ".join(lines)).strip()


def fetch_article(url: str, return_html: bool = False) -> Optional[Dict[str, object]]:
    """抓取一篇文章的正文（机会式：失败返回 None，证据仍以摘要为主）。

    只抓分级为官方/媒体/门户的域名；SSRF 防护 + 大小/超时限制。
    return_html=True 时附带截断的原始 HTML，供浏览会话抽取页面内链接。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if classify_domain(url) not in _FETCH_TIERS:
        return None
    if not _host_is_public(parsed.hostname):
        return None
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_FETCH_TIMEOUT, stream=True,
                            allow_redirects=True)
    except requests.RequestException:
        return None
    try:
        if resp.status_code != 200 or len(resp.content or b"") > _FETCH_MAX_BYTES:
            return None
        page = _decode_html(resp)
    finally:
        resp.close()
    text = _extract_text(page)
    if len(text) < _FETCH_MIN_TEXT:
        return None
    out = {"url": url, "text": text[:_FETCH_MAX_TEXT],
           "final_url": resp.url if hasattr(resp, "url") else url}
    if return_html:
        out["html"] = page[:_FETCH_MAX_BYTES]
    return out
