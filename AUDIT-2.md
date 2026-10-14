# AUDIT-2: performance under load

Audited at `7df9f44` (SG0 to SG6 merged) by a session that wrote none of the code. Scope: PLAN.md audit row A2 plus the coordinator's list (concurrent traces, many policies, long traces, pathological policies, Redis state growth and loss, Celery backlog, precheck under concurrency, concurrent ingest races).

**Result: 1 critical, 2 high, 4 medium, 3 low.** Critical and high findings are PLAN.md sub-goals `SG-A2.1` to `SG-A2.3`; the mediums are grouped into `SG-A2.4`.

## Setup

- 4-core cloud container, Python 3.11, `z3-solver` 5.1.0, Postgres 16 and Redis 7 local, `uvicorn --workers 2`, `celery worker -c 2`. The load generator ran on the same box, so absolute HTTP numbers are pessimistic; the ratios are what matter.
- Repro tools added in this PR:
  - `backend/scripts/bench_audit2.py <mode> N...`: in-process checker scaling (no DB).
  - `backend/scripts/load_e2e.py setup|append|precheck|flush`: HTTP load against a running backend.
  - `backend/tests/test_audit2.py`: one `xfail(strict=True)` test per fixable finding. Each fails today for the stated reason (`pytest tests/test_audit2.py --runxfail` shows the numbers) and will XPASS, failing CI, once fixed, so the fixing sub-goal must remove the marker.

## Budget rows re-checked

| Row | Budget p95/p99 | Measured (bench workload) | Under audit load |
|---|---|---|---|
| Live `Monitor.step` | 5 / 20 ms | 0.018 / 0.051 ms | 2x SG4 load (1 000 ev/s, 60 s, 50 traces): 0.046 / 0.075 ms, ok |
| Ingest overhead | 8 / 25 ms | 0.39 / 1.07 ms | 2x load: 0.30 / 0.58 ms, ok. 20 000 distinct names under `count by name`: 38 ms p95 (A2-6) |
| Batch `check_trace` | 250 / 600 ms | 85 / 88 ms | 50 policies: 394 ms p95. 500 events with `retry_cap`: 53 s (A2-1). 1 000 events: did not finish in 15 min |
| Precheck | 10 / 30 ms | 3.3 / 3.7 ms | After Redis state loss, 5 000-event trace: 280 ms per check over HTTP, every check (A2-3) |
| Counterexample | 500 / 1000 ms | 71 / 75 ms | `count … by name`, 200 events: 7.5–8.5 s (A2-1) |

The CI budget rows pass because the bench workload never exercises the slow paths: in the 200-event bench trace the `count … by name` policy is not violated, and the counterexample row uses only policy 0.

## Findings

### A2-1 · critical · `count … by` counterexamples are cubic; one trace can stall the batch pipeline

`FactEncoding._count` with `by` builds, for each position i, a comparison against every earlier event (O(n²) terms), and the deletion pass in `minimal_counterexample` re-solves that encoding once per core event. The measured growth is about cubic in the window length. This affects the policy shape PLAN.md uses as its DSL example and that the seed ships as `retry_cap`.

```
$ python backend/scripts/bench_audit2.py countby 50 100 200 300 400
countby  window     50:   110 ms
countby  window    100:   790 ms
countby  window    200:  7510 ms     # budget row: 500 ms for 200 events
countby  window    300: 23140 ms
countby  window    400: 63110 ms
$ python backend/scripts/bench_audit2.py batch 500     # the 10 bench policies on a 500-event bench trace
p2 (count(tool and status error) by name <= 3) violated at 352: counterexample 53389 ms
```

What follows from this:
- `check_trace` computes every policy's counterexample before returning, so one slow policy holds up the whole trace's batch rows, including rows for other policies.
- Celery's `task_time_limit=300` hard-kills the task for any first violation past about 650 events. The task is `acks_late` but default `task_acks_on_failure_or_timeout` acks it anyway, so the trace's batch check is lost: no batch rows, live rows never get their counterexample, and nothing records that the trace went unchecked.
- Every API key holder can submit such traces (no event cap, A2-2). With N worker processes, N poisoned traces block every other Celery task (batch checks, `evaluate_alert_rules`, anomaly detection, evaluations) for up to 5 minutes each. Celery repro: two 800-event poison traces, then a normal 200-event trace (see **Celery repro** below).

Failing test: `test_count_by_name_counterexample_meets_budget`.

### A2-2 · high · The batch path has no bound on work per trace and shares one queue with alerting

There is no cap on events per trace or per request (`TraceIn.events`, `EventsAppendIn.events` and `/ingest/bulk` are unbounded lists) and no soft time limit or fallback in `check_completed_trace`. Even without A2-1, batch cost is superlinear in trace length:

```
$ python backend/scripts/bench_audit2.py long 1000 5000 20000     # first_violations only, no counterexamples
long   1000 events:    369 ms
long   5000 events:   2455 ms
long  20000 events:  21484 ms
```

Backlog: with 2 worker processes, 300 whole-trace ingests of 200 events (accepted at 41 traces/s) took about 36 s to drain, about 8 traces/s, or about 120 ms of worker time per trace beyond `check_trace` itself (each task builds and disposes its own async engine and Redis client). Batch checks, the per-minute `evaluate_alert_rules` and anomaly detection share the default queue, so a batch backlog delays alerting with nothing to show it. SG-A2.2 covers the bounds; A2-1 is the worst case it has to absorb.

### A2-3 · high · Precheck replays the whole trace on every call once the Redis monitor state is gone

`precheck` rebuilds the monitor from all spans when `policy:monitor:{trace_id}` is missing, and by design never writes it back. Every check on that trace then loads every span row (payloads included) and replays it until the next append. The state goes missing after a Redis restart or eviction, or 24 h after the last append.

```
$ python backend/scripts/load_e2e.py setup && python backend/scripts/load_e2e.py flush --traces 10 --prefix 5000 --checks 5
append, state cached:       10 ok  p50   76 ms  p95   78 ms
append, after FLUSHDB:      10 ok  p50 1664 ms  p95 1674 ms      # each trace rebuilds once, then caches
precheck x5, after FLUSHDB: 50 ok  p50  831 ms  p95 1785 ms  max 1894 ms   # never recovers
sequential single check on one 5 000-event trace after FLUSHDB: 274, 285, 316 ms   (budget 10 ms)
$ python backend/scripts/bench_audit2.py replay 5000 20000    # replay CPU alone
replay   5000 prefix: would_violate   61 ms
replay  20000 prefix: would_violate  260 ms
```

The SDK's default `check_timeout_s=2.0` with `fail_closed=False` turns a slow check into `allowed=True`, so the guard stops guarding long traces after a Redis restart, and does so silently. Concurrent checks on 10 such traces already reach 1.9 s on 2 workers. Failing test: `test_precheck_after_state_loss_replays_spans_once`.

### A2-4 · medium · Concurrent violations fire one `policy_violations` rule twice (confirmed on Postgres)

`evaluate_violation_alerts` runs `compute_metric_value`, then `already_firing`, then `fire_alert` with nothing serialising it across sessions. 20 concurrent appends of `drop_db` on separate traces against a running server (2 workers), with one rule (`>= 1`, `dedup_minutes=30`, critical), produced 2, 2 and 1 `AlertEvent`s in three runs, and the same number of incidents. More workers give more duplicates. SG5a noted this as an open item; it is reproducible. Failing test: `test_concurrent_violations_fire_a_rule_once` (deterministic: two sessions are held between the dedup check and the insert).

### A2-5 · medium · Redis loss makes every open trace rebuild at once

Same root as A2-3 on the append path: after a Redis restart, the first append to each open trace replays all its spans (1.7 s each for 5 000 events under 10 concurrent appends, figures above). There is no throttling, so a restart with many long open traces is a thundering herd on Postgres and the API workers. Separately, `docker-compose*.yml` run Redis without `maxmemory` or a persistence policy, so monitor state (24 h TTL per open trace) grows without limit and is lost on restart.

### A2-6 · medium · `count … by name` monitor state grows with every distinct name

The per-group counter dict is unbounded, and event names are client-chosen (any string up to 255 characters). The snapshot is restored and re-serialised on every append.

```
$ python backend/scripts/bench_audit2.py names 1000 20000 50000
names   1000 distinct: snapshot  13 KB, restore+step+snapshot p50  0.3 ms p95  0.4 ms
names  20000 distinct: snapshot 282 KB, restore+step+snapshot p50  6.9 ms p95 37.7 ms   # ingest budget 8 ms
names  50000 distinct: snapshot 722 KB, restore+step+snapshot p50 50.3 ms p95 72.2 ms
```

Failing test: `test_ingest_step_with_many_distinct_names_meets_budget`.

### A2-7 · medium · Overlapping globs make counterexamples exponential in the glob count

`name_patterns` enumerates every realisable subset of a policy's globs (2^k for k mutually overlapping globs such as `*a*`, `*b*` …). Prefix globs like `http_*` don't overlap, so ordinary policies are unaffected, but nothing rejects a policy that is, and the cost is paid in the batch worker (A2-2).

```
$ python backend/scripts/bench_audit2.py globs 8 10 12 14
globs  8 overlapping:   31 ms  (256 glob-match sets)
globs 10 overlapping:  125 ms
globs 12 overlapping:  915 ms
globs 14 overlapping: 4818 ms  (16384 glob-match sets)
```

Failing test: `test_overlapping_globs_counterexample_meets_budget`.

### A2-8 · low · Batch cost is linear in policies; 50 structurally distinct policies exceed the 10-policy row

`python backend/scripts/bench_audit2.py policies 10 50`: p95 127 ms at 10 policies and 394 ms at 50 (200 events). The budget row is defined for 10 policies, so this is not a breach, but nothing caps policies per org.

### A2-9 · low · The streamed-event endpoint tops out around 155 requests/s, with or without policies

`load_e2e.py append` (one event per request, as the SDK streams it):

| Policies | 100 ev/s offered: p50 / p95 / p99 | 400 ev/s offered: achieved, p95 |
|---|---|---|
| 0 | 22.5 / 42.3 / 60.3 ms | 156/s, 764 ms |
| 10 | 24.3 / 48.4 / 63.1 ms | 156/s, 744 ms |
| 50 | 23.6 / 42.7 / 85.6 ms | 145/s, 804 ms |

The policy layer adds only about 2–6 ms p95, within the 8 ms row. The ceiling comes from the request path itself (Redis lock, about 8 DB round trips, commit). SG4's "500 events/s for 60 s" acceptance was measured in-process only and doesn't describe the service. Two consequences: SDK streaming sends one request per event with no batching, and the default `rate_limit_per_min=600` per org is shared by appends and prechecks, so a guarded agent makes at most about 5 tool calls/s per org before 429s, which fail open (already noted in SG6).

### A2-10 · low · CI budgets are in-process only

`bench_policy.py` never touches the DB, Redis or Celery and uses one fixed benign trace (see the table above), so A2-1, A2-3 and A2-6 sit inside green budget rows. Fixing this belongs to the sub-goals: each adds its own bench case.

## Celery repro

With `celery -A app.celery_app worker -c 2` and the `load_e2e.py setup` org (10 bench policies), `python backend/scripts/load_e2e.py poison --traces 2` ingests two 800-event traces whose 4th `fetch` error is the last event (and whose first event violates `historically not drop_db`), then one ordinary 200-event trace.

```
normal 200-event trace checked after 299 s          # both worker processes were busy with the poison traces
violation rows for the poison traces after 360 s: [0, 0]
celery: Hard time limit (300s) exceeded for app.tasks.policy_tasks.check_completed_trace[...]   (x2)
```

The poison traces end up with no violation rows at all, not even the trivial `drop_db` one at seq 0. Their tasks are acked and not retried, and only the worker log shows it.

## Checked and fine

- 2x the SG4 load in-process (1 000 events/s, 60 s, 50 interleaved traces): step p95 0.046 ms, restore+step+snapshot p95 0.30 ms.
- Precheck with cached state under concurrency: 10 traces x 2 000-event prefix at 50 checks/s over HTTP, p95 24.9 ms, p99 33.1 ms, no errors.
- Policy count barely affects the live path: 50 policies vs 0 add under 6 ms p95 per append (A2-9 table).
- Per-trace append lock: concurrent appends to different traces don't contend; the `seq` check plus the lock kept every trace consistent under load (the 409s seen at 1 000/s were the harness continuing after its own client-side timeouts).
