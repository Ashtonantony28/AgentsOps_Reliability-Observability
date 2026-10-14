# AUDIT-3: API/SDK consistency

Audited at `c741be2` (SG0 to SG6 and SG-A1.1 to SG-A2.4 merged) by a session that wrote none of the code. Scope: PLAN.md audit row A3 (OpenAPI vs pydantic vs TS types vs SDK models; error codes; legacy payload compatibility), plus whether the four entry points that decide a policy outcome agree with each other: whole-trace ingest (REST, bulk, `/ws/ingest`), event append (REST, `/ws/ingest`), `POST /api/trace-policies/check`, and the SDK (`check`, `Trace.check`, `@guard`, `stream=True`).

**Result: 0 critical, 4 high, 7 medium, 4 low.** The high findings are PLAN.md sub-goals `SG-A3.1` to `SG-A3.3`; the mediums are grouped into `SG-A3.4` and `SG-A3.5`.

Failing tests: `backend/tests/audit/test_audit_3.py` and `sdk/tests/test_audit_3.py`, one or two per finding, each `xfail(strict=True)`. Each fails today at its final assertion (the control assertions before it pass) and will XPASS, failing CI, once fixed, so the fixing sub-goal must remove the marker. Run them with `cd backend && pytest tests/audit/test_audit_3.py -q --runxfail` and `cd sdk && pytest tests/test_audit_3.py -q --runxfail`.

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A3-1 | high | Environment-scoped policies use the SDK's `environment` for whole traces but the agent row's for streamed traces and checks | SG-A3.1 |
| A3-2 | high | A streamed trace never sends its trace-level fields: it is stored as `success` with no input, output, tokens or cost, and legacy PII/injection rules never see its text | SG-A3.1 |
| A3-3 | high | `/check` ignores legacy `PolicyRule` block rules, so the guard runs a call the append path then rejects with 403 | SG-A3.2 |
| A3-4 | high | A sync check (`@guard` on a sync function) inside an async streamed trace never reaches the server and fails open | SG-A3.3 |
| A3-5 | medium | A check from a non-streamed trace carries no history (A1-M1, re-confirmed) | SG-A3.5 |
| A3-6 | medium | Checks share the ingest rate limit; a 429 becomes `allowed: true` under the default fail-open | SG-A3.4 |
| A3-7 | medium | `ViolationOut` can't say whether an empty counterexample is pending, computed, or a degraded fallback | SG-A3.5 |
| A3-8 | medium | `action: "alert"` and `action: "log"` behave identically | SG-A3.4 |
| A3-9 | medium | Whole-trace ingest is not idempotent on `trace_id`; an SDK retry stores and checks the trace twice | SG-A3.4 |
| A3-10 | medium | The SDK ignores the bulk endpoint's `rejected` list, so transient `storage_error` traces are lost | SG-A3.4 |
| A3-11 | medium | `/ws/ingest` skips the rate limit both REST ingest paths enforce | SG-A3.4 |
| A3-12 | low | Unknown agent is 404 on `/check` and 400 on ingest/append; string-detail errors all have code `http_error` | none |
| A3-13 | low | `TracePolicy.environment` is free text; a typo makes the policy silently never apply | none |
| A3-14 | low | `PolicyCheckIn.candidate.seq` is required but ignored | none |
| A3-15 | low | `/ws/ingest` error replies use a different shape from REST | none |

## What was checked and held

- **Types.** Every policy schema in OpenAPI (`EventIn`, `TracePolicyCreate/Update/Out`, `PolicyValidateOut`, `ViolationOut`, `PolicyCheckIn/Out`, `EventsAppendIn/Out`, `SpanOut`, `TraceOut`) matches `frontend/src/types/*.ts` field for field, including optionality and nullability (checked by script against `app.openapi()`). Enum values (`kind`, severity, action, mode) agree across pydantic, TS and the SDK's `EVENT_KINDS`.
- **SDK wire format.** `event_dict`, `check_request`, `events_request` and `CheckResult` match the OpenAPI schemas (`tests/api/test_sdk_contract.py`); the SDK's error parsing matches the server's `{"error": {"code", "message", "details"}}` envelope, including `seq_conflict.details.expected_seq` and `trace_busy`.
- **Legacy payloads.** SDK traces built with `add_tool_call` serialise exactly as before SG6 and validate against `TraceIn`; `tool_calls` map to events with `seq` = index; the bulk endpoint's per-trace `rejected` entries did not change the 202 status old SDKs rely on.
- **Precheck vs append for trace policies.** For `TracePolicy` rows, `allowed` is false exactly when the append would be a 403 (SG6 and SG-A1.1 tests), once the two paths see the same policy set (they do not when A3-1 applies).

## A3-1 (high): environment-scoped policies follow a different environment on each path

`TracePolicy.environment` (and legacy `PolicyRule.rule.environment`) scopes a policy to one environment. The environment it is matched against depends on how the trace arrives:

| Path | Environment used |
|---|---|
| Whole trace (REST, bulk, `/ws/ingest`, every non-streamed SDK trace) | `TraceIn.environment`, i.e. the SDK's `init(environment=...)` |
| Event append (`stream=True`, REST and WS) | `agent.environment` (the agent row, default `dev`); `EventsAppendIn` has no field for it |
| `/check` | `agent.environment`, or the stored trace's |
| Batch check of a streamed trace | `trace.environment`, which the first append set from the agent row |

The SDK README's own setup line is `agentops.init(..., environment="prod")`. With an agent registered as `dev` (the default), a `block` policy scoped to `prod` blocks the whole-trace form of a call and does nothing to the streamed form: no 403, no live row, no batch row. `/check` says `allowed: true`. One agent used across environments, which is what the SDK's per-trace `environment` argument is for, gets every streamed trace checked against the agent row's single environment.

```
agent.environment = "dev";  SDK environment = "prod";  policy no_drop (environment=prod, block)
whole trace  add_event drop_db  -> bulk: rejected policy_block, nothing stored
stream=True  add_event drop_db  -> 202, span stored, no violation (live or batch)
acheck("drop_db")               -> allowed: true
```

Tests: `test_environment_scoped_block_policy_applies_to_streamed_trace`, `test_environment_scoped_block_policy_applies_to_check`. The fix needs `environment` on `EventsAppendIn` and `PolicyCheckIn` (an interface change; see SG-A3.1).

## A3-2 (high): a streamed trace drops every trace-level field

`EventsAppendIn` carries only events and `final`, so a streamed trace's row is created from the agent row: `status="success"`, `input=""`, `output=""`, zero tokens, cost and latency, no `session_id`, `metadata` or `error_message`, `agent_version` from the agent. The README says output, tokens, cost and status are not sent; it does not say what follows from it:

- A streamed trace whose code raised is stored as `success`, so it is missing from `error_rate`, the `error_rate` alert metric, anomaly detection and the PDF/CSV reports; `cost_per_day` and `token_usage` alerts read zero.
- Legacy `forbid_pii` and `block_on_injection` rules scan the trace's `input`/`output` text on the whole-trace path. On the append path that text is never sent, so a `block_on_injection` rule on the user prompt, the case it exists for, never fires for a streamed trace (only strings inside event payloads are scanned).

```
policy rule no-pii {forbid_pii: true, action: block}
trace(input="mail jane.doe@example.com")             -> rejected policy_block
trace(stream=True, input="mail jane.doe@example.com") -> stored, no warning
```

Tests: `test_streamed_trace_keeps_trace_fields`, `test_streamed_trace_input_is_scanned_by_legacy_pii_rule`.

## A3-3 (high): `/check` ignores legacy block rules that the append path enforces

SG6 defines `allowed` as false exactly when appending the candidate would be a 403. `append_events` runs `evaluate_policies` (legacy `forbid_tool`, `forbid_pii` on event input/output, `block_on_injection` on event input) before the trace policies; `precheck` runs only the trace policies, and ignores the candidate's `input` entirely. So `/check` answers `allowed: true` for a `forbid_tool` call. Under `@guard` the forbidden tool runs; recording it with `add_event` then gets a 403, and the SDK stops streaming the trace (every later event is dropped and the trace stays open).

```
policy rule no-drop {forbid_tool: "drop_db", action: block}
POST /api/traces/t1/events  [drop_db]          -> 403 policy_block
POST /api/trace-policies/check  drop_db        -> 200 {"allowed": true, "violations": []}
```

Test: `test_check_agrees_with_append_on_legacy_block_rule`. Known since SG6 ("legacy `PolicyRule` block rules are not part of the precheck"), never rated; it breaks the one guarantee `/check` makes.

## A3-4 (high): a sync check in an async streamed trace skips the server

With `AsyncAgentOpsClient`, `add_event` schedules the push as a task. `Trace.check` (sync) does not push when the client is async, so right after any `add_event` it sees undelivered events and returns the failure result without calling the server: `allowed=True` under the default fail-open, with only a log warning. `@guard` on a sync function calls `Trace.check`, so in an async agent with sync tools every guarded call after the first event is unchecked. `acheck` is fine (it awaits the push).

```
async with aclient.trace(stream=True) as t:
    t.add_event("tool", "search")
    refund()            # @guard, server would answer allowed: false
-> refund runs; log: "agentops policy check failed (trace events not delivered); allowing"
```

Test: `sdk/tests/test_audit_3.py::test_sync_guard_in_async_streamed_trace_is_checked`.

## A3-5 (medium): a check from a non-streamed trace has no history

A1-M1, re-confirmed. `Trace.check` without `stream=True` sends `trace_uid: null`, so the server evaluates the candidate as event 0. For a past-time policy that is a false negative in one direction (`always tool("send_email") -> not once tool("vault.read")`: allowed after a secret read) and a false positive in the other (`delete_*` needs a prior confirmation: blocked even after one). The README documents it, but the guard's docstring says the call "is checked against that trace". When the false negative happens under a `block` policy, the whole-trace ingest at the end is a 403, so the trace is dropped too (and A3-10 makes that silent). The fix sends the unsent events as a prefix in `PolicyCheckIn`, an interface change. Test: `test_non_streamed_trace_check_sees_its_events`.

## A3-6 (medium): checks share the ingest rate limit

`/check` calls `enforce_rate_limit(f"org:{org.id}", ...)` with the same bucket as `/ingest`, `/ingest/bulk` and `/events`. When an org is over its limit, every check is a 429 and the SDK returns `allowed: true` (fail-open default); streamed pushes also get 429, stay pending, and the next `Trace.check` fails open on "events not delivered" without asking. So guards turn off exactly when the agent is busiest. Test: `test_check_is_not_starved_by_ingest`.

## A3-7 (medium): an empty counterexample is ambiguous in the API

A live row has `counterexample: []` until the batch check fills it in; the batch check may also compute `[]` (the violation holds whatever the events are, e.g. `always previously event("confirmation")` at seq 0) and leaves `mode: "live"`. The two are byte-identical in `GET /api/traces/{id}/violations`, which is why the UI polls every live row with an empty counterexample for 10 minutes and then guesses. Since SG-A1.2 a counterexample may also be a degraded fallback (every seq up to `at_seq`, after a solver timeout or error), flagged only in server logs, while the contract calls it a minimal unsat core. Test: `test_violation_says_when_counterexample_is_final`.

## A3-8 (medium): `alert` and `log` actions are identical

`PolicyAction` is `log | alert | block`, the editor offers all three, and the seed uses `alert`. Nothing reads `alert`: `policy_violations` alert rules count every violation in the window whatever its policy's action, so a `log` policy pages people exactly like an `alert` one. Test: `test_log_policy_does_not_fire_alert_rule`. Fix needs a decision: count only `alert` and `block` policies (proposed default), or drop `alert` from the enum.

## A3-9 (medium): whole-trace ingest is not idempotent

`traces.trace_uid` has no unique constraint and `ingest_trace` never looks it up. The SDK re-buffers a whole bulk batch on any timeout or exception, including one where the server stored it, so a retry stores each trace again, queues a second batch check and inserts a second set of violation rows (the unique key is per trace row), which fire alert rules again. `append_events` and `/check` then use only the newest row for that `trace_uid`. Test: `test_resent_trace_is_stored_once`.

## A3-10 (medium): the SDK ignores per-trace bulk rejections

Since SG0b the bulk endpoint answers 202 with `rejected: [{index, reason}]`. The SDK re-buffers only on HTTP ≥ 400, so it neither retries `storage_error` (transient by definition) nor logs `policy_block` or `invalid_payload`: those traces disappear silently. Test: `sdk/tests/test_audit_3.py::test_bulk_storage_error_is_retried`.

## A3-11 (medium): `/ws/ingest` has no rate limit

REST `/ingest`, `/ingest/bulk`, `/events` and `/check` call `enforce_rate_limit`; `/ws/ingest` handles any number of `TraceIn` and `events` messages on one socket without it. Pre-existing for whole traces, now extended to the event-append path. Test: `test_ws_ingest_is_rate_limited`.

## Low

- **A3-12 error codes.** An unknown `agent_id` is `404` on `/check` and `400` on `/ingest` and `/events`. Routers that raise `HTTPException(status, "some_code")` with a string end up as `{"error": {"code": "http_error", "message": "agent_not_found"}}`, so clients can only branch on the message (`agent_not_found`, `trace_not_found`, `policy_not_found`, `rate_limited`, `invalid_api_key`). The SDK treats a 404 from `/check` as a failed check, so a wrong `agent_id` allows every call under fail-open. Test: `test_unknown_agent_has_one_error_code`.
- **A3-13 policy environment.** `TracePolicyCreate.environment` (and the editor's text box) accepts any string; `"production"` is stored and the policy never matches the `prod` traces it was meant for. Test: `test_policy_environment_is_validated`.
- **A3-14 candidate seq.** `PolicyCheckIn.candidate` is an `EventIn`, so `seq` is required (422 without it) though the server ignores it; the TS type requires it too. Test: `test_check_candidate_needs_no_seq`.
- **A3-15 WS error shape.** `/ws/ingest` replies `{"ok": false, "error": "<code or text>"}`, with pydantic's error text inlined as `"invalid_payload: ..."` and `policy_block` without a `message`, where REST returns `{"error": {"code", "message", "details"}}` with `validation_error`. A non-policy exception on the whole-trace branch is not followed by a rollback (the events branch does roll back), so a database error can leave the socket's session unusable (inferred from the code, not reproduced). Repro: send a `TraceIn` message with an unknown `agent_id` and compare with `POST /api/traces/ingest`.
