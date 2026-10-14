from hypothesis import given, settings
from hypothesis import strategies as st

from app.policy.ast import Policy
from app.policy.checker import check_trace, compile_policy
from app.policy.encode import first_violations
from app.policy.reference import evaluate
from tests.policy.strategies import formulas, policies, traces, vocab_atoms

vocab_policies = st.builds(Policy, st.just("p"), st.just("high"), formulas(vocab_atoms, max_leaves=8))


def _agree(ps, events):
    got = {v.policy_id: v.at_seq for v in check_trace([compile_policy(str(i), p) for i, p in enumerate(ps)], events)}
    z3_firsts = first_violations([p.formula for p in ps], events)
    for i, p in enumerate(ps):
        bad = evaluate(p, events)
        assert got.get(str(i)) == z3_firsts[i] == (bad[0] if bad else None), p


@settings(deadline=None)
@given(st.lists(vocab_policies, min_size=1, max_size=3), traces())
def test_agrees_with_reference(ps, events):
    _agree(ps, events)


@settings(deadline=None)
@given(st.lists(policies, min_size=1, max_size=3), traces())
def test_agrees_with_reference_on_arbitrary_atoms(ps, events):
    _agree(ps, events)
