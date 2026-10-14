from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import time
from queue import Empty, Queue
from typing import Any, Optional

import httpx

from agentops_sdk.precheck import CheckResult, PrecheckMixin
from agentops_sdk.trace import Trace
from agentops_sdk.utils import append_to_disk, buffer_rejected, disk_buffer_path, drain_disk

log = logging.getLogger("agentops_sdk")


class AgentOpsClient(PrecheckMixin):
    def __init__(
        self,
        api_key: str,
        agent_id: str,
        base_url: str = "http://localhost:8000",
        environment: str = "dev",
        agent_version: str = "1.0.0",
        flush_interval_s: float = 2.0,
        batch_size: int = 50,
        fail_closed: bool = False,
        check_timeout_s: float = 2.0,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.agent_id = agent_id
        self.base_url = base_url.rstrip("/")
        self.environment = environment
        self.agent_version = agent_version
        self.flush_interval_s = flush_interval_s
        self.batch_size = batch_size
        self.fail_closed = fail_closed
        self.check_timeout_s = check_timeout_s

        self._queue: Queue = Queue()
        self._stop = threading.Event()
        self._disk = disk_buffer_path(api_key)
        self._thread = threading.Thread(target=self._loop, name="agentops-flusher", daemon=True)
        self._thread.start()
        atexit.register(self.close)

        # drain any disk buffer from previous run
        leftover = drain_disk(self._disk)
        for it in leftover:
            self._queue.put(it)

    def trace(self, *, input: str = "", session_id: Optional[str] = None,
              agent_id: Optional[str] = None, environment: Optional[str] = None,
              agent_version: Optional[str] = None, stream: bool = False) -> Trace:
        return Trace(
            agent_id=agent_id or self.agent_id,
            environment=environment or self.environment,
            agent_version=agent_version or self.agent_version,
            session_id=session_id,
            input=input,
            _client=self,
            stream=stream,
        )

    def enqueue(self, payload: dict) -> None:
        self._queue.put(payload)

    async def aenqueue(self, payload: dict) -> None:
        self._queue.put(payload)

    def flush(self) -> None:
        items = self._drain()
        if items:
            self._post(items)

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.flush()
        if self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _drain(self) -> list[dict]:
        out: list[dict] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except Empty:
                break
            if len(out) >= self.batch_size:
                break
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.flush_interval_s)
            items = self._drain()
            if items:
                self._post(items)

    def _post(self, items: list[dict]) -> None:
        headers = {"X-API-Key": self.api_key}
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.post(f"{self.base_url}/api/traces/ingest/bulk",
                           json={"traces": items}, headers=headers)
                if r.status_code >= 400:
                    log.warning("agentops ingest failed: %s %s", r.status_code, r.text[:200])
                    for it in items:
                        append_to_disk(self._disk, it)
                else:
                    buffer_rejected(self._disk, items, r)
        except Exception as e:
            log.warning("agentops ingest exception, buffering to disk: %s", e)
            for it in items:
                append_to_disk(self._disk, it)


# ---- Module-level singleton convenience API ----

_singleton: Optional[AgentOpsClient] = None


def init(*, api_key: str, agent_id: str, base_url: str = "http://localhost:8000",
         environment: str = "dev", agent_version: str = "1.0.0",
         flush_interval_s: float = 2.0, batch_size: int = 50, fail_closed: bool = False,
         check_timeout_s: float = 2.0) -> AgentOpsClient:
    global _singleton
    if _singleton is not None:
        _singleton.close()
    _singleton = AgentOpsClient(
        api_key=api_key, agent_id=agent_id, base_url=base_url,
        environment=environment, agent_version=agent_version,
        flush_interval_s=flush_interval_s, batch_size=batch_size,
        fail_closed=fail_closed, check_timeout_s=check_timeout_s,
    )
    return _singleton


def trace(*, input: str = "", session_id: Optional[str] = None,
          agent_id: Optional[str] = None, environment: Optional[str] = None,
          agent_version: Optional[str] = None, stream: bool = False) -> Trace:
    return _require().trace(
        input=input, session_id=session_id, agent_id=agent_id,
        environment=environment, agent_version=agent_version, stream=stream,
    )


def _require() -> AgentOpsClient:
    if _singleton is None:
        raise RuntimeError("agentops.init() must be called first")
    return _singleton


def check(name: str, *, kind: str = "tool", input: Optional[dict] = None, **attrs) -> CheckResult:
    return _require().check(name, kind=kind, input=input, **attrs)


async def acheck(name: str, *, kind: str = "tool", input: Optional[dict] = None, **attrs) -> CheckResult:
    return await _require().acheck(name, kind=kind, input=input, **attrs)


def atrace(*args, **kwargs) -> Trace:
    """Alias of trace() — both work in async contexts (use `async with`)."""
    return trace(*args, **kwargs)


def flush() -> None:
    if _singleton is not None:
        _singleton.flush()


def close() -> None:
    global _singleton
    if _singleton is not None:
        _singleton.close()
        _singleton = None
