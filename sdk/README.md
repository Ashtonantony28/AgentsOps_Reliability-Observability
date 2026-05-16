# AgentOps SDK (Python)

Instrument any Python AI agent in 3 lines:

```python
import agentops

agentops.init(api_key="aop_...", agent_id="agent-uuid", environment="prod",
              base_url="http://localhost:8000")

with agentops.trace(input=user_input) as t:
    result = my_agent.run(user_input)
    t.set_output(result, tokens_input=420, tokens_output=85, cost_usd=0.0021)
    t.add_tool_call(name="web_search", input={"q": user_input}, output={"hits": 5}, latency_ms=230)
```

## Async usage

```python
import agentops
agentops.init(api_key="aop_...", agent_id="agent-uuid")

async with agentops.trace(input=q) as t:
    result = await my_agent.arun(q)
    t.set_output(result)
```

## Features

- Context manager (sync + async)
- Auto-capture exceptions → marks trace status = error
- Background batched flush (configurable interval, batch size)
- Local disk buffer if the API is unreachable; auto-replay on next start
- Bulk ingestion uses `/api/traces/ingest/bulk` for efficiency

## Install

```bash
pip install -e ./sdk
```

## Demo

`python sdk/demo.py` runs against a local API at `http://localhost:8000`.
