from typing import Sequence

from app.policy.checker import CompiledPolicy
from app.policy.events import Event


def minimal_counterexample(policy: CompiledPolicy, events: Sequence[Event], at_seq: int) -> tuple[int, ...]:
    raise NotImplementedError
