import base64
import csv
import io
import re
import zlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app import seed
from app.middleware.auth import make_access_token
from app.models import (
    Agent, AlertRule, Organization, OrgMembership, Span, Trace, TracePolicy, TracePolicyViolation, User,
)
from app.policy.checker import check_trace
from app.policy.events import events_from_spans
from app.services.report_service import VIOLATION_CSV_COLUMNS, summary_to_pdf
from app.services.trace_policy_service import active_policies

CONTRACT_COLUMNS = ["detected_at", "agent", "trace_id", "policy", "severity", "at_seq", "counterexample", "mode"]


def pdf_text(pdf: bytes) -> str:
    """Text of reportlab's ASCII85 + Flate page streams."""
    out = []
    for raw in re.findall(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        try:
            out.append(zlib.decompress(base64.a85decode(raw.strip().removesuffix(b"~>"))).decode("latin-1"))
        except (ValueError, zlib.error):
            out.append(raw.decode("latin-1"))
    return "\n".join(out)


async def _violations(db, org, agent, specs):
    """specs: (policy, severity, at_seq, counterexample, mode, minutes_ago)"""
    trace = Trace(org_id=org.id, agent_id=agent.id, trace_uid=f"t-{len(specs)}-{agent.id}")
    db.add(trace)
    await db.flush()
    policies = {}
    for name, severity, *_ in specs:
        if name not in policies:
            policies[name] = TracePolicy(org_id=org.id, name=name, source="x", severity=severity)
            db.add(policies[name])
    await db.flush()
    now = datetime.utcnow()
    rows = [TracePolicyViolation(org_id=org.id, trace_id=trace.id, agent_id=agent.id, policy_id=policies[name].id,
                                 policy_name=name, policy_version=i, severity=sev, at_seq=at, counterexample=cx,
                                 message=f"{name} violated at seq {at}", mode=mode,
                                 detected_at=now - timedelta(minutes=ago))
            for i, (name, sev, at, cx, mode, ago) in enumerate(specs)]
    db.add_all(rows)
    await db.commit()
    return trace, rows


SPECS = [
    ("no_exfil", "critical", 4, [1, 4], "batch", 5),
    ("confirm", "high", 3, [0, 1, 2, 3], "live", 10),
    ("confirm", "high", 2, [2], "batch", 20),
    ("retry_cap", "medium", 7, [], "live", 30),
    ("old", "low", 0, [0], "batch", 60 * 24 * 10),  # outside the default 7-day window
]


def _csv(text):
    return list(csv.reader(io.StringIO(text)))


async def test_violations_csv_columns_and_rows(client, db, org, agent, auth_headers):
    trace, rows = await _violations(db, org, agent, SPECS)
    r = await client.get("/api/reports/violations.csv", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "violations.csv" in r.headers["content-disposition"]
    header, *body = _csv(r.text)
    assert header == CONTRACT_COLUMNS == VIOLATION_CSV_COLUMNS
    assert body == [[v.detected_at.isoformat(), "test-agent", trace.id, v.policy_name, v.severity, str(v.at_seq),
                     " ".join(map(str, v.counterexample)), v.mode] for v in rows[:4]]

    r = await client.get("/api/reports/violations.csv?days=30", headers=auth_headers)
    assert [row[3] for row in _csv(r.text)[1:]] == ["no_exfil", "confirm", "confirm", "retry_cap", "old"]


@pytest.mark.parametrize("days", [0, 91])
async def test_violations_csv_rejects_bad_window(client, auth_headers, days):
    assert (await client.get(f"/api/reports/violations.csv?days={days}", headers=auth_headers)).status_code == 422


async def test_violations_csv_is_org_scoped(client, db, org, agent, auth_headers):
    other = Organization(name="Other", slug="other")
    db.add(other)
    await db.flush()
    other_agent = Agent(org_id=other.id, name="theirs")
    db.add(other_agent)
    await db.commit()
    await _violations(db, other, other_agent, SPECS[:2])
    r = await client.get("/api/reports/violations.csv", headers=auth_headers)
    assert _csv(r.text) == [CONTRACT_COLUMNS]


async def test_violations_csv_viewer_allowed(client, db, org, agent, viewer_headers):
    await _violations(db, org, agent, SPECS[:1])
    r = await client.get("/api/reports/violations.csv", headers=viewer_headers)
    assert r.status_code == 200 and len(_csv(r.text)) == 2


async def test_summary_has_violation_section(client, db, org, agent, auth_headers):
    await _violations(db, org, agent, SPECS)
    s = (await client.get("/api/reports/summary", headers=auth_headers)).json()
    pv = s["policy_violations"]
    assert pv["total"] == 4
    assert pv["by_severity"] == {"critical": 1, "high": 2, "medium": 1, "low": 0, "info": 0}
    assert list(pv["by_severity"]) == ["critical", "high", "medium", "low", "info"]
    assert pv["by_policy"] == [
        {"policy_name": "no_exfil", "severity": "critical", "count": 1},
        {"policy_name": "confirm", "severity": "high", "count": 2},
        {"policy_name": "retry_cap", "severity": "medium", "count": 1},
    ]
    assert [(a["agent_name"], a["policy_violations"]) for a in s["agents"]] == [("test-agent", 4)]
    s30 = (await client.get("/api/reports/summary?days=30", headers=auth_headers)).json()
    assert s30["policy_violations"]["total"] == 5 and s30["policy_violations"]["by_severity"]["low"] == 1


async def test_summary_without_violations(client, auth_headers, agent):
    pv = (await client.get("/api/reports/summary", headers=auth_headers)).json()["policy_violations"]
    assert pv == {"total": 0, "by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                  "by_policy": []}


async def test_pdf_has_policy_violations_section(client, db, org, agent, auth_headers):
    await _violations(db, org, agent, SPECS)
    r = await client.get("/api/reports/summary.pdf", headers=auth_headers)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    text = pdf_text(r.content)
    assert "Policy violations" in text
    assert "Total: 4 \\(1 critical, 2 high, 1 medium\\)" in text
    assert "- no_exfil [critical]: 1" in text and "- confirm [high]: 2" in text and "old" not in text


def test_pdf_section_for_empty_and_legacy_summaries():
    empty = {"total": 0, "by_severity": {"critical": 0}, "by_policy": []}
    for s in ({"window_days": 7, "policy_violations": empty}, {"window_days": 7}):
        text = pdf_text(summary_to_pdf(s))
        assert "Policy violations" in text and "(Total: 0)" in text


async def test_seeded_demo_org_shows_violations(session_maker, db, client, monkeypatch, redis):
    async def _noop():
        pass
    monkeypatch.setattr(seed, "create_async_engine", lambda *a, **k: SimpleNamespace(dispose=_noop))
    monkeypatch.setattr(seed, "async_sessionmaker", lambda *a, **k: session_maker)
    await db.execute(text("PRAGMA foreign_keys = ON"))  # insert order matters on Postgres
    await seed.seed()

    org = (await db.execute(select(Organization).where(Organization.slug == "demo"))).scalar_one()
    owner = (await db.execute(select(User).join(OrgMembership, OrgMembership.user_id == User.id).where(
        OrgMembership.org_id == org.id, OrgMembership.role == "owner"))).scalar_one()
    headers = {"Authorization": f"Bearer {make_access_token(owner.id, org.id, 'owner')}"}

    rows = (await db.execute(select(TracePolicyViolation))).scalars().all()
    assert {(r.policy_name, r.severity) for r in rows} == {
        ("confirm_before_refund", "high"), ("no_email_after_secret", "critical"), ("retry_cap", "medium")}
    assert all(r.counterexample and r.mode == "batch" for r in rows)

    # every seeded row is what the batch check reports for its trace
    for trace_id in {r.trace_id for r in rows}:
        trace = await db.get(Trace, trace_id)
        policies = await active_policies(db, org.id, trace.agent_id, trace.environment)
        spans = (await db.execute(select(Span).where(Span.trace_id == trace_id))).scalars().all()
        expected = check_trace([c for _, c in policies], events_from_spans(spans))
        assert sorted((v.policy_name, v.at_seq, list(v.counterexample)) for v in expected) == sorted(
            (r.policy_name, r.at_seq, r.counterexample) for r in rows if r.trace_id == trace_id)

    pv = (await client.get("/api/reports/summary", headers=headers)).json()["policy_violations"]
    assert pv["total"] == len(rows) > 0
    assert len(_csv((await client.get("/api/reports/violations.csv", headers=headers)).text)) == len(rows) + 1
    r = await client.get(f"/api/traces/{rows[0].trace_id}/violations", headers=headers)
    assert [v["id"] for v in r.json()] == [rows[0].id]
    (rule,) = (await db.execute(select(AlertRule).where(AlertRule.metric == "policy_violations"))).scalars().all()
    assert rule.context == {"severity_at_least": "critical"}
    policies = (await db.execute(select(TracePolicy).where(TracePolicy.org_id == org.id))).scalars().all()
    assert sorted(p.name for p in policies) == ["confirm_before_refund", "no_email_after_secret", "retry_cap"]
