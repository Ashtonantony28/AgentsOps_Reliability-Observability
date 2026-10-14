# AUDIT-6: API, SDK and test quality (round 2)

Audited at `eb7e255` (everything through PR #21 merged) by a session that wrote none of the code. Scope: API and SDK consistency (routes, schemas, error and fail-open behaviour, sync vs async SDK parity, UI and report contracts, security of the new endpoints) and test quality (coverage CI from PR #21, mutation survivors, weak tests, Postgres vs SQLite gaps), plus a re-check of the round-1 fixes in PRs #17, #18, #20 and #21. Checker correctness and latency are AUDIT-5's.

**Result: 0 critical, 4 high, 6 medium, 6 low.** The high findings are PLAN.md sub-goals `SG-A6.1` to `SG-A6.3`; the mediums are grouped into `SG-A6.4`.

Failing tests: `backend/tests/audit/test_audit_6.py` and `sdk/tests/test_audit_6.py`, each `xfail(strict=True)`. Each fails today at its final assertion (the control assertions before it pass) and will XPASS, failing CI, once fixed, so the fixing sub-goal removes the marker. CI also runs `tests/audit` on Postgres, so every backend test fails on both databases, except A6-8 and A6-9, whose markers apply to one database each (see there). Run them with `cd backend && pytest tests/audit/test_audit_6.py -q --runxfail` (add `TEST_DB=postgres` for the Postgres run) and `cd sdk && pytest tests/test_audit_6.py -q --runxfail`.

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A6-1 | high | `AgentOpsClient.close()` (and the `atexit` hook) sends one batch and silently drops the rest of the queue | SG-A6.1 |
| A6-2 | high | The SDK batches by trace count only; a batch over the server's 10 000-event cap is a 422 for every trace in it, on every retry | SG-A6.1 |
| A6-3 | high | After the server rejects a streamed event (`policy_block`, `trace_too_long`, ...), every later check of that trace is allowed without asking the server | SG-A6.2 |
| A6-6 | high | `GET /api/reports/summary` returns other organisations' evaluation runs | SG-A6.3 |
| A6-4 | medium | 401, 404 and 422 from `/check` (revoked key, wrong `agent_id`) fail open like a timeout | SG-A6.4 |
| A6-5 | medium | The SDK disk buffer holds raw trace input and output in a world-readable file under a predictable shared temp directory | SG-A6.4 |
| A6-7 | medium | Two guarded calls running concurrently in one trace are both checked against the same prefix | SG-A6.4 |
| A6-8 | medium | Deleting a policy deletes every violation it recorded, from the trace view, reports, CSV and alert counts | SG-A6.4 |
| A6-10 | medium | A policy nested ~400 deep is accepted, then every `/check` and event append in its scope is a 500 | SG-A6.4 |
| A6-12 | medium | A streamed trace that never sends `final` stays open forever: no batch check, no counterexamples | SG-A6.4 |
| A6-9 | low | The SQLite test engine enforces no foreign keys, so cascades (A6-8) are invisible to the default suite | none |
| A6-11 | low | `/ws/traces` checks only the token signature: a user removed from the org keeps the live trace and violation feed | none |
| A6-13 | low | `/check` answers for a closed trace or another agent's `trace_uid` where the append would be a 409, and the SDK fails open on that 409 | none |
| A6-14 | low | Deeply nested sources make `/validate` and `POST /trace-policies` a 500 instead of a 422 | none |
| A6-15 | low | `violations.csv` writes agent names unescaped, so a name starting with `=` is a spreadsheet formula | none |
| A6-16 | low | Six surviving mutants: the async periodic flusher, `trace_uid` quoting, `_settle`, revoked keys and rollback on `/ws/ingest`, precheck's environment | rides with SG-A6.1, SG-A6.4 |

## Method

- Read every route, schema and service on the policy paths (`routers/trace_policies.py`, `traces.py`, `ws.py`, `reports.py`, `services/trace_service.py`, `trace_policy_service.py`, `violation_service.py`, `report_service.py`), the auth and rate-limit middleware, and the whole SDK, comparing the sync and async clients line by line.
- Ran the full backend suite (1009 passed, 10 xfailed, SQLite), the Postgres subset CI runs and the SDK suite at `eb7e255`.
- Turned each suspicion into a test against the real app (backend findings, through `ASGITransport`, with the real async SDK client where the SDK is involved) or against the SDK's `FakeServer` (SDK-only findings).
- Mutation probe of the API and SDK paths, which coverage alone can't judge: 21 hand-picked one-line mutants of the new routes, the error and fail-open paths and the streaming client, each replayed on a temporary copy (see **Mutation probe**).

## High

### A6-1 `close()` drops all but one batch (SDK)

`AgentOpsClient.close()` sets `_stop`, calls `flush()` and joins the flusher. `flush()` sends `_drain()`, which stops at `batch_size` (50 by default); the flusher thread then sees `_stop` and exits without draining. Everything after the first batch is neither sent nor written to the disk buffer. `close()` is also the `atexit` hook, so a script or job that records more than 50 traces in its last flush interval (2 s) loses the excess at exit, with no log line. Lost traces are never batch-checked, so their violations never exist.

The async client's `flush()` has the opposite problem: it sends the whole queue in one request regardless of `batch_size`, which makes A6-2 more likely.

Why the suite misses it: the SDK's `make_client` fixture sets `c._stop` so the flusher sleeps out the test, which also turns every `close()` in the sync-client tests into a no-op (`close()` returns early when `_stop` is set). No test calls `close()` with more than one batch queued.

Test: `sdk/tests/test_audit_6.py::test_close_delivers_every_queued_trace` (5 traces, `batch_size=2`: 2 sent, 0 buffered).

### A6-2 Bulk batches over the server's event cap are never delivered (SDK + API contract)

`TraceBulkIn` rejects a request whose traces hold more than `MAX_TRACE_EVENTS` (10 000) events in total, with a 422 for the whole request. Both clients batch by trace count only: 50 traces of 201 events (the budget table's own workload is 200-event traces) is one 422. `_post` treats any status ≥ 400 as transient and writes every trace in the batch to the disk buffer; the buffer is only read back at the next client construction, where the same traces form the same oversized batch and fail again. A single trace with more than 10 000 events poisons whichever batch it lands in the same way, and is not reported as a per-trace rejection.

Test: `sdk/tests/test_audit_6.py::test_bulk_requests_stay_under_the_server_event_cap` (3 traces of 4 000 events are sent as one 12 000-event request).

### A6-3 A dead stream disables the guard for the rest of the trace (SDK)

When an event append is rejected with anything `push_outcome` calls fatal (403 `policy_block`, 409 `trace_closed` or `agent_mismatch`, 413 `trace_too_long`, 422), the trace is marked `_dead`. From then on `Trace.check`/`acheck` return `_undelivered()` without calling the server: `failed_result("trace events not delivered", fail_closed)`, which is `allowed: True` under the default `fail_closed=False`. So:

- An agent that records an unguarded event the server refuses (`t.add_event("tool", "delete_user")` with a `block` policy) gets the server's verdict once, then every guarded call in the trace runs unchecked, including ones the server would refuse.
- A long-running agent loop passes `MAX_TRACE_EVENTS`; from that event on, every guarded call in the trace is allowed.

Fail-open is the documented answer when the server can't be reached; here the server was reached and said no. `sdk/tests/test_stream.py::test_stops_streaming_on_rejection` asserts the current behaviour ("the server lacks the trace's events, so the check fails open"), so the fixing sub-goal changes that test too.

Test: `sdk/tests/test_audit_6.py::test_check_after_policy_block_rejection_is_not_allowed`.

### A6-6 The summary report leaks other organisations' evaluation runs (API, security)

`report_service.org_summary` selects `recent_evals` as `EvaluationRun.created_at >= cutoff ... limit(20)` with no organisation filter. Every member of any org (viewer role included) gets up to 20 of the most recent evaluation runs across all tenants from `GET /api/reports/summary`: run id, agent id, score, passed and failed counts. The PDF does not render that section, but it builds the same summary.

Test: `backend/tests/audit/test_audit_6.py::test_summary_report_lists_only_own_evaluation_runs`.

## Medium

### A6-4 Client errors on `/check` fail open (SDK)

`check_result` maps every non-2xx status to `failed_result`, so under the default config a revoked or mistyped API key (401), a wrong `agent_id` (404 `agent_not_found`) or a payload the server rejects (422) make every check `allowed: True`, logged once per call at WARNING. These are configuration errors that never heal by retrying; an agent deployed with a wrong `agent_id` runs with its guard off and nothing else shows it (its traces are also rejected). This generalises the known 429 case (SG-A3.4's accepted gap): only timeouts, connection errors and 5xx are outages.

Test: `sdk/tests/test_audit_6.py::test_check_client_errors_do_not_allow` (401, 404, 422).

### A6-5 The SDK disk buffer is world-readable (SDK, security)

`disk_buffer_path` is `<tempdir>/agentops_sdk/buffer-<first 8 chars of the API key>.jsonl`, created with `mkdir(exist_ok=True)` and `open(..., "a")` under the process umask (0644 with the usual 022). It holds unmasked trace `input`/`output` (PII masking runs on the server) for any trace that failed to send, now including `storage_error` re-buffers (SG-A3.4). On a shared host other users can read it, and because the directory name is fixed and `exist_ok` accepts a directory someone else created, another user can pre-create it and plant lines that the next run sends with the victim's key.

Test: `sdk/tests/test_audit_6.py::test_disk_buffer_is_private`.

### A6-7 Concurrent guarded calls share one check (SDK)

`@guard` checks the call, runs it, and only then records it with `add_event`. Two guarded coroutines started together in one trace (`asyncio.gather`) are both checked against the same prefix, both allowed, and both run; the second append then violates the policy after the fact. With `count(tool("delete_*")) <= 1` as a `block` policy, both deletes run. The same holds for any history-dependent policy where one of the concurrent calls is the history (a secret read alongside an outbound call).

Test: `backend/tests/audit/test_audit_6.py::test_concurrent_guarded_calls_respect_a_count_policy`.

### A6-8 Deleting a policy erases its violation history (API, contract)

`policy_violations.policy_id` is `ON DELETE CASCADE`, so `DELETE /api/trace-policies/{id}` removes every violation the policy recorded, from the trace view, `violations.csv`, the summary and PDF, and the `policy_violations` alert count. Violation rows already store `policy_name` and `severity` so they can stand alone, and the audit log keeps only the deletion itself. Whether history should outlive the policy (soft delete, or `SET NULL`) is a product call; today the behaviour is untested and differs by database (A6-9). Disabling the policy (`enabled: false`) keeps history.

Test: `backend/tests/audit/test_audit_6.py::test_deleting_a_policy_keeps_its_violation_history`, `xfail` on Postgres only (on SQLite the rows survive as orphans, A6-9, so it passes there for the wrong reason).

### A6-10 A policy the API accepts can take the org's checks down (API)

`POST /api/trace-policies` only parses the source. A formula nested about 400 deep (`not not ... tool("a")`, or `once`/`historically` chains) parses and compiles, but `Monitor`'s `Plan._add` recurses per node and raises `RecursionError` (the Z3 batch check copes). `active_policies` skips only `PolicyParseError`/`ValueError` at compile time, so every `/check` and event append in the policy's scope is a 500, and so is every whole-trace ingest once the policy's action is `block`. With the default fail-open SDK, the 500s turn every guard in the org into `allowed: True`, so one bad policy disables all the others. (Deeper still, the parser itself recurses out: A6-14.)

Test: `backend/tests/audit/test_audit_6.py::test_deeply_nested_policy_does_not_break_ingest`.

### A6-12 Unfinished streamed traces never close (API)

A streamed trace closes only on an append with `final: true`. If the agent crashes, is killed, or its final push fails (the SDK logs and drops it, A6-3's `retry` branch), the trace keeps `meta.stream = "open"` forever: `check_completed_trace` skips open traces, so it is never batch-checked, its live violations never get counterexamples, and nothing in the Celery beat schedule closes idle traces. Its monitor state expires from Redis after 24 h, so a late append rebuilds from spans.

Repro: `POST /api/traces/t1/events` with one violating event and no `final`; the violation row keeps `counterexample: []` and the trace stays `stream: open` with no task ever queued (`enqueued` fixture stays empty).

## Low

- **A6-9** `tests/conftest.py`'s SQLite engine never runs `PRAGMA foreign_keys = ON`, so the default `pytest -q` run cannot see any `ON DELETE CASCADE` or FK violation; CI's Postgres pass covers only what a test asserts, and no test asserted A6-8. Test: `test_test_database_enforces_foreign_keys`, `xfail` on SQLite only.
- **A6-11** `/ws/traces` authenticates with `decode_token` alone: no token type check, no user-active check, no membership check, and the socket is never re-checked. A user removed from the org keeps the live trace and violation feed until the access token expires and, once connected, indefinitely. Test: `test_ws_feed_requires_membership`.
- **A6-13** `precheck` evaluates a candidate for a trace that is already final, and looks the trace up by `trace_uid` without the agent, so another agent's whole trace with the same id is a 409 `agent_mismatch` (whole-trace dedup is per agent). The append would be 409 too, but the SDK reads a 409 on `/check` as a failed check and allows. Repro: ingest a whole trace with `trace_id: "x"` from agent A, then `/check` from agent B with `trace_uid: "x"`.
- **A6-14** `parse` recurses per nesting level; around 400 nested parentheses it raises `RecursionError`, which `/validate` and create don't catch: a 500 with `internal_error` instead of a 422 with a line and column. Repro: `/validate` with `"policy p severity low:\n  always " + "(" * 400 + 'tool("a")' + ")" * 400`.
- **A6-15** `violations_to_csv` (and `traces_to_csv`) write cells as-is; an agent named `=HYPERLINK(...)` becomes a formula when the CSV is opened in a spreadsheet. Policy names are DSL identifiers and safe.

## Mutation probe

21 one-line mutants, each applied to a temporary copy of the repo (`mutants_audit4.py`'s runner): SDK mutants against the whole SDK suite, backend mutants against `tests/api`, `tests/audit` and the root test files, with every backend survivor replayed against the whole backend suite.

- **Killed (12)**: 429 treated as fatal; malformed-response guard; `close()` batch limit (by the new A6-1 test); version bump on source change; org filter on `violations.csv` and on `GET /traces/{id}/violations`; `agent_mismatch` on `/check` and on append; closed-trace check; `/ws/ingest` rate-limit `continue`; agent check on policy update; delete audit row.
- **Equivalent (3)**: `>= 400` to `> 400` in both clients' `_post` (bulk never answers 400), and dropping the rename name check in `update_trace_policy` (the unique index and `_commit_or_409` give the same 409).
- **Survived (A6-16, low, tests only)**: replay with `python backend/scripts/mutants_audit6.py`.
  - The async client's periodic flusher never posts: no test runs `start()` and waits for a flush, so `AsyncAgentOpsClient` delivering only on `close()` passes.
  - `_events_url` without `quote(trace_uid)`: no test uses a `trace_id` with `/` or `?`.
  - `_settle` assigning `start + n` instead of `max(...)`: no test has a stale `ok` land after a later resync.
  - `/ws/ingest` accepting a revoked API key: only REST tests revoke a key.
  - `/ws/ingest` not rolling back after a `TraceConflict`: no test sends a valid events message after a conflict on the same socket.
  - `precheck` taking the agent's environment for an existing trace: no test has a trace whose environment differs from its agent's (whole-trace ingest with the SDK's `environment`; A3-1's tests cover the reverse).

## Round-1 fixes re-checked

- **SG-A3.2 (PR #17)**: `/check` runs `evaluate_policies` on the candidate with the same environment as the append, and lists the legacy hit first; holds. `test_check_agrees_with_append_on_legacy_rules` covers all three rule types.
- **SG-A3.3 (PR #17)**: a sync check in an async streamed trace pushes synchronously and `_settle` is offset-based; holds. Known: it blocks the event loop.
- **SG-A3.4 (PR #18)**: `/check` has its own `check:` bucket; `policy_violations` alert rules and `compute_metric_value` count only `alert`/`block` policies; whole-trace ingest returns the stored row on a resend before any check; both clients re-buffer `storage_error` (but see A6-2 for the whole-request 422); `/ws/ingest` charges the `org:` bucket per message. All hold. Known gaps unchanged: concurrent first sends can both store; the count uses the policy's current action; a 429 on `/check` fails open (A6-4 widens this to all 4xx).
- **SG-A4.1 (PR #20)**: per-test SQLite file with a connection per session; holds. A deleted commit in `check_completed_trace` is still killed.
- **SG-A4.2 (PR #21)**: CI runs coverage with floors (backend 72 %, policy code 90 %, SDK 80 %) and a Postgres pass over `tests/api`, `tests/audit` and `test_audit2.py`, which are all the DB-backed tests (`tests/policy` and the other root tests use no database). Holds. The Postgres pass and the SQLite pass disagree on cascades (A6-9).

## Not findings

- Cross-org isolation of the new routes: `/check`, event append, `GET /traces/{id}/violations`, `violations.csv` and policy CRUD all filter by the caller's org, and tests cover it (`test_precheck.py`, `test_trace_policies.py`, `test_violation_pipeline.py`).
- Error shapes: every new REST error goes through the one `{"error": {code, message, details}}` handler, and the SDK reads `code` and `details.expected_seq` from it correctly.
- Schema drift: `tests/test_migration.py` compares the migrated Postgres schema with the ORM metadata.
