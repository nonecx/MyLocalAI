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

所有函数都不抛异常：失败返回 (空结果, 错误说明) 或 None，由上层记入降级组件。
"""

from __future__ import annotations

import copy
import html as _html
import ipaddress
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
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
_DEGRADE_SECONDS = 1800.0     # 百度触发验证码后 30 分钟内不再尝试
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
#: 查询级缓存：键 = (query, domains, k, 源集合签名)，值 = (过期时刻, 返回值深拷贝)。
#: 只存"完全成功"的查询结果（判定见 search_all 的 cacheable），命中零外发。
_QUERY_CACHE: Dict[Tuple, Tuple[float, Dict[str, object]]] = {}
_QUERY_CACHE_TTL_DEFAULT = 300.0     # 缓存 TTL 默认 300 秒（search_sources.json 可配）
_QUERY_CACHE_MAX = 128               # 缓存条数上限（满时先清过期、再按插入序逐出最旧）
_FAILURE_SKIP_THRESHOLD_DEFAULT = 3  # 连续失败跳过阈值默认值（search_sources.json 可配）
#: search_sources.json 顶层设置（failure_skip_threshold / query_cache_ttl_s）：
#: 每次 load_api_source_configs() 都刷新（改 JSON 即生效）；文件缺失/损坏回默认。
_SOURCE_SETTINGS: Dict[str, object] = {}


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
    """360 搜索（so.com）。主力后端：实测稳定返回，约 1 秒，无验证码。"""
    name = "360"
    # 视频/图片等垂直类目的卡片，不是网页结果，跳过
    _SKIP_HOSTS = ("tv.360kan.com", "image.so.com", "video.so.com", "www.360kan.com",
                   "leshou.com", "so.com")

    def search(self, query: str, k: int = 6) -> Tuple[List[SearchResult], Optional[str]]:
        url = "https://www.so.com/s?q=" + quote(query) + "&pn=1"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_SEARCH_TIMEOUT)
        except requests.RequestException as exc:
            return [], f"360请求失败: {type(exc).__name__}"
        if resp.status_code != 200:
            return [], f"360返回HTTP {resp.status_code}"
        return self.parse(resp.text)[:k], None

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

    触发验证码后类级降级 30 分钟（_degraded_until），期间直接返回错误，不再发请求。
    """
    name = "baidu"
    _degraded_until = 0.0

    @classmethod
    def is_degraded(cls) -> bool:
        return time.time() < cls._degraded_until

    def search(self, query: str, k: int = 6) -> Tuple[List[SearchResult], Optional[str]]:
        if self.is_degraded():
            return [], "百度后端已降级（近期触发验证码）"
        url = "https://www.baidu.com/s?wd=" + quote(query) + "&rn=20"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_SEARCH_TIMEOUT,
                                allow_redirects=False)
        except requests.RequestException as exc:
            return [], f"百度请求失败: {type(exc).__name__}"
        loc = resp.headers.get("Location", "")
        if resp.status_code in (301, 302) and "wappass" in loc:
            type(self)._degraded_until = time.time() + _DEGRADE_SECONDS
            return [], "百度触发验证码，后端降级30分钟"
        if resp.status_code != 200:
            return [], f"百度返回HTTP {resp.status_code}"
        if "wappass.baidu.com" in resp.text[:4000]:
            type(self)._degraded_until = time.time() + _DEGRADE_SECONDS
            return [], "百度返回验证码页，后端降级30分钟"
        return self.parse(resp.text)[:k], None

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


def load_api_source_configs(path: Optional[str] = None) -> List[Dict[str, object]]:
    """读 API 型搜索源清单，返回归一后的配置 dict 列表。

    fail-soft 纪律（对齐 config/rules.py 的 _load()）：
      - 文件不存在 → 合法配置（未启用 API 源），静默返回 []；
      - JSON 损坏 / 根不是对象 / schema_version 不符 / sources 段不是数组
        → 返回 [] 并经 add_warning 留告警（load_warnings() 可见）；
      - 单个源声明缺 id/protocol 未知/base_url 非法 → 丢弃该条并合并告警；
      - 绝不抛异常：检索主链路不因一个配置文件的笔误而崩。
    每次调用都重新读盘（文件很小，检索本身是网络 IO 量级）：改 JSON 即生效，
    不需要重启服务。
    顶层设置（P1-5 多源融合）：failure_skip_threshold（连续失败跳过阈值，默认 3）
    与 query_cache_ttl_s（查询缓存 TTL 秒数，默认 300，≤0 = 显式关闭缓存）在每次
    加载时刷新进 _SOURCE_SETTINGS，并随每条源配置附带 threshold；值非法时告警 +
    回默认，文件缺失/损坏时设置整体回默认。
    """
    _clear_source_warnings()
    _SOURCE_SETTINGS.clear()     # 本次加载结果即设置全集（缺失/损坏 → 回默认值）
    file_path = path or _SEARCH_SOURCES_PATH
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []                # 文件被移除 = 明确的不启用，不是故障
    except Exception as exc:     # noqa: BLE001  JSON 损坏、权限问题等
        _warn_sources(f"search_sources.json 读取失败（{file_path}）："
                      f"{type(exc).__name__}: {exc}，已退回无 API 源")
        return []
    if not isinstance(data, dict):
        _warn_sources("search_sources.json 根节点必须是对象，已退回无 API 源")
        return []
    version = data.get("schema_version")
    if version != _API_SOURCES_SCHEMA_VERSION:
        _warn_sources(f"search_sources.json schema_version 不符（期望 "
                      f"{_API_SOURCES_SCHEMA_VERSION}，实际 {version!r}），已退回无 API 源")
        return []
    # 顶层设置解析（P1-5）：fail-soft，非法值告警 + 回默认；TTL ≤0 是显式关闭开关。
    raw_threshold = data.get("failure_skip_threshold")
    if raw_threshold is None:
        _SOURCE_SETTINGS["failure_skip_threshold"] = _FAILURE_SKIP_THRESHOLD_DEFAULT
    elif (isinstance(raw_threshold, bool) or not isinstance(raw_threshold, (int, float))
          or raw_threshold <= 0):
        _warn_sources(f"failure_skip_threshold 不合法（{raw_threshold!r}），"
                      f"回默认 {_FAILURE_SKIP_THRESHOLD_DEFAULT}")
        _SOURCE_SETTINGS["failure_skip_threshold"] = _FAILURE_SKIP_THRESHOLD_DEFAULT
    else:
        _SOURCE_SETTINGS["failure_skip_threshold"] = raw_threshold
    raw_ttl = data.get("query_cache_ttl_s")
    if raw_ttl is None:
        _SOURCE_SETTINGS["query_cache_ttl_s"] = _QUERY_CACHE_TTL_DEFAULT
    elif isinstance(raw_ttl, bool) or not isinstance(raw_ttl, (int, float)):
        _warn_sources(f"query_cache_ttl_s 不合法（{raw_ttl!r}），"
                      f"回默认 {_QUERY_CACHE_TTL_DEFAULT:g}")
        _SOURCE_SETTINGS["query_cache_ttl_s"] = _QUERY_CACHE_TTL_DEFAULT
    else:
        _SOURCE_SETTINGS["query_cache_ttl_s"] = float(raw_ttl)
    rows = data.get("sources")
    if rows is None:
        return []                # 没写 sources 段 = 无 API 源（合法）
    if not isinstance(rows, list):
        _warn_sources("search_sources.json#sources 必须是数组，已退回无 API 源")
        return []
    out: List[Dict[str, object]] = []
    dropped: List[str] = []
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
        if not base_url.startswith(("http://", "https://")):
            dropped.append(f"{sid}（base_url 非 http/https）")
            continue
        if not key_env:
            dropped.append(f"{sid}（缺 key_env）")
            continue
        weight = item.get("weight", 1.0)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            weight = 1.0
        timeout = item.get("timeout_s", _SEARCH_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            timeout = _SEARCH_TIMEOUT
        enabled = item.get("enabled", True)
        out.append({"id": sid, "protocol": protocol, "base_url": base_url,
                    "key_env": key_env, "enabled": bool(enabled),
                    "weight": float(weight), "timeout_s": float(timeout),
                    "failure_skip_threshold":
                        _SOURCE_SETTINGS["failure_skip_threshold"]})
    if dropped:
        _warn_sources(f"search_sources.json#sources 里这些条不合规，已丢弃：{'；'.join(dropped)}")
    return [cfg for cfg in out if cfg["enabled"]]


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
      进程级 _API_SOURCE_HEALTH 账本同写）；连续失败 ≥ failure_skip_threshold
      （search_sources.json 顶层可配，默认 3）时 health_skip_reason() 给出原因，
      search_all 据此做健康度动态降权（P1-5）：本次调用先跳过该源（零外发、记
      skipped），跳过一次账本降温 1（cool_health_skip），源不会永久出局；成功
      一次即清零回归（"连续失败源暂时跳过并记数，恢复后自动回归"）。
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
        fails = _API_SOURCE_HEALTH.get(self.id, self.consecutive_failures)
        if fails >= self.failure_skip_threshold:
            return (f"unhealthy:consecutive_failures={int(fails)}"
                    f">={self.failure_skip_threshold}")
        return ""

    def cool_health_skip(self) -> None:
        """健康跳过一次给账本降温 1：被跳过的源下一轮即低于阈值、自动回归探针。

        探针成功清零回归；探针失败账本重新累加、回到跳过态（最坏情况跳过/探针
        交替，一半调用省掉该源的超时等待）——保证"暂时跳过"，不会永久出局。
        """
        cooled = max(0, _API_SOURCE_HEALTH.get(self.id, self.consecutive_failures) - 1)
        _API_SOURCE_HEALTH[self.id] = cooled
        self.consecutive_failures = cooled

    # ---- 请求与 normalize ----

    def search(self, query: str, k: int = 6) -> Tuple[List[SearchResult], Optional[str]]:
        results, err = self._request(query, max(1, int(k)))
        if err:
            self.consecutive_failures += 1
            _API_SOURCE_HEALTH[self.id] = self.consecutive_failures  # 跨调用健康账本
            _logger.warning("api-source %s 连续失败 %d 次: %s",
                            self.id, self.consecutive_failures, err)
            return [], err
        self.consecutive_failures = 0
        _API_SOURCE_HEALTH[self.id] = 0
        return results, None

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


def default_providers() -> List[object]:
    """按环境变量组装后端列表：
      RUMOR_SEARCH_BACKENDS  逗号分隔：custom-api,so360,baidu（默认 so360,baidu）
    配了 RUMOR_SEARCH_API_URL 时 custom-api 才会生效。
    另外前置 search_sources.json 里**已启用且已配密钥**的 API 型源（启用 =
    enabled:true + key_env 环境变量有非空密钥，两者共同决定——正是"改 JSON/
    填 .env 即启用，不改代码"的口径）。未配密钥的源不进默认装配，因此一个 API
    源都没启用时本函数返回值与从前逐位一致（既有装配契约不变）；skip 机制
    （skip_reason → skipped 计数）保留给显式装配的 provider 与"装配后才失钥"
    的运行时窗口，见 search_all。"""
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
    api = _build_api_providers()
    return api + out if api else (out or [So360Provider(), BaiduProvider()])


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
    engines: Dict[str, Dict[str, object]] = {}
    merged: List[SearchResult] = []
    seen = set()
    skipped_total = 0
    for p in provs:
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
                key = getattr(p, "name", None) or type(p).__name__
                slot = engines.setdefault(key, {"ok": False, "error": "", "count": 0})
                slot["skipped"] = int(slot.get("skipped", 0)) + 1
                slot["skip_reason"] = reason
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
                key = getattr(p, "name", None) or type(p).__name__
                slot = engines.setdefault(key, {"ok": False, "error": "", "count": 0})
                slot["skipped"] = int(slot.get("skipped", 0)) + 1
                slot["skip_reason"] = hreason
                skipped_total += 1
                cooler = getattr(p, "cool_health_skip", None)
                if callable(cooler):
                    try:
                        cooler()
                    except Exception:  # noqa: BLE001  降温失败不拖垮检索
                        pass
                continue
        for variant in queries:
            try:
                results, err = p.search(variant, k)
            except Exception as exc:  # noqa: BLE001  解析器 bug 不应炸掉整个检索
                results, err = [], f"{type(exc).__name__}: {exc}"
            key = getattr(p, "name", None) or type(p).__name__
            slot = engines.setdefault(key, {"ok": False, "error": "", "count": 0})
            if err:
                slot["error"] = str(err)
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
                    skipped_total=skipped_total)
    # 只缓存"完全成功"：结果非空、零错误、零跳过——部分失败/空结果/被跳过的源
    # 都意味着这次装配没跑满，缓存会冻结残缺视图（失败/空结果不缓存）。
    if cache_key is not None and out["results"] and skipped_total == 0 \
            and not any(slot.get("error") for slot in engines.values()):
        _query_cache_put(cache_key, out)
    return out


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
              skipped_total: int = 0) -> Dict[str, object]:
    """排序 + 组装返回（原 search_all 尾段；domain_scoped 时如实标注）。

    scope_degraded（R9）：site: 变体全空兜底的诚实标注——domain_scope_applied
    置 False 并带 scope_degraded=True；不传 domains 时两个键都不出现（逐位不变）。
    skipped_total（P0-1 API 型源）：密钥缺失等原因被跳过的源计数；只在 >0 时
    才加顶层 "skipped" 键（未配置 API 源时返回结构逐位不变）。skipped 不是
    error：该次调用确实没外发、也没失败，是合法的"源不可用"状态。
    截断由调用方做（见 search_all 的 limit 口径），这里只排序不裁。
    """
    # 排序不写死档名：按 registry 里 tiers 的声明顺序排（越靠前越权威），未声明的档排最后。
    merged.sort(key=lambda r: (tier_rank(r.domain_tier), r.rank))
    out: Dict[str, object] = {"query": query, "results": merged, "engines": engines}
    if scope_degraded:
        out["domain_scope_applied"] = False
        out["scope_degraded"] = True
    elif domain_scoped:
        out["domain_scope_applied"] = True
    if skipped_total > 0:
        out["skipped"] = int(skipped_total)
    return out


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
