"""FastAPI app: everything the dashboard needs.

Hard rules visible here:
  * /api/signals and /api/news return POSITIVE items only (rule 3), with confidence >= 65 (rule 5).
  * Every confidence carries `calibrated` and a label ("UNCALIBRATED" until evidence exists, rule 6).
  * Buys and sells go through the paper engine: next-open fills only, no same-day sells (rules 2, 7).
Run: uvicorn catalystedge.api.main:app
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from catalystedge.api.auth import check_password, issue_token, require_auth
from catalystedge.clock import SystemClock
from catalystedge.config import Settings, get_settings
from catalystedge.core import calendar
from catalystedge.db.models import (
    BacktestRun,
    BuyCandidate,
    CalibrationSnapshot,
    EquitySnapshot,
    Event,
    Notification,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PriceDaily,
    RefreshRun,
    Signal,
    SignalEvent,
    SignalOutcome,
    Source,
    SourceRun,
    Ticker,
)
from catalystedge.db.session import make_engine
from catalystedge.ml.registry import ModelRegistry
from catalystedge.notify.providers import build_provider
from catalystedge.paper import engine as paper
from catalystedge.paper.metrics import performance
from catalystedge.paper.settings import PaperSettings
from catalystedge.pipeline.window import NEWS_WINDOW
from catalystedge.sources import SOURCES

DISPLAY_MIN = 65.0
HIGHLIGHT = 80.0
DISCLAIMER = "Not financial advice. CatalystEdge is a research and paper-trading tool."


# ----------------------------------------------------------------------------- app state & deps


class State:
    settings: Settings
    session_factory: sessionmaker
    clock = SystemClock()
    ctx = None           # jobs.Context, built lazily (only needed for refresh)


state = State()


def create_app(settings: Settings | None = None, session_factory: sessionmaker | None = None) -> FastAPI:
    state.settings = settings or get_settings()
    state.session_factory = session_factory or sessionmaker(make_engine(state.settings.database_url),
                                                            expire_on_commit=False)
    app = FastAPI(title="CatalystEdge API", version="0.1.0", description=DISCLAIMER)
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in state.settings.cors_origins.split(",")],
                       allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type"])
    app.include_router(_router())
    return app


def db() -> Iterator[Session]:
    s = state.session_factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def auth(request: Request) -> None:
    require_auth(request, state.settings)


# ----------------------------------------------------------------------------- serializers


def confidence_label(sig: Signal) -> str:
    return "CALIBRATED" if sig.calibrated else "UNCALIBRATED"


def _f(x: Any) -> float | None:
    return float(x) if x is not None else None


def signal_json(s: Session, sig: Signal, now: dt.datetime, detail: bool = False) -> dict:
    t = s.get(Ticker, sig.symbol)
    events = s.scalars(select(Event).join(SignalEvent, SignalEvent.event_id == Event.id)
                       .where(SignalEvent.signal_id == sig.id).order_by(Event.available_at.desc())).all()
    latest = events[0].available_at if events else None
    senti = [e.sentiment for e in events if e.sentiment]
    out = {
        "id": sig.id, "symbol": sig.symbol, "company": t.name if t else sig.symbol,
        "sector": t.sector if t else None, "market_cap": _f(t.market_cap) if t else None,
        "as_of_date": sig.as_of_date.isoformat(), "catalyst": sig.catalyst_type, "rule_id": sig.rule_id,
        "confidence": round(sig.confidence, 1), "calibrated": sig.calibrated, "confidence_label": confidence_label(sig),
        "highlight": sig.confidence >= HIGHLIGHT, "expected_return_pct": round(sig.expected_return_pct, 2),
        "expected_return_basis": sig.expected_return_basis,
        "holding_days": [sig.holding_days_min, sig.holding_days_max], "entry_ref_price": _f(sig.entry_ref_price),
        "stop_price": _f(sig.stop_price), "target_price": _f(sig.target_price),
        "suggested_size_usd": _f(sig.suggested_size_usd), "risk_notes": list(sig.risk_notes or []),
        "reason": sig.reason, "status": sig.status,
        "news_at": latest.isoformat() if latest else None,
        "hours_since_news": round((now - latest).total_seconds() / 3600, 1) if latest else None,
        "headlines": [{"headline": e.headline, "url": e.url, "source": e.source_key, "at": e.available_at.isoformat(),
                       "event_type": e.event_type, "strength": e.strength} for e in events[:5]],
        "sentiment": {k: round(sum(x.get(k, 0) for x in senti) / len(senti), 3) for k in ("pos", "neu", "neg")}
        if senti else None,
        "sentiment_model": senti[0].get("model") if senti else None,
        "model_disagrees": bool((sig.features or {}).get("model_disagrees")),
    }
    if detail:
        out["features"] = sig.features
        out["shap"] = sig.shap
    return out


# ----------------------------------------------------------------------------- routes


class LoginBody(BaseModel):
    password: str


class BuyBody(BaseModel):
    signal_id: int


class SellBody(BaseModel):
    position_id: int


class SettingsBody(BaseModel):
    auto_buy: bool | None = None
    auto_buy_threshold: float | None = Field(None, ge=65, le=100)
    risk_per_trade: float | None = Field(None, gt=0, le=0.05)
    max_position_pct: float | None = Field(None, gt=0, le=0.25)
    max_open_positions: int | None = Field(None, ge=1, le=10)
    cash_floor_pct: float | None = Field(None, ge=0, le=0.5)
    time_stop_sessions: int | None = Field(None, ge=2, le=30)


def _router():
    from fastapi import APIRouter

    r = APIRouter()

    @r.get("/health")
    def health() -> dict:
        out: dict[str, Any] = {"status": "ok", "profile": state.settings.profile, "disclaimer": DISCLAIMER}
        try:
            with state.session_factory() as s:
                s.execute(text("SELECT 1"))
            out["database"] = "ok"
        except Exception as e:
            out["status"], out["database"] = "degraded", f"error: {type(e).__name__}"
        out["models"] = [m.__dict__ for m in ModelRegistry(state.settings).report()]
        return out

    @r.post("/api/login")
    def login(body: LoginBody, request: Request) -> dict:
        if not state.settings.app_password:
            return {"token": None, "auth": "disabled (local profile)"}
        ip = request.client.host if request.client else "?"
        key = f"login:{ip}:{int(dt.datetime.now(dt.UTC).timestamp() // 60)}"
        _attempts[key] += 1
        if _attempts[key] > 5:
            raise HTTPException(429, "too many attempts; wait a minute")
        if not check_password(state.settings, body.password):
            raise HTTPException(401, "wrong password")
        return {"token": issue_token(state.settings)}

    @r.get("/api/overview", dependencies=[Depends(auth)])
    def overview(s: Session = Depends(db)) -> dict:
        acct = paper.get_account(s)
        gate = paper.auto_buy_gate(s, acct)
        last = s.scalar(select(RefreshRun).order_by(RefreshRun.started_at.desc()).limit(1))
        return {
            "disclaimer": DISCLAIMER, "data_mode": state.settings.data_mode,
            "calibration": _calibration_status(s, gate),
            "auto_buy": {"allowed": gate.allowed, "reason": gate.reason, "closed_trades": gate.closed_trades,
                         "required_closed_trades": paper.settings_of(acct).auto_buy_min_closed_trades},
            "last_refresh": {"id": str(last.id), "status": last.status, "started_at": last.started_at.isoformat(),
                             "progress_pct": last.progress_pct} if last else None,
            "as_of_session": calendar.last_completed_session(state.clock.now()).isoformat(),
        }

    @r.get("/api/signals", dependencies=[Depends(auth)])
    def signals(s: Session = Depends(db), min_confidence: float = Query(DISPLAY_MIN, ge=0, le=100),
                sector: str | None = None, catalyst: str | None = None, cap_min: float | None = None,
                cap_max: float | None = None) -> dict:
        now = state.clock.now()
        latest = s.scalar(select(func.max(Signal.as_of_date)).where(Signal.displayed.is_(True)))
        q = select(Signal).where(Signal.displayed.is_(True), Signal.status == "active",
                                 Signal.confidence >= max(DISPLAY_MIN, min_confidence))
        q = q.where(Signal.as_of_date == latest) if latest else q.where(False)
        if catalyst:
            q = q.where(Signal.catalyst_type == catalyst)
        if sector or cap_min is not None or cap_max is not None:
            q = q.join(Ticker, Ticker.symbol == Signal.symbol)
            if sector:
                q = q.where(Ticker.sector == sector)
            if cap_min is not None:
                q = q.where(Ticker.market_cap >= cap_min)
            if cap_max is not None:
                q = q.where(Ticker.market_cap <= cap_max)
        rows = s.scalars(q.order_by(Signal.confidence.desc(), Signal.expected_return_pct.desc())).all()
        sectors = sorted(x for x in s.scalars(select(Ticker.sector).where(Ticker.sector.is_not(None)).distinct()))
        return {"as_of_date": latest.isoformat() if latest else None, "signals": [signal_json(s, x, now) for x in rows],
                "filters": {"sectors": sectors, "catalysts": sorted({x.catalyst_type for x in rows})},
                "note": "Positive signals only. Confidence is UNCALIBRATED until backtest or outcome evidence "
                        "supports it."}

    @r.get("/api/signals/{signal_id}", dependencies=[Depends(auth)])
    def signal_detail(signal_id: int, s: Session = Depends(db)) -> dict:
        sig = s.get(Signal, signal_id)
        if sig is None or not sig.displayed:
            raise HTTPException(404, "no such signal")
        return signal_json(s, sig, state.clock.now(), detail=True)

    @r.get("/api/news", dependencies=[Depends(auth)])
    def news(s: Session = Depends(db), limit: int = Query(100, le=500)) -> dict:
        now = state.clock.now()
        rows = s.scalars(select(Event).where(Event.polarity == "positive", Event.event_type != "other",
                                             Event.available_at >= now - NEWS_WINDOW)
                         .order_by(Event.available_at.desc()).limit(limit)).all()
        return {"window_hours": NEWS_WINDOW.total_seconds() / 3600, "items": [
            {"id": e.id, "symbol": e.symbol, "headline": e.headline, "url": e.url, "source": e.source_key,
             "at": e.available_at.isoformat(), "event_type": e.event_type, "strength": e.strength,
             "sentiment": e.sentiment, "reasons": list(e.reasons or [])} for e in rows]}

    @r.get("/api/portfolio", dependencies=[Depends(auth)])
    def portfolio(s: Session = Depends(db)) -> dict:
        acct = paper.get_account(s)
        day = calendar.last_completed_session(state.clock.now())
        closes = {sym: float(c) for sym, c in s.execute(
            select(PriceDaily.symbol, PriceDaily.close).where(PriceDaily.date == day))}

        def pos_json(p: PaperPosition) -> dict:
            last = closes.get(p.symbol)
            value = float(p.qty) * last if last else None
            return {"id": p.id, "symbol": p.symbol, "entry_date": p.entry_date.isoformat(), "qty": float(p.qty),
                    "cost_basis": float(p.cost_basis), "stop": float(p.stop_price), "target": float(p.target_price),
                    "time_stop_date": p.time_stop_date.isoformat(), "last_close": last, "value": value,
                    "unrealized_pnl": round(value - float(p.cost_basis), 2) if value is not None else None,
                    "status": p.status, "exit_date": p.exit_date.isoformat() if p.exit_date else None,
                    "exit_reason": p.exit_reason, "realized_pnl": _f(p.realized_pnl), "why": p.why,
                    "can_sell_from": calendar.next_session(p.entry_date).isoformat()}

        positions = s.scalars(select(PaperPosition).where(PaperPosition.account_id == acct.id)
                              .order_by(PaperPosition.entry_date.desc())).all()
        pending = s.scalars(select(PaperOrder).where(PaperOrder.account_id == acct.id,
                                                     PaperOrder.status == "pending")).all()
        since = day - dt.timedelta(days=14)
        cands = s.execute(select(BuyCandidate, Signal).join(Signal, Signal.id == BuyCandidate.signal_id)
                          .where(BuyCandidate.account_id == acct.id, BuyCandidate.decision_date >= since)
                          .order_by(BuyCandidate.decision_date.desc(), Signal.confidence.desc())).all()
        gate = paper.auto_buy_gate(s, acct)
        return {
            "cash": float(acct.cash), "start_cash": float(acct.start_cash),
            "performance": performance(s, acct).as_dict(),
            "auto_buy": {"allowed": gate.allowed, "reason": gate.reason},
            "open": [pos_json(p) for p in positions if p.status == "open"],
            "closed": [pos_json(p) for p in positions if p.status == "closed"],
            "pending_orders": [{"id": o.id, "symbol": o.symbol, "side": o.side, "execute_on": o.execute_on.isoformat(),
                                "notional_usd": _f(o.notional_usd), "qty": _f(o.qty), "origin": o.origin,
                                "exit_reason": o.exit_reason} for o in pending],
            "buy_options": [{"decision_date": c.decision_date.isoformat(), "symbol": sig.symbol,
                             "signal_id": sig.id, "confidence": sig.confidence, "calibrated": sig.calibrated,
                             "decision": c.decision, "skip_reasons": list(c.skip_reasons or []),
                             "skip_explained": [_explain(x) for x in (c.skip_reasons or [])], "details": c.details}
                            for c, sig in cands],
            "rules": "Buys fill at the next session's open. A position can be sold from the session after "
                     "its entry. Exits are decided on the close and filled at the next open.",
        }

    @r.post("/api/portfolio/buy", dependencies=[Depends(auth)])
    def buy(body: BuyBody, s: Session = Depends(db)) -> dict:
        sig = s.get(Signal, body.signal_id)
        if sig is None:
            raise HTTPException(404, "no such signal")
        try:
            order = paper.manual_buy(s, paper.get_account(s), sig, state.clock.now())
        except paper.OrderRejected as e:
            raise HTTPException(409, str(e)) from e
        return {"order_id": order.id, "execute_on": order.execute_on.isoformat(),
                "notional_usd": _f(order.notional_usd), "note": "fills at that session's official open"}

    @r.post("/api/portfolio/sell", dependencies=[Depends(auth)])
    def sell(body: SellBody, s: Session = Depends(db)) -> dict:
        try:
            order = paper.manual_sell(s, paper.get_account(s), body.position_id, state.clock.now())
        except (paper.OrderRejected, paper.SameDaySellError) as e:
            raise HTTPException(409, str(e)) from e
        return {"order_id": order.id, "execute_on": order.execute_on.isoformat(),
                "note": "sells fill at the next session's open (never the entry day)"}

    @r.get("/api/trades", dependencies=[Depends(auth)])
    def trades(s: Session = Depends(db), limit: int = Query(200, le=1000)) -> dict:
        rows = s.execute(select(PaperOrder, PaperFill).outerjoin(PaperFill, PaperFill.order_id == PaperOrder.id)
                         .order_by(PaperOrder.id.desc()).limit(limit)).all()
        return {"orders": [{"id": o.id, "symbol": o.symbol, "side": o.side, "origin": o.origin, "status": o.status,
                            "decision_date": o.decision_date.isoformat(), "execute_on": o.execute_on.isoformat(),
                            "exit_reason": o.exit_reason, "reject_reason": o.reject_reason,
                            "fill": {"date": f.fill_date.isoformat(), "raw_open": float(f.raw_open),
                                     "price": float(f.fill_price), "qty": float(f.qty),
                                     "half_spread_bps": f.spread_bps, "slippage_bps": f.slippage_bps} if f else None}
                           for o, f in rows]}

    @r.get("/api/performance", dependencies=[Depends(auth)])
    def perf(s: Session = Depends(db)) -> dict:
        acct = paper.get_account(s)
        snaps = s.scalars(select(EquitySnapshot).where(EquitySnapshot.account_id == acct.id)
                          .order_by(EquitySnapshot.date)).all()
        return {"stats": performance(s, acct).as_dict(), "curve": [
            {"date": x.date.isoformat(), "equity": float(x.equity),
             "spy": _f(x.spy_benchmark_equity)} for x in snaps]}

    @r.get("/api/calibration", dependencies=[Depends(auth)])
    def calibration(s: Session = Depends(db)) -> dict:
        acct = paper.get_account(s)
        gate = paper.auto_buy_gate(s, acct)
        snaps = s.scalars(select(CalibrationSnapshot).order_by(CalibrationSnapshot.created_at.desc()).limit(20)).all()
        runs = s.scalars(select(BacktestRun).order_by(BacktestRun.started_at.desc()).limit(5)).all()
        return {
            "status": _calibration_status(s, gate),
            "live_buckets": {h: live_buckets(s, h) for h in (1, 3, 10)},
            "snapshots": [{"id": x.id, "created_at": x.created_at.isoformat() if x.created_at else None,
                           "basis": x.basis, "event_family": x.event_family, "model_version": x.model_version, "n": x.n,
                           "buckets": x.buckets, "brier": x.brier, "ece": x.ece, "verdict": x.verdict}
                          for x in snaps],
            "backtests": [{"id": b.id, "started_at": b.started_at.isoformat(), "status": b.status,
                           "sources": b.data_sources, "families": b.event_families, "report": b.report} for b in runs],
            "news_backtest_note": "News APIs on free plans keep too little history to backtest news-only "
                                  "catalysts; those are calibrated from live outcomes only.",
        }

    @r.get("/api/sources", dependencies=[Depends(auth)])
    def sources(s: Session = Depends(db)) -> dict:
        rows = s.scalars(select(Source).order_by(Source.kind, Source.key)).all()
        latest: dict[str, SourceRun] = {}
        for run in s.scalars(select(SourceRun).order_by(SourceRun.id.desc()).limit(500)):
            latest.setdefault(run.source_key, run)
        now = state.clock.now()

        def health(src: Source) -> str:
            if not src.enabled and src.status == "disabled":
                return "disabled"
            if src.status == "failed":
                return "failed"
            spec = SOURCES.get(src.key)
            if src.last_success_at is None:
                return "stale"
            max_age = dt.timedelta(seconds=3 * (spec.poll_every_s if spec else 3600))
            return "ok" if now - src.last_success_at <= max(max_age, dt.timedelta(hours=1)) else "stale"

        return {"sources": [{
            "key": x.key, "kind": x.kind, "official": x.official, "fragile": x.fragile, "health": health(x),
            "status": x.status, "last_success_at": x.last_success_at.isoformat() if x.last_success_at else None,
            "last_error": x.last_error, "budget_used_today": x.budget_used_today, "daily_budget": x.daily_budget,
            "items_last_run": latest[x.key].items_fetched if x.key in latest else None,
            "note": SOURCES[x.key].note if x.key in SOURCES else ""} for x in rows],
            "models": [m.__dict__ for m in ModelRegistry(state.settings).report()],
            "email": {"provider": (p.name if (p := build_provider(state.settings)) else None),
                      "recipient_configured": bool(state.settings.alert_email_to)}}

    @r.get("/api/settings", dependencies=[Depends(auth)])
    def get_settings_(s: Session = Depends(db)) -> dict:
        acct = paper.get_account(s)
        gate = paper.auto_buy_gate(s, acct)
        return {"paper": paper.settings_of(acct).to_json(), "auto_buy_gate": gate.reason,
                "auto_buy_effective": gate.allowed}

    @r.put("/api/settings", dependencies=[Depends(auth)])
    def put_settings(body: SettingsBody, s: Session = Depends(db)) -> dict:
        acct = paper.get_account(s)
        current = paper.settings_of(acct).to_json()
        current.update({k: v for k, v in body.model_dump().items() if v is not None})
        acct.settings = {**(acct.settings or {}), **PaperSettings.from_json(current).to_json()}
        s.flush()
        gate = paper.auto_buy_gate(s, acct)
        return {"paper": paper.settings_of(acct).to_json(), "auto_buy_gate": gate.reason,
                "auto_buy_effective": gate.allowed}

    @r.get("/api/notifications", dependencies=[Depends(auth)])
    def notifications(s: Session = Depends(db), limit: int = Query(100, le=500)) -> dict:
        rows = s.scalars(select(Notification).order_by(Notification.id.desc()).limit(limit)).all()
        return {"log": [{"id": n.id, "kind": n.kind, "symbol": n.symbol, "status": n.status, "attempts": n.attempts,
                         "provider": n.provider, "created_at": n.created_at.isoformat() if n.created_at else None,
                         "sent_at": n.sent_at.isoformat() if n.sent_at else None, "last_error": n.last_error,
                         "subject": (n.payload or {}).get("subject")} for n in rows]}

    @r.post("/api/refresh", dependencies=[Depends(auth)])
    def refresh(trigger: str = Query("open", pattern="^(open|manual)$")) -> dict:
        from catalystedge import jobs

        ctx = _ctx()
        rid, started = jobs.start_refresh(ctx, trigger)
        if started:
            _dispatch_refresh(rid)
        return {"refresh_id": str(rid) if rid else None, "started": started,
                "cooldown_s": state.settings.refresh_cooldown_s}

    @r.get("/api/refresh/latest", dependencies=[Depends(auth)])
    def refresh_latest(s: Session = Depends(db)) -> dict:
        run = s.scalar(select(RefreshRun).order_by(RefreshRun.started_at.desc()).limit(1))
        return _refresh_json(s, run) if run else {"refresh": None}

    @r.get("/api/refresh/{refresh_id}/events", dependencies=[Depends(auth)])
    async def refresh_events(refresh_id: str) -> StreamingResponse:
        import uuid

        rid = uuid.UUID(refresh_id)

        async def stream():
            for _ in range(600):                      # up to 10 minutes
                with state.session_factory() as s:
                    run = s.get(RefreshRun, rid)
                    payload = _refresh_json(s, run) if run else {"refresh": None}
                yield f"data: {json.dumps(payload)}\n\n"
                if run is None or run.status != "running":
                    return
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return r


_attempts: defaultdict[str, int] = defaultdict(int)

SKIP_EXPLANATIONS = {
    "auto_buy_off": "Auto-buy is switched off (default). Use the Buy button to paper-buy manually.",
    "auto_buy_locked_uncalibrated": "Auto-buy is on but locked until a backtest calibration or enough "
                                    "closed paper trades exist.",
    "below_auto_buy_threshold": "Confidence is below your auto-buy threshold.",
    "already_held": "Already holding (or about to buy) this stock.",
    "max_positions": "Maximum number of open positions reached.",
    "price_below_minimum": "Share price below the minimum.",
    "illiquid": "Too little daily trading volume.",
    "cash_floor": "Buying would dip below the cash floor.",
    "below_min_order": "Order would be smaller than the minimum.",
    "invalid_stop": "The stop-loss is not below the entry price.",
}


def _explain(code: str) -> str:
    return SKIP_EXPLANATIONS.get(code, code.replace("_", " "))


def live_buckets(s: Session, horizon: int) -> list[dict]:
    """Hit rate per confidence bucket from real outcomes of DISPLAYED signals."""
    rows = s.execute(select(Signal.confidence, SignalOutcome.hit, SignalOutcome.return_pct)
                     .join(SignalOutcome, SignalOutcome.signal_id == Signal.id)
                     .where(Signal.displayed.is_(True), SignalOutcome.horizon_days == horizon)).all()
    out = []
    for lo in range(65, 100, 5):
        hi = lo + 5
        b = [r for r in rows if lo <= r[0] < hi or (hi == 100 and r[0] == 100)]
        out.append({"bucket": f"{lo}-{hi}", "n": len(b),
                    "avg_confidence": round(sum(r[0] for r in b) / len(b), 1) if b else None,
                    "hit_rate": round(100 * sum(1 for r in b if r[1]) / len(b), 1) if b else None,
                    "avg_return_pct": round(sum(r[2] for r in b) / len(b), 2) if b else None})
    return out


def _calibration_status(s: Session, gate) -> dict:
    n_outcomes = s.scalar(select(func.count()).select_from(SignalOutcome).join(Signal)
                          .where(Signal.displayed.is_(True), SignalOutcome.horizon_days == 10)) or 0
    best = s.scalar(select(CalibrationSnapshot).where(CalibrationSnapshot.basis == "backtest")
                    .order_by(CalibrationSnapshot.created_at.desc()).limit(1))
    calibrated = gate.has_backtest_calibration or gate.closed_trades >= 30
    if best is not None and best.verdict == "poor":
        message = "Calibration is POOR: confidence numbers do not match outcomes. Treat them as rankings only."
    elif calibrated:
        message = "Calibration evidence exists. Confidence is compared against outcomes on the Calibration page."
    else:
        message = ("UNCALIBRATED: no historical backtest calibration yet and fewer than 30 closed paper trades. "
                   "Confidence is a rule score, not a probability.")
    return {"label": "CALIBRATED" if calibrated else "UNCALIBRATED", "message": message,
            "closed_trades": gate.closed_trades, "required_closed_trades": 30,
            "backtest_calibration": best.verdict if best else None, "displayed_signal_outcomes_10d": n_outcomes}


def _ctx():
    if state.ctx is None:
        from catalystedge import jobs

        state.ctx = jobs.build_context(state.settings)
    return state.ctx


def _dispatch_refresh(rid) -> None:
    """Run in the Celery worker when one is configured, otherwise in a background thread."""
    if state.settings.redis_url:
        try:
            from catalystedge.worker.celery_app import refresh as refresh_task

            refresh_task.delay(str(rid))
            return
        except Exception:  # broker down: fall back to running here
            pass
    from catalystedge import jobs

    threading.Thread(target=jobs.run_refresh, args=(_ctx(), rid), daemon=True).start()


def _refresh_json(s: Session, run: RefreshRun) -> dict:
    runs = s.scalars(select(SourceRun).where(SourceRun.refresh_id == run.id).order_by(SourceRun.id)).all()
    return {"refresh": {"id": str(run.id), "trigger": run.trigger, "status": run.status,
                        "progress_pct": run.progress_pct, "started_at": run.started_at.isoformat(),
                        "finished_at": run.finished_at.isoformat() if run.finished_at else None},
            "sources": [{"key": x.source_key, "status": x.status, "items": x.items_fetched, "calls": x.http_calls,
                         "error": x.error, "at": x.finished_at.isoformat() if x.finished_at else None} for x in runs]}


app = create_app()   # uvicorn catalystedge.api.main:app  (the database is only contacted on first use)
