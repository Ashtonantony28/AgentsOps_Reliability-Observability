"""Seed demo data. Idempotent: bails if demo org already exists."""
import asyncio
import random
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models import (
    Agent, AgentVersion, AlertEvent, AlertRule, ApiKey, AuditLog,
    EvaluationCase, EvaluationResult, EvaluationRun, EvaluationSet,
    Incident, Organization, OrgMembership, PolicyRule, Span, Trace, TracePolicy, TracePolicyViolation, User,
)
from app.policy.checker import check_trace, compile_policy
from app.policy.dsl import parse
from app.policy.events import Event
from app.services.auth_service import generate_api_key, hash_password

DEMO_POLICIES = [  # (source, action)
    ('policy confirm_before_refund severity high:\n  always tool("stripe_refund") -> once event("confirmation")',
     "alert"),
    ('policy no_email_after_secret severity critical:\n  always tool("send_email") -> not once tool("vault.read")',
     "alert"),
    ('policy retry_cap severity medium:\n  always count(tool(*) and status("error")) by name <= 3', "log"),
]
# agent -> event sequences (kind, name, status); the second of each pair violates a demo policy
DEMO_SCENARIOS = {
    "Refund Bot": [
        [("tool", "order_lookup", "success"), ("confirmation", "user_confirmed", "success"),
         ("tool", "stripe_refund", "success")],
        [("tool", "order_lookup", "success"), ("tool", "stripe_refund", "success")],
    ],
    "Sales Outreach": [
        [("tool", "crm_lookup", "success"), ("tool", "send_email", "success")],
        [("tool", "vault.read", "success"), ("tool", "crm_lookup", "success"), ("tool", "send_email", "success")],
    ],
    "Support Triage": [
        [("tool", "kb_lookup", "error"), ("tool", "kb_lookup", "success")],
        [("tool", "kb_lookup", "error")] * 4 + [("tool", "web_search", "success")],
    ],
}


async def seed_trace_policies(db, org: Organization, agents: list[Agent], now: datetime, repeat: int = 4) -> None:
    """Demo trace policies plus streamed-style traces, with their violations checked as the batch check would."""
    policies = []
    for source, action in DEMO_POLICIES:
        (p,) = parse(source)
        row = TracePolicy(id=str(uuid.uuid4()), org_id=org.id, name=p.name, source=source, severity=p.severity,
                          action=action)
        db.add(row)
        policies.append(compile_policy(row.id, p))
    by_name = {a.name: a for a in agents}
    violations = []
    for agent_name, scenarios in DEMO_SCENARIOS.items():
        a = by_name[agent_name]
        for i in range(repeat):
            for steps in scenarios:
                ts = now - timedelta(hours=random.randint(1, 6 * 24), minutes=random.randint(0, 59))
                tr = Trace(id=str(uuid.uuid4()), org_id=org.id, agent_id=a.id, trace_uid=str(uuid.uuid4()),
                           status="success", input=f"{agent_name} demo task #{i}", output="done",
                           latency_ms=200 * len(steps), environment=a.environment, agent_version="1.0.0",
                           meta={"stream": "final"}, created_at=ts)
                db.add(tr)
                events = [Event(seq, kind, name, status, {}) for seq, (kind, name, status) in enumerate(steps)]
                for e in events:
                    at = ts + timedelta(milliseconds=200 * e.seq)
                    db.add(Span(trace_id=tr.id, seq=e.seq, kind=e.kind, name=e.name, status=e.status,
                                input={}, output={}, latency_ms=200, started_at=at, ended_at=at))
                for v in check_trace(policies, events):
                    violations.append(TracePolicyViolation(
                        org_id=org.id, trace_id=tr.id, agent_id=a.id, policy_id=v.policy_id,
                        policy_name=v.policy_name, policy_version=1, severity=v.severity, at_seq=v.at_seq,
                        counterexample=list(v.counterexample), message=v.message, mode="batch",
                        counterexample_status="minimal" if v.minimal else "fallback",
                        detected_at=ts + timedelta(seconds=1)))
    await db.flush()  # no ORM relationships, so traces must be inserted before their violations
    db.add_all(violations)
    db.add(AlertRule(
        org_id=org.id, agent_id=None, name="Critical policy violation", metric="policy_violations",
        operator=">=", threshold=1, window_minutes=60, severity="critical",
        context={"severity_at_least": "critical"}, channels=[],
    ))
    await db.flush()


async def seed():
    engine = create_async_engine(settings.db_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        existing = (await db.execute(select(Organization).where(Organization.slug == "demo"))).scalar_one_or_none()
        if existing:
            print("seed: demo org exists, skipping")
            await engine.dispose()
            return

        org = Organization(name="Demo Org", slug="demo", tier="pro", retention_days=90, rate_limit_per_min=600)
        db.add(org)
        await db.flush()

        owner = User(email=settings.DEMO_EMAIL, password_hash=hash_password(settings.DEMO_PASSWORD),
                     full_name="Demo Owner")
        dev = User(email="dev@agentops.dev", password_hash=hash_password("Dev1234!"), full_name="Demo Dev")
        viewer = User(email="viewer@agentops.dev", password_hash=hash_password("View1234!"), full_name="Demo Viewer")
        db.add_all([owner, dev, viewer])
        await db.flush()

        db.add_all([
            OrgMembership(org_id=org.id, user_id=owner.id, role="owner"),
            OrgMembership(org_id=org.id, user_id=dev.id, role="developer"),
            OrgMembership(org_id=org.id, user_id=viewer.id, role="viewer"),
        ])

        full, prefix, h = generate_api_key()
        db.add(ApiKey(org_id=org.id, name="Demo ingest key", key_prefix=prefix, key_hash=h))
        print(f"seed: demo API key = {full}")

        agents = []
        defs = [
            ("Support Triage", "Routes incoming customer messages.", "anthropic", "claude-haiku-4-5-20251001", ["web_search", "kb_lookup"]),
            ("Refund Bot", "Issues refunds for valid requests.", "openai", "gpt-4o-mini", ["stripe_refund", "order_lookup"]),
            ("Doc Summarizer", "Summarizes long documents on demand.", "anthropic", "claude-sonnet-4-6", []),
            ("Sales Outreach", "Drafts personalized outreach emails.", "anthropic", "claude-opus-4-7", ["crm_lookup", "send_email"]),
            ("Code Reviewer", "Reviews pull requests automatically.", "openai", "gpt-4o", ["github_read", "github_comment"]),
        ]
        for name, desc, prov, model, tools in defs:
            a = Agent(
                org_id=org.id, name=name, description=desc,
                model_provider=prov, model_name=model,
                environment=random.choice(["dev", "staging", "prod"]),
                tags=["demo"], allowed_tools=tools, eval_threshold=0.7,
            )
            db.add(a)
            await db.flush()
            db.add(AgentVersion(agent_id=a.id, version="1.0.0", model_name=model, environment=a.environment))
            agents.append(a)

        # Generate 30 days of synthetic traces
        now = datetime.utcnow()
        for a in agents:
            for d in range(30):
                day = now - timedelta(days=d)
                # ~ between 20 and 200 traces per agent per day
                n = random.randint(20, 200)
                for _ in range(n):
                    ts = day - timedelta(minutes=random.randint(0, 24 * 60))
                    err_chance = 0.08 if a.name in ("Refund Bot", "Code Reviewer") else 0.03
                    status = "success" if random.random() > err_chance else random.choice(["error", "timeout"])
                    latency = max(50, int(random.gauss(800, 350)))
                    tokens_in = random.randint(50, 1500)
                    tokens_out = random.randint(30, 800)
                    cost = round((tokens_in * 0.000003 + tokens_out * 0.000015), 6)
                    pii_flag = ["email"] if random.random() < 0.05 else []
                    inj = round(random.random() * 0.05, 3) if random.random() < 0.97 else 0.7
                    tr = Trace(
                        id=str(uuid.uuid4()), org_id=org.id, agent_id=a.id, trace_uid=str(uuid.uuid4()),
                        session_id=f"sess-{random.randint(1, 200)}",
                        status=status,
                        input=f"User asked about {random.choice(['refund', 'login', 'features', 'pricing'])} #{random.randint(1, 999)}",
                        output="" if status != "success" else f"Resolved (mock answer {random.randint(1, 999)}).",
                        error_message="UpstreamTimeout" if status == "timeout" else ("ToolError: not_found" if status == "error" else None),
                        latency_ms=latency,
                        tokens_input=tokens_in,
                        tokens_output=tokens_out,
                        cost_usd=cost,
                        environment=a.environment,
                        agent_version="1.0.0",
                        pii_flags=pii_flag,
                        injection_score=inj,
                        created_at=ts,
                    )
                    db.add(tr)
                    if random.random() < 0.5 and (a.allowed_tools or []):
                        tool_name = random.choice(a.allowed_tools)
                        db.add(Span(
                            trace_id=tr.id, seq=0, kind="tool", name=tool_name,
                            input={"query": "demo"}, output={"hits": random.randint(0, 5)},
                            latency_ms=random.randint(20, 400), status="success",
                            started_at=ts, ended_at=ts,
                        ))
            await db.flush()

        # Eval sets + runs
        es1 = EvaluationSet(
            org_id=org.id, name="Refund accuracy", description="Refund Bot core eval",
            evaluators=["exact", "semantic", "llm_judge"], runner={"type": "traces", "match": "contains"},
        )
        es2 = EvaluationSet(
            org_id=org.id, name="Triage routing", description="Support Triage routing accuracy",
            evaluators=["semantic", "latency", "cost"],
            schedule_cron="*/5 * * * *", runner={"type": "traces", "match": "contains"},
        )
        db.add_all([es1, es2])
        await db.flush()

        cases1 = [
            EvaluationCase(set_id=es1.id, input="Customer wants refund for order 1234", expected_output="Issued refund for order 1234."),
            EvaluationCase(set_id=es1.id, input="Refund denied case", expected_output="Refund denied: outside 30-day window."),
            EvaluationCase(set_id=es1.id, input="Partial refund?", expected_output="Issued partial refund."),
        ]
        cases2 = [
            EvaluationCase(set_id=es2.id, input="My password reset isn't working", expected_output="Routed to auth team."),
            EvaluationCase(set_id=es2.id, input="What is your pricing?", expected_output="Routed to sales team."),
        ]
        db.add_all(cases1 + cases2)
        await db.flush()

        # Pre-create a run with results
        refund_agent = next(a for a in agents if a.name == "Refund Bot")
        run = EvaluationRun(
            set_id=es1.id, agent_id=refund_agent.id, agent_version="1.0.0",
            status="completed", score=0.78, passed=2, failed=1, total=3,
            summary={"evaluators": ["exact", "semantic", "llm_judge"]},
            completed_at=datetime.utcnow(),
        )
        triage_agent = next(a for a in agents if a.name == "Support Triage")
        db.add_all([run, EvaluationRun(  # the agent the scheduled set runs against
            set_id=es2.id, agent_id=triage_agent.id, agent_version="1.0.0", status="completed", score=0.0,
            failed=2, total=2, summary={"evaluators": es2.evaluators}, completed_at=datetime.utcnow(),
        )])
        await db.flush()
        for c in cases1:
            ok = random.random() > 0.33
            db.add(EvaluationResult(
                run_id=run.id, case_id=c.id,
                actual_output=c.expected_output if ok else "Got: <wrong>",
                passed=ok, score=0.95 if ok else 0.35,
                scores_by_evaluator={"exact": 1.0 if ok else 0.0, "semantic": 0.9 if ok else 0.4, "llm_judge": 0.92 if ok else 0.3},
                reason="ok" if ok else "mismatch",
            ))

        # Alert rules: 1 firing, 1 resolved, 1 idle
        rule_fire = AlertRule(
            org_id=org.id, agent_id=refund_agent.id, name="Refund error rate > 10%",
            metric="error_rate", operator=">", threshold=0.10, window_minutes=15,
            severity="critical", channels=[{"type": "slack", "target": "https://hooks.slack.example/demo"}],
            runbook_url="https://example.com/runbooks/refund-errors",
        )
        rule_lat = AlertRule(
            org_id=org.id, agent_id=None, name="Org p95 latency > 3000ms",
            metric="latency_p95", operator=">", threshold=3000.0, window_minutes=30,
            severity="warning", channels=[{"type": "email", "target": settings.DEMO_EMAIL}],
        )
        rule_cost = AlertRule(
            org_id=org.id, agent_id=None, name="Daily cost > $100",
            metric="cost_per_day", operator=">", threshold=100.0, window_minutes=60,
            severity="warning", channels=[{"type": "webhook", "target": "https://example.com/hook"}],
        )
        db.add_all([rule_fire, rule_lat, rule_cost])
        await db.flush()

        db.add(AlertEvent(
            rule_id=rule_fire.id, org_id=org.id, agent_id=refund_agent.id,
            status="firing", value=0.18,
            message="Refund Bot error rate at 18%",
        ))
        db.add(AlertEvent(
            rule_id=rule_lat.id, org_id=org.id,
            status="resolved", value=3200, message="p95 latency spiked then recovered",
            resolved_at=datetime.utcnow() - timedelta(hours=2),
        ))

        # Incidents
        db.add(Incident(
            org_id=org.id, agent_id=refund_agent.id,
            title="Refund Bot error spike", severity="critical",
            timeline=[{"ts": datetime.utcnow().isoformat(), "event": "alert_fired", "detail": "error_rate=18%"}],
            narrative="Refund Bot showed elevated error rate over the last 15 minutes due to upstream timeouts.",
        ))

        await seed_trace_policies(db, org, agents, now)

        # Policy: prod can't use stripe_refund without approval
        db.add(PolicyRule(
            org_id=org.id, name="Block PII to prod",
            description="Disallow traces containing PII in prod environment",
            rule={"environment": "prod", "forbid_pii": True}, action="warn",
        ))
        db.add(PolicyRule(
            org_id=org.id, name="Block injection attempts",
            description="Block traces with prompt-injection signature score >=0.5",
            rule={"block_on_injection": True, "injection_threshold": 0.5}, action="block",
        ))

        db.add(AuditLog(
            org_id=org.id, actor_type="system", actor_id=None,
            action="seed_data", resource_type="org", resource_id=org.id,
            rationale="initial demo seed",
        ))

        await db.commit()
        print("seed: done")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
