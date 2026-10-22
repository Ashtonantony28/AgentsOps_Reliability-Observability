# AUDIT-13: checker correctness, performance and security (round 6)

Audited at `86315ff` (everything through PR #55 merged) by a session that wrote none of the code. Scope: the round-5 changes, re-checked for correctness, performance and security: per-org fair scheduling (`FairSlots`, per-org Redis batch lists, PR #53, SG-A11.1), the compile cache off the event loop (SG-A11.2), prefix checkpoints saved per slice with in-flight waiting, stall takeover, `CHECK_DEADLINE_S` and 503 `check_timeout` (PR #55, SG-A12.1), and the SDK's per-loop locks (SG-A12.2). Gaps PLAN.md already lists as known or accepted are not repeated.

**Result: 0 critical, 3 high, 2 medium, 2 low.** The checker itself holds: verdicts, counterexamples and resumed checkpoints agree with the reference evaluator (5 000 examples each, including checks resumed from another check's in-flight slice), checkpoint keys can't be shared across orgs, policy sets or traces, and every bench row is met. The highs are about availability: SG-A12.1 made every slice of a long check store a snapshot in Redis for an hour, so one org fills the Redis every org depends on (A13-1); the event loop still does unbounded work for requests nobody has authenticated (A13-2); and SG-A11.1's per-org turns are undone by registering more orgs, which is open and unbounded (A13-3). They are PLAN.md sub-goals `SG-A13.1` (A13-1, with A13-4 and A13-5), `SG-A13.2` (A13-2) and `SG-A13.3` (A13-3).

Failing tests: `backend/tests/audit/test_audit_13.py`, each `xfail(strict=True)`, failing today at its final assertion on SQLite and on Postgres 16. Run them with `cd backend && pytest tests/audit/test_audit_13.py -q --runxfail` (add `TEST_DB=postgres` for Postgres).

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A13-1 | high | Every slice of a long `/check` stores its own checkpoint for an hour (one per check before #55): a 10 000-event history at the org cap is 188 snapshots, 70 MB; one org's 4 concurrent checks added 292 MB to Redis in two minutes against the real app (4 MB before #55). Redis has no memory cap and is also the Celery broker and every org's monitor state | SG-A13.1 |
| A13-2 | high | The event loop does work for unauthenticated requests: bodies of any size are parsed and validated before `/check` or ingest reads the API key (20 MB: 2.4 to 4 s stall, then 401), and login and registration run bcrypt (0.3 s) on the loop. Within the schema caps, 16 of one org's 10 000-event checks arriving together pushed another org's guarded calls past the SDK's 2 s timeout | SG-A13.2 |
| A13-3 | high | Per-org fairness (SG-A11.1) is undone by registering orgs, which is open, unverified and not rate limited: with 24 orgs one check each, another org's check waited behind 21.5 of their slices per slice of its own, as with the FIFO pool before SG-A11.1 | SG-A13.3 |
| A13-4 | medium | Before each slice a check reads every checkpoint after it (`mget(keys[j:])`), so a resumed check pulls the whole history's snapshots into the API process: 70 MB and 0.4 to 1.2 s from a real Redis per check at the org cap, on every guarded call of an unstreamed trace | SG-A13.1 |
| A13-5 | medium | A `/check` of a stored trace without monitor state rebuilds it under a lock it waits for only 5 s, so the SDK's next checks each rebuild the whole trace again, and no rebuild stops at the deadline or when its request is gone (4 concurrent checks: 4 rebuilds) | SG-A13.1 |
| A13-6 | low | The org cap bounds compile time less than SG-A11.2 measured: a 20 KB string literal costs 2 units and compiles in 3.4 ms (atoms: 0.02 ms a unit), so 100 such policies are 0.34 s per cache miss; plans evicted from the 4 096-entry FIFO plan cache are rebuilt on the loop | none |
| A13-7 | low | A check queued for a pool slot doesn't look at its deadline or request, so a gone check still steps a slice; without Redis a prefix whose work is over `CHECK_DEADLINE_S` is a 503 on every resend | none |

## Method

- Read PLAN.md, AUDIT-11 and AUDIT-12, `trace_policy_service.py`, `policy_tasks.py`, `monitor.py`, `checker.py`, the `/check` route and `precheck`, the SDK's `loop_lock`, and the diffs of PRs #52 to #55.
- Ran `test_differential.py`, `test_monitor.py` and `test_precheck.py` at the `ci` Hypothesis profile (5 000 examples; 163 passed).
- Wrote a property test (not committed) of `prefix_monitor` itself against `reference.truth` at 5 000 examples: random policies over the small vocabulary, `WORK_SLICE` of 1 to 7 events, up to 4 overlapping prefixes and a branched history checked concurrently, then again sequentially. Steps were slowed 2 ms a piece so checks overlap: 5 550 slices stepped, 19 847 checkpoint restores, 10 443 waits on another check's in-flight slice. No disagreement.
- Read the checkpoint key against what a resumed monitor depends on: org, stored trace id and length, policy ids and plan digests (name, severity and formula), and each event's kind, name, status and attrs, hashed as an injective chain (JSON keeps `1`, `1.0` and `true` apart, as `attr_equal` does). In-flight slices are found by the same key, so a check only ever waits for, or restores from, a slice of its own org, policies and history. A digest collision needs a policy update by the org's own admin, who can disable the policy anyway.
- `FairSlots`: turn order, cancellation while waiting and after being handed a slot, `turn()` with only stale waiters; per-org batch lists: the push and pop scripts keep "org in the turn list iff its list is non-empty".
- Every `bench_policy.py --assert-budget` row: live p95 0.025 ms, ingest 0.087, batch 44, precheck 7.0, counterexample 284. All met.
- Two orgs at the caps (4 policies, 3 743 units a event, ten `count ... by name` nodes) against a real uvicorn app on Postgres 16 and Redis 7 (`/tmp` script, not committed): org B streamed a trace (a `/check` and an append per call) and sent 500-event unstreamed checks; org A sent 10 000-event histories of distinct names, 4 or 16 at a time. The same 4-at-a-time run against `3010a70` (before #55) for comparison.
- Measured checkpoint sizes in-process (188 checkpoints, 388 KB the largest, 70 MB in all for one history at the cap) and an `mget` of 190 such values from a real Redis (0.4 to 1.2 s).

## High

### A13-1 Per-slice checkpoints fill the shared Redis

SG-A12.1 saves `policy:prefix:<digest>` as soon as each slice of `k` = `WORK_SLICE` / per-event cost events is stepped, each for `PREFIX_TTL_S` = 1 h. Before, a check saved one checkpoint, at its end. A snapshot holds every node's state and up to `MAX_MONITOR_GROUPS` = 10 000 `count ... by` values, about 390 KB, so a 10 000-event history at the org cap (k = 53) leaves 188 snapshots, 70 MB, where it left 0.4 MB.

Against the real app, org A sending four 10 000-event histories at a time (far under its 600 a minute) added 292 MB to Redis in two minutes (873 checkpoints; every one of those checks ended 503 at the deadline, having saved what it stepped). The same load against `3010a70` added 4 MB. That is about 2.4 MB/s per API process, so with the one-hour TTL about 9 GB per process, and the production compose file runs 4 workers. Redis runs with no `maxmemory` (both compose files) and holds the Celery broker, every trace's monitor state, rate-limit counters, open-stream tracking and, since SG-A11.1, the batch turn lists: when it runs out of memory, every org loses its queued batch checks and live state (or, with a cap and `noeviction`, every write fails). PLAN.md lists "prefix checkpoints last an hour each with no Redis memory cap" as a low; #55 multiplies what each check stores by the number of its slices, and that is the finding.

Test: `test_a_long_check_keeps_about_one_checkpoint` (one 2 000-event check, 20 events a slice, ten `count ... by` nodes: at most 4 newest snapshots' worth of checkpoints may remain; 100 of them, 75x, today).

Fix direction: keep only the newest checkpoint of a history (delete the one it supersedes when saving the next; a branched history then starts from the last shared one it still finds, or over), or keep per-slice checkpoints only while the check runs and the newest one after; and bound checkpoint bytes per org (a per-org key count or total, oldest dropped first).

### A13-2 The event loop does work for unauthenticated requests

`/check` reads its API key inside the handler (`authenticate_api_key`), after FastAPI has read, JSON-parsed and validated the body on the event loop; nothing bounds the body's size (no limit in the app, uvicorn or gunicorn, and port 8000 is published directly). `EventIn.attrs` is an unbounded dict. A 20 MB body with no valid key stalls the loop for 2.4 to 4 s and is then a 401; size is the attacker's choice (a stall past gunicorn's 30 s worker timeout gets the worker, and every org's requests in it, killed). Login and registration call bcrypt (`passlib`, 0.28 to 0.30 s) on the loop too, so a few wrong-password logins a second for any known email (the seeded `demo@agentops.dev`, by default) keep a worker's loop busy. Every guarded call of every org on that worker times out meanwhile and is, by default, allowed unchecked.

Bodies within the schema caps cost loop time too: a 10 000-event `/check` body is 0.08 to 0.4 s to parse. Against the real app, with org A's 16 checks of 10 000 events arriving together (they all reach the deadline together and are resent together), org B's streamed `/check` reached 3.7 s and its 500-event checks 5.8 s (p95 1.2 to 1.5 s), past the SDK's 2 s `check_timeout_s`; with 4 at a time its p95 stayed under 0.65 s. (B's p50 was 21 ms alone and about 0.3 s under either load: that part is the CPU the pool shares, which PLAN.md lists.)

Test: `test_unauthenticated_requests_do_not_stall_the_event_loop` (`body`: a 20 MB `/check` with a wrong key; `login`: a wrong password for a registered email; the loop may stall at most 0.1 s; 2.4 s and 0.27 s today).

Fix direction: refuse bodies over a size bound before reading them (Content-Length, then counting chunks: 413), sized to the largest valid request; authenticate before parsing the body where the route allows it; parse large bodies and run password hashing off the loop (`asyncio.to_thread`); rate limit login and registration per client.

### A13-3 Registering orgs undoes per-org fairness

SG-A11.1 hands pool slices, span rebuilds and batch checks out in turn by org, which bounds what one org's backlog does to another. `POST /api/auth/register` makes a user and an org with no rate limit, no email verification and no cap per client; an agent and an API key are two more API calls. So an attacker with 24 orgs (one check in flight each) gets 24 turns for every turn of a victim: measured with the A11-1 test's setup (`WORK_SLICE` 20 000) and 24 orgs, the victim's check saw 21.5 of their slices per slice of its own, where 24 checks from one org give 2 since SG-A11.1 and the FIFO pool gave 24 before it. The batch queue's turns and every per-org rate limit and cap scale the same way. AUDIT-5, AUDIT-7, AUDIT-9 and AUDIT-11 used "anyone can register" as the threat model; per-org fairness is only as strong as the cost of an org.

Test: `test_one_client_cannot_make_orgs_without_bound` (12 registrations from one client: at most 5 may succeed; 12 today).

Fix direction: rate limit registration (and org creation) per client address and per email domain, or require a verified email before an org gets API keys; optionally give newly created orgs a smaller share until verified. The test only pins the per-client bound.

## Medium

### A13-4 A resumed check reads every checkpoint of its history

`_Steps.newest` reads `mget(keys[j:])`, every checkpoint after the slices already done, and restores the newest. With A13-1 every slice has one, so a check of a history an earlier check has stepped (the unstreamed SDK's every guarded call: the whole history plus one event) pulls them all into the API process: 70 MB at the org cap with `count ... by` values, 0.4 to 1.2 s from a real Redis (measured with 190 values of 420 KB), and that much memory per concurrent check. Before #55 the same `mget` returned at most one checkpoint per check that had run. Test: `test_a_resumed_check_reads_about_one_checkpoint` (the second check may read at most 4 newest snapshots' worth; 75x today). Fix direction: look for the newest first (one `GET` per key from the last, or a per-history pointer to the newest), which A13-1's fix may make moot.

### A13-5 Stored-trace rebuilds in `/check` are repeated and never stopped

`precheck_monitor` rebuilds a stored trace's monitor from spans when its state is missing or stale (a Redis loss, the 24 h expiry, a trace whose state was dropped at close) under `lock:monitor-rebuild:<trace>` with `blocking_timeout=5`; a check that waited 5 s goes on to rebuild it itself. At the org caps a 10 000-event rebuild is about 20 s alone, so with the SDK giving up after 2 s and making its next guarded call, each further check rebuilds the whole trace again (and the lock's own 30 s timeout expires under load). None of these rebuilds looks at `CHECK_DEADLINE_S` or `gone()`: `prefix_monitor` only checks between prefix slices, and not at all for a prefix under one slice, which is every streamed trace's check. SG-A12.1 made overlapping prefix work resume; this is the same overlap for the stored part. Test: `test_concurrent_checks_rebuild_a_stored_trace_once` (2 000 stored events, the 5 s wait scaled to 0.1 s, 4 concurrent checks: the stored events may be stepped once; 4 times today). Fix direction: wait for an in-flight rebuild of the same trace and state as for an in-flight slice (or rebuild in checkpointed slices like a prefix), and stop between slices at the deadline or once the request is gone.

## Low

### A13-6 What the org cap bounds of compile and plan time

`org_cost` charges the larger of `Plan.cost` and the nodes parsed, but the tokenizer reads strings a character at a time: a policy whose glob, attr key or value is a 20 KB literal costs 2 units and compiles in 3.4 ms (1.7 ms a unit; 500 atoms take 0.02 ms a unit). An org of 100 such policies compiles in 0.34 s on each cache miss, in a default-executor thread (PLAN.md accepts a thrashed cache costing the org's compile time; this is 4x what the cap was taken to allow). Separately, `plan_for`'s cache is 4 096 entries, first in first out, and `_check_org_limits` adds a plan (computed on the loop) for every create or update, refused ones included; a plan evicted while its compiled policy is still cached is rebuilt on the loop by `work()` and `prefix_monitor`, about 2.6 us a unit, 10 ms per request at the cap. Fix: charge source characters (or literal lengths) in `org_cost`; key plans by the compiled policy so they live as long as it.

### A13-7 Deadline and request checks miss waits

A check waiting in `FairSlots` for a pool slot, or in `precheck_monitor` for its stored trace, doesn't look at its deadline or its request, so a gone check steps one more slice once its turn comes (bounded, and it is saved for the next check). Without Redis no checkpoint is saved, so a prefix whose work is over `CHECK_DEADLINE_S` (a 10 000-event history at the cap with two such checks in flight) is a 503 on every resend.

## Re-checked, no finding

- Encoding soundness: verdicts and counterexamples (`test_differential.py` at 5 000 examples), the live monitor and pre-checks (`test_monitor.py`, `test_precheck.py`), and resumed checkpoints through `prefix_monitor` with concurrent overlapping checks (Method): all agree with the reference evaluator.
- PR #55: an in-flight slice is restored only from a snapshot of the same key, taken after its thread finished; a stalled slice taken over is stepped on the taker's own monitor, so two checks never share mutable state; a stopped check saves nothing for the slice it was in. `CheckStopped` is raised only between slices.
- PR #53: `FairSlots` gives a cancelled waiter's slot on, and a slot handed to a waiter cancelled before it resumed is released; the batch Lua scripts are atomic and keep the turn list consistent; a `TraceBusy` retry keeps its trace id.
- PR #52: compiled policies are keyed by (id, source), so no org can evict another's entry into a wrong policy, and a source that no longer parses is cached as `None`.
- PR #54 (SDK): per-loop `asyncio.Lock`s held in a `WeakKeyDictionary`, shared threading lock unchanged; PLAN.md lists its open low.
- Tenant isolation: checkpoint keys, in-flight slice keys and batch lists all include the org; rebuild and pool turns are per org.
- Latency budgets on the bench shapes (Method).
