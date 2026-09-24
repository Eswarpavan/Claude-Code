"""Historical events -> point-in-time backtest rows.

For every positive, signal-eligible event:
  decision  = close of the first session whose close can reflect the event
              (same rule as the live engine: engine.event_session)
  features  = price features from bars up to and including the decision session only,
              rule score from the same rules.score() the live engine uses
  entry     = OPEN of the next session (never the decision day: rule 2)
  labels    = net return to the close of session h (h = 1, 3, 5, 10), after the same
              round-trip costs the paper account pays
  trade     = the paper account's exits: stop / target evaluated on each close,
              time stop after 10 sessions, all filled at the NEXT open (gaps included)
  spy       = SPY over the identical entry/exit window
No row ever uses a bar dated after its decision session for features (rule 7), and the
tests assert it.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from catalystedge.core import calendar
from catalystedge.paper.costs import CostModel
from catalystedge.signals import rules
from catalystedge.signals.engine import event_session, levels
from catalystedge.signals.features import compute
from catalystedge.signals.priors import PRIORS

HORIZONS = (1, 3, 5, 10)
TIME_STOP = 10
NOVELTY_DAYS = 30


@dataclass
class HistEvent:
    symbol: str
    event_type: str
    origin: str
    headline: str
    available_at: dt.datetime
    materiality: float = 0.6
    credibility: float = 1.0
    strength: str | None = "normal"
    sentiment: dict | None = None
    source_key: str | None = None
    novelty: float = 1.0
    ref: str = ""


@dataclass
class Bar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Row:
    event: HistEvent
    decision_date: dt.date
    entry_date: dt.date
    entry_price: float
    features: dict
    rule_score: float
    skip_reason: str | None
    labels: dict[int, float] = field(default_factory=dict)       # h -> net return %
    trade_return: float | None = None
    trade_exit_date: dt.date | None = None
    trade_exit_reason: str | None = None
    trade_sessions: int | None = None
    spy_return: float | None = None
    cost_pct: float = 0.0

    @property
    def catalyst(self) -> str:
        return self.event.event_type


def _index(bars: Sequence[Bar]) -> dict[dt.date, int]:
    return {b.date: i for i, b in enumerate(bars)}


def mark_novelty(events: list[HistEvent]) -> None:
    """Novelty 0.3 if the same ticker had the same event type in the previous 30 days."""
    last: dict[tuple[str, str], dt.datetime] = {}
    for e in sorted(events, key=lambda x: x.available_at):
        k = (e.symbol, e.event_type)
        prev = last.get(k)
        e.novelty = 0.3 if prev is not None and (e.available_at - prev) < dt.timedelta(days=NOVELTY_DAYS) else 1.0
        last[k] = e.available_at


def simulate_trade(bars: Sequence[Bar], entry_i: int, stop: float, target: float, cost_pct: float
                   ) -> tuple[float, dt.date, str, int] | None:
    """Paper-account exits. Returns (net %, exit date, reason, sessions held) or None if the
    exit bar does not exist yet."""
    entry = bars[entry_i].open
    for k in range(entry_i, min(len(bars), entry_i + TIME_STOP)):
        c = bars[k].close
        reason = "stop" if c <= stop else "target" if c >= target else None
        if reason is None and k == entry_i + TIME_STOP - 1:
            reason = "time_stop"
        if reason:
            if k + 1 >= len(bars):
                return None
            exit_bar = bars[k + 1]                   # next open, so a gap through the stop is honest
            return (exit_bar.open / entry - 1) * 100 - cost_pct, exit_bar.date, reason, k + 1 - entry_i
    return None


def build_rows(events: list[HistEvent], bars_by_symbol: dict[str, list[Bar]], spy: list[Bar],
               costs: CostModel | None = None) -> list[Row]:
    costs = costs or CostModel()
    mark_novelty(events)
    spy_ix = _index(spy)
    groups: dict[tuple[str, str, dt.date], list[HistEvent]] = defaultdict(list)
    for e in events:
        if e.event_type in PRIORS:
            groups[(e.symbol, e.event_type, event_session(e.available_at))].append(e)
    rows: list[Row] = []
    for (symbol, catalyst, decision), evs in sorted(groups.items(), key=lambda kv: kv[0][2]):
        bars = bars_by_symbol.get(symbol) or []
        ix = _index(bars)
        if decision not in ix:
            continue
        d_i = ix[decision]
        if d_i + 1 >= len(bars) or d_i < 25:
            continue
        known = bars[: d_i + 1]                      # point in time: nothing after the decision close
        f = compute(known, decision)
        prior = PRIORS[catalyst]
        r = rules.score(prior, evs, f)
        entry_i = d_i + 1
        entry_bar = bars[entry_i]
        cost = costs.round_trip_pct(f.adv20_usd if f else None)
        feats = {"rule_score": r.score, "catalyst": catalyst, "rule_components": r.components,
                 "price": f.as_dict() if f else None, "catalyst_weight": prior.weight, "n_events": len(evs),
                 "origins": sorted({e.origin for e in evs}), "model_disagrees": r.model_disagrees}
        row = Row(evs[0], decision, entry_bar.date, entry_bar.open, feats, r.score, r.skip_reason, cost_pct=cost)
        for h in HORIZONS:
            k = entry_i + h - 1
            if k < len(bars):
                row.labels[h] = (bars[k].close / entry_bar.open - 1) * 100 - cost
        if f is not None:
            _, stop, target, _ = levels(prior, f)
            t = simulate_trade(bars, entry_i, stop, target, cost)
            if t is not None:
                row.trade_return, row.trade_exit_date, row.trade_exit_reason, row.trade_sessions = t
                if entry_bar.date in spy_ix and t[1] in spy_ix:
                    s0, s1 = spy[spy_ix[entry_bar.date]].open, spy[spy_ix[t[1]]].open
                    row.spy_return = (s1 / s0 - 1) * 100 - costs.round_trip_pct(1e10)
        rows.append(row)
    return rows


def sessions_after(day: dt.date, n: int) -> dt.date:
    return calendar.add_sessions(day, n)
