"""SSRF 防护 URL 校验（2026-08-28 开源前 review 修复）

背景：吸星 SSRF 读原语链（urlmd 零校验入库→collect 触发→export 外泄）、
/v1/process callback_url 任意 POST、执策 daemon api_health urlopen 任意 URL。

用法：
- check_external_url(url)：同步快速校验（scheme 白名单 + 显式 IP 私网拦截，
  不做 DNS 解析——适合同步上下文/高频路径）
- await check_external_url_async(url)：含 DNS 解析后 IP 复验（防域名指向内网），
  适合 async 入库校验等低频路径

已知限制（记录不阻断）：
- DNS rebinding TOCTOU：解析时公网、请求时内网——防住需 pin IP 请求，成本高，
  本轮不做，残留风险记录在 review 报告
- 302 重定向到内网：httpx follow_redirects 逐跳校验未实现，采集侧首跳已拦，
  重定向残留 P2
"""
import ipaddress
import logging
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = ("http", "https")


def _ip_allowed(ip: ipaddress._BaseAddress) -> bool:
    """公网地址放行；私网/环回/链路本地/保留/组播/未指定一律拒。"""
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _check_sync_core(url: str) -> tuple[bool, str]:
    if not url or not isinstance(url, str):
        return False, "URL 为空"
    try:
        parts = urlsplit(url.strip())
    except ValueError as e:
        return False, f"URL 解析失败: {e}"
    if parts.scheme not in _ALLOWED_SCHEMES:
        return False, f"协议不允许: {parts.scheme or '(空)'}（仅 http/https）"
    host = parts.hostname or ""
    if not host:
        return False, "缺少主机名"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True, ""          # 域名：同步版不解析，async 版复验
    if not _ip_allowed(ip):
        return False, f"内网/保留地址被拒绝: {ip}"
    return True, ""


def check_external_url(url: str) -> tuple[bool, str]:
    """同步快速校验：scheme 白名单 + 显式 IP（含 IPv6）私网拦截。"""
    return _check_sync_core(url)


async def check_external_url_async(url: str) -> tuple[bool, str]:
    """完整校验：同步规则 + DNS 解析后 IP 复验（域名指向内网也拒）。"""
    ok, reason = _check_sync_core(url)
    if not ok:
        return ok, reason
    host = urlsplit(url.strip()).hostname or ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return True, ""           # 已是显式 IP，同步层判过
    import asyncio
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError as e:
        return False, f"域名解析失败: {host} ({e})"
    for info in infos:
        addr = info[4][0]
        # IPv6 带 scope 的形式（fe80::1%eth0）去掉 scope 再判
        try:
            aip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if not _ip_allowed(aip):
            return False, f"域名解析到内网/保留地址: {host} → {aip}"
    return True, ""
