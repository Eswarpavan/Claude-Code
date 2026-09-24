"""Outcome tracking: 1/3/10-day results, net of costs, vs SPY, only once exit bars exist."""

import datetime as dt

import pytest
from sqlalchemy import select

from catalystedge.adapters.prices.base import Bar
from catalystedge.core import calendar
from catalystedge.db.models import SignalOutcome
from catalystedge.outcomes.tracker import update_outcomes
from catalystedge.prices import store_bars
from tests.test_paper import THU, make_signal

pytestmark = pytest.mark.db
UTC = dt.UTC


def series(symbol, start, closes, opens=None):
    days = [start] + [calendar.add_sessions(start, i) for i in range(1, len(closes))]
    opens = opens or closes
    return [Bar(symbol, d, o, max(o, c) + 1, min(o, c) - 1, c, None, 1000, "t")
            for d, o, c in zip(days, opens, closes, strict=True)]


def test_outcomes_net_of_costs_and_vs_spy(db):
    sig = make_signal(db, "ABC", as_of=THU)                         # entry = FRI open
    fri = calendar.next_session(THU)
    store_bars(db, series("ABC", fri, [102.0] * 10, opens=[100.0] + [102.0] * 9), fetched_at=dt.datetime.now(UTC))
    store_bars(db, series("SPY", fri, [501.0] * 10, opens=[500.0] * 10), fetched_at=dt.datetime.now(UTC))
    now = calendar.eod_available_at(calendar.add_sessions(fri, 9)) + dt.timedelta(minutes=1)
    assert update_outcomes(db, now) == 3
    rows = {o.horizon_days: o for o in db.scalars(select(SignalOutcome).where(SignalOutcome.signal_id == sig.id))}
    assert rows[1].exit_date == fri and rows[10].exit_date == calendar.add_sessions(fri, 9)
    assert rows[3].return_pct == pytest.approx(2.0 - 0.2)          # +2% gross minus 20 bps round trip (liquid)
    assert rows[3].excess_vs_spy_pct == pytest.approx(1.8 - 0.2)
    assert all(r.hit for r in rows.values())
    assert update_outcomes(db, now) == 0                            # idempotent


def test_no_outcome_before_the_exit_bar_is_available(db):
    make_signal(db, "ABC", as_of=THU)
    fri = calendar.next_session(THU)
    store_bars(db, series("ABC", fri, [99.0] * 10, opens=[100.0] * 10), fetched_at=dt.datetime.now(UTC))
    # Monday 15:00 ET: FRI (h=1) is known; the 3-day exit (TUE) is not.
    now = dt.datetime(2026, 9, 28, 19, 0, tzinfo=UTC)
    assert update_outcomes(db, now) == 1
    (row,) = db.scalars(select(SignalOutcome)).all()
    assert row.horizon_days == 1 and row.hit is False


def test_undisplayed_signals_are_tracked_too(db):
    make_signal(db, "LOW", confidence=40, displayed=False, as_of=THU)
    fri = calendar.next_session(THU)
    store_bars(db, series("LOW", fri, [10.0] * 3), fetched_at=dt.datetime.now(UTC))
    assert update_outcomes(db, calendar.eod_available_at(fri) + dt.timedelta(hours=1)) == 1
