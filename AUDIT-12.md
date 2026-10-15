# AUDIT-12: API, SDK and tests (round 5)

Audited at `18a721c` (everything through PR #49 merged) by a session that wrote none of the code. Scope: API and SDK contracts (`/check` and its prefix checkpoints, event appends and the 413 `max_events` split, `/ingest/bulk` and its shared work bound, the 422 `policy_limit` in cost units, the batch check's busy retry), the Python SDK (`check`/`acheck`/`@guard` and the shared guard lock, streaming, bulk resend, `fail_closed`), and test quality, including a mutation probe of the round-4 fixes (PRs #45, #47, #48, #49). Checker correctness and security are AUDIT-11's, which ran in parallel. Known and accepted gaps from AUDIT-1 to AUDIT-10 and the PLAN.md results are not re-reported.

**Result: 0 critical, 1 high, 1 medium, 3 low.** The high finding is in SG-A10.1's prefix checkpoints (#48): a check saves its checkpoint only once it has stepped the whole prefix, so checks that overlap an earlier one of the same history all start over, and past about 1 000 events at the org cap every check of an unstreamed trace overlaps the last. It is PLAN.md sub-goal `SG-A12.1`; the medium is `SG-A12.2`.

Failing tests: `backend/tests/audit/test_audit_12.py` (real app) and `sdk/tests/test_audit_12.py` (the SDK's `FakeServer`), each `xfail(strict=True)`. Each fails today at its final assertion, after its control assertions pass (the backend one on SQLite and on Postgres 16), and will XPASS, failing CI, once fixed, so the fixing sub-goal removes the marker. Run them with `cd backend && pytest tests/audit/test_audit_12.py -q --runxfail` (add `TEST_DB=postgres` for Postgres) and `cd sdk && pytest tests/test_audit_12.py -q --runxfail`.

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A12-1 | high | A long `/check` prefix saves its checkpoint only at the end, so a check that overlaps an earlier one of the same history steps it all again, and the earlier one keeps running after the SDK gave up on it: at the org cap, 8 checks of a 4 000-event trace a second apart took 49 to 65 s each, and an unrelated append went from 1.2 to 10.2 s on the shared two-thread pool | SG-A12.1 |
| A12-2 | medium | The async guard's per-trace `asyncio.Lock` binds to the first event loop it waits in: a trace whose async guarded calls run under `asyncio.run` per step raises `RuntimeError` from every contended guarded call after the first step | SG-A12.2 |
| A12-3 | low | Docs and README drift from the round-4 contracts: the limits table still counts "monitor nodes", and the README still lists a `/check` 413 the server no longer sends | none |
| A12-4 | low | An unstreamed trace past 10 000 events gets a 422 on every check, so every guarded call is blocked whatever `fail_closed`; a streamed trace at the cap is answered | none |
| A12-5 | low | Surviving mutants of the round-4 fixes, three of them in code that keeps checkpointed verdicts right (see **Mutation probe**) | none |

## Method

- Read every route, service and SDK path PRs #45, #47, #48 and #49 touched (`routers/{traces,trace_policies,ws}.py`, `services/{trace_service,trace_policy_service}.py`, `tasks/policy_tasks.py`, `sdk/agentops_sdk/{trace,guard,precheck,utils,client,async_client}.py`) against PLAN.md's contract entries, `docs/policy-dsl.md` and `sdk/README.md`.
- Ran the full backend suite on SQLite (1188 passed, 4 skipped, 5 xfailed), the AUDIT-12 backend test on a local Postgres 16, and the SDK suite with branch coverage (154 passed, 95%; the async guard's wait for the shared lock, `guard.py:44-45`, is never run).
- Measured A12-1 through the app in process with four `alert` policies of 999 cost units each (3 996 in all, under `MAX_ORG_POLICY_NODES`), unstreamed-style checks whose prefix grows by one event per check, as the SDK sends them.
- Turned each finding above low into a failing test.
- Mutation probe: 28 one-line mutants of the round-4 API and SDK fixes, replayed with `python backend/scripts/mutants_audit12.py` (see **Mutation probe**).

## High

### A12-1 Overlapping checks of a long prefix all start over (API + SDK, availability)

Since SG-A10.1 (#48) `/check` is always answered and a long prefix resumes from a checkpoint: `prefix_monitor` digests the prefix every `k` = `WORK_SLICE` / cost events, restores the latest digest Redis holds, steps the rest in slices on `trace_policy_service._pool` (two threads for the whole process), and then saves one checkpoint, for the last digest, once it has stepped everything. An unstreamed trace (the default) sends its whole history with every check, so the design relies on each check finishing before the next one starts. It doesn't, in two ways:

- **The SDK gives up first.** Stepping costs about 0.5 us per cost unit per event (measured here: 16 M units in 7.9 s), so a check from scratch passes the SDK's `check_timeout_s` (2 s) once events x cost is over about 4 M: 1 000 events at the org cap of 4 000 units, 4 000 at a quarter of it. The SDK then fails open (or blocks, with `fail_closed`) and the agent goes on to its next guarded call, one event longer. The server keeps working on the abandoned check (a request is not cancelled when its client goes away), and the next check finds no checkpoint yet, so it starts over too.
- **Concurrent tasks overlap anyway.** An agent whose tasks make guarded calls faster than one check takes is in the same position without any timeout.

Each overlapping check steps the whole prefix, all of them through the same two threads, so they slow each other down and none finishes in time; the pile drains only once checks stop arriving. Measured through the app in process (the test app, SQLite) with 3 996 units: one 4 000-event check alone took 7.9 s; 8 checks of the same growing history started a second apart took 49, 61, 64, 65, 65, 65, 64 and 63 s (none could resume: no checkpoint existed until the first had finished); once they had all finished, the next one took 0.1 s. Meanwhile a 400-event append of another trace (1.6 M units, so sliced on the same pool) took 10.2 s instead of 1.2 s, past the SDK's 10 s append timeout, and so would any org's long append, rebuild after a Redis loss, or upload's `block` check on that worker: the pool is shared by every org, so this is also a cross-tenant slowdown, caused by ordinary use of a policy set the API accepts. That cross-org side is AUDIT-11's A11-1 (SG-A11.1), found in parallel; what is new here is that one agent's checks pile up on their own, because none can resume from another. For the agent itself every guarded call in that window goes unchecked (fail-open) or is refused (fail-closed), though nothing is violated. SG-A10.2's open low ("the first check of a long unstreamed trace at the org caps (~20 s) still times out in the SDK, and the next one resumes from its checkpoints") holds only when the next check starts after the first has finished.

Fix direction: save each digest's checkpoint as soon as its slice is stepped, and look for a newer one before each slice (so a later check resumes from what an earlier, still running or abandoned, one has done); or run one job per digest at a time and let later checks wait for it; and stop a check's work when its request is gone or past a deadline near the SDK's timeout. A per-org share of `_pool` (SG-A11.1) bounds the damage to other orgs but not this pile-up.

Test: `test_check_resumes_from_an_earlier_check_still_running` (one 1 001-unit `block` policy, 2 000-event prefix; the first check is paused in its pool thread once it has stepped half the prefix, and a second identical check then steps all 2 000 events again).

## Medium

### A12-2 The async guard lock is bound to the first event loop (SDK)

SG-A10.3 (#47) has async guarded calls of a trace queue on `Trace._guard_alock`, an `asyncio.Lock` created on first use, before they share the threading lock with sync calls. An `asyncio.Lock` binds to the running loop the first time a task has to wait for it, and raises `RuntimeError: ... is bound to a different event loop` when waited on from another loop. A sync agent that runs its async tools with `asyncio.run(...)` per step (common with sync frameworks calling async tools) keeps one trace across several loops: the first step's guarded calls work, and from the second step on every guarded call that has to wait for another (two gathered tools, a tool and a background task) raises `RuntimeError` instead of running, so the tool call fails. `Trace._alock`, the push lock of the async client, is created the same way. Fix: create the asyncio lock per loop (e.g. keyed by the running loop, or recreated when the loop changes), or wait for the threading lock without an asyncio primitive.

Test: `sdk/tests/test_audit_12.py::test_guarded_calls_of_one_trace_in_two_event_loops` (two gathered guarded calls per `asyncio.run`, twice in one trace).

## Low

- **A12-3** `docs/policy-dsl.md`'s limits table says the org cap is 4 000 "monitor nodes" and that the work bounds are "events x nodes", but since SG-A9.1 (#45) both count `Plan.cost` (nodes plus glob costs), as the 422's `nodes` field does: a policy with a 2 400-glob list is 2 401 units, not 2 nodes, and nothing in the docs says so. `sdk/README.md` still lists "`413 policy_work_too_large`" among the `/check` replies that block whatever `fail_closed`, though since SG-A10.1 (#48) `/check` is never refused for work; the code path is harmless, the sentence misleads.
- **A12-4** `PolicyCheckIn.prefix` holds at most `MAX_TRACE_EVENTS` (10 000) events, and an unstreamed trace sends its whole history as the prefix, so from its 10 002nd event (`add_event` has no cap) every `check`, `acheck` and `@guard` call is a 422, which the SDK treats as a configuration error and blocks, whatever `fail_closed`. Its upload is already dropped by `batches()`, so the trace is lost either way, but a streamed trace at the cap is still checked (SG-A8.1) while an unstreamed one blocks every tool call. Repro: an unstreamed trace with 10 001 events, then `t.check("x")` returns `CheckResult(allowed=False, error="http 422")` with no policy at all.
- **A12-5** Mutants of the round-4 fixes the suite does not kill: see **Mutation probe**.

## Mutation probe

28 one-line mutants of the round-4 API and SDK fixes, each applied to a temporary copy of the repo: SDK mutants against the whole SDK suite, backend mutants against `tests/api`, `tests/audit` and `tests/test_audit2.py`. AUDIT-12's own xfail tests are left out (a mutant that makes one XPASS is not a kill).

- **Killed (18)**: SDK: the bulk resend dropped (sync client), refused traces never resent, an all-refused reply resent anyway; the append split ignoring `max_events` (`pending[:self._max_batch]`, the split push stopping after one append, a 413 `max_events` read as fatal). Backend: the bulk request's shared bound (`room` never charged, or `None`), an upload's `block` check charged nothing, the append's one-event exception and `max_events` off by one, checkpoints never used, never saved, or the prefix tail not stepped, `TraceBusy` not raised and the busy task not retried, and the org cap ignoring the new policy's or the stored policies' cost.
- **Survived (10)**, `python backend/scripts/mutants_audit12.py A12-8` (backend survivors replayed against the whole backend suite too; in one of those replays, run three at a time, `tests/policy/test_counterexample.py::test_sufficient_and_minimal_large_bound_counts` failed under the attrs mutant, which can't reach it, and the mutant survived a replay on its own):
  - Checkpoint keys and restores (#48): the digest leaving out an event's `status`, or its `attrs`; the seed leaving out the stored trace and its next seq; a restore expecting `j * k` events instead of `next_seq + j * k`; restoring the earliest checkpoint instead of the latest. The first three make a check restore a state from other events and answer it wrongly (a `status == "error"` policy, an attr-matching one, or another stored trace with the same next seq); no test checks two prefixes that differ only in status or attrs, or a stored trace with a prefix. The last two only lose the resume.
  - `run_sliced` never slicing (#48): every long job runs whole on the default executor; no test checks that long work goes through `_pool` in slices.
  - SDK: the async client's resend of refused traces dropped (only the sync client has a resend test), and the async guard going ahead after one poll of a lock a sync guarded call holds (`guard.py:44-45` never runs: no test has an async guarded call wait for a sync one).
  - AUDIT-10's backend A10-7 rows still survive: `/ws/ingest` answering a whole-trace `TraceConflict` with `str(e)`, and the re-tracking scan not capping `started_at` at now.

## Round-4 fixes re-checked

- **SG-A9.1 (#45), API side**: the org cap and the 422 `policy_limit` count `Plan.cost`, under the org row lock, archived policies excluded, a source update excluding itself; the append, upload and check work counts use the same cost. Holds (docs drift in A12-3).
- **SG-A10.3 (#47)**: a background push holds back a running guarded call only while it is the trace's last event; sync and async guarded calls share one threading lock and are checked in turn (A10-4's test passes), and a sync call on the loop's own thread goes ahead; a proxy's 413 on `/check` follows `fail_closed`. Holds, except A12-2 (the asyncio half of the lock) and the open medium PLAN.md records (an outcome lost when another event follows a running call).
- **SG-A10.1 and SG-A10.2 (#48)**: `/check` is answered at any prefix length and a sequential re-check resumes from its checkpoint, which agrees with a fresh check; an append over the bound is a 413 with `max_events` and the SDK splits by it (`_max_batch`, both clients through `_settle`); a one-event append is always taken; a bulk request's uploads share one bound, a trace over it alone is stored and batch-checked, and the SDK resends the refused traces at once, buffering them only if a reply refused them all. Holds, except A12-1 (overlapping checks).
- **SG-A9.2 (#49), API side**: a batch check that waits out the append lock with Redis up raises `TraceBusy`, stores nothing and is retried by its Celery task. Holds.

## Not findings

- Org scoping: prefix checkpoints are keyed by the org, the stored trace and its next seq, the policy ids and plan digests, so no org (or trace) can restore another's state; `/ingest/bulk`'s shared bound is per request.
- The SDK's bulk resend always makes progress: the server takes the first trace of a request (it fits the full bound, or is stored unchecked when over it alone), so each resend is shorter than the request before it.
- Error shapes: `policy_work_too_large` keeps `work` and `max_work` on every path and adds `max_events` only on appends; the SDK reads `max_events` only with that code, so a `413 trace_too_long` (which carries `max_events` too) still stops the stream.
- A sync `check()` in a worker thread holding the trace's push lock while an event is added on the loop stalls the loop for the push (measured 0.45 s with a 0.5 s server); already recorded as PR #34's open low.
