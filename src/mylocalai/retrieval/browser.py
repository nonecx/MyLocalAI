# -*- coding: utf-8 -*-
"""受控浏览会话：让 Agent "像人一样"浏览网页——先读证据来源，再顺着页面里的
相关链接继续阅读，但每一步都只能"点"代码允许的链接。

安全模型（全部是代码约束，不依赖大模型判断）：
  1. 允许集    会话开始时只包含证据本里的 URL；之后只有"已打开页面中提取出的、
               域名等级为官方/媒体/门户的链接"才能进入允许集（深度最多 1 跳）。
  2. 预算      每个会话最多打开 max_pages 页（默认 3），每页最多吸收
               max_links_per_page 条新链接（默认 5）——即使页面被注入恶意指令，
               大模型也只能从代码给出的候选里选，无法触发任意抓取。
  3. 抓取防线  实际抓取仍走 search.fetch_article：SSRF 私网拒绝、仅
               http/https、响应大小与超时上限、正文太短视为反爬页。
  4. 参数面    对外工具只接受 int 索引，不接受 URL 字符串——大模型无法注入地址。

MCP 封装（mcp_server.py）与 LangChain 工具（tools.read_source）共用本会话。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from .search import _strip_tags, fetch_article
from .sources import LINK_TIERS as _CONFIGURED_LINK_TIERS
from .sources import classify_domain, normalize_host

# 浏览会话只吸收这两级域名的链接（官方权威 / 权威媒体）；门户不再向外扩链，
# 防止从门户页面无限扩散。具体哪几档由 sources_registry.json 的 link_tiers 声明。
LINK_TIERS = _CONFIGURED_LINK_TIERS


def extract_links(html: str, base_url: str,
                  max_links: int = 5) -> List[Dict[str, str]]:
    """从页面 HTML 抽取域名等级达标的 <a> 链接（相对地址按 base_url 解析）。"""
    import re
    out: List[Dict[str, str]] = []
    seen = set()
    for m in re.finditer(r"<a[^>]+href=[\"'](https?://[^\"']+|/[^\"']*)[\"'][^>]*>(.*?)</a>",
                         html, re.S | re.I):
        try:
            url = urljoin(base_url, m[1])
        except ValueError:
            continue
        url = url.split("#")[0].rstrip("/")
        host = normalize_host(url)
        if not host or classify_domain(url) not in LINK_TIERS:
            continue
        title = _strip_tags(m[2])
        if len(title) < 8 or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "title": title[:60], "site": host,
                    "domain_tier": classify_domain(url)})
        if len(out) >= max_links:
            break
    return out


class BrowserSession:
    """一次分析对应的浏览会话：允许集 + 页数预算 + 深度限制，线程安全。"""

    def __init__(
        self,
        seed_urls: List[str],
        fetch_fn: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
        max_pages: int = 3,
        max_links_per_page: int = 5,
    ) -> None:
        # 抓取实现调用时解析（而非绑定默认参数），便于测试与部署替换
        self._fetch = fetch_fn or fetch_article
        self._max_pages = max_pages
        self._max_links = max_links_per_page
        self._lock = threading.Lock()
        self._pages: List[Dict[str, Any]] = []       # 允许集：[{url,title,site,tier,source,opened}]
        self._text_cache: Dict[str, Dict[str, Any]] = {}
        self.opened_count = 0
        for u in seed_urls:
            self._add_page(u, source="证据本", depth=0)

    # -- 允许集管理 ----------------------------------------------------- #
    def _add_page(self, url: str, title: str = "", source: str = "", depth: int = 0) -> int:
        """把 URL 加入允许集（不去重检查由调用方做）。返回其索引。"""
        self._pages.append({
            "url": url, "title": title, "site": normalize_host(url),
            "domain_tier": classify_domain(url), "source": source,
            "depth": depth, "opened": False,
        })
        return len(self._pages) - 1

    def list_pages(self) -> List[Dict[str, Any]]:
        """允许集清单（给大模型看的目录，不含正文）。"""
        with self._lock:
            return [
                {"index": i, "title": p["title"] or p["url"][:60], "site": p["site"],
                 "source": p["source"], "domain_tier": p["domain_tier"],
                 "opened": p["opened"]}
                for i, p in enumerate(self._pages)
            ]

    # -- 核心动作 ------------------------------------------------------- #
    def read(self, index: int) -> Tuple[bool, Dict[str, Any]]:
        """阅读允许集内第 index 页：返回正文 + 页内新发现的相关链接目录。

        拒绝情形全部由代码判定并给出原因：索引越界 / 非允许 URL / 预算耗尽 /
        抓取失败。任何拒绝都不会改变会话状态。
        """
        with self._lock:
            if not isinstance(index, int) or isinstance(index, bool):
                return False, {"error": "只接受整数索引"}
            if index < 0 or index >= len(self._pages):
                return False, {"error": f"索引越界：允许集共 {len(self._pages)} 页（0..{len(self._pages) - 1}）"}
            page = self._pages[index]
            url = page["url"]
            if url in self._text_cache:
                cached = self._text_cache[url]
                return True, {"url": url, "cached": True, **cached, "new_links": []}
            if self.opened_count >= self._max_pages:
                return False, {"error": f"浏览预算已用完（本会话最多打开 {self._max_pages} 页）"}

        # 抓取放在锁外（网络 IO 不占锁）；状态回写重新加锁
        art = None
        try:
            art = self._fetch(url, return_html=True)
        except Exception:      # noqa: BLE001  抓取失败视为该页不可读
            art = None
        if not art or not art.get("text"):
            with self._lock:
                return False, {"error": "该页面无法读取（反爬/超时/非新闻页）", "url": url}

        new_links = []
        if art.get("html") and page["depth"] == 0:   # 只有证据本页(深度0)能扩链
            candidates = extract_links(art["html"], art.get("final_url") or url,
                                       self._max_links)
        else:
            candidates = []
        with self._lock:
            self.opened_count += 1
            self._pages[index]["opened"] = True
            self._text_cache[url] = {"text": art["text"]}
            known = {p["url"] for p in self._pages}
            for c in candidates:
                if c["url"] in known:
                    continue
                idx = self._add_page(c["url"], title=c["title"],
                                     source=f"来自 {page['site']}", depth=1)
                new_links.append(self._pages[idx])
        link_view = [{"index": self._pages.index(p), "title": p["title"],
                      "site": p["site"], "domain_tier": p["domain_tier"]}
                     for p in new_links]
        return True, {"url": url, "cached": False, "text": art["text"],
                      "site": page["site"], "domain_tier": page["domain_tier"],
                      "new_links": link_view}

    # -- 会话审计 ------------------------------------------------------- #
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "allowed_pages": len(self._pages),
                "opened_pages": self.opened_count,
                "max_pages": self._max_pages,
                "budget_left": max(0, self._max_pages - self.opened_count),
            }
