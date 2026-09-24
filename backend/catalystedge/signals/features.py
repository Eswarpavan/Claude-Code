"""Point-in-time price features for a candidate signal.

Pure functions over daily bars that were already filtered to `available_at <= as_of`
(see prices.load_bars), so the same code serves live signals and the backtester
without lookahead.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Protocol


class BarLike(Protocol):
    date: dt.date
    open: object
    high: object
    low: object
    close: object
    volume: int


@dataclass(frozen=True)
class PriceFeatures:
    last_date: dt.date
    last_close: float
    atr20_pct: float | None          # average true range over 20 sessions, % of close
    adv20_usd: float | None          # average dollar volume, 20 sessions
    volume_ratio: float | None       # last session volume / 20-session average (before it)
    sma50: float | None
    above_sma50: bool | None
    mom5_pct: float | None
    mom20_pct: float | None
    mom60_pct: float | None
    pre_event_close: float | None    # last close strictly before the event became public
    reaction_pct: float | None       # last close vs pre-event close
    reaction_atr: float | None       # reaction in ATR units
    gap_pct: float | None            # first open after the event vs pre-event close
    sessions_since_event: int | None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["last_date"] = self.last_date.isoformat()
        return d


def _f(x: object) -> float:
    return float(x)  # Decimal or float


def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0


def compute(bars: Sequence[BarLike], event_session: dt.date | None) -> PriceFeatures | None:
    """`event_session`: the first session whose close could reflect the event (the session
    of `available_at` if before the close, else the next one). None when unknown."""
    if not bars:
        return None
    closes = [_f(b.close) for b in bars]
    last = bars[-1]
    last_close = closes[-1]

    trs: list[float] = []
    for i in range(max(1, len(bars) - 20), len(bars)):
        h, lo, pc = _f(bars[i].high), _f(bars[i].low), closes[i - 1]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    atr_pct = (sum(trs) / len(trs)) / last_close * 100 if trs else None

    window = bars[-20:]
    adv = sum(_f(b.close) * b.volume for b in window) / len(window) if window else None
    prior_vol = [b.volume for b in bars[-21:-1]]
    vol_ratio = (last.volume / (sum(prior_vol) / len(prior_vol))) if prior_vol and sum(prior_vol) > 0 else None
    sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None

    def mom(n: int) -> float | None:
        return _pct(last_close, closes[-1 - n]) if len(closes) > n else None

    pre_close = gap = reaction = reaction_atr = None
    since = None
    if event_session is not None:
        before = [b for b in bars if b.date < event_session]
        after = [b for b in bars if b.date >= event_session]
        if before:
            pre_close = _f(before[-1].close)
            reaction = _pct(last_close, pre_close) if after else 0.0
            if after:
                gap = _pct(_f(after[0].open), pre_close)
            if atr_pct:
                reaction_atr = reaction / atr_pct
        since = len(after)

    return PriceFeatures(
        last_date=last.date, last_close=last_close, atr20_pct=atr_pct, adv20_usd=adv, volume_ratio=vol_ratio,
        sma50=sma50, above_sma50=(last_close > sma50) if sma50 else None,
        mom5_pct=mom(5), mom20_pct=mom(20), mom60_pct=mom(60), pre_event_close=pre_close,
        reaction_pct=reaction, reaction_atr=reaction_atr, gap_pct=gap, sessions_since_event=since,
    )
