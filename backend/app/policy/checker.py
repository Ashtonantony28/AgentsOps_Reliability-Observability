import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.policy.ast import Policy
from app.policy.counterexample import minimal_counterexample
from app.policy.encode import first_violations
from app.policy.events import Event
from app.policy.monitor import plan_for


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
    return [Violation(p.policy_id, p.policy.name, p.policy.severity, at, minimal_counterexample(p, events, at),
                      f"{p.policy.name} violated at seq {at}")
            for p, at in zip(policies, firsts) if at is not None]


class Monitor:
    """Live checker: one `step` per event in seq order, state serialisable between steps."""

    def __init__(self, policies: Sequence[CompiledPolicy]):
        self.policies = list(policies)
        self._plans = [plan_for(p.policy) for p in self.policies]
        self._states = [plan.initial() for plan in self._plans]
        self._done = [False] * len(self.policies)
        self.failing: list[str] = []  # ids of the policies false at the last stepped event, violated before or not
        self.next_seq = 0

    def step(self, event: Event) -> list[Violation]:
        if event.seq != self.next_seq:
            raise ValueError(f"expected seq {self.next_seq}, got {event.seq}")
        self.next_seq += 1
        out, self.failing = [], []
        for i, (p, plan) in enumerate(zip(self.policies, self._plans)):
            if plan.step(self._states[i], event):
                continue
            self.failing.append(p.policy_id)
            if not self._done[i]:
                self._done[i] = True
                out.append(Violation(p.policy_id, p.policy.name, p.policy.severity, event.seq, (),
                                     f"{p.policy.name} violated at seq {event.seq}"))
        return out

    def check(self, event: Event) -> list[Violation]:
        """Policies false at `event` as the next event, whether or not violated before; the state is unchanged."""
        if event.seq != self.next_seq:
            raise ValueError(f"expected seq {self.next_seq}, got {event.seq}")
        return [Violation(p.policy_id, p.policy.name, p.policy.severity, event.seq, (),
                          f"{p.policy.name} would be violated at seq {event.seq}")
                for p, plan, state in zip(self.policies, self._plans, self._states)
                if not plan.step([dict(x) if isinstance(x, dict) else x for x in state], event)]

    def snapshot(self) -> bytes:
        return json.dumps({"v": 1, "n": self.next_seq, "p": [
            [p.policy_id, plan.digest, done, state]
            for p, plan, done, state in zip(self.policies, self._plans, self._done, self._states)
        ]}, separators=(",", ":")).encode()

    @classmethod
    def restore(cls, policies: Sequence[CompiledPolicy], blob: bytes) -> "Monitor":
        """Raises ValueError unless `blob` is a snapshot of a Monitor over the same policies, in order."""
        m = cls(policies)
        try:
            data = json.loads(blob)
            rows, m.next_seq = data["p"], data["n"]
            if data["v"] != 1 or len(rows) != len(m.policies):
                raise ValueError
            for i, (p, plan, (pid, digest, done, state)) in enumerate(zip(m.policies, m._plans, rows)):
                if pid != p.policy_id or digest != plan.digest or len(state) != len(plan.nodes):
                    raise ValueError
                m._done[i], m._states[i] = done, state
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError("snapshot does not match these policies") from e
        return m


def would_violate(policies: Sequence[CompiledPolicy], prefix: Sequence[Event], candidate: Event) -> list[Violation]:
    m = Monitor(policies)
    for e in prefix:
        m.step(e)
    return m.check(candidate)
