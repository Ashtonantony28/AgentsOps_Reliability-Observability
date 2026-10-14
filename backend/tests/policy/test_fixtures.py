import re

import pytest

from tests.policy.fixtures import load_fixtures

FIXTURES = load_fixtures()


def test_enough_fixtures_with_unique_names():
    assert len(FIXTURES) >= 20
    names = [f.name for f in FIXTURES]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_events_are_ordered(fx):
    assert [e.seq for e in fx.events] == list(range(len(fx.events)))


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_expected_violations_are_well_formed(fx):
    policies = [x.policy for x in fx.expected]
    assert len(policies) == len(set(policies))
    for x in fx.expected:
        assert re.search(rf"^\s*policy\s+{re.escape(x.policy)}\b", fx.source, re.M)
        assert 0 <= x.at_seq < len(fx.events)
        cx = x.counterexample
        assert list(cx) == sorted(set(cx))
        assert all(0 <= s <= x.at_seq for s in cx)
