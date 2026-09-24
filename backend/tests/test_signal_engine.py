"""Signal engine: news-first, positive-only, 48 h window, priced-in skip, low-weight upgrades,
model-disagrees penalty, TimesFM off/feature/filter/failure, idempotent reruns."""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from catalystedge.core import calendar
from catalystedge.db.models import Event, PriceDaily, Signal, SignalEvent, Ticker
from catalystedge.signals import engine as eng
from catalystedge.signals.engine import DISPLAY_MIN, generate_signals
from catalystedge.signals.features import compute
from catalystedge.signals.timesfm_hook import Forecast, TimesFMState

pytestmark = pytest.mark.db
UTC = dt.UTC
AS_OF = dt.date(2026, 9, 23)                                        # a Wednesday session
NOW = calendar.eod_available_at(AS_OF) + dt.timedelta(hours=1)      # evening after the close
NEWS_AT = calendar.session_open(AS_OF) - dt.timedelta(hours=2)      # pre-market that morning


def add_ticker(db, symbol):
    if db.get(Ticker, symbol) is None:
        db.add(Ticker(symbol=symbol, name=f"{symbol} Inc", aliases=[], adv20_usd=Decimal("5e8")))
        db.flush()


def add_bars(db, symbol, last_move_pct=0.0, n=80, volume=1_000_000, last_volume=None):
    """Flat-ish history ending AS_OF; the last session moves by `last_move_pct` (the reaction)."""
    days = calendar.sessions_between(calendar.add_sessions(AS_OF, -(n - 1)), AS_OF)
    for i, d in enumerate(days):
        base = 100.0 + (i % 3) * 0.5
        close = base * (1 + last_move_pct / 100) if d == AS_OF else base
        o = base if d == AS_OF else close
        db.add(PriceDaily(symbol=symbol, date=d, open=Decimal(str(o)), high=Decimal(str(max(o, close) + 1)),
                          low=Decimal(str(min(o, close) - 1)), close=Decimal(str(close)), adj_close=None,
                          volume=last_volume if (d == AS_OF and last_volume) else volume, source="test",
                          fetched_at=NOW, available_at=calendar.eod_available_at(d)))
    db.flush()


def add_event(db, symbol, event_type="earnings_beat", polarity="positive", at=NEWS_AT, materiality=0.8,
              credibility=0.7, sentiment=None, origin="news", headline=None):
    add_ticker(db, symbol)
    e = Event(symbol=symbol, event_type=event_type, origin=origin, headline=headline or f"{symbol} {event_type}",
              url="https://example.com/x", source_key="finnhub_news", available_at=at, polarity=polarity,
              strength="normal", sentiment=sentiment or {"model": "finbert", "pos": 0.8, "neu": 0.15, "neg": 0.05},
              materiality=materiality, novelty=1.0, credibility=credibility, reasons=["test"],
              classifier_version="test")
    db.add(e)
    db.flush()
    return e


def by_symbol(result):
    return {c.symbol: c for c in result.candidates}


def test_positive_event_becomes_explained_displayed_signal(db):
    add_bars(db, "AAA", last_move_pct=1.0, last_volume=2_000_000)
    e = add_event(db, "AAA", headline="AAA beats estimates and raises outlook")
    c = by_symbol(generate_signals(db, NOW))["AAA"]
    sig = c.signal
    assert c.displayed and sig.displayed and sig.confidence >= DISPLAY_MIN
    assert sig.rule_id == "R-EARN-v1" and sig.calibrated is False and sig.expected_return_basis == "prior"
    assert "UNCALIBRATED" in sig.reason and "beats estimates" in sig.reason
    assert set(sig.features["rule_components"]) >= {"base", "materiality", "novelty", "volume_confirmation"}
    assert float(sig.stop_price) < float(sig.entry_ref_price) < float(sig.target_price)
    assert float(sig.suggested_size_usd) > 0
    assert db.scalars(select(SignalEvent.event_id).where(SignalEvent.signal_id == sig.id)).all() == [e.id]
    assert sig.features["timesfm"]["enabled"] is False


def test_upgrade_is_low_weight(db):
    for s, kind in (("UPG", "upgrade"), ("FDA", "fda_approval")):
        add_bars(db, s)
        add_event(db, s, event_type=kind, materiality=0.6)
    r = by_symbol(generate_signals(db, NOW))
    assert r["UPG"].confidence < r["FDA"].confidence - 10
    assert not r["UPG"].displayed and r["UPG"].skip_reason == "below_display_threshold"
    assert any("low-weight" in n for n in r["UPG"].signal.risk_notes)


def test_skips_when_stock_already_rose_sharply_since_the_news(db):
    add_bars(db, "RUN", last_move_pct=12.0)
    add_event(db, "RUN")
    c = by_symbol(generate_signals(db, NOW))["RUN"]
    assert c.skip_reason == "priced_in" and not c.displayed
    assert c.features.reaction_pct > 10 and c.signal.status == "invalidated"
    assert any("priced in" in n for n in c.signal.risk_notes)


def test_negative_reaction_lowers_score(db):
    add_bars(db, "UP", last_move_pct=0.5)
    add_bars(db, "DN", last_move_pct=-4.0)
    add_event(db, "UP")
    add_event(db, "DN")
    r = by_symbol(generate_signals(db, NOW))
    assert r["DN"].confidence < r["UP"].confidence
    assert r["DN"].rule.components["reaction"] == -8.0


def test_only_positive_events_in_48h_window_and_not_future(db):
    for s in ("OLD", "FUT", "NEG", "MIX", "OTH"):
        add_bars(db, s)
    add_event(db, "OLD", at=NOW - dt.timedelta(hours=49))
    add_event(db, "FUT", at=NOW + dt.timedelta(minutes=5))
    add_event(db, "NEG", polarity="negative")
    add_event(db, "MIX", polarity="mixed")
    add_event(db, "OTH", event_type="other")
    assert generate_signals(db, NOW).candidates == []


def test_model_disagreement_lowers_confidence_but_keeps_signal(db):
    add_bars(db, "AGR")
    add_bars(db, "DIS")
    add_event(db, "AGR", event_type="fda_approval")
    add_event(db, "DIS", event_type="fda_approval",
              sentiment={"model": "finbert", "pos": 0.05, "neu": 0.05, "neg": 0.90, "model_disagrees": True})
    r = by_symbol(generate_signals(db, NOW))
    assert r["DIS"].rule.components["model_disagrees"] == -12.0
    assert r["DIS"].confidence < r["AGR"].confidence
    assert r["DIS"].signal.features["model_disagrees"] is True
    assert any("disagrees" in n for n in r["DIS"].signal.risk_notes)


def test_no_price_data_is_reported_not_stored(db):
    add_event(db, "NOP")
    c = by_symbol(generate_signals(db, NOW))["NOP"]
    assert c.skip_reason == "no_price_data" and c.signal is None


def test_ensure_prices_is_called_for_candidate_symbols(db):
    add_event(db, "FET")
    asked = []

    def ensure(symbols):
        asked.extend(symbols)
        add_bars(db, "FET")

    c = by_symbol(generate_signals(db, NOW, ensure_prices=ensure))["FET"]
    assert asked == ["FET"] and c.signal is not None


def test_rerun_same_day_updates_instead_of_duplicating(db):
    add_bars(db, "RRR")
    add_event(db, "RRR")
    generate_signals(db, NOW)
    generate_signals(db, NOW)
    assert db.scalar(select(func.count()).select_from(Signal).where(Signal.symbol == "RRR")) == 1


def test_several_sources_corroborate(db):
    add_bars(db, "COR")
    add_event(db, "COR", event_type="earnings_beat", origin="news")
    add_event(db, "COR", event_type="earnings_beat", origin="earnings", credibility=1.0)
    c = by_symbol(generate_signals(db, NOW))["COR"]
    assert c.rule.components["corroboration"] > 0 and len(c.events) == 2


# ----------------------------------------------------------------------------- TimesFM


def _fc(symbol, er):
    return Forecast(symbol, 5, er, er - 2, er + 2, "timesfm-test", AS_OF.isoformat())


def test_timesfm_off_leaves_pipeline_unchanged(db):
    add_bars(db, "TF1")
    add_event(db, "TF1")
    base = by_symbol(generate_signals(db, NOW))["TF1"].confidence
    off = by_symbol(generate_signals(db, NOW, timesfm_state=TimesFMState(False, "filter"),
                                     timesfm_forecasts={"TF1": _fc("TF1", -5)}))["TF1"]
    assert off.confidence == base and off.displayed and off.timesfm.confidence_delta == 0


def test_timesfm_feature_mode_nudges_confidence(db):
    add_bars(db, "TF2")
    add_event(db, "TF2")
    base = by_symbol(generate_signals(db, NOW))["TF2"].confidence
    up = by_symbol(generate_signals(db, NOW, timesfm_state=TimesFMState(True, "feature"),
                                    timesfm_forecasts={"TF2": _fc("TF2", 3)}))["TF2"]
    assert 0 < up.confidence - base <= 5
    assert up.signal.features["timesfm"]["mode"] == "feature"


def test_timesfm_filter_mode_removes_and_lists(db):
    for s in ("KEEP", "DROP"):
        add_bars(db, s)
        add_event(db, s)
    r = generate_signals(db, NOW, timesfm_state=TimesFMState(True, "filter"),
                         timesfm_forecasts={"KEEP": _fc("KEEP", 1.5), "DROP": _fc("DROP", -1.0)})
    assert [c.symbol for c in r.filtered_by_timesfm] == ["DROP"]
    drop = by_symbol(r)["DROP"]
    assert not drop.displayed and drop.skip_reason == "filtered_by_timesfm" and "not positive" in drop.timesfm.note
    assert by_symbol(r)["KEEP"].displayed


def test_timesfm_failure_falls_back_with_warning(db):
    add_bars(db, "TF3")
    add_event(db, "TF3")
    base = by_symbol(generate_signals(db, NOW))["TF3"].confidence
    r = generate_signals(db, NOW, timesfm_state=TimesFMState(True, "filter"), timesfm_forecasts={},
                         timesfm_status="failed")
    c = by_symbol(r)["TF3"]
    assert c.displayed and c.confidence == base
    assert any("failed" in w for w in r.warnings) and c.signal.features["timesfm"]["warning"]


# ----------------------------------------------------------------------------- features (pure)


def test_features_reaction_is_measured_from_the_pre_event_close():
    class B:
        def __init__(self, d, c):
            self.date, self.open, self.high, self.low, self.close, self.volume = d, c, c + 1, c - 1, c, 1000

    days = calendar.sessions_between(dt.date(2026, 9, 1), AS_OF)
    bars = [B(d, 100.0 if d < AS_OF else 106.0) for d in days]
    f = compute(bars, AS_OF)
    assert f.pre_event_close == 100.0 and round(f.reaction_pct, 6) == 6.0 and f.sessions_since_event == 1


def test_event_session_after_close_is_next_day():
    after = calendar.session_close(AS_OF) + dt.timedelta(minutes=5)
    assert eng.event_session(after) == calendar.next_session(AS_OF)
    assert eng.event_session(NEWS_AT) == AS_OF
