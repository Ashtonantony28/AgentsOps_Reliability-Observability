# AgentOps SDK (Python)

Instrument any Python AI agent in 3 lines:

```python
import agentops

agentops.init(api_key="aop_...", agent_id="agent-uuid", environment="prod",
              base_url="http://localhost:8000")

with agentops.trace(input=user_input) as t:
    result = my_agent.run(user_input)
    t.set_output(result, tokens_input=420, tokens_output=85, cost_usd=0.0021)
    t.add_tool_call(name="web_search", input={"q": user_input}, output={"hits": 5}, latency_ms=230)
```

## Async usage

```python
import agentops
agentops.init(api_key="aop_...", agent_id="agent-uuid")

async with agentops.trace(input=q) as t:
    result = await my_agent.arun(q)
    t.set_output(result)
```

## Events and policy pre-checks

Record typed events instead of tool calls (a trace holds one or the other, never both):

```python
with agentops.trace(input=q) as t:
    t.add_event("retrieval", "kb_search", input={"q": q}, output={"hits": 3}, index="faq")
    t.add_event("llm", "answer")
```

`kind` is one of `tool`, `llm`, `retrieval`, `confirmation`, `custom`; extra keyword arguments become the
event's `attrs` (`latency_ms=` sets the event latency). Events get `seq` 0, 1, 2, ... in the order added.

Ask the server whether an action would violate a trace policy before doing it:

```python
r = agentops.check("issue_refund", input={"amount": 120}, tier="gold")   # or: await agentops.acheck(...)
if not r.allowed:
    print(r.violations)          # ViolationOut dicts (policy_name, severity, message, ...)
```

`violations` lists every enabled policy the action would violate; `allowed` is `False` only when one of them
has action `block`, i.e. exactly when the server would reject the event with a 403. A legacy block rule
(`forbid_tool`, PII, injection) the action breaks is listed too, with the rule's id and name and severity `high`.

Or guard a function (sync or async). The check runs first and raises `agentops.PolicyBlocked` (with
`.result`) instead of calling the function; inside an active trace the call is then recorded with
`add_event` (status `success` / `error`). Function arguments are never sent.

```python
@agentops.guard                              # or @agentops.guard(name="refund", kind="tool", tier="gold")
def issue_refund(order_id, amount): ...
```

**History-aware checks need streaming.** `t.check(...)` / `await t.acheck(...)` evaluate the candidate after the
events the server already holds for that trace. With `agentops.trace(stream=True)` every `add_event` is sent at
once to `/api/traces/{trace_id}/events` (the trace is closed with `final=true` on exit), so the server sees the
whole prefix. Without `stream=True` the server has none of the trace yet, and checks see an empty history.
With `AsyncAgentOpsClient`, use `await t.acheck(...)` so pending events are pushed before the check.

Streaming retries on network errors, 5xx, 429 and `409 trace_busy` (the batch stays pending and goes with the
next push). A `409 seq_conflict` whose `expected_seq` shows the server already stored part of the batch (a reply
lost after commit) resyncs and sends the rest. While any added event is not on the server, a check does not
call the server and follows the failure policy below (`error="trace events not delivered"`). A `403 policy_block`
refuses the event a `block` policy forbids: it is dropped from `t.events`, later events are renumbered, and
streaming goes on (a refused batch of several events is resent one event at a time to find it). Any other
rejection (`409 seq_conflict`, `413 trace_too_long`, `422`, ...) drops that batch, logs a warning and stops
streaming that trace; later checks still go to the server and are judged on the events it holds
(with `fail_closed=True` they fail instead). The first push carries the trace's `environment`, `input`,
`session_id`, `agent_version` and metadata, and the final one its `output`, `status`, error, latency, tokens, cost
and metadata, so a streamed trace is stored, scoped and scanned like a whole one; the environment is fixed by the
first push (a different one later is `409 environment_mismatch`). Checks send the trace's environment too.
A streamed trace that never adds an event is sent the normal way.

**Failure policy.** A check that cannot complete (network error, timeout, non-2xx, malformed reply) logs a
warning and returns `CheckResult(allowed=not fail_closed, violations=[], error="<reason>")`. The default is
fail-open; pass `fail_closed=True` to `init()` / the client to block instead. `check_timeout_s` (default 2.0)
bounds each check.

```python
agentops.init(api_key="aop_...", agent_id="agent-uuid", fail_closed=True, check_timeout_s=1.0)
```

## Features

- Context manager (sync + async)
- Auto-capture exceptions → marks trace status = error
- Background batched flush (configurable interval, batch size)
- Local disk buffer if the API is unreachable; auto-replay on next start
- Bulk ingestion uses `/api/traces/ingest/bulk` for efficiency

## Install

```bash
pip install -e ./sdk
```

## Demo

`python sdk/demo.py` runs against a local API at `http://localhost:8000`.
