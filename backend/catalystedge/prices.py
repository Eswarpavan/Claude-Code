"""End-of-day prices: fallback chain, sanity checks, point-in-time storage and reads.

Point-in-time rule: a daily bar is `available_at` the session's official close
+ 30 minutes (early closes included). Readers pass `as_of` and never see a bar
before it existed. Bars for sessions that have not closed yet are refused, so a
partial intraday bar can never be stored as if it were a daily bar.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.adapters.prices.base import Bar, PriceAdapter
from catalystedge.adapters.prices.stooq import StooqEOD
from catalystedge.adapters.prices.tiingo import TiingoEOD
from catalystedge.clock import Clock, ensure_utc
from catalystedge.config import Settings
from catalystedge.core import calendar
from catalystedge.core.http import HttpClient, SourceError
from catalystedge.db.models import PriceDaily


def build_price_adapters(settings: Settings, http: HttpClient) -> list[PriceAdapter]:
    import os

    # Yahoo's chart endpoint was removed: it is unofficial and its terms do not allow automated use.
    return [TiingoEOD(http, settings.tiingo_api_key), StooqEOD(http, os.environ.get("STOOQ_API_KEY"))]


@dataclass
class FetchResult:
    bars: list[Bar]
    source: str | None
    errors: dict[str, str] = field(default_factory=dict)
    rejected: int = 0


class PriceService:
    def __init__(self, adapters: Sequence[PriceAdapter], clock: Clock):
        self.adapters = list(adapters)
        self.clock = clock

    def daily(self, symbol: str, start: dt.date, end: dt.date) -> FetchResult:
        """First adapter that returns sane bars wins. Never returns a bar for an unfinished session."""
        last_done = calendar.last_completed_session(self.clock.now())
        end = min(end, last_done)
        errors: dict[str, str] = {}
        if start > end:
            return FetchResult([], None, errors)
        for adapter in self.adapters:
            if not adapter.enabled:
                errors[adapter.source_key] = adapter.disabled_reason or "disabled"
                continue
            try:
                bars = adapter.daily(symbol, start, end)
            except SourceError as e:
                errors[adapter.source_key] = adapter.http.redact(str(e))
                continue
            except Exception as e:  # a parser bug in one fallback must not stop the chain
                errors[adapter.source_key] = adapter.http.redact(f"{type(e).__name__}: {e}")
                continue
            good = [b for b in bars if b.is_sane() and calendar.is_session(b.date) and b.date <= last_done]
            if good:
                return FetchResult(good, adapter.source_key, errors, rejected=len(bars) - len(good))
            errors[adapter.source_key] = "no usable bars"
        return FetchResult([], None, errors)


def store_bars(session: Session, bars: Sequence[Bar], fetched_at: dt.datetime) -> int:
    if not bars:
        return 0
    fetched_at = ensure_utc(fetched_at)
    rows = [dict(symbol=b.symbol, date=b.date, open=b.open, high=b.high, low=b.low, close=b.close,
                 adj_close=b.adj_close, volume=b.volume, source=b.source, fetched_at=fetched_at,
                 available_at=calendar.eod_available_at(b.date)) for b in bars]
    stmt = insert(PriceDaily).values(rows)
    session.execute(stmt.on_conflict_do_update(index_elements=[PriceDaily.symbol, PriceDaily.date], set_={
        k: getattr(stmt.excluded, k) for k in ("open", "high", "low", "close", "adj_close", "volume", "source",
                                               "fetched_at")}))
    return len(rows)


def load_bars(session: Session, symbol: str, as_of: dt.datetime, lookback_sessions: int = 300) -> list[PriceDaily]:
    """Bars known at `as_of`, oldest first (point-in-time)."""
    rows = session.scalars(select(PriceDaily).where(
        PriceDaily.symbol == symbol, PriceDaily.available_at <= ensure_utc(as_of))
        .order_by(PriceDaily.date.desc()).limit(lookback_sessions)).all()
    return list(reversed(rows))


def bar_on(session: Session, symbol: str, day: dt.date) -> PriceDaily | None:
    return session.get(PriceDaily, (symbol, day))
