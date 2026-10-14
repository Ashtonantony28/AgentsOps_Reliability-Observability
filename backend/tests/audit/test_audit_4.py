"""AUDIT-4.md findings that a test can state; each is xfail(strict=True) until its SG-A4 sub-goal fixes it.
Test gaps (mutants the suite does not kill) are replayed by `scripts/mutants_audit4.py` instead."""
import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.xfail(strict=True, reason="A4-6: CI never measures coverage, and the default config misses async code")
def test_ci_gates_backend_coverage_with_greenlet_tracing():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    configs = [p.read_text() for d in (ROOT, ROOT / "backend") for name in (".coveragerc", "setup.cfg", "pyproject.toml")
               if (p := d / name).exists()]
    text = "\n".join([ci, *configs])
    assert "--cov" in ci and re.search(r"fail[-_]under", text) and "greenlet" in text


@pytest.mark.xfail(strict=True, reason="A4-10: contract stub tests are parametrised over empty lists and never run")
def test_no_contract_stub_test_is_parametrised_over_nothing():
    try:
        stubs = importlib.import_module("tests.api.test_contract_stubs")
    except ModuleNotFoundError as e:
        if e.name != "tests.api.test_contract_stubs":
            raise
        return
    runs_over = {"test_jwt_routes_501_for_admin": "JWT_STUBS", "test_api_key_routes_501": "API_KEY_STUBS"}
    assert not [t for t, cases in runs_over.items() if hasattr(stubs, t) and not getattr(stubs, cases)]
