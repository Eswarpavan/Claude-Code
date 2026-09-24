"""Daily price bars and the price-adapter interface."""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass

from catalystedge.core.http import HttpClient


@dataclass(frozen=True)
class Bar:
    symbol: str
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    adj_close: float | None
    volume: int
    source: str

    def is_sane(self) -> bool:
        return (self.open > 0 and self.close > 0 and self.high >= self.low > 0 and self.volume >= 0
                and self.low <= min(self.open, self.close) + 1e-6 and self.high >= max(self.open, self.close) - 1e-6)


class PriceUnavailable(Exception):
    pass


class PriceAdapter(ABC):
    source_key: str
    fragile: bool = False
    disabled_reason: str | None = None

    def __init__(self, http: HttpClient):
        self.http = http

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    @abstractmethod
    def daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        """Daily bars with dates in [start, end], oldest first."""
