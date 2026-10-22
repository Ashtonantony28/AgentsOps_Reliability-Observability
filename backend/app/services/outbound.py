"""Requests to URLs that users configure (integrations, alert channels, eval runners). The host must resolve only to
public addresses, and the request goes to the address that was checked, so a second DNS answer (rebinding) is never
used. Redirects are not followed and proxies from the environment are not used (a proxy would resolve again)."""
import asyncio
import ipaddress
import socket

import httpx

RESOLVE_TIMEOUT_S = 5.0
_transport: httpx.AsyncBaseTransport | None = None  # tests swap in a mock transport


class BlockedURL(ValueError):
    pass


def is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
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


async def post(url: str, *, json, timeout: float = 8.0) -> httpx.Response:
    """POST `json` to `url`; BlockedURL if the URL is not http(s) or its host is not public."""
    u, pinned = await _pin(url)
    extensions = {"sni_hostname": u.raw_host.decode("ascii")} if u.scheme == "https" else {}
    async with httpx.AsyncClient(timeout=timeout, transport=_transport, trust_env=False) as c:
        return await c.post(pinned, json=json, headers={"Host": u.netloc.decode("ascii")}, extensions=extensions)
