import dataclasses
import re

import pytest
from hypothesis import given, settings

from app.policy.ast import (
    And, Count, Historically, Implies, Match, Not, Once, Or, Policy, Previously, Since,
)
from app.policy.dsl import PolicyParseError, parse, pretty, pretty_formula
from tests.policy.fixtures import load_fixtures
from tests.policy.strategies import formulas, policies

DOC_EXAMPLE = '''tag secret_read = tool("vault.read", "read_secret*")
tag network     = tool("http_*", "fetch_url", "send_email")

policy confirm_before_delete severity high:
  always tool("delete_*") -> once event("confirmation")

policy no_exfil_after_secret severity critical:
  always @network -> not once @secret_read

policy retry_cap severity medium:
  always count(tool(*) and status("error")) by name <= 3
'''

A, B, C = Match(kind="tool", globs=("a",)), Match(kind="tool", globs=("b",)), Match(kind="tool", globs=("c",))


def f(body: str):
    return parse(f"policy p:\n  always {body}")[0].formula


def strict(x):
    # repr tells True from 1 where dataclass equality does not
    return repr([dataclasses.replace(p, source="") for p in x])


def test_doc_example():
    secret = Match(kind="tool", globs=("vault.read", "read_secret*"))
    network = Match(kind="tool", globs=("http_*", "fetch_url", "send_email"))
    assert parse(DOC_EXAMPLE) == [
        Policy("confirm_before_delete", "high",
               Implies(Match(kind="tool", globs=("delete_*",)), Once(Match(kind="confirmation")))),
        Policy("no_exfil_after_secret", "critical", Implies(network, Not(Once(secret)))),
        Policy("retry_cap", "medium",
               Count(And(Match(kind="tool"), Match(status="error")), "<=", 3, "name")),
    ]


def test_doc_counterexample_example():
    assert f('tool("charge_*") -> count(event("confirmation")) > 1') == Implies(
        Match(kind="tool", globs=("charge_*",)), Count(Match(kind="confirmation"), ">", 1))


@pytest.mark.parametrize("fx", load_fixtures(), ids=lambda fx: fx.name)
def test_fixture_sources_parse_and_round_trip(fx):
    ps = parse(fx.source)
    assert {x.policy for x in fx.expected} <= {p.name for p in ps}
    assert strict(parse(pretty(ps))) == strict(ps)


@pytest.mark.parametrize("src,expected", [
    ("not a since b", Since(Not(A), B)),
    ("a or b and c", Or(A, And(B, C))),
    ("a and b or c", Or(And(A, B), C)),
    ("a -> b -> c", Implies(A, Implies(B, C))),
    ("(a -> b) -> c", Implies(Implies(A, B), C)),
    ("a or b or c", Or(Or(A, B), C)),
    ("a and b and c", And(And(A, B), C)),
    ("not once historically previously a", Not(Once(Historically(Previously(A))))),
    ("a since b and c", And(Since(A, B), C)),
    ("a since (b since c)", Since(A, Since(B, C))),
    ("count(a or b) >= 0 -> a", Implies(Count(Or(A, B), ">=", 0), A)),
])
def test_precedence(src, expected):
    assert f(re.sub(r"\b([abc])\b", r'tool("\1")', src)) == expected


@pytest.mark.parametrize("src,expected", [
    ("tool(*)", Match(kind="tool")),
    ('tool("x", "y*")', Match(kind="tool", globs=("x", "y*"))),
    ("event(*)", Match()),
    ('event("llm")', Match(kind="llm")),
    ('event("custom", "a", "b?")', Match(kind="custom", globs=("a", "b?"))),
    ('status("")', Match(status="")),
    ('attr("k", "v")', Match(attrs=(("k", "v"),))),
    ('attr("k", 0)', Match(attrs=(("k", 0),))),
    ('attr("k", 12345678901234567890)', Match(attrs=(("k", 12345678901234567890),))),
    ('attr("k", true)', Match(attrs=(("k", True),))),
    ('attr("k", false)', Match(attrs=(("k", False),))),
    ("count(tool(*)) by kind != 7", Count(Match(kind="tool"), "!=", 7, "kind")),
    *[(f"count(tool(*)) {op} 2", Count(Match(kind="tool"), op, 2)) for op in ("<", "<=", "==", "!=", ">=", ">")],
])
def test_atoms(src, expected):
    assert strict([Policy("p", "medium", f(src))]) == strict([Policy("p", "medium", expected)])


def test_string_escapes():
    assert f(r'status("a\"b\\c")') == Match(status='a"b\\c')


def test_severity_default_names_and_source():
    src = "# leading comment\npolicy name: always event(*)  # trailing\npolicy kind severity info:\n  always not tool(*)\n"
    p1, p2 = parse(src)
    assert (p1.name, p1.severity, p2.name, p2.severity) == ("name", "medium", "kind", "info")
    assert p1.source == "policy name: always event(*)"
    assert p2.source == "policy kind severity info:\n  always not tool(*)"


def test_tags_inline_and_nest():
    src = 'tag d = tool("del*")\ntag g = @d and attr("env", "prod")\npolicy p: always @g -> previously event("confirmation")'
    assert parse(src)[0].formula == Implies(
        And(Match(kind="tool", globs=("del*",)), Match(attrs=(("env", "prod"),))), Previously(Match(kind="confirmation")))


def test_size_caps():
    tags = 'tag t0 = tool("a")\n' + "".join(f"tag t{i} = @t{i - 1} and @t{i - 1}\n" for i in range(1, 31))
    assert parse(tags + "policy ok: always @t9")  # 1023 nodes; @t10 is 2047
    with pytest.raises(PolicyParseError, match="over 2000 atoms and operators") as e:
        parse(tags + "policy big:\n  always not @t30")
    assert (e.value.line, e.value.col) == (32, 1)
    with pytest.raises(PolicyParseError, match="over 20000 characters"):
        parse("#" * 20_001)


def test_depth_caps():
    assert parse("policy ok: always " + "not " * 63 + 'tool("a")')
    assert parse("policy ok: always " + "(" * 63 + 'tool("a")' + ")" * 63)
    deep = ["policy p: always " + "not " * 64 + 'tool("a")',
            "policy p: always " + " or ".join(['tool("a")'] * 65),
            'tag t0 = tool("a")\n' + "".join(f"tag t{i} = not @t{i - 1}\n" for i in range(1, 65)) + "policy p: always @t64",
            "policy p: always " + "not " * 4000 + 'tool("a")',
            "policy p: always " + "(" * 9000 + 'tool("a")' + ")" * 9000,
            "policy p: always " + "count(" * 1000 + 'tool("a")' + ") > 1" * 1000]
    for src in deep:
        with pytest.raises(PolicyParseError, match="nested over 64 deep"):
            parse(src)


def test_empty_and_tag_only_sources():
    assert parse("") == []
    assert parse("  # nothing\n") == []
    assert parse('tag t = tool("x")') == []


def err(src: str) -> PolicyParseError:
    with pytest.raises(PolicyParseError) as exc:
        parse(src)
    return exc.value


@pytest.mark.parametrize("src,line,col,msg", [
    # unexpected token
    ('policy p: always tool("x") tool("y")', 1, 28, "expected 'tag', 'policy' or end of input, got 'tool'"),
    ("policy and: always tool(*)", 1, 8, "expected policy name, got 'and'"),
    ("policy p always tool(*)", 1, 10, "expected ':', got 'always'"),
    ("policy p: tool(*)", 1, 11, "expected 'always', got 'tool'"),
    ("policy p:\n  always", 2, 9, "expected a formula, got end of input"),
    ("policy p: always tool(*) and", 1, 29, "expected a formula"),
    ("policy p: always tool(x)", 1, 23, "expected glob string, got 'x'"),
    ('policy p: always event(*, "x")', 1, 25, "expected ')', got ','"),
    ("policy p: always event(llm)", 1, 24, "expected event kind string or '*', got 'llm'"),
    ("policy p: always status(*)", 1, 25, "expected status string"),
    ("policy p: always attr(k, 1)", 1, 23, "expected attribute key string"),
    ('policy p: always attr("k", 1.5)', 1, 28, "expected attribute value"),
    ('policy p: always attr("k", -1)', 1, 28, "expected attribute value"),
    ('policy p: always attr("k" 1)', 1, 27, "expected ','"),
    ("policy p: always count(tool(*)) by name", 1, 40, "expected comparison"),
    ("policy p: always count(tool(*)) by status < 1", 1, 36, "expected 'name' or 'kind' after 'by', got 'status'"),
    ("policy p: always count(tool(*) < 1", 1, 32, "expected ')', got '<'"),
    ("tag policy = tool(*)", 1, 5, "expected tag name, got 'policy'"),
    ("tag t tool(*)", 1, 7, "expected '=', got 'tool'"),
    ("policy p: always @", 1, 19, "expected tag name after '@', got end of input"),
    ("policy p: always tool(*) % 2", 1, 26, "got '%'"),
    ("policy p: always tool(*)\n  and €", 2, 7, "expected a formula, got '€'"),
    ("always tool(*)", 1, 1, "expected 'tag', 'policy' or end of input"),
    # unbalanced parens
    ('policy p: always (tool("x")', 1, 28, "expected ')', got end of input"),
    ('policy p: always tool("x"))', 1, 27, "unbalanced ')'"),
    ('policy p: always ((tool("x"))', 1, 30, "expected ')'"),
    # unknown tag
    ("policy p: always @nope", 1, 18, "unknown tag 'nope'"),
    ('policy p: always @t\ntag t = tool("x")', 1, 18, "unknown tag 't'"),
    # bad glob
    ('policy p: always tool("")', 1, 23, 'invalid glob "": must not be empty'),
    ('policy p: always tool("ok", "del[ab]")', 1, 29, "must not contain '['"),
    ('policy p: always tool("a]")', 1, 23, "must not contain ']'"),
    (r'policy p: always event("tool", "a\\b")', 1, 32, "must not contain '\\'"),
    # non-integer bound
    ("policy p: always count(tool(*)) < 2.5", 1, 35, "expected integer bound after '<', got '2.5'"),
    ("policy p: always count(tool(*)) <= -1", 1, 36, "expected integer bound after '<=', got '-1'"),
    ('policy p: always count(tool(*)) < "3"', 1, 35, "expected integer bound after '<'"),
    ("policy p: always count(tool(*)) < n", 1, 35, "expected integer bound after '<', got 'n'"),
    ("policy p: always count(tool(*)) < 1e3", 1, 35, "expected integer bound after '<', got '1e3'"),
    ("policy p: always count(tool(*)) > 3x", 1, 35, "expected integer bound after '>', got '3x'"),
    ('policy p: always attr("k", 1e3)', 1, 28, "expected attribute value"),
    # future operators
    ("policy p: always eventually tool(*)", 1, 18, "future-time operator 'eventually' is not supported"),
    ("policy p: always next tool(*)", 1, 18, "future-time operator 'next'"),
    ('policy p: always tool("a") until tool("b")', 1, 28, "future-time operator 'until'"),
    ("policy p: always not always tool(*)", 1, 22, "nested 'always' is not supported"),
    ("policy p: always tool(*) -> always tool(*)", 1, 29, "nested 'always'"),
    # unknown kind
    ('policy p: always event("span")', 1, 24, "unknown kind 'span'"),
    ('policy p: always event("Tool", "x")', 1, 24, "unknown kind 'Tool'"),
    # duplicate names
    ('tag x = tool("a")\ntag x = tool("b")', 2, 5, "duplicate tag 'x'"),
    ("policy p: always tool(*)\npolicy p: always tool(*)", 2, 8, "duplicate policy 'p'"),
    # temporal operator in tag
    ('tag t = once tool("x")', 1, 9, "unexpected 'once': temporal operators and count are not allowed in tags"),
    ('tag t = not historically tool("x")', 1, 13, "unexpected 'historically'"),
    ('tag t = previously tool("x")', 1, 9, "unexpected 'previously'"),
    ('tag t = tool("x") since tool("y")', 1, 19, "unexpected 'since'"),
    ("tag t = count(tool(*)) < 3", 1, 9, "unexpected 'count'"),
    # unknown severity
    ("policy p severity urgent: always tool(*)", 1, 19, "expected severity level (info | low | medium | high | critical), got 'urgent'"),
    ("policy p severity: always tool(*)", 1, 18, "expected severity level"),
    # chained since
    ('policy p: always tool("a") since tool("b") since tool("c")', 1, 44, "'since' is non-associative; parenthesize"),
    # lexical
    ('policy p: always tool("abc', 1, 23, "unterminated string"),
    ('policy p: always tool("ab\n")', 1, 23, "unterminated string"),
    (r'policy p: always tool("a\nb")', 1, 25, "invalid escape"),
    ('policy p: always tool("a\\', 1, 25, "invalid escape"),
])
def test_errors(src, line, col, msg):
    e = err(src)
    assert (e.line, e.col) == (line, col), e
    assert msg in e.message
    assert str(e) == f"{line}:{col}: {e.message}"


def test_error_columns_count_code_points():
    e = err('tag t = tool("löschen_🚀")\npolicy p: always @t  @t')
    assert (e.line, e.col) == (2, 22)
    e = err('policy p: always tool("🚀🚀") )')
    assert (e.line, e.col) == (1, 29)


def test_parse_error_is_value_error():
    assert issubclass(PolicyParseError, ValueError)


@pytest.mark.parametrize("m", [
    Match(kind="tool", status="error"),
    Match(globs=("x",)),
    Match(kind="span"),
    Match(attrs=(("a", 1), ("b", 2))),
    Match(status="s", attrs=(("a", 1),)),
    Match(attrs=(("a", -1),)),
    Match(attrs=(("a", 1.5),)),
])
def test_pretty_rejects_non_atoms(m):
    with pytest.raises(ValueError):
        pretty_formula(m)


@pytest.mark.parametrize("name,severity", [("count", "low"), ("my-policy", "low"), ("1p", "low"), ("p", "urgent")])
def test_pretty_rejects_unprintable_policies(name, severity):
    with pytest.raises(ValueError):
        pretty(Policy(name, severity, Match()))


def test_pretty_rejects_non_formula():
    with pytest.raises(TypeError):
        pretty_formula("tool")


def test_pretty_single_policy_and_minimal_parens():
    p = Policy("p", "low", Implies(Implies(A, Or(A, B)), Not(Since(A, And(B, C)))))
    assert pretty(p) == 'policy p severity low:\n  always (tool("a") -> tool("a") or tool("b")) -> not (tool("a") since (tool("b") and tool("c")))\n'
    assert pretty_formula(Or(A, Or(B, C))) == 'tool("a") or (tool("b") or tool("c"))'
    assert pretty_formula(Count(Implies(A, B), "<", 1, "name")) == 'count(tool("a") -> tool("b")) by name < 1'


@settings(max_examples=1000, deadline=None)
@given(policies)
def test_round_trip(p):
    assert strict(parse(pretty(p))) == strict([p])


@settings(max_examples=300, deadline=None)
@given(formulas())
def test_round_trip_formula_is_stable(fm):
    s = pretty_formula(fm)
    assert pretty_formula(f(s)) == s
