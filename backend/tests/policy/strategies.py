from hypothesis import strategies as st

from app.policy.ast import (
    And, Count, Historically, Implies, Match, Not, Once, Or, Policy, Previously, Since,
)
from app.policy.dsl import CMPS, KINDS, LEVELS, RESERVED
from app.policy.events import Event

line_text = st.text(st.characters(exclude_characters="\n"), max_size=8)
globs = st.lists(
    st.text(st.characters(exclude_characters="\n[]\\"), min_size=1, max_size=6), min_size=1, max_size=3
).map(tuple)
idents = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,10}", fullmatch=True).filter(lambda s: s not in RESERVED)
attr_values = st.one_of(line_text, st.integers(0, 10**12), st.booleans())

atoms = st.one_of(
    st.just(Match()),
    st.sampled_from(KINDS).map(lambda k: Match(kind=k)),
    st.builds(lambda k, g: Match(kind=k, globs=g), st.sampled_from(KINDS), globs),
    line_text.map(lambda s: Match(status=s)),
    st.builds(lambda k, v: Match(attrs=((k, v),)), line_text, attr_values),
)


def _extend(sub):
    return st.one_of(
        st.builds(Not, sub), st.builds(Once, sub), st.builds(Historically, sub), st.builds(Previously, sub),
        st.builds(And, sub, sub), st.builds(Or, sub, sub), st.builds(Implies, sub, sub), st.builds(Since, sub, sub),
        st.builds(Count, sub, st.sampled_from(CMPS), st.integers(0, 10), st.sampled_from([None, "name", "kind"])),
    )


def formulas(atoms=atoms, max_leaves: int = 12):
    return st.recursive(atoms, _extend, max_leaves=max_leaves)


policies = st.builds(Policy, idents, st.sampled_from(LEVELS), formulas())


# small vocabularies, so that random atoms and random traces match each other often
STATUSES = ("success", "error", "timeout")
ATTR_KEYS = ("env", "level")
DSL_VALUES = (True, False, 0, 1, 2, "1", "prod")
vocab_atoms = st.one_of(
    st.just(Match()),
    st.builds(lambda k, g: Match(kind=k, globs=g), st.sampled_from(KINDS),
              st.lists(st.text("ab_é*?", min_size=1, max_size=3), max_size=2).map(tuple)),
    st.sampled_from(STATUSES).map(lambda s: Match(status=s)),
    st.builds(lambda k, v: Match(attrs=((k, v),)), st.sampled_from(ATTR_KEYS), st.sampled_from(DSL_VALUES)),
)
_event = st.tuples(st.sampled_from(KINDS), st.text("ab_é", max_size=3), st.sampled_from(STATUSES),
                   st.dictionaries(st.sampled_from(ATTR_KEYS), st.sampled_from(DSL_VALUES + (2.0,)), max_size=2))


@st.composite
def traces(draw, max_size: int = 50):
    return [Event(i, *e) for i, e in enumerate(draw(st.lists(_event, max_size=max_size)))]
