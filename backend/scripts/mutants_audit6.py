"""AUDIT-6 surviving mutants (A6-16), replayed like AUDIT-4's: python backend/scripts/mutants_audit6.py [A6-16]"""
import sys

import mutants_audit4 as m

# The sixth survivor, /ws/ingest without a rollback after a TraceConflict, is gone with the code it mutated: each
# message has its own session since SG-A6.4 (test_ws_events_see_writes_made_elsewhere fails on the old handler).

m.MUTANTS = [
    ("A6-16", "sdk/agentops_sdk/async_client.py", "                pass\n            await self.flush()\n",
     "                pass\n", "sdk", m.SDK_TESTS),
    ("A6-16", "sdk/agentops_sdk/precheck.py", "/api/traces/{quote(trace_uid, safe='')}/events",
     "/api/traces/{trace_uid}/events", "sdk", m.SDK_TESTS),
    ("A6-16", "sdk/agentops_sdk/trace.py", "self._sent = max(self._sent, start + n)", "self._sent = start + n",
     "sdk", m.SDK_TESTS),
    ("A6-16", "backend/app/routers/ws.py", "            if ak and not ak.revoked_at:", "            if ak:",
     "backend", m.BACKEND_TESTS),
    ("A6-16", "backend/app/services/trace_service.py",
     "    environment = (req.environment or agent.environment) if trace is None else trace.environment\n"
     "    policies = await tps.active_policies(db, org_id, agent.id, environment)\n    compiled =",
     "    environment = req.environment or agent.environment\n"
     "    policies = await tps.active_policies(db, org_id, agent.id, environment)\n    compiled =",
     "backend", m.BACKEND_TESTS),
]

if __name__ == "__main__":
    sys.exit(m.main())
