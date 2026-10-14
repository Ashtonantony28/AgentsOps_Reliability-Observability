"""AUDIT-5.md SDK findings as failing tests; each is xfail(strict=True) until its sub-goal fixes it."""
import sys
import threading

import pytest

from agentops_sdk.trace import Trace


# A5-6: add_event reads len(events) for the seq and appends later, with no lock between
@pytest.mark.xfail(strict=True, reason="A5-6: concurrent add_event calls give duplicate and out-of-order seqs")
def test_concurrent_add_event_keeps_seqs_contiguous():
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        t = Trace(agent_id="a")
        threads = [threading.Thread(target=lambda: [t.add_event("tool", "x") for _ in range(2000)]) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    finally:
        sys.setswitchinterval(old)
    assert [e["seq"] for e in t.events] == list(range(16000))
