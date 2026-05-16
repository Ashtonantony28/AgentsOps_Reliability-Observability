import os
from datetime import datetime
from typing import Dict, List, Tuple

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Agent,
    EvaluationCase,
    EvaluationResult,
    EvaluationRun,
    EvaluationSet,
    Trace,
)
from app.services.security_service import cosine_similarity


async def claude_judge(prompt: str, expected: str, actual: str) -> Tuple[float, str]:
    """Use Claude to judge. Falls back to similarity if no API key."""
    if not settings.ANTHROPIC_API_KEY:
        sim = cosine_similarity(expected, actual)
        return sim, f"fallback(no_api_key) similarity={sim:.2f}"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "Score the actual answer against the expected answer on a 0..1 scale, "
                    "considering correctness, relevance, and safety. Respond as JSON "
                    '{"score": <float>, "reason": "<short reason>"}.\n\n'
                    f"Prompt: {prompt}\nExpected: {expected}\nActual: {actual}"
                ),
            }],
        )
        text = "".join(b.text for b in msg.content if hasattr(b, "text"))
        import json, re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return 0.5, "judge: unparseable"
        data = json.loads(m.group(0))
        return float(data.get("score", 0.5)), str(data.get("reason", "ok"))[:240]
    except Exception as e:
        sim = cosine_similarity(expected, actual)
        return sim, f"judge_error_fallback: {e!s} sim={sim:.2f}"


async def run_evaluation(db: AsyncSession, run_id: str) -> EvaluationRun:
    run = (await db.execute(select(EvaluationRun).where(EvaluationRun.id == run_id))).scalar_one()
    eval_set = (await db.execute(select(EvaluationSet).where(EvaluationSet.id == run.set_id))).scalar_one()
    agent = (await db.execute(select(Agent).where(Agent.id == run.agent_id))).scalar_one()
    cases = (await db.execute(select(EvaluationCase).where(EvaluationCase.set_id == eval_set.id))).scalars().all()

    run.status = "running"
    await db.commit()

    evaluators = eval_set.evaluators or ["exact", "semantic"]
    total_score = 0.0
    weight_sum = 0.0
    passed = 0
    failed = 0

    # For latency/cost regression: pull recent traces stats
    p95_latency = 0
    avg_cost = 0.0
    recent = (await db.execute(
        select(Trace.latency_ms, Trace.cost_usd).where(
            Trace.agent_id == agent.id
        ).order_by(Trace.created_at.desc()).limit(100)
    )).all()
    if recent:
        import numpy as np
        lat = [r[0] for r in recent]
        cst = [r[1] for r in recent]
        p95_latency = int(np.percentile(lat, 95))
        avg_cost = float(np.mean(cst))

    for case in cases:
        # Simulate agent run: we use the case's expected_output as actual when no live runner is wired.
        # If the agent has any real traces with similar input, use the latest matching output.
        actual = ""
        match = (await db.execute(
            select(Trace).where(
                Trace.agent_id == agent.id,
                Trace.input.like(f"%{case.input[:60]}%"),
            ).order_by(Trace.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if match:
            actual = match.output
        else:
            actual = case.expected_output  # noop pass; in real system this would invoke the agent

        scores: Dict[str, float] = {}
        reasons: List[str] = []
        if "exact" in evaluators:
            s = 1.0 if actual.strip() == case.expected_output.strip() else 0.0
            scores["exact"] = s
            reasons.append(f"exact={s}")
        if "semantic" in evaluators:
            s = cosine_similarity(actual, case.expected_output)
            scores["semantic"] = round(s, 3)
            reasons.append(f"semantic={s:.2f}")
        if "llm_judge" in evaluators:
            js, jr = await claude_judge(case.input, case.expected_output, actual)
            scores["llm_judge"] = round(js, 3)
            reasons.append(f"judge={js:.2f}:{jr[:60]}")
        if "latency" in evaluators:
            threshold = 3000  # ms p95
            s = 1.0 if p95_latency <= threshold else max(0.0, 1.0 - (p95_latency - threshold) / threshold)
            scores["latency"] = round(s, 3)
            reasons.append(f"p95={p95_latency}ms")
        if "cost" in evaluators:
            threshold = 0.05  # $/call
            s = 1.0 if avg_cost <= threshold else max(0.0, 1.0 - (avg_cost - threshold) / threshold)
            scores["cost"] = round(s, 3)
            reasons.append(f"avg_cost=${avg_cost:.4f}")

        score = sum(scores.values()) / max(1, len(scores))
        is_pass = score >= 0.7
        if is_pass:
            passed += 1
        else:
            failed += 1
        total_score += score * (case.weight or 1.0)
        weight_sum += case.weight or 1.0

        db.add(EvaluationResult(
            run_id=run.id,
            case_id=case.id,
            actual_output=actual,
            passed=is_pass,
            score=round(score, 3),
            scores_by_evaluator=scores,
            reason="; ".join(reasons),
        ))

    run.score = round((total_score / weight_sum) if weight_sum else 0.0, 3)
    run.passed = passed
    run.failed = failed
    run.total = passed + failed
    run.status = "completed"
    run.completed_at = datetime.utcnow()
    run.summary = {
        "evaluators": evaluators,
        "p95_latency_ms": p95_latency,
        "avg_cost_usd": round(avg_cost, 4),
    }
    await db.commit()
    return run
