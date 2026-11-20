# -*- coding: utf-8 -*-
"""skills/transport.py 测试（取证三技能安全取页，契约 §2.4）：共 31 例。

锁定（抓真实缺陷的标准）：
   - 私网 / 混合 DNS / 重定向 / 超量 / 压缩 / PDF / 死线 / 并发槽位（《总览》§10.3）；
   - URL 语法（scheme/用户信息/本机后缀/IP 字面量）、域名允许集（子域命中）、
     协作取消、正文提取、send 契约（pinned_ip/Host 头）、strip_query_operators、
     is_public_ip 判定矩阵；
   - fetch 统一形状：成功/失败都是 dict，永不抛异常出模块，category 是封闭集合。
全离线：resolver 与 send 全部注入假件，零网络零 LLM；并发槽位用例用假 send 记录
同时在飞请求数；"死线在两次跳之间到期"用假单调时钟构造。
"""

from __future__ import annotations

import gzip
import socket
import sys
import threading
import time
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant.skills import transport as transport_mod  # noqa: E402
from mylocalai.assistant.skills.transport import (  # noqa: E402
    SafeTransport, default_transport, is_public_ip, strip_query_operators)

PAGE = ("<html><head><title>测试页面标题</title>"
        "<style>.x{color:red}</style>"
        "<script>var secret = 1;</script></head>"
        "<body><p>这是正文第一段，包含足够长度的文字用于提取。</p>"
        "<div><noscript>请开启脚本功能</noscript></div></body></html>")

# TransportError.category 封闭集合（契约 §2.1）
_CATEGORIES = {"bad_url", "dns_private", "dns_mixed", "dns_failed", "too_many_hops",
               "too_large", "timeout", "deadline", "unsupported_type", "network",
               "cancelled", "redirect_invalid", "domain_not_allowed"}


def static_resolver(*ips):
    """不管什么主机名都返回同一组 IP 的假 resolver。"""
    return lambda host: list(ips)


def raising_resolver(host):
    """一被调用就失败的假 resolver：用来证明"走了 DNS"这件事没发生。"""
    raise socket.gaierror(f"resolver should not be called for {host}")


def mapped_resolver(mapping, calls=None):
    """按主机名返回 IP 列表的假 resolver；可选记录被解析过的主机名。"""
    def _resolve(host):
        if calls is not None:
            calls.append(host)
        return list(mapping.get(host, []))

    return _resolve


def html_send(body=None, status=200, headers=None, recorder=None):
    """返回 200 + text/html 的假 send；可附加额外响应头、记录收到的 request。"""
    payload = PAGE.encode("utf-8") if body is None else body

    def _send(request):
        if recorder is not None:
            recorder.append(request)
        hdrs = {"content-type": "text/html; charset=utf-8"}
        if headers:
            hdrs.update(headers)
        return {"status": status, "headers": hdrs, "body": payload}

    return _send


class ScriptedSend:
    """按 URL 脚本返回 (status, headers, body)，并记录收到的 request。"""

    def __init__(self, script):
        self.script = script
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        status, headers, body = self.script[request["url"]]
        return {"status": status, "headers": headers, "body": body}


class FakeClock:
    """可前进的假单调时钟：确定性地构造"死线在两次跳之间到期"。"""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _transport(**kw):
    """全注入的 SafeTransport：resolver/send 默认都是假件（保证零网络）。"""
    resolver = kw.pop("resolver", static_resolver("1.1.1.1"))
    send = kw.pop("send", html_send())
    return SafeTransport(dns_resolver=resolver, send=send, **kw)


class TestURLSyntax(unittest.TestCase):
    """URL 语法校验（契约 §2.2.1）：坏语法一律 bad_url，且不发起任何 IO。"""

    def test_bad_scheme_credentials_and_empty_host_rejected(self):
        """锁定：非 http(s)/用户信息/空主机/含空格/端口越界 → bad_url，final_url 为空。"""
        transport = _transport()
        for bad in ("ftp://example.com/x", "http://user:pass@example.com/",
                    "http:///path", "not-a-url", "", "   ",
                    "http://exa mple.com/", "https://example.com:99999/"):
            result = transport.fetch(bad)
            self.assertEqual(result["status"], "error", bad)
            self.assertEqual(result["category"], "bad_url", bad)
            self.assertEqual(result["final_url"], "", bad)

    def test_local_suffixes_and_ip_literals_rejected_or_pinned(self):
        """锁定：localhost/.local/.internal 与非公网 IP 字面量拒绝；公网 IP 字面量
        不查 DNS 直接钉扎（IPv6 的 Host 头带方括号）。"""
        transport = _transport(resolver=raising_resolver)
        for bad in ("http://localhost/", "http://foo.localhost/", "http://svc.local/",
                    "http://db.internal/", "http://127.0.0.1/", "http://10.0.0.1/",
                    "http://192.168.1.9/", "http://[::1]/", "http://0.0.0.0/"):
            result = transport.fetch(bad)
            self.assertEqual(result["category"], "bad_url", bad)
        sent = []
        ok = _transport(resolver=raising_resolver,
                        send=html_send(recorder=sent)).fetch("http://8.8.8.8/x")
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["hops"][0]["ip"], "8.8.8.8")
        self.assertEqual(sent[0]["pinned_ip"], "8.8.8.8")
        sent6 = []
        ok6 = _transport(resolver=raising_resolver,
                         send=html_send(recorder=sent6)).fetch("https://[2606:4700::6810:4411]/")
        self.assertEqual(ok6["status"], "ok")
        self.assertEqual(ok6["hops"][0]["ip"], "2606:4700::6810:4411")
        self.assertEqual(sent6[0]["headers"]["Host"], "[2606:4700::6810:4411]")


class TestDnsPinning(unittest.TestCase):
    """DNS 钉扎与混合判定（契约 §2.2.2）。"""

    def test_private_resolution_rejected(self):
        """锁定：任一私网/环回地址 → dns_private，detail 里带地址，连接未发起。"""
        for ips in (("10.0.0.1",), ("127.0.0.1", "10.1.2.3"), ("169.254.1.1",)):
            result = _transport(resolver=static_resolver(*ips)).fetch("https://www.example.com/")
            self.assertEqual(result["category"], "dns_private", ips)
            self.assertIn(ips[0], result["detail"])

    def test_mixed_public_private_resolution_rejected(self):
        """锁定：公私混杂解析直接失败（dns_mixed），全公网才放行。"""
        transport = _transport(resolver=static_resolver("93.184.216.34", "10.0.0.1"))
        self.assertEqual(transport.fetch("https://www.example.com/")["category"], "dns_mixed")
        dedup = _transport(resolver=static_resolver("8.8.8.8", "10.0.0.1", "10.0.0.1"))
        self.assertEqual(dedup.fetch("https://www.example.com/")["category"], "dns_mixed")
        clean = _transport(resolver=static_resolver("8.8.8.8", "1.1.1.1"))
        self.assertEqual(clean.fetch("https://www.example.com/")["status"], "ok")

    def test_resolver_empty_or_raising_rejected(self):
        """锁定：解析失败/空结果/返回非列表 → dns_failed。"""
        for resolver in (lambda host: [], raising_resolver, lambda host: "1.1.1.1"):
            result = _transport(resolver=resolver).fetch("https://www.example.com/")
            self.assertEqual(result["category"], "dns_failed")

    def test_first_public_ip_pinned_into_request(self):
        """锁定：多个公网 IP 取第一个钉扎，Host 头写原主机名，输出域名去 www。"""
        sent = []
        transport = _transport(resolver=static_resolver("1.1.1.1", "8.8.8.8"),
                               send=html_send(recorder=sent))
        result = transport.fetch("https://www.example.com/")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(sent[0]["pinned_ip"], "1.1.1.1")
        self.assertEqual(sent[0]["hostname"], "www.example.com")
        self.assertEqual(sent[0]["headers"]["Host"], "www.example.com")
        self.assertEqual(result["domain"], "example.com")

    def test_io_timeout_is_min_of_timeout_and_deadline(self):
        """锁定：单次 IO 超时 = timeout_s 与死线剩余取小。"""
        sent = []
        transport = _transport(timeout_s=10.0, send=html_send(recorder=sent))
        # Windows 单调时钟粒度约 15.6ms：死线取 0.5s（远小于 timeout_s）足以证明取了小
        transport.fetch("https://www.example.com/", deadline=time.monotonic() + 0.5)
        self.assertLess(sent[0]["timeout"], 10.0)
        self.assertLessEqual(sent[0]["timeout"], 0.5)
        transport.fetch("https://www.example.com/", deadline=time.monotonic() + 30.0)
        self.assertEqual(sent[1]["timeout"], 10.0)


class TestRedirects(unittest.TestCase):
    """逐跳重定向复核（契约 §2.2.3）。"""

    def test_follows_redirect_revalidating_each_hop(self):
        """锁定：手工跟随 3xx，每跳重新解析 DNS 并重新钉扎，hops 含首跳。"""
        calls = []
        resolver = mapped_resolver({"a.example.com": ["1.1.1.1"],
                                    "b.example.org": ["2.2.2.2"]}, calls)
        send = ScriptedSend({
            "https://a.example.com/": (302, {"location": "https://b.example.org/page"}, b""),
            "https://b.example.org/page": (200, {"content-type": "text/html"},
                                           PAGE.encode("utf-8")),
        })
        result = _transport(resolver=resolver, send=send).fetch("https://a.example.com/")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["final_url"], "https://b.example.org/page")
        self.assertEqual([hop["ip"] for hop in result["hops"]], ["1.1.1.1", "2.2.2.2"])
        self.assertEqual(calls, ["a.example.com", "b.example.org"])
        self.assertEqual(send.requests[1]["pinned_ip"], "2.2.2.2")
        self.assertEqual(send.requests[1]["headers"]["Host"], "b.example.org")
        self.assertEqual(result["domain"], "b.example.org")

    def test_missing_or_non_http_location_rejected(self):
        """锁定：Location 缺失/空/非 http(s) → redirect_invalid，且不再发下一跳。"""
        for headers in ({}, {"location": ""}, {"location": "javascript:alert(1)"},
                        {"location": "mailto:a@b.c"}, {"location": "ftp://x.example/"}):
            send = ScriptedSend({"https://a.example.com/": (302, headers, b"")})
            result = _transport(send=send).fetch("https://a.example.com/")
            self.assertEqual(result["category"], "redirect_invalid", headers)
            self.assertEqual(len(send.requests), 1)
            self.assertEqual(result["final_url"], "https://a.example.com/")

    def test_too_many_hops_stops_following(self):
        """锁定：max_hops 含首跳；超过上限 → too_many_hops 且请求数恰好 max_hops。"""
        urls = [f"https://h{i}.example.com/" for i in range(4)]
        script = {url: (302, {"location": urls[i + 1]}, b"") if i < 3
                  else (200, {"content-type": "text/html"}, PAGE.encode("utf-8"))
                  for i, url in enumerate(urls)}
        send = ScriptedSend(script)
        resolver = mapped_resolver({url.split("/")[2]: ["1.1.1.1"] for url in urls})
        result = _transport(max_hops=3, resolver=resolver,
                            send=send).fetch(urls[0])
        self.assertEqual(result["category"], "too_many_hops")
        self.assertEqual(len(send.requests), 3)
        self.assertEqual(result["final_url"], urls[2])

    def test_redirect_to_private_host_rejected(self):
        """锁定：重定向到解析出私网地址的主机 → dns_private，那一跳连接未发起。"""
        send = ScriptedSend({
            "https://ok.example.com/": (302, {"location": "https://sneaky.example.net/"}, b""),
            "https://sneaky.example.net/": (200, {"content-type": "text/html"},
                                            PAGE.encode("utf-8")),
        })
        resolver = mapped_resolver({"ok.example.com": ["1.1.1.1"],
                                    "sneaky.example.net": ["10.1.2.3"]})
        result = _transport(resolver=resolver, send=send).fetch("https://ok.example.com/")
        self.assertEqual(result["category"], "dns_private")
        self.assertEqual(len(send.requests), 1)
        self.assertEqual(result["final_url"], "https://ok.example.com/")

    def test_redirect_to_disallowed_domain_rejected(self):
        """锁定：重定向跨出域名允许集 → domain_not_allowed（每跳都查允许集）。"""
        send = ScriptedSend({
            "https://good.example.com/": (302, {"location": "https://evil.example.net/x"}, b""),
        })
        resolver = mapped_resolver({"good.example.com": ["1.1.1.1"],
                                    "evil.example.net": ["2.2.2.2"]})
        result = _transport(resolver=resolver, send=send).fetch(
            "https://good.example.com/", allowed_domains=["example.com"])
        self.assertEqual(result["category"], "domain_not_allowed")
        self.assertEqual(len(send.requests), 1)


class TestDomainAllowlist(unittest.TestCase):
    """域名允许集（契约 §2.2.4）。"""

    def test_allowed_domains_subdomain_and_unrestricted(self):
        """锁定：子域命中允许域；不同域/相像域拒绝；空集 = 不限制。"""
        transport = _transport()
        ok = transport.fetch("https://www.who.int/", allowed_domains=["who.int"])
        self.assertEqual(ok["status"], "ok")
        for bad in ("https://not-who.example/", "https://who.int.evil.example/"):
            denied = transport.fetch(bad, allowed_domains=["who.int"])
            self.assertEqual(denied["category"], "domain_not_allowed", bad)
        self.assertEqual(transport.fetch("https://anything.example/",
                                         allowed_domains=None)["status"], "ok")
        self.assertEqual(transport.fetch("https://anything.example/",
                                         allowed_domains=[])["status"], "ok")


class TestSizeAndType(unittest.TestCase):
    """体积上限与内容类型白名单（契约 §2.2.5 / §2.2.6）。"""

    def test_oversize_body_rejected_raw_and_decompressed(self):
        """锁定：响应体超限 → too_large；解压后超限同样 too_large（防压缩炸弹）。"""
        big = b"x" * 1001
        small = _transport(max_bytes=1000, send=html_send(body=big))
        self.assertEqual(small.fetch("https://a.example/")["category"], "too_large")
        bomb = gzip.compress(b"A" * 5000)
        self.assertLess(len(bomb), 1000)     # 压缩后很小，绕过不了解压后检查
        gz = _transport(max_bytes=1000,
                        send=html_send(body=bomb, headers={"content-encoding": "gzip"}))
        self.assertEqual(gz.fetch("https://a.example/")["category"], "too_large")

    def test_gzip_and_deflate_bodies_decoded_within_limit(self):
        """锁定：gzip / deflate（zlib 包装与裸流）都正确解压后提取正文。"""
        page = PAGE.encode("utf-8")
        raw_deflate = zlib.compress(page)
        raw_stream = (lambda c: c.compress(page) + c.flush())(
            zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS))
        for enc, body in (("gzip", gzip.compress(page)), ("deflate", raw_deflate),
                          ("deflate", raw_stream)):
            transport = _transport(send=html_send(body=body,
                                                  headers={"content-encoding": enc}))
            result = transport.fetch("https://a.example/")
            self.assertEqual(result["status"], "ok", enc)
            self.assertIn("这是正文第一段", result["text"], enc)

    def test_pdf_and_other_content_types_rejected(self):
        """锁定：PDF 等白名单外类型 → unsupported_type（不当正文）；缺声明也拒绝。"""
        for ctype in ("application/pdf", "image/png", "application/json"):
            transport = _transport(send=html_send(body=b"%PDF-1.4 fake",
                                                  headers={"content-type": ctype}))
            result = transport.fetch("https://a.example/")
            self.assertEqual(result["category"], "unsupported_type", ctype)
            self.assertIn(ctype, result["detail"])
        missing = _transport(send=html_send(body=b"%PDF-1.4 fake",
                                            headers={"content-type": ""}))
        blank = missing.fetch("https://a.example/")
        self.assertEqual(blank["category"], "unsupported_type")
        self.assertIn("Content-Type", blank["detail"])
        plain = _transport(send=html_send(body="纯文本正文，长度足够通过提取。".encode("utf-8"),
                                          headers={"content-type": "text/plain"}))
        self.assertEqual(plain.fetch("https://a.example/")["status"], "ok")


class TestDeadlineAndCancel(unittest.TestCase):
    """时限与取消（契约 §2.2.7 / §2.2.8）。"""

    def test_past_deadline_stops_before_io(self):
        """锁定：已过死线直接失败，不发连接。"""
        sent = []
        result = _transport(send=html_send(recorder=sent)).fetch(
            "https://a.example/", deadline=time.monotonic() - 1.0)
        self.assertEqual(result["category"], "deadline")
        self.assertEqual(sent, [])

    def test_socket_timeout_reported_as_timeout(self):
        """锁定：socket 超时 → timeout；其他连接层异常 → network 且不外泄。"""
        def _slow(request):
            raise socket.timeout("timed out")

        result = _transport(send=_slow).fetch("https://a.example/")
        self.assertEqual(result["category"], "timeout")

        def _boom(request):
            raise RuntimeError("connection reset by peer")

        broken = _transport(send=_boom).fetch("https://a.example/")
        self.assertEqual(broken["category"], "network")
        self.assertIn("RuntimeError", broken["detail"])

    def test_cancel_check_stops_before_io(self):
        """锁定：取消命中 → cancelled 且不发连接；取消探针出错按未取消处理。"""
        sent = []
        result = _transport(send=html_send(recorder=sent)).fetch(
            "https://a.example/", cancel_check=lambda: True)
        self.assertEqual(result["category"], "cancelled")
        self.assertEqual(sent, [])

        def _broken_probe():
            raise ValueError("probe broke")

        ok = _transport(send=html_send()).fetch("https://a.example/",
                                                cancel_check=_broken_probe)
        self.assertEqual(ok["status"], "ok")

    def test_deadline_expires_between_hops(self):
        """锁定：死线在两次跳之间到期 → deadline，不再发起下一跳连接。"""
        clock = FakeClock()
        deadline = clock() + 5.0

        def _send(request):
            clock.advance(100.0)
            return {"status": 302, "headers": {"location": "https://b.example/"}, "body": b""}

        transport = _transport(send=_send)
        with mock.patch.object(transport_mod, "_now", clock):
            result = transport.fetch("https://a.example/", deadline=deadline)
        self.assertEqual(result["category"], "deadline")
        self.assertEqual(result["final_url"], "https://a.example/")


class TestConcurrency(unittest.TestCase):
    """并发槽位（契约 §2.2.9）。"""

    def test_slot_limits_inflight_and_releases_after_failure(self):
        """锁定：BoundedSemaphore 把同时在飞请求压到 max_concurrency；失败的 fetch
        也释放槽位（否则后续请求会卡死到死线）。"""
        inflight = {"now": 0, "max": 0}
        guard = threading.Lock()
        mode = {"fail": False}
        page = PAGE.encode("utf-8")

        def _send(request):
            if mode["fail"]:
                raise RuntimeError("connection reset")
            with guard:
                inflight["now"] += 1
                inflight["max"] = max(inflight["max"], inflight["now"])
            time.sleep(0.03)
            with guard:
                inflight["now"] -= 1
            return {"status": 200, "headers": {"content-type": "text/html"}, "body": page}

        transport = _transport(max_concurrency=1, send=_send)
        results = []

        def _run(index):
            results.append(transport.fetch(f"https://h{index}.example.com/"))

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10.0)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(item["status"] == "ok" for item in results))
        self.assertLessEqual(inflight["max"], 1)

        mode["fail"] = True
        failed = transport.fetch("https://h9.example.com/",
                                 deadline=time.monotonic() + 5.0)
        self.assertEqual(failed["category"], "network")
        mode["fail"] = False
        after = transport.fetch("https://h8.example.com/",
                                deadline=time.monotonic() + 5.0)
        self.assertEqual(after["status"], "ok")

    def test_deadline_while_waiting_for_slot(self):
        """锁定：等并发槽位等到死线 → deadline（不无限阻塞）。"""
        gate = threading.Event()
        entered = threading.Event()
        page = PAGE.encode("utf-8")

        def _blocking_send(request):
            entered.set()
            gate.wait(10.0)
            return {"status": 200, "headers": {"content-type": "text/html"}, "body": page}

        transport = _transport(max_concurrency=1, send=_blocking_send)
        holder_result = []

        def _hold():
            holder_result.append(transport.fetch("https://hold.example.com/"))

        holder = threading.Thread(target=_hold)
        holder.start()
        self.assertTrue(entered.wait(5.0), "持槽位的请求应已进入 send")

        waiter_result = []

        def _wait():
            waiter_result.append(transport.fetch("https://wait.example.com/",
                                                 deadline=time.monotonic() + 0.1))

        waiter = threading.Thread(target=_wait)
        waiter.start()
        waiter.join(10.0)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(waiter_result[0]["category"], "deadline")
        gate.set()
        holder.join(10.0)
        self.assertEqual(holder_result[0]["status"], "ok")


class TestBodyExtraction(unittest.TestCase):
    """正文提取（契约 §2.2.11）。"""

    def test_extracts_title_and_text_without_script(self):
        """锁定：title 单独提取；script/style/noscript 内容不进正文；标题不混进正文。"""
        result = _transport(send=html_send()).fetch("https://a.example/")
        self.assertEqual(result["title"], "测试页面标题")
        self.assertIn("这是正文第一段，包含足够长度的文字用于提取。", result["text"])
        self.assertNotIn("secret", result["text"])
        self.assertNotIn("color:red", result["text"])
        self.assertNotIn("请开启脚本", result["text"])
        self.assertNotIn("测试页面标题", result["text"])

    def test_empty_text_is_ok_with_empty_string(self):
        """锁定：提取不到正文 → 成功但 text=""（由调用方决定怎么处理）。"""
        body = b"<html><head><title></title></head><body><p>ok</p><nav>home</nav></body></html>"
        result = _transport(send=html_send(body=body)).fetch("https://a.example/")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["bytes"], 0)
        self.assertEqual(result["title"], "")

    def test_text_truncated_to_max_text_chars(self):
        """锁定：正文截断到 max_text_chars。"""
        long_page = ("<html><body><p>" + "长文本" * 500 + "</p></body></html>").encode("utf-8")
        transport = _transport(max_text_chars=50, send=html_send(body=long_page))
        result = transport.fetch("https://a.example/")
        self.assertEqual(len(result["text"]), 50)


class TestSendContract(unittest.TestCase):
    """send 契约（契约 §2.2.10）。"""

    def test_send_request_contract_keys_and_host_header(self):
        """锁定：request 含 url/hostname/pinned_ip/port/scheme/headers/timeout；
        Host 头写原主机名，显式端口时带端口。"""
        sent = []
        _transport(send=html_send(recorder=sent)).fetch("https://www.example.com/a?b=1")
        request = sent[0]
        for key in ("url", "hostname", "pinned_ip", "port", "scheme", "headers", "timeout"):
            self.assertIn(key, request)
        self.assertEqual(request["url"], "https://www.example.com/a?b=1")
        self.assertEqual(request["hostname"], "www.example.com")
        self.assertEqual(request["scheme"], "https")
        self.assertEqual(request["port"], 443)
        self.assertEqual(request["pinned_ip"], "1.1.1.1")
        self.assertEqual(request["headers"]["Host"], "www.example.com")
        self.assertEqual(request["headers"]["User-Agent"], transport_mod._USER_AGENT)

        sent2 = []
        _transport(send=html_send(recorder=sent2)).fetch("https://www.example.com:8443/")
        self.assertEqual(sent2[0]["port"], 8443)
        self.assertEqual(sent2[0]["headers"]["Host"], "www.example.com:8443")


class TestResultShape(unittest.TestCase):
    """统一返回形状（契约 §2.1）。"""

    def test_success_and_error_dict_shapes(self):
        """锁定：成功/失败 dict 的键集合、category 属封闭集合、非字符串 url 不炸。"""
        ok = _transport(send=html_send()).fetch("https://www.example.com/")
        self.assertEqual(set(ok), {"status", "url", "final_url", "domain", "title", "text",
                                   "bytes", "content_type", "hops", "fetched_at"})
        self.assertEqual(ok["url"], "https://www.example.com/")
        self.assertEqual(ok["content_type"], "text/html")
        self.assertEqual(ok["bytes"], len(ok["text"].encode("utf-8")))
        self.assertEqual(set(ok["hops"][0]), {"url", "domain", "ip"})
        datetime.fromisoformat(ok["fetched_at"])

        bad = _transport().fetch("ftp://a.example/")
        self.assertEqual(set(bad), {"status", "category", "detail", "url", "final_url"})
        self.assertIn(bad["category"], _CATEGORIES)
        self.assertIsInstance(bad["detail"], str)
        for weird in (None, 123, "", "   "):
            result = _transport().fetch(weird)
            self.assertEqual(result["status"], "error", weird)
            self.assertEqual(result["category"], "bad_url", weird)


class TestModuleHelpers(unittest.TestCase):
    """模块级辅助（契约 §2.3）。"""

    def test_is_public_ip_matrix(self):
        """锁定：公网/私网/环回/链路本地/保留/组播/未指定/映射地址/非法输入的判定矩阵。"""
        for ip in ("8.8.8.8", "1.1.1.1", "93.184.216.34",
                   "2606:4700::6810:4411", "2001:4860:4860::8888"):
            self.assertTrue(is_public_ip(ip), ip)
        for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.1.1",
                   "0.0.0.0", "224.0.0.1", "255.255.255.255", "::1", "fe80::1",
                   "fc00::1", "2001:db8::1", "::ffff:10.0.0.1", "8.8.8.256",
                   "not-an-ip", ""):
            self.assertFalse(is_public_ip(ip), ip)
        self.assertFalse(is_public_ip(None))

    def test_strip_query_operators(self):
        """锁定：site:/inurl:/intitle:/filetype: 等操作符被剥掉（含负号与紧贴中文），
        无操作符查询原样返回。"""
        self.assertEqual(strip_query_operators("新冠疫苗 site:who.int 最新"), "新冠疫苗 最新")
        self.assertEqual(strip_query_operators("site:cdc.gov flu inurl:gov"), "flu")
        self.assertEqual(strip_query_operators('filetype:pdf intitle:2024 "年报数据"'),
                         '"年报数据"')
        self.assertEqual(strip_query_operators("-site:spam.com 疫苗"), "疫苗")
        self.assertEqual(strip_query_operators("新冠疫苗site:who.int 报道"), "新冠疫苗 报道")
        self.assertEqual(strip_query_operators("普通查询 没有操作符"), "普通查询 没有操作符")
        self.assertEqual(strip_query_operators("mysite:example 网站"), "mysite:example 网站")
        self.assertEqual(strip_query_operators("parasite:control 措施"), "parasite:control 措施")
        self.assertEqual(strip_query_operators(""), "")
        self.assertEqual(strip_query_operators(None), "")


class TestProductionDefaults(unittest.TestCase):
    """生产默认件（契约 §2.3：default_transport + http.client 钉竖连接）。"""

    def test_default_transport_returns_production_singleton(self):
        """锁定：无参 = 进程内共享单例；带参 = 新实例；resolver/send 都接生产实现。"""
        first = default_transport()
        self.assertIsInstance(first, SafeTransport)
        self.assertIs(default_transport(), first)
        custom = default_transport(max_hops=2)
        self.assertIsInstance(custom, SafeTransport)
        self.assertEqual(custom.max_hops, 2)
        self.assertIsNot(custom, first)
        for transport in (first, custom):
            self.assertIs(transport._resolver, transport_mod._resolve_host)
            self.assertIs(transport._send, transport_mod._send_request)

    def test_pinned_connections_use_ip_with_original_hostname(self):
        """锁定：socket 连 pinned_ip（不再查 DNS），TLS 按原主机名校验 server_hostname；
        明文 HTTP 同样钉扎。"""
        created = {}

        def _fake_create_connection(address, timeout=None, source_address=None):
            created["address"] = address
            created["timeout"] = timeout
            return SimpleNamespace()

        class _FakeContext:
            def wrap_socket(self, sock, server_hostname=None):
                created["server_hostname"] = server_hostname
                return sock

        https = transport_mod._PinnedHTTPSConnection("www.example.com", "1.1.1.1", 443, 5.0)
        https._context = _FakeContext()
        with mock.patch("socket.create_connection", _fake_create_connection):
            https.connect()
        self.assertEqual(created["address"], ("1.1.1.1", 443))
        self.assertEqual(created["timeout"], 5.0)
        self.assertEqual(created["server_hostname"], "www.example.com")

        http = transport_mod._PinnedHTTPConnection("www.example.com", "1.1.1.1", 80, 5.0)
        with mock.patch("socket.create_connection", _fake_create_connection):
            http.connect()
        self.assertEqual(created["address"], ("1.1.1.1", 80))


if __name__ == "__main__":
    unittest.main(verbosity=2)
