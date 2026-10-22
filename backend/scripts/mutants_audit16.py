"""AUDIT-16 mutation probe of the SG-AP1 fixes (SG-AP1.1 #66, SG-AP1.2 #67, SG-AP1.3, SG-AP1.4 #69), replayed like
AUDIT-15's: python backend/scripts/mutants_audit16.py [probe | A16-8]. The "probe" rows were killed when AUDIT-16 ran;
the A16-8 rows survived it (AUDIT-16.md A16-8). Each mutant runs against the platform and SG-AP1 tests, not
the whole suite; `--all` runs them against the whole backend suite."""
import shutil
import sys
from pathlib import Path

import mutants_audit15 as m15

m = m15.m


def _copytree(src, dst, *a, **kw):  # the alert tick tests read the compose files too
    out = m15._copytree(src, dst, *a, **kw)
    if Path(src).name == "backend":
        for f in m.ROOT.glob("docker-compose*.yml"):
            shutil.copy(f, Path(dst).parent / f.name)
    return out


m.shutil = type("shutil", (), {"copytree": staticmethod(_copytree), "ignore_patterns": shutil.ignore_patterns})

# AUDIT-15's and AUDIT-16's xfail tests are left out: a mutant that makes one XPASS is not killed by the suite
P = ("backend", ["tests/api/test_alert_tick.py", "tests/api/test_auth_hardening.py", "tests/api/test_outbound_limits.py",
                 "tests/api/test_promotion_gate.py", "tests/api/test_platform_security.py",
                 "tests/api/test_platform_features.py", "tests/test_auth.py",
                 "tests/audit/test_audit_15.py::test_a_slow_alert_channel_of_one_org_does_not_hold_up_another_orgs_alert",
                 "tests/audit/test_audit_15.py::test_failed_logins_do_not_delay_another_orgs_ingest",
                 "tests/audit/test_audit_15.py::test_connection_test_does_not_buffer_an_unbounded_response",
                 "tests/audit/test_audit_15.py::test_promotion_gate_needs_a_passing_run_of_the_promoted_version",
                 "tests/audit/test_audit_15.py::test_outbound_refuses_nat64_addresses_of_private_ipv4",
                 "tests/audit/test_audit_15.py::test_evaluation_runs_do_not_share_the_alert_queue"])
ALERT = "backend/app/services/alert_service.py"
NOTE = "backend/app/services/notification_service.py"
AS = "backend/app/schemas/alert.py"
ET = "backend/app/tasks/evaluation_tasks.py"
EVAL = "backend/app/services/evaluation_service.py"
CEL = "backend/app/celery_app.py"
AUTH = "backend/app/services/auth_service.py"
AR = "backend/app/routers/auth.py"
OUT = "backend/app/services/outbound.py"
AG = "backend/app/routers/agents.py"
ER = "backend/app/routers/evaluations.py"
ES = "backend/app/schemas/evaluation.py"

m.MUTANTS = [
    # SG-AP1.1 alert tick, channels, eval queue
    ("probe", ALERT, "minutes = rule.dedup_minutes if (rule.dedup_minutes or 0) >= 1 else 30",
     "minutes = rule.dedup_minutes or 30", *P),
    ("A16-8", ALERT, "    gate = asyncio.Semaphore(DISPATCH_CONCURRENCY)", "    gate = asyncio.Semaphore(10 ** 6)", *P),
    ("probe", ALERT, "for turn in zip_longest(*by_org.values()) for p in turn if p is not None",
     "for turn in by_org.values() for p in turn if p is not None", *P),
    ("probe", ALERT, "    for t in pending:\n        t.cancel()", "    for t in []:\n        t.cancel()", *P),
    ("probe", NOTE, "(channels or [])[:MAX_CHANNELS]", "(channels or [])", *P),
    ("probe", AS, "    dedup_minutes: int = Field(30, ge=1)", "    dedup_minutes: int = Field(30, ge=0)", *P),
    ("probe", AS, "    dedup_minutes: Optional[int] = Field(None, ge=1)", "    dedup_minutes: Optional[int] = None", *P),
    ("probe", ET, '        fail_run(run_id, "time_limit")', "        pass", *P),
    ("A16-8", EVAL, "            await db.commit()  # each case as it is scored", "            pass  # each case", *P),
    ("probe", CEL, '"app.tasks.evaluation_tasks.execute_run": {"queue": "evaluations"}',
     '"app.tasks.evaluation_tasks.execute_run": {"queue": "celery"}', *P),
    # SG-AP1.2 password hashing, login limits, refresh
    ("probe", AUTH, "    if _hashes_in_flight >= settings.PASSWORD_HASH_QUEUE:", "    if False:", *P),
    ("probe", AUTH, "    ok = await _hashed(verify_password, password, user.password_hash if user else DUMMY_HASH)",
     "    ok = bool(user) and await _hashed(verify_password, password, user.password_hash)", *P),
    ("probe", AUTH, "    password_hash = await _hashed(hash_password, password)\n    user = User(",
     "    password_hash = await asyncio.to_thread(hash_password, password)\n    user = User(", *P),
    ("probe", AUTH, "    if consumed.rowcount != 1:", "    if consumed.rowcount < 0:", *P),
    ("probe", AR, '    await enforce_rate_limit(f"login:email:{req.email.lower()}", settings.LOGIN_LIMIT_PER_MIN)\n',
     "", *P),
    ("probe", AR, 'f"login:email:{req.email.lower()}"', 'f"login:email:{req.email}"', *P),
    # SG-AP1.3 outbound size, time and addresses
    ("probe", OUT, "        if ip.is_site_local:\n            return False\n", "", *P),
    ("probe", OUT, "elif ip in _NAT64 or int(ip) >> 32 == 0:", "elif ip in _NAT64:", *P),
    ("probe", OUT, "if len(body) > MAX_REPLY_BYTES:", "if len(body) > 2 * MAX_REPLY_BYTES:", *P),
    ("probe", OUT, '"Accept-Encoding": "identity"', '"Accept-Encoding": "gzip"', *P),
    ("probe", OUT, "    timeout = timeout or TIMEOUT_S", "    timeout = TIMEOUT_S", *P),
    ("probe", OUT, 'if h[0] != "content-encoding"]', "]", *P),
    ("probe", EVAL, "except (outbound.BlockedURL, outbound.ReplyTooLarge) as e:", "except outbound.BlockedURL as e:", *P),
    # SG-AP1.4 promotion gate, versions, weights
    ("probe", AG, "                EvaluationRun.agent_version == req.version,\n", "", *P),
    ("probe", EVAL, "        Trace.agent_version == run.agent_version,\n", "", *P),
    ("probe", ER, "if version != agent.current_version and not", "if False and not", *P),
    ("probe", ES, "    weight: float = Field(default=1.0, gt=0)", "    weight: float = Field(default=1.0, ge=0)", *P),
    ("A16-8", AG, ").order_by(AgentVersion.created_at.desc()).limit(1)  # each promotion",
     ").limit(1)  # each promotion", *P),
]

if __name__ == "__main__":
    if "--all" in sys.argv:
        sys.argv.remove("--all")
        m.MUTANTS = [(*row[:4], "backend", ["tests", "--ignore=tests/audit/test_audit_15.py",
                                            "--ignore=tests/audit/test_audit_16.py",
                                            "--ignore=tests/api/test_postgres.py"]) for row in m.MUTANTS]
    sys.exit(m.main())
