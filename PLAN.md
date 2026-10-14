# PLAN: Z3 policy verification

Formal safety policies over agent traces, verified with Z3, surfaced live and in reports.

Every thread reads this file first and updates its own entry (owner, status, result) when it finishes.
Statuses: `proposed` → `ready` → `in progress` → `in review` → `done` (or `blocked: <reason>`).

## Rules

- SG0 merges before any parallel work. Changing anything under **Interface contracts** is a stop-and-report, not a local fix.
- SG1–SG3 run on fixture traces only. Nothing touches live ingestion until the **fixture gate** passes.
- Threads delegate bounded, test-driven tasks to subagents, at most 2 at a time.
- Subagents return only: files changed, tests added, verification command + result, open issues.
- A task that fails verification 3 times stops and is reported in its thread.
- Code: match existing conventions (async SQLAlchemy, pydantic v2 schemas, routers/services split), minimal comments, one PR per sub-goal.

## What the codebase gives us (as of `7766898`)

| Fact | Consequence |
|---|---|
| Traces are ingested whole (`TraceIn.tool_calls`), via REST, bulk, or `/ws/ingest`. No per-event ingest. | Live incremental checking needs a new event-append path. |
| `Span` has no sequence number; `started_at` defaults to insert time. Order is implicit in list order. | Add `spans.seq`; event order must be explicit. |
| Only tool calls exist. No "confirmation", "secret read" or other event kinds. | Add a generic ordered event (`kind`, `name`, `status`, `attrs`); tool calls are one kind. |
| `PolicyRule` + `evaluate_policies` already exist (stateless block/warn/log on forbid_tool, PII, injection). | New feature is `TracePolicy`, separate table and router; existing rules stay untouched. |
| Redis channel `traces:{org_id}` feeds `/ws/traces`, each message wrapped as `{"type":"trace"}`. | Violations go on `violations:{org_id}`, forwarded on the same socket as `{"type":"violation"}`. |
| Backend tests are 3 pure-unit files; no DB fixtures, no CI. | SG0 adds test harness (SQLite + fakeredis) and a CI workflow. |
| One Alembic migration (`0001_init`). Docker image is Python 3.11. | New migration `0002_trace_policies`; `z3-solver` added to requirements. |

## Design decisions (defaults picked; change before SG0 starts if you disagree)

1. **Past-time temporal logic only in v1** (`once`, `historically`, `previously`, `since`, past counts). Every past-time formula is monitorable event by event and encodes linearly, which is what makes the latency budget reachable. Future obligations ("confirmation must follow within 3 events") are out of scope for v1.
2. **Encoding is linear "state-carrying"**: per event `i`, a Boolean per subformula defined from step `i-1` (e.g. `once_P[i] = once_P[i-1] ∨ P[i]`). The naive "∃ j < i" grounding is quadratic and was measured at 1.4 s for 200 events.
3. **A plain-Python reference evaluator is the semantic oracle.** It is written by a different thread than the Z3 encoder, and every encoder test is differential against it (Hypothesis-generated traces × policies). This is how "no false negatives" is tested rather than asserted.
4. **Minimal counterexample = minimal unsat core** of `facts(events) ∧ ¬violation_at(i)` with one assumption literal per event. It is the smallest set of events whose observed facts alone entail the violation.
5. **Batch checks run off the request path** (Celery), live checks run in the ingest path with monitor state in Redis.

## Latency budget (early, from a measurement)

Measured in a cloud container, `z3-solver` via pip, Python 3.13, one policy (`delete_*` needs prior confirmation):

| Encoding | 10 events | 50 | 200 | 1000 |
|---|---|---|---|---|
| Naive ∃-grounding | 9.5 ms | 102 ms | 1383 ms | — |
| Linear state-carrying | 7.6 ms | 18.6 ms | 75.6 ms | 343 ms |
| Incremental `push`/`check`/`pop` per event | ~0.22 ms / event | | | |

Budget (in-process, excludes DB and network; enforced by `scripts/bench_policy.py --assert-budget` in CI):

| Path | Workload | p95 | p99 |
|---|---|---|---|
| Live: `Monitor.step` per event | 10 enabled policies | ≤ 5 ms | ≤ 20 ms |
| Ingest request overhead added by policies | per event, enqueue + monitor step | ≤ 8 ms | ≤ 25 ms |
| Batch: `check_trace` | 200 events × 10 policies | ≤ 250 ms | ≤ 600 ms |
| Pre-execution `would_violate` | 200-event prefix, 10 policies | ≤ 10 ms | ≤ 30 ms |
| Counterexample (only on violation) | 200 events, 1 policy | ≤ 500 ms | ≤ 1 s |

SG0 reruns these numbers on the benchmark harness and may revise the budget once, recording the reason here. After SG0 the budget only changes through a stop-and-report.

## Sub-goals

Dependency order: `SG0` → (`SG1` ∥ `SG2`) → **fixture gate** → (`SG3` ∥ `SG4`) → (`SG5a` ∥ `SG5b`) → `SG6` → audits.

### SG0 Shared interfaces and harness
- **Owner:** SG0 thread · **Status:** done (merged `aa2f27a`, PR #2) · **Blocks:** everything
- **Scope:** everything under **Interface contracts** as code with stub bodies (`NotImplementedError` / HTTP 501): `app/policy/{ast,events,checker,counterexample}.py` signatures, pydantic schemas, SQLAlchemy models + migration `0002`, TS types, DSL grammar (`docs/policy-dsl.md`, EBNF + semantics table), fixture format + ≥ 20 fixture traces with expected verdicts, test harness (SQLite async + fakeredis), `scripts/bench_policy.py`, CI workflow running backend, SDK and frontend checks.
- **Acceptance:**
  - Migration upgrades and downgrades cleanly on Postgres (docker-compose) and the new tables exist.
  - Every contract symbol imports; schema round-trip tests pass for `EventIn`, `ViolationOut`, `TracePolicyOut`.
  - Fixture traces load and validate against `EventIn`; each fixture names its expected violations (policy, `at_seq`, counterexample seqs).
  - Existing ingest behaviour unchanged: legacy `tool_calls` payloads map to events with `seq` = list index.
  - Benchmark harness runs and prints p50/p95/p99 per path (against a trivial stub).
- **Verify:** `cd backend && pytest -q` · `cd backend && alembic upgrade head && alembic downgrade -1 && alembic upgrade head` · `cd frontend && npx tsc --noEmit` · `python backend/scripts/bench_policy.py --dry-run`
- **Result:** all four verify commands pass locally (164 backend tests incl. a Postgres migration test, SDK 2, tsc clean, bench dry run prints all 5 paths). 31 fixtures in `backend/tests/policy/fixtures/`, verdicts and unique minimal counterexamples cross-checked by brute force. Budget unchanged: only stubs exist, so the first real numbers come from SG2's `--assert-budget` run. Additions to the contract made while freezing it are listed under **SG0 contract notes**.

### SG1 DSL parser and reference evaluator
- **Owner:** SG1 thread · **Status:** done · **Depends:** SG0
- **Scope:** `app/policy/dsl.py` (`parse`), `app/policy/reference.py` (`evaluate`), pretty-printer for round-trips. Pure Python, no Z3.
- **Acceptance:**
  - Parses every example in `docs/policy-dsl.md`; `parse(pretty(p)) == p` for Hypothesis-generated ASTs.
  - Errors carry line, column and an expected-token message; one test per error class (unknown tag, bad glob, unbalanced parens, non-integer bound, future operator).
  - `evaluate` matches the expected verdicts on every SG0 fixture.
  - Line coverage ≥ 95 % on `dsl.py` and `reference.py`.
- **Verify:** `cd backend && pytest tests/policy/test_dsl.py tests/policy/test_reference.py -q --cov=app.policy.dsl --cov=app.policy.reference --cov-fail-under=95` (narrowed from `--cov=app/policy`, which also counted the SG2/SG3 stubs)
- **Result:** verify passes with 100 % coverage on both files (248 tests); full backend suite 411 passed. Every fixture's verdict matches. `pretty`/`pretty_formula` live in `dsl.py`. The Hypothesis AST generator for SG2's differential test is `tests/policy/strategies.py` (`formulas()`, `policies`); it emits only atoms the DSL can express. `reference.truth(f, events)` gives per-position values. Attr equality compares Python types exactly, so a float `2.0` does not match `attr("k", 2)`. The evaluator is also cross-checked against a naive quantifier-literal oracle in `test_reference.py`.

### SG2 Trace → Z3 encoding
- **Owner:** SG2 thread · **Status:** done · **Depends:** SG0
- **Scope:** `app/policy/encode.py`, `check_trace` in `checker.py`. Linear state-carrying encoding; one solver, event facts shared across policies, each policy checked under its own assumption.
- **Acceptance:**
  - Matches SG0 fixture verdicts exactly.
  - Differential test: 5 000 Hypothesis cases (traces ≤ 50 events, random ASTs from the SG1 generator) where `check_trace` and `reference.evaluate` agree on every `(policy, at_seq)`. Run against SG1 once it lands; until then against fixtures only.
  - Encoding size is linear: formula node count for 1 000 events ≤ 12 × count for 100 events.
  - Batch budget row met.
- **Verify:** `cd backend && pytest tests/policy/test_encode.py -q` · `pytest tests/policy/test_differential.py -q --hypothesis-profile=ci` · `python scripts/bench_policy.py --path batch --assert-budget`
- **Result:** all three verify commands pass locally and run in CI. Fixture verdicts match on all 31 fixtures, using hand-written ASTs (`tests/policy/fixture_policies.py`) that are also checked equal to `parse(source)`. Differential: 5 000 cases each against `reference.evaluate` for small-vocabulary ASTs and for SG1's `policies` generator, all agree; seeded mutants of `encode.py` (since, previously, count, attr typing, `?` glob) are each caught. Node count 6 244 at 100 events vs 62 484 at 1 000 (10.0×). Batch p95 ≈ 55–78 ms locally; 1 000 events ≈ 0.4 s, 5 000 events ≈ 2–4 s (for A2).
  - How it works: `TraceEncoding` emits SMT-LIB text (the Python API alone cost ~330 ms per batch). One Bool per (distinct atom, event) with its observed value as a fact literal in `facts[i]`; temporal and count nodes are defined chains; structurally equal subformulas are encoded once across policies. Instead of one solver check per policy, one check covers all policies and each policy's first false position is read from an Int defined as a nested `ite`, which is equivalent because the facts fix the model. Each call uses its own `z3.Context` so threads can check concurrently.
  - For SG3: `Violation.counterexample` is `()` until SG3 wires it. `facts[i]` are the per-event literals to put behind assumption literals, but `count … by name/kind` chains are grouped by the observed name/kind, so for counterexamples SG3 has to make that grouping depend on the event's facts. Note `Match(attrs=(("a", 1),)) == Match(attrs=(("a", True),))` under dataclass equality (`1 == True`); the encoder keys atoms by value type, and AST comparisons in tests should also compare `repr`.

### Fixture gate
SG1 and SG2 merged, the differential suite green at 5 000 cases, and the batch budget met. Recorded here with the commit hash before SG3/SG4 start.
- **Passed** at `10a4a3e` (SG2 merge, PR #4).

### SG3 Minimal counterexamples
- **Owner:** unassigned · **Status:** proposed · **Depends:** fixture gate
- **Scope:** `app/policy/counterexample.py` (`minimal_counterexample`), wired into `check_trace` output.
- **Acceptance:**
  - Returned set is sufficient (re-checking only those events' facts still entails the violation) and minimal (dropping any one event no longer entails it), property-tested on 1 000 cases.
  - Matches hand-written expected counterexamples on all fixtures.
  - Counterexample budget row met.
- **Verify:** `cd backend && pytest tests/policy/test_counterexample.py -q` · `python scripts/bench_policy.py --path counterexample --assert-budget`

### SG4 Checker service (batch + live)
- **Owner:** SG4 thread · **Status:** done · **Depends:** fixture gate
- **Scope:** `Monitor` (incremental), Celery task `check_completed_trace`, event-append endpoint and `/ws/ingest` event messages, monitor state in Redis, `TracePolicy` CRUD router + `validate`, persistence of `PolicyViolation`, `action=block` returning 403 like existing `PolicyViolation`.
- **Acceptance:**
  - For every fixture, streaming events one by one through `Monitor` yields the same violations as `check_trace` on the whole trace (same `at_seq`).
  - Monitor state survives a Redis round-trip mid-trace (snapshot/restore test).
  - Out-of-order or duplicate `seq` is rejected with 409, never silently reordered.
  - Completed traces are checked exactly once (idempotent on re-delivery).
  - Live and ingest-overhead budget rows met; load test at 500 events/s for 60 s keeps p95 within budget.
- **Verify:** `cd backend && pytest tests/policy/test_monitor.py tests/api/test_trace_policies.py tests/api/test_event_ingest.py -q` · `python scripts/bench_policy.py --path live --assert-budget` · `python scripts/bench_policy.py --load 500 --seconds 60`
- **Result:** all three verify commands pass (149 tests across the three files; full backend suite green). Live `Monitor.step` p95 ≈ 0.02 ms, ingest restore+step+snapshot p95 ≈ 0.2 ms; load test 30 000 events at 500/s over 50 interleaved traces: step p95 ≈ 0.05 ms, ingest p95 ≈ 0.4 ms. CI now runs the live, ingest and load benches.
  - `Monitor` (`checker.py`, plan in `monitor.py`) evaluates the same state-carrying recurrences as the Z3 encoding on concrete values, so a step needs no solver. It is checked against `check_trace` on every fixture and on Hypothesis traces, and against `reference.evaluate` on 3 000 cases; seeded mutants (since, previously, count by) are caught. `step` raises `ValueError` on any seq other than `next_seq`. Snapshots are JSON carrying each policy's id and formula digest; `restore` raises `ValueError` on any mismatch.
  - Live path (`trace_service.append_events`, REST and `/ws/ingest`): per-trace Redis lock; spans in the DB are the source of truth, and the Redis monitor state (`policy:monitor:{trace_id}`, 24 h TTL) is a cache that is rebuilt from spans when missing, stale or for a changed policy set (violations found while rebuilding are recorded too). A streamed trace is marked `meta.stream = "open"` until `final`; appending to a final or whole-ingested trace is 409 `trace_closed`, seq gaps/dups 409 `seq_conflict` with `expected_seq`. A `block` violation rejects the whole request (403, nothing stored, `policy_block` audit row); legacy `forbid_tool` rules also apply to appended events.
  - Whole-trace ingest runs only `block` policies inline (via `Monitor`, 403 as before) and queues `check_completed_trace` when any trace policy applies; `final` on the events path queues it too. The task is idempotent through `ON CONFLICT DO NOTHING` on `(trace_id, policy_id, policy_version)` and returns only new rows (for SG5a to publish). It also fills in counterexamples on live rows once SG3 wires them into `check_trace`.
  - Test-only dependency: `fakeredis[lua]` + `lupa` (Redis lock scripts).

### SG5a Integration: pipeline, alerts, reports
- **Owner:** unassigned · **Status:** proposed · **Depends:** SG3, SG4
- **Scope:** publish on `violations:{org_id}`, forward on `/ws/traces`, `AlertRule.metric = "policy_violations"`, incident on `critical`, `GET /api/traces/{id}/violations`, report summary section, PDF section, `GET /api/reports/violations.csv`, seed data with demo policies.
- **Acceptance:**
  - Ingesting a violating fixture produces: a DB row, a Redis message received by a WS test client, and (if a matching rule exists) an `AlertEvent`.
  - PDF contains a "Policy violations" section; CSV columns match the contract.
  - Seeded demo org shows violations on first boot.
- **Verify:** `cd backend && pytest tests/api/test_violation_pipeline.py tests/api/test_reports_violations.py -q`

### SG5b Integration: UI
- **Owner:** unassigned · **Status:** proposed · **Depends:** SG4 (API), SG5a for live events
- **Scope:** policy list/editor (with server-side validation errors inline) under Governance, violations panel on `TraceDetail` highlighting counterexample events in the span tree, live violation toast via `wsStore`.
- **Acceptance:** typecheck and build pass; component tests for the violations panel and editor error display; manual run screenshot of a seeded violation in TraceDetail attached to the PR.
- **Verify:** `cd frontend && npx tsc --noEmit && npm run build && npm test -- --run`

### SG6 SDK streaming and pre-execution checks (stretch)
- **Owner:** unassigned · **Status:** proposed · **Depends:** SG4
- **Scope:** `Trace.add_event(...)`, `agentops.trace(..., stream=True)`, `client.check(name, input=...)` / async twin calling `POST /api/trace-policies/check`, optional `@agentops.guard` decorator that raises `PolicyBlocked` before running a tool.
- **Acceptance:**
  - Legacy SDK payloads unchanged (existing SDK tests pass untouched).
  - Contract test: SDK request/response models match the backend OpenAPI schema.
  - `check` on a would-violate candidate returns `allowed=False` with the violating policy; a network failure follows a documented fail-open/fail-closed setting (default fail-open, logged).
  - Pre-execution budget row met server-side.
- **Verify:** `cd sdk && pytest -q` · `cd backend && pytest tests/api/test_precheck.py -q` · `python backend/scripts/bench_policy.py --path precheck --assert-budget`

## Audits (after SG5b; SG6 included if built)

Each audit is a fresh session that wrote none of the code. Findings go to `AUDIT-<n>.md`, ranked critical/high/medium/low, each with a repro or failing test. Critical and high findings become new `SG-A<n>.<k>` entries here and go through the same build-and-verify loop. Audit rounds repeat until one full round has no critical or high findings.

| # | Focus | Method |
|---|---|---|
| A1 | Encoding soundness (no false negatives) | Differential fuzz at 100 k cases vs reference evaluator, mutation testing of `encode.py` (surviving mutants are findings), adversarial fixtures (empty trace, 1-event trace, glob edge cases, unicode names, counts at exact bounds). |
| A2 | Performance under load | Load at 2× the SG4 rate, long traces (5 000 events), 50 policies, Redis restarts mid-trace; every budget row checked. |
| A3 | API/SDK consistency | OpenAPI vs pydantic vs TS types vs SDK models; error codes; legacy payload compatibility. |
| A4 | Test coverage and quality | Coverage ≥ 90 % on `app/policy` and new routers, assertion density, tests that cannot fail. |

## Interface contracts

Frozen at SG0 merge. Changes are stop-and-report.

### Events (`app/policy/events.py`, `app/schemas/trace.py`)

```python
class EventIn(BaseModel):            # wire format
    seq: int                         # 0-based, strictly increasing within a trace
    kind: str = "tool"               # tool | llm | retrieval | confirmation | custom
    name: str                        # e.g. "delete_user", "user_confirmed"
    status: str = "success"          # success | error | timeout
    input: Dict[str, Any] = {}
    output: Dict[str, Any] = {}
    attrs: Dict[str, Any] = {}       # free-form labels usable in policies
    latency_ms: int = 0
    timestamp: Optional[datetime] = None

@dataclass(frozen=True)
class Event:                         # checker-internal, no payloads
    seq: int; kind: str; name: str; status: str; attrs: Mapping[str, Any]

def events_from_trace(trace: TraceIn) -> list[Event]: ...
def events_from_spans(spans: Sequence[Span]) -> list[Event]: ...
```

`TraceIn` gains `events: List[EventIn] = []`. A payload with both `events` and `tool_calls` is a 422. Legacy `tool_calls` become events with `kind="tool"`, `seq` = index.

### DSL (`app/policy/dsl.py`, full grammar in `docs/policy-dsl.md`)

```
tag secret_read = tool("vault.read", "read_secret*")
tag network     = tool("http_*", "fetch_url", "send_email")

policy confirm_before_delete severity high:
  always tool("delete_*") -> once event("confirmation")

policy no_exfil_after_secret severity critical:
  always @network -> not once @secret_read

policy retry_cap severity medium:
  always count(tool(*) and status("error")) by name <= 3
```

```python
def parse(src: str) -> list[Policy]: ...          # raises PolicyParseError(line, col, message)
```

AST (`app/policy/ast.py`, frozen dataclasses): `Policy(name, severity, formula, source)`, formula nodes `Match(kind, globs, status, attrs)`, `Not`, `And`, `Or`, `Implies`, `Once`, `Historically`, `Previously`, `Since`, `Count(pred, op, bound, by)`. A policy is `always φ`; it is violated at the first `seq` where `φ` is false.

### Checker (`app/policy/checker.py`, `app/policy/counterexample.py`, `app/policy/reference.py`)

```python
@dataclass(frozen=True)
class Violation:
    policy_id: str; policy_name: str; severity: str
    at_seq: int
    counterexample: tuple[int, ...]   # sorted seqs, minimal unsat core
    message: str

def check_trace(policies: Sequence[CompiledPolicy], events: Sequence[Event]) -> list[Violation]: ...
def compile_policy(policy_id: str, policy: Policy) -> CompiledPolicy: ...

class Monitor:
    def __init__(self, policies: Sequence[CompiledPolicy]): ...
    def step(self, event: Event) -> list[Violation]: ...   # violations first reached at this event
    def snapshot(self) -> bytes: ...
    @classmethod
    def restore(cls, policies, blob: bytes) -> "Monitor": ...

def would_violate(policies, prefix: Sequence[Event], candidate: Event) -> list[Violation]: ...
def minimal_counterexample(policy: CompiledPolicy, events: Sequence[Event], at_seq: int) -> tuple[int, ...]: ...
def evaluate(policy: Policy, events: Sequence[Event]) -> list[int]: ...   # reference oracle: violating seqs
```

Each policy reports at most one violation per trace (its first `at_seq`) in v1.

### Storage (migration `0002_trace_policies`)

- `trace_policies`: `id, org_id, name, source, severity (info|low|medium|high|critical), action (log|alert|block), agent_id?, environment?, enabled, version, created_at, updated_at`; unique `(org_id, name)`.
- `policy_violations`: `id, org_id, trace_id, agent_id, policy_id, policy_version, severity, at_seq, counterexample JSON, message, mode (batch|live|precheck), detected_at`; index `(org_id, detected_at)`, unique `(trace_id, policy_id, policy_version)`.
- `spans.seq INTEGER NULL`, backfilled from `started_at` order for existing rows.

### HTTP / WS

| Method | Path | Auth | Body → Response |
|---|---|---|---|
| GET/POST | `/api/trace-policies` | jwt (write: admin) | `TracePolicyCreate` → `TracePolicyOut` |
| PUT/DELETE | `/api/trace-policies/{id}` | jwt admin | bumps `version` on source change |
| POST | `/api/trace-policies/validate` | jwt | `{source}` → `{ok, errors: [{line, col, message}]}` |
| POST | `/api/trace-policies/check` | api-key | `{agent_id, trace_uid?, candidate: EventIn}` → `{allowed, violations: [ViolationOut]}` |
| POST | `/api/traces/{trace_uid}/events` | api-key | `{agent_id, events: [EventIn], final: bool}` → `{accepted, violations}`; 409 on seq gap/dup; 403 on `block` |
| GET | `/api/traces/{id}/violations` | jwt | → `[ViolationOut]` |
| GET | `/api/reports/violations.csv` | jwt | columns: `detected_at, agent, trace_id, policy, severity, at_seq, counterexample, mode` |
| WS | `/ws/ingest` | api-key | new message `{"type":"events", trace_uid, agent_id, events, final}`; untyped messages stay `TraceIn` |
| WS | `/ws/traces` | jwt | adds `{"type":"violation","data": ViolationOut}` from Redis `violations:{org_id}` |

`ViolationOut` = `Violation` fields + `id, trace_id, agent_id, policy_version, mode, detected_at`. TS mirror in `frontend/src/types/policy.ts`. `AlertRule.metric` accepts `policy_violations` (count in window; `context.severity_at_least` optional).

### SDK

```python
t.add_event(kind: str, name: str, *, status="success", input=None, output=None, **attrs)
agentops.trace(..., stream: bool = False)       # stream=True sends events as they are added
client.check(name: str, *, kind="tool", input=None, **attrs) -> CheckResult(allowed: bool, violations: list[dict])
await async_client.acheck(...)
```

### SG0 contract notes

Decided while turning the contract into code; frozen with the rest.

- AST: `And`/`Or`/`Implies`/`Since` are binary; `Match.attrs` is a tuple of `(key, value)` pairs; `Count.by` is `None | "name" | "kind"`; `Policy.source` is excluded from equality. Tags are inlined by the parser. Full grammar and semantics in `docs/policy-dsl.md`.
- `CompiledPolicy(policy_id, policy, artifact)`: `artifact` is the encoder's opaque slot.
- Counterexample "unconstrained facts": kind ranges over the 5 `EventIn` kinds; name, status and attr values (including a missing key) range over any value. Attr equality is type-strict. A counterexample need not contain `at_seq` (e.g. `not previously tool("a")`).
- `EventIn.kind` is a literal of the 5 kinds; `status` stays free text so legacy `tool_calls` still map. `TraceIn.events` seqs must be exactly `0..n-1` (422 otherwise).
- Storage: `spans.attrs JSON NULL` added (events need their attrs back), unique index `(trace_id, seq)`. `policy_violations.policy_name` stores the name at detection time so rows map straight to `ViolationOut`. Event timestamps are stored as naive UTC. The ORM class for `policy_violations` is `TracePolicyViolation`, to avoid clashing with the existing `trace_service.PolicyViolation` exception.
- `TracePolicyCreate` is `{source, action, agent_id?, environment?, enabled}`: `source` holds exactly one `policy` block, and `name`/`severity` come from it. `ViolationOut.id`/`trace_id` are null for precheck results.
- Existing `forbid_tool` rules also apply to `tool`-kind events, so an `events` payload cannot bypass them.

## Log

- 2026-10-13: Plan drafted from codebase read at `7766898`.
- 2026-10-13: Plan approved by Ashton; SG0 dispatched.
- 2026-10-14: SG0 in review.
- 2026-10-14: SG0 merged (`aa2f27a`). SG1 done.
- 2026-10-14: SG2 done. With SG1 merged, its merge meets the fixture gate.
- 2026-10-14: SG4 done.
