"""AUDIT-17 mutation probe of the SG-AP2 fixes (SG-AP2.1 #71, SG-AP2.2 #72, SG-AP2.3 #73), replayed like AUDIT-16's:
python backend/scripts/mutants_audit17.py [probe]. Every row was killed when AUDIT-17 ran. Each mutant runs against the
SG-AP1 and SG-AP2 tests, not the whole suite; `--all` runs them against the whole backend suite."""
import sys

import mutants_audit16 as m16

m = m16.m

# AUDIT-17's xfail tests and AUDIT-16's A16-4 are left out: a mutant that makes one XPASS is not killed by the suite
P = ("backend", ["tests/api/test_alert_tick.py", "tests/api/test_auth_hardening.py", "tests/api/test_outbound_lookups.py",
                 "tests/api/test_outbound_limits.py", "tests/api/test_violation_pipeline.py",
                 "tests/api/test_platform_security.py", "tests/api/test_platform_features.py", "tests/test_auth.py",
                 "tests/audit/test_audit_16.py", "--deselect",
                 "tests/audit/test_audit_16.py::test_latency_evaluator_reads_the_runs_version"])
ALERT = "backend/app/services/alert_service.py"
AS = "backend/app/schemas/alert.py"
OUT = "backend/app/services/outbound.py"
INT = "backend/app/routers/integrations.py"
EVAL = "backend/app/services/evaluation_service.py"
VIO = "backend/app/services/violation_service.py"
AUTH = "backend/app/services/auth_service.py"
AR = "backend/app/routers/auth.py"
CFG = "backend/app/config.py"

m.MUTANTS = [
    # SG-AP2.1 per-rule isolation, check deadline, dispatch in a finally, window bounds, SQL aggregates
    ("probe", ALERT, "        except Exception:\n            log.exception(\"alert rule",
     "        except ZeroDivisionError:\n            log.exception(\"alert rule", *P),
    ("probe", ALERT, "            await db.rollback()\n            continue", "            continue", *P),
    ("probe", ALERT, "    db.expunge_all()  # a rollback", "    pass  # a rollback", *P),
    ("probe", ALERT, "    finally:\n        await _dispatch_all(pages)", "    else:\n        await _dispatch_all(pages)", *P),
    ("probe", ALERT, "        await asyncio.wait_for(_check_all(db, summary, pages, order), CHECK_DEADLINE_S)",
     "        await _check_all(db, summary, pages, order)", *P),
    ("probe", ALERT, "CHECK_DEADLINE_S = 180.0", "CHECK_DEADLINE_S = 300.0", *P),
    ("probe", ALERT, "    in_bound = 1 <= (rule.window_minutes or 0) <= MAX_WINDOW_MINUTES",
     "    in_bound = bool(rule.window_minutes)", *P),
    ("probe", AS, "    window_minutes: int = Field(15, ge=1, le=MAX_WINDOW_MINUTES)", "    window_minutes: int = Field(15, ge=1)", *P),
    ("probe", AS, "    window_minutes: Optional[int] = Field(None, ge=1, le=MAX_WINDOW_MINUTES)",
     "    window_minutes: Optional[int] = None", *P),
    ("probe", ALERT, "    q = select(*columns).where(Trace.org_id == rule.org_id, Trace.created_at >= since)",
     "    q = select(*columns).where(Trace.created_at >= since)", *P),
    ("probe", ALERT, "    return q.where(Trace.agent_id == rule.agent_id) if rule.agent_id else q", "    return q", *P),
    ("probe", ALERT, 'func.count().filter(Trace.status != "success")', 'func.count().filter(Trace.status == "error")', *P),
    ("probe", ALERT, "        pos = 0.95 * (total - 1)", "        pos = 0.95 * total", *P),
    ("probe", ALERT, ".offset(int(pos)).limit(2)", ".offset(int(pos)).limit(1)", *P),
    ("probe", ALERT, ".order_by(Trace.latency_ms)", ".order_by(Trace.latency_ms.desc())", *P),
    ("probe", ALERT, "func.avg(Trace.latency_ms)", "func.max(Trace.latency_ms)", *P),
    ("probe", ALERT, "func.sum(Trace.tokens_input + Trace.tokens_output)", "func.sum(Trace.tokens_input)", *P),
    # SG-AP2.2 resolver executor, queue bound, per-org calls and the callers that set the org
    ("probe", OUT, "    future = _resolver.submit(", "    future = asyncio.get_running_loop()._default_executor.submit(",
     *P),
    ("probe", OUT, "        if _lookups >= RESOLVE_QUEUE:", "        if False:", *P),
    ("probe", OUT, "            if _in_flight[org_id] >= ORG_CALLS:", "            if _in_flight[org_id] > ORG_CALLS:", *P),
    ("probe", OUT, "            call.refs += 1", "            pass", *P),
    ("probe", OUT, "    future.add_done_callback(done)", "    future.add_done_callback(lambda _: None)", *P),
    ("probe", OUT, "    call = _Call(org.get()) if org.get() else None", "    call = None", *P),
    ("probe", INT, "    outbound.org.set(ctx.org_id)\n", "", *P),
    ("probe", EVAL, "    outbound.org.set(eval_set.org_id)\n", "", *P),
    ("probe", VIO, "    outbound.org.set(rows[0].org_id)", "    pass", *P),
    # SG-AP2.3 the /password limit, each caller's share of the hash queue, the email+address login key
    ("probe", AR, '    await enforce_rate_limit(f"password:{ctx.user.id}", settings.PASSWORD_CHANGE_LIMIT_PER_MIN)\n', "",
     *P),
    ("probe", AUTH, "    if caller is not None and _caller_hashes[caller] >= settings.PASSWORD_HASH_PER_CALLER:",
     "    if False:", *P),
    ("probe", AUTH, "_caller_hashes[caller] >= settings.PASSWORD_HASH_PER_CALLER",
     "_caller_hashes[caller] > settings.PASSWORD_HASH_PER_CALLER", *P),
    ("probe", AUTH, "        _caller_hashes[caller] -= 1", "        pass", *P),
    ("probe", AUTH, "user.password_hash if user else DUMMY_HASH, caller=f\"ip:{ip}\")",
     "user.password_hash if user else DUMMY_HASH)", *P),
    ("probe", AUTH, "    password_hash = await _hashed(hash_password, password, caller=f\"ip:{ip}\")\n    user = User(",
     "    password_hash = await _hashed(hash_password, password)\n    user = User(", *P),
    ("probe", AUTH, "    password_hash = await _hashed(hash_password, password, caller=f\"ip:{ip}\")\n    # single use",
     "    password_hash = await _hashed(hash_password, password)\n    # single use", *P),
    ("probe", AUTH, "    if not await _hashed(verify_password, current, user.password_hash, caller=f\"user:{user.id}\"):",
     "    if not await _hashed(verify_password, current, user.password_hash):", *P),
    ("probe", AUTH, "    user.password_hash = await _hashed(hash_password, new, caller=f\"user:{user.id}\")",
     "    user.password_hash = await _hashed(hash_password, new)", *P),
    ("probe", AR, 'f"login:email:{req.email.lower()}:{ip}"', 'f"login:email:{req.email.lower()}"', *P),
    ("probe", CFG, "    PASSWORD_HASH_PER_CALLER: int = 4", "    PASSWORD_HASH_PER_CALLER: int = 16", *P),
]

if __name__ == "__main__":
    if "--all" in sys.argv:
        sys.argv.remove("--all")
        m.MUTANTS = [(*row[:4], "backend", ["tests", "--ignore=tests/audit/test_audit_15.py",
                                            "--ignore=tests/audit/test_audit_16.py",
                                            "--ignore=tests/audit/test_audit_17.py",
                                            "--ignore=tests/api/test_postgres.py"]) for row in m.MUTANTS]
    sys.exit(m.main())
