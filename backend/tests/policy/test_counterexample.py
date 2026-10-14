import ctypes
import itertools
import random
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import z3
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from app.policy.ast import And, Count, Implies, Match, Not, Once, Policy, Previously
from app.policy.checker import check_trace, compile_policy
from app.policy.counterexample import _Entailment, _nodes, minimal_counterexample, name_patterns
from app.policy.dsl import CMPS, KINDS, parse
from app.policy.encode import glob_regex
from app.policy.events import Event
from app.policy.reference import truth
from tests.policy.fixture_policies import event, fixture_policies, tool
from tests.policy.fixtures import load_fixtures
from tests.policy.strategies import formulas, traces, vocab_atoms

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import bench_policy  # noqa: E402

FIXTURES = load_fixtures()
CASES = [(fx, x) for fx in FIXTURES for x in fx.expected]


def ev(i, name="x", kind="tool", st="success", **attrs):
    return Event(i, kind, name, st, attrs)


def cx(formula, events, at=None):
    return minimal_counterexample(compile_policy("p", Policy("p", "high", formula)), events,
                                  events[-1].seq if at is None else at)


def decode(strings, value):
    n = z3.Z3_get_string_length(value.ctx_ref(), value.as_ast())
    points = (ctypes.c_uint * n)()
    z3.Z3_get_string_contents(value.ctx_ref(), value.as_ast(), n, points)
    char = {v: c for c, v in strings.code.items()}
    # code points outside the map stand for characters no glob mentions; any unused one will do
    return "".join(char.get(p, chr(0xF0000 + p % 0xFFFD)) for p in points)


def witness(formula, events, known):
    """Events that keep the facts of `known` and make `formula` true at the last one, decoded from a solver model."""
    check = _Entailment(formula, events)
    if check.core(known) is not None:
        return None
    enc, model = check.encoding, check.solver.model()
    values = {d.name(): model[d] for d in model.decls()}

    def code(var, i):
        v = values.get(f"ev_{var}{i}")
        return v.as_long() if v is not None else 0

    out = []
    for i, e in enumerate(events):
        if i in known:
            out.append(e)
            continue
        s = code("s", i)
        status = enc.statuses[s] if s < len(enc.statuses) else "\0other"
        attrs = {}
        for j, (key, consts) in enumerate(enc.attrs.items()):
            if (c := code(f"v{j}_", i)) < len(consts):
                attrs[key] = list(consts.values())[c]
        if enc.name_mode == "string":
            name = decode(enc.strings, values[f"ev_n{i}"]) if f"ev_n{i}" in values else ""
        elif enc.name_mode == "code":
            names = list(enc.name_codes)
            n = code("n", i)
            name = names[n] if n < len(names) else f"\0fresh{n}"
        elif enc.name_mode == "pattern":
            name = name_patterns(enc.globs)[code("n", i)][1]
        else:
            name = "\0any"
        out.append(Event(e.seq, KINDS[code("k", i)], name, status, attrs))
    return out


def completions(formula, events, known, rng, n=40):
    """Random events in place of the unknown ones, drawn from the trace and from events that satisfy each atom."""
    pool = [(e.kind, e.name, e.status, dict(e.attrs)) for e in events]
    for m in _nodes(formula):
        if isinstance(m, Match):
            name = (m.globs[0].replace("*", "").replace("?", "a")) if m.globs else "x"
            pool.append((m.kind or rng.choice(KINDS), name, m.status or "success", dict(m.attrs)))
    pool += [(k, "zz", "other", {}) for k in KINDS]
    for _ in range(n):
        yield [e if i in known else Event(e.seq, *rng.choice(pool)) for i, e in enumerate(events)]


@pytest.mark.parametrize("fx,x", CASES, ids=[f"{fx.name}:{x.policy}" for fx, x in CASES])
def test_fixture_counterexamples(fx, x):
    p = next(p for p in fixture_policies(fx) if p.name == x.policy)
    assert minimal_counterexample(compile_policy("id", p), fx.events, x.at_seq) == x.counterexample


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_check_trace_reports_counterexamples(fx):
    got = check_trace([compile_policy(p.name, p) for p in fixture_policies(fx)], fx.events)
    assert sorted((v.policy_name, v.at_seq, v.counterexample) for v in got) == \
        sorted((x.policy, x.at_seq, x.counterexample) for x in fx.expected)


def test_doc_examples():
    src = """
    policy confirm_before_delete severity high:
      always tool("delete_*") -> once event("confirmation")
    policy no_exfil_after_secret severity critical:
      always tool("http_*", "fetch_url", "send_email") -> not once tool("vault.read", "read_secret*")
    policy charge severity high:
      always tool("charge_*") -> count(event("confirmation")) > 1
    """
    confirm, exfil, charge = (compile_policy(p.name, p) for p in parse(src))
    llm = [ev(0, "plan", "llm"), ev(1, "search"), ev(2, "answer", "llm")]
    assert minimal_counterexample(confirm, llm + [ev(3, "delete_user")], 3) == (0, 1, 2, 3)
    trace = [ev(0, "plan", "llm"), ev(1, "vault.read"), ev(2, "search"), ev(3, "think", "llm"), ev(4, "fetch_url")]
    assert minimal_counterexample(exfil, trace, 4) == (1, 4)
    assert minimal_counterexample(charge, [ev(0, "ok", "confirmation"), ev(1, "charge_card")], 1) == (1,)


@pytest.mark.parametrize("formula,events,expected", [
    # kinds are exclusive: no event is both a tool and an llm call, so earlier events are irrelevant
    (Implies(tool("x"), Once(And(Match(kind="tool"), Match(kind="llm")))), [ev(0, "a"), ev(1, "b"), ev(2, "x")], (2,)),
    # "ab" matches "a*" itself
    (Implies(tool("ab"), Not(Once(tool("a*")))), [ev(0, "a1"), ev(1, "ab")], (1,)),
    # a tool can be no confirmation, whatever its name
    (Implies(tool("x"), Once(event("confirmation"))), [ev(0, "y"), ev(1, "x")], (0, 1)),
    (Count(Match(kind="tool"), ">", 5), [ev(0)], ()),
    (Previously(tool("plan")), [ev(0, "deploy")], ()),
    (Previously(tool("plan")), [ev(0, "deploy"), ev(1, "deploy")], (0,)),
    (Count(tool("a*"), "<=", 1, by="name"), [ev(0, "ab"), ev(1, "b"), ev(2, "ab")], (0, 2)),
    (Count(Match(kind="tool"), "<=", 1, by="name"), [ev(0, "ab"), ev(1, "b"), ev(2, "ab")], (0, 2)),
    (Count(Match(kind="tool"), "<=", 1, by="kind"), [ev(0, "ab"), ev(1, "b", "llm"), ev(2, "c")], (0, 2)),
    (Not(tool("🚀*")), [ev(0, "🚀x")], (0,)),
    (Count(tool("\U000e0041*"), "<", 2, by="name"), [ev(0, "\U000e0041\U0010fffd"), ev(1, "\U000e0041\U0010fffd")],
     (0, 1)),
    (Match(status="ok"), [ev(0, st="error")], (0,)),
    (Match(attrs=(("n", 1),)), [ev(0, n=True)], (0,)),
])
def test_cases(formula, events, expected):
    assert cx(formula, events) == expected


def test_seqs_and_later_events():
    f = Implies(tool("delete_*"), Once(event("confirmation")))
    events = [ev(10, "a"), ev(11, "delete_x"), ev(12, "ok", "confirmation")]
    assert cx(f, events, at=11) == (10, 11)
    with pytest.raises(ValueError, match="not violated"):
        cx(f, events, at=10)
    with pytest.raises(ValueError, match="no event with seq"):
        cx(f, events, at=3)


def test_name_patterns():
    pats = dict(name_patterns(("a*", "*b", "ab")))
    assert set(pats) == {frozenset(), frozenset({"a*"}), frozenset({"*b"}), frozenset({"a*", "*b"}),
                         frozenset({"a*", "*b", "ab"})}
    assert pats[frozenset({"a*", "*b", "ab"})] == "ab"
    assert dict(name_patterns(("*",))).keys() == {frozenset({"*"})}


@given(st.lists(st.text("ab*?", min_size=1, max_size=3), min_size=1, max_size=3).map(tuple))
def test_name_patterns_match_brute_force(globs):
    names = [""] + ["".join(t) for n in range(1, 8) for t in itertools.product("abx", repeat=n)]
    brute = {frozenset(g for g in globs if glob_regex(g).fullmatch(n)) for n in names}
    got = dict(name_patterns(globs))
    assert set(got) == brute
    assert all(frozenset(g for g in globs if glob_regex(g).fullmatch(n)) == p for p, n in got.items())


def test_solver_giving_up_returns_every_event(monkeypatch):
    monkeypatch.setattr(z3, "Z3_solver_check_assumptions", lambda *a: z3.Z3_L_UNDEF)
    assert cx(Not(tool("a")), [ev(0, "b"), ev(1, "a")]) == (0, 1)


def _assert_sufficient_and_minimal(formula, events, data):
    bad = [i for i, ok in enumerate(truth(formula, events)) if not ok]
    assume(bad)
    at = data.draw(st.sampled_from(bad))
    window = events[:at + 1]
    known = {i for i, e in enumerate(window) if e.seq in cx(formula, events, at=events[at].seq)}
    for completed in completions(formula, window, known, random.Random(at)):
        assert not truth(formula, completed)[-1], (formula, completed)
    for i in known:
        w = witness(formula, window, known - {i})
        assert w is not None and truth(formula, w)[-1], (formula, i)


PROPERTY = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])


@settings(PROPERTY, max_examples=1000)
@given(formulas(vocab_atoms, max_leaves=8), traces(max_size=8), st.data())
def test_sufficient_and_minimal(formula, events, data):
    _assert_sufficient_and_minimal(formula, events, data)


# counts grouped by name or kind, over few names, so that groups collide often
_globbed = st.builds(lambda g: Match(kind="tool", globs=g),
                     st.lists(st.text("ab*?", min_size=1, max_size=2), min_size=1, max_size=2).map(tuple))
_grouped = st.builds(Count, formulas(st.one_of(vocab_atoms, _globbed), max_leaves=3), st.sampled_from(CMPS),
                     st.integers(0, 3), st.sampled_from(["name", "kind"]))
_few_names = st.lists(st.tuples(st.sampled_from(["tool", "llm"]), st.text("ab", max_size=2)), max_size=7).map(
    lambda es: [Event(i, k, n, "success", {}) for i, (k, n) in enumerate(es)])


@settings(PROPERTY, max_examples=500)
@given(st.one_of(_grouped, st.builds(Implies, formulas(_globbed, max_leaves=2), _grouped), st.builds(Once, _grouped)),
       _few_names, st.data())
def test_sufficient_and_minimal_grouped_counts(formula, events, data):
    _assert_sufficient_and_minimal(formula, events, data)


def test_bench_workload_needs_every_event():
    events = bench_policy.counterexample_trace()
    p = compile_policy("p", bench_policy.synthetic_policies()[0])
    assert minimal_counterexample(p, events, events[-1].seq) == tuple(range(len(events)))
    shifted = [replace(e, seq=e.seq + 5) for e in events]
    assert minimal_counterexample(p, shifted, shifted[-1].seq) == tuple(e.seq for e in shifted)
