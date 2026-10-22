"""AUDIT-15 mutation probe of the platform fixes (SG-P1 #61, SG-P2 #64), replayed like AUDIT-4's:
python backend/scripts/mutants_audit15.py [probe | A15-12 | equiv]. The "probe" rows were killed when AUDIT-15 ran; the
A15-12 rows survived it (AUDIT-15.md A15-12); the "equiv" rows survive and are equivalent (see AUDIT-15.md). Each mutant
runs against the platform tests, not the whole suite; `--all` runs them against the whole backend suite."""
import shutil
import subprocess
import sys
from pathlib import Path

import mutants_audit4 as m

TIMEOUT_S = 900  # a mutant whose tests hang is killed, not waited for


def _run(*a, **kw):
    try:
        return subprocess.run(*a, timeout=TIMEOUT_S, **kw)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(e.cmd, 124, f"ERROR hung: no result in {TIMEOUT_S} s\n", "")


m.subprocess = type("subprocess", (), {"run": staticmethod(_run), "CompletedProcess": subprocess.CompletedProcess})


def _copytree(src, dst, *a, **kw):  # the platform tests read README.md and docs/ too
    out = shutil.copytree(src, dst, *a, **kw)
    if Path(src).name == "backend":
        shutil.copy(m.ROOT / "README.md", Path(dst).parent / "README.md")
        shutil.copytree(m.ROOT / "docs", Path(dst).parent / "docs", dirs_exist_ok=True)
    return out


m.shutil = type("shutil", (), {"copytree": staticmethod(_copytree), "ignore_patterns": shutil.ignore_patterns})

# AUDIT-15's own xfail tests are left out: a mutant that makes one XPASS is not killed by the suite
P = ("backend", ["tests/api/test_platform_security.py", "tests/api/test_platform_features.py",
                 "tests/api/test_platform_contracts.py", "tests/audit/test_audit_6.py", "tests/test_auth.py",
                 "tests/test_security.py", "tests/test_alert_compare.py"])
AUTH = "backend/app/services/auth_service.py"
MW = "backend/app/middleware/auth.py"
INT = "backend/app/routers/integrations.py"
OUT = "backend/app/services/outbound.py"
WS = "backend/app/routers/ws.py"
EVAL = "backend/app/services/evaluation_service.py"
ALERT = "backend/app/services/alert_service.py"
CRON = "backend/app/services/cron.py"
ET = "backend/app/tasks/evaluation_tasks.py"
RET = "backend/app/tasks/retention_tasks.py"

m.MUTANTS = [
    # SG-P1 (1) invites and (7) refresh, password change
    ("probe", AUTH, 'existing.role == "owner" and inviter_role != "owner"', 'existing.role == "owner" and False', *P),
    ("probe", AUTH, "    await db.execute(delete(Invite).where(Invite.org_id == org_id, Invite.email == email, "
                    "Invite.accepted_at.is_(None)))\n", "", *P),
    ("probe", AUTH, "Invite.token_hash == h, Invite.accepted_at.is_(None), Invite.expires_at > now,",
     "Invite.token_hash == h, Invite.accepted_at.is_(None),", *P),
    ("A15-12", AUTH, "    if used.rowcount != 1:\n        await db.rollback()", "    if used.rowcount < 0:\n"
                     "        await db.rollback()", *P),
    ("probe", AUTH, "db.add(OrgMembership(org_id=inv.org_id, user_id=user.id, role=inv.role))",
     'db.add(OrgMembership(org_id=inv.org_id, user_id=user.id, role="admin"))', *P),
    ("A15-12", AUTH, "update(Session).where(Session.user_id == user.id, Session.revoked_at.is_(None))",
     "update(Session).where(Session.revoked_at.is_(None))", *P),
    ("A15-12", AUTH, "    session.revoked_at = datetime.utcnow()\n    access, new_refresh", "    access, new_refresh", *P),
    ("equiv", AUTH, "if not session or session.revoked_at or session.expires_at < datetime.utcnow():",
     "if not session or session.revoked_at:", *P),
    ("probe", MW, ', "jti": secrets.token_hex(8)}', "}", *P),
    # SG-P1 (2) signed webhooks, (3) Slack state
    ("probe", INT, "Integration.org_id == org_id, Integration.kind == kind, Integration.enabled.is_(True),",
     "Integration.org_id == org_id, Integration.kind == kind,", *P),
    ("probe", INT, "Integration.org_id == org_id, Integration.kind == kind, Integration.enabled.is_(True),",
     "Integration.kind == kind, Integration.enabled.is_(True),", *P),
    ("probe", INT, "resource_type=\"integration\", resource_id=matched,", "resource_type=\"integration\", "
                    "resource_id=None,", *P),
    ("probe", INT, "OAuthState.used_at.is_(None),\n        OAuthState.expires_at > now,",
     "OAuthState.used_at.is_(None),", *P),
    ("probe", INT, "OrgMembership.user_id == st.user_id, User.is_active.is_(True))",
     "OrgMembership.user_id == st.user_id)", *P),
    ("probe", INT, "    await db.execute(delete(OAuthState).where(OAuthState.expires_at <= now))\n", "", *P),
    # SG-P1 (4) outbound
    ("A15-12", OUT, "trust_env=False", "trust_env=True", *P),
    ("probe", OUT, "return ip.is_global and not ip.is_multicast", "return ip.is_global", *P),
    ("probe", OUT, "if not ips or not all(is_public(ip) for ip in ips):", "if not ips or not any(is_public(ip) for ip "
                                                                          "in ips):", *P),
    ("probe", OUT, 'extensions = {"sni_hostname": u.raw_host.decode("ascii")} if u.scheme == "https" else {}',
     "extensions = {}", *P),
    ("probe", OUT, '"Host": u.netloc.decode("ascii"), ', "", *P),
    # SG-P1 (5) /ws/traces
    ("probe", WS, "                if not await _is_member(user_id, org_id):\n                    return True",
     "                if False:\n                    return True", *P),
    ("probe", WS, "OrgMembership.org_id == org_id, User.is_active.is_(True))", "OrgMembership.org_id == org_id)",
     *P),
    # SG-P2 (1) eval runner
    ("probe", EVAL, "    if output is not None and not output.strip():", "    if False:", *P),
    ("equiv", EVAL, "        Trace.org_id == eval_set.org_id, Trace.agent_id == run.agent_id,",
     "        Trace.agent_id == run.agent_id,", *P),
    ("probe", EVAL, "        Trace.created_at >= datetime.utcnow() - timedelta(days=runner.lookback_days),\n", "", *P),
    ("probe", EVAL, '(got.strip() == want) if runner.match == "exact" else (want in got)', "(want in got)", *P),
    ("probe", EVAL, "outbound.post(runner.url, json=body, timeout=runner.timeout_s)",
     "outbound.post(runner.url, json=body)", *P),
    # SG-P2 (2) cron and scheduled runs
    ("probe", CRON, "    return day or weekday  # both restricted", "    return day and weekday  # both restricted", *P),
    ("probe", CRON, "    stop = max(last, now - LOOKBACK)", "    stop = now - LOOKBACK", *P),
    ("probe", CRON, "any_day=fields[2] == \"*\", any_weekday=fields[4] == \"*\"",
     "any_day=fields[2] == \"*\", any_weekday=True", *P),
    ("probe", ET, "            if agent is None:\n                continue\n", "", *P),
    # SG-P2 (3, 4) alerts
    ("A15-12", ALERT, "            Trace.org_id == rule.org_id, Trace.created_at >= datetime.utcnow() - timedelta(days=1))",
     "            Trace.created_at >= datetime.utcnow() - timedelta(days=1))", *P),
    ("probe", ALERT, "        if rule.agent_id:\n            q = q.where(Trace.agent_id == rule.agent_id)\n"
                      "        return float(", "        return float(", *P),
    ("probe", ALERT, '    if incident or rule.severity == "critical":', "    if incident:", *P),
    ("probe", ALERT, "        AlertRule.enabled.is_(True), AlertRule.metric != \"policy_violations\")",
     "        AlertRule.metric != \"policy_violations\")", *P),
    # SG-P2 (5) retention
    ("A15-12", RET, "        if days and days > 0:", "        if days:", *P),
    ("probe", RET, "                Trace.org_id == org_id, Trace.created_at < now - timedelta(days=days)))",
     "                Trace.created_at < now - timedelta(days=days)))", *P),
]

if __name__ == "__main__":
    if "--all" in sys.argv:
        sys.argv.remove("--all")
        m.MUTANTS = [(*row[:4], "backend", ["tests", "--ignore=tests/audit/test_audit_15.py",
                                            "--ignore=tests/api/test_postgres.py"]) for row in m.MUTANTS]
    sys.exit(m.main())
