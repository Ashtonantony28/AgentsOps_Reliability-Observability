"""Policy checker latency benchmark. Budget rows come from PLAN.md ("Latency budget")."""
import argparse
import math
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.policy import ast  # noqa: E402
from app.policy.checker import CompiledPolicy  # noqa: E402
from app.policy.events import Event  # noqa: E402

TRACE_LEN = 200
N_POLICIES = 10


@dataclass(frozen=True)
class Budget:
    path: str
    target: str
    workload: str
    p95_ms: float
    p99_ms: float


BUDGETS = [
    Budget("live", "Monitor.step per event", "10 enabled policies", 5, 20),
    Budget("ingest", "ingest overhead per event", "restore + step + snapshot, 10 policies", 8, 25),
    Budget("batch", "check_trace", "200 events x 10 policies", 250, 600),
    Budget("precheck", "would_violate", "200-event prefix, 10 policies", 10, 30),
    Budget("counterexample", "minimal_counterexample", "200 events, p0 or p2 (count by)", 500, 1000),
]
PATHS = [b.path for b in BUDGETS]


def tool(*globs, status=None):
    return ast.Match(kind="tool", globs=tuple(globs), status=status)


def synthetic_policies() -> list[ast.Policy]:
    any_tool = ast.Match(kind="tool")
    network = tool("http_*", "fetch_url", "send_email")
    secret = tool("vault.read", "read_secret*")
    formulas = [
        ast.Implies(tool("delete_*"), ast.Once(ast.Match(kind="confirmation"))),
        ast.Implies(network, ast.Not(ast.Once(secret))),
        ast.Count(ast.And(any_tool, ast.Match(status="error")), "<=", 3, by="name"),
        ast.Implies(tool("deploy_*"), ast.Previously(tool("run_tests"))),
        ast.Implies(tool("send_email"), ast.Since(ast.Not(secret), ast.Match(kind="confirmation"))),
        ast.Historically(ast.Not(tool("drop_db"))),
        ast.Count(ast.Match(kind="llm"), "<", 150),
        ast.Implies(ast.Match(kind="retrieval", attrs=(("source", "external"),)),
                    ast.Once(ast.Match(kind="custom", globs=("sanitize",)))),
        ast.Or(ast.Not(tool("pay_*")), ast.Once(ast.Match(kind="confirmation", globs=("payment_ok",)))),
        ast.Count(tool("fetch_url"), "<=", 40, by="kind"),
    ]
    return [ast.Policy(f"p{i}", "high", f) for i, f in enumerate(formulas[:N_POLICIES])]


NAMES = {
    "tool": ["search", "fetch_url", "http_get", "send_email", "vault.read", "delete_user", "run_tests",
             "deploy_prod", "pay_invoice", "read_secret_key"],
    "llm": ["plan", "answer"],
    "retrieval": ["docs"],
    "confirmation": ["user_confirmed", "payment_ok"],
    "custom": ["sanitize"],
}


def synthetic_trace(n: int = TRACE_LEN, seed: int = 0) -> list[Event]:
    rng = random.Random(seed)
    kinds = ["tool"] * 6 + ["llm"] * 2 + ["retrieval", "confirmation", "custom"]
    events = []
    for i in range(n):
        kind = rng.choice(kinds)
        attrs = {"source": rng.choice(["internal", "external"])} if kind == "retrieval" else {}
        status = "error" if rng.random() < 0.05 else "success"
        events.append(Event(i, kind, rng.choice(NAMES[kind]), status, attrs))
    return events


def counterexample_trace(n: int = TRACE_LEN) -> list[Event]:
    """A delete at the end with no confirmation before it: every event is in the counterexample (worst case)."""
    events = [e if e.kind != "confirmation" else Event(e.seq, "llm", "plan", e.status, {})
              for e in synthetic_trace(n - 1)]
    return events + [Event(n - 1, "tool", "delete_user", "success", {})]


def count_by_trace(n: int = TRACE_LEN) -> list[Event]:
    """The 4th error of one tool, spread over the trace, is the last event: violates p2 (`count ... by name`)."""
    bad = {n - 1 - j * (n // 4) for j in range(4)}
    return [Event(i, "tool", "fetch_url" if i in bad else NAMES["tool"][i % 3], "error" if i in bad else "success", {})
            for i in range(n)]


def real_impl() -> SimpleNamespace:
    from app.policy.checker import Monitor, check_trace, compile_policy, would_violate
    from app.policy.counterexample import minimal_counterexample
    return SimpleNamespace(compile_policy=compile_policy, check_trace=check_trace, Monitor=Monitor,
                           would_violate=would_violate, minimal_counterexample=minimal_counterexample)


class _StubMonitor:
    def __init__(self, policies):
        self.policies = list(policies)
        self.n = 0

    def step(self, event):
        self.n += 1
        return [p for p in self.policies if p.policy_id is None]

    def snapshot(self) -> bytes:
        return self.n.to_bytes(4, "big")

    @classmethod
    def restore(cls, policies, blob: bytes):
        m = cls(policies)
        m.n = int.from_bytes(blob, "big")
        return m


def stub_impl() -> SimpleNamespace:
    def check_trace(policies, events):
        return [p for p in policies for e in events if p.policy_id is None]

    return SimpleNamespace(
        compile_policy=lambda pid, p: CompiledPolicy(pid, p),
        check_trace=check_trace,
        Monitor=_StubMonitor,
        would_violate=lambda policies, prefix, cand: check_trace(policies, [cand]),
        minimal_counterexample=lambda policy, events, at_seq: tuple(e.seq for e in events[:at_seq + 1][-1:]),
    )


def workload(path: str, impl, compiled, events) -> Callable[[int], float]:
    """Returns run(i) -> elapsed ms for one sample of `path`."""
    clock = time.perf_counter
    if path == "live":
        state = {"m": impl.Monitor(compiled)}

        def run(i):
            if i % len(events) == 0:
                state["m"] = impl.Monitor(compiled)
            t = clock()
            state["m"].step(events[i % len(events)])
            return (clock() - t) * 1000
    elif path == "ingest":
        state = {"blob": impl.Monitor(compiled).snapshot()}

        def run(i):
            if i % len(events) == 0:
                state["blob"] = impl.Monitor(compiled).snapshot()
            t = clock()
            m = impl.Monitor.restore(compiled, state["blob"])
            m.step(events[i % len(events)])
            state["blob"] = m.snapshot()
            return (clock() - t) * 1000
    elif path == "batch":
        def run(i):
            t = clock()
            impl.check_trace(compiled, events)
            return (clock() - t) * 1000
    elif path == "precheck":
        prefix, candidate = events[:-1], events[-1]

        def run(i):
            t = clock()
            impl.would_violate(compiled, prefix, candidate)
            return (clock() - t) * 1000
    elif path == "counterexample":
        cases = [(compiled[0], counterexample_trace(len(events))), (compiled[2], count_by_trace(len(events)))]

        def run(i):
            policy, cx_events = cases[i % 2]
            t = clock()
            impl.minimal_counterexample(policy, cx_events, len(cx_events) - 1)
            return (clock() - t) * 1000
    else:
        raise ValueError(path)
    return run


def percentile(samples: list[float], q: float) -> float:
    s = sorted(samples)
    return s[max(0, math.ceil(q / 100 * len(s)) - 1)]


def bench(path: str, impl, iterations: int, events, policies) -> list[float]:
    compiled = [impl.compile_policy(p.name, p) for p in policies]
    run = workload(path, impl, compiled, events)
    for i in range(min(10, iterations)):
        run(i)
    return [run(i) for i in range(iterations)]


def load_test(impl, rate: float, seconds: float, n_traces: int = 50) -> int:
    """Live events at `rate`/s across `n_traces` interleaved traces; each event is restore + step + snapshot."""
    compiled = [impl.compile_policy(p.name, p) for p in synthetic_policies()]
    traces = [synthetic_trace(seed=t) for t in range(n_traces)]
    blobs = [impl.Monitor(compiled).snapshot() for _ in traces]
    steps, totals = [], []
    clock, total = time.perf_counter, int(rate * seconds)
    start = clock()
    for i in range(total):
        delay = start + i / rate - clock()
        if delay > 0:
            time.sleep(delay)
        t, pos = i % n_traces, (i // n_traces) % TRACE_LEN
        if pos == 0:
            blobs[t] = impl.Monitor(compiled).snapshot()
        t0 = clock()
        m = impl.Monitor.restore(compiled, blobs[t])
        t1 = clock()
        m.step(traces[t][pos])
        t2 = clock()
        blobs[t] = m.snapshot()
        steps.append((t2 - t1) * 1000)
        totals.append((clock() - t0) * 1000)
    achieved = total / (clock() - start)
    failed = achieved < 0.95 * rate
    print(f"load: {total} events over {n_traces} traces, target {rate:g}/s, achieved {achieved:.1f}/s"
          f"{'  TOO SLOW' if failed else ''}")
    for b, samples in [(BUDGETS[0], steps), (BUDGETS[1], totals)]:
        p50, p95, p99 = (percentile(samples, q) for q in (50, 95, 99))
        ok = p95 <= b.p95_ms and p99 <= b.p99_ms
        failed |= not ok
        print(f"{b.path:<15} {b.target:<26} {p50:>9.3f} {p95:>9.3f} {p99:>9.3f} "
              f"{f'{b.p95_ms:g}/{b.p99_ms:g}':>15}  {'ok' if ok else 'OVER BUDGET'}")
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", choices=PATHS + ["all"], default="all")
    ap.add_argument("--iterations", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true", help="time trivial stubs instead of the real checker")
    ap.add_argument("--assert-budget", action="store_true", help="exit 1 if any p95/p99 is over budget")
    ap.add_argument("--load", type=float, metavar="RATE", help="events/s for a sustained load test")
    ap.add_argument("--seconds", type=float, default=60)
    args = ap.parse_args(argv)

    impl = stub_impl() if args.dry_run else real_impl()
    if args.load is not None:
        return load_test(impl, args.load, args.seconds)
    events, policies = synthetic_trace(), synthetic_policies()
    budgets = [b for b in BUDGETS if args.path in ("all", b.path)]

    print(f"{'path':<15} {'target':<26} {'workload':<40} {'n':>5} {'p50 ms':>9} {'p95 ms':>9} {'p99 ms':>9} "
          f"{'budget p95/p99':>15}  status")
    failed = False
    for b in budgets:
        try:
            samples = bench(b.path, impl, args.iterations, events, policies)
        except NotImplementedError:
            print(f"{b.path:<15} {b.target:<26} {b.workload:<40} {'-':>5} {'-':>9} {'-':>9} {'-':>9} "
                  f"{f'{b.p95_ms:g}/{b.p99_ms:g}':>15}  NOT IMPLEMENTED")
            failed = True
            continue
        p50, p95, p99 = (percentile(samples, q) for q in (50, 95, 99))
        ok = p95 <= b.p95_ms and p99 <= b.p99_ms
        failed |= args.assert_budget and not ok
        print(f"{b.path:<15} {b.target:<26} {b.workload:<40} {len(samples):>5} {p50:>9.3f} {p95:>9.3f} {p99:>9.3f} "
              f"{f'{b.p95_ms:g}/{b.p99_ms:g}':>15}  {'ok' if ok else 'OVER BUDGET'}")
    if args.dry_run:
        print("(dry run: timings are for trivial stubs, not the checker)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
