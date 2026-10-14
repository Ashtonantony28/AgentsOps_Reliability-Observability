# AUDIT-5: correctness and performance (round 2)

Audited at `eb7e255` (everything through PR #21 merged) by a session that wrote none of the code. Scope: the policy-verification layer end to end (DSL parser and reference evaluator, Z3 encoding, counterexamples, batch and live checker, alert pipeline, SDK pre-checks), whether the round-1 fixes (PRs #13, #14, #15, #17, #18, #20, #21) still hold, new soundness bugs, and latency-budget breaches outside the bench workload.

**Result: 1 critical, 2 high, 2 medium, 5 low.** The critical and high findings are PLAN.md sub-goals `SG-A5.1` to `SG-A5.3`; the mediums are grouped into `SG-A5.4`.

Failing tests: `backend/tests/audit/test_audit_5.py` (6) and `sdk/tests/test_audit_5.py` (1), each `xfail(strict=True)`; the fixing sub-goal removes the marker. Run them with `cd backend && pytest tests/audit/test_audit_5.py -q --runxfail` and `cd sdk && pytest tests/test_audit_5.py -q --runxfail`. Three of them cost wall time while unfixed (A5-1 10 s, A5-3 6 s, A5-2 2 s).

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A5-1 | critical | Tags are inlined as shared nodes, but the monitor, encoder and plan cache walk the formula as a tree: a 750-byte policy never finishes, and anyone who can register can stall shared API and Celery workers | SG-A5.1 |
| A5-2 | high | Counts with a bound of 16 or more get non-minimal counterexamples (169 events where 41 suffice) after 2 s, 4× the 500 ms worst-case row, at the budget's own 200-event size | SG-A5.2 |
| A5-3 | high | One blocked append ends enforcement for the rest of a streamed trace: the SDK stops streaming, every later guard check fails open without reaching the server, the trace never closes and the blocked violation is stored nowhere | SG-A5.3 |
| A5-4 | medium | The 2 s per-counterexample budget is not enforced: a nested `count … by` runs 2.7 to 17 s at 300 to 600 events and builds 72 MB of SMT text | SG-A5.4 |
| A5-5 | medium | Concurrent `add_event` calls from threads give duplicate and out-of-order seqs, so the whole trace is rejected (422) or the stream dies | SG-A5.4 |
| A5-6 | low | A deeply nested policy raises `RecursionError`: 500 on create and validate instead of a parse error | none |
| A5-7 | low | Past `MAX_GROUPS`, a live false positive is stored and blocks every later event, and the batch check cannot overturn it | none |
| A5-8 | low | A version bump with an unchanged formula reuses the monitor's "already reported" flag: no live row for the new version, then a second batch row for the same trace | none |
| A5-9 | low | Batch verdicts go through Z3 at about 1 ms per event per policy, 60× slower than replaying the monitor | none |
| A5-10 | low | The violations CSV writes agent names as is, so a name starting with `=` is a spreadsheet formula | none |

## Round-1 fixes: re-checked and held

- Full backend suite on `eb7e255`: 1005 passed, 4 skipped, 10 xfailed (the A3-1/A3-2/A3-5/A3-7/A3-12/A3-13/A3-14 markers waiting on the user, plus A1-L1). Every test that came from an A1, A2 or A4 finding passes without its marker.
- `python backend/scripts/mutants_audit4.py`: every A4 mutant still killed (see **Method**).
- All five `bench_policy.py --assert-budget` rows pass on the bench workload: live p95 0.023 ms, ingest 0.123 ms, batch 93 ms, precheck 3.2 ms, counterexample 91 ms.
- SG-A1.1: `append_events` blocks on `Monitor.failing`, so a `block` policy keeps blocking after its first recorded violation, and `Monitor.check` agrees.
- SG-A1.2 / SG-A2.1: verdicts are computed before any counterexample, every counterexample falls back to a sufficient set, and the bound-3 retry cap is minimal and fast at 1 000 events. A5-2 and A5-4 are shapes the fix did not reach.
- SG-A2.2: `MAX_TRACE_EVENTS` = 10 000 bounds the batch: 10 000 events × the 10 bench policies take 9.7 s for verdicts and 10.6 s with counterexamples, well inside the 240 s soft limit.
- SG-A2.3: a precheck after state loss rebuilds once per trace under a Redis lock.
- SG-A3.2 / SG-A3.3: `/check` runs legacy block rules on the candidate; a sync guard in an async streamed trace pushes before checking.
- SG-A3.4: own `check:{org}` rate-limit bucket, `log` policies kept out of alert counts, idempotent resends.
- SG-A4.1 / SG-A4.2: per-test database, Postgres run and coverage floors in CI.

Already-known items, severity unchanged: SG-A3.1 and SG-A3.5 (interface changes waiting on the user); nested `count by` inside `once` is quadratic (A5-4 is a new aspect: the fallback is not bounded); Redis has no memory limit or persistence; pre-check over HTTP is 13 to 17 ms; the sync guard in an async agent blocks the event loop while sending; concurrent duplicate uploads can both store.

## Method

- Read every module in scope and the PLAN.md contract, then probed each checker outside the bench shapes: tag-heavy policies, count bounds from 3 to 40, nested `count … by`, traces from 200 to 10 000 events, more than `MAX_GROUPS` distinct names, deep nesting.
- Differential checks against `reference.evaluate` for every repro below (the encoding itself was not re-fuzzed: A1's 100 000 cases and the Hypothesis suites cover it, and nothing here changed it).
- End-to-end through the FastAPI app and the real SDK over `httpx.ASGITransport` (the A3 harness) for A5-1 and A5-3.
- Timings are on the audit container (Python 3.11, z3-solver from `requirements.txt`), single process, warm.

## Findings

### A5-1 · critical · Policy cost is exponential in tag reuse

`tag b = @a and @a` inlines `a` twice by reference, so the parsed formula is a DAG: `parse` is linear. Nothing after the parser treats it as one:

- `monitor.Plan._add` recurses into both children before looking up its node key (`monitor.py:60`), so it visits every path.
- `monitor.plan_for` keys its cache on `repr((name, severity, formula))` (`monitor.py:42`), and `_plan`'s `lru_cache` hashes the `Policy`. Both are tree-sized and run on every `Monitor(...)`, cache hit or not.
- `encode.TraceEncoding._encode` and `counterexample._nodes` recurse the same way.

A policy of `n` doubling tags has `n + 2` distinct nodes and `2^n` leaves:

```
tag t0 = tool("a")
tag t1 = @t0 and @t0
...
tag tN = @t(N-1) and @t(N-1)
policy p severity high:
  always not @tN
```

| N | source | first `Monitor(...)` | `repr` on every later `Monitor(...)` |
|---|---|---|---|
| 16 | 400 B | 0.58 s | |
| 18 | 448 B | 2.3 s | 0.37 s, 19 MB |
| 20 | 496 B | 10.5 s | 1.8 s, 76 MB |
| 22 | 560 B | ≈ 45 s (est.) | 8.0 s, 306 MB |
| 30 | 750 B | does not finish | ≈ 80 GB (est.) |

`POST /api/trace-policies` accepts the source (200, it only parses), and so does `/validate`. From then on every append, `/check`, whole-trace ingest and batch check in that org builds a `Monitor` or encoding, synchronously on the API event loop, so one request stalls every tenant on that worker; Celery tasks run into the hard limit. `POST /auth/register` creates an org with the caller as owner, so this needs only a sign-up, an agent, an API key and one request. Severity critical for that reason.

- **Failing test**: `test_policy_cost_is_linear_in_source_size` (N = 30 in a subprocess with a 10 s timeout; a fix that rejects the policy at parse time passes too).
- **Fix**: memoise on node identity (or intern nodes in the parser) in `Plan._add`, `TraceEncoding._encode` and `_nodes`; key the plan cache on a digest computed over the DAG instead of `repr`; and cap the expanded size or the source length at parse time as a backstop, with a 422.

### A5-2 · high · Counts with a bound of 16 or more get non-minimal counterexamples, slowly

SG-A2.1 encodes counts in unary up to `_UNARY_MAX = 16` and keeps bit-vector adders above it (`counterexample.py:256`). Z3's unsat cores over the adders are close to the whole window, and the deletion pass runs out of the 2 s budget long before it shrinks them. The bench counterexample row only uses p0 and p2 (bound 3), so this is never measured.

Minimal counterexample for `always count(tool("fetch_url")) <= 40` violated at the 41st `fetch_url`: the 41 hits (every other event can only add to the count).

| policy (200-event trace) | result | time |
|---|---|---|
| `count(tool("fetch_url")) <= 40` | 169 events | 2.0 s |
| `count(tool("fetch_url")) by kind <= 40` | 171 events | 2.0 s |
| `count(tool(*) and status("error")) by name <= 40` | 168 events | 2.0 s |
| `count(tool("fetch*") and status("error")) by name <= 20` | 188 events | 2.0 s |
| `count(tool("fetch_url")) <= 15` (unary) | 16 events (minimal) | 85 ms |

At 400 events the first row keeps 365 events. Rate and usage caps (`<= 20`, `< 150`) are the common case for this DSL, and milestone 4 promises the minimal sequence; the counterexample row is 500 ms worst case at 200 events. Verdicts are unaffected.

- **Failing test**: `test_large_bound_count_counterexample_is_minimal` (asserts the 41 hits; the 500 ms row belongs in the bench).
- **Fix**: extend the "at least k" encoding (a sequential counter or totalizer, linear in window × bound) to any bound up to the window size, or seed the deletion pass with the events that satisfy the count's predicate before trying the rest; add a bound-40 case to `bench_policy.py --path counterexample`.

### A5-3 · high · One blocked append turns enforcement off for the rest of a streamed trace

PLAN.md's contract is "403 on `block`, nothing stored" for an append. The SDK treats that 403 as fatal (`precheck.push_outcome` → `Trace._settle` sets `_dead`), and from then on:

- `Trace._undelivered()` is true, so `check`/`acheck` and every `@guard` return `failed_result` without calling the server: `allowed=True` under the default `fail_closed=False` (only a log warning).
- No later event, and no `final`, is ever sent. The trace stays `meta.stream = "open"` for good, so `check_completed_trace` skips it: no batch check, no counterexamples.
- The blocked violation is stored nowhere but a `policy_block` audit row: no `policy_violations` row, no WS push, no alert, nothing in reports. Non-block violations in the same rejected batch are lost too.

The 403 is not rare. `/check` evaluates the candidate as `status: "success"` with the guard's static attrs, and the guard then records the real outcome. PLAN.md's own `retry_cap` example (`count(tool(*) and status("error")) by name <= 3`) as a `block` policy is allowed by every check and rejected by the append that records the 4th error. Any unguarded `add_event` that violates a `block` policy has the same effect.

```
policies: retry_cap (<= 1, block), no_drop (not tool("drop_db"), block)
@guard flaky_fetch() raises twice   -> both checks allowed; the 2nd recorded error is a 403, stream dead
@guard drop_db()                     -> "trace events not delivered; allowing": drop_db runs
```

- **Failing tests**: `test_guard_still_enforced_after_a_blocked_append` (`drop_db` must raise `PolicyBlocked`), `test_blocked_append_is_surfaced` (one violation row and a closed trace).
- **Fix** (default picked, no change to the interface contracts section): on a 403 the server stores the blocking violation (`mode: "live"`) for the trace while still rejecting the events; the SDK treats `policy_block` as "this batch was refused", not as a dead stream: it drops the refused events, renumbers what follows, keeps checking against the server, and sends `final` on exit. This reverses SG4's "nothing stored" line, so it should be confirmed before dispatch if that line was deliberate.

### A5-4 · medium · The counterexample budget is not enforced

`FactEncoding.cone` checks the deadline only before building each lazy `count … by` term, and `Solver.from_string` is not interruptible. For `always not once (count(tool(*) and status("error")) by name > 3)` with the 4th error as the last event:

| events | time | SMT text |
|---|---|---|
| 250 | 2.1 s | |
| 300 | 2.7 s | |
| 400 | 7.7 s | |
| 600 | 17.3 s | 72 MB (`cone` 3.7 s, `from_string` 14.7 s) |
| 800+ | 2.0 s (the cone times out first) | |

Every result is the every-event fallback, as SG-A2.1 noted for this shape; what it did not note is that the 2 s budget overruns 8×. `CHECK_BUDGET_S` is only checked before each counterexample starts, so a trace with several such policies runs well past 30 s.

- **Failing test**: `test_counterexample_budget_is_enforced` (350 events, asserts under 1.5 × `BUDGET_S`).
- **Fix**: estimate the encoding size before building it (window² per nested `count … by`) and fall back at once past a threshold; check the deadline inside `_group_count`.

### A5-5 · medium · Concurrent `add_event` calls give duplicate seqs

`Trace.add_event` computes `seq` as `len(self.events)` and appends afterwards, with no lock in between (`trace.py:85`). Eight threads × 2 000 events with a short switch interval gave 151 duplicate seqs out of 16 000. Agents that run tool calls in a thread pool with `@guard` on each tool record from those threads. A non-streamed trace with a duplicate seq is a 422 for the whole trace; a streamed one gets `seq_conflict` with an `expected_seq` the SDK cannot use, so the stream dies and A5-3's fail-open follows. Rare under the default switch interval (0 of 200 trials with 8 workers), likely under free-threaded Python.

- **Failing test**: `sdk/tests/test_audit_5.py::test_concurrent_add_event_keeps_seqs_contiguous`.
- **Fix**: take `_lock` around computing the seq and appending.

### A5-6 · low · Deep nesting is a 500

`always not not … tool("a")` with 1 000 `not`s raises `RecursionError` in the recursive-descent parser; `/validate`, create and update catch only `PolicyParseError`. 300 levels parse and check fine.

- **Failing test**: `test_deeply_nested_policy_is_a_validation_error`.

### A5-7 · low · Past `MAX_GROUPS`, a live false positive sticks

SG-A2.4 documented the trade-off: past 1 000 distinct counted values, an undecided count makes the policy false from then on, so nothing is missed. The consequences beyond "extra violations": with `always count(tool(*)) by name <= 3` and 1 010 distinct tool names (each used once, `reference.evaluate` = `[]`), the monitor reports a violation at seq 1003. The live row is stored and, under `block`, every later event of the trace is a 403 (which then triggers A5-3). The batch check is exact but its insert is `on conflict do nothing`, so the false live row stays; if a real violation occurs later, the batch fills the false row's empty counterexample with events from after its `at_seq`.

- **Repro**: `Monitor([compile_policy("cap", p)])` stepped over `Event(i, "tool", f"tool_{i}", "success", {})` for `i < 1010`.
- **Suggested**: when a count becomes undecided, recount that trace exactly from its spans (the DB is the source of truth) instead of failing the policy.

### A5-8 · low · Version bump with an unchanged formula

The monitor's plan digest covers name, severity and formula, not `version`. Editing only whitespace or comments bumps `version` but restores the old state, including `_done`, so the new version's violation is never recorded live; at `final` the batch stores it as a second row for the same trace (v1 live + v2 batch), counted twice in reports and alert windows.

### A5-9 · low · Batch verdicts are much slower than the monitor

10 000 events × the 10 bench policies: `check_verdicts` 9.7 s; replaying `Monitor` over the same events 0.16 s, with the same semantics (differentially tested since SG4). Within the limits, but it is most of the batch cost and the cost of the soft-limit retry. Z3 is still needed for counterexamples.

### A5-10 · low · CSV formula injection in the violations report

`report_service.violations_to_csv` writes agent names unescaped; an agent named `=HYPERLINK("http://…")` becomes a formula when the CSV is opened in a spreadsheet. Policy names are DSL identifiers and safe. Prefix cells that start with `=`, `+`, `-` or `@` with `'`.
