# -*- coding: utf-8 -*-
"""信息流候选（platform/candidates.py）：归一化与两道去重闸。

FeedCandidate 是"发现层"的统一形状：无论来自搜索引擎还是 RSS，进到 runner
之前都归一成它。去重两道闸：
  1. **URL 去重**（本模块 url_dedup_key / dedup_candidates）：同链接只留第一条；
     库侧还有 feed_entries.url 唯一约束兜底（跨巡检去重）。
  2. **标题指纹去重**（title_similarity）：同一事件被多家站点转载时 URL 不同、
     标题近似——用 dossier.store.title_similarity（与 assistant.fingerprint
     逐字节同口径的存储侧副本，parity 测试锁定）判相似，达阈值视为同一事件。

标题相似度阈值由调用方（runner）从 platform_rules.json 的
dedup.fingerprint_threshold 取，本模块不持有配置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from mylocalai.dossier.store import title_similarity
from mylocalai.retrieval.sources import classify_domain, extract_date, normalize_host

__all__ = ["FeedCandidate", "url_dedup_key", "dedup_candidates", "near_duplicate",
           "candidate_from_search_result"]

#: 标题进指纹比对前的长度上限（防超长标题拖慢 shingle；超出截断）。
_MAX_TITLE_CHARS = 120
#: 摘要入库上限（与 feed_entries.snippet 列宽一致）。
_MAX_SNIPPET_CHARS = 2000


@dataclass
class FeedCandidate:
    """一条发现候选（未落库的原始形状）。"""

    title: str
    url: str
    snippet: str = ""
    domain: str = ""
    domain_tier: str = ""
    published_at: str = ""
    source: str = ""                 # 提供方名（search / rss:<feed name>）
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet,
                "domain": self.domain, "domain_tier": self.domain_tier,
                "published_at": self.published_at, "source": self.source}


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def candidate_from_search_result(result: Any, source: str = "search") -> Optional[FeedCandidate]:
    """检索结果 → FeedCandidate。坏形状（无 URL）返回 None，绝不抛。"""
    if isinstance(result, dict):
        url = str(result.get("url") or "").strip()
        title = str(result.get("title") or "").strip()
        snippet = str(result.get("snippet") or "")
        date = str(result.get("date") or "")
        tier = str(result.get("domain_tier") or "")
    else:
        url = str(getattr(result, "url", "") or "").strip()
        title = str(getattr(result, "title", "") or "").strip()
        snippet = str(getattr(result, "snippet", "") or "")
        date = str(getattr(result, "date", "") or "")
        tier = str(getattr(result, "domain_tier", "") or "")
    if not url or not url.startswith(("http://", "https://")):
        return None
    if not tier:
        tier = classify_domain(url)
    return FeedCandidate(
        title=_clean_text(title, 200), url=url, snippet=_clean_text(snippet, _MAX_SNIPPET_CHARS),
        domain=normalize_host(url), domain_tier=tier,
        published_at=date or (extract_date(snippet, url) or ""), source=source)


def url_dedup_key(url: str) -> str:
    """URL 去重键：小写主机、去 fragment、去尾部斜杠（与检索层 _dedup_url 同口径）。"""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return str(url or "").strip()
    if not parsed.hostname:
        return str(url or "").strip()
    path = parsed.path or "/"
    return f"{parsed.hostname.lower()}{path.rstrip('/') or '/'}"


def dedup_candidates(candidates: Iterable[FeedCandidate]) -> List[FeedCandidate]:
    """批内 URL 去重：保留首次出现顺序（检索排序即优先级，不打乱）。"""
    seen = set()
    out: List[FeedCandidate] = []
    for c in candidates or []:
        if c is None:
            continue
        key = url_dedup_key(c.url)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def near_duplicate(candidate: FeedCandidate, others: Iterable[FeedCandidate],
                   threshold: float) -> bool:
    """candidate 的标题与 others 里任一条达阈值 → True（同一事件的转载）。

    阈值越界（非有限数/负数）按 0 处理（永不误判为"不相似"以外的极端行为：
    阈值<=0 时只有空标题对空标题才命中，而空标题候选在归一化阶段已被挡掉）。
    """
    try:
        thr = float(threshold)
    except (TypeError, ValueError):
        thr = 0.0
    title = str(candidate.title or "")[:_MAX_TITLE_CHARS]
    if not title:
        return False
    for other in others or []:
        if other is None or other is candidate:
            continue
        if title_similarity(title, str(other.title or "")[:_MAX_TITLE_CHARS]) >= thr:
            return True
    return False
