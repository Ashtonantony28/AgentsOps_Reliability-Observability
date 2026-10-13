from typing import Sequence

from app.policy.ast import Policy
from app.policy.events import Event


def evaluate(policy: Policy, events: Sequence[Event]) -> list[int]:
    """Seqs at which the policy's `always` body is false, in order."""
    raise NotImplementedError
