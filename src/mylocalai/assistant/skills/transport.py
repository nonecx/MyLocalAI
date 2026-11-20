# -*- coding: utf-8 -*-
"""安全取页（取证三技能模块 A）：DNS 钉扎、逐跳重定向复核、体积/时限上限。

改了什么、为什么、锁定什么：
  新增 assistant/skills/transport.py，作为取证三技能唯一的对外取页通道（《总览》§5.4
  安全模型的代码落地，全部是代码约束，不依赖模型判断）：
    - DNS 钉扎：解析结果逐个校验为公网后才发起连接，连校验过的 IP（request 的
      pinned_ip），TLS 仍按原主机名校验证书——防 DNS rebinding，且不削弱证书校验；
    - 混合解析直接失败：同时含公网与私网地址 → dns_mixed（宁可拒绝，不猜一个连）；
    - 逐跳重定向复核：手工跟随 3xx，每一跳重做 URL 语法 + DNS + 域名允许集校验；
    - 体积上限：响应体与解压后正文都不得超过 max_bytes（逐段限流解压，防压缩炸弹）；
    - 内容类型白名单：application/pdf 等一律 unsupported_type——证据链不解析 PDF，
      如实记错误，不当正文；
    - 绝对死线 / 协作取消 / 并发槽位：threading.BoundedSemaphore(max_concurrency)
      整个 fetch 期间持锁，等槽位也受死线约束；每次 IO 前与每跳之间检查取消。
  锁定：fetch 永不抛异常出模块——成功与失败都是统一形状的 dict；TransportError.category
  是封闭集合；send 契约（request 含 url/hostname/pinned_ip/port/scheme/headers/timeout）
  对生产实现与测试替身同时生效。
  依赖方向：只用标准库，不 import 任何 mylocalai 内部包（tests/test_import_rules.py）。
"""

from __future__ import annotations

import html as _html
import http.client
import ipaddress
import math
import re
import socket
import ssl
import threading
import time
import zlib
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urljoin, urlparse

__all__ = ["SafeTransport", "TransportError", "default_transport",
           "is_public_ip", "strip_query_operators"]

_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_COMPRESSED = ("gzip", "x-gzip", "deflate")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_MIN_TEXT_CHARS = 8        # 正文短于此视为没有正文（与检索层 _extract_text 同口径：滤导航残片）
_SLOT_POLL_S = 0.05        # 等并发槽位的轮询粒度：既要及时响应取消/死线，又不空转 CPU


# ===========================================================================
# 错误与基础判定
# ===========================================================================

class TransportError(Exception):
    """取页失败。category 为封闭集合，detail 为可读简述。

    封闭集合（调用方与测试都按此编程）：
      bad_url / dns_private / dns_mixed / dns_failed / too_many_hops / too_large /
      timeout / deadline / unsupported_type / network / cancelled / redirect_invalid /
      domain_not_allowed
    HTTP 4xx/5xx 归入 network：取到了响应但没有可用正文，封闭集合里没有更细的档，
    如实把状态码写进 detail。
    """

    def __init__(self, category: str, detail: str = "") -> None:
        super().__init__(f"{category}: {detail}" if detail else category)
        self.category = category
        self.detail = detail


def _now() -> float:
    """单调时钟间接层：测试可注入假时钟，确定性地构造"死线在两次跳之间到期"。"""
    return time.monotonic()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ip_literal(host: str) -> Optional[str]:
    """主机名是 IP 字面量时返回规范化字符串，否则 None（带 scope 的 IPv6 去掉 %zone）。"""
    try:
        return str(ipaddress.ip_address(str(host).split("%")[0]))
    except ValueError:
        return None


def is_public_ip(ip: str) -> bool:
    """公网判定：私网/环回/链路本地/保留/组播/未指定一律 False。

    IPv4 映射 IPv6（::ffff:10.0.0.1）按内层 IPv4 判，防映射地址绕过内网判定；
    无法解析的输入一律 False（fail-closed：判不了就不连）。
    """
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _normalize_domain(host: str) -> str:
    """输出用域名：小写、去尾点、去一层 www. 前缀（允许集匹配用原始主机名，不走这里）。"""
    domain = (host or "").strip().lower().rstrip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _domain_allowed(host: str, allowed: Optional[List[str]]) -> bool:
    """域名允许集：host 等于某允许域或是其子域即命中（a.b.com 命中 b.com）；空集 = 不限制。"""
    if not allowed:
        return True
    for entry in allowed:
        domain = str(entry or "").strip().lower().lstrip("*").lstrip(".").rstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            return True
    return False


def _strip_tags(fragment: str) -> str:
    """去标签、反转义实体、压空白（与检索层同口径）。"""
    text = re.sub(r"<[^>]+>", "", fragment or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _decode_body(raw: bytes, content_type: str) -> str:
    """按 Content-Type 声明 → meta charset → utf-8 → gbk 的顺序解码中文页面。"""
    declared = ""
    for part in (content_type or "").split(";")[1:]:
        if "charset=" in part.lower():
            declared = part.split("=", 1)[1].strip().strip("\"'")
            break
    meta = ""
    found = re.search(rb"charset=[\"']?([\w-]+)", raw[:2048])
    if found:
        meta = found[1].decode("ascii", "ignore")
    for enc in [e for e in (declared, meta) if e] + ["utf-8", "gbk", "gb18030"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _extract_page(page: str) -> Tuple[str, str]:
    """返回 (title, text)：去 script/style/noscript/title，压空白，截断由调用方做。

    正文短于 _MIN_TEXT_CHARS 视为"提取不到正文"：成功但 text=""，由调用方（pipeline）
    决定记错误还是丢弃——取页层不替业务判有没有价值。
    """
    found = re.search(r"(?is)<title[^>]*>(.*?)</title>", page or "")
    title = _strip_tags(found[1]) if found else ""
    body = re.sub(r"(?is)<(script|style|noscript|title)\b[^>]*>.*?</\1>", " ", page or "")
    body = re.sub(r"(?is)<(script|style|noscript|title)\b[^>]*>", " ", body)
    text = _strip_tags(body)
    return title, (text if len(text) >= _MIN_TEXT_CHARS else "")


def _inflate_with(raw: bytes, wbits: int, max_bytes: int) -> bytes:
    """按给定 wbits 限流解压：累计输出到 max_bytes 立即以 too_large 失败。"""
    dec = zlib.decompressobj(wbits)
    out = bytearray()
    data = raw
    limit = max_bytes + 1
    while data and not dec.eof:
        out += dec.decompress(data, limit - len(out))
        if len(out) >= limit:
            raise TransportError("too_large",
                                 f"解压后正文超过 {max_bytes} 字节上限（疑似压缩炸弹）")
        tail = dec.unconsumed_tail
        if tail == data:      # 无进展：防御性退出，避免死循环
            break
        data = tail
    if not dec.eof:
        raise TransportError("network", "压缩响应不完整或已损坏")
    return bytes(out)


def _inflate(raw: bytes, encoding: str, max_bytes: int) -> bytes:
    """解压 gzip/deflate 响应体；raw deflate（少数服务器不发 zlib 包装）走 -MAX_WBITS 兜底。"""
    attempts: Tuple[int, ...] = ((16 + zlib.MAX_WBITS,) if encoding in ("gzip", "x-gzip")
                                 else (zlib.MAX_WBITS, -zlib.MAX_WBITS))
    for wbits in attempts:
        try:
            return _inflate_with(raw, wbits, max_bytes)
        except zlib.error:
            continue
    raise TransportError("network", f"解压失败（Content-Encoding: {encoding}）")


# ===========================================================================
# 模块级辅助（§2.3）
# ===========================================================================

def strip_query_operators(query: str) -> str:
    """剥掉模型生成的 site:/inurl:/intitle:/filetype: 等范围操作符（《总览》§5.4）。

    域名限定由代码按已核验信源拼接，模型给的操作符一律剥掉，否则检索会被带到
    未核验域名。操作符前的 +/- 一并剥；无操作符的查询原样返回（仅压空白）。
    """
    text = query if isinstance(query, str) else ""
    return " ".join(_SCOPE_RE.sub(" ", text).split())


_SCOPE_OPERATORS = ("site", "inurl", "allinurl", "intitle", "allintitle", "filetype",
                    "ext", "allintext", "intext", "inanchor", "link", "related",
                    "cache", "author", "source")
# 前置边界只用 ASCII 字符类：中文查询里"新冠site:who.int"没有空格也要能剥，
# 而 "mysite:"（y 在前）不能误剥。
_SCOPE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])[+-]?(?:%s)\s*:\s*(?:\"[^\"]*\"|'[^']*'|\S+)"
    % "|".join(_SCOPE_OPERATORS))


# ===========================================================================
# 生产 send：标准库 http.client + DNS 钉扎连接
# ===========================================================================

class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP 明文连接也钉扎：connect 连 pinned_ip，不再走一次系统 DNS。"""

    def __init__(self, host: str, pinned_ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port),
                                             timeout=self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS 钉扎连接：socket 连 pinned_ip，wrap_socket 仍按原主机名校验证书。

    DNS 钉扎只换"连哪个 IP"，不换"校验哪个名字"——否则钉扎就变成了绕过证书校验。
    """

    def __init__(self, host: str, pinned_ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _resolve_host(host: str) -> List[str]:
    """生产 resolver：socket.getaddrinfo 收集全部 IP（保序去重）。"""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(info[4][0] for info in infos))


def _send_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """生产 send：http.client + 钉扎连接，返回 {"status","headers","body"}。

    额外用 request["max_bytes"] 限流：响应体超限即抛 TransportError("too_large")，
    不在内存里堆整个炸弹（压缩炸弹由解压层的逐段限流负责）。
    """
    url = str(request.get("url") or "")
    parsed = urlparse(url)
    scheme = str(request.get("scheme") or parsed.scheme or "https").lower()
    hostname = str(request.get("hostname") or parsed.hostname or "")
    pinned_ip = str(request.get("pinned_ip") or "")
    port = int(request.get("port") or _DEFAULT_PORTS.get(scheme, 443))
    timeout = float(request.get("timeout") or 10.0)
    headers = dict(request.get("headers") or {})
    headers.setdefault("Host", hostname if port in (80, 443) else f"{hostname}:{port}")
    headers.setdefault("User-Agent", _USER_AGENT)
    headers.setdefault("Accept", "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5")
    headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    limit = int(request.get("max_bytes") or 0)
    conn_cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    conn = conn_cls(hostname, pinned_ip, port, timeout)
    try:
        conn.request("GET", target, headers=headers)
        resp = conn.getresponse()
        body = resp.read(limit + 1) if limit else resp.read()
        if limit and len(body) > limit:
            raise TransportError("too_large", f"响应体超过 {limit} 字节上限")
        return {"status": int(resp.status),
                "headers": {str(k).lower(): str(v) for k, v in resp.getheaders()},
                "body": bytes(body)}
    except TransportError:
        raise
    except (socket.timeout, TimeoutError):
        raise TransportError("timeout", "socket IO 超时") from None
    except (OSError, http.client.HTTPException) as exc:
        raise TransportError("network", f"{type(exc).__name__}: {exc}") from None
    finally:
        conn.close()


# ===========================================================================
# 安全取页主体
# ===========================================================================

class _UrlParts(NamedTuple):
    scheme: str
    host: str
    port: Optional[int]


class _Plan(NamedTuple):
    """一跳请求的钉扎计划：连哪个 IP、用什么端口、Host 头写谁。"""

    url: str
    scheme: str
    host: str
    port: int
    ip: str
    ipv6_literal: bool


class SafeTransport:
    """安全取页：DNS 钉扎 + 逐跳重定向复核 + 体积/时限/取消/并发槽位约束。

    所有 IO 都可注入（dns_resolver / send），因此全离线可测、零网络零 LLM。
    fetch 永不抛异常出模块：成功与失败都是统一形状的 dict。
    """

    def __init__(self, *, max_hops: int = 3, max_bytes: int = 1_500_000,
                 max_text_chars: int = 20_000, timeout_s: float = 10.0,
                 max_concurrency: int = 4,
                 dns_resolver: Optional[Callable[[str], List[str]]] = None,
                 send: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
                 allowed_content_types: Tuple[str, ...] = ("text/html",
                     "application/xhtml+xml", "text/plain")) -> None:
        # 参数校验口径与 propagation.py 一致：bool 是 int 子类，必须显式排除
        for name, value in (("max_hops", max_hops), ("max_bytes", max_bytes),
                            ("max_text_chars", max_text_chars),
                            ("max_concurrency", max_concurrency)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是 ≥1 的整数")
        timeout_s = float(timeout_s)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s 必须是有限正数")
        self.max_hops = max_hops
        self.max_bytes = max_bytes
        self.max_text_chars = max_text_chars
        self.timeout_s = timeout_s
        self.max_concurrency = max_concurrency
        self._resolver = dns_resolver or _resolve_host
        self._send = send or _send_request
        # 内容类型白名单归一化：去掉参数（"; charset=…"）后小写比较
        self._allowed_types = tuple(
            str(ct).split(";")[0].strip().lower() for ct in allowed_content_types)
        self._slots = threading.BoundedSemaphore(max_concurrency)

    # ---------------- 对外入口 ----------------

    def fetch(self, url: str, *, allowed_domains: Optional[List[str]] = None,
              cancel_check: Optional[Callable[[], bool]] = None,
              deadline: Optional[float] = None) -> Dict[str, Any]:
        """安全取一页，返回统一形状 dict（TransportError 也不外泄，一律折成 error dict）。

        成功：{"status":"ok","url":<原始url>,"final_url","domain","title","text",
               "bytes","content_type","hops":[{"url","domain","ip"}…含首跳],"fetched_at"}
        失败：{"status":"error","category":<封闭集合>,"detail","url","final_url":<已走到的url或"">}
        """
        if isinstance(allowed_domains, str):     # 单个域名按一项处理，别按字符迭代
            allowed_domains = [allowed_domains]
        original = url if isinstance(url, str) else ""
        hops: List[Dict[str, str]] = []   # 每跳记录（含首跳）；失败时用它还原"已走到的url"
        try:
            return self._fetch_once(original, allowed_domains, cancel_check,
                                    deadline, hops)
        except TransportError as exc:
            return {"status": "error", "category": exc.category, "detail": exc.detail,
                    "url": original, "final_url": hops[-1]["url"] if hops else ""}
        except Exception as exc:  # noqa: BLE001  兜底：取页链路任何意外都折成 error dict
            return {"status": "error", "category": "network",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "url": original, "final_url": hops[-1]["url"] if hops else ""}

    # ---------------- 单次取页（持并发槽位） ----------------

    def _fetch_once(self, url: str, allowed_domains: Optional[List[str]],
                    cancel_check: Optional[Callable[[], bool]],
                    deadline: Optional[float],
                    hops: List[Dict[str, str]]) -> Dict[str, Any]:
        self._check_deadline(deadline)
        if not self._acquire_slot(deadline, cancel_check):
            raise TransportError("deadline", "等待并发槽位期间超过绝对死线")
        try:
            current = url
            while True:
                # 每跳之间：死线 + 取消（命中就不再发起新连接）
                self._check_deadline(deadline)
                self._check_cancelled(cancel_check)
                if len(hops) >= self.max_hops:
                    raise TransportError("too_many_hops",
                                         f"重定向超过 {self.max_hops} 跳上限"
                                         f"（max_hops 含首跳）")
                plan = self._plan(current, allowed_domains)
                timeout = self._io_timeout(deadline)
                self._check_cancelled(cancel_check)   # 发起连接前的最后一次确认
                response = self._call_send(plan, timeout)
                hops.append({"url": current, "domain": _normalize_domain(plan.host),
                             "ip": plan.ip})
                status = int(response["status"])
                if status in _REDIRECT_STATUSES:
                    current = _next_url(current, response["headers"])
                    continue
                if not 200 <= status < 300:
                    raise TransportError("network", f"服务器返回 HTTP {status}")
                return self._build_result(url, current, plan, response, hops)
        finally:
            self._slots.release()

    # ---------------- 逐跳校验与钉扎 ----------------

    def _plan(self, url: str, allowed_domains: Optional[List[str]]) -> _Plan:
        """一跳前的全部校验：URL 语法 → 域名允许集 → DNS 钉扎。"""
        parts = _parse_http_url(url)
        if not _domain_allowed(parts.host, allowed_domains):
            raise TransportError("domain_not_allowed",
                                 f"主机 {parts.host} 不在允许域名集内")
        literal = _parse_ip_literal(parts.host)
        if literal is not None:
            ip = literal                       # IP 字面量已在语法层判过公网，直接钉扎
        else:
            ip = self._resolve_and_pin(parts.host)
        port = parts.port if parts.port is not None else _DEFAULT_PORTS[parts.scheme]
        return _Plan(url=url, scheme=parts.scheme, host=parts.host, port=port, ip=ip,
                     ipv6_literal=literal is not None and ":" in literal)

    def _resolve_and_pin(self, host: str) -> str:
        """解析主机名并钉扎到第一个公网 IP。

        任一私网/环回/链路本地/保留/组播/未指定 → dns_private；公私混杂 → dns_mixed；
        解析失败/空/返回无法解析的地址 → dns_failed。
        """
        try:
            raw = self._resolver(host)
        except Exception as exc:  # noqa: BLE001  解析器异常（gaierror 等）按解析失败处理
            raise TransportError("dns_failed",
                                 f"DNS 解析失败: {type(exc).__name__}") from None
        if not isinstance(raw, (list, tuple)) or not raw:
            raise TransportError("dns_failed", f"DNS 未返回 {host} 的地址")
        ips: List[str] = []
        for item in raw:
            ip = _parse_ip_literal(str(item).strip())
            if ip is None:
                raise TransportError("dns_failed", f"DNS 返回无法解析的地址: {item!r}")
            ips.append(ip)
        public = [ip for ip in ips if is_public_ip(ip)]
        if not public:
            raise TransportError("dns_private",
                                 f"{host} 解析到非公网地址: {', '.join(ips)}")
        if len(public) != len(set(ips)):
            raise TransportError("dns_mixed",
                                 f"{host} 同时解析到公网与非公网地址（混合解析直接失败）")
        return public[0]

    # ---------------- send 契约 ----------------

    def _call_send(self, plan: _Plan, timeout: float) -> Dict[str, Any]:
        """按 send 契约组装 request 并调用注入的 send；异常折成对应类别。"""
        request = {
            "url": plan.url,
            "hostname": plan.host,
            "pinned_ip": plan.ip,
            "port": plan.port,
            "scheme": plan.scheme,
            "headers": _request_headers(plan),
            "timeout": timeout,
            # 额外字段：生产 send 据此限流；测试替身可忽略
            "max_bytes": self.max_bytes,
        }
        try:
            response = self._send(request)
        except (socket.timeout, TimeoutError):
            raise TransportError("timeout", "单次 IO 超时") from None
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001  send 替身/连接层异常一律折成 network
            raise TransportError("network", f"{type(exc).__name__}: {exc}") from None
        return _normalize_response(response)

    # ---------------- 成功形状 ----------------

    def _build_result(self, original: str, final_url: str, plan: _Plan,
                      response: Dict[str, Any],
                      hops: List[Dict[str, str]]) -> Dict[str, Any]:
        raw = response["body"]
        if len(raw) > self.max_bytes:
            raise TransportError("too_large",
                                 f"响应体 {len(raw)} 字节超过 {self.max_bytes} 上限")
        encoding = response["headers"].get("content-encoding", "").strip().lower()
        if encoding in _COMPRESSED:
            raw = _inflate(raw, encoding, self.max_bytes)
        content_type = response["headers"].get("content-type", "").strip()
        ctype = content_type.split(";")[0].strip().lower()
        if not ctype:
            raise TransportError("unsupported_type", "响应未声明 Content-Type（按不支持处理）")
        if ctype not in self._allowed_types:
            raise TransportError("unsupported_type", f"不支持的内容类型: {ctype}")
        title, text = _extract_page(_decode_body(raw, content_type))
        if len(text) > self.max_text_chars:
            text = text[: self.max_text_chars]
        return {"status": "ok", "url": original, "final_url": final_url,
                "domain": _normalize_domain(plan.host), "title": title, "text": text,
                "bytes": len(text.encode("utf-8")), "content_type": ctype,
                "hops": [dict(hop) for hop in hops], "fetched_at": _utc_now_iso()}

    # ---------------- 死线 / 取消 / 并发槽位 ----------------

    def _check_deadline(self, deadline: Optional[float]) -> None:
        if deadline is not None and _now() >= float(deadline):
            raise TransportError("deadline", "已超过绝对死线")

    def _remaining(self, deadline: Optional[float]) -> Optional[float]:
        """死线剩余秒数；无死线返回 None，已过死线抛 deadline。"""
        if deadline is None:
            return None
        left = float(deadline) - _now()
        if left <= 0:
            raise TransportError("deadline", "已超过绝对死线")
        return left

    def _io_timeout(self, deadline: Optional[float]) -> float:
        """单次 IO 超时 = timeout_s 与死线剩余取小。"""
        remaining = self._remaining(deadline)
        return self.timeout_s if remaining is None else min(self.timeout_s, remaining)

    def _acquire_slot(self, deadline: Optional[float],
                      cancel_check: Optional[Callable[[], bool]]) -> bool:
        """拿并发槽位：轮询式等待，死线到/取消即放弃（等槽位等到死线 → deadline）。"""
        while True:
            remaining = self._remaining(deadline)
            wait = _SLOT_POLL_S if remaining is None else min(_SLOT_POLL_S, max(remaining, 0.0))
            if self._slots.acquire(timeout=wait):
                return True
            self._check_cancelled(cancel_check)

    def _check_cancelled(self, cancel_check: Optional[Callable[[], bool]]) -> None:
        if _cancelled(cancel_check):
            raise TransportError("cancelled", "协作取消：不再发起新连接")


def _cancelled(cancel_check: Optional[Callable[[], bool]]) -> bool:
    if cancel_check is None:
        return False
    try:
        return bool(cancel_check())
    except Exception:  # noqa: BLE001  取消探针出错按"未取消"处理，不因探针故障中断取页
        return False


def _parse_http_url(url: Any) -> _UrlParts:
    """URL 语法校验（每跳都做）：仅 http/https、无用户信息、主机名非空、
    拒绝 localhost/.local/.internal 后缀与 IP 字面量中的非公网地址。坏语法 → bad_url。"""
    if not isinstance(url, str) or not url.strip():
        raise TransportError("bad_url", "URL 为空或不是字符串")
    if any(ord(ch) <= 0x20 for ch in url):
        raise TransportError("bad_url", "URL 含空格或控制字符")
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise TransportError("bad_url", f"URL 解析失败: {exc}") from None
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise TransportError("bad_url", "仅支持 http/https 绝对 URL")
    if parsed.username is not None or parsed.password is not None:
        raise TransportError("bad_url", "URL 不允许携带用户信息（user:pass@）")
    host = (host or "").rstrip(".").lower()
    if not host:
        raise TransportError("bad_url", "URL 缺少主机名")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise TransportError("bad_url", f"本机/内网主机名不被允许: {host}")
    literal = _parse_ip_literal(host)
    if literal is not None and not is_public_ip(literal):
        raise TransportError("bad_url", f"IP 字面量不是公网地址: {host}")
    return _UrlParts(scheme=scheme, host=host, port=port)


def _next_url(current: str, headers: Dict[str, str]) -> str:
    """从 3xx 响应解析下一跳；Location 缺失/非 http(s)/含控制字符 → redirect_invalid。

    下一跳的完整安全校验（语法/DNS/允许集）交给循环开头的 _plan，与首跳同口径。
    """
    location = str(headers.get("location") or "").strip()
    if not location:
        raise TransportError("redirect_invalid", "重定向响应缺少 Location 头")
    if any(ord(ch) <= 0x20 for ch in location):
        raise TransportError("redirect_invalid", "Location 含空格或控制字符")
    try:
        nxt = urljoin(current, location)
        scheme = (urlparse(nxt).scheme or "").lower()
    except ValueError:
        raise TransportError("redirect_invalid", "重定向目标无法解析") from None
    if scheme not in ("http", "https"):
        raise TransportError("redirect_invalid",
                             f"重定向目标不是 http(s): {location[:80]}")
    return nxt


def _request_headers(plan: _Plan) -> Dict[str, str]:
    """发给 send 的请求头：Host 写原主机名（IPv6 字面量加括号），非默认端口带端口。"""
    host = f"[{plan.host}]" if plan.ipv6_literal else plan.host
    if plan.port not in (80, 443):
        host = f"{host}:{plan.port}"
    return {"Host": host,
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            "Accept-Language": "zh-CN,zh;q=0.9"}


def _normalize_response(response: Any) -> Dict[str, Any]:
    """校验 send 返回形状：{"status": int, "headers": {小写名: 值}, "body": bytes}。"""
    if not isinstance(response, dict):
        raise TransportError("network", "send 返回不是 dict")
    status = response.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        raise TransportError("network", "send 返回缺少整数 status")
    raw_headers = response.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise TransportError("network", "send 返回的 headers 不是 dict")
    headers = {str(k).lower(): ("" if v is None else str(v))
               for k, v in raw_headers.items()}
    body = response.get("body")
    if body is None:
        body = b""
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise TransportError("network", "send 返回的 body 不是 bytes")
    return {"status": status, "headers": headers, "body": bytes(body)}


# ===========================================================================
# 生产工厂（§2.3）
# ===========================================================================

_DEFAULT_LOCK = threading.Lock()
_DEFAULT_TRANSPORT: Optional[SafeTransport] = None


def default_transport(**kw: Any) -> SafeTransport:
    """生产工厂：默认 resolver（socket.getaddrinfo）+ 默认 send（http.client 钉扎连接）。

    无参调用返回进程内共享单例（多个调用方共用一个并发槽位池，全局并发有上限）；
    带参调用返回按参数新建的实例，resolver/send 仍接生产默认件。
    """
    global _DEFAULT_TRANSPORT
    if kw:
        return SafeTransport(dns_resolver=_resolve_host, send=_send_request, **kw)
    with _DEFAULT_LOCK:
        if _DEFAULT_TRANSPORT is None:
            _DEFAULT_TRANSPORT = SafeTransport(dns_resolver=_resolve_host,
                                               send=_send_request)
        return _DEFAULT_TRANSPORT
