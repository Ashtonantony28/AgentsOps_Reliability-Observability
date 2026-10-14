"""AUDIT-6 surviving mutants (A6-16), replayed like AUDIT-4's: python backend/scripts/mutants_audit6.py [A6-16]"""
import sys

import mutants_audit4 as m

m.MUTANTS = [
    ("A6-16", "sdk/agentops_sdk/async_client.py", "                pass\n            await self.flush()\n",
     "                pass\n", "sdk", m.SDK_TESTS),
    ("A6-16", "sdk/agentops_sdk/precheck.py", "/api/traces/{quote(trace_uid, safe='')}/events",
     "/api/traces/{trace_uid}/events", "sdk", m.SDK_TESTS),
    ("A6-16", "sdk/agentops_sdk/trace.py", "self._sent = max(self._sent, start + n)", "self._sent = start + n",
     "sdk", m.SDK_TESTS),
    ("A6-16", "backend/app/routers/ws.py", "            if ak and not ak.revoked_at:", "            if ak:",
     "backend", m.BACKEND_TESTS),
    ("A6-16", "backend/app/routers/ws.py",
     "                    except TraceConflict as tc:\n                        await db.rollback()\n",
     "                    except TraceConflict as tc:\n", "backend", m.BACKEND_TESTS),
    ("A6-16", "backend/app/services/trace_service.py",
     "    environment = agent.environment if trace is None else trace.environment",
     "    environment = agent.environment", "backend", m.BACKEND_TESTS),
]

if __name__ == "__main__":
    sys.exit(m.main())
