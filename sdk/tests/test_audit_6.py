"""AUDIT-6.md SDK findings as failing tests; each is xfail(strict=True) until its sub-goal fixes it."""
import os
import stat
import uuid

import pytest

from agentops_sdk.client import AgentOpsClient
from agentops_sdk.utils import append_to_disk, disk_buffer_path, drain_disk

MAX_EVENTS_PER_REQUEST = 10_000  # backend TraceBulkIn limit (app/schemas/trace.py MAX_TRACE_EVENTS)


def _payload(events: int) -> dict:
    return {"trace_id": str(uuid.uuid4()), "agent_id": "a1", "events": [
        {"seq": i, "kind": "tool", "name": "t"} for i in range(events)]}


# A6-1: close() and the atexit hook send at most batch_size traces; the rest are dropped
@pytest.mark.xfail(strict=True, reason="A6-1: close() flushes one batch and drops the rest of the queue")
def test_close_delivers_every_queued_trace(server):
    c = AgentOpsClient(api_key=f"t{uuid.uuid4().hex}", agent_id="a1", base_url="http://test",
                       flush_interval_s=3600, batch_size=2)
    c._thread.join = lambda timeout=None: None  # the flusher sleeps for an hour; don't wait for it
    for _ in range(5):
        c.enqueue(_payload(1))
    c.close()
    assert server.bodies("bulk")  # control
    sent = sum(len(b["traces"]) for b in server.bodies("bulk"))
    assert sent + len(drain_disk(c._disk)) == 5  # sent, or buffered for the next run


# A6-2: a batch whose traces hold more than 10 000 events in total is a 422 for the whole batch, every retry
@pytest.mark.xfail(strict=True, reason="A6-2: the SDK batches by trace count only; the server caps events per request")
def test_bulk_requests_stay_under_the_server_event_cap(client, server):
    for _ in range(3):
        client.enqueue(_payload(4_000))
    client.flush()
    bodies = server.bodies("bulk")
    assert sum(len(b["traces"]) for b in bodies) == 3
    assert all(sum(len(t["events"]) for t in b["traces"]) <= MAX_EVENTS_PER_REQUEST for b in bodies)


# A6-3: after the server rejects a streamed event as policy_block, later checks of the trace allow everything
@pytest.mark.xfail(strict=True, reason="A6-3: a dead stream turns every later check into a fail-open allow")
def test_check_after_policy_block_rejection_is_not_allowed(client, server):
    server.queue("events", (403, {"error": {"code": "policy_block", "message": "blocked"}}))
    server.queue("check", (200, {"allowed": False, "violations": [{"policy_id": "p1", "policy_name": "p"}]}))
    with client.trace(stream=True) as t:
        t.add_event("tool", "delete_user")  # done without a guard; the server refuses to record it
        result = t.check("delete_account")
    assert t._dead  # control
    assert not result.allowed


# A6-4: 401, 404 and 422 from /check are configuration errors, not outages, but fail open like a timeout
@pytest.mark.xfail(strict=True, reason="A6-4: client errors on /check fail open under the default config")
@pytest.mark.parametrize("reply", [
    (401, {"error": {"code": "http_error", "message": "invalid_api_key"}}),
    (404, {"error": {"code": "http_error", "message": "agent_not_found"}}),
    (422, {"error": {"code": "validation_error"}}),
])
def test_check_client_errors_do_not_allow(client, server, reply):
    server.queue("check", reply)
    result = client.check("delete_user")
    assert result.error == f"http {reply[0]}"  # control
    assert not result.allowed


# A6-5: undelivered traces, with raw input and output, sit in a world-readable file in the shared temp dir
@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
@pytest.mark.xfail(strict=True, reason="A6-5: disk buffer is created with the process umask in a shared directory")
def test_disk_buffer_is_private():
    old = os.umask(0o022)
    try:
        path = disk_buffer_path(f"t{uuid.uuid4().hex}")
        append_to_disk(path, {"input": "ssn 123-45-6789"})
        mode = stat.S_IMODE(os.stat(path).st_mode)
    finally:
        os.umask(old)
        drain_disk(path)
    assert mode & 0o077 == 0, oct(mode)
