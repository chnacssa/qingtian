"""url_guard 单元测试（2026-08-28 SSRF 修复配套）

覆盖：
- 同步快校验：scheme 白名单、显式 IP（v4/v6）私网/环回/保留拦截、域名放行
- async 校验：域名解析到私网被拒（用真实解析：localhost 不走 DNS，
  用显式私网 IP 直测；DNS 解析失败路径 mock）
"""
import asyncio
import ipaddress

import pytest

from common.url_guard import check_external_url, check_external_url_async, _ip_allowed


class TestSyncCheck:
    def test_https_public_ok(self):
        ok, reason = check_external_url("https://example.com/a?b=1")
        assert ok, reason

    def test_http_public_ok(self):
        assert check_external_url("http://example.com")[0]

    def test_scheme_ftp_rejected(self):
        ok, reason = check_external_url("ftp://example.com")
        assert not ok and "协议" in reason

    def test_scheme_file_rejected(self):
        ok, _ = check_external_url("file:///etc/passwd")
        assert not ok

    def test_empty_rejected(self):
        assert not check_external_url("")[0]
        assert not check_external_url(None)[0]

    def test_no_host_rejected(self):
        ok, _ = check_external_url("http://")
        assert not ok

    def test_loopback_v4_rejected(self):
        ok, reason = check_external_url("http://127.0.0.1:1996/v1")
        assert not ok and "127.0.0.1" in reason

    def test_private_v4_rejected(self):
        for ip in ("10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.1.1", "0.0.0.0"):
            ok, _ = check_external_url(f"http://{ip}/x")
            assert not ok, ip

    def test_metadata_ip_rejected(self):
        # 云厂商 metadata 端点（169.254.169.254 属链路本地）
        ok, _ = check_external_url("http://169.254.169.254/latest/meta-data/")
        assert not ok

    def test_loopback_v6_rejected(self):
        ok, _ = check_external_url("http://[::1]:1996/x")
        assert not ok

    def test_private_v6_rejected(self):
        ok, _ = check_external_url("http://[fe80::1]/x")
        assert not ok
        ok, _ = check_external_url("http://[fc00::1]/x")
        assert not ok

    def test_domain_passthrough(self):
        # 域名不做 DNS 解析（同步版），放行交给 async 复验
        ok, _ = check_external_url("https://internal.corp.local/x")
        assert ok

    def test_ip_allowed_unit(self):
        assert _ip_allowed(ipaddress.ip_address("8.8.8.8"))
        assert not _ip_allowed(ipaddress.ip_address("10.1.2.3"))
        assert not _ip_allowed(ipaddress.ip_address("::1"))


class TestAsyncCheck:
    def test_explicit_private_rejected(self):
        ok, reason = asyncio.run(check_external_url_async("http://192.168.0.1/x"))
        assert not ok

    def test_dns_failure_rejected(self):
        # 不存在的域名 → 解析失败 → 拒绝
        ok, reason = asyncio.run(
            check_external_url_async("http://nonexistent-8f3a1c.example.invalid/"))
        assert not ok and "解析" in reason

    def test_public_domain_ok(self):
        ok, reason = asyncio.run(check_external_url_async("https://example.com/"))
        assert ok, reason

    def test_scheme_rejected_before_dns(self):
        ok, _ = asyncio.run(check_external_url_async("ftp://nonexistent.example.invalid/"))
        assert not ok
