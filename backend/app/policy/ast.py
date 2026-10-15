from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union

Severity = Literal["info", "low", "medium", "high", "critical"]
CountOp = Literal["<", "<=", "==", "!=", ">=", ">"]


@dataclass(frozen=True)
class Match:
    kind: Optional[str] = None                   # None: any kind
    globs: tuple[str, ...] = ()                  # empty: any name
    status: Optional[str] = None                 # None: any status
    attrs: tuple[tuple[str, Any], ...] = ()      # every (key, value) must equal event.attrs[key]


@dataclass(frozen=True)
class Not:
    arg: "Formula"


@dataclass(frozen=True)
class And:
    left: "Formula"
    right: "Formula"


@dataclass(frozen=True)
class Or:
    left: "Formula"
    right: "Formula"


@dataclass(frozen=True)
class Implies:
    left: "Formula"
    right: "Formula"


@dataclass(frozen=True)
class Once:
    arg: "Formula"


@dataclass(frozen=True)
class Historically:
    arg: "Formula"


@dataclass(frozen=True)
class Previously:
    arg: "Formula"


@dataclass(frozen=True)
class Since:
    left: "Formula"
    right: "Formula"


@dataclass(frozen=True)
class Count:
    pred: "Formula"
    op: CountOp
    bound: int
    by: Optional[Literal["name", "kind"]] = None


Formula = Union[Match, Not, And, Or, Implies, Once, Historically, Previously, Since, Count]


@dataclass(frozen=True)
class Policy:
    name: str
    severity: Severity
    formula: Formula                              # the body of `always φ`
    source: str = field(default="", compare=False)
    parsed: int = field(default=0, compare=False, repr=False)  # nodes parsed for its source, unused tags included
