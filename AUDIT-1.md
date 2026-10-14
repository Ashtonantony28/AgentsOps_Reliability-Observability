# AUDIT-1: encoding soundness (no false negatives)

Audited at `7df9f44` by a session that wrote none of the code. Scope: every path that decides a verdict, which is the reference evaluator (SG1), the Z3 encoding (SG2), the counterexample path (SG3, including its time-capped slow path), the live `Monitor` and its Redis snapshot (SG4), and the pre-check (SG6, server and SDK).

Failing tests for every finding are in `backend/tests/audit/test_audit_1.py`, marked `xfail(strict=True)`. Run them with `cd backend && pytest tests/audit -q --runxfail`.

## Summary

| ID | Severity | Finding | Sub-goal |
|---|---|---|---|
| A1-H1 | high | A live `block` policy stops blocking once one of its violations has been recorded | SG-A1.1 |
| A1-H2 | high | The batch verdict waits on counterexamples, which have no time bound outside the string theory; one slow or failing counterexample loses every violation of the trace | SG-A1.2 |
| A1-M1 | medium | SDK pre-checks on a trace that is not streamed are evaluated with no history | none (medium) |
| A1-L1 | low | Counterexample events range over the 5 kinds and the trace's observed kinds only | none |
| A1-L2 | low | `count … by name` plus globs: the 1 s string-theory budget runs out on ordinary traces and the counterexample keeps every event | none |

No critical findings. The core encoding is sound on everything tried; details under **What was checked and held**.

## A1-H1 (high): a live `block` policy stops blocking after its first recorded violation

`Monitor.step` reports a policy only the first time it is false (`_done[i]`), which is right for recording but not for enforcement. `append_events` blocks only on violations returned by `step`. So once a `block` policy has a stored violation in a trace, every later event that violates it is accepted. That state is reached in two ordinary ways:

1. An admin changes a policy's action from `log`/`alert` to `block` mid-trace. The source does not change, so the version and the plan digest stay the same, and the restored Redis monitor keeps `done = True`.
2. A `block` policy is created or enabled mid-trace. The next append rebuilds the monitor from spans, records the earlier violation as `live`, and marks the policy done.

In both cases `POST /api/trace-policies/check` answers `allowed: false` for the same candidate (`Monitor.check` ignores `_done`), so SG6's guarantee that `allowed` is false exactly when the append would be a 403 is also broken.

Repro (`test_block_after_action_change_still_blocks`, `test_block_policy_added_mid_trace_blocks_next_violation`):

```
policy no_drop: always not tool("drop_db")      action=log
append seq 0 drop_db        -> 202, violation recorded
set action=block
check  drop_db              -> allowed: false
append seq 1 drop_db        -> 202 {"accepted":1,"violations":[]}   expected 403
```

## A1-H2 (high): the batch verdict waits on counterexamples, which have no time bound

`check_trace` computes `minimal_counterexample` for every violated policy before returning any verdict, and `check_completed_trace` records nothing until `check_trace` returns. Only the string-theory mode has a time budget (`_STRING_BUDGET_S`); the bit-vector path and the deletion pass are unbounded. For whole-ingested traces (REST, bulk, `/ws/ingest` without `type: events`, and every non-streamed SDK trace) the batch check is the only place `log`/`alert` violations are found. When it overruns the Celery `task_time_limit` of 300 s, or any counterexample raises, every violation of every policy on that trace is lost, and the alert rules never see them.

Measured (Python 3.11, `z3-solver==5.1.0.0`, one policy, violation at the last event):

| Policy | 40 events | 80 | 200 | 300 | 400 |
|---|---|---|---|---|---|
| `always count(tool(*) and status("error")) by name <= 3` (the PLAN's retry cap) | 0.08 s | 0.6 s | 7.2 s | 41 s | 72 s |
| `always not once (count(tool(*)) by name > 3)` | 0.4 s | 11.3 s | | | |

The nested case grows about 29× per doubling, so it passes 300 s somewhere near 160 events. The budget row "counterexample, 200 events, 1 policy, p95 ≤ 500 ms" is missed by 14× on the retry cap. The SG3 bench passes because it uses a policy whose counterexample is cheap. `TraceIn.events` has no length cap, so trace length is up to the client.

Repro (`test_counterexample_failure_keeps_verdicts`, `test_nested_count_by_counterexample_is_bounded`, `test_retry_cap_counterexample_meets_budget`).

## A1-M1 (medium): SDK pre-checks on a trace that is not streamed have no history

Without `stream=True` a trace's `check` sends no `trace_uid`, so the server evaluates the candidate on an empty prefix. Every history-dependent `block` policy (`no_exfil_after_secret`, `confirm_before_delete` once a confirmation is required, retry caps) then reports `allowed: true`, and `@guard` lets the call run. `stream=False` is the default. This behaviour is deliberate and pinned by `sdk/tests/test_precheck.py::test_unstreamed_trace_check_has_no_history`, but nothing warns the caller. Related, and also documented: with the default `fail_closed=False`, a streamed trace with any undelivered event skips the server and allows every check until delivery catches up. Suggested fix: send the trace's local events as the prefix when not streaming, or log a warning once per trace.

## A1-L1 (low): counterexample kinds are a closed world

`FactEncoding.kinds` is the 5 `EventIn` kinds plus the kinds seen in the trace. The SG0 contract says exactly that, but `events_from_spans` can return other kinds from stored spans, and a `count … by kind` counterexample is then not sufficient for an event of a kind seen nowhere. Found by the fuzz below, once. Not reachable through the API today, because `EventIn.kind` is a literal of the 5. Repro: `test_counterexample_holds_for_unseen_kind`.

## A1-L2 (low): `count … by name` with globs keeps every event once the string budget runs out

This is documented in SG3 as a limit. It is recorded here because it already happens at 80 events with a plausible policy (`tool("delete_*") -> not once (count(status("error")) by name > 3)`): all 80 seqs are returned where `{75, 76, 77, 78, 79}` suffices, so the UI highlights the whole trace. Repro: `test_globbed_count_by_name_counterexample_is_minimal`.

## What was checked and held

- **Differential fuzz, 100 000 cases** (8 seeds × 12 500, `random`-based, not the SG1/SG2 generators): random formulas to depth 4 with every operator and all six comparisons, bounds 0 to 3 and 10²⁰, `by` none/name/kind; globs over `* ? . ( $ é 😀`, space and newline; statuses including `""` and `Error`; attr values `True/False/0/1/2/-1/2.0/1.0/"1"/""/"true"/None/[1]/10²⁰`, empty attr keys; names including `""`, newline and astral characters; unknown kinds (`span`, `Tool`, `""`) in 20 % of events; traces of 0, 1, 2, 5, 12 and 30 events. For each case the first violation from `reference.evaluate`, `encode.first_violations` and `Monitor.step` (with a JSON snapshot and restore mid-trace) agreed, and `would_violate` agreed with `reference.truth` at every prefix. No divergence.
- **Counterexample sufficiency** on every third violating case: the events outside the counterexample were re-drawn 20 times each and the violation re-checked with `reference.truth`. One failure, which is A1-L1.
- **Mutation testing of `encode.py`**: 20 hand-written mutants (previously, once, historically, since, count reset/grouping/`!=`/`<`/`>=`, attr type strictness, glob `?`/`re.S`/any-vs-all/fullmatch, atom key typing, kind and status checks, first-violation index). The existing `test_encode.py`, `test_differential.py` and `test_fixtures.py` kill all 20.
- **Parser**: precedence (`not a since b`, `a or b and c`, right-associative `->`), nested tags, `attr` typing (`01` is `1`, `true` is not `1`) and escapes all parse as `docs/policy-dsl.md` says.
- `encode.py` and `reference.py` build glob regexes the same way, and `monitor.py` reuses `encode.matches`, so a shared misreading of glob semantics would not be caught by the differential tests. The translation was checked by hand against the spec (whole name, case-sensitive, `?` is one code point, `re.S`) and is correct.

Not findings, by spec: globs are case-sensitive and kinds are client-chosen, so `tool("delete_*")` does not match `Delete_user` or `event("custom", "delete_user")`. Policy authors who need that should list the variants (`tool("delete_*", "Delete_*") or event("custom", "delete_*")`); worth a line in the DSL doc.
