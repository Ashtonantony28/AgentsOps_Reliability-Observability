# PLAN: Z3 policy verification

Formal safety policies over agent traces, verified with Z3, surfaced live and in reports.

Every thread reads this file first and updates its own entry (owner, status, result) when it finishes.
Statuses: `proposed` → `ready` → `in progress` → `in review` → `done` (or `blocked: <reason>`).

## Rules

- SG0 merges before any parallel work. Changing anything under **Interface contracts** is a stop-and-report, not a local fix.
- Since 2026-10-14 (Ashton): a contract change that the reviewing thread judges sound is approved by that thread and recorded in its sub-goal entry, not escalated.
- SG1–SG3 run on fixture traces only. Nothing touches live ingestion until the **fixture gate** passes.
- Threads delegate bounded, test-driven tasks to subagents, at most 2 at a time.
- Subagents return only: files changed, tests added, verification command + result, open issues.
- A task that fails verification 3 times stops and is reported in its thread.
- Code: match existing conventions (async SQLAlchemy, pydantic v2 schemas, routers/services split), minimal comments, one PR per sub-goal.

## What the codebase gives us (as of `7766898`)

| Fact | Consequence |
|---|---|
| Traces are ingested whole (`TraceIn.tool_calls`), via REST, bulk, or `/ws/ingest`. No per-event ingest. | Live incremental checking needs a new event-append path. |
| `Span` has no sequence number; `started_at` defaults to insert time. Order is implicit in list order. | Add `spans.seq`; event order must be explicit. |
| Only tool calls exist. No "confirmation", "secret read" or other event kinds. | Add a generic ordered event (`kind`, `name`, `status`, `attrs`); tool calls are one kind. |
| `PolicyRule` + `evaluate_policies` already exist (stateless block/warn/log on forbid_tool, PII, injection). | New feature is `TracePolicy`, separate table and router; existing rules stay untouched. |
| Redis channel `traces:{org_id}` feeds `/ws/traces`, each message wrapped as `{"type":"trace"}`. | Violations go on `violations:{org_id}`, forwarded on the same socket as `{"type":"violation"}`. |
| Backend tests are 3 pure-unit files; no DB fixtures, no CI. | SG0 adds test harness (SQLite + fakeredis) and a CI workflow. |
| One Alembic migration (`0001_init`). Docker image is Python 3.11. | New migration `0002_trace_policies`; `z3-solver` added to requirements. |

## Design decisions (defaults picked; change before SG0 starts if you disagree)

1. **Past-time temporal logic only in v1** (`once`, `historically`, `previously`, `since`, past counts). Every past-time formula is monitorable event by event and encodes linearly, which is what makes the latency budget reachable. Future obligations ("confirmation must follow within 3 events") are out of scope for v1.
2. **Encoding is linear "state-carrying"**: per event `i`, a Boolean per subformula defined from step `i-1` (e.g. `once_P[i] = once_P[i-1] ∨ P[i]`). The naive "∃ j < i" grounding is quadratic and was measured at 1.4 s for 200 events.
3. **A plain-Python reference evaluator is the semantic oracle.** It is written by a different thread than the Z3 encoder, and every encoder test is differential against it (Hypothesis-generated traces × policies). This is how "no false negatives" is tested rather than asserted.
4. **Minimal counterexample = minimal unsat core** of `facts(events) ∧ ¬violation_at(i)` with one assumption literal per event. It is the smallest set of events whose observed facts alone entail the violation.
5. **Batch checks run off the request path** (Celery), live checks run in the ingest path with monitor state in Redis.

## Latency budget (early, from a measurement)

Measured in a cloud container, `z3-solver` via pip, Python 3.13, one policy (`delete_*` needs prior confirmation):

| Encoding | 10 events | 50 | 200 | 1000 |
|---|---|---|---|---|
| Naive ∃-grounding | 9.5 ms | 102 ms | 1383 ms | — |
| Linear state-carrying | 7.6 ms | 18.6 ms | 75.6 ms | 343 ms |
| Incremental `push`/`check`/`pop` per event | ~0.22 ms / event | | | |

Budget (in-process, excludes DB and network; enforced by `scripts/bench_policy.py --assert-budget` in CI):

| Path | Workload | p95 | p99 |
|---|---|---|---|
| Live: `Monitor.step` per event | 10 enabled policies | ≤ 5 ms | ≤ 20 ms |
| Ingest request overhead added by policies | per event, enqueue + monitor step | ≤ 8 ms | ≤ 25 ms |
| Batch: `check_trace` | 200 events × 10 policies | ≤ 250 ms | ≤ 600 ms |
| Pre-execution `would_violate` | 200-event prefix, 10 policies | ≤ 10 ms | ≤ 30 ms |
| Counterexample (only on violation) | 200 events, 1 policy | ≤ 500 ms | ≤ 1 s |

SG0 reruns these numbers on the benchmark harness and may revise the budget once, recording the reason here. After SG0 the budget only changes through a stop-and-report.

## Sub-goals

Dependency order: `SG0` → (`SG1` ∥ `SG2`) → **fixture gate** → (`SG3` ∥ `SG4`) → `SG0b` → (`SG5a` ∥ `SG5b`) → `SG6` → audits.

### SG0 Shared interfaces and harness
- **Owner:** SG0 thread · **Status:** done (merged `aa2f27a`, PR #2) · **Blocks:** everything
- **Scope:** everything under **Interface contracts** as code with stub bodies (`NotImplementedError` / HTTP 501): `app/policy/{ast,events,checker,counterexample}.py` signatures, pydantic schemas, SQLAlchemy models + migration `0002`, TS types, DSL grammar (`docs/policy-dsl.md`, EBNF + semantics table), fixture format + ≥ 20 fixture traces with expected verdicts, test harness (SQLite async + fakeredis), `scripts/bench_policy.py`, CI workflow running backend, SDK and frontend checks.
- **Acceptance:**
  - Migration upgrades and downgrades cleanly on Postgres (docker-compose) and the new tables exist.
  - Every contract symbol imports; schema round-trip tests pass for `EventIn`, `ViolationOut`, `TracePolicyOut`.
  - Fixture traces load and validate against `EventIn`; each fixture names its expected violations (policy, `at_seq`, counterexample seqs).
  - Existing ingest behaviour unchanged: legacy `tool_calls` payloads map to events with `seq` = list index.
  - Benchmark harness runs and prints p50/p95/p99 per path (against a trivial stub).
- **Verify:** `cd backend && pytest -q` · `cd backend && alembic upgrade head && alembic downgrade -1 && alembic upgrade head` · `cd frontend && npx tsc --noEmit` · `python backend/scripts/bench_policy.py --dry-run`
- **Result:** all four verify commands pass locally (164 backend tests incl. a Postgres migration test, SDK 2, tsc clean, bench dry run prints all 5 paths). 31 fixtures in `backend/tests/policy/fixtures/`, verdicts and unique minimal counterexamples cross-checked by brute force. Budget unchanged: only stubs exist, so the first real numbers come from SG2's `--assert-budget` run. Additions to the contract made while freezing it are listed under **SG0 contract notes**.

### SG1 DSL parser and reference evaluator
- **Owner:** SG1 thread · **Status:** done · **Depends:** SG0
- **Scope:** `app/policy/dsl.py` (`parse`), `app/policy/reference.py` (`evaluate`), pretty-printer for round-trips. Pure Python, no Z3.
- **Acceptance:**
  - Parses every example in `docs/policy-dsl.md`; `parse(pretty(p)) == p` for Hypothesis-generated ASTs.
  - Errors carry line, column and an expected-token message; one test per error class (unknown tag, bad glob, unbalanced parens, non-integer bound, future operator).
  - `evaluate` matches the expected verdicts on every SG0 fixture.
  - Line coverage ≥ 95 % on `dsl.py` and `reference.py`.
- **Verify:** `cd backend && pytest tests/policy/test_dsl.py tests/policy/test_reference.py -q --cov=app.policy.dsl --cov=app.policy.reference --cov-fail-under=95` (narrowed from `--cov=app/policy`, which also counted the SG2/SG3 stubs)
- **Result:** verify passes with 100 % coverage on both files (248 tests); full backend suite 411 passed. Every fixture's verdict matches. `pretty`/`pretty_formula` live in `dsl.py`. The Hypothesis AST generator for SG2's differential test is `tests/policy/strategies.py` (`formulas()`, `policies`); it emits only atoms the DSL can express. `reference.truth(f, events)` gives per-position values. Attr equality compares Python types exactly, so a float `2.0` does not match `attr("k", 2)`. The evaluator is also cross-checked against a naive quantifier-literal oracle in `test_reference.py`.

### SG2 Trace → Z3 encoding
- **Owner:** SG2 thread · **Status:** done · **Depends:** SG0
- **Scope:** `app/policy/encode.py`, `check_trace` in `checker.py`. Linear state-carrying encoding; one solver, event facts shared across policies, each policy checked under its own assumption.
- **Acceptance:**
  - Matches SG0 fixture verdicts exactly.
  - Differential test: 5 000 Hypothesis cases (traces ≤ 50 events, random ASTs from the SG1 generator) where `check_trace` and `reference.evaluate` agree on every `(policy, at_seq)`. Run against SG1 once it lands; until then against fixtures only.
  - Encoding size is linear: formula node count for 1 000 events ≤ 12 × count for 100 events.
  - Batch budget row met.
- **Verify:** `cd backend && pytest tests/policy/test_encode.py -q` · `pytest tests/policy/test_differential.py -q --hypothesis-profile=ci` · `python scripts/bench_policy.py --path batch --assert-budget`
- **Result:** all three verify commands pass locally and run in CI. Fixture verdicts match on all 31 fixtures, using hand-written ASTs (`tests/policy/fixture_policies.py`) that are also checked equal to `parse(source)`. Differential: 5 000 cases each against `reference.evaluate` for small-vocabulary ASTs and for SG1's `policies` generator, all agree; seeded mutants of `encode.py` (since, previously, count, attr typing, `?` glob) are each caught. Node count 6 244 at 100 events vs 62 484 at 1 000 (10.0×). Batch p95 ≈ 55–78 ms locally; 1 000 events ≈ 0.4 s, 5 000 events ≈ 2–4 s (for A2).
  - How it works: `TraceEncoding` emits SMT-LIB text (the Python API alone cost ~330 ms per batch). One Bool per (distinct atom, event) with its observed value as a fact literal in `facts[i]`; temporal and count nodes are defined chains; structurally equal subformulas are encoded once across policies. Instead of one solver check per policy, one check covers all policies and each policy's first false position is read from an Int defined as a nested `ite`, which is equivalent because the facts fix the model. Each call uses its own `z3.Context` so threads can check concurrently.
  - For SG3: `Violation.counterexample` is `()` until SG3 wires it. `facts[i]` are the per-event literals to put behind assumption literals, but `count … by name/kind` chains are grouped by the observed name/kind, so for counterexamples SG3 has to make that grouping depend on the event's facts. Note `Match(attrs=(("a", 1),)) == Match(attrs=(("a", True),))` under dataclass equality (`1 == True`); the encoder keys atoms by value type, and AST comparisons in tests should also compare `repr`.

### Fixture gate
SG1 and SG2 merged, the differential suite green at 5 000 cases, and the batch budget met. Recorded here with the commit hash before SG3/SG4 start.
- **Passed** at `10a4a3e` (SG2 merge, PR #4).

### SG3 Minimal counterexamples
- **Owner:** SG3 thread · **Status:** done · **Depends:** fixture gate
- **Scope:** `app/policy/counterexample.py` (`minimal_counterexample`), wired into `check_trace` output.
- **Acceptance:**
  - Returned set is sufficient (re-checking only those events' facts still entails the violation) and minimal (dropping any one event no longer entails it), property-tested on 1 000 cases.
  - Matches hand-written expected counterexamples on all fixtures.
  - Counterexample budget row met.
- **Verify:** `cd backend && pytest tests/policy/test_counterexample.py -q` · `python scripts/bench_policy.py --path counterexample --assert-budget`
- **Result:** both verify commands pass locally and run in CI. All 28 expected counterexamples across the 31 fixtures match, directly and through `check_trace`. Property tests: 1 000 cases over SG2's small-vocabulary ASTs plus 500 over `count … by name/kind` with colliding names; each checks sufficiency against `reference.truth` on random completions of the unknown events, and minimality by decoding a solver model for every dropped event into concrete events that `reference.truth` confirms satisfy the policy. Counterexample p95 ≈ 75 ms for the worst case (200 events, all needed; the bench now uses that trace instead of a non-violation); batch p95 unchanged at ≈ 80 ms with counterexamples included.
  - How it works: a `TraceEncoding` subclass replaces observed atom constants with per-event variables (kind; status and attr values as codes for each policy constant plus "anything else"; names as the set of policy globs they match), all bit-vectors, so the solver is Z3's SAT-based `QF_FD`. Event i's observed values hold under assumption `a_i`; the first unsat core is shrunk by a deletion pass. The realizable glob-match sets are computed exactly by a BFS over the product of the glob automata (cached per glob set). `count by kind/name` compares each event's group with every earlier event's (quadratic, but only the positions the violation depends on are kept, so a top-level count is linear).
  - Limits: `count … by name` on a policy that also uses globs needs real strings (Z3 string theory, 1 s budget per counterexample); if Z3 gives up, the result keeps the events it could not rule out, so it stays sufficient but may not be minimal. Nested `count by` under `once`/`historically` is quadratic in trace length. Many overlapping `*` globs in one policy can make the glob-match set enumeration large. `check_trace` lets a counterexample error propagate rather than drop it silently; kinds outside the 5 (possible in stored spans) are handled.

### SG4 Checker service (batch + live)
- **Owner:** SG4 thread · **Status:** done · **Depends:** fixture gate
- **Scope:** `Monitor` (incremental), Celery task `check_completed_trace`, event-append endpoint and `/ws/ingest` event messages, monitor state in Redis, `TracePolicy` CRUD router + `validate`, persistence of `PolicyViolation`, `action=block` returning 403 like existing `PolicyViolation`.
- **Acceptance:**
  - For every fixture, streaming events one by one through `Monitor` yields the same violations as `check_trace` on the whole trace (same `at_seq`).
  - Monitor state survives a Redis round-trip mid-trace (snapshot/restore test).
  - Out-of-order or duplicate `seq` is rejected with 409, never silently reordered.
  - Completed traces are checked exactly once (idempotent on re-delivery).
  - Live and ingest-overhead budget rows met; load test at 500 events/s for 60 s keeps p95 within budget.
- **Verify:** `cd backend && pytest tests/policy/test_monitor.py tests/api/test_trace_policies.py tests/api/test_event_ingest.py -q` · `python scripts/bench_policy.py --path live --assert-budget` · `python scripts/bench_policy.py --load 500 --seconds 60`
- **Result:** all three verify commands pass (149 tests across the three files; full backend suite green). Live `Monitor.step` p95 ≈ 0.02 ms, ingest restore+step+snapshot p95 ≈ 0.2 ms; load test 30 000 events at 500/s over 50 interleaved traces: step p95 ≈ 0.05 ms, ingest p95 ≈ 0.4 ms. CI now runs the live, ingest and load benches.
  - `Monitor` (`checker.py`, plan in `monitor.py`) evaluates the same state-carrying recurrences as the Z3 encoding on concrete values, so a step needs no solver. It is checked against `check_trace` on every fixture and on Hypothesis traces, and against `reference.evaluate` on 3 000 cases; seeded mutants (since, previously, count by) are caught. `step` raises `ValueError` on any seq other than `next_seq`. Snapshots are JSON carrying each policy's id and formula digest; `restore` raises `ValueError` on any mismatch.
  - Live path (`trace_service.append_events`, REST and `/ws/ingest`): per-trace Redis lock; spans in the DB are the source of truth, and the Redis monitor state (`policy:monitor:{trace_id}`, 24 h TTL) is a cache that is rebuilt from spans when missing, stale or for a changed policy set (violations found while rebuilding are recorded too). A streamed trace is marked `meta.stream = "open"` until `final`; appending to a final or whole-ingested trace is 409 `trace_closed`, seq gaps/dups 409 `seq_conflict` with `expected_seq`. A `block` violation rejects the whole request (403, no events stored, `policy_block` audit row; since SG-A5.3 the trace stays open and the blocking violation is stored); legacy `forbid_tool` rules also apply to appended events.
  - Whole-trace ingest runs only `block` policies inline (via `Monitor`, 403 as before) and queues `check_completed_trace` when any trace policy applies; `final` on the events path queues it too. The task is idempotent through `ON CONFLICT DO NOTHING` on `(trace_id, policy_id, policy_version)` and returns only new rows (for SG5a to publish). It also fills in counterexamples on live rows once SG3 wires them into `check_trace`.
  - Test-only dependency: `fakeredis[lua]` + `lupa` (Redis lock scripts).

### SG0b Ingest hardening
- **Owner:** SG0b thread · **Status:** done · **Depends:** SG4
- **Scope:** ingest gaps found in the SG2 and SG4 reviews. Each item first got a test that failed on `c9e144e`.
  1. Legacy `forbid_tool` rules only matched `kind="tool"`, so the same name under another kind bypassed a `block` rule. Now every kind (`kind` is client-chosen).
  2. Spans without `seq` (pre-0002 rows, seeded data) crashed `events_from_spans` and the batch check. They are now ordered by `started_at` and numbered by position; the seed sets `seq` (and no longer inserts spans with a null `trace_id`).
  3. One invalid trace made `/api/traces/ingest/bulk` a 422 for the whole batch (and the SDK re-buffers the whole batch forever); a policy block or DB error mid-batch was a 500. Traces are now validated and stored one by one; rejected entries carry `index`, invalid ones `reason: "invalid_payload"` + `errors`, DB errors `reason: "storage_error"`.
  4. Fields had no bounds, so out-of-range values reached Postgres. `EventIn`/`ToolCallIn` `name` ≤ 255 and `status` ≤ 32, `TraceIn` strings and the events `trace_uid` bounded by their columns, `latency_ms`/`tokens_*` ≤ int32 (422). Tz-aware trace timestamps are stored as naive UTC.
  5. `0002` left columns nullable that the models require. Migration `0003_policy_not_null` backfills, adds server defaults and sets NOT NULL on `trace_policies` and `policy_violations`.
  6. Legacy `forbid_pii` / `block_on_injection` rules read trace text, which streamed traces lack. On both paths they now also scan each string in event `input` (PII, injection) and `output` (PII), one string at a time. So a whole-trace `tool_calls` payload with PII in a tool's input is now caught by a `forbid_pii` rule too.
  7. (found here) `evaluate_policies` returned on the first `warn` match, hiding a later `block` rule. It now raises on any matching block rule and keeps the first warning.
- **Acceptance:** each item has a failing-then-passing test; full backend suite green; migration round-trip on Postgres shows no nullability drift on the new tables.
- **Verify:** `cd backend && pytest tests/api/test_ingest_hardening.py tests/test_migration.py -q` · `cd backend && pytest -q` · `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
- **Result:** all pass locally (Postgres 16, migration test not skipped); 708 backend tests. Open: span payloads are stored unmasked on both paths (only trace text is masked); bulk ingest has no batch-size cap (unchanged); `spans` columns from `0001` keep their nullability drift (fixing it means a NOT NULL change on a large table).

### SG5a Integration: pipeline, alerts, reports
- **Owner:** SG5a thread · **Status:** done · **Depends:** SG3, SG4
- **Scope:** publish on `violations:{org_id}`, forward on `/ws/traces`, `AlertRule.metric = "policy_violations"`, incident on `critical`, `GET /api/traces/{id}/violations`, report summary section, PDF section, `GET /api/reports/violations.csv`, seed data with demo policies.
- **Acceptance:**
  - Ingesting a violating fixture produces: a DB row, a Redis message received by a WS test client, and (if a matching rule exists) an `AlertEvent`.
  - PDF contains a "Policy violations" section; CSV columns match the contract.
  - Seeded demo org shows violations on first boot.
- **Verify:** `cd backend && pytest tests/api/test_violation_pipeline.py tests/api/test_reports_violations.py -q`
- **Result:** verify passes (45 tests); full backend suite 832 passed, migration round-trip on Postgres 16 included; frontend tsc, build and tests pass. Also checked by hand on Postgres + Redis: `python -m app.seed` on an empty DB, then a streamed violating trace gave the row, the Redis message, an `AlertEvent` and an incident.
  - `violation_service.publish_violations` runs after the rows are committed, on the live path (`append_events`, REST and `/ws/ingest`) and after the Celery batch check (`check_and_publish`). It sends each new row as `ViolationOut` JSON on `violations:{org_id}`, then evaluates the org's enabled `policy_violations` alert rules. It never raises, so a Redis or alert failure cannot fail ingestion. `/ws/traces` subscribes to both channels before accepting the socket and wraps messages as `trace`/`violation`.
  - Live vs batch: one row per `(trace, policy, version)`. The batch check fills the counterexample into the live row (mode stays `live`) and publishes only rows it inserted, so a violation reaches the feed and the alert rules once. Reports, CSV and `GET /api/traces/{id}/violations` read the stored rows, so they carry the batch counterexample once it has run.
  - Alerts: `policy_violations` counts the org's violations with `detected_at` in the rule's window, filtered by the rule's agent and by `context.severity_at_least`. That needed a new `alert_rules.context` JSON column (migration `0004_alert_rule_context`, NOT NULL, default `{}`), exposed on the alert rule API and TS type and validated against the policy severities. These rules are evaluated only when new violations are stored, in a session separate from the ingest one (same dedup as other metrics). The periodic Celery evaluator skips them, so that its incident-less firing cannot dedup away the one that opens an incident. An incident (severity `critical`) opens when the rule is `critical` or when any violation that fired the alert is `critical`. Channel notifications go out in the background on the ingest path; the Celery task waits for them.
  - `GET /api/traces/{id}/violations` is org-scoped (404 otherwise), ordered by `at_seq`. `GET /api/reports/violations.csv?days=7` (1 to 90) has the contract columns; `agent` is the agent name and `counterexample` is space-separated seqs. The summary JSON gains `policy_violations: {total, by_severity, by_policy}` and a per-agent `policy_violations` count; the PDF gains a "Policy violations" section.
  - Seed: three demo trace policies (`confirm_before_refund` high, `no_email_after_secret` critical, `retry_cap` medium) and 24 multi-event traces over the last 6 days for Refund Bot, Sales Outreach and Support Triage. Half of them violate a policy, and their rows come from `check_trace` with counterexamples. A `Critical policy violation` alert rule is added too.
  - Open: alert rules count violations whatever the policy's `action` (`log` and `alert` currently behave the same). Two concurrent ingests can both fire the same rule, which was already true for the existing alert path.

### SG5b Integration: UI
- **Owner:** SG5b thread · **Status:** done · **Depends:** SG4 (API), SG5a for live events
- **Scope:** policy list/editor (with server-side validation errors inline) under Governance, violations panel on `TraceDetail` highlighting counterexample events in the span tree, live violation toast via `wsStore`.
- **Acceptance:** typecheck and build pass; component tests for the violations panel and editor error display; manual run screenshot of a seeded violation in TraceDetail attached to the PR.
- **Verify:** `cd frontend && npx tsc --noEmit && npm run build && npm test -- --run`
- **Result:** all three pass locally and now run in CI (vitest + Testing Library + jsdom added; 36 tests over the panel, span tree, editor, policy list, toasts, `wsStore` and `TraceDetail`). Screenshots in the PR were taken against the real UI with the API mocked, since `GET /api/traces/{id}/violations` is SG5a's and still 501 at `18f2d0f`.
  - Trace policies sit above the legacy policies on Governance: the editor validates as you type (`/validate`, 400 ms debounce) and shows 422 `invalid_policy` errors from save the same way, as line/col, message and the source line with a caret; other save errors (409 name taken, 403 non-admin) show as a message.
  - `TraceDetail` selects the first violation by default; its counterexample events get an amber outline and the `at_seq` event a red one. Spans are numbered like `events_from_spans`: by `seq`, or all by start order when any span lacks one. A live violation with an empty counterexample shows as pending and the query polls every 5 s until the batch check fills it in, for at most 10 min after `detected_at`, because a live row can't say whether the batch check has run and found an empty counterexample; an empty counterexample on a batch row means the violation holds whatever the events are.
  - `wsStore.receive` handles `{"type":"violation"}`: up to 3 toasts (8 s, link to the trace with that violation selected), and `TraceDetail` refetches when one arrives for the open trace.

### SG6 SDK streaming and pre-execution checks (stretch)
- **Owner:** SG6 thread · **Status:** done · **Depends:** SG4
- **Scope:** `Trace.add_event(...)`, `agentops.trace(..., stream=True)`, `client.check(name, input=...)` / async twin calling `POST /api/trace-policies/check`, optional `@agentops.guard` decorator that raises `PolicyBlocked` before running a tool.
- **Acceptance:**
  - Legacy SDK payloads unchanged (existing SDK tests pass untouched).
  - Contract test: SDK request/response models match the backend OpenAPI schema.
  - `check` on a would-violate candidate returns `allowed=False` with the violating policy; a network failure follows a documented fail-open/fail-closed setting (default fail-open, logged).
  - Pre-execution budget row met server-side.
- **Verify:** `cd sdk && pytest -q` · `cd backend && pytest tests/api/test_precheck.py -q` · `python backend/scripts/bench_policy.py --path precheck --assert-budget`
- **Result:** all three verify commands pass (SDK 73 tests; `test_precheck.py` 41 API tests, plus 38 checker tests in `tests/policy/test_precheck.py` and 7 in `tests/api/test_sdk_contract.py`); precheck p95 ≈ 5 ms against the 10 ms row, now in CI.
  - Server: `would_violate` replays the prefix through `Monitor` and calls the new `Monitor.check(event)`, which steps a copy of each policy's state and reports every policy whose formula is false at the candidate, even one already violated earlier in the trace (a guard should stop the second bad call too). Checked against `reference.truth` on 1 500 Hypothesis cases. `POST /api/trace-policies/check` loads the trace's monitor from Redis (rebuilt from spans if missing or stale, never written back), evaluates the candidate as the next event (its own `seq` is ignored; no or unknown `trace_uid` means an empty prefix) and stores nothing. `violations` lists every applicable enabled policy it would violate (`mode: precheck`, `id`/`trace_id` null, empty counterexample); `allowed` is false only when one of them is a `block` policy, i.e. exactly when appending the event would be a 403 (tested by streaming every violating fixture). Unknown agent 404, trace of another agent 409 `agent_mismatch`; rate-limited like the events path.
  - SDK: `Trace.add_event`, `trace(..., stream=True)` (each `add_event` is sent to the events endpoint, `final` on exit), `check`/`acheck` on both clients, the module and `Trace` (a trace's check carries its `trace_uid` when streaming, and follows the failure policy without calling the server while any of the trace's events are undelivered; a `seq_conflict` showing the server already stored part of a batch resyncs), `@guard` (sync and async; checks, raises `PolicyBlocked`, then records the call as an event in the active trace). Failure policy: `fail_closed=False` by default, so a failed check logs a warning and returns `allowed=True` with `error` set; `check_timeout_s=2.0`. The contract test compares the SDK's request builders and `CheckResult` with the OpenAPI schemas and drives the SDK against the app end to end. Legacy payloads are unchanged (existing SDK tests untouched; a trace with no events serialises exactly as before).
  - Open: a streamed trace sends only events, so its `output`, tokens, cost and status never reach the server (`EventsAppendIn` has no field for them). Legacy `PolicyRule` block rules (`forbid_tool`, PII, injection) are not part of the precheck. After a fatal stream rejection (403, `seq_conflict`) the SDK stops streaming that trace and the server-side trace stays open. Without `stream=True` a check has no history. Checks are rate-limited with ingest, so with the default fail-open a rate-limited org's checks allow everything. The endpoint never writes a rebuilt monitor back to Redis, so a trace whose state expired replays its spans on each check until the next append.

### SG-A1.1 Live block enforcement after a recorded violation (from A1-H1)
- **Owner:** SG-A1.1 thread · **Status:** done · **Depends:** AUDIT-1
- **Scope:** `append_events` must block every event that violates a `block` policy, not only the policy's first violation in the trace; recording stays one row per (trace, policy, version). Likely `Monitor.step` keeps reporting first violations while blocking uses `Monitor.check`-style per-event values. Snapshot format may change (stop-and-report if it touches the `Monitor` contract).
- **Acceptance:**
  - Both A1-H1 tests in `tests/audit/test_audit_1.py` pass with their `xfail` markers removed.
  - For every violating fixture streamed one event per request under a `block` policy that was `log` for the first violation, each later violating event is a 403 and `/check` returns `allowed: false` exactly when the append would be a 403.
  - Live rows unchanged: still one per (trace, policy, version), same `at_seq` as `check_trace`.
- **Verify:** `cd backend && pytest tests/audit -q -k block && pytest tests/api/test_event_ingest.py tests/api/test_precheck.py -q`
- **Result:** verify passes (2 + 121 tests). `Monitor.step` still returns first violations only (contract and snapshot unchanged) and now also sets `Monitor.failing`, the ids of every policy false at that event; `append_events` returns 403 on the first event where a `block` policy is in `failing`. New tests: every violating fixture logged then switched to block (each later violation 403, `/check` agrees, live rows match the reference on stored events), and a Hypothesis check of `failing` against `reference.evaluate` across restores. A policy that stays false once false (e.g. an unwindowed count cap logged, then switched to block) now blocks every later event of that trace, as `/check` already reported; `final` with no events still closes it. A1-M1 (SDK history) left as is: the real fix sends a prefix in `PolicyCheckIn`, a contract change.

### SG-A1.2 Bound counterexample time, never at the verdict's expense (from A1-H2)
- **Owner:** SG-A1.2/A2.1/A2.2 thread · **Status:** done · **Depends:** AUDIT-1
- **Scope:** `check_trace` / `check_completed_trace` record every violation even when a counterexample is slow or fails (store verdicts first, or fall back to a sufficient non-minimal counterexample); a per-counterexample time budget on every solver mode, not only strings; make `count … by name/kind` counterexamples fast enough for the budget row.
- **Acceptance:**
  - The three A1-H2 tests in `tests/audit/test_audit_1.py` pass with their `xfail` markers removed.
  - A whole-ingested 1 000-event trace with the retry-cap and nested `count by` policies stores both violations within the Celery time limit, with a sufficient counterexample (property-checked as in SG3).
  - Counterexample budget row met on a `count … by name` worst case, added to `bench_policy.py --path counterexample`.
- **Verify:** `cd backend && pytest tests/audit -q -k "counterexample or retry_cap" && pytest tests/policy/test_counterexample.py -q && python scripts/bench_policy.py --path counterexample --assert-budget`
- **Result:** verify passes; done together with SG-A2.1 and SG-A2.2, details under SG-A2.1. The three A1-H2 tests and A1-L2's (string budget no longer runs out) pass without markers. `check_trace` computes every verdict first; a counterexample that raises, or that would start after 30 s of counterexamples for the trace (`CHECK_BUDGET_S`), is every seq up to `at_seq`, logged. Every solver mode now has the per-counterexample budget (`BUDGET_S` = 2 s, encoding included), after which it keeps the events not yet ruled out, logged. 1 000 whole-ingested events with the retry cap and the nested `count by`: both rows stored in about 3 s, retry cap minimal (`test_long_count_by_trace_stores_every_verdict`). The `Violation` contract is unchanged, so a degraded counterexample is only flagged in the logs.

### SG-A2.1 Bounded counterexamples (AUDIT-2 A2-1 critical, A2-7)
- **Owner:** SG-A1.2/A2.1/A2.2 thread · **Status:** done · **Depends:** none. Same root cause as SG-A1.2 (found independently); run both in one thread.
- **Scope:** `count … by` counterexamples are about cubic in the window (7.5 s at 200 events, 53 s at 352) and overlapping globs are 2^k. Make `minimal_counterexample` meet its budget row for those shapes, or time-box it and fall back to a sufficient (non-minimal) set, without changing the `Violation` contract.
- **Acceptance:** `count … by name` counterexample for a 200-event window within the counterexample row (p95 ≤ 500 ms); worst case for any policy and trace ≤ 5 s with a sufficient result; 13 overlapping globs ≤ 500 ms; SG3 sufficiency/minimality property tests still pass; the bench gains a `count by` counterexample case in CI; xfail markers removed.
- **Verify:** `cd backend && pytest tests/test_audit2.py -k "count_by_name or overlapping_globs" tests/policy/test_counterexample.py -q` · `python backend/scripts/bench_audit2.py countby 200 400` · `python backend/scripts/bench_policy.py --path counterexample --assert-budget`
- **Result:** all three pass; markers removed. `countby` 66 ms at 200 events, 108 ms at 400, 293 ms at 1 000 (was 7.5 s, 63 s, did not finish); 13 overlapping globs 49 ms (was about 2 s); `bench_audit2.py batch 500` p2 139 ms (was 53 s). Counterexample bench row p95 101 ms with the new `count by` case (it alternates p0's delete trace with p2 on `count_by_trace`).
  - Counts saturate one past their bound and, up to 15, are encoded in unary ("at least k" literals) rather than bit-vector adders, which the SAT solver could not refute quickly; larger bounds keep the adders. `count … by` terms are quadratic, so each position's term is built only if the goal depends on it. Together these took the retry cap from 37 ms per solver call to under 1 ms.
  - Glob-match sets are enumerated only up to 512 automaton states; past that, names fall back to the string theory, which is budgeted like every mode.
  - New property tests: sufficiency and minimality with the bit-vector counts forced and with string names forced, plus saturation at bounds up to 10^20.
  - Open: a nested `count … by` (e.g. `not once (count(…) by name > 3)`) is still quadratic in the window: 0.5 s at 80 events, then the 2 s budget keeps every event from about 150 events on.

### SG-A2.2 Bounded batch pipeline (AUDIT-2 A2-2 high)
- **Owner:** SG-A1.2/A2.1/A2.2 thread · **Status:** done · **Depends:** none (pairs with SG-A2.1)
- **Scope:** cap events per trace and per request (413/422 with a clear code), a soft time limit on `check_completed_trace` that records an unchecked or partial result instead of losing it, a dedicated Celery queue for policy checks so alerting and anomaly tasks are never behind them, and violations of other policies stored even when one policy's counterexample is slow.
- **Acceptance:** a trace over the cap is rejected on every ingest path; a batch check that hits the soft limit still stores each policy's violation row (empty or partial counterexample) and logs it; with two poisoned traces queued, a normal trace is checked and `evaluate_alert_rules` runs within 10 s (Celery repro in AUDIT-2.md); full backend suite green.
- **Verify:** `cd backend && pytest -q` · AUDIT-2.md **Celery repro** with `celery -A app.celery_app worker -c 2`
- **Result:** `pytest -q` 962 passed. Celery repro (Postgres 16, `-Q celery -c 2 --beat` and `-Q policy -c 2` workers): the normal trace was checked after 1 s (was 299 s), each 800-event poison trace in 1.4 s with both its rows (was hard-killed, 0 rows), and `evaluate_alert_rules` ran every minute throughout.
  - Caps: `MAX_TRACE_EVENTS` = 10 000 (`app/schemas/trace.py`). `TraceIn.events`/`tool_calls` and `EventsAppendIn.events` over it are 422 `too_long` (REST, and per trace in bulk; `/ws/ingest` answers `invalid_payload`); a bulk request over it in total is 422; an append that would take a streamed trace past it is 413 `trace_too_long` with `max_events` (`/ws/ingest`: `{"ok": false, "error": "trace_too_long"}`).
  - `check_completed_trace` has `soft_time_limit=240` (hard 300). Past the soft limit it logs an error and stores the verdicts with every-event counterexamples (`check_verdicts`, no solver), so a trace is never left with no rows.
  - Policy checks are routed to a `policy` queue. `docker-compose*.yml` run the existing worker with `-Q celery --beat` and a new `policy-worker` with `-Q policy`; a deployment with one worker must add `-Q celery,policy` or batch checks are never consumed.

### SG-A2.3 Precheck and append after monitor-state loss (AUDIT-2 A2-3 high, A2-5)
- **Owner:** SG-A2.3/A2.4 thread · **Status:** done · **Depends:** none
- **Scope:** after the Redis monitor state is gone, a trace is replayed from spans at most once (precheck writes the rebuilt state back, or an equivalent), the replay loads only the columns the checker needs, and rebuilds are bounded so a Redis restart can't stampede.
- **Acceptance:** `test_precheck_after_state_loss_replays_spans_once` passes; after `FLUSHDB`, the second and later prechecks on a 5 000-event trace are within the precheck row over HTTP (`load_e2e.py flush`); SG6 precheck semantics (stores nothing user-visible, `allowed` rule) unchanged.
- **Verify:** `cd backend && pytest tests/test_audit2.py -k precheck tests/api/test_precheck.py -q` · `python backend/scripts/load_e2e.py setup && python backend/scripts/load_e2e.py flush --traces 10 --prefix 5000`
- **Result:** verify passes (1 + 68 tests). A precheck that finds the Redis state missing or stale rebuilds it once per trace (Redis lock, re-read after waiting) and caches it under its own key, `policy:precheck:{trace_id}`; prechecks read the append path's key first, then that one. The cache never stands in for the append path's state, whose rebuild records the violations it finds (tested: a policy added while the state was gone still gets its live row). `final` deletes both keys. Rebuilds select only the span columns the checker reads and at most `MAX_CONCURRENT_REBUILDS = 4` run at once per process, on the append and precheck paths. Measured on Postgres + Redis, `uvicorn --workers 2`, after `FLUSHDB` on 5 000-event traces: the first precheck per trace 160–180 ms sequential (was 274–316 ms on every check); later checks p50 13.8 / p95 16.3 ms sequential, the same as with the state never lost (13.2 / 17.0 ms on this shared box; the in-process row is 3.7 ms p95); `load_e2e.py flush` now reports first and later checks separately (later: p95 66 ms at 10 concurrent traces, vs 1 785 ms). SG6 semantics unchanged. Open: Redis in `docker-compose*.yml` still has no `maxmemory`/persistence policy (A2-5), and the first append to each open trace after a restart still replays it once.

### SG-A2.4 AUDIT-2 mediums (A2-4, A2-6)
- **Owner:** SG-A2.3/A2.4 thread · **Status:** done · **Depends:** none
- **Scope:** serialise `policy_violations` alert firing per rule (one `AlertEvent` and incident per rule per dedup window under concurrent ingests); bound `count … by name` monitor state per trace (documented behaviour past the bound).
- **Acceptance:** `test_concurrent_violations_fire_a_rule_once` and `test_ingest_step_with_many_distinct_names_meets_budget` pass; the race repro in AUDIT-2.md A2-4 gives exactly one alert on Postgres.
- **Verify:** `cd backend && pytest tests/test_audit2.py tests/api/test_violation_pipeline.py tests/policy/test_monitor.py -q`
- **Result:** verify passes (both audit tests unmarked; A2-1/A2-7 still xfail for SG-A2.1). A2-4: after the unlocked dedup check passes, firing a `policy_violations` rule takes an in-process lock per rule and `SELECT … FOR UPDATE` on the rule's row (held until `fire_alert` commits), then re-checks `already_firing`. AUDIT-2's race (20 concurrent `drop_db` appends, one critical rule) gave 2, 2, 1 alerts on Postgres before and 1 alert and 1 incident in each of 6 runs after, with 4 workers. A2-6: a `count … by` node keeps exact counts for its first `MAX_GROUPS = 1000` values with a hit (zero counts are no longer stored); later values share one overflow counter, giving bounds on their count. When the bounds don't settle the comparison, the policy is false at that event and every later one, so nothing is missed (Hypothesis-checked against `reference.evaluate` with the bound at 0–3, across restores) at the cost of extra violations, and blocks under a `block` policy, on traces past 1 000 distinct counted names. Snapshot at 50 000 distinct names: 13 KB, restore+step+snapshot p95 0.4 ms (was 722 KB, 72 ms). The batch check stays exact.

### SG-A3.1 Streamed traces carry their trace fields (AUDIT-3 A3-1, A3-2 high)
- **Owner:** thread SG-A3.1 · **Status:** done · **Depends:** AUDIT-3
- **Scope:** `EventsAppendIn` gains the trace-level fields of `TraceIn` (`environment`, `agent_version`, `session_id`, `input`, `metadata` on the first append; `output`, `status`, `error_message`, tokens, cost, `latency_ms` with `final`), `PolicyCheckIn` gains `environment`; WS `events` message and TS types follow. The first append fixes the trace's environment (a later different value is 409). Policy scope, legacy rules (PII/injection on `input`/`output` text, masking as on the whole-trace path) and the stored row use them. The SDK sends them.
- **Acceptance:** the A3-1 and A3-2 tests pass unmarked; an environment-scoped policy gives the same verdict on whole-trace ingest, streamed append and `/check` for every violating fixture; old SDKs (no new fields) behave as today; SDK contract test updated.
- **Verify:** `cd backend && pytest tests/audit/test_audit_3.py -k "environment or streamed" tests/api/test_event_ingest.py tests/api/test_sdk_contract.py -q` · `cd sdk && pytest -q` · `cd frontend && npx tsc --noEmit`
- **Contract change (approved by this thread, 2026-10-14, under the Rules line on sound contract changes):** `EventsAppendIn` (REST and the WS `events` message) gains optional `environment`, `agent_version`, `session_id`, `input`, `output`, `status`, `error_message`, `latency_ms`, `tokens_input`, `tokens_output`, `cost_usd`, `metadata`; null means unchanged, any append may set them. The creating append fixes `environment` (default the agent row's, as before); a different one later is 409 `environment_mismatch` with `environment`, on the append and on `/check`. `PolicyCheckIn` gains optional `environment`, used when the trace is not stored yet. Backward compatible both ways: old SDKs send none and get today's behaviour; old servers ignore the extra fields. TS types follow.
- **Result:** verify passes; A3-1 and A3-2 tests unmarked. The legacy-rule view of an append scans the `input`/`output` it sent; a successful append stores the sent fields (text PII-masked with `pii_flags` and `injection_score` set, as on whole ingest; `metadata` replaces the user part of `meta`, `stream` kept). The SDK sends `environment` on every push, the starting fields on the push at seq 0, the outcome fields and metadata on `final`, and `environment` on every check. Tests: every violating fixture with its policies `block` scoped to `prod` (agent row `dev`) gives 403 on whole ingest, `/check` and the append agree event by event, block at the first violation, and nothing blocks under `staging`; fields stored and masked; no fields gives agent defaults; environment fixed by the first append; legacy injection rule on streamed input; SDK push and check bodies. Backend 1056 passed, SDK 112 passed, frontend tsc and 36 tests pass; budgets met (live p95 0.03 ms, ingest 0.06, precheck 5.8, batch 85, counterexample 254). A legacy block on the sent `input`/`output` text (checked apart from the events) is a 403 `policy_block` with `target: "trace"` (`"event"` otherwise, REST details and WS); the SDK reads it as fatal and stops streaming that trace instead of dropping a clean event. Open (low): after such a block on the final `output`, the trace stays open with its events stored (whole ingest stores nothing; A6-12's sweeper covers it); `/check` judges the candidate only, not the trace text; a stream opened before this change took the agent row's environment, so a new SDK with another environment gets 409 `environment_mismatch` on it (fatal for that one trace).

### SG-A3.2 `/check` applies legacy block rules (AUDIT-3 A3-3 high)
- **Owner:** thread SG-A3.2/A3.3 · **Status:** done · **Depends:** none
- **Scope:** `precheck` runs the legacy `PolicyRule` checks the append path runs on the candidate (`forbid_tool`, `forbid_pii` on its `input`, `block_on_injection`), so `allowed` is false exactly when the append would be a 403. How a legacy hit appears in `violations` is the thread's call within `ViolationOut`; a shape change is stop-and-report.
- **Acceptance:** the A3-3 test passes unmarked; for each legacy rule type, a candidate the append rejects is `allowed: false` and one it accepts is `allowed: true`; precheck budget row still met.
- **Verify:** `cd backend && pytest tests/audit/test_audit_3.py -k legacy_block tests/api/test_precheck.py -q` · `python backend/scripts/bench_policy.py --path precheck --assert-budget`
- **Result:** `precheck` runs `evaluate_policies` on the candidate alone (no trace text, the trace's or agent's environment), as the append path does. A block-rule hit makes `allowed` false and is listed first in `violations` as a `ViolationOut` with the `PolicyRule` id and name, `policy_version: 1`, `severity: "high"`, `at_seq` = the candidate's position, `mode: "precheck"`; warn/log hits are not listed (the append only audits them). Tests: A3-3 unmarked; `test_check_agrees_with_append_on_legacy_rules` (3 rule types × block/warn, rejected and accepted candidate, against the real append), scope and ordering tests in `tests/api/test_precheck.py`. Legacy rules are now read in `created_at` order, so `/check` and the append's `policy_block` audit name the same rule. Full backend suite 981 passed; precheck bench p95 6.3 ms (budget 10; the bench times `would_violate` only), and `/check` over the test client with 5 legacy rules (PII and injection scans on a 600-char input) adds 0.4 ms p50 / 1.4 ms p95.

### SG-A3.3 Sync checks in async streamed traces (AUDIT-3 A3-4 high)
- **Owner:** thread SG-A3.2/A3.3 · **Status:** done · **Depends:** none (SDK only)
- **Scope:** `Trace.check` on a streamed trace with an async client delivers pending events before checking (or waits for the in-flight push), so a sync `@guard` in an async agent is checked against the server.
- **Acceptance:** `sdk/tests/test_audit_3.py::test_sync_guard_in_async_streamed_trace_is_checked` passes unmarked; event order and `seq` stay correct when a sync check and an `add_event` push race; existing SDK tests unchanged.
- **Verify:** `cd sdk && pytest -q`
- **Result:** `Trace.check` pushes pending events synchronously with an async client too (a sync check already blocks on its HTTP call). A push now settles by the batch's start offset (`_sent = max(_sent, start + n)`; a `seq_conflict` at or past the batch start resyncs), so a sync push overlapping an in-flight async push neither double-counts nor kills the stream. Tests: A3-4 unmarked; `test_sync_check_overlapping_async_push_keeps_order` (fails on the old `_settle`). SDK suite 76 passed.

### SG-A3.4 AUDIT-3 mediums without contract changes (A3-6, A3-8, A3-9, A3-10, A3-11)
- **Owner:** thread SG-A3.4 · **Status:** done · **Depends:** none
- **Scope:** checks get their own rate-limit bucket (A3-6); `policy_violations` alert rules count only `alert` and `block` policies (A3-8, default picked; the alternative is dropping `alert`); a re-sent whole trace with a known `trace_id` is not stored or checked twice (A3-9); the SDK re-buffers `storage_error` bulk rejections and logs the others (A3-10); `/ws/ingest` enforces the org rate limit per message (A3-11).
- **Acceptance:** the five tests pass unmarked; full backend and SDK suites green.
- **Verify:** `cd backend && pytest tests/audit/test_audit_3.py -q && pytest -q` · `cd sdk && pytest -q`
- **Result:** `/check` uses its own `check:{org}` bucket at the org's per-minute limit. `policy_violations` rules (on ingest and in `compute_metric_value`) count only violations whose policy's current action is `alert` or `block`. `ingest_trace` returns the stored row when the same agent already sent the `trace_id`, before any policy check, so a resend is a 202 with the first id and no second check or alert (a lookup, not a unique constraint: two concurrent first sends can still both store). Both SDK clients re-buffer `storage_error` bulk rejections to the disk buffer and log the others; the async client now drains that buffer at construction, as the sync one does. `/ws/ingest` charges the org bucket per message and replies `{"ok": false, "error": "rate_limited"}` (limit read once per socket); its whole-trace branch now rolls back after an error. Tests: the five unmarked, plus a `policy_block` rejection logged and not re-buffered, async storage_error resent by the next client (SDK), a resend from another agent stored separately, and a `log` policy's violation left out of the metric. Accepted: the alert count uses the policy's current action (no snapshot on the violation row); a fail-open SDK still allows on a 429 from the check bucket. Backend 986 passed, SDK 78 passed.

### SG-A3.5 AUDIT-3 mediums that change the contract (A3-5, A3-7)
- **Owner:** thread SG-A3.5 · **Status:** done · **Depends:** none
- **Scope:** a non-streamed `Trace.check` sends its unsent events as a prefix in `PolicyCheckIn` (A3-5); `ViolationOut` says whether the counterexample is pending, minimal or a degraded fallback, and the UI stops guessing (A3-7).
- **Acceptance:** the A3-5 and A3-7 tests pass unmarked; precheck budget row met with a 200-event prefix in the request.
- **Verify:** `cd backend && pytest tests/audit/test_audit_3.py -k "non_streamed or counterexample" -q` · `cd sdk && pytest -q` · `cd frontend && npx tsc --noEmit && npm test -- --run`
- **Approval:** contract change approved by this thread under the standing rule (PR #31): both additions are backward compatible (a new optional input field and a new output field), and no other way closes either finding.
- **Contract change:** `PolicyCheckIn.prefix: list[EventIn]` (default `[]`, at most `MAX_TRACE_EVENTS`): events the server does not hold, evaluated after any it holds for `trace_uid` and before the candidate; their seqs are ignored like the candidate's; stored + prefix + candidate over the cap is 409 `trace_too_long`. `ViolationOut.counterexample_status: pending | minimal | fallback | none`, stored in `policy_violations.counterexample_status` (migration `0006`, SQLite and Postgres; existing live rows with an empty counterexample become `pending` on an open stream and `none` on a closed one, the rest `minimal`). `checker.Violation` gains `minimal: bool = True`; `counterexample.bounded_counterexample` returns `(seqs, minimal)` and `minimal_counterexample` is unchanged. TS types and the SDK's `check_request` (now always sends `prefix`) follow.
- **Result:** live rows are stored `pending`; the batch check fills each one it finds at the same event and version with its counterexample and status (an empty minimal counterexample included, which the old fill-in skipped) and marks the rest `none` (an older policy version, a refused call never stored). Batch rows are `minimal`, or `fallback` when the solver gave up, raised, the trace's 30 s budget was spent, or the soft time limit stored verdicts only. Pre-check results are `none`. The SDK sends a non-streamed trace's events (or tool calls) as `prefix` with kind, name, status and attrs only. `TraceDetail` reads the status: polling only while `pending` (still capped at 10 min, since an unfinished stream never gets its batch check), "may not be minimal" on `fallback`, "No counterexample" on `none`. Tests: A3-5 and A3-7 unmarked; prefix as history, after stored events, seqs ignored, cap 409 (`tests/api/test_precheck.py`); status through stream, batch, solver failure and verdicts-only (`test_event_ingest.py`); `minimal` flag on budget and deletion-pass give-ups; migration 0006 on SQLite and in the Postgres round trip; SDK prefix for events, tool calls and none when streamed; panel and polling (frontend). Open (review, not fixed here): a refused call's live row holds its (trace, policy, version) key, so a later batch violation of that policy is not stored (pre-existing since #27; the row now reads `none`); settling a pending row is not published on the websocket, so an open page sees it on its next poll or reload; an SDK with this change talking to an older server gets no history (deploy the server first); each non-streamed check resends the whole prefix, quadratic over a long trace (streaming avoids it). Precheck bench p95 6.6 ms (budget 10); `/check` over the test client with a 200-event prefix and 10 policies p95 28.9 ms vs 20.6 ms without, the difference being the monitor replay the bench row measures.

### SG-A4.1 Test database that does not hide missing commits (AUDIT-4 A4-1 high)
- **Owner:** thread SG-A4.1 · **Status:** done · **Depends:** none (tests only)
- **Scope:** the `engine`/`session_maker` fixtures stop sharing one connection between sessions (a file-backed SQLite with a normal pool, or Postgres where CI has it), so a session sees only committed rows; tests that relied on the sharing are fixed, not the code under test. Add a test that a batch check's rows and filled-in counterexamples are visible from a fresh session.
- **Acceptance:** `python backend/scripts/mutants_audit4.py A4-1` reports the mutant killed by the new test; full backend suite green with no code change outside `tests/`.
- **Verify:** `cd backend && pytest -q` · `python backend/scripts/mutants_audit4.py A4-1`
- **Result:** `engine` is a per-test SQLite file under `tmp_path` with the default pool, so each session has its own connection; no existing test relied on the sharing. `test_batch_check_commits_rows_and_counterexamples` reads a batch check's rows and filled-in counterexamples from a fresh session and kills the A4-1 mutant. For SG-A4.2: a Postgres run (A4-4) can swap the URL in this one fixture; sessions now hold real SQLite write locks, so a test that writes from two sessions at once waits up to the 30 s `timeout` instead of interleaving.

### SG-A4.2 AUDIT-4 mediums (A4-2 to A4-8)
- **Owner:** thread SG-A4.2 · **Status:** done · **Depends:** SG-A4.1 (done)
- **Scope:** async SDK tests for `acheck` with undelivered events, default fail-open, `check_timeout_s`, transient stream errors and resync (A4-2); move single-sample wall-clock asserts out of `pytest -q` into the bench job or make them repeated-sample (A4-3); run the API and service tests on Postgres in CI, covering the `on_conflict` insert and the `FOR UPDATE` alert lock (A4-4); tests for the solver timeout and the budget-spent warning (A4-5); backend and SDK coverage in CI with `concurrency = greenlet` and a floor (A4-6); a check of `frontend/src/types/policy.ts` against `app.openapi()` and a test of the API client's paths (A4-7); a rate-limit test on `/check` and the events route (A4-8). A4-9 to A4-12 (low) ride along where cheap, starting with A4-9's repro as a test.
- **Acceptance:** `python backend/scripts/mutants_audit4.py A4-2 A4-4 A4-5 A4-8` reports every mutant killed by a test this sub-goal added; A4-9's repro is a deterministic test; `test_audit_4.py` passes with both markers removed; two consecutive full CI runs green.
- **Verify:** `cd backend && pytest -q` · `cd sdk && pytest -q` · `python backend/scripts/mutants_audit4.py`
- **Result:** `mutants_audit4.py` (all findings, full suites, local Postgres 16) kills every mutant with a test this sub-goal added. A4-2: `sdk/tests/test_async_paths.py`. A4-3: single-sample wall-clock asserts are the median of 3 to 20 runs (`tests/timing.py`); p95 budgets stay with `bench_policy.py`. A4-4: `engine` runs on Postgres for tests marked `postgres` (`tests/api/test_postgres.py`: upsert dedup, row lock across workers with per-worker in-process locks) and for every test under `TEST_DB=postgres`, which CI runs over `tests/api`, `tests/audit` and `test_audit2.py`; the marker skips only where no Postgres is reachable and `CI` is unset. A4-5: solver timeout spy and no budget warning from `check_verdicts`. A4-6: `backend/.coveragerc` (greenlet), CI floors 72 % backend (74.6 % now), 90 % policy code (98 % now), 80 % SDK (84 % now). A4-7: `tests/api/test_frontend_contract.py` checks every `frontend/src/api` call against OpenAPI routes and `types/policy.ts` fields and optionality against the schemas (the mutant script now copies `frontend`). A4-8: `/check` and events route 429 tests. A4-9: deterministic repro in `test_monitor.py`. A4-10: empty stub tests removed, viewer assertion exact, stale skip gone. Not done: A4-11 (other lows), A4-12 (lockfile).

### SG-A5.1 Policy cost linear in its size (AUDIT-5 A5-1 critical)
- **Owner:** thread "Fix tag blow-up" · **Status:** done · **Depends:** AUDIT-5
- **Scope:** every pass over a parsed formula visits each distinct node once: `monitor.Plan._add`, the plan cache key (`plan_for`'s `repr` and `_plan`'s hash of the `Policy`), `encode.TraceEncoding._encode`, `counterexample._nodes`, and anything else that recurses over the AST. Backstop: create, update and validate reject a policy whose expanded size or source length is over a documented cap (422 `invalid_policy`). No AST or contract change.
- **Acceptance:** `test_policy_cost_is_linear_in_source_size` passes unmarked; a 30-level doubling-tag policy can be created (or is rejected with a 422) and `/check`, an append and a batch check on it each finish within their budget rows; existing differential and fixture tests unchanged.
- **Verify:** `cd backend && pytest tests/audit/test_audit_5.py -k linear tests/policy -q` · `python backend/scripts/bench_policy.py --path live --assert-budget`
- **Result:** `encode.Dag` hash-conses a formula, visiting each node object once with flat keys (child indices); `monitor.Plan`, `TraceEncoding.values` and `counterexample._nodes` build on it. Plans are cached per `Policy` object and digested over the DAG keys (old Redis snapshots fail the digest check once and are rebuilt from spans). `parse` rejects a source over 20 000 characters and a policy over 2 000 atoms and operators with tags expanded (`PolicyParseError`, so 422 `invalid_policy` on create and update, `ok: false` on validate); documented in `docs/policy-dsl.md`. A 40-level doubling formula built in code (2^40 leaves) is monitored, checked and given a counterexample in milliseconds.

### SG-A5.2 Minimal counterexamples for large count bounds (AUDIT-5 A5-2 high)
- **Owner:** thread "Fix count counterexamples" · **Status:** done · **Depends:** AUDIT-5
- **Scope:** counts with bounds of 16 and more (plain, `by name`, `by kind`) get minimal counterexamples within the 500 ms row at 200 events, e.g. an "at least k" counter linear in window × bound for any bound up to the window size, or a deletion pass seeded with the count's hits. `bench_policy.py --path counterexample` gains a bound-40 case.
- **Acceptance:** `test_large_bound_count_counterexample_is_minimal` passes unmarked; the SG3 sufficiency and minimality property tests pass with bounds up to 64; counterexample bench row met with the new case.
- **Verify:** `cd backend && pytest tests/audit/test_audit_5.py -k large_bound tests/policy/test_counterexample.py -q` · `python backend/scripts/bench_policy.py --path counterexample --assert-budget`
- **Result:** counts are unary ("at least k") whenever window × (bound + 1) ≤ 50 000 (`_UNARY_CELLS`), bit-vector adders above. The bound-40 audit case keeps the 41 hits; bench counterexample p95 300 ms with a p9 (bound 40) case added; new property test with bounds up to 64 over 70-event traces. Open: `count … by name` over globs (string names) is slow at any bound, 3 included: its deletion calls hit the 2 s budget (bound-20 row now 21 events, minimal, still 2 s); 1 000 events × bound 100 also runs out of budget after finding the minimal 101.

### SG-A5.3 A blocked append does not end enforcement (AUDIT-5 A5-3 high)
- **Owner:** thread "Fix enforcement after a blocked call" · **Status:** done · **Depends:** AUDIT-5
- **Scope:** on a `block` 403 the server still rejects the events but stores the blocking violation (`mode: "live"`, published and alerted like other live rows) and keeps the trace open. The SDK treats a `policy_block` 403 as a refused batch, not a dead stream: it drops the refused events, renumbers the rest, keeps checking against the server, and sends `final` on exit. Response shapes unchanged.
- **Acceptance:** both A5-3 tests pass unmarked; under a status-based `block` policy, a guarded call after a refused append is checked by the server; the trace closes and gets its batch check; SG-A1.1's "403 exactly when `/check` says `allowed: false`" tests still pass.
- **Verify:** `cd backend && pytest tests/audit/test_audit_5.py -k "blocked_append" tests/api/test_event_ingest.py tests/api/test_precheck.py -q` · `cd sdk && pytest -q`
- **Result:** a trace policy `block` 403 on an append commits the trace (still open), the `policy_block` audit row and the blocking policy's violation at the refused event (`mode: "live"`, published and alerted); the events and the monitor state are not stored, so the next append continues at the same seq. Legacy `PolicyRule` blocks are unchanged (rolled back, audit row only). The SDK reads a `policy_block` 403 as `refused`: a refused multi-event batch is resent one event at a time to find the refused one, which is dropped from `Trace.events` with the rest renumbered; streaming, checks and `final` carry on. The batch check fills a live row's counterexample only when it found that policy's violation at the same seq. Not stored: violations of other policies at the refused event, or at events before it in the refused batch (they are stored when resent). Open (low): the row's `at_seq` is the refused event's seq, which the next accepted event reuses; a refused `final` leaves the trace open until the client resends `final` (the SDK does; A6-12's sweeper covers other clients).

### SG-A5.4 AUDIT-5 mediums (A5-4, A5-5)
- **Owner:** thread "Round 2 mediums: counterexample limit, sequence numbers" · **Status:** done · **Depends:** none
- **Scope:** the per-counterexample budget holds on every path, including encoding and SMT parsing (estimate nested `count … by` size up front and fall back at once; check the deadline in `_group_count`) (A5-4); `Trace.add_event` assigns seqs under the trace's lock (A5-5). A5-6 (RecursionError → 422) rides along. Also SG-A5.2's open item: `count … by name` over globs runs out of budget at any bound.
- **Acceptance:** `test_counterexample_encoding_stays_within_what_the_budget_can_parse`, `test_deeply_nested_policy_is_a_validation_error` and `sdk/tests/test_audit_5.py` pass unmarked; full backend and SDK suites green.
- **Verify:** `cd backend && pytest tests/audit/test_audit_5.py -q && pytest -q` · `cd sdk && pytest -q` · `python backend/scripts/bench_policy.py --path counterexample --assert-budget`
- **Result:** A5-4: every count charges its cells (window × bound terms, `count … by` ones 3×) against `_MAX_CELLS` = 100 000 before building them, and the charge checks the deadline, so an encoding too large to build and parse within the budget gives up at once with the every-event fallback (the 300-event nested case: 0.1 s, was 4 s; 20 counts × 1 000 events: 0.1 s, was 55 s). Nested `count … by` inside `once` stays exact up to about 115 events. Follow-up from SG-A5.2: `count … by name` with globs no longer uses string names: a name is an identity code, with the codes of the names matching each glob set in one block (observed names, then fresh ones up to how many names that set has, `pattern_sizes`), so a glob is a range check; the bound-3 and bound-20 cases are minimal in 0.1 and 0.3 s (were 2 s, not minimal). Counterexample bench p95 unchanged (434 ms). A5-5: `add_event` takes `_lock` around the seq and the append. Open (low, from review): with an async client, an `add_event` on the loop waits on `_lock` while a sync `check()` in another thread is sending; the async refused-batch renumbering runs under `_alock`, not `_lock`. A5-6: fixed by SG-A6.4's depth bound (A6-10); `test_deeply_nested_policy_is_a_validation_error` passes unmarked.

### SG-A6.1 SDK delivers every trace (AUDIT-6 A6-1, A6-2 high)
- **Owner:** thread "SDK drops traces on shutdown" · **Status:** done · **Depends:** none (SDK only)
- **Scope:** `close()`, `flush()` and the `atexit` hook of both clients send the whole queue in requests of at most `batch_size` traces (the async `flush()` currently ignores `batch_size`) and buffer what they can't send (A6-1). Requests are also split so none holds more than the server's 10 000 events; a single trace over the cap is logged and dropped, never buffered, since no retry can succeed (A6-2). The sync-client fixture stops making `close()` a no-op, so close is tested; tests kill A6-16's three SDK mutants.
- **Acceptance:** `sdk/tests/test_audit_6.py` A6-1 and A6-2 tests pass unmarked; tests for an over-cap single trace and for async `flush()` batching; SDK suite and coverage floor green.
- **Verify:** `cd sdk && pytest -q` · `python backend/scripts/mutants_audit6.py` (SDK rows killed)
- **Result:** `utils.batches` splits a flush into requests of at most `batch_size` traces and 10 000 events; a single trace over the cap is logged and dropped, not buffered. Both clients' `flush()` (and so `close()`, `atexit`, and each flusher tick) send the whole queue that way, and once the server can't be reached the rest of that flush goes straight to the disk buffer; the sync flusher waits on `_stop` instead of sleeping, so `close()` wakes it. The `make_client` fixture no longer sets `_stop`, so `close()` is tested; it unregisters the `atexit` hook at teardown. `tests/test_delivery.py` covers the splitting, close buffering, both flushers and kills A6-16's three SDK mutants (the async-flusher mutant in `mutants_audit6.py` is updated to the new loop).

### SG-A6.2 A rejected stream does not switch the guard off (AUDIT-6 A6-3 high)
- **Owner:** thread "Fix enforcement after a blocked call" · **Status:** done · **Depends:** SG-A5.3 (same code; can share its thread)
- **Scope:** SG-A5.3 turns a `policy_block` 403 into a refused batch; this covers the other fatal rejections (`trace_too_long`, `trace_closed`, `agent_mismatch`, 422): later checks of the trace still go to the server with the `trace_uid`, so the delivered prefix keeps counting, instead of failing open locally. Default picked; the alternative is failing per `fail_closed` but logging at ERROR. Best done in the same thread as SG-A5.3. `test_stops_streaming_on_rejection` is updated to the new behaviour.
- **Acceptance:** the A6-3 test passes unmarked; a test per fatal code for the check that follows it; sync and async clients behave the same.
- **Verify:** `cd sdk && pytest -q` · `cd backend && pytest tests/api/test_sdk_contract.py -q`
- **Result:** with SG-A5.3. The other rejections still stop streaming the trace (logged once), but under the default fail-open `_undelivered` no longer fails checks of a dead stream locally: they go to the server with the `trace_uid` and are judged on the events it holds. A `fail_closed` client still fails them. Checks still fail per `fail_closed` while events are only pending (retryable failures). `test_stops_streaming_on_rejection_but_keeps_checking` covers `seq_conflict`, `trace_closed`, `agent_mismatch`, `trace_too_long` and 422.

### SG-A6.3 Summary report scoped to the caller's org (AUDIT-6 A6-6 high)
- **Owner:** thread "Reports tenant leak" · **Status:** done · **Depends:** none
- **Scope:** `org_summary`'s `recent_evals` only lists runs whose evaluation set (or agent) belongs to the caller's org; check the other `report_service` and report-task queries for the same omission.
- **Acceptance:** the A6-6 test passes unmarked; a test that the PDF and weekly digest builders see only the org's runs.
- **Verify:** `cd backend && pytest tests/audit/test_audit_6.py -k summary tests/api/test_reports_violations.py -q` (and with `TEST_DB=postgres`)
- **Result:** `recent_evals` joins `EvaluationSet` and filters on the caller's org (A6-6 unmarked). The same omission was in `POST /api/evaluations/diff`, which read any org's runs and per-case results by id; it now 404s on a run outside the org. The PDF reuses `org_summary` and the weekly digest reads no evaluation runs; the CSV exports, `get_run`, `list_runs` and the promote eval gate were already scoped. Tests: `test_summary_lists_only_own_evaluation_runs`, `test_eval_diff_rejects_other_orgs_runs`.

### SG-A6.4 AUDIT-6 mediums without contract changes (A6-4, A6-5, A6-7, A6-10, A6-12; A6-16 rides along)
- **Owner:** thread "Fix remaining Audit A6 mediums" · **Status:** done · **Depends:** none
- **Scope:** a 401, 404 or 422 from `/check` is a failed check that does not allow, logged at ERROR (A6-4; outages stay `fail_closed`'s call). The disk buffer lives in a per-user directory created `0700`, the file `0600`, and a directory owned by another user is refused (A6-5). `@guard` serialises guarded calls within one trace from check through record (A6-7; default picked, the alternative is recording the event before the call runs). Create and update reject a formula deeper than a fixed bound with a 422, and `active_policies` skips a policy whose monitor plan can't be built (A6-10; coordinate with SG-A5.1 and A5-6). A Celery beat task closes streamed traces with no append for a fixed idle time and queues their batch check (A6-12). Tests kill A6-16's three backend mutants.
- **Acceptance:** the A6-4, A6-5, A6-7 and A6-10 tests pass unmarked; a test that an idle open trace is closed and checked and a recent one is not; precheck and live bench rows still met.
- **Verify:** `cd backend && pytest -q && TEST_DB=postgres pytest -q tests/api tests/audit` · `cd sdk && pytest -q` · `python backend/scripts/bench_policy.py --path precheck --assert-budget` · `python backend/scripts/mutants_audit6.py` (every row killed)
- **Result (A6-10 only, done early because the strict xfail started passing by chance after SG-A5.1):** `parse` rejects a formula nested over 64 deep with tags expanded (`MAX_FORMULA_DEPTH`; depth is tracked as nodes are built, and the parser's own nesting, parentheses included, is bounded by the same number), so 422 `invalid_policy` on create and update, `ok: false` on validate, and `active_policies` skips a stored policy over it as unparseable. At the bound the parser uses about 450 frames and the monitor, batch check and counterexample about 110. The A6-10 test now asserts the 422 and that policies at the bound pass `/check` and append. The rest of SG-A6.4 is still open.
- **Approval:** two small contract changes, approved under Ashton's standing rule (2026-10-14 17:50Z): `meta.stream` gains the value `"idle"` (a trace the sweeper closed; append is `409 trace_closed`, the batch check runs), and the SDK never allows on a 401, 403, 404 or 422 from `/check`, whatever `fail_closed` says.
- **Result (rest):** A6-4: `check_result` treats 401, 403, 404 and 422 as configuration errors: `allowed: false`, logged at ERROR with the server's message; 409, 429, 5xx, timeouts and network errors still follow `fail_closed`. A6-5: the buffer is `<tempdir>/agentops_sdk-<uid>/` created `0700` (an own directory with a looser mode is tightened); one owned by another user, a symlink or a file is refused, logged at ERROR, for a fresh `mkdtemp` (that run's leftovers are not replayed next start). The file is opened `O_CREAT|O_APPEND|O_NOFOLLOW`, `0600`. Buffers at the old shared path are not migrated: reading them is the attack. A6-7: `@guard` holds a per-trace lock from check through record (`RLock` for sync calls, `asyncio.Lock` for coroutines, with a context variable so a guarded call nested in another passes through); calls in different traces still run concurrently, and a sync and an async guarded call in one trace do not serialise against each other. A6-12: each append records its trace in the Redis sorted set `streams:open` (scored by time, under the append lock; `final` removes it). Beat task `close_idle_streams` (every 5 min, policy queue) closes traces idle for `STREAM_IDLE_S` = 1 h, at most 1 000 per run, skipping one an append holds, sets `meta.stream = "idle"`, drops the monitor state and queues the batch check, which settles the pending live rows. Open: the set lives in Redis, which has no persistence (known gap), so a Redis loss forgets open traces, and traces open before this deploy are not tracked. A6-16: the 3 SDK mutants were already killed by SG-A6.1; new tests kill the revoked key on `/ws/ingest` and precheck taking the agent's environment for a stored trace (that mutant's text updated after SG-A3.1). Killing the rollback mutant found a real bug: `/ws/ingest` kept one session per socket with `expire_on_commit=False`, so after any append it served the trace row it had cached and accepted events to a trace another connection had closed. Each message now gets its own session; the rollbacks, and so that mutant, went with it, and `test_ws_events_see_writes_made_elsewhere` fails on the old handler. `mutants_audit6.py` now replays the other five. Tests: `sdk/tests/test_buffer.py`, guard concurrency and nesting in `test_guard.py`, config errors on both clients in `test_precheck.py`, `backend/tests/api/test_idle_streams.py`, `test_ws_events_see_writes_made_elsewhere`, `test_ws_ingest_rejects_revoked_key`, `test_check_uses_the_stored_traces_environment`; A6-4, A6-5 and A6-7 unmarked; `test_sdk_check_with_a_wrong_key_does_not_allow` replaces the old fail-open assertion.

### SG-A6.5 Violation history outlives its policy (AUDIT-6 A6-8 medium)
- **Owner:** thread "Keep violation history" · **Status:** done · **Depends:** none
- **Approval:** the storage-contract change was approved by Ashton on 2026-10-14 17:14Z ("Keep history" on the decision card: deleted policies are kept as archived, and their past violations stay in alerts and reports).
- **Scope:** deleting a policy keeps its violations in the trace view, reports and alert counts (soft delete, or `policy_id` made nullable with `ON DELETE SET NULL` and `ViolationOut.policy_id` optional); the SQLite test engine enables foreign keys so both databases agree (A6-9).
- **Acceptance:** the A6-8 and A6-9 tests pass unmarked on both databases.
- **Verify:** `cd backend && pytest -q && TEST_DB=postgres pytest -q tests/api tests/audit`
- **Result:** soft delete. `trace_policies.archived_at` (migration `0005_policy_archive`); `DELETE` sets it, and an archived policy is a 404 to `PUT`/`DELETE`, never enforced (`active_policies`, so live, batch, whole-trace ingest and `/check`), and left out of `GET /api/trace-policies` unless `?include_archived=true`. `TracePolicyOut` and the TS type gain `archived_at`. The unique `(org_id, name)` index is partial (`archived_at IS NULL`) so a deleted policy's name can be reused. Its violations stay in the trace view, `violations.csv`, the summary and the `policy_violations` alert count. The SQLite test engine turns on foreign keys (A6-9); nothing else depended on them being off. 0001-0004 are Postgres-only, so 0005 is tested both ways on Postgres (full chain) and on SQLite (from a `create_all` schema); downgrading keeps archived policies (and their violations), disabled and renamed to their ids.

### SG-A7.1 Linear-time glob matching (AUDIT-7 A7-1 critical)
- **Owner:** SG-A7.1 thread · **Status:** done · **Depends:** AUDIT-7
- **Scope:** `encode.glob_regex`/`matches` (and `reference.py`'s own matcher, and `counterexample._hits`) match `*` and `?` without regex backtracking, e.g. the two-pointer wildcard algorithm; semantics unchanged (whole name, case-sensitive, code points).
- **Acceptance:** both A7-1 tests pass unmarked; `*a` x 10 + `b` against a 255-character name of `a`s matches (false) in under 1 ms; differential, fixture and counterexample tests unchanged; live and precheck bench rows met.
- **Verify:** `cd backend && pytest tests/audit/test_audit_7.py -k glob tests/policy -q` · `python backend/scripts/bench_policy.py --path live --assert-budget`
- **Result:** both pass; A7-1 markers removed. `encode.glob_match` (replaces `glob_regex`) splits a glob at `*` into fixed-length parts matched leftmost-first; `reference.glob_match` is an independent two-pointer matcher; both checked equal to the naive recursive oracle on 2 000 generated cases. `*a` x 10 + `b` against 255 `a`s: 0.15 ms (encode), 0.25 ms (reference). Live p95 0.06 ms; batch, counterexample and precheck rows met.

### SG-A7.2 Batch check linear in atoms x events (AUDIT-7 A7-2 high)
- **Owner:** SG-A7.2 thread · **Status:** done · **Depends:** AUDIT-7
- **Scope:** batch verdicts in time linear in distinct atoms x events (e.g. first violations from `Monitor`, already differential-tested equal to `check_trace`, or observed constants folded before solving), so the soft-limit retry is not the same work again; atom terms charged against the counterexample cell cap with the deadline checked while building, so `BUDGET_S` holds for every policy. No contract change.
- **Acceptance:** both A7-2 tests pass unmarked; a 990-atom policy on a 10 000-event trace stores its verdict and a (fallback) counterexample within the Celery soft limit; batch and counterexample bench rows met; differential and SG3 property tests unchanged.
- **Verify:** `cd backend && pytest tests/audit/test_audit_7.py -k "batch or counterexample" tests/policy -q` · `python backend/scripts/bench_policy.py --path batch --assert-budget` · `python backend/scripts/bench_policy.py --path counterexample --assert-budget`
- **Result:** both A7-2 tests pass unmarked (100 atoms x 1 000 events: verdict in milliseconds; 150 atoms x 1 000 events with `BUDGET_S` = 0.2: immediate fallback). `checker.first_violations` now gives batch verdicts from the monitor's plans with exact `count ... by` groups (no `MAX_GROUPS` overflow), linear in policy nodes x events, with attr-free atoms memoised per (kind, name, status), so the soft-limit retry repeats only that. Z3 still re-derives each violation in its counterexample search, and the Z3 encoding (`encode.first_violations`, no longer on the batch path) is held equal to `reference.evaluate` and to the batch verdicts in the differential tests. `FactEncoding` charges distinct atoms x window against `_MAX_CELLS` before building anything, so `BUDGET_S` holds for atom-heavy policies. A 990-atom (17.8 KB) policy on a 10 000-event trace: verdict plus fallback counterexample in 3.6 s (was over 240 s). Batch p95 32 ms (was 83), counterexample p95 257 ms. Open, for SG-A7.3: total batch work is still policy nodes x events summed over an org's policies, with no cap on how many policies an org has (about 3.6 s per 990-atom policy at 10 000 events with repeated names, more with all-distinct names, so some tens of such policies reach the soft limit); a per-org work bound belongs with A7-3's.

### SG-A7.3 Bounded policy work on the API path (AUDIT-7 A7-3, A7-4 high)
- **Owner:** thread SG-A7.3 · **Status:** done · **Depends:** AUDIT-7
- **Scope:** monitor replay and stepping in `precheck`, `append_events` and `ingest_trace` run off the event loop, and a request whose policy work (policy nodes x events) is over a documented bound is refused with a clear 413/422 code (A7-3); the monitor snapshot is bounded in bytes per trace, e.g. `count ... by name` groups keyed by a fixed-size digest and `MAX_GROUPS` counted per monitor rather than per node, keeping SG-A2.4's overflow rule (A7-4). A new error code or limit is a contract change, approved under the standing rule if sound.
- **Acceptance:** the A7-3 and A7-4 tests pass unmarked; the overflow rule stays Hypothesis-checked against `reference.evaluate` across restores; live, ingest and precheck bench rows met.
- **Verify:** `cd backend && pytest tests/audit/test_audit_7.py -k "stall or snapshot" tests/policy/test_monitor.py tests/api -q` · `python backend/scripts/bench_policy.py --path ingest --assert-budget` · `python backend/scripts/bench_policy.py --path precheck --assert-budget`
- **Contract change (approved by this thread, 2026-10-15, under the Rules line on sound contract changes):** a request whose policy work (events x monitor nodes of the policies it runs) is over `MAX_REQUEST_WORK` = 2 000 000 (about a second) is refused with 413 `policy_work_too_large` (`work`, `max_work`) and stores nothing: `/check` counts prefix + candidate against every active policy, an append its events against every active policy, a trace upload its events against `block` policies only (`/ingest/bulk` rejects that trace with the same reason). An org has at most `MAX_ORG_POLICIES` = 100 non-archived policies and `MAX_ORG_POLICY_NODES` = 4 000 monitor nodes over them; create or source update past either is 422 `policy_limit`. The SDK treats a 413 from `/check` as a configuration error and blocks, whatever `fail_closed` (an append 413 is fatal, as before). `count ... by` keeps 1 000 values per node and 10 000 per monitor, values over 32 characters as a 96-bit blake2b digest; the snapshot format is `v: 2`, so `v: 1` states are rebuilt from spans once. Policies stored before this that exceed the org caps keep running; their requests may get 413.
- **Result:** verify passes. Both A7-3 and A7-4 tests unmarked: the 500-atom `/check` over 9 998 events is now an immediate 413, and the 300-node snapshot is about 330 KB (was 73.5 MB). Monitor stepping, span rebuilds, snapshot and restore run in `asyncio.to_thread` on the append, `/check` and upload paths; a 1 001-node policy over 1 990 events (just under the cap) stalls the event loop under 0.2 s (`test_work_within_the_bound_runs_off_the_event_loop`). Property test of the group bound now also varies the per-monitor bound (0 to 6) and digests every value (`MAX_KEY_CHARS` = 0), across restores, against `reference.evaluate`. The batch memo of PR #40 is cleared past 100 000 entries: 4 000 nodes x 10 000 all-distinct names took 155 s at 9 900 nodes with the memo growing to tens of millions of entries, now 33 s and 141 MB at 3 962 nodes (9 s with repeated names), inside the 240 s soft limit. Bench: live p95 0.03 ms, ingest 0.09, precheck 4.3 to 4.7 (3.9 before; the per-monitor group count is kept incrementally), batch 35, counterexample 262. Backend 1101 passed on SQLite, SDK 138 passed. Open (low): a span rebuild after Redis loss is not counted against the request (bounded by the org caps, about 30 s worst case off the loop, at most 4 per process); the rate limit still counts requests, not work.

### SG-A7.4 A paused stream is not cut off (AUDIT-7 A7-5 medium)
- **Owner:** thread SG-A7.4 · **Status:** done · **Depends:** none. Also fixes AUDIT-8's A8-2.
- **Scope:** an append to a trace the idle sweeper closed (`meta.stream = "idle"`) reopens it (back to `open`, re-tracked in `streams:open`, monitor rebuilt from spans) instead of a 409; the batch check runs again on the next close and stores nothing twice. Default picked; the alternative is a configurable idle window several times longer. Documented in `docs/policy-dsl.md` or the API docs.
- **Acceptance:** the A7-5 test passes unmarked; a reopened trace's later violations are stored live and by its next batch check; `final` traces stay closed (409).
- **Verify:** `cd backend && pytest tests/audit/test_audit_7.py -k idle tests/api/test_idle_streams.py tests/api/test_event_ingest.py -q`
- **Contract change (approved by this thread, 2026-10-15, under the Rules line on sound contract changes):** an append (REST or `/ws/ingest`, accepted or refused by a trace policy block; a legacy rule's block rolls back and leaves it idle) to a trace with `meta.stream = "idle"` sets it back to `open` and re-tracks it; only `final` traces answer 409 `trace_closed`. The monitor is rebuilt from spans (the sweep dropped its state) and violations already stored are not stored again; the next close re-runs the batch check. `status` stays `timeout` until a `final` append sets it. The batch check stores its result under the trace's append lock (without it if Redis is down) and stores nothing if the trace's stream changed while it ran (reopened, or reopened and finalised), leaving pending rows for the next close. PR #35 gaps: when `streams:seeded` is absent from Redis (a Redis loss, or traces opened before tracking began), the sweep re-tracks every trace Postgres holds as `open` from its last stored event (span `started_at`, else `created_at`, capped at now), in chunks, then sets the key; an idle one is closed at once with its true latency, and a later append reopens it. Documented in `docs/policy-dsl.md` (Streamed traces).
- **Result:** verify passes. `test_append_after_an_idle_close_is_accepted` (A7-5) and `test_events_after_an_idle_pause_are_stored` (A8-2) unmarked; `test_check_after_idle_close_sees_undelivered_events` now asserts the stream carries on (the event cap test still covers a stopped stream). New in `test_idle_streams.py`: reopen with later live and batch violations and a 409 after `final`, a refused append reopening, a batch racing a reopen, a batch waiting for an append in progress, re-tracking after a Redis loss. Backend 1112 passed on SQLite, Postgres subset 414 passed; ingest p95 0.11 ms. Open (low): the re-tracking scan reads every open trace (unindexed JSON filter) once per Redis loss; a span `started_at` comes from the client, so a re-tracked trace with skewed clocks closes early (a later append reopens it).

### SG-A8.1 Checks of a stopped stream see the whole trace (AUDIT-8 A8-1 high)
- **Owner:** thread SG-A8.1 · **Status:** done · **Depends:** AUDIT-8
- **Scope:** a check of a streamed trace whose stream stopped (any fatal rejection, `trace_closed` from the idle sweep included) sends the events the server lacks (`events[_sent:]`) as `prefix` with the `trace_uid`, so it is judged on the whole trace (SDK only; `/check` already evaluates a prefix after the stored events). `/check` on a trace at the event cap answers from the stored events and prefix instead of refusing (the cap bounds only the prefix); if it must refuse, it answers 413 as the append does (A8-5) and the SDK does not allow on it.
- **Acceptance:** `test_check_after_idle_close_sees_undelivered_events`, `test_check_at_the_event_cap_does_not_allow` and `test_check_and_append_agree_on_trace_too_long` (if the refusal is kept) pass unmarked on SQLite and Postgres; the SDK suite's `FakeServer` tests for A6-3 assert the prefix sent.
- **Verify:** `cd backend && pytest -q tests/audit/test_audit_8.py tests/api && TEST_DB=postgres pytest -q tests/audit/test_audit_8.py` · `cd sdk && pytest -q` · `python backend/scripts/bench_policy.py --path precheck --assert-budget`
- **Contract change (approved by this thread, 2026-10-14, under the Rules line on sound contract changes):** `/check` no longer refuses a trace at the event cap: stored events + `prefix` + candidate over `MAX_TRACE_EVENTS` is answered like any other check (it stores nothing; the schema still bounds `prefix` to `MAX_TRACE_EVENTS`, 422 beyond), replacing SG-A3.5's 409 `trace_too_long`. Other `TraceConflict`s on `/check` now answer with their own status (all 409 today). Refusing with 413 instead would block every guarded call once a trace hits the cap, so answering was picked. The SDK sends a stopped stream's `events[_sent:]` (kind, name, status, attrs) as the check's `prefix` with its `trace_uid`; old servers evaluate it the same way (SG-A3.5).
- **Result:** verify passes. A8-1 tests unmarked; A8-5's `test_check_and_append_agree_on_trace_too_long` became `test_check_answers_a_trace_at_the_event_cap` (the refusal is gone). `test_prefix_past_the_trace_cap_is_409` became `..._is_still_checked`; the SDK A6-3 and stopped-stream tests assert the prefix sent; `mutants_audit8.py` drops the cap-boundary row and probes the new prefix (killed: 6 SDK tests fail). Backend 1091 passed on SQLite, Postgres subset 397 passed, SDK 137 passed; precheck p95 3.9 ms. Open (low): a check of a stopped stream resends all its undelivered events each time (bounded by the cap, 422 past it, which the SDK blocks on); after a fatal `seq_conflict` with no `expected_seq` the prefix may repeat events the server already holds (the server always sends `expected_seq`).

### SG-A8.2 AUDIT-8 mediums (A8-3, A8-4, A8-6)
- **Owner:** thread SG-A8.2 · **Status:** done · **Depends:** none. A8-2 is AUDIT-7's A7-5: SG-A7.4 fixes it and unmarks `test_events_after_an_idle_pause_are_stored` too.
- **Scope:** `@guard` neither lets calls gathered inside a guarded call skip the per-trace lock nor deadlocks a guarded call that awaits another task's guarded call (the fixing thread picks how; recording the event before the call runs, then releasing the lock, is one way, at the cost of the event's status and latency) (A8-3). `/ws/ingest` re-checks the API key's `revoked_at` per message and closes the socket with 4401 once revoked (A8-4). A streamed trace with tool calls and no events sends them as the check's prefix (A8-6).
- **Acceptance:** the A8-3, A8-4 and A8-6 tests pass unmarked on SQLite and Postgres; `python backend/scripts/mutants_audit8.py` still kills every `probe` row.
- **Verify:** `cd backend && pytest -q && TEST_DB=postgres pytest -q tests/api tests/audit` · `cd sdk && pytest -q`
- **Contract change (approved by this thread, 2026-10-15, under the Rules line on sound contract changes):** `@guard` records a call's event when the call starts (after its check, under the per-trace lock) and sets its status and latency when it ends, unless the event was sent meanwhile (background pushes stop short of a running call's event; a check or the trace's exit sends it as is); the lock covers check and record only, not the call, so the context-variable pass-through is gone. Nested, gathered and worker-task guarded calls each take the lock in turn and none can deadlock, at the cost the scope names: with `stream=True`, an event a later check sends while its call still runs (gathered or nested guarded calls) stays `success` with latency 0, and an outer call's event now precedes its nested calls'. `/ws/ingest` re-reads the key per message; once revoked or deleted it answers `{"ok": false, "error": "unauthorized"}` and closes with 4401, before the rate limit. Guard `input`/`output` attrs fill the recorded event's fields, as they did through `add_event`. The idle sweep sets `status = "timeout"` and `latency_ms` (first to last append) on the traces it closes (A8-7).
- **Result:** verify passes. A8-3, A8-4 and A8-6 tests unmarked. A8-6: a streamed trace with tool calls checks like an unstreamed one (tool calls as `prefix`, no `trace_uid`). A8-7 and A8-8 (lows) fixed: new tests kill both surviving mutants (an append between the sweep's listing and its lock; a pending row of an older version at the batch violation's seq), and `mutants_audit8.py` adds probes for the guard lock, the outcome update, the tool-call prefix, the revoked socket and the timeout status. A8-5's `agent_not_found` 400 vs 404 left as is (the SDK does not allow on either). Tests: `test_guard.py` (overlap, outcome set at end, threads), `test_precheck.py` (streamed tool calls), `test_idle_streams.py` (timeout, append after listing, older pending row). For SG-A7.4: a reopened idle trace keeps `status: timeout` until its `final` append sets the outcome.

### SG-A9.1 Policy work counts globs (AUDIT-9 A9-1 critical)
- **Owner:** thread SG-A9.1 · **Status:** done (PR #45) · **Depends:** AUDIT-9
- **Scope:** the request work bound (`checker.work`, 413 `policy_work_too_large`) and the org cap (422 `policy_limit`) charge each `Match` node for its globs (count and length), so the bound again tracks time on the live, append, upload and `/check` paths and the batch stays inside its soft limit; globs are compiled per plan, or the glob cache is sized to what the caps allow, so it can't thrash; runs of `*` collapse when parsed. The fixing thread picks the charge; a changed limit or error detail is a contract change, approved under the standing rule if sound.
- **Acceptance:** the A9-1 tests pass unmarked (`test_check_work_bound_sees_globs`, both `test_work_estimate_bounds_monitor_time` cases, `test_batch_work_sees_globs`); a policy set at the org caps over a 10 000-event trace finishes its batch verdicts inside the soft limit; live, ingest, batch and precheck bench rows met.
- **Verify:** `cd backend && pytest tests/audit/test_audit_9.py -k "work" tests/policy tests/api/test_policy_work.py -q` · `python backend/scripts/bench_policy.py --path live --assert-budget` · `python backend/scripts/bench_policy.py --path precheck --assert-budget`
- **Decision (contract change, approved under the standing rule):** `checker.work` and the org cap count `Plan.cost`: one per monitor node, plus each `Match`'s globs past its first simple one (1 per extra distinct glob, 2 per part searched for between two `*`, 1 per character of a part with `?`; matchers compiled on a plan's first step). `MAX_REQUEST_WORK` (2 000 000) and `MAX_ORG_POLICY_NODES` (4 000) keep their values; the 422 `policy_limit` body keeps `nodes`/`max_nodes`, now in cost units. Per-policy size limits unchanged (the org cap refuses a second 2 400-glob policy). Globs compile once per plan (`encode.compile_glob`), `**` parts dropped. Measured ≤ 0.44 us per unit on glob shapes; worst glob set at the cap over 10 000 distinct names: batch verdicts 19 s.

### SG-A9.2 AUDIT-9 mediums (A9-2, A9-3, A9-4)
- **Owner:** thread SG-A9.2 · **Status:** done · **Depends:** none (touches `counterexample.py` and `monitor.py`; coordinate with SG-A9.1 on `checker.py`)
- **Scope:** the counterexample deadline is checked inside the glob automaton and per-glob atom terms are charged against `_MAX_CELLS` (A9-2); a policy's exact `count ... by` values don't depend on other policies' (e.g. the per-monitor bound split per node, with a floor) (A9-3); batch `count ... by` state is compact (values mapped to integers once per trace, or a lower org cap on such nodes) (A9-4).
- **Acceptance:** the A9-2, A9-3 and A9-4 tests pass unmarked; the overflow rule stays Hypothesis-checked against `reference.evaluate` across restores; differential and counterexample property tests unchanged; counterexample, batch and precheck bench rows met.
- **Verify:** `cd backend && pytest tests/audit/test_audit_9.py -k "counterexample or groups or memory" tests/policy -q` · `python backend/scripts/bench_policy.py --path counterexample --assert-budget`
- **Decision (contract change, approved under the standing rule):** the live checker's per-monitor bound on exact `count ... by` values (`MAX_MONITOR_GROUPS` = 10 000) is split evenly over the monitor's `count ... by` nodes (each node keeps `min(MAX_GROUPS, 10 000 // nodes)`, at least 5 within the org node cap), so a policy's exactness depends only on its own events; the snapshot bound is unchanged and older snapshots restore as they are. A9-5: when the batch check's lock wait times out with Redis up it raises `TraceBusy` and stores nothing; the Celery task retries it (60 s apart, 5 times). Without Redis it still stores unlocked.
- **Result:** A9-2, A9-3 and A9-4 tests unmarked. A9-2: `_automaton` gives up (string names) once states x globs x distinct characters would pass `_MAX_NAME_STEPS` = 50 000 (about 1 to 3 us each), and string-mode atoms charge a cell per 50 characters of their regexes per event, so both audit cases fall back at once (2 000 globs: 0.05 s) or within the solver deadline (the 4 000-character glob). A9-4: batch `count ... by` values get one integer index per trace, shared by every node, and each node counts in an `array("i")`: 50 nodes x 5 000 names peaks at 45% of the 16 B per node x value bound and runs 40% faster. A9-5 tested in `test_idle_streams.py`. Backend 1192 passed on SQLite, Postgres subset 443 passed. Bench: counterexample p95 239 ms, batch 26, precheck 6.0, live 0.04, ingest 0.07. Open (low): `BUDGET_S` is still 2 s against the 500 ms worst-case budget, so a string-mode glob the solver can't settle (the 4 000-character case) takes the full 2 s before its fallback; a batch retried for a busy trace recomputes its counterexamples.

### SG-A10.1 The work bound fits what clients send (AUDIT-10 A10-1 high)
- **Owner:** thread SG-A10.1+A10.2 · **Status:** done · **Depends:** AUDIT-10; SG-A9.1 changes how the same bound counts work (globs), so one thread, or SG-A9.1 first
- **Scope:** no policy set the API accepts makes the SDK's ordinary requests fail on `policy_work_too_large`: a check of an unstreamed trace (its whole history as `prefix`), a stopped stream's check (SG-A8.1's prefix), a trace upload and a streamed backlog. The fixing thread picks how, e.g. org caps consistent with the bound (nodes x `MAX_TRACE_EVENTS` within it), or a bound sized for that product off the event loop with its own concurrency limit, or 413s that say how many events fit (`max_events`) with the SDK splitting appends and bulk batches by it while `/check` and single uploads are always answered. A contract change, approved under the Rules line if sound.
- **Acceptance:** the three A10-1 tests in `backend/tests/audit/test_audit_10.py` pass unmarked on SQLite and Postgres; `tests/api/test_policy_work.py` updated to the new contract; precheck, ingest and live bench rows met.
- **Verify:** `cd backend && pytest -q tests/audit/test_audit_10.py tests/api && TEST_DB=postgres pytest -q tests/audit/test_audit_10.py` · `cd sdk && pytest -q` · `python backend/scripts/bench_policy.py --path precheck --assert-budget`
- **Contract change (approved by this thread, 2026-10-15, under the Rules line on sound contract changes):** `/check` is never refused for policy work: the schema bounds its prefix, the org caps its policies. A prefix of `k` = `WORK_SLICE` / per-event cost events or more resumes from a checkpoint (Redis `policy:prefix:<digest>`, 1 h) an earlier check saved every `k` events, keyed by a running digest of the org, stored trace and seq, policy ids and plan digests, and each prefix event's kind, name, status and attrs, so the SDK's whole-history checks of an unstreamed trace pay for the events since the last checkpoint. An append over `MAX_REQUEST_WORK` (2 000 000, unchanged) is still 413 `policy_work_too_large`, now with `max_events` (what fits; a one-event append is always taken), and the SDK resends its backlog in appends of that size. An upload whose `block` check is over the bound alone is stored and judged by its batch check (a `block` violation recorded, not refused); see SG-A10.2 for bulk requests. Policy work over `WORK_SLICE` (200 000, about 0.1 s), span rebuilds included, runs in slices on a two-thread pool (`trace_policy_service._pool`), so no request holds a thread for long and long jobs can't starve short ones.
- **Result:** verify passes on SQLite and Postgres; the three A10-1 tests unmarked; `test_policy_work.py` rewritten to the contract (check answered, checkpoint resume and its agreement with a fresh check, append `max_events`, one-event append, upload over the bound stored and batch-checked); SDK tests for the split backlog and a non-policy 413 on append.

### SG-A10.2 A bulk upload's policy work is bounded (AUDIT-10 A10-2 high)
- **Owner:** thread SG-A10.1+A10.2 · **Status:** done · **Depends:** AUDIT-10; same code as SG-A10.1, so one thread may take both
- **Scope:** `/ingest/bulk` counts policy work across the request's traces against the per-request bound (rejecting the traces past it with `policy_work_too_large`, or the request with 413), so no request does more than the bound; consider a dedicated bounded executor for policy work so one org's uploads cannot starve other orgs' checks.
- **Acceptance:** `test_bulk_upload_work_is_bounded_per_request` passes unmarked; a bulk of 20 traces x 499 events against 3 984 `block` nodes no longer takes over the bound's time; SG-A6.1's SDK batching tests unchanged.
- **Verify:** `cd backend && pytest -q tests/audit/test_audit_10.py tests/api` · `cd sdk && pytest -q`
- **Contract change (approved by this thread, 2026-10-15):** a `/ingest/bulk` request's uploads share one `MAX_REQUEST_WORK` of inline `block` checks; a trace that doesn't fit in what is left is rejected `policy_work_too_large` with `work` and `max_work` (what was left), and the SDK sends those traces again at once in a new request (buffering them only if a reply refused all of a request's traces). A trace over the bound alone is stored (SG-A10.1), so every request makes progress and does at most the bound inline.
- **Result:** `test_bulk_upload_work_is_bounded_per_request` unmarked; `test_bulk_upload_charges_its_traces_to_one_bound` and SDK resend tests added; SG-A6.1's batching tests unchanged. Measured (SQLite test app): one bulk of 20 traces x 499 events against 3 986 `block` nodes now does one trace inline (1.1 s) and refuses 19 for resending, was 22 s; with 8 in flight a one-event `/check` peaked at 1.8 s, the same as with no policies at all (storing 80 000 spans), was 8.5 s. Benches: precheck p95 5.0 ms, live 0.03 ms, ingest 0.10 ms, batch 30 ms. **Open lows:** an upload over the bound alone skips its `block` refusal (the batch check records the violation); prefix checkpoints are one snapshot per new checkpoint for an hour, bounded only by the rate limit (Redis has no memory cap); the first check of a long unstreamed trace at the org caps (10 000 events x 4 000 units, ~20 s) still times out in the SDK, and the next one resumes from its checkpoints.

### SG-A10.3 AUDIT-10 mediums (A10-3, A10-4)
- **Owner:** thread SG-A10.3 · **Status:** done · **Depends:** AUDIT-10
- **Scope:** a running guarded call does not hold back events other tasks add after it (A10-3); sync and async guarded calls of one trace are checked in turn (A10-4). SDK only.
- **Acceptance:** both tests in `sdk/tests/test_audit_10.py` pass unmarked; `tests/test_guard.py` (A8-3 cases) unchanged; `python backend/scripts/mutants_audit10.py probe` kills every row.
- **Verify:** `cd sdk && pytest -q` · `cd backend && pytest -q tests/audit/test_audit_8.py`
- **Contract change (approved by this thread, 2026-10-15, under the Rules line on sound contract changes):** with `stream=True`, a background push holds back a running guarded call's event only while no later event follows it; once one is added, by any task, the push sends the running event too (`success`, latency 0, the cost SG-A8.2 accepted for a check), so the rest of the stream is never held. An append that updates an earlier event's outcome was not taken here: a policy may read `status` and live verdicts are already given, so it needs a backend contract (an outcome update the stored row and the batch check fold in) while SG-A9.1/SG-A10.1 change the same append path. **Open medium for the next audit round:** with concurrent tasks, a guarded call that another event follows before it ends is stored as `success`, latency 0, losing its real result. Sync and async guarded calls of one trace share one `threading.Lock` (async tasks still queue on an `asyncio.Lock` first, then poll the shared lock without blocking the loop); a sync guarded call on the loop's own thread while an async one of that loop holds the lock goes ahead unheld, since waiting would deadlock the loop. A `/check` 413 blocks only with code `policy_work_too_large`; any other 413 (a proxy's body limit) follows `fail_closed` (A10-5).
- **Result:** verify passes; both `sdk/tests/test_audit_10.py` tests unmarked, A8-3 cases in `test_guard.py` unchanged. New tests in `test_guard.py`: a running call's outcome survives a background push (both clients), a call sent while it runs keeps the outcome it was sent with, a sync guard on the loop thread does not hang. `mutants_audit10.py`: the six SDK A10-7 survivors are now `probe` rows, plus probes for the shared lock, the loop-thread exception and the 413 code; every SDK row of `mutants_audit4/6/8/10.py` killed. A10-6: the stale rows of `mutants_audit4.py` (A4-1), `mutants_audit6.py` (A6-16) and `mutants_audit8.py` (`CONFIG_ERRORS`, guard lock, `_end`, `traces.py` 409) rewritten to the current code. A10-8: `test_check_does_not_stall_the_event_loop` sends 3 000 prefix events instead of 9 998, since parsing that body on the loop alone took 0.5 to 0.6 s on CI (still over the bound, and still fails at 1.45 s with the bound lifted and the work moved onto the loop), and bounds the stall relative to the request's time; `test_work_within_the_bound_runs_off_the_event_loop` likewise allows up to half the request's time (failed at 0.24 s of 0.73 s; work moved onto the loop still fails it at 1.10 s of 1.17 s).

### SG-A11.1 One org's policy work doesn't delay another's (AUDIT-11 A11-1, A11-2 high)
- **Owner:** unassigned · **Status:** proposed · **Depends:** AUDIT-11
- **Scope:** policy work is bounded per org, not only per request: on the API, an org's long jobs on `_pool` (checks, appends, rebuilds) are limited in number or units in flight, or scheduled fairly across orgs, and the rebuild slots likewise (A11-1); in the batch, the work an org has queued on the `policy` queue is accounted and bounded, by deferring that org's checks or refusing its uploads (A11-2). Past a bound, 429 (the SDK follows `fail_closed`) is the expected answer. A changed status code or queue model is a contract change, approved under the Rules line if sound.
- **Acceptance:** `test_one_orgs_policy_work_does_not_queue_another_orgs` and `test_one_orgs_queued_batch_work_is_bounded` pass unmarked on SQLite and Postgres (the fixing thread may adapt how the batch test measures work "ahead" if the queue model changes); SG-A10.1's checkpoint and slicing tests unchanged; precheck, ingest, live and batch bench rows met.
- **Verify:** `cd backend && pytest -q tests/audit/test_audit_11.py -k "queue" tests/api/test_policy_work.py && TEST_DB=postgres pytest -q tests/audit/test_audit_11.py -k queue` · `python backend/scripts/bench_policy.py --path precheck --assert-budget`

### SG-A11.2 Policy compile off the event loop (AUDIT-11 A11-3 high)
- **Owner:** unassigned · **Status:** proposed · **Depends:** AUDIT-11
- **Scope:** `active_policies` (and `_check_org_limits`, `plan_for` on request paths) never parse or plan a policy on the event loop; parse time is bounded by what the caps count (tag definitions counted toward the size limits, or unused tags rejected), and/or compiled policies are cached so another org's traffic can't evict them; a stored policy that doesn't parse is not re-parsed per request. A11-4 (huge integers are a 500) rides along.
- **Acceptance:** `test_policy_compile_does_not_stall_the_event_loop` and `test_huge_integer_is_a_parse_error` pass unmarked; DSL and trace-policy API tests unchanged except for any new size rule; bench rows met.
- **Verify:** `cd backend && pytest -q tests/audit/test_audit_11.py -k "stall or integer" tests/policy/test_dsl.py tests/api/test_trace_policies.py`

### SG-A12.1 Overlapping checks of a long prefix resume (AUDIT-12 A12-1 high)
- **Owner:** unassigned · **Status:** proposed · **Depends:** AUDIT-12
- **Scope:** a `/check` whose prefix overlaps one still being stepped (an earlier check of the same history that the SDK gave up on, or a concurrent task's) resumes from what that one has done instead of stepping the whole prefix again, e.g. each digest's checkpoint saved as its slice is stepped and a newer one looked for before each slice, or one job per digest with later checks waiting for it; work for a check whose request is gone (or past a deadline near the SDK's `check_timeout_s`) stops. A per-org share of `_pool` is SG-A11.1's (AUDIT-11 A11-1, which found the cross-org side of the same pool); coordinate on `trace_policy_service.py`. A changed `/check` or checkpoint contract is approved under the Rules line if sound.
- **Acceptance:** `test_check_resumes_from_an_earlier_check_still_running` passes unmarked on SQLite and Postgres; `tests/api/test_policy_work.py` checkpoint tests unchanged; 8 checks of a 4 000-event history a second apart at 3 996 units each finish in about one check's time, not 49 to 65 s (AUDIT-12 A12-1); tests added so the checkpoint-key and restore rows of `python backend/scripts/mutants_audit12.py A12-8` are killed (A12-5: they guard checkpointed verdicts and this rewrites that code); precheck and ingest bench rows met.
- **Verify:** `cd backend && pytest -q tests/audit/test_audit_12.py tests/api/test_policy_work.py && TEST_DB=postgres pytest -q tests/audit/test_audit_12.py` · `python backend/scripts/bench_policy.py --path precheck --assert-budget`

### SG-A12.2 AUDIT-12 medium (A12-2)
- **Owner:** thread SG-A12.2 · **Status:** done · **Depends:** AUDIT-12
- **Scope:** a trace's async guarded calls (and the async client's pushes) work when the trace's calls run in more than one event loop, e.g. asyncio locks kept per loop, or the shared threading lock awaited without an asyncio primitive. SDK only.
- **Acceptance:** `sdk/tests/test_audit_12.py` passes unmarked; `tests/test_guard.py` and `tests/test_audit_10.py` unchanged.
- **Verify:** `cd sdk && pytest -q`
- **Result:** `trace.loop_lock` keeps one `asyncio.Lock` per running loop (`WeakKeyDictionary`) for the guard queue (`Trace._guard_alocks`) and the async push (`Trace._alocks`); calls in different loops still exclude each other through the shared threading `_guard_lock`, so the sync/async sharing of #47 holds. `test_audit_12.py` passes unmarked; `tests/test_guard_loops.py` adds the per-loop lock and an async guarded call waiting for a sync one in another thread (kills AUDIT-12's surviving `guard.py` one-poll mutant). Open low: async pushes of one trace from two loops running at once in different threads no longer exclude each other (before, the second loop raised `RuntimeError`).

## Audits (after SG5b; SG6 included if built)

Each audit is a fresh session that wrote none of the code. Findings go to `AUDIT-<n>.md`, ranked critical/high/medium/low, each with a repro or failing test. Critical and high findings become new `SG-A<n>.<k>` entries here and go through the same build-and-verify loop. Audit rounds repeat until one full round has no critical or high findings.

| # | Focus | Method |
|---|---|---|
| A1 | Encoding soundness (no false negatives); done, see `AUDIT-1.md`: 2 high → SG-A1.1, SG-A1.2 | Differential fuzz at 100 k cases vs reference evaluator, mutation testing of `encode.py` (surviving mutants are findings), adversarial fixtures (empty trace, 1-event trace, glob edge cases, unicode names, counts at exact bounds). |
| A2 | Performance under load | Load at 2× the SG4 rate, long traces (5 000 events), 50 policies, Redis restarts mid-trace; every budget row checked. |
| A3 | API/SDK consistency; done, see `AUDIT-3.md`: 4 high → SG-A3.1 to SG-A3.3 | OpenAPI vs pydantic vs TS types vs SDK models; error codes; legacy payload compatibility. |
| A4 | Test coverage and quality; done, see `AUDIT-4.md`: 1 high → SG-A4.1 | Coverage ≥ 90 % on `app/policy` and new routers, assertion density, tests that cannot fail. |
| A5 | Round 2: correctness and performance; done, see `AUDIT-5.md`: 1 critical, 2 high → SG-A5.1 to SG-A5.3 | Round-1 fixes re-checked; checkers probed outside the bench shapes (tag reuse, large count bounds, nested `count by`, 10 000-event traces); end to end through the app and SDK. |
| A6 | Round 2: API/SDK consistency and test quality; done, see `AUDIT-6.md`: 4 high → SG-A6.1 to SG-A6.3 | Route, schema and SDK read-through, sync vs async diff, tests against the real app, hand-picked mutants of the API and SDK paths. |
| A7 | Round 3: checker correctness, performance and security; done, see `AUDIT-7.md`: 1 critical, 3 high → SG-A7.1 to SG-A7.3 | Round-2 fixes re-checked, property tests at 10x, inputs sized to the caps (glob shape, policies near 20 000 characters, 10 000-event traces and prefixes, many `count ... by name` nodes), a paused stream. |
| A8 | Round 3: API/SDK consistency, contracts, security and test quality; done, see `AUDIT-8.md`: 1 high → SG-A8.1 | Read-through of the round-2 changes, tests against the real app with the real SDK, 31 mutants of the round-2 fixes. |
| A9 | Round 4: checker correctness, performance and security; done, see `AUDIT-9.md`: 1 critical → SG-A9.1; 3 medium → SG-A9.2 | Round-3 fixes re-checked, differential of the batch path at 5 000 examples with digested and marker-like names, globs sized to the caps (glob lists, long globs, wide alphabets), `count ... by` nodes up to the org cap, idle reopen against the batch. |
| A10 | Round 4: API/SDK consistency, contracts, security and test quality; done, see `AUDIT-10.md`: 2 high → SG-A10.1, SG-A10.2 | Read-through of the round-3 changes, tests against the real app with the real SDK, the work bound measured at the real caps, 30 mutants of the round-3 fixes. |
| A11 | Round 5: checker correctness, performance and security; done, see `AUDIT-11.md`: 3 high → SG-A11.1, SG-A11.2 | Round-4 changes re-checked, property tests at 5 000 examples, checkpoint key review, glob fast paths against the reference matcher, cost per unit on new shapes, two orgs against the real app sharing the policy pool, batch queue, compile cache and event loop. |
| A12 | Round 5: API/SDK contracts, SDK and test quality; done, see `AUDIT-12.md`: 1 high → SG-A12.1; 1 medium → SG-A12.2 | Read-through of the round-4 changes, overlapping and abandoned checks measured at the org caps, tests against the real app and both SDK clients, 28 mutants of the round-4 fixes. |

## Interface contracts

Frozen at SG0 merge. Changes are stop-and-report.

### Events (`app/policy/events.py`, `app/schemas/trace.py`)

```python
class EventIn(BaseModel):            # wire format
    seq: int                         # 0-based, strictly increasing within a trace
    kind: str = "tool"               # tool | llm | retrieval | confirmation | custom
    name: str                        # e.g. "delete_user", "user_confirmed"
    status: str = "success"          # success | error | timeout
    input: Dict[str, Any] = {}
    output: Dict[str, Any] = {}
    attrs: Dict[str, Any] = {}       # free-form labels usable in policies
    latency_ms: int = 0
    timestamp: Optional[datetime] = None

@dataclass(frozen=True)
class Event:                         # checker-internal, no payloads
    seq: int; kind: str; name: str; status: str; attrs: Mapping[str, Any]

def events_from_trace(trace: TraceIn) -> list[Event]: ...
def events_from_spans(spans: Sequence[Span]) -> list[Event]: ...
```

`TraceIn` gains `events: List[EventIn] = []`. A payload with both `events` and `tool_calls` is a 422. Legacy `tool_calls` become events with `kind="tool"`, `seq` = index.

### DSL (`app/policy/dsl.py`, full grammar in `docs/policy-dsl.md`)

```
tag secret_read = tool("vault.read", "read_secret*")
tag network     = tool("http_*", "fetch_url", "send_email")

policy confirm_before_delete severity high:
  always tool("delete_*") -> once event("confirmation")

policy no_exfil_after_secret severity critical:
  always @network -> not once @secret_read

policy retry_cap severity medium:
  always count(tool(*) and status("error")) by name <= 3
```

```python
def parse(src: str) -> list[Policy]: ...          # raises PolicyParseError(line, col, message)
```

AST (`app/policy/ast.py`, frozen dataclasses): `Policy(name, severity, formula, source)`, formula nodes `Match(kind, globs, status, attrs)`, `Not`, `And`, `Or`, `Implies`, `Once`, `Historically`, `Previously`, `Since`, `Count(pred, op, bound, by)`. A policy is `always φ`; it is violated at the first `seq` where `φ` is false.

### Checker (`app/policy/checker.py`, `app/policy/counterexample.py`, `app/policy/reference.py`)

```python
@dataclass(frozen=True)
class Violation:
    policy_id: str; policy_name: str; severity: str
    at_seq: int
    counterexample: tuple[int, ...]   # sorted seqs, minimal unsat core
    message: str

def check_trace(policies: Sequence[CompiledPolicy], events: Sequence[Event]) -> list[Violation]: ...
def compile_policy(policy_id: str, policy: Policy) -> CompiledPolicy: ...

class Monitor:
    def __init__(self, policies: Sequence[CompiledPolicy]): ...
    def step(self, event: Event) -> list[Violation]: ...   # violations first reached at this event
    def snapshot(self) -> bytes: ...
    @classmethod
    def restore(cls, policies, blob: bytes) -> "Monitor": ...

def would_violate(policies, prefix: Sequence[Event], candidate: Event) -> list[Violation]: ...
def minimal_counterexample(policy: CompiledPolicy, events: Sequence[Event], at_seq: int) -> tuple[int, ...]: ...
def evaluate(policy: Policy, events: Sequence[Event]) -> list[int]: ...   # reference oracle: violating seqs
```

Each policy reports at most one violation per trace (its first `at_seq`) in v1.

### Storage (migration `0002_trace_policies`)

- `trace_policies`: `id, org_id, name, source, severity (info|low|medium|high|critical), action (log|alert|block), agent_id?, environment?, enabled, version, created_at, updated_at, archived_at?` (set by delete; archived policies are never enforced); unique `(org_id, name)` where `archived_at IS NULL`.
- `policy_violations`: `id, org_id, trace_id, agent_id, policy_id, policy_version, severity, at_seq, counterexample JSON, message, mode (batch|live|precheck), detected_at`; index `(org_id, detected_at)`, unique `(trace_id, policy_id, policy_version)`.
- `spans.seq INTEGER NULL`, backfilled from `started_at` order for existing rows.

### HTTP / WS

| Method | Path | Auth | Body → Response |
|---|---|---|---|
| GET/POST | `/api/trace-policies` | jwt (write: admin) | `TracePolicyCreate` → `TracePolicyOut` |
| PUT/DELETE | `/api/trace-policies/{id}` | jwt admin | bumps `version` on source change; DELETE archives (violations kept); GET lists live policies unless `?include_archived=true` |
| POST | `/api/trace-policies/validate` | jwt | `{source}` → `{ok, errors: [{line, col, message}]}` |
| POST | `/api/trace-policies/check` | api-key | `{agent_id, trace_uid?, candidate: EventIn}` → `{allowed, violations: [ViolationOut]}` |
| POST | `/api/traces/{trace_uid}/events` | api-key | `{agent_id, events: [EventIn], final: bool}` → `{accepted, violations}`; 409 on seq gap/dup; 403 on `block` |
| GET | `/api/traces/{id}/violations` | jwt | → `[ViolationOut]` |
| GET | `/api/reports/violations.csv` | jwt | columns: `detected_at, agent, trace_id, policy, severity, at_seq, counterexample, mode` |
| WS | `/ws/ingest` | api-key | new message `{"type":"events", trace_uid, agent_id, events, final}`; untyped messages stay `TraceIn` |
| WS | `/ws/traces` | jwt | adds `{"type":"violation","data": ViolationOut}` from Redis `violations:{org_id}` |

`ViolationOut` = `Violation` fields + `id, trace_id, agent_id, policy_version, mode, detected_at`. TS mirror in `frontend/src/types/policy.ts`. `AlertRule.metric` accepts `policy_violations` (count in window; `context.severity_at_least` optional).

### SDK

```python
t.add_event(kind: str, name: str, *, status="success", input=None, output=None, **attrs)
agentops.trace(..., stream: bool = False)       # stream=True sends events as they are added
client.check(name: str, *, kind="tool", input=None, **attrs) -> CheckResult(allowed: bool, violations: list[dict])
await async_client.acheck(...)
```

### SG0 contract notes

Decided while turning the contract into code; frozen with the rest.

- AST: `And`/`Or`/`Implies`/`Since` are binary; `Match.attrs` is a tuple of `(key, value)` pairs; `Count.by` is `None | "name" | "kind"`; `Policy.source` is excluded from equality. Tags are inlined by the parser. Full grammar and semantics in `docs/policy-dsl.md`.
- `CompiledPolicy(policy_id, policy, artifact)`: `artifact` is the encoder's opaque slot.
- Counterexample "unconstrained facts": kind ranges over the 5 `EventIn` kinds; name, status and attr values (including a missing key) range over any value. Attr equality is type-strict. A counterexample need not contain `at_seq` (e.g. `not previously tool("a")`).
- `EventIn.kind` is a literal of the 5 kinds; `status` stays free text so legacy `tool_calls` still map. `TraceIn.events` seqs must be exactly `0..n-1` (422 otherwise).
- Storage: `spans.attrs JSON NULL` added (events need their attrs back), unique index `(trace_id, seq)`. `policy_violations.policy_name` stores the name at detection time so rows map straight to `ViolationOut`. Event timestamps are stored as naive UTC. The ORM class for `policy_violations` is `TracePolicyViolation`, to avoid clashing with the existing `trace_service.PolicyViolation` exception.
- `TracePolicyCreate` is `{source, action, agent_id?, environment?, enabled}`: `source` holds exactly one `policy` block, and `name`/`severity` come from it. `ViolationOut.id`/`trace_id` are null for precheck results.
- Existing `forbid_tool` rules also apply to events of every kind (SG0b; originally `tool`-kind only), so an `events` payload cannot bypass them.

## Log

- 2026-10-13: Plan drafted from codebase read at `7766898`.
- 2026-10-13: Plan approved by Ashton; SG0 dispatched.
- 2026-10-14: SG0 in review.
- 2026-10-14: SG0 merged (`aa2f27a`). SG1 done.
- 2026-10-14: SG2 done. With SG1 merged, its merge meets the fixture gate.
- 2026-10-14: SG4 done.
- 2026-10-14: SG0b done.
- 2026-10-14: SG3 done.
- 2026-10-14: SG5b done.
- 2026-10-14: SG5a done.
- 2026-10-14: SG6 done.
- 2026-10-14: A1 done (`AUDIT-1.md`): no critical, 2 high (SG-A1.1, SG-A1.2), 1 medium, 2 low.
- 2026-10-14: Audit A2 (performance) done: 1 critical, 2 high, 4 medium, 3 low in AUDIT-2.md; SG-A2.1 to SG-A2.4 proposed.
- 2026-10-14: SG-A2.3 and SG-A2.4 done (A2-3, A2-4, A2-6 fixed; A2-5 partly).
- 2026-10-14: SG-A1.2, SG-A2.1 and SG-A2.2 done.
- 2026-10-14: Audit A3 (API/SDK consistency) done: 0 critical, 4 high, 7 medium, 4 low in AUDIT-3.md; SG-A3.1 to SG-A3.5 proposed (SG-A3.1 and SG-A3.5 change the interface contract).
- 2026-10-14: Audit A4 (test coverage) done: 0 critical, 1 high, 7 medium, 4 low in AUDIT-4.md; SG-A4.1 and SG-A4.2 proposed (tests and CI only, no contract change).
- 2026-10-14: SG-A4.1 done (test sessions no longer share a connection).
- 2026-10-14: SG-A4.2 done (A4 mediums: every replayed mutant killed; coverage and a Postgres run in CI).
- 2026-10-14: Audit A5 (round 2, correctness and performance) done: 1 critical, 2 high, 2 medium, 5 low in AUDIT-5.md; SG-A5.1 to SG-A5.4 proposed. Round-1 fixes hold.
- 2026-10-14: Audit A6 (round 2, API/SDK and tests) done: 0 critical, 4 high, 6 medium, 4 low in AUDIT-6.md (two more lows were AUDIT-5's); SG-A6.1 to SG-A6.5 proposed (SG-A6.5 changes the storage contract).
- 2026-10-14: Audit A7 (round 3, checker correctness, performance and security) done: 1 critical, 3 high, 1 medium, 1 low in AUDIT-7.md; SG-A7.1 to SG-A7.4 proposed. Round-2 fixes hold.
- 2026-10-14: SG-A7.2 done (batch verdicts without a solver, linear in policy nodes x events; atom terms charged to the counterexample cap).
- 2026-10-14: Audit A8 (round 3, API/SDK and tests) done: 0 critical, 1 high, 3 medium, 3 low in AUDIT-8.md (one more medium is AUDIT-7's A7-5); SG-A8.1 and SG-A8.2 proposed.
- 2026-10-14: SG-A8.1 done (A8-1: checks of a stopped stream send its undelivered events; `/check` answers a trace at the event cap).
- 2026-10-15: SG-A8.2 done (A8-3 guard lock covers check and record only; A8-4 revoked key closes `/ws/ingest`; A8-6 streamed tool calls go as the check prefix; lows A8-7, A8-8 fixed).
- 2026-10-15: SG-A7.3 done (A7-3/A7-4: per-request policy work cap with 413, monitor work off the event loop, count-by state bounded per monitor with digests, org policy caps).
- 2026-10-15: SG-A7.4 done (A7-5/A8-2: an append reopens an idle-closed trace; the sweep re-tracks open traces after a Redis loss).
- 2026-10-15: Audit A9 (round 4, checker correctness, performance and security) done: 1 critical, 0 high, 3 medium, 1 low in AUDIT-9.md; SG-A9.1 and SG-A9.2 proposed. Round-3 fixes hold.
- 2026-10-15: Audit A10 (round 4, API/SDK and tests) done: 0 critical, 2 high, 2 medium, 4 low in AUDIT-10.md (one more low is AUDIT-9's A9-5); SG-A10.1 to SG-A10.3 proposed.
- 2026-10-15: SG-A10.1 and SG-A10.2 done (A10-1: checks always answered, long prefixes resumed from checkpoints, appends split by `max_events`, oversized uploads stored and batch-checked; A10-2: a bulk request's uploads share one work bound; long policy work sliced on its own pool).
- 2026-10-15: SG-A9.1 done (PR #45): policy work and the org cap charge globs (`Plan.cost`); globs compiled per plan. A9-1 tests unmarked.
- 2026-10-15: SG-A9.2 done (A9-2 glob automaton and string regexes bounded in the counterexample; A9-3 count-by values split per node; A9-4 batch count-by in integer arrays; A9-5 busy batch retried).
- 2026-10-15: SG-A10.3 done (A10-3: a running guarded call no longer holds back later events; A10-4: one guard lock for sync and async calls; lows A10-5 proxy 413, A10-6 stale mutant rows fixed).
- 2026-10-15: Audit A11 (round 5, checker correctness, performance and security) done: 0 critical, 3 high, 0 medium, 1 low in AUDIT-11.md; SG-A11.1 and SG-A11.2 proposed. Round-4 fixes hold; the highs are per-org bounds on shared resources.
- 2026-10-15: Audit A12 (round 5, API/SDK and tests) done: 0 critical, 1 high, 1 medium, 3 low in AUDIT-12.md; SG-A12.1 and SG-A12.2 proposed.
