# AUDIT-4: test coverage and quality

Audited at `c741be2` (SG0 to SG6, SG-A1.1, SG-A1.2, SG-A2.1 to SG-A2.4 merged) by a session that wrote none of the code. Every mutant named in a finding was replayed again at `816dd41`, after the SG-A3.2 to SG-A3.4 fixes merged. Scope: PLAN.md audit row A4. That row asks for coverage of at least 90 % on `app/policy` and the new routers, a look at assertion density, and a search for tests that cannot fail. It covers the backend policy code and its services, the SDK pre-check and streaming code, and the frontend policy UI.

**Result: 0 critical, 1 high, 7 medium, 4 low.** The high finding becomes PLAN.md sub-goal `SG-A4.1`, and the mediums are grouped into `SG-A4.2`.

## Method

- **Line and branch coverage**: `pytest --cov=app --cov-branch` was run over the full backend suite (971 passed), and `pytest --cov=agentops_sdk` over the SDK (73 passed).
- **Mutation testing**: an AST mutator flips comparisons, `and`/`or`, `not`, int and bool constants, `+`/`-`, deletes statements and returns `None`.
  - Each mutant runs the tests for its module with `-x`. A survivor there is a lead, not a finding: every mutant named in a finding below was replayed against the whole backend or SDK suite and still survived. Two leads were killed by the full suite and are not reported: dropping the Celery dispatch is caught by `test_enqueue_failure_does_not_raise`, and the `events_from_spans` survivors are caught by `test_ingest_hardening.py`.
  - 18 backend module slices were covered: 969 mutants, all sites except a seeded sample of 150 of 247 in `dsl.py` and 120 of 225 in `counterexample.py`. The SDK had 16 hand-picked mutants.
  - Every survivor below was then read by hand. Equivalent mutants are not reported: cache sizes, log arguments, default values the code never reads, and extra parentheses in the test-only pretty-printer.
- **Replaying the confirmed survivors**: `backend/scripts/mutants_audit4.py`. Each listed mutant goes into a temporary copy of `backend/` and `sdk/`, its tests run, and the script exits 1 while any mutant survives:

  ```
  $ python backend/scripts/mutants_audit4.py
  ```

  A fixing sub-goal is done when its findings' mutants are killed.
- **Strict-xfail tests**: findings a test can state directly are in `backend/tests/audit/test_audit_4.py`, marked `xfail(strict=True)`. They are A4-6 and A4-10.
- **Test-quality scan**: every `test_*` function without an `assert`, `pytest.raises` or assert helper (6, all Hypothesis tests that assert through a shared helper, which is fine); skipped tests; parametrisations with no cases; wall-clock assertions.

## Coverage

| Module | Line + branch | Note |
|---|---|---|
| `app/policy/*` (8 modules) | 99–100 % | `encode.py` 99, `monitor.py` 99, the rest 100 |
| `routers/trace_policies.py` | 97 % | |
| `routers/ws.py` | 82 % | the uncovered lines are mostly the legacy whole-trace path and error replies |
| `routers/traces.py` | 45 % | the events and violations routes are covered; the uncovered lines are legacy query and export routes |
| `services/trace_service.py` | 94 % | |
| `services/trace_policy_service.py` | 83 % | |
| `services/violation_service.py`, `tasks/policy_tasks.py` | 100 % | |
| `services/report_service.py` | 93 % | |
| SDK `guard.py` / `precheck.py` / `trace.py` | 100 / 96 / 93 % | |

The ≥ 90 % target is met for `app/policy` and the new routers, but only when coverage is told to follow greenlets. With the default configuration, `trace_service` reads 42 % and `routers/trace_policies.py` 57 %. Even with `concurrency = greenlet,thread`, coverage reports lines as missed that a spy shows running, for example `load_monitor`'s restore branch. Nothing in CI measures coverage at all (A4-6). Coverage alone also says little here: `violation_service.py` is at 100 % and still carries an A4-4 survivor, and `trace_policy_service.py`'s A4-1 line is covered.

## Mutation results

| Slice | Mutants | Survived | Real gaps |
|---|---|---|---|
| `policy/reference.py` | 68 | 1 | 0 |
| `policy/monitor.py` | 76 | 9 | A4-9, plus an off-by-one at `MAX_GROUPS` (A4-11) |
| `policy/dsl.py` (sample) | 150 | 16 (+4 hung) | 0 (all in the test-only `pretty()`, or tokenizer offsets) |
| `policy/checker.py` | 45 | 7 | A4-5 |
| `policy/encode.py` | 75 | 7 | 0 |
| `policy/counterexample.py` (sample) | 120 | 11 (+2 hung) | A4-5 |
| `policy/events.py` | 11 | 5 | 0 (killed by `test_ingest_hardening.py`, which was outside that run's test set) |
| `routers/trace_policies.py` | 69 | 8 | A4-8, plus an untested `IntegrityError` fallback (A4-11) |
| `routers/ws.py` (new code) | 44 | 19 (+11 hung) | A4-11 (rollback after a conflict, error replies) |
| `schemas/policy.py`, `schemas/trace.py` | 33 | 8 | A4-11 (the `MAX_TRACE_EVENTS` boundary) |
| `services/trace_policy_service.py` | 57 | 12 | A4-1, A4-4, A4-11 |
| `services/trace_service.py` (events path, ingest checks) | 73 | 8 | A4-11 (`policy_warn` audit row on appends) |
| `services/violation_service.py` | 30 | 10 | A4-4, A4-11 |
| `services/alert_service.py` (policy code) | 60 | 34 | A4-11 (`compare` boundaries); the rest are legacy metrics |
| `services/report_service.py` (violations) | 42 | 22 | A4-11 (ordering ties); the rest are the legacy org summary |
| `tasks/policy_tasks.py` | 16 | 7 | 0 (the dispatch survivor is killed by the full suite) |

## Findings

### A4-1 · high · The test database shares one connection across sessions, so a missing commit is invisible

The `engine` fixture is SQLite in memory with `StaticPool`. Every session, including the separate `AsyncSession` that `publish_violations` opens, uses the same connection, so each session sees the others' uncommitted rows. Removing `await db.commit()` from `trace_policy_service.check_completed_trace` therefore passes the suite. In production the Celery task's session then closes with a rollback, and every batch violation row and filled-in counterexample is lost.

The SG-A2.4 thread hit the same fixture property from the other side: an approach that was correct on Postgres failed its test on SQLite. The other commits on the ingest and alert paths were not mutated one by one, but they rely on the same fixture.

- **Mutant**: `mutants_audit4.py A4-1`.

### A4-2 · medium · The SDK's async streaming and pre-check paths are mostly untested

The sync client is well covered. These async counterparts survive mutation (`mutants_audit4.py A4-2`):

- `Trace.acheck` no longer skips the server while events are undelivered or the stream is dead (`trace.py:108`). Under `fail_closed=True` this turns the "not delivered" block into a server check against a partial history.
- `AsyncAgentOpsClient(fail_closed=...)` is ignored and always `True` (`async_client.py:46`). PLAN says the default is fail-open.
- `_apost_check` ignores `check_timeout_s` and uses 30 s (`precheck.py:126`).
- A transient network error in `_asend_events` is treated as fatal, which ends streaming for the trace.

The SDK suite runs in under a second, so the tests are cheap to add. (The async resync gap found at `c741be2` is now covered by SG-A3.3's tests.) There is no async test that streams a rejected or failed batch and then calls `acheck`.

### A4-3 · medium · Single-sample wall-clock assertions in the unit suite flake

`tests/test_audit2.py::test_overlapping_globs_counterexample_meets_budget` asserts that one timed call takes ≤ 500 ms. It takes about 50 ms alone and failed in 1 of 3 full-suite runs on this box:

```
E   AssertionError: 661 ms for a 1-event trace and 13 globs
```

`test_ingest_step_with_many_distinct_names_meets_budget` (p95 of 20 samples ≤ 8 ms) and the `time.monotonic()` bounds in `tests/audit/test_audit_1.py` have the same shape. Every pull request runs these in `pytest -q`, next to the CPU-heavy Hypothesis and Z3 tests, while the budget rows already have a dedicated, repeated-sample check in `bench_policy.py --assert-budget`. A flaky red is retried until green, which trains everyone to ignore real failures in the same job.

### A4-4 · medium · Postgres-only code paths are never run by a test

CI starts Postgres 16, but only `test_migration.py` uses it, and that test skips locally when Postgres is missing. Every API and service test runs on SQLite, so the code that exists only for Postgres is dead in the suite:

- `record_violations` uses the `postgresql.insert … on_conflict_do_nothing` branch. Making it `None` passes.
- `evaluate_violation_alerts` takes `SELECT … FOR UPDATE` on the rule row. This is SG-A2.4's cross-worker serialisation, and SQLite ignores it. Deleting it passes.

AUDIT-2's race repro for A2-4 was a manual Postgres run, and nothing keeps it working.

- **Mutants**: `mutants_audit4.py A4-4`, which covers the insert branch and the `FOR UPDATE`.

### A4-5 · medium · The safeguards that bound and flag degraded counterexamples are untested

- **Solver timeout**: deleting `self.solver.set("timeout", …)` in `counterexample._Entailment.core` passes. The per-call solver timeout is what keeps one hard query inside `BUDGET_S`. The budget tests only time cases that finish anyway.
- **Budget-spent warning**: `checker._check` logs a warning when `CHECK_BUDGET_S` is spent. SG-A1.2 says this log is the only signal of a degraded counterexample, because the `Violation` contract has no flag. Flipping its condition passes.

A regression would bring back A1-H2 or A2-1 (counterexamples that run until Celery kills the task) without a red test.

- **Mutants**: `mutants_audit4.py A4-5`.

### A4-6 · medium · CI does not measure coverage, and the default configuration under-reports async code

`.github/workflows/ci.yml` runs `pytest -q` with no `--cov` and no floor. `backend/` has no coverage configuration. Coverage run without `concurrency = greenlet` misses most lines after an `await` on an async SQLAlchemy session, as the table above shows (`trace_service` 42 % against 94 %). The SDK job has no coverage either.

PLAN's A4 target cannot be checked or held without this.

- **Failing test**: `test_ci_gates_backend_coverage_with_greenlet_tracing`.

### A4-7 · medium · Nothing ties the frontend's types and API calls to the backend

`frontend/src/types/policy.ts` is PLAN's "TS mirror" of `TracePolicyOut` and `ViolationOut`. It matches today, checked by hand. Nothing generates or checks it from the OpenAPI schema.

All 36 vitest tests mock `@/api/tracePolicies` with objects built from those same types, so no test exercises the API client's URLs (`/api/trace-policies`, `/validate`, `/api/traces/{id}/violations`). A backend rename or a wrong path passes `tsc`, the build and every test.

Audit A3 (API/SDK consistency) covers the schema side. This entry records that no test would catch the drift.

### A4-8 · medium · Rate limiting is untested on every new API-key route

At `c741be2`, `middleware/rate_limit.py` was at 44 %, and its limit branch never ran. SG-A3.4 has since added tests that `/ws/ingest` is limited and that ingest does not starve `/check`. Deleting `enforce_rate_limit` from `POST /api/trace-policies/check` still passes, and no test drives the events route past its limit.

With the default fail-open, a check that returns 429 allows the call. The limit is what bounds how hard a client can hit the checker, and it has no test.

- **Mutant**: `mutants_audit4.py A4-8`.

### A4-9 · low · The live monitor's guard against missed `count … by` violations past the group bound is tested only by chance

Past `MAX_GROUPS` distinct values, `count … by` keeps one shared overflow counter and gets only bounds on a count. `monitor.py:107` marks the count `_UNSURE` when those bounds cannot decide the comparison, which makes the policy false from then on. This is the "never misses" guarantee SG-A2.4 added, and it also decides live blocking.

Changing `> 1` to `> 2` on that line disables the guard. Only `test_past_the_group_bound_no_violation_is_missed` catches it, and only when Hypothesis happens to draw a case where the upper bound hides a violation. It survived the mutation run, and was caught in 9 of 10 later runs, each started with an empty example database.

Repro with the mutant in place:

```python
p = ast.Policy("p", "high", ast.Implies(ast.Match(kind="tool", globs=("b",)),
                                        ast.Count(ast.Match(), ">=", 2, by="name")))
events = [Event(0, "tool", "a", "success", {}), Event(1, "tool", "b", "success", {})]
reference.evaluate(p, events)                       # [1]
with patch.object(monitor, "MAX_GROUPS", 0):        # mutant: no violation, m.failing == [] at seq 1
    m = Monitor([compile_policy("p", p)]); [m.step(e) for e in events]
```

- **Effect if it regresses**: a `block` policy with a `>=`/`>` count lets violating events through once a trace passes 1 000 distinct names, and about one CI run in ten stays green.
- **Fix**: add the repro as an `@example` (or a plain test), so `mutants_audit4.py A4-9` is killed every time.

### A4-10 · low · Tests that can never fail

- **Empty stub parametrisations**: `tests/api/test_contract_stubs.py::test_jwt_routes_501_for_admin` and `::test_api_key_routes_501` are parametrised over `JWT_STUBS` and `API_KEY_STUBS`. Both are empty now that every route is implemented. They are 2 of the suite's 3 skips; the third is the local migration test.
  - **Failing test**: `test_no_contract_stub_test_is_parametrised_over_nothing`.
- **Weak viewer assertion**: in the same file, `test_jwt_routes_viewer` asserts only `status_code != 403` for read routes, which a 500 also passes.
- **Stale skip**: `tests/policy/test_encode.py::test_fixture_asts_match_parser` still carries a `NotImplementedError` skip, "parser lands in SG1".

### A4-11 · low · Boundary and error-branch gaps (surviving mutants, small effect)

These mutants survived their module's test set. Unlike the ones above, they were not each replayed on the full suite.

- `monitor.py:30`: `len(groups) < MAX_GROUPS` becomes `<=`. The bound is off by one, and the markers share the dict, but it is still sound.
- `schemas/trace.py:64`: a trace of exactly `MAX_TRACE_EVENTS` events is not tested at the boundary.
- `trace_policies.py:80`: the `IntegrityError` → 409 fallback for a concurrent create or rename is never reached.
- `trace_policy_service.py:45`: a stored policy whose source no longer parses is skipped, untested.
- `trace_policy_service.py:172`: the version check in the batch fill of live counterexamples is untested. Changing `and` to `or` passes.
- `violation_service.py:48`: the alert rule's hits filter, `and` becomes `or`. That makes `context.violation_ids` and the critical-incident flag include other agents' or lower-severity rows.
- `violation_service.py:70` (`drain_notifications`) and the Celery task's drain call: deleting them passes.
- `ws.py`: the rollback after a `TraceConflict` or another error on the `events` branch of `/ws/ingest`, followed by another message on the same socket, is untested.
- `trace_service.py:254`: the `policy_warn` audit row on a streamed append is untested.
- `alert_service.compare`: the boundaries of `>` and `<` are untested.
- `report_service.violation_summary`: the ordering ties are untested.
- **SDK**: `__aexit__` gathering pending tasks, the `add_event` path of an async client with no running loop, and guarded-call latency.

### A4-12 · low · The frontend CI install is not reproducible

There is no `package-lock.json`, and CI runs `npm install` against `^` ranges (vitest, jsdom, Testing Library). A new upstream release can change test behaviour with no change to the repo.

## What was checked and held

- **Core verdict and semantics**: `reference.py` (1 surviving mutant, a cache size) and `encode.py` (7, all equivalent). The Hypothesis differential tests kill every semantic mutant in both, so A1's mutation result still holds after the SG-A2.1 changes to the encoding.
- **Live paths**: the events append path and whole-trace ingest checks. 0 real survivors in `trace_service.py` lines 105–135, and the 8 in the events path are low.
- **DSL parser**: every surviving mutant is in the test-only pretty-printer or the tokenizer's char-token offsets.
- **Assertions**: no test function lacks an assertion. Frontend tests assert concrete text, ARIA state and highlighted seqs; none was found that cannot fail.
