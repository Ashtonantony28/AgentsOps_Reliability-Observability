# AUDIT-11: checker correctness, performance and security (round 5)

Audited at `18a721c` (everything through PR #49 merged) by a session that wrote none of the code. Scope: policy semantics, the live monitor, batch check, counterexamples, `/check` and its prefix checkpoints, sliced policy work, the bulk work bound, latency budgets, and security (DoS, tenant isolation), with the round-4 changes (PRs #45, #47, #48, #49) re-checked. Gaps PLAN.md already lists as known or accepted are not repeated.

**Result: 0 critical, 3 high, 0 medium, 1 low.** The checker itself holds: verdicts, counterexamples and checkpoints agree with the reference evaluator, and every bench row is met. All three high findings are about sharing: work that is now bounded per request is still unbounded per org, and the API process, its policy pool and the batch queue are shared by every org. One org within its caps and rate limit can delay every other org's checks, appends and batch verdicts by minutes to hours, and stall the event loop of the process. They are PLAN.md sub-goals `SG-A11.1` (A11-1, A11-2) and `SG-A11.2` (A11-3).

Failing tests: `backend/tests/audit/test_audit_11.py`, each `xfail(strict=True)`, failing today at its final assertion on SQLite and on Postgres 16. Run them with `cd backend && pytest tests/audit/test_audit_11.py -q --runxfail` (add `TEST_DB=postgres` for Postgres).

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A11-1 | high | Policy work past `WORK_SLICE` runs on one two-thread FIFO pool per process, and one org may have any number of such jobs in flight (`/check` has no work bound since #48): another org's long check, append or rebuild waits behind a slice of each, 20x slower with 24 in flight | SG-A11.1 |
| A11-2 | high | Batch checks share one FIFO Celery queue and nothing bounds the batch work one org queues: at the org cap a 10 000-event upload is 31 s of verdicts with no inline work, so a minute of uploads at the rate limit queues about 5 hours of worker time ahead of every other org | SG-A11.1 |
| A11-3 | high | Policies are parsed on the event loop whenever the process-wide 2 048-entry compile cache misses; 21 self-registered orgs at the 100-policy cap make every request miss, and a 2-unit policy of unused tags parses in 22 ms, so each request stalls the loop for every org for about 2.6 s | SG-A11.2 |
| A11-4 | low | An integer over 4 300 digits in an `attr` value or `count` bound raises `ValueError`, not `PolicyParseError`: `/validate` and create answer 500 | none |

## Method

- Read PLAN.md, AUDIT-1 to AUDIT-10, every file under `app/policy/`, `trace_policy_service.py`, `trace_service.py`, `policy_tasks.py`, the policy routes, and the diffs of PRs #45 to #49.
- Ran the whole backend suite (1188 passed, 5 xfailed), then `test_differential.py`, `test_monitor.py` and `test_precheck.py` at the `ci` Hypothesis profile (5 000 examples; 163 passed).
- 200 000 random glob lists over `ab*?`, 1 000 000 names over `abc` and newline: `encode.compile_globs` (#45's set, prefix and suffix fast paths) equal to `reference.glob_match`.
- Prefix checkpoints (#48): read the key derivation against everything a monitor's state depends on (org, stored trace id and length, policy ids and plan digests, which also fix the `count ... by` split of #49, and each prefix event's kind, name, status and attrs, hashed as an injective chain). No way found to resume from another trace's, org's or policy set's state.
- `Plan.cost` against measured time on shapes the A9-1 tests don't cover (`?` parts that fail late, 3 000 literal middle parts, 300 to 500 `count ... by` nodes with 255-character names, attrs): 0.0 to 1.2 us per unit, within the 10x the A9-1 tests allow.
- Counterexample preprocessing (#49): glob product automata up to the `_MAX_NAME_STEPS` bound, then `pattern_sizes`: at most 0.2 s.
- Every `bench_policy.py --assert-budget` row: live p95 0.02 ms, ingest 0.10, batch 29, precheck 4.3, counterexample 303. All met.
- Probed what is shared between orgs in one API process (the policy pool, the rebuild semaphore, the compile and plan caches, the event loop) and across processes (the `policy` Celery queue), with two orgs against the real app.

## High

### A11-1 One org's policy work queues every other org's

SG-A10.1 made `/check` always answered, and SG-A10.1/A10.2 moved work over `WORK_SLICE` (200 000 units, about 0.1 s) onto `trace_policy_service._pool`, two threads, so that "long jobs can't starve short ones". That holds, but nothing bounds how many long jobs one org has in flight, and the pool is first come, first served across orgs. A `/check` with a 10 000-event prefix the server has not seen (no checkpoint) at the org cap is 40 000 000 units, about 20 s; the only limit on how many an org sends is its rate limit, 600 a minute by default. Each in-flight job keeps one slice queued, so another org's job waits behind one slice per job in flight, per slice of its own.

Measured with two orgs and `WORK_SLICE` scaled down to 20 000: another org's 10-slice `/check` took 0.24 s alone and 4.7 s with 24 of the first org's checks in flight, and the pool ran 376 to 441 of their slices (four runs) during its 10 (about 40 per slice). At the real slice size that is about 4 s per slice of the victim's job with 24 in flight; at the rate limit an org can keep hundreds in flight (inferred, not measured at full scale). What waits:

- another org's `/check` whose prefix or stored trace needs stepping past `WORK_SLICE` (any unstreamed trace past `WORK_SLICE` / its per-event cost events, e.g. 500 events at 400 units). The SDK gives up after 2 s and, by default, allows the call unchecked (`fail_closed=False`);
- appends of more than that many events, and every append or check that rebuilds a trace's monitor after a Redis loss or the 24 h state expiry. Rebuilds also share `MAX_CONCURRENT_REBUILDS` = 4 slots per process across orgs, which one org fills by checking long stored traces with no cached state;
- under that wait the append lock (60 s) and the rebuild lock (30 s) can expire mid-append, which AUDIT-9 noted leads to a second concurrent append and a 500 on the `(trace_id, seq)` index.

Test: `test_one_orgs_policy_work_does_not_queue_another_orgs` (24 checks from one org in flight; the other org's check must see at most 4 of their slices per slice of its own; about 40 today). It counts slices rather than time, so it is stable on loaded CI runners.

Fix direction: bound policy work per org, not only per request: a per-org limit on in-flight long jobs (or on units in flight), answering 429 past it (the SDK treats 429 per `fail_closed`), and/or schedule pool slices fairly across orgs (round robin per org rather than FIFO). The same per-org bound should cover rebuilds.

### A11-2 One org's uploads fill the batch queue for every org

Whole-trace uploads queue `check_completed_trace` on the `policy` Celery queue (one `policy-worker` in `docker-compose*.yml`, a process per CPU by default), first come, first served. The batch verdicts of a 10 000-event trace at the org cap take 31 s here (four 1 001-node policies, distinct names; 7 s when names repeat), plus up to `CHECK_BUDGET_S` = 30 s of counterexamples if a policy is violated. The upload itself does none of that work: an upload whose inline `block` work is over `MAX_REQUEST_WORK` is stored unchecked (SG-A10.1), and `log`/`alert` policies are never checked inline. So at the default rate limit of 600 uploads a minute, one org queues about 600 x 31 s = 5 hours of worker time per minute (over an hour of wall time on a 4-CPU worker), and every other org's batch check waits behind it.

For an org that uploads whole traces (the SDK default), the batch check is the only place its `log` and `alert` policies are checked, so its violations, alerts (`policy_violations` metric) and report rows arrive hours late; streamed traces' live rows stay `pending` without a counterexample for as long.

Test: `test_one_orgs_queued_batch_work_is_bounded` (20 uploads from one org, each just over `MAX_REQUEST_WORK`, then one from another org: at most 4 x `MAX_REQUEST_WORK` of batch work may be queued ahead of it; 20 x today).

Fix direction: account each org's queued batch work (`checker.work` of the trace's policies) and bound it: past the bound, defer that org's checks (a per-org queue, or a countdown) or refuse its uploads with 429; or route checks through a fair per-org scheduler. The fixing thread may change how the test measures "ahead" if the queue model changes.

### A11-3 Policy parsing stalls the event loop when the compile cache misses

`active_policies` runs on the event loop for every append, upload and `/check`, and calls `_compile(policy_id, source)`, an `lru_cache(maxsize=2048)` shared by every org of the process; a miss parses and plans the policy right there. The org caps (`MAX_ORG_POLICIES` = 100, `MAX_ORG_POLICY_NODES` = 4 000 cost units) don't bound parse time: tags the formula never uses cost nothing, so a 19.7 KB policy of 500 unused tag definitions costs 2 units and parses in 22 ms. Registration (`POST /api/auth/register`) is open and creates an org, so 21 orgs at the 100-policy cap hold 2 100 policies; requests that cycle through them evict each other's entries, and each request then parses its org's 100 policies on the loop: a 2.6 s stall for every request of every org on that process, per request. A handful of such requests a second keeps the loop blocked, so every guarded call times out (and, by default, is allowed unchecked) and ingest stops. `_check_org_limits` and `plan_for` (4 096 entries, keyed by policy object) add to the same miss.

AUDIT-7's A7-3 (monitor work on the event loop, 4 s per `/check`) was high; this is the same stall through the parser.

Test: `test_policy_compile_does_not_stall_the_event_loop` (100 such policies created through the API, the cache then filled to its size by other policies, one `/check`: the loop must not stall 0.25 s; 2.6 s on SQLite and 2.9 s on Postgres today).

Fix direction: parse and plan off the event loop (`asyncio.to_thread`), and either count tag definitions toward the size limits (or reject unused tags) or cache compiled policies per org with a bound the org caps make sufficient; a stored policy that no longer parses should be cached as such rather than re-parsed on every request.

## Low

### A11-4 A huge integer is a 500, not a parse error

`_Parser.value` and `_Parser.count` call `int()` on a digit token, which raises `ValueError` past Python's 4 300-digit limit. `parse_single` callers catch only `PolicyParseError`, so `POST /api/trace-policies/validate` and create (and update) answer 500 with a stack trace in the log instead of 200 `ok: false` and 422 `invalid_policy`. Nothing is stored. Fix: catch it in the parser and raise `PolicyParseError` at the token (or bound the digit count). Test: `test_huge_integer_is_a_parse_error` (both places).

## Re-checked, no finding

- PR #45 (glob cost): `compile_globs` agrees with the reference matcher (Method); `**` handling and the `prefix*`/`*suffix` fast paths are exact; measured cost per unit is within bounds on the shapes above, including `?` parts that fail late.
- PR #48 (checkpoints, slices, bulk): checkpoint keys cover everything a monitor's state depends on, are org-scoped, and a checkpoint is restored only at the exact seq it was saved for; slicing keeps each pool job under `WORK_SLICE`; a bulk request's inline work stays within one `MAX_REQUEST_WORK`. What it does not bound is A11-1.
- PR #49: `_automaton` and `pattern_sizes` stay well under a second at `_MAX_NAME_STEPS`; batch `count ... by` arrays share one value index across nodes (a kind and a name with the same text share an index but never a counter); the live per-node split is documented in `docs/policy-dsl.md` and only ever adds false positives, never misses; `TraceBusy` retries only matter while an append reopens the trace, after which the batch stores nothing by design.
- Tenant isolation: Redis keys are per trace id (UUIDs), per org (`lock:trace-events`, rate limits) or include the org (`policy:prefix`); every lookup the round-4 PRs added filters by org.
- Latency budgets on the bench shapes (Method).
