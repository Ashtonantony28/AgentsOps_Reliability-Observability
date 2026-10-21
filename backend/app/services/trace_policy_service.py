import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
import weakref
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from functools import partial
from typing import Awaitable, Callable, Sequence

from redis.exceptions import LockError
from sqlalchemy import or_, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Span, Trace, TracePolicy, TracePolicyViolation
from app.policy.ast import Policy
from app.policy.checker import (CompiledPolicy, Monitor, Violation, check_trace, check_verdicts, compile_policy,
                                work)
from app.policy.dsl import PolicyParseError, parse
from app.policy.events import Event, events_from_spans
from app.policy.monitor import plan_for
from app.redis import get_redis
from app.schemas.trace import EventIn

log = logging.getLogger(__name__)
STATE_TTL_S = 24 * 3600
# `checker.work` (events x policy nodes and glob costs) of an append, or of a request's uploads checked inline, about a
# second: a longer append is a 413 saying how many events fit, a later upload of a bulk request is refused for another
MAX_REQUEST_WORK = 2_000_000
WORK_SLICE = 200_000  # work one thread does at a time; longer jobs go in slices through _pool
GIL_PIECE = 1_000  # work a pool thread does between offers of the GIL, about a millisecond
POOL_THREADS = 2
_pool = ThreadPoolExecutor(POOL_THREADS, thread_name_prefix="policy-work")  # so long jobs can't starve the short ones
PREFIX_TTL_S = 3600
# a history keeps its newest checkpoint; an org's checkpoints past this many bytes go, oldest first
MAX_ORG_CHECKPOINT_BYTES = 32 * 2**20
CHECK_DEADLINE_S = 30  # a check's policy work stops past this, or once its request is gone
# a check waits for another's slice of the same prefix, looking at its deadline and request this often, and steps the
# slice itself once the other has stepped no piece of it for this long (about 1 000 pieces' time: its thread is stalled)
SLICE_WAIT_S = 1.0
BATCH_LOCK_WAIT_S = 10
MAX_ORG_POLICIES = 100  # non-archived, so a span rebuild or batch check is bounded too
MAX_ORG_POLICY_NODES = 4_000  # `org_cost` summed

Active = list[tuple[TracePolicy, CompiledPolicy]]


COMPILE_CACHE_SIZE = 2048
_compiled: "OrderedDict[tuple[str, str], CompiledPolicy | None]" = OrderedDict()
_compiled_lock = threading.Lock()


def _compile(policy_id: str, source: str) -> CompiledPolicy | None:
    """Parsed and planned once per (id, source) while cached; None, also cached, if it no longer parses to one policy.
    A miss can take tens of ms per policy, so callers on the event loop compile misses in a thread."""
    key = (policy_id, source)
    with _compiled_lock:
        if key in _compiled:
            _compiled.move_to_end(key)
            return _compiled[key]
    try:
        (p,) = parse(source)
        c = compile_policy(policy_id, p)
        plan_for(p)
    except (PolicyParseError, ValueError):
        log.warning("skipping trace policy %s: source does not parse to one policy", policy_id)
        c = None
    with _compiled_lock:
        _compiled[key] = c
        if len(_compiled) > COMPILE_CACHE_SIZE:
            _compiled.popitem(last=False)
    return c


def org_cost(p: Policy) -> int:
    """`Plan.cost` (nodes, and glob lists or long globs on top), or the nodes parsed for it if more (unused tags)."""
    return max(plan_for(p).cost, p.parsed)


def _compile_all(keys: list[tuple[str, str]]) -> list[CompiledPolicy | None]:
    return [_compile(*k) for k in keys]


async def compile_all(keys: list[tuple[str, str]]) -> list[CompiledPolicy | None]:
    if all(k in _compiled for k in keys):
        return _compile_all(keys)
    return await asyncio.to_thread(_compile_all, keys)


async def active_policies(db: AsyncSession, org_id: str, agent_id: str, environment: str) -> Active:
    rows = (await db.execute(
        select(TracePolicy).where(
            TracePolicy.org_id == org_id, TracePolicy.enabled == True,  # noqa: E712
            TracePolicy.archived_at.is_(None),
            or_(TracePolicy.agent_id.is_(None), TracePolicy.agent_id == agent_id),
            or_(TracePolicy.environment.is_(None), TracePolicy.environment == environment),
        ).order_by(TracePolicy.id)
    )).scalars().all()
    compiled = await compile_all([(r.id, r.source) for r in rows])
    return [(r, c) for r, c in zip(rows, compiled) if c is not None]


def _status(mode: str, v: Violation) -> str:
    return "pending" if mode == "live" else "minimal" if v.minimal else "fallback"


async def record_violations(db: AsyncSession, trace: Trace, policies: Active, violations: Sequence[Violation],
                            mode: str) -> list[TracePolicyViolation]:
    """Inserts violations not already stored for (trace, policy, version); returns the new rows."""
    if not violations:
        return []
    by_id = {row.id: row for row, _ in policies}
    now = datetime.utcnow()
    values = [dict(
        id=str(uuid.uuid4()), org_id=trace.org_id, trace_id=trace.id, agent_id=trace.agent_id,
        policy_id=v.policy_id, policy_name=v.policy_name, policy_version=by_id[v.policy_id].version,
        severity=v.severity, at_seq=v.at_seq, counterexample=list(v.counterexample), message=v.message,
        mode=mode, detected_at=now, counterexample_status=_status(mode, v),
    ) for v in violations]
    insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
    stmt = (insert(TracePolicyViolation).values(values)
            .on_conflict_do_nothing(index_elements=["trace_id", "policy_id", "policy_version"])
            .returning(TracePolicyViolation.id))
    ids = set((await db.execute(stmt)).scalars().all())
    return [TracePolicyViolation(**v) for v in values if v["id"] in ids]


def _state_key(trace_id: str) -> str:
    return f"policy:monitor:{trace_id}"


def _precheck_key(trace_id: str) -> str:
    return f"policy:precheck:{trace_id}"


# the columns events_from_spans reads; full rows carry payloads the checker never needs
_EVENT_COLUMNS = (Span.id, Span.seq, Span.kind, Span.name, Span.status, Span.attrs, Span.started_at)
MAX_CONCURRENT_REBUILDS = 4


class FairSlots:
    """`n` slots, handed out round robin across orgs as they free up, first come first served within an org: an org
    with many jobs waiting delays another org's next job by at most one of its own."""

    def __init__(self, n: int):
        self._free = n
        self._waiting: dict[str, deque[asyncio.Future]] = {}  # in turn order

    @asynccontextmanager
    async def __call__(self, org_id: str):
        """Holds a slot; `await turn()` hands it on to the next org in turn, if one waits, and waits for this org's
        next turn."""
        if self._free:  # then nobody waits
            self._free -= 1
        else:
            await self._wait(org_id)
        held = True

        async def turn() -> None:
            nonlocal held
            if self._waiting:
                fut = self._queue(org_id)
                held = False
                self._release()
                await self._wait(org_id, fut)
                held = True

        try:
            yield turn
        finally:
            if held:
                self._release()

    def _queue(self, org_id: str) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._waiting.setdefault(org_id, deque()).append(fut)
        return fut

    async def _wait(self, org_id: str, fut: asyncio.Future | None = None) -> None:
        fut = fut or self._queue(org_id)
        try:
            await fut
        except asyncio.CancelledError:
            if not fut.cancelled():  # handed a slot as it was cancelled
                self._release()
            raise

    def _release(self) -> None:
        while self._waiting:
            org_id = next(iter(self._waiting))
            queue = self._waiting.pop(org_id)
            fut = queue.popleft()
            if queue:
                self._waiting[org_id] = queue  # to the back of the turn
            if not fut.done():
                fut.set_result(None)
                return
        self._free += 1


_slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # per event loop: (pool slices, rebuilds)


def _fair() -> tuple[FairSlots, FairSlots]:
    loop = asyncio.get_running_loop()
    return _slots.get(loop) or _slots.setdefault(loop, (FairSlots(POOL_THREADS), FairSlots(MAX_CONCURRENT_REBUILDS)))


def _restore(blob: str | None, compiled: Sequence[CompiledPolicy], next_seq: int,
             partial: bool = False) -> Monitor | None:
    """The monitor `blob` holds if it is at `next_seq`, or with `partial` before it."""
    if blob:
        with suppress(ValueError):
            m = Monitor.restore(compiled, blob.encode())
            if m.next_seq == next_seq or partial and m.next_seq < next_seq:
                return m
    return None


async def _replay(db: AsyncSession, org_id: str, trace_id: str, compiled: Sequence[CompiledPolicy],
                  m: Monitor | None = None,
                  between: Callable[[], Awaitable[None]] | None = None) -> tuple[Monitor, list[Violation]]:
    """Rebuilds from spans, or steps `m` through the spans after it. After a Redis restart every open trace needs
    this, so few run at once per process, taken in turn by orgs."""
    async with _fair()[1](org_id):
        spans = (await db.execute(select(*_EVENT_COLUMNS).where(Span.trace_id == trace_id))).all()
        m = m or Monitor(compiled)
        events = [e for e in await asyncio.to_thread(events_from_spans, spans) if e.seq >= m.next_seq]
        return m, await run_sliced(partial(_steps, m), events, compiled, org_id, between)


async def run_sliced(fn: Callable[[Sequence], list], items: Sequence, compiled: Sequence[CompiledPolicy],
                     org_id: str, between: Callable[[], Awaitable[None]] | None = None) -> list:
    """`fn` over consecutive slices of `items` (events, stepped through `compiled`) off the event loop, results
    concatenated; past WORK_SLICE in slices through `_pool`, taken in turn by orgs, so no job holds a thread for long
    and no org's jobs queue another's. `between()` is awaited between slices."""
    cost = work(compiled, 1)
    piece = max(1, GIL_PIECE // max(1, cost))
    if cost * len(items) <= WORK_SLICE:
        return await asyncio.to_thread(_in_pieces, fn, items, piece)
    loop, slots, n, out = asyncio.get_running_loop(), _fair()[0], max(1, WORK_SLICE // cost), []
    async with slots(org_id) as turn:
        for i in range(0, len(items), n):
            if i:
                if between is not None:
                    await between()
                await turn()
            out += await loop.run_in_executor(_pool, _in_pieces, fn, items[i:i + n], piece)
    return out


def _in_pieces(fn: Callable[[Sequence], list], items: Sequence, n: int) -> list:
    """`fn` over pieces of `n` items, offering the GIL between them: the event loop (and so every other org's
    requests) waits for at most a piece, not the interpreter's 5 ms switch interval, each time it needs it."""
    out = []
    for i in range(0, len(items), n):
        time.sleep(0)
        out += fn(items[i:i + n])
    return out


def _steps(m: Monitor, events: Sequence[Event]) -> list[Violation]:
    return [v for e in events for v in m.step(e)]


def _step_inputs(m: Monitor, events: Sequence[EventIn]) -> list:
    for e in events:
        m.step(Event(m.next_seq, e.kind, e.name, e.status, dict(e.attrs)))
    return []


async def load_monitor(db: AsyncSession, org_id: str, trace_id: str, compiled: Sequence[CompiledPolicy],
                       next_seq: int) -> tuple[Monitor, list[Violation]]:
    """Monitor positioned after the stored events. Rebuilt from spans if the Redis state is missing, stale or
    for other policies; the violations found while rebuilding are returned so they can be recorded."""
    blob = await get_redis().get(_state_key(trace_id))
    if blob and (m := await asyncio.to_thread(_restore, blob, compiled, next_seq)):
        return m, []
    await get_redis().delete(_precheck_key(trace_id))  # the state this append saves supersedes it
    return await _replay(db, org_id, trace_id, compiled)


async def precheck_monitor(db: AsyncSession, org_id: str, trace_id: str, compiled: Sequence[CompiledPolicy],
                           next_seq: int, stopped: Callable[[], Awaitable[bool]]) -> Monitor:
    """As `load_monitor`, but a rebuild is cached under its own key: it must not stand in for the append path's
    state, whose own rebuild records the violations it finds. One rebuild per trace at a time: a check waits for the
    one in flight, and resumes from where a stopped one got to. Raises CheckStopped once `stopped()`."""
    r = get_redis()
    keys = (_state_key(trace_id), _precheck_key(trace_id))
    while True:
        for blob in await r.mget(keys):
            if blob and (m := await asyncio.to_thread(_restore, blob, compiled, next_seq)):
                return m
        if await stopped():
            raise CheckStopped
        if m := await _rebuild(db, org_id, trace_id, compiled, next_seq, stopped):
            return m


async def _rebuild(db: AsyncSession, org_id: str, trace_id: str, compiled: Sequence[CompiledPolicy], next_seq: int,
                   stopped: Callable[[], Awaitable[bool]]) -> Monitor | None:
    """The trace's precheck monitor, rebuilt under its lock from where a stopped rebuild got to, and saved; None if
    another check held the lock for SLICE_WAIT_S. Once `stopped()`, saves where it got to and raises CheckStopped."""
    r, key = get_redis(), _precheck_key(trace_id)
    lock = r.lock(f"lock:monitor-rebuild:{trace_id}", timeout=30, blocking_timeout=SLICE_WAIT_S, sleep=0.05)
    if not await lock.acquire():
        return None
    try:
        m = await asyncio.to_thread(_restore, await r.get(key), compiled, next_seq, True) or Monitor(compiled)

        async def between() -> None:
            with suppress(LockError):
                await lock.reacquire()  # held while the rebuild moves, past the lock's timeout
            if await stopped():
                await r.set(key, await asyncio.to_thread(m.snapshot), ex=STATE_TTL_S)
                raise CheckStopped

        if m.next_seq < next_seq:
            await _replay(db, org_id, trace_id, compiled, m, between)
        await r.set(key, await asyncio.to_thread(m.snapshot), ex=STATE_TTL_S)
        return m
    finally:
        with suppress(LockError):
            await lock.release()


async def save_monitor(trace_id: str, monitor: Monitor | None) -> None:
    if monitor is None:
        await get_redis().delete(_state_key(trace_id), _precheck_key(trace_id))
    else:
        await get_redis().set(_state_key(trace_id), await asyncio.to_thread(monitor.snapshot), ex=STATE_TTL_S)


async def blocking_violations(org_id: str, compiled: Sequence[CompiledPolicy],
                              events: Sequence[Event]) -> list[Violation]:
    if not compiled:
        return []
    m = Monitor(compiled)
    return await run_sliced(partial(_steps, m), events, compiled, org_id)


def _marks(seed: str, prefix: Sequence[EventIn], k: int) -> list[str]:
    """Digests of prefix[:k], prefix[:2k], ...: each covers every event before it."""
    h, out = hashlib.blake2b(seed.encode(), digest_size=16).digest(), []
    for i, e in enumerate(prefix[:len(prefix) // k * k], 1):
        row = json.dumps([e.kind, e.name, e.status, e.attrs], sort_keys=True, default=str, separators=(",", ":"))
        h = hashlib.blake2b(h + row.encode(), digest_size=16).digest()
        if i % k == 0:
            out.append(h.hex())
    return out


class CheckStopped(Exception):
    """A check's request went away, or its work ran past CHECK_DEADLINE_S; what it stepped is kept for the next."""


async def prefix_monitor(db: AsyncSession, org_id: str, trace_id: str | None, compiled: Sequence[CompiledPolicy],
                         next_seq: int, prefix: Sequence[EventIn],
                         gone: Callable[[], Awaitable[bool]] | None = None) -> Monitor:
    """The monitor after the trace's stored events, if any, and `prefix`. The SDK sends an unstreamed trace's whole
    history with each check, so a long prefix resumes from the checkpoints earlier checks of the same events (and
    policies) saved every `k` events, each as soon as it was stepped and replacing the one before: before each slice
    of `k` the newest one is looked for, and a slice another check of this process is stepping is waited for, not
    stepped again. Raises CheckStopped once `gone()` or past the deadline."""
    deadline = time.monotonic() + CHECK_DEADLINE_S

    async def stopped() -> bool:
        return time.monotonic() > deadline or (gone is not None and await gone())

    async def stored() -> Monitor:
        if trace_id is None:
            return Monitor(compiled)
        return await precheck_monitor(db, org_id, trace_id, compiled, next_seq, stopped)

    k = max(1, WORK_SLICE // max(1, work(compiled, 1)))
    if len(prefix) < k:
        m = await stored()
        if prefix:
            await asyncio.to_thread(_step_inputs, m, prefix)
        return m
    plans = ",".join(f"{c.policy_id}:{plan_for(c.policy).digest}" for c in compiled)
    keys = [f"policy:prefix:{d}" for d in await asyncio.to_thread(
        _marks, f"{org_id}|{trace_id}|{next_seq}|{plans}", prefix, k)]
    steps, j, m = _Steps(compiled, org_id, get_redis(), next_seq, k), 0, None
    while j < len(keys):
        if await stopped():
            raise CheckStopped
        found, newer = await steps.newest(keys, j)
        if newer:
            j, m = found, newer
        elif m is None:
            m = await stored()
        elif steps.free(keys[j]):  # else another check is stepping this slice: wait for it again
            await steps.step(keys[j], m, prefix[j * k:(j + 1) * k], keys[j - 1] if j else None)
            j += 1
    await asyncio.to_thread(_step_inputs, m, prefix[len(keys) * k:])
    return m


class _Slice:
    def __init__(self):
        self.done, self.blob, self.moved = asyncio.Event(), None, None


_stepping: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # per event loop: checkpoint key -> _Slice


# the 1-based index of the last of KEYS that exists, or 0
_NEWEST = "for i = #KEYS, 1, -1 do if redis.call('EXISTS', KEYS[i]) == 1 then return i end end return 0"
# saves checkpoint KEYS[1] (ARGV: blob, ttl, now, max bytes) in place of KEYS[2], the one before it, and counts it
# in its org's bytes: KEYS[3] orders the org's checkpoints by age, KEYS[4] holds their sizes and their 'total'. The
# org's oldest go once over the bound; a checkpoint over the bound alone is not saved.
_SAVE = """
local function drop(k)
  local n = redis.call('HGET', KEYS[4], k)
  if n then
    redis.call('HDEL', KEYS[4], k)
    redis.call('HINCRBY', KEYS[4], 'total', -tonumber(n))
  end
  redis.call('ZREM', KEYS[3], k)
  redis.call('DEL', k)
end
local ttl, now, max, size = tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4]), string.len(ARGV[1])
drop(KEYS[2])
drop(KEYS[1])
for _, k in ipairs(redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now - ttl)) do drop(k) end
if size > max then return 0 end
while tonumber(redis.call('HGET', KEYS[4], 'total') or '0') + size > max do
  local oldest = redis.call('ZRANGE', KEYS[3], 0, 0)[1]
  if not oldest then redis.call('DEL', KEYS[4]) break end
  drop(oldest)
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)
redis.call('ZADD', KEYS[3], now, KEYS[1])
redis.call('HSET', KEYS[4], KEYS[1], size)
redis.call('HINCRBY', KEYS[4], 'total', size)
redis.call('EXPIRE', KEYS[3], ttl)
redis.call('EXPIRE', KEYS[4], ttl)
return 1
"""


class _Steps:
    """Checkpointed slices of one check's prefix (`k` events each) on `_pool`, taken in turn by orgs."""

    def __init__(self, compiled: Sequence[CompiledPolicy], org_id: str, r, next_seq: int, k: int):
        self.compiled, self.org_id, self.r, self.next_seq, self.k = compiled, org_id, r, next_seq, k
        self.piece = max(1, GIL_PIECE // max(1, work(compiled, 1)))
        loop = asyncio.get_running_loop()
        self.running: dict[str, _Slice] = _stepping.get(loop) or _stepping.setdefault(loop, {})

    async def newest(self, keys: list[str], j: int) -> tuple[int, Monitor | None]:
        """The newest checkpoint after the first `j` slices, in Redis (only it is read) or the next one, waited for up
        to SLICE_WAIT_S if another check of this process is stepping it: (slices it covers, its monitor), or
        (j, None)."""
        i, blob = 0, None
        if self.r is not None:
            try:
                for _ in range(2):  # again if a newer one replaced it as it was read
                    i = await self.r.eval(_NEWEST, len(keys) - j, *keys[j:])
                    if not i or (blob := (await self.r.mget(keys[j + i - 1:j + i]))[0]):
                        break
            except Exception:  # a check is answered without Redis, from the start
                log.exception("could not read prefix checkpoints")
                self.r = None
        if blob and (m := await self._restore(blob, j + i)):
            return j + i, m
        if j < len(keys) and (s := self.running.get(keys[j])):
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(s.done.wait(), SLICE_WAIT_S)
            if m := await self._restore(s.blob, j + 1):
                return j + 1, m
        return j, None

    def free(self, key: str) -> bool:
        """No check of this process is stepping `key`, or the one that is has stalled."""
        s = self.running.get(key)
        return s is None or s.moved is not None and time.monotonic() - s.moved > SLICE_WAIT_S

    async def _restore(self, blob: str | None, slices: int) -> Monitor | None:
        if not blob:
            return None
        return await asyncio.to_thread(_restore, blob, self.compiled, self.next_seq + slices * self.k)

    async def step(self, key: str, m: Monitor, events: Sequence[EventIn], before: str | None) -> None:
        """Steps `m` through `events` and saves the checkpoint `key` in place of `before`; checks waiting for it get
        its snapshot."""
        s = self.running[key] = _Slice()

        def piece(events: Sequence[EventIn]) -> list:
            s.moved = time.monotonic()  # a slow machine's slice is waited for as long as it moves, not stepped twice
            return _step_inputs(m, events)

        try:
            async with _fair()[0](self.org_id):
                await asyncio.get_running_loop().run_in_executor(_pool, _in_pieces, piece, events, self.piece)
            s.blob = (await asyncio.to_thread(m.snapshot)).decode()
            if self.r is not None:
                with suppress(Exception):
                    await self.r.eval(_SAVE, 4, key, before or key, f"policy:checkpoints:{self.org_id}",
                                      f"policy:checkpoint-bytes:{self.org_id}", s.blob, PREFIX_TTL_S, time.time(),
                                      MAX_ORG_CHECKPOINT_BYTES)
        finally:
            s.done.set()  # with no blob if stopped: a waiting check steps it
            if self.running.get(key) is s:
                del self.running[key]


class TraceBusy(Exception):
    """An append held the trace for `BATCH_LOCK_WAIT_S`; its batch check stored nothing and is to be retried."""


async def check_completed_trace(db: AsyncSession, trace_id: str,
                                counterexamples: bool = True) -> list[TracePolicyViolation]:
    """Batch check of a completed trace; re-running it stores nothing new. Raises TraceBusy."""
    trace = await db.get(Trace, trace_id, populate_existing=True)
    if trace is None or (stream := (trace.meta or {}).get("stream")) == "open":
        return []
    policies = await active_policies(db, trace.org_id, trace.agent_id, trace.environment)
    spans = (await db.execute(select(Span).where(Span.trace_id == trace_id))).scalars().all()
    check = check_trace if counterexamples else check_verdicts
    violations = check([c for _, c in policies], events_from_spans(spans))
    # under the append lock, so an idle trace can't be reopened between this re-read and the commit
    lock = get_redis().lock(f"lock:trace-events:{trace.org_id}:{trace.trace_uid}", timeout=60,
                            blocking_timeout=BATCH_LOCK_WAIT_S)
    try:
        locked = await lock.acquire()
    except Exception:  # the verdicts are still stored without Redis, at the risk the lock guards against
        log.exception("could not lock trace %s for its batch check", trace_id)
        locked = False
    else:
        if not locked:
            raise TraceBusy(trace_id)
    try:
        return await _store_batch(db, trace, stream, policies, violations)
    finally:
        if locked:
            with suppress(LockError):
                await lock.release()


async def _store_batch(db: AsyncSession, trace: Trace, stream: str | None, policies: Active,
                       violations: list[Violation]) -> list[TracePolicyViolation]:
    trace_id = trace.id
    trace = await db.get(Trace, trace_id, populate_existing=True)
    if trace is None or (trace.meta or {}).get("stream") != stream:  # reopened meanwhile; its next close checks it
        return []
    rows = await record_violations(db, trace, policies, violations, "batch")
    # live rows are stored before their counterexample: fill in those the batch found at the same event; the rest
    # (an older policy version, a refused call that was never stored) get none
    pending = (await db.execute(select(TracePolicyViolation).where(
        TracePolicyViolation.trace_id == trace_id, TracePolicyViolation.counterexample_status == "pending"
    ))).scalars().all()
    found = {v.policy_id: v for v in violations}
    versions = {row.id: row.version for row, _ in policies}
    for row in pending:
        v = found.get(row.policy_id)
        if v is not None and row.policy_version == versions[row.policy_id] and row.at_seq == v.at_seq:
            row.counterexample, row.counterexample_status = list(v.counterexample), _status("batch", v)
        else:
            row.counterexample_status = "none"
    await db.commit()
    return rows
