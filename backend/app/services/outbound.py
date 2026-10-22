"""Requests to URLs that users configure (integrations, alert channels, eval runners). The host must resolve only to
public addresses, and the request goes to the address that was checked, so a second DNS answer (rebinding) is never
used. Redirects are not followed and proxies from the environment are not used (a proxy would resolve again). A reply
is read up to MAX_REPLY_BYTES as sent (never inflated), and the whole call, lookup included, has one deadline."""
import asyncio
import ipaddress
import socket

import httpx

RESOLVE_TIMEOUT_S = 5.0
TIMEOUT_S = 8.0
MAX_REPLY_BYTES = 64 * 1024
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_transport: httpx.AsyncBaseTransport | None = None  # tests swap in a mock transport


class BlockedURL(ValueError):
    pass


class ReplyTooLarge(ValueError):
    pass


def is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6:
        if ip.is_site_local:
            return False
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in _NAT64 or int(ip) >> 32 == 0:  # NAT64 and IPv4-compatible reach the IPv4 inside
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _pin(url: str) -> tuple[httpx.URL, httpx.URL]:
    """(the URL, the same URL with its host replaced by a checked address)."""
    try:
        u = httpx.URL(url)
    except Exception:
        raise BlockedURL("invalid_url")
    if u.scheme not in ("http", "https") or not u.host:
        raise BlockedURL("invalid_url")
    try:
        addrs = await asyncio.wait_for(_resolve(u.host, u.port or (443 if u.scheme == "https" else 80)),
                                       RESOLVE_TIMEOUT_S)
        ips = [ipaddress.ip_address(a.split("%")[0]) for a in addrs]
    except (OSError, ValueError, asyncio.TimeoutError):
        raise BlockedURL("unresolvable_host")
    if not ips or not all(is_public(ip) for ip in ips):
        raise BlockedURL("blocked_address")
    return u, u.copy_with(host=str(ips[0]))


async def post(url: str, *, json, timeout: float | None = None) -> httpx.Response:
    """POST `json` to `url` within `timeout` seconds (TIMEOUT_S by default). BlockedURL if the URL is not http(s) or its
    host is not public, ReplyTooLarge past MAX_REPLY_BYTES, httpx.TimeoutException past the deadline."""
    timeout = timeout or TIMEOUT_S
    try:
        async with asyncio.timeout(timeout):
            return await _post(url, json, timeout)
    except TimeoutError:
        raise httpx.TimeoutException("timeout")


async def _post(url: str, json, timeout: float) -> httpx.Response:
    u, pinned = await _pin(url)
    extensions = {"sni_hostname": u.raw_host.decode("ascii")} if u.scheme == "https" else {}
    headers = {"Host": u.netloc.decode("ascii"), "Accept-Encoding": "identity"}
    async with httpx.AsyncClient(timeout=timeout, transport=_transport, trust_env=False) as c:
        async with c.stream("POST", pinned, json=json, headers=headers, extensions=extensions) as r:
            body = bytearray()
            async for chunk in r.stream:  # raw bytes, as sent
                body += chunk
                if len(body) > MAX_REPLY_BYTES:
                    raise ReplyTooLarge("reply_too_large")
    return httpx.Response(r.status_code, headers=[h for h in r.headers.multi_items() if h[0] != "content-encoding"],
                          content=bytes(body), request=r.request)
