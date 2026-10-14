"""Async-native client.

Note: the default `AgentOpsClient` works fine in async code (use `async with agentops.trace(...)`)
because its background flusher is a thread. This module provides a fully-async variant for
async-first applications that want to avoid threads entirely.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from agentops_sdk.precheck import PrecheckMixin
from agentops_sdk.trace import Trace
from agentops_sdk.utils import append_to_disk, buffer_rejected, disk_buffer_path

log = logging.getLogger("agentops_sdk.async")


class AsyncAgentOpsClient(PrecheckMixin):
    is_async = True

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

        self._queue: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._disk = disk_buffer_path(api_key)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

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
        try:
            self._queue.put_nowait(payload)
        except Exception:
            append_to_disk(self._disk, payload)

    async def aenqueue(self, payload: dict) -> None:
        await self._queue.put(payload)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.flush_interval_s)
            except asyncio.TimeoutError:
                pass
            items: list[dict] = []
            while not self._queue.empty() and len(items) < self.batch_size:
                items.append(await self._queue.get())
            if items:
                await self._post(items)

    async def flush(self) -> None:
        items: list[dict] = []
        while not self._queue.empty():
            items.append(await self._queue.get())
        if items:
            await self._post(items)

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await self._task
            except Exception:
                pass
        await self.flush()

    async def _post(self, items: list[dict]) -> None:
        headers = {"X-API-Key": self.api_key}
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(f"{self.base_url}/api/traces/ingest/bulk",
                                 json={"traces": items}, headers=headers)
                if r.status_code >= 400:
                    log.warning("ingest failed: %s %s", r.status_code, r.text[:200])
                    for it in items:
                        append_to_disk(self._disk, it)
                else:
                    buffer_rejected(self._disk, items, r)
        except Exception as e:
            log.warning("ingest exception, buffering: %s", e)
            for it in items:
                append_to_disk(self._disk, it)
