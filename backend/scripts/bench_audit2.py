"""AUDIT-2 checker scaling repros (in-process, no DB): one line per measurement.

    python scripts/bench_audit2.py countby 50 100 200 400   # count-by-name counterexample vs window length
    python scripts/bench_audit2.py globs 8 10 12 14         # counterexample vs number of overlapping globs
    python scripts/bench_audit2.py long 1000 5000 20000     # first_violations (no counterexamples), 10 policies
    python scripts/bench_audit2.py batch 500                # check_trace on the bench trace, per policy
    python scripts/bench_audit2.py policies 10 50           # check_trace, 200 events x N policies
    python scripts/bench_audit2.py names 1000 20000 50000   # snapshot size and restore+step at N distinct names
    python scripts/bench_audit2.py replay 5000 20000        # would_violate (precheck replay) vs prefix length
"""
import string
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench_policy import percentile, synthetic_policies, synthetic_trace  # noqa: E402

from app.policy import ast  # noqa: E402
from app.policy.checker import Monitor, check_trace, compile_policy, would_violate  # noqa: E402
from app.policy.counterexample import minimal_counterexample, name_patterns  # noqa: E402
from app.policy.encode import first_violations  # noqa: E402
from app.policy.events import Event  # noqa: E402

RETRY_CAP = ast.Count(ast.And(ast.Match(kind="tool"), ast.Match(status="error")), "<=", 3, by="name")


def ms(f) -> float:
    t = time.perf_counter()
    f()
    return (time.perf_counter() - t) * 1000


def retry_trace(n: int) -> list[Event]:
    bad = {n - 1 - j * (n // 4) for j in range(4)}
    return [Event(i, "tool", "fetch" if i in bad else f"t{i % 7}", "error" if i in bad else "success", {})
            for i in range(n)]


def renamed(f, suffix: str):
    if isinstance(f, ast.Match):
        return replace(f, globs=tuple(g + suffix for g in f.globs))
    if isinstance(f, ast.Count):
        return replace(f, pred=renamed(f.pred, suffix))
    if hasattr(f, "arg"):
        return type(f)(renamed(f.arg, suffix))
    return type(f)(renamed(f.left, suffix), renamed(f.right, suffix))


def compiled(n: int = 10):
    base = synthetic_policies()
    return [compile_policy(f"p{i}", replace(base[i % 10], name=f"p{i}", formula=renamed(base[i % 10].formula,
                                                                                         str(i // 10) * (i >= 10))))
            for i in range(n)]


def main(mode: str, *ns: int) -> None:
    for n in ns:
        if mode == "countby":
            p = compile_policy("retry", ast.Policy("retry_cap", "medium", RETRY_CAP))
            print(f"countby  window {n:>6}: {ms(lambda: minimal_counterexample(p, retry_trace(n), n - 1)):>10.0f} ms")
        elif mode == "globs":
            name_patterns.cache_clear()
            p = compile_policy("g", ast.Policy("g", "high", ast.Not(
                ast.Match(kind="tool", globs=tuple(f"*{c}*" for c in string.ascii_lowercase[:n])))))
            t = ms(lambda: minimal_counterexample(p, [Event(0, "tool", "abc", "success", {})], 0))
            print(f"globs    {n:>2} overlapping: {t:>10.0f} ms  ({2 ** n} glob-match sets)")
        elif mode == "long":
            forms = [p.policy.formula for p in compiled()]
            print(f"long     {n:>6} events: {ms(lambda: first_violations(forms, synthetic_trace(n))):>10.0f} ms")
        elif mode == "batch":
            events = synthetic_trace(n)
            for p, at in zip(compiled(), first_violations([p.policy.formula for p in compiled()], events)):
                if at is not None:
                    print(f"batch    {n} events, {p.policy_id} violated at {at:>4}: counterexample "
                          f"{ms(lambda: minimal_counterexample(p, events, at)):>10.0f} ms", flush=True)
        elif mode == "policies":
            ps, events = compiled(n), synthetic_trace()
            samples = [ms(lambda: check_trace(ps, events)) for _ in range(30)]
            print(f"policies {n:>3} x 200 events: check_trace p50 {percentile(samples, 50):.0f} ms "
                  f"p95 {percentile(samples, 95):.0f} ms")
        elif mode == "names":
            ps = compiled()
            m = Monitor(ps)
            for i in range(n):
                m.step(Event(i, "tool", f"tool_{i}", "error", {}))
            blob = m.snapshot()
            def ingest(k):
                m2 = Monitor.restore(ps, blob)
                m2.step(Event(n, "tool", f"x{k}", "success", {}))
                m2.snapshot()

            samples = [ms(lambda k=k: ingest(k)) for k in range(40)]
            print(f"names    {n:>6} distinct: snapshot {len(blob) // 1024:>5} KB, restore+step+snapshot "
                  f"p50 {percentile(samples, 50):.1f} ms p95 {percentile(samples, 95):.1f} ms")
        elif mode == "replay":
            ps, events = compiled(), synthetic_trace(n)
            print(f"replay   {n:>6} prefix: would_violate {ms(lambda: would_violate(ps, events[:-1], events[-1])):>8.0f} ms")
        sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], *map(int, sys.argv[2:]))
