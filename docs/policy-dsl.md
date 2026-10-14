# Trace policy DSL

Past-time temporal safety policies over the ordered events of one trace (`app/policy/events.py: Event`).
`app.policy.dsl.parse(src) -> list[Policy]` produces the AST in `app/policy/ast.py`.

## Example

```
tag secret_read = tool("vault.read", "read_secret*")
tag network     = tool("http_*", "fetch_url", "send_email")

policy confirm_before_delete severity high:
  always tool("delete_*") -> once event("confirmation")

policy no_exfil_after_secret severity critical:
  always @network -> not once @secret_read

policy retry_cap severity medium:
  always count(tool(*) and status("error")) by name <= 3
```

## Lexical rules

- Whitespace (including newlines) separates tokens and is otherwise insignificant. `#` starts a comment that runs to the end of the line.
- `IDENT = [A-Za-z_][A-Za-z0-9_]*`. Reserved words (not usable as tag or policy names): `tag policy severity always not and or once historically previously since count by tool event status attr true false eventually next until`. `name`, `kind` and the severity levels are contextual, not reserved.
- `STRING`: double-quoted, on one line; the only escapes are `\"` and `\\`.
- `INT`: non-negative decimal, `[0-9]+`.
- Positions are 1-based line and column; columns count Unicode code points.

## Grammar (EBNF)

```ebnf
source      = { tag_def | policy_def } ;
tag_def     = "tag" IDENT "=" formula ;                         (* no temporal operators or count *)
policy_def  = "policy" IDENT [ "severity" level ] ":" "always" formula ;
level       = "info" | "low" | "medium" | "high" | "critical" ;  (* default: medium *)

formula     = or_expr [ "->" formula ] ;                        (* right-associative *)
or_expr     = and_expr { "or" and_expr } ;                      (* left-associative *)
and_expr    = since_expr { "and" since_expr } ;                 (* left-associative *)
since_expr  = unary [ "since" unary ] ;                         (* non-associative *)
unary       = ( "not" | "once" | "historically" | "previously" ) unary | primary ;
primary     = atom | "@" IDENT | count | "(" formula ")" ;

count       = "count" "(" formula ")" [ "by" ( "name" | "kind" ) ] cmp INT ;
cmp         = "<" | "<=" | "==" | "!=" | ">=" | ">" ;

atom        = "tool" "(" ( "*" | globs ) ")"
            | "event" "(" ( "*" | STRING [ "," globs ] ) ")"    (* STRING is the kind *)
            | "status" "(" STRING ")"
            | "attr" "(" STRING "," value ")" ;
globs       = STRING { "," STRING } ;
value       = STRING | INT | "true" | "false" ;
```

Precedence, high to low: unary (`not`, `once`, `historically`, `previously`), `since`, `and`, `or`, `->`.
So `not a since b` is `(not a) since b`, `a or b and c` is `a or (b and c)`, and `a -> b -> c` is `a -> (b -> c)`.
`a since b since c` is an error; parenthesize.

## Semantics

A trace is events `e0 … e(n-1)`, `seq = i`. Each formula is true or false at each position `i`.

| Construct | AST | True at position `i` iff |
|---|---|---|
| `tool(*)` | `Match(kind="tool")` | `kind(i) == "tool"` |
| `tool("g1", "g2")` | `Match(kind="tool", globs=("g1","g2"))` | `kind(i) == "tool"` and `name(i)` matches some glob |
| `event(*)` | `Match()` | always |
| `event("k")` | `Match(kind="k")` | `kind(i) == k`; `k` ∈ tool, llm, retrieval, confirmation, custom |
| `event("k", "g1", …)` | `Match(kind="k", globs=("g1", …))` | `kind(i) == k` and `name(i)` matches some glob |
| `status("s")` | `Match(status="s")` | `status(i) == s` |
| `attr("key", v)` | `Match(attrs=(("key", v),))` | `key` is in `attrs(i)` and its value equals `v` with the same JSON type (`true` ≠ `1`, `"2"` ≠ `2`) |
| `@t` | the tag's formula, inlined | as the tag's formula |
| `not φ` | `Not(φ)` | `φ` is false at `i` |
| `φ and ψ` | `And(φ, ψ)` | both true at `i` |
| `φ or ψ` | `Or(φ, ψ)` | either true at `i` |
| `φ -> ψ` | `Implies(φ, ψ)` | `φ` false or `ψ` true at `i` |
| `once φ` | `Once(φ)` | ∃ `j ≤ i`: `φ(j)` |
| `historically φ` | `Historically(φ)` | ∀ `j ≤ i`: `φ(j)` |
| `previously φ` | `Previously(φ)` | `i > 0` and `φ(i-1)`; false at seq 0 |
| `φ since ψ` | `Since(φ, ψ)` | ∃ `j ≤ i`: `ψ(j)` and ∀ `k` with `j < k ≤ i`: `φ(k)` |
| `count(φ) OP N` | `Count(φ, OP, N)` | `#{ j ≤ i : φ(j) } OP N` |
| `count(φ) by name OP N` | `Count(φ, OP, N, by="name")` | `#{ j ≤ i : φ(j) and name(j) == name(i) } OP N` |
| `count(φ) by kind OP N` | `Count(φ, OP, N, by="kind")` | `#{ j ≤ i : φ(j) and kind(j) == kind(i) } OP N` |
| `policy P …: always φ` | `Policy(P, severity, φ, source)` | violated at the first `i` where `φ(i)` is false |

- The parser never merges atoms: `tool(*) and status("error")` is `And(Match(kind="tool"), Match(status="error"))`.
- Globs match the whole name, case-sensitively. `*` matches any sequence including the empty one, `?` exactly one code point; every other character is literal. A glob must be non-empty and must not contain `[`, `]` or `\`.
- Tags must be defined before use (tag bodies may use earlier tags). They are inlined at parse time; the AST has no tag node.
- `always` appears exactly once, at the top of each policy. Each policy reports at most one violation per trace, at its first false position. An empty trace violates nothing.
- Only past-time operators exist in v1. `eventually`, `next`, `until` and an inner `always` are rejected.

## Counterexamples

The counterexample of a violation at `at_seq` is the minimal set `S` of seqs such that the observed facts (`kind`, `name`, `status`, `attrs`) of the events in `S` alone entail that `φ` is false at `at_seq`, where events outside `S` keep their positions but have unconstrained facts. Removing any one seq from `S` breaks the entailment. Seqs after `at_seq` never appear.

- `confirm_before_delete` violated at a delete at seq 3 with no earlier confirmation: `{0, 1, 2, 3}`; every earlier event must be known not to be a confirmation.
- `no_exfil_after_secret` with one secret read at seq 1 and a network call at seq 4: `{1, 4}`.
- `always tool("charge_*") -> count(event("confirmation")) > 1` violated at a charge at seq 1: `{1}`; two events cannot hold two confirmations when one of them is the charge.

## Errors

`parse` raises `PolicyParseError(line, col, message)` at the first error; `line`/`col` locate the offending token and `message` names what was expected (e.g. `expected ')'`, `expected integer bound after '<='`).

| Class | Example | Position |
|---|---|---|
| unexpected token | `always tool("x") tool("y")`, `policy and: …`, missing `:` or `always` | the token |
| unbalanced parens | `always (tool("x")`, `always tool("x"))` | the token where `)` was expected, or the stray `)` |
| unknown tag | `@nope`, or a tag used before its definition | the `@` |
| bad glob | `tool("")`, `tool("del[ab]")`, `tool("a\\b")` | the string |
| non-integer bound | `count(tool(*)) < 2.5`, `< -1`, `< "3"`, `< n` | the token after the operator |
| future operator | `eventually φ`, `next φ`, `φ until ψ`, a nested `always` | the keyword |
| unknown kind | `event("span")` | the kind string |
| duplicate name | a second `tag x` or `policy p` in one source | the repeated name |
| temporal operator in tag | `tag t = once tool("x")`, `count` in a tag | the operator (reported as unexpected token) |
| unknown severity | `severity urgent` | the level (reported as unexpected token) |

## Fixtures

`backend/tests/policy/fixtures/*.json`, one per file, loaded by `tests/policy/fixtures.py`:

```json
{
  "name": "no_exfil_violation",
  "description": "…",
  "source": "<DSL source, may hold several policies>",
  "events": [{"seq": 0, "kind": "llm", "name": "plan"}, …],
  "expected": [{"policy": "no_exfil_after_secret", "at_seq": 4, "counterexample": [1, 4]}]
}
```

`events` are `EventIn` dicts with `seq` 0..n-1. Policies that are not violated have no `expected` entry. Every fixture's minimal counterexample is unique.
