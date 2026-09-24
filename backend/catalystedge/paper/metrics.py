"""Daily marking, the S&P 500 comparison line, and performance statistics."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalystedge.core import calendar
from catalystedge.db.models import EquitySnapshot, PaperAccount, PaperPosition
from catalystedge.paper.costs import CostModel
from catalystedge.paper.engine import _d, open_positions

BENCHMARK = "SPY"


def mark_to_market(session: Session, acct: PaperAccount, day: dt.date, closes: dict[str, float],
                   spy_open: Callable[[dt.date], float | None], costs: CostModel | None = None) -> EquitySnapshot:
    """Equity at the close of `day`. The benchmark buys $start_cash of SPY at the first open after the
    account was created, with the same cost model, and is marked at SPY's close."""
    costs = costs or CostModel()
    positions_value = 0.0
    for p in open_positions(session, acct):
        px = closes.get(p.symbol)
        positions_value += float(p.qty) * (px if px is not None else float(p.cost_basis) / float(p.qty))
    settings = dict(acct.settings or {})
    bench = settings.get("benchmark")
    if bench is None:
        # First open strictly after the account existed (buying at an earlier open would be lookahead).
        created = acct.created_at.astimezone(calendar._cal().tz).date() if acct.created_at else day
        start_day = calendar.next_session(created)
        o = spy_open(start_day)
        if o:
            price, _, _ = costs.fill_price(o, "buy", 1e12)
            bench = {"symbol": BENCHMARK, "entry_date": start_day.isoformat(),
                     "shares": float(acct.start_cash) / price}
            settings["benchmark"] = bench
            acct.settings = settings
    spy_close = closes.get(BENCHMARK)
    spy_equity = bench["shares"] * spy_close if bench and spy_close else None
    snap = session.get(EquitySnapshot, (acct.id, day)) or EquitySnapshot(account_id=acct.id, date=day)
    snap.cash = acct.cash
    snap.positions_value = _d(positions_value)
    snap.equity = _d(float(acct.cash) + positions_value)
    snap.spy_benchmark_equity = _d(spy_equity) if spy_equity is not None else None
    session.merge(snap)
    session.flush()
    return snap


@dataclass
class Performance:
    start_equity: float
    equity: float
    total_return_pct: float
    spy_return_pct: float | None
    closed_trades: int
    win_rate_pct: float | None
    avg_win_pct: float | None
    avg_loss_pct: float | None
    profit_factor: float | None
    sharpe: float | None
    max_drawdown_pct: float | None
    days: int
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


def sharpe_ratio(daily_returns: list[float]) -> float | None:
    """Annualised, risk-free rate taken as 0 (no FRED key configured)."""
    if len(daily_returns) < 2:
        return None
    mean = sum(daily_returns) / len(daily_returns)
    var = sum((r - mean) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
    return None if var <= 0 else mean / math.sqrt(var) * math.sqrt(252)


def max_drawdown_pct(values: list[float]) -> float | None:
    if not values:
        return None
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst * 100


def performance(session: Session, acct: PaperAccount) -> Performance:
    snaps = session.scalars(select(EquitySnapshot).where(EquitySnapshot.account_id == acct.id)
                            .order_by(EquitySnapshot.date)).all()
    start = float(acct.start_cash)
    eq = [float(s.equity) for s in snaps]
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    closed = session.scalars(select(PaperPosition).where(PaperPosition.account_id == acct.id,
                                                         PaperPosition.status == "closed")).all()
    pnl_pct = [float(p.realized_pnl) / float(p.cost_basis) * 100 for p in closed]
    wins = [x for x in pnl_pct if x > 0]
    losses = [x for x in pnl_pct if x <= 0]
    gross_win = sum(float(p.realized_pnl) for p in closed if float(p.realized_pnl) > 0)
    gross_loss = -sum(float(p.realized_pnl) for p in closed if float(p.realized_pnl) <= 0)
    spy = [float(s.spy_benchmark_equity) for s in snaps if s.spy_benchmark_equity is not None]
    current = eq[-1] if eq else float(acct.cash)
    note = "risk-free rate taken as 0 for Sharpe"
    if len(closed) < 30:
        note += f"; only {len(closed)} closed trades: statistics are not yet meaningful"
    return Performance(
        start_equity=start, equity=round(current, 2), total_return_pct=round((current / start - 1) * 100, 2),
        spy_return_pct=round((spy[-1] / start - 1) * 100, 2) if spy else None,
        closed_trades=len(closed),
        win_rate_pct=round(100 * len(wins) / len(closed), 1) if closed else None,
        avg_win_pct=round(sum(wins) / len(wins), 2) if wins else None,
        avg_loss_pct=round(sum(losses) / len(losses), 2) if losses else None,
        profit_factor=round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        sharpe=round(s, 2) if (s := sharpe_ratio(rets)) is not None else None,
        max_drawdown_pct=round(d, 2) if (d := max_drawdown_pct(eq)) is not None else None,
        days=len(snaps), note=note,
    )
