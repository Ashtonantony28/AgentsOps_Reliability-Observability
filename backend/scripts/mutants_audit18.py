"""AUDIT-18 mutation probe of the SG-AP3 fixes (SG-AP3.1 #75, SG-AP3.2 #76), replayed like AUDIT-17's:
python backend/scripts/mutants_audit18.py [probe]. Every row was killed when AUDIT-18 ran. Each mutant runs against
the SG-AP3 tests and AUDIT-18's regression guards, not the whole suite; `--all` runs the whole backend suite."""
import sys

import mutants_audit16 as m16

m = m16.m

P = ("backend", ["tests/api/test_alert_tick.py", "tests/api/test_outbound_lookups.py",
                 "tests/audit/test_audit_18.py"])
ALERT = "backend/app/services/alert_service.py"
AS = "backend/app/schemas/alert.py"
OUT = "backend/app/services/outbound.py"
ALR = "backend/app/routers/alerts.py"
INT = "backend/app/routers/integrations.py"

m.MUTANTS = [
    # SG-AP3.1: resume, turns, the 200-rule cap, the per-rule fire lock + dedup re-check
    ("probe", ALERT, "    i = bisect_left(keyed, (_resume_at,)) if _resume_at else 0",
     "    i = 0", *P),
    ("probe", ALERT, '        _resume_at = order[summary["checked"]][0] if summary["checked"] < len(order) else None',
     "        _resume_at = None", *P),
    ("probe", ALERT, "    keyed = sorted(((turn, rs[0].created_at, org), r) for org, rs in by_org.items() "
                     "for turn, r in enumerate(rs))",
     "    keyed = sorted(((rs[0].created_at, org, turn), r) for org, rs in by_org.items() "
     "for turn, r in enumerate(rs))", *P),
    ("probe", ALR, ")).scalar_one() >= MAX_ORG_RULES:", ")).scalar_one() > MAX_ORG_RULES:", *P),
    ("probe", AS, "MAX_ORG_RULES = 200", "MAX_ORG_RULES = 20000", *P),
    # SG-AP3.2: a thread per queue place, ORG_LOOKUPS, the _Places bound, the tick's org.set, the /test limit
    ("probe", OUT, "RESOLVE_THREADS = RESOLVE_QUEUE", "RESOLVE_THREADS = 16", *P),
    ("probe", OUT, "ORG_LOOKUPS = 2  ", "ORG_LOOKUPS = 64  ", *P),
    ("probe", OUT, "            if self.held[org_id] < bound:", "            if self.held[org_id] <= bound:", *P),
    ("probe", ALERT, "        outbound.org.set(org_id)  # this task's own context", "        pass", *P),
    ("probe", OUT, "    if call:\n        await _org_lookups.take(call.org_id, ORG_LOOKUPS)\n",
     "    if False:\n        await _org_lookups.take(call.org_id, ORG_LOOKUPS)\n", *P),
    ("probe", INT, '    await enforce_rate_limit(f"integration-test:{ctx.org_id}", '
                   'settings.INTEGRATION_TEST_LIMIT_PER_MIN)', "    pass", *P),
]

if __name__ == "__main__":
    if "--all" in sys.argv:
        sys.argv.remove("--all")
        m.MUTANTS = [(*row[:4], "backend", ["tests", "--ignore=tests/audit/test_audit_15.py",
                                            "--ignore=tests/audit/test_audit_16.py",
                                            "--ignore=tests/api/test_postgres.py"]) for row in m.MUTANTS]
    sys.exit(m.main())
