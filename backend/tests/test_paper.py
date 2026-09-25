"""Paper trading: next-open fills, exits on close, gaps, sizing, skip reasons, auto-buy gate,
and the proof that same-day sells are impossible (hard rule 2)."""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from catalystedge.db.models import (
    BuyCandidate,
    CalibrationSnapshot,
    Event,
    PaperFill,
    PaperOrder,
    PaperPosition,
    Signal,
    Ticker,
)
from catalystedge.paper.costs import CostModel
from catalystedge.paper.engine import (
    OrderRejected,
    SameDaySellError,
    auto_buy_gate,
    evaluate_candidates,
    evaluate_exits,
    execute_orders,
    get_account,
    manual_buy,
    manual_sell,
    size_order,
    submit_sell,
)
from catalystedge.paper.metrics import max_drawdown_pct, performance, sharpe_ratio
from catalystedge.paper.settings import PaperSettings

pytestmark = pytest.mark.db
UTC = dt.UTC
THU, FRI, MON, TUE, WED = (dt.date(2026, 9, 24), dt.date(2026, 9, 25), dt.date(2026, 9, 28),
                           dt.date(2026, 9, 29), dt.date(2026, 9, 30))
LIQUID = lambda s: 1e9  # noqa: E731


def make_signal(db, symbol="ABC", confidence=70.0, ref=100.0, stop=94.0, target=106.0, as_of=THU, displayed=True):
    if db.get(Ticker, symbol) is None:
        db.add(Ticker(symbol=symbol, name=f"{symbol} Corp", aliases=[], adv20_usd=Decimal("1e9")))
        db.flush()
    sig = Signal(symbol=symbol, as_of_date=as_of, catalyst_type="earnings_beat", rule_id="R_EARNINGS_BEAT",
                 rule_score=confidence, confidence=confidence, expected_return_pct=4.0, expected_return_basis="prior",
                 holding_days_min=3, holding_days_max=10, entry_ref_price=Decimal(str(ref)),
                 stop_price=Decimal(str(stop)), target_price=Decimal(str(target)), suggested_size_usd=Decimal("20"),
                 risk_notes=["note"], reason="test", features={}, displayed=displayed)
    db.add(sig)
    db.flush()
    return sig


def opens(prices):
    """open_price(symbol, day) from a dict {(symbol, day): price}; records every lookup."""
    calls = []

    def f(symbol, day):
        calls.append((symbol, day))
        return prices.get((symbol, day))

    f.calls = calls
    return f


def buy_and_fill(db, acct, sig, decision=THU, open_px=100.0):
    manual_buy(db, acct, sig, dt.datetime.combine(decision, dt.time(20, 30), UTC))
    fill_day = {THU: FRI, FRI: MON}[decision]
    execute_orders(db, acct, fill_day, opens({(sig.symbol, fill_day): open_px}), LIQUID)
    return db.scalars(select(PaperPosition).where(PaperPosition.symbol == sig.symbol)).one()


# ----------------------------------------------------------------------------- rule 2: same-day sells


def test_same_day_sell_is_impossible_everywhere(db):
    acct = get_account(db)
    pos = buy_and_fill(db, acct, make_signal(db))                  # entered at FRI open
    assert pos.entry_date == FRI

    # 1) Code: a sell decided before the entry day would execute on/before entry -> refused.
    with pytest.raises(SameDaySellError):
        submit_sell(db, pos, THU, "manual")

    # 2) Manual one-click sell during the entry day is scheduled for the NEXT session, never today.
    order = manual_sell(db, acct, pos.id, dt.datetime(2026, 9, 25, 15, 0, tzinfo=UTC))   # 11:00 ET Friday
    assert order.execute_on == MON > pos.entry_date

    # 3) Exit rules evaluated on the entry day's close also execute next session.
    assert order.decision_date == FRI and order.execute_on == MON

    # 4) Database: even raw SQL cannot schedule or fill a sell on the entry day.
    with pytest.raises(IntegrityError, match="rule 2"), db.begin_nested():
        db.execute(text("UPDATE paper_orders SET execute_on = :d, decision_date = :p WHERE id = :id"),
                   {"d": FRI, "p": THU, "id": order.id})


def test_stop_hit_on_entry_day_close_sells_next_open(db):
    acct = get_account(db)
    pos = buy_and_fill(db, acct, make_signal(db))
    orders = evaluate_exits(db, acct, FRI, {"ABC": 90.0})           # crashes on the entry day
    assert [o.execute_on for o in orders] == [MON]
    report = execute_orders(db, acct, FRI, opens({("ABC", FRI): 90.0}), LIQUID)
    assert report.filled == [] and pos.status == "open"             # nothing can fill on FRI


# ----------------------------------------------------------------------------- fills & lookahead


def test_buy_fills_at_next_session_open_with_costs(db):
    acct = get_account(db)
    sig = make_signal(db)
    order = manual_buy(db, acct, sig, dt.datetime(2026, 9, 24, 21, 0, tzinfo=UTC))   # after Thursday close
    assert order.execute_on == FRI
    f = opens({("ABC", FRI): 101.0})
    execute_orders(db, acct, FRI, f, LIQUID)
    assert f.calls == [("ABC", FRI)]                                # only the execution day's open is read
    fill = db.scalars(select(PaperFill)).one()
    assert float(fill.raw_open) == 101.0
    assert float(fill.fill_price) == pytest.approx(101.0 * (1 + 10 / 1e4))   # 5 bps half-spread + 5 bps slippage
    pos = db.scalars(select(PaperPosition)).one()
    assert float(pos.qty) == pytest.approx(float(order.notional_usd) / float(fill.fill_price), rel=1e-5)
    assert float(pos.qty) % 1 != 0                                  # fractional shares


def test_friday_decision_fills_monday(db):
    acct = get_account(db)
    order = manual_buy(db, acct, make_signal(db, as_of=FRI), dt.datetime(2026, 9, 25, 21, 0, tzinfo=UTC))
    assert order.execute_on == MON


def test_orders_never_read_prices_before_decision(db):
    acct = get_account(db)
    manual_buy(db, acct, make_signal(db), dt.datetime(2026, 9, 24, 21, 0, tzinfo=UTC))
    f = opens({("ABC", FRI): 100.0, ("ABC", THU): 50.0})
    execute_orders(db, acct, FRI, f, LIQUID)
    assert all(day > THU for _, day in f.calls)


def test_missed_session_cancels_instead_of_filling_later(db):
    acct = get_account(db)
    manual_buy(db, acct, make_signal(db), dt.datetime(2026, 9, 24, 21, 0, tzinfo=UTC))
    report = execute_orders(db, acct, MON, opens({("ABC", MON): 80.0}), LIQUID, last_completed=FRI)
    # FRI had no open price and FRI is over: cancel; never fill at Monday's (different) price.
    assert report.cancelled and report.cancelled[0].reject_reason.startswith("missed_session")
    assert db.scalars(select(PaperPosition)).all() == []


def test_waits_if_open_not_known_yet(db):
    acct = get_account(db)
    manual_buy(db, acct, make_signal(db), dt.datetime(2026, 9, 24, 21, 0, tzinfo=UTC))
    report = execute_orders(db, acct, FRI, opens({}), LIQUID, last_completed=THU)
    assert len(report.waiting) == 1


def test_gap_up_beyond_limit_is_not_chased(db):
    acct = get_account(db)
    manual_buy(db, acct, make_signal(db, ref=100.0), dt.datetime(2026, 9, 24, 21, 0, tzinfo=UTC))
    report = execute_orders(db, acct, FRI, opens({("ABC", FRI): 108.0}), LIQUID)
    assert report.cancelled[0].reject_reason.startswith("gap_up_priced_in")
    assert float(acct.cash) == 100.0


# ----------------------------------------------------------------------------- exits & gaps


def test_gap_down_through_stop_fills_at_the_open_not_the_stop(db):
    acct = get_account(db)
    pos = buy_and_fill(db, acct, make_signal(db, ref=100.0, stop=94.0))
    stop = float(pos.stop_price)
    evaluate_exits(db, acct, MON, {"ABC": stop - 1})                  # close below stop on Monday
    execute_orders(db, acct, TUE, opens({("ABC", TUE): stop * 0.90}), LIQUID)   # opens 10% lower
    assert pos.status == "closed" and pos.exit_reason == "stop" and pos.exit_date == TUE
    exit_fill = db.get(PaperFill, pos.exit_fill_id)
    assert float(exit_fill.fill_price) < stop * 0.90                  # worse than the stop, plus costs
    assert pos.why["exit_trigger"]["gap_below_stop_pct"] < -9.9
    assert float(pos.realized_pnl) < -(float(pos.cost_basis) * 0.06)  # lost more than the 6% stop distance


def test_target_and_time_stop(db):
    acct = get_account(db)
    pos = buy_and_fill(db, acct, make_signal(db))
    assert evaluate_exits(db, acct, MON, {"ABC": float(pos.target_price) + 0.01})[0].exit_reason == "target"
    acct2 = get_account(db, "second")
    pos2 = buy_and_fill(db, acct2, make_signal(db, symbol="XYZ"))
    assert evaluate_exits(db, acct2, pos2.time_stop_date, {"XYZ": 100.0})[0].exit_reason == "time_stop"


def test_signal_decay_on_new_negative_event(db):
    acct = get_account(db)
    buy_and_fill(db, acct, make_signal(db))
    db.add(Event(symbol="ABC", event_type="other", origin="news", headline="ABC cuts guidance", url="u",
                 available_at=dt.datetime(2026, 9, 28, 15, tzinfo=UTC), polarity="negative",
                 classifier_version="t"))
    db.flush()
    assert evaluate_exits(db, acct, MON, {"ABC": 100.0})[0].exit_reason == "signal_decay"


def test_round_trip_updates_cash_and_pnl(db):
    acct = get_account(db)
    pos = buy_and_fill(db, acct, make_signal(db))
    evaluate_exits(db, acct, MON, {"ABC": 107.0})
    execute_orders(db, acct, TUE, opens({("ABC", TUE): 107.0}), LIQUID)
    assert pos.status == "closed" and float(pos.realized_pnl) > 0
    assert float(acct.cash) == pytest.approx(100.0 + float(pos.realized_pnl), abs=0.01)


# ----------------------------------------------------------------------------- sizing, skips, auto-buy


def test_sizing_rules():
    s = PaperSettings()
    notional, reason = size_order(100, 100, 100, 94, s)       # 2% risk / 6% stop = $33 -> capped at 25%
    assert (notional, reason) == (25.0, None)
    notional, _ = size_order(100, 100, 100, 80, s)            # 20% stop -> 2/0.2 = $10
    assert notional == 10.0
    assert size_order(100, 10.5, 100, 94, s) == (0.0, "cash_floor")   # would dip under the 10% cash floor
    assert size_order(100, 100, 100, 101, s)[1] == "invalid_stop"


def test_auto_buy_off_by_default_every_option_logged(db):
    acct = get_account(db)
    sigs = [make_signal(db, "AAA", 90), make_signal(db, "BBB", 70), make_signal(db, "CCC", 60, displayed=False)]
    cands = evaluate_candidates(db, acct, THU, sigs, {})
    assert [c.decision for c in cands] == ["skipped", "skipped"]      # CCC not displayed -> not a buy option
    assert "auto_buy_off" in cands[0].skip_reasons
    assert set(cands[1].skip_reasons) >= {"auto_buy_off", "below_auto_buy_threshold"}
    assert db.scalars(select(PaperOrder)).all() == []
    assert not auto_buy_gate(db, acct).allowed


def test_auto_buy_switch_alone_is_not_enough(db):
    acct = get_account(db)
    acct.settings = {**acct.settings, "auto_buy": True}
    gate = auto_buy_gate(db, acct)
    assert not gate.allowed and "locked" in gate.reason
    (cand,) = evaluate_candidates(db, acct, THU, [make_signal(db, "AAA", 90)], {})
    assert cand.skip_reasons == ["auto_buy_locked_uncalibrated"]


def test_auto_buy_with_backtest_evidence_places_next_day_order(db):
    acct = get_account(db)
    acct.settings = {**acct.settings, "auto_buy": True}
    db.add(CalibrationSnapshot(basis="backtest", event_family="all", n=500, buckets=[], verdict="fair"))
    db.flush()
    (cand,) = evaluate_candidates(db, acct, THU, [make_signal(db, "AAA", 90)], {})
    assert cand.decision == "bought"
    order = db.scalars(select(PaperOrder)).one()
    assert order.execute_on == FRI and order.origin == "auto"


def test_skip_reasons_held_and_max_positions(db):
    acct = get_account(db)
    acct.settings = {**acct.settings, "max_open_positions": 1}
    buy_and_fill(db, acct, make_signal(db, "AAA", 90))
    cands = evaluate_candidates(db, acct, MON, [make_signal(db, "AAA", 90, as_of=MON),
                                                make_signal(db, "BBB", 90, as_of=MON)], {})
    assert "already_held" in cands[0].skip_reasons and "max_positions" in cands[1].skip_reasons


def test_decisions_are_idempotent(db):
    acct = get_account(db)
    sig = make_signal(db, "AAA", 90)
    evaluate_candidates(db, acct, THU, [sig], {})
    evaluate_candidates(db, acct, THU, [sig], {})
    from catalystedge.db.models import BuyCandidate

    assert len(db.scalars(select(BuyCandidate)).all()) == 1


def test_manual_buy_rules(db):
    acct = get_account(db)
    with pytest.raises(OrderRejected):
        manual_buy(db, acct, make_signal(db, "LOW", 50, displayed=False), dt.datetime(2026, 9, 24, 21, tzinfo=UTC))
    buy_and_fill(db, acct, make_signal(db, "AAA"))
    with pytest.raises(OrderRejected, match="already holding"):
        manual_buy(db, acct, make_signal(db, "AAA", as_of=FRI), dt.datetime(2026, 9, 25, 21, tzinfo=UTC))


# ----------------------------------------------------------------------------- metrics


def test_sharpe_and_drawdown():
    assert sharpe_ratio([0.01]) is None
    assert sharpe_ratio([0.01, -0.01, 0.02, 0.0]) > 0
    assert max_drawdown_pct([100, 110, 99, 120]) == pytest.approx((99 / 110 - 1) * 100)


def test_performance_and_spy_line(db):
    from catalystedge.paper.metrics import mark_to_market

    acct = get_account(db)
    acct.created_at = dt.datetime(2026, 9, 24, 15, tzinfo=UTC)          # created Thursday 11:00 ET
    pos = buy_and_fill(db, acct, make_signal(db))
    spy_opens = {FRI: 500.0}
    mark_to_market(db, acct, FRI, {"ABC": 100.0, "SPY": 505.0}, spy_opens.get, CostModel())
    assert acct.settings["benchmark"]["entry_date"] == FRI.isoformat()  # never Thursday's already-past open
    evaluate_exits(db, acct, MON, {"ABC": 107.0})
    execute_orders(db, acct, TUE, opens({("ABC", TUE): 107.0}), LIQUID)
    mark_to_market(db, acct, TUE, {"SPY": 510.0}, spy_opens.get, CostModel())
    perf = performance(db, acct)
    assert perf.closed_trades == 1 and perf.win_rate_pct == 100.0 and perf.total_return_pct > 0
    assert perf.spy_return_pct == pytest.approx((100 / (500 * 1.001) * 510 / 100 - 1) * 100, abs=0.01)
    assert "not yet meaningful" in perf.note
    assert pos.why["calibrated"] is False


# ----------------------------------------------------------------------------- next-open re-check (8b)


def _catalyst_signal(db, symbol, pre_close, decision_close, reaction):
    sig = make_signal(db, symbol=symbol, ref=decision_close, stop=decision_close * 0.94,
                      target=decision_close * 1.06)
    sig.features = {"price": {"pre_event_close": pre_close, "reaction_pct": reaction, "last_close": decision_close}}
    db.flush()
    return sig


@pytest.mark.parametrize("pre,close,open_px,reasons", [
    (100.0, 103.0, 104.0, []),                                                 # 4% since news: fills
    (100.0, 112.0, 116.0, ["extended_since_catalyst"]),                         # +16% since news, +3.6% gap
    (100.0, 103.0, 99.0, ["reversed_since_catalyst"]),                          # below the pre-news price
    (100.0, 103.0, 109.0, ["gap_up_priced_in"]),                                # +5.8% gap, +9% since news
    (100.0, 110.0, 117.0, ["gap_up_priced_in", "extended_since_catalyst"]),    # both, logged separately
])
def test_next_open_recheck_logs_each_reason_separately(db, pre, close, open_px, reasons):
    acct = get_account(db)
    sig = _catalyst_signal(db, "CAT1", pre, close, (close / pre - 1) * 100)
    manual_buy(db, acct, sig, dt.datetime.combine(THU, dt.time(20, 30), UTC))
    execute_orders(db, acct, FRI, opens({("CAT1", FRI): open_px}), LIQUID)
    order = db.scalars(select(PaperOrder).where(PaperOrder.symbol == "CAT1")).one()
    cand = db.scalars(select(BuyCandidate).where(BuyCandidate.signal_id == sig.id)).one()
    fc = cand.details["fill_check"]
    assert fc["reasons"] == reasons and fc["move_at_fill_pct"] == pytest.approx((open_px / pre - 1) * 100, abs=0.01)
    if reasons:
        assert order.status == "rejected" and cand.decision == "skipped"
        assert all(r in cand.skip_reasons for r in reasons)
        assert all(r in order.reject_reason for r in reasons)
    else:
        assert order.status == "filled" and fc["skipped"] is False and fc["band_at_fill"] == "very_early"
        pos = db.scalars(select(PaperPosition).where(PaperPosition.symbol == "CAT1")).one()
        assert pos.why["move_since_catalyst_at_fill_pct"] == pytest.approx(4.0, abs=0.01)


def test_catalyst_bands():
    from catalystedge.paper.engine import catalyst_band

    assert [catalyst_band(x) for x in (-1, 0, 5, 7, 12, 20, None)] == \
        ["reversed", "very_early", "very_early", "early", "borderline", "extended", None]
