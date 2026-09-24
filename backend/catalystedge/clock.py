"""Injectable clock. Production code never calls datetime.now() directly, so tests
(and fixture replays) can pin "now" and prove there is no lookahead."""

from __future__ import annotations

import datetime as dt
from typing import Protocol

UTC = dt.UTC


class Clock(Protocol):
    def now(self) -> dt.datetime: ...


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(UTC)


class FrozenClock:
    def __init__(self, at: dt.datetime):
        self.at = ensure_utc(at)

    def now(self) -> dt.datetime:
        return self.at

    def advance(self, **kwargs: float) -> None:
        self.at = self.at + dt.timedelta(**kwargs)


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """Reject naive datetimes instead of guessing their zone."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"naive datetime not allowed: {value!r}")
    return value.astimezone(UTC)
