# AUDIT-9: checker correctness, performance and security (round 4)

Audited at `6c28cfe` (everything through PR #43 merged) by a session that wrote none of the code. Scope: the parser, reference evaluator, Z3 encoding, counterexamples, live monitor and its Redis state, batch pipeline, pre-check, idle close and reopen, per-request work limits and org policy caps, with the round-3 fixes (PRs #38 to #43) re-checked. API, SDK and test quality are AUDIT-10's. Gaps PLAN.md already lists as known or accepted are not repeated.

**Result: 1 critical, 0 high, 3 medium, 1 low.** The round-3 fixes hold where they were aimed: the linear glob matcher agrees with a regex oracle, the plain-Python batch verdicts agree with the reference evaluator and the Z3 encoding, and the bench rows are met. The critical finding is that SG-A7.3's work bound counts monitor nodes, while one node can hold thousands of globs: the bound, and with it the live, pre-check and batch budgets, can be bypassed by a policy well inside the size caps. It is PLAN.md sub-goal `SG-A9.1`; the mediums are `SG-A9.2`.

Failing tests: `backend/tests/audit/test_audit_9.py`, each `xfail(strict=True)`, failing today at its final assertion. The timed ones fail by 7x or more on the audit box (4 cores). Run them with `cd backend && pytest tests/audit/test_audit_9.py -q --runxfail`.

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A9-1 | critical | `checker.work` counts monitor nodes, not globs: a 2-node policy with 2 400 globs (or one 19 900-star glob) passes the 413 bound at 10 000 events and costs 1 to 700 ms per event, so one request holds an API thread for minutes to hours and a batch check runs past its hard limit | SG-A9.1 |
| A9-2 | medium | The counterexample's glob automaton and per-glob atom terms are built before the deadline is checked: one glob on a one-event trace takes 19 s against `BUDGET_S` = 2 s | SG-A9.2 |
| A9-3 | medium | The per-monitor bound on exact `count ... by` values (SG-A7.3) is shared by all of an org's policies: broad count policies use it up, and a narrow blocking policy then blocks a legitimate call and every later one in the trace | SG-A9.2 |
| A9-4 | medium | Batch `count ... by` state is about 100 bytes per node per distinct value: about 2 GB for one 10 000-event trace at the org node cap | SG-A9.2 |
| A9-5 | low | The batch check stores without the append lock when the lock wait times out, not only when Redis is down | none |

## Method

- Read PLAN.md, AUDIT-1 to AUDIT-8 and every file under `app/policy/`, `trace_policy_service.py`, the policy paths of `trace_service.py` and `policy_tasks.py`, with the diffs of PRs #38 to #43.
- Ran `tests/policy` and `tests/audit` (741 passed, 5 xfailed), then `test_differential.py` and `test_monitor.py` at the `ci` Hypothesis profile (5 000 examples; 118 passed, 4 min).
- An extra differential of `checker.first_violations` (the PR #40 batch path) and `Monitor` against `reference.evaluate`, 4 000 examples, with names chosen to hit the PR #42 key handling: over 32 characters (digested), starting with `\0`, equal to the overflow markers, empty. No divergence.
- 300 000 random globs and names over `ab*?` and newline: `encode.glob_match` and `reference.glob_match` (PR #38) both equal to a `re.fullmatch` oracle.
- Every `bench_policy.py --assert-budget` row: live p95 0.06 ms, ingest 0.12, batch 53, counterexample 434, all met. Precheck p95 ran 8.5 to 10.1 ms over three runs (one 0.1 ms over its 10 ms budget); `756416b` (before PR #42) gives the same spread on this box, so that is box noise rather than a regression, but the row has little headroom here (PR #42 measured 4.3 to 4.7).
- Probed outside the bench shapes: glob count and glob length within the 20 000-character cap, wide glob alphabets, many `count ... by` nodes up to the org cap, idle close, reopen and the batch racing them.

## Critical

### A9-1 The work bound counts nodes, not globs

SG-A7.3 refuses a request whose `work` (events x monitor nodes) is over `MAX_REQUEST_WORK` = 2 000 000, "about a second", and caps an org at 4 000 nodes. But `tool("a*", "b*", ...)` is one `Match` node however many globs it lists, and a glob's cost grows with its length, so the estimate can be off by a factor of thousands:

| Policies (each 2 nodes, under 20 000 characters) | Cost per event | `work` per event |
|---|---|---|
| 1 x `not tool(<2 300 globs>)`, names 255 characters | 1.3 to 1.9 ms | 2 |
| 1 x `not tool("*" x 19 900 + "z")` | 6 ms | 2 |
| 2 x 2 400 globs | 148 ms | 4 |
| 10 x 2 400 globs | 704 ms | 20 |

Past 4 096 distinct globs the `_glob_parts` cache thrashes, so every event recompiles every glob: that is the jump from one policy to two. At 20 `work` per event, a 10 000-event `/check` prefix or append is 200 000, a tenth of the bound, and would take about two hours (10 policies; the org cap is 100). Measured: `/check` with three such policies and a 20-event prefix takes 4.6 s and answers 200; one policy and a 9 998-event prefix, 12.7 s.

The work runs in `asyncio.to_thread`, so the event loop is not stalled outright as in A7-3, but each request holds a thread of the default executor (8 on the audit box) until it ends; Python threads can't be cancelled and the client going away doesn't stop it. Every append, restore, snapshot and `/check` of every org goes through the same executor, so a handful of such requests from one self-registered org leaves no thread for anyone else, for hours. The append lock (60 s) also expires under a long append, so a second append to that trace fails on the `(trace_id, seq)` index with a 500.

The batch check is affected the same way: `first_violations` memoises attr-free atoms by name, which doesn't help when names are distinct. Three 2 400-glob policies over 20 events take 4.3 s; over 10 000 distinct names the soft limit (240 s) passes, and the verdicts-only retry, with 60 s left before the hard limit, does the same work and is killed. The trace gets no batch rows and its live rows stay `pending`, the outcome SG-A2.2 and SG-A7.2 fixed.

Tests: `test_check_work_bound_sees_globs` (three policies created through `POST /api/trace-policies`, a 20-event `/check`: 413 or under 0.5 s; 4.6 s and 200 today), `test_work_estimate_bounds_monitor_time` (glob list and long glob: stepping must take at most 10x the documented 0.5 us per unit of `work`; 14x and 9x over the 0.2 s floor today) and `test_batch_work_sees_globs`.

Fix direction: charge each `Match` node its glob cost in `work` and in the org cap (globs, and their characters past a small constant), or bound globs per atom and per org; collapse runs of `*` when parsing (`**` matches what `*` does); size the glob cache to what the org caps allow, or compile globs once per plan instead of through a global LRU. Whatever the charge, `test_work_estimate_bounds_monitor_time` checks it against measured time.

## Medium

### A9-2 Glob preprocessing escapes the counterexample budget

SG-A7.2 charges atoms x window against `_MAX_CELLS` before building, but `FactEncoding` counts `Match` nodes, not globs, and two steps run before any deadline check:

- `name_patterns` builds the product automaton of the policy's globs. Each state steps every glob on every character any glob mentions, up to 512 states. One 4 000-character glob of distinct CJK characters on a one-event trace: 3.7 s; 19 000 characters: 19 s. 2 000 short globs: 5.3 s on a 100-event trace.
- In string mode each atom term is an `or` over every glob's `str.in_re`, per event: 2 000 globs x 1 000 events is tens of MB of SMT text, charged as 1 000 cells.

`BUDGET_S` is 2 s and the PLAN budget for the worst case is 500 ms. The batch keeps starting counterexamples until `CHECK_BUDGET_S` (30 s) has passed, so one trace can still take 30 s plus one such counterexample; `name_patterns` is cached per glob tuple (256 entries), so the cost repeats per worker process and when more than 256 glob sets are in use.

Test: `test_counterexample_budget_holds_for_globs` (wide alphabet and glob list, `BUDGET_S` = 0.2: must finish in 0.6 s; about 4 s today).

Fix direction: check the deadline inside `_automaton`'s loop (returning None, the string fallback, or giving up at once); charge `_name_in` by the number of globs per atom per event.

### A9-3 One policy's count groups make another policy false

SG-A7.3 bounds exact `count ... by` values per monitor (`MAX_MONITOR_GROUPS` = 10 000) as well as per node (`MAX_GROUPS` = 1 000), with `room` shared by every plan of the monitor, and a monitor holds all of an org's active policies. When the room is used up, a new value goes to the node's overflow counter; once the overflow makes a comparison uncertain the policy is marked `_UNSURE` and is false at every later event (SG-A2.4's rule, sound but not exact). Before PR #42 a node only lost exactness past its own 1 000 values (accepted as low in AUDIT-5). Now other policies spend the room: with 20 `log` policies `count(tool(*)) by name <= 50` and a `block` policy `count(tool("delete_*")) by name <= 1`, a trace with 600 distinct tool names then `delete_a`, `delete_b` blocks `delete_b`, which the reference evaluator and the batch accept, and every later call of that trace is blocked too (append and `/check`). The live row is never confirmed by the batch (`counterexample_status` becomes `none`). With 100 count policies, 100 distinct names are enough.

Test: `test_one_policys_groups_do_not_make_another_false` (deterministic).

Fix direction: give each plan its own share of the monitor bound (e.g. `MAX_MONITOR_GROUPS` divided over the `count ... by` nodes, at least a floor), so a policy's exactness depends only on its own events; or bound `count ... by` nodes per org at policy create, as the node cap does.

### A9-4 Batch count-by memory per trace

The batch evaluator counts every `count ... by` value exactly (`max_groups=math.inf`, PR #40), a dict entry per node per distinct value, each value over 32 characters a new 26-character digest string per node. Measured with 40-character names over 10 000 events: 50 such nodes, 116 MB; 1 000 nodes (20 policies, half the 4 000-node org cap), 1.05 GB and 50 s. At the cap (about 2 000 `count ... by` nodes, each with its `and`) one trace needs about 2 GB, on each of the two `policy` workers. PLAN.md records 141 MB at the cap; that measurement had no `count ... by` nodes.

Test: `test_batch_count_by_memory_is_bounded` (one policy, 50 nodes, 5 000 names: peak at most 16 bytes per node x value; 98 today).

Fix direction: map each distinct value to an integer once per trace (shared by all nodes) and keep per-node counts in integer arrays, or digest once per event rather than per node; or count `count ... by` nodes against a lower per-org cap.

## Low

### A9-5 The batch stores unlocked when the lock wait times out

`check_completed_trace` (PR #43) takes `lock:trace-events:{org}:{uid}` with `blocking_timeout=10` and stores without it if `acquire` raises (Redis down), as its comment says, but also when `acquire` returns `False`, which is what a 10 s wait without the lock returns. An append to an idle trace that holds the lock past 10 s (A9-1 makes that easy; otherwise it needs a slow rebuild) can then commit its reopen and its live `pending` rows between `_store_batch`'s stream re-read and its pending-row query, and the batch, computed on the older spans, marks those rows `none`; the next close's batch never revisits them, so they keep no counterexample. Repro: hold the append lock for over 10 s in one task while appending to an idle trace with a violating event, run `check_completed_trace` for that trace. Fix: when the lock is not acquired and Redis answered, store nothing and leave the trace for its next close (or retry the task).

## Re-checked, no finding

- PR #38: both matchers are linear in practice and agree with a regex oracle (Method); `?` matches newlines and astral characters as before.
- PR #40: batch verdicts agree with `reference.evaluate` and with `Monitor` (Method), including digested and marker-like names; the 100 000-entry memo clear is sound (it is a cache only).
- PR #42: the snapshot bound holds (330 KB worst case); digests and markers can't collide with client values; `v: 1` snapshots are rebuilt; `check` doesn't mutate the monitor's `room`.
- PR #43: a reopened trace's rebuild re-finds stored violations without storing them twice (`on_conflict_do_nothing` on `(trace, policy, version)`, rows published only when new); a refused append to an idle trace reopens it and a legacy block leaves it idle; the batch's stream compare can't miss a reopen and re-close during one task run (the task is limited to 300 s, the idle window is an hour).
- Budgets on the bench shapes (Method).
