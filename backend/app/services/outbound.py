"""Requests to URLs that users configure (integrations, alert channels, eval runners). The host must resolve only to
public addresses, and the request goes to the address that was checked, so a second DNS answer (rebinding) is never
used. Redirects are not followed and proxies from the environment are not used (a proxy would resolve again). A reply
is read up to MAX_REPLY_BYTES as sent (never inflated), and the whole call, lookup included, has one deadline.
Lookups run on their own executor, never the default one: the deadline stops the wait, not the thread, so each lookup
gets a thread of its own and an org has few lookups running at once."""
import asyncio
import ipaddress
import socket
import threading
from collections import Counter, defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar

import httpx

RESOLVE_TIMEOUT_S = 5.0
TIMEOUT_S = 8.0
MAX_REPLY_BYTES = 64 * 1024
RESOLVE_QUEUE = 64  # lookups running, abandoned ones until their thread ends; past it unresolvable_host
RESOLVE_THREADS = RESOLVE_QUEUE  # a thread for each, so no lookup waits behind one held past its deadline
ORG_LOOKUPS = 2  # an org's lookups running, each until its thread ends; more wait for one within the lookup's deadline
ORG_CALLS = 10  # an org's calls in flight (a rule's channels fit), each until its lookup's thread ends too; more wait
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_transport: httpx.AsyncBaseTransport | None = None  # tests swap in a mock transport
_resolver = ThreadPoolExecutor(RESOLVE_THREADS, thread_name_prefix="outbound-resolve")
_lock = threading.Lock()  # lookups end on resolver threads
_lookups = 0
org: ContextVar[str | None] = ContextVar("outbound_org", default=None)  # set by callers in the API process
_call: ContextVar["_Call | None"] = ContextVar("outbound_call", default=None)


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


class _Places:
    """Places counted per org. Past the bound a taker waits in turn for a place given back, which is handed over
    through a concurrent future, so a resolver thread can give it."""

    def __init__(self):
        self.held: Counter[str] = Counter()
        self.waiting: defaultdict[str, deque[Future]] = defaultdict(deque)

    async def take(self, org_id: str, bound: int) -> None:
        with _lock:
            if self.held[org_id] < bound:
                self.held[org_id] += 1
                return
            turn = Future()
            self.waiting[org_id].append(turn)
        try:
            await asyncio.wrap_future(turn)
        except asyncio.CancelledError:
            with _lock:
                if not turn.cancel():  # handed a place as it was cancelled
                    self.give(org_id)
                elif turn in self.waiting[org_id]:
                    self.waiting[org_id].remove(turn)
                if not self.waiting[org_id]:
                    del self.waiting[org_id]
            raise

    def give(self, org_id: str) -> None:
        """Called with _lock held."""
        queue = self.waiting.get(org_id)
        while queue:
            turn = queue.popleft()
            if turn.set_running_or_notify_cancel():
                turn.set_result(None)
                break
        else:
            self.held[org_id] -= 1
            if not self.held[org_id]:
                del self.held[org_id]
        if queue is not None and not queue:
            del self.waiting[org_id]


_calls, _org_lookups = _Places(), _Places()
_in_flight = _calls.held


class _Call:
    """An org's place among its calls in flight, given back once the call and any lookup it started have ended."""

    def __init__(self, org_id: str):
        self.org_id, self.refs = org_id, 0

    async def start(self) -> None:
        await _calls.take(self.org_id, ORG_CALLS)
        self.refs = 1

    def release(self) -> None:
        with _lock:
            self.refs -= 1
            if not self.refs:
                _calls.give(self.org_id)


async def _resolve(host: str, port: int) -> list[str]:
    global _lookups
    call = _call.get()
    if call:
        await _org_lookups.take(call.org_id, ORG_LOOKUPS)
    with _lock:
        if _lookups >= RESOLVE_QUEUE:
            if call:
                _org_lookups.give(call.org_id)
            raise OSError("resolver_busy")
        _lookups += 1
        if call:
            call.refs += 1

    def done(_):
        global _lookups
        with _lock:
            _lookups -= 1
            if call:
                _org_lookups.give(call.org_id)
        if call:
            call.release()
    future = _resolver.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    future.add_done_callback(done)  # also when cancelled before it ran
    infos = await asyncio.wrap_future(future)
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
    host is not public; ReplyTooLarge past MAX_REPLY_BYTES, httpx.TimeoutException past the deadline, including a
    wait for one of the `org`'s ORG_CALLS places."""
    timeout = timeout or TIMEOUT_S
    call = _Call(org.get()) if org.get() else None
    token = _call.set(call)
    try:
        async with asyncio.timeout(timeout):
            if call:
                await call.start()
            return await _post(url, json, timeout)
    except TimeoutError:
        raise httpx.TimeoutException("timeout")
    finally:
        _call.reset(token)
        if call and call.refs:
            call.release()


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
