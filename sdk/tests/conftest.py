import asyncio
import json
import uuid

import httpx
import pytest

from agentops_sdk import client as client_mod
from agentops_sdk.async_client import AsyncAgentOpsClient
from agentops_sdk.client import AgentOpsClient

_RealClient, _RealAsyncClient = httpx.Client, httpx.AsyncClient


class FakeServer:
    """Records every request; replies from per-route queues ('check', 'events', 'bulk') or a default."""

    def __init__(self):
        self.requests: list[tuple[str, dict, dict]] = []
        self.queues: dict[str, list] = {"check": [], "events": [], "bulk": []}
        self.timeouts: list = []
        self.delay = 0.0

    def queue(self, route: str, *replies) -> None:
        self.queues[route].extend(replies)

    def route(self, path: str) -> str:
        return "check" if path.endswith("/check") else "events" if path.endswith("/events") else "bulk"

    def reply(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        route = self.route(request.url.path)
        self.requests.append((request.url.path, body, dict(request.headers)))
        r = self.queues[route].pop(0) if self.queues[route] else None
        if isinstance(r, BaseException):
            raise r
        if r is None:
            r = {"check": (200, {"allowed": True, "violations": []}),
                 "events": (202, {"accepted": len(body.get("events", [])), "violations": []}),
                 "bulk": (200, {"accepted": 1, "rejected": []})}[route]
        status, payload = r
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    def handler(self, request):
        return self.reply(request)

    async def ahandler(self, request):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.reply(request)

    def bodies(self, route: str) -> list[dict]:
        return [b for p, b, _ in self.requests if self.route(p) == route]

    def routes(self) -> list[str]:
        return [self.route(p) for p, _, _ in self.requests]


@pytest.fixture
def server(monkeypatch):
    s = FakeServer()

    def client(**kw):
        s.timeouts.append(kw.get("timeout"))
        return _RealClient(transport=httpx.MockTransport(s.handler), **kw)

    def aclient(**kw):
        s.timeouts.append(kw.get("timeout"))
        return _RealAsyncClient(transport=httpx.MockTransport(s.ahandler), **kw)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(httpx, "AsyncClient", aclient)
    return s


def _key() -> str:
    return f"t{uuid.uuid4().hex}"  # unique disk-buffer path per test


@pytest.fixture
def make_client(server):
    def make(**kw):
        c = AgentOpsClient(api_key=_key(), agent_id="a1", base_url="http://test", flush_interval_s=3600, **kw)
        c._stop.set()  # the flusher thread sleeps out the test; queued payloads stay inspectable
        return c

    return make


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def make_async_client(server):
    def make(**kw):
        return AsyncAgentOpsClient(api_key=_key(), agent_id="a1", base_url="http://test", **kw)
    return make


@pytest.fixture
def aclient(make_async_client):
    return make_async_client()


@pytest.fixture
def singleton(monkeypatch, make_client):
    def install(**kw):
        c = make_client(**kw)
        monkeypatch.setattr(client_mod, "_singleton", c)
        return c
    monkeypatch.setattr(client_mod, "_singleton", None)
    return install

