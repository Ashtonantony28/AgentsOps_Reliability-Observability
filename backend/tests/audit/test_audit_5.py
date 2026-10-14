"""AUDIT-5.md (round 2: correctness and performance) findings as failing tests; each is xfail(strict=True) until its
sub-goal fixes it."""
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk"))

from agentops_sdk.async_client import AsyncAgentOpsClient  # noqa: E402
from agentops_sdk.guard import guard  # noqa: E402
from agentops_sdk.precheck import PolicyBlocked  # noqa: E402
from app.models import Trace, TracePolicy, TracePolicyViolation  # noqa: E402
from app.policy import reference  # noqa: E402
from app.policy.checker import compile_policy  # noqa: E402
from app.policy.counterexample import minimal_counterexample  # noqa: E402
from app.policy.dsl import parse  # noqa: E402
from app.policy.events import Event  # noqa: E402

BACKEND = Path(__file__).resolve().parents[2]


def _doubling_tags(levels: int) -> str:
    """About 25 bytes per level; the inlined formula has 2^levels leaves but only levels + 2 distinct nodes."""
    return ('tag t0 = tool("a")\n' + "".join(f"tag t{i} = @t{i - 1} and @t{i - 1}\n" for i in range(1, levels + 1))
            + f"policy p severity high:\n  always not @t{levels}\n")


# A5-1: tags are inlined as shared nodes, but every checker walks the formula as a tree
def test_policy_cost_is_linear_in_source_size():
    script = f"""
import sys
sys.path.insert(0, {str(BACKEND)!r})
from app.policy.checker import Monitor, check_trace, compile_policy
from app.policy.dsl import PolicyParseError, parse
from app.policy.events import Event
try:
    (p,) = parse({_doubling_tags(30)!r})
except PolicyParseError:
    sys.exit(0)  # rejecting the policy is a fix too
c = compile_policy("p", p)
events = [Event(i, "tool", "a", "success", {{}}) for i in range(3)]
m = Monitor([c])
for e in events:
    m.step(e)
check_trace([c], events)
"""
    try:
        r = subprocess.run([sys.executable, "-c", script], timeout=10, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        pytest.fail("checking a 750-byte policy took over 10 s")
    assert r.returncode == 0, r.stderr


# A5-2: counts with a bound of 16 or more are bit-vector sums, which the deletion pass cannot shrink in budget
@pytest.mark.xfail(strict=True, reason="A5-2: a bound-40 count's counterexample keeps 169 events, not the 41 hits")
def test_large_bound_count_counterexample_is_minimal():
    (p,) = parse('policy cap severity high:\n  always count(tool("fetch_url")) <= 40')
    hits = set(range(3, 200, 5)) | {199}  # 41 hits; the other events are never counted
    events = [Event(i, "tool", "fetch_url" if i in hits else "search", "success", {}) for i in range(200)]
    assert reference.evaluate(p, events) == [199]
    assert minimal_counterexample(compile_policy("cap", p), events, 199) == tuple(sorted(hits))


# A5-4: the per-counterexample budget is checked between steps that are themselves unbounded; the overrun is
# measured as the SMT text handed to the solver (parsing it is not interruptible), not as wall time
@pytest.mark.xfail(strict=True, reason="A5-4: a 300-event nested count-by builds 17 MB of SMT text to parse")
def test_counterexample_encoding_stays_within_what_the_budget_can_parse(monkeypatch):
    from app.policy import counterexample

    class TooBig(Exception):
        pass

    def smt2(self, *extra):
        text = to_smt2(self, *extra)
        if len(text) > 4_000_000:
            raise TooBig(len(text))
        return text

    to_smt2 = counterexample.FactEncoding.smt2
    monkeypatch.setattr(counterexample.FactEncoding, "smt2", smt2)
    monkeypatch.setattr(counterexample, "BUDGET_S", 60.0)  # so the encoding completes on any machine
    (p,) = parse('policy p severity high:\n  always not once (count(tool(*) and status("error")) by name > 3)')
    bad = {33, 100, 225, 299}
    events = [Event(i, "tool", "fetch_url" if i in bad else ("search", "plan", "http_get")[i % 3],
                    "error" if i in bad else "success", {}) for i in range(300)]
    minimal_counterexample(compile_policy("p", p), events, 299)


# A5-3: one block on an append no longer ends the stream or enforcement
NO_DROP = 'policy no_drop severity critical:\n  always not tool("drop_db")'
RETRY_CAP = 'policy retry_cap severity medium:\n  always count(tool(*) and status("error")) by name <= 1'


async def _policy(db, org, source, action="block"):
    (p,) = parse(source)
    db.add(TracePolicy(org_id=org.id, name=p.name, source=source, severity=p.severity, action=action))
    await db.commit()


@pytest.fixture
def sdk_client(app_, api_key, agent, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.ASGITransport(app=app_)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    return AsyncAgentOpsClient(api_key=api_key, agent_id=agent.id, base_url="http://test")


async def test_guard_still_enforced_after_a_blocked_append(db, org, sdk_client):
    await _policy(db, org, NO_DROP)
    await _policy(db, org, RETRY_CAP)

    @guard
    async def flaky_fetch():
        raise TimeoutError("upstream")

    @guard
    async def drop_db():
        return "dropped"

    async with sdk_client.trace(stream=True) as t:
        for _ in range(2):  # the check sees status "success"; the recorded event is an error
            with pytest.raises(TimeoutError):
                await flaky_fetch()
        with pytest.raises(PolicyBlocked):
            await drop_db()
        t.add_event("llm", "answer")


async def test_blocked_append_is_surfaced(db, org, sdk_client):
    await _policy(db, org, RETRY_CAP)
    async with sdk_client.trace(stream=True) as t:
        t.add_event("tool", "fetch", status="error")
        t.add_event("tool", "fetch", status="error")
        t.add_event("llm", "answer")
    traces = (await db.execute(select(Trace).execution_options(populate_existing=True))).scalars().all()
    stored = (await db.execute(select(func.count()).select_from(TracePolicyViolation))).scalar_one()
    assert (stored, [t.meta.get("stream") for t in traces]) == (1, ["final"])


# A5-6: a deeply nested policy raises RecursionError in the parser, which the routes do not catch
@pytest.mark.xfail(strict=True, reason="A5-6: 1000 nested 'not' is a 500, not a validation error")
async def test_deeply_nested_policy_is_a_validation_error(client, auth_headers):
    source = "policy deep:\n  always " + "not " * 1000 + 'tool("a")'
    r = await client.post("/api/trace-policies/validate", headers=auth_headers, json={"source": source})
    assert r.status_code == 200 and r.json()["ok"] is False
