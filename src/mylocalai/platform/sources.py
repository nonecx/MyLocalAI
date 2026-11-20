# -*- coding: utf-8 -*-
"""信息发现源（platform/sources.py）：监控词 → 候选列表。

两个内置 provider（计划决策 2：监控词 + 搜索引擎 + RSS 都要）：
  - `SearchDiscoveryProvider`：包 retrieval.search.search_all（360/百度聚合），
    授权门与检索层同口径（external_allowed("public-search")）；
  - `RssDiscoveryProvider`：平台规则 rss_sources 里配置的 RSS/Atom 源，
    抓取复用 retrieval.search 的 SSRF 防护（_host_is_public，不重复实现安全层），
    按关键词过滤条目。热榜（微博热搜等无公开 API 的源）经 RSSHub 之类中转后
    同样走本 provider，无需新代码——扩展点即"再加一条 rss_sources 配置"。

统一契约（runner 只面对这个形状）：
    discover(query, k) -> ProviderResult{candidates, degraded, error}
  - **绝不抛异常**：单源失败/超时/解析坏 → 该源 degraded + error，不影响其他源；
  - 未授权不是错误而是合法状态：degraded=True + error=external_processing_not_authorized，
    由上层决定跳过巡检并记录原因（隐私 fail-closed，不静默）。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from mylocalai.platform.candidates import FeedCandidate
from mylocalai.retrieval.sources import classify_domain, normalize_host

__all__ = ["ProviderResult", "DiscoveryProvider", "SearchDiscoveryProvider",
           "RssDiscoveryProvider", "build_providers"]

#: 搜索提供方名（隐私授权表里的 provider id，与 retrieval/search.py 的默认口径一致）。
SEARCH_PROVIDER_ID = "public-search"

#: RSS 抓取上限（与检索层 _FETCH_* 同量级；RSS 文档小，刻意收紧）。
_RSS_MAX_BYTES = 1_000_000
_RSS_TIMEOUT = 10
_RSS_MAX_ITEMS = 30           # 单源最多取的条目数（过滤前）
_RSS_MIN_TEXT = 20            # 标题+摘要短于此的条目丢弃（导航/空条目）


@dataclass
class ProviderResult:
    """一次发现的结果：候选列表 + 降级标记 + 原因。"""

    candidates: List[FeedCandidate] = field(default_factory=list)
    degraded: bool = False
    error: str = ""


class DiscoveryProvider:
    """发现源协议：name + discover。实现必须是纯 IO 封装（不碰库、不碰 LLM）。"""

    name = "abstract"

    def discover(self, query: str, k: int) -> ProviderResult:  # pragma: no cover - 协议
        raise NotImplementedError


def _split_terms(query: str) -> List[str]:
    """监控词 → 检索/过滤词：按空白与逗号切分，保留长度≥2 的词（单词噪音大）。"""
    parts = re.split(r"[\s,，、;；]+", str(query or "").strip())
    return [p for p in parts if len(p) >= 2] or ([str(query or "").strip()]
                                              if str(query or "").strip() else [])


class SearchDiscoveryProvider(DiscoveryProvider):
    """搜索引擎发现源：包 search_all，授权门逐次检查（设置改动即时生效）。"""

    name = "search"

    def __init__(self, provider_id: str = SEARCH_PROVIDER_ID) -> None:
        self._provider_id = provider_id

    def _authorized(self) -> Tuple[bool, str]:
        try:
            from mylocalai.core.privacy import load_preferences
            prefs = load_preferences()
            return prefs.external_allowed(self._provider_id), ""
        except Exception as exc:  # noqa: BLE001  偏好读取失败按未授权（fail-closed）
            return False, f"privacy_check_failed: {type(exc).__name__}"

    def discover(self, query: str, k: int) -> ProviderResult:
        allowed, why = self._authorized()
        if not allowed:
            return ProviderResult(degraded=True,
                                  error="external_processing_not_authorized"
                                  + (f"（{why}）" if why else ""))
        try:
            from mylocalai.retrieval.search import search_all
            out = search_all(query, k=max(1, int(k)))
        except Exception as exc:  # noqa: BLE001  检索层自身承诺不抛，双保险
            return ProviderResult(degraded=True, error=f"search_failed: {type(exc).__name__}")
        if out.get("blocked"):
            return ProviderResult(degraded=True,
                                  error=str(out.get("error") or "external_processing_not_authorized"))
        engines = out.get("engines") or {}
        results = out.get("results") or []
        # 有结果但全部后端报错 ≠ 成功；部分后端失败只记 degraded（候选照常用）。
        degraded = bool(results) is False and bool(engines)
        if engines and all(not e.get("ok") for e in engines.values() if isinstance(e, dict)):
            degraded = True
        from mylocalai.platform.candidates import candidate_from_search_result
        candidates = [c for c in (candidate_from_search_result(r, source=self.name)
                                  for r in results) if c is not None]
        return ProviderResult(candidates=candidates, degraded=degraded)


class RssDiscoveryProvider(DiscoveryProvider):
    """RSS/Atom 发现源：配置的 feed 列表 + 关键词过滤。

    抓取安全：只允许 http/https、解析主机后拒绝私网/环回/链路本地（复用
    retrieval.search._host_is_public）、大小与超时上限。XML 解析前剥掉
    DOCTYPE/ENTITY（stdlib ET 不解析外部实体，剥掉是纵深防御）。
    """

    name = "rss"

    def __init__(self, feeds: Optional[List[Dict[str, Any]]] = None) -> None:
        self._feeds = [f for f in (feeds or []) if f.get("enabled", True)
                       and str(f.get("url") or "").startswith(("http://", "https://"))]

    def discover(self, query: str, k: int) -> ProviderResult:
        if not self._feeds:
            return ProviderResult(degraded=True, error="no_rss_sources_configured")
        terms = _split_terms(query)
        if not terms:
            return ProviderResult(degraded=True, error="empty_query")
        candidates: List[FeedCandidate] = []
        errors: List[str] = []
        for feed in self._feeds:
            if len(candidates) >= max(1, int(k)):
                break
            items, err = self._fetch_feed(str(feed.get("url") or ""))
            if err:
                errors.append(f"{feed.get('name') or feed.get('url')}: {err}")
                continue
            for item in items:
                if len(candidates) >= max(1, int(k)):
                    break
                text = f"{item.title} {item.snippet}".lower()
                if not any(t.lower() in text for t in terms):
                    continue
                candidates.append(item)
        if errors and not candidates:
            return ProviderResult(degraded=True, error="；".join(errors)[:300])
        return ProviderResult(candidates=candidates, degraded=bool(errors),
                              error="；".join(errors)[:300] if errors else "")

    # ---- 内部：抓取与解析 ----

    def _fetch_feed(self, url: str) -> Tuple[List[FeedCandidate], str]:
        """抓一个 feed → (候选, 错误串)。任何失败都返回 ([], reason)，不抛。"""
        try:
            from urllib.parse import urlparse
            import requests
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return [], "url_invalid"
            # 复用检索层同一 SSRF 防护（跨包借私有函数有注释约束：不重复实现安全层）
            from mylocalai.retrieval.search import _host_is_public
            if not _host_is_public(parsed.hostname):
                return [], "host_not_public"
            resp = requests.get(url, timeout=_RSS_TIMEOUT, stream=True,
                                headers={"User-Agent": "Mozilla/5.0 (compatible; MyLocalAI-Platform/1.0)",
                                         "Accept": "application/rss+xml,application/xml,text/xml,*/*"})
            try:
                if resp.status_code != 200:
                    return [], f"http_{resp.status_code}"
                raw = resp.content or b""
            finally:
                resp.close()
            if len(raw) > _RSS_MAX_BYTES:
                return [], "too_large"
            text = _decode_xml(raw)
            return self._parse_feed(text, url)
        except Exception as exc:  # noqa: BLE001  单源失败不拖垮整个发现
            return [], f"{type(exc).__name__}"

    def _parse_feed(self, xml_text: str, feed_url: str) -> Tuple[List[FeedCandidate], str]:
        """解析 RSS 2.0 / Atom → (候选, 错误串)。

        坏 XML → ([], "xml_parse_error")：**解析失败必须与"空 feed"区分**——
        前者是源坏了（degraded），后者是合法空结果。
        安全：解析前剥掉 DOCTYPE/ENTITY 声明（stdlib ET 本就不解析外部实体，
        剥掉是纵深防御；未定义的实体引用会让解析失败——fail-closed，不泄内容）。
        """
        if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
            xml_text = re.sub(r"<!DOCTYPE[^>]*>", "", xml_text, flags=re.I)
            xml_text = re.sub(r"<!ENTITY[^>]*>", "", xml_text, flags=re.I)
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return [], "xml_parse_error"
        tag = _localname(root.tag)
        items: List[ET.Element] = []
        if tag == "rss":
            channel = root.find("channel")
            items = channel.findall("item") if channel is not None else []
        elif tag in ("feed", "rdf:RDF", "RDF"):
            items = (root.findall("{http://www.w3.org/2005/Atom}entry")
                     or root.findall("entry") or root.findall("item"))
        feed_name = _text_of(root.find("channel/title")) or _localname(root.tag)
        out: List[FeedCandidate] = []
        for item in items[:_RSS_MAX_ITEMS]:
            cand = self._item_to_candidate(item, feed_url, feed_name)
            if cand is not None:
                out.append(cand)
        return out, ""

    def _item_to_candidate(self, item: ET.Element, feed_url: str,
                           feed_name: str) -> Optional[FeedCandidate]:
        title = _text_of(_first(item, ("title",)))
        link = _link_of(item)
        summary = _text_of(_first(item, ("description", "summary", "content"))) \
            or _text_of(_first(item, ("abstract",)))
        if not title or not link or len(f"{title}{summary}") < _RSS_MIN_TEXT:
            return None
        published = _text_of(_first(item, ("pubDate", "published", "updated", "date"))) or ""
        return FeedCandidate(
            title=title[:200], url=link, snippet=re.sub(r"<[^>]+>", "", summary)[:2000],
            domain=normalize_host(link), domain_tier=classify_domain(link),
            published_at=published[:40], source=f"rss:{feed_name}"[:60])


def build_providers(rules: Dict[str, Any]) -> Tuple[List[DiscoveryProvider], List[str]]:
    """按平台规则装配发现源：搜索引擎常驻；RSS 源按 rss_sources 配置装配。

    返回 (providers, warnings)。规则段缺失/写坏 → 只装配搜索源并留告警
    （坏配置不挡住已有能力，与全项目"段级降级"一致）。
    """
    warnings: List[str] = []
    providers: List[DiscoveryProvider] = [SearchDiscoveryProvider()]
    raw = (rules or {}).get("rss_sources")
    if raw is None:
        return providers, warnings
    if not isinstance(raw, list):
        return providers, ["rss_sources 段不是数组，已忽略（仅使用搜索引擎源）"]
    feeds = [f for f in raw if isinstance(f, dict) and f.get("enabled", True)]
    if feeds:
        providers.append(RssDiscoveryProvider(feeds))
    return providers, warnings


# ---- XML 小工具（命名空间无关） ----


def _localname(tag: Any) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


def _text_of(el: Optional[ET.Element]) -> str:
    if el is None or el.text is None:
        return ""
    return re.sub(r"\s+", " ", el.text).strip()


def _first(item: ET.Element, names: Tuple[str, ...]) -> Optional[ET.Element]:
    for child in item:
        if _localname(child.tag) in names:
            return child
    return None


def _link_of(item: ET.Element) -> str:
    """RSS 的 <link>text</link> 与 Atom 的 <link href="..."/> 都认。"""
    for child in item:
        if _localname(child.tag) != "link":
            continue
        href = child.get("href")
        if href:
            return href.strip()
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


def _decode_xml(raw: bytes) -> str:
    """按 XML 声明 → utf-8 → gbk 顺序解码（中文 RSS 常见 gb2312）。"""
    m = re.search(rb'encoding=["\']([\w-]+)', raw[:2048])
    for enc in ([m[1].decode("ascii", "ignore")] if m else []) + ["utf-8", "gbk", "gb18030"]:
        try:
            return raw[:_RSS_MAX_BYTES].decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw[:_RSS_MAX_BYTES].decode("utf-8", "replace")
