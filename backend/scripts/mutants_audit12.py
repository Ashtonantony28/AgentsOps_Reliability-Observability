"""AUDIT-12 mutation probe of the round-4 API and SDK fixes (PRs #45, #47, #48, #49), replayed like AUDIT-4's:
python backend/scripts/mutants_audit12.py [probe | A12-8 | A12-1 | A13-1]. The "probe" rows were killed when AUDIT-12
ran; the A12-8 rows survived it (AUDIT-12.md A12-8); the A12-1 rows are SG-A12.1's and the A13-1 rows SG-A13.1's,
killed by their tests."""
import sys

import mutants_audit4 as m

# AUDIT-12's own xfail tests are left out: a mutant that makes one XPASS is not killed by the suite
S = ("sdk", ["tests", "--ignore=tests/test_audit_12.py"])
B = ("backend", ["tests/api", "tests/audit", "tests/test_audit2.py", "--ignore=tests/audit/test_audit_12.py"])
TS = "backend/app/services/trace_service.py"
TPS = "backend/app/services/trace_policy_service.py"
TR = "sdk/agentops_sdk/trace.py"
U = "sdk/agentops_sdk/utils.py"

m.MUTANTS = [
    # SDK: bulk resend of traces refused for policy work, appends split by max_events (#48)
    ("probe", U, "    if len(again) < len(items):", "    if True:", *S),
    ("probe", U, 'elif r.get("reason") == "policy_work_too_large":', "elif False:", *S),
    ("probe", "sdk/agentops_sdk/client.py", "return self._post(again) if again else True", "return True", *S),
    ("A12-8", "sdk/agentops_sdk/async_client.py", "return await self._post(again) if again else True", "return True",
     *S),
    ("probe", TR, "pending = pending[:self._max_batch]", "pending = pending", *S),
    ("probe", TR, "or bool(self._max_batch))", ")", *S),
    ("probe", "sdk/agentops_sdk/precheck.py", 'return "max_events", fit', 'return "fatal"', *S),
    # SDK: an async guard waits for a sync one's lock (#47)
    ("A12-8", "sdk/agentops_sdk/guard.py",
     "        while not t._guard_lock.acquire(blocking=False):\n            await asyncio.sleep(delay)\n",
     "        if not t._guard_lock.acquire(blocking=False):\n            await asyncio.sleep(delay)\n", *S),
    # backend: a bulk request's shared bound, the append's max_events (#48)
    ("probe", TS, "        room[0] -= w", "        pass", *B),
    ("probe", TS, "if events > 1 and (w := work", "if events > 0 and (w := work", *B),
    ("probe", TS, "fit = max(1, tps.MAX_REQUEST_WORK // work(compiled, 1))",
     "fit = max(1, tps.MAX_REQUEST_WORK // work(compiled, 1) - 1)", *B),
    ("probe", "backend/app/routers/traces.py", "room = [], [], [tps.MAX_REQUEST_WORK]", "room = [], [], None", *B),
    ("probe", TS, "blocked = _charge(blocking, len(events), room) and", "blocked = _charge(blocking, 0, room) and", *B),
    # backend: prefix checkpoints of /check (#48)
    ("probe", TPS, "    if len(prefix) < k:", "    if True:", *B),
    ("A12-8", TPS, "self.next_seq + slices * self.k)", "slices * self.k)", *B),
    ("A12-8", TPS, 'f"{org_id}|{trace_id}|{next_seq}|{plans}"', 'f"{org_id}|{plans}"', *B),
    ("A12-8", TPS, "json.dumps([e.kind, e.name, e.status, e.attrs]", "json.dumps([e.kind, e.name, e.attrs]", *B),
    ("A12-8", TPS, "json.dumps([e.kind, e.name, e.status, e.attrs]", "json.dumps([e.kind, e.name, e.status]", *B),
    ("probe", TPS, "redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)", "", *B),
    ("probe", TPS, "await asyncio.to_thread(_step_inputs, m, prefix[len(keys) * k:])", "None", *B),
    ("A12-8", TPS, "for i = #KEYS, 1, -1 do", "for i = 1, #KEYS do", *B),
    # SG-A12.1: a check resumes from, or waits for, what another check of the same prefix steps; it stops when gone
    ("A12-1", TPS, "if j < len(keys) and (s := self.running.get(keys[j])):", "if False:", *B),
    ("A12-1", TPS, "return s is None or s.moved is not None and", "return s is None and", *B),
    ("A12-1", TPS, "return time.monotonic() > deadline or (gone is not None and await gone())", "return False", *B),
    ("A12-1", TPS, "return time.monotonic() > deadline or", "return", *B),
    ("A12-1", TPS, "            s.done.set()", "            pass", *B),
    # SG-A13.1: a history keeps its newest checkpoint, an org's are bounded in bytes, a resumed check reads one; a
    # stored trace's precheck rebuild runs once at a time, stops with its check and is resumed by the next
    ("A13-1", TPS, "drop(KEYS[2])\n", "", *B),
    ("A13-1", TPS, "if size > max then return 0 end", "", *B),
    ("A13-1", TPS, "  drop(oldest)\n", "  break\n", *B),
    ("A13-1", TPS, "(await self.r.mget(keys[j + i - 1:j + i]))[0]", "(await self.r.mget(keys[j:]))[i - 1]", *B),
    ("A13-1", TPS, "compiled, next_seq, stopped):", "compiled, next_seq, lambda: asyncio.sleep(0, False)):", *B),
    ("A13-1", TPS, "    if not await lock.acquire():\n        return None", "    await lock.acquire()", *B),
    ("A13-1", TPS, "compiled, next_seq, True) or Monitor(compiled)", "compiled, next_seq) or Monitor(compiled)", *B),
    ("A13-1", TPS, "                await lock.reacquire()", "                pass", *B),
    ("A13-1", TPS, "        if m.next_seq < next_seq:\n            await _replay(",
     "        if True:\n            await _replay(", *B),
    # backend: long work sliced on its own pool (#48), busy batch retried (#49), org cap in cost units (#45)
    ("A12-8", TPS, "if cost * len(items) <= WORK_SLICE:", "if True:", *B),
    ("probe", TPS, "raise TraceBusy(trace_id)", "locked = False", *B),
    ("probe", "backend/app/tasks/policy_tasks.py", "raise self.retry(exc=e, countdown=60)", "return 0", *B),
    ("probe", "backend/app/routers/trace_policies.py", "nodes = plan_for(parsed).cost", "nodes = 0", *B),
    ("probe", "backend/app/routers/trace_policies.py", "nodes += plan_for(tps._compile(pid, source).policy).cost",
     "nodes += len(plan_for(tps._compile(pid, source).policy).nodes)", *B),
    # AUDIT-10's backend A10-7 survivors, re-checked
    ("A12-8", "backend/app/routers/ws.py",
     '    except TraceConflict as tc:\n        return {"ok": False, "error": tc.code, "message": tc.message, **tc.details}\n'
     '    except Exception as e:\n        return {"ok": False, "error": str(e)}\n    return {"ok": True, "id": trace.id}',
     '    except Exception as e:\n        return {"ok": False, "error": str(e)}\n    return {"ok": True, "id": trace.id}',
     *B),
    ("A12-8", TS, "{tid: min(now, last.replace(tzinfo=timezone.utc).timestamp())",
     "{tid: last.replace(tzinfo=timezone.utc).timestamp()", *B),
]

if __name__ == "__main__":
    sys.exit(m.main())
