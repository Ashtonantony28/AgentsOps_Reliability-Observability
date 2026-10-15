"""AUDIT-10 mutation probe of the round-3 API and SDK fixes (PRs #39, #41, #42, #43), replayed like AUDIT-4's:
python backend/scripts/mutants_audit10.py [probe | A10-7]. The "probe" rows were killed when AUDIT-10 ran; the
A10-7 rows survived it (AUDIT-10.md A10-7); the SDK ones are killed since SG-A10.3, which added its own."""
import sys

import mutants_audit4 as m

# AUDIT-10's own xfail tests are left out: a mutant that makes one XPASS is not killed by the suite
S = ("sdk", ["tests", "--ignore=tests/test_audit_10.py"])
# A10-8's wall-clock test is left out too: it fails without any mutant when the replays share the CPU
B = ("backend", ["tests/api", "tests/audit", "tests/test_audit2.py", "--ignore=tests/audit/test_audit_10.py",
                 "--deselect=tests/audit/test_audit_7.py::test_check_does_not_stall_the_event_loop"])
TS = "backend/app/services/trace_service.py"
TPS = "backend/app/services/trace_policy_service.py"
TP = "backend/app/routers/trace_policies.py"
TR = "sdk/agentops_sdk/trace.py"
G = "sdk/agentops_sdk/guard.py"

m.MUTANTS = [
    # SDK: 413 blocks, stopped-stream prefix (#39, #42); guard records at start, outcome at end (#41)
    ("probe", "sdk/agentops_sdk/precheck.py", "CONFIG_ERRORS = (401, 403, 404, 413, 422)",
     "CONFIG_ERRORS = (401, 403, 404, 422)", *S),
    ("probe", TR, "_bare(self.events[self._sent:]) if self._dead else []",
     "_bare(self.events[self._sent + 1:]) if self._dead else []", *S),
    ("probe", TR, "            pending = _has(self.events[self._sent:], ev) and not _has(self._sending, ev)",
     "            pending = _has(self.events[self._sent:], ev)", *S),
    ("probe", TR, "                self._sending = batch[1]", "                self._sending = []", *S),
    ("probe", TR, "        while hold and pending and", "        while False and pending and", *S),
    ("probe", TR, "_has(self._running, pending[-1]):", "_has(self._running, pending[0]):", *S),
    ("probe", TR, "            self._push(hold=True)\n            return", "            self._push()\n            return", *S),
    ("probe", TR, "create_task(self._apush(hold=True))", "create_task(self._apush())", *S),
    ("probe", TR, "        if pending:\n            self._schedule_push()", "        if False:\n            self._schedule_push()",
     *S),
    ("probe", TR, "            self._running = [e for e in self._running if e is not ev]",
     "            self._running = list(self._running)", *S),
    ("probe", G, "a, running=True)", "a, running=False)", *S),
    # SDK: one guard lock for sync and async calls, a proxy's 413 is an outage (SG-A10.3)
    ("probe", G, "        while not t._guard_lock.acquire(blocking=False):", "        while False:", *S),
    ("probe", G, "    if t is None or t._guard_owner == threading.get_ident():", "    if t is None:", *S),
    ("probe", "sdk/agentops_sdk/precheck.py", 'or _error(outcome).get("code") == "policy_work_too_large"):',
     "or True):", *S),
    # backend: per-request work bound (#42)
    # (rows rewritten to SG-A10.1/A10.2's bound: appends, a request's uploads, checkpointed checks)
    ("probe", TS, "_charge(blocking, len(events), room)", "_charge([], len(events), room)", *B),
    ("probe", TS, "_bound_append([c for _, c in policies], len(req.events))", "_bound_append([], len(req.events))", *B),
    ("probe", TS, "if events > 1 and (w := work(compiled, events)) > tps.MAX_REQUEST_WORK:",
     "if events > 1 and (w := work(compiled, events)) > 2 * tps.MAX_REQUEST_WORK:", *B),
    ("probe", TS, "        if w > room[0]:", "        if w > tps.MAX_REQUEST_WORK:", *B),
    ("probe", TPS, "            start = j * k\n", "            start = 0\n", *B),
    ("probe", "backend/app/routers/traces.py", 'rejected.append({"index": i, "reason": e.code, **e.details})',
     'rejected.append({"index": i, "reason": "invalid"})', *B),
    ("A10-7", "backend/app/routers/ws.py",
     '    except TraceConflict as tc:\n        return {"ok": False, "error": tc.code, "message": tc.message, **tc.details}\n'
     '    except Exception as e:\n        return {"ok": False, "error": str(e)}\n    return {"ok": True, "id": trace.id}',
     '    except Exception as e:\n        return {"ok": False, "error": str(e)}\n    return {"ok": True, "id": trace.id}',
     *B),
    ("probe", TP, "        raise HTTPException(e.status, detail=", "        raise HTTPException(409, detail=", *B),
    # backend: org policy caps (#42)
    ("probe", TP, "    if len(rows) >= tps.MAX_ORG_POLICIES:", "    if len(rows) > tps.MAX_ORG_POLICIES:", *B),
    ("probe", TP, "    if nodes > tps.MAX_ORG_POLICY_NODES:", "    if nodes > tps.MAX_ORG_POLICY_NODES + 1:", *B),
    ("probe", TP, "        await _check_org_limits(db, ctx.org_id, parsed, exclude_id=p.id)",
     "        await _check_org_limits(db, ctx.org_id, parsed)", *B),
    ("probe", TP, "                                                         TracePolicy.archived_at.is_(None))\n"
                  "    if exclude_id:", "                                                         True)\n    if exclude_id:", *B),
    # backend: reopen and re-tracking (#43), timeout outcome (#41)
    ("probe", TS, 'get("stream") not in ("open", "idle"):', 'get("stream") != "open":', *B),
    ("probe", TS, "            _reopen(trace)  # kept only", "            pass  # kept only", *B),
    ("probe", TPS, '    if trace is None or (trace.meta or {}).get("stream") != stream:  # reopened',
     "    if trace is None:  # reopened", *B),
    ("probe", TPS, "    try:\n        locked = await lock.acquire()\n    except Exception:",
     "    try:\n        locked = False\n    except Exception:", *B),
    ("probe", TS, "    if not await r.exists(SEEDED_KEY):", "    if False:", *B),
    ("probe", TS, "rows[i:i + chunk]}, nx=True)", "rows[i:i + chunk]}, nx=False)", *B),
    ("A10-7", TS, "{tid: min(now, last.replace(tzinfo=timezone.utc).timestamp())",
     "{tid: last.replace(tzinfo=timezone.utc).timestamp()", *B),
    ("probe", TS, '.where(Trace.meta["stream"].as_string() == "open")', '.where(Trace.meta["stream"].as_string() == "idle")',
     *B),
    ("probe", TS, "                trace.latency_ms = max(0, int((score - started) * 1000))",
     "                trace.latency_ms = 0", *B),
]

if __name__ == "__main__":
    sys.exit(m.main())
