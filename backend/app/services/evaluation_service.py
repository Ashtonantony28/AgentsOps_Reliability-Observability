import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import httpx
from pydantic import TypeAdapter
from sqlalchemy import select
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
from app.schemas.evaluation import EvalRunner, TracesRunner, WebhookRunner
from app.services import outbound
from app.services.security_service import cosine_similarity

_runner = TypeAdapter(EvalRunner)


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


async def _webhook_output(runner: WebhookRunner, run: EvaluationRun, case: EvaluationCase) -> Tuple[Optional[str], str]:
    body = {"run_id": run.id, "set_id": run.set_id, "case_id": case.id, "agent_id": run.agent_id,
            "agent_version": run.agent_version, "input": case.input}
    try:
        r = await outbound.post(runner.url, json=body, timeout=runner.timeout_s)
    except (outbound.BlockedURL, outbound.ReplyTooLarge) as e:
        return None, f"runner_error: {e}"
    except httpx.TimeoutException:
        return None, "runner_error: timeout"
    except httpx.HTTPError as e:
        return None, f"runner_error: {type(e).__name__}"
    if not r.is_success:
        return None, f"runner_error: status {r.status_code}"
    try:
        data = r.json()
    except ValueError:
        return None, "runner_error: invalid_json"
    if not isinstance(data, dict) or not isinstance(data.get("output"), str):
        return None, "runner_error: no output string"
    return data["output"], ""


async def _trace_output(db: AsyncSession, runner: TracesRunner, eval_set: EvaluationSet, run: EvaluationRun,
                        case: EvaluationCase) -> Tuple[Optional[str], str]:
    want = case.input.strip()
    rows = await db.stream(select(Trace.input, Trace.output).where(
        Trace.org_id == eval_set.org_id, Trace.agent_id == run.agent_id,
        Trace.created_at >= datetime.utcnow() - timedelta(days=runner.lookback_days),
        Trace.input.contains(want, autoescape=True),  # a prefilter: LIKE ignores case on SQLite
    ).order_by(Trace.created_at.desc()).execution_options(yield_per=100))
    async for got, output in rows:
        if (got.strip() == want) if runner.match == "exact" else (want in got):
            await rows.close()
            return output, ""
    return None, "no_matching_trace"


async def case_output(db: AsyncSession, eval_set: EvaluationSet, run: EvaluationRun,
                      case: EvaluationCase) -> Tuple[Optional[str], str, int]:
    """(the case's output, or None with why it has none; the runner's latency in ms)."""
    if not eval_set.runner:
        return None, "no_runner", 0
    runner = _runner.validate_python(eval_set.runner)
    t0 = time.perf_counter()
    if isinstance(runner, WebhookRunner):
        output, why = await _webhook_output(runner, run, case)
    else:
        output, why = await _trace_output(db, runner, eval_set, run, case)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    if output is not None and not output.strip():
        return None, "no_output", latency_ms
    return output, why, latency_ms


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
        weight = case.weight or 1.0
        weight_sum += weight
        actual, why, latency_ms = await case_output(db, eval_set, run, case)
        if actual is None:
            failed += 1
            db.add(EvaluationResult(run_id=run.id, case_id=case.id, actual_output="", passed=False, score=0.0,
                                    scores_by_evaluator={}, reason=why, latency_ms=latency_ms))
            continue

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
        total_score += score * weight

        db.add(EvaluationResult(
            run_id=run.id,
            case_id=case.id,
            actual_output=actual,
            passed=is_pass,
            score=round(score, 3),
            scores_by_evaluator=scores,
            reason="; ".join(reasons),
            latency_ms=latency_ms,
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
