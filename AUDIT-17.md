# AUDIT-17: platform after SG-AP2 (Audit P3)

Audited at `e9f8d0a` (the default branch with SG-AP2.1 #71, SG-AP2.2 #72 and SG-AP2.3 #73 merged) by a session that wrote none of the code. Scope: everything merged since AUDIT-16 (`git log 6f316c0..e9f8d0a`): the alert tick's per-rule isolation, check deadline, dispatch in a `finally`, SQL aggregates and window bounds (SG-AP2.1); outbound lookups on their own executor, the queue bound, per-org call bounds through `outbound.org` and the callers that set it (SG-AP2.2); the `/password` limit, each caller's share of the hash queue and the email+address login key (SG-AP2.3); and regression checks of org scoping, the policy check path, ingest and the A16 findings still open. The SG-AP2 open lows and A16-4, A16-6 are known and are not re-reported, except where a finding here is a new consequence of one (each says so).

**Result: 0 critical, 2 high, 1 medium, 1 low.** The highs: the tick's check phase stops at 180 s and always starts again from the oldest rule, so one self-registered org with a few thousand rules over its own traces keeps every newer rule of every org from ever being checked (A17-1, the part of A16-1 that SG-AP2.1 did not close). Two self-registered orgs' lookups of a name that never answers hold all 16 resolver threads past their deadline, so every other org's outbound calls in that process fail as `unresolvable_host`, their policy-violation pages included (A17-2, SG-AP2.2's open low, re-ranked). They are PLAN.md sub-goals `SG-AP3.1` and `SG-AP3.2`.

Failing tests: `backend/tests/audit/test_audit_17.py`, one per finding above low, each `xfail(strict=True)`. Each fails today at its final assertion, after its control assertions pass, on SQLite and on Postgres 16 (`TEST_DB=postgres`). Each passes with `--runxfail` against a small sketch fix applied to the tree and then reverted (not committed): rules checked in turns by org; `RESOLVE_THREADS` raised to `RESOLVE_QUEUE`, so a lookup never waits behind a blocked thread; a call's org place given back once its lookup is done rather than when its reply is read. Run them with `cd backend && pytest tests/audit/test_audit_17.py -q --runxfail`.

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A17-1 | high | The check phase stops at `CHECK_DEADLINE_S` (180 s) and every tick starts again from the oldest rule. Rules per org are unbounded, so one org's rules that take 180 s to check keep every rule created after them, of every org, from ever being checked. On Postgres a `latency_p95` rule over 100k traces in a 30-day window checks in about 60 ms, so about 3 000 such rules are enough | SG-AP3.1 |
| A17-2 | high | A lookup's thread stays blocked until the system resolver gives up, and its place is not given back until then. Two orgs at `ORG_CALLS` (10) hold all 16 `RESOLVE_THREADS`, so every other org's lookups in that process wait past `RESOLVE_TIMEOUT_S` and fail as `unresolvable_host`, including policy-violation pages, integration tests and inline eval runs. Seven orgs fill `RESOLVE_QUEUE` (64), and then lookups are refused at once. Any self-registered member can do this through `/test`, which has no rate limit | SG-AP3.2 |
| A17-3 | medium | `ORG_CALLS` (10) equals `MAX_CHANNELS` (10) and counts whole calls, replies included. So a rule at the channel bound loses a page, logged as `too_many_calls` and not retried, whenever any other outbound call of its org is in flight in the process (an integration test, an inline eval run's runner call, another rule's sends) | none |
| A17-4 | low | A tick can now take 225 s against a 60 s schedule, so ticks overlap, and the tick's `already_firing` → `fire_alert` is not locked, so two overlapping ticks fire and page one rule twice | none |

## Method

- Read the SG-AP2 diff (`git diff 6f316c0..e9f8d0a -- backend/app`) line by line against each sub-goal's Problem, Acceptance, Result and open lows: `services/{alert_service,outbound,auth_service,violation_service,evaluation_service}.py`, `schemas/alert.py`, `routers/{auth,integrations}.py` and `config.py`. Also read the code each change reaches: `notification_service.py`, `tasks/alert_tasks.py`, `celery_app.py`, `routers/{alerts,traces,evaluations}.py` and both compose files.
- Measured the check cost on Postgres 16: one org, 100k traces spread over 30 days, a rule with `window_minutes` 43 200. Per check: `latency_p95` 61 ms, `error_rate` 33 ms, `token_usage` 31 ms. 2 000 rules over no traces took 1.8 s per tick (0.9 ms each).
- Ran each finding through the real app in process (the test app on SQLite and on Postgres 16, fakeredis). Outbound calls went through a stubbed `socket.getaddrinfo` (a name that does not answer holds its resolver thread) and a mock transport.
- Turned each finding above low into a failing test, and checked that each passes against a sketch fix.
- Mutation probe: 37 one-line mutants of the SG-AP2 fixes, replayed with `python backend/scripts/mutants_audit17.py` (see **Mutation probe**).
- Ran the full backend suite on SQLite (`--ignore=tests/api/test_postgres.py`): 1627 passed, 12 xfailed (AUDIT-17's 3 included). `tests/audit/test_audit_17.py` on Postgres 16 (`TEST_DB=postgres`): 3 xfailed, and with `--runxfail` each fails at its final assertion, as on SQLite. Run five times on SQLite, all 3 xfailed each time.

## High

### A17-1 One org's rules keep every newer rule of every org from being checked (alerts, availability)

`_check_all` loads every enabled metric rule `order_by(AlertRule.created_at)` and checks them one after another. SG-AP2.1 put the loop under `asyncio.wait_for(..., CHECK_DEADLINE_S)` (180 s) and sends the pages fired so far in a `finally`, so a tick that runs long no longer loses its pages. But the rules after the stop point are not checked in that tick. The next tick starts again from the oldest rule and stops at the same place, so those rules are never checked. Nothing records this: the summary counts the rules checked, and the warning names only the count.

Rules per org are unbounded (`POST /api/alerts/rules` has no cap and no rate limit; a developer of any self-registered org can call it), and since SG-AP2.1 `window_minutes` can be 43 200. Measured on Postgres 16, a `latency_p95` check over 100k traces in that window takes about 61 ms (count, then a sort for the `OFFSET`). Bulk ingest at the default 600 requests a minute stores 100k traces in a few requests. So about 3 000 rules (or about 200 000 cheap ones over no traces) fill the 180 s. From then on, no rule created after them, in any org, is checked: new customers' rules and existing customers' new or recreated rules never fire. Rules older than the attacker's are still checked.

This is the "later rules are never checked" half of A16-1. SG-AP2.1 closed the dropped pages but not this half; SG-AP1.1 and SG-AP2.1 each recorded "rules per org are unbounded and checked serially" as an open low. The new check deadline makes the cut-off the same every tick.

Fix direction: check rules in turns by org, as `_dispatch_all` already sends pages, so each org's first rules are checked in the first round; or carry the stop point over to the next tick. Bound rules per org (a 4xx past the bound is a contract addition), and give each org a share of the check phase.

Test: `test_one_orgs_many_rules_do_not_keep_another_orgs_rule_unchecked`. The deadline is 0.3 s. Org A creates one rule through the API (200, no cap) and has 3 000 older stored rules that never fire. Org B's newer rule fires on `token_usage >= 0` with a webhook. Each of three ticks stops at its deadline, and B's webhook is never called.

### A17-2 Two orgs' slow lookups fail every other org's outbound calls (outbound, alerts, availability)

SG-AP2.2 moved lookups to `_resolver` (`RESOLVE_THREADS` = 16) with a queue of `RESOLVE_QUEUE` = 64. It bounds each org to `ORG_CALLS` = 10 calls in flight, each held "until its lookup's thread has ended". That keeps ingest off the resolver (A16-2 holds). But a thread blocked in `getaddrinfo` cannot be stopped. It holds its place until the system resolver gives up, which with glibc's defaults is 10 s per name server or more. Meanwhile any lookup that waits for a thread is abandoned at `RESOLVE_TIMEOUT_S` (5 s) and fails as `unresolvable_host`.

- Two orgs, each with 10 lookups of a name whose name server does not answer, hold all 16 threads. Every other org's lookup in that process then waits and fails. One address may register five orgs an hour.
- Seven orgs (70 places) fill the 64-place queue. An abandoned lookup that is still waiting gives its place back when it is cancelled, but each org can refill its 10 places at once. Other orgs' lookups are then refused without waiting.

The cheapest way in is `POST /api/integrations/{id}/test`. Any member can call it for a URL their admin set, it has no rate limit, and it answers at the lookup's deadline while the thread stays blocked. Policy-violation alert channels in the API process (and in the policy worker) do the same. In the alert worker, the tick's calls are not counted per org, so any orgs whose pages come before another org's in the tick's turn order (rules created earlier) hold the threads the same way.

What fails for every other org in that process: Slack and webhook pages of `policy_violations` rules (logged, not retried, and dedup then holds the rule for its window), integration tests, and inline eval runs, whose webhook runner cases fail with `runner_error: unresolvable_host` and score 0. SG-AP2.2 recorded this as an open low ("ten orgs' slow names (or two orgs at 10 each …) can still keep every lookup waiting until `RESOLVE_TIMEOUT_S` and then `unresolvable_host`, never ingest"). It is re-ranked high here because what fails is other orgs' alert pages, the impact that made A15-1 and A16-1 high, and because it takes two self-registered orgs and a domain.

Fix direction: lookups that do not hold a shared thread past their deadline, such as an async resolver whose timeout ends the query, or a thread per lookup with `RESOLVE_THREADS` ≥ `RESOLVE_QUEUE` so that no lookup waits behind a blocked one. Also a small per-org bound on lookups still running past their deadline, so the number of orgs needed to fill the queue is out of reach of registrations (with 2, that is 32 orgs); and a rate limit on `/test`.

Test: `test_two_orgs_slow_lookups_do_not_fail_another_orgs_alert_page`. `RESOLVE_TIMEOUT_S` is 0.2 s. Orgs X and Y each run 10 integration tests of `slow-dns.example`. All 20 answer `unresolvable_host`, and their threads stay held. The victim org's trace then violates an `alert` policy whose rule has a webhook channel, and the page is never sent (`unresolvable_host`).

## Medium

### A17-3 A rule at the channel bound loses a page while another call of its org runs

`ORG_CALLS` = 10 is `MAX_CHANNELS` = 10 ("a rule's channels fit"), but the bound counts whole calls, held until the reply is read (up to `TIMEOUT_S`, 8 s) and the lookup's thread has ended. `dispatch_alert` sends a rule's channels at once, so while any other outbound call of the org is in flight in the same process, a rule with 10 Slack or webhook channels has one or more of them refused as `too_many_calls`. The send is logged and never retried. The other call can be an integration test run by any member, an inline eval run's runner call (one at a time for the whole run), or a second rule firing on the same violation. No attacker is needed: two rules of six channels each on one violation lose two pages. Before SG-AP2.2 every channel was sent. SG-AP2.2 recorded "a burst of violation alerts with more than 10 channels in flight fails the excess sends" as an open low. It is re-ranked medium here because one rule within the allowed bound loses a page whenever one other call is in flight, and a viewer can keep that going through `/test`.

Fix direction: count only the lookup (the resource the bound protects), not the reply. Or let alert sends wait for a place under their deadline instead of being refused, with `ORG_CALLS` above `MAX_CHANNELS`. Test: `test_a_rule_at_the_channel_bound_is_paged_while_an_integration_test_runs`: an integration test to a receiver that answers slowly is in flight, a violation fires a rule with 10 webhook channels, and 9 are paged.

## Low

- **A17-4** SG-AP2.1's check deadline (180 s) plus the dispatch deadline (45 s) lets one tick run 225 s, and the beat schedule is 60 s. The worker (`-Q celery --beat`, prefork, one process per CPU) runs overdue ticks at once. Unlike the violation path, which locks the rule row `FOR UPDATE` and checks again, the tick's `already_firing` → `fire_alert` takes no lock, so two ticks that overlap both fire and page a rule. Before SG-AP2.1 a slow tick could also overlap (up to its 300 s limit). The deadline does not create the overlap, but under A17-1's load it is the normal case. Fix: a Redis lock per tick or per rule (as A15-18's scheduler needs too).

## Mutation probe

37 one-line mutants of the SG-AP2 fixes (`backend/scripts/mutants_audit17.py`, AUDIT-16's harness). Each runs against `tests/api/test_{alert_tick,auth_hardening,outbound_lookups,outbound_limits,violation_pipeline,platform_security,platform_features}.py`, `tests/test_auth.py` and AUDIT-16's tests (not A16-4's xfail).

- **Killed (37 of 37):**
  - SG-AP2.1: the per-rule `except` narrowed, the rollback, `expunge_all`, the `finally` turned into `else`, no check deadline, the deadline at 300 s, the stored-window bound, both API window bounds, the org and agent filters of the aggregates, `error_rate`'s filter, the percentile's position, `LIMIT 1`, the order reversed, `avg` as `max`, `token_usage` without output tokens.
  - SG-AP2.2: lookups on the default executor, no queue bound, `>` for the org bound, a lookup not holding its call's place, no done callback, no org counted, and each of the three callers' `org.set` removed.
  - SG-AP2.3: no `/password` limit, no per-caller check, `>` for it, a place never given back, each of the five `_hashed` callers without `caller`, the email key without the address, and `PASSWORD_HASH_PER_CALLER` at 16.
- Two kills are weaker than the rest. In the harness run, `PASSWORD_HASH_PER_CALLER` at 16 was killed only by the 900 s hang guard; run alone, `test_one_users_password_changes_hold_a_bounded_share_of_the_queue` fails on it at once. Three alert-service rows (the rollback, `expunge_all`, `finally`) are reported with a log line as the killer, because the harness prints the first line that starts with `ERROR`. Each run still failed, so they count as killed.
- No surviving mutants, so there is no low finding for them this round.

## Fixes re-checked

- **SG-AP2.1:** a rule whose check raises is logged and skipped, with a rollback, and the rules were detached first, so later rules are still checked; pages fired before an error or the deadline are sent from the `finally`; `window_minutes` is 1 to 43 200 on create and update (422), and stored values outside that range, or null, are checked as 15; `error_rate`, `latency_avg` and `token_usage` are one aggregate each, `latency_p95` matches numpy's linear percentile from two rows, and no `Trace` row is loaded. A16-1's test passes and A16-7 holds. Not closed: rules after the deadline are never checked (A17-1).
- **SG-AP2.2:** `getaddrinfo` runs on `outbound-resolve` threads, never on the default executor; the queue counts abandoned lookups until their thread ends and refuses past 64; the org's places are taken and given back under a lock from both the loop and the resolver threads, on every path (blocked, invalid URL, too large, timeout, transport error, a lookup cancelled before it ran). A16-2's ingest case holds. The callers that run in the API process (the integration test, `run_evaluation`, `evaluate_violation_alerts`) set `outbound.org` before their sends, and the channel tasks inherit it. Not closed: blocked threads deny other orgs' lookups (A17-2), and the per-org bound costs an org its own pages (A17-3).
- **SG-AP2.3:** `/password` is limited to 5 a minute per user before the verify; each address or user holds at most 4 of the 16 hash places, and every `_hashed` caller passes one (login, register and accept-invite by address, both of `/password`'s hashes by user); the per-email login key includes the address. A16-3's and A16-5's tests pass. Filling the queue for a moment now takes four addresses, and keeping it full (bcrypt drains about 400 a minute per process) takes about 40, each held to 10 logins a minute.

## Regression checks

- **Org scoping:** only `routers/{auth,integrations}.py` changed among the routers. `/test` still looks up the integration with the caller's org before it sets `outbound.org` to that org; `/password` acts on the caller only. AUDIT-15's route-by-route check stands (including that alert rules may name another org's `agent_id` without a leak).
- **Policy check path and ingest:** nothing under `app/policy`, `services/trace_service.py`, `services/trace_policy_service.py` or `routers/{traces,trace_policies}.py` changed since AUDIT-16. `violation_service` gained only `outbound.org.set`. Their tests pass in the full suite. Ingest body parsing no longer shares an executor with outbound lookups (A16-2 holds).
- **A16 findings still open:** A16-4 (medium; the `latency` and `cost` evaluators read the agent's last 100 traces of any version) is unchanged, and `test_latency_evaluator_reads_the_runs_version` still fails at its final assertion. A16-6 (Infinity case weights) is unchanged. Of A16-8's survivors, the dispatch semaphore is now killed by `test_alert_tick.py`.

## Not findings

- `_check_all`'s `db.expunge_all()` before the loop means a rollback does not expire the rules still to check. `fire_alert` and the page text use only the detached rule's loaded columns (`expire_on_commit=False` in the tick's session).
- `latency_p95` reads the count, then the rows at `OFFSET int(pos)`. A trace stored between the two queries shifts the value by at most one row (SG-AP2.1's open low). If retention deletes rows in that gap, the `OFFSET` can go past the end, and the rule is skipped for that tick by the per-rule `try`.
- `_resolve` adds its done callback after `submit`, with no `await` in between, so a lookup that ends or is cancelled before it ran still frees its place. `wait_for` in `_pin` copies the context, so `_call` reaches the lookup.
- `_hashed` checks and takes a caller's place without an `await` in between, so concurrent requests of one caller cannot pass the share together.
- The backend in `docker-compose.prod.yml` listens on port 8000 directly, not behind the frontend's nginx, so `request.client.host` is the client's address and the per-address keys are per client.
