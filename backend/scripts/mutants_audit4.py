"""AUDIT-4 surviving mutants: each is a one-line change the test suite does not catch.

    python backend/scripts/mutants_audit4.py            # replays every mutant, exits 1 if any survives
    python backend/scripts/mutants_audit4.py A4-1 A4-2  # only these findings

Each mutant is applied to a temporary copy of the repo, then its tests run there; the unmutated copy must pass
first. A fixing sub-goal adds tests until its mutants are killed, and the printed failing test should be one it added
(a flaky wall-clock test, A4-3, can kill a mutant by accident).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_TESTS = ["tests"]
SDK_TESTS = ["tests"]

# (finding, file, exact text, replacement, test dir, pytest args)
MUTANTS = [
    ("A4-1", "backend/app/services/trace_policy_service.py",
     '            row.counterexample_status = "none"\n    await db.commit()',
     '            row.counterexample_status = "none"', "backend", BACKEND_TESTS),
    ("A4-2", "sdk/agentops_sdk/trace.py", "return self._undelivered() or await self._require_client()._apost_check(",
     "return await self._require_client()._apost_check(", "sdk", SDK_TESTS),
    ("A4-2", "sdk/agentops_sdk/async_client.py", "self.fail_closed = fail_closed", "self.fail_closed = True",
     "sdk", SDK_TESTS),
    ("A4-2", "sdk/agentops_sdk/precheck.py", "async with httpx.AsyncClient(timeout=self.check_timeout_s) as c:",
     "async with httpx.AsyncClient(timeout=30.0) as c:", "sdk", SDK_TESTS),
    ("A4-2", "sdk/agentops_sdk/precheck.py",
     "r = await c.post(self._events_url(trace_uid), json=body, headers=self._headers())\n"
     "        except Exception as e:\n            log.warning(\"agentops event stream error, will retry: %s\", e)\n"
     "            return push_outcome(e)",
     "r = await c.post(self._events_url(trace_uid), json=body, headers=self._headers())\n"
     "        except Exception as e:\n            return \"fatal\"",
     "sdk", SDK_TESTS),
    ("A4-4", "backend/app/services/trace_policy_service.py",
     'insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert',
     'insert = None if db.get_bind().dialect.name == "postgresql" else sqlite.insert', "backend", BACKEND_TESTS),
    ("A4-4", "backend/app/services/violation_service.py",
     "            await db.execute(select(AlertRule.id).where(AlertRule.id == rule.id).with_for_update())\n", "",
     "backend", BACKEND_TESTS),
    ("A4-5", "backend/app/policy/counterexample.py",
     '        self.solver.set("timeout", max(1, int((self.deadline - time.monotonic()) * 1000)))\n', "",
     "backend", BACKEND_TESTS),
    ("A4-5", "backend/app/policy/checker.py", "        elif deadline is not None:\n            log.warning(",
     "        elif deadline is None:\n            log.warning(", "backend", BACKEND_TESTS),
    ("A4-8", "backend/app/routers/trace_policies.py",
     "    org, _api_key = request.state.api_key\n"
     "    await enforce_rate_limit(f\"check:{org.id}\", org.rate_limit_per_min)\n    try:\n        return await precheck(",
     "    org, _api_key = request.state.api_key\n    try:\n        return await precheck(",
     "backend", BACKEND_TESTS),
]


def _pytest(rel, text, cwd, args) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        ignore = shutil.ignore_patterns(".venv", "__pycache__", ".hypothesis", "node_modules", "*.egg-info")
        for d in ("backend", "sdk", "frontend", ".github") if cwd == "backend" else ("sdk",):
            shutil.copytree(ROOT / d, Path(tmp) / d, ignore=ignore)
        if rel:
            (Path(tmp) / rel).write_text(text)
        path = os.pathsep.join(filter(None, [str(Path(tmp) / "sdk"), os.environ.get("PYTHONPATH")]))
        return subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *args],
                              cwd=Path(tmp) / cwd, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": path})


def _failed(r: subprocess.CompletedProcess) -> str:
    return next((line for line in r.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))), f"exit {r.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("findings", nargs="*")
    want = set(ap.parse_args().findings)
    mutants = [m for m in MUTANTS if not want or m[0] in want]
    for cwd, args in {(m[4], tuple(m[5])) for m in mutants}:
        if (r := _pytest(None, None, cwd, args)).returncode:
            print(f"baseline {cwd} fails without any mutant: {_failed(r)}")
            return 2
    bad = 0
    for finding, rel, old, new, cwd, args in mutants:
        src = (ROOT / rel).read_text()
        if src.count(old) != 1:
            result = "STALE (text not found exactly once; update this mutant)"
        else:
            r = _pytest(rel, src.replace(old, new), cwd, args)
            result = f"killed by {_failed(r)}" if r.returncode else "SURVIVED"
        bad += not result.startswith("killed")
        print(f"{finding}  {rel}: {result}  ({old.strip().splitlines()[0][:60]!r})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
