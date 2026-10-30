# -*- coding: utf-8 -*-
"""检索层：向搜索引擎查询与文本相关的事件，抓取原文做证据。

设计要点（来自对本机网络的实测）：
  - 360 搜索（so.com）是唯一稳定可用的搜索后端 → 主力；百度首页搜索可用但
    连续请求会触发验证码（302 跳 wappass.baidu.com）→ 备用，触发后降级 30 分钟。
  - 证据以搜索结果摘要为主：很多原文页（公众号、百家号、澎湃）有反爬，全文抓取
    是机会式的，抓不到不影响判定。
  - fetch_article 做了 SSRF 防护：只允许 http/https、解析主机后拒绝私网/环回/
    链路本地地址，限制响应大小与超时——搜索结果里的 URL 是外部输入，不能盲信。

所有函数都不抛异常：失败返回 (空结果, 错误说明) 或 None，由上层记入降级组件。
"""

from __future__ import annotations

import html as _html
import ipaddress
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


def default_providers() -> List[object]:
    """按环境变量组装后端列表：
      RUMOR_SEARCH_BACKENDS  逗号分隔：custom-api,so360,baidu（默认 so360,baidu）
    配了 RUMOR_SEARCH_API_URL 时 custom-api 才会生效。"""
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
    return out or [So360Provider(), BaiduProvider()]


def search_all(query: str, k: int = 6,
               providers: Optional[List[object]] = None) -> Dict[str, object]:
    """多后端搜索 + 按 URL 去重合并。返回 {query, results, engines}，绝不抛异常。

    providers 缺省时用 default_providers()（环境变量可插拔专用 API）。
    """
    engines: Dict[str, Dict[str, object]] = {}
    merged: List[SearchResult] = []
    seen = set()
    for p in (providers if providers is not None else default_providers()):
        try:
            results, err = p.search(query, k)
        except Exception as exc:  # noqa: BLE001  解析器 bug 不应炸掉整个检索
            results, err = [], f"{type(exc).__name__}: {exc}"
        engines[p.name] = {"ok": not err, "count": len(results), "error": err}
        for r in results:
            key = _dedup_url(r.url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
    # 排序不写死档名：按 registry 里 tiers 的声明顺序排（越靠前越权威），未声明的档排最后。
    merged.sort(key=lambda r: (tier_rank(r.domain_tier), r.rank))
    return {"query": query, "results": merged[:k], "engines": engines}


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
