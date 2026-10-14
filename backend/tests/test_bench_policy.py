import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend" / "scripts" / "bench_policy.py"


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, capture_output=True, text=True, timeout=120)


def test_dry_run_prints_every_path():
    r = _run("--dry-run", "--assert-budget", "--iterations", "20")
    assert r.returncode == 0, r.stderr
    rows = {line.split()[0]: line for line in r.stdout.splitlines()[1:] if line and not line.startswith("(")}
    assert set(rows) == {"live", "ingest", "batch", "precheck", "counterexample"}
    assert all(row.rstrip().endswith("ok") for row in rows.values())


def test_single_path():
    r = _run("--dry-run", "--path", "batch", "--iterations", "5")
    assert r.returncode == 0 and "batch" in r.stdout and "live" not in r.stdout


def test_load_reports_live_and_ingest_rows():
    r = _run("--load", "500", "--seconds", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "500 events over 50 traces" in r.stdout
    rows = [line for line in r.stdout.splitlines()[1:]]
    assert [row.split()[0] for row in rows] == ["live", "ingest"] and all(row.endswith("ok") for row in rows)


def test_percentile_and_workloads():
    sys.path.insert(0, str(SCRIPT.parent))
    import bench_policy as bp
    assert bp.percentile(list(map(float, range(1, 101))), 95) == 95.0
    assert bp.percentile([3.0], 99) == 3.0
    events = bp.synthetic_trace()
    assert [e.seq for e in events] == list(range(bp.TRACE_LEN))
    assert len(bp.synthetic_policies()) == bp.N_POLICIES
    assert {b.path: (b.p95_ms, b.p99_ms) for b in bp.BUDGETS} == {
        "live": (5, 20), "ingest": (8, 25), "batch": (250, 600), "precheck": (10, 30), "counterexample": (500, 1000),
    }
