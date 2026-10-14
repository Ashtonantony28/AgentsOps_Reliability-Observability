from dataclasses import dataclass, field
from typing import Any, Sequence

from app.policy.ast import Policy
from app.policy.encode import first_violations
from app.policy.events import Event


@dataclass(frozen=True)
class CompiledPolicy:
    policy_id: str
    policy: Policy
    artifact: Any = field(default=None, compare=False, repr=False)  # encoder-owned


@dataclass(frozen=True)
class Violation:
    policy_id: str
    policy_name: str
    severity: str
    at_seq: int
    counterexample: tuple[int, ...]
    message: str


def compile_policy(policy_id: str, policy: Policy) -> CompiledPolicy:
    return CompiledPolicy(policy_id, policy)


def check_trace(policies: Sequence[CompiledPolicy], events: Sequence[Event]) -> list[Violation]:
    firsts = first_violations([p.policy.formula for p in policies], events)
    return [Violation(p.policy_id, p.policy.name, p.policy.severity, at, (), f"{p.policy.name} violated at seq {at}")
            for p, at in zip(policies, firsts) if at is not None]


class Monitor:
    def __init__(self, policies: Sequence[CompiledPolicy]):
        self.policies = list(policies)

    def step(self, event: Event) -> list[Violation]:
        raise NotImplementedError

    def snapshot(self) -> bytes:
        raise NotImplementedError

    @classmethod
    def restore(cls, policies: Sequence[CompiledPolicy], blob: bytes) -> "Monitor":
        raise NotImplementedError


def would_violate(policies: Sequence[CompiledPolicy], prefix: Sequence[Event], candidate: Event) -> list[Violation]:
    raise NotImplementedError
