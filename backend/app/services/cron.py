"""Five-field cron expressions (minute hour day-of-month month day-of-week, UTC; day of week 0-6 or sun-sat)."""
import re
from datetime import datetime, timedelta
from typing import NamedTuple

from celery.schedules import crontab_parser

_FIELDS = ((60, 0), (24, 0), (31, 1), (12, 1), (7, 0))
_NUMERIC = re.compile(r"[\d*/,\-]+")
LOOKBACK = timedelta(days=1)  # runs missed for longer are not caught up


class Cron(NamedTuple):
    minute: frozenset
    hour: frozenset
    day: frozenset
    month: frozenset
    weekday: frozenset
    any_day: bool
    any_weekday: bool


def parse(expr: str) -> Cron:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron needs 5 fields")
    sets = []
    for i, (field, (max_, min_)) in enumerate(zip(fields, _FIELDS)):
        if i < 4 and not _NUMERIC.fullmatch(field):
            raise ValueError(f"invalid cron field {field!r}")
        try:
            sets.append(frozenset(crontab_parser(max_, min_).parse(field.lower())))
        except Exception as e:
            raise ValueError(f"invalid cron field {field!r}") from e
    return Cron(*sets, any_day=fields[2] == "*", any_weekday=fields[4] == "*")


def matches(c: Cron, at: datetime) -> bool:
    if at.minute not in c.minute or at.hour not in c.hour or at.month not in c.month:
        return False
    day, weekday = at.day in c.day, at.isoweekday() % 7 in c.weekday
    if c.any_day or c.any_weekday:
        return day and weekday
    return day or weekday  # both restricted: either, as in cron


def due(c: Cron, last: datetime, now: datetime) -> datetime | None:
    """The latest minute in (last, now] that `c` matches, looking back at most LOOKBACK."""
    at = now.replace(second=0, microsecond=0)
    stop = max(last, now - LOOKBACK)
    while at > stop:
        if matches(c, at):
            return at
        at -= timedelta(minutes=1)
    return None
