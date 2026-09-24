"""Step 1: database and migrations.

These tests insert rows with raw SQL on purpose: they prove the database itself
rejects rule violations, independent of any Python code path.
"""

import datetime as dt
import os
import subprocess
import sys

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import BACKEND

pytestmark = pytest.mark.db

D0 = dt.date(2026, 9, 21)   # Monday
D1 = dt.date(2026, 9, 22)
D2 = dt.date(2026, 9, 23)


def _one(db, sql, **params):
    return db.execute(text(sql), params).scalar_one()


def _seed_open_position(db, entry_date=D1):
    """Account → buy order (decided D0, executes D1) → fill → open position."""
    acct = _one(db, "INSERT INTO paper_accounts (name, start_cash, cash, settings) "
                    "VALUES ('t', 100, 100, '{}') RETURNING id")
    buy = _one(db, "INSERT INTO paper_orders (account_id, symbol, side, notional_usd, decision_date, execute_on, "
                   "origin, status, idempotency_key) VALUES (:a, 'ABC', 'buy', 20, :d0, :d1, 'manual', 'filled', "
                   "'k-buy') RETURNING id", a=acct, d0=entry_date - dt.timedelta(days=1), d1=entry_date)
    fill = _one(db, "INSERT INTO paper_fills (order_id, fill_date, raw_open, spread_bps, slippage_bps, fill_price, qty) "
                    "VALUES (:o, :d, 10, 5, 5, 10.01, 1.998) RETURNING id", o=buy, d=entry_date)
    pos = _one(db, "INSERT INTO paper_positions (account_id, symbol, entry_fill_id, entry_date, qty, cost_basis, "
                   "stop_price, target_price, time_stop_date, status) VALUES (:a, 'ABC', :f, :d, 1.998, 20, 9, 12, "
                   ":ts, 'open') RETURNING id", a=acct, f=fill, d=entry_date,
               ts=entry_date + dt.timedelta(days=14))
    return acct, pos


def _sell_order(db, acct, pos, decision, execute_on, key="k-sell"):
    return _one(db, "INSERT INTO paper_orders (account_id, position_id, symbol, side, qty, decision_date, execute_on, "
                    "origin, exit_reason, status, idempotency_key) VALUES (:a, :p, 'ABC', 'sell', 1.998, :dd, :ex, "
                    "'exit', 'stop', 'pending', :k) RETURNING id", a=acct, p=pos, dd=decision, ex=execute_on, k=key)


# ----------------------------------------------------------------------------- migrations


def test_migration_matches_models_and_round_trips(engine):
    env = {**os.environ, "DATABASE_URL": os.environ["TEST_DATABASE_URL"]}
    run = lambda *args: subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env,
                                       capture_output=True, text=True)
    check = run("check")
    assert check.returncode == 0, check.stdout + check.stderr   # models == migrations, no drift
    assert run("downgrade", "base").returncode == 0
    tables = set(inspect(engine).get_table_names())
    assert tables <= {"alembic_version"}
    assert run("upgrade", "head").returncode == 0


def test_all_tables_exist(engine):
    expected = {
        "tickers", "sources", "refresh_runs", "source_runs", "news_items", "news_item_tickers", "ticker_link_log",
        "filings", "insider_transactions", "earnings", "fda_events", "trial_events", "prices_daily",
        "macro_series", "events", "signals", "signal_events", "signal_outcomes", "calibration_snapshots",
        "backtest_runs", "backtest_trades", "model_registry", "paper_accounts", "buy_candidates", "paper_orders",
        "paper_fills", "paper_positions", "equity_snapshots", "notifications", "app_settings",
    }
    assert expected <= set(inspect(engine).get_table_names())


def test_news_items_cannot_store_article_bodies(engine):
    cols = {c["name"] for c in inspect(engine).get_columns("news_items")}
    assert not cols & {"body", "content", "summary", "text", "article", "description"}


# ----------------------------------------------------------------------------- rule 2 (DB level)


def test_rule2_sell_order_on_entry_day_rejected_by_trigger(db):
    acct, pos = _seed_open_position(db)
    with pytest.raises(IntegrityError, match="rule 2"):
        # decision D0 → execute D1 passes the generic check, but D1 is the entry day.
        _sell_order(db, acct, pos, decision=D0, execute_on=D1)


def test_rule2_sell_order_next_day_allowed(db):
    acct, pos = _seed_open_position(db)
    assert _sell_order(db, acct, pos, decision=D1, execute_on=D2)


def test_rule2_sell_order_cannot_be_moved_to_entry_day(db):
    acct, pos = _seed_open_position(db)
    oid = _sell_order(db, acct, pos, decision=D1, execute_on=D2)
    with pytest.raises(IntegrityError, match="rule 2"):
        db.execute(text("UPDATE paper_orders SET execute_on = :d, decision_date = :dd WHERE id = :id"),
                   {"d": D1, "dd": D0, "id": oid})
        db.flush()


def test_rule2_sell_fill_on_entry_day_rejected_by_trigger(db):
    acct, pos = _seed_open_position(db)
    oid = _sell_order(db, acct, pos, decision=D1, execute_on=D2)
    with pytest.raises(IntegrityError, match="rule 2"):
        db.execute(text("INSERT INTO paper_fills (order_id, fill_date, raw_open, spread_bps, slippage_bps, "
                        "fill_price, qty) VALUES (:o, :d, 10, 5, 5, 9.99, 1.998)"), {"o": oid, "d": D1})


def test_rule2_position_exit_same_day_rejected_by_check(db):
    _, pos = _seed_open_position(db)
    with pytest.raises(IntegrityError, match="ck_positions_no_same_day_exit"):
        db.execute(text("UPDATE paper_positions SET exit_date = entry_date WHERE id = :p"), {"p": pos})


def test_orders_always_execute_on_a_later_session(db):
    acct = _one(db, "INSERT INTO paper_accounts (name, cash, settings) VALUES ('x', 100, '{}') RETURNING id")
    with pytest.raises(IntegrityError, match="ck_orders_next_session"):
        db.execute(text("INSERT INTO paper_orders (account_id, symbol, side, notional_usd, decision_date, "
                        "execute_on, origin, idempotency_key) VALUES (:a, 'ABC', 'buy', 10, :d, :d, 'auto', 'k')"),
                   {"a": acct, "d": D1})


def test_only_one_open_position_per_symbol(db):
    acct, _ = _seed_open_position(db)
    buy2 = _one(db, "INSERT INTO paper_orders (account_id, symbol, side, notional_usd, decision_date, execute_on, "
                    "origin, status, idempotency_key) VALUES (:a, 'ABC', 'buy', 20, :d1, :d2, 'manual', 'filled', "
                    "'k-buy2') RETURNING id", a=acct, d1=D1, d2=D2)
    fill2 = _one(db, "INSERT INTO paper_fills (order_id, fill_date, raw_open, spread_bps, slippage_bps, fill_price, "
                     "qty) VALUES (:o, :d, 10, 5, 5, 10, 2) RETURNING id", o=buy2, d=D2)
    with pytest.raises(IntegrityError, match="uq_positions_one_open_per_symbol"):
        db.execute(text("INSERT INTO paper_positions (account_id, symbol, entry_fill_id, entry_date, qty, "
                        "cost_basis, stop_price, target_price, time_stop_date) VALUES (:a, 'ABC', :f, :d, 2, 20, 9, "
                        "12, :ts)"), {"a": acct, "f": fill2, "d": D2, "ts": D2 + dt.timedelta(days=14)})


# ----------------------------------------------------------------------------- rules 3, 5, 6, 8


def _signal_sql(displayed: bool, confidence: float, calibrated: bool = False) -> str:
    return (
        "INSERT INTO signals (symbol, as_of_date, catalyst_type, rule_id, rule_score, confidence, calibrated, "
        "expected_return_pct, expected_return_basis, holding_days_min, holding_days_max, entry_ref_price, "
        "stop_price, target_price, suggested_size_usd, risk_notes, reason, features, displayed) VALUES "
        f"('ABC', '2026-09-22', 'earnings_beat', 'r1', 70, {confidence}, {str(calibrated).lower()}, 4.0, "
        f"'prior', 3, 10, 10, 9, 11, 20, ARRAY['n'], 'why', '{{}}', {str(displayed).lower()})"
    )


def test_displayed_signal_needs_confidence_at_least_65(db):
    db.execute(text("INSERT INTO tickers (symbol, name) VALUES ('ABC', 'ABC Corp')"))
    with pytest.raises(IntegrityError, match="ck_signals_display_threshold"):
        db.execute(text(_signal_sql(displayed=True, confidence=64.9)))


def test_undisplayed_low_confidence_signal_is_still_logged(db):
    # Outcome tracking logs every candidate, including ones never shown.
    db.execute(text("INSERT INTO tickers (symbol, name) VALUES ('ABC', 'ABC Corp')"))
    db.execute(text(_signal_sql(displayed=False, confidence=40)))


def test_signal_cannot_claim_calibration_without_snapshot(db):
    db.execute(text("INSERT INTO tickers (symbol, name) VALUES ('ABC', 'ABC Corp')"))
    with pytest.raises(IntegrityError, match="ck_signals_calibrated_has_snapshot"):
        db.execute(text(_signal_sql(displayed=True, confidence=70, calibrated=True)))


def test_signals_default_to_uncalibrated(db):
    db.execute(text("INSERT INTO tickers (symbol, name) VALUES ('ABC', 'ABC Corp')"))
    db.execute(text(_signal_sql(displayed=True, confidence=70).replace(", calibrated", "").replace(
        "70, false,", "70,")))
    assert _one(db, "SELECT calibrated FROM signals WHERE symbol = 'ABC'") is False


def test_skipped_buy_candidate_must_say_why(db):
    db.execute(text("INSERT INTO tickers (symbol, name) VALUES ('ABC', 'ABC Corp')"))
    sid = _one(db, _signal_sql(displayed=True, confidence=70) + " RETURNING id")
    acct = _one(db, "INSERT INTO paper_accounts (name, cash, settings) VALUES ('x', 100, '{}') RETURNING id")
    with pytest.raises(IntegrityError, match="ck_candidates_skip_reason"):
        db.execute(text("INSERT INTO buy_candidates (account_id, signal_id, decision_date, decision, skip_reasons) "
                        "VALUES (:a, :s, :d, 'skipped', '{}')"), {"a": acct, "s": sid, "d": D1})


def test_event_polarity_restricted(db):
    db.execute(text("INSERT INTO tickers (symbol, name) VALUES ('ABC', 'ABC Corp')"))
    with pytest.raises(IntegrityError, match="ck_events_polarity"):
        db.execute(text("INSERT INTO events (symbol, event_type, origin, headline, url, available_at, polarity, "
                        "classifier_version) VALUES ('ABC', 'upgrade', 'news', 'h', 'u', now(), 'bullish', 'v1')"))


# ----------------------------------------------------------------------------- news integrity


def _source(db):
    db.execute(text("INSERT INTO sources (key, kind) VALUES ('finnhub_news', 'news')"))


def test_news_dedupe_on_source_and_provider_id(db):
    _source(db)
    sql = ("INSERT INTO news_items (source_key, provider_item_id, headline, url, published_at, fetched_at, "
           "available_at, dedupe_hash) VALUES ('finnhub_news', '1', 'h', 'u', now(), now(), now(), 1)")
    db.execute(text(sql))
    with pytest.raises(IntegrityError, match="uq_news_source_item"):
        db.execute(text(sql))


def test_news_cannot_be_available_before_published(db):
    _source(db)
    with pytest.raises(IntegrityError, match="ck_news_available_after_published"):
        db.execute(text("INSERT INTO news_items (source_key, provider_item_id, headline, url, published_at, "
                        "fetched_at, available_at, dedupe_hash) VALUES ('finnhub_news', '1', 'h', 'u', now(), "
                        "now(), now() - interval '1 hour', 1)"))
