# AUDIT-10: API, SDK and tests (round 4)

Audited at `6c28cfe` (everything through PR #43 merged) by a session that wrote none of the code. Scope: API and SDK consistency, contracts and security (org scoping, auth, `/check`, REST and websocket ingest, streamed traces, check history, idle close and reopen, `timeout` status, the new 413 `policy_work_too_large` and 422 `policy_limit`, SDK `check`/`acheck`/`@guard` locking and event order, buffering, `fail_closed`), and test quality, including a mutation probe of the round-3 fixes (PRs #39, #41, #42, #43). Checker correctness, performance and checker security are AUDIT-9's, which ran in parallel. Known and accepted gaps from AUDIT-1 to AUDIT-8 and the PLAN.md results are not re-reported.

**Result: 0 critical, 2 high, 2 medium, 4 low**, plus A10-9, a low AUDIT-9 also found (A9-5; it merged first). Both high findings are in SG-A7.3's work bound (#42): it refuses what the SDK sends in normal use, under policy sets the API accepts (A10-1), and it does not bound a bulk upload (A10-2). AUDIT-9's critical A9-1 (the bound counts nodes, not globs) changes the same bound, so their fixes should be made together. They are PLAN.md sub-goals `SG-A10.1` and `SG-A10.2`; the mediums are grouped into `SG-A10.3`.

Failing tests: `backend/tests/audit/test_audit_10.py` (real app, the async SDK through `ASGITransport`) and `sdk/tests/test_audit_10.py` (the SDK's `FakeServer`), each `xfail(strict=True)`. Each fails today at its final assertion, after its control assertions pass (the backend ones on SQLite and on Postgres), and will XPASS, failing CI, once fixed, so the fixing sub-goal removes the marker. Run them with `cd backend && pytest tests/audit/test_audit_10.py -q --runxfail` (add `TEST_DB=postgres` for Postgres) and `cd sdk && pytest tests/test_audit_10.py -q --runxfail`.

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A10-1 | high | The per-request work bound refuses requests the SDK makes in normal use: past `MAX_REQUEST_WORK` / nodes events (500 at the org cap of 4 000 nodes), every guarded call of an unstreamed trace is blocked whatever `fail_closed`, the trace's upload is dropped, and a streamed backlog stops the stream | SG-A10.1 |
| A10-2 | high | `/ingest/bulk` bounds each trace's work, not the request's: one bulk request did 20 x `MAX_REQUEST_WORK` (22 s), and 8 at once made an unrelated `/check` take 8.5 s, past the SDK's 2 s check timeout | SG-A10.2 |
| A10-3 | medium | While a guarded call runs, no event added after it is streamed, by any task: a long tool call or approval holds back live violations and alerts for the whole trace (new in #41) | SG-A10.3 |
| A10-4 | medium | Sync and async guarded calls of one trace take different locks, so a sync guarded call in a thread and an async one are checked against the same history (A6-7 again, since #35) | SG-A10.3 |
| A10-5 | low | The SDK blocks on any 413 from `/check`, including a proxy's request-body limit, not only `policy_work_too_large` | none |
| A10-6 | low | Mutant replay scripts have drifted: 5 rows of `mutants_audit4/6/8.py` no longer match the code, so those scripts exit 1 and replay nothing for those rows | none |
| A10-7 | low | Surviving mutants of the round-3 fixes (see **Mutation probe**) | none |
| A10-8 | low | `test_check_does_not_stall_the_event_loop` is a wall-clock test that failed in a full-suite run on a busy 4-core machine | none |
| A10-9 | low | The batch check stores without the append lock when the lock is busy for 10 s, not only when Redis is down as its comment says (AUDIT-9's A9-5) | none |

## Method

- Read every route, service and SDK path the round-3 PRs touched (`routers/trace_policies.py`, `traces.py`, `ws.py`, `services/trace_service.py`, `trace_policy_service.py`, `sdk/agentops_sdk/{trace,guard,precheck}.py`) against PLAN.md's contracts, `docs/policy-dsl.md` and `sdk/README.md`, and the whole SDK delivery path (`client.py`, `async_client.py`, `utils.batches`, `buffer_rejected`).
- Ran the full backend suite on SQLite (1154 passed, 5 xfailed, 1 failed: A10-8), the AUDIT-10 tests on a local Postgres 16, and the SDK suite (142 passed) at `6c28cfe`.
- Turned each suspicion into a test against the real app, with the real async SDK client where the SDK delivers or checks; SDK-only concurrency through `FakeServer`.
- Measured A10-2 with the real constants: four `block` policies of 3 984 nodes in all (under `MAX_ORG_POLICY_NODES`), bulk requests of 20 traces x 499 events (under the 10 000-event request cap, each trace under `MAX_REQUEST_WORK`).
- Mutation probe: 30 one-line mutants of the round-3 API and SDK fixes, replayed with `python backend/scripts/mutants_audit10.py` (see **Mutation probe**).

## High

### A10-1 The work bound refuses what the SDK sends in normal use (API + SDK)

SG-A7.3 (#42) refuses a request whose policy work, events x monitor nodes of the policies it runs, is over `MAX_REQUEST_WORK` = 2 000 000, with 413 `policy_work_too_large`, and the SDK treats a 413 on `/check` as a configuration error and blocks. The org caps it added allow 4 000 nodes, and a trace may hold `MAX_TRACE_EVENTS` = 10 000 events: 20 times the bound. So for any policy set over 200 nodes, which the API accepts, the SDK's ordinary requests start failing once a trace is long enough (500 events at 4 000 nodes, 1 998 at 1 001), with nothing the agent can do about it:

- **Every guarded call of an unstreamed trace is blocked.** An unstreamed trace (the default) sends its whole history as the check's `prefix` (SG-A3.5), so past the threshold every `check`, `acheck` and `@guard` call is a 413 and blocked, whatever `fail_closed`, while no policy is violated. Before #42 these were answered.
- **The trace itself is lost.** Its upload counts only `block` policies, but past the same threshold `/ingest/bulk` rejects it with `policy_work_too_large`, which `buffer_rejected` logs as "dropped" and never retries. The observability record of the agent's longest runs is gone.
- **A streamed trace stops streaming after a backlog.** The SDK pushes all pending events as one append; when they pile up (a slow server, a `retry` outage, a burst), that append is a 413, which `push_outcome` reads as fatal: the stream stops, and every later check sends the undelivered events as its prefix (SG-A8.1), so it is a 413 too and blocked. The rest of the trace is never stored.

The SDK cannot avoid any of these: it does not know the org's node count, and a check's prefix cannot be split. Proposed fix (a contract change for the fixing thread to approve): either make the caps consistent (an org's nodes x `MAX_TRACE_EVENTS` within the per-request bound, i.e. at most 200 nodes, or a bound sized for that product off the event loop with its own concurrency limit), or bound only what the client can split and say how: the append and bulk 413s carry the largest event count that fits (`max_events`) and the SDK splits by it, while `/check` and a single upload are always answered.

Tests: `test_long_unstreamed_trace_checks_are_answered`, `test_long_unstreamed_trace_is_stored`, `test_streamed_backlog_is_delivered` (one 1 001-node policy created through the API; 1 000 events work, 2 000 do not).

### A10-2 A bulk upload's policy work is not bounded (API, availability)

`ingest_bulk` calls `ingest_trace` per trace, and `_bound_work` checks each trace against `MAX_REQUEST_WORK` on its own, so one request does up to 10 000 events x 4 000 nodes = 40 000 000 steps, 20 times the bound SG-A7.3 set at "about a second". Measured here: one bulk request of 20 traces x 499 events against 3 984 `block` nodes took 22 s (one such trace alone: 1.1 s). The work runs in `asyncio.to_thread`, so the event loop is not blocked outright, but it holds the default executor and the GIL: with 8 such requests in flight, a one-event `/check` from the same server took 8.5 s, four times the SDK's default `check_timeout_s`, so every guarded call of every org on that worker fails open (or is blocked, with `fail_closed`). It needs no adversary: the SDK's flusher packs up to 10 000 events into one bulk request (SG-A6.1), so an org with large `block` policies does this in normal use. Fix: count the work across a bulk request's traces and reject the traces past the bound with the same reason (or 413 the request), and consider a dedicated, bounded executor for policy work so one org's uploads cannot starve every org's checks.

Test: `test_bulk_upload_work_is_bounded_per_request` (bound patched to 1 000; three traces of 800 steps each are all accepted).

## Medium

### A10-3 A running guarded call holds back the stream (SDK)

Since SG-A8.2 (#41) `@guard` records a call's event when it starts, and background pushes stop short of a running call's event (`_batch(hold=True)`) so its outcome can be set at the end. A push sends events in order, so every event added after it, by any task of the trace, waits too: while `wait_for_approval()` (or a long tool call, a queue wait) runs, a `read_secret` and an `http_post` added by another task are not streamed, so their live violations, alerts and `/ws/traces` updates wait for the guarded call to end (and if it never does, they are sent only by the next check or the trace's exit). Before #41 those events streamed at once. A check still flushes them, so blocking decisions are not affected. Seqs are contiguous, so a push cannot skip the running event. Either push it at once, as a check already does (its status and latency are then lost, the cost SG-A8.2 accepted for overlapping calls), or let an append update an earlier event's outcome (a contract change).

Test: `sdk/tests/test_audit_10.py::test_events_after_a_running_guarded_call_are_streamed`.

### A10-4 Sync and async guarded calls are not checked in turn (SDK)

The async guard serialises on `t._guard_alock` (an `asyncio.Lock`) and the sync guard on `t._guard_lock` (a `threading.RLock`). An async agent that runs a sync tool in a thread (`asyncio.to_thread`, a framework's executor) while an async guarded tool runs has two guarded calls of one trace checked at once, each against a history without the other: with `count(tool("delete_*")) <= 1` as a `block` policy, both deletes run. This is the A6-7 race for mixed tools; SG-A6.4 (#35) added the two locks and no test mixes them. Fix: one lock per trace for both, e.g. the threading lock taken by the async guard through `asyncio.to_thread` (or a non-blocking acquire loop), held for check and record only as now.

Test: `sdk/tests/test_audit_10.py::test_sync_and_async_guarded_calls_are_checked_in_turn` (both checks are sent with an empty prefix).

## Low

- **A10-5** `CONFIG_ERRORS` now includes 413 for `policy_work_too_large`, but `check_result` matches the status only. A reverse proxy's request-body limit (nginx's default is 1 MB, about 8 000 prefix events) answers 413 too, and since #42 the SDK blocks on it whatever `fail_closed`, where before it failed per `fail_closed`. Matching the error code (`policy_work_too_large`) keeps the documented behaviour without that side effect.
- **A10-6** `mutants_audit4.py` (A4-1), `mutants_audit6.py` (A6-16) and three `probe` rows of `mutants_audit8.py` (`CONFIG_ERRORS` and the `_end` check changed in #41/#42; `raise HTTPException(e.status, detail=` now occurs twice in `traces.py` since #42) no longer find their text exactly once, so each script reports STALE and exits 1, and those mutants are not replayed. The fixing PRs changed the lines without updating the rows. `mutants_audit10.py` covers the changed lines again.
- **A10-7** Mutants of the round-3 fixes the suite does not kill: see **Mutation probe**.
- **A10-8** `tests/audit/test_audit_7.py::test_check_does_not_stall_the_event_loop` asserts a loop stall under 0.5 s while a 10 000-event `/check` body is parsed (on the loop) and refused. It measured 0.09 s alone but failed in a full-suite run while other test processes shared the 4 cores. CI runners are smaller; a relative bound (as `test_work_within_the_bound_runs_off_the_event_loop` uses) would hold.
- **A10-9** (AUDIT-9's A9-5, found independently) `check_completed_trace` takes the trace's append lock with `blocking_timeout=10`; `acquire()` returns `False` when it times out, and the batch then stores without the lock, though the comment says that only happens "without Redis". An append can hold the lock that long (a span rebuild after a Redis loss). The effect is small: a first violation found on a prefix is the trace's first violation too, so the rows are right; only SG-A7.4's "stores nothing if reopened meanwhile" can be missed. Retrying later, as the busy-trace case of the sweep does, would match the comment.

## Mutation probe

30 one-line mutants of the round-3 API and SDK fixes, each applied to a temporary copy of the repo: SDK mutants against the whole SDK suite, backend mutants against `tests/api`, `tests/audit` and `tests/test_audit2.py`, every backend survivor then replayed against the whole backend suite. AUDIT-10's own xfail tests are left out (a mutant that makes one XPASS is not a kill), and so is A10-8's wall-clock test, which failed the unmutated baseline twice while three replays shared the CPU.

- **Killed (22)**: SDK: 413 dropped from `CONFIG_ERRORS`; the stopped-stream prefix off by one; `_end` never pushing the outcome; `_running` never cleared. Backend: each of the three `_bound_work` calls dropped, the bound doubled, the `/check` prefix counted without the candidate; `/check` and bulk losing the conflict's status and reason; the org policy count and node caps off by one, archived policies counted, a source update counting itself; the append not reopening an idle trace (two ways); the batch storing after a reopen; the batch lock never taken; the sweep never re-tracking, re-tracking idle instead of open traces, overwriting live scores (`nx`), or leaving `latency_ms` 0.
- **Survived (8)**, `python backend/scripts/mutants_audit10.py A10-7`:
  - SDK, the guard's hold (#41): `_batch` ignoring `hold`, either background push (sync or async) sent without `hold`, the guard recording its event as not running, `_sending` never set, and `_end` ignoring `_sending`. No test adds an event, or lets a background push run, while a streamed guarded call is in progress, so the machinery that keeps a running call's outcome is untested end to end (A10-3's test is the first that exercises it, and it asserts the opposite).
  - Backend: the whole-trace message on `/ws/ingest` answering a `TraceConflict` (e.g. `policy_work_too_large`) with the generic `str(e)` instead of its code and details: no websocket test sends a trace over the bound.
  - Backend: the re-tracking scan not capping a span's (client-supplied) `started_at` at now: no test re-tracks a trace whose last event is in the future, which the sweep would then never close.

## Round-3 fixes re-checked

- **SG-A8.1 (#39)**: a check of a stopped stream sends `events[_sent:]` as the prefix; `/check` answers a trace at the event cap. Holds, except that such a prefix is subject to A10-1's 413.
- **SG-A8.2 (#41)**: guarded calls gathered, nested or in worker tasks take the lock in turn and none deadlock (A8-3's tests pass); a revoked key closes `/ws/ingest` with 4401 before the rate limit; a streamed trace with tool calls sends them as the prefix; the sweep sets `timeout` and the latency to the last append. Holds, except A10-3 (the cost of recording at the start reaches other tasks' events) and A10-4 (mixed sync and async guards).
- **SG-A7.3 (#42), API side**: `/check`, appends and uploads over the bound are 413 and store nothing; the org caps are 422 `policy_limit`, counted under the org row lock, archived policies excluded, a source update excluding itself. The bound is what A10-1 and A10-2 are about.
- **SG-A7.4 (#43)**: an append (REST or websocket, accepted or refused by a block) reopens an idle trace; `final` traces stay closed (409); a reopened trace's later violations are stored live and by its next batch check, which stores nothing twice; the sweep re-tracks open traces after a Redis loss. Holds, except A10-9.

## Not findings

- Org scoping: every query the round-3 PRs added filters by org (`_check_org_limits`, the reopen lookup, the batch check's lock key, the re-tracking scan feeds only the sweep, which re-reads each trace). An append to another agent's idle trace is `agent_mismatch` (409) and does not reopen it.
- Error shapes: `policy_work_too_large` and `policy_limit` go through the one `{"error": {code, message, details}}` handler on REST, the websocket mirrors the code, and `/ingest/bulk` reports it per trace; the policy editor shows the message through the generic `errorMessage`.
- The websocket's per-message key check reads only `revoked_at` by the key's hash; a deleted key is treated as revoked.
