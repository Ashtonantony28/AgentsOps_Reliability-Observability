# AgentOps

**AI Agent Reliability & Observability Platform.** Monitor, evaluate, govern, and scale AI agents from pilot to production.

```
git clone <repo>
cd agentops
cp .env.example .env
docker-compose up --build
# UI:   http://localhost:3000
# API:  http://localhost:8000   (Swagger at /docs)
# Demo: demo@agentops.dev / Demo1234!
```

The first time the backend container starts it runs Alembic migrations and seeds a demo organisation with 30 days of synthetic trace data, 5 demo agents, evaluation runs, alert rules, incidents, and policies — log in and the dashboard is fully populated.

---

## Architecture

```
                              ┌─────────────────┐
                              │   React (Vite)  │
                              │  shadcn-style   │
                              │   Recharts UI   │
                              └────────┬────────┘
                                       │ axios + WS
                                       ▼
┌──────────┐    pub/sub    ┌─────────────────────┐
│  Agents  │ ──ingest───►  │  FastAPI (async)    │
│  (SDK)   │ ◄──auth/key── │  + JWT + API keys   │
└──────────┘               │  + WebSockets       │
                           └────────┬────────────┘
                                    │ asyncpg / aioredis
                  ┌─────────────────┴──────────────────┐
                  ▼                                    ▼
           ┌─────────────┐                     ┌──────────────┐
           │ PostgreSQL  │                     │    Redis     │
           │ (SQLAlchemy)│                     │  (pubsub,    │
           └─────────────┘                     │   rate-limit,│
                                               │   celery)    │
                                               └──────┬───────┘
                                                      │
                                            ┌─────────┴────────┐
                                            │  Celery worker   │
                                            │  + beat schedule │
                                            │  (anomaly,       │
                                            │   alerts, evals, │
                                            │   reports)       │
                                            └──────────────────┘
```

## Features

- **Authentication & multi-tenancy** — JWT + refresh tokens, org invites, RBAC (owner/admin/developer/viewer), API keys (hashed), device/session tracking.
- **Agent registry** — CRUD, versioning, dev→staging→prod promotion (gated on eval score), health (healthy/degraded/down), dependency graph.
- **Telemetry ingestion** — REST + WebSocket ingest, bulk ingest, span-level tool-call tracing, rate-limited per API key, configurable retention.
- **Real-time monitoring** — live trace feed (Redis pub/sub), time-series charts (success/error/latency/tokens/cost) with 1h/6h/24h/7d/30d selectors, cost-burn projection, agent comparison.
- **Evaluations** — eval sets with cases, on-demand or scheduled runs, evaluators (exact / semantic / LLM-judge / latency-regression / cost-regression), A/B diff reports, prod-promotion gate.
- **Security & governance** — PII masking, prompt-injection scanner (regex), policy engine (block/warn/log), least-privilege report, immutable audit log (CSV/JSON export), org scorecard.
- **Alerting & incidents** — rule engine (error_rate / latency_p95 / cost / tokens), Slack / PagerDuty / email / webhook channels, dedup, auto-incidents with narrative.
- **Integrations** — Slack (OAuth2), PagerDuty, Datadog, Grafana, New Relic, OpenAI/Anthropic/Cohere/Azure (key validation), GitHub Actions, GitLab CI, generic webhooks, webhook receiver.
- **Reporting** — exec summary, PDF + CSV exports, weekly digest email job.
- **Python SDK** — context-manager API (`with agentops.trace(...) as t:`), async support, auto error capture, batched flush with disk fallback.

## API reference (top 10 endpoints)

| Method | Path | Purpose | Auth |
|---|---|---|---|
| POST | `/api/auth/login` | exchange email/password for tokens | none |
| POST | `/api/auth/refresh` | rotate access/refresh tokens | refresh |
| GET | `/api/agents` | list agents w/ health | jwt |
| POST | `/api/traces/ingest` | ingest a single trace | api-key |
| POST | `/api/traces/ingest/bulk` | bulk ingest traces | api-key |
| GET | `/api/traces/metrics?agent_id&hours` | time-series metrics | jwt |
| GET | `/api/traces/cost-burn?days` | daily cost + projection | jwt |
| POST | `/api/evaluations/sets/{id}/run` | execute eval | jwt |
| POST | `/api/agents/{id}/promote` | promote w/ eval gate | jwt (admin) |
| GET | `/ws/traces?token=...` | live trace feed (WS) | jwt |

Full OpenAPI docs at `http://localhost:8000/docs`.

## SDK usage

```bash
pip install -e ./sdk
```

```python
import agentops

agentops.init(api_key="aop_…", agent_id="<agent-uuid>",
              base_url="http://localhost:8000", environment="prod")

with agentops.trace(input=user_msg) as t:
    answer = my_agent.run(user_msg)
    t.set_output(answer, tokens_input=420, tokens_output=85, cost_usd=0.0021)
    t.add_tool_call(name="web_search", input={"q": user_msg},
                    output={"hits": 5}, latency_ms=230)
```

Async:

```python
async with agentops.trace(input=q) as t:
    ans = await my_agent.arun(q)
    t.set_output(ans)
```

Exceptions inside the block automatically mark the trace `status=error`. Traces are batched and flushed in the background; if the API is unreachable they are written to a disk buffer (`/tmp/agentops_sdk/`) and replayed on the next process start.

## Development

```bash
# backend tests
docker-compose run --rm backend pytest

# frontend dev (without Docker)
cd frontend && npm install && npm run dev

# rerun seed
docker-compose run --rm backend python -m app.seed
```

## Demo accounts

- **Owner**: `demo@agentops.dev` / `Demo1234!`
- **Developer**: `dev@agentops.dev` / `Dev1234!`
- **Viewer**: `viewer@agentops.dev` / `View1234!`

The seed script also prints a fresh API key on first run — grep the backend logs for `seed: demo API key =`.
