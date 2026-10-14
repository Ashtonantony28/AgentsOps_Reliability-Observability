"""Hand-written ASTs for every fixture policy body, so the encoder is tested independently of the parser."""
import re

from app.policy.ast import (And, Count, Formula, Historically, Implies, Match, Not, Once, Or, Policy, Previously,
                            Since)
from tests.policy.fixtures import Fixture


def tool(*globs):
    return Match(kind="tool", globs=globs)


def event(kind=None, *globs):
    return Match(kind=kind, globs=globs)


def status(s):
    return Match(status=s)


def attr(k, v):
    return Match(attrs=((k, v),))


CONFIRMATION = event("confirmation")
SECRET_READ = tool("vault.read", "read_secret*")
NETWORK = tool("http_*", "fetch_url", "send_email")

BODIES: dict[str, Formula] = {
    '@guarded -> previously event("confirmation")':
        Implies(And(tool("delete_*", "drop_*"), attr("env", "prod")), Previously(CONFIRMATION)),
    "@network -> not once @secret_read": Implies(NETWORK, Not(Once(SECRET_READ))),
    'attr("a", 1) -> attr("b", 1) -> attr("c", 1)': Implies(attr("a", 1), Implies(attr("b", 1), attr("c", 1))),
    'attr("env", "prod") -> once (event("confirmation") and attr("approver_level", 2))':
        Implies(attr("env", "prod"), Once(And(CONFIRMATION, attr("approver_level", 2)))),
    'attr("pii", true) -> attr("redacted", true)': Implies(attr("pii", True), attr("redacted", True)),
    'count(event("llm")) < 3': Count(event("llm"), "<", 3),
    'count(event("llm")) >= 1': Count(event("llm"), ">=", 1),
    'count(event("llm", "plan")) == 1': Count(event("llm", "plan"), "==", 1),
    'count(status("error")) by kind < 2': Count(status("error"), "<", 2, by="kind"),
    'count(tool(*) and status("error")) by name <= 3': Count(And(tool(), status("error")), "<=", 3, by="name"),
    "count(tool(*)) != 2": Count(tool(), "!=", 2),
    'event("custom", "answer") -> once (event("llm") or event("retrieval"))':
        Implies(event("custom", "answer"), Once(Or(event("llm"), event("retrieval")))),
    'event(*) -> not status("error")': Implies(event(), Not(status("error"))),
    'not (tool("x") or tool("y") and status("error"))': Not(Or(tool("x"), And(tool("y"), status("error")))),
    'not event("custom", "?launch")': Not(event("custom", "?launch")),
    'not event("llm", "caf?")': Not(event("llm", "caf?")),
    'not status("error")': Not(status("error")),
    'not tool("DELETE*")': Not(tool("DELETE*")),
    'not tool("drop*")': Not(tool("drop*")),
    'not tool("get_?")': Not(tool("get_?")),
    'not tool("löschen_*")': Not(tool("löschen_*")),
    'status("timeout") -> not previously status("timeout")':
        Implies(status("timeout"), Not(Previously(status("timeout")))),
    'tool("admin_*") -> not event("custom", "logout") since event("custom", "login")':
        Implies(tool("admin_*"), Since(Not(event("custom", "logout")), event("custom", "login"))),
    'tool("charge_*") -> count(event("confirmation")) > 1': Implies(tool("charge_*"), Count(CONFIRMATION, ">", 1)),
    'tool("commit") -> historically not status("error")': Implies(tool("commit"), Historically(Not(status("error")))),
    'tool("delete_*") -> once event("confirmation")': Implies(tool("delete_*"), Once(CONFIRMATION)),
    'tool("deploy*") -> previously event("llm", "plan*")': Implies(tool("deploy*"), Previously(event("llm", "plan*"))),
}

_POLICY = re.compile(r"^policy\s+(\w+)(?:\s+severity\s+(\w+))?:\s*\n\s*always (.+)$", re.M)


def fixture_policies(fx: Fixture) -> list[Policy]:
    return [Policy(name, sev or "medium", BODIES[body.strip()]) for name, sev, body in _POLICY.findall(fx.source)]
