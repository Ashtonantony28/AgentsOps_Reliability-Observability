"""AUDIT-14 mutation probe of the round-5 fixes (PRs #52, #53, #54, #55), replayed like AUDIT-4's:
python backend/scripts/mutants_audit14.py [probe | A14-3]. The "probe" rows were killed when AUDIT-14 ran; the A14-3
rows survived it (AUDIT-14.md A14-3). Each mutant runs against the tests of the code it changes, not the whole suite."""
import sys

import mutants_audit4 as m

# AUDIT-14's own xfail tests are left out: a mutant that makes one XPASS is not killed by the suite
_X = ["--deselect=tests/audit/test_audit_14.py::test_check_of_a_stored_trace_resumes_from_a_rebuild_still_running",
      "--deselect=tests/audit/test_audit_14.py::test_a_checks_rebuild_stops_once_its_request_is_gone"]
POOL = ("backend", ["tests/api/test_policy_work.py", "tests/audit/test_audit_11.py", "tests/audit/test_audit_12.py",
                    "tests/audit/test_audit_14.py", *_X])
BATCH = ("backend", ["tests/api/test_violation_pipeline.py", "tests/api/test_idle_streams.py",
                     "tests/api/test_event_ingest.py", "tests/audit/test_audit_11.py"])
COMPILE = ("backend", ["tests/api/test_policy_work.py", "tests/api/test_trace_policies.py",
                       "tests/audit/test_audit_11.py", "tests/policy/test_dsl.py"])
S = ("sdk", ["tests"])
TPS = "backend/app/services/trace_policy_service.py"
TS = "backend/app/services/trace_service.py"
PT = "backend/app/tasks/policy_tasks.py"
TP = "backend/app/routers/trace_policies.py"
TR = "sdk/agentops_sdk/trace.py"

m.MUTANTS = [
    # SG-A11.1: pool slices and rebuilds in turn by org (#53)
    ("A14", TPS, "            if self._waiting:\n                fut = self._queue(org_id)",
     "            if False:\n                fut = self._queue(org_id)", *POOL),
    ("A14", TPS, "                self._waiting[org_id] = queue  # to the back of the turn",
     "                self._waiting = {org_id: queue, **self._waiting}", *POOL),
    ("A14", TPS, "            if not fut.cancelled():  # handed a slot as it was cancelled", "            if False:", *POOL),
    ("A14", TPS, "            self._free -= 1\n", "            pass\n", *POOL),
    ("A14", TPS, "    async with slots(org_id) as turn:", '    async with slots("") as turn:', *POOL),
    ("A14", TPS, "            async with _fair()[0](self.org_id):", '            async with _fair()[0](""):', *POOL),
    ("A14", TPS, "    async with _fair()[1](org_id):", '    async with _fair()[1](""):', *POOL),
    ("A14", TPS, "        time.sleep(0)\n", "", *POOL),
    # SG-A11.1: batch checks from per-org lists in turn (#53)
    ("A14", PT, "if redis.call('llen', KEYS[2]) == 1 then", "if true then", *BATCH),
    ("A14", PT, "redis.call('rpush', KEYS[1], org) end", "redis.call('lpush', KEYS[1], org) end", *BATCH),
    ("A14", PT, "raise self.retry(exc=e, args=(trace_id,), countdown=60)", "raise self.retry(exc=e, countdown=60)",
     *BATCH),
    ("A14", PT, "        args = (trace_id,)\n", "        pass\n", *BATCH),
    ("A14", TS, "await enqueue_trace_check(trace_id, orgs[trace_id])", 'await enqueue_trace_check(trace_id, "")',
     *BATCH),
    ("A14", TS, "    if policies:\n        await enqueue_trace_check(trace.id, trace.org_id)",
     '    if policies:\n        await enqueue_trace_check(trace.id, "")', *BATCH),
    # SG-A11.2: compile off the loop, cached, unused tags charged (#52)
    ("A14", TPS, "    if all(k in _compiled for k in keys):", "    if True:", *COMPILE),
    ("A14", TPS, "            _compiled.move_to_end(key)\n", "", *COMPILE),
    ("A14", TPS, "            _compiled.popitem(last=False)", "            _compiled.popitem()", *COMPILE),
    ("A14", TPS, "        c = None\n", "        return None\n", *COMPILE),
    ("A14", TPS, "    return max(plan_for(p).cost, p.parsed)", "    return plan_for(p).cost", *COMPILE),
    ("A14", TP, "        return await asyncio.to_thread(parse_single, source)", "        return parse_single(source)",
     *COMPILE),
    # SG-A12.1: waiting, takeover, request-gone stop and its 503 (#55); AUDIT-12's mutants_audit12 A12-1 rows too
    ("A14", TPS, "time.monotonic() - s.moved > SLICE_WAIT_S", "time.monotonic() - s.moved >= 0", *POOL),
    ("A14", TPS, "            if self.running.get(key) is s:\n                del self.running[key]",
     "            pass", *POOL),
    ("A14", TPS, "                await asyncio.wait_for(s.done.wait(), SLICE_WAIT_S)",
     "                await s.done.wait()", *POOL),
    ("A14", TP, 'raise HTTPException(503, detail={"error": "check_timeout"',
     'raise HTTPException(500, detail={"error": "check_timeout"', *POOL),
    ("A14", TP, "return await precheck(db, org.id, req, request.is_disconnected)",
     "return await precheck(db, org.id, req)", *POOL),
    # SDK: 503 check_timeout follows fail_closed (#55); asyncio locks per loop (#54)
    ("A14", "sdk/agentops_sdk/precheck.py", "CONFIG_ERRORS = (401, 403, 404, 413, 422)",
     "CONFIG_ERRORS = (401, 403, 404, 413, 422, 503)", *S),
    ("A14", TR, "    lock = locks.get(loop)", "    lock = next(iter(locks.values()), None)", *S),
    ("A14", TR, "        async with loop_lock(self._alocks):", "        async with asyncio.Lock():", *S),
    ("A14", "sdk/agentops_sdk/guard.py", "    async with loop_lock(t._guard_alocks):", "    async with asyncio.Lock():",
     *S),
]

if __name__ == "__main__":
    sys.exit(m.main())
