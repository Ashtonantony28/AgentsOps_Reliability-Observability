# AUDIT-16: platform after SG-AP1 (Audit P2)

Audited at `6f316c0` (the default branch with SG-AP1.1 #66, SG-AP1.2 #67 and SG-AP1.4 #69 merged, plus SG-AP1.3) by a session that wrote none of the code. Scope, from PLAN.md's **Audit P2** entry: the alert tick's isolation and dispatch deadline, login limits, the password-hash pool and single-use refresh, outbound size and time bounds and address checks, the promotion gate, version validation and case weights; and regression checks of org scoping on every route, the policy check path and ingest. A15-5 to A15-18 and the SG-AP1 open lows are known and are not re-reported, except where a finding here is a new consequence of one (each says so).

**Result: 0 critical, 3 high, 2 medium, 3 low.** The highs: a rule whose check raises (one developer's `window_minutes` past `datetime`'s range) ends the alert tick before any page goes out, so since SG-AP1.1 no org's periodic alert is paged and dedup then hides the fired ones (A16-1). Outbound lookups run `getaddrinfo` on the default executor, and SG-AP1.3's deadline cancels the wait but not the thread, so a name that resolves slowly delays every org's ingest and `/check` parsing, A15-2's effect by another road (A16-2). `POST /api/auth/password` is not limited and shares SG-AP1.2's 16-place hash queue, so one signed-in user's wrong-password changes make every login in the process 429 (A16-3). They are PLAN.md sub-goals `SG-AP2.1` to `SG-AP2.3`.

Failing tests: `backend/tests/audit/test_audit_16.py`, one per finding above low, each `xfail(strict=True)`. Each fails today at its final assertion, after its control assertions pass, on SQLite and on Postgres 16 (`TEST_DB=postgres`). Each passes with `--runxfail` against a small sketch fix applied to a scratch copy (not committed): per-rule `try` in the tick; lookups on their own executor; a per-user bound on `/password`; the latency and cost evaluators filtered by the run's version; the per-email key including the address. Run them with `cd backend && pytest tests/audit/test_audit_16.py -q --runxfail`.

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A16-1 | high | A rule whose check raises (`window_minutes` 2 000 000 000 is accepted, and `utcnow() - timedelta` overflows) ends `evaluate_metric_rules`. Since SG-AP1.1 pages go out only after every rule is checked, so every org's rules that already fired that tick are recorded and never paged, and dedup suppresses them for 30 minutes. Later rules are never checked. The 300 s time limit or an out-of-memory kill during the check phase drops pages the same way | SG-AP2.1 |
| A16-2 | high | `outbound._resolve` calls `loop.getaddrinfo`, which runs on asyncio's default executor (ingest and `/check` body parsing, live checks). SG-AP1.3's deadline cancels the await, not the thread. A self-registered admin's integration test of a name that resolves slowly answers at the lookup deadline, while the thread stays blocked until the system resolver gives up: in the test, ingest took 0.81 s against under 0.5 s alone. Alert channels on the violation path, in the API process, take the same road | SG-AP2.2 |
| A16-3 | high | `POST /api/auth/password` has no limit and runs bcrypt through `_hashed`, whose 16-place queue is shared by every login, register and accept-invite of the process. One signed-in member's 16 wrong-password changes in flight make another user's login 429 | SG-AP2.3 |
| A16-4 | medium | `run_evaluation`'s `latency` and `cost` evaluators read the agent's last 100 traces of any version, so A15-4's fix covers the `traces` runner but not these evaluators: a run of 2.0.0 (p95 10 s) scores 1.0 on 1.0.0's 100 ms | none |
| A16-5 | medium | The per-email login bound (SG-AP1.2) counts attempts from every address. Ten wrong logins a minute from one address keep the account's right password refused (429) from any other address. SG-AP1.2 recorded this as "a minute at a time" (low), but it repeats every minute | none |
| A16-6 | low | `CaseIn.weight` is `gt=0`, which admits `Infinity` (Python's JSON reader accepts it) and finite weights whose sum overflows. A run then scores NaN, and the gate's `last.score < threshold` is false for NaN, so it passes (Postgres keeps NaN; SQLite stores NULL, and the gate fails with a `TypeError`, a 500) | none |
| A16-7 | low | The periodic check loads every `Trace` row (with input and output) in an unbounded `window_minutes`, per rule, with rules per org unbounded; it is the slow path behind A16-1's time-limit case | none |
| A16-8 | low | Surviving mutants of the SG-AP1 fixes (see **Mutation probe**) | none |

## Method

- Read the SG-AP1 diff (`git diff 94b6720..6f316c0 -- backend/app`) line by line against each sub-goal's Problem, Acceptance and Result: `services/{alert_service,notification_service,auth_service,outbound,evaluation_service}.py`, `routers/{auth,agents,evaluations}.py`, `schemas/{alert,evaluation}.py`, `tasks/evaluation_tasks.py`, `celery_app.py` and both compose files. Also read the callers each change reaches (`routers/integrations.py`, `services/violation_service.py`, `tasks/alert_tasks.py`).
- Drove each finding through the real app in process (the test app, SQLite and Postgres 16, fakeredis), with outbound requests through `outbound`'s mock transport or a stubbed `socket.getaddrinfo`, and bcrypt stood in by a sleep, as AUDIT-15's tests do.
- Turned each finding above low into a failing test, and checked that each passes against a sketch fix in a scratch copy.
- Mutation probe: one-line mutants of the SG-AP1 fixes, replayed with `python backend/scripts/mutants_audit16.py` (see **Mutation probe**).
- Ran the full backend suite on SQLite (`--ignore=tests/api/test_postgres.py`): 1580 passed, 13 xfailed (the 8 before and AUDIT-16's 5). `tests/audit/test_audit_16.py` on Postgres 16 (`TEST_DB=postgres`): 5 xfailed, and with `--runxfail` each fails at its final assertion, as on SQLite.

## High

### A16-1 One rule that raises drops every org's periodic pages (alerts, availability)

`evaluate_metric_rules` checks every enabled metric rule of every org in one loop, with no per-rule error handling, and since SG-AP1.1 it sends the collected pages only after the loop (`_dispatch_all(pages)`). `fire_alert` commits each `AlertEvent` as its rule fires. So when one rule's check raises, the exception ends the tick:

- rules checked before it have fired and been recorded, but their pages are never sent; the next tick's `already_firing` finds them firing and skips them, so they are not paged until their dedup window (30 minutes by default) passes, and then the same tick dies again;
- rules after it are never checked.

`AlertRuleCreate.window_minutes` is any int, and `compute_metric_value` computes `datetime.utcnow() - timedelta(minutes=rule.window_minutes)`: 2 000 000 000 minutes (about 3 800 years; it fits the `Integer` column) raises `OverflowError`. Any developer of any org, including a self-registered one, can create it with one call (`POST /api/alerts/rules`, 200). Before SG-AP1.1 the same rule stopped only later rules; the drop of pages already fired is new. The tick dying for any other reason during the check phase has the same effect: the 300 s `task_time_limit` (rules per org are unbounded and checked one after another, an SG-AP1.1 open low, and each check loads its window's full rows, A16-7), or the worker running out of memory.

Fix direction: check each rule in its own `try` (log and skip a rule that raises); send pages fired so far even when the check phase stops (send per org as checked, or in a `finally`), or record an event as firing only once its page was attempted; bound `window_minutes` (for example 1 to 43 200, 422 otherwise, a contract addition) and treat stored rows outside it as the default.

Test: `test_one_orgs_failing_rule_does_not_drop_another_orgs_page`. Org B's older rule fires on `token_usage >= 0` with a webhook channel. Org A creates a rule with `window_minutes` 2 000 000 000 through the API (200 today; a stored row if a fix answers 422). After one tick B's event is recorded, but its webhook was never called.

### A16-2 Outbound lookups hold the default executor past their deadline (outbound, availability)

`outbound._resolve` awaits `asyncio.get_running_loop().getaddrinfo(...)`, which runs `socket.getaddrinfo` in a thread of asyncio's default executor. That is the pool A15-2 was about: authenticated ingest and `/check` body parsing (`ApiKeyRoute`), live monitor checks and `/check` stepping. `_pin` wraps the lookup in `wait_for(RESOLVE_TIMEOUT_S)`, and SG-AP1.3 put the whole call under one deadline, but cancelling the await does not stop the thread: it stays blocked until the system resolver gives up. With glibc's defaults (`timeout:5 attempts:2` per name server) that can be several times `RESOLVE_TIMEOUT_S`.

Who can start lookups in the API process:

- `POST /api/integrations/{id}/test`: any member, for a URL an admin set (any self-registered org), with no rate limit;
- alert channels of `policy_violations` rules, sent from ingest in a background task (`violation_service`), 10 per rule, with rules per org unbounded;
- eval runs started with `POST /sets/{id}/run`, which still run inline (an SG-P2 open low).

Each request answers at its deadline while its thread stays busy, so new requests keep the pool occupied. Ingest and `/check` of every org then wait for a thread before their body is parsed. This is A15-2's impact, which SG-AP1.2 closed for bcrypt, reached through SG-AP1.3's path. SG-AP1.3's Result says "the whole call, lookup included, runs under one deadline"; that holds for the coroutine but not for the thread.

Fix direction: resolve without the default executor, either on a small dedicated executor with a bounded queue (refusing past it) or with an async resolver; bound outbound calls in flight per org in the API process.

Test: `test_a_slow_outbound_lookup_does_not_delay_another_orgs_ingest`. The default executor is sized 2, as in A15-2's test. A stubbed `getaddrinfo` takes 1 s for one name, and `RESOLVE_TIMEOUT_S` is 0.2 s. An ingest alone takes under 0.5 s. Two integration tests of that name both answer `ok: false` within 0.6 s, and the next ingest then took 0.81 s.

### A16-3 Password changes fill the hash queue and every login is refused (auth, availability)

SG-AP1.2 moved bcrypt to `_hash_pool` (2 threads) behind `_hashed`, which refuses with 429 once `PASSWORD_HASH_QUEUE` (16) hashes are running or waiting in the process. It limited `/login` per address and per email; register has its hourly bound, and accept-invite needs a live invite and hashes only before the single-use update. `POST /api/auth/password` has no limit: any signed-in member (every self-registered owner) verifies `current_password` through `_hashed` on every call, whether it is right or not. With 16 wrong-password changes in flight from one account, every login, register and accept-invite of every org in that process is answered 429 until they drain, and in-flight requests keep it that way. SG-AP1.2's Result lists the queue-full 429 on `/password` as a contract addition; the missing limit on that route is what makes the queue shared with logins.

Fix direction: limit `/password` per user (with `enforce_rate_limit`, as login is, and 429 `rate_limited`), and bound each user's or address's places in the hash queue so that no single caller can fill it.

Test: `test_password_changes_do_not_refuse_another_users_login`. A 0.2 s sleep stands in for bcrypt. Another user's wrong-password login alone answers 401. With 16 wrong-password changes from an admin in flight, the same login answered 429.

## Medium

### A16-4 The latency and cost evaluators read every version's traces

SG-AP1.4 made the `traces` runner match `Trace.agent_version == run.agent_version`, but `run_evaluation` still computes `p95_latency` and `avg_cost` (the `latency` and `cost` evaluators) from `select(Trace.latency_ms, Trace.cost_usd).where(Trace.agent_id == agent.id)` (the last 100 traces, of any version). While the old version still serves traffic, those are almost all its traces, so a run of the new version, and the prod gate on it, scores the old version's latency and cost. Fix: filter by `run.agent_version` (and the set's org). Test: `test_latency_evaluator_reads_the_runs_version` (five 2.0.0 traces at 10 s, 100 newer 1.0.0 traces at 100 ms; the 2.0.0 run scores 1.0).

### A16-5 One address's wrong logins lock an account out for everyone

`/login` counts `rl:login:email:<email>:<minute>` before the verify, over all addresses. Anyone who knows an email can spend its ten attempts in each minute from one address (the per-address bound allows exactly that), and the account's owner, from any address and with the right password, is answered 429. A signed-in user keeps working through refresh, but a new device, an expired session or a password-reset flow cannot sign in. SG-AP1.2 recorded this as "lets anyone lock an email's logins out for a minute at a time" (low). Because the attempts can repeat every minute, the lockout can be kept up indefinitely from one address, so it is re-ranked medium here. Fix: key the per-email bound on email and address (with a higher bound per email alone), or count only failures and let a right password through. Test: `test_wrong_logins_from_one_address_do_not_lock_the_owner_out` (ten wrong logins from 198.51.100.7, then the right password from 203.0.113.9: 429).

## Low

- **A16-6** `CaseIn.weight = Field(default=1.0, gt=0)` admits `inf` (FastAPI parses bodies with `json.loads`, which reads `Infinity`; pydantic floats allow inf) and finite weights such as `1e308` whose sum overflows. A failing case of weight `Infinity` makes `total_score` `0 * inf` = NaN, so the run's score is NaN. On Postgres the gate's `if not last or last.score < threshold` is false for NaN and the version is promoted. On SQLite NaN is stored as NULL and the gate raises `TypeError` (500). Repro: `CaseIn.model_validate(json.loads('{"input":"q","weight":Infinity}'))` is accepted, and `round(0*inf/inf, 3) < 0.8` is `False`. Fix: `allow_inf_nan=False` with an upper bound (say `le=1000`), and a gate that blocks a score that is not finite. A15-14 is not fully closed.
- **A16-7** `compute_metric_value` loads `select(Trace)` (whole rows, input and output included) for `error_rate`, `latency_*` and `token_usage` over `window_minutes`, which is unbounded, per rule, with rules per org unbounded and checked one after another in the tick. One org's large or many rules make the tick's check phase slow or large in memory, and with A16-1 that is where the tick dies. Aggregate in SQL and bound the window.
- **A16-8** Mutants of the SG-AP1 fixes that no test kills: see **Mutation probe**.

## Mutation probe

28 one-line mutants of the SG-AP1 fixes (`backend/scripts/mutants_audit16.py`, AUDIT-15's harness, which also copies the compose files because `test_alert_tick.py` reads them). Each runs against `tests/api/test_{alert_tick,auth_hardening,outbound_limits,promotion_gate,platform_security,platform_features}.py`, `tests/test_auth.py` and A15's unmarked tests.

- **Killed (25):** SG-AP1.1: stored dedup below 1 taken as is, pages not alternating by org, nothing cancelled at the deadline, `MAX_CHANNELS` not applied to stored rows, both API dedup bounds, a soft-limited run not marked failed, eval runs on the default queue. SG-AP1.2: no queue bound, unknown emails skipping the dummy verify, register hashing on the default executor, refresh's rowcount check, no per-email limit, the email key not case-folded. SG-AP1.3: site-local, IPv4-compatible, the cap one byte over, `Accept-Encoding: identity`, the caller's timeout ignored, `Content-Encoding` kept, the runner's `ReplyTooLarge`. SG-AP1.4: the gate's version filter, the `traces` runner's version filter, the run's version check, weight `ge=0`.
- **Survived (3)**, `python backend/scripts/mutants_audit16.py A16-8`:
  - `_dispatch_all`'s `Semaphore(DISPATCH_CONCURRENCY)` raised to 10^6: no test has more than 20 pages in one tick, so the bound on pages in flight (and so on outbound sends from the alert worker) is untested.
  - The per-case commit on the no-output path removed (`pass`): no test stops a run part way through after a failed case, so "a run stopped part way keeps the cases it scored" is tested only for scored cases.
  - The gate's source row taken without `order_by(created_at.desc())`: the test of promoting a version twice checks only that it is not a 500. Without the order (the oldest row on SQLite) `promoted_from` reads the first row's environment.

## Fixes re-checked

- **SG-AP1.1:** the tick checks every rule before paging, and pages by org turns, 20 at once, cancelled at 45 s; at most 10 channels per rule, and a rule's channels are sent at once; `dedup_minutes` of at least 1, with stored values below 1 treated as 30; eval runs on their own queue, results committed per case, and a run stopped at its soft limit marked failed. A15-1's slow-channel case holds. Not closed: a check that raises or a tick that dies before dispatch (A16-1).
- **SG-AP1.2:** bcrypt never runs on the default executor; login is limited per address and per email; unknown emails are verified against a cost-12 dummy hash; refresh is consumed by one `UPDATE`. A15-2's case holds. Not closed: `/password` fills the shared queue (A16-3), and the per-email bound locks accounts (A16-5).
- **SG-AP1.3:** replies are read up to 64 KiB as sent under one deadline; NAT64, IPv4-compatible and site-local addresses are judged as specified. A15-3 and A15-9 hold. Not closed: the lookup thread outlives the deadline (A16-2).
- **SG-AP1.4:** the gate reads the promoted version's latest completed run; the `traces` runner matches the version; a run's version must be the agent's; weights must be above 0. A15-4 holds for the runner. Not closed: the latency and cost evaluators (A16-4), and NaN scores (A16-6).

## Regression checks

- **Org scoping:** the routes SG-AP1 changed scope as before. `run_set` checks the version against an agent already looked up with the caller's org; `promote` looks up the agent with the caller's org before its run and version queries; alert rule create and update are unchanged apart from the bounds. No other router changed since AUDIT-15, whose route-by-route check stands.
- **Policy check path and ingest:** no code under `app/policy`, `services/trace_service.py`, `services/trace_policy_service.py` or `routers/{traces,trace_policies}.py` changed since AUDIT-15. Their tests pass in the full suite. Ingest's one new exposure is A16-2 (its body parsing shares the default executor with outbound lookups).

## Not findings

- `_dispatch_all` cancels sends still running at 45 s and `outbound.post` has its own 8 s deadline, so the tick's send phase is bounded. Turns by org put every org's first page in the first round.
- `refresh_tokens` checks the `UPDATE`'s rowcount and rolls back on 0, so two refreshes of one token give one session on both databases (READ COMMITTED re-checks the `WHERE`).
- `MAX_REPLY_BYTES` is checked after each chunk read, and httpcore reads at most 64 KiB at a time, so at most about 128 KiB is held.
- bcrypt's 72-byte truncation (register allows 128 characters) is the library's documented behaviour, not new.
