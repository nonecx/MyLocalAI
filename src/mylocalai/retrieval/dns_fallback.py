# -*- coding: utf-8 -*-
"""DNS 回退：系统解析失败时，用公网 DNS（DNSPod/阿里）再查一次。

起因：本机运营商 DNS 偶发解析不了 www.so.com（其他域名正常，nslookup 指定
202.96.128.86 或 119.29.29.29 都能解析，直连 IP 也通）。与其换搜索后端，
不如让解析层自愈：

    install() 把 socket.getaddrinfo 包一层——原版成功时原样返回（零影响），
    失败时发一个极简 UDP DNS A 查询到公网 DNS，成功则缓存 10 分钟并返回。

不引入第三方依赖，响应报文手工解析 A 记录（CNAME 段跳过）。
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

_PUBLIC_DNS = ("119.29.29.29", "223.5.5.5")   # DNSPod / 阿里
_QUERY_TIMEOUT = 2.0
_CACHE_TTL = 600.0                            # 成功解析缓存 10 分钟
_installed = False
_lock = threading.Lock()
_cache: Dict[str, Tuple[float, List[str]]] = {}
_orig_getaddrinfo = socket.getaddrinfo


def _build_query(host: str, qid: int) -> bytes:
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in host.split(".")) + b"\x00"
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    return header + qname + struct.pack(">HH", 1, 1)   # QTYPE=A, QCLASS=IN


def _read_name(data: bytes, offset: int) -> Tuple[str, int]:
    """读一个（可能带压缩指针的）域名，返回 (域名, 下一个字段偏移)。"""
    labels, jumps = [], 0
    end = -1
    while True:
        if offset >= len(data):
            raise ValueError("DNS 响应截断")
        n = data[offset]
        if n == 0:
            offset += 1
            break
        if n & 0xC0 == 0xC0:                      # 压缩指针
            if end < 0:
                end = offset + 2
            offset = ((n & 0x3F) << 8) | data[offset + 1]
            jumps += 1
            if jumps > 8:
                raise ValueError("DNS 指针循环")
            continue
        labels.append(data[offset + 1:offset + 1 + n].decode("ascii", "replace"))
        offset += 1 + n
    return ".".join(labels), (end if end >= 0 else offset)


def query_a(host: str) -> Optional[List[str]]:
    """向公网 DNS 查 A 记录，返回 IPv4 列表；全部失败返回 None。"""
    for server in _PUBLIC_DNS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(_QUERY_TIMEOUT)
                qid = int(time.time()) & 0xFFFF
                s.sendto(_build_query(host, qid), (server, 53))
                data, _ = s.recvfrom(1024)
            if len(data) < 12 or struct.unpack(">H", data[:2])[0] != qid:
                continue
            rcode = data[3] & 0x0F
            if rcode != 0:
                continue
            qdcount, ancount = struct.unpack(">HH", data[4:8])
            off = 12
            for _ in range(qdcount):
                _, off = _read_name(data, off)
                off += 4
            ips: List[str] = []
            for _ in range(ancount):
                _, off = _read_name(data, off)
                rtype, _, _, rdlen = struct.unpack(">HHIH", data[off:off + 10])
                off += 10
                if rtype == 1 and rdlen == 4:
                    ips.append(".".join(str(b) for b in data[off:off + 4]))
                off += rdlen
            if ips:
                return ips
        except (OSError, ValueError, struct.error):
            continue
    return None


def _fallback_getaddrinfo(orig_exc, host, port, family=0, type=0, proto=0, flags=0):
    """getaddrinfo 失败后的回退：查公网 DNS 拿 IPv4。仅处理字符串主机名。"""
    if not isinstance(host, (str, bytes)):
        raise orig_exc
    host_str = host.decode("ascii", "replace") if isinstance(host, bytes) else host
    if not host_str or all(c in "0123456789.:" for c in host_str):
        raise orig_exc                         # IP 字面量/空值没有回退意义
    now = time.time()
    with _lock:
        hit = _cache.get(host_str)
    if hit and hit[0] > now:
        ips = hit[1]
    else:
        ips = query_a(host_str)
        if ips:
            with _lock:
                _cache[host_str] = (now + _CACHE_TTL, ips)
        else:
            raise orig_exc
    if isinstance(port, str):
        try:
            port = socket.getservbyname(port)
        except OSError:
            port = 0
    port = port or 0
    results = []
    for ip in ips:
        results.append((socket.AF_INET, type or socket.SOCK_STREAM, proto or socket.IPPROTO_TCP,
                        "", (ip, port)))
    return results


def install() -> None:
    """安装 getaddrinfo 包装（幂等）。已安装则什么都不做。"""
    global _installed, _orig_getaddrinfo
    if _installed:
        return
    _orig_getaddrinfo = socket.getaddrinfo

    def wrapped(host, port, family=0, type=0, proto=0, flags=0):
        try:
            return _orig_getaddrinfo(host, port, family, type, proto, flags)
        except socket.gaierror as exc:
            return _fallback_getaddrinfo(exc, host, port, family, type, proto, flags)

    wrapped.dns_fallback = True                 # type: ignore[attr-defined]
    socket.getaddrinfo = wrapped
    _installed = True
