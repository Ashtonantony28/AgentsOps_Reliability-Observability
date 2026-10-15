"""AUDIT-8 mutation probe of the round-2 fixes (PRs #24-#35), replayed like AUDIT-4's:
python backend/scripts/mutants_audit8.py [probe | A8-8]. The "probe" rows were killed when AUDIT-8 ran (SG-A8.2 added
its own); the A8-8 rows survived it (AUDIT-8.md A8-8) and are killed since SG-A8.2."""
import sys

import mutants_audit4 as m

S, B = ("sdk", m.SDK_TESTS), ("backend", m.BACKEND_TESTS)
TS = "backend/app/services/trace_service.py"
TPS = "backend/app/services/trace_policy_service.py"

m.MUTANTS = [
    # SDK
    ("probe", "sdk/agentops_sdk/precheck.py", "CONFIG_ERRORS = (401, 403, 404, 422)",
     "CONFIG_ERRORS = (401, 404, 422)", *S),
    ("probe", "sdk/agentops_sdk/utils.py", "    if st.st_uid != os.getuid():\n        return False\n", "", *S),
    ("probe", "sdk/agentops_sdk/utils.py", "        os.chmod(base, 0o700)", "        pass", *S),
    ("probe", "sdk/agentops_sdk/utils.py", "getattr(os, \"O_NOFOLLOW\", 0), 0o600)",
     "getattr(os, \"O_NOFOLLOW\", 0), 0o644)", *S),
    ("probe", "sdk/agentops_sdk/utils.py", "        if k > max_events:", "        if k >= max_events:", *S),
    ("probe", "sdk/agentops_sdk/utils.py", "len(out) >= size or n + k > max_events",
     "len(out) >= size or n + k >= max_events", *S),
    ("probe", "sdk/agentops_sdk/guard.py", "with t._guard_lock if t is not None else contextlib.nullcontext():",
     "with contextlib.nullcontext():", *S),
    ("probe", "sdk/agentops_sdk/guard.py", "                async with _alock(t):", "                async with _alock(None):",
     *S),
    ("probe", "sdk/agentops_sdk/trace.py", "            if any(e is ev for e in self.events[self._sent:]):",
     "            if True:", *S),
    ("probe", "sdk/agentops_sdk/trace.py", "if self._streaming() and not self.tool_calls:", "if self._streaming():",
     *S),
    ("probe", "backend/app/routers/ws.py", "if key is None or key.revoked_at:", "if key is None:", *B),
    ("probe", TS, '                trace.status = "timeout"', "                pass", *B),
    ("probe", "sdk/agentops_sdk/trace.py", "(not self._dead or self._client.fail_closed)", "not self._dead", *S),
    ("probe", "sdk/agentops_sdk/precheck.py",
     'return "fatal" if (err.get("details") or {}).get("target") == "trace" else "refused"',
     'return "refused"', *S),
    ("probe", "sdk/agentops_sdk/trace.py", "            self._refused += 1\n", "", *S),
    ("probe", "sdk/agentops_sdk/trace.py", "_bare(self.events[self._sent:]) if self._dead else []", "[]", *S),
    ("probe", "sdk/agentops_sdk/async_client.py", "        for batch in batches(items, self.batch_size):",
     "        for batch in [items] if items else []:", *S),
    # backend
    ("A8-8", TS, "            if score is None or score > cutoff:", "            if score is None:", *B),
    ("probe", TS, "            await tps.save_monitor(trace_id, None)\n        finally:", "        finally:", *B),
    ("probe", TS, "        if not await lock.acquire(blocking=False):\n            continue\n",
     "        await lock.acquire(blocking=False)\n", *B),
    ("probe", TS, "            await _track_stream(trace.id, True)",
     "            await _track_stream(trace.id, False)", *B),
    ("probe", TS, "            await _track_stream(trace.id, not req.final)",
     "            await _track_stream(trace.id, True)", *B),
    ("probe", TS, "replayed + pv.violations, \"live\")", "pv.violations, \"live\")", *B),
    ("probe", TS, 'trace.meta = {**req.metadata, "stream": trace.meta["stream"]}',
     "trace.meta = dict(req.metadata)", *B),
    ("probe", TS, "setattr(trace, side, mask_pii(text) if pii else text)", "setattr(trace, side, text)", *B),
    ("probe", TPS, "            TracePolicy.archived_at.is_(None),\n            or_(", "            or_(", *B),
    ("probe", TPS, "row.policy_version == versions[row.policy_id] and row.at_seq == v.at_seq",
     "row.policy_version == versions[row.policy_id]", *B),
    ("A8-8", TPS, "if v is not None and row.policy_version == versions[row.policy_id] and",
     "if v is not None and", *B),
    ("probe", "backend/app/routers/trace_policies.py", "    if not include_archived:\n        q = q.where(",
     "    if False:\n        q = q.where(", *B),
    ("probe", "backend/app/routers/trace_policies.py",
     "TracePolicy.name == name,\n                                     TracePolicy.archived_at.is_(None))",
     "TracePolicy.name == name)", *B),
    ("probe", "backend/app/routers/trace_policies.py",
     "TracePolicy.org_id == org_id,\n                                  TracePolicy.archived_at.is_(None)).with_for_update()",
     "TracePolicy.org_id == org_id).with_for_update()", *B),
    ("probe", "backend/app/services/report_service.py",
     "            EvaluationSet.org_id == org_id, EvaluationRun.created_at >= cutoff,",
     "            EvaluationRun.created_at >= cutoff,", *B),
    ("probe", "backend/app/routers/evaluations.py", "        EvaluationSet.org_id == ctx.org_id\n    )\n    a =",
     "        True\n    )\n    a =", *B),
    ("probe", "backend/app/routers/traces.py", "raise HTTPException(e.status, detail=",
     "raise HTTPException(409, detail=", *B),
    ("probe", "backend/app/policy/dsl.py", "MAX_FORMULA_DEPTH = 64 ", "MAX_FORMULA_DEPTH = 400 ", *B),
    ("probe", "backend/app/policy/dsl.py", "MAX_SOURCE_CHARS = 20_000", "MAX_SOURCE_CHARS = 200_000", *B),
]

if __name__ == "__main__":
    sys.exit(m.main())
