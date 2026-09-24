"""Outcome tracking: every signal ever created (displayed or not) gets its 1, 3 and 10
trading-day result, so CatalystEdge measures its own accuracy from day one.

Entry = open of the first session after the signal's as-of date (the earliest a
paper trade could have filled). Exit for horizon h = close of the h-th session
counting the entry session (h=1 is the entry day's close). Returns are net of the
same round-trip costs the paper account pays, and compared with SPY over the
identical window. Nothing is computed until the exit bar actually exists.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.core import calendar
from catalystedge.db.models import PriceDaily, Signal, SignalOutcome, Ticker
from catalystedge.paper.costs import CostModel

HORIZONS = (1, 3, 10)
BENCHMARK = "SPY"


def _bar(session: Session, symbol: str, day: dt.date, now: dt.datetime) -> PriceDaily | None:
    bar = session.get(PriceDaily, (symbol, day))
    return bar if bar is not None and bar.available_at <= now else None


def update_outcomes(session: Session, now: dt.datetime, costs: CostModel | None = None, lookback_days: int = 60) -> int:
    """Fill in any outcome whose exit bar is now available. Idempotent. Returns rows written."""
    costs = costs or CostModel()
    since = (now - dt.timedelta(days=lookback_days)).date()
    written = 0
    for sig in session.scalars(select(Signal).where(Signal.as_of_date >= since)):
        have = set(session.scalars(select(SignalOutcome.horizon_days).where(SignalOutcome.signal_id == sig.id)))
        missing = [h for h in HORIZONS if h not in have]
        if not missing:
            continue
        entry_day = calendar.next_session(sig.as_of_date)
        entry = _bar(session, sig.symbol, entry_day, now)
        spy_entry = _bar(session, BENCHMARK, entry_day, now)
        if entry is None:
            continue
        ticker = session.get(Ticker, sig.symbol)
        cost_pct = costs.round_trip_pct(float(ticker.adv20_usd) if ticker and ticker.adv20_usd else None)
        for h in missing:
            exit_day = calendar.add_sessions(entry_day, h - 1) if h > 1 else entry_day
            exit_bar = _bar(session, sig.symbol, exit_day, now)
            if exit_bar is None:
                continue
            gross = (float(exit_bar.close) / float(entry.open) - 1) * 100
            net = gross - cost_pct
            excess = None
            spy_exit = _bar(session, BENCHMARK, exit_day, now)
            if spy_entry is not None and spy_exit is not None:
                excess = net - (float(spy_exit.close) / float(spy_entry.open) - 1) * 100
            session.execute(insert(SignalOutcome).values(
                signal_id=sig.id, horizon_days=h, entry_date=entry_day, entry_price=entry.open, exit_date=exit_day,
                exit_price=exit_bar.close, return_pct=round(net, 4),
                excess_vs_spy_pct=round(excess, 4) if excess is not None else None, hit=net > 0,
            ).on_conflict_do_nothing())
            written += 1
    session.flush()
    return written
