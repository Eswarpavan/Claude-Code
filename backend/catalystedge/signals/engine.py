"""Signal engine: positive events (48 h window) -> ranked, explained signals.

    generate_signals(session, now) -> EngineResult

1. Candidates = positive, signal-eligible events whose `available_at` is inside the
   48-hour window (rule 4) and not after `now` (rule 7). News, SEC filings, earnings
   and FDA events all arrive through the same `events` table, so they are scored alike.
2. Per (ticker, catalyst): point-in-time price features, the transparent rule score
   (rules.py), and the priced-in check (skip if the stock already rose sharply).
3. Optional ranker blend (only if a validated model is enabled) and optional
   TimesFM effect (off by default).
4. Every candidate is written to `signals` (rule 3 logging: shown or not) with its
   events linked; only positive, non-skipped, confidence >= 65 rows are `displayed`.
Confidence stays UNCALIBRATED (`calibrated=false`) unless a calibrator backed by a
calibration snapshot is supplied.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.clock import ensure_utc
from catalystedge.core import calendar
from catalystedge.db.models import Event, Signal, SignalEvent, Ticker
from catalystedge.pipeline.window import cutoff
from catalystedge.prices import load_bars
from catalystedge.signals import rules
from catalystedge.signals.features import PriceFeatures, compute
from catalystedge.signals.priors import PRIORS, CatalystPrior
from catalystedge.signals.timesfm_hook import Effect, Forecast, TimesFMState, apply

ET = ZoneInfo("America/New_York")
DISPLAY_MIN = 65.0      # rule 5: never lower
HIGHLIGHT_MIN = 80.0
ENGINE_VERSION = "engine-v1"
MAX_STOP_PCT = 8.0
MIN_STOP_PCT = 3.0


class Ranker(Protocol):
    version: str
    weight: float           # blend weight chosen in walk-forward (0..1)

    def predict_proba(self, features: dict) -> float: ...


class Calibrator(Protocol):
    def calibrate(self, catalyst: str, raw_confidence: float) -> tuple[float, int] | None:
        """(calibrated confidence 0-100, calibration_snapshot id) or None if not calibrated."""


@dataclass
class Candidate:
    symbol: str
    catalyst: str
    events: list[Event]
    confidence: float
    rule: rules.RuleScore
    features: PriceFeatures | None
    displayed: bool
    skip_reason: str | None
    timesfm: Effect
    signal: Signal | None = None


@dataclass
class EngineResult:
    as_of_date: dt.date
    candidates: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def displayed(self) -> list[Candidate]:
        return sorted((c for c in self.candidates if c.displayed),
                      key=lambda c: (-c.confidence, -(c.signal.expected_return_pct if c.signal else 0)))

    @property
    def skipped(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.displayed]

    @property
    def filtered_by_timesfm(self) -> list[Candidate]:
        return [c for c in self.candidates if c.timesfm.filtered]


def event_session(available_at: dt.datetime) -> dt.date:
    """First session whose close can reflect information public at `available_at`."""
    day = available_at.astimezone(ET).date()
    if calendar.is_session(day) and available_at < calendar.session_close(day):
        return day
    return calendar.next_session(day)


def levels(prior: CatalystPrior, f: PriceFeatures) -> tuple[float, float, float, float]:
    """(entry_ref, stop, target, expected_return_pct)."""
    entry = f.last_close
    atr = f.atr20_pct or 3.0
    stop_pct = min(MAX_STOP_PCT, max(MIN_STOP_PCT, 1.5 * atr))
    exp = prior.expected_return_pct
    target_pct = max(1.5 * exp, 2.0 * atr)
    return entry, entry * (1 - stop_pct / 100), entry * (1 + target_pct / 100), exp


def _reason(prior: CatalystPrior, events: Sequence[Event], r: rules.RuleScore, confidence: float,
            f: PriceFeatures | None, tfm: Effect, model_note: str | None) -> str:
    lead = events[0]
    src = {"news": "news", "filing": "an SEC filing", "fda": "an FDA record", "trial": "a trial registry update",
           "earnings": "an earnings report"}.get(lead.origin, lead.origin)
    top = sorted(((k, v) for k, v in r.components.items() if k != "base"), key=lambda kv: -abs(kv[1]))[:3]
    parts = [f"{prior.plain.capitalize()} from {src}: “{lead.headline}”.",
             f"Rule {prior.rule_id} scores {r.score:.0f}/100 (UNCALIBRATED)."]
    if top:
        parts.append("Biggest factors: " + ", ".join(f"{k.replace('_', ' ')} {v:+.0f}" for k, v in top) + ".")
    if f is not None and f.reaction_pct is not None:
        parts.append(f"Price since the news: {f.reaction_pct:+.1f}%.")
    if model_note:
        parts.append(model_note)
    if tfm.note:
        parts.append(tfm.note + ".")
    if confidence != r.score:
        parts.append(f"Final confidence {confidence:.0f}.")
    return " ".join(parts)


def _suggested_size(session: Session, entry: float, stop: float) -> float:
    from catalystedge.paper.engine import equity, get_account, settings_of, size_order

    acct = get_account(session)
    s = settings_of(acct)
    eq = equity(session, acct, {})
    notional, _ = size_order(eq, float(acct.cash), entry, stop, s)
    return notional


def generate_signals(session: Session, now: dt.datetime, *,
                     ensure_prices: Callable[[list[str]], None] | None = None,
                     ranker: Ranker | None = None,
                     calibrator: Calibrator | None = None,
                     timesfm_state: TimesFMState | None = None,
                     timesfm_forecasts: dict[str, Forecast] | None = None,
                     timesfm_status: str = "ready") -> EngineResult:
    now = ensure_utc(now)
    as_of_date = calendar.last_completed_session(now)
    tfm_state = timesfm_state or TimesFMState()
    result = EngineResult(as_of_date)
    if tfm_state.enabled and timesfm_status != "ready":
        result.warnings.append(f"TimesFM is on but {timesfm_status}; signals use the normal pipeline.")

    events = session.scalars(select(Event).where(
        Event.polarity == "positive", Event.event_type.in_(tuple(PRIORS)),
        Event.available_at > cutoff(now), Event.available_at <= now).order_by(Event.available_at)).all()
    groups: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for e in events:
        groups[(e.symbol, e.event_type)].append(e)
    if ensure_prices and groups:
        ensure_prices(sorted({s for s, _ in groups}))

    for (symbol, catalyst), evs in groups.items():
        prior = PRIORS[catalyst]
        bars = load_bars(session, symbol, now, lookback_sessions=120)
        f = compute(bars, event_session(evs[0].available_at)) if bars else None
        r = rules.score(prior, evs, f)
        confidence, model_prob, model_version, model_note = r.score, None, None, None
        feats = {"rule_score": r.score, "catalyst": catalyst,
                 "rule_components": r.components, "price": f.as_dict() if f else None,
                 "catalyst_weight": prior.weight, "n_events": len(evs), "origins": sorted({e.origin for e in evs}),
                 "model_disagrees": r.model_disagrees, "engine": ENGINE_VERSION}
        tfm = apply(tfm_state, (timesfm_forecasts or {}).get(symbol), timesfm_status)
        if tfm.warning and tfm_state.enabled and timesfm_status == "ready":
            result.warnings.append(f"{symbol}: {tfm.warning}")
        feats["timesfm"] = tfm.as_dict(tfm_state)
        shap = None
        if ranker is not None and ranker.weight > 0:
            model_prob = ranker.predict_proba(feats)
            model_version = ranker.version
            confidence = ranker.weight * model_prob * 100 + (1 - ranker.weight) * r.score
            model_note = f"Ranking model {model_version}: {model_prob * 100:.0f}% chance of a gain."
            explain = getattr(ranker, "explain", None)
            shap = explain(feats) if explain else None
            if tfm.confidence_delta and tfm_state.mode == "feature":
                tfm = Effect(0.0, tfm.filtered, (tfm.note or "") + " (used as a model feature)", tfm.warning,
                             tfm.forecast)
                feats["timesfm"] = tfm.as_dict(tfm_state)
        confidence = max(0.0, min(99.0, confidence + tfm.confidence_delta))
        calibrated, calib_id = False, None
        if calibrator is not None and (cal := calibrator.calibrate(catalyst, confidence)) is not None:
            confidence, calib_id = cal
            calibrated = True

        skip = r.skip_reason or ("no_price_data" if f is None else None) or ("filtered_by_timesfm" if tfm.filtered
                                                                            else None)
        if skip is None and confidence < DISPLAY_MIN:
            skip = "below_display_threshold"
        displayed = skip is None
        cand = Candidate(symbol, catalyst, evs, round(confidence, 1), r, f, displayed, skip, tfm)
        result.candidates.append(cand)
        if f is None:
            continue   # no price -> no stop/entry -> cannot be stored as a signal row
        entry, stop, target, exp = levels(prior, f)
        ticker = session.get(Ticker, symbol)
        risk = list(r.risk_notes)
        if ticker is not None and ticker.market_cap is not None and float(ticker.market_cap) < 3e8:
            risk.append("Micro-cap: larger spreads and gaps.")
        row = dict(
            symbol=symbol, as_of_date=as_of_date, catalyst_type=catalyst, rule_id=prior.rule_id,
            rule_score=r.score, model_prob=model_prob, model_version=model_version, confidence=cand.confidence,
            calibrated=calibrated, calibration_id=calib_id, expected_return_pct=exp, expected_return_basis="prior",
            holding_days_min=prior.holding_days[0], holding_days_max=prior.holding_days[1],
            entry_ref_price=Decimal(str(round(entry, 4))), stop_price=Decimal(str(round(stop, 4))),
            target_price=Decimal(str(round(target, 4))),
            suggested_size_usd=Decimal(str(round(_suggested_size(session, entry, stop), 2))),
            risk_notes=risk, reason=_reason(prior, evs, r, cand.confidence, f, tfm, model_note), features=feats,
            shap=shap,
            status="active" if skip in (None, "below_display_threshold") else "invalidated", displayed=displayed,
        )
        if skip:
            row["features"] = {**feats, "skip_reason": skip}
        stmt = insert(Signal).values(row)
        sig_id = session.execute(stmt.on_conflict_do_update(
            constraint="uq_signals_symbol_type_date",
            set_={k: getattr(stmt.excluded, k) for k in row if k not in ("symbol", "as_of_date", "catalyst_type")},
        ).returning(Signal.id)).scalar_one()
        for e in evs:
            session.execute(insert(SignalEvent).values(signal_id=sig_id, event_id=e.id).on_conflict_do_nothing())
        session.flush()
        cand.signal = session.get(Signal, sig_id)
        session.refresh(cand.signal)
    return result
