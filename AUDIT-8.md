# AUDIT-8: API, SDK and tests (round 3)

Audited at `53ea0df` (everything through PR #35 merged) by a session that wrote none of the code. Scope: API and SDK consistency, contracts and security (org scoping, auth, `/check`, REST and websocket ingest, streamed traces, check history, idle stream state, SDK `check`/`acheck`/`@guard`, buffering, `fail_closed`), and test quality, including a mutation probe of the round-2 fixes (PRs #24 to #35). Checker correctness, performance and checker security are AUDIT-7's, which ran in parallel. Known and accepted gaps from AUDIT-1 to AUDIT-6 and the PLAN.md results are not re-reported.

**Result: 0 critical, 1 high, 3 medium, 3 low**, plus A8-2, a medium AUDIT-7 also found (A7-5; it merged first). The high finding is PLAN.md sub-goal `SG-A8.1`; the other mediums are grouped into `SG-A8.2`, and A8-2 is fixed by AUDIT-7's `SG-A7.4`.

Failing tests: `backend/tests/audit/test_audit_8.py`, each `xfail(strict=True)` against the real app (the async SDK through `ASGITransport`). Each fails today at its final assertion, after its control assertions pass, on SQLite and on Postgres, and will XPASS, failing CI, once fixed, so the fixing sub-goal removes the marker. Run them with `cd backend && pytest tests/audit/test_audit_8.py -q --runxfail` (add `TEST_DB=postgres` for the Postgres run).

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A8-1 | high | Once a stream stops (idle close, event cap, 422, ...), checks are judged on the server's events only, and at the event cap `/check` answers 409, which the SDK allows: a guarded call after an undelivered secret read is allowed | SG-A8.1 |
| A8-2 | medium | The idle sweep closes a trace that is only slow (a tool call or human approval over an hour): its later events, output, status and cost are dropped for good (AUDIT-7's A7-5) | SG-A7.4 |
| A8-3 | medium | `@guard`'s per-trace lock follows the context, not the call: guarded calls gathered inside a guarded call skip it (A6-7 again), and a guarded call awaiting a task that makes its own guarded call deadlocks | SG-A8.2 |
| A8-4 | medium | `/ws/ingest` checks the API key once, at connect: an open socket keeps writing traces and events after the key is revoked | SG-A8.2 |
| A8-6 | medium | A streamed trace's `add_tool_call` history is invisible to its checks: never streamed, and no `prefix` sent | SG-A8.2 |
| A8-5 | low | `/check` answers `trace_too_long` with 409 where the append answers 413; `agent_not_found` is 400 on the append and 404 on `/check` | none (A8-5's 409 is fixed with A8-1) |
| A8-7 | low | A trace the idle sweep closes keeps `status: success` and `latency_ms: 0`, so crashed streamed agents count as successes in metrics and error-rate alerts | none |
| A8-8 | low | Two surviving mutants: the sweep's re-check of the score after taking the lock, and the version guard when the batch fills a live row | none |

## Method

- Read every route, service and task the round-2 PRs touched (`routers/trace_policies.py`, `traces.py`, `ws.py`, `evaluations.py`, `services/trace_service.py`, `trace_policy_service.py`, `report_service.py`, `tasks/policy_tasks.py`, the auth middleware) and the whole SDK, against PLAN.md's contracts and `sdk/README.md`.
- Ran the full backend suite on SQLite (1092 passed, 5 xfailed), the Postgres subset CI runs (`tests/api tests/audit tests/test_audit2.py`) and the SDK suite at `53ea0df`.
- Turned each suspicion into a test against the real app, with the real async SDK client where the SDK is involved. AUDIT-6's A6-3 test runs against the SDK's `FakeServer`, which answers `/check` with a canned 200; A8-1 is what the real server answers.
- Mutation probe: 31 one-line mutants of the round-2 fixes, replayed with `python backend/scripts/mutants_audit8.py` (see **Mutation probe**).

## High

### A8-1 A stopped stream's checks ignore the events the SDK still holds (SDK + API)

SG-A6.2 made a fail-open client keep checking a trace after the server rejected one of its appends: "later checks still go to the server and are judged on the events it holds" (`sdk/README.md`). The events the SDK added after that rejection are never sent, neither as appends (`_batch` returns `None` once `_dead`) nor as the check's `prefix` (`Trace._check_body` sends none for a streamed trace). So every guarded call after the stream stops is checked against a history that ends at the rejection:

- **Idle close (new in #35; SG-A7.4 removes this trigger).** An agent pauses for over an hour (a slow tool call, a human approval), the sweep closes the trace, the next push is `409 trace_closed` and the stream stops. With `tool("http_post") -> not once tool("read_secret")` as a `block` policy, `read_secret` added after the pause and then `acheck("http_post")` is allowed. Nothing else records the secret read (A8-2), so no batch violation follows either.
- **Event cap.** A long agent loop streams one event per push, so the server holds exactly `MAX_TRACE_EVENTS` when the next push is a 413. `/check` on that trace then raises `TraceTooLong`, which the route maps to a 409 (A8-5); the SDK reads a 409 as an outage and, by default, allows. So at the cap every guarded call is allowed even when the server holds the secret read. This is A6-3's own `trace_too_long` case: its test passes because `FakeServer` answers the check with 200.
- **Any other fatal rejection** (422 on an event payload, `agent_mismatch`, a legacy block on the trace text): the same as the idle case.

A `fail_closed` client fails these checks instead, so only the default is affected, but that default is what A6-3 (high) was about. Proposed fix (SDK, no contract change): a check of a stopped stream sends `events[_sent:]` as its `prefix` with the `trace_uid`, which `/check` already evaluates after the stored events (SG-A3.5). For the cap, `/check` should answer from the stored events plus prefix (it stores nothing; the cap only needs to bound the prefix) instead of refusing; the alternative is a 413 the SDK treats as not allowed.

Tests: `test_check_after_idle_close_sees_undelivered_events`, `test_check_at_the_event_cap_does_not_allow` (cap patched to 3).

## Medium

### A8-2 An hour's pause ends a live trace and drops the rest (API + SDK)

AUDIT-7 reports the same as A7-5 and proposes `SG-A7.4` (an append reopens an idle-closed trace), which also fixes this test; it is kept because it asserts the SDK end of it (the outcome fields and the events after the pause are stored).


`close_idle_streams` closes a streamed trace after `STREAM_IDLE_S` (1 h) without an append. That is meant for agents that crashed (A6-12), but an agent that is alive and slow looks the same: a long tool call, a queue wait, a human-in-the-loop confirmation (the DSL's own example is "no `delete_*` without a prior confirmation event"). Its next push is `409 trace_closed`, which the SDK treats as fatal: the events after the pause, and the final push's `output`, `status`, error, latency, tokens and cost, are logged once and dropped. The whole-trace upload is not used as a fallback (`_finish` sees a streamed trace), so the trace is stored as its pre-pause prefix, batch-checked as such, and the post-pause events are never checked by anything. Either an idle-closed trace should accept a late append (reopen it, or start a continuation), or the SDK should fall back to uploading the whole trace on `trace_closed`; both change the documented contract (`meta.stream = "idle"` is final).

Test: `test_events_after_an_idle_pause_are_stored`.

### A8-3 The guard lock follows the context, not the call (SDK)

SG-A6.4 serialises guarded coroutines per trace with `t._guard_alock` and lets a guarded call nested in another pass through by putting the trace in the `_held` context variable. Tasks copy the context when they are created, so:

- **Bypass.** Guarded calls started with `asyncio.gather` or `create_task` inside a guarded call inherit `_held` and skip the lock, so they are all checked against the same prefix, as A6-7 was. With `count(tool("delete_*")) <= 1` as a `block` policy, a guarded `cleanup()` that gathers two guarded `delete_user` calls runs both deletes.
- **Deadlock.** A task created before the guarded call (a worker reading a queue) does not inherit `_held`. A guarded call that hands it a job and awaits the result, where the job is itself a guarded call, waits on the lock its own caller holds, forever. This pattern worked before #35.

Holding the lock across the user's function is what makes both cases possible. Recording the event before the call runs and releasing the lock then (the alternative PLAN.md noted for A6-7) avoids both, at the cost of the event's status and latency.

Tests: `test_concurrent_guarded_calls_inside_a_guarded_call`, `test_guarded_call_awaiting_a_worker_finishes` (fails after a 2 s wait).

### A8-4 A revoked API key keeps its open websocket (API, security)

`/ws/ingest` authenticates once in `_authenticate_ws` and then serves the socket indefinitely; since #35 each message gets a fresh session, but the key is never re-read. Revoking a leaked key stops REST at once (401) while any socket opened with it keeps ingesting whole traces and events (alerts, incidents and live violations included) until it disconnects. A6-11 is the read side of the same gap on `/ws/traces`. Re-checking `revoked_at` per message (or on a short interval) closes it.

Test: `test_ws_ingest_stops_after_the_key_is_revoked`.

### A8-6 Streamed traces check without their tool calls (SDK)

`add_tool_call` on a `stream=True` trace is not streamed (only `add_event` pushes), and `_check_body` sends no `prefix` for a streamed trace, so its checks see no history at all, while the same calls on a non-streamed trace are sent as the prefix (SG-A3.5). A `read_secret` tool call followed by `acheck("http_post")` is blocked without `stream=True` and allowed with it. The whole trace is uploaded at exit and batch-checked, so the violation is found afterwards, but the block never happens. The SDK should send the tool calls as the prefix when a streamed trace has no events (or refuse `add_tool_call` with `stream=True`).

Test: `test_streamed_trace_checks_see_its_tool_calls`.

## Low

- **A8-5** `check_candidate` maps every `TraceConflict` to 409, so `TraceTooLong` (413 on the append and in `TraceTooLong.status`) is a 409 on `/check`, which the SDK reads as an outage (A8-1). Separately, an unknown `agent_id` is `400 agent_not_found` on the REST append and whole ingest but `404` on `/check`; the SDK handles both, but the contract table does not say which. Test: `test_check_and_append_agree_on_trace_too_long`.
- **A8-7** `close_idle_streams` sets only `meta.stream = "idle"`. A streamed trace's outcome fields arrive on `final`, so an idle-closed one keeps the defaults `status: success`, `latency_ms: 0`: the agent that crashed mid-trace (the case A6-12 was for) counts as a success in `/api/traces/metrics`, the dashboard and `error_rate` alert rules. `status` already documents `timeout` as a value. Repro: append one event without `final`, then run `close_idle_streams` past the idle time: the trace reads `status: success`, `latency_ms: 0`.
- **A8-8** Mutants the suite does not kill (replay with `python backend/scripts/mutants_audit8.py A8-8`):
  - `close_idle_streams` without the score re-check after taking the lock (`if score is None or score > cutoff`): `test_sweep_skips_a_trace_mid_append` re-adds the trace with a future score before the sweep lists it, so the case the re-check guards, an append landing between the listing and the lock, is never run.
  - `check_completed_trace` filling a pending live row without comparing `policy_version`: no test has a pending row of an older version at the seq where the current version's batch violation lands.

## Mutation probe

31 one-line mutants of the round-2 fixes, each applied to a temporary copy of the repo: SDK mutants against the whole SDK suite, backend mutants against `tests/api`, `tests/audit` and `tests/test_audit2.py`, every backend survivor then replayed against the whole backend suite.

- **Killed (29)**: SDK: 403 dropped from `CONFIG_ERRORS`; the buffer directory owner check, its `chmod`, the file's `0600`; both event-cap boundaries in `batches`; the sync guard lock; `fail_closed` on a stopped stream; a trace-text block read as `refused`; the refused-event count; the async flush ignoring `batches`. Backend: the sweep's monitor drop and busy-trace skip; stream tracking on refused and final appends; replayed violations on a block; `meta.stream` kept on metadata; PII masking of streamed text; archived policies enforced, listed, name-blocking and editable; pending rows filled at another seq; the org filter on summary evaluation runs and on eval diff; 413 on the append; the `/check` prefix cap; the formula depth and source size bounds (by `tests/policy`).
- **Survived (2)**: A8-8.

## Round-2 fixes re-checked

- **SG-A5.1, SG-A5.2, SG-A5.4 (#24, #25, #34)**: checker internals are AUDIT-7's; the API side (422 `invalid_policy` over 20 000 characters, 2 000 atoms or 64 deep; `ok: false` on validate) holds.
- **SG-A5.3 + SG-A6.2 (#27)**: a block 403 stores the violation and leaves the trace open; the SDK drops the refused event and goes on. Holds, except A8-1 for the other rejections.
- **SG-A6.3 (#26)**: summary and eval diff are org-scoped; both mutants killed.
- **SG-A6.1 (#29)**: `batches` and both flushers hold; `close()` is now tested.
- **SG-A6.5 (#30)**: archived policies are never enforced (live, batch, whole ingest, `/check`), keep their violations and free their name; all four mutants killed.
- **SG-A3.1, SG-A3.5 (#32, #33)**: trace fields, environment and the non-streamed prefix hold, except A8-6 (a streamed trace's tool calls).
- **SG-A6.4 (#28, #35)**: config errors on `/check` never allow; the buffer is private; idle streams close (but A8-2, A8-7); the guard lock serialises sibling calls (but A8-3); `/ws/ingest` sees other writers' changes.

## Not findings

- Org scoping: every route the round-2 PRs added or changed filters by the caller's org (`/check`, appends, the idle sweep's lock key, archived listing, reports, eval diff), and the Redis keys are per trace id or per org.
- Error shapes: new REST errors still go through the one `{"error": {code, message, details}}` handler; the websocket mirrors the codes.
- `fail_closed`: a `fail_closed` client fails every check whose answer A8-1 would get wrong; the gap is the default.
