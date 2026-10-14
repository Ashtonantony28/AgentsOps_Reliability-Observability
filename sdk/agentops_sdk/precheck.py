"""Policy pre-checks and the event wire format shared by both clients."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import httpx

log = logging.getLogger("agentops_sdk")

EVENT_KINDS = ("tool", "llm", "retrieval", "confirmation", "custom")


@dataclass
class CheckResult:
    allowed: bool
    violations: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None  # set only when the check itself failed


class PolicyBlocked(Exception):
    def __init__(self, result: CheckResult):
        self.result = result
        names = sorted({str(v.get("policy_name") or v.get("policy_id")) for v in result.violations})
        super().__init__(f"blocked by policy: {', '.join(names)}" if names
                         else f"blocked: policy check failed ({result.error})")


def event_dict(seq: int, kind: str, name: str, status: str = "success", input: Optional[dict] = None,
               output: Optional[dict] = None, attrs: Optional[dict] = None, *,
               latency_ms: int = 0, timestamp: Optional[str] = None) -> dict:
    if kind not in EVENT_KINDS:
        raise ValueError(f"event kind must be one of {EVENT_KINDS}, got {kind!r}")
    return {"seq": seq, "kind": kind, "name": name, "status": status, "input": input or {},
            "output": output or {}, "attrs": attrs or {}, "latency_ms": int(latency_ms), "timestamp": timestamp}


def check_request(agent_id: str, trace_uid: Optional[str], candidate: dict,
                  prefix: Optional[List[dict]] = None, environment: Optional[str] = None) -> dict:
    return {"agent_id": agent_id, "trace_uid": trace_uid, "environment": environment, "prefix": prefix or [],
            "candidate": candidate}


def events_request(agent_id: str, events: List[dict], final: bool, **fields) -> dict:
    """`fields` are the trace-level fields of the append (environment, input, output, status, ...)."""
    return {"agent_id": agent_id, "events": events, "final": final, **fields}


# the server answered but rejected the key, the agent or the request: a configuration error no retry fixes
CONFIG_ERRORS = (401, 403, 404, 422)


def failed_result(reason: str, fail_closed: bool) -> CheckResult:
    log.warning("agentops policy check failed (%s); %s", reason, "blocking" if fail_closed else "allowing")
    return CheckResult(allowed=not fail_closed, error=reason)


def check_result(outcome: Union[httpx.Response, BaseException], fail_closed: bool) -> CheckResult:
    """Turns a /trace-policies/check response, or the exception raised sending it, into a CheckResult."""
    if isinstance(outcome, httpx.TimeoutException):
        return failed_result("timeout", fail_closed)
    if isinstance(outcome, BaseException):
        return failed_result(f"{type(outcome).__name__}: {outcome}"[:200], fail_closed)
    if outcome.status_code in CONFIG_ERRORS:
        log.error("agentops policy check rejected (http %s: %s); blocking", outcome.status_code, outcome.text[:200])
        return CheckResult(allowed=False, error=f"http {outcome.status_code}")
    if not outcome.is_success:
        return failed_result(f"http {outcome.status_code}", fail_closed)
    try:
        body = outcome.json()
        allowed, violations = body["allowed"], body.get("violations") or []
        if not isinstance(allowed, bool) or not isinstance(violations, list):
            raise TypeError
    except Exception:
        return failed_result("malformed response", fail_closed)
    return CheckResult(allowed=allowed, violations=violations)


def _error(r: httpx.Response) -> dict:
    try:
        err = r.json().get("error")
    except Exception:
        return {}
    return err if isinstance(err, dict) else {"code": err}


def push_outcome(outcome: Union[httpx.Response, BaseException]) -> Union[str, int]:
    """'ok', 'retry' (keep pending events), 'refused' (a policy blocked the batch; the trace goes on without it),
    'fatal' (drop the batch and stop streaming the trace, also when a policy blocked the trace's text), or the server's expected seq on a seq conflict,
    so a batch stored before a lost reply can be resynced."""
    if isinstance(outcome, BaseException):
        return "retry"
    if outcome.is_success:
        return "ok"
    if outcome.status_code >= 500 or outcome.status_code == 429:
        return "retry"
    err = _error(outcome) if outcome.status_code in (403, 409) else {}
    if outcome.status_code == 403 and err.get("code") == "policy_block":
        log.warning("agentops events refused by policy: %s", err.get("message"))
        # a block on the trace's input/output text refuses the trace, not an event
        return "fatal" if (err.get("details") or {}).get("target") == "trace" else "refused"
    if err.get("code") == "trace_busy":
        return "retry"
    expected = (err.get("details") or {}).get("expected_seq") if err.get("code") == "seq_conflict" else None
    if isinstance(expected, int) and not isinstance(expected, bool):
        return expected
    log.warning("agentops event stream rejected: %s %s", outcome.status_code, outcome.text[:200])
    return "fatal"


class PrecheckMixin:
    """HTTP transport for pre-checks and event streaming; needs api_key, agent_id, base_url,
    fail_closed and check_timeout_s on the instance."""

    is_async = False

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key}

    def _check_body(self, name: str, kind: str, input: Optional[dict], attrs: dict) -> dict:
        return check_request(self.agent_id, None, event_dict(0, kind, name, input=input, attrs=attrs),
                             environment=self.environment)

    def check(self, name: str, *, kind: str = "tool", input: Optional[dict] = None, **attrs) -> CheckResult:
        return self._post_check(self._check_body(name, kind, input, attrs))

    async def acheck(self, name: str, *, kind: str = "tool", input: Optional[dict] = None, **attrs) -> CheckResult:
        return await self._apost_check(self._check_body(name, kind, input, attrs))

    def _post_check(self, body: dict) -> CheckResult:
        try:
            with httpx.Client(timeout=self.check_timeout_s) as c:
                r = c.post(f"{self.base_url}/api/trace-policies/check", json=body, headers=self._headers())
        except Exception as e:
            return check_result(e, self.fail_closed)
        return check_result(r, self.fail_closed)

    async def _apost_check(self, body: dict) -> CheckResult:
        try:
            async with httpx.AsyncClient(timeout=self.check_timeout_s) as c:
                r = await c.post(f"{self.base_url}/api/trace-policies/check", json=body, headers=self._headers())
        except Exception as e:
            return check_result(e, self.fail_closed)
        return check_result(r, self.fail_closed)

    def _events_url(self, trace_uid: str) -> str:
        return f"{self.base_url}/api/traces/{quote(trace_uid, safe='')}/events"

    def _send_events(self, trace_uid: str, body: dict) -> Union[str, int]:
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.post(self._events_url(trace_uid), json=body, headers=self._headers())
        except Exception as e:
            log.warning("agentops event stream error, will retry: %s", e)
            return push_outcome(e)
        return push_outcome(r)

    async def _asend_events(self, trace_uid: str, body: dict) -> Union[str, int]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(self._events_url(trace_uid), json=body, headers=self._headers())
        except Exception as e:
            log.warning("agentops event stream error, will retry: %s", e)
            return push_outcome(e)
        return push_outcome(r)
