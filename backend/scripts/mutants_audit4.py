"""AUDIT-4 surviving mutants: each is a one-line change the test suite does not catch.

    python backend/scripts/mutants_audit4.py            # replays every mutant, exits 1 if any survives
    python backend/scripts/mutants_audit4.py A4-1 A4-2  # only these findings

Each mutant is applied to a temporary copy of `backend/` and `sdk/`, then its tests run there. A fixing
sub-goal adds tests until its mutants are killed (the suite fails with the mutant in place).
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
     "                s.counterexample = with_cx[s.policy_id]\n    await db.commit()",
     "                s.counterexample = with_cx[s.policy_id]", "backend", BACKEND_TESTS),
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
     "    org, _api_key = await authenticate_api_key(request, db)\n"
     "    await enforce_rate_limit(f\"check:{org.id}\", org.rate_limit_per_min)\n    try:\n        return await precheck(",
     "    org, _api_key = await authenticate_api_key(request, db)\n    try:\n        return await precheck(",
     "backend", BACKEND_TESTS),
    ("A4-9", "backend/app/policy/monitor.py", "}) > 1:", "}) > 2:", "backend", BACKEND_TESTS),
]


def run(finding, rel, old, new, cwd, args) -> bool:
    """True when the mutant is killed."""
    src = (ROOT / rel).read_text()
    assert src.count(old) == 1, f"{finding}: expected exactly one match in {rel}"
    with tempfile.TemporaryDirectory() as tmp:
        ignore = shutil.ignore_patterns(".venv", "__pycache__", ".hypothesis", "node_modules", "*.egg-info")
        for d in ("backend", "sdk"):
            shutil.copytree(ROOT / d, Path(tmp) / d, ignore=ignore)
        (Path(tmp) / rel).write_text(src.replace(old, new))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *args],
                           cwd=Path(tmp) / cwd, capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(Path(tmp) / "sdk")})
    return r.returncode != 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("findings", nargs="*")
    want = set(ap.parse_args().findings)
    survived = 0
    for m in MUTANTS:
        if want and m[0] not in want:
            continue
        killed = run(*m)
        survived += not killed
        print(f"{m[0]}  {m[1]}: {'killed' if killed else 'SURVIVED'}  ({m[2].strip().splitlines()[0][:60]!r})")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
