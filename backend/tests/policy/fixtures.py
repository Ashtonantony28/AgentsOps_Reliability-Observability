import json
from dataclasses import dataclass
from pathlib import Path

from app.policy.events import Event
from app.schemas.trace import EventIn

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class Expected:
    policy: str
    at_seq: int
    counterexample: tuple[int, ...]


@dataclass(frozen=True)
class Fixture:
    name: str
    description: str
    source: str
    events: list[Event]
    expected: list[Expected]


def load_fixture(path: Path) -> Fixture:
    raw = json.loads(path.read_text(encoding="utf-8"))
    events = [EventIn.model_validate(e) for e in raw["events"]]
    return Fixture(
        name=raw["name"],
        description=raw["description"],
        source=raw["source"],
        events=[Event(e.seq, e.kind, e.name, e.status, dict(e.attrs)) for e in events],
        expected=[Expected(x["policy"], x["at_seq"], tuple(x["counterexample"])) for x in raw["expected"]],
    )


def load_fixtures() -> list[Fixture]:
    return [load_fixture(p) for p in sorted(FIXTURE_DIR.glob("*.json"))]
