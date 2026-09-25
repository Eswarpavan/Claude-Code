"""Scheduled jobs and the on-open refresh. Plain functions, so they are testable and
can run from Celery (worker/celery_app.py), the CLI, or the API's refresh endpoint.

Every job is idempotent and takes a lock, so overlapping runs (scheduler + a
user's "refresh now") never double-trade or double-email.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from catalystedge.adapters.news.base import NewsAdapter
from catalystedge.adapters.news.registry import build_news_adapters
from catalystedge.adapters.prices.finnhub_quote import FinnhubQuote
from catalystedge.clock import Clock, SystemClock
from catalystedge.config import Settings, get_settings
from catalystedge.core import calendar
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import KV, InMemoryKV, RedisKV
from catalystedge.db.models import (
    Event,
    PaperOrder,
    PaperPosition,
    PriceDaily,
    RefreshRun,
    Signal,
    SignalEvent,
    Source,
    SourceRun,
    Ticker,
)
from catalystedge.db.session import make_engine
from catalystedge.ml.registry import ModelRegistry
from catalystedge.notify.dispatcher import dispatch, queue_digest, queue_high_confidence, queue_paper_buy
from catalystedge.notify.providers import build_provider
from catalystedge.outcomes.tracker import update_outcomes
from catalystedge.paper import engine as paper
from catalystedge.paper.metrics import mark_to_market, performance
from catalystedge.pipeline.collect import collect_news
from catalystedge.pipeline.run import persist, process_items
from catalystedge.pipeline.store import seed_sources, seed_tickers
from catalystedge.pipeline.ticker_link import Universe
from catalystedge.pipeline.window import NEWS_WINDOW, purge_expired_news
from catalystedge.prices import PriceService, build_price_adapters, store_bars
from catalystedge.reference import load_universe

log = logging.getLogger("catalystedge.jobs")
PRICE_HISTORY_SESSIONS = 260        # about a year: enough for momentum/volatility features
MAX_PRICE_SYMBOLS_PER_RUN = 40      # Tiingo free: 45/h budget
BENCHMARKS = ("SPY",)
WATCHLIST_MAX = 30                  # Finnhub company-news calls per poll (60/min limit, ~1.2 s spacing)


@dataclass
class Context:
    settings: Settings
    session_factory: Callable[[], Session]
    http: HttpClient
    kv: KV
    clock: Clock
    _universe: Universe | None = field(default=None, repr=False)
    _model: Any = field(default=None, repr=False)

    def universe(self) -> Universe:
        if self._universe is None:
            self._universe, _ = load_universe(self.settings, self.http)
        return self._universe

    def sentiment_model(self):
        if self._model is None:
            self._model = ModelRegistry(self.settings).sentiment()
        return self._model

    def news_adapters(self) -> list[NewsAdapter]:
        return build_news_adapters(self.settings, self.http)


def build_context(settings: Settings | None = None, clock: Clock | None = None) -> Context:
    settings = settings or get_settings()
    clock = clock or SystemClock()
    kv: KV = RedisKV(settings.redis_url) if settings.redis_url else InMemoryKV(clock)
    from catalystedge.core.ratelimit import FileSlots, RedisSlots

    http = HttpClient(kv=kv, clock=clock, slots=RedisSlots(settings.redis_url) if settings.redis_url else FileSlots())
    for secret in (settings.finnhub_api_key, settings.marketaux_api_key, settings.alphavantage_api_key,
                   settings.tiingo_api_key):
        http.register_secret(secret)
    engine = make_engine(settings.database_url)
    return Context(settings, sessionmaker(engine, expire_on_commit=False), http, kv, clock)


@contextlib.contextmanager
def job_lock(kv: KV, name: str, ttl_s: int = 1800) -> Iterator[bool]:
    """Yields True if this caller holds the lock; False if another run is in progress."""
    key = f"lock:{name}"
    got = kv.set_if_absent(key, b"1", ttl_s)
    try:
        yield got
    finally:
        if got:
            kv.delete(key)


HEARTBEAT_KEY = "scheduler:heartbeat"


def record_heartbeat(kv: KV, task: str, now: dt.datetime, ok: bool = True) -> None:
    """Written by the worker after every scheduled task (shared Redis/Upstash), read by /health."""
    kv.set(HEARTBEAT_KEY, json.dumps({"task": task, "at": now.isoformat(), "ok": ok}).encode(), 7 * 24 * 3600)


def read_heartbeat(kv: KV, now: dt.datetime, stale_after_min: int = 75) -> dict:
    raw = kv.get(HEARTBEAT_KEY)
    if raw is None:
        return {"status": "no heartbeat yet", "note": "the worker has not finished a scheduled task yet"}
    hb = json.loads(raw)
    age = (now - dt.datetime.fromisoformat(hb["at"])).total_seconds() / 60
    return {"status": "ok" if age <= stale_after_min else "stale", "last_task": hb["task"], "last_run_at": hb["at"],
            "minutes_ago": round(age, 1), "last_task_ok": hb.get("ok", True)}


@contextlib.contextmanager
def _session(ctx: Context) -> Iterator[Session]:
    s = ctx.session_factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ----------------------------------------------------------------------------- source health


def _record_source(session: Session, ctx: Context, key: str, status: str, fetched: int = 0, new: int = 0,
                   calls: int = 0, error: str | None = None, refresh_id: Any = None,
                   started: dt.datetime | None = None) -> None:
    now = ctx.clock.now()
    src = session.get(Source, key)
    if src is None:
        seed_sources(session)
        src = session.get(Source, key)
    if src is not None:
        if status == "ok":
            src.status, src.last_success_at, src.last_error = "ok", now, None
        elif status == "disabled":
            src.status = "disabled"
            src.last_error = error
        else:
            src.status, src.last_error_at, src.last_error = "failed", now, error
        with contextlib.suppress(KeyError):
            src.budget_used_today = ctx.http.budget_used(key)
    session.add(SourceRun(refresh_id=refresh_id, source_key=key, started_at=started or now, finished_at=now,
                          status=status, items_fetched=fetched, items_new=new, http_calls=calls,
                          error=error[:500] if error else None))


# ----------------------------------------------------------------------------- news


def job_news(ctx: Context, refresh_id: Any = None, progress: Callable[[str, str], None] | None = None) -> dict:
    """Poll every news adapter (one at a time, so each reports its own status), then run
    the pipeline over the 48-hour window and store events. Purges expired news."""
    now = ctx.clock.now()
    watch = _watchlist(ctx)
    all_items = []
    per_source = {}
    for adapter in ctx.news_adapters():
        started = ctx.clock.now()
        if progress:
            progress(adapter.source_key, "running")
        items, (report,) = collect_news([adapter], now, watch)
        all_items += items
        per_source[adapter.source_key] = report
        with _session(ctx) as s:
            _record_source(s, ctx, adapter.source_key, "ok" if report.status == "ok" else
                           "disabled" if report.status == "disabled" else "failed", fetched=report.kept,
                           calls=report.http_calls, error=report.error, refresh_id=refresh_id, started=started)
        if progress:
            progress(adapter.source_key, report.status)
    processed = process_items(all_items, ctx.universe(), ctx.sentiment_model(), now)
    with _session(ctx) as s:
        seed_sources(s)
        _ensure_tickers(s, ctx, processed)
        new_events = persist(s, processed, now)
        purged = purge_expired_news(s, now)
    return {"headlines": len(all_items), "stories": len(processed), "new_events": new_events,
            "purged_expired": purged, "sources": {k: v.status for k, v in per_source.items()}}


def _ensure_tickers(session: Session, ctx: Context, processed) -> None:
    """Only the tickers that were actually linked need rows (FKs for links/events)."""
    needed = {m.symbol for p in processed for m in p.link.mentions}
    missing = needed - set(session.scalars(select(Ticker.symbol).where(Ticker.symbol.in_(needed))))
    if missing:
        u = ctx.universe()
        seed_tickers(session, Universe([u.by_symbol[s] for s in missing if s in u.by_symbol]))
        session.flush()


def _watchlist(ctx: Context) -> list[str]:
    """Per-company news for what we hold or are about to trade, plus recent positive-event tickers."""
    with _session(ctx) as s:
        held = set(s.scalars(select(PaperPosition.symbol).where(PaperPosition.status == "open")))
        pending = set(s.scalars(select(PaperOrder.symbol).where(PaperOrder.status == "pending")))
        since = ctx.clock.now() - NEWS_WINDOW
        recent = list(s.scalars(select(Event.symbol).where(Event.polarity == "positive", Event.event_type != "other",
                                                           Event.available_at >= since).distinct().limit(12)))
    base = [x.strip().upper() for x in ctx.settings.news_watchlist.split(",") if x.strip()]
    return list(dict.fromkeys([*sorted(held | pending), *recent, *base]))[:WATCHLIST_MAX]


# ----------------------------------------------------------------------------- SEC / FDA / earnings events (hook)


def job_events(ctx: Context, refresh_id: Any = None) -> dict:
    """Primary-event ingestion is owned by the signal-engine work (docs/SESSION_SPLIT.md).
    This calls it when present: catalystedge.events.jobs.poll_events(session, http, settings, now)."""
    try:
        from catalystedge.events.jobs import poll_events  # type: ignore[import-not-found]
    except ImportError:
        return {"status": "not available yet"}
    with _session(ctx) as s:
        return poll_events(s, ctx.http, ctx.settings, ctx.clock.now(), universe=ctx.universe(),
                           model=ctx.sentiment_model())


# ----------------------------------------------------------------------------- prices


def symbols_needing_prices(session: Session, now: dt.datetime) -> list[str]:
    held = set(session.scalars(select(PaperPosition.symbol).where(PaperPosition.status == "open")))
    pending = set(session.scalars(select(PaperOrder.symbol).where(PaperOrder.status == "pending")))
    since = now - NEWS_WINDOW
    events = list(session.scalars(
        select(Event.symbol).where(Event.polarity == "positive", Event.event_type != "other",
                                   Event.available_at >= since)
        .group_by(Event.symbol).order_by(func.max(Event.materiality).desc().nulls_last())))
    recent_signals = set(session.scalars(select(Signal.symbol).where(
        Signal.as_of_date >= (now - dt.timedelta(days=20)).date())))
    ordered = list(BENCHMARKS) + sorted(held | pending) + sorted(recent_signals - held - pending) + events
    return list(dict.fromkeys(ordered))[:MAX_PRICE_SYMBOLS_PER_RUN]


def job_prices(ctx: Context, refresh_id: Any = None, symbols: list[str] | None = None) -> dict:
    """Fetch only the missing daily bars per symbol (incremental), then update 20-day ADV."""
    now = ctx.clock.now()
    svc = PriceService(build_price_adapters(ctx.settings, ctx.http), ctx.clock)
    last_done = calendar.last_completed_session(now)
    stored, failures, used = 0, {}, {}
    with _session(ctx) as s:
        symbols = symbols or symbols_needing_prices(s, now)
    for sym in symbols:
        with _session(ctx) as s:
            last = s.scalar(select(func.max(PriceDaily.date)).where(PriceDaily.symbol == sym))
        if last is not None and last >= last_done:
            continue
        start = (calendar.next_session(last) if last else
                 last_done - dt.timedelta(days=int(PRICE_HISTORY_SESSIONS * 1.45)))
        res = svc.daily(sym, start, last_done)
        if not res.bars:
            failures[sym] = res.errors
            continue
        used[res.source] = used.get(res.source, 0) + 1
        with _session(ctx) as s:
            if s.get(Ticker, sym) is None:
                s.add(Ticker(symbol=sym, name=sym, aliases=[]))
                s.flush()
            stored += store_bars(s, res.bars, now)
            _update_adv(s, sym)
    with _session(ctx) as s:
        for src in ("tiingo_eod", "yahoo_eod", "stooq_eod"):
            if src in used:
                _record_source(s, ctx, src, "ok", fetched=used[src], calls=used[src], refresh_id=refresh_id)
        if failures and not used:
            _record_source(s, ctx, "tiingo_eod", "failed", error=json.dumps(failures)[:500], refresh_id=refresh_id)
    return {"symbols": len(symbols), "bars_stored": stored, "sources": used, "failed": sorted(failures)}


def _update_adv(session: Session, symbol: str) -> None:
    rows = session.execute(select(PriceDaily.close, PriceDaily.volume).where(PriceDaily.symbol == symbol)
                           .order_by(PriceDaily.date.desc()).limit(20)).all()
    if rows:
        t = session.get(Ticker, symbol)
        if t is not None:
            t.adv20_usd = Decimal(str(round(sum(float(c) * v for c, v in rows) / len(rows), 2)))


def _closes(session: Session, day: dt.date) -> dict[str, float]:
    return {sym: float(c) for sym, c in session.execute(
        select(PriceDaily.symbol, PriceDaily.close).where(PriceDaily.date == day))}


# ----------------------------------------------------------------------------- signals (hook)


def job_signals(ctx: Context) -> dict:
    """Signals are generated by catalystedge.signals.engine (docs/SESSION_SPLIT.md). After it runs,
    queue 80%+ alerts for newly displayed signals."""
    try:
        from catalystedge.signals.engine import generate_signals  # type: ignore[import-not-found]
    except ImportError:
        return {"status": "signal engine not available yet"}
    def ensure_prices(symbols: list[str]) -> None:
        job_prices(ctx, symbols=list(dict.fromkeys([*BENCHMARKS, *symbols]))[:MAX_PRICE_SYMBOLS_PER_RUN])

    from catalystedge.ml.timeseries import engine_kwargs

    with _session(ctx) as s:
        now = ctx.clock.now()
        result = generate_signals(s, now, ensure_prices=ensure_prices,
                                  **engine_kwargs(s, now, Path(ctx.settings.models_dir)))
        shown = [c.signal for c in result.displayed if c.signal is not None]
        queued = sum(queue_high_confidence(s, sig) for sig in shown)
    llm = job_llm_notes(ctx, [sig.id for sig in shown])
    if queued:
        job_email(ctx)      # send 80%+ alerts now instead of waiting for the next scheduled email run
    return {"as_of": result.as_of_date.isoformat(), "candidates": len(result.candidates), "displayed": len(shown),
            "alerts_queued": queued, "warnings": result.warnings, "llm": llm}


def job_llm_notes(ctx: Context, signal_ids: list[int]) -> dict:
    """Optional Ollama explanations for displayed signals, in its own transaction after scoring and
    alerts, so an unreachable or failing LLM can never affect them."""
    from catalystedge.ml.llm import annotate_signals, build_explainer

    explainer = build_explainer(ctx.settings)
    if explainer is None or not signal_ids:
        return {"status": "off" if explainer is None else "nothing to explain"}
    try:
        with _session(ctx) as s:
            sigs = list(s.scalars(select(Signal).where(Signal.id.in_(signal_ids))))

            def headlines(sig: Signal) -> list[str]:
                return list(s.scalars(select(Event.headline).join(SignalEvent, SignalEvent.event_id == Event.id)
                                      .where(SignalEvent.signal_id == sig.id).order_by(Event.available_at.desc())))

            def company(sym: str) -> str | None:
                t = s.get(Ticker, sym)
                return t.name if t else None

            return annotate_signals(sigs, explainer, ctx.kv, company_of=company, headlines_of=headlines)
    except Exception as e:
        log.warning("llm notes skipped: %s", type(e).__name__)
        return {"status": f"error: {type(e).__name__}"}
    finally:
        explainer.close()


def job_timesfm(ctx: Context) -> dict:
    """Background TimesFM forecasts for tickers with a positive event (only when switched on).
    Runs before the EOD signal job; the signal job reads the cache and never waits for it."""
    from catalystedge.ml.timeseries import run_timesfm_job

    with _session(ctx) as s:
        return run_timesfm_job(s, Path(ctx.settings.models_dir), ctx.clock.now())


# ----------------------------------------------------------------------------- paper trading


def job_paper_decide(ctx: Context) -> dict:
    """After the close: outcomes, exits on the close, buy decisions for the next open, marking."""
    now = ctx.clock.now()
    day = calendar.last_completed_session(now)
    with _session(ctx) as s:
        acct = paper.get_account(s)
        closes = _closes(s, day)
        exits = paper.evaluate_exits(s, acct, day, closes)
        signals = list(s.scalars(select(Signal).where(Signal.as_of_date == day, Signal.displayed.is_(True),
                                                      Signal.status == "active")))
        candidates = paper.evaluate_candidates(s, acct, day, signals, closes)

        def spy_open(d: dt.date) -> float | None:
            bar = s.get(PriceDaily, ("SPY", d))
            return float(bar.open) if bar is not None and bar.available_at <= now else None

        mark_to_market(s, acct, day, closes, spy_open)
        outcomes = update_outcomes(s, now)
        if ctx.settings.daily_digest:
            perf = performance(s, acct)
            queue_digest(s, day, [f"Equity ${perf.equity:.2f} ({perf.total_return_pct:+.2f}%), "
                                  f"S&P 500 {perf.spy_return_pct if perf.spy_return_pct is not None else 'n/a'}%",
                                  f"Signals shown today: {len(signals)}; exits scheduled: {len(exits)}",
                                  "All confidence is UNCALIBRATED until the evidence exists."])
    return {"decision_date": day.isoformat(), "exits": len(exits), "candidates": len(candidates),
            "bought": sum(1 for c in candidates if c.decision == "bought"), "outcomes_written": outcomes}


def job_paper_execute(ctx: Context) -> dict:
    """Fill orders due today at today's official open: Finnhub's quote open during the day,
    or the stored daily bar's open once it exists. Never any other price."""
    now = ctx.clock.now()
    today = now.astimezone(calendar._cal().tz).date()
    if not calendar.is_session(today) or now < calendar.session_open(today) + dt.timedelta(minutes=5):
        day = calendar.last_completed_session(now)
    else:
        day = today
    quotes = FinnhubQuote(ctx.http, ctx.settings.finnhub_api_key)

    with _session(ctx) as s:
        def open_price(symbol: str, d: dt.date) -> float | None:
            bar = s.get(PriceDaily, (symbol, d))
            if bar is not None:
                return float(bar.open)
            if d == today and calendar.is_session(today) and now >= calendar.session_open(today):
                q = quotes.quote(symbol)
                if q and q.open and q.as_of and q.as_of.astimezone(calendar._cal().tz).date() == today:
                    return q.open
            return None

        def adv(symbol: str) -> float | None:
            t = s.get(Ticker, symbol)
            return float(t.adv20_usd) if t is not None and t.adv20_usd is not None else None

        acct = paper.get_account(s)
        report = paper.execute_orders(s, acct, day, open_price, adv,
                                      last_completed=calendar.last_completed_session(now))
        emails = sum(queue_paper_buy(s, o) for o in report.filled if o.side == "buy")
    if emails:
        job_email(ctx)
    return {"day": day.isoformat(), "filled": len(report.filled), "waiting": len(report.waiting),
            "cancelled": len(report.cancelled), "buy_emails_queued": emails}


# ----------------------------------------------------------------------------- email & retention


def job_email(ctx: Context) -> dict:
    with _session(ctx) as s:
        r = dispatch(s, build_provider(ctx.settings), ctx.settings.alert_email_to, ctx.clock.now(),
                     daily_cap=ctx.settings.email_daily_cap)
    return {"sent": r.sent, "failed": r.failed, "deferred": r.deferred, "note": r.skipped_reason}


def job_retention(ctx: Context) -> dict:
    with _session(ctx) as s:
        return {"purged_expired_news": purge_expired_news(s, ctx.clock.now())}


# ----------------------------------------------------------------------------- on-open refresh


REFRESH_STEPS = ("news", "events", "prices", "signals", "portfolio")


def start_refresh(ctx: Context, trigger: str = "open") -> tuple[Any, bool]:
    """Returns (refresh_id, started). Respects a cooldown and never runs two refreshes at once:
    within the cooldown the latest refresh id is returned with started=False."""
    cooldown_key = "refresh:cooldown"
    with _session(ctx) as s:
        running = s.scalar(select(RefreshRun.id).where(
            RefreshRun.status == "running",
            RefreshRun.started_at >= ctx.clock.now() - dt.timedelta(minutes=15))
            .order_by(RefreshRun.started_at.desc()).limit(1))
    if running is not None:          # one refresh at a time: follow the one already in progress
        return running, False
    if not ctx.kv.set_if_absent(cooldown_key, b"1", ctx.settings.refresh_cooldown_s):
        with _session(ctx) as s:
            latest = s.scalar(select(RefreshRun.id).order_by(RefreshRun.started_at.desc()).limit(1))
        return latest, False
    rid = uuid.uuid4()
    with _session(ctx) as s:
        s.add(RefreshRun(id=rid, trigger=trigger, started_at=ctx.clock.now(), status="running", progress_pct=0))
    return rid, True


def run_refresh(ctx: Context, refresh_id: Any) -> dict:
    """News, events, prices, signals, portfolio marking, recording progress as it goes.
    A slow or failed step is recorded and the refresh continues with the next one."""
    results: dict[str, Any] = {}
    steps = {"news": lambda: job_news(ctx, refresh_id), "events": lambda: job_events(ctx, refresh_id),
             "prices": lambda: job_prices(ctx, refresh_id), "signals": lambda: job_signals(ctx),
             "portfolio": lambda: _refresh_portfolio(ctx)}
    with job_lock(ctx.kv, "refresh", ttl_s=900) as got:
        if not got:
            _finish_refresh(ctx, refresh_id, "skipped", {"note": "another refresh is running"})
            return {"status": "skipped"}
        for i, name in enumerate(REFRESH_STEPS):
            try:
                results[name] = steps[name]()
            except Exception as e:  # never let one step block the others
                log.exception("refresh step %s failed", name)
                results[name] = {"error": ctx.http.redact(f"{type(e).__name__}: {e}")[:300]}
            with _session(ctx) as s:
                run = s.get(RefreshRun, refresh_id)
                if run is not None:
                    run.progress_pct = round(100 * (i + 1) / len(REFRESH_STEPS), 1)
        _finish_refresh(ctx, refresh_id, "done", results)
    return results


def _refresh_portfolio(ctx: Context) -> dict:
    """Mark the account with the latest closes (no trading decisions outside the scheduled jobs)."""
    now = ctx.clock.now()
    day = calendar.last_completed_session(now)
    with _session(ctx) as s:
        acct = paper.get_account(s)
        closes = _closes(s, day)

        def spy_open(d: dt.date) -> float | None:
            bar = s.get(PriceDaily, ("SPY", d))
            return float(bar.open) if bar is not None and bar.available_at <= now else None

        mark_to_market(s, acct, day, closes, spy_open)
        written = update_outcomes(s, now)
    return {"marked": day.isoformat(), "outcomes_written": written}


def _finish_refresh(ctx: Context, refresh_id: Any, status: str, results: dict) -> None:
    with _session(ctx) as s:
        run = s.get(RefreshRun, refresh_id)
        if run is not None:
            run.status = status if status in ("done", "skipped") else "failed"
            run.finished_at = ctx.clock.now()
            run.progress_pct = 100.0
    ctx.kv.set(f"refresh:result:{refresh_id}", json.dumps(results, default=str).encode(), 86400)
