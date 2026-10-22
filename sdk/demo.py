"""Demo script — instrument a fake agent and stream traces to a local AgentOps instance.

Usage:
    AGENTOPS_API_KEY=aop_xxx AGENT_ID=<uuid> python sdk/demo.py
"""
import os
import random
import time

import agentops_sdk as agentops


def fake_agent(question: str) -> str:
    time.sleep(random.uniform(0.05, 0.4))
    if random.random() < 0.08:
        raise RuntimeError("upstream timeout")
    return f"Answer: {question[::-1]}"


def main():
    api_key = os.environ.get("AGENTOPS_API_KEY", "aop_demo_key")
    agent_id = os.environ.get("AGENT_ID", "demo-agent-id")
    base_url = os.environ.get("AGENTOPS_BASE_URL", "http://localhost:8000")
    agentops.init(api_key=api_key, agent_id=agent_id, base_url=base_url,
                  environment="prod", agent_version="1.0.0")

    questions = [
        "What is our return policy?",
        "How do I reset my password?",
        "Is there a discount for annual plans?",
        "Where can I download my invoices?",
        "Tell me about feature X.",
    ]
    for i, q in enumerate(questions * 3):
        try:
            with agentops.trace(input=q, session_id=f"demo-{i // 5}") as t:
                t.add_tool_call(name="kb_lookup", input={"query": q},
                                output={"hits": random.randint(0, 5)}, latency_ms=random.randint(20, 150))
                ans = fake_agent(q)
                t.set_output(ans, tokens_input=len(q) // 4, tokens_output=len(ans) // 4,
                             cost_usd=round(random.random() * 0.01, 5))
        except Exception as e:
            # The context manager already recorded the error; we just keep going.
            print(f"agent error: {e}")

    agentops.flush()
    print("demo: done")


if __name__ == "__main__":
    main()
