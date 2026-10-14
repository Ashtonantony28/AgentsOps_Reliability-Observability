from dataclasses import dataclass, field
from typing import Any, Sequence

from app.policy.ast import Policy
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
    raise NotImplementedError


def check_trace(policies: Sequence[CompiledPolicy], events: Sequence[Event]) -> list[Violation]:
    raise NotImplementedError


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
