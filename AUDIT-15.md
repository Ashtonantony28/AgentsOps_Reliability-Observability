# AUDIT-15: platform (Audit P1)

Audited at `94b6720` (the default branch with SG-P0 #60, SG-P1 #61 and SG-P2 #64 merged) by a session that wrote none of the platform code. Scope, from PLAN.md's **Audit P1** entry: auth and tenancy (every route's org scoping, roles, invites, refresh, password change, `/ws/traces`), outbound requests (`services/outbound.py` and its callers), signed webhooks and Slack OAuth, the evaluation runner, the cron scheduler, alerts and incidents, retention, and tests that cannot fail. Known and accepted gaps (SG-P0's, SG-P1's and SG-P2's open lows and approvals, the AUDIT-1 to AUDIT-13 results in PLAN.md) are not re-reported; where a finding is a new consequence of one, it says so.

**Result: 0 critical, 4 high, 6 medium, 8 low.** The highs: one org's alert channels hold up every later org's periodic alerts (A15-1); wrong-password logins, unlimited and unauthenticated, run bcrypt on the thread pool that every org's ingest parsing and `/check` stepping share (A15-2); outbound requests read the whole reply, so a configured URL makes the API process (or the alert worker) buffer any amount (A15-3); and the prod promotion gate passes a version that was never evaluated (A15-4). They are PLAN.md sub-goals `SG-AP1.1` to `SG-AP1.4`.

Failing tests: `backend/tests/audit/test_audit_15.py`, one per finding above low, each `xfail(strict=True)`. Each fails today at its final assertion, after its control assertions pass (SQLite; Postgres was not run, its test database being shared), and will XPASS, failing CI, once fixed, so the fixing sub-goal removes the marker. Run them with `cd backend && pytest tests/audit/test_audit_15.py -q --runxfail`.

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A15-1 | high | Periodic alert rules of every org are checked in one serial loop that awaits each firing rule's channels before the next rule: one org's slow or many channels (or a negative `dedup_minutes`, which never dedups) delay or, past the 300 s task limit, stop the alerts of every org whose rules sort after it | SG-AP1.1 |
| A15-2 | high | `/api/auth/login` is unlimited and each attempt on a registered email (the attacker's own) runs bcrypt on asyncio's default executor, the pool that ingest body parsing, live monitor checks and `/check` stepping share: 8 failed logins put another org's ingest 2 s behind | SG-AP1.2 |
| A15-3 | high | `outbound.post` reads the whole reply into memory with only per-read timeouts: an integration's test (any self-registered admin, in the API process), an alert channel (the alert worker) or a webhook runner buffers whatever the URL sends; 64 MiB read in the test | SG-AP1.3 |
| A15-4 | high | The prod promotion gate reads the agent's latest completed run of any version, and a `traces` run scores traces of any version: a new version is promoted to prod, without `force`, on an older version's score | SG-AP1.4 |
| A15-5 | medium | The Slack OAuth callback needs only the state, not the starting admin: whoever completes an admin's authorize URL lands their Slack workspace's webhook in that admin's org (the contract table lists the callback as "jwt admin; state") | none |
| A15-6 | medium | `/ws/traces` checks its token only at connect: a feed stays open past the token's expiry, a password change and the revocation of every session, for as long as the membership lasts | none |
| A15-7 | medium | Login signs in to one arbitrary membership (`LIMIT 1`, no order) and nothing switches org, so an existing account an invite `added` can never act in that org | none |
| A15-8 | medium | A set's `schedule_cron` cannot be removed (PATCH drops nulls) and no route deletes a set, so a scheduled set runs, and calls its webhook runner, for good | none |
| A15-9 | medium | `is_public` accepts NAT64 `64:ff9b::/96`: on a NAT64 deployment a host answering `64:ff9b::a9fe:a9fe` reaches 169.254.169.254 | none |
| A15-10 | medium | Evaluation runs share the default Celery queue with the alert tick, unbounded per org; a run killed by `task_time_limit` (5 webhook cases at 60 s) stays `running` with no results | none |
| A15-11 | low | Refresh is not single-use under concurrency (3 concurrent refreshes of one token: 3 sessions); login's timing tells registered emails apart (3 ms vs 296 ms) | none |
| A15-12 | low | Surviving mutants of the platform fixes (see **Mutation probe**) | none |
| A15-13 | low | Viewers read Slack incoming-webhook URLs and alert-channel targets in clear (redaction is by key name), and any viewer can run an integration's test, which makes outbound requests | none |
| A15-14 | low | Eval case `weight` is unbounded: a negative weight lowers the denominator, so a failing case can raise a run's score (to 2.0 with weights 1 and -0.5); a weight of 0 counts as 1 | none |
| A15-15 | low | Retention deletes traces only: the `traces` runner's copies of trace outputs (`evaluation_results.actual_output`), webhook payloads in the audit log and incident narratives (sample error messages) outlive `retention_days` | none |
| A15-16 | low | The webhook receiver HMACs the body only when the org has an enabled integration of the kind, so the 401's timing tells which orgs and kinds exist, which the contract says it does not | none |
| A15-17 | low | Pending invites cannot be revoked (no route; an admin removed since keeps theirs valid for 72 h), and an invite to `Bob@x` where `bob@x` has an account makes a second account (the local part keeps its case) | none |
| A15-18 | low | Two scheduler ticks that overlap (two beat instances, a slow worker) both start a due run: the due check and the insert are not locked | none |

## Method

- Read every router and service on the platform paths against `docs/platform-contracts.md`, PLAN.md's Platform entries and their approvals: `middleware/auth.py`, `routers/{auth,integrations,ws,evaluations,alerts,incidents,agents,governance,reports,traces}.py`, `services/{auth_service,outbound,notification_service,alert_service,evaluation_service,cron}.py`, `tasks/{alert_tasks,evaluation_tasks,retention_tasks}.py`, `celery_app.py`. Every route's queries checked for an `org_id` filter or a lookup through an org-scoped parent; every role dependency compared across routers.
- Drove each finding through the real app in process (the test app, SQLite, fakeredis), outbound requests through `outbound`'s mock transport with a public resolution, as the SG-P1 tests do.
- Turned each finding above low into a failing test.
- Mutation probe: 37 one-line mutants of the SG-P1 and SG-P2 fixes, replayed with `python backend/scripts/mutants_audit15.py` (see **Mutation probe**).
- Ran the full backend suite on SQLite: 1473 passed, 1 skipped, 14 xfailed (AUDIT-15's 10 and 4 before), and 3 errors, `tests/api/test_postgres.py`, which needs a Postgres server (none was running, and the shared test database was left alone).

## High

### A15-1 One org's alert channels hold up every org's periodic alerts (alerts, availability)

`alert_service.evaluate_metric_rules` (the beat task every minute, SG-P2) loads every enabled metric rule of every org ordered by `created_at` and, for each, computes the value, fires, and then awaits `dispatch_alert(rule.channels, ...)` before going on to the next rule. `dispatch_alert` sends the channels one after another, each through `outbound.post` with an 8 s timeout per connect and per read (A15-3: a reply trickled one byte every 7 s never times out). Any registered user is an owner of their own org and so can create rules (developer and up):

- a rule that always fires (`token_usage >= 0`) with many webhook channels to an address that never answers costs 8 s per channel; 40 of them are past the task's 300 s `task_time_limit`, so the task is killed every tick before it reaches any rule created after the attacker's;
- `dedup_minutes` is unvalidated: 0 becomes 30, but a negative value puts the dedup window in the future, so the rule fires (and pages) every minute instead of once per 30 minutes;
- one channel that trickles its reply holds the loop until the task limit.

Every org whose rules were created after the attacker's then gets its error-rate, latency, cost and token alerts late or never. The violation path is not affected (it dispatches in a background task). A15-10 is a second way to the same effect (the eval runs share the queue).

Fix direction: evaluate every rule first and dispatch afterwards, concurrently and under one bound for the whole tick (or per org), with a total deadline per request (A15-3); bound channels per rule and `dedup_minutes` ≥ 1; or run each org's rules as its own task.

Test: `test_a_slow_alert_channel_of_one_org_does_not_hold_up_another_orgs_alert` (org A's older rule pages a webhook that answers when released; org B's rule, no channel, has not fired 0.3 s into A's page; both fire once it returns).

### A15-2 Unlimited logins run bcrypt on the pool every org's ingest and checks use (auth, availability)

SG-A13.2 moved bcrypt off the event loop with `asyncio.to_thread`, and moved the parsing of authenticated ingest and `/check` bodies to `asyncio.to_thread` as well (`ApiKeyRoute`); the live monitor check of every streamed event (`trace_service.py:480`), monitor restores and snapshots and `/check`'s prefix stepping (`trace_policy_service.py:385`, `:402`) run there too. That is one pool for the whole process: asyncio's default executor, `min(32, cpus + 4)` threads. `/api/auth/login` has no rate limit (an accepted low of SG-A13.3, recorded as a guessing concern), and every attempt on a registered email costs one bcrypt verify (about 0.3 s of a thread here) whether the password is right or not. An attacker needs no victim's email: they register an account (5 per address per hour) and log in to it with a wrong password, about `threads / 0.3 s` times a second (40 a second for 8 cores) to keep the pool full. Every org's ingest then waits for a thread before its body is even parsed, and every `/check` waits for its parse and its stepping, past the SDK's 2 s check timeout (fail open or, with `fail_closed`, every guarded call blocked). Logins of unknown emails return in 3 ms, which also tells registered emails apart (A15-11).

Fix direction: hash and verify passwords on a small dedicated executor with a bounded queue (503 or 429 when full), never on the default one; add a per-address and per-email login limit (a contract addition: 429 `rate_limited`, as registration has); verify against a dummy hash for unknown emails.

Test: `test_failed_logins_do_not_delay_another_orgs_ingest` (verify stands in for bcrypt with 0.5 s; the default executor sized 2; an ingest alone under 0.5 s; with 8 wrong-password logins in flight, the same ingest took about 2 s).

### A15-3 Outbound replies are read whole (outbound, availability)

`outbound.post` (SG-P1) calls `httpx.AsyncClient.post`, which reads the entire reply body into memory, and the `timeout` is httpx's per-operation timeout (connect, and each read), not a deadline for the request. None of the callers looks at more than the status, except the webhook runner, which reads a small JSON object:

- `POST /api/integrations/{id}/test` (any member; admins set the URL) runs in the API process: the URL's server sends gigabytes and the worker buffers them all, or trickles bytes so the request never ends; the integration's admin is anyone who registered an org.
- Alert channels (`send_slack`, `send_webhook`) run in the alert worker (and, for violations, in the API process's background task).
- The webhook runner is bounded in time (`wait_for(timeout_s)`, 60 s at most) but not in size: at 100 MB/s that is up to 6 GB per case.

Fix direction: stream the reply (`client.stream`) and read at most a small cap (64 KiB; the runner's JSON included), refusing past it; wrap every call in a total deadline; release the connection.

Test: `test_connection_test_does_not_buffer_an_unbounded_response` (an integration test against a URL that sends 64 x 1 MiB chunks: all 64 MiB were read; a small reply is fine).

### A15-4 The prod promotion gate is version-blind (evaluations, correctness)

The README promises "dev→staging→prod promotion (gated on eval score)", and SG-P2 made a case without output fail so that the gate means something. But `POST /api/agents/{id}/promote` checks `last completed EvaluationRun of the agent`, whatever its `agent_version`, against the agent's threshold, and promotes `req.version`. Creating a version (`POST /api/agents/{id}/versions`) does not evaluate it, so the ordinary flow (evaluate 1.0.0, create 2.0.0, promote 2.0.0) passes the gate on 1.0.0's score. A version whose own run failed passes too once any later run of another version scores well. And a run's version is only a label for the `traces` runner: `_trace_output` takes the newest matching trace of the agent of any `agent_version`, so a run "of 2.0.0" can score 1.0.0's outputs; `EvalRunRequest.agent_version` is also free text, not checked against the agent's versions.

Fix direction: the gate takes the latest completed run whose `agent_version` is the promoted version (and whose set has a runner); the `traces` runner matches `Trace.agent_version == run.agent_version`; a run's `agent_version` must be one of the agent's versions.

Test: `test_promotion_gate_needs_a_passing_run_of_the_promoted_version` (no run: 400 `eval_gate_blocked`; a webhook-runner run of 1.0.0 scores 1.0; 2.0.0 created and promoted to prod: 200).

## Medium

### A15-5 The Slack callback is not bound to the admin who started it

`/oauth/slack/callback` is unauthenticated and checks only that the state is live, unused and that its starting user is still an admin. The state proves who *started* the flow, not who completes it, so the authorize URL is a bearer token for 10 minutes: an admin of org A (any self-registered user) starts the flow, sends the Slack authorize URL to an admin of a Slack workspace W ("connect AgentOps"), and when W's admin approves, the callback stores W's incoming webhook in org A. Org A can then post into W's channel as the app (phishing from a trusted bot). The interface contract table lists the callback as "jwt admin; state"; the code has no JWT. Fix: the callback page (frontend) relays `code` and `state` with the starting admin's token, and the API requires the token's user to be the state's user; or bind the state to a cookie set at start. Test: `test_slack_callback_is_completed_only_by_the_admin_who_started_it`.

### A15-6 `/ws/traces` outlives its access token

The feed decodes the token once, at connect; the heartbeat re-checks the membership only. A feed opened with a stolen access token keeps streaming every trace and violation of the org after the token expires, after the user changes their password and after every session is revoked, for as long as the user stays a member (there is no route to remove a member, so in practice for good). SG-P1's accepted low is about an access token staying valid until it expires (60 min); here it never does. Fix: the heartbeat also closes 4401 once the token's `exp` passes (and, if wanted, when the user's sessions were revoked after `iat`). Test: `test_ws_feed_closes_once_its_token_expires` (token valid 2 s; pings still arrive after 3 s).

### A15-7 An account added to a second org can never sign in to it

`POST /api/auth/invite` of an email that has an account adds a membership (`added`), as SG-P1 froze. But `auth_service.login` signs in to `select(OrgMembership).where(user).limit(1)` with no order (on SQLite the oldest row; on Postgres whatever the plan returns), refresh keeps the session's org, and no route issues a token for another org (`/me` lists the orgs; the frontend has no switcher). The added user sees the org in Settings and can never act in it; on Postgres they may instead land in it and lose their own. Fix: an org choice at login and a switch route (`POST /api/auth/switch-org {org_id}` → `TokenPair` for a current membership), a contract addition; or deterministic order plus switch. The test pins one shape (`org_id` in the login body); the fixing sub-goal may rewrite it for the shape approved. Test: `test_an_added_account_can_sign_in_to_the_org_it_was_added_to`.

### A15-8 A set's schedule cannot be removed

`PATCH /api/evaluations/sets/{id}` applies `req.model_dump(exclude_none=True, exclude={"runner"})`, so `{"schedule_cron": null}` is dropped (the runner got an explicit-null path in SG-P0; the schedule did not), and there is no `DELETE /sets/{id}`. Once scheduled, a set runs at its cron for good (and a webhook runner is called per case each time); the only way out is a cron that never matches (`0 0 31 2 *`). Fix: treat an explicit null like the runner's, and add a delete route. Test: `test_a_sets_schedule_can_be_removed`.

### A15-9 NAT64 addresses pass the public-address check

`outbound.is_public` unwraps IPv4-mapped addresses and refuses non-global ones, but Python's `is_global` is true for `64:ff9b::/96`, the well-known NAT64 prefix, which on a NAT64 network (IPv6-only clusters with DNS64) is translated to the embedded IPv4 address: an attacker's DNS answering `AAAA 64:ff9b::a9fe:a9fe` reaches the metadata service at 169.254.169.254, and `64:ff9b::a00:1` reaches 10.0.0.1. `::127.0.0.1` (IPv4-compatible) and `fec0::/10` (site-local) are also "global" to Python, and are refused by most stacks, but not by contract. Fix: unwrap `64:ff9b::/96` (and `64:ff9b:1::/48`) like mapped addresses and check the IPv4 inside; refuse IPv4-compatible and site-local. Test: `test_outbound_refuses_nat64_addresses_of_private_ipv4`.

### A15-10 Evaluation runs share the alert queue, and a killed run stays running

`execute_run` (each scheduled run) is routed to the default queue, which the alert tick also uses (only policy tasks have their own, "so a backlog of them never delays alerting"). A run makes one webhook call per case, each up to `timeout_s` (60 s), with no bound on cases, sets or schedules per org, so one org's `* * * * *` sets keep the default workers busy and the alert tick waits behind them (the second road to A15-1). Past `task_time_limit` (300 s, five 60 s cases) the worker is killed; results are committed only at the end, so the run stays `running` for good with no results (and the next scheduled run starts anyway). Fix: an `evaluations` queue; commit each case's result as it is scored; mark runs left `running` past the time limit as failed; per-org bounds on scheduled sets. Test: `test_evaluation_runs_do_not_share_the_alert_queue`.

## Low

- **A15-11** `refresh_tokens` reads the session, then sets `revoked_at` in Python: three concurrent `/refresh` calls with one token all answered 200, leaving three sessions (an atomic `UPDATE … WHERE revoked_at IS NULL` like `accept_invite`'s would make it single use). Login for an unknown email skips bcrypt: 3 ms against 296 ms for a registered one, so emails can be enumerated (verify against a dummy hash).
- **A15-12** Mutants of the platform fixes the platform tests do not kill: see **Mutation probe**.
- **A15-13** `GET /api/integrations` redacts config keys containing `key`, `secret` or `token`; a Slack `webhook_url` (itself the credential to post into the channel) and webhook `url`s are shown to viewers, as are alert channel targets in `GET /api/alerts/rules`. `POST /api/integrations/{id}/test` is open to viewers and makes the outbound request (and, with A15-3, the buffering).
- **A15-14** `CaseIn.weight` is any float. `run_evaluation` adds every case's weight to the denominator and only scored cases' to the numerator, so with a passing case of weight 1 and a failing one of -0.5 the run scores 2.0 (and passes the gate); `weight or 1.0` makes 0 count as 1. Bound it to `> 0`.
- **A15-15** `purge_expired_traces` deletes traces (spans and violations cascade), but the `traces` runner copies trace outputs into `evaluation_results.actual_output`, the webhook receiver stores payloads in `audit_logs.details`, and incident narratives quote a trace's error message; all are kept past `retention_days`.
- **A15-16** `receive_webhook` computes an HMAC per enabled integration of `kind` in `org_id` (up to 16 MiB each, SG-P1's open low) and none when there is none, so the time to the 401 says whether the org exists and has such an integration; the contract says "the answer does not say which orgs exist". Compute one HMAC with a fixed dummy key when no row matches.
- **A15-17** There is no route to revoke a pending invite (re-inviting replaces it, nothing deletes it), so an invite made by an admin who has since been demoted stays valid for its 72 h. `EmailStr` keeps the local part's case and `users.email` is compared exactly, so an invite to `Bob@example.com` where `bob@example.com` has an account is `pending` and its acceptance makes a second account.
- **A15-18** `schedule_due_runs` reads each set's last scheduled run and inserts the next without a lock or unique key, so two ticks that overlap (two beat instances, or a backed-up worker running two ticks at once) start the same due run twice.

## Mutation probe

37 one-line mutants of the SG-P1 and SG-P2 fixes (`backend/scripts/mutants_audit15.py`, AUDIT-4's harness, copying `README.md` and `docs/` too since the platform tests read them), each applied to a temporary copy of the repo and run against the platform tests (`tests/api/test_platform_{security,features,contracts}.py`, `tests/audit/test_audit_6.py`, `tests/test_auth.py`, `tests/test_security.py`, `tests/test_alert_compare.py`).

- **Killed (29)**: SG-P1: an admin changing an owner's role by invite, an old invite token kept on re-invite, an expired invite accepted, an accepted invite's role ignored, refresh tokens without a `jti`, the receiver ignoring `enabled` or the org, the audit row without the integration id, an expired Slack state taken, a Slack starter's `is_active` ignored, expired states never deleted, multicast taken as public, `all` resolved addresses read as `any`, no SNI, no `Host`, the feed's membership re-check dropped, an inactive user's feed kept. SG-P2: an empty output passing, the `traces` runner's lookback and exact match dropped, the webhook runner's total timeout dropped, the dom/dow OR rule, the cron lookback ignoring the last run, `any_weekday` always true, a scheduled run without an agent, `cost_per_day` ignoring the rule's agent, critical rules without incidents, disabled rules evaluated, retention ignoring the org.
- **Survived (6)**, `python backend/scripts/mutants_audit15.py A15-12` (all six, and both equivalents, also survive the whole backend suite, `--all`, `test_postgres.py` left out):
  - `accept_invite` taking an invite whose single-use `UPDATE` changed no row (`rowcount < 0`): no test accepts one token twice at once.
  - `change_password` revoking every user's sessions, not just the caller's: the test has one user.
  - `refresh_tokens` not revoking the session it refreshes, so a refresh token can be used again and again: no test refreshes the same token twice (SG-P1 item 7's tests check the org and role only).
  - `outbound` with `trust_env=True`: no test sets a proxy in the environment (the tests swap the transport), so SG-P1's "no env proxy" (a proxy would resolve the name again) is untested.
  - `cost_per_day` without `Trace.org_id == rule.org_id`: a rule without an agent then sums every org's cost; the test has one org.
  - Retention taking negative `retention_days` (`if days:`), which deletes every trace of the org (the cutoff lies in the future): no test has a negative value, though the docstring promises "0 or less keeps them".
- **Equivalent (2)**: refresh without the session's `expires_at` check (the token's own `exp`, checked by `decode_token`, is the same instant); the `traces` runner without `Trace.org_id` (ingest refuses another org's agent, so a trace of the run's agent is always in the set's org).

## Platform fixes re-checked

- **SG-P1 (1) invites:** no password is set or returned; the token is 256-bit, stored as SHA-256, 72 h, single use by one `UPDATE`; roles limited; an admin cannot change an owner's role. Holds, except A15-7 (an `added` account cannot use the org) and A15-17.
- **(2) signed webhooks:** the signature is checked before parsing, in constant time per secret, against enabled integrations of the kind in the org; every failure is the same 401. Holds, except the timing side (A15-16).
- **(3) Slack state:** a 10-minute single-use DB nonce bound to the starter's org and still-admin user. Holds for forgery and replay; not bound to who completes it (A15-5).
- **(4) outbound:** http(s) only, every resolved address checked, the request pinned to the checked address with the name's Host and SNI, no redirects, no env proxy. Holds for rebinding and redirects; NAT64 (A15-9) and reply size and duration (A15-3) are open.
- **(5) `/ws/traces`:** an active member's access token at connect, membership re-checked by the heartbeat. Holds, except token expiry (A15-6).
- **(6) secrets:** refuses to start outside development with the default or example secrets. Holds.
- **(7) refresh:** keeps the session's org with the current role; 401 `no_org` once the membership is gone. Holds, except concurrent use (A15-11).
- **SG-P2:** a case without output fails, the runner's errors fail the case, the `traces` runner stays in the org and the agent; cron parsing, the dom/dow rule and the day's lookback; `cost_per_day` is one 24 h sum; periodic alerts go through `fire_alert` and open incidents for critical rules; retention deletes per org and keeps traces for `retention_days` ≤ 0. Holds, except A15-4 (versions), A15-8, A15-10, A15-14, A15-15 and A15-18.

## Not findings

- Org scoping: every route under `/api` filters by the caller's org or looks rows up through an org-scoped parent (eval runs through their set, versions through their agent, violations through their trace, integration ids with `org_id`); `run_set` refuses another org's agent, so a run's agent (and the scheduler's "agent of the latest run") is always the set's org's. Alert rules may name another org's `agent_id`, but every query also filters by the rule's org, so nothing leaks.
- Roles: writes need developer (agents, eval sets, alert rules) or admin (integrations, API keys, invites, policies, promotion, deletes), consistently across routers; acknowledging and resolving alerts and incidents is open to viewers, which nothing documents otherwise.
- Tokens: access and refresh tokens are typed, the algorithm is pinned, refresh tokens are refused as access tokens and vice versa, and `get_current_user` takes the role from the membership, not the token.
- `test_connection`'s Datadog check and PagerDuty sends go to fixed public hosts, outside `outbound`, which is right.
