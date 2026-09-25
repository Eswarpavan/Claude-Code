"""The $100 paper-trading engine.

Timeline (hard rules 2 and 7):
  decision after the close of day D   -> orders with execute_on = next session after D
  execution at the open of that day   -> fill at the official open +/- spread and slippage
  exits evaluated on each close        -> sell orders for the next session's open
So a position bought at the open of day E can first be sold at the open of the session
after E. A stop is never assumed to fill at the stop price: it fills at the next open,
gaps included.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from catalystedge.core import calendar
from catalystedge.db.models import (
    BuyCandidate,
    CalibrationSnapshot,
    Event,
    PaperAccount,
    PaperFill,
    PaperOrder,
    PaperPosition,
    Signal,
    Ticker,
)
from catalystedge.paper.costs import CostModel
from catalystedge.paper.settings import PaperSettings

DEFAULT_ACCOUNT = "default"
Q6 = Decimal("0.000001")


class SameDaySellError(ValueError):
    """Hard rule 2: a position can only be sold on a later session than its entry."""


class OrderRejected(ValueError):
    pass


def _d(x: float | Decimal) -> Decimal:
    return Decimal(str(round(float(x), 6)))


# ----------------------------------------------------------------------------- account & gates


def get_account(session: Session, name: str = DEFAULT_ACCOUNT) -> PaperAccount:
    acct = session.scalar(select(PaperAccount).where(PaperAccount.name == name))
    if acct is None:
        settings = PaperSettings()
        acct = PaperAccount(name=name, start_cash=_d(settings.start_cash), cash=_d(settings.start_cash),
                            settings=settings.to_json())
        session.add(acct)
        session.flush()
    return acct


def settings_of(acct: PaperAccount) -> PaperSettings:
    return PaperSettings.from_json(acct.settings)


@dataclass(frozen=True)
class AutoBuyGate:
    allowed: bool
    closed_trades: int
    has_backtest_calibration: bool
    reason: str


def auto_buy_gate(session: Session, acct: PaperAccount) -> AutoBuyGate:
    """Rule 6: auto-buy needs the user's switch AND evidence (a passing backtest calibration,
    or at least N closed paper trades)."""
    s = settings_of(acct)
    closed = session.scalar(select(func.count()).select_from(PaperPosition).where(
        PaperPosition.account_id == acct.id, PaperPosition.status == "closed")) or 0
    has_bt = session.scalar(select(func.count()).select_from(CalibrationSnapshot).where(
        CalibrationSnapshot.basis == "backtest", CalibrationSnapshot.verdict.in_(("good", "fair")))) or 0
    evidence = has_bt > 0 or closed >= s.auto_buy_min_closed_trades
    if not s.auto_buy:
        reason = "auto-buy is switched off (default)"
    elif not evidence:
        reason = (f"auto-buy is on but locked: needs a passing backtest calibration or "
                  f"{s.auto_buy_min_closed_trades} closed paper trades (have {closed})")
    else:
        reason = "auto-buy enabled and supported by evidence"
    return AutoBuyGate(s.auto_buy and evidence, closed, has_bt > 0, reason)


# ----------------------------------------------------------------------------- sizing


def equity(session: Session, acct: PaperAccount, prices: dict[str, float]) -> float:
    total = float(acct.cash)
    for p in open_positions(session, acct):
        total += float(p.qty) * prices.get(p.symbol, float(p.cost_basis) / float(p.qty))
    return total


def size_order(eq: float, cash: float, entry_ref: float, stop: float, s: PaperSettings) -> tuple[float, str | None]:
    """Fixed-fractional sizing: risk `risk_per_trade` of equity to the stop, capped by
    max position size and the cash floor. Returns (notional, skip reason or None)."""
    stop_dist = (entry_ref - stop) / entry_ref
    if stop_dist <= 0:
        return 0.0, "invalid_stop"
    by_risk = eq * s.risk_per_trade / stop_dist
    by_cap = eq * s.max_position_pct
    by_cash = cash - eq * s.cash_floor_pct
    notional = min(by_risk, by_cap, by_cash)
    if notional < s.min_order_usd:
        return 0.0, "cash_floor" if by_cash < s.min_order_usd else "below_min_order"
    return round(notional, 2), None


def open_positions(session: Session, acct: PaperAccount) -> list[PaperPosition]:
    return list(session.scalars(select(PaperPosition).where(
        PaperPosition.account_id == acct.id, PaperPosition.status == "open")))


def _pending_buys(session: Session, acct: PaperAccount) -> list[PaperOrder]:
    return list(session.scalars(select(PaperOrder).where(
        PaperOrder.account_id == acct.id, PaperOrder.side == "buy", PaperOrder.status == "pending")))


# ----------------------------------------------------------------------------- decisions


def evaluate_candidates(session: Session, acct: PaperAccount, decision_date: dt.date, signals: list[Signal],
                        prices: dict[str, float]) -> list[BuyCandidate]:
    """Record every displayed signal as bought or skipped, with every reason that applies (rule 8).
    Creates buy orders for the next session only when auto-buy is allowed."""
    s = settings_of(acct)
    gate = auto_buy_gate(session, acct)
    held = {p.symbol for p in open_positions(session, acct)} | {o.symbol for o in _pending_buys(session, acct)}
    n_open = len(held)
    eq = equity(session, acct, prices)
    cash = float(acct.cash) - sum(float(o.notional_usd or 0) for o in _pending_buys(session, acct))
    out: list[BuyCandidate] = []
    for sig in sorted(signals, key=lambda x: (-x.confidence, -x.expected_return_pct)):
        if not sig.displayed:
            continue
        existing = session.scalar(select(BuyCandidate).where(
            BuyCandidate.account_id == acct.id, BuyCandidate.signal_id == sig.id,
            BuyCandidate.decision_date == decision_date))
        if existing is not None:
            out.append(existing)
            continue
        reasons: list[str] = []
        ticker = session.get(Ticker, sig.symbol)
        if not gate.allowed:
            reasons.append("auto_buy_off" if not s.auto_buy else "auto_buy_locked_uncalibrated")
        if sig.confidence < s.auto_buy_threshold:
            reasons.append("below_auto_buy_threshold")
        if sig.symbol in held:
            reasons.append("already_held")
        if n_open >= s.max_open_positions:
            reasons.append("max_positions")
        if float(sig.entry_ref_price) < s.min_price:
            reasons.append("price_below_minimum")
        if ticker is not None and ticker.adv20_usd is not None and float(ticker.adv20_usd) < s.min_adv_usd:
            reasons.append("illiquid")
        notional, size_reason = size_order(eq, cash, float(sig.entry_ref_price), float(sig.stop_price), s)
        if size_reason:
            reasons.append(size_reason)
        details = {"suggested_notional": notional, "auto_buy_gate": gate.reason, "confidence": sig.confidence,
                   "calibrated": sig.calibrated}
        if reasons:
            cand = BuyCandidate(account_id=acct.id, signal_id=sig.id, decision_date=decision_date,
                                decision="skipped", skip_reasons=reasons, details=details)
        else:
            _submit_buy(session, acct, sig, decision_date, notional, origin="auto")
            cand = BuyCandidate(account_id=acct.id, signal_id=sig.id, decision_date=decision_date,
                                decision="bought", skip_reasons=[], details=details)
            held.add(sig.symbol)
            n_open += 1
            cash -= notional
        session.add(cand)
        out.append(cand)
    session.flush()
    return out


def _submit_buy(session: Session, acct: PaperAccount, sig: Signal, decision_date: dt.date, notional: float,
                origin: str) -> PaperOrder:
    execute_on = calendar.next_session(decision_date)
    order = PaperOrder(account_id=acct.id, signal_id=sig.id, symbol=sig.symbol, side="buy",
                       notional_usd=_d(notional), decision_date=decision_date, execute_on=execute_on,
                       origin=origin, status="pending",
                       idempotency_key=f"buy:{acct.id}:{sig.symbol}:{decision_date.isoformat()}")
    existing = session.scalar(select(PaperOrder).where(PaperOrder.idempotency_key == order.idempotency_key))
    if existing is not None:
        return existing
    session.add(order)
    session.flush()
    return order


def manual_buy(session: Session, acct: PaperAccount, sig: Signal, now: dt.datetime,
               prices: dict[str, float] | None = None) -> PaperOrder:
    """One-click buy. Always executes at the NEXT session's open, never today."""
    s = settings_of(acct)
    if not sig.displayed:
        raise OrderRejected("only displayed (positive, >= threshold) signals can be bought")
    if sig.symbol in {p.symbol for p in open_positions(session, acct)}:
        raise OrderRejected(f"already holding {sig.symbol}")
    today = now.astimezone(calendar._cal().tz).date()
    eq = equity(session, acct, prices or {})
    cash = float(acct.cash) - sum(float(o.notional_usd or 0) for o in _pending_buys(session, acct))
    notional, reason = size_order(eq, cash, float(sig.entry_ref_price), float(sig.stop_price), s)
    if reason:
        raise OrderRejected(f"cannot size order: {reason}")
    order = _submit_buy(session, acct, sig, today, notional, origin="manual")
    existing = session.scalar(select(BuyCandidate).where(
        BuyCandidate.account_id == acct.id, BuyCandidate.signal_id == sig.id, BuyCandidate.decision_date == today))
    if existing is None:
        session.add(BuyCandidate(account_id=acct.id, signal_id=sig.id, decision_date=today, decision="bought",
                                 skip_reasons=[], details={"manual": True, "suggested_notional": notional}))
    session.flush()
    return order


def submit_sell(session: Session, pos: PaperPosition, decision_date: dt.date, reason: str) -> PaperOrder:
    """The only way to create a sell. Raises SameDaySellError if it would execute on the entry day or earlier."""
    execute_on = calendar.next_session(decision_date)
    if execute_on <= pos.entry_date:
        raise SameDaySellError(f"{pos.symbol}: sell would execute {execute_on}, entry was {pos.entry_date}")
    key = f"sell:{pos.id}"
    existing = session.scalar(select(PaperOrder).where(PaperOrder.idempotency_key == key))
    if existing is not None:
        return existing
    order = PaperOrder(account_id=pos.account_id, position_id=pos.id, symbol=pos.symbol, side="sell", qty=pos.qty,
                       decision_date=decision_date, execute_on=execute_on, origin="exit" if reason != "manual"
                       else "manual", exit_reason=reason, status="pending", idempotency_key=key)
    session.add(order)
    session.flush()
    return order


def manual_sell(session: Session, acct: PaperAccount, position_id: int, now: dt.datetime) -> PaperOrder:
    pos = session.get(PaperPosition, position_id)
    if pos is None or pos.account_id != acct.id or pos.status != "open":
        raise OrderRejected("no such open position")
    today = now.astimezone(calendar._cal().tz).date()
    return submit_sell(session, pos, today, "manual")


# ----------------------------------------------------------------------------- exits


def evaluate_exits(session: Session, acct: PaperAccount, as_of: dt.date, closes: dict[str, float]) -> list[PaperOrder]:
    """After the close of `as_of`: target, stop, time stop, signal decay. Orders go to the next open."""
    orders = []
    for pos in open_positions(session, acct):
        close = closes.get(pos.symbol)
        if close is None:
            continue
        reason = None
        if close <= float(pos.stop_price):
            reason = "stop"
        elif close >= float(pos.target_price):
            reason = "target"
        elif as_of >= pos.time_stop_date:
            reason = "time_stop"
        elif _signal_decayed(session, pos, as_of):
            reason = "signal_decay"
        if reason is None:
            continue
        pos.why = {**(pos.why or {}), "exit_trigger": {"reason": reason, "on_close_of": as_of.isoformat(),
                                                      "close": close}}
        orders.append(submit_sell(session, pos, as_of, reason))
    session.flush()
    return orders


def _signal_decayed(session: Session, pos: PaperPosition, as_of: dt.date) -> bool:
    """A negative or mixed event for the symbol after entry means the thesis is broken."""
    since = calendar.session_open(pos.entry_date)
    until = calendar.eod_available_at(as_of)
    return session.scalar(select(func.count()).select_from(Event).where(
        Event.symbol == pos.symbol, Event.polarity.in_(("negative", "mixed")),
        Event.available_at >= since, Event.available_at <= until)) > 0


# ----------------------------------------------------------------------------- execution


@dataclass
class ExecutionReport:
    filled: list[PaperOrder]
    waiting: list[PaperOrder]
    cancelled: list[PaperOrder]


def execute_orders(session: Session, acct: PaperAccount, day: dt.date,
                   open_price: Callable[[str, dt.date], float | None],
                   adv: Callable[[str], float | None] = lambda s: None,
                   costs: CostModel | None = None, last_completed: dt.date | None = None) -> ExecutionReport:
    """Fill pending orders scheduled on or before `day` at that session's official open.

    Orders whose day has fully passed with no open price available are cancelled
    (`missed_session`), never filled at a later, different price."""
    costs = costs or CostModel()
    s = settings_of(acct)
    report = ExecutionReport([], [], [])
    pending = session.scalars(select(PaperOrder).where(
        PaperOrder.account_id == acct.id, PaperOrder.status == "pending", PaperOrder.execute_on <= day)
        .order_by(PaperOrder.side.desc(), PaperOrder.id)).all()   # sells first: frees cash
    for order in pending:
        raw = open_price(order.symbol, order.execute_on)
        if raw is None or raw <= 0:
            if last_completed is not None and order.execute_on <= last_completed:
                order.status, order.reject_reason = "cancelled", "missed_session: no open price for that day"
                report.cancelled.append(order)
            else:
                report.waiting.append(order)
            continue
        if order.side == "buy":
            _fill_buy(session, acct, order, raw, adv(order.symbol), costs, s, report)
        else:
            _fill_sell(session, acct, order, raw, adv(order.symbol), costs, report)
    session.flush()
    return report


def catalyst_band(move_pct: float | None) -> str | None:
    """Move since the catalyst (vs the pre-news close)."""
    if move_pct is None:
        return None
    if move_pct < 0:
        return "reversed"
    return "very_early" if move_pct <= 5 else "early" if move_pct <= 10 else "borderline" if move_pct <= 15 \
        else "extended"


def fill_check(sig: Signal | None, raw_open: float, s: PaperSettings) -> dict:
    """The two separate re-checks at the next-day open, each logged as its own reason:
      gap_up_priced_in          open > max_gap_up_pct above the decision-day close (the signal price)
      extended_since_catalyst   open > max_move_since_catalyst_pct above the pre-news close
      reversed_since_catalyst   open below the pre-news close (the news move has fully reversed)"""
    price = (sig.features or {}).get("price") or {} if sig else {}
    ref = float(sig.entry_ref_price) if sig else raw_open
    pre = price.get("pre_event_close")
    gap = (raw_open / ref - 1) * 100
    move = (raw_open / pre - 1) * 100 if pre else None
    reasons, text = [], []
    if gap > s.max_gap_up_pct:
        reasons.append("gap_up_priced_in")
        text.append(f"gap_up_priced_in: opened {gap:.1f}% above the signal price")
    if move is not None and move > s.max_move_since_catalyst_pct:
        reasons.append("extended_since_catalyst")
        text.append(f"extended_since_catalyst: opened {move:.1f}% above the pre-news close")
    if move is not None and move < 0:
        reasons.append("reversed_since_catalyst")
        text.append(f"reversed_since_catalyst: opened {move:.1f}% below the pre-news close")
    return {"gap_pct": round(gap, 2), "move_at_decision_pct": price.get("reaction_pct"),
            "band_at_decision": catalyst_band(price.get("reaction_pct")),
            "move_at_fill_pct": round(move, 2) if move is not None else None, "band_at_fill": catalyst_band(move),
            "reasons": reasons, "text": "; ".join(text)}


def _record_fill_check(session, acct, order, check: dict, skipped: bool) -> None:
    """Keep the outcome on the buy option so it shows in the Portfolio's list and feeds the qualify-at-fill rate."""
    if not order.signal_id:
        return
    cand = session.scalar(select(BuyCandidate).where(
        BuyCandidate.account_id == acct.id, BuyCandidate.signal_id == order.signal_id,
        BuyCandidate.decision_date == order.decision_date))
    if cand is None:
        cand = BuyCandidate(account_id=acct.id, signal_id=order.signal_id, decision_date=order.decision_date,
                            decision="skipped" if skipped else "bought",
                            skip_reasons=list(check["reasons"]) if skipped else [], details={})
        session.add(cand)
    cand.details = {**(cand.details or {}), "fill_check": {**check, "fill_date": order.execute_on.isoformat(),
                                                           "skipped": skipped}}
    if skipped:
        cand.decision = "skipped"
        cand.skip_reasons = list(dict.fromkeys([*(cand.skip_reasons or []), *check["reasons"]]))


def _fill_buy(session, acct, order, raw, adv, costs, s, report) -> None:
    sig = session.get(Signal, order.signal_id) if order.signal_id else None
    ref = float(sig.entry_ref_price) if sig else raw
    check = fill_check(sig, raw, s)
    gap_pct = check["gap_pct"]
    if check["reasons"]:
        order.status = "rejected"
        order.reject_reason = check["text"]
        _record_fill_check(session, acct, order, check, skipped=True)
        report.cancelled.append(order)
        return
    price, spread, slip = costs.fill_price(raw, "buy", adv)
    notional = min(float(order.notional_usd), float(acct.cash) - costs.fee_per_order)
    if notional < s.min_order_usd:
        order.status, order.reject_reason = "rejected", "insufficient_cash"
        report.cancelled.append(order)
        return
    qty = (Decimal(str(notional)) / Decimal(str(price))).quantize(Q6)
    fill = PaperFill(order_id=order.id, fill_date=order.execute_on, raw_open=_d(raw), spread_bps=spread,
                     slippage_bps=slip, fill_price=_d(price), qty=qty, fees=_d(costs.fee_per_order))
    session.add(fill)
    session.flush()
    # Keep the signal's stop/target distances, re-anchored to the actual fill (honest after a gap).
    stop_pct = (1 - float(sig.stop_price) / ref) if sig else 0.08
    target_pct = (float(sig.target_price) / ref - 1) if sig else 0.08
    pos = PaperPosition(
        account_id=acct.id, symbol=order.symbol, entry_fill_id=fill.id, entry_date=order.execute_on, qty=qty,
        cost_basis=_d(float(qty) * price + costs.fee_per_order), stop_price=_d(price * (1 - stop_pct)),
        target_price=_d(price * (1 + target_pct)),
        time_stop_date=calendar.add_sessions(order.execute_on, s.time_stop_sessions), status="open",
        why={"signal_id": order.signal_id, "rule_id": sig.rule_id if sig else None,
             "reason": sig.reason if sig else "manual", "confidence": sig.confidence if sig else None,
             "calibrated": bool(sig.calibrated) if sig else False, "entry_gap_pct": round(gap_pct, 2),
             "move_since_catalyst_at_fill_pct": check["move_at_fill_pct"],
             "costs_bps": {"half_spread": spread, "slippage": slip}},
    )
    session.add(pos)
    _record_fill_check(session, acct, order, check, skipped=False)
    acct.cash = _d(float(acct.cash) - float(qty) * price - costs.fee_per_order)
    order.status = "filled"
    report.filled.append(order)


def _fill_sell(session, acct, order, raw, adv, costs, report) -> None:
    pos = session.get(PaperPosition, order.position_id)
    if order.execute_on <= pos.entry_date:     # unreachable via submit_sell; the DB trigger also blocks it
        raise SameDaySellError(f"{pos.symbol}: fill on {order.execute_on} for entry {pos.entry_date}")
    price, spread, slip = costs.fill_price(raw, "sell", adv)
    fill = PaperFill(order_id=order.id, fill_date=order.execute_on, raw_open=_d(raw), spread_bps=spread,
                     slippage_bps=slip, fill_price=_d(price), qty=pos.qty, fees=_d(costs.fee_per_order))
    session.add(fill)
    session.flush()
    proceeds = float(pos.qty) * price - costs.fee_per_order
    pos.exit_fill_id, pos.exit_date, pos.exit_reason = fill.id, order.execute_on, order.exit_reason
    pos.realized_pnl = _d(proceeds - float(pos.cost_basis))
    pos.status = "closed"
    trigger = (pos.why or {}).get("exit_trigger", {})
    if order.exit_reason == "stop":
        # Honest gap accounting: how far below the stop the next open actually filled.
        trigger["gap_below_stop_pct"] = round((price / float(pos.stop_price) - 1) * 100, 2)
    pos.why = {**(pos.why or {}), "exit_trigger": trigger}
    acct.cash = _d(float(acct.cash) + proceeds)
    order.status = "filled"
    report.filled.append(order)
