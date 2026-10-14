# AUDIT-7: checker correctness, performance and security (round 3)

Audited at `53ea0df` (everything through PR #35 merged) by a session that wrote none of the code. Scope: the parser, reference evaluator, Z3 encoding, counterexamples, live monitor and its Redis state, batch pipeline, pre-check and idle-stream closing, with the round-2 fixes (PRs #24 to #35) re-checked. API, SDK and test quality are AUDIT-8's. Gaps PLAN.md already lists as known or accepted are not repeated.

**Result: 1 critical, 3 high, 1 medium, 1 low.** The round-2 fixes hold where they were aimed: the 10x property runs and every budget row pass. The new findings come from inputs the earlier audits did not size: glob shape, policy size near the caps, and traces near 10 000 events. The critical and high findings are PLAN.md sub-goals `SG-A7.1` to `SG-A7.3`; the medium is `SG-A7.4`.

Failing tests: `backend/tests/audit/test_audit_7.py`, each `xfail(strict=True)`, failing today at its final assertion. The timed ones fail by 4x or more on the audit box (4 cores), and assert limits a fix meets with room to spare. Run them with `cd backend && pytest tests/audit/test_audit_7.py -q --runxfail`.

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A7-1 | critical | Globs are matched with backtracking regexes: a 30-byte glob against one 255-character event name never finishes, on the API event loop | SG-A7.1 |
| A7-2 | high | The batch verdict solve is superlinear in distinct atoms x events and unbounded; a policy within the size caps takes 280 s and 4.3 GB at 1 000 events, and the counterexample budget is not enforced for atom-heavy policies | SG-A7.2 |
| A7-3 | high | `/check`, event appends and whole-trace ingest step the monitor on the event loop with no bound on policy size x events; one request stalls the worker for seconds | SG-A7.3 |
| A7-4 | high | Each `count ... by name` node keeps up to 1 000 names of 255 characters in the trace's Redis snapshot; a 13 KB policy makes one trace's snapshot 73 MB | SG-A7.3 |
| A7-5 | medium | The idle sweeper (PR #35) closes a paused trace for good: the next append is 409, the SDK stops streaming, and the rest of the trace is neither stored nor counted | SG-A7.4 |
| A7-6 | low | The batch check loads whole span rows, payloads included | none |

## Method

- Read PLAN.md, AUDIT-1 to AUDIT-6 and every file under `app/policy/`, `trace_policy_service.py`, the policy paths of `trace_service.py`, `policy_tasks.py` and the SDK's streaming code, with the diffs of PRs #24 to #35.
- Ran `tests/audit` and `tests/policy` (688 passed, 5 xfailed), then the property tests of `test_counterexample.py`, `test_monitor.py` and `test_differential.py` at 10x their example counts (181 passed, 17 min): sufficiency and minimality, including the PR #34 name codes, and monitor vs reference across restores. No divergence.
- Ran every `bench_policy.py --assert-budget` row: all within budget (live p95 0.02 ms, ingest 0.11, batch 83, precheck 3.7, counterexample 240).
- Probed outside the bench shapes: glob shapes, policies near the 20 000-character and 2 000-node caps, traces and prefixes near 10 000 events, many `count ... by name` nodes, and a paused stream.

## Critical

### A7-1 Backtracking glob matching (ReDoS)

`encode.glob_regex` turns a glob into a Python regex by replacing each `*` with `.*`, and `matches()` calls `fullmatch` on every event name. Python's `re` backtracks, so a glob with k stars and a name that almost matches cost about C(n, k) steps:

| Glob | Name | Time |
|---|---|---|
| `*a` x 6 + `b` | `a` x 60 | 0.56 s |
| `*a` x 7 + `b` | `a` x 50 | 1.2 s |
| `*a` x 8 + `b` | `a` x 60 | 27 s |
| `*a` x 10 + `b` | `a` x 60 | killed after 60 s |

Names are up to 255 characters, so `tool("*a*a*a*a*a*a*a*a*a*ab")` against a 255-character name of `a`s does not finish in any useful time. The match runs inside `Monitor.step`, called on the event loop by `/check`, event appends (REST and `/ws/ingest`) and whole-trace ingest, and in the batch check, the counterexample's `_hits` and the reference evaluator (same translation). A regex holds the GIL and can't be cancelled. So any user can register, create this 30-byte policy in their own org, and send one event or one `/check`: that API worker stops serving every tenant, and each further request takes another worker. The DSL docs promise `*` and `?` only, so no regex features are needed.

Tests: `test_glob_matching_is_not_exponential` (`Monitor.step`, 7 stars against 50 characters, 1.1 s against a 0.1 s limit) and `test_check_with_a_backtracking_glob_returns_quickly` (the same through `/check`).

Fix direction: match globs without backtracking, e.g. the two-pointer wildcard algorithm (`O(len(name) x len(glob))` worst case, linear in practice) or a translation whose `*` segments can't re-split (each literal run between stars found by `str.find`, left to right). `reference.py` needs the same change, or the differential tests will hang on such globs. The `_automaton` in `counterexample.py` is already a DFA and is unaffected.

## High

### A7-2 Batch verdicts and counterexamples are unbounded in atoms x events

`first_violations` declares one Boolean per distinct atom per event, asserts its observed value, and solves with no timeout. Solve time grows with the square of atom-event cells or worse (`TraceEncoding` + `Then("simplify", "propagate-values", "solve-eqs", "smt")`, one policy `always historically not (tool("n0") or ... )`):

| Distinct atoms x events | Cells | Build | Parse | Solve | RSS |
|---|---|---|---|---|---|
| 20 x 10 000 | 200 k | 0.3 s | 3.5 s | 1.9 s | |
| 200 x 1 000 | 200 k | 0.4 s | 3.6 s | 2.2 s | 1.1 GB |
| 200 x 2 000 | 400 k | 0.9 s | 9.2 s | 14.4 s | |
| 100 x 10 000 | 1 M | 2.2 s | 22 s | 261 s | |
| 1 000 x 1 000 | 1 M | | | 278 s in all | 4.3 GB |

The 1 000-atom policy is 17.9 KB, inside the 20 000-character and 2 000-node caps (SG-A5.1). At 1 000 events `check_completed_trace` passes its 240 s soft limit; the verdicts-only retry in the handler runs the same solve and is hard-killed at 300 s, so the trace gets no batch rows and its pending live rows stay `pending`, the outcome SG-A2.2 fixed. At 5 000 events the counterexample alone ran out of memory (exit 137 on a 15 GB box). The cost doesn't need malice: 100 distinct atoms across an org's policies on one 10 000-event trace is 1 M cells, 285 s (row 4). (The bench's 10 policies at 10 000 events take 5.4 s, so the CI rows don't show this.) Each such trace holds one of the two `policy` workers for 5 minutes, delaying every org's batch checks.

The counterexample budget (`BUDGET_S` = 2 s, said by SG-A5.4 to hold on every path) only charges count cells. `FactEncoding` builds and Z3 parses one term per atom per event before the deadline is first checked: 990 atoms x 1 000 events took 59 s for a fallback.

Tests: `test_batch_verdicts_scale_with_atoms_times_events` (100 atoms x 1 000 events, 4 s against 0.5 s) and `test_counterexample_budget_holds_for_many_atoms` (150 atoms x 1 000 events with `BUDGET_S` = 0.2, 3.8 s against 1 s).

Fix direction: the verdict needs no solver. Every observed atom is a constant, so `Monitor` (already differential-tested equal to `check_trace`) gives the first violations in linear time; or the encoding can substitute the observed constants so `simplify` folds them. Charge atom terms (atoms x window) against the counterexample cell cap and check the deadline while building them.

### A7-3 Monitor work on the event loop is unbounded per request

`precheck`, `append_events` and `ingest_trace` (via `blocking_violations`) are `async` and step `Monitor` synchronously. Per-event cost is linear in policy size, and a request carries up to 10 000 events (`prefix`, `events`, `tool_calls`). Measured with `would_violate` over a 9 998-event prefix:

- one 990-atom policy (17.9 KB): 7.2 s per `/check`;
- the bench's 10 policies: 0.19 s per `/check`, at the default 600 checks per minute per org.

During those seconds the worker serves nothing else: `test_check_does_not_stall_the_event_loop` measures a 4.0 s event-loop stall from one `/check` with a 500-atom policy. The rate limit counts requests, not work, so one org at its own limit occupies several workers. A long append can also outlive the 60 s append lock; a second append to that trace then fails on the `(trace_id, seq)` index with a 500 instead of a 409.

Test: `test_check_does_not_stall_the_event_loop` (stall must stay under 0.5 s).

Fix direction: run monitor replay and stepping off the event loop (`asyncio.to_thread` or a process pool), and bound the work a request may cause (policy nodes x events per request or per org), answering 413/422 past it. Charging the rate limit by events would also help.

### A7-4 Monitor snapshot size per trace is unbounded

SG-A2.4 bounded each `count ... by name` node to `MAX_GROUPS` = 1 000 names, but not their length (255 characters) or the number of such nodes in a policy. A 12.8 KB policy with 300 `count(not tool("zN")) by name < 99` nodes, after 1 000 events with distinct 252-character names, gives a 73.5 MB snapshot. Each append restores, steps and re-snapshots it on the event loop (1.0 s, so A7-3 again), and Redis keeps it 24 h per trace. Redis has no `maxmemory` (known gap A2-5), so about a hundred such traces exhaust it, taking down rate limits, locks, pub/sub and every org's monitor state.

Test: `test_monitor_snapshot_size_is_bounded` (77 MB against an 8 MB limit; deterministic).

Fix direction: key groups by a fixed-size digest of the name, and bound the total groups per monitor (all nodes, all policies) rather than per node, keeping the overflow rule SG-A2.4 already proves sound.

## Medium

### A7-5 An idle close is final (PR #35)

`close_idle_streams` sets `meta.stream = "idle"` on a trace with no append for `STREAM_IDLE_S` = 1 h, and `append_events` treats any stream other than `open` as closed: the next append is 409 `trace_closed`. An agent that waits over an hour (a human approval, a long tool call) then loses the rest of its trace. The SDK reads `trace_closed` as fatal (`_settle` sets `_dead`, SG-A6.2), drops the events and stops streaming. Later checks go to the server, which judges them against the events stored before the pause. So events after the pause are never stored or batch-checked, count policies (`max N retries`) never count them, and a `confirmation` sent after the pause can't satisfy a later `delete_*` check. With `fail_closed`, every later check of that trace fails. A6-12 asked for traces that never send `final` to be closed, not for paused ones to be cut off.

Test: `test_append_after_an_idle_close_is_accepted`.

Fix direction: an append to an `idle` trace reopens it (`stream` back to `open`, re-tracked, monitor rebuilt from spans). The batch check is idempotent per (trace, policy, version) and runs again on the next close. Alternatively, a much longer, configurable idle window. Either way the docs should say what happens.

## Low

### A7-6 The batch check loads full span rows

`check_completed_trace` selects whole `Span` rows (`select(Span)`, `trace_policy_service.py`), payload `input`/`output` JSON included, while the live replay reads only the checker's columns (`_EVENT_COLUMNS`, SG-A2.3). At 10 000 events with large tool payloads that is the payload size again in the Celery worker's memory, on top of A7-2. Fix: reuse `_EVENT_COLUMNS`.

## Re-checked, no finding

- Round-2 encodings: unary and bit-vector counts (SG-A5.2), name codes for `count ... by name` over globs (SG-A5.4), the cell cap. Sufficiency and minimality hold at 10x the property-test examples; SMT text holds no client strings (names become codes or `\u{}` escapes), so there is no SMT injection.
- `Monitor`: the `_count_group` marker escaping, `failing` vs `check`, snapshot digests after policy edits, `MAX_GROUPS` overflow soundness (10x Hypothesis vs reference).
- Refused appends (SG-A5.3): the refused row's key, replayed violations recorded once, state not saved. The pending-row fill-in in `check_completed_trace` (SG-A3.5) doesn't raise for archived or disabled policies.
- The idle sweeper's locking against appends; the depth bound (SG-A6.4) on every recursive walker.
- Budgets on the bench shapes (Method).
