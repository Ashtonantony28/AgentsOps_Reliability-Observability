import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Union

from app.policy.ast import (
    And, Count, Formula, Historically, Implies, Match, Not, Once, Or, Policy, Previously, Since,
)

KINDS = ("tool", "llm", "retrieval", "confirmation", "custom")
LEVELS = ("info", "low", "medium", "high", "critical")
CMPS = ("<=", ">=", "==", "!=", "<", ">")
RESERVED = frozenset(
    "tag policy severity always not and or once historically previously since count by tool event "
    "status attr true false eventually next until".split()
)
FUTURE = frozenset(("eventually", "next", "until"))
UNARY = {"not": Not, "once": Once, "historically": Historically, "previously": Previously}
TEMPORAL = frozenset(("once", "historically", "previously", "since", "count"))

_TOKEN = re.compile(
    r"(?P<ws>[ \t\r\f\v]+)|(?P<nl>\n)|(?P<comment>\#[^\n]*)"
    r"|(?P<num>-?[0-9]+(?:\.[0-9]*)?)|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op>->|<=|>=|==|!=|[<>()=,:@*])|(?P<str>\")"
)


class PolicyParseError(ValueError):
    def __init__(self, line: int, col: int, message: str):
        super().__init__(f"{line}:{col}: {message}")
        self.line = line
        self.col = col
        self.message = message


@dataclass(frozen=True)
class Token:
    type: str          # ident, keyword, str, num, op, char, eof
    value: Any
    text: str
    line: int
    col: int
    start: int
    end: int

    def describe(self) -> str:
        return "end of input" if self.type == "eof" else f"'{self.text}'"


def _tokenize(src: str) -> list[Token]:
    out: list[Token] = []
    pos, line, line_start = 0, 1, 0
    while pos < len(src):
        col = pos - line_start + 1
        m = _TOKEN.match(src, pos)
        if not m:
            out.append(Token("char", src[pos], src[pos], line, col, pos, pos + 1))
            pos += 1
            continue
        kind, text = m.lastgroup, m.group()
        if kind == "nl":
            line, line_start = line + 1, m.end()
        elif kind == "str":
            value, end = _read_string(src, pos, line, col)
            out.append(Token("str", value, src[pos:end], line, col, pos, end))
            pos = end
            continue
        elif kind == "ident":
            out.append(Token("keyword" if text in RESERVED else "ident", text, text, line, col, pos, m.end()))
        elif kind in ("num", "op"):
            out.append(Token(kind, text, text, line, col, pos, m.end()))
        pos = m.end()
    out.append(Token("eof", None, "", line, pos - line_start + 1, pos, pos))
    return out


def _read_string(src: str, start: int, line: int, col: int) -> tuple[str, int]:
    chars, i = [], start + 1
    while i < len(src):
        c = src[i]
        if c == '"':
            return "".join(chars), i + 1
        if c == "\n":
            break
        if c == "\\":
            nxt = src[i + 1] if i + 1 < len(src) else ""
            if nxt not in ('"', "\\"):
                raise PolicyParseError(line, col + i - start, "invalid escape; only \\\" and \\\\ are allowed")
            chars.append(nxt)
            i += 2
            continue
        chars.append(c)
        i += 1
    raise PolicyParseError(line, col, "unterminated string")


class _Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = _tokenize(src)
        self.i = 0
        self.tags: dict[str, Formula] = {}
        self.policy_names: set[str] = set()
        self.in_tag = False

    @property
    def tok(self) -> Token:
        return self.toks[self.i]

    def advance(self) -> Token:
        t = self.toks[self.i]
        self.i += 1
        return t

    def at(self, *values: str) -> bool:
        t = self.tok
        return t.type in ("keyword", "op") and t.value in values

    def fail(self, expected: str, tok: Optional[Token] = None) -> PolicyParseError:
        t = tok or self.tok
        if t.type == "keyword" and t.value in FUTURE:
            return PolicyParseError(t.line, t.col, f"future-time operator '{t.value}' is not supported")
        return PolicyParseError(t.line, t.col, f"expected {expected}, got {t.describe()}")

    def expect(self, value: str, expected: Optional[str] = None) -> Token:
        if not self.at(value):
            raise self.fail(expected or f"'{value}'")
        return self.advance()

    def name(self, what: str) -> Token:
        if self.tok.type != "ident":
            raise self.fail(what)
        return self.advance()

    def source(self) -> list[Policy]:
        policies: list[Policy] = []
        while self.tok.type != "eof":
            if self.at("tag"):
                self.tag_def()
            elif self.at("policy"):
                policies.append(self.policy_def())
            elif self.at(")"):
                raise PolicyParseError(self.tok.line, self.tok.col, "unbalanced ')'")
            else:
                raise self.fail("'tag', 'policy' or end of input")
        return policies

    def tag_def(self) -> None:
        self.advance()
        name = self.name("tag name")
        if name.value in self.tags:
            raise PolicyParseError(name.line, name.col, f"duplicate tag '{name.value}'")
        self.expect("=")
        self.in_tag = True
        self.tags[name.value] = self.formula()
        self.in_tag = False

    def policy_def(self) -> Policy:
        start = self.advance()
        name = self.name("policy name")
        if name.value in self.policy_names:
            raise PolicyParseError(name.line, name.col, f"duplicate policy '{name.value}'")
        self.policy_names.add(name.value)
        severity = "medium"
        if self.at("severity"):
            self.advance()
            if not (self.tok.type == "ident" and self.tok.value in LEVELS):
                raise self.fail("severity level (" + " | ".join(LEVELS) + ")")
            severity = self.advance().value
        self.expect(":")
        self.expect("always")
        formula = self.formula()
        end = self.toks[self.i - 1].end
        return Policy(name.value, severity, formula, self.src[start.start:end])

    def formula(self) -> Formula:
        left = self.or_expr()
        if self.at("->"):
            self.advance()
            return Implies(left, self.formula())
        return left

    def or_expr(self) -> Formula:
        left = self.and_expr()
        while self.at("or"):
            self.advance()
            left = Or(left, self.and_expr())
        return left

    def and_expr(self) -> Formula:
        left = self.since_expr()
        while self.at("and"):
            self.advance()
            left = And(left, self.since_expr())
        return left

    def since_expr(self) -> Formula:
        left = self.unary()
        if self.at("since"):
            self.no_temporal()
            self.advance()
            left = Since(left, self.unary())
            if self.at("since"):
                raise PolicyParseError(self.tok.line, self.tok.col, "'since' is non-associative; parenthesize")
        return left

    def no_temporal(self) -> None:
        if self.in_tag:
            raise PolicyParseError(self.tok.line, self.tok.col,
                                   f"unexpected '{self.tok.value}': temporal operators and count are not allowed in tags")

    def unary(self) -> Formula:
        t = self.tok
        if t.type == "keyword" and t.value in UNARY:
            if t.value != "not":
                self.no_temporal()
            self.advance()
            return UNARY[t.value](self.unary())
        return self.primary()

    def primary(self) -> Formula:
        t = self.tok
        if self.at("("):
            self.advance()
            f = self.formula()
            self.expect(")")
            return f
        if self.at("@"):
            self.advance()
            name = self.name("tag name after '@'")
            if name.value not in self.tags:
                raise PolicyParseError(t.line, t.col, f"unknown tag '{name.value}'")
            return self.tags[name.value]
        if self.at("count"):
            self.no_temporal()
            return self.count()
        if self.at("tool"):
            self.advance()
            self.expect("(")
            globs = () if self.star() else self.globs()
            self.expect(")")
            return Match(kind="tool", globs=globs)
        if self.at("event"):
            self.advance()
            self.expect("(")
            m = Match()
            if not self.star():
                kind = self.string("event kind string or '*'")
                if kind.value not in KINDS:
                    raise PolicyParseError(kind.line, kind.col,
                                           f"unknown kind '{kind.value}'; expected one of " + ", ".join(KINDS))
                globs = ()
                if self.at(","):
                    self.advance()
                    globs = self.globs()
                m = Match(kind=kind.value, globs=globs)
            self.expect(")")
            return m
        if self.at("status"):
            self.advance()
            self.expect("(")
            status = self.string("status string").value
            self.expect(")")
            return Match(status=status)
        if self.at("attr"):
            self.advance()
            self.expect("(")
            key = self.string("attribute key string").value
            self.expect(",")
            value = self.value()
            self.expect(")")
            return Match(attrs=((key, value),))
        if self.at("always"):
            raise PolicyParseError(t.line, t.col, "nested 'always' is not supported; it may only start a policy")
        raise self.fail("a formula")

    def star(self) -> bool:
        if self.at("*"):
            self.advance()
            return True
        return False

    def string(self, expected: str) -> Token:
        if self.tok.type != "str":
            raise self.fail(expected)
        return self.advance()

    def globs(self) -> tuple[str, ...]:
        out = [self.glob()]
        while self.at(","):
            self.advance()
            out.append(self.glob())
        return tuple(out)

    def glob(self) -> str:
        t = self.string("glob string")
        bad = next((c for c in "[]\\" if c in t.value), None)
        if not t.value or bad:
            reason = "must not be empty" if not t.value else f"must not contain '{bad}'"
            raise PolicyParseError(t.line, t.col, f"invalid glob {t.text}: {reason}")
        return t.value

    def value(self) -> Union[str, int, bool]:
        t = self.tok
        if t.type == "str":
            return self.advance().value
        if t.type == "num" and t.value.isdigit():
            return int(self.advance().value)
        if self.at("true", "false"):
            return self.advance().value == "true"
        raise self.fail("attribute value (string, integer, true or false)")

    def count(self) -> Count:
        self.advance()
        self.expect("(")
        pred = self.formula()
        self.expect(")")
        by = None
        if self.at("by"):
            self.advance()
            if not (self.tok.type == "ident" and self.tok.value in ("name", "kind")):
                raise self.fail("'name' or 'kind' after 'by'")
            by = self.advance().value
        if not self.at(*CMPS):
            raise self.fail("comparison (" + " ".join(CMPS) + ")")
        op = self.advance().value
        if not (self.tok.type == "num" and self.tok.value.isdigit()):
            raise self.fail(f"integer bound after '{op}'")
        return Count(pred, op, int(self.advance().value), by)


def parse(src: str) -> list[Policy]:
    return _Parser(src).source()


# Pretty-printing. Precedence levels: -> 1, or 2, and 3, since 4, unary 5, primary 6.

def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int) and v >= 0:
        return str(v)
    if isinstance(v, str):
        return _q(v)
    raise ValueError(f"attr value {v!r} has no DSL form")


def _match(m: Match) -> str:
    if m.status is None and not m.attrs:
        if m.kind is None and not m.globs:
            return "event(*)"
        if m.kind == "tool":
            return "tool(" + (", ".join(map(_q, m.globs)) or "*") + ")"
        if m.kind in KINDS:
            return "event(" + ", ".join(map(_q, (m.kind, *m.globs))) + ")"
    elif m.kind is None and not m.globs:
        if not m.attrs:
            return f"status({_q(m.status)})"
        if m.status is None and len(m.attrs) == 1:
            k, v = m.attrs[0]
            return f"attr({_q(k)}, {_value(v)})"
    raise ValueError(f"{m!r} is not a single DSL atom")


def _fmt(f: Formula, ctx: int) -> str:
    if isinstance(f, Match):
        return _match(f)
    if isinstance(f, Count):
        by = f" by {f.by}" if f.by else ""
        return f"count({_fmt(f.pred, 0)}){by} {f.op} {f.bound}"
    if isinstance(f, (Not, Once, Historically, Previously)):
        kw = {Not: "not", Once: "once", Historically: "historically", Previously: "previously"}[type(f)]
        s, level = f"{kw} {_fmt(f.arg, 5)}", 5
    elif isinstance(f, Since):
        s, level = f"{_fmt(f.left, 5)} since {_fmt(f.right, 5)}", 4
    elif isinstance(f, And):
        s, level = f"{_fmt(f.left, 3)} and {_fmt(f.right, 4)}", 3
    elif isinstance(f, Or):
        s, level = f"{_fmt(f.left, 2)} or {_fmt(f.right, 3)}", 2
    elif isinstance(f, Implies):
        s, level = f"{_fmt(f.left, 2)} -> {_fmt(f.right, 1)}", 1
    else:
        raise TypeError(f"not a formula: {f!r}")
    return f"({s})" if level < ctx else s


def pretty_formula(f: Formula) -> str:
    return _fmt(f, 0)


def pretty(policies: Union[Policy, Iterable[Policy]]) -> str:
    if isinstance(policies, Policy):
        policies = [policies]
    return "\n".join(f"policy {p.name} severity {p.severity}:\n  always {_fmt(p.formula, 0)}\n" for p in policies)
