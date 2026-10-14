"""End-to-end load against a running backend (Postgres + Redis): streamed appends, prechecks, Redis loss.

    python scripts/load_e2e.py setup                          # org, agent, api key, the 10 bench policies
    python scripts/load_e2e.py append --rate 1000 --traces 100 --seconds 30
    python scripts/load_e2e.py precheck --rate 200 --traces 20 --prefix 2000
    python scripts/load_e2e.py flush --prefix 5000            # appends to long traces after Redis lost the state
    python scripts/load_e2e.py poison --traces 2              # needs a Celery worker; batch check of a normal
                                                              # trace queued behind `count by` traces (AUDIT-2)

Reads POSTGRES_* / REDIS_URL like the app; `setup` writes the ids to agentops_load_e2e.json in the temp dir.
"""
import argparse
import asyncio
import json
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench_policy import percentile, synthetic_policies, synthetic_trace  # noqa: E402

STATE = Path(tempfile.gettempdir()) / "agentops_load_e2e.json"


def _atoms(f):
    """The bench formulas with `attrs` split out of other atoms, so the DSL printer can write them."""
    from app.policy import ast
    if isinstance(f, ast.Match):
        return ast.And(replace(f, attrs=()), ast.Match(attrs=f.attrs)) if f.attrs and f.kind else f
    if isinstance(f, ast.Count):
        return replace(f, pred=_atoms(f.pred))
    if hasattr(f, "arg"):
        return type(f)(_atoms(f.arg))
    return type(f)(_atoms(f.left), _atoms(f.right))


async def setup(n_policies: int) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.middleware.auth import hash_api_key
    from app.models import Agent, ApiKey, Organization, TracePolicy
    from app.policy.dsl import pretty

    engine = create_async_engine(settings.db_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        org = Organization(name="load", slug=f"load-{uuid.uuid4().hex[:8]}", rate_limit_per_min=0)
        db.add(org)
        await db.flush()
        agent = Agent(org_id=org.id, name="load-agent")
        key = f"ak_load_{uuid.uuid4().hex}"
        db.add_all([agent, ApiKey(org_id=org.id, name="load", key_prefix=key[:8], key_hash=hash_api_key(key))])
        await db.flush()
        policies = synthetic_policies()
        for i in range(n_policies):
            p = policies[i % len(policies)]
            p = replace(p, name=f"{p.name}_{i}", formula=_atoms(p.formula))
            db.add(TracePolicy(org_id=org.id, name=p.name, source=pretty(p), severity=p.severity, action="log"))
        await db.commit()
        STATE.write_text(json.dumps({"agent_id": agent.id, "api_key": key}))
    await engine.dispose()
    print(f"org {org.id}, agent {agent.id}, {n_policies} policies -> {STATE}")


def _event(e) -> dict:
    return {"seq": e.seq, "kind": e.kind, "name": e.name, "status": e.status, "attrs": dict(e.attrs)}


def report(label: str, samples: list[float], errors: dict, wall: float) -> None:
    n = len(samples)
    p50, p95, p99 = (percentile(samples, q) for q in (50, 95, 99)) if n else (0, 0, 0)
    print(f"{label}: {n} ok in {wall:.1f}s ({n / wall:.0f}/s)  p50 {p50:.1f} ms  p95 {p95:.1f} ms  "
          f"p99 {p99:.1f} ms  max {max(samples, default=0):.1f} ms  errors {errors or 0}")


class Runner:
    def __init__(self, url: str, concurrency: int):
        st = json.loads(STATE.read_text())
        self.agent_id = st["agent_id"]
        self.http = httpx.AsyncClient(base_url=url, headers={"X-API-Key": st["api_key"]}, timeout=120,
                                      limits=httpx.Limits(max_connections=concurrency))
        self.sem = asyncio.Semaphore(concurrency)
        self.samples: list[float] = []
        self.errors: dict[str, int] = {}

    async def call(self, path: str, body: dict, record: bool = True) -> httpx.Response | None:
        async with self.sem:
            t = time.perf_counter()
            try:
                r = await self.http.post(path, json=body)
            except httpx.HTTPError as e:
                self.errors[type(e).__name__] = self.errors.get(type(e).__name__, 0) + 1
                return None
            ms = (time.perf_counter() - t) * 1000
        if r.status_code >= 300:
            self.errors[str(r.status_code)] = self.errors.get(str(r.status_code), 0) + 1
        elif record:
            self.samples.append(ms)
        return r

    async def append(self, uid: str, events, final: bool = False, record: bool = True):
        return await self.call(f"/api/traces/{uid}/events", {
            "agent_id": self.agent_id, "events": [_event(e) for e in events], "final": final}, record)

    async def prefill(self, n_traces: int, prefix: int) -> list[str]:
        uids = [f"load-{uuid.uuid4().hex}" for _ in range(n_traces)]
        for uid, t in zip(uids, range(n_traces)):
            events = synthetic_trace(prefix, seed=t)
            for i in range(0, prefix, 500):
                await self.append(uid, events[i:i + 500], record=False)
        return uids


async def paced(rate: float, total: int, make) -> float:
    start, tasks = time.perf_counter(), []
    for i in range(total):
        delay = start + i / rate - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        tasks.append(asyncio.create_task(make(i)))
    await asyncio.gather(*tasks)
    return time.perf_counter() - start


async def run_append(a) -> None:
    """One event per request, round-robin over traces; each trace's appends are sequential (seq order)."""
    r = Runner(a.url, a.concurrency)
    uids = [f"load-{uuid.uuid4().hex}" for _ in range(a.traces)]
    traces = [synthetic_trace(a.length, seed=t) for t in range(a.traces)]
    locks = [asyncio.Lock() for _ in uids]
    pos = [0] * a.traces

    async def one(i):
        t = i % a.traces
        async with locks[t]:
            p, pos[t] = pos[t], pos[t] + 1
            await r.append(uids[t], [replace(traces[t][p % a.length], seq=p)])

    wall = await paced(a.rate, int(a.rate * a.seconds), one)
    report(f"append {a.traces} traces @ {a.rate:g}/s", r.samples, r.errors, wall)


async def run_precheck(a) -> None:
    r = Runner(a.url, a.concurrency)
    uids = await r.prefill(a.traces, a.prefix)
    cand = {"seq": 0, "kind": "tool", "name": "delete_user", "status": "success", "attrs": {}}

    async def one(i):
        await r.call("/api/trace-policies/check",
                     {"agent_id": r.agent_id, "trace_uid": uids[i % len(uids)], "candidate": cand})

    wall = await paced(a.rate, int(a.rate * a.seconds), one)
    report(f"precheck {a.traces} traces x {a.prefix}-event prefix @ {a.rate:g}/s", r.samples, r.errors, wall)


async def run_flush(a) -> None:
    """Appends to `traces` long traces with Redis state present, then again after FLUSHDB (rebuild from spans)."""
    import redis.asyncio as aioredis

    from app.config import settings

    r = Runner(a.url, a.concurrency)
    uids = await r.prefill(a.traces, a.prefix)
    events = [synthetic_trace(a.prefix + 2, seed=t)[a.prefix:] for t in range(a.traces)]
    t0 = time.perf_counter()
    await asyncio.gather(*(r.append(u, ev[:1]) for u, ev in zip(uids, events)))
    report("append, state cached", r.samples, r.errors, time.perf_counter() - t0)
    await aioredis.from_url(settings.REDIS_URL).flushdb()
    r.samples, r.errors = [], {}
    t0 = time.perf_counter()
    await asyncio.gather(*(r.append(u, ev[1:]) for u, ev in zip(uids, events)))
    report("append, after FLUSHDB", r.samples, r.errors, time.perf_counter() - t0)
    r.samples, r.errors = [], {}
    await aioredis.from_url(settings.REDIS_URL).flushdb()
    cand = {"seq": 0, "kind": "tool", "name": "delete_user", "status": "success", "attrs": {}}
    for n, label in ((1, "precheck, first after FLUSHDB"), (a.checks - 1, f"precheck x{a.checks - 1}, later")):
        r.samples, r.errors = [], {}
        t0 = time.perf_counter()
        for _ in range(n):
            await asyncio.gather(*(r.call("/api/trace-policies/check", {"agent_id": r.agent_id, "trace_uid": u,
                                                                        "candidate": cand}) for u in uids))
        report(label, r.samples, r.errors, time.perf_counter() - t0)


async def run_poison(a) -> None:
    """`traces` 800-event traces whose `retry_cap` counterexample spans the trace, then one ordinary trace."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import settings
    from app.models import Trace, TracePolicyViolation

    r, n = Runner(a.url, a.concurrency), 800
    bad = {n - 1 - j * (n // 4) for j in range(4)}
    poison = [{"seq": i, "kind": "tool", "name": "drop_db" if i == 0 else "fetch" if i in bad else f"t{i % 7}",
               "status": "error" if i in bad else "success"} for i in range(n)]
    uids = [f"poison-{uuid.uuid4().hex}" for _ in range(a.traces)]
    for uid in uids:
        await r.call("/api/traces/ingest", {"agent_id": r.agent_id, "trace_id": uid, "events": poison})
    normal = f"normal-{uuid.uuid4().hex}"
    await asyncio.sleep(2)
    t0 = time.perf_counter()
    await r.call("/api/traces/ingest", {"agent_id": r.agent_id, "trace_id": normal,
                                        "events": [_event(e) for e in synthetic_trace(seed=1)]})
    engine = create_async_engine(settings.db_url)

    async def rows(uid):
        async with engine.connect() as db:
            return (await db.execute(select(func.count()).select_from(TracePolicyViolation).join(Trace).where(
                Trace.trace_uid == uid))).scalar_one()

    while not await rows(normal):
        await asyncio.sleep(1)
    print(f"normal 200-event trace checked after {time.perf_counter() - t0:.0f} s")
    await asyncio.sleep(max(0.0, 360 - (time.perf_counter() - t0)))
    print(f"violation rows for the poison traces after 360 s: {[await rows(u) for u in uids]}")
    await engine.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["setup", "append", "precheck", "flush", "poison"])
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--policies", type=int, default=10)
    ap.add_argument("--rate", type=float, default=1000)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--traces", type=int, default=100)
    ap.add_argument("--length", type=int, default=200)
    ap.add_argument("--prefix", type=int, default=2000)
    ap.add_argument("--checks", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=64)
    a = ap.parse_args()
    if a.mode == "setup":
        asyncio.run(setup(a.policies))
    else:
        asyncio.run({"append": run_append, "precheck": run_precheck, "flush": run_flush, "poison": run_poison}[a.mode](a))


if __name__ == "__main__":
    main()
